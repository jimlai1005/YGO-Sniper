# run_daily.sh 技術筆記

`scripts/run_daily.sh` 是排程的實際入口（launchd 跑這個，你手動也可以跑）。
腳本本身只留「這裡在做什麼」的短註解；這份文件收留「為什麼」的長篇分析
——2026-08-12 從腳本裡搬出來的，原因是那支腳本當時已經長到「一堆註解夾
兩行程式碼」，繼續往裡面加分析只會讓真正的邏輯更難掃過去。

改動 `run_daily.sh` 的行為時，如果影響到下面任何一節描述的東西，
請一併更新本檔——過期的技術筆記比沒有筆記更容易誤導人。

---

## 一、為什麼用 `.venv/bin/python` 而不是裸 `python`

`watchdog` 那一段（見下一節）呼叫的是 `.venv/bin/python scripts/run_with_timeout.py`，
不是裸的 `python`。

上面已經 `source .venv/bin/activate`，裸 `python` 理論上也會落在 venv 的
bin 裡，但那樣就是在依賴「activate 這步真的成功把 PATH 排到前面」——
多一層隱含順序。改成 `.venv/bin/python` 直接指名，不繞 PATH 解析；而且
腳本前面的 `.venv/bin/activate` 存在檢查已經保證了 `.venv/bin/python`
必定存在，不是新增依賴，只是把既有保證講得更明白。

stock macOS 沒有 `python`，只有 `python3`；activate 失敗時裸 `python` 會是
exit 127，雖然照樣會走進失敗分支大聲告警，不是靜默失敗，但沒必要讓失敗
多繞一層猜測。

## 二、watchdog 的孤兒視窗（已知、可接受的殘留風險）

`run_with_timeout.py` 當 supervisor：醒著卡死超過門檻（`YGO_CYCLE_TIMEOUT`，
預設 1500 秒）就強制終止整個 process group，exit=124。這防的是
Playwright／chromium 卡死導致排程永遠停擺。

**新孤兒視窗**（只記錄，不重新設計鎖）：如果有東西只殺掉了
`run_with_timeout.py` 這個 supervisor 行程本身、而不是它的 process group
（例如 OOM killer 挑上了 supervisor 的 pid，或有人手動 `kill -9` 這個
pid），`run_daily.sh` 裡的 shell `wait` 會直接拿到回傳、往下走、觸發 EXIT
trap 把鎖釋放掉——但孫行程 `ygo-sniper daily` 的 process group 沒人管了，
變成孤兒，而且從此沒有任何 timeout 在管它。下一輪排程一看鎖已經沒了，
就會開始跑第二個 `ygo-sniper daily`，兩個行程同時打同一批賣場、同時寫
同一個 sqlite DB。

鎖檔存的是這支 shell 自己的 `$$`，不是 supervisor 或 `ygo-sniper` 的
pid，所以殘鎖回收邏輯（`acquire_lock`）看不出這個孤兒。

**這個洞被判定為可接受**：範圍窄（要精準殺中 supervisor pid 而不動整組），
而且不是新問題——加 watchdog 之前，只要 shell 本身被 `SIGKILL`，鎖一樣會
被釋放、`ygo-sniper daily` 一樣會變孤兒，形狀相同。

**2am 除錯線索**：如果 log 裡看到兩段重疊的「===== 開始 =====」／
「===== 結束 =====」區塊，或同一時段 sqlite 出現寫入衝突／重複推播，
先懷疑這個孤兒視窗，去找有沒有系統層級的 OOM kill 或手動 `kill -9` 紀錄
（`log show` / `dmesg`）。

## 三、排程監督帳本（`data/last_run_exit`）——2026-08-12 加，事故背景

**觸發事故**：2026-08-10 有一輪卡了 8.5 小時（23:00:44 開始，隔天 07:33:16
才印出「結束」）。事後追查發現卡點其實在 `pipe.close()`／Python 直譯器
結束（Playwright 行程洩漏），**不是**在 `pipe.scan()` 中途——`ygo-sniper
daily` 早就把完整輸出（comps、掃描摘要、推播、告警）都印完，代表
`schedule_watch.RUN_FINISHED_KEY` 也已經正常寫下。下一輪的排程空窗偵測
（`schedule_watch.schedule_health`，見 `src/ygo_sniper/schedule_watch.py`
模組開頭的 Fix 1／Fix B 記錄）因此完全看不出異狀：正常收尾、準時接棒。

