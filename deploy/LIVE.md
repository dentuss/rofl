# Going live on Bybit — step-by-step

This is the runbook for switching from paper to real money on a Bybit USDT
perpetual account. Follow it in order. **Do not skip the preflight checks**
— they're there because in paper mode every mistake is free; in live mode
some are not.

## 0. Decide which portfolio

**Production allocation (2026-07-02): 5-pair SOFT5 — INJ 25 / SOL 18.75 /
ADA 18.75 / ETH 18.75 / LINK 18.75.** The Bybit-perp robustness study
(`research/portfolio_robustness.py` + `portfolio_softened.py`) found:

- the **8-pair's edge is overfit** — in-sample Sharpe 4.60 decays to 2.70
  out-of-sample, with the worst tails of any book (worst month −9.6%). It is
  **on hold**; do not deploy it on the strength of the older KuCoin numbers.
- the original `inj_heavy` (INJ 40) leans on a single-name concentration the
  backtest rewards but can't risk-price forward;
- **SOFT5** keeps the 5-name selection (97th pct vs the random-5 null OOS),
  posts the best monthly Sharpe (3.81) and consistency (91% positive months),
  and caps the INJ concentration.

| Deposit | Recommended | Why |
|---|---|---|
| $100 – $400 | 5-pair (SOFT5 weights) | Per-bot slices stay above exchange minimums. |
| **$400+** | **5-pair SOFT5** | Best OOS-robust risk-adjusted book; INJ de-concentrated. |
| — | 8-pair | On hold: OOS-overfit (see above). Revisit only with fresh OOS evidence. |

## 1. Bybit account setup (one-time)

1. **Sign up** at bybit.com; complete KYC level 1.
2. **Deposit USDT** to your **Unified Trading Account** (UMA, NOT Funding).
3. **Enable USDT perpetuals** (default on Unified accounts).
4. **Set margin mode to Cross** on the perpetual section (Position Mode
   should be **One-Way Mode**, not Hedge — the bot does not run in hedge mode).
5. **Create API key** at <https://www.bybit.com/app/user/api-management>:
   - **Permissions:** *Contract → Trade* + *Position* (Orders + Positions).
   - **NO withdrawal**, **NO transfer**, **NO sub-account**.
   - **IP whitelist your EC2 public IP** — this is the single most important
     security control. Without it a key leak = drained account; with it the
     key is useless off your server.
   - Save the **API key and secret** somewhere safe. You can't view the
     secret again later.
6. **Funding-rate awareness:** Bybit charges/credits funding every 8h. The
   backtest already models a fair central estimate (≈1bp/8h). For a long/short
   balanced book this nets to ≈0. Don't panic when you see funding line items
   on your account history.

## 2. Sync the box and prep `.env`

```bash
cd ~/rofl
git pull origin main
sudo ./portfolio.sh archive   # snapshot the paper run for the record FIRST
```

Then create the live `.env` (do NOT keep the paper one):

```bash
# WIPE the paper state — paper-mode equity/peak/positions must NOT carry into live.
sudo ./portfolio.sh down -v   # (run with PORTFOLIO=8 too if you tried that)

# Write the live env file. Set TOTAL_EQUITY to YOUR actual Bybit balance.
cat > .env <<'EOF'
MODE=live
EXCHANGE=bybit
PORTFOLIO=5
TOTAL_EQUITY=2300

# SOFT5 production weights (INJ capped at 25%; without these lines the wrapper
# defaults to the old inj_heavy 40/20/15/15/10 split)
INJ_WEIGHT=0.25
SOL_WEIGHT=0.1875
ADA_WEIGHT=0.1875
ETH_WEIGHT=0.1875
LINK_WEIGHT=0.1875

API_KEY=your_bybit_key_here
API_SECRET=your_bybit_secret_here
# API_PASSPHRASE is NOT used by Bybit — leave unset

# Telegram (optional but strongly recommended for live)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_personal_chat_id
EOF

chmod 600 .env   # readable only by you
```

> **Two values you MUST get right or the bot won't start in live mode:**
> 1. `API_KEY` and `API_SECRET` are correct and the key has Trade permission.
> 2. `TOTAL_EQUITY` matches what you actually have on Bybit (the bot doesn't
>    auto-sync — it sizes positions off this number).

## 3. Pre-flight checks (do NOT skip)

```bash
# 1. Test the API key talks to Bybit at all
python3 -c "
import ccxt, os
from dotenv import dotenv_values
env = dotenv_values('.env')
ex = ccxt.bybit({'apiKey': env['API_KEY'], 'secret': env['API_SECRET'],
                  'options': {'defaultType': 'linear'}})
bal = ex.fetch_balance()
print('USDT total :', bal.get('USDT', {}).get('total'))
print('USDT free  :', bal.get('USDT', {}).get('free'))
"
```

Expected: a number close to your deposit. If you get `AuthenticationError`
or `Permission denied`, the key/secret are wrong or the IP isn't
whitelisted. **Fix this before going further.**

```bash
# 2. (Optional) Test Telegram: open Telegram, send /start to your bot, then:
curl -s "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" | jq '.result[-1].message.chat.id'
# Confirm the number matches TELEGRAM_CHAT_ID in .env.
```

## 4. Start live

```bash
sudo -E ./portfolio.sh up -d --build
```

