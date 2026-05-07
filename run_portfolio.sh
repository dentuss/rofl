#!/bin/bash
# Run a 3-pair portfolio: ETH 1h, BTC 1h, SOL 30m.
#
# 5-year backtest (per asset, 100 USDT, triple_long, 0.06% fee, 2bp slip):
#   ETH 1h r=1.5%:  +201% / MDD -29% / monthly med +1.4%
#   BTC 1h r=1.5%:  +188% / MDD -43% / monthly med +0.7%
#   SOL 30m r=1.5%: +945% / MDD -52% / monthly med +3.0%
#   SOL 30m r=2.0%: +1842%/ MDD -63% / monthly med +3.9%
#
# This launcher splits capital across all three. SOL 30m at r=1.5% is the
# growth engine; ETH/BTC 1h at r=1.5% are the stabilizers.
# Each pair runs as a separate bot instance with its own state file and log.
# Stop with `pkill -f bot.py` or by Ctrl+C in the launcher.
#
# Higher-monthly variants:
#   PRESET_SOL=high_return ./run_portfolio.sh   # SOL r=2.0% (~+4%/mo target)
#   PRESET_SOL=aggressive  ./run_portfolio.sh   # SOL r=2.5% (~+4.6%/mo, MDD -73%)
#   PRESET_SOL=yolo        ./run_portfolio.sh   # SOL r=3.0% (~+5.3%/mo, MDD -80%)

set -euo pipefail
cd "$(dirname "$0")"

: "${MODE:=paper}"
: "${EXCHANGE:=kucoin}"
: "${TOTAL_EQUITY:=100}"
: "${ETH_WEIGHT:=0.34}"
: "${BTC_WEIGHT:=0.33}"
: "${SOL_WEIGHT:=0.33}"
: "${PRESET_ETH:=steady}"
: "${PRESET_BTC:=steady}"
: "${PRESET_SOL:=growth}"

eth_eq=$(python3 -c "print(${TOTAL_EQUITY} * ${ETH_WEIGHT})")
btc_eq=$(python3 -c "print(${TOTAL_EQUITY} * ${BTC_WEIGHT})")
sol_eq=$(python3 -c "print(${TOTAL_EQUITY} * ${SOL_WEIGHT})")

echo "Launching 3-pair portfolio:"
echo "  ETH/USDT  $eth_eq USDT  (preset=$PRESET_ETH, mode=$MODE)"
echo "  BTC/USDT  $btc_eq USDT  (preset=$PRESET_BTC, mode=$MODE)"
echo "  SOL/USDT  $sol_eq USDT  (preset=$PRESET_SOL, mode=$MODE)"
echo ""

MODE="$MODE" EXCHANGE="$EXCHANGE" \
    STARTING_EQUITY="$eth_eq" \
    STATE_FILE="bot_state_eth.json" LOG_FILE="bot_eth.log" \
    STRATEGY_PRESET="$PRESET_ETH" \
    python3 bot.py &
eth_pid=$!

MODE="$MODE" EXCHANGE="$EXCHANGE" \
    STARTING_EQUITY="$btc_eq" \
    STATE_FILE="bot_state_btc.json" LOG_FILE="bot_btc.log" \
    STRATEGY_PRESET="$PRESET_BTC" \
    python3 bot.py &
btc_pid=$!

MODE="$MODE" EXCHANGE="$EXCHANGE" \
    STARTING_EQUITY="$sol_eq" \
    STATE_FILE="bot_state_sol.json" LOG_FILE="bot_sol.log" \
    STRATEGY_PRESET="$PRESET_SOL" \
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
