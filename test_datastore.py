"""Tests for core.datastore against a synthetic copy of the real box layout.

The loader's whole job is to be unsurprising when the data is ugly, because
it always will be: the collector appends to a file while we rsync it, a day
rolls over mid-pull, a leg has never traded, a box has been up for an hour.
Every one of those must load, not raise.

Cases:
  1. plain + gzipped day files concatenate, with a UTC index
  2. a truncated final line (mid-append rsync) is dropped, not fatal
  3. symbol + date filtering
  4. missing kinds / missing days / empty dirs return empty frames
  5. bot_state.json -> load_states, incl. a leg with no state yet
  6. events -> live_blotter in core.backtest.Trade shape, entry joined to exit
  7. a leg that opened but has not closed produces no blotter row
  8. an exit with no preceding entry (restart mid-position) yields NaT, not a crash

Run:  ./.venv/bin/python test_datastore.py
"""
import gzip
import json
import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="rofl_ds_test_"))
os.environ["ROFL_DATA"] = str(_TMP)

import pandas as pd  # noqa: E402

from core.backtest import Trade  # noqa: E402
from core import datastore as ds  # noqa: E402


def _build():
    t = _TMP / "ticks"
    # --- day 1: gzipped (the collector gzips yesterday at UTC midnight) ---
    d1 = t / "2026-08-04"; d1.mkdir(parents=True)
    with gzip.open(d1 / "trades_1s.csv.gz", "wt") as f:
        f.write("ts,sym,n,vol,vwap,buy_vol,sell_vol\n")
        f.write("1785974400,BTC,10,1.5,62000.0,1.0,0.5\n")
        f.write("1785974401,ETH,4,20.0,1840.0,12.0,8.0\n")
    (d1 / "ticker_1m.csv").write_text(
        "ts,sym,last,mark,index,funding,oi\n"
        "1785974400,BTC,62000.0,62001.0,62002.0,0.0001,60000.0\n")

    # --- day 2: plain, and trades_1s has a TRUNCATED final line ---
    d2 = t / "2026-08-05"; d2.mkdir(parents=True)
    (d2 / "trades_1s.csv").write_text(
        "ts,sym,n,vol,vwap,buy_vol,sell_vol\n"
        "1786060800,BTC,7,0.9,62500.0,0.4,0.5\n"
        "1786060801,BTC,3,0.2,625")            # rsync caught a partial append
    (d2 / "liq.csv").write_text(
        "ts_ms,sym,side,price,amount\n"
        "1786060800000,ETH,Sell,1830.5,12.0\n")
    # note: no book_1s on either day -> must return empty, not raise

    # --- live legs ---
    lv = _TMP / "live"
    for leg in ("btc-t", "eth-t", "sol-t", "xrp-t"):
        (lv / leg / "state").mkdir(parents=True)
        (lv / leg / "logs").mkdir(parents=True)

    (lv / "btc-t" / "state" / "bot_state.json").write_text(json.dumps({
        "equity": 118.40, "realised_pnl": 5.90, "realised_trades": 2,
        "realised_wins": 1,
        "position": {"side": 1, "qty": 0.001, "entry_px": 62000.0,
                     "sl": 60800.0, "tp": 65600.0},
    }))
    (lv / "btc-t" / "state" / "heartbeat").write_text("")
    # eth-t: state file exists but holds no open position
    (lv / "eth-t" / "state" / "bot_state.json").write_text(json.dumps({
        "equity": 110.0, "realised_pnl": -2.5, "realised_trades": 1,
        "realised_wins": 0, "position": None}))
    # sol-t: brand new leg, nothing written yet -> must not crash
    # xrp-t: opened a position, never closed -> no blotter row

    def ev(leg, rows):
        (lv / leg / "logs" / "events-2026-08-05.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n")

    ev("btc-t", [
        {"ts": "2026-08-05T00:00:00+00:00", "event": "bot_start", "mode": "live"},
        {"ts": "2026-08-05T04:00:00+00:00", "event": "entry", "side": 1,
         "qty": 0.001, "entry_px": 61000.0, "sl": 59800.0, "tp": 64600.0,
         "notional": 61.0},
        {"ts": "2026-08-05T12:00:00+00:00", "event": "exit", "side": 1,
         "qty": 0.001, "entry_px": 61000.0, "exit_px": 62000.0, "pnl": 0.94,
         "fees": 0.06, "reason": "tp", "bars_held": 2, "notional": 61.0,
         "symbol": "BTC/USDT"},
    ])
    # eth-t: exit with NO preceding entry (bot restarted holding a position)
    ev("eth-t", [
        {"ts": "2026-08-05T08:00:00+00:00", "event": "exit", "side": -1,
         "qty": 0.05, "entry_px": 1850.0, "exit_px": 1880.0, "pnl": -1.56,
         "fees": 0.04, "reason": "sl-external", "bars_held": 3,
         "notional": 92.5, "symbol": "ETH/USDT"},
        {"ts": "2026-08-05T09:00:00+00:00", "event": "error",
         "message": "close order failed: timeout"},
    ])
    # xrp-t: entry only, still open
    ev("xrp-t", [
        {"ts": "2026-08-05T04:00:00+00:00", "event": "entry", "side": 1,
         "qty": 100.0, "entry_px": 0.52, "sl": 0.50, "tp": 0.58,
         "notional": 52.0},
    ])


