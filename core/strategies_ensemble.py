"""Ensemble: take a trade only when N out of M strategies agree.

Each base strategy returns its own signal (-1/0/+1). We sum up the votes
and only act when |votes| >= min_agreement. Stops/TPs come from the FIRST
strategy in the list (typically the most conservative).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategies import (
    triple_confirm_long,
    ema_trend,
    macd_trend,
    supertrend_rsi,
)


def ensemble_signal(df: pd.DataFrame,
                    min_agreement: int = 2,
                    long_only: bool = True,
                    triple_kw: dict | None = None,
                    ema_kw: dict | None = None,
                    macd_kw: dict | None = None,
                    super_kw: dict | None = None) -> pd.DataFrame:
    """Combine triple_long + ema_trend + macd_trend + supertrend_rsi votes."""
    triple_kw = triple_kw or {}
    ema_kw = ema_kw or {}
    macd_kw = macd_kw or {}
    super_kw = super_kw or {}

    sig_triple = triple_confirm_long(df, **triple_kw)
    sig_ema = ema_trend(df, **ema_kw)
    sig_macd = macd_trend(df, **macd_kw)
    sig_super = supertrend_rsi(df, **super_kw)

    votes = (sig_triple["signal"]
             + sig_ema["signal"]
             + sig_macd["signal"]
             + sig_super["signal"])

    if long_only:
        # Sum can be 0..4 for longs, -4..0 for shorts. Clip to longs only.
        out_signal = (votes >= min_agreement).astype(int)
    else:
        out_signal = np.where(votes >= min_agreement, 1,
                     np.where(votes <= -min_agreement, -1, 0))
        out_signal = pd.Series(out_signal, index=df.index)

    out = pd.DataFrame(index=df.index)
    out["signal"] = out_signal
    # Use triple_long stops/tps where signal is positive
    out["sl"] = np.where(out["signal"] == 1, sig_triple["sl"],
                np.where(out["signal"] == -1, sig_triple["sl"], np.nan))
    out["tp"] = np.where(out["signal"] == 1, sig_triple["tp"],
                np.where(out["signal"] == -1, sig_triple["tp"], np.nan))
    return out
