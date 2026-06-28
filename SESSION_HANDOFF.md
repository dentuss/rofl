# Session handoff — rofl

> **For a fresh Claude Code session.** Read this top-to-bottom before doing
> anything. It captures everything earned the hard way in the previous
> session that isn't already encoded in the code or `README.md`.

---

## 0. TL;DR (60 seconds)

This is a **crypto trading bot** in production on Bybit USDT perpetuals.
Strategy is a bidirectional trend-follower with regime + sentiment filtering.
Validated on 33 pairs across 4–9 years of history. Currently **live on a
real $300** in a 5-pair portfolio on a t4g.medium EC2 box.

- **Repo**: `dentuss/rofl`, work on branch `main` (squash-merge PRs only)
- **Live setup**: 5 docker containers (`rofl-inj`, `rofl-sol`, `rofl-ada`, `rofl-eth`, `rofl-link`)
- **Backtest expectation**: +5–7% / month median, −10–15% worst month, −18% MDD
- **Read first**: this file → `README.md` → `deploy/LIVE.md` → `research/FINDINGS.md`

The single most important thing to know: **paper-mode behavior must never
change** when fixing live-mode bugs. The `cfg.mode == "live"` gate matters.
Test changes with `python3 test_parity.py` and an offline paper smoke before
shipping anything.

---

## 0.5 ⏯ CONTINUE FROM HERE — handoff @ 2026-06-28 (read this first)

> Written when the session moved to terminal Claude Code on the local repo.
> The numbered sections below are the durable onboarding; THIS block is the
> live picture and the open threads to pick up.

### Current state
- **Bots are STOPPED** (`portfolio.sh down`). **No open positions** — the last
  one (an ETH short) was closed manually. Clean slate for a reassessment.
- **Latest code is on `main` but NOT yet deployed** to EC2 (bots are down).
  Resume = `git pull origin main && sudo ./portfolio.sh up -d --build`.
- **Books vs exchange disagree.** Bots' state booked ≈ **$302.52** total, but
  real Bybit equity is **lower** (was ~$297.9 on 06-22). Causes: (a) every
  `*-external` exit booked at the *theoretical* SL/TP, not the real fill;
  (b) two reduce-only orphan trades (SOL 06-20, ETH 06-22) never entered the
  books; (c) funding/fee modeling drift. **Live reconcile still pending** — the
  read-only Bybit MCP kept dropping this session.

### What shipped this session (all squash-merged to `main`, in order)
- `306ade1` **reduce-only on ALL closes** (Pattern F) — closes were
  non-reduce-only and REVERSED into unprotected positions when the exchange had
  already flattened. Regression: `test_reduce_only_close.py`.
- `94a12ef` gitignore `.mcp.json`.
- `04c0bcc` **per-symbol fetch stagger** + clearer startup log (live rate-limits
  dropped 17/31 → 1-3 per day).
- `5a39744` docstring fix + documented the (immaterial) signal-flip approximation.
- `943c115` **`test_exec_parity.py`** — replays the REAL bot exec methods vs the
  backtester; catches exec-path divergences `test_parity.py` cannot.
- `60d83c7` funding-as-signal research → **rejected** (IC≈0).
- `2b0d6eb` **exec fixes**: recompute notional after lot-step rounding (ETH was
  under-sized ~54%); warm markets at startup (cold-start spot-routing miss);
  throttled alert when the DD hard-stop freezes a bot.
- `0c22229` re-entry-cooldown + ETH-swap research (FINDINGS "Promising" section).

### Open threads — pick up here (priority order)
1. **Reconnect the Bybit MCP, then RECONCILE.** Read-only (`rolfbot_dev`,
   IP-whitelisted). A terminal session in this repo should load the project
   `.mcp.json` — verify `claude mcp list` / `claude mcp get bybit`; full app
   restart after any `setx`. Then `getWalletBalance` + `getClosedPnl`
   (06-19→now) + `getPositionInfo`, compare to the booked $302.52, quantify the
   gap. (Memory: `bybit-readonly-mcp-dev`.)
