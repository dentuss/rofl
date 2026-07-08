# Going live — the 4h BLEND50_CONF book (runbook, 2026-07-08)

This is the complete deployment guide for the **live-first program**
(research/ROADMAP.md, Phase 6). Follow it top to bottom; every section gates
the next. The deposit this runbook is sized for: **$2,177.56 USDT total**.

**What you are deploying** — the promoted trend book, every layer gate-passed
on the fixed engine with full costs (see `research/FINDINGS.md`):

- **MAJORS8** (BTC ETH SOL XRP DOGE ADA LINK AVAX) × **two entry legs** at
  50/50 capital: `-t` = triple_bidir (EMA stack + RSI + ADX, tp 6×ATR),
  `-p` = pullback-in-trend (EMA50 side + RSI 40/60 recross, tp 6×ATR)
- Overlays: walk-forward GMM regime mask, F&G 3-day persistence, drawdown
  decay tiers, CHOP half-sizing, vol targeting (60% ann), GMM-confidence
  sizing
- Execution: **post-only maker entries** (`ENTRY_LIMIT_ORDERS`) and
  **TP as a resting limit** (`TP_LIMIT_ORDERS`) — the exact cost model the
  backtests price
- 16 bot containers + 1 Telegram control panel, one compose file

**Honest expectations at unit weights** (what L1/L2 runs; fixed engine, real
funding, 2023-08 → 2026-07, start $2,177.56 — `research/deploy_report.py`):

| | final$ | CAGR | Sh(mo) | dMDD | worst day | worst month | win% | IS → OOS |
|---|---|---|---|---|---|---|---|---|
| **UNIT WEIGHTS (live now)** | 2,901 | **10.4%** | **1.50** | **−4.5%** | −1.5% | −1.7% | 57 | 1.47 → 1.51 |
| @15% vol (x2.1) — L3 later | 3,883 | 22.2% | 1.49 | −9.2% | −3.1% | −3.5% | 57 | 1.46 → 1.50 |
| @25% vol (x3.5) — L3 later | 5,510 | 37.9% | 1.48 | −15.1% | −5.2% | −5.8% | 57 | 1.44 → 1.49 |

Anchor on the **full-history Sharpe ~1.2** (the 2022-inclusive gate), not the
table's 1.5: expect the live experience to be *worse* than the common-window
numbers, and treat matching them as upside. Typical L1 month: **±1–3%**
(±$25–70). The pullback legs trade ~once per 6 weeks per name — weeks of
idle `-p` containers are CORRECT, not broken.

---

## 1. Box prep (one-time) — `deploy/setup.sh`

On a fresh EC2 instance (t4g.medium or larger — 17 containers; t4g.small
will OOM), as `ec2-user`/`ubuntu` (not root):

```bash
curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/setup.sh | bash
# or, with the repo already cloned:
bash deploy/setup.sh
```

It installs Docker + Compose v2, clones/pulls the repo to `~/rofl`, and
**pre-builds the image without starting anything**. Log out and back in (or
`exec sg docker`) so the docker group takes effect.

Sanity: the box clock must be NTP-synced — Bybit auth breaks on skew
(`timedatectl` → "System clock synchronized: yes").

## 2. Bybit accounts — TWO of them (this is not optional)

The `-t` and `-p` legs trade the **same symbols**. Two bots on one account
would net against each other (a pullback short literally reduces the triple
long) and trip every reconcile guard. The pullback book therefore lives on a
**sub-account**.

**Main account (triple book, $1,088.78):**
1. Unified Trading Account, USDT in UTA (not Funding), **Cross margin**,
   **One-Way** position mode (never Hedge).
2. API key: permissions *Contract → Orders + Positions* ONLY — **no
   withdrawal, no transfer**. **IP-whitelist the EC2 public IP** (the single
   most important security control).

**Sub-account (pullback book, $1,088.78):**
1. Bybit → Account → Subaccount Management → create a **Standard**
   sub-account with UTA.
2. Transfer **$1,088.78 USDT** main → sub ($300.02 for BTC-p + 7 × $112.68).
3. Same margin/position-mode settings as main (Cross, One-Way).
4. Cut the sub-account its **own API key**: same trade-only permissions,
   same IP whitelist.

Leave **$1,088.78** on the main account for the triple book. After the
transfer, main and sub each hold half the book.

## 3. `.env` — the only place secrets live

```bash
cd ~/rofl
cat > .env <<'EOF'
# --- main account = triple (-t) legs -------------------------------------
API_KEY=your_MAIN_key
API_SECRET=your_MAIN_secret

# --- sub-account = pullback (-p) legs ------------------------------------
# Empty values are SAFE: the -p legs fail auth loudly and trade nothing.
PULL_API_KEY=your_SUB_key
PULL_API_SECRET=your_SUB_secret

# --- Telegram (strongly recommended: fills, errors, the control panel) ---
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# --- sizing (defaults already match the $2,177.56 deposit) ---------------
# BTC4H_LIVE_EQUITY=300.02
# LEG4H_LIVE_EQUITY=112.68
EOF
chmod 600 .env
```

Compose reads `.env` automatically from the repo directory. The per-leg
splits are compose defaults — you only uncomment the two sizing lines to
override them (L3 dial-ups later happen here).

## 4. Preflight (do not skip)

```bash
# Keys talk to Bybit and see the right balances (~1088.78 each):
python3 - <<'PY'
import ccxt
from dotenv import dotenv_values
env = dotenv_values('.env')
for tag, k, s in (("MAIN", env.get('API_KEY'), env.get('API_SECRET')),
                  ("SUB ", env.get('PULL_API_KEY'), env.get('PULL_API_SECRET'))):
    ex = ccxt.bybit({'apiKey': k, 'secret': s, 'options': {'defaultType': 'linear'}})
    bal = ex.fetch_balance()
    print(tag, "USDT total:", bal.get('USDT', {}).get('total'))
PY
```

