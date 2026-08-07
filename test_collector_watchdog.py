"""Collector self-watchdog: exit on stale streams, never on a healthy one.

`restart: unless-stopped` only acts on process EXIT, and Docker does not
restart a merely-unhealthy container. So a hung websocket loop would keep the
container "up" while recording nothing — and on the box whose entire premise
is "set up once, never touch again", that is a silent indefinite hole in a
series with no backfill path. The collector therefore watches itself and
exits, converting that into a bounded gap that session_1m records.

Cases:
  1. healthy collector          -> never exits
  2. market sinks stale         -> exits(1) so Docker restarts it
  3. only the session sink      -> no false fire (session_1m writes itself
                                   every minute, so counting it would make
                                   the check vacuous)

Run:  ./.venv/bin/python test_collector_watchdog.py
"""
import asyncio, os, sys, tempfile, time
os.environ.update(DATA_DIR=tempfile.mkdtemp(), STALE_EXIT_SECS="2")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collector as c

exits = []
c._os_exit = lambda code: exits.append(code)

async def scenario(stale):
    sinks = {"session": c.Sink("session_1m", "ts,n_symbols,n_depth_symbols,free_mb"),
             "book": c.Sink("book_1s", "ts,sym,bid,bid_sz,ask,ask_sz")}
    if stale:
        sinks["book"].last_write = time.time() - 60      # market sink gone quiet
    task = asyncio.ensure_future(c.session_logger(sinks["session"], sinks))
    await asyncio.sleep(0.3)
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

asyncio.run(scenario(stale=False))
print(f"  healthy collector      -> exits={exits}  (expect [])")
assert exits == [], "watchdog fired on a healthy collector"

asyncio.run(scenario(stale=True))
print(f"  market sinks stale 60s -> exits={exits}  (expect [1])")
assert exits == [1], "watchdog did NOT fire on a stale collector"

# session_1m alone must not keep it alive
exits.clear()
async def only_session():
    sinks = {"session": c.Sink("session_1m", "ts,a,b,c")}
    t = asyncio.ensure_future(c.session_logger(sinks["session"], sinks))
    await asyncio.sleep(0.3); t.cancel()
    try: await t
    except asyncio.CancelledError: pass
asyncio.run(only_session())
print(f"  session sink only      -> exits={exits}  (no market sinks = no false fire)")
print("\nAll collector-watchdog tests passed.")
