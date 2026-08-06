# Edge rediscovery roadmap — post-artifact rebuild program

Goal: push CAGR / Sharpe / MDD to their honest limits for this bot, starting
from the validated base (4h port: CAGR ~15%, Sharpe(mo) ~1.0, MDD ~−10%).
Reference points that are real: top-decile systematic crypto programs print
Sharpe ~1.5–2.5 and 30–70% CAGR — but they get there through execution
economics, diversification, and sizing, not magic entries. That is the path
here too. **Every number in this program is measured on the FIXED engine with
the full cost model — no exceptions.**

## Standing rules (apply to every experiment)

1. **Cost model is mandatory**: taker fee 0.06%/side, slippage 2 bps/side,
   funding modeled (flat 1 bp/8h until Phase 2 upgrades it to real per-pair
   funding series — after that, real series only). Any new cost discovered in
   paper/live (spread, partial fills, min-notional rounding) gets added here.
2. **Validation gates** (all must pass before FINDINGS "Adopted"):
   G1 IS→OOS 60/40 stability (no big decay; suspicious if OOS >> IS too)
   G2 sub-window thirds all positive
   G3 random-entry null ≥95th pct (for anything touching entries)
   G4 universe generalization (≥8/11 names profitable, zero re-tuning)
   G5 exec parity green on the exact deployment config
3. **Pre-register cells**: write the variant list in the script docstring
   BEFORE running. No post-hoc grid mining; a widened grid = a new experiment
   with its own OOS.
4. Artifact-era results are void. Pre-2026-07-05 rejections may be RE-TRIED;
   pre-2026-07-05 adoptions may be RE-CHALLENGED.

## OPERATING PLAN — from 2026-08-03 (read this first)

### Where we actually are

- **Nothing is trading. Nothing is collecting.** Both are stopped.
- Capital **$1,797.14** on-exchange, in the WRONG wallets: main has $797.65
  in FUND + $0.00003 in UNIFIED (the `-t` legs would start with zero margin);
  sub `roflbot_pullback` has $999.49 in UNIFIED. Split is 797.65/999.49, not
  the planned 900/900.
- Book re-measured 2026-08-03: **CAGR 9.5%, Sh(mo) 1.33, dMDD −5.6%, worst
  month −3.6%** at unit weights (the older 10.4/1.50/−4.5/−1.7 predates July
  2026 closing at −3.64%).
- Code: per-side maker/taker fee booking fixed; 11/11 suites green on
  Python 3.14.4 / pandas 3.0.5 / sklearn 1.9.0.
- Hosting: Oracle A1, **2 OCPU / 12 GB free**, split into a 1/2 collector box
  and a 1/10 trading box (`deploy/ORACLE.md`).

### Three tracks. A runs NOW; B is gated; C needs no capital.

**TRACK A — DATA (no keys, no capital, no gate — start today)**

- [ ] **A1. Collector up** on its own box: `deploy/init-collector.sh`.
      QUAL23 universe, ~7.7 GB/yr. *Every day this is off is a day of tick
      history that can never be recovered.* Highest value-per-effort action
      available, and it is blocked on nothing.
- [ ] A2. **+30 days** → data-sanity study (gap rate, liquidation print
      counts, book-snapshot coverage). Not a strategy study.
- [ ] A3. **+60 days** → tier-2 moonshot studies unlock (liquidation cascades,
      funding-settlement microstructure, book imbalance). 8 of the 32
      taxonomy bot types are gated on exactly this.

**TRACK B — CAPITAL (sequential; no stage starts before the previous is green)**

- [x] **L0 — account isolation.** DONE (sub exists, verified 2026-08-03).
- [x] **L0.5 — CAPITAL SPLIT — DONE** (verified on-exchange 2026-08-06 from the
      trading box: main **$899.86**, sub **$898.57**, both in UNIFIED, FUND
      drained, **0 open positions and 0 open orders on both**). Per-leg
      `LEG4H_LIVE_EQUITY=112.20` (lower of 899.86/8 and 898.57/8; one value
      feeds all 16 legs). Both keys AUTH OK *from the box*, so the Bybit IP
      whitelist is correct. Stale state files from the mis-sized run were
      cleared — `State.load()` would otherwise have overridden the fix.
      Original spec below.
