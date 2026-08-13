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

## ⚠ 2026-08-05 — `test_exec_parity` is RED, on a retired config, via a rolling window

`test_exec_parity.py` now fails: **ADA-USDT, 180d of 1h data, K=3 — 16
divergence regions, 1 UNEXPECTED.** It passed on 2026-08-03. Recorded rather
than silenced, because a red G5 gate is exactly the thing this project must
not learn to ignore.

**Diagnosis — it is NOT a regression from the fee-booking fix.** A/B against
`git show fac8f7b:bot.py` (the last commit before that change) reproduces the
result *identically*: same 16 regions, same 1 UNEXPECTED, same 163/160 trade
counts. Cause is the test's **rolling data window** — every section fetches
`days=180` (1h) or `days=540` (4h) relative to *today*, so two days of new
bars slid in and surfaced a fresh edge case.

**The deployed book is unaffected.** Every 4h section is clean:

| section | pairs | UNEXPECTED |
|---|---|---|
| 4h K=0, tp6 (honest-rebuild config) | 5 | **0** |
| pullback 4h K=3 (BLEND50_CONF leg 2) | BTC/ETH/SOL | **0** |
| 1h K=0 (retired) | 5 | 0 |
| **1h K=3 (retired)** | ADA | **1** |

The failing cell belongs to the 1h program that the same-bar artifact retired
in 2026-07-05; nothing deployed runs 1h or that cooldown path.

**The real defect this exposes is methodological: a gate test whose result
depends on the date it is run is not reproducible**, which contradicts the
whole basis of the ledger.

### RESOLVED same day — both fixes applied

1. **Window pinned.** All cases now clip to `PARITY_END` (default
   **2026-08-01**), over-fetching past it and slicing. The date was fixed as a
   calendar boundary — the 1st of the current month — **before** re-running;
   it was not chosen by testing which date made the suite pass. Verified
   deterministic: two consecutive runs are byte-identical (md5 of output).
   Re-pin deliberately with `PARITY_END=YYYY-MM-DD`.
2. **Two tiers.** *DEPLOYED* (4h/tp6 K=0, pullback 4h K=3) is gate G5 and its
   assertions are always fatal. *LEGACY* (the retired 1h program, K=0 and K=3)
   is reported loudly and recorded but does not gate — letting drift in dead
   code block the suite would train us to ignore a red G5, which is the exact
   opposite of the point. `PARITY_STRICT=1` makes legacy fatal too.

**Result at the pinned window: everything is clean, legacy included** (1h K=0
and K=3 both 0 UNEXPECTED; 4h 0; pullback 4h 0). So the ADA divergence was
entirely the two new bars, exactly as the A/B indicated. Note this outcome was
not engineered — the pin date was committed to first.

The assert was NOT loosened for the deployed tier; that would have converted a
live tripwire into decoration.

## 2026-08-13 — sl 3.0 PASSES the long-history gate, but SPLITS the two legs

`research/stop_longhist.py`. The 2022-inclusive pseudo-OOS that rejected
TSMOM-90 (−0.70) and carry (−0.34), method identical to `trend_longhist.py` so
the control is directly comparable. Expanding MAJORS8 book from 2022-03,
per-pair data start + 365d warmup, nothing clipped to the common window.

| book | cell | full | pre-2023-08 | post | MDD |
|---|---|---|---|---|---|
| TRIPLE_CONF | sl 1.8 | +0.98 | +0.56 | +1.16 | −9.6% |
| TRIPLE_CONF | **sl 3.0** | **+1.09** | +0.56 | **+1.34** | **−6.2%** |
| PULL_CONF | sl 1.8 | +0.37 | −2.63 | +1.32 | −6.9% |
| PULL_CONF | **sl 3.0** | **+0.16** | −2.54 | **+1.13** | −5.1% |
| BLEND50 | sl 1.8 | +1.01 | +0.16 | +1.35 | −5.9% |
| BLEND50 | **sl 3.0** | **+1.07** | **+0.18** | **+1.49** | **−4.3%** |

**GATE: PASS.** Absolute bar full +1.07 ≥ 0.50 and pre +0.18 ≥ 0.00; relative
bar ΔSh pre +0.02 (not negative, so not the pre-registered regime-artifact
signature). Control reproduces `trend_longhist` within drift (+1.01/+0.16/+1.35
vs the 2026-07-06 +1.20/+0.18/+1.68 — the window has since absorbed July 2026's
−3.6%).

**What the pass does and does not say.** ΔSh pre-2023 is **+0.02 — nothing.**
The gate was cleared on "does not degrade", NOT on "improves". The entire gain
is post-2023 (+0.14), which is exactly where it was discovered. So this gate has
confirmed the mechanism does not BREAK in the bear regime; it has NOT
independently confirmed the mechanism. That distinction is the whole point of a
pseudo-OOS and it should not be blurred in the summary.

**THE MATERIAL NEW FINDING — sl 3.0 helps TRIPLE and hurts PULL.** No prior test
could see this; G4 measured blends only.
* TRIPLE full +0.98 → **+1.09**, post +1.16 → +1.34, MDD −9.6% → −6.2%.
* PULL full +0.37 → **+0.16**, post +1.32 → +1.13.
The blend improves only because triple's gain outweighs pull's loss. Adopting
sl 3.0 **uniformly would make the already-weak leg weaker** — PULL was
+0.37 (already under the 0.5 sleeve-law floor) and this takes it to +0.16, with
its armed demotion trigger (trailing-3mo live+paper Sh < 0 → BLEND75 or
triple-only) that much closer to firing.

**The obvious follow-up is a NEW EXPERIMENT, not a tweak.** Per-leg stop width
(3.0 on triple, 1.8 on pull) is a widened grid and law 2 requires its own
pre-registration and its own OOS. Deliberately NOT run opportunistically off
the back of this result — that is precisely the post-hoc mining the law forbids.

**Real prize is the drawdown:** blend MDD −5.9% → −4.3%, a 1.6pp improvement,
which is what converts to leverage capacity under the vol dial and pays for the
CAGR give-up measured in G4.

**GATE STATUS: G1 ✓ G2 ✓ G3 N/A G4 ✓ LONG-HISTORY ✓. G5 exec parity is the
only gate left. STILL NOT ADOPTED — deployed sl_mult stays 1.8, live config
untouched.**

## 2026-08-13 — LIVE GAP: bot.py never books funding (backtest does)

Routine reconcile of the 7 live closes against Bybit `closed-pnl`. Two checks,
one clean and one not.

**CLEAN — fill accounting is honest.** `bot.py:1216` books an autonomous close
at the THEORETICAL stop (`fill = p.sl`), which looked like a slippage-blind
optimism bug. It is not: `close_position` overrides the hint with
`fetch_last_closed_fill()` (`fill_px = real["exit_px"]`). All 5 comparable
exits match the exchange's `avgExitPrice` EXACTLY (64168.1 / 75.02 / 0.184 /
1916.8 / 0.07044). The all-at-bar-close `exit_time`s are DETECTION times from
the bar poll, not fill times — the exchange fires the attached SL intra-bar.
Protection is real. Recorded so this is not re-investigated.

**NOT CLEAN — funding is never booked.** `grep -c funding bot.py` = 0. Bot PnL
is gross − entry fee − exit fee; Bybit's `closedPnl` includes funding.

