# CLAUDE.md — rofl project rules & goal

Systematic crypto-perp trading program on Bybit USDT-linear. This file is the
constitution; `SESSIONHANDOFF.md` is the current status; `research/FINDINGS.md`
is the evidence ledger. Read all three before acting.

**Current capital: $1,685.99 USDT** (2026-08-17, measured on-exchange:
main 888.57 + sub `roflbot_pullback` 797.42). Down from $1,800.00 by a
**100.00 operator withdrawal** (2026-08-16, sub UNIFIED -> main FUND -> off
book) plus ~$10.12 of realised trading loss over the L1 shakedown's 10 trades.
Any number in a doc that implies a different deposit is stale — flag it.
Percentages in the research
tables are deposit-invariant; only dollar figures move.

⚠ **Book weights are BLEND75 since 2026-08-17** (0.75 triple / 0.25 pull, the
demotion pre-registered 2026-07-06). Capital is split ACROSS TWO ACCOUNTS to
match: main (`-t`) 1,264.49 and sub (`-p`) 421.50. The keys are trade-only, so
any rebalance is a MANUAL Bybit-UI step — see deploy/LIVE.md "Changing the
blend weights". The −8% halt line is anchored to the 1,685.99 reset baseline
(halt at 1,551.11).

## The goal

Build the highest **honest** risk-adjusted return this book can sustain, and
run it live on real margin. Concretely: get the gate-validated trend book
(Sh **~1.33** common-window / ~1.2 full-history — re-measured 2026-08-03 after
July 2026 closed at −3.6%; the older ~1.5 predates that month) safely through
the staged go-live, then **stack orthogonal sleeves toward book Sharpe ~2.0+**
(3 validated, in forward paper). "Honest" is the whole point — the original
212% CAGR was an engine artifact; every number now is measured on the fixed
engine with full costs or it does not exist.

## Methodology laws (non-negotiable)

1. **Fixed engine + full cost model, always.** Taker 6bp/side, 2bp slip,
   real per-pair funding, maker where modeled. `legacy_same_bar_reentry`
   reproduces the artifact — never trust a number that needs it.
2. **Pre-register cells** in the script docstring BEFORE running. No post-hoc
   grid mining; a widened grid is a new experiment with its own OOS. Fixed
   external specs (e.g. MOP-2012) are admissible; note the test budget.
3. **Gates before "Adopted":** G1 IS/OOS 60-40 stability, G2 sub-window
   thirds, G3 random-entry null ≥95th pct, G4 universe generalization, G5
   exec parity — plus the **long-history gate** (2022-inclusive pseudo-OOS:
   full Sh ≥ 0.5 AND pre-2023-08 ≥ 0 for a sleeve).
4. **Universe is structural** (liquidity/class, ex-ante), NEVER picked by
   backtest performance.
5. **Record negatives as prominently as wins.** The Rejected tables are why
   the adopted stack is trustworthy. Every result gets a FINDINGS entry.
6. **Nothing touches capital** without the full gate battery AND a forward
   paper stage. Sleeve law for a seat: standalone Sh ≥ 0.5 full & ≥ 0
   pre-2023 & |corr to book| ≤ 0.5.
7. **De-concentrate weights** (the carry lesson): no single sleeve above its
   book-majority cap just because IV loves it.

## Security & ops laws (money is real)

- Trading keys live ONLY on the single IP-whitelisted box, **trade-only**
  permissions (NO withdrawal/transfer). Credentials in env vars
  (`.env`, chmod 600), never in files or git. `.mcp.json` stays gitignored.
- **All closes reduce-only** (Pattern F). Never let a close re-open.
- **Two-account isolation**: the `-t` (triple) and `-p` (pullback) legs
  trade the same symbols → separate Bybit accounts (main + sub), or they net
  and trip every reconcile guard (Pattern A).
- **Never run two LIVE portfolios at once.** Paper/collector are keyless and
  safe to co-locate.
- **Never compare per-bot state to account-wide exchange values** (a $112
  leg vs a $1,088 account → false −85% "drawdown").
- Paper-mode behavior must NEVER change when fixing live bugs (`cfg.mode ==
  "live"` gate). Clock must be UTC + NTP-synced (skew breaks Bybit auth).
- Laptop MCP / dev scripts are **read-only** (`mcp__bybit__get*`/`query*`
  only). Never edit directly on the production box.

## How to work here

- **Local env (LINUX since 2026-07-30)**: `python3 -m venv .venv &&
  ./.venv/bin/pip install -r requirements.txt`. Run research via
  `PYTHONIOENCODING=utf-8 ./.venv/bin/python research/<x>.py`; heavy runs go
  to background Bash with `tee` to the scratchpad. Tests are plain-assert
  `__main__` runners (no pytest) — `./.venv/bin/python test_<x>.py`.
  ⚠ 39 research scripts still carry the old Windows `Run:` line in their
  docstrings (`./.venv/Scripts/python.exe`) — that is cosmetic; on Linux
  substitute `./.venv/bin/python`. Only `collector.py` has real
  platform-specific code, and it is properly `sys.platform` guarded.
- Commit to `main` (feat branches + PRs are fine); the human merges. End
  commit bodies with the Co-Authored-By trailer. Push when a unit of work
  is validated + documented (FINDINGS/ROADMAP updated).
- When asked "can we go live / dial up / deploy X": check the relevant
  ROADMAP checkboxes first and report which stage is active. Never skip a
  stage or start a dial early.
- Deliver findings the human can act on: the outcome first, the honest
  caveat second, the mechanism third. If a result looks too good, suspect an
  artifact before celebrating — that instinct is the project's origin story.
