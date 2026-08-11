#!/bin/bash
# ---------------------------------------------------------------------------
# 掃描的實際入口。launchd 跑這個（現在是每小時第 30 分），你手動也可以跑這個。
# 檔名維持 run_daily.sh 是為了不動到已經裝在使用者機器上的 plist 路徑。
#
# 為什麼要包一層 shell script，而不是讓 launchd 直接叫 python：
#   1. launchd 的環境變數幾乎是空的，PATH 裡沒有 homebrew、沒有 pyenv
#   2. 需要 cd 到專案目錄，否則相對路徑的 config/ 找不到
#   3. 要記 log，不然半夜失敗你永遠不會知道，只會以為「今天沒好貨」
#   4. 需要防重入 —— 見下面的鎖
#
# 排程監督（`data/last_run_exit`）的事故背景，以及 watchdog／孤兒視窗的
# 細節分析，都移到 docs/run_daily.md 了——這支腳本
# 本身只留「這裡在做什麼」，不留「為什麼」的長篇分析，避免這個檔案繼續
# 往「一堆註解夾兩行程式碼」的方向長下去。
# ---------------------------------------------------------------------------

set -uo pipefail

PROJECT_DIR="$HOME/projects/ygo-sniper"
LOG_DIR="$PROJECT_DIR/data/logs"
# log 仍然一天一個檔（現在是一天 24 段而不是 1 段），14 天輪替邏輯照舊可用
LOG_FILE="$LOG_DIR/daily-$(date +%Y%m%d).log"
LOCK_DIR="$PROJECT_DIR/data/run_daily.lock"
# 排程監督帳本：這一輪結束時（不論成功失敗）覆寫一次，讓下一輪的
# `ygo-sniper daily` 讀得到「上一輪到底是怎麼結束的」。細節見
# write_last_run_ledger 與 docs/run_daily.md。
LAST_RUN_FILE="$PROJECT_DIR/data/last_run_exit"

# 通知送達狀態。要在任何 early exit 之前就初始化——`set -u` 之下，
# write_last_run_ledger 在還沒呼叫過 notify_failure 的路徑（例如成功收尾）
# 引用未設變數會直接讓腳本掛掉，那就是本末倒置。
NOTIFY_ATTEMPTED=0
NOTIFY_HTTP="000"
NOTIFY_CURL_EXIT=0

mkdir -p "$LOG_DIR"
# mkdir -p 剛剛已經把 PROJECT_DIR 這條路徑上所有目錄都建出來了，所以這裡
# 的 cd 理論上不會失敗；留著只是防禦「mkdir 成功但 cd 失敗」這種更底層的
# 檔案系統問題（例如某層目錄少了執行權限）。沒有接通知／帳本：這個情境
# 下 LOG_FILE／LAST_RUN_FILE／.env 多半也一起不可靠，硬要接只是製造
# 另一個可能失敗的動作，不值得——真的踩到這裡，代表機器本身出了問題，
# 不是這支腳本能自己說清楚的層級。
cd "$PROJECT_DIR" || exit 1

# ---------------------------------------------------------------------------
# 防重入。改成每小時之後這是必需品：一輪掃描要開 Playwright（chromium），
# 遇到 WAF 重取 token 或網路慢時可能跑超過一小時，下一輪就會疊上來
# —— 兩個 chromium 同時搶記憶體、同時打同一個站（更容易被擋）、
# 還會對同一批 signal 重複推播。
#
# macOS 沒有 util-linux 的 flock，所以用 mkdir 當鎖：mkdir 在既有目錄上必定失敗，
# 這個「建立或失敗」是原子的，比「先 test -f 再 touch」可靠（後者有 TOCTOU 窗口）。
# 另外存 pid，才能分辨「真的還在跑」與「上次被 kill -9 留下的殘鎖」——
# 只看鎖存不存在的話，一次當機就會讓排程永遠停擺，而且完全無聲。
# ---------------------------------------------------------------------------
LOCK_OWNER=""
acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo $$ > "$LOCK_DIR/pid"
        return 0
    fi
    LOCK_OWNER=$(cat "$LOCK_DIR/pid" 2>/dev/null)
    # kill -0 只探測行程是否存在，不送出任何訊號
    if [ -n "$LOCK_OWNER" ] && kill -0 "$LOCK_OWNER" 2>/dev/null; then
        return 1
    fi
    # 殘鎖（持有者已不在）：接管它
    echo "[$(date '+%H:%M:%S')] 清掉殘鎖（前持有者 pid=${LOCK_OWNER:-未知} 已不存在）" \
        >> "$LOG_FILE"
    echo $$ > "$LOCK_DIR/pid"
    return 0
}

if ! acquire_lock; then
    # 變數一律加大括號：後面接的是全形「）」，bash 在多位元組 locale 下會把它
    # 吃進識別字，變成 `LOCK_OWNER）: unbound variable`（set -u 直接讓本輪掛掉）。
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 上一輪仍在執行中（pid ${LOCK_OWNER}），本輪跳過" \
        >> "$LOG_FILE"
    # exit 0 而不是非零：「跳過」是預期內的正常結果，不是失敗。
    # 回非零會讓下面那段失敗通知每小時發一則「掃描失敗」，把真正的故障淹掉。
    # 不動 LAST_RUN_FILE：這一輪根本沒有代表任何一次真正的執行，帳本應該
    # 繼續留著上一次真正跑完的那筆紀錄，給下一次真正執行的那一輪去讀。
    exit 0
fi
# trap 只在真的拿到鎖之後才設 —— 設在 acquire_lock 之前的話，
# 被跳過的那一輪會在結束時把**正在跑的那一輪**的鎖刪掉。
trap 'rm -rf "$LOCK_DIR"' EXIT

