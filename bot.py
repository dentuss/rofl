"""Live trading bot. Paper by default; live on Bybit USDT-perps with keys.

One symbol per process. The DEPLOYED program (2026-07-08) is the 4h
BLEND50_CONF book — 8 majors x two presets at 50/50 capital, launched by
docker-compose.bidir4h-live.yml (see deploy/LIVE.md):
  adaptive_bidir_4h   triple_bidir tp6 (EMA 9/26/50 stack + RSI 55/45 +
                      ADX 22, sl 1.8x / tp 6x ATR via TL_TP_MULT)
  pullback_bidir_4h   pullback_in_trend (EMA50 side + RSI 40/60 recross,
                      same stops) — the low-correlation second leg
plus the adopted overlay stack: walk-forward regime mask, F&G 3-day
persistence, 3-tier drawdown decay, CHOP half-sizing (CHOP_RISK_MULT),
vol targeting (VOL_TARGET_ANN), GMM-confidence sizing (REGIME_CONF_SIZING),
post-SL cooldown (COOLDOWN_BARS), maker entries (ENTRY_LIMIT_ORDERS) and
TP-as-limit exits (TP_LIMIT_ORDERS).

Honest expectations and every adopted/rejected layer: research/FINDINGS.md.
Numbers in older preset comments below are ARTIFACT-ERA (pre-2026-07-05)
and invalid — kept only as historical context on the legacy presets.

Default mode is PAPER (dry-run). MODE=live + keys trades real money; the
go-live program (research/ROADMAP.md Phase 6) gates when that is allowed.

Run:
  python3 bot.py                                   # paper, default preset
  STRATEGY_PRESET=adaptive_bidir_4h SYMBOL=BTC/USDT python3 bot.py
  docker compose -f docker-compose.bidir4h-live.yml up -d   # the real thing
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import time
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from core.event_log import EventLog
from core.notifier import Notifier
from core.risk import DEFAULT_DECAY_TIERS, decay_risk_scale, parse_tiers
from core.sentiment import fetch_fear_greed
from core.strategies import (donchian_breakout, pullback_in_trend,
                             triple_confirm_bidir, triple_confirm_long)
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
    # Was the 1h portfolio preset (launchers removed in the 2026-07-08
    # cleanup; artifact-era numbers void). Kept for ad-hoc single runs.
    "adaptive_bidir":           ("triple_bidir", "INJ/USDT", "1h", 0.020, 5.0, False, False, True),

    # 4h port of adaptive_bidir — the HONEST-REBUILD validated config
    # (research/FINDINGS.md 2026-07-05, honest_rebuild_r2/r3 on the FIXED
    # engine): same signal + regime + F&G + decay, 4h bars, TL_TP_MULT=6.0,
    # COOLDOWN_BARS=0 (on the fixed engine a 1-bar cooldown is subsumed by
    # the same-bar-re-entry fix; no K re-validated for 4h yet).
    # SOFT5 2.88y Bybit: CAGR 15.1%, Sharpe(mo) 0.98, MDD -9.6%; passed
    # universe (10/11), sub-window, and random-entry-null gates. Expectations
    # are MODEST and honest — see FINDINGS before touching risk numbers.
    "adaptive_bidir_4h":        ("triple_bidir", "INJ/USDT", "4h", 0.020, 5.0, False, False, True),

    # Pullback-in-trend 4h — the SECOND leg of the promoted BLEND50_CONF
    # trend book (research/FINDINGS.md 2026-07-06): EMA50 side + RSI recross
    # of 40/60, sl 1.8 / tp 6.0 ATR (set TL_TP_MULT=6.0). Same overlay stack
    # as adaptive_bidir_4h (regime + F&G persistence + decay + CHOP half-size
    # + VT + cooldown). Runs at HALF the book's capital next to a triple
    # service on the same symbol. Standalone: Sh(mo) ~1.35, MDD -2.1%, G3
    # 98th pct; blend with triple: Sh 1.47-1.52. Symbol-agnostic via SYMBOL=.
    "pullback_bidir_4h":        ("pullback_trend", "BTC/USDT", "4h", 0.020, 5.0, False, False, True),

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
                 "adaptive_bidir", "adaptive_bidir_4h", "pullback_bidir_4h"}

# Presets that use ML regime detection — block longs in BEAR.
# Bidirectional presets ALSO block shorts in BULL (directional filter).
ADAPTIVE_PRESETS = {"adaptive_inj_growth", "adaptive_inj_high_return",
                    "adaptive_inj_bidir", "adaptive_inj_bidir_wf",
                    "adaptive_bidir", "adaptive_bidir_4h",
                    "pullback_bidir_4h"}

# Presets that apply the F&G extreme-zone filter on top of the bidir signal:
#   - block longs when F&G >= 80 (extreme greed)
#   - block shorts when F&G <= 20 (extreme fear)
# 5y backtest on INJ 1h: same return as without, MDD improved ~6pp.
FNG_EXTREME_PRESETS = {"adaptive_inj_bidir", "adaptive_inj_bidir_wf",
                       "adaptive_bidir", "adaptive_bidir_4h",
                       "pullback_bidir_4h"}


def vol_target_mult(closes: "pd.Series", target_ann: float,
                    clip_lo: float = 0.5, clip_hi: float = 1.5) -> float:
    """Vol-target risk multiplier, parity with research/vol_target.vt_mult:
    trailing 30d std of daily returns (annualized) using COMPLETE days only —
    the in-progress day is dropped, matching the backtest's shift(1). Returns
    1.0 when there is not enough history (min 21 complete days)."""
    try:
        daily = closes.resample("1D").last().dropna()
        if len(daily) and daily.index[-1].normalize() == \
                closes.index[-1].normalize():
            daily = daily.iloc[:-1]          # drop the partial/current day
        if len(daily) < 21:
            return 1.0
        vol = float(daily.pct_change().tail(30).std()) * (365.0 ** 0.5)
        if not vol or vol != vol:
            return 1.0
        return min(max(target_ann / vol, clip_lo), clip_hi)
    except Exception:
        return 1.0

# Presets that read dynamic (ema_fast, ema_slow, rsi_min) from params_file
# (written by research/retune.py). Falls back to BotConfig defaults if the
# file is missing or unreadable.
WF_RETUNE_PRESETS = {"adaptive_inj_bidir_wf"}

# Bybit non-VIP linear-perp fees, matching research/cost_engine.py exactly.
# Booking used to charge FEE_TAKER on BOTH sides unconditionally, which
# overstated cost on every maker leg: the deployed stack runs post-only maker
# entries (ENTRY_LIMIT_ORDERS) and TP-as-limit exits (TP_LIMIT_ORDERS), so a
# maker-in / TP-out round trip really costs ~4bp but was booked at 12bp. That
# understates state.equity, which drives risk sizing, vol targeting and the
# decay ladder — and would show up in L2's live-vs-engine reconcile as an
# unexplained cost gap against the >0.2 Sh HALT criterion. Override for a VIP
# tier via env. (2026-08-03)
FEE_TAKER = float(os.getenv("FEE_TAKER", "0.0006"))
FEE_MAKER = float(os.getenv("FEE_MAKER", "0.0002"))


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
    # Post-stop same-side re-entry cooldown: after a SL on side X, block new
    # side-X entries for this many bars. Targets the post-stop V-bounce whipsaw.
    # Validated on the full production stack (5 pairs, 3.7y, walk-forward + OOS):
    # K=3 lifts portfolio Sharpe 2.27->3.46, worst month -8.6%->-2.6%, beats the
    # no-cooldown baseline in 41/44 months. 0 disables. See research/FINDINGS.md.
    cooldown_bars: int = int(os.getenv("COOLDOWN_BARS", "3"))
    # CHOP half-sizing (honest-era adoption 2026-07-05, baseline_promote.py):
    # scale risk-per-trade by this factor while the detected regime is CHOP.
    # Mirrors the backtester's risk_mult column. Default 1.0 = OFF (no
    # behavior change until explicitly enabled via env CHOP_RISK_MULT=0.5).
    chop_risk_mult: float = float(os.getenv("CHOP_RISK_MULT", "1.0"))
    # GMM-confidence sizing (adopted 2026-07-06, research/regime_upgrades.py):
    # risk x (0.5 + 0.5 * posterior of the detected regime). Off by default;
    # the paper compose enables it. Uses the same full-history fit as the
    # live regime label (the research validation used walk-forward fits —
    # same acknowledged approximation as the label itself).
    regime_conf_sizing: bool = os.getenv("REGIME_CONF_SIZING", "0") == "1"
    # TP as a resting LIMIT order on the exchange (engine parity for
    # tp_as_limit, adopted 2026-07-06 — maker fee on the profit side).
    # Off by default; enable in live only after the stage-A minimum-size
    # smoke test (ROADMAP Phase 6). No effect in paper mode.
    tp_limit_orders: bool = os.getenv("TP_LIMIT_ORDERS", "0") == "1"
    # Post-only maker ENTRIES (engine parity for entry_style="maker_close",
    # the adopted cost model): limit at the signal bar's close, resting for
    # exactly one bar; unfilled -> cancelled, a persisting signal re-places
    # at the next close. Live mode only; paper behavior unchanged.
    entry_limit_orders: bool = os.getenv("ENTRY_LIMIT_ORDERS", "0") == "1"
    # Vol-targeted sizing (honest-era adoption 2026-07-05, vol_target.py):
    # scale risk by clip(target / trailing-30d-annualized-vol, 0.5, 1.5),
    # vol from COMPLETE daily closes only (parity with the backtest's
    # shift(1)). 0 = OFF (default — no behavior change until enabled, e.g.
    # VOL_TARGET_ANN=0.60).
    vol_target_ann: float = float(os.getenv("VOL_TARGET_ANN", "0"))
    allow_short_override: str = os.getenv("ALLOW_SHORT", "")
    state_file: str = os.getenv("STATE_FILE", "bot_state.json")
    log_file: str = os.getenv("LOG_FILE", "bot.log")
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "30"))
    # Deterministic per-symbol fetch stagger (seconds). Portfolio bots share one
    # IP; without staggering they all fetch within a few seconds of each bar
    # close and burst Bybit's per-IP rate limit (retCode 10006 -> KuCoin
    # fallback). Each symbol waits a stable 0..fetch_stagger_secs past the
    # settle buffer before its first fetch, spreading the 5 bots out.
    fetch_stagger_secs: int = int(os.getenv("FETCH_STAGGER_SECS", "45"))
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
    # True when opened via a post-only maker entry (ENTRY_LIMIT_ORDERS). The
    # fill happened MID-bar, so a TP print on the fill bar may predate the
    # fill — check_exit suppresses the bar-based TP check on that first bar
    # (engine parity: maker entries never credit a same-bar TP; the exchange
    # TP order is the source of truth and reconcile books real fills).
    maker_entry: bool = False


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
    # Post-stop same-side re-entry cooldown: bar timestamp (unix s) until which a
    # new same-side entry is blocked after a stop-loss on that side. 0 = no block.
    block_long_until_ts: int = 0
    block_short_until_ts: int = 0
    # ENTRY_LIMIT_ORDERS: resting post-only entry, checked every tick and
    # finalized (cancel / adopt) at the next bar close. Keys: order_id, side,
    # qty, limit_px, sl, tp, signal_bar_ts, risk_pct, created_ts.
    pending_entry: Optional[dict] = None

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
            block_long_until_ts=d.get("block_long_until_ts", 0),
            block_short_until_ts=d.get("block_short_until_ts", 0),
            pending_entry=d.get("pending_entry"),
        )

    def save(self, path: str) -> None:
        """Atomic write (tmp + replace): a crash mid-write must never leave a
        truncated state.json — with a live position that would crash-loop the
        restarted bot while the position runs unmanaged."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f"{p.name}.tmp{os.getpid()}"
        tmp.write_text(self.to_json())
        os.replace(tmp, p)


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

    def _attached_sltp_params(self, sl: float | None, tp: float | None,
                              qty: float | None = None) -> dict:
        """Build ccxt params to attach SL/TP to the entry order.
        Bybit V5 accepts stopLoss/takeProfit on the entry; the exchange then
        manages the conditional orders autonomously, so the position is
        protected even if the bot is down.

        TP_LIMIT_ORDERS=1 (engine parity for tp_as_limit, adopted 2026-07-06):
        the TP becomes a LIMIT order at the target (maker 0.02%) instead of
        the default conditional market (taker 0.055% + slip). Bybit requires
        tpslMode=Partial for limit-type TPs. The SL deliberately stays a
        MARKET conditional (slOrderType defaults to Market) — a stop resting
        as a limit can fail to fill in the gap it exists to protect against.

        2026-08-06 — this previously failed live with retCode 10001 "Request
        parameter error", so EVERY entry was skipped and the book never
        traded. Two faults, both fixed here:
          1. tpSize / slSize are NOT parameters of /v5/order/create. They
             belong to /v5/position/trading-stop. Bybit rejects the unknown
             fields. Under tpslMode=Partial on an entry order the TP/SL cover
             that order's qty, which is what we want anyway.
          2. Bybit V5 takes every numeric as a STRING. ccxt stringifies the
             fields it knows (price, qty, stopLoss, takeProfit) but passes
             custom params through raw, so tpLimitPrice went as a JSON float.
        Verified against the documented linear example, which carries
        tpslMode/tpOrderType/tpLimitPrice and no sizes."""
        if sl is None and tp is None or self.paper:
            return {}
        params: dict = {}
        if sl is not None:
            params["stopLoss"] = self._fmt_price(sl)
        if tp is not None:
            params["takeProfit"] = self._fmt_price(tp)
            if self.cfg.tp_limit_orders and qty is not None:
                params["tpslMode"] = "Partial"
                params["tpOrderType"] = "Limit"
                params["tpLimitPrice"] = self._fmt_price(tp)
        return params

    def _fmt_price(self, p: float) -> str:
        """Round to the symbol's tick AND return a STRING — Bybit V5 rejects
        bare numbers on custom params with retCode 10001."""
        rounded = self._round_price(p)
        if self._ccxt is not None:
            try:
                return str(self._ccxt.price_to_precision(self._ccxt_symbol(), rounded))
            except Exception:
                pass
        return str(rounded)

    def market_buy(self, qty: float, sl: float | None = None,
                   tp: float | None = None, reduce_only: bool = False) -> dict:
        if self.paper:
            px = self.fetch_price()
            return {"id": f"paper-{int(time.time())}", "price": px, "amount": qty}
        params = self._attached_sltp_params(sl, tp, qty=qty)
        if reduce_only:
            # Closes MUST be reduce-only. If the exchange already flattened the
            # position (its attached SL/TP fired autonomously), a plain market
            # order would OPEN a new reversed, UNPROTECTED position instead of
            # closing. reduce_only makes Bybit reject the order on a flat book,
            # which close_position's except-branch then books as "already closed".
            params["reduceOnly"] = True
        return self._ccxt.create_market_buy_order(self._ccxt_symbol(), qty,
                                                   params=self._ccxt_params(params))

    def market_sell(self, qty: float, sl: float | None = None,
                    tp: float | None = None, reduce_only: bool = False) -> dict:
        if self.paper:
            px = self.fetch_price()
            return {"id": f"paper-{int(time.time())}", "price": px, "amount": qty}
        params = self._attached_sltp_params(sl, tp, qty=qty)
        if reduce_only:  # see market_buy: never let a close re-open a position
            params["reduceOnly"] = True
        return self._ccxt.create_market_sell_order(self._ccxt_symbol(), qty,
                                                    params=self._ccxt_params(params))

    def limit_entry(self, side: int, qty: float, price: float,
                    sl: float | None, tp: float | None) -> dict:
        """Post-only limit ENTRY at `price` with attached SL/TP (live only —
        callers gate on mode). postOnly guarantees maker-or-cancel: if the
        price would cross the book, Bybit rejects instead of taking."""
        params = self._attached_sltp_params(sl, tp, qty=qty)
        params["postOnly"] = True
        px = self._round_price(price)
        if side == 1:
            return self._ccxt.create_limit_buy_order(
                self._ccxt_symbol(), qty, px, params=self._ccxt_params(params))
        return self._ccxt.create_limit_sell_order(
            self._ccxt_symbol(), qty, px, params=self._ccxt_params(params))

    def fetch_order_status(self, order_id: str) -> dict:
        """Normalized order status: {status: open|closed|canceled|rejected|
        unknown, filled: float, avg_px: float|None}."""
        o = self._ccxt.fetch_order(order_id, self._ccxt_symbol(),
                                   params=self._ccxt_params())
        status = (o.get("status") or "unknown").lower()
        filled = float(o.get("filled") or 0.0)
        avg = o.get("average")
        ts = o.get("lastTradeTimestamp") or o.get("timestamp")
        return {"status": status, "filled": filled,
                "avg_px": float(avg) if avg else None,
                "ts_ms": int(ts) if ts else None}

    def cancel_order(self, order_id: str) -> bool:
        """Best-effort cancel. False (not an exception) when the order is
        already gone — filled or cancelled — so callers re-check status."""
        try:
            self._ccxt.cancel_order(order_id, self._ccxt_symbol(),
                                    params=self._ccxt_params())
            return True
        except Exception as e:
            self.log.info(f"cancel_order {order_id}: {e} (already gone?)")
            return False

    def cancel_all(self) -> None:
        """Fail-safe sweep of resting orders for our symbol (used when a
        limit-entry placement raises after possibly reaching the exchange)."""
        try:
            self._ccxt.cancel_all_orders(self._ccxt_symbol(),
                                         params=self._ccxt_params())
        except Exception as e:
            self.log.warning(f"cancel_all failed: {e}")

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

    def fetch_last_closed_fill(self, pos, since_ms: int | None = None) -> dict | None:
        """Real average exit price of the most recent closed position on this
        symbol, from Bybit's closed-PnL history.

        Used to book an autonomous close (the exchange's attached SL/TP fired)
        at the ACTUAL fill instead of the theoretical SL/TP price — a market
        stop slips past its trigger, so the theoretical price systematically
        overstates PnL and is the main source of the booked-vs-exchange gap.
        Read-only, live-only. Returns None (caller then falls back to the
        theoretical price) if unavailable or there is no confident match.
        """
        if self.paper or self._ccxt is None:
            return None
        try:
            sym_id = (self._load_market() or {}).get("id")
            if not sym_id:
                return None
            params = self._ccxt_params({"symbol": sym_id, "limit": 20})
            if since_ms:
                params["startTime"] = int(since_ms)
            resp = self._ccxt.private_get_v5_position_closed_pnl(params)
            rows = (resp.get("result") or {}).get("list") or []
        except Exception as e:
            self.log.warning(f"closed-PnL fetch failed ({e})")
            return None
        # Rows are newest-first; take the most recent close whose qty matches our
        # position (each bot runs at most one position per symbol at a time).
        for r in rows:
            try:
                qty = float(r.get("qty") or 0)
                exit_px = float(r.get("avgExitPrice") or 0)
            except (TypeError, ValueError):
                continue
            if exit_px <= 0 or qty <= 0:
                continue
            if abs(qty - pos.qty) > max(pos.qty * 0.05, 1e-9):
                continue
            # Callers need the real exit price; the fill timestamp additionally
            # lets the resume path arm the re-entry cooldown at the correct bar.
            # Parse the timestamp defensively — a malformed row must never crash
            # the booking path (the reason the return is otherwise kept minimal).
            try:
                updated_ms = int(float(r.get("updatedTime") or 0)) or None
            except (TypeError, ValueError):
                updated_ms = None
            return {"exit_px": exit_px, "qty": qty, "updated_ms": updated_ms}
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
        self._vt_mult: float = 1.0
        self._regime_conf: float = 1.0   # posterior of the detected regime
        self._last_daily_summary_day: int = 0
        # Set when state and the exchange irreconcilably disagree about the
        # open position (side conflict / unexpected extra size). While halted
        # the bot touches NOTHING on the exchange — it won't act on a phantom
        # position nor open new ones — until a human resolves it and restarts.
        self._halted: bool = False
        self._halt_reason: str = ""
        self._last_halt_warn: float = 0.0
        # Stable per-symbol offset (see BotConfig.fetch_stagger_secs). crc32 is
        # used (not Python's salted hash) so the offset is identical every run.
        self._fetch_stagger = zlib.crc32(self.cfg.symbol.encode()) \
            % (max(self.cfg.fetch_stagger_secs, 0) + 1)
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
        elif self.cfg.strategy == "pullback_trend":
            # Promoted 2026-07-06 (BLEND50_CONF second leg). band=40 is the
            # pre-registered constant; sl/tp share the TL_* envs (1.8 / 6.0).
            sig = pullback_in_trend(
                df,
                ema_n=self.cfg.ema_trend,
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
                gmm, _mapping, regime_series = _regime_fit_predict(
                    df, bars_per_day=bpd)
                regime_label = regime_series.iloc[-1]
                if self.cfg.regime_conf_sizing:
                    try:
                        from core.regime import build_features, feature_matrix
                        fm = feature_matrix(build_features(df,
                                                           bars_per_day=bpd))
                        import numpy as _np
                        valid = fm.iloc[[-1]].replace(
                            [_np.inf, -_np.inf], _np.nan).dropna()
                        self._regime_conf = float(
                            gmm.predict_proba(valid.values).max()) \
                            if len(valid) else 1.0
                    except Exception as e:
                        self.log.warning(f"regime conf failed, neutral 1.0: {e}")
                        self._regime_conf = 1.0
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
        # Maker fills happen MID-bar: a TP print on the fill bar may predate
        # the fill, so the bar-based TP check is unreliable there. Suppress it
        # (engine parity: maker entries never credit a same-bar TP). A REAL
        # post-fill TP fills the exchange-side TP order and is booked by the
        # reconcile path instead. The same-bar SL stays honored (any path to
        # the stop passed through the fill first).
        suppress_tp = getattr(pos, "maker_entry", False) and pos.bars_open <= 1
        if pos.side == 1:
            if l <= pos.sl:
                return "sl"
            if h >= pos.tp and not suppress_tp:
                return "tp"
        else:  # short — used by triple_bidir and any preset with allow_short=True
            if h >= pos.sl:
                return "sl"
            if l <= pos.tp and not suppress_tp:
                return "tp"
        if pos.bars_open >= self.cfg.max_bars_in_trade:
            return "time"
        # NOTE: deliberately NO signal-flip exit. The backtest closes (and
        # reverses) a position when the opposite signal appears; the live bot
        # only evaluates new signals when flat, so it holds through flips.
        # Measured immaterial: across the 5 prod pairs over 1y this fires <1%
        # of exits and those trades are slightly net-negative — a full opposite
        # EMA-stack+RSI reversal almost always trips the 1.8xATR stop first.
        # Accepted approximation. (test_parity only checks the signal series,
        # NOT position management — re-measure if exit logic ever changes.)
        return None

    def _reconcile_position_with_exchange(self, last_bar: pd.Series,
                                          bar_ts: int | None = None) -> bool:
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
            if why == "external":
                # The KuCoin-fallback bar didn't confirm a touch, but a real perp
                # SL can wick past a level the spot bar missed. Consult the real
                # closed-PnL fill: if it landed nearer the stop than the target,
                # treat it as a stop so the same-side cooldown still arms (matches
                # the backtest). PnL is booked at the real fill by close_position
                # either way, so this only affects cooldown classification.
                real = self.ex.fetch_last_closed_fill(p, since_ms=p.open_ts * 1000)
                if real is not None and abs(real["exit_px"] - p.sl) <= abs(real["exit_px"] - p.tp):
                    why = "sl-external"
            self.log.warning(f"exchange position is FLAT — booking autonomous "
                             f"close at {fill:.4f} (reason={why})")
            self.close_position(fill, why, bar_ts=bar_ts)
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
        # CHOP half-sizing (parity with the backtester's regime risk_mult):
        # scale down while the current detected regime is CHOP. Off at 1.0.
        if self.cfg.chop_risk_mult != 1.0 and self._last_regime == "CHOP":
            risk *= self.cfg.chop_risk_mult
            self.log.info(f"CHOP regime: risk x{self.cfg.chop_risk_mult:.2f} "
                          f"-> {risk*100:.2f}%")
        # Vol targeting (parity with research/vol_target.vt_mult); _vt_mult is
        # refreshed each signal cycle from complete daily closes.
        if self.cfg.vol_target_ann > 0 and self._vt_mult != 1.0:
            risk *= self._vt_mult
            self.log.info(f"vol target: risk x{self._vt_mult:.2f} "
                          f"-> {risk*100:.2f}%")
        # GMM-confidence sizing (parity with research risk_mult x
        # (0.5 + 0.5 * p_label)); _regime_conf refreshed each signal cycle.
        if self.cfg.regime_conf_sizing and self._regime_conf < 1.0:
            m = 0.5 + 0.5 * self._regime_conf
            risk *= m
            self.log.info(f"regime conf {self._regime_conf:.2f}: risk x{m:.2f} "
                          f"-> {risk*100:.2f}%")
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

    def enter_position(self, signal_info: dict, bar_ts: int | None = None) -> None:
        side = signal_info["signal"]
        sl = signal_info["sl"]
        tp = signal_info["tp"]
        if sl is None or tp is None or side == 0:
            return
        if self.state.pending_entry is not None:
            # A maker entry is still resting; the rollover finalizer clears
            # it before any new placement. Never stack orders.
            self.log.info("entry skipped: a maker entry is already resting")
            return
        # Post-stop same-side re-entry cooldown (see close_position). Block a new
        # same-side entry until the recorded bar timestamp. Skip BEFORE touching
        # the exchange so a blocked entry costs no API call.
        if self.cfg.cooldown_bars > 0 and bar_ts is not None:
            until = (self.state.block_long_until_ts if side == 1
                     else self.state.block_short_until_ts)
            if bar_ts < until:
                self.log.info(f"re-entry cooldown active ({'LONG' if side == 1 else 'SHORT'}): "
                              f"skipping entry until bar ts {until}")
                return
        if self._daily_loss_blocked():
            return
        # Maker mode: size and price at the SIGNAL bar's close (the engine's
        # maker_close limit price). Taker mode: at the current ticker.
        maker_mode = self.cfg.entry_limit_orders and self.cfg.mode == "live"
        if maker_mode and signal_info.get("close"):
            entry_px = float(signal_info["close"])
        else:
            if maker_mode:
                self.log.warning("maker entry: signal close missing — "
                                 "taker fallback for this entry")
                maker_mode = False
            entry_px = self.ex.fetch_price()
        stop_dist = abs(entry_px - sl) / entry_px
        if stop_dist < 1e-5:
            self.log.warning("stop too tight, skipping")
            return
        risk = self._effective_risk()
        if risk <= 0:
            self.log.warning("deep-drawdown hard stop active — not opening new trade")
            # Surface the freeze (throttled): a stale/poisoned equity_peak silently
            # blocks ALL entries until a restart self-heals it (see the 06-19
            # freeze in the archives). Alert once a day so it can't go unnoticed.
            now = time.time()
            if now - getattr(self, "_last_freeze_alert", 0.0) > 86400:
                self._last_freeze_alert = now
                try:
                    self.notifier.error(
                        f"{self.cfg.symbol}: deep-drawdown hard-stop is blocking "
                        f"entries (equity {self.state.equity:.2f} vs peak "
                        f"{self.state.equity_peak:.2f}). If that drawdown looks "
                        f"wrong, equity_peak may be stale — restart to self-heal.")
                except Exception:
                    pass
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
        # qty may have been floored to the exchange lot step (e.g. ETH's 0.01),
        # dropping it below the risk-target size. Recompute notional from the
        # ACTUAL qty so fees / pnl% / risk accounting reflect the real position.
        notional = qty * entry_px
        if maker_mode:
            # Post-only limit at the signal close; rests for exactly one bar.
            # A reject (price already crossed) is a MISSED entry — the engine
            # models the same miss; a persisting signal retries next bar.
            try:
                order = self.ex.limit_entry(side, qty, entry_px, sl, tp)
            except Exception as e:
                self.log.info(f"maker entry not placed ({e}) — postonly reject "
                              f"or API error; sweeping + skipping this bar")
                self.ex.cancel_all()
                net = self.ex.fetch_position_size()
                if net is not None and abs(net) > 1e-9:
                    self.log.critical(f"ORPHAN POSITION after failed maker "
                                      f"entry (net={net}); force-closing")
                    try:
                        if net > 0:
                            self.ex.market_sell(abs(net), reduce_only=True)
                        else:
                            self.ex.market_buy(abs(net), reduce_only=True)
                    except Exception as e2:
                        self.log.critical(f"force-close FAILED: {e2} — MANUAL "
                                          f"INTERVENTION required")
                return
            self.state.pending_entry = {
                "order_id": order.get("id"), "side": side, "qty": qty,
                "limit_px": entry_px, "sl": sl, "tp": tp,
                "signal_bar_ts": int(bar_ts or 0), "risk_pct": risk,
                "created_ts": int(time.time()),
            }
            self.state.save(self.cfg.state_file)
            self.log.info(
                f"PENDING {('LONG' if side == 1 else 'SHORT')} maker limit "
                f"{qty:.6f} @ {entry_px:.4f} sl={sl:.4f} tp={tp:.4f} "
                f"(rests until next bar close)")
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
                        self.ex.market_sell(abs(net), reduce_only=True)
                    else:
                        self.ex.market_buy(abs(net), reduce_only=True)
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

    def _book_resume_autonomous_close(self, p) -> bool:
        """On startup we found a tracked position but the exchange is flat: an
        SL/TP fired while the bot was DOWN. Book the real close from closed-PnL
        history and return True; return False if history is unavailable (caller
        then clears the position without PnL).

        The fill happened in the past, so we derive the cooldown's bar timestamp
        from the fill's OWN timestamp: a stop from many bars ago yields an
        already-expired cooldown (blocks nothing), while a stop from a fast
        redeploy still suppresses the same-side re-entry — matching the backtest,
        where every SL arms the cooldown regardless of process restarts.
        """
        real = self.ex.fetch_last_closed_fill(p, since_ms=p.open_ts * 1000)
        if real is None:
            return False
        bar_sec = Exchange.TF_SECONDS.get(self.cfg.timeframe, 3600)
        fill_ms = real.get("updated_ms")
        close_bar_ts = (int(fill_ms) // 1000 // bar_sec * bar_sec) if fill_ms else None
        # Classify by fill proximity (same rule as the mid-run reconcile): a
        # TP that filled while we were down must NOT arm the SL cooldown.
        why = "sl-external" if abs(real["exit_px"] - p.sl) <= \
            abs(real["exit_px"] - p.tp) else "tp-external"
        self.log.warning(f"position in state but exchange flat — SL/TP fired while "
                         f"down; booking real close at {real['exit_px']:.4f} ({why})"
                         + ("" if close_bar_ts else " (no fill ts — cooldown not armed)"))
        self.close_position(real["exit_px"], why, bar_ts=close_bar_ts)
        return True

    # --- maker-entry (post-only) lifecycle ------------------------------------
    def _adopt_pending_fill(self, pe: dict, qty: float,
                            avg_px: float | None,
                            fill_ms: int | None = None) -> None:
        """A resting maker entry filled — promote it to a tracked Position.
        The attached SL/TP went live with the fill, so the position was never
        unprotected. open_ts uses the exchange's fill timestamp when known
        (a fill during downtime must not look freshly opened — it would skew
        time-exit counting and closed-PnL lookups)."""
        fill_px = float(avg_px or pe["limit_px"])
        notional = qty * fill_px
        open_ts = int(fill_ms // 1000) if fill_ms else int(time.time())
        self.state.position = Position(
            side=int(pe["side"]), qty=qty, entry_px=fill_px,
            sl=float(pe["sl"]), tp=float(pe["tp"]), open_ts=open_ts,
            notional=notional, bars_open=0, order_id=pe.get("order_id"),
            maker_entry=True,
        )
        self.state.pending_entry = None
        self.state.save(self.cfg.state_file)
        side = int(pe["side"])
        self.log.info(
            f"OPEN {('LONG' if side == 1 else 'SHORT')} (maker fill) "
            f"{qty:.6f} @ {fill_px:.4f} sl={pe['sl']:.4f} tp={pe['tp']:.4f} "
            f"notional={notional:.2f} equity={self.state.equity:.2f}")
        self.events.entry(side=side, qty=qty, entry_px=fill_px,
                          sl=pe["sl"], tp=pe["tp"], notional=notional,
                          risk_pct=pe.get("risk_pct"),
                          equity=self.state.equity, symbol=self.cfg.symbol,
                          preset=self.cfg.preset, regime=self._last_regime)
        try:
            self.notifier.trade_open(side=side, qty=qty, price=fill_px,
                                     sl=pe["sl"], tp=pe["tp"],
                                     notional=notional,
                                     risk=pe.get("risk_pct") or 0.0,
                                     equity=self.state.equity,
                                     symbol=self.cfg.symbol)
        except Exception as e:
            self.log.warning(f"notifier trade_open failed: {e}")

    def _close_partial_entry(self, pe: dict, filled: float) -> None:
        """v1 policy: a PARTIALLY filled maker entry is closed immediately
        (reduce-only) instead of adopted — the attached tpSize/slSize were
        sized for the full qty, and running a position whose exchange-side
        protection is mis-sized is the ambiguity class behind past incidents.
        Rare (tiny orders on liquid perps at a traded-through level); costs
        one taker round-trip on a fraction of one leg. Revisit if the L1
        shakedown shows partials are common."""
        self.log.warning(f"partial maker fill {filled:.6f}/{pe['qty']:.6f} — "
                         f"closing the partial (v1 policy: no mis-protected "
                         f"positions)")
        try:
            if int(pe["side"]) == 1:
                self.ex.market_sell(filled, reduce_only=True)
            else:
                self.ex.market_buy(filled, reduce_only=True)
        except Exception as e:
            self.log.critical(f"partial-entry close FAILED: {e} — check "
                              f"{self.cfg.symbol} on Bybit manually")
            try:
                self.notifier.error(f"{self.cfg.symbol}: partial maker entry "
                                    f"close failed — manual check needed")
            except Exception:
                pass

    def _check_pending_entry(self, rollover: bool = False) -> None:
        """Track the resting post-only entry. Every tick: adopt a fill
        promptly. At bar rollover: the engine's one-bar rest is over —
        cancel whatever still rests (a persisting signal re-places at the
        new close via the normal entry path)."""
        pe = self.state.pending_entry
        if pe is None:
            return
        try:
            st = self.ex.fetch_order_status(pe["order_id"])
        except Exception as e:
            self.log.warning(f"pending-entry status check failed: {e}")
            self._warn_stale_pending(pe)
            if not rollover:
                return                      # transient — retry next tick
            st = {"status": "unknown", "filled": 0.0, "avg_px": None}
        filled = float(st.get("filled") or 0.0)
        full = filled >= float(pe["qty"]) * 0.999
        if st["status"] == "closed" or full:
            self._adopt_pending_fill(pe, filled if filled > 0 else
                                     float(pe["qty"]), st.get("avg_px"),
                                     fill_ms=st.get("ts_ms"))
            return
        if st["status"] in ("canceled", "cancelled", "rejected", "expired"):
            if filled > 1e-12:
                self._close_partial_entry(pe, filled)
            else:
                self.log.info("maker entry gone unfilled (reject/cancel) — "
                              "missed entry, engine models the same miss")
            self.state.pending_entry = None
            self.state.save(self.cfg.state_file)
            return
        if not rollover:
            return                           # still resting mid-bar — fine
        # Bar rolled over: cancel, then re-check once (cancel/fill race).
        self.ex.cancel_order(pe["order_id"])
        try:
            st2 = self.ex.fetch_order_status(pe["order_id"])
        except Exception as e:
            # Terminal state UNKNOWN: never assume "unfilled" — clearing a
            # FILLED order here would leave a live, untracked position on the
            # exchange. Keep the pending (the resting-order guard blocks new
            # entries) and let the every-tick check retry until the API
            # answers; escalate if it stays unknowable.
            self.log.warning(f"pending-entry final status unknown ({e}) — "
                             f"keeping it for retry; entries stay blocked")
            self._warn_stale_pending(pe)
            return
        if st2["status"] == "unknown":
            self.log.warning("pending-entry final status unknown — keeping "
                             "it for retry; entries stay blocked")
            self._warn_stale_pending(pe)
            return
        filled2 = float(st2.get("filled") or 0.0)
        if st2["status"] == "closed" or filled2 >= float(pe["qty"]) * 0.999:
            self._adopt_pending_fill(pe, filled2 if filled2 > 0 else
                                     float(pe["qty"]), st2.get("avg_px"),
                                     fill_ms=st2.get("ts_ms"))
            return
        if filled2 > 1e-12:
            self._close_partial_entry(pe, filled2)
        else:
            self.log.info(f"maker entry unfilled after one bar @ "
                          f"{pe['limit_px']:.4f} — cancelled (miss is missed)")
        self.state.pending_entry = None
        self.state.save(self.cfg.state_file)

    def _warn_stale_pending(self, pe: dict) -> None:
        """Escalate ONCE when a pending order's status has been unknowable
        for more than ~2 bars — entries are blocked until it resolves, so a
        persistent API failure needs human eyes, not silence."""
        bar_sec = Exchange.TF_SECONDS.get(self.cfg.timeframe, 3600)
        if pe.get("stale_warned") or \
                time.time() - int(pe.get("created_ts") or 0) < 2 * bar_sec:
            return
        pe["stale_warned"] = True
        self.state.save(self.cfg.state_file)
        self.log.critical(f"pending maker entry {pe.get('order_id')} status "
                          f"unknown for >2 bars — entries blocked; check "
                          f"{self.cfg.symbol} on Bybit")
        try:
            self.notifier.error(f"{self.cfg.symbol}: pending entry status "
                                f"unknown >2 bars — manual check needed")
        except Exception:
            pass

    def close_position(self, exit_px_hint: float, reason: str,
                       bar_ts: int | None = None) -> None:
        pos = self.state.position
        if pos is None:
            return
        order: dict | None = None
        try:
            order = (self.ex.market_sell(pos.qty, reduce_only=True) if pos.side == 1
                     else self.ex.market_buy(pos.qty, reduce_only=True))
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
        # Booking the exit fill:
        #  - Live, our reduce-only close FILLED (order set): keep the real fill.
        #  - Live, order is None: the exchange's attached SL/TP fired
        #    autonomously. Book the REAL fill from closed-PnL history, not the
        #    theoretical SL/TP price — a market stop slips past its trigger, so
        #    the theoretical price overstates PnL (the booked-vs-exchange gap).
        #    Fall back to the theoretical hint only if history is unavailable.
        #  - Paper: use the SL/TP price when triggered — matches the backtest.
        if self.cfg.mode == "live":
            if order is None:
                real = self.ex.fetch_last_closed_fill(pos, since_ms=pos.open_ts * 1000)
                if real is not None:
                    self.log.info(f"booking autonomous close at REAL fill "
                                  f"{real['exit_px']:.4f} (theoretical was {fill_px:.4f})")
                    fill_px = real["exit_px"]
                else:
                    self.log.warning(f"closed-PnL unavailable ({reason}); booking "
                                     f"theoretical price {fill_px:.4f}")
            else:
                # Our reduce-only close FILLED. A ccxt Bybit market order carries
                # no fill price, so fill_px fell back to the bar-close hint above;
                # prefer the REAL fill (order 'average' if present, else closed-PnL
                # history) so bot-initiated closes don't widen the booked-vs-real
                # gap at $2300+/8-pair notionals. Theoretical hint stays the last
                # resort.
                avg = None
                try:
                    avg = float(order.get("average") or 0) or None
                except (TypeError, ValueError):
                    avg = None
                if avg:
                    fill_px = avg
                else:
                    real = self.ex.fetch_last_closed_fill(pos, since_ms=pos.open_ts * 1000)
                    if real is not None:
                        self.log.info(f"booking bot-initiated close at REAL fill "
                                      f"{real['exit_px']:.4f} (hint was {fill_px:.4f})")
                        fill_px = real["exit_px"]
        else:
            if reason == "sl":
                fill_px = pos.sl
            elif reason == "tp":
                fill_px = pos.tp
        gross = (fill_px - pos.entry_px) * pos.qty * pos.side
        # Per-side fee keyed off what ACTUALLY executed, not a flat taker rate.
        #  - entry: maker iff this position was opened by a post-only limit
        #    (ENTRY_LIMIT_ORDERS; live-only — paper takes market entries, so
        #    paper honestly keeps the taker rate on entry).
        #  - exit: maker iff the resting reduce-only TP limit was what filled.
        #    `order is None` means our market close was rejected because the
        #    exchange had already flattened us — i.e. the TP limit filled. A
        #    bot-initiated market close on a "tp" reason is still taker. Paper
        #    has no resting order, so it mirrors the engine's tp_as_limit.
        entry_rate = FEE_MAKER if getattr(pos, "maker_entry", False) else FEE_TAKER
        exit_maker = (self.cfg.tp_limit_orders and reason.startswith("tp")
                      and (order is None or self.ex.paper))
        exit_rate = FEE_MAKER if exit_maker else FEE_TAKER
        fees = pos.notional * entry_rate + fill_px * pos.qty * exit_rate
        pnl = gross - fees
        pct = (pnl / pos.notional * 100) if pos.notional else 0.0
        self.state.equity += pnl
        self.state.realised_trades += 1
        if pnl > 0:
            self.state.realised_wins += 1
        self.state.realised_pnl += pnl
        # Post-stop same-side re-entry cooldown: after a stop-loss (incl. the
        # exchange's autonomous 'sl-external'), block a NEW same-side entry for
        # cooldown_bars bars. bar_ts is the just-closed bar; expiry is in calendar
        # seconds (== bar index + K on gapless 1h data, matching the backtester).
        # Both the tick exit path and the resume path pass bar_ts (the resume
        # path derives it from the real fill's own timestamp): a stop fired long
        # ago yields an already-expired 'until' that blocks nothing, while a stop
        # from a fast redeploy still suppresses the same-side re-entry.
        if (bar_ts is not None and self.cfg.cooldown_bars > 0
                and reason.startswith("sl")):
            bar_sec = Exchange.TF_SECONDS.get(self.cfg.timeframe, 3600)
            until = int(bar_ts) + self.cfg.cooldown_bars * bar_sec
            if pos.side == 1:
                self.state.block_long_until_ts = until
            else:
                self.state.block_short_until_ts = until
            self.log.info(f"post-SL cooldown: blocking {'LONG' if pos.side == 1 else 'SHORT'} "
                          f"re-entry for {self.cfg.cooldown_bars} bars")
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

        # Resting maker entry: check every tick so a fill is adopted (and its
        # TP/SL tracking starts) within one poll, not at the next bar close.
        # Also the resume path: a pending order in restored state is picked
        # up here on the first tick after a restart.
        if self.cfg.entry_limit_orders and self.state.pending_entry is not None:
            self._check_pending_entry(rollover=False)

        # Bar-close gate: signals only change at bar close. With 5+ portfolio
        # bots polling every ~30s, fetching 300 bars on EVERY tick burns
        # through exchange rate limits (Bybit retCode 10006) — but on 1h
        # bars the strategy only acts ~once an hour. Skip the heavy fetch
        # unless a NEW closed bar exists since the last one we processed,
        # AND its data has had time to settle on the exchange (30s buffer).
        bar_sec = Exchange.TF_SECONDS.get(self.cfg.timeframe, 3600)
        now = int(time.time())
        current_bar_open = (now // bar_sec) * bar_sec     # the bar currently forming
        last_closed_open = current_bar_open - bar_sec     # the bar that just closed
        already_processed = last_closed_open <= self.state.last_bar_ts
        # 30s base settle buffer + deterministic per-symbol stagger so the
        # portfolio bots don't all fetch at once and trip the per-IP rate limit.
        data_not_settled = now < current_bar_open + 30 + self._fetch_stagger
        if self.state.last_bar_ts > 0 and (already_processed or data_not_settled):
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
        if self.cfg.vol_target_ann > 0:
            self._vt_mult = vol_target_mult(df["close"], self.cfg.vol_target_ann)
        last_bar = df.iloc[-1]
        bar_ts = int(df.index[-1].timestamp())

        # 0) A maker entry rests for exactly ONE bar (engine parity): when a
        #    new bar has closed since it was placed, finalize it — adopt a
        #    fill or cancel the rest. A persisting signal re-places at the
        #    new close in step 2.
        if self.cfg.entry_limit_orders and self.state.pending_entry is not None \
                and bar_ts > int(self.state.pending_entry.get("signal_bar_ts") or 0):
            self._check_pending_entry(rollover=True)

        # 1) If we have a position, check exits on the just-closed bar
        if self.state.position is not None and bar_ts > self.state.last_bar_ts:
            self.state.position.bars_open += 1
            # LIVE: detect exchange-side autonomous closes (Bybit's attached
            # SL/TP fired but our bar-based check missed it — e.g. the KuCoin
            # REST fallback used while rate-limited has a slightly different
            # bar low/high than Bybit's actual perp). One extra
            # fetch_positions per bar advance ≈ 1/hour, negligible API cost.
            if self.cfg.mode == "live" and not self._reconcile_position_with_exchange(last_bar, bar_ts):
                reason = self.check_exit(last_bar)
                if reason is not None:
                    self.close_position(float(last_bar["close"]), reason, bar_ts=bar_ts)
            elif self.cfg.mode != "live":
                reason = self.check_exit(last_bar)
                if reason is not None:
                    self.close_position(float(last_bar["close"]), reason, bar_ts=bar_ts)

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
                self.enter_position(sig_info, bar_ts=bar_ts)

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
                      f"strategy={self.cfg.strategy} short={self.cfg.allow_short} "
                      f"regime={self.cfg.use_adaptive_regime} "
                      f"fng={self.cfg.use_fng_extreme_filter} "
                      f"decay={'on' if (self.cfg.eq_decay_tiers or self.cfg.eq_risk_decay) else 'off'} "
                      f"cooldown={self.cfg.cooldown_bars if self.cfg.cooldown_bars > 0 else 'off'} "
                      f"htf={self.cfg.use_htf} sentiment={self.cfg.use_sentiment} "
                      f"stagger={self._fetch_stagger}s equity={self.state.equity:.2f}")
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
            # Warm the market-metadata cache now so the FIRST entry doesn't race
            # ccxt's symbol resolution to the spot market (the "attached stopLoss
            # not supported for spot market orders" failure seen on a cold start).
            self.ex._load_market()
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
                    # SL/TP fired while the bot was down. Book the real close from
                    # closed-PnL history so the missed trade enters the books
                    # (previously dropped — a source of booked-vs-real drift). Fall
                    # back to clearing without PnL only if history is unavailable.
                    if not self._book_resume_autonomous_close(p):
                        self.log.warning(f"position in state but exchange is flat — "
                                         f"SL/TP fired while down; closed-PnL "
                                         f"unavailable, clearing without PnL.")
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
        elif self.cfg.mode == "live" and self.state.pending_entry is None:
            # FLAT state, live mode: sweep the crash window. A previous
            # process may have placed an order (or had a fill land) and died
            # before state.save — the exchange would hold a resting order or
            # a position this fresh state knows nothing about. Cancel any
            # resting orders (safe: state tracks none) and HALT on an
            # untracked position rather than trade around it. Skipped when a
            # pending is tracked — cancel_all would kill our own order.
            try:
                self.ex.cancel_all()
                net = self.ex.fetch_position_size()
                if net is not None and abs(net) > 1e-9:
                    self._halted = True
                    self._halt_reason = (f"untracked position at startup "
                                         f"(net={net:+.6f}) with flat state")
                    self.log.critical(
                        f"UNTRACKED POSITION at startup (net={net:+.6f}) with "
                        f"flat state — HALTED; reconcile {self.cfg.symbol} "
                        f"manually and restart")
                    try:
                        self.notifier.error(f"{self.cfg.symbol}: untracked "
                                            f"position at startup; bot HALTED")
                    except Exception:
                        pass
            except Exception as e:
                self.log.warning(f"startup flat-state sweep failed: {e}")
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
