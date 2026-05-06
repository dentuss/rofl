"""Live trading bot for the Donchian-breakout strategy with F&G sentiment filter.

Default mode is PAPER (dry-run). Set MODE=live and provide API keys to trade
real money — only do this after you've reviewed the code and the risks.

Single-pair operation (default):
  python3 bot.py            # paper, ETH/USDT, sentiment filter on
  MODE=live python3 bot.py  # real orders, requires API keys

Multi-pair portfolio (recommended for steadier monthly returns):
  Run two instances with capital split. Each gets its own state file.
  See run_portfolio.sh for the canonical 70/30 ETH/SOL setup.
  Backtest: 70/30 portfolio cuts worst-month from -8% to -5% and MDD
  from -11% to -9% vs single-pair, with the same 83% months-won rate.

Configuration:
  Strategy params:  STRATEGY_PRESET=steady (default) | maxsharpe | aggressive
                    See PRESETS table below for what each one enables.
  Filters:          USE_SENTIMENT=1 (default), USE_HTF=auto-from-preset
  Risk:             RISK_PER_TRADE=0.015, MAX_LEVERAGE=3
  Allow shorts:     ALLOW_SHORT=1 (default)

Backtest evidence on 1y of ETH/USDT 1h (100 USDT, 0.06% fee, 2bp slip):
  aggressive (no filter):        +98.7%, Sharpe 2.32, MDD -12.2%, 75% mo win
  steady (sentiment, default):   +93.0%, Sharpe 2.41, MDD -10.8%, 83% mo win
  maxsharpe (sentiment + HTF):   +76.5%, Sharpe 2.68, MDD  -8.4%, 67% mo win
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from sentiment import fetch_fear_greed
from strategies import donchian_breakout
from strategies_enhanced import with_htf_trend_filter
from strategies_sentiment import donchian_skip_fear


# Strategy presets. Each row: (use_sentiment, use_htf_filter)
PRESETS = {
    "aggressive": (False, False),  # raw Donchian, biggest return, biggest DD
    "steady":     (True,  False),  # +sentiment, recommended default
    "maxsharpe":  (True,  True),   # +sentiment +HTF daily filter
}


# ----------------------------- Configuration --------------------------------
@dataclass
class BotConfig:
    exchange: str = os.getenv("EXCHANGE", "kucoin")
    symbol: str = os.getenv("SYMBOL", "ETH/USDT")
    timeframe: str = os.getenv("TIMEFRAME", "1h")
    mode: str = os.getenv("MODE", "paper")  # 'paper' | 'live'
    starting_equity: float = float(os.getenv("STARTING_EQUITY", "100"))
    risk_per_trade: float = float(os.getenv("RISK_PER_TRADE", "0.015"))
    max_leverage: float = float(os.getenv("MAX_LEVERAGE", "3"))
    sl_mult: float = float(os.getenv("SL_MULT", "2.5"))
    tp_mult: float = float(os.getenv("TP_MULT", "5.0"))
    max_bars_in_trade: int = int(os.getenv("MAX_BARS", "96"))
    allow_short: bool = os.getenv("ALLOW_SHORT", "1") == "1"
    state_file: str = os.getenv("STATE_FILE", "bot_state.json")
    log_file: str = os.getenv("LOG_FILE", "bot.log")
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "30"))
    # Strategy params (locked from 1y validation, sweep top-Sharpe config)
    entry_n: int = 20
    exit_n: int = 10
    adx_n: int = 14
    adx_min: float = 20.0
    atr_n: int = 14
    # Strategy preset selects which filters are active
    preset: str = os.getenv("STRATEGY_PRESET", "steady")
    # Sentiment filter (Crypto Fear & Greed Index)
    fear_threshold: float = float(os.getenv("FEAR_THRESHOLD", "25"))
    # HTF trend filter
    htf_rule: str = os.getenv("HTF_RULE", "1D")
    htf_ema_n: int = int(os.getenv("HTF_EMA_N", "50"))
    # Manual overrides (otherwise read from preset)
    use_sentiment_override: str = os.getenv("USE_SENTIMENT", "")
    use_htf_override: str = os.getenv("USE_HTF", "")

    @property
    def use_sentiment(self) -> bool:
        if self.use_sentiment_override:
            return self.use_sentiment_override == "1"
        return PRESETS[self.preset][0]

    @property
    def use_htf(self) -> bool:
        if self.use_htf_override:
            return self.use_htf_override == "1"
        return PRESETS[self.preset][1]


# ----------------------------- State ----------------------------------------
@dataclass
class Position:
    side: int                    # +1 long
    qty: float
    entry_px: float
    sl: float
    tp: float
    open_ts: int                 # unix seconds
    notional: float
    bars_open: int = 0
    order_id: Optional[str] = None


@dataclass
class State:
    equity: float
    last_bar_ts: int = 0         # last fully-closed bar we processed
    position: Optional[Position] = None
    realised_trades: int = 0
    realised_pnl: float = 0.0

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2, default=str)

    @classmethod
    def load(cls, path: str, starting_equity: float) -> "State":
        p = Path(path)
        if not p.exists():
            return cls(equity=starting_equity)
        d = json.loads(p.read_text())
        pos = d.get("position")
        return cls(
            equity=d["equity"],
            last_bar_ts=d.get("last_bar_ts", 0),
            position=Position(**pos) if pos else None,
            realised_trades=d.get("realised_trades", 0),
            realised_pnl=d.get("realised_pnl", 0.0),
        )

    def save(self, path: str) -> None:
        Path(path).write_text(self.to_json())


# ----------------------------- Logging --------------------------------------
def setup_logging(log_file: str) -> logging.Logger:
    log = logging.getLogger("bot")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log
    fmt = logging.Formatter("%(asctime)s %(levelname)s | %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


# ----------------------------- Exchange wrapper -----------------------------
class Exchange:
    """Thin wrapper around ccxt, with a paper-mode fallback."""
    TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}

    def __init__(self, cfg: BotConfig, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.paper = cfg.mode == "paper"
        try:
            import ccxt  # noqa: F401
        except ImportError:
            if not self.paper:
                raise RuntimeError("ccxt not installed; pip install ccxt")
            ccxt = None
        self._ccxt = None
        if not self.paper or os.getenv("FORCE_LIVE_DATA") == "1":
            import ccxt
            klass = getattr(ccxt, cfg.exchange)
            params = {
                "apiKey": os.getenv("API_KEY", ""),
                "secret": os.getenv("API_SECRET", ""),
                "password": os.getenv("API_PASSPHRASE", ""),
                "enableRateLimit": True,
            }
            self._ccxt = klass({k: v for k, v in params.items() if v})

    def fetch_recent(self, n: int = 250) -> pd.DataFrame:
        """Fetch last n closed bars (skips current forming bar in caller)."""
        if self._ccxt is not None:
            raw = self._ccxt.fetch_ohlcv(self.cfg.symbol, self.cfg.timeframe, limit=n)
            df = pd.DataFrame(raw, columns=["ts_ms", "open", "high", "low", "close", "volume"])
            df["dt"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
            df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
            return df
        # Paper mode without ccxt: pull through our KuCoin data layer
        from data import fetch_ohlcv
        sym = self.cfg.symbol.replace("/", "-")
        return fetch_ohlcv(sym, self.cfg.timeframe, days=10, use_cache=False).tail(n)

    def fetch_price(self) -> float:
        if self._ccxt is not None:
            t = self._ccxt.fetch_ticker(self.cfg.symbol)
            return float(t["last"])
        return float(self.fetch_recent(1)["close"].iloc[-1])

    def market_buy(self, qty: float) -> dict:
        if self.paper:
            px = self.fetch_price()
            return {"id": f"paper-{int(time.time())}", "price": px, "amount": qty}
        order = self._ccxt.create_market_buy_order(self.cfg.symbol, qty)
        return order

    def market_sell(self, qty: float) -> dict:
        if self.paper:
            px = self.fetch_price()
            return {"id": f"paper-{int(time.time())}", "price": px, "amount": qty}
        order = self._ccxt.create_market_sell_order(self.cfg.symbol, qty)
        return order


# ----------------------------- Bot core -------------------------------------
class Bot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.log = setup_logging(cfg.log_file)
        self.ex = Exchange(cfg, self.log)
        self.state = State.load(cfg.state_file, cfg.starting_equity)
        self._stop = False
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_):
        self.log.info("shutdown signal received")
        self._stop = True

    # --- signal generation (matches backtest exactly) -----------------------
    def compute_signal(self, df: pd.DataFrame) -> dict:
        donchian_kwargs = dict(
            entry_n=self.cfg.entry_n,
            exit_n=self.cfg.exit_n,
            adx_n=self.cfg.adx_n,
            adx_min=self.cfg.adx_min,
            atr_n=self.cfg.atr_n,
            sl_mult=self.cfg.sl_mult,
            tp_mult=self.cfg.tp_mult,
        )
        if self.cfg.use_sentiment:
            try:
                fng = fetch_fear_greed()
                sig = donchian_skip_fear(df, fng,
                                         fear_min=self.cfg.fear_threshold,
                                         **donchian_kwargs)
            except Exception as e:
                self.log.warning(f"sentiment fetch failed, falling back to baseline: {e}")
                sig = donchian_breakout(df, **donchian_kwargs)
        else:
            sig = donchian_breakout(df, **donchian_kwargs)
        if self.cfg.use_htf:
            sig = with_htf_trend_filter(df, sig,
                                        htf_rule=self.cfg.htf_rule,
                                        ema_n=self.cfg.htf_ema_n)
        # Use the last fully-closed bar's signal (no look-ahead)
        last = sig.iloc[-1]
        side = int(last["signal"])
        if not self.cfg.allow_short and side < 0:
            side = 0
        return {
            "signal": side,
            "sl": float(last["sl"]) if pd.notna(last["sl"]) else None,
            "tp": float(last["tp"]) if pd.notna(last["tp"]) else None,
            "ts": int(df.index[-1].timestamp()),
            "close": float(df["close"].iloc[-1]),
        }

    # --- position management -------------------------------------------------
    def check_exit(self, last_bar: pd.Series) -> Optional[str]:
        """Return exit reason if SL/TP/time hit on the latest closed bar."""
        pos = self.state.position
        if pos is None:
            return None
        h, l, c = float(last_bar["high"]), float(last_bar["low"]), float(last_bar["close"])
        if pos.side == 1:
            if l <= pos.sl:
                return "sl"
            if h >= pos.tp:
                return "tp"
        else:  # short (unused for long-only, kept for completeness)
            if h >= pos.sl:
                return "sl"
            if l <= pos.tp:
                return "tp"
        if pos.bars_open >= self.cfg.max_bars_in_trade:
            return "time"
        return None

    def enter_position(self, signal_info: dict) -> None:
        side = signal_info["signal"]
        sl = signal_info["sl"]
        tp = signal_info["tp"]
        entry_px = self.ex.fetch_price()
        if sl is None or tp is None or side == 0:
            return
        stop_dist = abs(entry_px - sl) / entry_px
        if stop_dist < 1e-5:
            self.log.warning("stop too tight, skipping")
            return
        risk_dollars = self.state.equity * self.cfg.risk_per_trade
        notional = min(risk_dollars / stop_dist, self.state.equity * self.cfg.max_leverage)
        qty = notional / entry_px
        if qty <= 0 or notional < 5:  # min order size sanity
            self.log.info(f"skipping tiny order qty={qty:.6f} notional={notional:.2f}")
            return
        order = self.ex.market_buy(qty) if side == 1 else self.ex.market_sell(qty)
        fill_px = float(order.get("price") or entry_px)
        self.state.position = Position(
            side=side, qty=qty, entry_px=fill_px,
            sl=sl, tp=tp, open_ts=int(time.time()),
            notional=notional, bars_open=0, order_id=order.get("id"),
        )
        self.log.info(
            f"OPEN {('LONG' if side == 1 else 'SHORT')} {qty:.6f} @ {fill_px:.4f} "
            f"sl={sl:.4f} tp={tp:.4f} notional={notional:.2f} "
            f"risk={risk_dollars:.2f} equity={self.state.equity:.2f}"
        )
        self.state.save(self.cfg.state_file)

    def close_position(self, exit_px_hint: float, reason: str) -> None:
        pos = self.state.position
        if pos is None:
            return
        order = self.ex.market_sell(pos.qty) if pos.side == 1 else self.ex.market_buy(pos.qty)
        fill_px = float(order.get("price") or exit_px_hint)
        # Use the SL/TP price (not market) when we know they triggered, since
        # that matches the backtest convention and is realistic for stop orders.
        if reason == "sl":
            fill_px = pos.sl
        elif reason == "tp":
            fill_px = pos.tp
        gross = (fill_px - pos.entry_px) * pos.qty * pos.side
        # Approx round-trip fee 0.06% per side on notional
        fees = (pos.notional + fill_px * pos.qty) * 0.0006
        pnl = gross - fees
        self.state.equity += pnl
        self.state.realised_trades += 1
        self.state.realised_pnl += pnl
        self.log.info(
            f"CLOSE {('LONG' if pos.side == 1 else 'SHORT')} {pos.qty:.6f} @ {fill_px:.4f} "
            f"reason={reason} pnl={pnl:.3f} equity={self.state.equity:.2f}"
        )
        self.state.position = None
        self.state.save(self.cfg.state_file)

    # --- main loop ----------------------------------------------------------
    def tick(self) -> None:
        df = self.ex.fetch_recent(n=300)
        warmup = max(self.cfg.entry_n, self.cfg.adx_n, self.cfg.atr_n) + 5
        if len(df) < warmup:
            self.log.warning(f"not enough bars: {len(df)}")
            return
        # Drop the still-forming current bar (the last row from ccxt usually
        # represents the open candle). We use the second-to-last row as the
        # most recent CLOSED bar.
        df = df.iloc[:-1]
        last_bar = df.iloc[-1]
        bar_ts = int(df.index[-1].timestamp())

        # 1) If we have a position, check exits on the just-closed bar
        if self.state.position is not None and bar_ts > self.state.last_bar_ts:
            self.state.position.bars_open += 1
            reason = self.check_exit(last_bar)
            if reason is not None:
                self.close_position(float(last_bar["close"]), reason)

        # 2) If no position, evaluate fresh signal
        if self.state.position is None and bar_ts > self.state.last_bar_ts:
            sig_info = self.compute_signal(df)
            if sig_info["signal"] != 0:
                self.enter_position(sig_info)

        if bar_ts > self.state.last_bar_ts:
            self.state.last_bar_ts = bar_ts
            self.state.save(self.cfg.state_file)

    def run(self) -> None:
        self.log.info(f"starting bot mode={self.cfg.mode} symbol={self.cfg.symbol} "
                      f"tf={self.cfg.timeframe} preset={self.cfg.preset} "
                      f"sentiment={self.cfg.use_sentiment} htf={self.cfg.use_htf} "
                      f"equity={self.state.equity:.2f}")
        if self.state.position:
            p = self.state.position
            self.log.info(f"resuming with open position: side={p.side} qty={p.qty:.6f} "
                          f"entry={p.entry_px:.4f} sl={p.sl:.4f} tp={p.tp:.4f}")
        while not self._stop:
            try:
                self.tick()
            except Exception as e:
                self.log.exception(f"tick error: {e}")
            time.sleep(self.cfg.poll_seconds)
        # Graceful: do not auto-close any position
        self.log.info(f"exiting | trades={self.state.realised_trades} "
                      f"pnl={self.state.realised_pnl:.2f} equity={self.state.equity:.2f}")


if __name__ == "__main__":
    Bot(BotConfig()).run()