2. **VALIDATE the re-entry cooldown — the big lead.** Block same-side entry for
   K bars after a SL on that side. Recent 400d, *no* regime/F&G: K=3 lifted mean
   Sharpe **1.48→2.58**, MDD −39%→−29% on all 5 pairs
   (`research/test_reentry_cooldown.py`). **Too good to trust as-is** — re-run
   **WITH regime+F&G** (needs sklearn → 3.11 venv or EC2/Docker) and
   **walk-forward** over multiple windows. Survives → paper → small live. It's
   an ENTRY filter (FINDINGS is skeptical) but a genuinely NEW mechanism.
3. **External-fill accounting fix** (root of the balance gap): book `*-external`
   closes at the actual exchange fill, not the theoretical SL/TP. Proposed, not
   built; adds one API call per external close.
4. **Resume decision:** deploy latest `main` + restart the fleet, or stay
   stopped pending the cooldown validation.

### Settled this session (don't re-litigate)
- **Keep ETH.** Mid-pack over 400d (+107% standalone); its live 0/4 week was
  variance. AAVE looks better recently but that's the chase-recent-winners trap.
- **Funding-as-signal: rejected** (IC≈0). **Signal-flip exit** the live bot
  lacks is **immaterial** (measured <1% of exits).

### Local-env notes for the terminal session
- Repo runs on **Python 3.14** locally with **no sklearn** → the regime GMM is
  skipped (`REGIME_AVAILABLE=False`); paper smokes run but without regime, and
  `test_parity.py` / any with-regime backtest needs a **sklearn env** (3.11
  venv, or run in the Docker image which bundles it).
- No-sklearn-needed checks: `python test_reduce_only_close.py`,
  `python test_exec_parity.py`, and the `research/*` backtests.

---

## 1. What this project IS and IS NOT

**Is**: a strict, well-validated, conservatively-engineered trading bot for a
*specific* deterministic strategy. The validation phase is over — this is now
*operating* a tested system.

**Is NOT**:
- A strategy research playground (FINDINGS.md is the closed list — many
  "improvements" have been tried, most failed, do not re-litigate)
- An ML platform (ML entry filters have been tried twice, both failed; the
  features are already in the strategy)
