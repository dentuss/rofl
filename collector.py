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
SYMBOLS = [s.strip() for s in os.getenv(
    "SYMBOLS",
    "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT,"
    "DOGE/USDT:USDT,ADA/USDT:USDT,LINK/USDT:USDT,AVAX/USDT:USDT").split(",")]


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
        self._fh.write(line + "\n")
        self._fh.flush()


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
                    with open(f, "rb") as src, \
                            gzip.open(f"{f}.gz", "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    f.unlink()
                    log.info(f"gzipped {f}")
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
    }
    log.info(f"collector up: {len(SYMBOLS)} symbols -> {DATA_DIR.resolve()}")
    tasks = [gzip_rotator()]
    for s in SYMBOLS:
        tasks += [collect_trades(ex, s, sinks["trades"]),
                  collect_liq(ex, s, sinks["liq"]),
                  collect_book(ex, s, sinks["book"]),
                  collect_ticker(ex, s, sinks["ticker"])]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
