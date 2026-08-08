"""Book replay + volume-through, on SYNTHETIC archive files.

Same rule as test_maker_fill_depth.py: generated data proves the CODE does
what it says, never that a model works. Here that matters more than usual —
book reconstruction is snapshot-plus-delta replay, where an off-by-one in
delta handling silently produces a plausible-but-wrong book, and a wrong book
would quietly corrupt any fill study built on it. So the deltas are authored
with a known answer.

Covers:
  1. snapshot then deltas -> correct book at each requested time
  2. size "0" DELETES a level (the classic replay bug)
  3. a later snapshot RESETS the book rather than merging into it
  4. requested timestamps between events get the last known state
  5. a truncated final line is dropped, not fatal (rsync/partial download)
  6. volume_through counts only volume at or through the limit, per side

Run:  ./.venv/bin/python test_bybit_archive.py
"""
import io
import json
import zipfile

import pandas as pd

from core.bybit_archive import iter_book_events, replay_book, volume_through

T0 = 1786000000000      # ms


def _zip(events: list[dict], truncate_last: bool = False) -> bytes:
    lines = [json.dumps(e) for e in events]
    body = "\n".join(lines)
    if truncate_last:
        body = body[:-25]        # chop the tail mid-object
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("2026-08-01_TESTUSDT_ob200.data", body)
    return buf.getvalue()


def _ev(ts, typ, bids, asks):
    return {"topic": "orderbook.200.TESTUSDT", "type": typ, "ts": ts,
            "data": {"s": "TESTUSDT",
                     "b": [[str(p), str(s)] for p, s in bids],
                     "a": [[str(p), str(s)] for p, s in asks]}}


def test_snapshot_then_delta():
    raw = _zip([
        _ev(T0,      "snapshot", [(100.0, 5.0), (99.0, 3.0)], [(101.0, 4.0)]),
        _ev(T0 + 1000, "delta",  [(100.0, 9.0)],              []),
    ])
    at = [pd.Timestamp(T0, unit="ms", tz="UTC"),
          pd.Timestamp(T0 + 1000, unit="ms", tz="UTC")]
    df = replay_book(raw, at, levels=5)
    assert len(df) == 2, df
    assert df.iloc[0].bid == 100.0 and df.iloc[0].bid_sz == 5.0
    assert df.iloc[1].bid_sz == 9.0, "delta must UPDATE the level"
    assert df.iloc[0].ask == 101.0
    print("PASS snapshot + delta   level updated, best bid/ask correct")


def test_zero_size_deletes():
    raw = _zip([
        _ev(T0,      "snapshot", [(100.0, 5.0), (99.0, 3.0)], [(101.0, 4.0)]),
        _ev(T0 + 1000, "delta",  [(100.0, 0.0)],              []),
    ])
    df = replay_book(raw, [pd.Timestamp(T0 + 1000, unit="ms", tz="UTC")], levels=5)
    assert df.iloc[0].bid == 99.0, \
        f"size 0 must DELETE the level; best bid is {df.iloc[0].bid}, expected 99.0"
    print("PASS zero size          deletes the level (not a 0-size ghost)")


def test_later_snapshot_resets():
    raw = _zip([
        _ev(T0,      "snapshot", [(100.0, 5.0), (99.0, 3.0)], [(101.0, 4.0)]),
        _ev(T0 + 1000, "snapshot", [(50.0, 1.0)],             [(51.0, 1.0)]),
    ])
    df = replay_book(raw, [pd.Timestamp(T0 + 1000, unit="ms", tz="UTC")], levels=5)
    assert df.iloc[0].bid == 50.0 and df.iloc[0].n_bid_levels == 1, \
        "a snapshot must RESET the book, not merge into the previous one"
    print("PASS re-snapshot        resets the book instead of merging")


def test_timestamp_between_events_uses_last_state():
    raw = _zip([
        _ev(T0,        "snapshot", [(100.0, 5.0)], [(101.0, 4.0)]),
        _ev(T0 + 5000, "delta",    [(100.0, 8.0)], []),
    ])
    mid = pd.Timestamp(T0 + 2500, unit="ms", tz="UTC")
    df = replay_book(raw, [mid], levels=5)
    assert df.iloc[0].bid_sz == 8.0, \
        "a request between events resolves at the next event that passes it"
    print("PASS between events     resolves against the crossing event")


def test_truncated_tail_is_dropped():
    raw = _zip([
        _ev(T0,        "snapshot", [(100.0, 5.0)], [(101.0, 4.0)]),
        _ev(T0 + 1000, "delta",    [(100.0, 7.0)], []),
    ], truncate_last=True)
    evs = list(iter_book_events(raw))
    assert len(evs) == 1, f"truncated final line must be dropped, got {len(evs)}"
    print("PASS truncated tail     dropped, not fatal")


def test_volume_through():
    tr = pd.DataFrame({
        "ts": pd.to_datetime([T0, T0 + 100, T0 + 200, T0 + 300], unit="ms", utc=True),
        "price": [99.0, 100.0, 101.0, 98.0],
        "size":  [1.0, 2.0, 4.0, 8.0],
        "side":  ["Sell", "Sell", "Buy", "Sell"],
    })
    s, e = tr.ts.min(), tr.ts.max() + pd.Timedelta(milliseconds=1)
    # a resting BUY at 100 fills only from volume at or BELOW 100
    assert volume_through(tr, s, e, 100.0, 1) == 1.0 + 2.0 + 8.0
    # a resting SELL at 100 fills only from volume at or ABOVE 100
    assert volume_through(tr, s, e, 100.0, -1) == 2.0 + 4.0
    assert volume_through(tr, s, e, 97.0, 1) == 0.0
    print("PASS volume_through     counts only volume at/through the limit, per side")


if __name__ == "__main__":
    test_snapshot_then_delta()
    test_zero_size_deletes()
    test_later_snapshot_resets()
    test_timestamp_between_events_uses_last_state()
    test_truncated_tail_is_dropped()
    test_volume_through()
    print("\nAll bybit-archive tests passed.")
