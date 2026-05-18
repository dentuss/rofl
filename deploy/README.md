# EC2 deployment — quick reference

## Instance choice

| What | Pick | Why |
|---|---|---|
| **AMI** | Amazon Linux 2023 (x86_64 or arm64) | Tiny, fast boot, Docker via dnf |
| **Instance** | `t4g.small` (ARM, ~$12/mo) | Bot is mostly idle. 2 vCPU + 2GB is plenty. |
| **Region** | `ap-southeast-1` (Singapore) for KuCoin | Closest to exchange = lower API latency |
| **Storage** | 16 GiB gp3 | Code, logs, cache — small footprint |
| **Security group** | Inbound: SSH (22) from YOUR IP only. Outbound: all | Bot only initiates outbound HTTPS |

## One-shot user-data script

When launching the instance, paste this into "Advanced details → User data".
It installs Docker, clones the repo, and starts the bot in paper mode.

```bash
#!/bin/bash
set -e
dnf update -y
dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user

# Clone repo as ec2-user
sudo -u ec2-user bash <<'EOSU'
cd /home/ec2-user
git clone https://github.com/dentuss/rofl.git
cd rofl
# Build image + start bot. Paper mode by default.
docker compose up -d --build
EOSU
```

When the instance boots, SSH in and check:
```bash
ssh ec2-user@<public-ip>
cd rofl
docker compose ps
docker compose logs -f
```

## Exchange — Bybit by default (paper mode)

Paper mode uses Bybit's public OHLCV API for **real market data** (no
auth needed). Simulated orders, no funds at risk. The bot defaults to
**Bybit USDT perpetuals** (linear): 0.055% taker / 0.02% maker fees,
deep liquidity on INJ/SOL/ETH/BTC, best ccxt support.

If Bybit is geo-blocked from your VPC, the bot transparently falls back
to KuCoin REST for OHLCV. To force this fallback, set
`EXCHANGE=kucoin_offline`. (Singapore = `ap-southeast-1` reaches Bybit
fine.)

## Going live on Bybit

After 2 weeks of clean paper, create API keys at https://www.bybit.com/app/user/api-management
(permissions: **Contract Trade — Orders & Positions** only; **no withdrawal**).

```bash
ssh rofl
cd rofl
cat > .env <<EOF
MODE=live
EXCHANGE=bybit
STRATEGY_PRESET=safer_inj_high_return
STARTING_EQUITY=100
API_KEY=<your_bybit_key>
API_SECRET=<your_bybit_secret>
# API_PASSPHRASE is NOT needed for Bybit (leave empty)
EOF
chmod 600 .env
docker compose down
docker compose --env-file .env up -d
```

**Bybit account setup checklist** (one-time):
1. Sign up at bybit.com, complete KYC level 1 (passport scan)
2. Deposit USDT to your **Unified Trading Account** (UMA) — not Funding
3. Enable USDT perpetual trading (it's the default)
4. Set **Cross margin** mode on UMA (gives the bot full equity to size against)
5. Generate API keys with `Contract → Trade` permission, IP-whitelist
   your EC2 public IP

### Going live on KuCoin instead
```bash
EXCHANGE=kucoin
API_KEY=...
API_SECRET=...
API_PASSPHRASE=<required>     # KuCoin requires this; Bybit doesn't
```

## Switching to the 3-bot portfolio

```bash
docker compose down
docker compose -f docker-compose.portfolio.yml up -d --build
```

## Viewing live logs from your PC

### Option 1 — simple SSH tail (works anywhere)
```bash
ssh ec2-user@<public-ip> 'cd rofl && docker compose logs -f'
```
Add to your `~/.ssh/config` to make it one keystroke:
```
Host rofl
  HostName <public-ip>
  User ec2-user
  IdentityFile ~/.ssh/your-key.pem
```
Then: `ssh rofl 'cd rofl && docker compose logs -f'`

### Option 2 — `docker context` (run docker commands locally against remote)
Once-only setup on your PC:
```bash
docker context create rofl --docker "host=ssh://ec2-user@<public-ip>"
docker context use rofl
```
After that, every docker command on your PC hits the EC2 instance:
```bash
docker compose -f docker-compose.yml logs -f
docker compose -f docker-compose.yml ps
```
Switch back to local: `docker context use default`

### Option 3 — persistent log file via SSH
```bash
# Stream the on-disk log:
ssh rofl 'docker exec rofl-bot tail -f /app/logs/bot.log'
```

### Option 4 — Telegram alerts (push to phone)
*(Section moved above — please scroll up to the Telegram setup block.)*

## Updating to a new bot version

```bash
ssh rofl
cd rofl
git pull
docker compose up -d --build       # picks up new code, restarts bot
```
State (open positions, equity, trade history) persists in the
named Docker volume — no data loss on rebuild.

## Backing up state

```bash
ssh rofl 'docker run --rm -v rofl_bot_state:/from -v $PWD:/to alpine \
    sh -c "cd /from && tar -cf /to/bot_state.tar ."'
scp rofl:bot_state.tar ./
```

## Cost expectations

| Component | Monthly |
|---|---|
| t4g.small (730 hrs) | ~$12 |
| 16 GB EBS gp3 | ~$1.30 |
| Outbound data (KuCoin API) | <$0.10 |
| **Total** | **~$14 / month** |

If you want it even cheaper, t4g.nano ($3.40/mo) also works but
2GB RAM gives you headroom for sklearn + pandas.

## Health monitoring

The Docker `restart: unless-stopped` policy means the bot auto-restarts
if it crashes. For extra peace of mind, add a CloudWatch alarm:

```bash
# Check the bot is alive every 5 min (run this on the instance)
*/5 * * * * docker compose -f /home/ec2-user/rofl/docker-compose.yml ps \
  | grep -q "Up" || echo "BOT DOWN" | mail -s "rofl-bot down" you@example.com
```

Or simpler: check `docker compose ps` once a day yourself.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `docker compose` not found | Wait 30s — user-data is still running. Or `sudo dnf install -y docker-compose-plugin` |
| `permission denied` on docker | Log out and back in (group change) |
| KuCoin 400/401 error in live mode | Check API_PASSPHRASE — required for KuCoin |
| Bot not opening positions | Normal! Most bars don't trigger. Check `bot_status.py` |
| `ModuleNotFoundError` | Old image cached — `docker compose build --no-cache` |
