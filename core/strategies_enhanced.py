"""Strategy enhancements layered on top of the Donchian + sentiment baseline.

Each function takes the same OHLCV df, optional F&G data, and returns a
DataFrame with the standard `signal/sl/tp` schema. Live bot can use any
of them via configuration.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.indicators import atr
from core.strategies import donchian_breakout
from core.strategies_sentiment import donchian_skip_fear


# --- Higher-timeframe trend filter ------------------------------------------
def with_htf_trend_filter(df: pd.DataFrame, base_signal: pd.DataFrame,
                          htf_rule: str = "1D",
                          ema_n: int = 50) -> pd.DataFrame:
    """Block entries that fight the higher-timeframe trend.

    Long allowed only if last fully-closed HTF close > HTF EMA(ema_n).
    Short allowed only if last fully-closed HTF close < HTF EMA(ema_n).

    To avoid look-ahead and live-bar parity issues, we shift the HTF series
    by one period: the in-progress HTF bar (e.g. today's daily candle) is
    NOT used. The bot acts on yesterday's close compared to yesterday's EMA.
    """
    out = base_signal.copy()
    htf_unique = df["close"].resample(htf_rule).last().ffill()
    htf_ema = htf_unique.ewm(span=ema_n, adjust=False, min_periods=ema_n).mean()
    # Shift by 1 HTF period so we only use fully-closed HTF bars
    htf_close_safe = htf_unique.shift(1)
    htf_ema_safe = htf_ema.shift(1)
    above = (htf_close_safe > htf_ema_safe).reindex(df.index, method="ffill")

    long_mask = (out["signal"] == 1) & ~above.fillna(False)
    short_mask = (out["signal"] == -1) & above.fillna(True)
    blocked = long_mask | short_mask
    out.loc[blocked, "signal"] = 0
    out.loc[blocked, ["sl", "tp"]] = np.nan
    return out


def with_htf_risk_bias(df: pd.DataFrame, base_signal: pd.DataFrame,
                       htf_rule: str = "1D", ema_n: int = 50,
                       with_mult: float = 1.0,
                       counter_mult: float = 0.5) -> pd.DataFrame:
    """Soft version of with_htf_trend_filter: instead of blocking entries that
    fight the higher-timeframe trend, scale their risk through the engine's
    `risk_mult` column (with-trend entries get `with_mult`, counter-trend get
    `counter_mult`; counter_mult=0 degenerates to a hard block on entries).

    Same shift-by-1 HTF construction as the hard filter, so only fully-closed
    HTF bars are used (no look-ahead; the engine additionally shifts risk_mult
    by one LTF bar together with the signal). While the HTF EMA is warming up
    the bias is neutral (1.0). Multiplies into any pre-existing risk_mult
    column rather than overwriting it. UNDER VALIDATION
    (research/tp_cooldown_htf_bias.py) — not wired into the live bot.
    """
    out = base_signal.copy()
    htf_unique = df["close"].resample(htf_rule).last().ffill()
    htf_ema = htf_unique.ewm(span=ema_n, adjust=False, min_periods=ema_n).mean()
    close_safe = htf_unique.shift(1)
    ema_safe = htf_ema.shift(1)
    above = (close_safe > ema_safe).reindex(df.index, method="ffill") \
        .fillna(False).astype(bool).to_numpy()
    known = (close_safe.notna() & ema_safe.notna()) \
        .reindex(df.index, method="ffill").fillna(False).astype(bool).to_numpy()

    sig = out["signal"].to_numpy()
    long_m = np.where(above, with_mult, counter_mult)
    short_m = np.where(above, counter_mult, with_mult)
    mult = np.where(~known, 1.0,
                    np.where(sig == 1, long_m,
                             np.where(sig == -1, short_m, 1.0)))
    base_mult = out["risk_mult"].to_numpy() if "risk_mult" in out.columns else 1.0
    out["risk_mult"] = base_mult * mult
    return out


# --- Volatility filter -------------------------------------------------------
def with_vol_filter(df: pd.DataFrame, base_signal: pd.DataFrame,
                    atr_n: int = 14, min_pct: float = 0.003) -> pd.DataFrame:
    """Skip entries when current ATR/price is below `min_pct`. Low volatility
    = chop; breakouts in chop tend to fail."""
    out = base_signal.copy()
    a = atr(df["high"], df["low"], df["close"], atr_n)
    vol = a / df["close"]
    blocked = (out["signal"] != 0) & (vol < min_pct)
    out.loc[blocked, "signal"] = 0
    out.loc[blocked, ["sl", "tp"]] = np.nan
    return out


# --- Stricter ADX ------------------------------------------------------------
def donchian_strict_adx(df, fng=None, fear_min=25.0,
                        adx_min: float = 25.0, **kwargs) -> pd.DataFrame:
    """Same as donchian_skip_fear but with a higher ADX threshold."""
    kwargs["adx_min"] = adx_min
    if fng is not None:
        return donchian_skip_fear(df, fng, fear_min=fear_min, **kwargs)
    return donchian_breakout(df, **kwargs)
