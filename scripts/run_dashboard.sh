#!/bin/bash
# ---------------------------------------------------------------------------
# Dashboard 的實際入口。launchd 用 KeepAlive 常駐跑這個，你手動也可以跑這個。
# 跟 run_daily.sh 不同：這支不是「跑完就結束」的批次工作，是長駐的
# uvicorn server，所以沒有防重入鎖、沒有 watchdog timeout ——
# launchd 的 KeepAlive 本身就是「掛了就重啟」的機制。
# ---------------------------------------------------------------------------

set -uo pipefail

PROJECT_DIR="$HOME/projects/ygo-sniper"
LOG_DIR="$PROJECT_DIR/data/logs"
LOG_FILE="$LOG_DIR/dashboard.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

if [ ! -f ".venv/bin/activate" ]; then
    echo "[$(date)] 找不到 .venv，請先跑 make setup" >> "$LOG_FILE"
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "===== $(date '+%Y-%m-%d %H:%M:%S') dashboard 啟動 =====" >> "$LOG_FILE"
exec ygo-sniper serve >> "$LOG_FILE" 2>&1