| leg | bot PnL | exchange | funding |
|---|---|---|---|
| btc-t | −3.39537 | −3.45861 | −0.0632 |
| sol-t | −1.68580 | −1.72831 | −0.0425 |
| ada-t | −2.22384 | −2.24477 | −0.0209 |
| doge-t short | −1.77750 | −1.75728 | +0.0202 |
| eth-t short | −1.53016 | −1.51868 | +0.0115 |
| xrp-t short | −1.73048 | −1.73014 | +0.0003 |
| doge-t long | −1.76055 | −1.76055 | 0.0000 |
| **total** | **−14.104** | **−14.198** | **−0.095** |

Longs paid, shorts received. Net −0.095 on ~701 cumulative entry notional =
**~1.4 bp per round trip, ~11% of the 12 bp fee bill.** Small in absolute
terms and NOT an emergency, but three reasons it is a real defect:

1. **Wrong direction.** Local equity reads BETTER than the exchange. Every
   other cost gap this project found read worse.
2. **It feeds a safety control.** The −8% halt line is evaluated on bot
   equity, so it fires LATE, and later the longer positions are held.
3. **It inverts the usual asymmetry.** The backtest models real per-pair
   funding (`apply_funding_real`, every research script). The LIVE executor
   is the less honest side — the one place the laws assume it never is.

**FIX (proposed, not shipped):** in live mode only, take realised PnL from the
exchange's `closedPnl` rather than recomputing it locally. That absorbs
funding, fee-tier drift and partial fills in one move and makes live equity
identical to the venue by construction. Paper path untouched
(`cfg.mode == "live"` gate, house rule). Requires a 16-container redeploy, so
it waits for a go.

**Also observed, NOT a finding:** 7 live trades, all on `-t`; the `-p`
(pullback) leg has taken 0 entries in 5 days. Backtest frequency is ~40
trades/container/yr → λ≈4.4 expected across 8 `-p` containers in 5 days, and
P(0)≈1% under INDEPENDENCE — but entries are regime-gated and correlated
across symbols, so the effective draw count is far below 8 and 0 is
unremarkable. Same discipline as the 7-stop streak: watch it, do not act on
it. Revisit at ~20 `-p` opportunities.

## 2026-08-13 — sl_mult 3.0 PASSES G4; my noise call was wrong, but it is not adopted

`research/stop_g4.py`. The gate that separates "real mechanism" from "fit to
one book", run with no re-tuning: the same single comparison (sl 1.8 vs 3.0)
carried onto structural books built the legal way — QUAL23 ranked by EX-ANTE
median daily dollar volume, top 8/12/16/all.

| book | n | sl 1.8 Sh | sl 3.0 Sh | ΔSh | ΔCAGR |
|---|---|---|---|---|---|
| MAJORS8 | 8 | 1.11 | 1.25 | **+0.15** | −2.1 |
| MAJORS12 | 12 | 0.96 | 1.11 | **+0.14** | −1.2 |
| MAJORS16 | 16 | 0.72 | 0.86 | **+0.14** | −0.6 |
| EW23 | 23 | 0.67 | 0.73 | +0.06 | −0.6 |

**G4 PASS.** All four positive; MAJORS8 (+0.15) is not an outlier against the
+0.12 mean of the others. **I called the +0.12 noise on the strength of the
non-monotone ladder — that call is weakened.** A fit to MAJORS8 does not
reproduce +0.14 on MAJORS12 and MAJORS16 with a book it never saw.

**What survives from the earlier caveats:**
* **CAGR falls in EVERY book** (−2.1 / −1.2 / −0.6 / −0.6). This is a
  risk-adjusted gain bought with return — legitimate in this framework only
  because the vol dial converts lower drawdown into leverage, which is the
  same argument that justified BLEND50 over triple-only.
* **The non-monotone ladder is still unexplained.** G4 tests generalisation
  across UNIVERSES, not across the parameter axis. sl 2.2 sitting −0.14 below
  a control that 3.0 beats by +0.15 remains odd, and no mechanism has been
  offered for it. Per-trade expectancy is smooth (+0.076R / +0.077R / +0.107R
  at 1.8 / 2.2 / 3.0), so the dip is not in expectancy — it is path or
  variance, and it is unexplained rather than explained away.
* **The R:R confound stands**: sl 3.0 / tp 6.0 is 2.00 reward:risk, not 3.33.
  TP rate rises 23.3% → 30.5% while each win is worth less in R.

**GATE STATUS: G1 ✓ (IS 1.34→1.47, OOS 0.99→1.03), G2 ✓ (thirds all +),
G3 N/A (no entry logic), G4 ✓. STILL MISSING: G5 exec parity, and the
LONG-HISTORY GATE (2022-inclusive, full Sh ≥ 0.5 AND pre-2023-08 ≥ 0).**

The long-history gate is the decisive one and the likeliest place for this to
die — it is what killed TSMOM-90 (−0.70) and carry (−0.34), and 2022 is
exactly the regime where wider stops with smaller positions behave differently.
**NOT ADOPTED. Deployed sl_mult stays 1.8 and nothing in the live config
changed.**

## 2026-08-13 — STOP GEOMETRY: trailing stop REJECTED; wider SL passes but is not monotone

Prompted by a live run of **7 consecutive stop-outs**. That streak is NOT
evidence and was not treated as such: with the backtested exit mix (sl 67.6% /
tp 23.6%), **P(no TP in 7 trades) = 15%, about one stretch in seven**, and the
95% CI on the true TP-rate given 0/7 is **[0%, 41%]**, which contains 23.6%.
Seven trades cannot reject the design; ~20 would begin to.

The study was justified instead by an audit finding: **`sl_mult = 1.8` had
never been swept** (TP width was — tp_mult 3→6, monotone) and **`trail_atr`
carried no verdict at all** despite living in the engine since the honest
rebuild. Two orphaned parameters, same class as `max_bars_in_trade = 96`.

| cell | trades | tp% | sl% | CAGR% | Sh | ΔSh | ΔdMDD | IS | OOS |
|---|---|---|---|---|---|---|---|---|---|
| A1.2 sl 1.2 | 2370 | 17.2 | 78.5 | 7.0 | 0.85 | −0.32 | −2.1 | 0.68 | 1.03 |
| A1.5 sl 1.5 | 2115 | 20.0 | 73.6 | 5.6 | 0.80 | −0.38 | −1.2 | 0.79 | 0.79 |
| **A1.8 CONTROL** | 1921 | 23.3 | 68.0 | **8.5** | **1.18** | — | — | 1.34 | 0.99 |
| A2.2 sl 2.2 | 1775 | 26.3 | 62.5 | 6.2 | 1.04 | −0.14 | +1.2 | 1.22 | 0.81 |
| A3.0 sl 3.0 | 1589 | 30.5 | 49.4 | 6.0 | **1.30** | **+0.12** | +2.6 | 1.47 | 1.03 |
| B2.0 trail 2.0 | 2471 | 11.9 | 87.4 | 2.7 | 0.70 | −0.48 | +0.3 | 0.75 | 0.64 |
| B3.0 trail 3.0 | 2076 | 18.1 | 79.6 | 5.9 | 0.99 | −0.18 | +0.2 | 1.21 | 0.77 |
| B4.0 trail 4.0 | 1968 | 21.1 | 74.4 | 5.6 | 0.94 | −0.23 | +0.1 | 1.10 | 0.72 |

