# Research findings — what worked, what didn't

A running log of experiments so we don't re-litigate dead ends. All on
5y (or max-available) 1h data, production preset = triple_bidir +
directional regime filter + F&G extreme filter + three-tier decay, funding
modeled. Scripts that produced each result are named.

## Adopted (live in production)

| Change | Effect | Script |
|---|---|---|
| Bidirectional strategy (`triple_bidir`) | +73%→+146% CAGR on INJ; shorts profitable on all 7 pairs | test_short_adapt.py |
| Directional regime filter | long in BULL/CHOP, short in BEAR/CHOP — avoids countertrend | test_improvements.py |
| F&G extreme-zone filter (≥80/≤20) | MDD −33%→−28%, return flat | test_improvements.py |
| **F&G 3-day persistence** (superseded single-day) | only block ENTRENCHED extremes; flash-extreme continuation shorts are profitable. INJ +25pp CAGR / +0.14 Sharpe, portfolio +24pp CAGR / +0.23 Sharpe, same MDD | test_fng_soften.py |
| Three-tier decay (−20/−35/−50%) | free on healthy pairs; LTC MDD −69%→−51% AND higher final | test_decay_funding.py, decay on LTC/BTC |
| Multi-pair portfolio (inj_heavy) | MDD −28%→−18%, Sharpe 1.75→1.95, worst-month −16%→−7% | multipair_bidir.py |
| scikit-learn bundled | adaptive presets actually run the regime GMM | — |
| **Post-stop same-side re-entry cooldown** (K=3 bars; env `COOLDOWN_BARS`) | After a SL on side X, block new side-X entries for 3 bars — kills the post-stop V-bounce whipsaw. Full prod stack, 5 pairs, 3.7y: portfolio Sharpe **2.27→3.46**, worst month **−8.6%→−2.6%**, MDD −19.8%→−17.2%, beats baseline 41/44 months; OOS (held-out 40%) ΔSharpe +1.10. Placebo control confirms it is post-SL-same-side-specific. Implemented in bot.py + both backtest engines; exec-parity locked (live==backtest to the cent). | test_reentry_cooldown_prod.py, cooldown_report.py |

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

## Promising — UNDER VALIDATION (not adopted; do not deploy on this evidence)

| Idea | Preliminary result | Caveats | Script |
|---|---|---|---|
| ~~Post-stop same-side re-entry cooldown~~ | **PROMOTED to Adopted (2026-06-29)** after passing with-regime + walk-forward + OOS validation. See the Adopted table. | — | test_reentry_cooldown_prod.py |
| **ETH → AAVE swap / drop ETH** | Recent 400d: ETH→AAVE beats keep on all metrics (Sharpe 1.92→2.16, ret +174→+194%, MDD −28→−22%); drop-to-4 mild + (Sharpe 2.08). | ETH is NOT a bad pair (+107% standalone, mid-pack; its live 0/4 week was variance). AAVE's edge is recent-window-specific = the "chase recent winners" trap. Keep ETH; revisit only with multi-window + with-regime evidence. | test_eth_swap.py |

## Universe + portfolio construction (research/expand_universe.py, portfolio_construction.py)

- **Strategy is NOT INJ-specific.** Of 33 pairs swept, 20 clear Sharpe ≥ 1.1;
  RUNE/AAVE/DOGE/AVAX/GRT/NEAR all rival or beat 4 of the current 5.
- **Diversification depth:** best-N equal-weight Sharpe peaks at N≈7 (2.44) then
  slowly dilutes; tail keeps improving with N (worst month −16%→−9%, MDD −24%→−18%).
  Sweet spot ≈ 7-8 pre-screened pairs, equal-weight.
- **Pair performance is period-dependent** — ETH Sharpe 1.31 over 8.7y but 0.78 on
  the recent 4.6y window; FIL 1.69 full-history vs 0.74 recent. So "pick the top-N
  Sharpe" is overfitting; the robust play is a broad equal-weight basket.
- **Weighting:** current inj_heavy is near-optimal on the 5; equal-weight gives the
  best worst-month; leaning into recent winners (SOL/ADA) is the worst.

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
