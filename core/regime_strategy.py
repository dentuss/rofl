"""Regime-switching wrapper around any base strategy.

Idea: detect the current market regime (BULL/CHOP/BEAR) and either
  - skip trading entirely in BEAR (defense)
  - reduce risk in CHOP (preserve capital)
  - trade full risk in BULL (capture upside)

The regime is detected from a Gaussian Mixture Model fit on rolling
features (return, vol, ADX, trend). Walk-forward: we re-fit on each
chunk and predict forward to avoid look-ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.regime import (
    REGIME_LABELS,
    build_features,
    feature_matrix,
    fit_gmm,
    predict_regimes,
)


# Default per-regime risk multipliers (applied to base risk_per_trade)
DEFAULT_REGIME_RISK = {"BULL": 1.0, "CHOP": 0.5, "BEAR": 0.0}


def filter_by_regime(sig: pd.DataFrame, regimes: pd.Series,
                     allow: tuple[str, ...] = ("BULL", "CHOP")) -> pd.DataFrame:
    """Zero out signals when the current regime is not in `allow`."""
    out = sig.copy()
    aligned = regimes.reindex(out.index, method="ffill").fillna("CHOP")
    skip = (out["signal"] != 0) & (~aligned.isin(allow))
    out.loc[skip, "signal"] = 0
    out.loc[skip, ["sl", "tp"]] = np.nan
    return out


def walk_forward_regimes(df: pd.DataFrame, bars_per_day: int = 24,
                         train_days: int = 365,
                         step_days: int = 30) -> pd.Series:
    """Walk-forward regime prediction. Re-fit GMM every `step_days` days
    on the last `train_days` of data, then predict forward.

    Returns a Series of regime labels, indexed like df. Bars before the
    first prediction window get 'CHOP' (neutral default).
    """
    feats = build_features(df, bars_per_day=bars_per_day)
    fm = feature_matrix(feats)
    out = pd.Series("CHOP", index=df.index)

    train_bars = train_days * bars_per_day
    step_bars = step_days * bars_per_day
    cursor = train_bars
    while cursor < len(df):
        train_slice = fm.iloc[max(0, cursor - train_bars):cursor]
        if len(train_slice.dropna()) < 200:
            cursor += step_bars
            continue
        try:
            gmm, mapping = fit_gmm(train_slice, n_components=3)
        except Exception:
            cursor += step_bars
            continue
        end = min(cursor + step_bars, len(df))
        test_slice = fm.iloc[cursor:end]
        preds = predict_regimes(gmm, mapping, test_slice)
        out.loc[preds.index] = preds.values
        cursor = end
    return out


def fit_predict_full(df: pd.DataFrame, bars_per_day: int = 24):
    """Fit on full history and return regime series (used for live bot —
    the most recent bar's regime is what we act on). NOT walk-forward.
    """
    feats = build_features(df, bars_per_day=bars_per_day)
    fm = feature_matrix(feats)
    gmm, mapping = fit_gmm(fm, n_components=3)
    preds = predict_regimes(gmm, mapping, fm)
    return gmm, mapping, preds
