#!/usr/bin/env bash
#
# Unattended sync for cron. Wraps the make targets with the things cron needs
# and an interactive shell gives you for free.
#
#     deploy/cron-sync.sh pull      # rsync both boxes + health report
#     deploy/cron-sync.sh daily     # pull + prune + backup
#
# Install (see `deploy/cron-sync.sh install` to print the lines):
#     0 */12 * * *  /home/dent/repos/rofl/deploy/cron-sync.sh pull
#     20 3   * * *  /home/dent/repos/rofl/deploy/cron-sync.sh daily
#
# Why a wrapper and not `cd repo && make pull` in the crontab:
#   * cron's PATH is ~/usr/bin:/bin — rsync/ssh resolve, but be explicit
#   * no SSH_AUTH_SOCK: auth must come from ~/.ssh/config IdentityFile, which
#     it does (IdentitiesOnly, passphrase-less Oracle keys)
#   * cron starts in $HOME, and the Makefile uses ./.venv — must cd first
#   * two runs must never overlap mid-rsync -> flock
#   * output has to go somewhere with timestamps, and not grow forever
#
# READ-ONLY against both boxes. Never restarts or writes to a box.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$REPO/logs/cron-sync.log"
LOCK="$REPO/.cron-sync.lock"
MAX_LOG_BYTES=$((5 * 1024 * 1024))
export PATH="/usr/local/bin:/usr/bin:/bin"
export PYTHONIOENCODING=utf-8

MODE="${1:-pull}"

mkdir -p "$(dirname "$LOG")"
# Trim from the FRONT so the newest entries always survive.
if [[ -f "$LOG" ]] && [[ $(stat -c %s "$LOG") -gt $MAX_LOG_BYTES ]]; then
    tail -c $((MAX_LOG_BYTES / 2)) "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

say() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"; }

run() {
    cd "$REPO" || { say "FATAL: cannot cd $REPO"; return 1; }
    local rc=0
    case "$MODE" in
        pull)
            say "=== pull ==="
            make pull; rc=$?
            ;;
        trackers)
            # Deterministic and self-reconstructing from the anchor (lagged
            # signals, so past values never revise) — a missed day is recovered
            # by the next run, not lost. Scheduled anyway: the 8-week XS record
            # gates the BOOK50/XS25/BAB25 capital discussion, and "it would
            # have recovered" is not a reason to leave it unrun for a month,
            # which is exactly what happened up to 2026-08-07.
            say "=== sleeve forward trackers ==="
            XS_ANCHOR=2026-07-09 PYTHONIOENCODING=utf-8 ./.venv/bin/python xs_paper.py || rc=$?
            SLEEVES_ANCHOR=2026-07-05 PYTHONIOENCODING=utf-8 ./.venv/bin/python sleeves_paper.py || rc=$?
            ;;
        daily)
            say "=== daily: pull + prune + backup + trackers ==="
            make pull || rc=$?
            # prune BEFORE backup: a stale .csv sitting next to its .csv.gz is
            # pure duplication (~75 MB/day) and would double the tarball.
            make prune || rc=$?
            make backup || rc=$?
            say "--- sleeve forward trackers ---"
            XS_ANCHOR=2026-07-09 PYTHONIOENCODING=utf-8 ./.venv/bin/python xs_paper.py || rc=$?
            SLEEVES_ANCHOR=2026-07-05 PYTHONIOENCODING=utf-8 ./.venv/bin/python sleeves_paper.py || rc=$?
            ;;
        install)
            cat <<EOF
Add these with \`crontab -e\` (times are the machine's local zone):

  0 */12 * * *  $REPO/deploy/cron-sync.sh pull
  20 3   * * *  $REPO/deploy/cron-sync.sh daily

  log: $LOG
EOF
            return 0
            ;;
        *)
            say "unknown mode '$MODE' (pull|daily|trackers|install)"; return 2
            ;;
    esac
    say "=== $MODE finished rc=$rc ==="
    return $rc
}

# flock: a 12-hourly pull must never start on top of a running one. -n means
# "skip this run" rather than queue, which is right for a periodic sync.
exec 9>"$LOCK"
if ! flock -n 9; then
    say "another cron-sync is running — skipping this tick" >> "$LOG"
    exit 0
fi

run >> "$LOG" 2>&1
exit $?
