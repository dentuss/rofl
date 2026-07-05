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
| **8-pair equal-weight portfolio (go-live, 2026-07-02)** | The KuCoin in-sample edge (hourly Sharpe 3.70 vs 5p 3.37) did NOT survive the honest re-test on Bybit perp with monthly Sharpe + OOS holdout: IS 4.60 → OOS **2.70** (worst decay of any book), worst month −9.6% vs 5p −3.3%, and only 94th pct vs 500 random 8-baskets OOS (selection-driven). Both 5-pair books beat it OOS. ON HOLD — everything stays wired (compose, cooldown, tg-control) if fresh OOS evidence reverses this. | portfolio_robustness.py, portfolio_compare.py |
| **Post-TP same-side cooldown (K_tp=2/3)** (2026-07-05, on the FIXED engine) | The live-blockable post-TP re-entries are zero-EV: gap-1 n=1756 meanR **+0.02**, gap-2 n=56 meanR −0.11; chase re-entries actually outperform pullbacks (+0.10R vs −0.06R). Blocking costs a hair (ΔSh −0.25/−0.22 vs FIXED_K4). The 2026-07-03 ADA TP→re-enter→SL round trip was variance, not a systematic leak. Engine knob `cooldown_bars_tp` stays (default 0) for future re-tests. | tp_cooldown_htf_bias.py |
| **HTF risk-size bias** (1D/4h EMA50, counter-trend risk ×0.5/×0.75, via `with_htf_risk_bias`) | Cannot rescue a no-edge base: best cell (1D ×0.5) is ΔSh +0.02 / CAGR +0.3pp over FIXED_K4 (MDD −20.3→−15.2 is the one bright spot); OOS Sh 0.84 vs IS 0.07 is an unstable inversion = noise, and 4h is flat-negative. Re-tried on the honest 4h base (graveyard 2026-07-05): ΔSh −0.32, MDD worse — rejected again. | tp_cooldown_htf_bias.py, graveyard_retrial.py |
| **Naive breadth (EW-23 universe)** (2026-07-05) | Indiscriminate widening DILUTES: EW-23 Sh 0.46 vs SOFT5-family ~1.0; only 12/23 names profitable. Edge is cross-sectionally heterogeneous — lives in liquid trending majors (ETH +1.11, SOL +0.90, BTC +0.77, RUNE, NEAR), bleeds in legacy alts (LTC −1.58, ETC −1.55, FIL −1.38, AAVE −1.09). | breadth_allin.py |
| **IS-performance name selection** (2026-07-05) | The honest version of "pick winning pairs": rank on IS window only, freeze basket, judge OOS vs 300 random same-size subsets → 75th/43rd/76th pct (K=5/8/10). Trailing pair performance does NOT persist enough to select on. Universe choice must be structural (liquidity/class), never backtest ranking. | breadth_select.py |
| **Partial TP + breakeven — re-trial** (2026-07-05) | Rejection upheld honestly: 2.0 ATR −9.6pp CAGR / −0.48 Sh; 3.0 ATR −6.0pp / −0.17. Winners must run (the old "−27% catastrophic" number was artifact-inflated but directionally right). | graveyard_retrial.py |
| **ADX 25 entry gate — re-trial** | −0.13 Sh, −1.6pp CAGR on the honest 4h base. | graveyard_retrial.py |
| Chop filter min_pct=0.003 — re-trial | UNINFORMATIVE on 4h (threshold tuned for 1h never fires — identical results). Needs re-parameterization for a real trial. | graveyard_retrial.py |

## Promising — UNDER VALIDATION (not adopted; do not deploy on this evidence)

| Idea | Preliminary result | Caveats | Script |
|---|---|---|---|
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
