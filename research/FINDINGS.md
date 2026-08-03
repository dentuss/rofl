# Research findings — what worked, what didn't

A running log of experiments so we don't re-litigate dead ends. All on
5y (or max-available) 1h data, production preset = triple_bidir +
directional regime filter + F&G extreme filter + three-tier decay, funding
modeled. Scripts that produced each result are named.

## ⚠ 2026-07-05 — CRITICAL CORRECTION: same-bar re-entry artifact

**Every absolute backtest number in this file produced before 2026-07-05 is
invalid.** Both engines allowed closing a position intra-bar (SL/TP/time
exit) and opening a new one at that SAME bar's OPEN — a fill that
chronologically precedes the exit. In trends, the "sell at TP, re-buy at the
earlier, lower open" loop manufactured the edge: on SOFT5/Bybit/2.88y, 2,538
same-bar post-TP re-entries (41% of all trades) carried a mean **+1.34%
physically impossible fill advantage** and accounted for essentially ALL of
the backtested return (portfolio CAGR 212% → 0.6% with the artifact removed;
raw-ADA sanity check: final 520,105 → 43). bot.py is bar-close gated and
cannot make these trades — **live never had this edge**.

Fix (this date): both engines now block same-bar entries after SL/TP/time
exits by default. `legacy_same_bar_reentry=True` reproduces pre-fix numbers
(sanity: reproduces the adopted cooldown wrapper to delta 0); signal-flip
reversals still fill at the open, which is time-consistent. Also corrected:
`test_exec_parity`'s replay used to replicate the artifact AND thread the
fill-bar ts into the cooldown gate, masking a real off-by-one — the live gate
compares the SIGNAL bar's ts, so live `COOLDOWN_BARS=3` ≡ fixed-engine
`cooldown_bars=4`.

Corrected SOFT5 @ $2300, Bybit 2.88y, full production stack
(`research/tp_cooldown_htf_bias.py`):

| variant | CAGR% | Sharpe(mo) | MDD% | worst mo % | pos mo % |
|---|---|---|---|---|---|
| LEGACY (pre-fix illusion) | 212.4 | 3.87 | −17.4 | −6.4 | 91 |
| FIXED_K3 (adopted config, honest) | −1.8 | −0.10 | −17.0 | −5.0 | 43 |
| **FIXED_K4 (live-gate semantics, honest)** | **0.6** | **0.22** | **−20.3** | **−7.6** | **51** |

**Conclusion: the production system has NO measurable edge once fills are
realistic** — ~0% CAGR against a −20% max drawdown. Relative comparisons
between pre-correction variants may retain some ordinal information (all
shared the artifact), but nothing absolute survives, and any experiment whose
treatment changed re-entry behavior (the SL-cooldown lift, TP chunking) is
contaminated in magnitude too.

## ⚠ 2026-08-03 — HEADLINE NUMBERS ARE STALE: the book had a bad July 2026

`research/deploy_report.py` (canonical, unmodified) re-run 2026-08-03 on the
Linux box does **not** reproduce the numbers quoted in CLAUDE.md and
SESSIONHANDOFF. The cause is not an engine or version artifact — it is that
**July 2026 closed at −3.64% on the blend (−5.61% on the triple leg), the
worst month in the entire common window.** The docs were written 2026-07-30,
mid-month, before that month closed.

| metric (unit weights) | documented | 2026-08-03 | delta |
|---|---|---|---|
| CAGR% | 10.4 | **9.5** | −0.9 |
| Sh(mo) | 1.50 | **1.33** | −0.16 |
| dMDD% | −4.5 | **−5.6** | −1.1 |
| worst month% | −1.7 | **−3.6** | −1.9 |

Checked and excluded as causes: the GMM **is** seeded (`random_state=42`,
`n_init=3`, core/regime.py:79), so this is not sampling noise; and the
trailing partial month (Aug 1–3 entering the monthly series as a full
observation via `resample("ME")`) is worth only **0.01 Sh** (1.33 → 1.34 with
incomplete months dropped) — a real but immaterial harness wart, logged here
rather than fixed. Verified on pandas 3.0.5 / scikit-learn 1.9.0 / ccxt
4.5.70, all 10 test suites green.

**Consequences.** (1) Go-live sizing conversations must quote 9.5% / 1.33 /
−5.6%, not 10.4 / 1.50 / −4.5. (2) The L1 halt line (−8% ≈ −$144) is
unchanged but the margin has halved: worst month is now ≈**−$66** at $1,800,
not ≈$31. (3) The full-history anchor of Sh ~1.2 is *better* calibrated than
it looked — the common-window premium shrank from +0.30 to +0.13.

## ⚠ 2026-08-03 — PULL leg deteriorating; the demotion trigger is UNDER-POWERED

Leg decomposition of the deployed blend on the same window
(`research/book_recheck.py`):

| leg | full Sh | last 12mo | last 6mo | last 3mo |
|---|---|---|---|---|
| TRIPLE (`-t`) | +1.27 | +1.22 | +1.26 | +1.20 |
| **PULL (`-p`)** | +1.14 | **+0.57** | **−1.08** | **−3.93** |
| BLEND50 | +1.34 | +1.19 | +1.14 | +1.00 |

