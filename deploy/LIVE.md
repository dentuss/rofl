# Going live — the 4h BLEND50_CONF book (runbook, 2026-07-08)

This is the complete deployment guide for the **live-first program**
(research/ROADMAP.md, Phase 6). Follow it top to bottom; every section gates
the next. The deposit this runbook is sized for: **$1,800.00 USDT total**
(updated 2026-07-30; was $2,177.56).

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
funding, 2023-08 → 2026-07, start $1,800.00 — `research/deploy_report.py`;
percentages are deposit-invariant, only the final-$ column scales):

| | final$ | CAGR | Sh(mo) | dMDD | worst day | worst month | win% | IS → OOS |
|---|---|---|---|---|---|---|---|---|
| **UNIT WEIGHTS (L1 runs this)** | 2,358 | **9.5%** | **1.33** | **−5.6%** | −1.4% | −3.6% | 61 | 1.45 → 1.19 |
| @15% vol (x2.1) — L3 later | 3,112 | 20.3% | 1.32 | −11.4% | −3.0% | −7.5% | 61 | 1.44 → 1.20 |
| @25% vol (x3.5) — L3 later | 4,322 | 34.4% | 1.32 | −18.3% | −5.0% | −12.2% | 58 | 1.42 → 1.21 |

> Re-measured 2026-08-03 after July 2026 closed at −3.64%, the worst month in
> the window. The older 10.4 / 1.50 / −4.5 / −1.7 row predates it.

Anchor on the **full-history Sharpe ~1.2** (the 2022-inclusive gate), not the
table's 1.5: expect the live experience to be *worse* than the common-window
numbers, and treat matching them as upside. Typical L1 month: **±1–3%**
(±$25–70). The pullback legs trade ~once per 6 weeks per name — weeks of
idle `-p` containers are CORRECT, not broken.

---

## 1. Box prep (one-time) — `deploy/setup.sh`

> ⚠ The host is now **Oracle A1 (free)**, not EC2. Run
> `deploy/init-trading.sh` instead of the manual steps below — it does clock,
> swap, Docker, `.env` template and the build, then deliberately stops. See
> `deploy/ORACLE.md`. The EC2 path is kept for reference.

On a fresh EC2 instance — **t4g.medium WITH a 4 GB swapfile, or t4g.large**
(17 Python containers ≈ 3.3 GB resident — measured 2026-08-03 as 124 MB
shared + 198 MB private per leg; see `deploy/ORACLE.md` §2) — as
`ec2-user`/`ubuntu` (not root):

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

**Main account (triple book, $900.00):**
1. Unified Trading Account, USDT in UTA (not Funding), **Cross margin**,
   **One-Way** position mode (never Hedge).
2. API key: permissions *Contract → Orders + Positions* ONLY — **no
   withdrawal, no transfer**. **IP-whitelist the live box's static IP**
   (the single most important security control).

**Sub-account (pullback book, $900.00):**
1. Bybit → Account → Subaccount Management → create a **Standard**
   sub-account with UTA.
2. Transfer **$900.00 USDT** main → sub (8 `-p` legs × $112.50).
3. Same margin/position-mode settings as main (Cross, One-Way).
4. Cut the sub-account its **own API key**: same trade-only permissions,
   same IP whitelist.

Leave **$900.00** on the main account for the triple book. After the
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

# --- sizing: REQUIRED. real per-account balance / 8. --------------------
# PER LEG = per-account balance / 8, and this ONE value feeds all 16 legs.
# Entering the per-ACCOUNT balance (~898) sizes the book 8x too large — that
# happened on 2026-08-06. As of that date: main 898.89, sub 897.60, so use the
# lower / 8 = 112.20. init-trading.sh rejects >=300 and <10.
LEG4H_LIVE_EQUITY=112.20
EOF
chmod 600 .env
```

Compose reads `.env` automatically from the repo directory. The per-leg
split is a single number — a deposit change or an L3 dial-up is just this one
value. `deploy/init-trading.sh` writes this same template if `.env` is absent
and refuses to call the box ready until the four keys AND the equity are set.

**Do not add `API_KEY2` / `API_SECRET2`.** tg-control reads the sub-account
through the compose mapping `API_KEY2: ${PULL_API_KEY}` — setting them in
`.env` does nothing.

## 4. Preflight (do not skip)

```bash
# Keys talk to Bybit and see the right balances (~900.00 each):
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

Expected: MAIN ≈ 898.89, SUB ≈ 897.60 (verified 2026-08-06). `AuthenticationError` = wrong
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
**Stats**: 16 legs, booked equity ≈ 1,800.00, real equity per account shown.

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
- [ ] `min-notional`/`skipping order` lines rare on the 7 non-BTC symbols.
      **BTC is the known exception at $1,800**: its 0.001 lot (~$64) sits
      near the risk-scaled size, so BTC trades slightly under-sized in
      normal vol and skips in high vol. **Log its skip rate** — if BTC
      misses most entries during L1, that is the signal to either fund the
      BTC legs specifically or run the book ex-BTC (a sizing decision, not
      a strategy change).

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
| Book equity −8% from deploy (≈ −$144 on $1,800) at unit weights | halt new entries (`down`), full review before restart |
| A bot logs `HALTED` (side conflict / extra size / untracked position) | it already stopped touching the exchange; reconcile that symbol on Bybit manually, then restart the service |
| `pending entry status unknown >2 bars` Telegram alert | check the order on Bybit; entries on that leg are blocked until it resolves |
| PULL legs' trailing-3-month Sharpe < 0 | demote to BLEND75 or triple-only (pre-registered fallback; ask for the reweighted compose) |
| Any UNEXPECTED exec divergence vs the engine | halt, diagnose, re-run `test_exec_parity.py` before resuming |
| Emergency — want everything flat NOW | Bybit UI → Close All Positions on BOTH accounts, then `docker compose -f docker-compose.bidir4h-live.yml down` |

