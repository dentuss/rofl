"""Tick collector — public Bybit websocket streams to daily CSV files.

Feeds the moonshot program's tier-2 studies (real liquidation-cascade fades,
funding-settlement microstructure, book-imbalance) with the data OHLC bars
cannot provide. READ-ONLY public streams; no keys, no account interaction.

Per symbol it records, under DATA_DIR/YYYY-MM-DD/:
  trades_1s.csv   per-second aggregates: n, vol, vwap, buy_vol, sell_vol
                  (raw prints would be ~100+ MB/day for BTC alone; 1-second
                  bars keep the cascade shape at ~1% of the size)
  liq.csv         EVERY liquidation print, raw (rare + precious)
  book_1s.csv     top-of-book snapshot 1/second: bid, ask, sizes
  ticker_1m.csv   mark/index price, funding rate, open interest 1/minute

Yesterday's files are gzipped at UTC midnight. Disk: ~5-10 MB/day gzipped
for 8 symbols. Reconnects are handled by ccxt.pro internally; any watch
error backs off 5s and resumes.

Run:  python3 collector.py           (or the collector compose service)
Env:  SYMBOLS="BTC/USDT:USDT,..."    DATA_DIR=/app/data
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    # aiodns (ccxt's async resolver) needs the selector loop on Windows;
    # the Linux/Docker deployment is unaffected.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("collector")

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
_DEFAULT_SYMBOLS = ("BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,"
                    "DOGE/USDT:USDT,ADA/USDT:USDT,LINK/USDT:USDT,AVAX/USDT:USDT")
# `or` not a getenv default: compose passes SYMBOLS="" when unset, and an
# empty string would otherwise parse to [""] and collect nothing.
SYMBOLS = [s.strip() for s in (os.getenv("SYMBOLS") or _DEFAULT_SYMBOLS).split(",")
           if s.strip()]


# Depth is an EXECUTION question, so it is collected only for the names we
# actually trade. Subscribing 23 symbols to orderbook.50 would mean ~1150
# msg/s of delta parsing on a 1-OCPU box and risks starving the streams that
# already work — damaging an irreplaceable series to add a new one. MAJORS8
# keeps it to 8. Set DEPTH_SYMBOLS="" to disable entirely.
_DEFAULT_DEPTH = ("BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,"
                  "DOGE/USDT:USDT,ADA/USDT:USDT,LINK/USDT:USDT,AVAX/USDT:USDT")
_depth_env = os.getenv("DEPTH_SYMBOLS")
DEPTH_SYMBOLS = [x.strip() for x in
                 ((_DEFAULT_DEPTH if _depth_env is None else _depth_env).split(","))
                 if x.strip() and x.strip() in SYMBOLS]
# Cumulative notional available within N bps of mid, per side. Chosen to
# bracket real order sizes: at $1,800 a leg trades $50-80, and 2026-08-07
# measurement found LINK/AVAX top-of-book below that ~9% of the time.
DEPTH_BPS = (1, 5, 10, 25)
# Stop writing before the disk is full: a full disk raises OSError inside
# Sink.write, which the collect_* handlers swallow as "retrying in 5s" — the
# collector would spin forever, logging warnings, silently recording nothing.
MIN_FREE_MB = int(os.getenv("MIN_FREE_MB", "2048"))


def free_mb() -> float:
    try:
        st = os.statvfs(DATA_DIR)
        return st.f_bavail * st.f_frsize / 1024 / 1024
    except OSError:
        return float("inf")


def day_dir(ts: float | None = None) -> Path:
    d = DATA_DIR / time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))
    d.mkdir(parents=True, exist_ok=True)
    return d


class Sink:
    """Append-only daily CSV with a header, flushed on every write batch."""

    def __init__(self, name: str, header: str):
        self.name, self.header = name, header
        self._fh, self._day = None, None

    def write(self, line: str) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self._day != today:
            if self._fh:
                self._fh.close()
            path = day_dir() / f"{self.name}.csv"
            fresh = not path.exists()
            self._fh = open(path, "a", encoding="utf-8")
            if fresh:
                self._fh.write(self.header + "\n")
            self._day = today
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except OSError as e:
            # Distinguish "disk problem" from "stream problem". Without this
            # the caller logs a websocket retry and loops forever writing
            # nothing.
            log.error(f"SINK WRITE FAILED ({self.name}): {e} — "
                      f"{free_mb():.0f} MB free")
            raise


async def collect_trades(ex, sym: str, sink: Sink):
    """1-second aggregates of the public trade stream."""
    base = sym.split("/")[0]
    bucket_ts, n, vol, notional, buy_v, sell_v = None, 0, 0.0, 0.0, 0.0, 0.0
    while True:
        try:
            trades = await ex.watch_trades(sym)
            for t in trades:
                sec = int(t["timestamp"] // 1000)
                if bucket_ts is None:
                    bucket_ts = sec
                if sec != bucket_ts:
                    if n:
                        vwap = notional / vol if vol else 0.0
                        sink.write(f"{bucket_ts},{base},{n},{vol:.6f},"
                                   f"{vwap:.6f},{buy_v:.6f},{sell_v:.6f}")
                    bucket_ts, n, vol, notional, buy_v, sell_v = \
                        sec, 0, 0.0, 0.0, 0.0, 0.0
                amt = float(t["amount"] or 0)
                px = float(t["price"] or 0)
                n += 1
                vol += amt
                notional += amt * px
                if (t.get("side") or "") == "buy":
                    buy_v += amt
                else:
                    sell_v += amt
        except Exception as e:
            log.warning(f"trades {sym}: {e}; retrying in 5s")
            await asyncio.sleep(5)


async def collect_liq(ex, sym: str, sink: Sink):
    """Raw liquidation prints — the tier-2 crown jewel."""
    base = sym.split("/")[0]
    while True:
        try:
            liqs = await ex.watch_liquidations(sym)
            for q in liqs:
                sink.write(f"{int((q.get('timestamp') or time.time()*1000))},"
                           f"{base},{q.get('side','')},"
                           f"{float(q.get('price') or 0):.6f},"
                           f"{float(q.get('amount') or 0):.6f}")
        except Exception as e:
            log.warning(f"liq {sym}: {e}; retrying in 5s")
            await asyncio.sleep(5)


async def collect_book(ex, sym: str, sink: Sink):
    """Top-of-book snapshot at most once per second."""
    base = sym.split("/")[0]
    last = 0
    while True:
        try:
            ob = await ex.watch_order_book(sym, limit=1)
            now = int(time.time())
            if now == last or not ob.get("bids") or not ob.get("asks"):
                continue
            last = now
            b, a = ob["bids"][0], ob["asks"][0]
            sink.write(f"{now},{base},{b[0]:.6f},{b[1]:.6f},"
                       f"{a[0]:.6f},{a[1]:.6f}")
        except Exception as e:
            log.warning(f"book {sym}: {e}; retrying in 5s")
            await asyncio.sleep(5)


async def collect_depth(ex, sym: str, sink: Sink):
    """Cumulative notional within N bps of mid, per side, at most 1/second.

    Stores the ANSWER (how much size sits within a price band) rather than raw
    ladders: a 50-level ladder for 8 symbols at 1/s is ~100 GB/yr, the buckets
    are ~4 GB/yr, and the buckets are what an execution-cost study actually
    consumes. The trade-off is deliberate and worth stating: if a future study
    needs the raw shape of the book, these buckets cannot reconstruct it.
    book_1s (top of book, all 23 symbols) is unchanged and continues alongside.
    """
    base = sym.split("/")[0]
    last = 0
    while True:
        try:
            ob = await ex.watch_order_book(sym, limit=50)
            now = int(time.time())
            bids, asks = ob.get("bids") or [], ob.get("asks") or []
            if now == last or not bids or not asks:
                continue
            last = now
            mid = (bids[0][0] + asks[0][0]) / 2
            if mid <= 0:
                continue
            out = []
            for side in (bids, asks):
                sign = -1 if side is bids else 1
                for bps in DEPTH_BPS:
                    lim = mid * (1 + sign * bps / 1e4)
                    tot = sum(px * qty for px, qty in side
                              if (px >= lim if sign < 0 else px <= lim))
                    out.append(f"{tot:.2f}")
            sink.write(f"{now},{base},{mid:.8f}," + ",".join(out))
        except Exception as e:
            log.warning(f"depth {sym}: {e}; retrying in 5s")
            await asyncio.sleep(5)


async def session_logger(sink: Sink):
    """Heartbeat + disk pressure, once a minute.

    Gaps are facts (see research/data_health.py). Without an explicit record,
    a gap is indistinguishable from "the market was quiet" at analysis time,
    and the only honest response to an unrecorded gap is to distrust the
    window around it. One row per minute makes every gap self-evident and
    costs ~50 KB/day.
    """
    while True:
        try:
            mb = free_mb()
            sink.write(f"{int(time.time())},{len(SYMBOLS)},{len(DEPTH_SYMBOLS)},"
                       f"{mb:.0f}")
            if mb < MIN_FREE_MB:
                log.error(f"DISK PRESSURE: {mb:.0f} MB free (< {MIN_FREE_MB}); "
                          f"the collector will start failing writes")
        except Exception as e:
            log.warning(f"session logger: {e}")
        await asyncio.sleep(60)


async def collect_ticker(ex, sym: str, sink: Sink):
    """Mark/index/funding/OI once per minute (from the ticker stream)."""
    base = sym.split("/")[0]
    last_min = 0
    while True:
        try:
            tk = await ex.watch_ticker(sym)
            minute = int(time.time() // 60)
            if minute == last_min:
                continue
            last_min = minute
            info = tk.get("info") or {}
            sink.write(f"{minute*60},{base},{tk.get('last') or ''},"
                       f"{info.get('markPrice','')},{info.get('indexPrice','')},"
                       f"{info.get('fundingRate','')},"
                       f"{info.get('openInterest','')}")
        except Exception as e:
            log.warning(f"ticker {sym}: {e}; retrying in 5s")
            await asyncio.sleep(5)


async def gzip_rotator():
    """Gzip any non-today CSVs once an hour."""
    while True:
        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            for d in DATA_DIR.iterdir() if DATA_DIR.exists() else []:
                if not d.is_dir() or d.name == today:
                    continue
                for f in d.glob("*.csv"):
                    gz = Path(f"{f}.gz")
                    with open(f, "rb") as src, gzip.open(gz, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    # VERIFY before unlinking the only other copy. A short
                    # write (disk pressure) or an interrupted flush can leave a
                    # truncated .gz; deleting the .csv on faith would destroy
                    # a day of unbackfillable history.
                    try:
                        with gzip.open(gz, "rb") as chk:
                            while chk.read(1 << 20):
                                pass
                    except Exception as e:
                        log.error(f"gzip verify FAILED for {gz}: {e} — "
                                  f"keeping {f}")
                        gz.unlink(missing_ok=True)
                        continue
                    f.unlink()
                    log.info(f"gzipped + verified {f}")
        except Exception as e:
            log.warning(f"rotator: {e}")
        await asyncio.sleep(3600)


async def main():
    import aiohttp
    import ccxt.pro as ccxtpro
    ex = ccxtpro.bybit({"options": {"defaultType": "linear"}})
    # ThreadedResolver instead of aiodns: c-ares fails to locate DNS servers
    # on some boxes (observed on the Windows dev machine); lookups are rare
    # for a long-lived collector, so threaded resolution costs nothing.
    ex.session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver(),
                                       ttl_dns_cache=300))
    sinks = {
        "trades": Sink("trades_1s", "ts,sym,n,vol,vwap,buy_vol,sell_vol"),
        "liq": Sink("liq", "ts_ms,sym,side,price,amount"),
        "book": Sink("book_1s", "ts,sym,bid,bid_sz,ask,ask_sz"),
        "ticker": Sink("ticker_1m", "ts,sym,last,mark,index,funding,oi"),
        "depth": Sink("depth_1s", "ts,sym,mid," + ",".join(
            [f"bid_{b}bp" for b in DEPTH_BPS] + [f"ask_{b}bp" for b in DEPTH_BPS])),
        "session": Sink("session_1m", "ts,n_symbols,n_depth_symbols,free_mb"),
    }
    log.info(f"collector up: {len(SYMBOLS)} symbols "
             f"({len(DEPTH_SYMBOLS)} with depth) -> {DATA_DIR.resolve()}; "
             f"{free_mb():.0f} MB free")
    tasks = [gzip_rotator(), session_logger(sinks["session"])]
    for s in SYMBOLS:
        tasks += [collect_trades(ex, s, sinks["trades"]),
                  collect_liq(ex, s, sinks["liq"]),
                  collect_book(ex, s, sinks["book"]),
                  collect_ticker(ex, s, sinks["ticker"])]
        if s in DEPTH_SYMBOLS:
            tasks.append(collect_depth(ex, s, sinks["depth"]))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
