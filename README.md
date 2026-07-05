# rofl — adaptive bidirectional crypto trading bot

A trend-following perpetual-futures bot for crypto. Trades **long and short**,
switches direction by detected market regime, and sizes risk down as drawdown
deepens. Runs in **paper mode by default** — no keys, real market data — and
goes live on Bybit (or KuCoin/OKX) with a one-line config change.

> **⚠ STATUS CORRECTION (2026-07-05):** a backtest-engine realism bug
> (same-bar re-entry at the bar's open after an intra-bar TP/SL exit — a
> chronologically impossible fill) manufactured essentially ALL of the
> backtested edge. With realistic fills, SOFT5 on Bybit 2.88y measures
> **~0% CAGR / Sharpe(mo) 0.22 / MDD −20%** — no demonstrated edge. Every
> performance claim below and in the runbooks predates this correction and
> is inflated. See `research/FINDINGS.md` (correction section) before
> risking money. The engines are fixed by default
> (`legacy_same_bar_reentry` reproduces old numbers).
>
> Prior status (pre-correction, for context): production (2026-07-02) is the
> **SOFT5 5-pair** (INJ 25 / SOL, ADA, ETH, LINK 18.75) with the post-SL
> re-entry cooldown; the 8-pair basket is on hold (OOS-overfit).

---

## The strategy in one screen

**Entry — `triple_confirm_bidir`** (`core/strategies.py`). Three confirmations,
mirror-imaged for longs and shorts:

| | Long | Short |
|---|---|---|
| EMA stack | fast > slow > trend (9/26/50) | fast < slow < trend |
| Momentum | RSI(14) > 55 | RSI(14) < 45 |
| Trend strength | ADX(14) > 22 | ADX(14) > 22 |
| Stop / target | 1.8× / 3.0× ATR(14) | symmetric |

Then four defensive layers, each validated to add value (or, where they didn't,
documented in [`research/FINDINGS.md`](research/FINDINGS.md) and left out):

1. **Directional regime filter** — a Gaussian-Mixture model labels each bar
   BULL / CHOP / BEAR (walk-forward, no look-ahead). Longs are allowed only in
   BULL/CHOP, shorts only in BEAR/CHOP. Avoids fighting the macro trend.
2. **Fear & Greed extreme filter** — blocks longs at F&G ≥ 80 (euphoric tops)
   and shorts at F&G ≤ 20 (capitulation bottoms). Cuts drawdown ~6pp for free.
3. **Three-tier drawdown decay** — risk-per-trade scales down as equity
   drawdown deepens: ×0.5 at −20%, ×0.25 at −35%, stop at −50%. On healthy
   pairs the deep tiers never fire (zero cost); on a death-spiral pair (LTC)
   they cut max drawdown −69% → −51% *and raise* final equity.
4. **Equity-curve risk sizing** — risk is a fixed % of current equity, so the
   book compounds up and de-grosses down automatically.

**Why bidirectional matters:** shorts contributed positively on *all 7* tested
pairs (~45% of total profit on INJ). In 2024 the long-only version lost 12% on
INJ while the bidirectional version made +131% — the shorts carried the bear.

## What we tried and rejected

The strategy is deliberately simple because almost everything we layered on top
*failed* to add edge — documented honestly in
[`research/FINDINGS.md`](research/FINDINGS.md). Rejected: ML entry filters
(×2 attempts), choppiness/efficiency chop detectors, a strategy-health gate,
partial take-profit, fresh-crossover gating, finer regime taxonomies, and
walk-forward parameter retuning. The recurring lesson: the losses these
filters target aren't predictable from entry-time signals. **Gains came from
risk management and diversification, not more filtering.**

---

## Quick start (paper mode, local)

```bash
pip install -r requirements.txt
STRATEGY_PRESET=adaptive_inj_bidir python3 bot.py
```

That runs the production single-pair preset on real INJ/USDT market data with
no API keys and no real money. Watch `bot.log` or check status:

```bash
python3 bot_status.py
```

### Docker (single pair)

```bash
docker compose up -d --build           # adaptive_inj_bidir by default
docker compose logs -f
```

### Docker — diversified portfolio (recommended)

One `portfolio.sh` wrapper drives both:

```bash
TOTAL_EQUITY=100 sudo ./portfolio.sh up -d --build         # 5-pair, inj_heavy
PORTFOLIO=8 TOTAL_EQUITY=100 sudo -E ./portfolio.sh up -d --build  # 8-pair, equal
sudo ./portfolio.sh status                                  # per-bot health/equity/positions
sudo ./portfolio.sh archive                                 # snapshot to archives/ for the record
```

