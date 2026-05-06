#!/bin/bash
# Run the recommended ETH+SOL 70/30 portfolio.
#
# Backtest (1y, 100 USDT):
#   single ETH:        +93%, MDD -10.8%, worst month -8.2%
#   ETH+SOL 70/30:     +76%, MDD  -9.3%, worst month -4.9% <-- this script
#
# Each pair runs as a separate bot instance, with its own state file and log.
# Stop both with `pkill -f bot.py` or by Ctrl+C in each window.
#
# Override MODE=live, EXCHANGE, API_KEY/SECRET/PASSPHRASE via env to go live.

set -euo pipefail
cd "$(dirname "$0")"

: "${MODE:=paper}"
: "${EXCHANGE:=kucoin}"
: "${TOTAL_EQUITY:=100}"
: "${ETH_WEIGHT:=0.7}"
: "${SOL_WEIGHT:=0.3}"
: "${PRESET:=steady}"

eth_eq=$(python3 -c "print(${TOTAL_EQUITY} * ${ETH_WEIGHT})")
sol_eq=$(python3 -c "print(${TOTAL_EQUITY} * ${SOL_WEIGHT})")

echo "Launching portfolio:"
echo "  ETH/USDT  $eth_eq USDT  (preset=$PRESET, mode=$MODE)"
echo "  SOL/USDT  $sol_eq USDT  (preset=$PRESET, mode=$MODE)"
echo ""

MODE="$MODE" EXCHANGE="$EXCHANGE" \
    SYMBOL="ETH/USDT" STARTING_EQUITY="$eth_eq" \
    STATE_FILE="bot_state_eth.json" LOG_FILE="bot_eth.log" \
    STRATEGY_PRESET="$PRESET" \
    python3 bot.py &
eth_pid=$!

MODE="$MODE" EXCHANGE="$EXCHANGE" \
    SYMBOL="SOL/USDT" STARTING_EQUITY="$sol_eq" \
    STATE_FILE="bot_state_sol.json" LOG_FILE="bot_sol.log" \
    STRATEGY_PRESET="$PRESET" \
    python3 bot.py &
sol_pid=$!

echo "ETH bot PID: $eth_pid"
echo "SOL bot PID: $sol_pid"
echo ""
echo "Tail logs:"
echo "  tail -f bot_eth.log bot_sol.log"
echo ""
echo "Stop:"
echo "  kill $eth_pid $sol_pid"

trap "kill $eth_pid $sol_pid 2>/dev/null || true" EXIT INT TERM
wait
