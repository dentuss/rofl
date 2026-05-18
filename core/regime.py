"""ML-based regime detection.

Three regimes are inferred from rolling features:
    BULL  - strong uptrend, moderate volatility
    CHOP  - sideways, low directionality
    BEAR  - downtrend or capitulation

Approach: Gaussian Mixture Model fit on (30d-return, 14d-ATR/price, ADX).
Each bar gets a regime probability vector; we take the argmax.

For live use: fit on the full available history, predict the latest bar's
regime, and use that to switch trading presets.

The labels are unsupervised but stable: we map components to BULL/CHOP/BEAR
by their mean 30d-return at fit time (highest = BULL, middle/lowest = CHOP/BEAR).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

try:
    from sklearn.mixture import GaussianMixture
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

from core.indicators import adx, atr, ema


REGIME_LABELS = ["BEAR", "CHOP", "BULL"]


@dataclass
class RegimeFeatures:
    ret_30d: pd.Series      # rolling 30-day cumulative return
    vol: pd.Series          # ATR(14) / price (normalised vol)
    adx14: pd.Series        # trend strength
    trend_score: pd.Series  # close / EMA50 - 1


def build_features(df: pd.DataFrame, bars_per_day: int = 24) -> RegimeFeatures:
    close = df["close"]
    h, l = df["high"], df["low"]
    win_30d = 30 * bars_per_day
    return RegimeFeatures(
        ret_30d=close.pct_change(win_30d),
        vol=atr(h, l, close, 14) / close,
        adx14=adx(h, l, close, 14) / 100.0,
        trend_score=close / ema(close, 50) - 1,
    )


def feature_matrix(feat: RegimeFeatures) -> pd.DataFrame:
    return pd.concat({
        "ret_30d": feat.ret_30d,
        "vol": feat.vol,
        "adx14": feat.adx14,
        "trend_score": feat.trend_score,
    }, axis=1)


def fit_gmm(features: pd.DataFrame, n_components: int = 3,
            random_state: int = 42) -> tuple[object, dict]:
    """Fit a Gaussian Mixture and return (model, regime_mapping).

    regime_mapping is {component_id: 'BULL'/'CHOP'/'BEAR'} ordered by mean
    ret_30d of that component (highest = BULL, lowest = BEAR).
    """
    if not SKLEARN_AVAILABLE:
        raise RuntimeError("scikit-learn not installed; pip install scikit-learn")
    X = features.replace([np.inf, -np.inf], np.nan).dropna()
    if len(X) < 200:
        raise RuntimeError("not enough rows to fit GMM")
    gmm = GaussianMixture(n_components=n_components, covariance_type="full",
                          random_state=random_state, n_init=3, max_iter=200)
    gmm.fit(X.values)
    # Map component_id -> regime by mean ret_30d (column 0)
    means = gmm.means_[:, 0]
    order = np.argsort(means)
    mapping = {}
    if n_components == 3:
        for rank, comp_id in enumerate(order):
            mapping[int(comp_id)] = REGIME_LABELS[rank]
    else:
        # fallback: use BEAR/BULL only
        mapping[int(order[0])] = "BEAR"
        for c in order[1:-1]:
            mapping[int(c)] = "CHOP"
        mapping[int(order[-1])] = "BULL"
    return gmm, mapping


def predict_regimes(gmm, mapping: dict, features: pd.DataFrame) -> pd.Series:
    """Predict regime label for each row of features. Rows with NaN features
    get 'CHOP' as a safe default (no trading bias)."""
    out = pd.Series("CHOP", index=features.index)
    valid = features.replace([np.inf, -np.inf], np.nan).dropna()
    if len(valid) == 0:
        return out
    pred = gmm.predict(valid.values)
    labels = pd.Series([mapping[int(p)] for p in pred], index=valid.index)
    out.loc[labels.index] = labels.values
    return out


def regime_proba(gmm, mapping: dict, features: pd.DataFrame) -> pd.DataFrame:
    """Per-bar probability of each regime."""
    valid = features.replace([np.inf, -np.inf], np.nan).dropna()
    proba = gmm.predict_proba(valid.values)
    cols = [mapping[i] for i in range(gmm.n_components)]
    out = pd.DataFrame(proba, index=valid.index, columns=cols)
    # Sum any duplicate column names (shouldn't happen but safety)
    out = out.groupby(out.columns, axis=1).sum()
    return out


def transition_matrix(regimes: pd.Series) -> pd.DataFrame:
    """Empirical Markov transition matrix between regimes."""
    states = REGIME_LABELS
    mat = pd.DataFrame(0.0, index=states, columns=states)
    pairs = list(zip(regimes.shift(1).dropna().values, regimes.iloc[1:].values))
    for a, b in pairs:
        if a in states and b in states:
            mat.loc[a, b] += 1
    row_sums = mat.sum(axis=1).replace(0, 1)
    return mat.div(row_sums, axis=0)


def regime_returns(bar_returns: pd.Series, regimes: pd.Series) -> dict:
    """Per-regime statistics of bar returns. Used for regime-aware MC."""
    out = {}
    aligned = pd.concat([bar_returns.rename("r"), regimes.rename("regime")],
                        axis=1).dropna()
    for label in REGIME_LABELS:
        sub = aligned.loc[aligned["regime"] == label, "r"].values
        if len(sub) < 10:
            sub = aligned["r"].values  # fallback to overall
        out[label] = {
            "n": int(len(sub)),
            "mean": float(np.mean(sub)),
            "std": float(np.std(sub)),
            "samples": sub,  # keep for empirical bootstrap
        }
    return out
