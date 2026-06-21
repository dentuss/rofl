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
import random
import signal
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from core.event_log import EventLog
from core.notifier import Notifier
from core.risk import DEFAULT_DECAY_TIERS, decay_risk_scale, parse_tiers
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
    # 5y INJ 1h walk-forward, r=2% + decay + F&G persistence filter (3-day):
    #   adaptive_inj_bidir: CAGR +147% MDD -27% Sharpe 1.91 ($100 -> $6531)
    #   F&G persistence (FNG_PERSIST_DAYS=3): only blocks ENTRENCHED extremes
    #   (>=3 consecutive days >=80 or <=20); lets flash-extremes through since
    #   those continuation shorts are profitable. vs single-day 80/20 blocking:
    #   +25pp CAGR, +0.14 Sharpe, same MDD. Blocking longs at greed gives the
    #   MDD protection; the old short-block at fear was leaving money on table.
    "adaptive_inj_bidir":       ("triple_bidir", "INJ/USDT", "1h", 0.020, 5.0, False, False, True),

    # Same as adaptive_inj_bidir but ALSO reads dynamic (ema_fast, ema_slow,
    # rsi_min) from state/params.json. Run research/retune.py weekly to
    # refresh. 5y walk-forward: CAGR +176% MDD -34% Sharpe 1.84 (vs +140%/-28%/1.75 fixed).
    "adaptive_inj_bidir_wf":    ("triple_bidir", "INJ/USDT", "1h", 0.020, 5.0, False, False, True),

    # Generic bidir preset (symbol-agnostic) — identical machinery to
    # adaptive_inj_bidir but meant to be pointed at any pair via SYMBOL=.
    # Used by the multi-pair portfolio launcher (run_bidir_portfolio.sh /
    # docker-compose.bidir-portfolio.yml). Validated profitable on 7 pairs
    # (INJ/SOL/ADA/ETH/LINK Sharpe>1.1; BTC 0.77; LTC 0.30).
    "adaptive_bidir":           ("triple_bidir", "INJ/USDT", "1h", 0.020, 5.0, False, False, True),

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
                 "adaptive_inj_bidir", "adaptive_inj_bidir_wf",
                 "adaptive_bidir"}

# Presets that use ML regime detection — block longs in BEAR.
# Bidirectional presets ALSO block shorts in BULL (directional filter).
ADAPTIVE_PRESETS = {"adaptive_inj_growth", "adaptive_inj_high_return",
                    "adaptive_inj_bidir", "adaptive_inj_bidir_wf",
                    "adaptive_bidir"}

