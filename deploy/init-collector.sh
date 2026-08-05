#!/usr/bin/env bash
#
# Initialise the COLLECTOR box (Oracle A1, 1 OCPU / 2 GB slice).
#
# This box holds NO KEYS and touches NO ACCOUNT. It records public Bybit
# websocket streams to disk, forever. It is deliberately the machine you set
# up once and never touch again: tick history CANNOT be backfilled, so every
# restart, rebuild or OOM here is data you can never recover. That is the whole
# reason it lives apart from the trading box.
#
# Usage (fresh Ubuntu 24.04 A1 instance, as `ubuntu`):
#     curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/init-collector.sh | bash
#
# Idempotent — safe to re-run.
#
# Env overrides:
#     SYMBOLS       collector universe (default: QUAL23, the sleeve universe)
#     SWAP_GB       swap file size in GB (default 2)
#     REPO_DIR      checkout location (default ~/rofl)

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/rofl}"
SWAP_GB="${SWAP_GB:-2}"

# QUAL23 — the cross-sectional sleeve universe (research/tsmom_sleeve.py), NOT
# the narrower MAJORS8 trading book. Collecting wider costs ~7.7 GB/yr instead
# of 2.7 and carries no selection-bias risk (this is data, not a strategy
# choice) — but MAJORS8-only ticks would leave XSMOM/XSBAB microstructure
# permanently unstudiable, and that gap cannot be filled in later.
SYMBOLS="${SYMBOLS:-BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,ADA/USDT:USDT,LINK/USDT:USDT,AVAX/USDT:USDT,NEAR/USDT:USDT,AAVE/USDT:USDT,GRT/USDT:USDT,RUNE/USDT:USDT,DOGE/USDT:USDT,DOT/USDT:USDT,ATOM/USDT:USDT,LTC/USDT:USDT,XRP/USDT:USDT,BNB/USDT:USDT,FIL/USDT:USDT,OP/USDT:USDT,UNI/USDT:USDT,ETC/USDT:USDT,BCH/USDT:USDT,TRX/USDT:USDT,SAND/USDT:USDT}"

log() { echo -e "\033[1;34m==>\033[0m $*"; }
err() { echo -e "\033[1;31mERROR:\033[0m $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && err "Do not run as root — run as ubuntu; the script sudos where needed."

# ----- 1. Clock (UTC + NTP) -------------------------------------------------
# The collector stamps every row in UTC and rotates files at UTC midnight.
log "Setting clock to UTC + NTP"
sudo timedatectl set-timezone UTC
sudo timedatectl set-ntp true || true
timedatectl | sed 's/^/    /'

# ----- 2. Swap --------------------------------------------------------------
# 2 GB box: the collector needs ~0.3 GB, but swap keeps a transient spike from
# OOM-killing the one process whose data cannot be recreated.
if ! sudo swapon --show | grep -q '/swapfile'; then
    log "Creating ${SWAP_GB}G swap"
    sudo fallocate -l "${SWAP_GB}G" /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || \
        echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
    log "Swap already present — skipping"
fi

# ----- 3. Docker + repo (delegates to the shared installer) -----------------
if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker via deploy/setup.sh"
    bash <(curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/setup.sh)
else
    log "Docker present — pulling latest repo"
    [[ -d "$REPO_DIR" ]] && git -C "$REPO_DIR" pull --ff-only || true
fi
[[ -d "$REPO_DIR" ]] || err "repo not found at $REPO_DIR"

# ----- 4. Pin the collector universe ----------------------------------------
# Written to a .env the compose file picks up, so the symbol list survives
# `git pull` without a merge conflict in the tracked YAML.
log "Pinning SYMBOLS (${SYMBOLS//,/ } )" >/dev/null
printf 'SYMBOLS=%s\n' "$SYMBOLS" > "$REPO_DIR/.env.collector"
chmod 600 "$REPO_DIR/.env.collector"
log "Wrote $REPO_DIR/.env.collector ($(tr ',' '\n' <<<"$SYMBOLS" | wc -l) symbols)"

# ----- 5. Start ------------------------------------------------------------
cd "$REPO_DIR"
log "Building + starting the collector"
sg docker -c "docker compose --env-file .env.collector -f docker-compose.collector.yml up -d --build"
sudo systemctl enable docker >/dev/null 2>&1 || true

# ----- 6. Verify ------------------------------------------------------------
log "Waiting 60s for the first files to land..."
sleep 60
TODAY="$(date -u +%F)"
if sg docker -c "docker exec rofl-collector ls /app/data/$TODAY" 2>/dev/null; then
    log "OK — collector is writing to /app/data/$TODAY"
else
    err "No data directory yet. Check: docker compose -f docker-compose.collector.yml logs --tail 50"
fi

cat <<EOF

--------------------------------------------------------------------
COLLECTOR BOX READY.  Started $(date -u +'%Y-%m-%d %H:%M UTC')

  logs     docker compose -f docker-compose.collector.yml logs -f
  files    docker exec rofl-collector ls -la /app/data/\$(date -u +%F)
  size     docker exec rofl-collector du -sh /app/data

Disk: ~7.7 GB/yr at this symbol count. Occasional "retrying in 5s" in
the logs is normal websocket reconnect noise, not a fault.

DO TWO MORE THINGS:
  1. Upgrade the tenancy to Pay-As-You-Go. This box is CPU-idle and
     low-bandwidth = exactly Oracle's idle-reclamation profile. No money
     is at risk here, but the tick record is, and it cannot be rebuilt.
  2. Schedule the volume backup (the data lives in a Docker volume; lose
     the instance, lose the history):
       tar czf ticks-\$(date -u +%F).tgz -C ~/rofl/data ticks
--------------------------------------------------------------------
EOF
