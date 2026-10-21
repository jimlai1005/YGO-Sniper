#!/bin/bash
# ---------------------------------------------------------------------------
# Dashboard 的實際入口。launchd 用 KeepAlive 常駐跑這個，你手動也可以跑這個。
# 跟 run_daily.sh 不同：這支不是「跑完就結束」的批次工作，是長駐的
# uvicorn server，所以沒有防重入鎖、沒有 watchdog timeout ——
# launchd 的 KeepAlive 本身就是「掛了就重啟」的機制。
# ---------------------------------------------------------------------------

set -uo pipefail

# 2026-10-21 事故：bash 邊執行邊依位元組位移讀檔，正式輪次跑到一半時原地改寫本檔，
# bash 回頭讀到錯的位移（同一輪重跑、無結束行、ledger 漏寫，且無任何錯誤訊息）。
# 所以 set 之後的全部內容包進 main()，檔尾 `main "$@"; exit $?`：bash 讀完整個函式
# 定義才開始執行。函式本體刻意不縮排（heredoc 結束符與 tests 的行首錨定正規式依賴它）。
main() {

# 排程時鐘釘台北（docs/plans/schedule-pin-taipei.md）：bash 的 date、ledger 的 finished_at、
# Python 內所有未指定時區的 datetime.now() 都跟著變成台北。Python 端排程邏輯另有
# clock.schedule_now() 自己釘，這行只是讓 log 與帳本看起來一致。
export TZ=Asia/Taipei

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
}

main "$@"; exit $?
