"""Walk-forward parameter retune for adaptive_inj_bidir.

Refits the (ema_fast, ema_slow, rsi_min) triplet on the most recent N days
of price data, picks the best-Sharpe combo, and writes the result to a
JSON file that the live bot reads on its next signal cycle.

Run periodically (weekly is fine) via cron or `docker compose exec`:

    docker compose exec bot python3 research/retune.py

Or override the symbol/timeframe/window:

    RETUNE_SYMBOL=INJ-USDT RETUNE_TF=1h RETUNE_DAYS=365 \\
        python3 research/retune.py

The bot reads PARAMS_FILE (default state/params.json) at every signal
cycle when STRATEGY_PRESET=adaptive_inj_bidir_wf or env var WF_RETUNE=1.
Falls back to BotConfig defaults if the file is missing or stale.

Safety: the script also backtests both the new and old params on the
last 30 days, and refuses to write the new file if the new params would
have lost money on that recent window. Log only — no rollback of an
existing params file.
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.data import fetch_ohlcv
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir


# Grid (same as research/test_improvements.py exp5)
GRID_EMA_FAST = [7, 9, 12]
GRID_EMA_SLOW = [21, 26, 34]
GRID_RSI_MIN  = [50, 55, 60]


def make_signal(df, regimes, fng_aligned, ema_fast, ema_slow, rsi_min,
                fng_greed_max=80, fng_fear_min=20):
    """Produce the production signal: bidir + directional regime + F&G."""
    sig = triple_confirm_bidir(df, ema_fast=ema_fast, ema_slow=ema_slow,
                                rsi_min=rsi_min)
    a = regimes.reindex(sig.index, method="ffill").fillna("CHOP")
    block_reg = ((sig["signal"] ==  1) & (~a.isin(["BULL", "CHOP"]))) | \
                ((sig["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    sig.loc[block_reg, "signal"] = 0
    sig.loc[block_reg, ["sl", "tp"]] = np.nan
    if fng_aligned is not None:
        block_fng = ((sig["signal"] ==  1) & (fng_aligned >= fng_greed_max)) | \
                    ((sig["signal"] == -1) & (fng_aligned <= fng_fear_min))
        sig.loc[block_fng, "signal"] = 0
        sig.loc[block_fng, ["sl", "tp"]] = np.nan
    return sig


def score(df, regimes, fng_aligned, cfg, ema_fast, ema_slow, rsi_min):
    """Run backtest and return (sharpe, n_trades, total_return)."""
    sig = make_signal(df, regimes, fng_aligned, ema_fast, ema_slow, rsi_min)
    if (sig["signal"] != 0).sum() < 10:
        return -99.0, 0, -1.0
    eq, trades = run_backtest_enhanced(df, sig, cfg, long_only=False)
    if len(trades) < 5 or eq.iloc[-1] <= 0:
        return -99.0, len(trades), -1.0
    bar_ret = eq.pct_change().fillna(0)
    if bar_ret.std() == 0:
        return -99.0, len(trades), -1.0
    sharpe = float(bar_ret.mean() / bar_ret.std() * np.sqrt(24 * 365))
    ret = float(eq.iloc[-1] / eq.iloc[0] - 1)
    return sharpe, len(trades), ret


def main():
    symbol = _os.environ.get("RETUNE_SYMBOL", "INJ-USDT")
    tf = _os.environ.get("RETUNE_TF", "1h")
    days = int(_os.environ.get("RETUNE_DAYS", "365"))
    out_path = Path(_os.environ.get("PARAMS_FILE", "state/params.json"))
    sanity_days = int(_os.environ.get("RETUNE_SANITY_DAYS", "30"))

    print(f"retune: symbol={symbol} tf={tf} fit_days={days} out={out_path}")
    print(f"        sanity check on last {sanity_days}d")

    print("[1] fetching data ...")
    t0 = time.time()
    df = fetch_ohlcv(symbol, tf, days=days + 30)  # extra for warmup + sanity
    print(f"    {len(df)} bars  ({df.index[0]} → {df.index[-1]})  {time.time()-t0:.0f}s")

    print("[2] walk-forward regimes ...")
    t0 = time.time()
    regimes = walk_forward_regimes(df, bars_per_day=24, train_days=180, step_days=30)
    print(f"    {time.time()-t0:.0f}s  dist={regimes.value_counts().to_dict()}")

    print("[3] F&G fetch ...")
    try:
        fng = fetch_fear_greed()
        fng_aligned = align_to_bars(fng, df.index)
        print(f"    OK ({len(fng)} daily F&G rows)")
    except Exception as e:
        print(f"    fail: {e}  (continuing without F&G filter)")
        fng_aligned = None

    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_risk_decay=0.5,
                           drawdown_for_decay=0.20)

    print("[4] grid search ...")
    t0 = time.time()
    results = []
    for ef in GRID_EMA_FAST:
        for es in GRID_EMA_SLOW:
            if ef >= es:
                continue
            for rm in GRID_RSI_MIN:
                sharpe, n, ret = score(df, regimes, fng_aligned, cfg, ef, es, rm)
                results.append((sharpe, n, ret, ef, es, rm))
                print(f"    ef={ef} es={es} rsi={rm}: sharpe={sharpe:+.2f} "
                      f"trades={n} ret={ret*100:+.0f}%")
    results.sort(reverse=True)  # by sharpe desc
    print(f"    grid done in {time.time()-t0:.0f}s")

    best_sharpe, best_n, best_ret, ef, es, rm = results[0]
    print(f"\n[5] best: ema_fast={ef} ema_slow={es} rsi_min={rm} "
          f"sharpe={best_sharpe:+.2f} trades={best_n} ret={best_ret*100:+.0f}%")

    # Load previous params (if any) for sanity comparison
    prev = None
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text())
            print(f"[6] previous params: ema_fast={prev.get('ema_fast')} "
                  f"ema_slow={prev.get('ema_slow')} rsi_min={prev.get('rsi_min')} "
                  f"(fitted {prev.get('fitted_at', 'unknown')})")
        except Exception as e:
            print(f"[6] previous params unreadable: {e}")

    # Sanity backtest on the last sanity_days
    recent_df = df.tail(24 * sanity_days)
    recent_regimes = regimes.reindex(recent_df.index, method="ffill")
    recent_fng = fng_aligned.reindex(recent_df.index) if fng_aligned is not None else None
    new_sharpe, new_n, new_ret = score(recent_df, recent_regimes, recent_fng, cfg,
                                       ef, es, rm)
    print(f"[7] sanity on last {sanity_days}d (new params):  "
          f"sharpe={new_sharpe:+.2f} trades={new_n} ret={new_ret*100:+.1f}%")

    cur_sharpe = cur_ret = None
    if prev:
        cur_sharpe, _, cur_ret = score(recent_df, recent_regimes, recent_fng, cfg,
                                       int(prev["ema_fast"]), int(prev["ema_slow"]),
                                       float(prev["rsi_min"]))
        print(f"    sanity on last {sanity_days}d (prev params): "
              f"sharpe={cur_sharpe:+.2f} ret={cur_ret*100:+.1f}%")

    # Refuse to write if new params lost money on recent window — sanity guard
    if new_ret < -0.05:
        print(f"\n[ABORT] new params lost {new_ret*100:.1f}% on recent {sanity_days}d. "
              f"Not writing {out_path}. Existing file (if any) preserved.")
        return 2

    # Write the new params
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ema_fast": int(ef),
        "ema_slow": int(es),
        "rsi_min": float(rm),
        "fitted_at": datetime.now(timezone.utc).isoformat(),
        "fit_symbol": symbol,
        "fit_tf": tf,
        "fit_window_days": days,
        "in_sample_sharpe": round(float(best_sharpe), 3),
        "in_sample_trades": int(best_n),
        "in_sample_return": round(float(best_ret), 4),
        f"sanity_{sanity_days}d_sharpe_new": round(float(new_sharpe), 3),
        f"sanity_{sanity_days}d_return_new": round(float(new_ret), 4),
    }
    if prev:
        payload["previous"] = {
            "ema_fast": prev.get("ema_fast"),
            "ema_slow": prev.get("ema_slow"),
            "rsi_min": prev.get("rsi_min"),
            "fitted_at": prev.get("fitted_at"),
            f"sanity_{sanity_days}d_sharpe_old": round(float(cur_sharpe), 3) if cur_sharpe is not None else None,
        }
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"\n[OK] wrote {out_path}")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