The wrapper prints the computed split BEFORE starting — **check it**. For
SOFT5 at $2300 it must read `INJ 575.0  SOL 431.25  ADA 431.25  ETH 431.25
LINK 431.25`. If it shows the 40/20/15/15/10 amounts, your `*_WEIGHT` lines
didn't load — stop and fix `.env`.

The bot will then run its **own preflight**:
- `LIVE balance check OK: NNN.NN USDT on exchange` — keys work.
- `STATE/SLICE MISMATCH` → **the bot refused to start** because paper
  state survived into live. Run `sudo ./portfolio.sh down -v` then retry.
  (To override, set `TRUST_STATE=1` in `.env` — but really, just wipe.)

Within ~60s:
```bash
sudo ./portfolio.sh status
```
Expect all 5 bots `healthy`, equity equal to the splits (575/431.25×4 at
$2300), all flat.

## 5. What to expect — the first 24-72 hours

| Time window | Normal | Worth investigating |
|---|---|---|
| 0–4 hours | All bots quiet, regime-change logs trickling | Any `ERROR`, `CRITICAL`, or `ORPHAN POSITION` lines |
| First trade | One of the bots opens a position. Telegram: `OPEN LONG/SHORT…` | `entry order FAILED` — check API key, balance, or pair min-cost |
| First close | TP, SL, or time stop. Telegram: `CLOSE…` with PnL | Bot showing position but Bybit doesn't (or vice versa) → check `./portfolio.sh status` |
| Day 1 totals | Total equity ±2% is fine | Sustained −5% with no clear trade explanation → review `events-*.jsonl` |
| Week 1 | 5-15 closed trades, win rate 40–55%, PnL ±10% | Win rate <30% across all bots OR no trades after 5+ days → check the F&G filter isn't blocking everything |

**The bidir backtest median is +5–7%/month with a monthly worst-month
around −10–15%.** Daily moves can be ±5% on either side and still be
normal. The strategy makes money over months, not days — don't intervene
on a slow week.

## 6. Day-to-day operations

```bash
sudo ./portfolio.sh status              # equity + positions across all bots
sudo ./portfolio.sh logs -f --tail=50   # follow logs
sudo ./portfolio.sh logs inj-bot --tail=200 | grep -E "OPEN|CLOSE|ERROR"
sudo ./portfolio.sh archive             # snapshot every week or before stopping
```

**Telegram is your remote eyes** — when you're away from the box you'll
get trade-open / trade-close / regime-change / error pings. If Telegram
goes silent for >12h while bots are healthy, something's wrong with the
notifier (not necessarily the bot).

## 7. Emergency procedures

| Situation | Action |
|---|---|
| **Markets going crazy, want everything OUT** | On Bybit UI: Trading → Close All Positions. Then on box: `sudo ./portfolio.sh down`. State is preserved; you can resume later. |
| **A bot logs `ORPHAN POSITION`** | The bot already tried to force-close. Check Bybit UI; if a position remains, close it manually. The bot will stop trading that pair until you `restart` it. |
| **Equity divergence (bot equity ≠ Bybit balance)** | `sudo ./portfolio.sh down`, manually reconcile (record true balance), then either `down -v` + restart fresh OR set `TRUST_STATE=1` and accept the drift will persist. |
| **Bot won't start: `STATE/SLICE MISMATCH`** | Run `sudo ./portfolio.sh down -v` and start again — wipes state to match exchange balance. |
| **EC2 instance becomes unreachable** | Stop/start via AWS console (state survives), then `sudo ./portfolio.sh status` to verify everything resumed. With `restart: unless-stopped` the bots auto-resume. |

## 8. Scaling up later

Once you're past 2-3 weeks of clean live runtime and the live PnL
broadly tracks paper expectations, you can:

1. **Deposit more.** Just bump `TOTAL_EQUITY` in `.env` AND deposit the
   delta on Bybit. Then `down -v && up -d --build` (wiping state and
   re-initializing at the new amount). Note: this resets PnL accounting —
   archive first.
2. **8-pair: ON HOLD** (2026-07-02). The Bybit-perp OOS study found its edge
   overfit (IS Sharpe 4.60 → OOS 2.70, worst month −9.6%). Everything is wired
   (`docker-compose.bidir8.yml`, cooldown, tg-control) if fresh OOS evidence
   ever reverses the call, but do not switch on the old KuCoin numbers. If you
   do run it: archive, `down -v`, set `PORTFOLIO=8` in `.env` and REMOVE the
   5-pair `*_WEIGHT` lines (INJ/ADA weight overrides leak across portfolios —
   the wrapper aborts if the weights don't sum to 1.0). Needs `t4g.medium`;
   `t4g.small` will OOM with 8 bots.

## 9. The honest risks (read this once)

- **The backtest is in-sample**. Real out-of-sample returns will be
  lower. Expect 50–80% of the backtest's monthly return at best.
- **The 2022-style "everything crashes together" event** will hurt — the
  decay ladder caps the damage but doesn't make it zero. A −25% portfolio
  drawdown month is possible.
- **Bybit can de-list pairs, change fees, or have outages.** The bot
  handles short outages cleanly; multi-day exchange issues need manual
  attention.
- **The bot does not know about anything you do manually on Bybit.** If
  you open a position by hand, the bot may try to close it as if it were
  its own. Don't touch the account except via the bot once live.

Good luck. If you see anything weird in the first week, save the logs
and ping. The data from your first live run will be more useful than
months of paper.
