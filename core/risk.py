"""Shared risk-sizing helpers used by BOTH the backtester and the live bot.

Keeping this logic in one place guarantees the live bot decays risk exactly
the way the backtest measured it.
"""
from __future__ import annotations


# Default multi-tier drawdown decay ladder.
# Each entry is (drawdown_depth, risk_multiplier): when the current equity
# drawdown is at least this deep, risk-per-trade is scaled by the multiplier.
# The DEEPEST breached tier wins. 0.0 multiplier = stop opening new trades.
#   -20% -> half risk   (slow the bleed)
#   -35% -> quarter risk (defend hard)
#   -50% -> no new trades (capital preservation; existing positions run stops)
DEFAULT_DECAY_TIERS: tuple[tuple[float, float], ...] = (
    (0.20, 0.50),
    (0.35, 0.25),
    (0.50, 0.00),
)


def decay_risk_scale(drawdown: float,
                     tiers: tuple[tuple[float, float], ...]) -> float:
    """Return the risk multiplier for the current drawdown.

    drawdown : current equity drawdown as a NEGATIVE fraction (e.g. -0.27).
    tiers    : iterable of (depth, multiplier). depth is a POSITIVE fraction
               (0.20 == a -20% drawdown).

    The deepest breached tier wins. Returns 1.0 if no tier is breached or
    tiers is empty.
    """
    scale = 1.0
    for depth, mult in sorted(tiers):  # ascending depth: deepest breach wins
        if drawdown <= -depth:
            scale = mult
    return scale


def parse_tiers(spec: str) -> tuple[tuple[float, float], ...]:
    """Parse an env-style tier spec like '0.20:0.5,0.35:0.25,0.50:0.0'.

    Returns () on empty/blank input so callers can fall back to a default.
    """
    spec = (spec or "").strip()
    if not spec:
        return ()
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        depth_s, mult_s = part.split(":")
        out.append((float(depth_s), float(mult_s)))
    return tuple(out)