- A general-purpose trading framework (it's tuned for this exact bidir setup)

**Decision pattern**: if a proposed change isn't in `research/FINDINGS.md`'s
"Adopted" column, default to skeptical. The strategy is information-efficient;
most additions hurt more than help.

---

## 2. Live operational state (as of handoff)

| Item | Value |
|---|---|
| **Mode** | **STOPPED 2026-06-28** (was LIVE; resume = `git pull origin main && sudo ./portfolio.sh up -d --build`) |
| **Capital deployed** | $300 USDT |
| **Portfolio** | 5-pair `inj_heavy` (INJ 40 / SOL 20 / ADA 15 / ETH 15 / LINK 10) |
| **Realised P&L** | bots booked **+$2.52 → $302.52** over the first ~week; real exchange equity is LOWER — reconcile pending (see §0.5) |
| **EC2 instance** | t4g.medium (4 GB) in `ap-southeast-1` Singapore |
| **API key** | prod: IP-whitelisted to EC2, Contract Trade + Position, no withdrawal. dev (laptop/terminal): read-only `rolfbot_dev` for the MCP |

**Open positions**: **FLAT** — all closed (the last, an ETH short, closed
manually). Bots stopped. Verify with `sudo ./portfolio.sh status` before
assuming anything.

**Critical**: do not advise wiping state (`down -v`) while positions are open
— it disconnects the bot's accounting from the exchange and the user has to
manually reconcile. Use `./portfolio.sh archive` for snapshots and
`./portfolio.sh reset-position <bot>` only after the user has manually
flattened that pair on Bybit.

---

## 3. The strategy in one screen

Entry — `core/strategies.py:triple_confirm_bidir`. Three confirmations
mirror-imaged for long/short:

| | Long | Short |
|---|---|---|
| EMA stack | fast > slow > trend (9/26/50) | inverted |
| Momentum | RSI(14) > 55 | RSI(14) < 45 |
| Trend strength | ADX(14) > 22 | ADX(14) > 22 |
| Stops | 1.8× ATR(14) | symmetric |
| Targets | 3.0× ATR(14) | symmetric |

Four defensive layers, all backtest-validated:

1. **Directional regime filter** — GMM labels each bar BULL/CHOP/BEAR
   walk-forward (`core/regime.py`). Longs allowed in BULL/CHOP only, shorts
   in BEAR/CHOP only.
2. **Fear & Greed 3-day persistence** — blocks longs at F&G ≥ 80 for ≥3
   consecutive days, shorts at F&G ≤ 20 for ≥3 days. Flash extremes pass
   through (continuation trades). Discovered by the user mid-session
   (the original 1-day rule was leaving money on table).
3. **Three-tier drawdown decay** (`core/risk.py`) — ×0.5 at −20%, ×0.25
   at −35%, halt at −50%. Per-bot, anchored to the bot's own
   `starting_equity` slice (NOT account balance — that's the bug class).
4. **Bybit-attached SL/TP** on entry orders — managed by the exchange
   autonomously even if the bot is down.

**The strategy is bar-close-based.** Indicators only update at bar close.
The bot polls but only acts at bar close. Intra-bar data is not used. This
is why the bar-close gate works (`bot.py:tick` floor-arithmetic gate).

---

## 4. Architecture

```
EC2 t4g.medium (Singapore)
└── ~/rofl/
    ├── docker-compose.bidir-portfolio.yml  (5-pair, inj_heavy weights)
    ├── docker-compose.bidir8.yml            (8-pair, equal-weight; NOT running)
    ├── portfolio.sh                         (wrapper: up/down/status/archive/reset-position)
    ├── bot.py                               (entrypoint, in each container)
    └── core/                                (strategy + indicators + reconcile)

  5 containers running, each:
   - 1 symbol (INJ/SOL/ADA/ETH/LINK)
   - own state file in own named docker volume
   - own equity slice (sums to $300)
   - shared cache volume for OHLCV history
   - heartbeat every tick → docker healthcheck
```

**Per-bot vs portfolio**: every bot is an independent process with its own
slice. No shared equity, no auto-rebalancing. Total = sum of state files.
`./portfolio.sh status` execs into each container, reads its state, prints
a unified table.

**Key files**:
- `bot.py` — single-bot entrypoint. The class is `Bot`; the loop is `tick()`.
- `core/strategies.py` — `triple_confirm_bidir` and friends
- `core/regime.py` + `core/regime_strategy.py` — GMM regime detection
- `core/sentiment.py` — F&G fetch + 3-day persistence
- `core/risk.py` — drawdown decay tiers
- `core/backtest_enhanced.py` — the backtester (must match `bot.py` exactly)
- `test_parity.py` — proves live signal generation matches the backtest

---

## 5. How to work on this code safely

### Workflow (local laptop → GitHub → EC2)

```
laptop: ~/code/rofl    ← edit here
  │ python3 test_parity.py            ← must pass 3/3
  │ offline paper smoke               ← must run clean for ≥60s
  │ git commit + push (open PR)
  ▼
GitHub: dentuss/rofl/main              ← squash-merge after CI green
  │
  ▼
EC2: ~/rofl
  $ git pull origin main
  $ sudo ./portfolio.sh up -d --build  ← state preserved across this
```

**Never edit directly on EC2.** It bypasses the parity test, can't be
reverted cleanly, and one bad save kills production.

### Offline paper smoke (30s)

```bash
rm -rf state logs
timeout 60 env MODE=paper EXCHANGE=kucoin_offline \
    STRATEGY_PRESET=adaptive_inj_bidir \
    STATE_FILE=state/bot_state.json LOG_FILE=logs/bot.log \
    POLL_SECONDS=5 \
    python3 bot.py >/dev/null 2>&1
grep -E "Traceback|ERROR|CRITICAL" logs/bot.log || echo "clean"
```

If `grep` shows anything, do not commit.

### Parity test (~3 min)

```bash
timeout 200 python3 test_parity.py 2>&1 | grep -c "PARITY OK"
# Must be 3
```

This runs the live signal generator bar-by-bar across history and asserts
the signals match the backtest signal exactly. **Any code change to the
signal path or strategy must preserve this**, or backtest numbers become
meaningless.

### Releasing

1. Open a PR. Title should be specific (no "fixes" or "updates").
2. Body should describe the bug or feature, the root cause, the fix, and
   the verification done.
3. Squash-merge to `main`.
4. User pulls and rebuilds on EC2.

---

## 6. The "everything we tried" reference

`research/FINDINGS.md` is the definitive record. Quick summary:

### Adopted (live in production)
- Bidirectional strategy (`triple_bidir`)
- Directional regime filter
- F&G **3-day** persistence filter (not 1-day; that was an early bug-of-omission)
- Three-tier drawdown decay
- Multi-pair portfolio (5-pair inj_heavy validated, 8-pair backtested but not deployed at $300)
- scikit-learn bundled (regime GMM actually runs)

### Rejected after testing
- Partial TP + breakeven (catastrophic — strategy needs winners to run)
- Fresh-crossover gating (kills return; strategy works *because* it rides stale trends)
- Finer regime taxonomies (4/5 components: worse than 3)
- ML entry filter (tested twice, no edge — features already in strategy)
- Per-regime risk sizing (+0.10 Sharpe, −36% return — defensive only)
- Walk-forward param retune (overfits annual window)
- Chop / health filters (all variants — strategy already handles chop via ADX)
- `lean_sol_ada` weighting (catastrophic vs equal/inj_heavy)
- Dynamic rebalance toward trailing winners (+4% vs +2358% static)

### Validated periphery (haven't been promoted but proven sound)
- Pair universe: 20 of 33 pairs clear Sharpe ≥ 1.1. Strategy is not
  INJ-specific. Worst pairs (BNB, BCH, XRP, etc.) Sharpe ~0.7 — still
  profitable but mediocre. Avoid as production picks.
- Diversification peaks at 7–8 equal-weight pairs (Sharpe ~2.4 backtest)

**Decision pattern**: if asked to "try X to improve the strategy", check
the rejected list first. If X overlaps with rejected ideas, give the
honest history and require strong reason to retest.

---

## 7. Operational commands

```bash
# Status (most common — equity, positions, healthchecks)
sudo ./portfolio.sh status

# Logs
sudo ./portfolio.sh logs -f --tail=50
sudo ./portfolio.sh logs --tail=200 | grep -iE "open|close|conflict|halt|error"

# Snapshot for record-keeping (safe while live)
sudo ./portfolio.sh archive
scp -r ec2:~/rofl/archives/run_5pair_<date> ./

# Apply code changes (rebuilds containers, preserves state)
git pull origin main
sudo ./portfolio.sh up -d --build

# Stop everything (preserves state on volumes)
sudo ./portfolio.sh down

# DANGER: stop + wipe ALL state (positions, equity, peaks, logs gone)
sudo ./portfolio.sh down -v

# Clear ONE bot's tracked position (use after manually closing on exchange)
sudo ./portfolio.sh reset-position sol

# Switch to 8-pair (requires t4g.medium; needs ≥$600 to clear min-cost on every pair)
PORTFOLIO=8 sudo -E ./portfolio.sh up -d --build
```

---

## 8. Bug patterns recurring across the project

These have bitten us multiple times. Watch for them when reviewing code or
proposed changes.

### Pattern A: per-bot state confused with account-wide state
Every bot in a portfolio sees the **whole** Bybit account when it calls
`fetch_balance` or `fetch_positions`. **Never** compare a per-bot state
field (equity, equity_peak, starting slice) against an account-wide
exchange value. Already bit us 3 times:
- `equity_peak = max(peak, fetch_balance())` → fake −85% drawdown on every bot
- preflight `state.equity / fetch_balance()` ratio check → would have frozen the whole portfolio
- resume-reconcile auto-adopt of bigger exchange position

If a code change reads `fetch_balance()` or `fetch_positions()` in a
per-bot context, **assume bug** until proven otherwise.

### Pattern B: persisted state survives "fix" code
If you `max(persisted, new)` to "floor" a value upward, the persisted
value can still be poisoned. You may also need **self-heal** code that
detects + lowers it. PR #19 had this exact mistake — fix only worked
forward, the corrupted state on disk survived. PR #20 added the self-heal.

When fixing a stored-value bug: ask "if a user has the bad value sitting
in their state file right now, will this fix actually correct it on the
next startup?"

### Pattern C: Bybit's spot vs perp symbol ambiguity
`SOL/USDT` matches both spot and linear-perp markets on Bybit V5. ccxt's
`market(symbol)` returns spot first. **Always** route through
`Exchange._ccxt_symbol()` which appends `:USDT` for the perp form. Same
for any private endpoint — pass `_ccxt_params()` to include
`category=linear`.

### Pattern D: backtest assumes things that aren't true live
- Backtest assumes stop orders fill at the stop price. Live, they fill
  at market and may slip.
- Backtest's bar high/low comes from KuCoin REST. Live perp prices on
  Bybit differ slightly — see the SOL phantom-position bug (PR #25).
- Periodic exchange-position reconcile catches the "autonomous SL fired
  but bar-based check missed it" case.

### Pattern E: rate limit IP-based, not key-based
Public market data (`fetch_ohlcv`, `fetch_ticker`) on Bybit is rate-limited
by **IP**. Adding more API keys does NOT help. The fix is to fetch less
often (PR #23 / PR #26 bar-close gate, plus the per-symbol fetch stagger that
de-bursts the 5 bots at each bar close).

### Pattern F: closes must be reduce-only (or they REVERSE the position)
`close_position()` and the orphan force-close place market orders to flatten.
If the exchange already closed the position (its attached SL/TP fired
autonomously), a plain market order does NOT error — it **opens a new
reversed, unprotected position**. The "already flat → just book it" path only
works because `reduce_only=True` makes Bybit reject the order on a flat book,
which the `except` branch then treats as "already closed". Found live
2026-06-22: an ETH short with no SL/TP appeared after the ETH long stopped
out, because the reconcile booked a phantom close while the non-reduce-only
order quietly opened the short. Fixed by passing `reduce_only=True` on every
close (regression: `test_reduce_only_close.py`). **Rule:** any order whose
purpose is to CLOSE must be reduce-only.

---

## 9. Bybit MCP setup (if using desktop Claude)

The Bybit MCP makes API calls from your laptop, not from EC2. The
production API key is whitelisted to the EC2 elastic IP, so it will get
403 from your laptop.

**Recommendation**: separate read-only key for the laptop.
1. Bybit → API Management → Create Key
2. Permissions: **Position + Account Info READ-only**. **NO Trade**, **NO Withdrawal**.
3. IP-whitelist your laptop's public IP (check at whatismyipaddress.com)
4. Use this for the MCP — for diagnostics (check balance, list positions, recent trades)

The production trading key stays on EC2 only. Desktop Claude can never
place an order, only observe. This is the correct security boundary.

---

## 10. What's worth doing next vs not

### Worth doing
- **Monitor.** This is now an operational system, not a research project.
  Daily `status` checks. Weekly `archive`. Telegram pings for trades.
- **Two-week paper-equivalent live run.** Compare realised PnL distribution
  to the backtest's monthly distribution. Real out-of-sample data is
  more valuable than any new feature.
- **Scale up after 2–3 weeks of clean runtime.** Deposit more, bump
  `TOTAL_EQUITY` in `.env`, `down -v` + `up -d --build`. Or switch to
  8-pair at ~$600+.
- **Re-tune F&G persistence days** if you see edge-case behavior (the
  3-day threshold was a discovery; it might not be globally optimal).
- **Funding-rate awareness** — currently modeled as flat 1bp/8h; real
  funding has spikes that could be informative as a signal (not just a
  cost). Possibly worth experimenting but unproven.

### NOT worth doing (already disproven, see FINDINGS.md)
- More entry filters of any kind (ML, chop, health, partial TP, crossover)
- Weight optimization across the 5 pairs
- Dynamic rebalancing toward winners
- Higher leverage (the bot's 2% risk sizing makes the cap inert)
- Lower timeframe (15m loses money with the current strategy)
- Switching primary OHLCV source to KuCoin (Bybit perp prices ARE the
  ground truth in live mode)

### Worth thinking carefully about before doing
- Adding a 6th, 7th, 8th pair gradually (small Sharpe lift but tail-smoothing
  benefit — backtested fine, untested live)
- WebSocket data feeds instead of polling (overkill for hourly strategy)
- Multi-exchange (Bybit + OKX/KuCoin) for fault tolerance

---

## 11. Personality / interaction patterns

The user is **engaged, sharp-eyed, and asks good questions**. Catches real
bugs from logs (the F&G persistence asymmetry, the equity-peak DD spam,
the SOL phantom position were all user observations that led to genuine
improvements). Treat user intuitions seriously, but ground every change
in data + backtest validation.

The user is **comfortable with honest pushback**. If they propose
something that wouldn't actually help (e.g. multiple API keys for the
IP-rate-limit problem, or dual-source data streams for a bar-close
strategy), explain *why* it doesn't help in plain terms — don't capitulate.

**Don't sandbag** — write detailed PR descriptions, link to the rejected
ideas in FINDINGS.md when relevant, and explain root causes not just
symptoms. The user reads the descriptions and remembers.

---

## 12. Quick recovery: "I'm a new session, what do I do?"

```bash
# Step 1: see the current state of the world
gh pr list --state merged --limit 10    # recent fixes
cat README.md                            # strategy + setup
cat research/FINDINGS.md                 # everything tried
git log --oneline -20                    # recent context

# Step 2: confirm production health (need SSH access or user help)
# Ask user: "Run `sudo ./portfolio.sh status` on EC2 and paste output"

# Step 3: only then start any work
```

Do not propose changes to the strategy core without:
1. Reading FINDINGS.md to verify the idea isn't already-rejected
2. Reading the relevant code in `core/`
3. Running the parity test against any proposed change

---

## 13. Files for deeper reference

| Doc | Purpose |
|---|---|
| `README.md` | Strategy + setup at a glance |
| `deploy/README.md` | EC2 deployment & operations |
| `deploy/LIVE.md` | Going-live runbook + emergency procedures |
| `research/FINDINGS.md` | The closed list of what's been tried |
| `research/build_archive.py` | Snapshot tool for portfolio runs |
| `research/test_*.py` | The experiments (most rejected, all instructive) |

**Last meaningful state (2026-06-28)**: first ~week of live running done, then
**bots stopped, flat, for a reassessment**. See **§0.5 "Continue from here"** for
the full picture — the live-exec bugs found & fixed this session, the new test
tooling, and the open threads (MCP reconcile, the re-entry-cooldown validation,
the external-fill accounting fix). The strategy core is stable; this session's
work was all in the live-execution path + research.

Good luck. Don't break it.
