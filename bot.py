"""Live trading bot for the triple-confirm long-only strategy.

Default mode is PAPER (dry-run). Set MODE=live and provide API keys to trade
real money — only do this after you've reviewed the code and the risks.

Strategy: triple_long (long-only)
  - EMA stack: fast > slow > trend (9/26/50)
  - Momentum: RSI(14) > 55
  - Trend strength: ADX(14) > 22
  - Stops: 1.8x ATR(14)
  - TP:    3.0x ATR(14)

Presets (STRATEGY_PRESET env var):
  steady (default)  ETH/USDT 1h, risk 1.5%
                    5y: +201%, CAGR ~25%, MDD -29%, monthly median +1.4%
  conservative      ETH+BTC+SOL 1h portfolio (run via run_portfolio.sh)
                    5y: +434%, CAGR ~40%, MDD -22%, monthly median ~2-3%
  growth            SOL/USDT 30m, risk 1.5%
                    5y: +945%, CAGR ~64%, MDD -52%, monthly median +3.0%
  high_return       SOL/USDT 30m, risk 2.0%
                    5y: +1842%, CAGR ~87%, MDD -63%, monthly median +3.9%
  aggressive        SOL/USDT 30m, risk 2.5%
                    5y: +3243%, CAGR ~109%, MDD -73%, monthly median +4.6%
  yolo              SOL/USDT 30m, risk 3.0%
                    5y: +5296%, CAGR ~131%, MDD -80%, monthly median +5.3%

WARNING: 'aggressive' and 'yolo' have catastrophic drawdowns. The 2022 bear
year had -43% (r=2%) to -50%+ (r=3%) for SOL 30m. With $50k capital under
'yolo', expect to see equity drop to $10k before recovering. Only use these
if you can stomach the variance and the strategy bottoms out.

Crisis behaviour at 'steady' (vs buy & hold ETH):
  China mining ban (May-Jul 2021):  +1.9% strategy vs -39% B&H
  2022 bear market:                +25.5% strategy vs -68% B&H
  Terra/Luna (May 2022):            no losses
  FTX collapse (Nov 2022):          flat strategy vs -24% B&H

Run:
  python3 bot.py                       # default = steady (ETH 1h)
  STRATEGY_PRESET=growth python3 bot.py        # SOL 30m, ~3% monthly
  STRATEGY_PRESET=high_return python3 bot.py   # SOL 30m higher risk, ~4% monthly
  ./run_portfolio.sh                   # paper, ETH+BTC+SOL portfolio
  MODE=live python3 bot.py             # real orders
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

from core.event_log import EventLog
from core.notifier import Notifier
from core.sentiment import fetch_fear_greed
from core.strategies import donchian_breakout, triple_confirm_bidir, triple_confirm_long
from core.strategies_enhanced import with_htf_trend_filter
from core.strategies_sentiment import donchian_skip_fear

# Optional ML regime detector — imported lazily so missing sklearn doesn't
# break the bot for users who don't enable adaptive presets.
try:
    from core.regime_strategy import fit_predict_full as _regime_fit_predict
    REGIME_AVAILABLE = True
except Exception:
    REGIME_AVAILABLE = False


# Strategy presets. Each row defines:
#   (strategy, symbol, timeframe, risk_per_trade, max_leverage,
#    use_sentiment, use_htf_filter, allow_short)
# Backtest evidence in the docstring at the top of this file.
# `safer_*` presets enable equity-curve risk decay (0.5x risk after -20% DD)
# `adaptive_*` presets add ML regime detection — skip new entries when
# the current market regime is detected as BEAR.
PRESETS = {
    # Conservative (steady-default) — ETH 1h, low MDD, modest monthly return
    "steady":       ("triple_long", "ETH/USDT", "1h",  0.015, 3.0, False, False, False),
    "btc_filtered": ("triple_long", "BTC/USDT", "1h",  0.015, 3.0, False, True,  False),

    # Higher monthly target — SOL 30m at increasing risk levels
    # Monthly median: growth ~3%, high_return ~4%, aggressive ~4.6%, yolo ~5.3%
    # MDD: growth -52%, high_return -63%, aggressive -73%, yolo -80%
    "growth":       ("triple_long", "SOL/USDT", "30m", 0.015, 5.0, False, False, False),
    "high_return":  ("triple_long", "SOL/USDT", "30m", 0.020, 5.0, False, False, False),
    "aggressive":   ("triple_long", "SOL/USDT", "30m", 0.025, 5.0, False, False, False),
    "yolo":         ("triple_long", "SOL/USDT", "30m", 0.030, 5.0, False, False, False),

    # Safer SOL variants with equity-curve risk decay enabled
    # 5y r=2% + decay: CAGR +84% / MDD -47% (vs no-decay -63%)
    "safer_growth":      ("triple_long", "SOL/USDT", "30m", 0.015, 5.0, False, False, False),
    "safer_high_return": ("triple_long", "SOL/USDT", "30m", 0.020, 5.0, False, False, False),

    # INJ 1h — DISCOVERED BEST PAIR/TF on 5y data:
    # Profitable every year incl. 2022 bear; lower MDD than SOL; higher Sharpe.
    # 5y stats:
    #   inj_growth        r=1.5%        CAGR +76%  MDD -29%  Sharpe 1.79  med +3.83%/mo
    #   inj_high_return   r=2.0%        CAGR +109% MDD -38%  Sharpe 1.82  med +4.98%/mo
    #   inj_aggressive    r=2.5%        CAGR +146% MDD -45%  Sharpe 1.83  med +6.07%/mo
    #   safer_inj_growth  r=1.5%+decay  CAGR +X%   MDD ~-25% (recommended low-DD high-monthly)
    "inj_growth":            ("triple_long", "INJ/USDT", "1h", 0.015, 5.0, False, False, False),
    "inj_high_return":       ("triple_long", "INJ/USDT", "1h", 0.020, 5.0, False, False, False),
    "inj_aggressive":        ("triple_long", "INJ/USDT", "1h", 0.025, 5.0, False, False, False),
    "safer_inj_growth":      ("triple_long", "INJ/USDT", "1h", 0.015, 5.0, False, False, False),
    "safer_inj_high_return": ("triple_long", "INJ/USDT", "1h", 0.020, 5.0, False, False, False),

    # ML adaptive — INJ + skip new entries when current regime is BEAR
    # 5y backtest with walk-forward regime detection:
    #   adaptive_inj_high_return: CAGR +113% MDD -31% Sharpe 1.87 (vs +109%/-38%/1.82 fixed)
    "adaptive_inj_growth":      ("triple_long", "INJ/USDT", "1h", 0.015, 5.0, False, False, False),
    "adaptive_inj_high_return": ("triple_long", "INJ/USDT", "1h", 0.020, 5.0, False, False, False),

    # Bidirectional (long + mirror short) + directional regime filter:
    #   - long only in BULL/CHOP, short only in BEAR/CHOP
    # 5y INJ 1h walk-forward, r=2% + decay + F&G extreme-zone filter (>=80 / <=20):
    #   adaptive_inj_bidir: CAGR +140% MDD -28% Sharpe 1.75 (vs +146%/-33%/1.74 no-F&G)
    #   F&G filter: blocks 139 extreme-greed longs + 865 extreme-fear shorts over 5y;
    #   same return, MDD improved by ~6pp.
    "adaptive_inj_bidir":       ("triple_bidir", "INJ/USDT", "1h", 0.020, 5.0, False, False, True),

    # Same as adaptive_inj_bidir but ALSO reads dynamic (ema_fast, ema_slow,
    # rsi_min) from state/params.json. Run research/retune.py weekly to
    # refresh. 5y walk-forward: CAGR +176% MDD -34% Sharpe 1.84 (vs +140%/-28%/1.75 fixed).
    "adaptive_inj_bidir_wf":    ("triple_bidir", "INJ/USDT", "1h", 0.020, 5.0, False, False, True),

    # AVAX 30m — SOL's distant cousin, alternative growth pair
    # Backtest 5y: CAGR +41% / MDD -52% / monthly median +1.3%
    "avax_growth":  ("triple_long", "AVAX/USDT", "30m", 0.015, 5.0, False, False, False),

    # Original strategies kept for completeness
    "donchian":     ("donchian",    "ETH/USDT", "1h",  0.015, 3.0, True,  False, True),
    "donchian_htf": ("donchian",    "ETH/USDT", "1h",  0.015, 3.0, True,  True,  True),
}

# Presets that auto-enable equity-curve decay
SAFER_PRESETS = {"safer_growth", "safer_high_return",
                 "safer_inj_growth", "safer_inj_high_return",
                 "adaptive_inj_growth", "adaptive_inj_high_return",
                 "adaptive_inj_bidir", "adaptive_inj_bidir_wf"}

# Presets that use ML regime detection — block longs in BEAR.
# Bidirectional presets ALSO block shorts in BULL (directional filter).
ADAPTIVE_PRESETS = {"adaptive_inj_growth", "adaptive_inj_high_return",
                    "adaptive_inj_bidir", "adaptive_inj_bidir_wf"}

# Presets that apply the F&G extreme-zone filter on top of the bidir signal:
#   - block longs when F&G >= 80 (extreme greed)
#   - block shorts when F&G <= 20 (extreme fear)
# 5y backtest on INJ 1h: same return as without, MDD improved ~6pp.
FNG_EXTREME_PRESETS = {"adaptive_inj_bidir", "adaptive_inj_bidir_wf"}

# Presets that read dynamic (ema_fast, ema_slow, rsi_min) from params_file
# (written by research/retune.py). Falls back to BotConfig defaults if the
# file is missing or unreadable.
WF_RETUNE_PRESETS = {"adaptive_inj_bidir_wf"}


# ----------------------------- Configuration --------------------------------
@dataclass
class BotConfig:
    exchange: str = os.getenv("EXCHANGE", "bybit")
    # Preset drives symbol/tf/risk/leverage. Override via env vars below.
    preset: str = os.getenv("STRATEGY_PRESET", "steady")
    mode: str = os.getenv("MODE", "paper")  # 'paper' | 'live'
    starting_equity: float = float(os.getenv("STARTING_EQUITY", "100"))
    # Override env vars (default: empty string -> use preset value)
    symbol_override: str = os.getenv("SYMBOL", "")
    timeframe_override: str = os.getenv("TIMEFRAME", "")
    risk_override: str = os.getenv("RISK_PER_TRADE", "")
    leverage_override: str = os.getenv("MAX_LEVERAGE", "")
    # Donchian stops
    sl_mult: float = float(os.getenv("SL_MULT", "2.5"))
    tp_mult: float = float(os.getenv("TP_MULT", "5.0"))
    # Triple-long stops (used when preset selects triple_long)
    tl_sl_mult: float = float(os.getenv("TL_SL_MULT", "1.8"))
    tl_tp_mult: float = float(os.getenv("TL_TP_MULT", "3.0"))
    max_bars_in_trade: int = int(os.getenv("MAX_BARS", "96"))
    allow_short_override: str = os.getenv("ALLOW_SHORT", "")
    state_file: str = os.getenv("STATE_FILE", "bot_state.json")
    log_file: str = os.getenv("LOG_FILE", "bot.log")
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "30"))
    # Donchian params
    entry_n: int = 20
    exit_n: int = 10
    adx_n: int = 14
    adx_min: float = 20.0
    atr_n: int = 14
    # Triple-long params (5y-validated)
    ema_fast: int = 9
    ema_slow: int = 26
    ema_trend: int = 50
    rsi_min: float = 55.0
    tl_adx_min: float = 22.0
    # Sentiment filter (Crypto Fear & Greed Index)
    fear_threshold: float = float(os.getenv("FEAR_THRESHOLD", "25"))
    # F&G extreme-zone filter (block longs at extreme greed, shorts at extreme fear)
    fng_greed_max: float = float(os.getenv("FNG_GREED_MAX", "80"))
    fng_fear_min: float = float(os.getenv("FNG_FEAR_MIN", "20"))
    fng_extreme_override: str = os.getenv("FNG_EXTREME", "")
    # HTF trend filter
    htf_rule: str = os.getenv("HTF_RULE", "1D")
    htf_ema_n: int = int(os.getenv("HTF_EMA_N", "50"))
    # Equity-curve risk decay (5y-tested: cuts MDD significantly with modest cost)
    # Auto-enabled by `safer_*` presets; can be forced via env.
    eq_risk_decay_override: str = os.getenv("EQ_RISK_DECAY", "")
    drawdown_for_decay: float = float(os.getenv("DD_FOR_DECAY", "0.20"))
    # Daily loss limit / circuit breaker
    daily_loss_pct: float = float(os.getenv("DAILY_LOSS_PCT", "0.0"))    # 0 disables
    # Manual overrides (otherwise read from preset)
    strategy_override: str = os.getenv("STRATEGY", "")
    use_sentiment_override: str = os.getenv("USE_SENTIMENT", "")
    use_htf_override: str = os.getenv("USE_HTF", "")
    # Walk-forward dynamic params (read from params_file on each signal cycle).
    # Auto-enabled for adaptive_inj_bidir_wf, or via env WF_RETUNE=1.
    params_file: str = os.getenv("PARAMS_FILE", "state/params.json")
    wf_retune_override: str = os.getenv("WF_RETUNE", "")

    @property
    def symbol(self) -> str:
        return self.symbol_override or PRESETS[self.preset][1]

    @property
    def timeframe(self) -> str:
        return self.timeframe_override or PRESETS[self.preset][2]

    @property
    def risk_per_trade(self) -> float:
        return float(self.risk_override) if self.risk_override else PRESETS[self.preset][3]

    @property
    def max_leverage(self) -> float:
        return float(self.leverage_override) if self.leverage_override else PRESETS[self.preset][4]

    @property
    def eq_risk_decay(self) -> float:
        if self.eq_risk_decay_override:
            return float(self.eq_risk_decay_override)
        # Auto-enable for safer_* presets
        if self.preset in SAFER_PRESETS:
            return 0.5
        return 0.0

    @property
    def use_adaptive_regime(self) -> bool:
        """ML regime detection — skip new entries when current regime is BEAR."""
        return self.preset in ADAPTIVE_PRESETS

    @property
    def use_fng_extreme_filter(self) -> bool:
        """Block longs at F&G >= fng_greed_max, shorts at F&G <= fng_fear_min."""
        if self.fng_extreme_override:
            return self.fng_extreme_override == "1"
        return self.preset in FNG_EXTREME_PRESETS

    @property
    def use_wf_retune(self) -> bool:
        """Read (ema_fast, ema_slow, rsi_min) from params_file each cycle."""
        if self.wf_retune_override:
            return self.wf_retune_override == "1"
        return self.preset in WF_RETUNE_PRESETS

    @property
    def strategy(self) -> str:
        if self.strategy_override:
            return self.strategy_override
        return PRESETS[self.preset][0]

    @property
    def use_sentiment(self) -> bool:
        if self.use_sentiment_override:
            return self.use_sentiment_override == "1"
        return PRESETS[self.preset][5]

    @property
    def use_htf(self) -> bool:
        if self.use_htf_override:
            return self.use_htf_override == "1"
        return PRESETS[self.preset][6]

    @property
    def allow_short(self) -> bool:
        if self.allow_short_override:
            return self.allow_short_override == "1"
        return PRESETS[self.preset][7]


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
    equity_peak: float = 0.0     # high-water mark for equity-decay risk scaling
    day_start_ts: int = 0        # midnight UTC of current trading day
    day_start_equity: float = 0.0

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2, default=str)

    @classmethod
    def load(cls, path: str, starting_equity: float) -> "State":
        p = Path(path)
        if not p.exists():
            return cls(equity=starting_equity, equity_peak=starting_equity,
                       day_start_equity=starting_equity)
        d = json.loads(p.read_text())
        pos = d.get("position")
        return cls(
            equity=d["equity"],
            last_bar_ts=d.get("last_bar_ts", 0),
            position=Position(**pos) if pos else None,
            realised_trades=d.get("realised_trades", 0),
            realised_pnl=d.get("realised_pnl", 0.0),
            equity_peak=d.get("equity_peak", d["equity"]),
            day_start_ts=d.get("day_start_ts", 0),
            day_start_equity=d.get("day_start_equity", d["equity"]),
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
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


# ----------------------------- Exchange wrapper -----------------------------
class Exchange:
    """Thin wrapper around ccxt, with a paper-mode fallback.

    Modes:
      live:                     ccxt client with API keys, real orders.
      paper + ccxt-compatible:  ccxt client for REAL market data (no auth
                                needed for public OHLCV), simulated orders.
                                This is the recommended paper mode — real
                                Bybit/Binance prices, no order risk.
      paper + offline:          falls back to KuCoin REST OHLCV (our
                                core/data.py). Used when ccxt unreachable
                                or EXCHANGE=kucoin_offline.
    """
    TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}

    def __init__(self, cfg: BotConfig, log: logging.Logger):
        self.cfg = cfg
        self.log = log
        self.paper = cfg.mode == "paper"
        try:
            import ccxt
        except ImportError:
            ccxt = None
            if not self.paper:
                raise RuntimeError("ccxt not installed; pip install ccxt")
        self._ccxt = None

        # In paper mode we still want REAL market data through ccxt's public
        # endpoints (no auth). Skip only if user explicitly opts out.
        offline_paper = os.getenv("EXCHANGE", "").lower() == "kucoin_offline"
        if ccxt is not None and not offline_paper:
            exch_name = cfg.exchange.lower().replace("kucoin_offline", "kucoin")
            try:
                klass = getattr(ccxt, exch_name)
            except AttributeError:
                self.log.warning(f"ccxt has no {exch_name} class; falling back to offline data")
                klass = None
            if klass is not None:
                params = {
                    "apiKey": os.getenv("API_KEY", ""),
                    "secret": os.getenv("API_SECRET", ""),
                    "password": os.getenv("API_PASSPHRASE", ""),
                    "enableRateLimit": True,
                }
                # Bybit-specific: default to USDT-margined perpetuals (linear)
                # since that's where the low fees live (0.055% taker).
                if exch_name == "bybit":
                    params["options"] = {"defaultType": "linear"}
                self._ccxt = klass({k: v for k, v in params.items() if v})

    def fetch_recent(self, n: int = 250) -> pd.DataFrame:
        """Fetch last n closed bars (skips current forming bar in caller)."""
        if self._ccxt is not None:
            try:
                raw = self._ccxt.fetch_ohlcv(self.cfg.symbol, self.cfg.timeframe,
                                             limit=n)
                df = pd.DataFrame(raw, columns=["ts_ms", "open", "high", "low",
                                                "close", "volume"])
                df["dt"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
                df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
                if len(df) >= n // 2:
                    return df
                self.log.warning(f"ccxt returned only {len(df)} bars; "
                                 f"falling back to KuCoin REST")
            except Exception as e:
                self.log.warning(f"ccxt fetch failed ({e}); using KuCoin REST fallback")
        # Paper-mode offline fallback: use KuCoin REST. Estimate days.
        from core.data import fetch_ohlcv
        sym = self.cfg.symbol.replace("/", "-").replace(":USDT", "")
        bars_per_day = {"15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}.get(
            self.cfg.timeframe, 24)
        days = max(10, int(n / bars_per_day) + 5)
        return fetch_ohlcv(sym, self.cfg.timeframe, days=days, use_cache=False).tail(n)

    def fetch_price(self) -> float:
        if self._ccxt is not None:
            try:
                t = self._ccxt.fetch_ticker(self.cfg.symbol)
                return float(t["last"])
            except Exception as e:
                self.log.warning(f"ccxt fetch_ticker failed ({e}); using last close")
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
        self.events = EventLog()
        self.notifier = Notifier(mode=cfg.mode, preset=cfg.preset,
                                 symbol=cfg.symbol, logger=self.log)
        self._stop = False
        self._last_regime: str | None = None
        self._last_daily_summary_day: int = 0
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_):
        self.log.info("shutdown signal received")
        self._stop = True

    def _load_dynamic_params(self) -> Optional[dict]:
        """Return (ema_fast, ema_slow, rsi_min) dict from params_file, or None."""
        if not self.cfg.use_wf_retune:
            return None
        p = Path(self.cfg.params_file)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            # cache key based on mtime; log only on change
            mtime = p.stat().st_mtime
            cached_mtime = getattr(self, "_params_mtime", None)
            if cached_mtime != mtime:
                self.log.info(f"loaded dynamic params from {p}: "
                              f"ef={data.get('ema_fast')} es={data.get('ema_slow')} "
                              f"rsi={data.get('rsi_min')} "
                              f"fitted_at={data.get('fitted_at')}")
                self._params_mtime = mtime
            return data
        except Exception as e:
            self.log.warning(f"failed to load {p}: {e}")
            return None

    # --- signal generation (matches backtest exactly) -----------------------
    def compute_signal(self, df: pd.DataFrame) -> dict:
        if self.cfg.strategy in ("triple_long", "triple_bidir"):
            fn = triple_confirm_bidir if self.cfg.strategy == "triple_bidir" \
                 else triple_confirm_long
            dyn = self._load_dynamic_params()
            ef = int(dyn["ema_fast"]) if dyn and "ema_fast" in dyn else self.cfg.ema_fast
            es = int(dyn["ema_slow"]) if dyn and "ema_slow" in dyn else self.cfg.ema_slow
            rm = float(dyn["rsi_min"]) if dyn and "rsi_min" in dyn else self.cfg.rsi_min
            sig = fn(
                df,
                ema_fast=ef,
                ema_slow=es,
                ema_trend=self.cfg.ema_trend,
                rsi_min=rm,
                adx_min=self.cfg.tl_adx_min,
                atr_n=self.cfg.atr_n,
                sl_mult=self.cfg.tl_sl_mult,
                tp_mult=self.cfg.tl_tp_mult,
            )
        else:  # donchian
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
                    self.log.warning(f"sentiment fetch failed, falling back: {e}")
                    sig = donchian_breakout(df, **donchian_kwargs)
            else:
                sig = donchian_breakout(df, **donchian_kwargs)
        if self.cfg.use_htf:
            sig = with_htf_trend_filter(df, sig,
                                        htf_rule=self.cfg.htf_rule,
                                        ema_n=self.cfg.htf_ema_n)
        # F&G extreme-zone filter (defensive overlay for bidir):
        #   block longs at F&G >= 80 (extreme greed → tops)
        #   block shorts at F&G <= 20 (extreme fear → bottoms)
        fng_value = None
        if self.cfg.use_fng_extreme_filter:
            try:
                fng_df = fetch_fear_greed()
                fng_value = int(fng_df["fng"].iloc[-1])
                last_sig = int(sig.iloc[-1]["signal"])
                block_fng = (last_sig ==  1 and fng_value >= self.cfg.fng_greed_max) \
                         or (last_sig == -1 and fng_value <= self.cfg.fng_fear_min)
                if block_fng:
                    self.log.info(f"F&G={fng_value}, blocking side={last_sig} "
                                  f"(extremes filter)")
                    sig = sig.copy()
                    import numpy as _np
                    sig.iloc[-1, sig.columns.get_loc("signal")] = 0
                    sig.iloc[-1, sig.columns.get_loc("sl")] = _np.nan
                    sig.iloc[-1, sig.columns.get_loc("tp")] = _np.nan
            except Exception as e:
                self.log.warning(f"F&G fetch failed, ignoring filter: {e}")

        # Adaptive regime filter:
        #   long-only strategy: block longs in BEAR
        #   bidirectional:     additionally block shorts in BULL
        regime_label = None
        if self.cfg.use_adaptive_regime and REGIME_AVAILABLE:
            try:
                bpd = {"15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}.get(
                    self.cfg.timeframe, 24)
                _, _, regime_series = _regime_fit_predict(df, bars_per_day=bpd)
                regime_label = regime_series.iloc[-1]
                last_sig = int(sig.iloc[-1]["signal"])
                block = (last_sig ==  1 and regime_label == "BEAR") \
                     or (last_sig == -1 and regime_label == "BULL")
                if block:
                    self.log.info(f"adaptive regime={regime_label}, "
                                  f"blocking side={last_sig}")
                    sig = sig.copy()
                    import numpy as _np
                    sig.iloc[-1, sig.columns.get_loc("signal")] = 0
                    sig.iloc[-1, sig.columns.get_loc("sl")] = _np.nan
                    sig.iloc[-1, sig.columns.get_loc("tp")] = _np.nan
            except Exception as e:
                self.log.warning(f"regime detection failed, ignoring: {e}")
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
            "regime": regime_label,
            "fng": fng_value,
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
        else:  # short — used by triple_bidir and any preset with allow_short=True
            if h >= pos.sl:
                return "sl"
            if l <= pos.tp:
                return "tp"
        if pos.bars_open >= self.cfg.max_bars_in_trade:
            return "time"
        return None

    def _effective_risk(self) -> float:
        """Risk-per-trade with equity-curve decay applied."""
        risk = self.cfg.risk_per_trade
        if self.cfg.eq_risk_decay > 0:
            self.state.equity_peak = max(self.state.equity_peak, self.state.equity)
            if self.state.equity_peak > 0:
                cur_dd = (self.state.equity / self.state.equity_peak) - 1
                if cur_dd <= -self.cfg.drawdown_for_decay:
                    risk *= self.cfg.eq_risk_decay
                    self.log.info(f"equity-decay active: dd={cur_dd*100:.1f}% "
                                  f"-> risk reduced to {risk*100:.2f}%")
        return risk

    def _daily_loss_blocked(self) -> bool:
        """True if today's loss has hit the circuit-breaker."""
        if self.cfg.daily_loss_pct <= 0:
            return False
        # Roll the day at UTC midnight
        cur_day = int(time.time() // 86400) * 86400
        if cur_day > self.state.day_start_ts:
            self.state.day_start_ts = cur_day
            self.state.day_start_equity = self.state.equity
            return False
        if self.state.day_start_equity <= 0:
            return False
        day_pnl_pct = (self.state.equity - self.state.day_start_equity) / self.state.day_start_equity
        if day_pnl_pct <= -self.cfg.daily_loss_pct:
            self.log.warning(f"daily loss limit hit: {day_pnl_pct*100:.2f}% "
                             f"<= -{self.cfg.daily_loss_pct*100:.1f}% — no new entries")
            return True
        return False

    def enter_position(self, signal_info: dict) -> None:
        side = signal_info["signal"]
        sl = signal_info["sl"]
        tp = signal_info["tp"]
        entry_px = self.ex.fetch_price()
        if sl is None or tp is None or side == 0:
            return
        if self._daily_loss_blocked():
            return
        stop_dist = abs(entry_px - sl) / entry_px
        if stop_dist < 1e-5:
            self.log.warning("stop too tight, skipping")
            return
        risk = self._effective_risk()
        risk_dollars = self.state.equity * risk
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
        self.events.entry(side=side, qty=qty, entry_px=fill_px,
                          sl=sl, tp=tp, notional=notional,
                          risk_pct=risk, equity=self.state.equity,
                          symbol=self.cfg.symbol, preset=self.cfg.preset,
                          regime=signal_info.get("regime"))
        try:
            self.notifier.trade_open(side=side, qty=qty, price=fill_px,
                                     sl=sl, tp=tp, notional=notional,
                                     risk=risk, equity=self.state.equity,
                                     regime=signal_info.get("regime"))
        except Exception as e:
            self.log.warning(f"notifier trade_open failed: {e}")
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
        pct = (pnl / pos.notional * 100) if pos.notional else 0.0
        self.state.equity += pnl
        self.state.realised_trades += 1
        self.state.realised_pnl += pnl
        self.log.info(
            f"CLOSE {('LONG' if pos.side == 1 else 'SHORT')} {pos.qty:.6f} @ {fill_px:.4f} "
            f"reason={reason} pnl={pnl:.3f} equity={self.state.equity:.2f}"
        )
        self.events.exit(side=pos.side, qty=pos.qty, exit_px=fill_px,
                         entry_px=pos.entry_px, pnl=pnl, pnl_pct=pct,
                         reason=reason, bars_held=pos.bars_open,
                         fees=fees, notional=pos.notional,
                         equity=self.state.equity, symbol=self.cfg.symbol,
                         preset=self.cfg.preset)
        try:
            self.notifier.trade_close(side=pos.side, qty=pos.qty,
                                      price=fill_px, pnl=pnl, reason=reason,
                                      bars_held=pos.bars_open,
                                      equity=self.state.equity, pct=pct)
        except Exception as e:
            self.log.warning(f"notifier trade_close failed: {e}")
        self.state.position = None
        self.state.save(self.cfg.state_file)

    # --- main loop ----------------------------------------------------------
    def _maybe_emit_daily_summary(self) -> None:
        """Once per UTC day, log + push a summary."""
        cur_day = int(time.time() // 86400)
        if cur_day <= self._last_daily_summary_day:
            return
        if self._last_daily_summary_day == 0:
            # first tick — don't fire on startup
            self._last_daily_summary_day = cur_day
            return
        day_pnl = self.state.equity - self.state.day_start_equity
        wr = 0.0  # we don't track per-trade win history; approximate via realised_pnl > 0
        if self.state.realised_trades > 0:
            # Best-effort: positive realised PnL means at least some wins.
            # The event log has full per-trade data for accurate stats.
            wr = 1.0 if self.state.realised_pnl > 0 else 0.0
        peak = max(self.state.equity_peak, self.state.equity)
        dd = (self.state.equity / peak - 1) if peak > 0 else 0.0
        self.events.daily_summary(equity=self.state.equity, day_pnl=day_pnl,
                                  total_trades=self.state.realised_trades,
                                  total_pnl=self.state.realised_pnl,
                                  current_dd=dd,
                                  regime=self._last_regime)
        try:
            self.notifier.daily_summary(equity=self.state.equity, day_pnl=day_pnl,
                                        day_trades=0,  # not separately tracked
                                        total_trades=self.state.realised_trades,
                                        total_pnl=self.state.realised_pnl,
                                        win_rate=wr, current_dd=dd,
                                        regime=self._last_regime)
        except Exception as e:
            self.log.warning(f"notifier daily_summary failed: {e}")
        self._last_daily_summary_day = cur_day

    def tick(self) -> None:
        # Daily summary check (before bar processing)
        self._maybe_emit_daily_summary()

        # Adaptive regime needs ~1y of history for the GMM to fit well
        n_bars = 1200 if self.cfg.use_adaptive_regime else 300
        df = self.ex.fetch_recent(n=n_bars)
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
            # Detect regime changes for adaptive presets and notify
            cur_regime = sig_info.get("regime") if isinstance(sig_info, dict) else None
            if cur_regime and cur_regime != self._last_regime:
                if self._last_regime is not None:
                    self.events.regime_change(from_regime=self._last_regime,
                                              to_regime=cur_regime,
                                              equity=self.state.equity)
                    try:
                        self.notifier.regime_change(self._last_regime, cur_regime,
                                                    self.state.equity)
                    except Exception as e:
                        self.log.warning(f"notifier regime_change failed: {e}")
                self._last_regime = cur_regime
            # Log a compact tick event for forensics
            self.events.signal(signal=sig_info["signal"],
                               sl=sig_info.get("sl"), tp=sig_info.get("tp"),
                               close=sig_info.get("close"),
                               regime=cur_regime, equity=self.state.equity)
            if sig_info["signal"] != 0:
                self.enter_position(sig_info)

        if bar_ts > self.state.last_bar_ts:
            self.state.last_bar_ts = bar_ts
            self.state.save(self.cfg.state_file)

    def run(self) -> None:
        self.log.info(f"starting bot mode={self.cfg.mode} symbol={self.cfg.symbol} "
                      f"tf={self.cfg.timeframe} preset={self.cfg.preset} "
                      f"strategy={self.cfg.strategy} sentiment={self.cfg.use_sentiment} "
                      f"htf={self.cfg.use_htf} short={self.cfg.allow_short} "
                      f"equity={self.state.equity:.2f}")
        self.events.bot_start(mode=self.cfg.mode, exchange=self.cfg.exchange,
                              symbol=self.cfg.symbol, tf=self.cfg.timeframe,
                              preset=self.cfg.preset, strategy=self.cfg.strategy,
                              risk_per_trade=self.cfg.risk_per_trade,
                              max_leverage=self.cfg.max_leverage,
                              equity=self.state.equity,
                              adaptive=self.cfg.use_adaptive_regime,
                              eq_decay=self.cfg.eq_risk_decay,
                              starting_equity=self.cfg.starting_equity)
        try:
            self.notifier.startup(self.state.equity, extra={
                "tf": self.cfg.timeframe,
                "risk": f"{self.cfg.risk_per_trade * 100:.1f}%",
                "leverage": f"{self.cfg.max_leverage:.0f}x",
                "adaptive": str(self.cfg.use_adaptive_regime).lower(),
            })
        except Exception as e:
            self.log.warning(f"notifier startup failed: {e}")
        if self.state.position:
            p = self.state.position
            self.log.info(f"resuming with open position: side={p.side} qty={p.qty:.6f} "
                          f"entry={p.entry_px:.4f} sl={p.sl:.4f} tp={p.tp:.4f}")
        while not self._stop:
            try:
                self.tick()
            except Exception as e:
                self.log.exception(f"tick error: {e}")
                self.events.error(message=str(e), exception_type=type(e).__name__)
                try:
                    self.notifier.error(f"tick error: {type(e).__name__}: {e}")
                except Exception:
                    pass
            time.sleep(self.cfg.poll_seconds)
        # Graceful: do not auto-close any position
        self.log.info(f"exiting | trades={self.state.realised_trades} "
                      f"pnl={self.state.realised_pnl:.2f} equity={self.state.equity:.2f}")
        self.events.bot_stop(trades=self.state.realised_trades,
                             pnl=self.state.realised_pnl,
                             equity=self.state.equity)
        try:
            self.notifier.shutdown(self.state.realised_trades,
                                   self.state.realised_pnl,
                                   self.state.equity)
        except Exception:
            pass


if __name__ == "__main__":
    Bot(BotConfig()).run()
