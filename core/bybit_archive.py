"""Bybit's free historical archives — download, reconstruct, extract.

Two separate archives, verified 2026-08-09:

  TRADES      https://public.bybit.com/trading/{SYM}/{SYM}{YYYY-MM-DD}.csv.gz
              tick-by-tick executions, sub-second timestamps, side/size/price
              2020-03-25 -> present (~6.4 years).  BTCUSDT ~46 MB/day gz.

  ORDER BOOK  https://quote-saver.bycsi.com/orderbook/linear/{SYM}/
              {YYYY-MM-DD}_{SYM}_ob200.data.zip
              raw WebSocket JSONL: one snapshot then deltas, 200 levels,
              200 ms cadence.  ~2025-09 -> present (~11 months).
              MAJORS8 ~322 MB/day zipped => ~118 GB/yr for all eight.

  LIQUIDATIONS  not published anywhere free. Our own collector is the only
              source, which is why it must keep running.

THE DESIGN CONSTRAINT IS DISK, NOT BANDWIDTH. 118 GB/yr of book data does not
fit on the collector box (41 GB free) or comfortably on a laptop, and we do
not need it: a fill study needs the book around DECISION POINTS, not every
200 ms of every day. So the pattern here is always
    download one day -> extract the windows we asked for -> DISCARD the raw
which turns ~40 MB/day into ~kilobytes and makes the whole exercise bounded.

Nothing here touches the live path. It is research input only.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

TRADES_URL = "https://public.bybit.com/trading/{sym}/{sym}{day}.csv.gz"
BOOK_URL = ("https://quote-saver.bycsi.com/orderbook/linear/{sym}/"
            "{day}_{sym}_ob200.data.zip")
BOOK_FIRST_DAY = "2025-09-01"     # bisected; 2025-08 returns 404
TRADES_FIRST_DAY = "2020-03-25"
UA = {"User-Agent": "Mozilla/5.0"}
MIN_FREE_MB = int(os.getenv("ARCHIVE_MIN_FREE_MB", "5000"))


def _free_mb(path: Path) -> float:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 1024 / 1024
    except OSError:
        return float("inf")


def _fetch(url: str, timeout: int = 600) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def available(kind: str, sym: str, day: str) -> int:
    """Content-Length if the file exists, else 0. Cheap HEAD, no download."""
    url = (BOOK_URL if kind == "book" else TRADES_URL).format(sym=sym, day=day)
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


# ------------------------------------------------------------------ trades
def load_trades(sym: str, day: str, cache: Path | None = None) -> pd.DataFrame:
    """One day of tick trades. Columns: ts (UTC), side, size, price."""
    blob = None
    cf = (cache / f"{sym}_{day}_trades.parquet") if cache else None
    if cf is not None and cf.exists():
        return pd.read_parquet(cf)
    blob = _fetch(TRADES_URL.format(sym=sym, day=day))
    df = pd.read_csv(io.BytesIO(gzip.decompress(blob)),
                     usecols=["timestamp", "side", "size", "price"])
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.drop(columns=["timestamp"]).sort_values("ts").reset_index(drop=True)
    if cf is not None:
        cf.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cf)
    return df


def volume_through(trades: pd.DataFrame, start, end, limit_px: float,
                   side: int) -> float:
    """Volume that traded AT OR THROUGH `limit_px` in [start, end).

    This is the honest fill question for a resting order: a passive BUY at
    `limit_px` can only be filled by volume trading at or below it. Compare
    against our own order size to decide whether the fill was ever realistic —
    which is what the engine's `maker_fill_min_bp` proxy was guessing at.
    """
    w = trades[(trades.ts >= start) & (trades.ts < end)]
    if w.empty:
        return 0.0
    m = (w.price <= limit_px) if side == 1 else (w.price >= limit_px)
    return float(w.loc[m, "size"].sum())


# -------------------------------------------------------------- order book
def iter_book_events(raw: bytes):
    """Stream (ts_ms, type, bids, asks) from a downloaded ob200 zip.

    The archive is the raw WebSocket feed: one `snapshot` followed by
    `delta`s, so the book must be REPLAYED, not read. Streamed line by line
    because a single day decompresses to >100 MB.
    """
    z = zipfile.ZipFile(io.BytesIO(raw))
    with z.open(z.namelist()[0]) as fh:
        for line in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue          # truncated tail; drop it rather than guess
            d = o.get("data") or {}
            yield int(o.get("ts") or 0), o.get("type"), d.get("b") or [], d.get("a") or []


def replay_book(raw: bytes, at: list[pd.Timestamp], levels: int = 10
                ) -> pd.DataFrame:
    """Reconstruct the book and snapshot it at each requested timestamp.

    `at` must be sorted. Returns one row per requested time with the top
    `levels` aggregated, plus best bid/ask and cumulative notional — the
    compact form worth keeping after the raw day is discarded.
    """
    want = sorted(pd.Timestamp(t).tz_convert("UTC") for t in at)
    targets = [int(t.timestamp() * 1000) for t in want]
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    out, k = [], 0

    def snap(ts_ms: int) -> dict:
        b = sorted(((p, s) for p, s in bids.items() if s > 0), reverse=True)[:levels]
        a = sorted((p, s) for p, s in asks.items() if s > 0)[:levels]
        row = {"ts": pd.to_datetime(ts_ms, unit="ms", utc=True),
               "bid": b[0][0] if b else float("nan"),
               "bid_sz": b[0][1] if b else float("nan"),
               "ask": a[0][0] if a else float("nan"),
               "ask_sz": a[0][1] if a else float("nan"),
               "bid_notional": sum(p * s for p, s in b),
               "ask_notional": sum(p * s for p, s in a),
               "n_bid_levels": len(bids), "n_ask_levels": len(asks)}
        return row

    for ts_ms, typ, b, a in iter_book_events(raw):
        if typ == "snapshot":
            bids, asks = {}, {}
        for px, sz in b:
            px, sz = float(px), float(sz)
            if sz == 0:
                bids.pop(px, None)
            else:
                bids[px] = sz
        for px, sz in a:
            px, sz = float(px), float(sz)
            if sz == 0:
                asks.pop(px, None)
            else:
                asks[px] = sz
        # emit every requested timestamp this event has now passed
        while k < len(targets) and ts_ms >= targets[k]:
            out.append(snap(targets[k]))
            k += 1
        if k >= len(targets):
            break
    return pd.DataFrame(out)


def book_at(sym: str, day: str, at: list[pd.Timestamp], levels: int = 10,
            cache: Path | None = None) -> pd.DataFrame:
    """Download one day, extract the book at `at`, DISCARD the raw.

    The raw day is never written to disk — that is the whole point. Only the
    extracted rows are cached, so a study over hundreds of decision points
    costs megabytes instead of the ~118 GB/yr the full archive would.
    """
    cf = (cache / f"{sym}_{day}_book.parquet") if cache else None
    if cf is not None and cf.exists():
        return pd.read_parquet(cf)
    if cache is not None and _free_mb(cache) < MIN_FREE_MB:
        raise RuntimeError(f"only {_free_mb(cache):.0f} MB free at {cache}; "
                           f"refusing to download (need {MIN_FREE_MB})")
    raw = _fetch(BOOK_URL.format(sym=sym, day=day))
    df = replay_book(raw, at, levels=levels)
    del raw                       # explicit: the ~40-120 MB day is dropped here
    if cf is not None and not df.empty:
        cf.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cf)
    return df
