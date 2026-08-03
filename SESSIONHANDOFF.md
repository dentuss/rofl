# Session handoff — rofl (as of 2026-07-30)

> **Fresh session, read this first, then `CLAUDE.md`, then
> `research/FINDINGS.md` + `research/ROADMAP.md`.** This file is written to
> be self-sufficient: assume no memory, no prior context, a freshly cloned
> repo on a **Linux laptop**. Section 8 is the Linux migration checklist.

## 0. TL;DR (60 seconds)

Systematic crypto perp trading program on Bybit USDT-linear, rebuilt from
zero after the **same-bar re-entry engine artifact** (2026-07-05) was found
to have manufactured the entire original backtest edge (212% CAGR → 0.6%
honest). Everything since is gate-validated on the fixed engine with a full
cost model. The product is a **two-leg trend book (BLEND50_CONF)**; three
diversifying **sleeves** are validated and in forward paper-tracking toward
a stacked book at Sharpe ~1.8–2.0.

- Repo `dentuss/rofl`, branch **`main`**, clean and fully pushed.
- **Capital: $1,800.00 USDT** (was $2,177.56 — resized 2026-07-30).
- **Status: NOT RUNNING.** Live was deployed ~2026-07-08 (stage L1), then
  stopped twice for personal reasons. Nothing is trading right now.
- Host plan: **Oracle Cloud Always-Free A1** (`deploy/ORACLE.md`), replacing
  the EC2 box on cost grounds. Dev machine moving Windows → **Linux**.

## 1. What the bot is — BLEND50_CONF

**MAJORS8** = BTC ETH SOL XRP DOGE ADA LINK AVAX (chosen ex-ante by median
daily dollar volume — *never* by backtest performance; that is a hard law).
Each symbol runs **two entry legs at 50/50 capital**:

| leg | preset | signal |
|---|---|---|
| `-t` | `adaptive_bidir_4h` | `triple_confirm_bidir` — EMA 9/26/50 stack + RSI 55/45 + ADX 22, sl 1.8× / tp 6× ATR |
| `-p` | `pullback_bidir_4h` | `pullback_in_trend` — EMA50 side + RSI recross of 40/60, same stops |

The `-p` leg fires ~once per 6 weeks per name (in-market 4–8%) and has only
**0.17 monthly correlation** to `-t` — that low correlation is the whole
point. Long idle stretches on `-p` containers are correct behavior.

Shared overlays (each individually gate-passed): walk-forward GMM regime
mask (long in BULL/CHOP, short in BEAR/CHOP), F&G 3-day persistence filter,
3-tier drawdown decay, CHOP half-sizing, vol targeting (60% ann),
GMM-confidence sizing, post-SL cooldown. Execution is **maker on both
sides**: post-only limit entries (`ENTRY_LIMIT_ORDERS`) + TP-as-limit
(`TP_LIMIT_ORDERS`), which is the exact cost model the backtests price;
exec-parity verified against the engine.

**Honest expectations** (fixed engine, full costs, 2023-08 → 2026-07;
percentages are deposit-invariant, dollars shown on $1,800):

| sizing | CAGR | Sh(mo) | dMDD | worst mo | median mo | final $ |
|---|---|---|---|---|---|---|
| **unit weights (L1/L2 runs here)** | 10.4% | 1.50 | −4.5% | −1.7% | +0.40% | 2,398 |
| @15% vol dial (L3) | 22.2% | 1.49 | −9.2% | −3.5% | +0.79% | 3,210 |
| @25% vol dial (L3) | 37.9% | 1.48 | −15.1% | −5.8% | +1.24% | 4,555 |

**Anchor expectations on the full-history Sharpe ~1.2, not 1.5.** The
2022-inclusive long-history gate PASSED at blend +1.20 full / **+0.18
pre-2023-08** — the triple leg carries it (+0.57); the pull leg alone was
**−2.57** pre-2023 and therefore has a pre-registered demotion trigger:
*trailing-3-month forward Sharpe < 0 → drop to BLEND75 or triple-only.*

## 2. Sizing at $1,800 (changed 2026-07-30 — read before deploying)

All **16 legs equal at $112.50** (16 × 112.50 = $1,800.00 exactly),
**$900.00 per account**. One env var: `LEG4H_LIVE_EQUITY`.

The previous split gave BTC legs a premium ($300 each) because BTC's 0.001
lot ≈ $64 notional is coarse. At $1,800 that premium would make BTC **33%
of the book** vs the 12.5% the backtest validated — an unvalidated
concentration bet, so it was dropped in favour of equal weights.

**Known friction to measure in L1:** at $112.50/leg, BTC's risk-scaled
notional is ≈$78 in normal vol (trades, slightly under-sized) and ≈$47 in
high vol (**skips**, logged as min-notional). Watch the skip rate. If BTC
misses most entries, the fix is a sizing decision — fund the BTC legs
specifically, or run the book ex-BTC — not a strategy change.

## 3. Go-live program (`research/ROADMAP.md` Phase 6)

Human decision: real margin, staged, **no rush**, pre-registered kills.