唯一還留著證據的地方是 watchdog：卡死超過 `YGO_CYCLE_TIMEOUT` 秒會被
強制殺掉、exit=124。但原本那則 124 專屬的失敗通知是
`curl -s ... > /dev/null`——結果直接丟掉。如果那顆 curl 本身送不出去
（筆電剛醒、Wi-Fi 還沒穩，正是上面「MBP 喚醒後等網路」那個迴圈要處理的
情境），整個事故就會 100% 無聲：唯一的告警管道自己先啞掉了。

**修法**：`run_daily.sh` 每一輪結束都（不論成功失敗）把結果寫進
`data/last_run_exit`，格式：

```json
{"exit": 124, "finished_at": "2026-08-10T23:35:00", "notify_attempted": true, "notify_http": "000", "notify_curl_exit": 6}
```

- `exit`：這一輪 `ygo-sniper daily` 的結束碼（0 = 成功；124 = watchdog
  強制終止；其他 = 一般失敗）。
- `finished_at`：台北時間、naive ISO（wrapper 開頭 `export TZ=Asia/Taipei`，與
  `clock.schedule_now()` 同基準——不要混用 UTC 或機器時區，否則就是又一次
  「兩個數字不同源」的錯，見 CLAUDE.md 第三節第九項）。
- `notify_attempted`／`notify_http`／`notify_curl_exit`：這一輪失敗通知的
  curl 有沒有真的打出去、拿到的 HTTP 狀態碼、curl 自己的 exit code。

**成功也要覆寫**：不覆寫的話，一次 exit=124 的殘留紀錄會被下一輪誤讀成
「還沒被處理過」，永遠重複告警下去；每一輪不分成敗都寫，档案內容因此
永遠只反映「最近一次真正執行」的結果。

下一輪 `ygo-sniper daily` 起跑時（`Pipeline._read_watchdog_ledger`，讀完
確認 pending 已落帳才由 `_consume_watchdog_ledger` 刪檔）會讀
這個檔案、丟給純函式 `schedule_watch.watchdog_message` 判斷要不要出聲
——只有「上一輪真的失敗，而且 `run_daily.sh` 自己那則失敗通知也沒確認
送達」才折進排程監督的 `PENDING_ALERT_KEY` 帳本，跟排程空窗共用同一套
「只有 Telegram 真的送達才消耗」的保障（`schedule_watch.resolve_alert`）。
折進去之後這個檔案會被刪掉（「讀了就算數」），避免同一次失敗被下一輪、
下下輪重複折算。

## 四、失敗通知現在會記錄送達與否

過去失敗通知的 curl 呼叫是 `> /dev/null`，成功失敗都不看。現在
`notify_failure()`（腳本內的 shell function）會量測 HTTP 狀態碼與 curl
自己的 exit code，寫一行到當天的 log：送達了記「已送達（http=200）」，
沒送達記「本身也沒送成功（curl exit=… http=…）」。這兩個數字也會存進
`data/last_run_exit`，供第三節的排程監督帳本讀。

## 五、參數透傳與 dashboard 的補跑按鈕（2026-10-20）

**參數透傳**：腳本最後一行是 `ygo-sniper daily "$@"`——額外參數原樣交給 `daily`。launchd 的
plist 不帶參數，行為不變；手動補跑用 `scripts/run_daily.sh --catchup`。這樣補跑走的是與排程
完全相同的路徑：鎖、watchdog（`YGO_CYCLE_TIMEOUT`）、log、`data/last_run_exit` 全部免費繼承，
不需要另外一套「補跑專用」的執行環境。補跑模式本身（多翻頁、強制重挖、回顧訊息）的邏輯
在 `ygo-sniper daily --catchup`，說明見 CLAUDE.md 第五節「漏跑視窗帳與補跑模式」。

