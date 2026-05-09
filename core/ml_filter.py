"""Machine-learning entry filter for triple_long.

Idea: triple_long generates signals; we keep only the ones a classifier
predicts to win. The classifier is trained on past signals and labeled
by what actually happened to each (TP hit -> win, SL hit -> lose).

To avoid overfitting:
  - Walk-forward training: train on rolling N months, predict next M
  - Out-of-sample validation only — never train on the same bars we score
  - Conservative feature set, no fancy interactions
  - Gradient boosting (sklearn) with light depth

Features (all computed at signal-bar close, no look-ahead):
  - Price / EMA200 ratio          (long-term trend)
  - 4h return, 1d return          (multi-TF momentum)
  - RSI(14), ADX(14)              (current strategy already uses these)
  - ATR(14) / price               (volatility regime)
  - Bollinger position (0..1)     (where in recent range)
  - Volume / 20-bar MA            (activity)
  - F&G index                     (sentiment regime)
  - Hour of day (cyclical encoding) (session effects)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.indicators import adx, atr, ema, rsi, sma, bollinger
from core.strategies import triple_confirm_long
from core.strategies_sentiment import _align_fng


def build_features(df: pd.DataFrame, fng: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per-bar feature matrix. Same index as df."""
    f = pd.DataFrame(index=df.index)
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    f["px_over_ema200"] = close / ema(close, 200) - 1
    f["ret_4h"] = close.pct_change(4)
    f["ret_24h"] = close.pct_change(24)
    f["ret_72h"] = close.pct_change(72)
    f["rsi14"] = rsi(close, 14) / 100.0
    f["adx14"] = adx(high, low, close, 14) / 100.0
    f["atr_pct"] = atr(high, low, close, 14) / close
    upper, mid, lower = bollinger(close, 20, 2.0)
    f["bb_pos"] = ((close - lower) / (upper - lower)).clip(-1, 2)
    f["vol_ratio"] = volume / sma(volume, 20)
    f["log_vol_ratio"] = np.log(f["vol_ratio"].clip(lower=0.1))
    if fng is not None:
        fng_aligned = _align_fng(df, fng) / 100.0
        f["fng"] = fng_aligned
        f["fng_change"] = fng_aligned.diff(24)
    # Cyclical hour-of-day
    hour = df.index.hour
    f["sin_hod"] = np.sin(2 * np.pi * hour / 24)
    f["cos_hod"] = np.cos(2 * np.pi * hour / 24)
    return f


def label_signals(df: pd.DataFrame, sig: pd.DataFrame,
                  max_horizon_bars: int = 96) -> pd.Series:
    """For each signal bar, label 1 if TP was hit before SL, else 0.

    Labels are produced for bars where signal != 0; NaN otherwise.
    """
    labels = pd.Series(np.nan, index=df.index, dtype="float64")
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    sigs = sig["signal"].values
    sls = sig["sl"].values
    tps = sig["tp"].values
    n = len(df)

    for i in range(n):
        s = int(sigs[i])
        if s == 0:
            continue
        sl = sls[i]
        tp = tps[i]
        if not np.isfinite(sl) or not np.isfinite(tp):
            continue
        # Walk forward up to horizon to see which fires first
        end = min(i + 1 + max_horizon_bars, n)
        outcome = 0
        for j in range(i + 1, end):
            h = highs[j]
            l = lows[j]
            if s == 1:
                hit_sl = l <= sl
                hit_tp = h >= tp
            else:
                hit_sl = h >= sl
                hit_tp = l <= tp
            if hit_sl and hit_tp:
                outcome = 0  # conservative: assume SL filled first
                break
            if hit_sl:
                outcome = 0
                break
            if hit_tp:
                outcome = 1
                break
        else:
            # Time stop — closed at last bar's close
            exit_px = closes[end - 1]
            outcome = 1 if (exit_px - closes[i]) * s > 0 else 0
        labels.iat[i] = outcome
    return labels


def walk_forward_predict(features: pd.DataFrame, labels: pd.Series,
                         train_bars: int = 24 * 365,
                         step_bars: int = 24 * 90,
                         model_factory=None,
                         min_proba: float = 0.5) -> pd.Series:
    """Walk-forward prediction. Returns per-bar predicted P(win).

    For each window, train on the prior train_bars worth of LABELED rows,
    then predict for the next step_bars. Slide forward step_bars at a time.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    if model_factory is None:
        model_factory = lambda: GradientBoostingClassifier(
            n_estimators=120, max_depth=3, learning_rate=0.05,
            subsample=0.85, random_state=42)

    preds = pd.Series(np.nan, index=features.index, dtype="float64")
    feat = features.copy()
    feat = feat.replace([np.inf, -np.inf], np.nan).fillna(0)
    lab = labels.copy()
    n = len(feat)

    start = train_bars
    while start < n:
        end = min(start + step_bars, n)
        train_idx = feat.index[max(0, start - train_bars):start]
        test_idx = feat.index[start:end]

        # Only train on rows that have a label (i.e. were signals)
        train_mask = lab.loc[train_idx].notna()
        if train_mask.sum() < 30:  # not enough samples
            start = end
            continue
        Xtr = feat.loc[train_idx][train_mask].values
        ytr = lab.loc[train_idx][train_mask].astype(int).values
        if len(set(ytr)) < 2:
            start = end
            continue
        model = model_factory()
        model.fit(Xtr, ytr)

        Xte = feat.loc[test_idx].values
        proba = model.predict_proba(Xte)[:, 1]
        preds.loc[test_idx] = proba
        start = end
    return preds


def filter_signals_by_proba(sig: pd.DataFrame, proba: pd.Series,
                            min_proba: float) -> pd.DataFrame:
    """Zero out signals whose predicted P(win) is below threshold."""
    out = sig.copy()
    skip = (out["signal"] != 0) & (proba.fillna(0) < min_proba)
    out.loc[skip, "signal"] = 0
    out.loc[skip, ["sl", "tp"]] = np.nan
    return out
