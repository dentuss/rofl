#!/bin/bash
# Docker wrapper for the 5-pair bidir portfolio that splits ONE total equity
# across the bots by weight, so you only set TOTAL_EQUITY (not 5 vars).
#
# Weights default to the validated "inj_heavy" split (40/20/15/15/10).
# Set TOTAL_EQUITY in .env (or the environment); everything else passes
# through to `docker compose`.
#
# Usage:
#   sudo ./portfolio.sh up -d --build      # start (computes split from TOTAL_EQUITY)
#   sudo ./portfolio.sh logs -f            # tail all 5 bots
#   sudo ./portfolio.sh ps                 # status
#   sudo ./portfolio.sh down               # stop (keeps state)
#   sudo ./portfolio.sh down -v            # stop AND reset equity/state
#
#   TOTAL_EQUITY=1000 sudo -E ./portfolio.sh up -d --build
#
# Note: changing TOTAL_EQUITY only re-initializes equity on a FRESH state.
# To apply a new total to running bots, use `down -v` first (wipes state).

set -euo pipefail
cd "$(dirname "$0")"

COMPOSE_FILE="docker-compose.bidir-portfolio.yml"
ENV_FILE=".env"

# Read TOTAL_EQUITY + weights from environment, falling back to .env, then defaults.
read_env() {  # read_env KEY DEFAULT
    local key="$1" def="$2" val=""
    val="${!key:-}"
    if [[ -z "$val" && -f "$ENV_FILE" ]]; then
        val="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
    fi
    echo "${val:-$def}"
}

TOTAL_EQUITY="$(read_env TOTAL_EQUITY 100)"
INJ_WEIGHT="$(read_env INJ_WEIGHT 0.40)"
SOL_WEIGHT="$(read_env SOL_WEIGHT 0.20)"
ADA_WEIGHT="$(read_env ADA_WEIGHT 0.15)"
ETH_WEIGHT="$(read_env ETH_WEIGHT 0.15)"
LINK_WEIGHT="$(read_env LINK_WEIGHT 0.10)"

calc() { python3 -c "print(round($1 * $2, 2))"; }
export INJ_EQUITY="$(calc "$TOTAL_EQUITY" "$INJ_WEIGHT")"
export SOL_EQUITY="$(calc "$TOTAL_EQUITY" "$SOL_WEIGHT")"
export ADA_EQUITY="$(calc "$TOTAL_EQUITY" "$ADA_WEIGHT")"
export ETH_EQUITY="$(calc "$TOTAL_EQUITY" "$ETH_WEIGHT")"
export LINK_EQUITY="$(calc "$TOTAL_EQUITY" "$LINK_WEIGHT")"

# Only print the split for lifecycle commands that start bots.
case "${1:-}" in
    up|create|run)
        echo "Portfolio split of ${TOTAL_EQUITY} total:"
        echo "  INJ  ${INJ_EQUITY}   SOL ${SOL_EQUITY}   ADA ${ADA_EQUITY}   ETH ${ETH_EQUITY}   LINK ${LINK_EQUITY}"
        ;;
    status)
        # Custom subcommand: dump per-bot equity/position by execing into each
        # container. State files live inside Docker volumes, so the host's
        # `python3 bot_status.py` can't see them.
        total_eq=0
        total_pnl=0
        printf "%-6s%12s%12s%9s%12s  %s\n" "bot" "equity" "realized" "trades" "peak" "position"
        printf -- "%.0s-" {1..100}; printf "\n"
        for bot in inj sol ada eth link; do
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
                printf "%-6s%12s%12s%9s%12s  %s\n" "$bot" "-" "-" "-" "-" "container not running"
                continue
            fi
            IFS='|' read -r eq pnl n peak pos <<< "$line"
            total_eq=$(python3 -c "print($total_eq + $eq)")
            total_pnl=$(python3 -c "print($total_pnl + $pnl)")
            printf "%-6s%12.2f%+12.2f%9s%12.2f  %s\n" "$bot" "$eq" "$pnl" "$n" "$peak" "$pos"
        done
        printf -- "%.0s-" {1..100}; printf "\n"
        printf "%-6s%12.2f%+12.2f\n" "TOTAL" "$total_eq" "$total_pnl"
        exit 0
        ;;
esac

exec docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
