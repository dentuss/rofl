#!/usr/bin/env bash
#
# Pull collector + live-trading data from the Oracle boxes into ./data.
#
# READ-ONLY on the remote: rsync only reads, and nothing here restarts,
# rebuilds or writes to either box. Safe to run against a live trading box
# mid-position.
#
# Both boxes bind-mount their output to <repo>/data (see the composes'
# ${ROFL_DATA:-./data}), so the remote and local trees are identical and the
# sync is a plain incremental rsync — no docker cp, no staging copy, no
# doubling disk on a 50 GB box.
#
# Usage:
#     deploy/pull-data.sh                 # both boxes
#     deploy/pull-data.sh ticks           # collector only
#     deploy/pull-data.sh live            # trading box only
#
# Hosts come from ~/.ssh/config aliases (recommended) or env:
#     COLLECTOR_HOST=ubuntu@<collector-ip>
#     TRADING_HOST=ubuntu@<trading-ip>
#     REMOTE_DIR=~/rofl/data            (default)
#
# Suggested ~/.ssh/config:
#     Host rofl-collector
#         HostName <collector-ip>
#         User ubuntu
#         IdentityFile ~/.ssh/id_ed25519
#     Host rofl-trading
#         HostName <trading-ip>
#         User ubuntu
#         IdentityFile ~/.ssh/id_ed25519

set -euo pipefail

COLLECTOR_HOST="${COLLECTOR_HOST:-rofl-collector}"
TRADING_HOST="${TRADING_HOST:-rofl-trading}"
REMOTE_DIR="${REMOTE_DIR:-rofl/data}"
LOCAL_DIR="${ROFL_DATA:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data}"
WHAT="${1:-all}"

log()  { echo -e "\033[1;34m==>\033[0m $*"; }
warn() { echo -e "\033[1;33mWARN:\033[0m $*"; }

command -v rsync >/dev/null || { echo "rsync not installed: sudo apt install rsync"; exit 1; }
mkdir -p "$LOCAL_DIR"

# Files the collector is actively appending to change under us; --partial keeps
# what transferred and the next run completes it. Gzipped days never change
# again, so rsync skips them after the first pull.
RSYNC_OPTS=(-az --partial --info=stats1,progress2 --timeout=60)

pull() {
    local host="$1" subdir="$2"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" true 2>/dev/null; then
        warn "cannot reach $host over ssh — skipping $subdir"
        warn "  set COLLECTOR_HOST / TRADING_HOST, or add a ~/.ssh/config alias"
        return 1
    fi
    log "pulling $subdir from $host"
    mkdir -p "$LOCAL_DIR/$subdir"
    rsync "${RSYNC_OPTS[@]}" "$host:$REMOTE_DIR/$subdir/" "$LOCAL_DIR/$subdir/" || {
        warn "rsync reported an error for $subdir (partial data kept)"; return 1; }
}

RC=0
case "$WHAT" in
    ticks) pull "$COLLECTOR_HOST" ticks || RC=1 ;;
    live)  pull "$TRADING_HOST"  live  || RC=1 ;;
    all)   pull "$COLLECTOR_HOST" ticks || RC=1
           pull "$TRADING_HOST"  live  || RC=1 ;;
    *)     echo "usage: $0 [all|ticks|live]"; exit 2 ;;
esac

echo
log "local tree: $LOCAL_DIR"
du -sh "$LOCAL_DIR"/* 2>/dev/null | sed 's/^/    /' || true

if [[ -x "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/python" ]]; then
    echo
    "$(dirname "${BASH_SOURCE[0]}")/../.venv/bin/python" \
        -c "import sys; sys.path.insert(0,'$(dirname "${BASH_SOURCE[0]}")/..');
from core.datastore import summary
for k,v in summary().items(): print(f'    {k:14s} {v}')" 2>/dev/null || true
fi

exit $RC
