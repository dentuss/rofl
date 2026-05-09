"""Volatility-targeted position sizing.

Idea: instead of risking a fixed % of equity per trade, scale position size
inversely to recent realized volatility. When vol is low, take bigger
positions. When vol is high, take smaller. Aim is a more uniform risk
contribution per trade and a smoother equity curve.

Implementation: we compute a vol multiplier as
    vol_mult = clip(target_vol / realized_vol_n, 0.3, 3.0)
and pass it as a per-bar multiplier on the standard risk amount.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import atr


def vol_targeting_mult(df: pd.DataFrame, target_atr_pct: float = 0.012,
                       atr_n: int = 14,
                       lo: float = 0.3, hi: float = 3.0) -> pd.Series:
    """Return per-bar position-size multiplier (1.0 = baseline risk).

    target_atr_pct: target ATR/price (1.2% per bar by default).
    Larger ATR than target -> smaller position (lower than 1.0).
    Smaller ATR than target -> bigger position (higher than 1.0).
    """
    a = atr(df["high"], df["low"], df["close"], atr_n)
    vol_pct = a / df["close"]
    mult = (target_atr_pct / vol_pct).clip(lower=lo, upper=hi)
    return mult.fillna(1.0)


def vol_targeting_risk(df: pd.DataFrame, base_risk: float = 0.015,
                       target_atr_pct: float = 0.012, atr_n: int = 14,
                       lo: float = 0.3, hi: float = 3.0) -> pd.Series:
    """Per-bar risk fraction (replaces cfg.risk_per_trade)."""
    return vol_targeting_mult(df, target_atr_pct, atr_n, lo, hi) * base_risk