The triple leg is stable across every horizon; all of the blend's recent
weakness is PULL. The pre-registered trigger ("trailing-3-month Sharpe < 0 →
BLEND75 or triple-only") reads −3.93 on this backtest analogue — i.e. it
would fire.

**But it would be firing on noise, and that is the actual finding.** PULL's
last six monthly returns are 0.00, 0.00, +0.46, −0.70, 0.00, −0.56: the leg
fires ~once per 6 weeks per name, so **three of the last six months contain no
trades at all** and the −3.93 is computed from essentially two nonzero
observations. A 3-month Sharpe has no power to distinguish the pre-2023 −2.57
disease from an ordinary quiet stretch in *either* direction.

**Recommendation (not adopted — needs the human's call): re-specify the
trigger on trade count or cumulative R before L1, not after.** As written it
will fire spuriously inside the first live quarter and force an unjustified
book change. The trigger is defined on the FORWARD live+paper record, which
does not exist yet, so nothing is firing today.

## Adopted (live in production)

> ⚠ The performance numbers in this table predate the 2026-07-05 same-bar
> re-entry correction and are inflated — see the correction section above.
> The configs are as listed; the edge claims do not stand.

| Change | Effect | Script |
|---|---|---|
| Bidirectional strategy (`triple_bidir`) | +73%→+146% CAGR on INJ; shorts profitable on all 7 pairs | test_short_adapt.py |
| Directional regime filter | long in BULL/CHOP, short in BEAR/CHOP — avoids countertrend | test_improvements.py |
| F&G extreme-zone filter (≥80/≤20) | MDD −33%→−28%, return flat | test_improvements.py |
| **F&G 3-day persistence** (superseded single-day) | only block ENTRENCHED extremes; flash-extreme continuation shorts are profitable. INJ +25pp CAGR / +0.14 Sharpe, portfolio +24pp CAGR / +0.23 Sharpe, same MDD | test_fng_soften.py |
| Three-tier decay (−20/−35/−50%) | free on healthy pairs; LTC MDD −69%→−51% AND higher final | test_decay_funding.py, decay on LTC/BTC |
| Multi-pair portfolio (inj_heavy) | MDD −28%→−18%, Sharpe 1.75→1.95, worst-month −16%→−7% | multipair_bidir.py |
| scikit-learn bundled | adaptive presets actually run the regime GMM | — |
| **Post-stop same-side re-entry cooldown** (K=3 bars; env `COOLDOWN_BARS`) | After a SL on side X, block new side-X entries for 3 bars — kills the post-stop V-bounce whipsaw. Full prod stack, 5 pairs, 3.7y: portfolio Sharpe **2.27→3.46**, worst month **−8.6%→−2.6%**, MDD −19.8%→−17.2%, beats baseline 41/44 months; OOS (held-out 40%) ΔSharpe +1.10. Placebo control confirms it is post-SL-same-side-specific. Implemented in bot.py + both backtest engines; exec-parity locked (live==backtest to the cent). Restart-robust since 2026-07-02 (an SL firing while the bot is down arms the cooldown from the real fill's timestamp on resume). | test_reentry_cooldown_prod.py, cooldown_report.py |
| **SOFT5 weighting** (INJ 25 / SOL, ADA, ETH, LINK 18.75 — adopted 2026-07-02, supersedes inj_heavy) | Caps the INJ-40% single-name concentration the backtest rewards but can't risk-price. Bybit-perp (execution venue), 2.87y, K=3, honest MONTHLY Sharpe: SOFT5 3.81 vs inj_heavy 3.58 vs 8p 3.49; OOS holdout ~parity with inj_heavy (3.01 vs 3.09) — the insurance is ~free on risk-adjusted terms (costs ~9% CAGR, worst month −3.3%→−6.7%). Both 5p books beat EVERY random basket OOS; a 3-lens decision panel was unanimous for SOFT5. | portfolio_softened.py, portfolio_robustness.py |

## Adopted — HONEST-ERA research baseline (2026-07-05; paper program, NOT live)

All on the FIXED engine with the full cost model (entry-bar exit check, maker
entries at 0.02%, real per-pair funding, 2 bps slip on taker legs).

| Change | Effect (SOFT5-family, Bybit 4h, 2.88y) | Script |
|---|---|---|
| **4h port + tp_mult 6.0** (ALL_IN cost model) | The validated core: SOFT5 13.3% CAGR / Sh(mo) 0.89 / MDD −10.3 / OOS Sh 1.16. Passed universe, sub-window, random-entry-null gates. | honest_rebuild*, cost_engine.py |
| **CHOP half-sizing (risk ×0.5 in CHOP)** | Graveyard survivor (old "−36% return" verdict was the artifact): +0.08–0.12 Sh on every book, ~free. | graveyard_retrial.py, breadth_select.py |
| **SL cooldown K=3 engine bars** (≡ live `COOLDOWN_BARS=2` — signal-bar gate is 1 stricter) | With half-sizing: better on EVERY metric on every book tested. | graveyard_retrial.py, baseline_promote.py |
| **EW5 equal weighting** (supersedes SOFT5 25/18.75) | CAGR 15.4%, Sh(mo) 1.11, MDD −8.6%, worst mo −4.7%, IS 1.07 → OOS 1.27. Equal weight is also the least-overfit choice. (EW4-ex-INJ better still but = performance selection; deferred.) | baseline_promote.py |
| **Vol-targeted sizing** (30d trailing vol → risk_mult clip[0.5,1.5], 60% ann target; single pre-registered parameterization) | **+0.30 Sh / +5.2pp CAGR on EW5** (1.11→1.41, 15.4→20.6%); G4 PASSED — improves ALL four books (EW10 1.04→1.36, MAJORS8 1.17→1.42, EW23 0.58→0.86); IS and OOS both improve everywhere. | vol_target.py, structural_universe.py |
| **MAJORS8 structural book** (top-8 by median daily $vol, ex-ante: BTC ETH SOL XRP DOGE ADA LINK AVAX) | With RSCD3+VT: **CAGR 20.0%, Sh(mo) 1.42, MDD −8.0%, worst mo −3.5%, IS 1.38 → OOS 1.41** — ties EW5+VT with better tails/stability, zero selection bias, and BTC/ETH capacity. The legal version of "trade the majors". | structural_universe.py |
| **TSMOM-90 sleeve** (1d sign-of-90d-return, 23 names, inverse-vol, 8bp/turnover + real funding) | Standalone Sh 0.67, IS 0.68 → OOS 0.79, thirds all +, corr to trend 0.25–0.46. k=30 dies OOS (−0.23) — only 90d survives. Modest solo; earns its place in the ensemble. | tsmom_sleeve.py |
| **Funding-carry sleeve** (7d funding rank, weekly long-cheap/short-expensive quintiles, 23 names) | **Sh(mo) 1.18 standalone, IS 1.14 → OOS 1.51, MDD −2.2%, worst mo −0.8%, corr to trend −0.01** — the textbook orthogonal carry stream, harvesting the measured 40× funding dispersion. | carry_sleeve.py |
| **3-SLEEVE ASSEMBLED PORTFOLIO** (trend MAJORS8/RSCD3+VT + TSMOM-90 + carry; agnostic inverse-vol weights — 0.12/0.11/0.76) | **Sh(mo) 1.55, IS 1.36 → OOS 2.01, worst month −0.8%, thirds +1.54/+1.38/+1.82.** Carry-dominated at unit weights → low absolute return; CAGR comes from vol-targeting the combo (leverage): at Sh 1.55, ~25% vol ≈ ~40% CAGR with materially larger tails. Caveats: 35 monthly obs (Sh se ≈ ±0.3); carry sleeve needs new execution machinery (weekly rebalance book). | assemble_portfolio.py |
| **⚠ SLEEVE LONG-HISTORY GATE: FAILED (2026-07-05)** | Extending the sleeves to 2021 with their canonical params (pseudo-OOS — all design done on 2023-08+ data): TSMOM-90 pre-2023-08 Sh **−0.70**, carry **−0.34**; both edges are concentrated in 2024–2026. Daily-granularity path: @25% vol real dMDD −13.4% (vs −6.0% month-end), @50% vol dMDD −25.8%, worst day −20.9%, **x14.4 gross scaling — a fresh ~7% adverse combo day would approach liquidation**. Weight schemes within ~0.2 Sh (IV best). **Verdict: the sleeves have NOT earned leverage. High-vol deployment of the 3-sleeve book is blocked by our own gates until the sleeves prove out in the forward paper record or are redesigned (regime-gated carry/TSMOM). The fully-gated component remains the trend book (Sh 1.42).** *(Redesign round run 2026-07-06: all three pre-registered conditioned variants FAILED the same gate — see Rejected.)* | path_weights_history.py |
| **GMM confidence-weighted sizing (CONF)** (2026-07-06, Phase 4) | risk_mult ×(0.5 + 0.5·p_label) from the walk-forward GMM posterior — pure de-risking of low-conviction bars, zero new fitted constants. Improved every metric in BOTH independent runs (regime_upgrades: Sh 1.36→1.40, MDD −8.0→−7.4, OOS 1.29→1.33; phase4_promote: 1.31→1.33, −8.5→−7.9). Each single improvement is within run-to-run jitter (~0.03–0.05 Sh from bar-boundary data drift between runs) but the direction is consistent across runs and across Sh/MDD/worst/IS/OOS simultaneously. BTC-pooled regimes REJECTED in the same study (per-pair fits are load-bearing). | regime_upgrades.py, phase4_promote.py |
| **Pullback-in-trend family (PULL_T6) + 50/50 blend — NEW TREND-BOOK BASELINE** (2026-07-06) | New entry family (EMA50 side + RSI14 recross of 40/60, sl 1.8/tp 6.0 ATR, event-style, ~22 trades/name/3y, time-in-market 4–8%): standalone Sh(mo) ≈ triple (1.33–1.36) with A QUARTER of the drawdown (MDD −2.1%, worst month −0.6%) and monthly corr to triple only **0.17**. **G3 random-entry null: 98th pct** (real +1.36 vs null median +0.14, 60 matched draws); thirds all positive; 8/8 names profitable on the common window. Promoted stack **BLEND50_CONF** (0.5 triple + 0.5 pull, both with CONF sizing): **Sh(mo) 1.47, IS 1.53 → OOS 1.43, MDD −4.8%, worst month −1.7%, thirds +2.40/+0.79/+1.46** vs triple-only 1.31/−8.5/−3.6. CAGR at unit weights is lower (10.5% vs 17.6%) because PULL deploys rarely — the vol-target layer converts the halved drawdown into leverage capacity, so the blend dominates risk-adjusted. Caveats: PULL leg is 178 trades (small sample; per-name Sh +0.13..+1.04, all positive); BLEND75_CONF (1.38) is the fallback if forward paper shows the PULL leg degrading. LIVE-WIRED 2026-07-06: `pullback_bidir_4h` preset + `REGIME_CONF_SIZING` + 16-service paper compose (8 names × 2 legs, 50/50); exec parity green incl. the pullback leg (G5). | entry_families.py, pullback_validation.py, phase4_promote.py |
| **TP-as-limit (engine `tp_as_limit`)** (2026-07-06, Phase 5) | Exit winners with a resting limit at the target: maker fee (0.02%) instead of taker+slip (0.08%) on every TP leg, honest strict-penetration fills (a touch doesn't fill). Result on the promoted books: **ZERO TP fills lost** (409/409 triple, 42/42 pull — a 6-ATR target that gets touched gets penetrated), triple-book fees −18%, blend CAGR 10.7→11.0, Sh 1.50→1.52. Pure win, adopted into the research stack; unit-tested (test_tp_limit.py). LIVE execution still exits TP via exchange-side conditional (taker) — switching to a reduce-only limit is pending bot execution work. | tp_limit.py |
| **TREND-BOOK LONG-HISTORY GATE: PASS — with a demotion trigger armed** (2026-07-06) | The gate that killed the sleeves, applied to our own promoted book (expanding MAJORS8 book from 2022-03, per-pair data start + 365d warmup; pre-2023-08 = pseudo-OOS incl. the 2022 bear tail): **BLEND50_CONF full +1.20, pre +0.18, post +1.68, MDD −6.0%** at unit weights → PASS (bar: full ≥ 0.5, pre ≥ 0.0). Decomposition is the real finding: **TRIPLE leg pre +0.57** — the core made money through the bear; **PULL leg pre −2.57** — 2024+-concentrated, the sleeves' disease (caveat: 6 names, rare trades in 2022–23). The blend passes because triple carries the bad era. Consequences (adopted): go-live sizing anchors on full-history Sh ~1.2, not post-2023 ~1.5; PULL demotion trigger pre-registered (trailing-3mo live+paper Sh < 0 → BLEND75 or triple-only); yearly blend line 2022 +0.5, 2023 +1.0, 2024 +2.0, 2025 +0.4, 2026 +2.3. | trend_longhist.py |
| **CAPITAL-EFFICIENCY STANDING v2** (2026-07-06, assembly with the promoted trend book) | The deployable book (BLEND50_CONF, full stack + tp_as_limit) runs at just **~7% ann vol at unit weights** — leverage to a vol target is cheap. Canonical dial table (deploy_report.py; all percentages are deposit-invariant — only final-$ scales; the ~0.5pp deltas vs assemble_v2's earlier run are 2-day-window noise): **@15% vol (x2.1): 22.2% CAGR, dMDD −9.2%; @25% (x3.5): 37.9% CAGR, Sh 1.48, dMDD −15.1%, worst mo −5.8%, IS 1.44 → OOS 1.49; @50% (x6.9): 78.8% CAGR, dMDD −28.9%, worst day −10.3%**. The x3.5 gross at 25% is far from liquidation on any historical day. 3-sleeve assembly v2 (info only — sleeves still blocked): IV weights shift toward trend (0.22/0.11/0.66), @25% 42.5% CAGR / Sh 1.50 / OOS 1.91. | deploy_report.py (canonical); assemble_v2.py |

## Rejected (tested, did not clear the bar)

| Idea | Why rejected | Script |
|---|---|---|
| Partial TP + breakeven | catastrophic — strategy needs winners to run (−27%) | test_improvements.py |
| Fresh-crossover gating | kills return; strategy works *because* it rides trends | test_improvements.py |
| Finer regime GMM (4/5 components) | worse than 3 — fragments the data | test_improvements.py |
| ML entry filter (GBM, all variants) | well-calibrated but no discrimination; features already in strategy | test_ml_retry.py |
| Per-regime risk sizing | +0.10 Sharpe but −36% return (defensive only) | test_improvements.py |
| Walk-forward param retune + F&G | overfits annual window; underperforms fixed params | monthly_report.py |
| **Chop filter (CI / efficiency ratio)** | hurts winners (INJ $3780→$1032), no weak-pair benefit; ADX>22 already filters chop | test_chop_health.py |
| **Price-based health filter (90d ER)** | ER structurally ~0 at long lookback, can't separate dead from healthy — blocks ~everything | test_chop_health.py |
| **Strategy-health gate (trailing equity)** | every threshold costs winner return (≥17%) for marginal weak-pair help; decay already handles drawdown | test_health_gate.py |
| **lean_sol_ada weighting** | WORST scheme of 6 (Sharpe 1.99 vs 2.16 inj_heavy); concentrating in recent winners underperforms | portfolio_construction.py |
| **max-Sharpe weight optimization** | in-sample optimum (2.13) didn't even beat the inj_heavy heuristic (2.16) — weight tuning has no edge | portfolio_construction.py |
| **dynamic rebalance (trailing-3mo Sharpe)** | catastrophic performance-chasing: +4% vs +2358% static equal over 4.6y | portfolio_construction.py |
| **Funding rate as signal** | IC ≈ 0 (mean −0.02 Spearman, funding-z vs 8–72h fwd return; weakly mean-reverting but unexploitable). "Fade extreme funding" overlay cut return ~24pp avg / −0.11 Sharpe — blocks profitable trend trades. Real Bybit/OKX funding, 5 pairs, 400d | funding_signal.py |
| **8-pair equal-weight portfolio (go-live, 2026-07-02)** | The KuCoin in-sample edge (hourly Sharpe 3.70 vs 5p 3.37) did NOT survive the honest re-test on Bybit perp with monthly Sharpe + OOS holdout: IS 4.60 → OOS **2.70** (worst decay of any book), worst month −9.6% vs 5p −3.3%, and only 94th pct vs 500 random 8-baskets OOS (selection-driven). Both 5-pair books beat it OOS. ON HOLD — and subsequently voided entirely by the 2026-07-05 same-bar artifact (all 1h-era numbers). Its wiring (compose, launchers) was removed in the 2026-07-08 repo cleanup — resurrect from git history only via a full re-validation on the fixed engine. | portfolio_robustness.py, portfolio_compare.py |
| **Post-TP same-side cooldown (K_tp=2/3)** (2026-07-05, on the FIXED engine) | The live-blockable post-TP re-entries are zero-EV: gap-1 n=1756 meanR **+0.02**, gap-2 n=56 meanR −0.11; chase re-entries actually outperform pullbacks (+0.10R vs −0.06R). Blocking costs a hair (ΔSh −0.25/−0.22 vs FIXED_K4). The 2026-07-03 ADA TP→re-enter→SL round trip was variance, not a systematic leak. Engine knob `cooldown_bars_tp` stays (default 0) for future re-tests. | tp_cooldown_htf_bias.py |
| **HTF risk-size bias** (1D/4h EMA50, counter-trend risk ×0.5/×0.75, via `with_htf_risk_bias`) | Cannot rescue a no-edge base: best cell (1D ×0.5) is ΔSh +0.02 / CAGR +0.3pp over FIXED_K4 (MDD −20.3→−15.2 is the one bright spot); OOS Sh 0.84 vs IS 0.07 is an unstable inversion = noise, and 4h is flat-negative. Re-tried on the honest 4h base (graveyard 2026-07-05): ΔSh −0.32, MDD worse — rejected again. | tp_cooldown_htf_bias.py, graveyard_retrial.py |
| **Naive breadth (EW-23 universe)** (2026-07-05) | Indiscriminate widening DILUTES: EW-23 Sh 0.46 vs SOFT5-family ~1.0; only 12/23 names profitable. Edge is cross-sectionally heterogeneous — lives in liquid trending majors (ETH +1.11, SOL +0.90, BTC +0.77, RUNE, NEAR), bleeds in legacy alts (LTC −1.58, ETC −1.55, FIL −1.38, AAVE −1.09). | breadth_allin.py |
| **IS-performance name selection** (2026-07-05) | The honest version of "pick winning pairs": rank on IS window only, freeze basket, judge OOS vs 300 random same-size subsets → 75th/43rd/76th pct (K=5/8/10). Trailing pair performance does NOT persist enough to select on. Universe choice must be structural (liquidity/class), never backtest ranking. | breadth_select.py |
| **Partial TP + breakeven — re-trial** (2026-07-05) | Rejection upheld honestly: 2.0 ATR −9.6pp CAGR / −0.48 Sh; 3.0 ATR −6.0pp / −0.17. Winners must run (the old "−27% catastrophic" number was artifact-inflated but directionally right). | graveyard_retrial.py |
| **ADX 25 entry gate — re-trial** | −0.13 Sh, −1.6pp CAGR on the honest 4h base. | graveyard_retrial.py |
| Chop filter min_pct=0.003 — re-trial | UNINFORMATIVE on 4h (threshold tuned for 1h never fires — identical results). Needs re-parameterization for a real trial. | graveyard_retrial.py |
| **Alternative entry families as REPLACEMENT** (2026-07-06, Phase 4) | Six families through the IDENTICAL adopted stack (MAJORS8, regime+F&G+chop+VT+cooldown, maker, real funding): donchian breakout Sh 0.46–0.76 with **OOS −0.28/−0.24** at the 20-bar settings (breakout entries die out-of-sample on 4h majors); EMA-trend T6 1.11, supertrend T6 1.19, MACD ≤0.45, BB mean-reversion −0.95 (control, expected conflict with the directional mask). None beats TRIPLE_T6 1.36. triple_bidir is genuinely the best solo generator in the library — but see the PULL blend adoption. | entry_families.py |
| **Structural breadth beyond top-8** (2026-07-06) | Monotone dilution by ex-ante liquidity cutoff: MAJORS8 Sh 1.36 → MAJORS12 1.10 → MAJORS16 0.93 → EW23 0.80 (full window and thirds agree; tails no better). The edge lives in the 8 most liquid names; the structural cutoff stays at 8. | structural_breadth.py |
| **1d arm + 4h/1d ensemble** (2026-07-06) | The same stack on daily bars: D1_T6 Sh 0.69, D1_T9 0.83 (7/8 profitable but weak), monthly corr to the 4h book +0.58/+0.62 — too correlated to diversify. BLEND50 1.25 and BLEND75 1.33 both BELOW 4h-only 1.36. The 4h clock dominates; rejected. | arm_1d.py |
| **BTC-pooled regime clock** (2026-07-06) | One macro GMM (BTC's walk-forward regimes driving all 8 names' mask+chop) vs per-pair fits: Sh 1.06 vs 1.36, worst month −4.9 vs −3.6. Per-pair regime fits are load-bearing; pooling rejected. | regime_upgrades.py |
| **Walk-forward param re-tune on 4h — re-trial** (2026-07-06) | The honest stitched version (27-combo grid scored on trailing 365d, refit every 90d, zero look-ahead): WF Sh 1.15 / 15.3% vs FIXED (9,26,55) 1.36 / 18.6% — worse on every metric, and the param timeline churns at ~70% of refits (LINK's rare stable stretch didn't save the book). Confirms the artifact-era lesson honestly: periodic re-selection overfits the trailing window; params stay frozen. | wf_retune4h.py |
| **Stacking round 2 — the five deaths** (2026-07-09) | Same sleeve law, five fresh dimensions: **XSVOL-21** (dollar-volume/attention momentum): +0.49 — missed the 0.5 bar by a hair, no appeals; **BREADTH-LF/LS** (participation timing of the majors basket): +0.52 but corr to book at the 0.5 boundary and pre +0.34 / LS +0.30 with corr 0.82 — breadth is the book's own exposure in a trenchcoat; **FNG-CONTRA** (entrenched-extreme contrarian as an ENGINE): −0.05 — the F&G brake is not an engine, validating its defensive-only role; **DOMTREND-90** (ETH/BTC dominance trend): −0.01 with pre −0.72 — with MR already dead, the ETHBTC family is now fully closed. | sleeve_battery2.py |
| **Stacking round 1 — the six deaths** (2026-07-09) | Pre-registered battery (sleeve law: full ≥ 0.5, pre-2023-08 ≥ 0.0, \|corr\| ≤ 0.5): **crypto-stack-on-commodities** (our exact 1d triple+pull on GC/SI/CL/BZ, 24y): full **+0.06** — the crypto-tuned fast-trend clock does not carry to TradFi (2015-19 trend winter −0.71); **CHOP-MR** (BB fades only in CHOP, MAJORS8 4h): **−1.24**, negative every year — the CHOP label means "no directional edge", not "fade edge"; its information is fully spent as a sizing signal; **ETHBTC-MR** (4h ratio z-score): −0.79; **XSMOM-14**: +0.46 (k=21 sibling passed — see Under Validation); **CORE-MA200** BTC+ETH (long-flat/long-short): +0.50/+0.56 borderline but pre weak (+0.31/+0.41) and LS corr to book 0.57 — also largely embedded in the trend book already; **calendar** (turn-of-month +0.23, weekend +0.44 with pre −0.13): too weak. | sleeve_battery.py, tradfi_sleeve.py, chop_mr_sleeve.py |
| **Time stop `max_bars_in_trade` relaxation (TIMESTOP-4H)** (2026-08-03) | Audit of an INHERITED default, not a new overlay: `max_bars_in_trade=96` is dated by its own comment ("96 bars = 24h on 15m, 4d on 1h", core/backtest.py:26) and was never re-examined on 4h, where it forces exit at **16 days**. It is live in both paths (engine core/backtest_enhanced.py:172; bot.py:1068 via `MAX_BARS`, set in NO compose → live inherits 96), and the preset tuple carries no `max_bars`, so parity holds and every honest-era number was measured with it. Pre-registered prior: "winners must run" (partial-TP rejected twice, TP widening monotone) predicts relaxing helps. **The prior was WRONG.** Monotone ladder T96/T180/T360/TOFF: Sh(mo) 1.33 / **1.38** / 1.33 / 1.32, CAGR 9.5 / 9.4 / 9.0 / **8.9**. Best cell T180 cleared bars (b) IS+OOS both up, (c) thirds all +, (d) dMDD −4.8 vs −5.6 — but **failed (a) ΔSh ≥ +0.10 at only +0.05**, inside the documented 0.03–0.05 run-to-run jitter band. INCONCLUSIVE → the inherited 96 stands. Mechanism (the real finding): time-exits are 5.5% of trades at mean **+1.07R** — *profitable*, not bleeding — and letting them run is slightly worse. A position that has meandered 16 days without touching a 1.8-ATR stop or a 6-ATR target is a different population (chop) than a running winner; "winners must run" has a boundary and the inherited 96 sits near it by accident. Footnote for a future re-trial: dMDD is −4.8 for T180/T360/TOFF *identically* vs −5.6 for T96, which is not noise-shaped and suggests the 16d cap deepens one specific drawdown episode. G3 N/A (no entry logic touched); G4/G5 not run — nothing here is promotable. | timestop_4h.py |
| **`bot.py` signal-flip parity claim is STALE** (2026-08-03) | bot.py:1046 documents a deliberate engine-vs-live divergence (backtest closes+reverses on an opposite signal; live holds through flips) justified as "**<1% of exits**, immaterial", measured on 5 pairs of **1h SOFT5-era** data — and the comment itself says "re-measure if exit logic ever changes". It has changed completely since (4h, tp6, maker entries, TP-as-limit, K=3 cooldown). Measured on the deployed book: **triple 54/1740 = 3.1%** of exits (meanR −0.22, medR −0.31, mean 71.5 bars held), pull 2/182 = 1.1%, combined **2.9% — ~3× the documented claim**. Materiality: ≈−12R of the ≈+271R net over the window (~4% of net profit) that the engine books and live does not. Small, but "immaterial" and "<1%" are both now factually wrong. NOT a bug and NOT a change — the divergence is deliberate and may well favour live; the ask is to update the comment with the measured number and make the choice deliberately rather than on a stale measurement. | timestop_4h.py |
| **Conditioned sleeve variants — redesign round** (2026-07-06) | Diagnosis first: 2021 universe is THIN (median 13/23 names with data); funding dispersion has secularly COLLAPSED (median cross-sec std of 7d funding 43bp 2021 → 15bp 2023 → 7bp 2025), and corr(carry month, LAGGED dispersion) = **−0.27** — the "carry pays when dispersion is fat" hypothesis is REFUTED: fat dispersion was the 2021 funding mania that whipsaws a 7d rank (short-the-expensive names that keep mooning), while carry's actual profits came from the thin-dispersion 2024–25 era. corr(TSMOM month, \|BTC month\|) = **+0.25** — TSMOM earns in big-move months, so a calm/vol filter trims exactly the wrong months. All three pre-registered variants (lagged rolling-quantile gates, no fitted constants, early history defaults to TRADING so a gate can't hide the bleed) **FAIL the full-history gate** (need full Sh ≥ 0.5 AND pre-2023-08 ≥ 0.0): TSMOM_STRONG full +0.21 / pre −0.68; TSMOM_CALM +0.17 / −0.59; CARRY_GATED +0.26 / −0.40 (direction right but far from the bar, and it kills 2025: +1.8 → −0.2). Caveats: the 2021 "whipsaw" count (451 flips/name) is contaminated by missing-data NaN transitions — the honest 2021 signal is universe thinness, not measured churn; the harness's simplified common frame gives slightly different ref numbers than the sleeve scripts (carry pre −0.67 vs −0.34) but the same shape and verdict. **Conclusion: the pre-2023 bleed has no exploitable structure under honest conditioning. Sleeves remain prove-it-forward only; leverage stays BLOCKED; the trend book (MAJORS8, Sh 1.42) remains the only leverage-eligible component.** | sleeve_diagnosis.py |

## Promising — UNDER VALIDATION (not adopted; do not deploy on this evidence)

| Idea | Preliminary result | Caveats | Script |
|---|---|---|---|
| **XSMOM-21 sleeve** (2026-07-09 stacking round; QUAL23 21d residual-vs-BTC momentum, weekly long-top/short-bottom quintiles, inverse-vol, 8bp/turnover + real funding) | **Full Sh(mo) +1.00, pre-2023-08 +0.85, post +1.14 — the first crypto sleeve POSITIVE THROUGH 2021 AND 2022.** Corr to the deployed book 0.18. Assembly: BOOK+XSMOM at IV Sh **1.74 (IS 1.73 → OOS 1.81, dMDD −1.8%)**; recommended deployable weights BOOK60/XSMOM40 → Sh 1.69, OOS 1.74 (capping the new sleeve below IV = the carry-concentration lesson applied preemptively). | (1) **Recent decay**: common-window thirds +1.83/+1.21/+0.14 and 2026 ytd −0.7 — the signal weakened toward the present; the forward record decides if it is alive or being arbed away. (2) k=14 sibling much weaker (+0.46) — parameter sensitivity noted; k=21 was pre-registered alongside, not selected post-hoc, and dominates uniformly. (3) Needs its own weekly paper executor + the full gate battery before capital. | sleeve_battery.py, assembly_v3.py |
| **XSBAB-60 sleeve** (2026-07-09 round 2; betting-against-beta: 60d beta vs BTC, weekly long low-beta / short high-beta quintiles, QUAL23, inverse-vol, 8bp/turnover + real funding) | **Full Sh +0.74, pre-2023-08 +0.70, post +0.77 — era-symmetric like MOP; 2022 +1.1 (positive through the bear); corr to book −0.03, corr to XSMOM −0.02.** Assembly v4 deployable proposal **BOOK50/XSMOM25/XSBAB25: Sh 1.80, IS 1.63 → OOS 2.01, dMDD −2.1%, thirds +2.65/+1.15/+2.07**; @15% vol ≈ 28.5% CAGR / dMDD −8.4% / worst mo −3.7%. XSBAB's thirds (−0.27/+2.24/+0.80) complement XSMOM's fade — the two cross-sectional sleeves peak in different eras. | Same road as XSMOM: weekly paper executor + ≥8wk forward record + full gates before capital. Plain quintiles (no beta-parity leverage) — stated simplification. Sleeve-heavy IV/CAP40 variants print higher (1.87–1.91, OOS 3.3) but concentrate 83% in sleeves — not proposed (the carry lesson). | sleeve_battery2.py, assembly_v4.py |
| **MOP-TSMOM commodities sleeve** (12-month sign, monthly rebalance, vol-scaled — the literature-canonical MOP-2012 spec on GC/SI/CL/BZ via Yahoo proxies; Bybit now lists all four as USDT perps) | **25-year gate PASS: full Sh +0.53, pre-2023-08 +0.53, post +0.52** — same Sharpe in every era (worst stretch 2015-19 at −0.12), corr to book **−0.21**, corr to XSMOM −0.01. The most durable diversifier found to date. | (1) Raw 40%-vol scaling has −60% MDD — must be vol-normalized before assembly weighting (IV gave it only 5%; its role is era-diversification, not recent Sharpe: OOS on the common window ≈ 0). (2) Proxy data has no perp funding; Bybit's contracts are ~4 months old with 4h funding intervals and synthetic weekend pricing — a paper/min-size forward stage on the real venue is mandatory. (3) Admissible-test budget on this data is SPENT (fast-trend failed, MOP passed) — no further commodity variants. | tradfi_mop.py, assembly_v3.py |
| ~~Post-stop same-side re-entry cooldown~~ | **PROMOTED to Adopted (2026-06-29)** after passing with-regime + walk-forward + OOS validation. See the Adopted table. | — | test_reentry_cooldown_prod.py |
| **ETH → AAVE swap / drop ETH** | Recent 400d: ETH→AAVE beats keep on all metrics (Sharpe 1.92→2.16, ret +174→+194%, MDD −28→−22%); drop-to-4 mild + (Sharpe 2.08). | ETH is NOT a bad pair (+107% standalone, mid-pack; its live 0/4 week was variance). AAVE's edge is recent-window-specific = the "chase recent winners" trap. Keep ETH; revisit only with multi-window + with-regime evidence. | test_eth_swap.py |
| **HONEST REBUILD (post-artifact, 2026-07-05): 4h port of the production stack** | r1 (fixed engine, 1h): overlays are REAL — regime +12pp/y, F&G +9pp/y honest lift over a raw signal at −20%/y; small real gross edge (+398/100u) but fees eat 73%; widening TP improves monotonically (tight TP was selected BY the artifact). r2: the UNCHANGED production params on **4h bars**: CAGR 7.0%, Sh(mo) 0.69, MDD −13.3%, **OOS Sh 1.47 > IS 0.41** (no overfit decay), fees ÷3.4; **4h + tp_mult 6.0: CAGR 15.1%, Sh(mo) 0.98, MDD −9.6%, OOS 1.23**. All four 4h cells OOS-positive; 1h trails don't rescue 1h. | **r3: ALL THREE VALIDATION GATES PASSED (2026-07-05).** (A) Generalizes: TP60 profitable on **10/11** universe names with zero re-tuning; EW11 CAGR 12.1% / Sh 0.84 ≈ SOFT5 15.1% / 0.98 — the mechanism lifts the whole cross-section. (B) Stable: Sh(mo) positive in **all three** sub-window thirds on both books. (C) Entry signal is REAL: vs 60 random-entry draws (same regime/F&G gates, SL/TP geometry, costs), real TP60 ranks **100th pct** on final and Sharpe — null median is deeply negative ($1,969 / Sh −0.71), so entries, not just regime-direction, carry the information. Honest expectations if ever deployed: ~12–15% CAGR, Sh(mo) ~0.9, MDD ~−10%, worst mo ~−6%. NOT yet adopted: needs 4h live wiring + exec parity on 4h + a paper period. Note: engine `cooldown_bars=1` on the fixed engine ≡ no cooldown (the fix already blocks the exit bar), so the validated 4h config runs WITHOUT an SL cooldown. Artifact-era REJECTIONS of churn-reducing ideas remain up for honest re-trial. | honest_rebuild.py, honest_rebuild_r2.py, honest_rebuild_r3.py |

## Universe + portfolio construction (research/expand_universe.py, portfolio_construction.py)

- **Strategy is NOT INJ-specific.** Of 33 pairs swept, 20 clear Sharpe ≥ 1.1;
  RUNE/AAVE/DOGE/AVAX/GRT/NEAR all rival or beat 4 of the current 5.
- **Diversification depth:** best-N equal-weight Sharpe peaks at N≈7 (2.44) then
  slowly dilutes; tail keeps improving with N (worst month −16%→−9%, MDD −24%→−18%).
  Sweet spot ≈ 7-8 pre-screened pairs, equal-weight. **SUPERSEDED 2026-07-02:** this
  was KuCoin in-sample; the Bybit-perp OOS re-test (portfolio_robustness.py) showed
  the 8-basket edge decaying OOS while the 5-name selection persists (97th pct vs a
  like-for-like random-5 null). More names ≠ more robustness on the honest test.
- **Pair performance is period-dependent** — ETH Sharpe 1.31 over 8.7y but 0.78 on
  the recent 4.6y window; FIL 1.69 full-history vs 0.74 recent. So "pick the top-N
  Sharpe" is overfitting; the robust play is a broad equal-weight basket.
- **Weighting:** inj_heavy was near-optimal on the 5 in-sample, but its tails lean on
  the INJ-40% concentration; **production moved to SOFT5 (INJ 25 / rest 18.75) on
  2026-07-02** — OOS parity, best monthly Sharpe, single-name risk capped. Equal-weight
  gives the best worst-month; leaning into recent winners (SOL/ADA) is the worst.

## Meta-conclusion

Six independent attempts to add a predictive/defensive *entry* filter
(ML ×2, chop, two health variants, funding rate) all fail the same way: they
remove good trades because the losses they target are **not predictable** from
price/indicator/positioning signals available at entry. The deterministic core
(EMA stack + RSI + ADX + regime + F&G) is information-efficient.

What *does* help is **risk management** (decay ladder) and
**diversification** (multi-pair) — not more entry filtering.

## Validated robustness

7-pair cross-asset sweep (validate_sweep.py): production preset profitable
on all 7 (INJ/SOL/ADA/ETH/LINK Sharpe >1.1, BTC 0.77, LTC 0.30). Shorts
contributed positively on every pair. Avg pairwise monthly correlation 0.16
— the bidir strategy decorrelates pairs, which is why diversification works.

## Data limits

KuCoin free OHLCV history caps: BTC/ETH 8.7y, LTC 8.3y, XRP 7.5y, BNB/ADA
6.9y, LINK 5.8y, SOL 4.8y, INJ 4.6y (INJ launched late 2021 — no more
exists). Real funding: Bybit (full history, EC2/Singapore only) or OKX
(~90d, reachable from CI). INJ real funding ≈ 0 mean with −0.3% spikes;
flat 1bp/8h constant is a fair central estimate for a balanced book.