- **L0 — two accounts (REQUIRED).** `-t` and `-p` trade the same symbols; on
  one account they net against each other and trip every reconcile guard
  (Pattern A). Triple book = **main** (`API_KEY`/`API_SECRET`), pullback
  book = **Bybit sub-account** (`PULL_API_KEY`/`PULL_API_SECRET`), $900 each.
- **L1 — shakedown ≥2 weeks**, full $1,800 at unit weights. Week-1 checklist
  in `deploy/LIVE.md` §5. Halt line **−8% ≈ −$144**.
- **L2 — 2–4 weeks measurement**: reconcile live vs engine, feed measured
  slippage/fill rates into the cost model (>0.2 Sharpe degradation halts).
- **L3 — vol dial**: 15% first (`LEG4H_LIVE_EQUITY` ×2.1), 25% a separate
  later decision.

## 4. Sleeve stacking — 3 seats, forward-tracking

Two battery rounds, ~1 seat per 5 candidates. Sleeve law for a seat:
standalone Sh ≥ 0.5 full **AND** pre-2023-08 ≥ 0.0 **AND** |corr to book| ≤ 0.5.

| sleeve | full Sh | pre-2023 | corr(book) | status |
|---|---|---|---|---|
| **XSMOM-21** (21d residual-vs-BTC momentum, weekly quintiles, QUAL23) | +1.00 | +0.85 | 0.18 | paper — **decay watch** |
| **XSBAB-60** (betting-against-beta, weekly quintiles) | +0.74 | +0.70 | −0.03 | paper |
| **MOP-TSMOM** (12m TSMOM on GC/SI/CL/BZ, 25-year gate) | +0.53 | +0.53 | −0.21 | needs Bybit-venue paper stage |

**Deployable proposal: BOOK50 / XSMOM25 / XSBAB25** → Sh(mo) **1.80, IS 1.63
→ OOS 2.01, dMDD −2.1%**; @15% vol ≈ 28.5% CAGR. **Blocked** until ≥8 weeks
of forward record. The headline open question: is XSMOM's recent fade (last
third +0.14, 2026 −0.7) a rough patch or genuine decay? The forward track
answers it; nothing touches capital before that + the full gate battery.

## 5. Moonshot program (firewalled "fun budget")

Aggressive/fast ideas. If one ever goes live: own sub-account, ≤10% cap,
full gates. **Tier-1 battery (bar data): 0 of 6 survived** — crash-fade,
funding-settlement fade, squeeze breakout, BTC→alts lead-lag, xs-reversal
all died at the 22bp taker cost floor. The lesson, empirically: the signals
exist (lead-lag gross was +3–8bp, real) but they are 3–7× below our cost
floor — the moat is execution cost, not signal discovery.

**Tier-2 needs tick data** → `collector.py` + `docker-compose.collector.yml`
(public Bybit websockets: raw liquidations, 1s trade aggregates, top-of-book,
funding/OI; ~5–10 MB/day, no keys). **Start it early** — tick history cannot
be backfilled. First tier-2 studies at the ~60-day mark.

## 6. Forward trackers — anchors are NOT in git

Both run daily, keyless, deterministic (lagged signals ⇒ past values never
revise, so the track file *is* the forward record once anchored):

| script | sleeves | anchor | env override |
|---|---|---|---|
| `sleeves_paper.py` | TSMOM-90 + funding-carry — the ORIGINAL sleeves that **failed** the long-history gate; kept as a negative control | **2026-07-05** | `SLEEVES_ANCHOR` |
| `xs_paper.py` | XSMOM-21 + XSBAB-60 — the promoted ones | **2026-07-09** | `XS_ANCHOR` |

`state/` is gitignored, so **on the new laptop the first run of each MUST
pass its anchor env var** (see §8) or the forward record silently restarts
from today and the 8-week clock resets.

## 7. File map

| path | what |
|---|---|
| `bot.py` | live/paper executor, one symbol per process (all overlays, maker-entry + TP-limit lifecycle, reconcile/HALT logic) |
| `core/` | strategies, fixed engines, regime GMM, risk, data, funding, sentiment |
| `research/FINDINGS.md` | **the adopted/rejected ledger** — read before trusting any number |
| `research/ROADMAP.md` | program state, gates, Phase 6, stacking rounds, moonshot |
| `research/*.py` | every experiment, pre-registered. Key ones: `deploy_report.py`, `sleeve_battery{,2}.py`, `assembly_v{3,4}.py`, `tradfi_mop.py`, `moonshot_heartbeats.py`, `trend_longhist.py`, `cost_engine.py` |
| `docker-compose.bidir4h-live.yml` | 16 live legs + tg-control (project `rofl4h-live`, image `rofl-bot:4h-live`) |
| `docker-compose.bidir4h-paper.yml` | keyless paper twin (project `rofl4h-paper`) |
| `docker-compose.collector.yml` | tick collector (project `rofl-collector`) |
| `tg_control.py` | Telegram panel (Stats/Positions/Today/Health/Reconcile), dual-account **read-only** |
| `sleeves_paper.py`, `xs_paper.py` | daily sleeve forward trackers (cron) |
| `deploy/LIVE.md` | the go-live runbook (accounts → .env → preflight → start → kills) |
| `deploy/ORACLE.md` | Oracle A1 hosting runbook (**the current host plan**) |
| `deploy/SELFHOST.md` | running the keyless stack on a home laptop |
| `deploy/setup.sh` | box prep: docker + compose + clone + prebuild; **starts nothing** |
| `test_*.py` | plain-assert suites (no pytest): engines, sizing, maker entries, TP limits, reduce-only, exec parity |