**TRAILING STOP: REJECTED, decisively and on the pre-registered prior.**
Every cell is worse, and the mechanism is legible — trail 2.0 pushes the SL
rate from 68% → **87.4%** and TP from 23.3% → **11.9%**. It converts winners
into stop-outs. This is the THIRD independent confirmation of "winners must
run" (partial-TP rejected twice, now trailing). `trail_atr` moves from
orphaned to explicitly rejected; the default stays 0.0.

**SL WIDTH: A3.0 clears the pre-registered bar (a/b/c/d all Y, ΔSh +0.12) —
and I do not think it should be acted on yet.** Three reasons the bar did not
encode:

1. **The ladder is NOT monotone**: 0.85 → 0.80 → **1.18** → 1.04 → 1.30. The
   control sits on a local peak, 2.2 DIPS below it, then 3.0 rises. A genuine
   "the stop is too tight" effect would climb monotonically from 1.8. A jagged
   curve with the neighbouring cell moving −0.14 in the OPPOSITE direction is
   the shape of noise, not of a mechanism.
2. **+0.12 is 2–4 units of the documented 0.03–0.05 run-to-run jitter.**
3. **IS improves more than OOS** (1.34→1.47 vs 0.99→1.03) — mild overfit shape.

In fairness the other way: the CAGR drop (8.5→6.0) is **not** automatically the
"defensive only" pattern that killed per-regime sizing, because dMDD is 2.6pp
BETTER and this framework converts lower drawdown into leverage via the vol
dial — at equal vol, Sh 1.30 vs 1.18 is ~10% more CAGR at equal risk. The R:R
confound was declared in advance: at sl 3.0 / tp 6.0 reward:risk falls 3.33 →
2.00, so A3.0 is a different payoff shape, not the same strategy with more room.

**Verdict: A3.0 earns G4 universe generalization, NOT adoption.** If the effect
is real it should appear across EW23/MAJORS12 too; if it is the noise the
non-monotone ladder suggests, G4 will say so. Deployed value stays 1.8.

**MECHANISM (clean, monotone, and the one solid result here).** Mean R on
stopped trades improves monotonically with stop width — exactly as a fixed
cost divided by a wider stop predicts:

| sl_mult | mean stop | mean R on stops |
|---|---|---|
| 1.2 | 3.08% | −1.047 |
| 1.8 | 4.58% | −1.031 |
| 3.0 | 7.25% | −1.019 |

Confirmed independently in the live fills (btc-t 1.14% stop → −1.152R;
ada-t 4.33% → −1.027R). The effect is real but small: **0.028R across the whole
range.** It is a reason tight stops are mildly expensive, not a reason to widen.

## 2026-08-09 — Venue shopping: the whole gain is on Bybit already

Asked whether a cheaper venue could support a higher-frequency system.
Re-priced the deployed book across fee scenarios (identical config, fees the
only variable):

| scenario | CAGR% | Sh(mo) | ΔSh |
|---|---|---|---|
| **A. us now** 10.0/3.6 bp | 8.8 | 1.24 | — |
| **B. Bybit's own published** 5.5/2.0 | 9.5 | **1.33** | **+0.09** |
| C. Binance / OKX 5.0/2.0 | 9.6 | 1.33 | +0.09 |
| D. MEXC-like 2.0/0.0 | 10.3 | 1.40 | +0.16 |

**We are paying 1.8× Bybit's own advertised non-VIP rate** (published 0.02% /
0.055%; we are billed 0.036% / 0.10%, verified across all 831 linear symbols
and by two live closes). That single discrepancy is worth **+0.09 Sh** — and
**+0.09 of the total +0.16 available anywhere.**

**Migrating to Binance or OKX is worth ZERO beyond fixing the Bybit tier** —
identical published fees. The zero-maker venue buys **+0.07 Sh more**, against
which: a full re-validation (every gate, every backtest and exec parity were
measured on Bybit data — the same reason the program moved KuCoin→Bybit),
a materially different counterparty, and the loss of Bybit's free tick+book
archives on which the entire research capability now depends. **+0.07 Sh does
not buy that.**

### Does it unlock a higher-frequency system? No.

At MEXC-like costs (~1.5 bp round trip) the measured flow signals (3.5–5.2 bp)
would net +2 to +3.7 bp, which looks viable until three things are accounted
for:

1. **The 3.5–5.2 bp is IC-implied**, i.e. `|IC| × σ(fwd)` — an optimistic
   upper bound on capturable edge, not a backtested return.
2. **`large_imb`'s IC was NEGATIVE** (−0.034/−0.031). Trading it means FADING
   flow, a far more crowded posture than following it, and the graveyard
   already holds CHOP-MR (−1.24) and BB-MR (−0.95).
3. **Frequency amplifies everything we have not measured** — adverse
   selection, latency, rate limits, outages. Our ~0 bp measured slippage comes
   from 4h cadence at $60 clips; it is not evidence about behaviour at 100×
   the turnover against colocated counterparties.

Fee drag today is ~1.7pp of a ~10.5pp gross CAGR — **~16% of gross**.
Meaningful, not dominant. Cost scales linearly with turnover, so a 10× more
frequent book needs >10× the gross edge to come out ahead. Nothing measured
supports that.

**Where rebates genuinely change the business is market MAKING itself** (Bitget
advertises −0.005% maker on ~130 perps; Bybit MM 3 is −0.0075%). That is one of
the three untested taxonomy types — and it is a different business needing
latency and quoting infrastructure, not a fee tweak to this one.

**Action: one support question to Bybit about the rate. It is worth more than
any venue migration, and it costs nothing.**

## 2026-08-09 — Why quants harvest edges we cannot: the fee ladder, from the ground up

Asked why big firms trade the 3–8 bp signals our cost floor kills. Answered
from Bybit's own `GET /v5/market/fee-group-info` rather than from theory. It is
not signals, infrastructure or cleverness — **it is a published fee ladder we
sit at the bottom of.**

G1 "Major Coins" (BTCUSDT, ETHUSDT, XRPUSDT, SOLUSDT — three of ours). Round
trip = maker in + our measured exit mix (23.6% maker TP / 76.4% taker):

| tier | maker bp | taker bp | round trip | vs us |
|---|---|---|---|---|
| **us (no tier)** | **3.60** | **10.00** | **12.09 bp** | 100% |
| Pro 1 | 1.00 | 2.80 | 3.38 bp | 28% |
| Pro 3 | 0.25 | 2.20 | 1.99 bp | 16% |
| Pro 6 | 0.00 | 1.50 | 1.15 bp | 9% |
| **MM 3** | **−0.75 (rebate)** | 2.80 | 1.21 bp | 10% |

**Market makers are PAID to provide liquidity** (`makerRebate: -0.000075`).
That is the structural answer: our 4.9 bp `large_imb` signal nets **−7.19 bp**
for us, **+1.52 bp** at Pro 1 and **+3.75 bp** at Pro 6. *Identical signal,
identical data — the tier decides whether it is an edge or a loss.*

**And it is unreachable, precisely.** Our traded volume is ~**$6,400/month**
(640 trades/yr × ~$60 × 2 sides). Pro 1 needs on the order of $10M/month —
we are **~1,562× short**, equivalent to a **~$2.8M book**. No optimisation on
our side closes a 1,500× volume gap. Chasing it is not a plan.

