# Hosting on Oracle Cloud Always-Free (ARM A1) — the runbook

**Verdict: feasible, and the economics demand it.** At L1 unit weights the
book's expected profit is ~$9–17/mo on $1,800. A $30/mo box would eat the
entire edge, so this has to run at **$0** or not at all. Oracle's A1 is ARM
(aarch64), as the old EC2 `t4g` was, so the image, the aarch64 wheels
(pandas/numpy/sklearn/ccxt all ship them) and `deploy/setup.sh` run unchanged.

> **The free allowance is 2 OCPU / 12 GB** (revised 2026-08-03 — earlier
> revisions of this file said 4/24, which is wrong for this tenancy; check
> your own console, Oracle varies this by region and account age). Everything
> below is sized against 2/12 using **measured** figures, not estimates.

## 0. Two caveats that actually matter

1. **Idle-reclamation.** Oracle may reclaim Always-Free compute it judges idle
   (low CPU + RAM + network over ~7 days). Both of our boxes look idle: the
   bots think for seconds once per 4h, and the collector is low-bandwidth.
   The fix is not a keep-busy hack — **upgrade the tenancy to Pay-As-You-Go**
   (Governance → Upgrade to Paid). PAYG exempts you from reclamation and keeps
   Always-Free resources genuinely $0 within the free limits. Do this before
   either box matters. For the trading box it protects money; **for the
   collector it protects the tick record, which cannot be rebuilt** — that is
   arguably the stronger reason.
2. **Capacity ("Out of host capacity").** A1 is popular and often refuses at
   create time. It is transient: try each Availability Domain, retry off-peak,
   or loop the create call. Once created it's yours.

The design degrades safely if a box dies: positions carry **exchange-side
SL/TP** (they fire whether the bot is up or not), closes are **reduce-only**,
and resume **reconciles** against the exchange and **HALTs** on anything it
didn't open. Box loss is a restart event, never an unprotected position.

## 1. Layout — split the allowance into two VMs

| VM | shape | boot vol | runs | holds keys |
|---|---|---|---|---|
| `rofl-collector` | **1 OCPU / 2 GB** | 50 GB | tick collector | **no** |
| `rofl-trading` | **1 OCPU / 10 GB** | 100 GB | live book + tg-control | yes |

Total 2 OCPU / 12 GB / 150 GB — inside the free tier.

**Why split rather than run one box.** The collector's entire value is
*continuity*: tick history cannot be backfilled, so every restart, rebuild or
OOM costs data permanently. During L1 you will be redeploying the trading
stack repeatedly. Isolating the collector means none of that churn — and no
trading-box OOM — can ever touch it. It is the machine you set up once and
never log into again.

If your console enforces a memory floor per OCPU and won't allow 1/2, put
everything on one 2/12 box; the per-container caps in §4 make that safe.

## 2. Measured resource budget

Measured 2026-08-03 on the real image (`smaps_rollup`, three concurrent legs):

| metric | per leg |
|---|---|
| RSS | 322 MB |
| PSS (3 concurrent) | 237 MB |
| **private — the marginal cost** | **198 MB** |
| shared libs, counted once | 124 MB |

**N legs cost `124 MB + N × 198 MB`, not `N × 322`** — containers from one
image share their mapped library pages. Multiplying RSS overstates 16 legs by
~1.9 GB and is the mistake that made 12 GB look impossible.

| | trading box (10 GB) | collector box (2 GB) |
|---|---|---|
| 16 legs | 3.29 GB | — |
| tg-control | ~0.20 GB | — |
| collector (23 symbols) | — | ~0.30 GB |
| Docker + Ubuntu | ~0.65 GB | ~0.65 GB |
| **total** | **~4.14 GB of 10** | **~0.95 GB of 2** |

CPU is not the constraint: legs are idle between 4h boundaries and wake on a
deterministic `crc32(symbol) % FETCH_STAGGER_SECS` offset that spreads them.

## 3. Create the instances

1. Sign up, pick a home region (see capacity note), verify card.
2. **Upgrade to Pay-As-You-Go.** This is the anti-reclamation step.
3. Compute → Create Instance, twice, per the table in §1:
   - Image: **Canonical Ubuntu 24.04** (Minimal is fine)
   - Shape: **VM.Standard.A1.Flex**
   - SSH: paste your public key
4. **Reserved public IP for `rofl-trading` only** (Networking → Reserved
   Public IPs → attach to the VNIC). The Bybit keys are IP-whitelisted so that
   box needs a stable address. The collector uses public streams with no auth
   and needs no reserved IP.
5. Security list — **Ingress: TCP 22 from your IP only.** Nothing else: bots,
   collector and Telegram are all outbound.

## 4. Initialise

Two scripts, one per box. Both are idempotent.

```bash
# on rofl-collector — starts collecting immediately, no keys involved
curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/init-collector.sh | bash

# on rofl-trading — prepares the box and DELIBERATELY STARTS NOTHING
curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/init-trading.sh | bash
```

