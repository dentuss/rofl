# Session handoff — rofl (as of 2026-07-17)

> For a fresh Claude Code session (the human is migrating to an Ubuntu
> laptop). Read this, then `CLAUDE.md`, then `research/FINDINGS.md` +
> `research/ROADMAP.md`. Auto-memory lives at
> `~/.claude/projects/.../memory/` — `go-live-program-state.md` is the
> load-bearing one.

## 0. TL;DR (60 seconds)

Systematic crypto perp trading program on Bybit USDT-linear, rebuilt from
zero after the **same-bar re-entry engine artifact** (2026-07-05) was found
to have manufactured the entire original backtest edge. Everything since is
gate-validated on the fixed engine with a full cost model. The deployed
product is a **two-leg trend book (BLEND50_CONF)**; three diversifying
**sleeves** are validated and in forward paper-tracking toward a stacked
book targeting Sharpe ~2.0. Repo `dentuss/rofl`, branch **`main`** (clean,
all work merged; PR #34 was the last).

**Operational status right now:** the live book was deployed (~2026-07-08,
stage L1), then **stopped for ~a week** (human was ill). Resuming
today/tomorrow, **migrating the host EC2 → Oracle Cloud Always-Free A1**
(see `deploy/ORACLE.md`). Resume/reconcile logic handles the downtime
automatically (positions carry exchange-side SL/TP).

## 1. What is deployed — BLEND50_CONF

MAJORS8 = BTC ETH SOL XRP DOGE ADA LINK AVAX (ex-ante liquidity, never
performance-picked). Each name runs **two entry legs at 50/50 capital**:

- `-t` **adaptive_bidir_4h**: `triple_confirm_bidir` (EMA 9/26/50 + RSI
  55/45 + ADX 22, sl 1.8×/tp 6× ATR)
- `-p` **pullback_bidir_4h**: `pullback_in_trend` (EMA50 side + RSI 40/60
  recross, same stops) — low-corr (0.17) second leg, trades ~1×/6wk/name

Shared overlays, each individually gate-passed: walk-forward GMM regime mask,
F&G 3-day persistence, drawdown-decay tiers, CHOP half-sizing, vol targeting
(60% ann), **GMM-confidence sizing** (`REGIME_CONF_SIZING`), SL cooldown.
Execution is maker both sides: **post-only limit entries** (`ENTRY_LIMIT_ORDERS`)
+ **TP-as-limit** (`TP_LIMIT_ORDERS`) — the exact cost model the backtests
price, exec-parity verified.

**Honest numbers** (fixed engine, full costs, 2023-08→2026-07, at the real
deposit **$2,177.56**):

| sizing | CAGR | Sh(mo) | dMDD | worst mo | median mo |
|---|---|---|---|---|---|
| unit weights (L1/L2) | 10.4% | 1.50 | −4.5% | −1.7% | +0.40% |
| @15% vol dial (L3) | 22.2% | 1.49 | −9.2% | −3.5% | +0.79% |
| @25% vol dial (L3) | 37.9% | 1.48 | −15.1% | −5.8% | +1.24% |

**Anchor expectations on the full-history (2022-inclusive) Sharpe ~1.2, not
1.5.** Long-history gate PASSED: blend pre-2023-08 +0.18 (triple leg carries
it, +0.57; the pull leg alone was −2.57 and has a pre-registered demotion
trigger: trailing-3mo forward Sh < 0 → BLEND75 / triple-only).

## 2. Go-live program (live-first, `research/ROADMAP.md` Phase 6)

Human decision: real margin, no rush, staged with pre-registered kills.

- **L0** — two-account setup (REQUIRED): `-t` and `-p` trade the same
  symbols; on one account they'd net + trip reconcile guards (Pattern A).
  Triple book = **main account** (`API_KEY`/`API_SECRET`), pullback book =
  **Bybit sub-account** (`PULL_API_KEY`/`PULL_API_SECRET`, ~$1,088.78 each).
  tg-control reads both.
- **L1** — live shakedown ≥2wk, full $2,177.56 at **unit weights** (backtest
  dMDD −4.5%; halt at −8% ≈ −$175). Was in progress when stopped. Week-1
  checklist in ROADMAP/`deploy/LIVE.md`.
- **L2** — 2–4wk measurement: reconcile live vs engine, feed measured
  slippage/fills into the cost model (>0.2 Sh degradation halts).
- **L3** — vol dial (15% first, 25% a separate later decision).

Splits (compose defaults): BTC legs **$300.02**, other 14 legs **$112.68**
(= $2,177.56). Sizing anchors on full-history Sh ~1.2.

## 3. Sleeve stacking — 3 seats, forward-tracking (toward Sharpe 2.0)

Two battery rounds, ~1 seat per 5 candidates. Sleeve law: standalone
Sh(mo) ≥ 0.5 full **AND** pre-2023-08 ≥ 0.0 **AND** |corr to book| ≤ 0.5.

| sleeve | full Sh | pre-2023 | corr(book) | status |
|---|---|---|---|---|
| **XSMOM-21** (21d residual-vs-BTC momentum, weekly quintiles) | +1.00 | +0.85 | 0.18 | paper (decay watch) |
| **XSBAB-60** (betting-against-beta, weekly quintiles) | +0.74 | +0.70 | −0.03 | paper |
| **MOP-TSMOM** (12m TSMOM on GC/SI/CL/BZ commodities, 25y gate) | +0.53 | +0.53 | −0.21 | needs Bybit-venue paper stage |

**Deployable proposal: BOOK50 / XSMOM25 / XSBAB25** → Sh(mo) **1.80, IS 1.63
→ OOS 2.01, dMDD −2.1%**; @15% vol ≈ 28.5% CAGR / dMDD −8.4%. Blocked until
**≥8 weeks forward record** (~early Sept 2026) — the headline question is
whether XSMOM's recent fade (last third +0.14) is a rough patch or decay.
Nothing touches capital before that + the full gate battery.

Forward trackers (run daily, keyless, deterministic, anchor-stamped —
**anchors do NOT travel through git**, preserve via env on a new box):
- `sleeves_paper.py` (TSMOM-90 + funding-carry — the ORIGINAL sleeves that
  FAILED the long-history gate; kept as a negative-control track). Anchor
  **2026-07-06** (`SLEEVES_ANCHOR`).
- `xs_paper.py` (XSMOM-21 + XSBAB-60 — the promoted ones). Anchor
  **2026-07-09** (`XS_ANCHOR`).

## 4. Moonshot program (the "fun budget", firewalled)

Aggressive/fast ideas, ≤10% cap + own sub-account + full gates if any ever
lives. **Tier-1 battery (bar data): 0/6** — crash-fade, funding fade,
squeeze breakout, BTC→alts lead-lag, xs-reversal all died at the 22bp taker
cost floor (the empirical HFT lesson: the signals exist, the cost moat
decides who eats them). **Tier-2** needs tick data → `collector.py` +
`docker-compose.collector.yml` (public Bybit websockets, raw liquidations +
1s trades + book + funding, ~5–10 MB/day, no keys). Start it early; first
tier-2 studies at the ~60-day mark.

## 5. File map

| path | what |
|---|---|
| `bot.py` | the live/paper executor (one symbol/process; all overlays + maker/TP-limit lifecycle) |
| `core/` | strategies, fixed engines, regime GMM, risk, data, funding, cost helpers |
| `research/FINDINGS.md` | **the adopted/rejected ledger** — read before trusting any number |
| `research/ROADMAP.md` | program state, gates, Phase 6 go-live, stacking rounds, moonshot |
| `research/*.py` | every experiment (pre-registered). Key: `deploy_report.py`, `sleeve_battery{,2}.py`, `assembly_v{3,4}.py`, `tradfi_mop.py`, `moonshot_heartbeats.py`, `trend_longhist.py` |
| `docker-compose.bidir4h-live.yml` | 16 live legs + tg-control (project `rofl4h-live`, image `rofl-bot:4h-live`) |
| `docker-compose.bidir4h-paper.yml` | keyless paper twin (project `rofl4h-paper`) |
| `docker-compose.collector.yml` | tick collector (project `rofl-collector`) |
| `tg_control.py` | Telegram panel (Stats/Positions/Today/Health/Reconcile), dual-account read-only |
| `sleeves_paper.py`, `xs_paper.py` | daily sleeve forward trackers (cron) |
| `deploy/` | `LIVE.md` (go-live runbook), `ORACLE.md` (this migration), `SELFHOST.md` (laptop), `README.md`, `setup.sh` |
| `test_*.py` | plain-assert suites (no pytest in venv): engines, sizing, maker entries, TP limits, reduce-only, exec parity |

Local: Python 3.14 `.venv` (sklearn/pandas3). Research scripts run via
`PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe research/<x>.py`. Book
daily returns cached at `research/.book_daily.parquet` (gitignored).

## 6. Immediate next actions (in order)

1. **Set up Oracle A1** per `deploy/ORACLE.md` (PAYG upgrade first, reserve
   IP, re-whitelist BOTH Bybit keys to the new IP).
2. **Resume live** — fresh `up` on Oracle with `STARTING_EQUITY` = actual
   current balances; the reconcile handles the week down.
3. Start **paper + collector** (same box or laptop) and the two daily
   trackers (preserve anchors via `SLEEVES_ANCHOR`/`XS_ANCHOR`).
4. Let L1 run its ≥2 weeks; watch first maker fill + first TP-limit fill.
5. **~early Sept**: XS forward record matures → re-assembly on forward data
   → the BOOK50/XS25/BAB25 capital discussion (and L3 dial talk converge).

## 7. Laws (full text in `CLAUDE.md`)

Methodology: pre-register cells before running; fixed engine + full cost
model always; gates G1–G5 + the long-history gate before "Adopted"; record
negatives as prominently as wins; universe is structural, never
performance-picked. Security/ops: keys ONLY on the one IP-whitelisted box,
trade-only (no withdrawal); all closes reduce-only; two-account isolation;
never two LIVE portfolios at once; credentials in env vars only; nothing
touches capital without the full gate battery + a forward paper stage.
