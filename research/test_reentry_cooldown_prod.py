"""Validate the post-SL same-side re-entry cooldown ON THE FULL PRODUCTION STACK.

The original test_reentry_cooldown.py measured the cooldown on the RAW
triple_confirm_bidir signal (no regime, no F&G, no decay, no funding) over a
single in-sample 400d window — FINDINGS flagged that as insufficient. This is
the honest re-run:

  signal = triple_confirm_bidir
           -> directional regime mask (walk-forward GMM, no look-ahead)
           -> F&G 3-day-persistence mask (the ADOPTED production filter)
  engine = run_backtest_enhanced + DEFAULT_DECAY_TIERS, then apply_funding
  history = long (DAYS, default 1500d) with the first 365d dropped (regime warmup)
  robustness = full post-warmup period AND N contiguous sub-windows

K=0 MUST reproduce run_backtest_enhanced (sanity assert). Needs sklearn for the
regime GMM (assert the regime series is non-trivial, else it is a silent no-op).

Run from project root:
    python research/test_reentry_cooldown_prod.py
    DAYS=1500 N_WIN=3 python research/test_reentry_cooldown_prod.py
"""
from __future__ import annotations

import sys as _sys, os as _os
_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PARENT not in _sys.path:
    _sys.path.insert(0, _PARENT)

import time
import numpy as np
import pandas as pd

import core.regime as _regime
from core.backtest import Trade
from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced
from core.risk import DEFAULT_DECAY_TIERS, decay_risk_scale
from core.data import fetch_ohlcv
from core.indicators import atr as atr_fn
from core.regime_strategy import walk_forward_regimes
from core.sentiment import align_to_bars, fetch_fear_greed
from core.strategies import triple_confirm_bidir

PAIRS = ["INJ-USDT", "SOL-USDT", "ADA-USDT", "ETH-USDT", "LINK-USDT"]
DAYS = int(_os.environ.get("DAYS", 1500))
WARMUP_D = 365
N_WIN = int(_os.environ.get("N_WIN", 3))
KS = [0, 3, 6, 12]


def _slip(p, side, slip_bps, is_entry):
    s = slip_bps / 10_000
    return p * (1 + s * side) if is_entry else p * (1 - s * side)


