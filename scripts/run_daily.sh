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
# ---------------------------------------------------------------------------

set -uo pipefail

PROJECT_DIR="$HOME/projects/ygo-sniper"
LOG_DIR="$PROJECT_DIR/data/logs"
# log 仍然一天一個檔（現在是一天 24 段而不是 1 段），14 天輪替邏輯照舊可用
LOG_FILE="$LOG_DIR/daily-$(date +%Y%m%d).log"
LOCK_DIR="$PROJECT_DIR/data/run_daily.lock"

mkdir -p "$LOG_DIR"
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
    exit 0
fi
# trap 只在真的拿到鎖之後才設 —— 設在 acquire_lock 之前的話，
# 被跳過的那一輪會在結束時把**正在跑的那一輪**的鎖刪掉。
trap 'rm -rf "$LOCK_DIR"' EXIT

if [ ! -f ".venv/bin/activate" ]; then
    echo "[$(date)] 找不到 .venv，請先跑 make setup" >> "$LOG_FILE"
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
# 124 = 被 watchdog 終止，下面的失敗通知會用不同文案。
#
# 用 .venv/bin/python 而不是裸 python：上面已經 source .venv/bin/activate，
# 裸 python 理論上也會落在 venv 的 bin 裡，但那樣就是在依賴「activate 這步
# 真的成功把 PATH 排到前面」——多一層隱含順序。改成 .venv/bin/python 直接指名，
# 不繞 PATH 解析；而且上面的 `.venv/bin/activate` 存在檢查已經保證了
# `.venv/bin/python` 必定存在，不是新增依賴，只是把既有保證講得更明白。
# （stock macOS 沒有 `python`，只有 `python3`；活動失敗時裸 python 會是
# exit 127，雖然照樣會走進下面失敗分支大聲告警，不是靜默失敗，但沒必要
# 讓失敗多繞一層猜測。）
#
# watchdog 的新孤兒視窗（只記錄，不重新設計鎖）：如果有東西只殺掉了
# run_with_timeout.py 這個 supervisor 行程本身、而不是它的 process group
# （例如 OOM killer 挑上了 supervisor 的 pid，或有人手動 kill -9 這個 pid），
# 這裡的 shell wait 會直接拿到回傳、往下走、觸發 EXIT trap 把鎖釋放掉——
# 但孫行程 `ygo-sniper daily` 的 process group 沒人管了，變成孤兒，
# 而且從此沒有任何 timeout 在管它。下一輪排程一看鎖已經沒了，就會開始
# 跑第二個 `ygo-sniper daily`，兩個行程同時打同一批賣場、同時寫同一個
# sqlite DB。鎖檔存的是這支 shell 自己的 $$，不是 supervisor 或
# ygo-sniper 的 pid，所以殘鎖回收邏輯（上面 acquire_lock 那段）看不出這個孤兒。
# 這個洞被判定為可接受：範圍窄（要精準殺中 supervisor pid 而不動整組），
# 而且不是新問題——加 watchdog 之前，只要 shell 本身被 SIGKILL，鎖一樣會
# 被釋放、`ygo-sniper daily` 一樣會變孤兒，形狀相同。2am 除錯線索：如果
# log 裡看到兩段重疊的「===== 開始 =====」／「===== 結束 =====」區塊，
# 或同一時段 sqlite 出現寫入衝突／重複推播，先懷疑這個孤兒視窗，
# 去找有沒有系統層級的 OOM kill 或手動 kill -9 紀錄（`log show` / `dmesg`）。
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
    if [ -f .env ]; then
        # shellcheck disable=SC1091
        source .env
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "text=${MSG}" \
            > /dev/null
    fi
fi

echo "===== $(date '+%H:%M:%S') 結束 (exit=$STATUS) =====" >> "$LOG_FILE"

# 只留最近 14 天的 log
find "$LOG_DIR" -name 'daily-*.log' -mtime +14 -delete 2>/dev/null

exit $STATUS