- [ ] ~~L0.5 — capital split~~. Move main FUND → UNIFIED;
      rebalance to ~$898.57 per account; set `LEG4H_LIVE_EQUITY` =
      real per-account balance / 8. Confirm BOTH accounts flat. Until this is
      done, starting the stack means 8 legs with no margin and a 25% size
      asymmetry that breaks the validated 50/50 blend.
- [ ] **L1 — shakedown, ≥2 weeks**, full deposit at unit weights. Halt line
      **−8% ≈ −$144**. Week-1 checklist in `deploy/LIVE.md` §5.
- [ ] **L2 — measurement, 2–4 weeks.** Reconcile live vs engine; feed measured
      slippage/fill-rate/funding back into the cost model.
- [ ] **L3 — vol dial.** 15% first (×2.1); 25% is a separate later decision.

**TRACK C — RESEARCH (no capital at risk)**

- [ ] **C1. Re-specify the PULL demotion trigger** *before* L1 concludes. As
      written (trailing-3mo Sharpe < 0) it fires off ~2 observations and will
      trip spuriously in the first live quarter — see FINDINGS 2026-08-03.
      Replace with cumulative R over ≥20 PULL trades, or a trade-count gate.
- [ ] **C2. Grid / short-gamma sleeve**, pre-registered, validated against the
      real 2-day bot result first, then run through 2022 against the sleeve
      law. The only untested payoff shape in the stack. Prediction on record:
      **it dies pre-2023.**

### IF → THEN (pre-registered responses; decide now, not at 3am)

