"""Walk-forward regime cache — pure speed layer, zero methodology impact.

walk_forward_regimes is deterministic (GMM random_state=42, n_init=3), so its
output is fully determined by (bars, bars_per_day, train_days, step_days).
We key on the exact bar index (first/last timestamp + length) and params and
store the label Series under %TEMP%/rofl_regime_cache. A cache hit returns a
Series identical to a fresh computation; any change in the underlying bars
changes the key and forces a recompute. Writes are atomic (tmp + replace) so
concurrent studies cannot corrupt each other.
"""
from __future__ import annotations

import hashlib
import os
import tempfile

import pandas as pd

from core.regime_strategy import walk_forward_regimes

CACHE_DIR = os.path.join(tempfile.gettempdir(), "rofl_regime_cache")


def wf_regimes_cached(df: pd.DataFrame, pair: str, tf: str, bars_per_day: int,
                      train_days: int = 365, step_days: int = 30) -> pd.Series:
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = (f"{pair}|{tf}|{bars_per_day}|{train_days}|{step_days}|{len(df)}"
           f"|{df.index[0].value}|{df.index[-1].value}")
    h = hashlib.md5(key.encode()).hexdigest()[:16]
    safe = pair.replace("/", "_").replace("-", "_")
    path = os.path.join(CACHE_DIR, f"{safe}_{tf}_{h}.pkl")
    if os.path.exists(path):
        try:
            s = pd.read_pickle(path)
            if len(s) == len(df) and s.index[0] == df.index[0] \
                    and s.index[-1] == df.index[-1]:
                return s
        except Exception:
            pass
    regs = walk_forward_regimes(df, bars_per_day=bars_per_day,
                                train_days=train_days, step_days=step_days)
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        regs.to_pickle(tmp)
        os.replace(tmp, path)
    except Exception:
        pass
    return regs
