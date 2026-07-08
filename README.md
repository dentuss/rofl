# rofl — a gate-validated crypto perp trading program

A trend-following bot for Bybit USDT perpetuals, rebuilt from zero after a
2026-07-05 discovery that the original backtest edge was manufactured by an
engine artifact (same-bar re-entry — see the correction section of
[`research/FINDINGS.md`](research/FINDINGS.md)). Everything below survived
the honest rebuild: fixed engine, full cost model (maker entries, TP-as-limit
fills, real per-pair funding, slippage), and a pre-registered gate battery —
IS/OOS stability, sub-window thirds, random-entry nulls, universe
generalization, exec parity, and a 2022-inclusive long-history test.

## The deployed book — BLEND50_CONF (promoted 2026-07-06)

**MAJORS8** (BTC ETH SOL XRP DOGE ADA LINK AVAX, chosen ex-ante by liquidity
— never by backtest performance) × **two entry legs** at 50/50 capital on 4h
bars:

| leg | entry | character |
|---|---|---|
| `-t` triple_bidir | EMA 9/26/50 stack + RSI 55/45 + ADX 22, sl 1.8× / tp 6× ATR | in the market ~40% of the time, the workhorse |
| `-p` pullback_in_trend | EMA50 side + RSI recross of 40/60, same stops | fires ~once per 6 weeks per name, monthly corr to `-t` only 0.17 |

Shared overlay stack, each layer individually gate-passed: walk-forward GMM
regime mask (long in BULL/CHOP, short in BEAR/CHOP), Fear&Greed 3-day
persistence filter, three-tier drawdown decay, CHOP half-sizing, vol
targeting (60% ann), GMM-confidence sizing, post-SL cooldown. Execution is
maker on both sides: post-only limit entries and TP as a resting limit
(engine-parity implementations, unit-tested + exec-parity-verified).

**Honest numbers** (fixed engine, full costs, 2023-08 → 2026-07, daily
granularity; `research/deploy_report.py`):

| sizing | CAGR | Sh(mo) | dMDD | worst month | IS → OOS |
|---|---|---|---|---|---|
| unit weights (live today) | 10.4% | 1.50 | −4.5% | −1.7% | 1.47 → 1.51 |
| @25% vol dial (x3.5, later) | 37.9% | 1.48 | −15.1% | −5.8% | 1.44 → 1.49 |

Full-history anchor (2022-inclusive pseudo-OOS): blend Sharpe **+1.20**,
pre-2023-08 **+0.18** — the book survived the bear; it did not print in it.
Judge it accordingly.

## Status

- **Live-first go-live program running** — see
  [`research/ROADMAP.md`](research/ROADMAP.md) Phase 6 (stages L0–L3 with
  pre-registered kill criteria) and the runbook
  [`deploy/LIVE.md`](deploy/LIVE.md).
- Two-account layout is REQUIRED: triple book on the main account, pullback
  book on a Bybit sub-account (same symbols would net against each other on
  one account).
- Research sleeves (TSMOM-90, funding carry) failed the long-history gate
  twice and are demoted to paper forward-tracking (`sleeves_paper.py`).

## Layout

| | |
|---|---|
| `bot.py` | the live/paper executor (one symbol per process) |
| `core/` | strategies, engines (fixed), regime GMM, risk, data, funding |
| `research/` | every experiment, pre-registered; `FINDINGS.md` = adopted/rejected ledger, `ROADMAP.md` = program state |
| `docker-compose.bidir4h-live.yml` | the 16-leg live stack + Telegram panel |
| `docker-compose.bidir4h-paper.yml` | keyless paper twin |
| `deploy/` | EC2 setup + the go-live runbook |
| `test_*.py` | plain-assert suites: engines, sizing, maker entries, TP limits, reduce-only closes, exec parity |
| `tg_control.py` | Telegram button panel (Stats/Positions/Today/Health/Reconcile), dual-account read-only |

## Method (the part that matters)

Every change enters through `research/` with pre-registered cells, runs on
the fixed engine with the full cost model, and must pass the gates before
touching the bot. Negative results get recorded as prominently as wins —
the rejected list in `FINDINGS.md` (donchian, walk-forward re-tuning, the 1d
arm, naive breadth, performance-picked universes, pooled regimes, …) is the
reason the adopted stack can be trusted. When in doubt, read `FINDINGS.md`
before believing any number in a doc — including this one.