Expected: MAIN ≈ 1088.78, SUB ≈ 1088.78. `AuthenticationError` = wrong
key/secret or missing IP whitelist — fix before continuing.

```bash
# Telegram wired? (send /start to your bot first)
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"id":[0-9]*' | head -1
```

## 5. Start — L1 live shakedown

```bash
cd ~/rofl
docker compose -f docker-compose.bidir4h-live.yml up -d --build
docker compose -f docker-compose.bidir4h-live.yml ps      # 17 services healthy
docker compose -f docker-compose.bidir4h-live.yml logs -f --tail=20
```

Within a minute each bot logs its config line (`starting bot mode=live …`)
and the Telegram panel (`rofl4hL-tg`) answers with the button menu — press
**Stats**: 16 legs, booked equity ≈ 2,177.56, real equity per account shown.

**Week-1 checklist (L1, from ROADMAP Phase 6)** — all of these observed:

- [ ] first `PENDING LONG/SHORT maker limit …` log line (post-only accepted)
- [ ] first `OPEN … (maker fill)` — a maker entry actually filled
- [ ] first TP-limit fill (position closed by the resting target order)
- [ ] Partial/Limit TP attach accepted on all 8 symbols (no
      `TP_LIMIT_ORDERS` rejections in logs)
- [ ] a `-t` and `-p` position coexisting on the same symbol with zero
      reconcile complaints (the two-account isolation working)
- [ ] one deliberate restart (`docker compose … restart btc-t`) while an
      order rests — the pending survives and resolves
- [ ] `min-notional`/`skipping order` lines rare (only on the smallest
      risk-scaled entries)

Fallback flags (documented in the compose): if Bybit rejects an order type
on some symbol, set `TP_LIMIT_ORDERS=0` and/or `ENTRY_LIMIT_ORDERS=0` in
`.env`… then `up -d` again and note the economics delta (taker entries cost
~4bp+slip more; expectations shift down 1–3pp CAGR).

## 6. Operations

```bash
docker compose -f docker-compose.bidir4h-live.yml ps                     # health
docker compose -f docker-compose.bidir4h-live.yml logs btc-t --tail=100  # one leg
docker compose -f docker-compose.bidir4h-live.yml logs -f | grep -E "OPEN|CLOSE|PENDING|ERROR|CRITICAL|HALT"
```

Telegram buttons: **Stats** (equity/DD per leg + real-vs-booked), 
**Positions** (live from both accounts, `·sub` = pullback book), **Today**,
**Health** (heartbeats), **Reconcile** (booked vs real equity gap).

Weekly (L2 measurement): compare the trade log against engine expectations —
entry fills vs signal closes, TP fill rate, funding paid. The measured
slippage feeds back into the cost model; >0.2 Sharpe degradation halts the
program (ROADMAP Phase 6).

## 7. Kill criteria (pre-registered — act, don't deliberate)

| Trigger | Action |
|---|---|
| Book equity −8% from deploy (≈ −$175) at unit weights | halt new entries (`down`), full review before restart |
| A bot logs `HALTED` (side conflict / extra size / untracked position) | it already stopped touching the exchange; reconcile that symbol on Bybit manually, then restart the service |
| `pending entry status unknown >2 bars` Telegram alert | check the order on Bybit; entries on that leg are blocked until it resolves |
| PULL legs' trailing-3-month Sharpe < 0 | demote to BLEND75 or triple-only (pre-registered fallback; ask for the reweighted compose) |
| Any UNEXPECTED exec divergence vs the engine | halt, diagnose, re-run `test_exec_parity.py` before resuming |
| Emergency — want everything flat NOW | Bybit UI → Close All Positions on BOTH accounts, then `docker compose -f docker-compose.bidir4h-live.yml down` |

Never touch the accounts manually while the stack runs (the bots own them);
never run a second live portfolio next to this one.

## 8. L2 → L3: the dial

After **L1 (≥2 weeks) + L2 (2–4 weeks)** are green and measured costs match
the model: the first dial is **15% vol** — multiply both equity lines in
`.env` by ~2.1 (`BTC4H_LIVE_EQUITY=630`, `LEG4H_LIVE_EQUITY=237`) **only if
the wallet actually holds that much**, or keep the deposit and accept that
the dial is really a leverage decision (the bot sizes off STARTING_EQUITY).
Honest L3 expectations: ~15–22%/y, dMDD ~−9%, worst day ~−3%. The 25% dial
(x3.5, ~38%/y, −15% dMDD) is a separate decision after a full quarter of
live record matching the model. Archive state before any resize
(`down` + copy volumes), then `down -v && up -d` to re-initialize splits.

## 9. The honest risks

- The trend book passed the 2022-inclusive long-history gate at **+0.18**
  pre-2023 Sharpe — it survived the bear, it did not print in it. A repeat
  of 2022 at unit weights looks like months of −1–2% grinding.
- The pullback leg alone did NOT pass the pre-2023 gate (−2.57) — it is
  carried by the blend and has a pre-registered demotion trigger. Watch it.
- Maker entries mean MISSED trades are normal (the engine models the same
  miss). A signal that runs away without filling is not a bug.
- Bybit can reject the Partial/Limit TP attach, delist a pair, or have an
  outage. Short outages self-heal (restart-resume is tested); multi-day
  issues need hands.
- The backtest's monthly numbers are common-window (2023-08+). The honest
  full-history anchor is lower. Judge the program on L2's reconcile, not on
  week-1 P&L.