Never touch the accounts manually while the stack runs (the bots own them);
never run a second live portfolio next to this one.

## 8. L2 → L3: the dial

After **L1 (≥2 weeks) + L2 (2–4 weeks)** are green and measured costs match
the model: the first dial is **15% vol** — multiply the single equity line in
`.env` by ~2.1 (`LEG4H_LIVE_EQUITY=236.25`) **only if the wallet actually
holds that much**, or keep the deposit and accept that the dial is really a
leverage decision (the bot sizes off STARTING_EQUITY).
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

---

## Changing the blend weights (BLEND50 → BLEND75)

Adopted 2026-08-13 (research/FINDINGS.md). The `-t` legs move to
`TRIPLE_LEG_EQUITY`, the `-p` legs to `PULL_LEG_EQUITY`; the old single
per-leg var is gone. **This is not a config-only change** — the two books live
in two different Bybit accounts, so the cash has to move too.

**Prerequisite: the book must be FLAT.** Changing sizing requires clearing the
state files (see the restart trap below), and clearing state while a position
is open leaves the bot untracking real exposure — it restarts flat, finds a
position it has no record of, and halts (best case). Wait for the open legs to
close on their own, or close them by hand, and verify zero positions on BOTH
accounts before starting.

```bash
# 1) confirm flat — must print 0 positions on each account
make health | sed -n '/LIVE LEGS/,/HALT LINE/p'     # 'pos' column all '-'
```

**2) Measure the real capital, then split it 75/25.** Do NOT reuse the numbers
in the compose defaults — they describe the nominal 1,795.20 book and the
account has since drifted. Read both accounts' UNIFIED equity (main = `-t`,
sub `roflbot_pullback` = `-p`), add them, and split:

```
  total = main_equity + sub_equity
  main (triple) = total * 0.75      ->  TRIPLE_LEG_EQUITY = main / 8
  sub  (pull)   = total * 0.25      ->  PULL_LEG_EQUITY   = sub  / 8
```

**3) Transfer the difference in the Bybit UI — by hand.** The trading keys are
trade-only with NO transfer permission (by design, see CLAUDE.md), so neither
the bot nor any tooling here can move it. Going from 50/50 to 75/25 means
moving ~25% of the book from the SUB to MAIN. Verify both balances after.

**4) Set the two vars** in `.env` on the box (never in the compose file):

```bash
TRIPLE_LEG_EQUITY=<main / 8>
PULL_LEG_EQUITY=<sub / 8>
```

`init-trading.sh` refuses to call the box ready unless both are set.

**5) The −8% halt line: re-base ONLY for capital moves, never for losses.**
The distinction is the whole point:
* **A withdrawal/deposit MUST adjust the baseline** — otherwise the halt line
  reads your own transfer as a drawdown. Not doing this is an accounting bug.
* **A LOSS must NOT adjust it.** Re-basing after a bad month quietly forgives
  the drawdown already taken, which is moving the goalposts.

Anchored 2026-08-17 at the **1,685.99** reset baseline (**halt at 1,551.11**),
which combines a −100.00 withdrawal (legitimate re-base) with a deliberate,
operator-approved reset of the −10.12 realised (a goalpost move, taken
knowingly because both the weights and the capital changed at the same time).
The trade record survives in FINDINGS and the `events-*.jsonl` audit trail —
only the halt-line reference moved.

**6) Clear state and restart** — the procedure immediately below. Then verify
every leg came up on the intended number:

```bash
make health | sed -n '/LIVE LEGS/,/HALT LINE/p'   # -t legs at the new equity,
                                                  # -p legs at a THIRD of them
```

If any leg reports ~100.00 the anchor merge silently failed and it fell back to
bot.py's default — stop and fix before it sizes an order.
(`test_compose.py::test_blend_weights_resolve` guards this locally.)

## ⚠ Restart trap: the state file overrides `STARTING_EQUITY`

`State.load()` reads `equity=d["equity"]` from `bot_state.json` when the file
exists — **the env var is only used when there is no state file.** So changing
`LEG4H_LIVE_EQUITY` in `.env` has NO effect on a leg that already has state.

Hit for real on 2026-08-06: the stack had been started with
`LEG4H_LIVE_EQUITY=898` (the per-ACCOUNT balance instead of per-leg), writing
16 state files at `equity: 898.0`. Correcting `.env` to 112.20 alone would
have left every leg resuming 8x oversized. Zero trades had been taken, so
nothing was lost — but only by luck.

**Whenever you change the sizing, clear the state before restarting:**

```bash
cd ~/rofl
tar czf ~/state-backup-$(date -u +%Y%m%d-%H%M%S).tgz data/live   # cheap insurance
sudo find data/live -name bot_state.json -delete
sudo find data/live -name heartbeat -delete
# events-*.jsonl are the audit trail — KEEP them
```

`sudo` is required: the containers run as root, so bind-mounted files are
root-owned even though `ubuntu` can read them (which is why `pull-data.sh`
still works without it).

Only safe when the legs are **flat**. If any position is open, closing it on
the exchange first is mandatory — otherwise resume sees an unknown position
and HALTs that leg (by design).
