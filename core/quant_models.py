"""Mathematical model add-ons.

Module groups:
  - garch:   GARCH(1,1) volatility forecasting (no scipy deps)
  - hurst:   Hurst exponent for trend/mean-reversion regime detection
  - monte:   Monte Carlo simulation of forward equity paths
  - bootstrap: Edge significance test (compare to GBM null)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# GARCH(1,1) volatility forecasting (vanilla, no scipy)
# --------------------------------------------------------------------------
def garch_11_fit(returns: np.ndarray, max_iter: int = 80) -> tuple[float, float, float]:
    """Fit GARCH(1,1): sigma^2_t = omega + alpha * eps^2_{t-1} + beta * sigma^2_{t-1}.

    Uses simple coordinate-descent on quasi-log-likelihood. Not as good as
    arch package but no scipy dep. Returns (omega, alpha, beta).
    """
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 100:
        return 1e-6, 0.05, 0.92  # weak prior
    var_uncond = float(np.var(r))
    # Reasonable starting values
    alpha = 0.05
    beta = 0.92
    omega = var_uncond * (1 - alpha - beta)

    def neg_ll(params):
        om, a, b = params
        if om <= 0 or a < 0 or b < 0 or a + b >= 1:
            return 1e18
        sigma2 = np.empty_like(r)
        sigma2[0] = var_uncond
        for i in range(1, len(r)):
            sigma2[i] = om + a * r[i - 1] ** 2 + b * sigma2[i - 1]
        sigma2 = np.maximum(sigma2, 1e-12)
        return 0.5 * float(np.sum(np.log(sigma2) + r ** 2 / sigma2))

    best = (omega, alpha, beta)
    best_ll = neg_ll(best)
    # Very simple coordinate search
    for _ in range(max_iter):
        improved = False
        for d in (1.10, 0.90):
            for idx in range(3):
                trial = list(best)
                trial[idx] = trial[idx] * d
                if idx >= 1 and trial[1] + trial[2] >= 0.999:
                    continue
                ll = neg_ll(trial)
                if ll < best_ll:
                    best = tuple(trial)
                    best_ll = ll
                    improved = True
        if not improved:
            break
    return best


def garch_forecast(returns: pd.Series, horizon: int = 1) -> pd.Series:
    """One-step-ahead GARCH(1,1) volatility forecast for each bar.

    Returns sigma (not sigma^2). Forecast at bar t uses returns up to t-1,
    so no look-ahead. The first ~100 bars are filled with rolling std as
    a warm-up.
    """
    r = returns.fillna(0).values
    n = len(r)
    out = np.empty(n)
    out[:] = np.nan
    warmup = 200
    if n < warmup:
        return pd.Series(out, index=returns.index)

    # Fit once on the first warmup window, then forecast forward
    omega, alpha, beta = garch_11_fit(r[:warmup])
    sigma2 = np.var(r[:warmup])
    for i in range(warmup, n):
        eps = r[i - 1]
        sigma2 = omega + alpha * eps * eps + beta * sigma2
        out[i] = np.sqrt(max(sigma2, 1e-12))
    return pd.Series(out, index=returns.index)


# --------------------------------------------------------------------------
# Hurst exponent — trending vs mean-reverting regime
# --------------------------------------------------------------------------
def hurst_exponent(series: pd.Series, min_lag: int = 2, max_lag: int = 100) -> float:
    """Estimate Hurst exponent via R/S analysis (rescaled range).
       H > 0.5  -> trending
       H = 0.5  -> random walk
       H < 0.5  -> mean-reverting
    """
    s = np.asarray(series.dropna(), dtype=float)
    if len(s) < max_lag + 10:
        return 0.5
    lags = range(min_lag, max_lag)
    # Variance of differences at different lags (cheap Hurst estimate)
    tau = [np.sqrt(np.std(s[lag:] - s[:-lag])) for lag in lags]
    # log-log regression
    if min(tau) <= 0:
        return 0.5
    poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
    return float(poly[0]) * 2.0


def rolling_hurst(close: pd.Series, window: int = 200) -> pd.Series:
    """Rolling Hurst exponent. Slow (O(N*W)) so only call on 1h+ data."""
    out = pd.Series(np.nan, index=close.index)
    for i in range(window, len(close)):
        sub = close.iloc[i - window:i]
        out.iat[i] = hurst_exponent(sub)
    return out


# --------------------------------------------------------------------------
# Monte Carlo simulation — block bootstrap of strategy bar returns
# --------------------------------------------------------------------------
def monte_carlo_paths(equity_curve: pd.Series, n_paths: int = 1000,
                      horizon_bars: int = 30 * 24,
                      block_size: int = 24,
                      seed: int = 42) -> np.ndarray:
    """Generate n_paths simulated forward equity curves of length horizon_bars
    by block-bootstrapping the historical bar-returns.

    Block bootstrap (Politis & Romano) preserves short-term autocorrelation
    that destroys naive iid bootstrap.

    Returns: array shape (n_paths, horizon_bars+1) starting at 1.0.
    """
    rng = np.random.default_rng(seed)
    rets = equity_curve.pct_change().dropna().values
    if len(rets) < block_size * 2:
        raise ValueError("not enough returns")
    n_blocks = horizon_bars // block_size + 1
    n_source_blocks = len(rets) - block_size + 1
    paths = np.zeros((n_paths, horizon_bars + 1))
    paths[:, 0] = 1.0
    for p in range(n_paths):
        starts = rng.integers(0, n_source_blocks, size=n_blocks)
        blocks = [rets[s:s + block_size] for s in starts]
        sim = np.concatenate(blocks)[:horizon_bars]
        for t in range(horizon_bars):
            paths[p, t + 1] = paths[p, t] * (1 + sim[t])
    return paths


def mc_summary(paths: np.ndarray, percentiles=(5, 25, 50, 75, 95)) -> dict:
    """Distribution stats over the terminal value and max drawdown."""
    final = paths[:, -1]
    mdd = np.array([(p / np.maximum.accumulate(p) - 1).min() for p in paths])
    out = {
        "n_paths": int(paths.shape[0]),
        "final_pct": {f"p{q}": round((np.percentile(final, q) - 1) * 100, 2)
                      for q in percentiles},
        "mdd_pct": {f"p{q}": round(np.percentile(mdd, q) * 100, 2)
                    for q in percentiles},
        "p_loss": round(float((final < 1.0).mean()), 3),
        "p_double": round(float((final >= 2.0).mean()), 3),
        "p_halve": round(float((final <= 0.5).mean()), 3),
    }
    return out


# --------------------------------------------------------------------------
# Kelly criterion — optimal risk fraction given win rate and payoff
# --------------------------------------------------------------------------
def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> dict:
    """Kelly fraction for binary bet with asymmetric payoffs.

    Optimal fraction f* = (p*b - q) / b
    where p = win_rate, q = 1-p, b = |avg_win| / |avg_loss|

    Returns full Kelly, half-Kelly (commonly used in practice), and
    quarter-Kelly. Most professional traders cap at half-Kelly because
    Kelly's variance is large and the formula assumes the win rate /
    payoffs are known exactly (they aren't).
    """
    p = win_rate
    q = 1 - p
    b = abs(avg_win) / abs(avg_loss) if avg_loss != 0 else 0
    f_star = (p * b - q) / b if b > 0 else 0
    return {
        "win_rate": round(p, 3),
        "win_loss_ratio": round(b, 3),
        "edge_per_trade": round(p * b - q, 4),
        "kelly_full_pct": round(f_star * 100, 2),
        "kelly_half_pct": round(f_star * 50, 2),
        "kelly_quarter_pct": round(f_star * 25, 2),
    }


# --------------------------------------------------------------------------
# Bootstrap edge-significance test — strategy vs random-entry strategies
# --------------------------------------------------------------------------
def edge_significance(strategy_eq_curve: pd.Series,
                      asset_close: pd.Series,
                      strategy_n_trades: int,
                      avg_hold_bars: int,
                      n_simulations: int = 1000,
                      bars_per_year: int = 24 * 365,
                      seed: int = 42) -> dict:
    """Compare strategy Sharpe to RANDOM-TIMING strategies on the same asset.

    Null hypothesis: the strategy has no selectivity — its Sharpe is no
    better than a strategy that goes long at random times for similar
    holding periods.

    We generate `n_simulations` random-entry strategies with the same trade
    count and avg holding period; each simulated strategy's Sharpe forms
    the null distribution. p-value = P(random Sharpe >= observed).
    """
    rng = np.random.default_rng(seed)
    asset_rets = asset_close.pct_change().fillna(0).values
    n_bars = len(asset_rets)
    eq_rets = strategy_eq_curve.pct_change().fillna(0).values

    def sharpe(x):
        s = x.std()
        return (x.mean() / s * np.sqrt(bars_per_year)) if s > 0 else 0.0

    obs_sharpe = sharpe(eq_rets)

    # Generate random strategies: pick `strategy_n_trades` random entry bars
    # and hold each for `avg_hold_bars`. The "in-position" mask multiplies
    # the asset's per-bar return.
    boot = np.empty(n_simulations)
    for i in range(n_simulations):
        positions = np.zeros(n_bars, dtype=bool)
        starts = rng.integers(0, n_bars - avg_hold_bars,
                              size=strategy_n_trades)
        for s in starts:
            positions[s:s + avg_hold_bars] = True
        sim_rets = asset_rets * positions
        boot[i] = sharpe(sim_rets)

    p_val = float((boot >= obs_sharpe).mean())
    return {
        "observed_sharpe": round(float(obs_sharpe), 3),
        "random_strats_median_sharpe": round(float(np.median(boot)), 3),
        "random_strats_p95_sharpe": round(float(np.percentile(boot, 95)), 3),
        "random_strats_p99_sharpe": round(float(np.percentile(boot, 99)), 3),
        "p_value": round(p_val, 4),
        "n_simulations": int(n_simulations),
    }
