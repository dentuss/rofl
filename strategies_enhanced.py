"""Strategy enhancements layered on top of the Donchian + sentiment baseline.

Each function takes the same OHLCV df, optional F&G data, and returns a
DataFrame with the standard `signal/sl/tp` schema. Live bot can use any
of them via configuration.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import atr, ema, sma
from strategies import donchian_breakout
from strategies_sentiment import donchian_skip_fear


# --- Higher-timeframe trend filter ------------------------------------------
def _resample_close(df: pd.DataFrame, rule: str) -> pd.Series:
    """Build a higher-timeframe close series aligned back to the bar index."""
    htf = df["close"].resample(rule).last().ffill()
    return htf.reindex(df.index, method="ffill")


def with_htf_trend_filter(df: pd.DataFrame, base_signal: pd.DataFrame,
                          htf_rule: str = "1D",
                          ema_n: int = 50) -> pd.DataFrame:
    """Block entries that fight the higher-timeframe trend.

    Long allowed only if HTF close > HTF EMA(ema_n).
    Short allowed only if HTF close < HTF EMA(ema_n).
    """
    out = base_signal.copy()
    htf_close = _resample_close(df, htf_rule)
    # Compute EMA on the unique HTF series (without intraday duplicates)
    htf_unique = df["close"].resample(htf_rule).last().ffill()
    htf_ema = htf_unique.ewm(span=ema_n, adjust=False, min_periods=ema_n).mean()
    htf_ema_aligned = htf_ema.reindex(df.index, method="ffill")
    above = htf_close > htf_ema_aligned

    long_mask = (out["signal"] == 1) & ~above.fillna(False)
    short_mask = (out["signal"] == -1) & above.fillna(True)
    blocked = long_mask | short_mask
    out.loc[blocked, "signal"] = 0
    out.loc[blocked, ["sl", "tp"]] = np.nan
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