def test_tick_days_and_concat():
    assert ds.tick_days() == ["2026-08-04", "2026-08-05"], ds.tick_days()
    df = ds.load_ticks("trades_1s")
    # 2 rows day1 (gz) + 1 valid row day2; the truncated line is dropped
    assert len(df) == 3, f"expected 3 rows, got {len(df)}\n{df}"
    assert isinstance(df.index, pd.DatetimeIndex) and str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing
    print(f"PASS gz+plain concat, truncated line dropped ({len(df)} rows)")


def test_gz_preferred_over_stale_plain_csv():
    """The collector gzips a day at UTC midnight and removes the .csv, but
    rsync (deliberately no --delete, so a wiped box cannot wipe our copy)
    leaves the stale .csv behind. Loading BOTH gave ~45% duplicate rows across
    the whole tick tree on 2026-08-07. The .gz must win."""
    d = _TMP / "ticks" / "2026-08-04"
    plain = d / "trades_1s.csv"
    # same day already has trades_1s.csv.gz with 2 rows; add a stale partial .csv
    plain.write_text("ts,sym,n,vol,vwap,buy_vol,sell_vol\n"
                     "1785974400,BTC,10,1.5,62000.0,1.0,0.5\n")
    try:
        df = ds.load_ticks("trades_1s", start="2026-08-04", end="2026-08-04")
        assert len(df) == 2, (f"expected the 2 gz rows, got {len(df)} — the stale "
                              f".csv was loaded alongside the .csv.gz")
        assert not df.duplicated(subset=["ts", "sym"]).any()
        print("PASS .csv.gz wins over a stale sibling .csv (no double-load)")
    finally:
        plain.unlink()


def test_filters():
    assert len(ds.load_ticks("trades_1s", symbols=["BTC"])) == 2
    assert len(ds.load_ticks("trades_1s", start="2026-08-05")) == 1
    assert len(ds.load_ticks("trades_1s", end="2026-08-04")) == 2
    print("PASS symbol + date filtering")


def test_missing_is_empty_not_fatal():
    assert ds.load_ticks("book_1s").empty          # kind never written
    assert ds.load_ticks("liq", start="2030-01-01").empty
    assert list(ds.load_ticks("book_1s").columns)[:2] == ["ts", "sym"]
    try:
        ds.load_ticks("nope")
        raise AssertionError("unknown kind must raise")
    except ValueError:
        pass
    print("PASS missing kinds/days -> empty frames; bad kind raises")


def test_liq_uses_ms():
    liq = ds.load_ticks("liq")
    assert len(liq) == 1
    assert liq.index[0].year == 2026, liq.index[0]
    print(f"PASS liq ts_ms parsed as ms -> {liq.index[0]}")


def test_states():
    st = ds.load_states().set_index("leg")
    assert set(st.index) == {"btc-t", "eth-t", "sol-t", "xrp-t"}
    assert abs(st.loc["btc-t", "equity"] - 118.40) < 1e-9
    assert st.loc["btc-t", "pos_side"] == 1
    assert pd.isna(st.loc["eth-t", "pos_side"]) or st.loc["eth-t", "pos_side"] is None
    assert "error" in st.columns and isinstance(st.loc["sol-t", "error"], str)
    # pandas coerces the mixed int/None column to float, so a missing
    # heartbeat surfaces as NaN rather than None — assert on that.
    assert pd.notna(st.loc["btc-t", "heartbeat_age_s"])
    assert pd.isna(st.loc["sol-t", "heartbeat_age_s"])
    print("PASS states incl. empty leg + heartbeat age")


def test_blotter_shape_matches_engine_trade():
    bl = ds.live_blotter()
    assert len(bl) == 2, f"expected 2 closed trades, got {len(bl)}\n{bl}"
    engine_fields = set(Trade.__dataclass_fields__)
    assert engine_fields.issubset(set(bl.columns)), \
        f"blotter missing engine Trade fields: {engine_fields - set(bl.columns)}"
    b = bl[bl.leg == "btc-t"].iloc[0]
    assert b["entry_time"] == pd.Timestamp("2026-08-05T04:00:00Z"), b["entry_time"]
    assert b["sl"] == 59800.0 and b["tp"] == 64600.0     # joined from the entry
    assert b["reason"] == "tp"
    print(f"PASS blotter has all {len(engine_fields)} engine Trade fields, "
          f"entry joined to exit")


def test_blotter_edge_cases():
    bl = ds.live_blotter()
    assert "xrp-t" not in set(bl.leg), "an unclosed position must not appear"
    e = bl[bl.leg == "eth-t"].iloc[0]
    assert pd.isna(e["entry_time"]), "exit without entry must be NaT, not a guess"
    assert pd.isna(e["sl"])
    assert e["pnl"] == -1.56
    print("PASS open position excluded; orphan exit -> NaT rather than a guess")


def test_events_and_summary():
    assert len(ds.load_events(event="error")) == 1
    assert len(ds.load_events(leg="btc-t")) == 3
    s = ds.summary()
    assert s["tick_days"] == 2 and s["legs"] == 4 and s["live_trades"] == 2
    assert abs(s["live_pnl"] - (0.94 - 1.56)) < 1e-9
    print(f"PASS events filter + summary {s['live_trades']} trades "
          f"pnl {s['live_pnl']:+.2f}")


if __name__ == "__main__":
    _build()
    try:
        test_tick_days_and_concat()
        test_gz_preferred_over_stale_plain_csv()
        test_filters()
        test_missing_is_empty_not_fatal()
        test_liq_uses_ms()
        test_states()
        test_blotter_shape_matches_engine_trade()
        test_blotter_edge_cases()
        test_events_and_summary()
        print("\nAll datastore tests passed.")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