### The reframe that actually matters

**The cost floor is nearly irrelevant to THIS strategy, and fatal only to
tick-scale ones.** With a 1.8-ATR stop, 1R is 200–400 bp of notional:

| stop | 1R | RT 12.1 bp as % of 1R | as % of the avg +3.33R win |
|---|---|---|---|
| 2% | 200 bp | 6.0% | 1.8% |
| 3% | 300 bp | 4.0% | 1.2% |
| 4% | 400 bp | 3.0% | 0.9% |

Confirmed empirically by the re-pricing: +4 bp of round trip (8→12.1) cost
**0.08 Sharpe**. Extrapolating, reaching Pro 1 would be worth roughly **+0.17
Sh** — real, but not transformative, and unavailable.

**So the existing design already IS the answer to the cost problem.** A 4h
trend book targeting 6-ATR moves doesn't fight the floor, it steps over it.
That was arguably the single most important structural decision in the
program, and this is the first time it has been quantified as such.

### What is actually actionable (small, in order)

1. **Confirm 10.0/3.6 is the correct standard rate for a no-VIP UTA account.**
   Bybit publishes lower non-VIP derivative figures; four distinct schedules
   exist on this very account (a promo tier of 2.75 bp taker / **0 maker** runs
   on 178 TOKENISED EQUITY symbols — AMZN, ORCL, HOOD — irrelevant to us and
   barred by the structural-universe law anyway). One support question. If a
   standard tier applies, it is worth more than every code optimisation here.
2. **Referral discount** — `inviterID: 0`, so none is applied. Typically only
   available at account creation; worth knowing for any future account.
3. **Turnover** is the only lever we control: cost scales linearly with trades.
   That is a strategy change and owes the gate battery.
4. **NOT the SL-as-maker idea.** A stop resting as a limit can fail to fill in
   exactly the gap it exists to protect against. The taker stop is a
   deliberate, correct choice and should not be traded away for 6 bp.

## 2026-08-09 — TRADE-FLOW REJECTED on 6.4 years: real information, below the cost floor

The first study the tick archives unblocked, and it independently reproduces
the tier-1 moonshot conclusion on new data. Pre-registered IC screen
(`research/tradeflow_ic.py`), modelled on `funding_signal.py` — measure the
information before writing a strategy, so a dead idea dies cheaply.

Four features, one parameterisation each, fixed before the first download:
`cvd_bar`, `cvd_6bar`, `large_imb` (trades ≥ 95th pct size, per symbol-day),
`count_imb`. Target: NEXT 4h bar return. Sample: BTCUSDT + ETHUSDT, every 20th
day 2020-03-25 → 2026-08-08 (117 days, ~700 bars, 529 usable forward returns
per symbol — days are disjoint, so cross-day forward returns are dropped).

| feature | IC next (BTC / ETH) | IC same-bar (BTC / ETH) | implied edge |
|---|---|---|---|
| cvd_bar | −0.014 / −0.038 | **+0.418 / +0.393** | 3.9 bp |
| cvd_6bar | +0.047 / −0.001 | +0.146 / +0.107 | 3.5 bp |
| large_imb | −0.034 / −0.031 | **+0.360 / +0.347** | 4.9 bp |
| count_imb | −0.025 / −0.044 | **+0.348 / +0.326** | 5.2 bp |

**NO CELL CLEARS THE BAR** (|IC| ≥ 0.03, consistent sign, edge > 13.6 bp).

**The same-bar column is the point.** Contemporaneous IC of **+0.33 to +0.42**
proves the features are computed correctly and genuinely measure aggressive
flow — price and flow move together mechanically within a bar. Next-bar IC
collapses to ≈0 and is mostly *negative*. Had the same-bar control also been
flat I would suspect a bug; it isn't, so the null is about the world rather
than the code. That control is why it is in the design.

**Implied edge 3.5–5.2 bp against a 13.6 bp measured round trip** — roughly 3×
below the floor. The tier-1 battery concluded "the signals exist (lead-lag
gross +3–8bp, real) but are 3–7× below our cost floor; the moat is execution
cost, not signal discovery." **This lands squarely in that band, on a
different dataset, six weeks later.** That is replication, not coincidence,
and it is the strongest evidence yet that the conclusion is structural.

The mild NEGATIVE next-bar IC also fits the existing graveyard: fading does not
work either (CHOP-MR −1.24, BB-MR −0.95).

**No backtest is owed and none was run.** ~270 GB of downloads saved by
screening first. "Order Flow" moves from the tick-gated column of the
2026-08-03 taxonomy triage to the dead column.

## 2026-08-09 — FILL CALIBRATION: the engine's optimistic fill assumption VALIDATED

The measurement the guessed `maker_fill_min_bp` ladder was standing in for.
Pre-registered before the first download (`research/fill_calibration.py`).

A passive order at price P fills once the volume trading through P exceeds the
QUEUE AHEAD of it. Both terms are now observable from Bybit's free archives:
**Q** = resting size at the limit when the bar opens (ob200 book), **V** =
volume traded at/through the limit during the bar (tick trades), **S** = our
own order size.

**Sample (fixed in advance):** N=64, eight per pair, `default_rng(20260809)`,
drawn from the 359 engine entries falling in the book-archive overlap
(2025-09-01→now). Stratified so no liquid name dominates; seeded so the draw
cannot be re-rolled toward a nicer answer. A census would be ~21 GB.

| criterion | pass | fail |
|---|---|---|
| C1 lenient `V ≥ Q` | **64/64 (100%)** | 0% |
| C2 strict `V ≥ Q+S` | **64/64 (100%)** | 0% |
| C3 paranoid `V ≥ 2(Q+S)` | **64/64 (100%)** | 0% |

Coverage ratio `V/(Q+S)`: **median 2,233×**, p10 111×, p5 36×, p1 3.6×,
**minimum 2.26×**. Our order is a median **0.43% of the queue ahead of it**.

**Every sampled entry clears even the paranoid criterion, and the WORST case
still had 2.26× the volume required.** The engine's "any penetration fills"
assumption is not merely tolerable — on this sample it is never the binding
constraint. The fills the backtest books are fills that had the volume to
happen.

**This retires the fragility question, in the direction of the book being
sound.** The 2026-08-08 ladder measured ≤0.15 Sh of exposure to the assumption
and could not say whether that exposure was real; it now looks like headroom
rather than risk. `maker_fill_min_bp` stays at its 0.0 default — the knob keeps
its value as a stress lever, not as a correction.

**Scope, stated before running and unchanged after:** the book archive begins
2025-09, i.e. the last ~30% of the 2023-08→2026-08 window. This is evidence
about 2025-2026 only. It says NOTHING about 2023-2024, when the book was
thinner and our size a larger share of it — applying it there is extrapolation.
Also n=64 of 359, and only entries the engine actually took (fills it rejected
are not in the sample, so this measures whether booked fills were real, not
whether missed fills should have filled).

## 2026-08-09 — Bybit publishes free L2 order book. I was wrong yesterday.

Yesterday I checked `public.bybit.com/orderbook/`, got a 404, and concluded
the order book was not available free. **It is** — on a different host, which
a single 404 was never sufficient evidence to rule out.

**VERIFIED, not claimed** (HEAD/GET probes, 2026-08-09):

