# EC2 deployment guide

## TL;DR — one-liner that just works

After launching an EC2 instance with the spec below, SSH in and run:

```bash
curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/claude/trading-bot-strategy-Uf9FR/deploy/setup.sh | bash
```

> **Private repo?** `raw.githubusercontent.com` returns 404 for anonymous
> requests against private repos. Either make the repo public, or use the
> [manual clone](#manual-setup) flow with a GitHub PAT / SSH key.

That single command:
1. Installs Docker Engine for your OS (Amazon Linux, Ubuntu 22.04, or Ubuntu 24.04)
2. Installs the Compose v2 plugin (AL2023 doesn't bundle it — common gotcha)
3. Adds your user to the `docker` group
4. Clones the repo
5. Builds the image and starts the bot in **paper mode** with the default preset
6. Verifies the container is running

If you'd rather do it manually, see [Manual setup](#manual-setup).

---

## EC2 instance specs

| Setting | Pick | Why |
|---|---|---|
| **AMI** | **Ubuntu 24.04 LTS** (confirmed working) or 22.04 or Amazon Linux 2023 | Ubuntu has cleaner Docker packaging via Docker's official repo. AL2023 works but needs manual compose-plugin install (the script handles it). |
| **Instance type** | **`t4g.small`** (ARM, ~$12/mo) | 2 vCPU + 2 GB RAM. Bot is mostly idle. Pick the `arm64` AMI variant to match. |
| **Storage** | 16 GiB gp3 | Code, cache, logs, sklearn wheels |
| **Region** | **`ap-southeast-1`** (Singapore) | Closest to Bybit's API servers |
| **Security group** | **Inbound**: SSH (22) from your IP only. **Outbound**: all | Bot only initiates outbound HTTPS |
| **Key pair** | Create one in the same region | Needed for SSH |

When you click **Launch**, paste this into **Advanced details → User data** for hands-off setup, or skip it and run the curl one-liner after first SSH:

```bash
#!/bin/bash
# Drop into "User data". Installs Docker + starts the bot.
# (Ubuntu version — for AL2023 the setup.sh script auto-detects.)
exec > /var/log/user-data.log 2>&1
set -xe
sleep 10   # let cloud-init finish initial network setup
sudo -u ubuntu bash -c 'curl -fsSL \
    https://raw.githubusercontent.com/dentuss/rofl/claude/trading-bot-strategy-Uf9FR/deploy/setup.sh \
    | bash'
```

Note: user-data runs as **root**, which is why we explicitly `sudo -u ubuntu`
(or `ec2-user` on AL2023). The setup script refuses to run as root. Check
`/var/log/user-data.log` on the instance after boot to see what happened.

---

## Picking a preset

The bot ships with ~15 presets (see `bot.py` → `PRESETS`). For a **2-3 week paper-trading competence test**, use:

```
STRATEGY_PRESET=adaptive_inj_bidir
```

**Why this one:**
- **INJ/USDT 1h** is the discovered best pair/tf on 5y backtest data — profitable every year incl. 2022 bear, lower MDD than SOL, higher Sharpe.
- **Bidirectional** (`triple_bidir` strategy) takes long *and* short trades — mirror-image entry rules (EMA stack down, RSI < 45, ADX > 22, ATR-based symmetric stops).
- **Directional regime filter** — longs only when GMM regime is BULL/CHOP, shorts only in BEAR/CHOP. Avoids countertrend trades in the wrong regime.
- **F&G extreme-zone filter** — additionally blocks longs at Fear & Greed ≥ 80 (extreme greed → likely tops) and shorts at F&G ≤ 20 (extreme fear → likely bottoms). Walk-forward 5y test: same return, MDD improved by ~6pp.
- 5y walk-forward backtest (r=2%, decay=0.5, funding @ 1bp/8h modeled):

  | Preset | CAGR | MDD | Sharpe | Total return (5y) |
  |---|---|---|---|---|
  | `adaptive_inj_high_return` (long-only) | +73% | −30% | 1.51 | +1052% |
  | `adaptive_inj_bidir` (no F&G) | +146% | −33% | 1.74 | +3742% |
  | **`adaptive_inj_bidir` (current, F&G on)** | **+144%** | **−28%** | **1.75** | **+3682%** |

  Short trades alone in the bidir backtest: ~672 trades after F&G filter, ~43% win rate, +1526 USDT contribution. Worst year for bidir was 2021 startup window (−1%); every full year after was positive.

- 2% per trade gives enough trade frequency on 1h bars (~30-40/week combined) to evaluate in 2-3 weeks.
- Equity-curve risk decay auto-enabled (halves risk after −20% drawdown).
- Bybit funding cost (~1bp / 8h) modeled in the backtest. Bidir's long/short mix nets to roughly zero net funding over 5y.

### Caveats before going live

- Backtest is in-sample — these parameters were tuned on this same 5y data. Out-of-sample real performance will likely be lower.
- Bybit funding rate (~±1bp / 8h) is now modeled in the backtest. Net effect on bidir is ≈0 over 5y because long pays / short receives roughly cancel.
- The F&G filter requires a network fetch to alternative.me on each signal cycle (cached 6h). If the fetch fails the bot logs a warning and proceeds without the filter — won't break trading.
- ~670 short trades over 5y with the F&G filter = ~11/month — enough volume to validate in 2-3 weeks.
- Always paper-trade for 2-3 weeks before switching `MODE=live`.

### When to pick something else

| Preset | Use when |
|---|---|
| `adaptive_inj_bidir`       | **Default recommendation** — bidirectional, max return |
| `adaptive_inj_high_return` | Conservative — long-only with ML BEAR filter |
| `adaptive_inj_growth`      | Even lower variance (r=1.5%) |
| `safer_inj_high_return`    | No ML layer (deterministic, no sklearn) |
| Portfolio (`run_portfolio.sh`) | Diversified ETH+BTC+SOL instead of single-pair INJ |

`ALLOW_SHORT=1` is no longer needed as a manual override — the bidir preset enables it automatically. For long-only presets, setting `ALLOW_SHORT=1` is a no-op (the strategy never generates short signals).

---

## `.env` cheat sheet

Everything is configured via `~/rofl/.env`. Minimal recommended:

```
# Core
MODE=paper
STRATEGY_PRESET=adaptive_inj_bidir
STARTING_EQUITY=100
EXCHANGE=bybit

# Telegram (optional but recommended — see below)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Live-mode only — leave blank for paper
API_KEY=
API_SECRET=
API_PASSPHRASE=     # KuCoin/OKX need it; Bybit doesn't
```

After editing `.env`:
```bash
cd ~/rofl
docker compose --env-file .env up -d --force-recreate
```

---

## Telegram alerts (3-minute setup)

Events go to your phone from **both paper and live mode**, tagged `📝 PAPER` or `🟢 LIVE`.

1. Telegram → **@BotFather** → `/newbot` → save the token.
2. Find your new bot in Telegram, send `/start` to it. **Required** — Telegram bots can't message users who haven't initiated.
3. Telegram → **@userinfobot** (in a direct chat with userinfobot, not forwarded) → save **your** numeric chat ID. If you forward from your bot, you'll get the bot's ID and sends will fail with `403 Forbidden: the bot can't send messages to the bot`.
4. Append to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=7891234567:AAHxxxxxxxxx
   TELEGRAM_CHAT_ID=123456789
   ```
5. `docker compose --env-file .env up -d --force-recreate`
6. Verify: `docker compose logs bot | grep -i telegram` should show `telegram notifier enabled (chat=...)`.

You'll get an instant "🚀 Bot started" message. Trades, daily summaries, regime changes, and errors arrive automatically. Missing token? Notifier silently disables itself; bot keeps running.

---

## Going live on Bybit (after 2-3 weeks of paper)

1. Create API keys at https://www.bybit.com/app/user/api-management with:
   - Permission: **Contract → Trade (Orders + Positions)** only
   - **NO** withdrawal permission
   - **IP-whitelist** your EC2 public IP
2. Edit `~/rofl/.env`:
   ```
   MODE=live
   API_KEY=<your_bybit_key>
   API_SECRET=<your_bybit_secret>
   ```
3. `chmod 600 .env && docker compose --env-file .env up -d --force-recreate`

Bybit account checklist (one-time):
1. Sign up, complete KYC level 1
2. Deposit USDT to your **Unified Trading Account** (UMA — not Funding)
3. USDT perpetual trading is enabled by default
4. Set **Cross margin** mode on UMA (gives the bot full equity to size against)

### KuCoin instead of Bybit
```
EXCHANGE=kucoin
API_KEY=...
API_SECRET=...
API_PASSPHRASE=<required>
```

---

## Manual setup

If you'd rather see every command and skip the script:

### Ubuntu 22.04 / 24.04
```bash
# 1. Install Docker via Docker's official repo
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
    https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

# 2. Allow your user to run docker (LOG OUT and back in after this)
sudo usermod -aG docker $USER

# 3. Pull the repo and start the bot
git clone -b claude/trading-bot-strategy-Uf9FR https://github.com/dentuss/rofl.git
cd rofl
docker compose up -d --build
docker compose ps
```

### Amazon Linux 2023
```bash
# 1. Install Docker (compose plugin NOT bundled — see step 2)
sudo dnf -y update
sudo dnf -y install docker git
sudo systemctl enable --now docker

# 2. Install compose plugin manually (AL2023 gotcha)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -fsSL \
    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# 3. Allow your user to run docker
sudo usermod -aG docker $USER

# 4. Pick up the new group in the current shell
exec sg docker

# 5. Pull the repo and start the bot
git clone -b claude/trading-bot-strategy-Uf9FR https://github.com/dentuss/rofl.git
cd rofl
docker compose up -d --build
docker compose ps
```

---

## Verify it's working

```bash
$ docker compose ps
NAME       IMAGE             STATUS         PORTS
rofl-bot   rofl-bot:latest   Up 12 seconds

$ docker compose logs --tail 20
2026-05-24 02:14:13 INFO | telegram notifier enabled (chat=123456789)
2026-05-24 02:14:14 INFO | starting bot mode=paper symbol=INJ/USDT tf=1h preset=adaptive_inj_high_return ...
```

If `docker compose ps` shows the container running and `logs` shows a "starting bot" line, you're good. On adaptive presets you'll also see periodic `adaptive regime=BULL/CHOP/BEAR` lines once the bot has enough history.

---

## Viewing live logs from your PC

### Easiest — SSH tail
Add to `~/.ssh/config` on your PC:
```
Host rofl
  HostName <ec2-public-ip>
  User ubuntu      # or ec2-user for Amazon Linux
  IdentityFile ~/.ssh/your-key.pem
```
Then:
```bash
ssh rofl 'cd rofl && docker compose logs -f'
```

### Better — `docker context` (local commands hit remote daemon)
One-time on your PC:
```bash
docker context create rofl --docker "host=ssh://ubuntu@<public-ip>"
docker context use rofl
```
Now every docker command runs against EC2:
```bash
docker compose logs -f          # tails the EC2 bot
docker compose ps
docker compose restart bot
```
Switch back: `docker context use default`.

### Structured event log — after 2-3 weeks of paper
Every event is written to `events-YYYY-MM-DD.jsonl` inside the `bot_logs` volume. To analyse locally:
```bash
# From your PC — copy event files off the EC2 box
mkdir -p ./bot_events
ssh rofl 'docker run --rm -v rofl_bot_logs:/from alpine \
    sh -c "tar -cf - -C /from events-*.jsonl"' \
  | tar -xf - -C ./bot_events

# Run the analyser locally
python3 research/analyze_logs.py ./bot_events
```

---

## Updating the bot
```bash
ssh rofl
cd rofl
git pull
docker compose up -d --build       # rebuilds image, restarts
```
Bot state (open positions, equity, trade history) persists in the named Docker volume across rebuilds.

---

## Cost expectations
| Item | Monthly |
|---|---|
| t4g.small (730 hrs) | ~$12 |
| 16 GB EBS gp3 | ~$1.30 |
| Outbound data | < $0.10 |
| **Total** | **~$14 / month** |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `curl ... setup.sh` returns 404 | Repo is private. Make it public, use a PAT (`-H "Authorization: token $PAT"`), or clone manually with auth. |
| `docker: command not found` | Setup script didn't run / user-data failed. Re-run `bash deploy/setup.sh`. |
| `docker compose: 'compose' is not a docker command` | Compose plugin missing. On AL2023 install manually (see Manual setup step 2). |
| `permission denied while trying to connect to the Docker daemon socket` | Shell hasn't picked up the `docker` group. Log out and back in, or run `exec sg docker`. |
| `docker compose up` hangs at "exporting layers" | Building wheels for pandas/numpy/sklearn can take ~3-5 min on t4g.small. Be patient. |
| `git clone` fails with `Repository not found` | Branch name must match (`claude/trading-bot-strategy-Uf9FR`). Private repo? Use SSH keys or a PAT. |
| `docker compose ps` shows status `Restarting` | Check `docker compose logs` — usually a missing env var (API_KEY in live mode) or an unreachable exchange. Bot falls back to KuCoin REST if Bybit is geo-blocked. |
| User-data ran but nothing started | User-data runs as **root**. Check `/var/log/user-data.log` and `/var/log/cloud-init-output.log`. The setup script refuses to run as root, so user-data must `sudo -u ubuntu` (or ec2-user). |
| `ccxt` fetch fails — country block | Launch in `ap-southeast-1` (Singapore). Bybit blocks several US/EU regions. Bot falls back to KuCoin REST automatically. |
| Telegram: `403 Forbidden: the bot can't send messages to the bot` | The chat ID is your bot's, not yours. Message @userinfobot in a fresh direct chat to get **your** ID. Also send `/start` to your own bot at least once. |
| `regime detection failed, ignoring: scikit-learn not installed` | You're on an old image. `git pull && docker compose up -d --build` — sklearn is in `requirements.txt` now. |

### Quick health checks
```bash
docker compose ps                                       # status
docker compose logs --tail 50                           # recent log
docker compose exec bot python3 bot_status.py           # equity, position
docker compose exec bot ls /app/logs                    # event log files
docker compose exec bot cat /app/state/bot_state.json   # raw state
```

### Reset everything
```bash
docker compose down -v             # stop AND delete state/logs volumes
docker compose up -d --build       # fresh start
```

### Force-recreate without losing state
```bash
docker compose down                # stop only (state preserved)
docker compose up -d --build       # rebuild image, restart
```