# Presets that apply the F&G extreme-zone filter on top of the bidir signal:
#   - block longs when F&G >= 80 (extreme greed)
#   - block shorts when F&G <= 20 (extreme fear)
# 5y backtest on INJ 1h: same return as without, MDD improved ~6pp.
FNG_EXTREME_PRESETS = {"adaptive_inj_bidir", "adaptive_inj_bidir_wf",
                       "adaptive_bidir"}

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
    # Only block when F&G has been extreme for >=N consecutive daily readings.
    # 1 = block on a single extreme reading (old behavior). 3 (default) lets
    # flash-extremes through (often the START of a continuation move) and only
    # blocks entrenched capitulation/euphoria (which mean-revert). 5y backtest:
    # +24pp portfolio CAGR, +0.23 Sharpe, same MDD vs single-day blocking.
    fng_persist_days: int = int(os.getenv("FNG_PERSIST_DAYS", "3"))
    # HTF trend filter
    htf_rule: str = os.getenv("HTF_RULE", "1D")
    htf_ema_n: int = int(os.getenv("HTF_EMA_N", "50"))
    # Equity-curve risk decay (5y-tested: cuts MDD significantly with modest cost)
    # Auto-enabled by `safer_*` presets; can be forced via env.
    eq_risk_decay_override: str = os.getenv("EQ_RISK_DECAY", "")
    drawdown_for_decay: float = float(os.getenv("DD_FOR_DECAY", "0.20"))
    # Multi-tier decay ladder. Overrides the single-tier pair above when set.
    # Default (for decay-enabled presets): -20%->half, -35%->quarter, -50%->stop.
    # Env override: EQ_DECAY_TIERS="0.20:0.5,0.35:0.25,0.50:0.0"
    eq_decay_tiers_override: str = os.getenv("EQ_DECAY_TIERS", "")
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
    def eq_decay_tiers(self) -> tuple:
        """Multi-tier decay ladder. Env override wins; otherwise decay-enabled
        presets get the default ladder, others get an empty tuple (single-tier
        behavior governed by eq_risk_decay)."""
        if self.eq_decay_tiers_override:
            return parse_tiers(self.eq_decay_tiers_override)
        if self.preset in SAFER_PRESETS:
            return DEFAULT_DECAY_TIERS
        return ()

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
    realised_wins: int = 0       # winning closes (pnl > 0) — for true win rate
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
            realised_wins=d.get("realised_wins", 0),
            realised_pnl=d.get("realised_pnl", 0.0),
            equity_peak=d.get("equity_peak", d["equity"]),
            day_start_ts=d.get("day_start_ts", 0),
            day_start_equity=d.get("day_start_equity", d["equity"]),
        )

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())


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
        self._market: dict | None = None

    # ---- symbol normalization -----------------------------------------
    def _ccxt_symbol(self) -> str:
        """Symbol formatted for ccxt API calls.

        Bybit V5 has SEPARATE markets for the same pair string: 'SOL/USDT'
        matches BOTH the spot market AND the linear-perp market. The spot
        market rejects attached stopLoss/takeProfit. The defaultType=linear
        hint we set in __init__ is not enough — ccxt's `market(symbol)` lookup
        returns the spot market first when the bare 'SOL/USDT' string is
        ambiguous. The unified-perp form is 'SOL/USDT:USDT' (settlement
        currency suffix). Append it when missing on bybit; leave the
        human-friendly cfg.symbol untouched for logs / state / notifier.
        """
        sym = self.cfg.symbol
        if self.cfg.exchange.lower() == "bybit" and sym and ":" not in sym:
            return f"{sym}:USDT"
        return sym

    def _ccxt_params(self, extra: dict | None = None) -> dict:
        """Bybit V5 also wants an explicit category on private endpoints; the
        symbol suffix isn't enough for fetch_positions / fetch_balance."""
        p = dict(extra or {})
        if self.cfg.exchange.lower() == "bybit" and "category" not in p:
            p["category"] = "linear"
        return p

    # ---- live-mode order helpers ---------------------------------------
    def _load_market(self) -> dict | None:
        """Cache the exchange's market metadata for our symbol (precision,
        min amount, min cost). Returns None on paper or if unavailable."""
        if self._market is not None or self.paper or self._ccxt is None:
            return self._market
        try:
            if not getattr(self._ccxt, "markets", None):
                self._ccxt.load_markets()
            self._market = self._ccxt.market(self._ccxt_symbol())
        except Exception as e:
            self.log.warning(f"load_markets failed ({e}); orders will use raw qty")
            self._market = {}
        return self._market

    def normalize_order(self, qty: float, entry_px: float) -> tuple[float, str | None]:
        """Round qty to the exchange's amount precision and validate
        min-amount + min-cost. Returns (rounded_qty, reject_reason_or_None).
        Paper mode: pass-through."""
        if self.paper or self._ccxt is None:
            return qty, None
        m = self._load_market() or {}
        try:
            qty = float(self._ccxt.amount_to_precision(self._ccxt_symbol(), qty))
        except Exception:
            pass
        limits = m.get("limits") or {}
        min_amount = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        if min_amount and qty < min_amount:
            return qty, f"qty {qty} < exchange min amount {min_amount}"
        if min_cost and (qty * entry_px) < min_cost:
            return qty, f"notional {qty*entry_px:.4f} < exchange min cost {min_cost}"
        return qty, None

    def _round_price(self, p: float) -> float:
        if self.paper or self._ccxt is None:
            return p
        try:
            return float(self._ccxt.price_to_precision(self._ccxt_symbol(), p))
        except Exception:
            return p

    def fetch_recent(self, n: int = 250) -> pd.DataFrame:
        """Fetch last n closed bars (skips current forming bar in caller)."""
        if self._ccxt is not None:
            try:
                raw = self._ccxt.fetch_ohlcv(self._ccxt_symbol(), self.cfg.timeframe,
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
                t = self._ccxt.fetch_ticker(self._ccxt_symbol())
                return float(t["last"])
            except Exception as e:
                self.log.warning(f"ccxt fetch_ticker failed ({e}); using last close")
        return float(self.fetch_recent(1)["close"].iloc[-1])

    def _attached_sltp_params(self, sl: float | None, tp: float | None) -> dict:
        """Build ccxt params to attach SL/TP to the entry order.
        Bybit V5 accepts stopLoss/takeProfit on the entry; the exchange then
        manages the conditional orders autonomously, so the position is
        protected even if the bot is down."""
        if sl is None and tp is None or self.paper:
            return {}
        params: dict = {}
        if sl is not None:
            params["stopLoss"] = self._round_price(sl)
        if tp is not None:
            params["takeProfit"] = self._round_price(tp)
        return params

    def market_buy(self, qty: float, sl: float | None = None,
                   tp: float | None = None) -> dict:
        if self.paper:
            px = self.fetch_price()
            return {"id": f"paper-{int(time.time())}", "price": px, "amount": qty}
        params = self._attached_sltp_params(sl, tp)
        return self._ccxt.create_market_buy_order(self._ccxt_symbol(), qty,
                                                   params=self._ccxt_params(params))

    def market_sell(self, qty: float, sl: float | None = None,
                    tp: float | None = None) -> dict:
        if self.paper:
            px = self.fetch_price()
            return {"id": f"paper-{int(time.time())}", "price": px, "amount": qty}
        params = self._attached_sltp_params(sl, tp)
        return self._ccxt.create_market_sell_order(self._ccxt_symbol(), qty,
                                                    params=self._ccxt_params(params))

    def fetch_position_size(self, retries: int = 2) -> float | None:
        """Net signed position size on the exchange (long > 0, short < 0).
        Used in live mode to detect when the exchange's SL/TP closed our
        position autonomously. Returns None in paper or on persistent error.

        Bybit unified positions can report side as "long"/"short"/"none";
        in hedge mode an empty side means no position. We sign explicitly.
        """
        if self.paper or self._ccxt is None:
            return None
        last_err = None
        ccxt_sym = self._ccxt_symbol()
        for attempt in range(retries + 1):
            try:
                poss = self._ccxt.fetch_positions([ccxt_sym],
                                                  params=self._ccxt_params())
                for p in poss:
                    if p.get("symbol") not in (ccxt_sym, self.cfg.symbol):
                        continue
                    amt_raw = p.get("contracts")
                    if amt_raw in (None, 0, 0.0, "0"):
                        return 0.0
                    amt = abs(float(amt_raw))
                    side = (p.get("side") or "").lower()
                    if side == "short":
                        return -amt
                    if side == "long":
                        return amt
                    # Side unknown but qty present — refuse to guess sign.
                    self.log.warning(f"fetch_positions: ambiguous side '{p.get('side')}' "
                                     f"with qty {amt_raw}; treating as unknown")
                    return None
                return 0.0
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        self.log.warning(f"fetch_positions failed after {retries+1} tries: {last_err}")
        return None

    def fetch_balance_usdt(self) -> float | None:
        """Best-effort USDT balance for the unified/derivatives account.
        Used to validate API keys at startup and reconcile equity_peak when
        switching from paper to live. Returns None in paper or on error."""
        if self.paper or self._ccxt is None:
            return None
        try:
            bal = self._ccxt.fetch_balance(params=self._ccxt_params())
        except Exception as e:
            self.log.warning(f"fetch_balance failed ({e})")
            return None
        try:
            return float(bal.get("USDT", {}).get("total") or bal["total"].get("USDT") or 0)
        except Exception:
            return None


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
        # Set when state and the exchange irreconcilably disagree about the
        # open position (side conflict / unexpected extra size). While halted
        # the bot touches NOTHING on the exchange — it won't act on a phantom
        # position nor open new ones — until a human resolves it and restarts.
        self._halted: bool = False
        self._halt_reason: str = ""
        self._last_halt_warn: float = 0.0
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
                # Persistence: require the last N daily readings to all be
                # extreme before blocking (flash-extremes pass through).
                n = max(1, self.cfg.fng_persist_days)
                recent = fng_df["fng"].tail(n)
                greed_persist = len(recent) >= n and bool((recent >= self.cfg.fng_greed_max).all())
                fear_persist = len(recent) >= n and bool((recent <= self.cfg.fng_fear_min).all())
                block_fng = (last_sig ==  1 and greed_persist) \
                         or (last_sig == -1 and fear_persist)
                if block_fng:
                    self.log.info(f"F&G={fng_value} extreme for {n}d, "
                                  f"blocking side={last_sig} (persistence filter)")
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

    def _reconcile_position_with_exchange(self, last_bar: pd.Series) -> bool:
        """Compare our tracked position to the actual exchange position.

        Catches the case where Bybit's exchange-side SL/TP fires autonomously
        but our bar-based check_exit misses it (because the bar-fetch fell back
        to KuCoin spot during a rate-limit, whose H/L differs slightly from
        Bybit perp's).

        Returns True if a reconcile action was taken (caller should skip the
        bar-based check); False to defer to check_exit.
        """
        if self.state.position is None:
            return False
        net = self.ex.fetch_position_size()
        if net is None:
            return False  # exchange unreachable — let bar-based check try
        p = self.state.position
        expected = p.qty * p.side
        if abs(net) < 1e-9:
            # Exchange autonomously closed. Best-effort fill price: if the
            # closed bar's H/L touched our SL or TP, that's the most likely
            # exchange fill; otherwise use the bar close (limited info).
            h, l = float(last_bar["high"]), float(last_bar["low"])
            if p.side == 1:
                if l <= p.sl:   fill, why = p.sl, "sl-external"
                elif h >= p.tp: fill, why = p.tp, "tp-external"
                else:           fill, why = float(last_bar["close"]), "external"
            else:
                if h >= p.sl:   fill, why = p.sl, "sl-external"
                elif l <= p.tp: fill, why = p.tp, "tp-external"
                else:           fill, why = float(last_bar["close"]), "external"
            self.log.warning(f"exchange position is FLAT — booking autonomous "
                             f"close at {fill:.4f} (reason={why})")
            self.close_position(fill, why)
            return True
        # Live mismatch — same halt logic as the resume reconcile (PR #24):
        # never adopt an unexpected size/side mid-run.
        if (net > 0) != (p.side > 0):
            self.log.critical(f"POSITION SIDE CONFLICT mid-run: state={expected:+.6f} "
                              f"vs exchange={net:+.6f}. HALTING {self.cfg.symbol}.")
            self._halted = True
            self._halt_reason = (f"side conflict mid-run "
                                 f"(state {expected:+.4f} vs exchange {net:+.4f})")
            try: self.notifier.error(f"side conflict on {self.cfg.symbol}; bot HALTED")
            except Exception: pass
            return True
        if abs(net) > abs(expected) * 1.05:
            self.log.critical(f"position LARGER mid-run: state={expected:+.6f} "
                              f"vs exchange={net:+.6f}. HALTING {self.cfg.symbol}.")
            self._halted = True
            self._halt_reason = (f"extra size mid-run "
                                 f"(state {expected:+.4f} vs exchange {net:+.4f})")
            try: self.notifier.error(f"extra size on {self.cfg.symbol}; bot HALTED")
            except Exception: pass
            return True
        # Tolerable size mismatch (rounding etc.) — let bar-based check proceed.
        return False

    def _effective_risk(self) -> float:
        """Risk-per-trade with equity-curve decay applied.

        Returns 0.0 when a 0.0-multiplier decay tier is breached (deep-drawdown
        hard stop) — callers must skip opening a new trade in that case.
        Uses the SAME decay_risk_scale() the backtester uses (parity).
        """
        risk = self.cfg.risk_per_trade
        self.state.equity_peak = max(self.state.equity_peak, self.state.equity)
        cur_dd = (self.state.equity / self.state.equity_peak - 1) \
            if self.state.equity_peak > 0 else 0.0
        tiers = self.cfg.eq_decay_tiers
        if tiers:
            scale = decay_risk_scale(cur_dd, tiers)
            if scale != 1.0:
                self.log.info(f"equity-decay tier active: dd={cur_dd*100:.1f}% "
                              f"-> risk scale {scale:.2f} "
                              f"(risk {risk*scale*100:.2f}%)")
            risk *= scale
        elif self.cfg.eq_risk_decay > 0:
            if cur_dd <= -self.cfg.drawdown_for_decay:
                risk *= self.cfg.eq_risk_decay
                self.log.info(f"equity-decay active: dd={cur_dd*100:.1f}% "
                              f"-> risk reduced to {risk*100:.2f}%")
        return risk

    def _roll_day(self) -> None:
        """Reset the daily baseline at UTC midnight. Called every tick so the
        rollover happens even when the bot holds a position across midnight
        without attempting an entry (otherwise day_pnl would span days)."""
        cur_day = int(time.time() // 86400) * 86400
        if cur_day > self.state.day_start_ts:
            self.state.day_start_ts = cur_day
            self.state.day_start_equity = self.state.equity

    def _daily_loss_blocked(self) -> bool:
        """True if today's loss has hit the circuit-breaker."""
        if self.cfg.daily_loss_pct <= 0:
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
        if risk <= 0:
            self.log.warning("deep-drawdown hard stop active — not opening new trade")
            return
        risk_dollars = self.state.equity * risk
        notional = min(risk_dollars / stop_dist, self.state.equity * self.cfg.max_leverage)
        qty = notional / entry_px
        if qty <= 0 or notional < 5:  # min order size sanity
            self.log.info(f"skipping tiny order qty={qty:.6f} notional={notional:.2f}")
            return
        # SL/TP geometry sanity — a malformed pair would close on the next bar.
        if (side == 1 and not (sl < entry_px < tp)) or \
           (side == -1 and not (tp < entry_px < sl)):
            self.log.warning(f"malformed SL/TP for side={side}: "
                             f"sl={sl:.4f} entry≈{entry_px:.4f} tp={tp:.4f} — skipping entry")
            return
        # Round qty to exchange precision + reject below min-amount / min-cost.
        # (Paper mode is pass-through.)
        qty, reject = self.ex.normalize_order(qty, entry_px)
        if reject:
            self.log.info(f"skipping order: {reject}")
            return
        try:
            order = (self.ex.market_buy(qty, sl=sl, tp=tp) if side == 1
                     else self.ex.market_sell(qty, sl=sl, tp=tp))
        except Exception as e:
            self.log.error(f"entry order FAILED: {e}")
            self.events.error(message=f"entry order failed: {e}",
                              exception_type=type(e).__name__)
            # FAIL-SAFE: in live mode the order may have filled on the exchange
            # but the SL/TP attach or response parsing raised. If we just return,
            # we'd have an unprotected open position the bot doesn't know about.
            # Query the exchange; if a position exists, market-close it now.
            net = self.ex.fetch_position_size()
            if net is not None and abs(net) > 1e-9:
                self.log.critical(f"ORPHAN POSITION DETECTED on exchange (net={net}); "
                                  f"force-closing to fail safe")
                try:
                    if net > 0:
                        self.ex.market_sell(abs(net))
                    else:
                        self.ex.market_buy(abs(net))
                except Exception as e2:
                    self.log.critical(f"force-close FAILED: {e2} — MANUAL INTERVENTION "
                                      f"required on Bybit for {self.cfg.symbol}")
                    try:
                        self.notifier.error(f"ORPHAN POSITION on {self.cfg.symbol}; "
                                            f"manual close needed")
                    except Exception:
                        pass
            return
        # Sanity: confirm filled qty ≈ requested (Bybit market orders almost
        # always fill in full, but a tiny qty discrepancy from rounding/maker
        # is normal; >1% gap is suspicious and worth noting).
        filled = float(order.get("filled") or order.get("amount") or qty)
        if abs(filled - qty) / max(qty, 1e-9) > 0.01:
            self.log.warning(f"partial fill: requested {qty:.6f} got {filled:.6f}")
            qty = filled
            notional = filled * float(order.get("price") or entry_px)
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
        order: dict | None = None
        try:
            order = (self.ex.market_sell(pos.qty) if pos.side == 1
                     else self.ex.market_buy(pos.qty))
        except Exception as e:
            # In live mode the exchange-attached SL/TP may have already closed
            # this position autonomously, in which case our market close errors.
            # Reconcile by querying the actual position size; if it's flat we
            # treat the trade as already filled at the SL/TP price.
            net = self.ex.fetch_position_size()
            if net is not None and abs(net) < 1e-9:
                self.log.info(f"close order failed but exchange position is flat "
                              f"({reason}) — assuming exchange SL/TP filled")
                order = None  # signal "already closed; use theoretical price"
            else:
                self.log.error(f"close order FAILED ({reason}): {e} — retrying next tick")
                self.events.error(message=f"close order failed: {e}",
                                  exception_type=type(e).__name__)
                return
        fill_px = float(order.get("price") or exit_px_hint) if order else exit_px_hint
        # Paper mode: use the SL/TP price when they triggered — matches the
        # backtest convention. Live mode: keep the ACTUAL fill when we have it.
        # The "exchange already closed" reconcile path also uses the SL/TP price
        # (best estimate without an extra trades-history query).
        if self.cfg.mode != "live" or order is None:
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
        if pnl > 0:
            self.state.realised_wins += 1
        self.state.realised_pnl += pnl
        # Persist immediately: a crash after the fill but before the end-of-tick
        # save must not resurrect an already-closed position on restart.
        self.state.position = None
        self.state.save(self.cfg.state_file)
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
        # True win rate from the per-trade win counter (0.0 when no trades yet).
        wr = (self.state.realised_wins / self.state.realised_trades
              if self.state.realised_trades > 0 else 0.0)
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

    def _fetch_bars_for_signal(self) -> pd.DataFrame:
        """Bars for signal computation.

        Adaptive presets need ~1y of history for the regime GMM to fit the way
        the backtest fit it (walk-forward trained on 365d + 30d feature
        warmup). ccxt's fetch_ohlcv caps at ~1000 bars per call (= 41 days on
        1h), which starves the GMM and yields regime labels that differ from
        the validated backtest. So: pull deep history through the cached
        KuCoin path (shared 1h-TTL cache across all portfolio bots) and splice
        the fresh ccxt tail on top. Signals only act on closed bars, so a
        <=1h-stale deep section is exact for a 1h-bar strategy.
        """
        recent = self.ex.fetch_recent(n=300)
        if not self.cfg.use_adaptive_regime:
            return recent
        try:
            from core.data import fetch_ohlcv
            sym = self.cfg.symbol.replace("/", "-").replace(":USDT", "")
            deep = fetch_ohlcv(sym, self.cfg.timeframe, days=400, use_cache=True)
            df = pd.concat([deep, recent])
            df = df[~df.index.duplicated(keep="last")].sort_index()
            return df
        except Exception as e:
            self.log.warning(f"deep history fetch failed ({e}); "
                             f"regime fit degraded to {len(recent)} bars")
            return recent

    def tick(self) -> None:
        # Halted: state and exchange irreconcilably disagree. Do NOTHING that
        # touches the exchange (no exits on a phantom position, no new entries)
        # until a human resolves it and restarts. Just heartbeat + warn hourly.
        if self._halted:
            self._write_heartbeat()
            now = time.time()
            if now - self._last_halt_warn > 3600:
                self._last_halt_warn = now
                self.log.critical(f"HALTED ({self._halt_reason}); idle until manual "
                                  f"reconcile + restart of {self.cfg.symbol}")
            return

        # Daily summary reports PnL for the day that just ended (uses the
        # current day_start_equity), THEN we roll the daily baseline. Rolling
        # here (not lazily inside the circuit breaker) keeps day_pnl correct
        # even when a position is held across midnight with no entry attempt.
        self._maybe_emit_daily_summary()
        self._roll_day()

        # Bar-close gate: signals only change at bar close. With 5+ portfolio
        # bots polling every ~30s, fetching 300 bars on EVERY tick burns
        # through exchange rate limits (Bybit retCode 10006) — but on 1h
        # bars the strategy only acts ~once an hour. Skip the heavy fetch
        # when we're still inside the bar we already processed; just
        # heartbeat and return. In live mode, exchange-side SL/TP fires
        # autonomously when triggered — we don't need to be polling.
        bar_sec = Exchange.TF_SECONDS.get(self.cfg.timeframe, 3600)
        now = int(time.time())
        if self.state.last_bar_ts > 0 and now < self.state.last_bar_ts + bar_sec + 30:
            self._write_heartbeat()
            return

        df = self._fetch_bars_for_signal()
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
            # LIVE: detect exchange-side autonomous closes (Bybit's attached
            # SL/TP fired but our bar-based check missed it — e.g. the KuCoin
            # REST fallback used while rate-limited has a slightly different
            # bar low/high than Bybit's actual perp). One extra
            # fetch_positions per bar advance ≈ 1/hour, negligible API cost.
            if self.cfg.mode == "live" and not self._reconcile_position_with_exchange(last_bar):
                reason = self.check_exit(last_bar)
                if reason is not None:
                    self.close_position(float(last_bar["close"]), reason)
            elif self.cfg.mode != "live":
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

        self._write_heartbeat()

    def _write_heartbeat(self) -> None:
        """Touched every tick (including bar-gated early returns) so a hung
        bot — deadlock, network stall — shows up as Unhealthy in `docker ps`
        even when no new bar has closed."""
        try:
            hb = Path(self.cfg.state_file).parent / "heartbeat"
            hb.write_text(str(int(time.time())))
        except Exception:
            pass

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
        # ----- LIVE-mode preflight: validate keys, warn on stale state, reconcile -----
        if self.cfg.mode == "live":
            bal = self.ex.fetch_balance_usdt()
            if bal is None:
                msg = ("LIVE pre-flight FAILED: could not fetch balance — check "
                       "API_KEY/API_SECRET, key permissions (Contract Trade), "
                       "and IP whitelist. Refusing to start.")
                self.log.critical(msg)
                try: self.notifier.error(msg)
                except Exception: pass
                raise SystemExit(2)
            self.log.info(f"LIVE balance check OK: {bal:.2f} USDT on exchange")
            # Paper-carryover guard: compare state.equity against THIS bot's
            # own slice (starting_equity), NOT the shared account balance. In a
            # portfolio every bot sees the FULL account via fetch_balance, so
            # comparing to it false-trips on every bot (a $45 slice vs a $300
            # account => ratio 0.15). Paper state carried into live instead
            # shows up as equity wildly off this bot's slice (e.g. a paper INJ
            # that ran to $20k vs a $120 live slice).
            slice_ratio = self.state.equity / max(self.cfg.starting_equity, 1e-9)
            if self.state.realised_trades > 0 and not (0.2 <= slice_ratio <= 5.0):
                self.log.warning(
                    f"STATE/SLICE MISMATCH: state.equity={self.state.equity:.2f} vs "
                    f"this bot's starting slice {self.cfg.starting_equity:.2f} "
                    f"(ratio {slice_ratio:.2f}). Likely paper state carried into "
                    f"live. STOP and wipe with `down -v`, or set TRUST_STATE=1 "
                    f"to override.")
                if os.getenv("TRUST_STATE", "") != "1":
                    raise SystemExit(3)
            # Anchor each bot's equity_peak to its OWN starting equity, not the
            # whole-account balance. In a portfolio every bot shares one Bybit
            # account, so reconciling against the full balance poisons the
            # peak (e.g. ada with $45 vs $300 account => instant -85% "dd",
            # tripping the 0.0x decay tier and freezing entries). Trust the
            # state's own peak; only floor it to the bot's starting equity in
            # case it had drifted lower from a fresh init.
            self.state.equity_peak = max(self.state.equity_peak,
                                         self.state.equity,
                                         self.cfg.starting_equity)
            # Self-heal a poisoned peak left over from earlier code that
            # reconciled against the whole-account balance. If the bot has
            # made 0 trades, the peak can only legitimately be at most the
            # current equity / starting equity — anything higher is the bug.
            if self.state.realised_trades == 0:
                clean_peak = max(self.state.equity, self.cfg.starting_equity)
                if self.state.equity_peak > clean_peak * 1.05:
                    self.log.warning(
                        f"healing poisoned equity_peak: {self.state.equity_peak:.2f} "
                        f"-> {clean_peak:.2f} (0 trades on file)")
                    self.state.equity_peak = clean_peak
                    self.state.save(self.cfg.state_file)

        if self.state.position:
            p = self.state.position
            self.log.info(f"resuming with open position: side={p.side} qty={p.qty:.6f} "
                          f"entry={p.entry_px:.4f} sl={p.sl:.4f} tp={p.tp:.4f}")
            # LIVE: verify the position is still actually on the exchange.
            if self.cfg.mode == "live":
                net = self.ex.fetch_position_size()
                expected = p.qty * p.side
                if net is None:
                    self.log.warning("could not verify open position on exchange "
                                     "(fetch_positions failed); continuing with state as-is")
                elif abs(net) < 1e-9:
                    self.log.warning(f"position in state but exchange is flat — "
                                     f"the SL/TP must have fired while bot was down. "
                                     f"Clearing state position; PnL will not be recorded.")
                    self.state.position = None
                    self.state.save(self.cfg.state_file)
                elif (net > 0) != (p.side > 0):
                    # Exchange shows the OPPOSITE side from our state. Never
                    # adopt — this means another source touched the account
                    # (manual trade, or — only if mis-deployed — a second
                    # process on this symbol). Refuse to manage a position we
                    # didn't open; require human attention.
                    self.log.critical(
                        f"POSITION SIDE CONFLICT: state={expected:+.6f} but "
                        f"exchange={net:+.6f}. HALTING this bot — manual review "
                        f"needed for {self.cfg.symbol}.")
                    self._halted = True
                    self._halt_reason = (f"side conflict (state {expected:+.4f} "
                                         f"vs exchange {net:+.4f})")
                    try: self.notifier.error(f"position side conflict on "
                                             f"{self.cfg.symbol}; bot HALTED, manual review")
                    except Exception: pass
                elif abs(net) < abs(expected) * 0.95:
                    # Exchange has LESS than we recorded (e.g. partial SL/TP
                    # fill, or rounding). Safe to shrink our accounting to the
                    # exchange truth — we never want to try to close more than
                    # exists.
                    self.log.warning(f"position smaller on exchange: state={expected:+.6f} "
                                     f"exchange={net:+.6f} — shrinking to exchange size")
                    self.state.position.qty = abs(net)
                    self.state.save(self.cfg.state_file)
                elif abs(net) > abs(expected) * 1.05:
                    # Exchange has MORE than we opened. Do NOT adopt the larger
                    # size — managing stops against a position we didn't fully
                    # open is the money-losing failure mode. Keep our own
                    # (smaller) accounting and flag for review.
                    self.log.critical(
                        f"position LARGER on exchange than state: state={expected:+.6f} "
                        f"exchange={net:+.6f}. HALTING this bot — manual review "
                        f"needed for {self.cfg.symbol}.")
                    self._halted = True
                    self._halt_reason = (f"extra size (state {expected:+.4f} "
                                         f"vs exchange {net:+.4f})")
                    try: self.notifier.error(f"unexpected extra size on "
                                             f"{self.cfg.symbol}; bot HALTED, manual review")
                    except Exception: pass
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
            # Jitter de-synchronizes the portfolio bots (5+ on one IP) so they
            # don't all hit the exchange API in the same instant.
            time.sleep(self.cfg.poll_seconds + random.uniform(0, 5))
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
