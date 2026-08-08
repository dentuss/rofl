"""Mechanics of the maker-fill depth gate, on SYNTHETIC bars.

WHY SYNTHETIC IS LEGITIMATE HERE — AND ONLY HERE. Generated data proves that
code does what it says: we author an exact penetration depth and assert the
fill gate agrees. It can NEVER support a performance claim, because a backtest
on generated prices measures the generator's assumptions and nothing else.
That distinction is the whole reason this file tests plumbing while
research/maker_fill_depth.py measures behaviour on real bars.

The gate under test (core/backtest_enhanced.py):
    long  fills when  low  < limit * (1 - min_bp/1e4)
    short fills when  high > limit * (1 + min_bp/1e4)
with min_bp = 0.0 reproducing the historical "any penetration fills".

Cases:
  1. 0.0 bp reproduces the old behaviour exactly (1 bp penetration fills)
  2. a threshold ABOVE the authored penetration blocks the fill
  3. a threshold BELOW it still fills
  4. symmetry: shorts gate on the high, mirrored
  5. monotonicity: raising the threshold can only remove fills, never add
  6. the default really is 0.0, so every prior FINDINGS number is preserved

Run:  ./.venv/bin/python test_maker_fill_depth.py
"""
import numpy as np
import pandas as pd

from core.backtest_enhanced import EnhancedBTConfig, run_backtest_enhanced

LIMIT = 100.0


WARMUP = 20   # the engine requires a defined atr14, so 14+ bars must precede


def _bars(pen_bp: float, side: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """WARMUP quiet bars (so atr14 is defined — the engine gates entries on
    pd.notna(atr14)), then a signal bar closing at LIMIT, then a bar that
    penetrates that limit by EXACTLY pen_bp."""
    n = WARMUP + 2
    idx = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    pen = LIMIT * pen_bp / 1e4
    high = [LIMIT + 0.05] * n
    low = [LIMIT - 0.05] * n
    # bar WARMUP is the signal bar; bar WARMUP+1 is where the fill is decided
    if side == 1:
        low[WARMUP + 1] = LIMIT - pen
        high[WARMUP + 1] = LIMIT + 5.0
    else:
        high[WARMUP + 1] = LIMIT + pen
        low[WARMUP + 1] = LIMIT - 5.0
    price = pd.DataFrame({"open": [LIMIT] * n, "high": high, "low": low,
                          "close": [LIMIT] * n}, index=idx)
    sl = LIMIT * (0.90 if side == 1 else 1.10)
    tp = LIMIT * (1.50 if side == 1 else 0.50)
    signal = [0] * n
    signal[WARMUP] = side          # acted on the NEXT bar
    sig = pd.DataFrame({"signal": signal, "sl": [sl] * n, "tp": [tp] * n},
                       index=idx)
    return price, sig


def _filled(pen_bp: float, min_bp: float, side: int = 1) -> bool:
    price, sig = _bars(pen_bp, side)
    cfg = EnhancedBTConfig(starting_equity=1000.0, entry_style="maker_close",
                           maker_fill_min_bp=min_bp, slip_bps=0.0,
                           entry_bar_exit_check=False)
    eq, trades = run_backtest_enhanced(price, sig, cfg)
    # A fill shows up as equity moving off the start (the maker fee is debited).
    return bool(trades) or float(eq.iloc[-1]) != cfg.starting_equity


def test_zero_reproduces_old_behaviour():
    assert _filled(pen_bp=1.0, min_bp=0.0), \
        "at 0.0 bp any penetration must fill — this is the historical engine"
    print("PASS 0.0 bp        1 bp penetration fills (historical behaviour)")


def test_threshold_above_penetration_blocks():
    assert not _filled(pen_bp=1.0, min_bp=5.0), \
        "a 1 bp penetration must NOT fill a 5 bp gate"
    print("PASS gate 5 bp     1 bp penetration correctly does NOT fill")


def test_threshold_below_penetration_fills():
    assert _filled(pen_bp=10.0, min_bp=5.0), \
        "a 10 bp penetration must fill a 5 bp gate"
    print("PASS gate 5 bp     10 bp penetration fills")


def test_short_side_is_mirrored():
    assert _filled(pen_bp=10.0, min_bp=5.0, side=-1)
    assert not _filled(pen_bp=1.0, min_bp=5.0, side=-1)
    print("PASS short side    gates on the high, mirrored")


def test_monotone_in_threshold():
    """Raising the gate can only REMOVE fills. That is what makes this a safe
    fragility test: it can lower a measured edge but never inflate one."""
    pen = 4.0
    seq = [_filled(pen_bp=pen, min_bp=m) for m in (0.0, 1.0, 2.0, 5.0, 10.0)]
    assert seq == sorted(seq, reverse=True), f"non-monotone fills: {seq}"
    assert seq[0] and not seq[-1]
    print(f"PASS monotone      fills across gates 0/1/2/5/10 bp: {seq}")


def test_default_preserves_every_prior_result():
    assert EnhancedBTConfig().maker_fill_min_bp == 0.0, \
        "a non-zero default would silently re-price every number in FINDINGS"
    print("PASS default 0.0   prior FINDINGS numbers preserved")


if __name__ == "__main__":
    test_zero_reproduces_old_behaviour()
    test_threshold_above_penetration_blocks()
    test_threshold_below_penetration_fills()
    test_short_side_is_mirrored()
    test_monotone_in_threshold()
    test_default_preserves_every_prior_result()
    print("\nAll maker-fill-depth mechanics tests passed.")
