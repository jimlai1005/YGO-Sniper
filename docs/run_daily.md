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
- `finished_at`：本機時間、naive ISO（跟 `schedule_watch.py` 全域使用
  naive local datetime 的慣例一致——不要混用 UTC，否則就是又一次
  「兩個數字不同源」的錯，見 CLAUDE.md 第三節）。
- `notify_attempted`／`notify_http`／`notify_curl_exit`：這一輪失敗通知的
  curl 有沒有真的打出去、拿到的 HTTP 狀態碼、curl 自己的 exit code。

**成功也要覆寫**：不覆寫的話，一次 exit=124 的殘留紀錄會被下一輪誤讀成
「還沒被處理過」，永遠重複告警下去；每一輪不分成敗都寫，档案內容因此
永遠只反映「最近一次真正執行」的結果。

下一輪 `ygo-sniper daily` 起跑時（`Pipeline._fold_watchdog_ledger`）會讀
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
