#!/bin/bash
# Docker wrapper for the bidir portfolios. Splits ONE total equity across the
# bots by weight, so you only set TOTAL_EQUITY (not per-pair vars).
#
# Two portfolios are available:
#   PORTFOLIO=5 (default)  inj_heavy:   INJ 40 / SOL 20 / ADA 15 / ETH 15 / LINK 10
#   PORTFOLIO=8            equal-weight: INJ AVAX NEAR AAVE GRT RUNE DOGE ADA (12.5 each)
#
# Usage:
#   sudo ./portfolio.sh up -d --build              # start 5-pair (computes split)
#   PORTFOLIO=8 sudo -E ./portfolio.sh up -d --build   # start 8-pair
#   sudo ./portfolio.sh status                     # per-bot equity/position
#   sudo ./portfolio.sh logs -f                    # tail all bots
#   sudo ./portfolio.sh down                       # stop (keeps state)
#   sudo ./portfolio.sh down -v                    # stop AND reset equity/state
#
#   TOTAL_EQUITY=1000 sudo -E ./portfolio.sh up -d --build
#
# Notes:
#   - changing TOTAL_EQUITY only re-initializes equity on a FRESH state; use
#     `down -v` first to apply a new total to running bots.
#   - PORTFOLIO can also be set in .env. Don't run both portfolios at once:
#     they share container names for overlapping pairs (INJ, ADA).

set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env"

# Read a var from environment, falling back to .env, then a default.
read_env() {  # read_env KEY DEFAULT
    local key="$1" def="$2" val=""
    val="${!key:-}"
    if [[ -z "$val" && -f "$ENV_FILE" ]]; then
        val="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
    fi
    echo "${val:-$def}"
}

PORTFOLIO="$(read_env PORTFOLIO 5)"
TOTAL_EQUITY="$(read_env TOTAL_EQUITY 100)"

if [[ "$PORTFOLIO" == "8" ]]; then
    COMPOSE_FILE="docker-compose.bidir8.yml"
    BOTS=(inj avax near aave grt rune doge ada)
    declare -A WEIGHTS=(
        [inj]="$(read_env INJ_WEIGHT 0.125)"   [avax]="$(read_env AVAX_WEIGHT 0.125)"
        [near]="$(read_env NEAR_WEIGHT 0.125)" [aave]="$(read_env AAVE_WEIGHT 0.125)"
        [grt]="$(read_env GRT_WEIGHT 0.125)"   [rune]="$(read_env RUNE_WEIGHT 0.125)"
        [doge]="$(read_env DOGE_WEIGHT 0.125)" [ada]="$(read_env ADA_WEIGHT 0.125)"
    )
else
    COMPOSE_FILE="docker-compose.bidir-portfolio.yml"
    BOTS=(inj sol ada eth link)
    declare -A WEIGHTS=(
        [inj]="$(read_env INJ_WEIGHT 0.40)" [sol]="$(read_env SOL_WEIGHT 0.20)"
        [ada]="$(read_env ADA_WEIGHT 0.15)" [eth]="$(read_env ETH_WEIGHT 0.15)"
        [link]="$(read_env LINK_WEIGHT 0.10)"
    )
fi

calc() { python3 -c "print(round($1 * $2, 2))"; }
for bot in "${BOTS[@]}"; do
    var="$(echo "$bot" | tr '[:lower:]' '[:upper:]')_EQUITY"
    export "$var"="$(calc "$TOTAL_EQUITY" "${WEIGHTS[$bot]}")"
done

# Refuse to start a portfolio if the OTHER one is already running — they
# share container names for INJ/ADA and the running bots would be evicted.
case "${1:-}" in
    up|create|run)
        if [[ "$PORTFOLIO" == "5" ]]; then
            other_running="$(docker ps --filter "name=rofl-(avax|near|aave|grt|rune|doge)$" -q 2>/dev/null | wc -l)"
            other_name="8-pair"
        else
            other_running="$(docker ps --filter "name=rofl-(sol|eth|link)$" -q 2>/dev/null | wc -l)"
            other_name="5-pair"
        fi
        if [[ "$other_running" -gt 0 ]]; then
            echo "ERROR: $other_name portfolio appears to be running ($other_running containers)."
            echo "       Bring it down first with: sudo ./portfolio.sh down"
            echo "       (or the corresponding 'PORTFOLIO=N sudo -E ./portfolio.sh down')"
            exit 1
        fi
        ;;
esac

# Only print the split for lifecycle commands that start bots.
case "${1:-}" in
    up|create|run)
        echo "Portfolio ${PORTFOLIO}-pair split of ${TOTAL_EQUITY} total:"
        line="  "
        for bot in "${BOTS[@]}"; do
            var="$(echo "$bot" | tr '[:lower:]' '[:upper:]')_EQUITY"
            line+="$(echo "$bot" | tr '[:lower:]' '[:upper:]') ${!var}   "
        done
        echo "$line"
        ;;
    status)
        # Custom subcommand: dump per-bot equity/position by execing into each
        # container. State files live inside Docker volumes, so the host's
        # `python3 bot_status.py` can't see them.
        total_eq=0
        total_pnl=0
        printf "%-6s%-9s%12s%12s%9s%12s  %s\n" "bot" "health" "equity" "realized" "trades" "peak" "position"
        printf -- "%.0s-" {1..108}; printf "\n"
        for bot in "${BOTS[@]}"; do
            health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "rofl-$bot" 2>/dev/null || echo "?")"
            line="$(docker exec "rofl-$bot" cat /app/state/bot_state.json 2>/dev/null \
                | python3 -c '