def run_enhanced_with_cooldown(price_df, sig_df, cfg, K, long_only=False):
    """Verbatim copy of core.backtest_enhanced.run_backtest_enhanced + a post-SL
    same-side re-entry cooldown of K bars. K=0 == run_backtest_enhanced."""
    df = price_df.join(sig_df, how="inner").copy()
    df["sig_next"] = df["signal"].shift(1).fillna(0).astype(int)
    df["sl_next"] = df["sl"].shift(1)
    df["tp_next"] = df["tp"].shift(1)
    df["atr14"] = atr_fn(df["high"], df["low"], df["close"], 14)
    df["risk_mult_next"] = 1.0
    if long_only or not cfg.allow_short:
        df["sig_next"] = df["sig_next"].clip(lower=0)

    equity = cfg.starting_equity
    eq_peak = cfg.starting_equity
    eq_curve = []
    trades = []
    pos_side = 0
    pos_qty = pos_entry = pos_sl = pos_tp = pos_atr = 0.0
    pos_open_time = None
    pos_bars = 0
    pos_notional = 0.0
    partial_done = False
    day_start_eq = equity
    day = None
    day_blocked = False
    block_long_until = block_short_until = -1

    for i, (ts, row) in enumerate(df.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        sig_next = int(row["sig_next"])
        cur_day = ts.normalize()
        if cur_day != day:
            day = cur_day
            day_start_eq = equity
            day_blocked = False

        if cfg.daily_loss_pct > 0:
            day_pnl_pct = (equity - day_start_eq) / day_start_eq if day_start_eq > 0 else 0
            if day_pnl_pct <= -cfg.daily_loss_pct:
                day_blocked = True

        eq_peak = max(eq_peak, equity)
        risk_scale = 1.0
        cur_dd = (equity / eq_peak - 1) if eq_peak > 0 else 0
        if cfg.eq_decay_tiers:
            risk_scale = decay_risk_scale(cur_dd, cfg.eq_decay_tiers)
        elif cfg.eq_risk_decay > 0:
            if cur_dd <= -cfg.drawdown_for_decay:
                risk_scale = cfg.eq_risk_decay

        if pos_side != 0:
            pos_bars += 1
            if cfg.partial_tp_atr > 0 and not partial_done and pos_atr > 0:
                pt_hit = ((h - pos_entry) >= cfg.partial_tp_atr * pos_atr) if pos_side == 1 \
                    else ((pos_entry - l) >= cfg.partial_tp_atr * pos_atr)
                if pt_hit:
                    half_qty = pos_qty / 2
                    fill_px = pos_entry + cfg.partial_tp_atr * pos_atr * pos_side
                    fill_px = _slip(fill_px, pos_side, cfg.slip_bps, False)
                    gross = (fill_px - pos_entry) * half_qty * pos_side
                    fee = fill_px * half_qty * cfg.fee_rate
                    equity += gross - fee
                    pos_qty -= half_qty
                    if cfg.partial_to_breakeven:
                        pos_sl = pos_entry
                    partial_done = True

            hit_sl = (l <= pos_sl) if pos_side == 1 else (h >= pos_sl)
            hit_tp = (h >= pos_tp) if pos_side == 1 else (l <= pos_tp)
            exit_px = None
            reason = ""
            if hit_sl and hit_tp:
                exit_px = pos_sl; reason = "sl"
            elif hit_sl:
                exit_px = pos_sl; reason = "sl"
            elif hit_tp:
                exit_px = pos_tp; reason = "tp"
            elif pos_bars >= cfg.max_bars_in_trade:
                exit_px = c; reason = "time"
            elif sig_next != 0 and sig_next != pos_side:
                exit_px = o; reason = "signal"
            if exit_px is not None:
                fill = _slip(exit_px, pos_side, cfg.slip_bps, False)
                gross = (fill - pos_entry) * pos_qty * pos_side
                fee = fill * pos_qty * cfg.fee_rate
                pnl = gross - fee
                equity += pnl
                trades.append(Trade(
                    side=pos_side, entry_time=pos_open_time, exit_time=ts,
                    entry_px=pos_entry, exit_px=fill, qty=pos_qty,
                    notional=pos_notional, sl=pos_sl, tp=pos_tp,
                    pnl=pnl, fees=fee, reason=reason, bars_held=pos_bars))
                if reason == "sl" and K > 0:                 # arm same-side cooldown
                    if pos_side == 1:
                        block_long_until = i + K
                    else:
                        block_short_until = i + K
                pos_side = 0
                pos_qty = 0.0
                partial_done = False

        blocked = (sig_next == 1 and i < block_long_until) or \
                  (sig_next == -1 and i < block_short_until)
        if (pos_side == 0 and sig_next != 0 and not day_blocked
                and risk_scale > 0 and not blocked):
            sl = row["sl_next"]; tp = row["tp_next"]; bar_atr = row["atr14"]
            if pd.notna(sl) and pd.notna(tp) and equity > 0 and pd.notna(bar_atr):
                entry_fill = _slip(o, sig_next, cfg.slip_bps, True)
                stop_dist = abs(entry_fill - sl) / entry_fill
                if stop_dist > 1e-5:
                    risk = equity * cfg.risk_per_trade * risk_scale
                    notional = min(risk / stop_dist, equity * cfg.max_leverage)
                    qty = notional / entry_fill
                    fee = notional * cfg.fee_rate
                    equity -= fee
                    pos_side = sig_next; pos_qty = qty; pos_entry = entry_fill
                    pos_sl = sl; pos_tp = tp; pos_atr = float(bar_atr)
                    pos_open_time = ts; pos_bars = 0; pos_notional = notional
                    partial_done = False

        mark = equity + ((c - pos_entry) * pos_qty * pos_side if pos_side else 0.0)
        eq_curve.append((ts, mark))

    if pos_side != 0:
        last_ts = df.index[-1]
        fill = _slip(df["close"].iloc[-1], pos_side, cfg.slip_bps, False)
        gross = (fill - pos_entry) * pos_qty * pos_side
        fee = fill * pos_qty * cfg.fee_rate
        pnl = gross - fee
        equity += pnl
        trades.append(Trade(side=pos_side, entry_time=pos_open_time, exit_time=last_ts,
                            entry_px=pos_entry, exit_px=fill, qty=pos_qty,
                            notional=pos_notional, sl=pos_sl, tp=pos_tp,
                            pnl=pnl, fees=fee, reason="eod", bars_held=pos_bars))
        eq_curve[-1] = (last_ts, equity)
    eq = pd.Series([v for _, v in eq_curve], index=[t for t, _ in eq_curve])
    return eq, trades


def regime_filtered(df, regs):
    """Production DIRECTIONAL regime mask (matches research/test_fng_soften.py)."""
    s = triple_confirm_bidir(df)
    a = regs.reindex(s.index, method="ffill").fillna("CHOP")
    b = ((s["signal"] == 1) & (~a.isin(["BULL", "CHOP"]))) | \
        ((s["signal"] == -1) & (~a.isin(["BEAR", "CHOP"])))
    s.loc[b, "signal"] = 0
    s.loc[b, ["sl", "tp"]] = np.nan
    return s


def fng_persist_3d(s, fa):
    """ADOPTED production F&G filter: block only when extreme >=3 consecutive days."""
    bars = 3 * 24
    above = (fa >= 80).rolling(bars, min_periods=bars).sum() == bars
    below = (fa <= 20).rolling(bars, min_periods=bars).sum() == bars
    bl = (s["signal"] == 1) & above.fillna(False)
    bs = (s["signal"] == -1) & below.fillna(False)
    block = bl | bs
    s = s.copy()
    s.loc[block, "signal"] = 0
    s.loc[block, ["sl", "tp"]] = np.nan
    return s


def apply_funding(eq, trades, bps=1.0):
    if not trades:
        return eq
    pb = (bps / 1e4) / 8
    off = pd.Series(0.0, index=eq.index)
    for t in trades:
        off.loc[eq.index >= t.exit_time] -= pb * t.notional * t.bars_held * t.side
    return eq + off


def stats(eq, trades):
    if len(eq) < 2 or eq.iloc[0] == 0:
        return dict(ret=0, sharpe=0, mdd=0, n=len(trades), wr=0)
    final = float(eq.iloc[-1]); ret = final / float(eq.iloc[0]) - 1
    mdd = float((eq / eq.cummax() - 1).min())
    br = eq.pct_change().fillna(0)
    sharpe = float(br.mean() / br.std() * np.sqrt(24 * 365)) if br.std() > 0 else 0
    wins = sum(1 for t in trades if t.pnl > 0)
    return dict(ret=ret, sharpe=sharpe, mdd=mdd, n=len(trades),
                wr=(wins / len(trades) if trades else 0))


def main():
    if not _regime.SKLEARN_AVAILABLE:
        raise SystemExit("sklearn not available — regime GMM would be a silent "
                         "all-CHOP no-op. Install scikit-learn before trusting this.")
    cfg = EnhancedBTConfig(starting_equity=100.0, risk_per_trade=0.020,
                           max_leverage=5.0, eq_decay_tiers=DEFAULT_DECAY_TIERS)
    print(f"DAYS={DAYS}  warmup_drop={WARMUP_D}d  windows={N_WIN}  KS={KS}\n")
    print("F&G fetch ...", flush=True)
    fng = fetch_fear_greed()

    agg_full = {K: {"ret": [], "sharpe": [], "mdd": []} for K in KS}
    agg_win = {w: {K: {"ret": [], "sharpe": [], "mdd": []} for K in KS} for w in range(N_WIN)}
    sanity_done = False

    for p in PAIRS:
        t0 = time.time()
        df = fetch_ohlcv(p, "1h", days=DAYS)
        regs = walk_forward_regimes(df, bars_per_day=24, train_days=365, step_days=30)
        rc = regs.value_counts().to_dict()
        # Guard against the all-CHOP silent no-op (sklearn missing/broken). A given
        # window may legitimately have only one of BULL/BEAR, so require some
        # non-CHOP structure rather than both.
        assert (rc.get("BULL", 0) + rc.get("BEAR", 0)) > 0, \
            f"{p}: regime series is all-CHOP {rc} — sklearn likely broken"
        fa = align_to_bars(fng, df.index)
        sig = fng_persist_3d(regime_filtered(df, regs), fa)

        cut = df.index[0] + pd.Timedelta(days=WARMUP_D)
        dfe = df[df.index >= cut]
        sige = sig[sig.index >= cut]

        if not sanity_done:
            eqA, _ = run_backtest_enhanced(dfe, sige, cfg, long_only=False)
            eqB, _ = run_enhanced_with_cooldown(dfe, sige, cfg, 0, long_only=False)
            d = abs(float(eqA.iloc[-1]) - float(eqB.iloc[-1]))
            assert d < 1e-6, f"K=0 sanity FAIL: {eqA.iloc[-1]} vs {eqB.iloc[-1]}"
            print(f"sanity: K=0 reproduces run_backtest_enhanced (delta={d:.2e}) OK\n")
            sanity_done = True

        idx = dfe.index
        bounds = [idx[0] + (idx[-1] - idx[0]) * w / N_WIN for w in range(N_WIN + 1)]
        print(f"--- {p}  ({time.time()-t0:.0f}s, {len(dfe)} eval bars, "
              f"regimes BULL={rc.get('BULL',0)} BEAR={rc.get('BEAR',0)} CHOP={rc.get('CHOP',0)}) ---")
        for K in KS:
            eq, tr = run_enhanced_with_cooldown(dfe, sige, cfg, K, long_only=False)
            eq = apply_funding(eq, tr)
            s = stats(eq, tr)
            agg_full[K]["ret"].append(s["ret"]); agg_full[K]["sharpe"].append(s["sharpe"])
            agg_full[K]["mdd"].append(s["mdd"])
            tag = " (baseline)" if K == 0 else ""
            print(f"  K={K:<2d} ret {s['ret']*100:+7.0f}%  Sharpe {s['sharpe']:5.2f}  "
                  f"MDD {s['mdd']*100:5.0f}%  trades {s['n']:3d}  WR {s['wr']*100:.0f}%{tag}")
            for w in range(N_WIN):
                lo, hi = bounds[w], bounds[w + 1]
                dw = dfe[(dfe.index >= lo) & (dfe.index < hi)]
                sw = sige[(sige.index >= lo) & (sige.index < hi)]
                if len(dw) < 50:
                    continue
                eqw, trw = run_enhanced_with_cooldown(dw, sw, cfg, K, long_only=False)
                eqw = apply_funding(eqw, trw)
                sw_ = stats(eqw, trw)
                agg_win[w][K]["ret"].append(sw_["ret"])
                agg_win[w][K]["sharpe"].append(sw_["sharpe"])
                agg_win[w][K]["mdd"].append(sw_["mdd"])
        print()

    def line(d, K, base_sh):
        msh = np.mean(d[K]["sharpe"]); mret = np.mean(d[K]["ret"]); mmdd = np.mean(d[K]["mdd"])
        delta = "" if K == 0 else f"  (dSharpe {msh-base_sh:+.2f})"
        return (f"  K={K:<2d} mean ret {mret*100:+6.0f}%  mean Sharpe {msh:5.2f}  "
                f"mean MDD {mmdd*100:5.0f}%{delta}")

    print("=" * 70)
    print(f"FULL post-warmup period — mean across {len(PAIRS)} pairs:")
    bsh = np.mean(agg_full[0]["sharpe"])
    for K in KS:
        print(line(agg_full, K, bsh))
    for w in range(N_WIN):
        if not agg_win[w][0]["sharpe"]:
            continue
        print("\n" + "-" * 70)
        print(f"WINDOW {w+1}/{N_WIN} — mean across pairs:")
        bshw = np.mean(agg_win[w][0]["sharpe"])
        for K in KS:
            print(line(agg_win[w], K, bshw))


if __name__ == "__main__":
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