| dataset | source | history | size |
|---|---|---|---|
| tick trades | `public.bybit.com/trading/{SYM}/{SYM}{DATE}.csv.gz` | **2020-03-25 → now (6.4y)** | BTC 46 MB/day |
| **L2 book, 200 lvl @200ms** | `quote-saver.bycsi.com/orderbook/linear/{SYM}/{DATE}_{SYM}_ob200.data.zip` | **~2025-09-01 → now (~11mo)** | MAJORS8 322 MB/day = **118 GB/yr** |
| liquidations | **nowhere free** | — | our collector is the only source |

Boundaries established by bisection rather than taken from the claim that
prompted the search: 2025-08-01 is a 404, all of 2025-09 is present, so the
book archive is **~11 months, not the "2+ years" asserted**. The `ob500` and
`.zst` variants do not exist; only `ob200` zip.

Format is the **raw WebSocket feed** — JSONL, one `snapshot` then `delta`s
(`{"topic":"orderbook.200.SYM","type":...,"ts":<ms>,"data":{"b":[[px,sz]],"a":[...]}}`),
18 MB zipped → 122 MB raw for one AVAX day. Snapshot+deltas means the book can
be **replayed exactly**, so this is Tardis-equivalent for the book — for 11
months, free.

**Disk is the binding constraint, not bandwidth.** 118 GB/yr does not fit the
collector box (41 GB free). `core/bybit_archive.py` therefore never stores a
raw day: download → extract only the requested decision points → discard.
Measured on a real day: **1.66 MB cached from ~130 MB of archive.**

**End-to-end proof on our own ADA entry** (2026-08-06 12:00 bar, maker limit
0.1923, 260 ADA): the book reconstructs to 200 levels a side with
$849k resting on the bid, and **21,747,428 ADA traded through our limit during
the bar — 83,644× our order size**. The fill was never marginal, and this is
now measured rather than assumed.

**What it changes.** The `maker_fill_min_bp` ladder (2026-08-08) picked 1/2/5/10
bp because nothing better existed; the fill question can now be answered from
data. Trade-flow/CVD work has 6.4 years available immediately. Book-imbalance
work has 11 months. Liquidation-cascade work still depends entirely on the
collector — which is the strongest argument yet for never letting it stop.

**The honest caveat for any fill model built on this:** the book archive covers
2025-09 onward, i.e. only the last ~30% of the 2023-08→2026-08 backtest window.
A relationship calibrated there and applied to 2023–2024 is an EXTRAPOLATION,
and must be labelled as one.

## 2026-08-08 — Maker-fill fragility: the edge does NOT rest on marginal touches

Pre-registered fragility test (`research/maker_fill_depth.py`), prompted by
asking for a book-aware entry model. A real book-consuming model is **not
buildable yet** — 3 days of book data against a 3-year window would be fitting
noise. What IS answerable now is how much of the edge depends on the engine's
most optimistic assumption:

    fill_ok = (low < limit) if long else (high > limit)   # ANY penetration

That is why FINDINGS reports "only 6/1048 fills missed" — the number is a
*consequence* of the assumption, not evidence for it. New engine knob
`maker_fill_min_bp` requires the bar to penetrate by at least X bp before a
resting order counts as filled. **Default 0.0 preserves every prior number**,
and raising it can only REMOVE fills — so no cell can flatter the book.

| cell | trades | CAGR% | Sh(mo) | ΔSh | IS | OOS |
|---|---|---|---|---|---|---|
| **F0 0.0 bp (control)** | 1916 | 8.5 | **1.23** | — | 1.37 | 1.05 |
| F1 1.0 bp | 1916 | 8.0 | 1.17 | −0.06 | 1.27 | 1.04 |
| F2 2.0 bp | 1916 | 8.0 | 1.17 | −0.06 | 1.27 | 1.04 |
| F5 5.0 bp | 1914 | 7.9 | 1.16 | −0.07 | 1.20 | 1.09 |
| F10 10.0 bp | 1914 | 7.0 | 1.08 | −0.15 | 1.11 | 1.02 |

**The reassuring part — realised penetration on filling bars (n=21,502):**

| pct | 1 | 5 | 10 | 25 | 50 | 75 | 95 |
|---|---|---|---|---|---|---|---|
| bp | 2.4 | 9.5 | 17.2 | 40.4 | **87.0** | 165.3 | 379.8 |

Median penetration is **87 bp** — ~17× ADA's one-tick spread and ~4,000× BTC's.
Only **0.3%** of filling bars penetrate under 1 bp; 5.3% under 10 bp. The
marginal-touch case the assumption is most generous about is *rare*, and even
a 10 bp gate (well beyond one tick on every major) costs just **0.15 Sh**.

**Mechanism worth noting: trade COUNT barely moves (1916 → 1914).** A blocked
entry is not a lost trade — a persisting signal simply retries next bar. The
cost is a worse entry PRICE, not a missing position, which is why CAGR falls
while the count holds.

**Nothing promoted, and the default stays 0.0.** No cell here is more *correct*
than the control: we do not know the true fill threshold, so this bounds
exposure rather than measuring it. What it establishes is that the adopted
stack is not quietly living on fills that never happened — which was the real
worry, and it is now bounded at ≤0.15 Sh across a deliberately harsh ladder.

## 2026-08-08 — Tick data: measurement only, and the first fill-quality numbers

Asked whether the tick record can feed the live system. Answered three ways,
and only one of them is open:

* **As a SIGNAL — no.** Blocked by A3 (tier-2 at ~60 days; we have 3). More
  fundamentally *mismatched*: the book decides on a 4h clock ~40x/leg/year,
  while tick information has a horizon of seconds.
* **As an EXECUTION input — not directly, and this is the subtle one.** The
  engine prices `maker_close` (limit at the signal bar's close, strict
  penetration, a miss is missed), and **G5 exec parity is what makes the edge
  estimate mean anything**. Smarter live placement using live book state would
  stop live matching the engine — trading a measurable edge for an
  unmeasurable one. Order must be engine-first: model, gate, then wire. Also
  architectural: the boxes are isolated so trading churn can never threaten
  the tick record; piping ticks to the money box reverses that.
