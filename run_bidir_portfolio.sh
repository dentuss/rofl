#!/bin/bash
# Multi-pair BIDIR portfolio launcher (non-Docker) — 5 bots, "inj_heavy" split.
#
# Each bot runs the production preset (triple_bidir + dir-regime + F&G + decay)
# on its own pair via the SYMBOL override. Default split (of TOTAL_EQUITY):
#   INJ 40% / SOL 20% / ADA 15% / ETH 15% / LINK 10%
#
# 4.6y backtest (common window, $100 total, funding modeled):
#   inj_heavy:  CAGR +92%  MDD -18.2%  Sharpe 1.95  worst month -6.6%  win 75%
#   (vs single INJ: CAGR +120% MDD -27.6% Sharpe 1.75 worst month -16.2%)
#
# Usage:
#   ./run_bidir_portfolio.sh                          # paper, $100 total
#   MODE=live TOTAL_EQUITY=1000 ./run_bidir_portfolio.sh
#
# Status:  python3 bot_status.py
# Stop:    Ctrl+C (the trap kills all child bots)

set -euo pipefail
cd "$(dirname "$0")"

: "${MODE:=paper}"
: "${EXCHANGE:=bybit}"
: "${TOTAL_EQUITY:=100}"
# Weights (must sum to 1.0)
: "${INJ_WEIGHT:=0.40}"
: "${SOL_WEIGHT:=0.20}"
: "${ADA_WEIGHT:=0.15}"
: "${ETH_WEIGHT:=0.15}"
: "${LINK_WEIGHT:=0.10}"

eq() { python3 -c "print(round(${TOTAL_EQUITY} * $1, 2))"; }

declare -A PAIRS=(
  [inj]="INJ/USDT:${INJ_WEIGHT}"
  [sol]="SOL/USDT:${SOL_WEIGHT}"
  [ada]="ADA/USDT:${ADA_WEIGHT}"
  [eth]="ETH/USDT:${ETH_WEIGHT}"
  [link]="LINK/USDT:${LINK_WEIGHT}"
)

echo "Launching 5-pair bidir portfolio (mode=$MODE, total=$TOTAL_EQUITY USDT):"
pids=()
for key in inj sol ada eth link; do
  IFS=':' read -r symbol weight <<< "${PAIRS[$key]}"
  pair_eq=$(eq "$weight")
  echo "  ${symbol}  ${pair_eq} USDT  (weight ${weight})"
  MODE="$MODE" EXCHANGE="$EXCHANGE" \
    STRATEGY_PRESET="adaptive_bidir" \
    SYMBOL="$symbol" \
    STARTING_EQUITY="$pair_eq" \
    STATE_FILE="state/bot_state_${key}.json" \
    LOG_FILE="logs/bot_${key}.log" \
    PARAMS_FILE="state/params_${key}.json" \
    python3 bot.py &
  pids+=($!)
done

echo ""
echo "PIDs: ${pids[*]}"
echo "Tail logs:  tail -f logs/bot_*.log"
echo "Status:     python3 bot_status.py"
echo "Stop:       Ctrl+C"

trap 'echo; echo "stopping..."; kill ${pids[*]} 2>/dev/null || true' EXIT INT TERM
wait
