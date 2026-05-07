#!/bin/bash
# Run the recommended ETH/BTC/SOL equal-weight portfolio.
#
# 5-year backtest (100 USDT, 0.06% fee, 2bp slip, triple_long no filters):
#   Single ETH:               +201%, MDD -29%, Sharpe 0.80
#   ETH+BTC 50/50:            +195%, MDD -30%, Sharpe 0.84
#   ETH+SOL 70/30:            +410%, MDD -22%, Sharpe 1.10
#   ETH+BTC+SOL 33/33/33:     +434%, MDD -22%, Sharpe 1.13   <-- this script
#
# Year-by-year of single ETH triple_long (every year profitable!):
#   2021: +17%   2022: +25.5% (bear)   2023: +15%
#   2024: +50%   2025: +11%   2026: +7% (partial)
#
# Each pair runs as a separate bot instance with its own state file and log.
# Stop with `pkill -f bot.py` or by Ctrl+C in the launcher.

set -euo pipefail
cd "$(dirname "$0")"

: "${MODE:=paper}"
: "${EXCHANGE:=kucoin}"
: "${TOTAL_EQUITY:=100}"
: "${ETH_WEIGHT:=0.34}"
: "${BTC_WEIGHT:=0.33}"
: "${SOL_WEIGHT:=0.33}"
: "${PRESET:=steady}"

eth_eq=$(python3 -c "print(${TOTAL_EQUITY} * ${ETH_WEIGHT})")
btc_eq=$(python3 -c "print(${TOTAL_EQUITY} * ${BTC_WEIGHT})")
sol_eq=$(python3 -c "print(${TOTAL_EQUITY} * ${SOL_WEIGHT})")

echo "Launching ETH/BTC/SOL portfolio:"
echo "  ETH/USDT  $eth_eq USDT  (preset=$PRESET, mode=$MODE)"
echo "  BTC/USDT  $btc_eq USDT  (preset=$PRESET, mode=$MODE)"
echo "  SOL/USDT  $sol_eq USDT  (preset=$PRESET, mode=$MODE)"
echo ""

MODE="$MODE" EXCHANGE="$EXCHANGE" \
    SYMBOL="ETH/USDT" STARTING_EQUITY="$eth_eq" \
    STATE_FILE="bot_state_eth.json" LOG_FILE="bot_eth.log" \
    STRATEGY_PRESET="$PRESET" \
    python3 bot.py &
eth_pid=$!

MODE="$MODE" EXCHANGE="$EXCHANGE" \
    SYMBOL="BTC/USDT" STARTING_EQUITY="$btc_eq" \
    STATE_FILE="bot_state_btc.json" LOG_FILE="bot_btc.log" \
    STRATEGY_PRESET="$PRESET" \
    python3 bot.py &
btc_pid=$!

MODE="$MODE" EXCHANGE="$EXCHANGE" \
    SYMBOL="SOL/USDT" STARTING_EQUITY="$sol_eq" \
    STATE_FILE="bot_state_sol.json" LOG_FILE="bot_sol.log" \
    STRATEGY_PRESET="$PRESET" \
    python3 bot.py &
sol_pid=$!

echo "ETH bot PID: $eth_pid"
echo "BTC bot PID: $btc_pid"
echo "SOL bot PID: $sol_pid"
echo ""
echo "Tail logs:"
echo "  tail -f bot_eth.log bot_btc.log bot_sol.log"
echo ""
echo "Stop:"
echo "  kill $eth_pid $btc_pid $sol_pid"

trap "kill $eth_pid $btc_pid $sol_pid 2>/dev/null || true" EXIT INT TERM
wait
