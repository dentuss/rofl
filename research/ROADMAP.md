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
- [ ] TP-as-limit (maker on the target side) once the bot's execution layer
      is touched for maker entries

## Phase 3 — Re-trial of the artifact-era graveyard (on the 4h base)

Each was "rejected" by an engine that paid a bounty per TP fired — all void:
- [ ] Partial TP + breakeven ("catastrophic −27%" — verdict rendered by the bug)
- [ ] Chop filter (CI/efficiency ratio), stricter ADX
- [ ] Per-regime risk sizing ("+0.10 Sharpe, −36% return")
- [ ] HTF risk bias (1D over 4h is a different geometry than 1D over 1h;
      the MDD −20→−15 hint from r2 deserves its shot on the 4h base)
- [ ] SL cooldown re-validation for 4h (currently OFF; find honest K if any)

## Phase 4 — Signal frontier

- [ ] Honest walk-forward param re-tune on 4h (retune.py machinery, fixed
      engine, gates G1–G4) — current params were tuned under the artifact
- [ ] 1d arm + 4h/1d ensemble (entries on 4h, exposure scaled by 1d state)
- [ ] Entry family variants: pullback-in-trend vs breakout; donchian on 4h
- [ ] Universe expansion: the 20 viable names from expand_universe.py on the
      4h stack → portfolio breadth is the cheapest Sharpe there is
- [ ] Regime layer upgrades (the GMM is the strongest verified layer):
      finer features, per-pair vs pooled fits, confidence-weighted sizing

## Phase 5 — Capital efficiency (how real systems reach 30–70%)

- [ ] Vol-targeted position sizing (risk scales to realized vol, not fixed 2%)
- [ ] Correlation-aware weights (honest re-test; the old study was void)
- [ ] Leverage schedule against an explicit MDD budget (e.g. target −15%:
      with Sharpe ~1.5 base, modest leverage compounds honestly)
- [ ] Only after G1–G5 on everything above: sizing-up discussion

## Scoreboard (fixed engine, full costs, SOFT5 unless noted)

| date | config | CAGR | Sh(mo) | MDD | gates | status |
|---|---|---|---|---|---|---|
| 2026-07-05 | 1h production (old live) | 0.6% | 0.22 | −20% | — | retired |
| 2026-07-05 | 4h + tp6 (r3 cost model) | 15.1% | 0.98 | −9.6% | G1–G4 ✓ | superseded by ALL_IN |
| 2026-07-05 | **ALL_IN: 4h + tp6, entry-bar check, real funding, maker entries** | **13.2%** | **0.88** | **−10.3%** | G1–G4 ✓ (OOS Sh 1.14) | **trustworthy baseline — all future work measured against this** |