import json, sys, datetime
d = json.load(sys.stdin)
eq, pnl = d.get("equity",0), d.get("realised_pnl",0)
n, peak = d.get("realised_trades",0), d.get("equity_peak",eq)
pos = d.get("position")
if pos:
    side = "LONG" if pos["side"]==1 else "SHORT"
    qty, ent = pos["qty"], pos["entry_px"]
    sl, tp = pos["sl"], pos["tp"]
    age_s = int(datetime.datetime.now(datetime.timezone.utc).timestamp() - pos.get("open_ts",0))
    age_str = f"{age_s//3600}h{(age_s%3600)//60}m"
    pos_s = f"{side} qty={qty:.4f} entry={ent:.4f} sl={sl:.4f} tp={tp:.4f} age={age_str}"
else:
    pos_s = "(flat)"
print(f"{eq:.2f}|{pnl:.2f}|{n}|{peak:.2f}|{pos_s}")
' 2>/dev/null)"
            if [[ -z "$line" ]]; then
                printf "%-6s%-9s%12s%12s%9s%12s  %s\n" "$bot" "$health" "-" "-" "-" "-" "container not running"
                continue
            fi
            IFS='|' read -r eq pnl n peak pos <<< "$line"
            total_eq=$(python3 -c "print($total_eq + $eq)")
            total_pnl=$(python3 -c "print($total_pnl + $pnl)")
            printf "%-6s%-9s%12.2f%+12.2f%9s%12.2f  %s\n" "$bot" "$health" "$eq" "$pnl" "$n" "$peak" "$pos"
        done
        printf -- "%.0s-" {1..108}; printf "\n"
        printf "%-6s%-9s%12.2f%+12.2f\n" "TOTAL" "" "$total_eq" "$total_pnl"
        exit 0
        ;;
    archive)
        # Copy each bot's state + log + event log out of its docker volume into
        # a timestamped local dir, then build a markdown summary + trades CSV.
        # Safe to run while bots are live (cp is non-blocking, brief snapshot).
        ts="$(date -u +%Y-%m-%d_%H%M%S)"
        out="archives/run_${PORTFOLIO}pair_${ts}"
        mkdir -p "$out"
        echo "Archiving ${PORTFOLIO}-pair portfolio to $out ..."
        for bot in "${BOTS[@]}"; do
            mkdir -p "$out/$bot"
            # Skip silently if the container isn't running
            if ! docker inspect --format '{{.State.Running}}' "rofl-$bot" 2>/dev/null | grep -q true; then
                echo "  $bot: container not running, skipped"
                continue
            fi
            docker cp "rofl-$bot:/app/state/." "$out/$bot/" 2>/dev/null || true
            docker cp "rofl-$bot:/app/logs/." "$out/$bot/" 2>/dev/null || true
            echo "  $bot: copied"
        done
        python3 research/build_archive.py "$out"
        echo
        echo "Archive ready: $out"
        echo "  scp -r ec2:~/rofl/$out ./   # to copy to your laptop"
        exit 0
        ;;
    reset-position)
        # Clear ONE bot's tracked position back to flat, leaving equity/PnL/
        # peak intact. Use after you've manually flattened that symbol on the
        # exchange and the bot is stuck in a state/exchange conflict.
        #   sudo ./portfolio.sh reset-position sol
        target="${2:-}"
        if [[ -z "$target" ]]; then
            echo "usage: ./portfolio.sh reset-position <bot>   (e.g. sol)"; exit 2
        fi
        cont="rofl-${target}"
        echo "Resetting tracked position for ${cont} to flat ..."
        echo "  (make sure you've already closed ${target^^} on the exchange!)"
        docker stop "$cont" >/dev/null 2>&1 || true
        # Edit the state file inside its volume via a throwaway container.
        vol="$(docker inspect "$cont" --format '{{range .Mounts}}{{if eq .Destination "/app/state"}}{{.Name}}{{end}}{{end}}' 2>/dev/null)"
        if [[ -z "$vol" ]]; then
            echo "ERROR: could not find state volume for $cont"; exit 1
        fi
        docker run --rm -v "$vol":/s python:3.11-slim python3 -c "
import json, pathlib
f = pathlib.Path('/s/bot_state.json')
d = json.loads(f.read_text())
old = d.get('position')
d['position'] = None
f.write_text(json.dumps(d, indent=2))
print('  cleared position:', old)
print('  equity preserved:', d.get('equity'))
"
        docker start "$cont" >/dev/null 2>&1 || true
        echo "Done. ${cont} restarted flat; it will re-enter on its next signal."
        exit 0
        ;;
esac

exec docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
