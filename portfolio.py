"""Multi-pair portfolio: run a strategy on N pairs simultaneously and
combine their equity curves with fixed weights.

This captures the "diversification" idea: two uncorrelated edges produce a
smoother equity curve than either alone, even if their absolute returns are
similar. We test ETH+SOL because both showed positive 1y Sharpe with the
Donchian strategy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from backtest import BTConfig, run_backtest
from data import fetch_ohlcv


@dataclass
class PortfolioResult:
    equity_curve: pd.Series
    per_asset: dict[str, pd.Series]
    stats: dict


def run_portfolio(pairs: list[str], tf: str, days: int,
                  signal_fn: Callable[[pd.DataFrame], pd.DataFrame],
                  weights: list[float] | None = None,
                  cfg: BTConfig | None = None,
                  long_only: bool = False) -> PortfolioResult:
    """Each pair gets `weight * starting_equity` of capital and runs
    independently. We sum the dollar-equity curves to get the portfolio."""
    if weights is None:
        weights = [1.0 / len(pairs)] * len(pairs)
    assert len(weights) == len(pairs)
    assert abs(sum(weights) - 1.0) < 1e-6
    if cfg is None:
        cfg = BTConfig()

    per_asset = {}
    full_equity: pd.Series | None = None
    for pair, w in zip(pairs, weights):
        sub_cfg = BTConfig(
            starting_equity=cfg.starting_equity * w,
            risk_per_trade=cfg.risk_per_trade,
            max_leverage=cfg.max_leverage,
            fee_rate=cfg.fee_rate,
            slip_bps=cfg.slip_bps,
            max_bars_in_trade=cfg.max_bars_in_trade,
            allow_short=cfg.allow_short,
        )
        df = fetch_ohlcv(pair, tf, days=days)
        sig = signal_fn(df)
        res = run_backtest(df, sig, sub_cfg, long_only=long_only)
        per_asset[pair] = res.equity_curve
        if full_equity is None:
            full_equity = res.equity_curve.copy()
        else:
            # Align indexes (pairs share the same KuCoin grid for same tf+days)
            full_equity = full_equity.add(res.equity_curve, fill_value=0)

    eq = full_equity
    peak = eq.cummax()
    dd = float((eq / peak - 1).min())
    ret = float(eq.iloc[-1] / eq.iloc[0] - 1)
    bar_ret = eq.pct_change().fillna(0)
    bars_per_year = {"15m": 96 * 365, "1h": 24 * 365, "4h": 6 * 365}.get(tf, 24 * 365)
    sharpe = 0.0
    if bar_ret.std() > 0:
        import numpy as np
        sharpe = (bar_ret.mean() / bar_ret.std()) * np.sqrt(bars_per_year)
    stats = {
        "final_equity": round(float(eq.iloc[-1]), 2),
        "total_return": round(ret, 4),
        "max_drawdown": round(dd, 4),
        "sharpe": round(float(sharpe), 2),
    }
    return PortfolioResult(equity_curve=eq, per_asset=per_asset, stats=stats)