`init-collector.sh` sets UTC+NTP, adds 2 GB swap, installs Docker via
`setup.sh`, pins `SYMBOLS` to **QUAL23** (the 23-name sleeve universe, ~7.7
GB/yr — wider than MAJORS8 because cross-sectional microstructure can't be
backfilled either), starts the stack and verifies files are landing.

`init-trading.sh` sets UTC+NTP, adds 4 GB swap, installs Docker, creates a
`chmod 600` `.env` template if absent, builds the live image, and **stops** —
printing the ordered prerequisites. Starting real money stays a manual, gated
decision (ROADMAP Phase 6). No script skips a stage.

**Per-container memory caps** are set in the live compose (`mem_limit: 512m`,
~2.5× the measured 198 MB private) so one leaking leg is killed and restarted
by Docker instead of OOMing the box.

## 5. Bringing the trading stack up

Only when ROADMAP L1 is the active stage, and only after L0.5 (capital split)
is fixed:

```bash
cd ~/rofl && git pull
docker compose -f docker-compose.bidir4h-live.yml up -d --build
docker compose -f docker-compose.bidir4h-live.yml logs -f
```

**Do not run the paper twin on this box.** The program is live-first by
decision; paper cannot produce real fills (it simulates them), which is the
one thing L1 exists to measure. It would also collide with live: stagger
offsets are `crc32(symbol)`-derived, so live-BTC and paper-BTC wake on the
*same second* and double every spike. If you run it anyway, set
`FETCH_STAGGER_SECS: "90"` on the paper stack so the offsets interleave.

Startup is the heaviest moment — 17 containers each importing pandas/sklearn
and fitting a GMM. On 1 OCPU that takes a few minutes; it is not a fault.

## 6. Operating

```bash
# collector box
docker compose -f docker-compose.collector.yml logs -f
docker exec rofl-collector ls -la /app/data/$(date -u +%F)
docker exec rofl-collector du -sh /app/data

# trading box
docker compose -f docker-compose.bidir4h-live.yml ps
docker stats --no-stream          # confirm legs sit near 198 MB
free -h                           # swap should stay ~empty
```

**Back up the tick volume** — it lives on one instance and is the only
irreplaceable asset here. Monthly is enough:
```bash
tar czf ticks-$(date -u +%F).tgz -C ~/rofl/data ticks
```

## 7. Pulling data back for analysis

Both boxes **bind-mount** their output to `<repo>/data` (the composes use
`${ROFL_DATA:-./data}`), rather than writing into named Docker volumes. That
one choice is what makes analysis painless: the remote and local trees are
identical, so pulling is a plain incremental `rsync` — no `docker cp`, no
staging copy, no doubling disk on a 50 GB box.

```
data/
  ticks/YYYY-MM-DD/{trades_1s,liq,book_1s,ticker_1m}.csv[.gz]
  live/<sym>-<leg>/state/{bot_state.json,heartbeat}
  live/<sym>-<leg>/logs/{bot.log,events-YYYY-MM-DD.jsonl}
```

From the laptop:
```bash
deploy/pull-data.sh              # both boxes   (also: ticks | live)
./.venv/bin/python research/data_health.py
```

`pull-data.sh` is **read-only on the remote** — it only reads, restarts
nothing, and is safe to run against a live trading box mid-position. Add
`rofl-collector` / `rofl-trading` aliases to `~/.ssh/config`, or set
`COLLECTOR_HOST` / `TRADING_HOST`. Gzipped past days never change, so after
the first pull rsync only moves today's file.

Then in any research script:
```python
from core.datastore import load_ticks, live_blotter, load_states, summary
liq = load_ticks("liq", start="2026-08-05", symbols=["ETH"])
bl  = live_blotter()      # live fills in core.backtest.Trade shape
```

`live_blotter()` is deliberately shaped to match the engine's `Trade`
dataclass field-for-field, so **ROADMAP L2's "reconcile live vs engine" is a
DataFrame join** rather than an afternoon of eyeballing. `research/
data_health.py` reports tick coverage vs expected, missing days, per-symbol
gaps, leg heartbeats and PnL by exit reason — run it after every pull, before
any study that consumes the data.

The loaders tolerate ugly data by design (`test_datastore.py`, 8 cases):
partial final lines from rsyncing a file mid-append, a day that rolled over
during the pull, a leg that never traded, an exit whose entry predates the
log. Missing data yields empty frames, never an exception — and **never a
synthesised row.** A fake tick poisons every study built on it.

## 8. Resuming after downtime

On the first live `up`, each leg reconciles against Bybit automatically:
position still open → resumes managing it (its attached SL/TP were live
throughout, which is why downtime was safe); position gone → books the real
close from closed-PnL history and classifies sl/tp by fill proximity; state
flat but exchange holds something → cancels strays and **HALTs** that leg for
manual review rather than guessing.

Because the deposit changed, prefer a **clean start**: confirm flat on Bybit,
set `LEG4H_LIVE_EQUITY` from real balances, bring the stack up fresh. Do not
copy old live volumes between hosts.
