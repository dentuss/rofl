#!/usr/bin/env bash
#
# The data pipeline: get everything both Oracle boxes have produced onto this
# laptop, ready to analyse, in one command.
#
#     deploy/pull-data.sh setup     # once: enter the two IPs, verifies SSH
#     deploy/pull-data.sh           # every time after: pull both + health report
#
# or via the Makefile:  make setup / make pull / make watch
#
# COMMANDS
#   setup          interactive one-time host config -> deploy/hosts.env
#   pull | all     pull both boxes, then run the health report   (default)
#   ticks | live   pull just one box
#   health         health report on whatever is already local
#   watch [secs]   pull on a loop (default 300s) — leave it running while
#                  you work and the local tree stays warm
#   status         show configured hosts + reachability + local sizes
#
# READ-ONLY on the remote: rsync only reads. It restarts nothing, writes
# nothing, and is safe against a live trading box holding a position.
#
# Hosts resolve in this order: env vars > deploy/hosts.env > ~/.ssh/config
# aliases `rofl-collector` / `rofl-trading`.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
HOSTS_FILE="$HERE/hosts.env"
LOCAL_DIR="${ROFL_DATA:-$REPO/data}"
PY="$REPO/.venv/bin/python"

# hosts.env holds no secrets (IPs only) but is gitignored — it is per-machine.
[[ -f "$HOSTS_FILE" ]] && . "$HOSTS_FILE"
COLLECTOR_HOST="${COLLECTOR_HOST:-rofl-collector}"
TRADING_HOST="${TRADING_HOST:-rofl-trading}"
REMOTE_DIR="${REMOTE_DIR:-rofl/data}"

bold() { echo -e "\033[1m$*\033[0m"; }
log()  { echo -e "\033[1;34m==>\033[0m $*"; }
ok()   { echo -e "\033[1;32m  ok\033[0m $*"; }
warn() { echo -e "\033[1;33mWARN:\033[0m $*"; }
err()  { echo -e "\033[1;31mERROR:\033[0m $*" >&2; }

reachable() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$1" true 2>/dev/null; }

# ------------------------------------------------------------------ setup
cmd_setup() {
    bold "One-time host setup for the rofl data pipeline"
    echo "Enter the PUBLIC IP (or user@ip) of each Oracle box."
    echo "Leave blank to keep the current value. User defaults to 'ubuntu'."
    echo
    read -rp "  collector box [${COLLECTOR_HOST}]: " c
    read -rp "  trading box   [${TRADING_HOST}]: " t
    c="${c:-$COLLECTOR_HOST}"; t="${t:-$TRADING_HOST}"
    [[ "$c" == *@* || "$c" == rofl-* ]] || c="ubuntu@$c"
    [[ "$t" == *@* || "$t" == rofl-* ]] || t="ubuntu@$t"

    cat > "$HOSTS_FILE" <<EOF
# Written by deploy/pull-data.sh setup. Per-machine, gitignored.
COLLECTOR_HOST=$c
TRADING_HOST=$t
REMOTE_DIR=$REMOTE_DIR
EOF
    ok "wrote $HOSTS_FILE"
    echo
    log "verifying SSH (BatchMode — your key must already be authorised)"
    local bad=0
    for pair in "collector:$c" "trading:$t"; do
        local name="${pair%%:*}" host="${pair#*:}"
        if reachable "$host"; then ok "$name  $host"
        else err "$name  $host  unreachable"; bad=1; fi
    done
    if [[ $bad -eq 1 ]]; then
        echo
        warn "Fix SSH before pulling. Usual causes:"
        echo "    - key not on the box:  ssh-copy-id $c"
        echo "    - Oracle security list only allows port 22 from your IP,"
        echo "      and your IP changed"
        echo "    - wrong user (Ubuntu images use 'ubuntu')"
        return 1
    fi
    echo; ok "setup complete — from now on just run:  make pull"
}

