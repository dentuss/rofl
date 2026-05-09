"""Sentiment-aware variants of the Donchian breakout strategy.

We test several ways of incorporating the Fear & Greed index:
  - SKIP_GREED:       skip longs when F&G > greed_max (avoid greed peaks)
  - SKIP_FEAR:        skip shorts when F&G < fear_min (avoid fear pits)
  - BOTH:             apply both filters
  - REVERSE_BIAS:     long bias in extreme fear (contrarian), short bias in greed
  - SIZE_ADJUST:      same signals, but scale risk down at extremes (returns
                      a 'risk_mult' column that the backtester can read)

Each function takes the raw OHLCV df + an aligned F&G series and returns the
same DataFrame schema as strategies.py (signal, sl, tp).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategies import donchian_breakout


def _align_fng(df: pd.DataFrame, fng_daily: pd.DataFrame) -> pd.Series:
    """Forward-fill daily F&G onto df's intraday index. No look-ahead: each
    bar sees the F&G value as of its own UTC date."""
    s = fng_daily["fng"].copy()
    s.index = pd.DatetimeIndex(s.index, tz="UTC")
    aligned = s.reindex(df.index, method="ffill")
    return aligned.fillna(50)  # neutral default if missing


def donchian_skip_greed(df, fng_daily, greed_max=75,
                        **donchian_kwargs) -> pd.DataFrame:
    """Skip long entries when F&G > greed_max (top is near)."""
    base = donchian_breakout(df, **donchian_kwargs)
    fng = _align_fng(df, fng_daily)
    mask = (base["signal"] == 1) & (fng > greed_max)
    base.loc[mask, "signal"] = 0
    base.loc[mask, ["sl", "tp"]] = np.nan
    return base


def donchian_skip_fear(df, fng_daily, fear_min=25,
                       **donchian_kwargs) -> pd.DataFrame:
    """Skip short entries when F&G < fear_min (bottom is near)."""
    base = donchian_breakout(df, **donchian_kwargs)
    fng = _align_fng(df, fng_daily)
    mask = (base["signal"] == -1) & (fng < fear_min)
    base.loc[mask, "signal"] = 0
    base.loc[mask, ["sl", "tp"]] = np.nan
    return base


def donchian_both_filters(df, fng_daily, greed_max=75, fear_min=25,
                          **donchian_kwargs) -> pd.DataFrame:
    """Skip longs in extreme greed AND shorts in extreme fear."""
    base = donchian_breakout(df, **donchian_kwargs)
    fng = _align_fng(df, fng_daily)
    long_mask = (base["signal"] == 1) & (fng > greed_max)
    short_mask = (base["signal"] == -1) & (fng < fear_min)
    mask = long_mask | short_mask
    base.loc[mask, "signal"] = 0
    base.loc[mask, ["sl", "tp"]] = np.nan
    return base


def donchian_reverse_bias(df, fng_daily, **donchian_kwargs) -> pd.DataFrame:
    """Allow only longs in fear regimes (<45), only shorts in greed (>55).
    This makes the trend-follower trade WITH consensus reversion thinking."""
    base = donchian_breakout(df, **donchian_kwargs)
    fng = _align_fng(df, fng_daily)
    long_in_greed = (base["signal"] == 1) & (fng > 55)
    short_in_fear = (base["signal"] == -1) & (fng < 45)
    mask = long_in_greed | short_in_fear
    base.loc[mask, "signal"] = 0
    base.loc[mask, ["sl", "tp"]] = np.nan
    return base


def donchian_long_only_fear(df, fng_daily, fng_max=55,
                            **donchian_kwargs) -> pd.DataFrame:
    """Long-only (no shorts) AND only when F&G is below fng_max.
    Pure 'buy the dip' play."""
    base = donchian_breakout(df, **donchian_kwargs)
    fng = _align_fng(df, fng_daily)
    skip = (base["signal"] == -1) | ((base["signal"] == 1) & (fng > fng_max))
    base.loc[skip, "signal"] = 0
    base.loc[skip, ["sl", "tp"]] = np.nan
    return base
