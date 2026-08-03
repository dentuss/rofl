#!/usr/bin/env bash
#
# Initialise the TRADING box (Oracle A1, 1 OCPU / 10 GB slice).
#
# *** THIS SCRIPT DELIBERATELY STARTS NOTHING THAT TRADES. ***
# It prepares the machine and runs a read-only preflight, then stops and
# prints what to do next. Bringing up real money is a manual, gated decision
# (research/ROADMAP.md Phase 6) — no script gets to skip a stage.
#
# Usage (fresh Ubuntu 24.04 A1 instance, as `ubuntu`):
#     curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/init-trading.sh | bash
#
# Idempotent — safe to re-run.
#
# Env overrides:
#     SWAP_GB       swap file size in GB (default 4)
#     REPO_DIR      checkout location (default ~/rofl)

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/rofl}"
SWAP_GB="${SWAP_GB:-4}"

log()  { echo -e "\033[1;34m==>\033[0m $*"; }
warn() { echo -e "\033[1;33mWARN:\033[0m $*"; }
err()  { echo -e "\033[1;31mERROR:\033[0m $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && err "Do not run as root — run as ubuntu; the script sudos where needed."

# ----- 1. Clock -------------------------------------------------------------
# Bybit rejects authenticated requests on clock skew. This is not optional.
log "Setting clock to UTC + NTP"
sudo timedatectl set-timezone UTC
sudo timedatectl set-ntp true || true
if ! timedatectl show -p NTPSynchronized --value | grep -q yes; then
    warn "clock not yet NTP-synchronised — re-check before going live (Bybit auth breaks on skew)"
fi
timedatectl | sed 's/^/    /'

# ----- 2. Swap --------------------------------------------------------------
# Measured footprint: 124 MB shared + 198 MB private per leg => 16 legs
# ~3.3 GB, +tg ~0.2, +Docker/OS ~0.65 = ~4.2 GB of 10. Swap is insurance
# against GMM-fit spikes at the 4h boundary, not routine capacity.
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

# ----- 3. Docker + repo -----------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker via deploy/setup.sh"
    bash <(curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/setup.sh)
else
    log "Docker present — pulling latest repo"
    [[ -d "$REPO_DIR" ]] && git -C "$REPO_DIR" pull --ff-only || true
fi
[[ -d "$REPO_DIR" ]] || err "repo not found at $REPO_DIR"
cd "$REPO_DIR"
sudo systemctl enable docker >/dev/null 2>&1 || true

# ----- 4. .env --------------------------------------------------------------
# Never generated here: keys are pasted by a human, on the box, once.
if [[ ! -f .env ]]; then
    warn "no .env — creating a TEMPLATE with empty values (fill it in by hand)"
    cat > .env <<'TEMPLATE'
# Trade-only, IP-whitelisted keys. NO withdrawal permission. chmod 600.
# main account = the 8 triple (-t) legs
API_KEY=
API_SECRET=
# sub-account roflbot_pullback = the 8 pullback (-p) legs
PULL_API_KEY=
PULL_API_SECRET=
# tg-control reads BOTH accounts (API_KEY2 = the sub key)
API_KEY2=
API_SECRET2=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
# Per-leg equity. RE-DERIVE FROM REAL BALANCES — see ROADMAP L0.5.
LEG4H_LIVE_EQUITY=112.50
TEMPLATE
    chmod 600 .env
else
    log ".env present"
    chmod 600 .env
fi
[[ "$(stat -c %a .env)" == "600" ]] || err ".env must be chmod 600"

# ----- 5. Preflight (READ-ONLY) --------------------------------------------
log "Preflight: verifying the image builds (starts nothing)"
sg docker -c "docker compose -f docker-compose.bidir4h-live.yml build" >/dev/null
log "Image rofl-bot:4h-live built OK"

MISSING=0
for V in API_KEY API_SECRET PULL_API_KEY PULL_API_SECRET; do
    grep -qE "^${V}=.+" .env || { warn "$V is empty in .env"; MISSING=1; }
done

cat <<EOF

--------------------------------------------------------------------
TRADING BOX PREPARED — AND NOTHING IS RUNNING. That is intentional.

Resource budget for this box (measured, not estimated):
  16 legs   124 MB shared + 16 x 198 MB private   = 3.29 GB
  tg-control                                       ~0.20 GB
  Docker + Ubuntu                                  ~0.65 GB
  ----------------------------------------------------------
  total                                            ~4.14 GB of 10

BEFORE YOU START ANYTHING, in order (research/ROADMAP.md Phase 6):

 1. $( [[ $MISSING -eq 1 ]] && echo "FILL IN .env — keys are still empty." || echo "Keys present in .env." )
 2. Re-whitelist BOTH Bybit keys to THIS box's reserved public IP:
      curl -s https://api.ipify.org; echo
 3. L0.5 — FIX THE CAPITAL SPLIT. As of 2026-08-03 the money is in the
    wrong wallets: main has \$797.65 in FUND and \$0.00003 in UNIFIED
    (the -t legs would start with zero margin); the sub has \$999.49.
    Move main FUND -> UNIFIED, rebalance to ~\$898.57 per account, then
    set LEG4H_LIVE_EQUITY from the REAL per-account balance / 8.
 4. Confirm both accounts are FLAT before the first start.
 5. Only then, and only if ROADMAP L1 is the active stage:
      docker compose -f docker-compose.bidir4h-live.yml up -d --build

The paper twin is NOT recommended on this box — the program is
live-first, paper cannot produce real fills, and its legs collide with
live's stagger offsets (same crc32(symbol) => same wake second). If you
run it anyway, set FETCH_STAGGER_SECS=90 on the paper stack.
--------------------------------------------------------------------
EOF