# ------------------------------------------------------------------ pull
pull_one() {
    local host="$1" subdir="$2" label="$3"
    if ! reachable "$host"; then
        warn "$label box ($host) unreachable — skipping $subdir"
        return 1
    fi
    mkdir -p "$LOCAL_DIR/$subdir"
    # --partial: the collector appends while we read, so a cut transfer resumes
    # next run instead of restarting. Gzipped past days never change, so after
    # the first pull only today's files move.
    rsync -az --partial --timeout=60 --info=stats1 \
          "$host:$REMOTE_DIR/$subdir/" "$LOCAL_DIR/$subdir/" \
        | sed 's/^/    /'
    local rc=${PIPESTATUS[0]}
    if [[ $rc -ne 0 ]]; then
        warn "rsync exited $rc for $subdir (whatever transferred is kept)"
        return 1
    fi
    ok "$label -> $LOCAL_DIR/$subdir"
    return 0
}

cmd_pull() {
    local what="${1:-all}" rc=0
    mkdir -p "$LOCAL_DIR"
    case "$what" in
        ticks) log "pulling ticks";       pull_one "$COLLECTOR_HOST" ticks collector || rc=1 ;;
        live)  log "pulling live state";  pull_one "$TRADING_HOST"  live  trading   || rc=1 ;;
        all)   log "pulling both boxes"
               pull_one "$COLLECTOR_HOST" ticks collector || rc=1
               pull_one "$TRADING_HOST"  live  trading   || rc=1 ;;
        *)     err "unknown target '$what'"; return 2 ;;
    esac
    if [[ $rc -ne 0 ]]; then
        echo
        warn "at least one box did not sync. 'deploy/pull-data.sh status' shows why."
        warn "Local data is unchanged for that box — nothing was deleted."
    fi
    return $rc
}

# ------------------------------------------------------------------ health
cmd_health() {
    echo
    if [[ -x "$PY" ]]; then
        ROFL_DATA="$LOCAL_DIR" PYTHONIOENCODING=utf-8 "$PY" "$REPO/research/data_health.py"
    else
        warn "no venv at $PY — skipping the health report"
        echo "  python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
    fi
}

# ------------------------------------------------------------------ status
cmd_status() {
    bold "rofl data pipeline"
    echo "  config      ${HOSTS_FILE}$( [[ -f "$HOSTS_FILE" ]] || echo '  (not created — run: make setup)')"
    echo "  local dir   $LOCAL_DIR"
    echo
    for pair in "collector:$COLLECTOR_HOST" "trading:$TRADING_HOST"; do
        local name="${pair%%:*}" host="${pair#*:}"
        if reachable "$host"; then ok "$name  $host  reachable"
        else err "$name  $host  UNREACHABLE"; fi
    done
    echo
    if [[ -d "$LOCAL_DIR" ]]; then
        bold "local tree"
        du -sh "$LOCAL_DIR"/* 2>/dev/null | sed 's/^/    /' || echo "    (empty)"
        local nd
        nd=$(find "$LOCAL_DIR/ticks" -maxdepth 1 -type d -name '20*' 2>/dev/null | wc -l)
        echo "    tick day-dirs: $nd"
    else
        echo "  no local data yet"
    fi
}

# ------------------------------------------------------------------ watch
cmd_watch() {
    local secs="${1:-300}"
    log "watching — pulling every ${secs}s. Ctrl-C to stop."
    while true; do
        echo; echo "--- $(date -u +'%Y-%m-%d %H:%M:%S UTC') ---"
        cmd_pull all || true
        sleep "$secs"
    done
}

# ------------------------------------------------------------------ main
case "${1:-pull}" in
    setup)          cmd_setup ;;
    status)         cmd_status ;;
    health)         cmd_health ;;
    watch)          cmd_watch "${2:-300}" ;;
    ticks|live)     cmd_pull "$1"; cmd_health ;;
    pull|all)       cmd_pull all; cmd_health ;;
    -h|--help|help) sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
    *)              err "unknown command '$1'"; sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