* **As MEASUREMENT — yes**, and it closes the open Phase 2 item ("replace the
  flat 2 bps assumption with measured per-pair values") with no live change
  and no gate broken.

`research/fill_quality.py` joins the live blotter to book_1s/depth_1s. First
two fills (both `sl-external`, taker market):

| leg | notional | spread bp | top-of-book | our size | slip bp |
|---|---|---|---|---|---|
| doge-t | $81.01 | 1.42 | $8,848 | **0.92%** | +0.15 |
| xrp-t | $72.05 | 0.96 | $25,538 | **0.28%** | −1.84 |

**Measured slippage: median −0.85 bp against a modelled +2.0 bp charge** — the
model is conservative. **NOT acted on**: n=2, both stops, both mid-depth
names, none in stress. Lowering a cost on this is exactly the flattering
adjustment the ledger exists to prevent. Revisit at n≥30 spanning TP (maker)
and SL (taker).

**Useful correction to the 2026-08-07 depth worry.** That analysis used p5
top-of-book — a tail statistic — and flagged LINK/AVAX. At these actual fills
top-of-book was $8.8k/$25.5k and our order was **under 1%** of it. The p5 case
is real but did not materialise here; thin-name stops remain untested. The
honest reading is that depth risk is a tail concern, not a routine one.

Now runs in the daily cron so the record accumulates rather than being
re-derived by hand.

## ⚠ 2026-08-08 — FEES ARE ~2x THE MODEL: every absolute number was too cheap

The first two live stops closed and Bybit's closed-PnL gave ground truth for
the first time. **The cost model has always been wrong about fees.**

| | model | **measured** | ratio |
|---|---|---|---|
| maker | 2.0 bp | **3.60 bp** | 1.8× |
| taker | 6.0 bp | **10.00 bp** | 1.67× |

Derived identically from both closes — XRP `openFee 0.02594 / 72.0473` and
DOGE `0.02916 / 81.0074` are each exactly 3.60 bp; both `closeFee` ratios are
exactly 10.00 bp — and confirmed outright by `GET /v5/account/fee-rate`:
`takerFeeRate 0.001`, `makerFeeRate 0.00036`. The old 6/2 were Bybit's
published *non-VIP* figures; they are simply not this account's rates.

**RE-PRICED (`research/refee.py`, identical config, fees the only change):**

| cell | CAGR% | Sh(mo) | dMDD% | worst mo% | IS | OOS |
|---|---|---|---|---|---|---|
| MODEL 6.0/2.0 bp | 9.2 | 1.31 | −5.6 | −3.6 | 1.44 | 1.14 |
| **MEASURED 10.0/3.6 bp** | **8.5** | **1.23** | −5.6 | −3.6 | 1.37 | **1.05** |

**L2 COST GATE: ΔSh −0.08, ΔCAGR −0.7pp — INSIDE the pre-registered 0.2 Sh
tolerance. No halt.** Recorded because the gate answering "no" on measurement
is the gate working, not a reason to stop measuring. Note OOS falls to **1.05**
— the thinnest it has been.

**Consequences.** (1) Quote **8.5% / 1.23** for sizing, not 9.5/1.33 and
certainly not the original 10.4/1.50. (2) Every absolute number in this file
produced before today is priced ~2x too cheaply on fees and is optimistic by
roughly this much; relative rankings between variants are largely preserved
because the change is common to all of them. (3) `cost_engine.FEE_TAKER/
FEE_MAKER` now default to the measured rates (env-overridable — they are
per-account and move with VIP tier).

**Root-cause fix, not a constant patch:** `bot.py` now calls
`Exchange.refresh_fee_rates()` at startup and reads the account's real rates
from the venue before anything can be booked, falling back to constants only
on failure. Rates change with tier; asking beats assuming. Guarded by
`test_fee_booking.py`.

## 2026-08-08 — First two exits: the live-vs-engine reconcile, and it is clean

The first fills to complete a round trip. Both `-t` shorts, both stopped out.

| leg | entry | exit | exchange PnL | bot booked | delta | R |
|---|---|---|---|---|---|---|
| doge-t | 0.06906 | 0.07048 | −1.7573 | −1.7315 | +0.026 | −1.03 |
| xrp-t | 1.02050 | 1.04360 | −1.7301 | −1.6895 | +0.041 | −1.02 |

**Exit prices match the exchange EXACTLY.** `fetch_last_closed_fill` worked:
the bot booked the real fill, not the theoretical SL. Visible in xrp — the SL
trigger was 1.0438 and the booked exit is 1.0436, i.e. the actual fill, which
is what the whole method exists to capture.

**Slippage ≈ 0.** DOGE filled exactly at its 0.07048 trigger; XRP filled at
1.0436 against a 1.0438 trigger — marginally *favourable*. Both stops came in
at −1.02/−1.03 R, i.e. 1R plus fees and nothing else. On n=2 this is weak
evidence, but it is evidence *against* the LINK/AVAX depth worry mattering at
this size — and DOGE/XRP are mid-depth names, so the thin ones are still
untested.

**The +0.067 total booking gap is entirely the fee constants**, now fixed
above. Nothing else in the reconcile diverges.

## 2026-08-07 — Full-bot audit: three findings, two non-findings

A deliberate sweep of the money paths, risk controls and program state rather
than a bug hunt. Recording the NON-findings too, because "I checked and it was
fine" is evidence and unrecorded checks get re-done.

**F1. The forward sleeve trackers had never run — 4 weeks unrecorded.** No
`state/`, no track files, not scheduled, not on either box. The XS record is
what gates the BOOK50/XSMOM25/XSBAB25 capital discussion (≥8 weeks from the
2026-07-09 anchor), and it did not exist. **Not lost, though:** both trackers
recompute from public data with ≥1-day-lagged signals, so past values never
revise and the anchor reconstructs the whole track. Recovered in one run —
**30 days now on file** (xsmom +0.59%, xsbab +0.15%; the annualised Sharpes
they print are meaningless at n=30 and are not quoted here). Both are now in
the daily cron. The lesson is not "we nearly lost it" but that a deterministic
job with no schedule silently does nothing, and nothing was watching.

**F2. Nothing enforces the book-level −8% halt line.** It is a HUMAN control:
per-leg decay does not fire until −20% of a leg (~2.5× past the book line),
`DAILY_LOSS_PCT` is 0/disabled (deliberately — it matches the engine), and no
component computes book-level PnL at all. In mitigation it is a slow control:
the worst backtested month is −3.6%, so −8% is a multi-day event and the
12-hourly cron is adequate detection. **Surfaced, not automated** —
`data_health` now prints book PnL, the halt line and headroom, with the honest
caveat that bot equity is REALISED-only so it lags open positions. An
auto-flatten was deliberately NOT built: a bug in one is worse than the gap.

**F3. `fetch_last_closed_fill` passed an unclamped `startTime`.** Bybit's
closed-PnL history is documented/tooled as ~7 days per query, and our own
16-day time stop makes >7-day holds routine — so a position opened 10 days ago
would query outside any such window. The failure mode is SILENT (retCode 0,
empty list), which sends the caller quietly back to the theoretical SL/TP price
and re-opens the booked-vs-real gap the method exists to close. **NOT a
confirmed bug:** probed with a 31-day `startTime` and got retCode 0, but the
account has no closed PnL yet, so truncation could not be distinguished from
"no rows". Clamped to 6.5 days anyway — free, and the scoping is not
load-bearing (rows are newest-first and matched on qty).

**N1 (non-finding). `equity_peak` poisoning is already handled.** The mis-sized
run persisted `equity_peak=898` against a new 112.20 leg = a computed −87.5%
drawdown, which trips the 0.0× decay tier and blocks entries *permanently*. I
expected the alert's "restart to self-heal" advice to be wrong since the peak
is persisted — **it is not wrong**: `Bot.run()` heals a peak that exceeds
`max(equity, starting_equity)*1.05` when `realised_trades == 0`, which is
exactly this case. Correct, deliberate, and already tested by an earlier
incident. No change.

**N2 (non-finding). Entry sizing and risk composition are correct.**
`_effective_risk` composes decay × CHOP × VT × CONF in the same order and form
as the engine's `mult`, and `enter_position` recomputes notional from the
POST-rounding qty (so lot-flooring does not silently misstate risk), checks
SL/TP geometry, and rejects sub-min orders. Parity holds. No change.

## 2026-08-07 — The PULL demotion trigger cannot be both fast and powered

`research/pull_trigger_power.py` — a report measuring the SAMPLE SIZE behind an
existing pre-registered kill criterion, so it can be specified on evidence.

**Arrivals.** 113 PULL closes over 36 months, book-wide across all 8 names:
**3.1/month** (median 3, max 8), and **14% of months contain zero trades**. A
trailing-3-month window holds a median of **9** trades; **100% of windows hold
fewer than 20**.

**Noise floor.** Bootstrapping the leg's own realised R distribution
(mean +0.402, sd 1.997, n=113, win rate 35% — 31% tp / 64% sl, the expected
trend shape of rare large winners), the probability a leg with this leg's TRUE
positive edge nonetheless shows cumulative R < 0 purely by chance:

| N trades | false-demotion rate | 5th pct cumR | months to accumulate |
|---|---|---|---|
| 5 | **44%** | −5.1 | 1.6 |
| 10 | **30%** | −5.9 | 3.2 |
| 20 | 18% | −6.8 | 6.4 |
| 30 | 14% | −5.4 | 9.6 |
| 50 | 8% | −2.7 | 15.9 |

**The current rule sees ~9 trades, i.e. a ~30% false-demotion rate.** It would
wrongly demote a perfectly healthy leg roughly one live quarter in three. That
is not a threshold that needs tuning; it is a rule with no power.

**And it cannot be fixed by raising N.** Getting the false-demotion rate under
10% needs N≈50 = **~16 months** of live record — far beyond L1/L2, and useless
against the pre-2023 disease it exists to catch. *No trigger on this leg can be
both fast and well-powered*, because the leg trades ~3x/month with sd 2.0 R on
a mean of 0.4. That is a property of the strategy's trade rate, not a defect in
the rule.

**Re-specification (replaces the trailing-3-month Sharpe rule):**

1. **Catastrophe stop — automatic.** Demote to BLEND75 or triple-only when
   **cumulative R < −10 over the trailing ≥20 PULL trades**. At N≥20 the 5th
   percentile of chance outcomes is −6.8, so −10 sits outside the noise floor
   and fires on genuine collapse (the pre-2023 leg ran −2.57 Sharpe, not a mild
   sag). Never evaluate below 20 trades.
2. **Slow review — human, not automatic.** At N≥50 (~16 months), cumulative
   R < 0 triggers a REVIEW, not a demotion.

**The consequence worth acting on is about weights, not monitoring.** If PULL
failure is undetectable for ~16 months, the protection cannot come from
watching it — it has to come from how much capital it is given. BLEND50 hands
PULL half the book against a leg whose death we could not notice for over a
year. BLEND75 (25% PULL) halves that exposure. **Not adopted here** — changing
the blend is a strategy change and owes the full gate battery — but it is now
an evidence-backed argument rather than a preference, and it should be settled
before the L3 vol dial multiplies whatever the weights are.

## 2026-08-07 — Collector first look (~2.5 days): spread is a tick, depth is the risk

`research/collector_first_look.py` — a REPORT, no cells, no strategy claim.
2.73M rows over 45.2h observed, 23 symbols. What this sample can and cannot
support, stated up front: it supports STRUCTURAL microstructure and
infrastructure facts; it supports NOTHING conditional, because 2.5 days is one
regime state and zero stress events.

**1. Spread is pinned at exactly one tick, 88–100% of the time.** BTC 99.9% at
0.10, ETH 99.8% at 0.01, SOL 100.0% at 0.01, ADA 99.9% at 0.0001. So
"spread" on these perps is just `tickSize / price` — a constant derivable from
`getInstrumentsInfo` in one call. **Two days of book data confirmed something
that needed no collection at all.** Recording that plainly because the
opposite conclusion — "we measured it, therefore we learned it" — is how
effort gets mistaken for evidence.

**2. The 2bp slip assumption is conservative on 7 of 8 majors, optimistic on
ADA.** MAJORS8 median half-spread **0.65 bp** vs the assumed 2.0. Implied
taker round trip 13.3 bp measured vs 16.0 bp modelled. Per-name half-spread:
BTC 0.01, ETH 0.03, XRP 0.48, LINK 0.61, SOL 0.68, DOGE 0.72, AVAX 0.77 —
and **ADA 2.61**, where the model understates. (GRT 3.46 is worse but untraded.)

**Recommendation: do NOT lower the 2bp assumption.** Lowering a cost makes
every backtest look better, which is the direction to distrust on reflex, and
this window contains no stress-widening whatsoever. Conservative costs are a
feature. Revisit at A2/A3 with variance in the sample. Logged as measured, not
adopted.

**3. The finding that matters: top-of-book depth, not spread.** `book_1s`
carries sizes, so p5 top-of-book notional (thinner side) against real order
sizes:

| sym | p5 top-of-book | order | order as % of p5 | seconds top < order |
|---|---|---|---|---|
| BTC | $8,548 | $65 | 0.8% | 0.19% |
| ETH | $1,213 | $65 | 5.4% | 1.00% |
| ADA | $1,714 | $53 | 3.1% | 0.48% |
| DOGE | $196 | $81 | 41% | 1.76% |
| **LINK** | **$26** | $65 | **253%** | **9.8%** |
| **AVAX** | **$29** | $65 | **221%** | **8.7%** |

For six names our size is negligible. **LINK and AVAX top-of-book is below our
order size ~9% of the time.** Post-only maker ENTRIES are unaffected in cost
(we rest rather than cross — thin depth delays a fill, it does not worsen it).
The exposure is the **SL exit**, which is a taker market order and 67.6% of all
exits: a stop firing into a $26 top-of-book walks levels, at exactly the moment
depth is worst. This sample cannot size that effect — no stop has fired live —
and top-of-book alone never could, which is why depth collection was added
(below).

**4. Liquidations: 1,210/day across 20 symbols** (BTC 507, ETH 413, XRP 381,
SOL 311, ADA 293 per 1.88d). Projects to ~36k by A2 (30d), ~73k by A3 (60d).
Raw counts are not the constraint — cascade studies need CLUSTERED prints, and
whether a stress event falls in the window is not forecastable from here.

**Explicitly still off the table until A2/A3**: liquidation-cascade fades,
funding-settlement microstructure, book-imbalance signals, and any edge
estimate of any kind.

## 2026-08-07 — Collector upgraded: depth buckets, disk guard, gzip verification

Changes batched into ONE restart, because every restart is an unbackfillable
gap in a series whose entire value is continuity.

**`depth_1s` (new, MAJORS8 only).** Cumulative notional within 1/5/10/25 bps of
mid, per side, 1/second. Stores the ANSWER rather than raw ladders: a 50-level
ladder for 8 symbols at 1/s is ~100 GB/yr, these buckets are **~4.1 GB/yr**
(measured 93 B/row), and the buckets are what an execution-cost study consumes.
**Stated trade-off: if a future study needs the raw shape of the book, buckets
cannot reconstruct it.** MAJORS8 only, and behind `DEPTH_SYMBOLS`, because
subscribing 23 symbols to orderbook.50 is ~1150 msg/s of delta parsing on a
1-OCPU box — adding a new series must not risk starving the ones that already
work. `book_1s` (top of book, all 23) continues unchanged.

**Two latent data-loss bugs fixed while in there:**
* `gzip_rotator` deleted the source `.csv` with **no verification**. A short
  write under disk pressure would have left a truncated `.gz` and destroyed
  the only other copy of a day. Now decompresses and reads the whole `.gz`
  before unlinking; on failure it keeps the `.csv` and drops the bad `.gz`.
* `Sink.write` did not catch `OSError`, so a full disk would surface as a
  websocket error in the caller's handler — logging "retrying in 5s" forever
  while recording nothing. Now logged as `SINK WRITE FAILED` with free space.

**`session_1m` (new).** One row per minute: symbol counts and free disk. Makes
gaps EXPLICIT rather than inferred — an unrecorded gap is indistinguishable
from "the market was quiet" at analysis time. ~50 KB/day. Also warns below
`MIN_FREE_MB` (default 2048).

Projected disk: 7.7 → **~12 GB/yr**, ~3 years on the 41 GB free of the
collector's 50 GB boot volume.

## ⚠ 2026-08-06 — TP_LIMIT_ORDERS never worked live: every entry was skipped

The first live start (2026-08-05 11:38, 93 seconds, brought straight down)
placed **zero orders**. Two legs had genuine entry signals — avax-t LONG in
CHOP, ada-t LONG in BULL — and both logged:

    maker entry not placed (bybit {"retCode":10001,"retMsg":"Request parameter
    error."}) — postonly reject or API error; sweeping + skipping this bar

The bot degrades safely (skip the bar), so nothing reached the exchange:
order history, executions and closed-PnL are all **0 on both accounts**. But
it means an L1 run would have sat inert for two weeks looking merely quiet —
the `-p` legs fire ~once per 6 weeks, so "no trades" is exactly what a healthy
book looks like early on. **That is the dangerous part: the failure mode was
indistinguishable from normal.**

Root cause, two faults in `Exchange._attached_sltp_params`, confirmed against
the Bybit V5 docs:

1. **`tpSize` / `slSize` are not `/v5/order/create` parameters.** They belong
   to `/v5/position/trading-stop`. Bybit's documented `linear` Partial example
   carries `tpslMode`/`tpOrderType`/`tpLimitPrice` and **no sizes**. The
   unknown fields are the 10001.
2. **Bybit V5 requires numerics as STRINGS.** ccxt stringifies the params it
   knows (price, qty, stopLoss, takeProfit) but forwards custom ones raw, so
   `tpLimitPrice` went as a JSON float.

**Both entry paths were affected** — the post-only maker entry AND the market
entry used as the `ENTRY_LIMIT_ORDERS=0` fallback, because both attach TP/SL
through the same helper. So the ROADMAP IF→THEN fallback ("Bybit rejects a
post-only entry → set ENTRY_LIMIT_ORDERS=0") would ALSO have failed. That row
is only a real fallback now.

Why nothing caught it: `test_tp_limit_orders.py` asserted the *buggy* shape
(floats, plus tpSize/slSize) — it locked in the defect rather than the
contract. Nothing in the suite validated a payload against Bybit's schema.
`test_order_params.py` now builds the real ccxt payload through a dry
transport (no keys, nothing sent) and asserts: no `/v5/position/trading-stop`
params, no bare numerics, `PostOnly` set, `tpslMode=Partial` present — and
includes a regression case proving the guard rejects the payload that shipped.

**Status: fixed and unit-verified, NOT yet proven live.** Only a real fill
proves it, which is L1's job. The first entry attempt is the thing to watch.

## Bot-type taxonomy triage (2026-08-03) — 32 architectures vs this ledger

Source: `research/Trading Bot Types Reference.pdf` (32 bot types) and
`research/BOT_ARCHITECTURE.md.pdf` (a modular "trading OS" design). Recorded
so the dead column cannot be re-litigated later. **Yield: 3 of 32 untested.**

**The central proposal of BOT_ARCHITECTURE.md — Module 5 "Strategy Selector"
(detect regime → score strategies by confidence → hand control to the winner)
plus Module 12 periodic re-ranking — is the most-rejected idea in this ledger.**
Four independent tests: dynamic rebalance on trailing-3mo Sharpe (+4% vs
+2358% for static equal weight, 4.6y); walk-forward param retune (rejected
twice — 1.15 vs 1.36 frozen, params churn at 70% of refits); IS-performance
name selection (75th/43rd/76th pct OOS); ML entry filter (calibrated, no
discrimination). Generalised by the meta-conclusion: predictive/defensive
selection fails because the losses it targets are not predictable from
signals available at decision time.

**What survives is the distinction we already paid for: regime information is
worth a great deal as a RISK DIAL (CHOP half-size, CONF sizing — both adopted)
and nothing as a SELECTOR.** The supported architecture is therefore *blend,
don't select*: orthogonal payoff shapes at fixed pre-registered weights, which
is what BOOK50/XSMOM25/XSBAB25 already is. No new architecture is needed.

| status | count | types | governing evidence |
|---|---|---|---|
| **Dead — do not re-test** | 19 | Trend (= the deployed book), Mean Reversion, Breakout, Momentum, Sentiment-as-engine, Funding, Correlation, Pairs, Seasonal, ML, RL, Copy Trading, Portfolio Rebalancing (already adopted), Position, Swing, News, Pattern Recognition, Smart Money (ICT/SMC), DCA | CHOP-MR −1.24, BB-MR −0.95, ETHBTC-MR −0.79, donchian OOS −0.28/−0.24, MACD ≤0.45, FNG-CONTRA −0.05, funding IC≈0, calendar +0.23/+0.44 w/ pre −0.13, ML no discrimination, XSVOL +0.49, BREADTH corr 0.82, DOMTREND −0.01. Pattern Recognition and Smart Money have no falsifiable spec — they fail law 2 (pre-registration) before they can be run. Copy Trading is a survivorship leaderboard (same illusion as the grid ROI counter). |
| **Tick-gated** | 7 | Market Making, Scalping, HFT, Arbitrage, Liquidation, Volume, part of Volatility | Bar-data versions already died in the tier-1 moonshot battery at the 22bp taker floor (0/6). ⚠ **Updated 2026-08-09**: the gating premise ("tick history cannot be backfilled") was only half true — Bybit publishes 6.4y of tick TRADES and ~11mo of L2 book free, so trade- and book-derived ideas are testable NOW. Only LIQUIDATION-derived work still depends on the collector's forward record. **Order Flow left this column and is now REJECTED** (IC screen, 2026-08-09). |
| **Genuinely untested** | **3** | **Grid / short-gamma**, **Avellaneda–Stoikov inventory MM**, **IV-vs-RV variance risk premium** | The convexity gap: every sleeve in the stack is long-gamma (trend) or cross-sectional. No short-vol payoff has ever been tested here. |

⚠ **REFUSED on principle, not on evidence: Martingale DCA.** Doubling into
losers is anti-Kelly with unbounded loss and is the exact inverse of the
adopted decay ladder (cut risk at −20/−35/−50%). It also produces the
prettiest possible equity curve until it doesn't. If it ever appears in a
config that is a bug, not a strategy.

⚠ **Test-budget warning (law 2).** `sleeve_battery2` states the arithmetic:
"6 cells; expect ~0–1 false positives at this bar." Running the full taxonomy
would mean 100+ cells at a 5% bar — that manufactures ~5 winners from pure
noise, indistinguishable from real ones. The stack improves by adding ONE
orthogonal payoff shape that survives 2022, not twenty that survived a search.

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
