# EC2 deployment guide

## TL;DR — one-liner that just works

After launching an EC2 instance with the spec below, SSH in and run:

```bash
curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/claude/trading-bot-strategy-Uf9FR/deploy/setup.sh | bash
```

That single command:
1. Installs Docker Engine for your OS (handles Amazon Linux **and** Ubuntu)
2. Installs the Compose v2 plugin (AL2023 doesn't bundle it by default — common gotcha)
3. Adds your user to the `docker` group
4. Clones the repo
5. Builds the image and starts the bot in **paper mode**
6. Verifies the container is running

If you'd rather do it manually, see the [Manual setup](#manual-setup) section below.

---

## EC2 instance specs

| Setting | Pick | Why |
|---|---|---|
| **AMI** | **Ubuntu 22.04 LTS** (preferred) **or** Amazon Linux 2023 | Ubuntu has cleaner Docker packaging via Docker's official repo. AL2023 works but needs manual compose-plugin install (the script handles it). |
| **Instance type** | **`t4g.small`** (ARM, ~$12/mo) | 2 vCPU + 2 GB RAM. Bot is mostly idle. Pick `arm64` AMI variant to match. |
| **Storage** | 16 GiB gp3 | Code, cache, logs |
| **Region** | **`ap-southeast-1`** (Singapore) | Closest to Bybit's API servers |
| **Security group** | **Inbound**: SSH (22) from your IP only. **Outbound**: all | Bot only initiates outbound HTTPS |
| **Key pair** | Create one in the same region | Needed for SSH |

When you click **Launch**, you can paste this into **Advanced details → User data** (bottom of the form) for hands-off setup, OR skip it and run the curl one-liner above after first SSH:

```bash
#!/bin/bash
# Drop this into "User data". It will install Docker + start the bot.
# (Ubuntu version — for AL2023 the setup.sh script auto-detects.)
exec > /var/log/user-data.log 2>&1
set -xe
sleep 10   # let cloud-init finish initial network setup
sudo -u ubuntu bash -c 'curl -fsSL \
    https://raw.githubusercontent.com/dentuss/rofl/claude/trading-bot-strategy-Uf9FR/deploy/setup.sh \
    | bash'
```

Note: if you use the user-data path, check `/var/log/user-data.log` on the
instance after boot to see what happened. User-data runs as **root**, which
is why we explicitly `sudo -u ubuntu` (or `ec2-user` for AL2023). The setup
script refuses to run as root.

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
# 1. Install Docker (compose plugin NOT bundled — see step 2!)
sudo dnf -y update
sudo dnf -y install docker git
sudo systemctl enable --now docker

# 2. Install compose plugin manually (this is the AL2023 gotcha)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -fsSL \
    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# 3. Allow your user to run docker
sudo usermod -aG docker $USER

# 4. (CRITICAL) Log out and back in, OR run `exec sg docker` to pick up
#    the new group membership in your current shell. Without this, you'll
#    get "permission denied while trying to connect to the Docker daemon".
exec sg docker

# 5. Pull the repo and start the bot
git clone -b claude/trading-bot-strategy-Uf9FR https://github.com/dentuss/rofl.git
cd rofl
docker compose up -d --build
docker compose ps
```

---

## Verify it's working

After setup, you should see something like:
```bash
$ docker compose ps
NAME       IMAGE             STATUS         PORTS
rofl-bot   rofl-bot:latest   Up 12 seconds

$ docker compose logs --tail 20
2026-05-19 02:14:13 INFO | telegram notifier disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
2026-05-19 02:14:14 INFO | starting bot mode=paper symbol=INJ/USDT tf=1h preset=safer_inj_high_return ...
```

If `docker compose ps` shows the container running and `logs` shows a "starting bot" line, you're good.

---

## Going live on Bybit (after 2 weeks of paper)

1. Create API keys at https://www.bybit.com/app/user/api-management with:
   - Permission: **Contract → Trade (Orders + Positions)** only
   - **NO** withdrawal permission
   - **IP-whitelist** your EC2 public IP
2. SSH to the instance:
   ```bash
   cd ~/rofl
   cat > .env <<'EOF'
   MODE=live
   EXCHANGE=bybit
   STRATEGY_PRESET=safer_inj_high_return
   STARTING_EQUITY=100
   API_KEY=<your_bybit_key>
   API_SECRET=<your_bybit_secret>
   # No API_PASSPHRASE needed for Bybit
   EOF
   chmod 600 .env
   docker compose --env-file .env up -d --force-recreate
   ```

Bybit account checklist (one-time):
1. Sign up, complete KYC level 1
2. Deposit USDT to your **Unified Trading Account** (UMA — not Funding)
3. USDT perpetual trading is enabled by default
4. Set **Cross margin** mode on UMA (gives the bot full equity to size against)

### KuCoin instead of Bybit
```bash
EXCHANGE=kucoin
API_KEY=...
API_SECRET=...
API_PASSPHRASE=<required>     # KuCoin requires this; Bybit doesn't
```

---

## Telegram alerts (3-minute setup)

The bot has a built-in Telegram notifier — events go to your phone from **both paper and live mode**, tagged `📝 PAPER` or `🟢 LIVE`.

1. On Telegram, message **@BotFather** → `/newbot` → pick a name and a username ending in `_bot`. Save the token.
2. Search for your new bot, send `/start`.
3. Message **@userinfobot** → save the numeric chat ID.
4. Append to your `.env`:
   ```
   TELEGRAM_BOT_TOKEN=7891234567:AAHxxxxxxxxx
   TELEGRAM_CHAT_ID=123456789
   ```
5. `docker compose --env-file .env up -d --force-recreate`

You'll get an instant "🚀 Bot started" message. Trades, daily summaries, regime changes, and errors arrive automatically. Missing token? Notifier silently disables itself; bot keeps running.

---

## Viewing live logs from your PC

### Easiest — SSH tail
Add to your local `~/.ssh/config`:
```
Host rofl
  HostName <ec2-public-ip>
  User ubuntu      # or ec2-user for Amazon Linux
  IdentityFile ~/.ssh/your-key.pem
```
Then anywhere:
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
Switch back to local: `docker context use default`.

### Structured event log — after 2-3 weeks of paper
Every event is written to `events-YYYY-MM-DD.jsonl` inside the bot_logs volume. To analyse locally:
```bash
# From your PC — copy event files off the EC2 box
mkdir -p ./bot_events
ssh rofl 'docker run --rm -v rofl_bot_logs:/from alpine \
    sh -c "tar -cf - -C /from events-*.jsonl"' \
  | tar -xf - -C ./bot_events

# Run the analyser locally
python3 research/analyze_logs.py ./bot_events
```
Paste the output back to me + the JSONL files and we'll decide whether to tune parameters.

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
| `docker: command not found` | Setup script didn't run / user-data failed. Re-run `bash deploy/setup.sh`. |
| `docker compose: 'compose' is not a docker command` | Compose plugin missing. On AL2023 install manually (see Manual setup step 2). |
| `permission denied while trying to connect to the Docker daemon socket` | Your shell hasn't picked up the `docker` group yet. **Log out and back in**, or run `exec sg docker`. |
| `docker compose up` hangs at "exporting layers" | Building wheels for pandas/numpy can take ~3 minutes on t4g.small. Be patient. |
| `git clone` fails with `Repository not found` | Branch name in the URL must match — use `claude/trading-bot-strategy-Uf9FR` (URL-encoded slash is fine). For a private repo, set up SSH keys or use a PAT. |
| `docker compose ps` shows status `Restarting` | Check `docker compose logs` — usually a missing env var (API_KEY in live mode) or unreachable exchange. Bot will fall back to KuCoin REST if Bybit is geo-blocked. |
| User-data ran but nothing started | User-data runs as **root**. Check `/var/log/user-data.log` and `/var/log/cloud-init-output.log`. The setup script refuses to run as root, so user-data must `sudo -u ubuntu` (or ec2-user). |
| `ccxt` fetch fails — country block | Make sure you launched in `ap-southeast-1` (Singapore). Bybit blocks several US/EU regions. The bot falls back to KuCoin REST automatically. |
| Bot opens trades on paper but no notifications | Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` and `docker compose logs` for `telegram notifier enabled`. |

### Quick health checks
```bash
docker compose ps                # status
docker compose logs --tail 50    # recent log
docker compose exec bot python3 bot_status.py    # equity, position
docker compose exec bot ls /app/logs              # event log files
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