**dashboard 的「補跑 daily」按鈕走這支腳本**：`POST /api/daily-run` 用 `subprocess` 起
`/bin/bash scripts/run_daily.sh --catchup`（`start_new_session=True`，不隨 web 行程結束）。
起之前先做兩個**唯讀預檢**——`scan_status` 是否已有一輪在跑、`data/run_daily.lock/pid` 的持有者
是否還活著——任一成立就回「已經有一輪在跑」而不啟動。理由：腳本遇到鎖會直接跳過，如果 web
不先檢查，使用者會看到「已啟動」然後什麼都沒發生（靜默失敗）。**預檢不清鎖**，清殘鎖是腳本
自己的事；鎖的權威仍是腳本。腳本啟動後要先取鎖、再過網路 ping（最多 60 秒）、起 python 才會寫
`scan_status`，所以按鈕有 90 秒寬限期，見 `docs/dashboard.md`。

## 六、排程時鐘：釘台北，launchd 不持有網格（2026-10-21）

**為什麼**：機器時區會跟著使用者出差換（2026-10 Dallas → LA → 台北）。launchd 的
`StartCalendarInterval` 用 job *載入時*的時區解讀，Python `datetime.now()` 用*現在的*系統時區，
兩者差 13 小時時 `schedule_health` 連續 12 天每天假報漏跑（CLAUDE.md 第三節第九項）。

**三層**：
1. `src/ygo_sniper/clock.py`：`schedule_now()` 回台北 naive datetime，排程監督／視窗帳／補跑回顧
   全走它；與 UTC aware 值比較經 `schedule_naive_to_utc()`。
2. plist 只寫 `Minute` 0/15/30/45（省略 `Hour`＝每小時），**每 15 分鐘叫醒**；網格只在
   `schedule_watch._WINDOWS`（低價帶）／`_HIGH_WINDOWS`（高價帶）。`run_daily.sh`／`run_high.sh`
   在取鎖之前先跑 `ygo-sniper slot-check --band std|high`。
3. meta `schedule_clock_tz`：不是 `Asia/Taipei`（含不存在）時，第一輪不做漏跑偵測、不寫視窗帳，
   只寫新基準，避免拿舊時區寫的 `prev_started` 算出假漏格。

**`slot-check` 的 exit code**：`0` = 該掃（台北現在落在網格某一格的 `[slot, slot+7 分)`，
或上一輪之後有格被跳過，如筆電睡醒只補觸發一次）；`3` = 不在格上也沒漏格，**靜默結束**
（不寫 log、不碰鎖、不碰帳本，一天 96 次裡低價帶有 81 次走這條）。只讀 meta、不印任何字。
其他 exit code（例如 db 壞掉）wrapper **放行往下跑**，讓既有失敗路徑大聲報錯，不在閘門吞掉。

**`YGO_SLOT_GATE=1` 只由 plist 設定**：wrapper 的閘門寫成
`if [ "${YGO_SLOT_GATE:-0}" = "1" ] ...`。第一版閘門是無條件的，結果 dashboard 的「補跑 daily」
（第五節，`run_daily.sh --catchup`）與手動執行只要不在網格上就被靜默吞掉，dashboard 還回
「已啟動」——靜默失敗。現在只有 launchd 觸發（plist `EnvironmentVariables` 有這個變數）才過閘門；
補跑按鈕與手動執行永遠直接掃。**不要把它放進 shell profile 或 wrapper。**

**部署步驟**（改 plist 或第一次套用本機制時）：
```bash
make unschedule && make schedule && make unschedule-high && make schedule-high   # 載入新 plist
launchctl kickstart -k gui/$(id -u)/com.jim.ygosniper.dashboard                  # dashboard 吃到 clock.py
launchctl print gui/$(id -u)/com.jim.ygosniper | grep "last exit"                # 應為 0
.venv/bin/ygo-sniper slot-check --band std; echo $?                              # 只印 0 或 3
```
改**時段**（網格）只改 `schedule_watch._WINDOWS`／`_HIGH_WINDOWS`，不必重載 plist。
驗證：`grep -h "開始 =====" data/logs/daily-<日期>.log` 的時刻（台北）應全部落在網格 ±7 分內；
第一輪真正掃描應印 `[schedule] 排程時鐘基準 None → Asia/Taipei，本輪不做漏跑偵測` 且無排程監督告警。