| trigger | response |
|---|---|
| Book −8% from L1 start (≈ −$144) | **HALT everything, flatten, post-mortem before any restart.** Not a dial-down — a stop. |
| A single leg HALTs on a reconcile guard | Leave it halted. Investigate that leg only. **Never** blanket-restart to clear a halt. |
| Bybit rejects a post-only entry | Set `ENTRY_LIMIT_ORDERS=0` for that leg, record the economics delta (maker→taker is +4bp/side). |
| Bybit rejects the TP limit attach | Set `TP_LIMIT_ORDERS=0`, record the delta. Both fallbacks are documented in the compose. |
| BTC min-notional skips >30% of its signals | A **sizing** decision, not a strategy change: fund the BTC legs specifically or run ex-BTC. Re-validate weights either way. |
| Measured costs degrade the edge >0.2 Sh (L2) | HALT, re-price the cost model, re-run the gate battery before resuming. |
| PULL cumulative R < 0 over ≥20 trades | Demote to BLEND75 or triple-only (per C1's re-specified trigger — **not** the 3-month Sharpe). |
| A month closes worse than −5% | Not an automatic halt: backtest worst is −3.6% and the tail is real. Do re-check realised dMDD against the model. |
| Live equity diverges >1% from the exchange | Check **per-leg**, never per-bot-vs-account-wide (Pattern: a $112 leg vs a $900 account reads as a false −85%). Suspect the fee model first. |
| Bybit auth failures | Check NTP/clock skew first — it is the single most common cause. |
| A box is reclaimed or dies | Positions are safe (exchange-side SL/TP fire regardless). Rebuild from the init script; resume reconciles and HALTs anything it didn't open. |
| Collector gap >24h | Record the gap in the data log and move on. **Never** synthesise or backfill ticks — a fake row poisons every study built on it. |

## Phase 1 — Paper the validated base (NOW)

- [x] Engine fixed + validated (honest_rebuild r1–r3)
- [x] 4h wiring: `adaptive_bidir_4h` preset, `TL_TP_MULT=6.0`,
      `COOLDOWN_BARS=0`, docker-compose.bidir4h-paper.yml (paper-pinned)
- [x] Exec parity on the exact 4h/tp6 config
- [ ] Run the paper portfolio ≥4 weeks; weekly reconcile of paper decisions
      vs fixed-engine expectations (trade count, entry prices, exit reasons)

## Phase 2 — Cost engine (the biggest known lever)

Fees were 73% of gross on 1h and are still the largest cost line on 4h.
- [x] Real funding series per pair wired in (`apply_funding_real`, Bybit→OKX
      events; per-pair means 0.04–1.69 bp/8h — the flat 1bp assumption was
      slightly *flattering*: real funding costs ~0.6pp CAGR more)
- [x] Maker-entry model (`entry_style="maker_close"`, strict-penetration
      fills, 0.02% fee, no slip; entry-bar SL debited, same-bar TP never
      credited). Result: fees −33%, only 6/1048 fills missed, ≈flat CAGR but
      better Sharpe/MDD/worst-month — a mild risk-adjusted win, adopted into
      the trustworthy baseline
- [x] Entry-bar SL/TP check (`entry_bar_exit_check`, default ON): the old
      one-bar grace period was worth 1.2pp CAGR / 0.07 Sh — removed
- [ ] Slippage/spread measurement from paper+live fill logs → replace the
      flat 2 bps assumption with measured per-pair values
- [x] TP-as-limit — **ADOPTED 2026-07-06** (engine `tp_as_limit`, strict
      penetration, maker fee): ZERO TP fills lost on the promoted books,
      fees −18% on the triple leg, blend Sh 1.50→1.52. Live execution
      still needs the reduce-only-limit order type in bot.py (currently
      exchange-side conditional = taker) — pending execution work

## Phase 3 — Re-trial of the artifact-era graveyard (on the 4h base) — DONE 2026-07-05

- [x] Partial TP: rejection upheld honestly (−6 to −10pp CAGR) — winners must run
- [x] Chop filter: UNINFORMATIVE on 4h (1h-tuned threshold never fires);
      re-parameterize for 4h if ever revisited
- [x] Stricter ADX (25): rejected (−0.13 Sh)
- [x] **Per-regime sizing: SURVIVOR — ADOPTED** (CHOP ×0.5: +0.1 Sh ~free;
      the old "−36% return" cost was the artifact punishing reduced churn)
- [x] HTF risk bias: rejected again on the honest 4h base (−0.32 Sh)
- [x] **SL cooldown K=3 engine bars: ADOPTED with REGSIZE** (better on every
      metric on every book; ≡ live COOLDOWN_BARS=2 via the signal-bar gate)
- [ ] LIVE WIRING for CHOP half-sizing (bot.py risk_mult by regime) — needed
      before the paper program reflects the full promoted stack

## Phase 3.5 — Breadth (DONE 2026-07-05; two laws learned)

- [x] Naive breadth REJECTED: EW-23 dilutes (Sh 0.46 vs ~1.0); edge lives in
      liquid trending majors, bleeds in legacy alts (12/23 profitable)
- [x] IS-performance name selection REJECTED: IS-ranked baskets hit only
      75th/43rd/76th pct of random-basket nulls OOS — NEVER pick names by
      backtest ranking; universe choice must be structural
- [x] EW5 equal weighting ADOPTED over SOFT5 weights (least-overfit + better)
- [ ] Structural-universe study: define liquidity/mcap criteria EX-ANTE, test
      the resulting fixed majors basket (BTC/ETH/SOL-class) vs EW5

## Phase 4 — Signal frontier (DONE 2026-07-06)

- [x] Honest walk-forward param re-tune on 4h — **REJECTED**: stitched WF
      (27-combo grid, trailing 365d, 90d refits, zero look-ahead) Sh 1.15
      vs FIXED (9,26,55) 1.36; params churn at ~70% of refits. Frozen params
      confirmed honestly (wf_retune4h.py)
- [x] 1d arm + 4h/1d ensemble — **REJECTED**: D1 Sh 0.69–0.83, corr to 4h
      book +0.6; both blends dilute (1.25/1.33 vs 1.36). The 4h clock
      dominates (arm_1d.py)
- [x] Entry family bake-off — donchian (OOS-negative), EMA/ST/MACD/BB all
      fail to beat triple as replacements; **pullback-in-trend ADOPTED via
      blend** — G3 98th pct, corr 0.17, MDD −2.1%; promoted stack
      BLEND50_CONF Sh 1.47 / MDD −4.8% (entry_families.py,
      pullback_validation.py, phase4_promote.py)
- [x] Universe expansion — reframed structurally (the "20 viable names" were
      performance-picked = illegal): MAJORS12/16 by ex-ante liquidity
      **dilute monotonically** (1.10/0.93 vs MAJORS8 1.36). Cutoff stays 8
      (structural_breadth.py)
- [x] Regime layer upgrades — **CONF sizing ADOPTED** (posterior-confidence
      risk mult, better on every metric in two runs); BTC-pooled clock
      REJECTED (1.06); finer features deferred (regime_upgrades.py)
- [x] LIVE WIRING for the promoted stack (2026-07-06): `pullback_bidir_4h`
      preset (pullback_in_trend now in core.strategies), REGIME_CONF_SIZING
      (posterior of the full-history fit — same approximation as the live
      label), paper compose = 8 names x 2 legs @ 50/50 + CONF; exec parity
      green on the pullback leg (0 unexpected regions, ETH/SOL to the cent)

## Phase 5 — Capital efficiency (how real systems reach 30–70%)

- [x] Vol-targeted position sizing — ADOPTED (G4 all books) and LIVE-WIRED
      (`VOL_TARGET_ANN`, bot.vol_target_mult, unit-tested; paper compose 0.60)
- [x] Sleeves prototyped + gated: TSMOM-90 (Sh 0.67, OOS 0.79) and funding
      carry (Sh 1.18, OOS 1.51, corr to trend −0.01); paper forward-tracking
      via sleeves_paper.py (deterministic, lagged signals, anchor-stamped)
- [x] 3-sleeve assembly: Sh(mo) 1.55, IS 1.36 → OOS 2.01 (full_report.py:
      @25% vol ≈ 42% CAGR, month-end MDD −6.0%; NOTE month-end MDD
      understates intra-month — daily-granularity MDD study pending)
- [ ] Order-placing executors for the sleeves (paper first, then keys):
      weekly carry rebalancer + daily TSMOM checker
- [x] Longer-history sleeve validation — **FAILED**: TSMOM-90 pre-2023-08
      Sh −0.70, carry −0.34 (both edges concentrated in 2024–2026).
      Sleeves are demoted to "prove it forward" status; leverage on the
      3-sleeve book is BLOCKED by our own gates.
- [x] Weight-scheme sensitivity: IV/EQ/CAP40 within ~0.2 Sh; IV best on
      Sharpe AND dMDD — concentration not fragile per this data
- [x] Daily-granularity path: @25% vol dMDD −13.4% (month-end said −6.0%),
      @50% dMDD −25.8% / worst day −20.9% / x14.4 gross — tail-risk math
      says 50% vol is liquidation-adjacent on a fresh outlier day
- [x] Sleeve redesign round — **FAILED** (2026-07-06): dispersion-gated
      carry, strength-masked TSMOM and calm-filtered TSMOM all fail the
      full-history gate (best: CARRY_GATED full +0.26 / pre −0.40, and it
      kills 2025). Diagnosis: carry's edge is a thin-dispersion 2024+
      phenomenon (corr to lagged dispersion −0.27 — hypothesis refuted);
      TSMOM earns in big-move months (corr +0.25 to |BTC|) so vol-filtering
      trims the wrong months; 2021 universe was half-thin (13/23 names).
      No exploitable structure in the bleed → sleeves stay prove-it-forward,
      leverage stays blocked (sleeve_diagnosis.py)
- [x] Assembly v2 with the promoted trend book (2026-07-06): the deployable
      BLEND50_CONF book runs at ~7% ann vol unweighted → @25% vol is only
      x3.5 gross: 37.9% CAGR / Sh 1.48 / dMDD −15.1% / worst mo −5.8% /
      OOS 1.49 (deploy_report.py; percentages are deposit-invariant, run at
      the then-current deposit; assemble_v2's own
      x3.6 run was 37.4%/−13.4%). @50% ≈ x6.9, 78.8% CAGR, dMDD −28.9% (vs the old
      sleeve-combo needing x14.4 for similar CAGR). 3-sleeve v2 info-only:
      @25% 42.5% CAGR / Sh 1.50 / OOS 1.91 (assemble_v2.py)
- [ ] Only after forward paper evidence: sizing-up discussion. The
      trend book (fully gated, now BLEND50_CONF Sh ~1.5) is the only
      component currently eligible for leverage talk; the vol dial (x2-x4)
      is where the 20-40% CAGR lives once the paper record confirms.

## Phase 6 — Go-live program (real margin; NO RUSH — every stage gates the next)

The user's standing decision (2026-07-06): live deploy happens on real margin
when — and only when — every criterion below is green. Files are prepared and
versioned in advance: docker-compose.bidir4h-live.yml (MODE=live, DO NOT
START header) mirrors the paper compose + TP_LIMIT_ORDERS.

Gate status at program start:
- G1–G5 all green on the promoted BLEND50_CONF stack (incl. exec parity on
  the pullback leg).
- **Long-history gate: PASS** (trend_longhist.py): blend full +1.20, pre-
  2023-08 +0.18, post +1.68, MDD −6.0% at unit weights on the expanding
  book. Decomposition: TRIPLE leg pre **+0.57** (all-weather core); PULL leg
  pre **−2.57** (2024+-concentrated — same disease as the sleeves, small
  2022–23 sample). The blend passes because triple carries the bad era.

### Green-light criteria — LIVE-FIRST track (user decision 2026-07-06;
### supersedes the paper-first P1–P4 sequence)

Rationale: at UNIT weights the book's long-history month-end MDD is −6.0%
(daily common-window dMDD −4.5%), worst month −1.7% (≈$108 / ≈$31 on the
current $1,800) — a price worth paying for real fills, which
paper mode cannot produce (it simulates them). What live-first does NOT do
is shorten the calendar: the weeks still pass before any vol dial.

Execution readiness (done before L1): maker entries (ENTRY_LIMIT_ORDERS,
post-only at signal close, one-bar rest, partials closed not adopted,
fill-bar TP suppressed — test_maker_entries.py, 9 cases) + TP limit orders
(TP_LIMIT_ORDERS) + CONF sizing, all wired in the live compose. Splits:
all 16 legs EQUAL at $112.50 ($1,800 / 16; $900 per account). Equal weights
match the validated book — the earlier BTC-premium slice would be ~33% of
the book at this deposit. Known friction: BTC's 0.001 lot (≈$64) sits near
the risk-scaled size → under-sized in normal vol, skips in high vol (L1
measures the skip rate).

- [x] **L0 — Account isolation — DONE** (verified read-only 2026-08-03): sub
      `roflbot_pullback` uid 574595575 exists (created ~2026-07-07, UTA,
      status normal) and holds $999.49 in UNIFIED. ⚠ Two things still open
      before L1: main's capital sits in the **FUND** wallet ($797.65) with
      **$0.00003 in UNIFIED**, so the 8 `-t` legs would start with no margin;
      and the resulting split is 797.65/999.49, not the planned 900/900.
      Rebalance to $898.57 each (or re-derive per-account
      `LEG4H_LIVE_EQUITY`) before starting. Original spec below.
- [ ] ~~L0 — Account isolation (one-time, before L1)~~: the -t and -p legs
      trade the SAME symbols; on one account they would net against each
      other and trip every reconcile guard (Pattern A). Create a Bybit
      SUB-ACCOUNT for the pullback book, transfer **$900.00**
      (8 `-p` legs × $112.50; the deposit is $1,800.00 total), cut an
      IP-whitelisted trade-only key, export PULL_API_KEY/PULL_API_SECRET.
      tg-control reads both accounts (API_KEY2 = sub key) and aggregates
      equity/positions in one panel. Full runbook: deploy/LIVE.md.
- [ ] **L1 — Live shakedown (≥2 weeks, full $1,800.00 at UNIT weights)**:
      week-1 checklist — post-only entries accepted (postonly-reject rate
      logged), first maker entry fill, first TP-limit fill, Partial/Limit
      attach accepted on all 8 symbols, reduce-only closes, sl-external
      reconcile, restart-resume with a resting order, min-notional skips
      rare, and LEG ISOLATION verified: a -t and a -p position coexisting
      on the same symbol (opposite sides included) with zero reconcile
      conflicts — possible only because of L0. Any Bybit rejection of an order type → flip that flag to "0"
      (documented fallbacks in the compose) and note the economics delta.
- [ ] **L2 — Measurement (2–4 more weeks, same size)**: weekly reconcile of
      live decisions vs fixed-engine expectations; measured entry-fill rate
      vs the engine's strict-penetration assumption; measured slippage/fees/
      funding fed back into the cost model. If measured costs degrade the
      edge >0.2 Sh, HALT and re-price.
- [ ] **L3 — Vol dial**: only after L1+L2 green. Sizing anchored on
      FULL-HISTORY Sh ~1.2 (not post-2023 1.5): first the 15% dial (x2.1,
      expect ~15–20%/y honest, dMDD ~−10%); the 25% dial (x3.6) is a
      SEPARATE decision after ≥1 quarter of live record matching the model.

### Standing kill / demotion criteria (pre-registered NOW, not on the day)

- Book-level: at UNIT weights (L1/L2) a drawdown from deploy exceeding −8%
  (≈$144 on $1,800, ~1.3× the −6% long-history MDD) → halt new entries, full review. At the 15%
  dial (L3) the halt line is −15%.
- PULL leg: if its live+paper Sharpe over the trailing 3 months is < 0 →
  demote to BLEND75 (pre-registered fallback) or triple-only; the leg's
  pre-2023 record earns it zero benefit of the doubt.
- Parity: any UNEXPECTED exec divergence (not flip-cascade/entry-skip/
  cooldown classified) → halt, diagnose, fix, re-run test_exec_parity.py
  before resuming.
- Ops (unchanged laws): trading key only on the single IP-whitelisted live
  box (EC2 → migrating to Oracle Cloud A1), trade-only
  permissions; closes reduce-only; never two live portfolios at once; the
  paper stack may keep running (holds no keys).

### While the clock runs

- sleeves_paper.py daily + weekly carry rebalance tracking (their only path
  back in is this forward record).
- Frontier research continues (third orthogonal leg, regime features) — but
  NOTHING joins the live book without the full gate battery + its own paper
  period.

## Moonshot program (the fun budget — firewalled from the main book)

Standing rules (user mandate 2026-07-08: "drop the hesitations… of course we
do our precautions"):
1. **FIREWALL**: moonshot experiments never touch the main accounts or the
   main book's capital. If anything ever goes live it gets its own
   sub-account, capped at ≤10% of the book — and only after the FULL gate
   battery. Same law as everything else.
2. **Heartbeat bar** (pre-registered): survive a 22bp taker round trip with
   t ≥ 2 and year-stability. A heartbeat is a hunting license (a full
   engine-level pre-registered study follows), never an edge.
3. Multiple-testing honesty: ~15 tests per battery ⇒ expect ~1 false
   positive at t ≥ 2; survivors must reconfirm on NEW data.
4. Expect ~90% deaths. That is the deal.

**Tier-1 battery (bar data) — RUN 2026-07-08: 0/6 alive**
(moonshot_heartbeats.py): 15m crash-fade (gross ≈ −4bp — panic bars KEEP
falling), funding-settlement fade (gross ≈ 0 — priced in), 1h vol-squeeze
breakout (closest: net +22.7bp @24h, 100% yrs+, beat the random null, but
t=1.11 → dead by pre-registration; eligible for ONE re-test as new data
accrues), BTC→alts lead-lag 15m/1h (gross +3–8bp REAL but 3–7× below the
cost floor — the textbook "signal exists, cost moat decides who eats it"),
1d cross-sectional reversal (gross ≈ +4bp). The lesson tier-1 bought: on
public bars at retail costs, there is no fast free lunch — as expected.

**Tier-2 (needs tick data — collector.py feeds these; start after ≥60 days
of collection)**: real liquidation-cascade fades (raw liq prints: cluster
size/side/terminal-print timing), funding-settlement microstructure
(seconds around the 8h clock), order-book-imbalance scalps, passive
market-making feasibility read (spread capture vs adverse selection at our
fee tier). Collector: docker-compose.collector.yml — public websockets
only, ~5–10 MB/day gzipped, run it on the live box alongside the live stack
(START IT EARLY; tick history cannot be backfilled).

**Tier-3 (needs external feeds)**: token-unlock calendar fades, listing
events, news momentum.

**The honest path to the "5%/month median" dream**: median ≈ 5%/mo at a
survivable dial needs a book Sharpe ~2.5–3. Route: stack ORTHOGONAL sleeves
(third leg, tier-2 event machines, sleeves earning back in) on the trend
book until the dial reaches it — not a single magic aggressive strategy.

## Sharpe-stacking round 1 (2026-07-09) — book 1.5 → honest ~1.7

Battery of 9 pre-registered candidates (sleeve law; user mandate: "complete
freedom, let's get to sleeving"). Two seats, six deaths (FINDINGS has both
tables), one literature save:

- **XSMOM-21** (21d residual-vs-BTC momentum, weekly, QUAL23): full +1.00,
  **pre-2023-08 +0.85** — first crypto sleeve positive through 2021+2022.
  Corr to book 0.18. CAVEAT: decaying toward the present (last third +0.14,
  2026 −0.7) — forward record adjudicates.
- **MOP-TSMOM commodities** (canonical 12m spec on GC/SI/CL/BZ — Bybit now
  lists all four as perps): **25-year gate PASS** (+0.53 every era), corr
  to book −0.21. Our crypto stack on the same data FAILED (+0.06) — the
  admissible-test budget on commodities is spent.
- **Assembly v3**: BOOK+XSMOM IV → **Sh 1.74, IS 1.73 → OOS 1.81, dMDD
  −1.8%**; recommended deployable BOOK60/XSMOM40 → 1.69 (OOS 1.74) —
  capping the new sleeve below IV weight (the carry lesson, preemptive).
  3-way IV prints 1.82 but IS 2.44 → OOS 1.03 (xsmom-heavy) — not
  proposed. MOPTF joins only vol-normalized ~10% after its venue stage.

## Sharpe-stacking round 2 (2026-07-09) — honest ~1.8, OOS 2.0 touched

Six fresh pre-registered dimensions; 1 seat, 5 deaths (FINDINGS):

- **XSBAB-60** (betting against beta, weekly quintiles): full +0.74, pre
  +0.70, post +0.77 — era-symmetric, 2022 POSITIVE, corr to book −0.03 and
  to XSMOM −0.02. The two cross-sectional sleeves peak in different eras.
- Deaths: XSVOL-21 (+0.49, one hair short — no appeals), BREADTH-LF/LS
  (the book's own exposure in a trenchcoat: corr 0.50/0.82), FNG-CONTRA
  (−0.05: the brake is not an engine), DOMTREND-90 (ETHBTC family closed
  both directions).
- **Assembly v4 deployable: BOOK50/XSMOM25/XSBAB25 — Sh 1.80, IS 1.63 →
  OOS 2.01, dMDD −2.1%, thirds +2.65/+1.15/+2.07**; @15% vol ≈ 28.5%
  CAGR / dMDD −8.4%. Sleeve-heavy IV/CAP40 print 1.87–1.91 but hold only
  14–17% book — not proposed (carry lesson). Cumulative scoreboard: book
  1.53 → +XSMOM 1.74 → +XSBAB **1.80** with the book still majority.

Next steps (nothing touches capital before these):
- [ ] XS paper executor covering BOTH cross-sectional sleeves (XSMOM-21 +
      XSBAB-60, same weekly Monday cadence, sleeves_paper.py pattern) +
      ≥8 weeks of forward record — XSMOM's decay question is the headline
- [ ] MOPTF: vol-normalize, then paper/min-size on the real Bybit contracts
      (XAUUSDT/XAGUSDT/CLUSDT/BZUSDT — 4h funding intervals, synthetic
      weekend pricing; measure both)
- [ ] Re-assembly with forward data; only then a BOOK50/XS25/BAB25 capital
      discussion. L1/L2 of the main book proceed unchanged in parallel.
- [ ] Round 3 candidate pool (pre-register before running): tier-2 tick
      studies at the 60-day collector mark; XS carry-momentum interaction
      ONLY if the funding-family budget is formally re-opened; nothing else
      currently clears the 10%-prior bar.

## Scoreboard (fixed engine, full costs, SOFT5 unless noted)

| date | config | CAGR | Sh(mo) | MDD | gates | status |
|---|---|---|---|---|---|---|
| 2026-07-05 | 1h production (old live) | 0.6% | 0.22 | −20% | — | retired |
| 2026-07-05 | 4h + tp6 (r3 cost model) | 15.1% | 0.98 | −9.6% | G1–G4 ✓ | superseded by ALL_IN |
| 2026-07-05 | ALL_IN: 4h + tp6, entry-bar check, real funding, maker entries | 13.2% | 0.88 | −10.3% | G1–G4 ✓ (OOS Sh 1.14) | superseded |
| 2026-07-05 | EW5 / RSCD3: ALL_IN + CHOP half-size + SL cooldown K=3, equal weights | 15.4% | 1.11 | −8.6% | IS 1.07 → OOS 1.27 | superseded |
| 2026-07-05 | EW5 / RSCD3 + **vol targeting** | 20.6% | 1.41 | −9.6% | IS 1.37 → OOS 1.54; G4 ✓ all books | adopted layer |
| 2026-07-05 | **MAJORS8 / RSCD3+VT** (ex-ante liquidity top-8) | 20.0% | 1.42 | −8.0% | IS 1.38 → OOS 1.41 | **trend-sleeve BASELINE** |
| 2026-07-05 | **3-SLEEVE PORTFOLIO** (trend + TSMOM-90 + carry, inverse-vol) | vol-dial | **1.55** | mo −0.8% @ unit wts | IS 1.36 → **OOS 2.01**, thirds +1.54/+1.38/+1.82 | **PORTFOLIO TARGET — needs carry/TSMOM execution engineering + paper** |
| 2026-07-06 | **TRIPLE+PULL BLEND50 + CONF sizing** (Phase-4 promotion) | 10.5% @ unit wts (vol-dial) | **1.47** | −4.8% | PULL G3 98th pct; IS 1.53 → OOS 1.43; thirds +2.40/+0.79/+1.46 | superseded by +TP-limit below |
| 2026-07-06 | **BLEND50_CONF + TP-as-limit @ 25% vol (x3.5)** — deploy_report.py (origin assembly v2), daily granularity | **37.9%** | **1.48** | dMDD **−15.1%**, worst mo −5.8% | IS 1.44 → OOS 1.49 | **DEPLOYED-TO-PAPER CONFIG** (compose = 8 names × 2 legs + CONF; note paper runs UNlevered unit weights — the dial is a sizing decision, not bot logic) |
