"""Regime-aware Monte Carlo simulation.

Standard block-bootstrap MC assumes the future has the same return
distribution as the past. Regime-aware MC is more realistic:

    1. Detect the regimes in historical data (BULL / CHOP / BEAR).
    2. Build a Markov transition matrix between regimes.
    3. Build a per-regime distribution of bar returns.
    4. Simulate forward by walking the Markov chain and sampling
       returns from the appropriate regime distribution.

This generates scenarios that are consistent with crypto's actual
behavior (e.g., extended bear markets followed by recovery), not just
permutations of the historical sample.
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
    regime_returns,
    transition_matrix,
)


def fit_regime_mc(equity_curve: pd.Series, asset_close: pd.Series,
                  bars_per_day: int = 24,
                  warmup_bars: int = 200) -> dict:
    """Fit the regime model + per-regime return distributions on history.

    Returns a dict with everything needed to generate paths:
        - 'regimes': pd.Series of regime labels per bar
        - 'transitions': transition matrix as DataFrame
        - 'regime_stats': per-regime return distribution dicts
        - 'gmm', 'mapping': fitted GMM and component->label dict
        - 'initial_regime': most recent observed regime
    """
    # Build features from the asset (regimes are about market state, not
    # strategy state). Strategy bar-returns are sampled from those regimes.
    asset_df = pd.DataFrame({"close": asset_close,
                             "high": asset_close, "low": asset_close})
    feats = build_features(asset_df, bars_per_day=bars_per_day)
    fm = feature_matrix(feats)
    gmm, mapping = fit_gmm(fm.iloc[warmup_bars:], n_components=3)
    regimes = predict_regimes(gmm, mapping, fm)
    bar_rets = equity_curve.pct_change().fillna(0)
    rstats = regime_returns(bar_rets, regimes)
    trans = transition_matrix(regimes)
    return {
        "regimes": regimes,
        "transitions": trans,
        "regime_stats": rstats,
        "gmm": gmm,
        "mapping": mapping,
        "initial_regime": regimes.iloc[-1],
    }


def run_regime_mc(equity_curve: pd.Series, asset_close: pd.Series,
                  n_paths: int = 2000, horizon_bars: int = 24 * 30 * 6,
                  bars_per_day: int = 24,
                  seed: int = 42) -> tuple[np.ndarray, np.ndarray, dict]:
    """Run `n_paths` regime-aware simulations.

    Vectorised: generates all path bars in tight numpy loops over time,
    not over paths. Returns (paths, regime_idx_sequences, model).
    paths has shape (n_paths, horizon_bars+1).
    """
    model = fit_regime_mc(equity_curve, asset_close,
                          bars_per_day=bars_per_day)
    rng = np.random.default_rng(seed)
    states = REGIME_LABELS
    n_states = len(states)
    state_idx = {s: i for i, s in enumerate(states)}

    # Transition matrix as numpy
    T = model["transitions"].reindex(index=states, columns=states).values
    # Cumulative for fast inverse-CDF sampling
    Tcum = np.cumsum(T, axis=1)

    # Per-state samples arrays
    samples = [model["regime_stats"][s]["samples"] for s in states]
    sample_lens = np.array([len(x) for x in samples])

    # Initialise
    initial = model["initial_regime"]
    if initial not in state_idx:
        initial = "CHOP"
    init_i = state_idx[initial]

    # State trajectories: (n_paths, horizon_bars)
    regs = np.empty((n_paths, horizon_bars), dtype=np.int8)
    regs[:, 0] = init_i
    rand_state = rng.random((n_paths, horizon_bars - 1))
    for t in range(1, horizon_bars):
        # For each path, sample next state from row T[regs[:, t-1]]
        cur = regs[:, t - 1]
        cum_rows = Tcum[cur]            # (n_paths, n_states)
        u = rand_state[:, t - 1][:, None]
        regs[:, t] = (u >= cum_rows[:, :-1]).sum(axis=1)

    # Returns: for each path, sample one return per bar from that bar's
    # regime's distribution.
    rets = np.zeros((n_paths, horizon_bars))
    for s_i in range(n_states):
        mask = regs == s_i
        n_needed = int(mask.sum())
        if n_needed == 0 or sample_lens[s_i] == 0:
            continue
        idx = rng.integers(0, sample_lens[s_i], size=n_needed)
        rets[mask] = samples[s_i][idx]

    paths = np.empty((n_paths, horizon_bars + 1))
    paths[:, 0] = 1.0
    paths[:, 1:] = np.cumprod(1 + rets, axis=1)
    return paths, regs, model