# ---------------------------------------------------------------------------
# 失敗通知：唯一真的打 curl 的地方，成功／失敗與 HTTP 狀態都要留痕。
# 過去這裡是 `curl -s ... > /dev/null`——結果直接丟掉，如果這顆 curl 本身
# 送不出去（筆電剛醒、Wi-Fi 還沒穩，正是下面 ping 等待迴圈要處理的那個
# 情境），失敗通知就會靜靜地失敗第二次，整個事故 100% 無聲。
# 現在改成量到 http_code／curl 自己的 exit code，寫進 log，也一併存進
# NOTIFY_* 全域變數讓 write_last_run_ledger 讀——下一輪 `ygo-sniper daily`
# 會把「連失敗通知都沒送達」這件事折進它自己的排程監督告警裡
# （schedule_watch.watchdog_message，走同一套 pending／送達才消耗的帳本）。
# ---------------------------------------------------------------------------
notify_failure() {
    local msg="$1"
    NOTIFY_ATTEMPTED=0
    NOTIFY_HTTP="000"
    NOTIFY_CURL_EXIT=0
    if [ ! -f .env ]; then
        echo "[$(date '+%H:%M:%S')] 失敗通知沒有送出——找不到 .env（TELEGRAM_BOT_TOKEN／CHAT_ID 沒地方讀）" \
            >> "$LOG_FILE"
        return
    fi
    # shellcheck disable=SC1091
    source .env
    NOTIFY_ATTEMPTED=1
    NOTIFY_HTTP=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "text=${msg}")
    NOTIFY_CURL_EXIT=$?
    if [ "$NOTIFY_CURL_EXIT" -eq 0 ] && [ "$NOTIFY_HTTP" = "200" ]; then
        echo "[$(date '+%H:%M:%S')] 失敗通知已送達（http=200）" >> "$LOG_FILE"
    else
        echo "[$(date '+%H:%M:%S')] 失敗通知本身也沒送成功（curl exit=$NOTIFY_CURL_EXIT, http=$NOTIFY_HTTP）——" \
            "留痕於 $LAST_RUN_FILE，下一輪起跑時會經 PENDING_ALERT_KEY 補送" \
            >> "$LOG_FILE"
    fi
}

# 排程監督帳本：不論這一輪怎麼結束都要覆寫（成功也要寫）——見腳本開頭
# LAST_RUN_FILE 的註解：不覆寫的話，一次 exit=124 的殘留紀錄會被下一輪
# 誤讀成「還沒被處理過」，永遠重複告警下去。
write_last_run_ledger() {
    local exit_code="$1"
    local attempted_json="false"
    [ "$NOTIFY_ATTEMPTED" -eq 1 ] && attempted_json="true"
    cat > "$LAST_RUN_FILE" <<EOF
{"exit": $exit_code, "finished_at": "$(date '+%Y-%m-%dT%H:%M:%S')", "notify_attempted": $attempted_json, "notify_http": "$NOTIFY_HTTP", "notify_curl_exit": $NOTIFY_CURL_EXIT}
EOF
}

if [ ! -f ".venv/bin/activate" ]; then
    echo "[$(date)] 找不到 .venv，請先跑 make setup" >> "$LOG_FILE"
    notify_failure "⚠️ ygo-sniper 找不到 .venv（請先跑 make setup），本輪整個沒執行到掃描，請看 data/logs/"
    write_last_run_ledger 1
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "===== $(date '+%Y-%m-%d %H:%M:%S') 開始 =====" >> "$LOG_FILE"

# MBP 剛從睡眠喚醒時 Wi-Fi 可能還沒連上，等一下再跑
for i in 1 2 3 4 5 6; do
    if ping -c 1 -t 2 1.1.1.1 > /dev/null 2>&1; then
        break
    fi
    echo "[$(date '+%H:%M:%S')] 等待網路… ($i/6)" >> "$LOG_FILE"
    sleep 10
done

# watchdog：醒著卡死不得超過 25 分（睡眠凍結不計入，見 run_with_timeout.py）。
# 124 = 被 watchdog 終止，下面的失敗通知會用不同文案。孤兒視窗、
# `.venv/bin/python` 而非裸 `python` 的理由都寫在
# docs/run_daily.md，這裡不重複。
.venv/bin/python scripts/run_with_timeout.py "${YGO_CYCLE_TIMEOUT:-1500}" \
    ygo-sniper daily >> "$LOG_FILE" 2>&1
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo "[$(date '+%H:%M:%S')] 掃描失敗，exit=$STATUS" >> "$LOG_FILE"
    if [ "$STATUS" -eq 124 ]; then
        MSG="🚨 ygo-sniper 本輪被 watchdog 強制終止（超過 ${YGO_CYCLE_TIMEOUT:-1500}s），可能 Playwright 卡死，請看 data/logs/"
    else
        MSG="⚠️ ygo-sniper 掃描失敗 (exit=${STATUS})，請看 data/logs/"
    fi
    # 失敗一定要主動告訴你。沉默的失敗是這類排程工具最大的坑：
    # 你會連續三週以為市場沒好貨，其實是 parser 早就掛了。
    notify_failure "$MSG"
fi

echo "===== $(date '+%H:%M:%S') 結束 (exit=$STATUS) =====" >> "$LOG_FILE"

write_last_run_ledger "$STATUS"

# 只留最近 14 天的 log
find "$LOG_DIR" -name 'daily-*.log' -mtime +14 -delete 2>/dev/null

exit $STATUS
