"""Composable signal filters (same pattern as the regime / F&G filters).

Each takes a signal DataFrame (signal/sl/tp) + the OHLCV df and returns a
copy with some entries zeroed out. They compose left-to-right.

  chop_filter   — short-term: block entries when the market is choppy NOW
                  (low directional efficiency / high choppiness index).
  health_filter — long-term: pause a pair that has structurally gone "dead"
                  (sustained low trend quality over a multi-month window).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.indicators import choppiness_index, efficiency_ratio


def chop_filter(sig: pd.DataFrame, df: pd.DataFrame,
                n: int = 14,
                max_choppiness: float = 61.8,
                min_efficiency: float = 0.30) -> pd.DataFrame:
    """Zero out entries taken in choppy conditions.

    Blocks when Choppiness Index >= max_choppiness OR Efficiency Ratio <
    min_efficiency at the signal bar. Defaults are the classic CI 61.8
    consolidation line and a moderate ER floor.
    """
    out = sig.copy()
    ci = choppiness_index(df["high"], df["low"], df["close"], n)
    er = efficiency_ratio(df["close"], n)
    block = (out["signal"] != 0) & (
        (ci.reindex(out.index) >= max_choppiness) |
        (er.reindex(out.index) < min_efficiency)
    )
    out.loc[block, "signal"] = 0
    out.loc[block, ["sl", "tp"]] = np.nan
    return out


def health_filter(sig: pd.DataFrame, df: pd.DataFrame,
                  lookback_bars: int = 24 * 90,
                  min_efficiency: float = 0.18) -> pd.DataFrame:
    """Pause a pair whose long-run trend quality has decayed.

    Computes the asset's efficiency ratio over a multi-month lookback
    (default 90 days on 1h bars). When the long-run ER drops below
    min_efficiency the asset is meandering with no net direction — a
    trend strategy can't work — so all new entries are blocked until it
    recovers. Path-independent (depends only on price), so it backtests
    cleanly and matches live exactly.
    """
    out = sig.copy()
    long_er = efficiency_ratio(df["close"], lookback_bars).reindex(out.index)
    block = (out["signal"] != 0) & (long_er < min_efficiency)
    out.loc[block, "signal"] = 0
    out.loc[block, ["sl", "tp"]] = np.nan
    return out


def health_score(df: pd.DataFrame, lookback_bars: int = 24 * 90) -> pd.Series:
    """Expose the long-run efficiency ratio used by health_filter (for the
    live bot to log / decide on the latest bar)."""
    return efficiency_ratio(df["close"], lookback_bars)
