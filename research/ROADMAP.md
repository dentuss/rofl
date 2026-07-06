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
      BLEND50_CONF book runs at 7% ann vol unweighted → @25% vol is only
      x3.6 gross: 37.4% CAGR / Sh 1.49 / dMDD −13.4% / worst day −5.1% /
      OOS 1.52. @50% = x7.2 gross, 77.5% CAGR, dMDD −26% (vs the old
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

### Green-light criteria (ALL required, in order)

- [ ] **P1 — Paper record**: ≥4 weeks on docker-compose.bidir4h-paper.yml.
      Weekly reconcile vs fixed-engine expectations: trade counts, entry
      prices vs signal closes, exit reasons. Zero unexplained divergences.
- [ ] **P2 — Stage A (mechanics, ~2 weeks)**: live compose with
      LEG4H_LIVE_EQUITY≈15 (near exchange minimums; total at risk ≈ $240).
      Purpose is ORDER MECHANICS, not P&L: first TP_LIMIT_ORDERS fill
      verified on Bybit (Partial/Limit attach accepted on all 8 symbols),
      reduce-only closes, sl-external reconcile, restart-resume, no
      min-notional rejects at this size. Any Bybit rejection of the limit-TP
      attach → set TP_LIMIT_ORDERS=0 (falls back to conditional market) and
      re-run tp_limit.py economics before proceeding.
- [ ] **P3 — Stage B (25% size, 2–4 weeks)**: LEG4H_LIVE_EQUITY = target/64.
      Compare live fills vs paper vs engine: entry slippage vs signal close,
      TP maker-fill rate, funding paid. Feed measured slippage back into the
      cost model (ROADMAP Phase 2 open item) — if measured costs degrade the
      edge >0.2 Sh, STOP and re-price.
- [ ] **P4 — Stage C (full size)**: only after P3 numbers match the model.
      Sizing anchored on FULL-HISTORY Sh ~1.2 (not post-2023 1.5): start at
      the 15% vol dial (x2.1 gross, expect ~15–20%/y honest, dMDD ~−10%);
      the 25% dial (x3.6) is a SEPARATE later decision after ≥1 quarter of
      live record matching expectations.

### Standing kill / demotion criteria (pre-registered NOW, not on the day)

- Book-level: live drawdown from deploy exceeding −15% → halt new entries,
  full review before restart (at the 15% dial this is ~2x the backtest dMDD).
- PULL leg: if its live+paper Sharpe over the trailing 3 months is < 0 →
  demote to BLEND75 (pre-registered fallback) or triple-only; the leg's
  pre-2023 record earns it zero benefit of the doubt.
- Parity: any UNEXPECTED exec divergence (not flip-cascade/entry-skip/
  cooldown classified) → halt, diagnose, fix, re-run test_exec_parity.py
  before resuming.
- Ops (unchanged laws): trading key only on EC2, IP-whitelisted, trade-only
  permissions; closes reduce-only; never two live portfolios at once; the
  paper stack may keep running (holds no keys).

### While the clock runs

- sleeves_paper.py daily + weekly carry rebalance tracking (their only path
  back in is this forward record).
- Frontier research continues (third orthogonal leg, regime features) — but
  NOTHING joins the live book without the full gate battery + its own paper
  period.

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
| 2026-07-06 | **BLEND50_CONF + TP-as-limit @ 25% vol (x3.6)** — assembly v2, daily granularity | **37.4%** | **1.49** | dMDD **−13.4%**, worst day −5.1% | IS 1.43 → OOS 1.52 | **DEPLOYED-TO-PAPER CONFIG** (compose = 8 names × 2 legs + CONF; note paper runs UNlevered unit weights — the dial is a sizing decision, not bot logic) |