| Portfolio | Pairs | Status |
|---|---|---|
| **5-pair SOFT5** (25/18.75×4) — **production** | INJ, SOL, ADA, ETH, LINK | Bybit-perp, cooldown on: Sharpe(mo) 3.81, OOS 3.01, 91% pos months (`research/portfolio_softened.py`) |
| 5-pair `inj_heavy` (40/20/15/15/10) — superseded | INJ, SOL, ADA, ETH, LINK | Wrapper default; production overrides via `*_WEIGHT` in `.env` (caps the INJ concentration) |
| 8-pair equal-weight — **on hold** | INJ, AVAX, NEAR, AAVE, GRT, RUNE, DOGE, ADA | OOS-overfit: IS Sharpe 4.60 → OOS 2.70, worst mo −9.6% (`research/portfolio_robustness.py`) |

The bidirectional strategy decorrelates the pairs (avg pairwise monthly correlation 0.16) so diversification actually delivers — see [`research/FINDINGS.md`](research/FINDINGS.md) for the full universe sweep and rejected ideas.

### Going live

Put credentials in `.env` and flip the mode (paper → live):

```
MODE=live
EXCHANGE=bybit
API_KEY=...
API_SECRET=...
```

```bash
docker compose --env-file .env up -d --force-recreate
```

**Full EC2 deployment guide, Telegram alerts, going-live checklist, and
troubleshooting:** [`deploy/README.md`](deploy/README.md).

---

## Presets

| Preset | What |
|---|---|
| **`adaptive_inj_bidir`** | **Production single-pair** — bidir + regime + F&G + decay on INJ 1h |
| `adaptive_inj_bidir_wf` | Experimental — adds weekly walk-forward param retune (see deploy guide) |
| `adaptive_bidir` | Symbol-agnostic bidir (used by the portfolio launcher; point at any pair via `SYMBOL=`) |
| `adaptive_inj_high_return` | Conservative long-only with ML BEAR-skip |
| `safer_inj_high_return` | Deterministic long-only, no ML dependency |

Multi-pair launchers: `docker-compose.bidir-portfolio.yml` (Docker) or
`./run_bidir_portfolio.sh` (bare). Older single-strategy portfolio:
`run_portfolio.sh` / `docker-compose.portfolio.yml`.

---

## Repository layout

```
bot.py                      Live trading bot (entrypoint). Paper + live.
bot_status.py               Equity/PnL/position snapshot across all bots.
risk_analyzer.py            Monte Carlo + Kelly + edge-significance analyzer.
test_parity.py              Asserts live signals == backtest signals (no drift).
core/
  strategies.py             triple_confirm_long / triple_confirm_bidir + others
  backtest.py               Event-style vectorized backtester
  backtest_enhanced.py      + partial TP, daily-loss limit, decay tiers, health gate
  regime.py / regime_strategy.py   GMM regime detection (walk-forward)
  risk.py                   Shared decay ladder (bot + backtester use identically)
  funding.py                Real historical funding (Bybit/OKX) for backtests
  filters.py                Chop / health filters (research-only, see FINDINGS)
  ml_filter.py              ML entry filter (research-only, rejected)
  indicators.py             EMA/RSI/ADX/ATR/Bollinger/Supertrend/Choppiness/ER
  sentiment.py              Crypto Fear & Greed index
  notifier.py               Telegram push notifications
  event_log.py              Structured JSONL event log
deploy/                     EC2 deployment guide + setup.sh
research/                   Backtests + validation scripts; FINDINGS.md is the log
```

## How it's validated

- **`test_parity.py`** — walks the live signal generator bar-by-bar over history
  and asserts it matches the backtest exactly (0 mismatches). Run before any
  live deploy.
- **`research/validate_sweep.py`** — runs the production preset across all
  long-history pairs; the robustness evidence behind the headline numbers.
- **`research/FINDINGS.md`** — the running log of every experiment, adopted and
  rejected, with the script that produced each result.

## Caveats

- Backtests are **in-sample** — parameters were chosen on this same history.
  Real out-of-sample returns will be lower. Always paper-trade first.
- Crypto perps carry **funding** (modeled in backtests; paid live automatically).
  For a balanced long/short book it roughly cancels, but spikes during big moves.
- Leverage and shorting can lose money fast. Use money you can afford to lose,
  IP-whitelist your API keys, and never grant withdrawal permission.