Older `research/` scripts from dead eras are kept deliberately — the
audit-trail law means every rejected idea stays reproducible.

## 8. Linux migration checklist (do this on the new laptop)

```bash
git clone git@github.com:dentuss/rofl.git && cd rofl

# 1. venv — note the path differs from the Windows docstrings
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -c "import pandas,sklearn,ccxt;print('deps ok')"

# 2. sanity: the suites must pass before you trust anything
./.venv/bin/python test_maker_entries.py
./.venv/bin/python test_reduce_only_close.py
./.venv/bin/python test_chop_sizing.py

# 3. forward trackers — FIRST RUN ONLY, anchors must be passed explicitly
SLEEVES_ANCHOR=2026-07-05 ./.venv/bin/python sleeves_paper.py
XS_ANCHOR=2026-07-09     ./.venv/bin/python xs_paper.py
# then cron them (see deploy/SELFHOST.md §5)
```

**Things that do NOT travel through git (recreate by hand):**

| item | what to do |
|---|---|
| `.env` | template in `deploy/LIVE.md` §3 — both key pairs + Telegram. `chmod 600`. |
| `.mcp.json` | Bybit MCP server config: `npx -y bybit-official-trading-server@2.1.15` with `BYBIT_API_KEY`/`BYBIT_API_SECRET` in `env`. **Use a READ-ONLY key on the laptop** — dev boxes never get trade permissions. Stays gitignored. |
| `state/*.json` + `*_track.csv` | the sleeve anchors + forward records. Either copy `state/` across from the old machine (preferred — keeps the actual track), or re-anchor with the env vars above (loses the recorded days). |
| `.cache/` | bar/funding parquet cache — just re-fetches on first run (a few minutes). |
| `research/.book_daily.parquet` | book daily-returns cache; `assembly_v*.py` rebuilds it in ~3 min if absent. |

**Platform notes:** `.gitattributes` now forces LF everywhere (a CRLF
`setup.sh` would fail on Linux with `bad interpreter: ...^M`). Only
`collector.py` has platform-specific code and it is `sys.platform`-guarded.
~50 research docstrings still show the old `./.venv/Scripts/python.exe`
line — cosmetic; substitute `./.venv/bin/python`.

## 9. Immediate next actions (in order)

1. **Linux laptop**: §8 checklist — venv, tests green, trackers re-anchored.
2. **Oracle A1 box**: `deploy/ORACLE.md` — upgrade tenancy to **Pay-As-You-Go
   first** (idle-reclamation would otherwise kill a live box under a
   position), reserve a static IP, re-whitelist **both** Bybit keys to it.
3. **Confirm the exchange is flat** before restarting live (the bot has been
   down twice; §10 covers what resume does).
4. **Resume L1** with `LEG4H_LIVE_EQUITY=112.50` and actual balances
   ($900/$900). Watch for the first `PENDING … maker limit` → `OPEN …
   (maker fill)` pair, the first TP-limit fill, and BTC's skip rate.
5. Start **paper + collector** (same box or laptop) and cron both trackers.
6. **~8 weeks after 2026-07-09**: XS forward record matures → re-assembly on
   forward data → the BOOK50/XS25/BAB25 capital discussion (converges with
   the L3 dial decision).

## 10. What resume-after-downtime does

On the first live `up`, each leg reconciles against Bybit automatically:

- **Position still open on the exchange** → resumes managing it (its
  attached SL/TP were live the whole time — that is why downtime was safe).
- **Position gone (SL/TP fired while down)** → books the real close from
  Bybit's closed-PnL history, classifies sl/tp by fill proximity, arms or
  expires the cooldown from the actual fill time.
- **State flat but exchange has something** → cancels stray orders and
  **HALTs** that leg for manual review rather than guessing.

Because the deposit changed, prefer a **clean start**: confirm/flatten on
Bybit, set `LEG4H_LIVE_EQUITY` to match real balances, bring the stack up
fresh (flat state + reconcile). Do **not** copy old live volumes across
hosts.

## 11. Laws (full text in `CLAUDE.md`)

**Methodology:** pre-register cells before running; fixed engine + full cost
model always; gates G1–G5 + the long-history gate before "Adopted"; record
negatives as prominently as wins; universe is structural, never
performance-picked; de-concentrate weights.

**Security/ops:** trading keys ONLY on the single IP-whitelisted live box,
trade-only (no withdrawal); all closes reduce-only; two-account isolation;
never two LIVE portfolios at once; credentials in env vars only; never
compare per-bot state to account-wide balances; paper behavior must never
change when fixing live bugs; nothing touches capital without the full gate
battery **and** a forward paper stage.
