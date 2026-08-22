# 高價帶掃描（High Band Scan）Implementation Plan

> **For agentic workers:** 逐 task 派工（builder/@inline）。主線程逐 task 親跑驗收指令後才派下一個。
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增一條獨立的「高價帶」掃描（¥8,624 動態下限 ～ ¥50,000 固定上限、只掛 buyee_mercari），
只在標的相對市場行情 ≤ 7 折且證據充分（L1/L2 同卡成交）時推播；與現有掃描完全隔離
（獨立 CLI 指令、獨立 launchd 排程、錯開時段），高價帶壞掉不影響既有低價帶。

**事故背景（為什麼做）：** 2026-08-22，`m11400671293`（PSA9 封印されしエクゾディア 初期
ウルトラ，¥22,222，約市價 6 折）從未進過系統——每條搜尋都帶 `price_max=8,624`
（`_price_ceiling_jpy` = 鑑定費反推 ×2.5），伺服器端過濾、本地零痕跡。
系統對 ¥8,624 以上的深折價卡整段全盲。

**Architecture:** 沿用 `_scan` 的 `watch_only` 前例——同一條管線加一面 `high_band` 旗，
候選判定／估價／落庫走同一份程式碼（工程原則 1：不能有兩套判準），band 專屬行為全部
鎖在旗子後面，旗子關閉時舊路徑 diff 為零。價格帶邊界**同源**：高價帶的下限與低價帶的
上限是同一個 `_price_ceiling_jpy()` 呼叫算出來的值，匯率怎麼漂都無縫無重疊。

**Tech Stack:** 既有：Python 3.12 / typer / sqlite / launchd / pytest。無新依賴。

**已實測的事實（2026-08-22，不必重驗）：**
- Buyee Mercari 搜尋 `price_min` 參數**生效且閉區間**：對照組 `price_max=8624` 74 筆全
  ≤¥7,999；實驗組 `price_min=15000&price_max=50000` 99 筆全部落在 [15000, 50000]。
- comps 已售出搜尋**不帶價格上限**（`pipeline.py:306` 的 `src.search(keyword, sold=True, pages=pages)`）：
  庫裡 3,014 筆 JPY 成交中 1,392 筆落在 (8624, 50000]——7 折 trigger 有真實比較基準。
- 高價帶第 1 頁存量近滿頁（99 筆）——第 1 頁時間深度要在上線後用 log 觀察，見 Task 7。

**使用者已定案：** 上限 ¥50,000；只掛 buyee_mercari；獨立模組＋排程錯開
（低價帶白天在每小時 :30、晚上 :00/:30——高價帶落在白天偶數整點 :00、晚上 :15，
完全不與既有觸發分鐘重疊）；推播門檻「7 折以下」。

---

## 全域紅線（每個 task 都要遵守）

1. **這不是過濾／解析規則改動**（不動 exclude_keywords、不動 regex、不動 parse_*），
   所以不需要 corpus-diff；但**任何 task 不得順手動到那些檔案**。
2. 比較兩個數字前先問同源同基準（CLAUDE.md 第三節）。本案兩個關鍵同源點：
   （a）帶邊界：高價帶 min ＝ 低價帶 max ＝ 同一個 `_price_ceiling_jpy()`；
   （b）7 折判定：**只准用訊號上既有的估價欄位**（scoring 已算好的 comps 中位與
   discount），不得為高價帶另起一套比價。
3. 靜默失敗是頭號敵人：`min_price` 傳給不支援的來源必須**大聲拋錯**，不准靜默丟棄
   （前科：category 在 `**kwargs` 被安靜丟掉一年）。
4. 派工回報不是證據——每個 task 完成後主線程親跑該 task 的驗收指令。

---

### Task 1（@inline）：`min_price` 縱貫線——search 介面到 Buyee URL 參數

**Files:**
- Modify: `src/ygo_sniper/pipeline.py`（`run_source_search`，約 :151-185）
- Modify: `src/ygo_sniper/sources/buyee.py`（`_search_params` 約 :114-137、`search`／`search_detailed` 約 :149-195）
- Test: `tests/test_source_pages.py`（或該檔既有慣例所在的鄰近測試檔）

**Steps:**

- [ ] 先讀 `sources/buyee.py:100-200` 與 `pipeline.py:120-200`，確認現行參數流。
- [ ] 紅燈測試 A：`BuyeeSource` 的搜尋 URL 建構函式收到 `min_price=15000` 時，
  產出的 params 含 `price_min=15000`；`min_price=None` 時 **完全沒有** `price_min` 鍵。
  （測試寫法比照該檔既有的 URL 參數測試，如分類參數那組。）
- [ ] 紅燈測試 B：`run_source_search` 對一個**沒有** `supports_min_price` 屬性（或為 False）
  的假來源傳 `min_price=1000` 時，回傳的 `SearchResult.health` 是 `PARSER_BROKEN`
  等級的大聲失敗或直接 `raise ValueError`——擇一，但**不准**靜默丟參數。
  （實作方式：`run_source_search` 簽名加 `min_price: float | None = None`；
  `min_price is not None and not getattr(src, "supports_min_price", False)` → 拋
  `ValueError(f"{source_name} 不支援 min_price——參數會被靜默丟棄，拒絕執行")`。）
- [ ] 實作：`BuyeeSource` 加類別屬性 `supports_min_price = True`，
  `search`／`search_detailed` 簽名加 `min_price: float | None = None`，
  傳入 `_search_params`，`if min_price: params["price_min"] = int(min_price)`
  （與既有 `price_max` 同一寫法、同一位置）。
- [ ] 跑 Task 1 的新測試＋`tests/test_source_pages.py` 全檔綠。

**驗收（主線程親跑）：**
```bash
.venv/bin/pytest tests/test_source_pages.py -v 2>&1 | tail -5
```
全綠，且能指出新測試的名字在輸出裡。

---

### Task 2（@inline）：watchlist `high_band:` 區塊與 loader

**Files:**
- Modify: `config/watchlist.yaml`（新增頂層 `high_band:` 區塊）
- Modify: `src/ygo_sniper/queries.py`（新增 `load_high_band_queries`）
- Test: `tests/test_queries_category.py`（鄰近既有 watchlist 測試）

**設定內容（照抄進 watchlist.yaml，含註解）：**
```yaml
# ---------------------------------------------------------------------------
# 高價帶掃描（2026-08-22）。事故驅動：m11400671293（PSA9 初期エクゾディア
# ¥22,222，約市價 6 折）被 price_max=8,624 在伺服器端擋掉，本地零痕跡。
# 這一帶的下限**不寫在這裡**——它動態等於低價帶的上限（_price_ceiling_jpy，
# 同一個函式同一次呼叫的值），匯率漂移時兩帶永遠無縫相接、不重疊。
# 只在 `ygo-sniper daily-high`／`scan-high` 跑（獨立排程，時段與低價帶錯開）；
# 推播另有嚴格閘門（≤7 折＋L1/L2 證據），見 notify_rules 規則 5。
# 關鍵字沿用低價帶 Mercari 實測覆蓋最好的四條；上線後照 recall 方法論
# 量淨增量再修剪，不憑印象加減。
high_band:
  max_price_jpy: 50000
  queries:
    - name: "高價帶 PSA 初期（分類）"
      keyword: "PSA 初期"
      category: yugioh
      sources: [buyee_mercari]
    - name: "高價帶 PSA 初期（無分類保險）"
      keyword: "遊戯王 PSA 初期"
      sources: [buyee_mercari]
    - name: "高價帶 PSA 1999"
      keyword: "PSA 1999"
      sources: [buyee_mercari]
    - name: "高價帶 ARS 初期（分類）"
      keyword: "ARS 初期"
      category: yugioh
      sources: [buyee_mercari]
```

**Loader 介面（queries.py）：**
```python
def load_high_band_queries(watchlist: dict[str, Any]) -> tuple[float | None, list[QuerySpec]]:
    """`watchlist['high_band']` → (max_price_jpy, QuerySpec 清單)。

    區塊不存在 → (None, [])——高價帶是選配，不裝就是沒有。
    max_price_jpy 缺失或非正數 → 印警告並回 (None, [])：沒有上限的高價帶
    等於整個市場，那不是這個功能的語意，寧可整段不跑並大聲說。
    查詢列的解析與 load_queries 同一套容錯（壞列印警告跳過）。
    """
```
（`QuerySpec` dataclass **不動**——band 資訊由 pipeline 旗子攜帶，不進 QuerySpec。）

**Steps:**

- [ ] 紅燈測試：三個 case——正常區塊解出 (50000.0, 4 條)；區塊缺失回 (None, [])；
  `max_price_jpy` 缺失回 (None, []) 且 capsys 撈得到警告字樣。
- [ ] 紅燈測試（預算，比照 `test_real_watchlist_stays_within_request_budget`）：
  實際設定檔的 high_band 查詢 ≤4 條、來源展開後每輪請求 ≤4——這是硬約束，
  加查詢前要先改測試（強迫重新論證預算）。
- [ ] 實作 loader＋寫入 watchlist.yaml。
- [ ] 跑新測試＋`tests/test_queries_category.py` 全檔綠。

**驗收（主線程親跑）：**
```bash
.venv/bin/pytest tests/test_queries_category.py -v 2>&1 | tail -5
.venv/bin/python -c "
from ygo_sniper.config import load_config
from ygo_sniper.queries import load_high_band_queries
cap, qs = load_high_band_queries(load_config().watchlist)
print(cap, [q.label for q in qs])"
```
印出 `50000.0` 與四條查詢的 label。

---

### Task 3（@inline）：pipeline 高價帶掃描路徑＋signals band 欄位

**Files:**
- Modify: `src/ygo_sniper/pipeline.py`（`_scan_source`、`_scan`、`scan` 入口）
- Modify: `src/ygo_sniper/store.py`（`_migrate_signals` 加 `band TEXT DEFAULT 'std'`；
  signal upsert 寫入 band）
- Test: `tests/` 內 pipeline/store 既有測試檔的鄰近位置

**設計（builder 照此實作，發現 plan 沒涵蓋的決策就停下回報）：**

1. `_scan_source` 加參數 `price_band: tuple[float, float] | None = None`。
   `None` ＝ 現行為（`price_ceiling` 旗照舊）；有值 ＝ `(min_price, max_price)`
   直接下傳 `run_source_search(..., min_price=..., max_price=...)`，
   **min 的值必須來自同一次 `_price_ceiling_jpy(src.site)` 呼叫**（同源條款）。
2. `_scan` 加旗 `high_band: bool = False`（比照 `watch_only` 前例）。`high_band=True` 時：
   - 查詢改用 `load_high_band_queries`；`(None, [])` 時印「高價帶未設定」直接空手而回。
   - 每條查詢的 `price_band = (self._price_ceiling_jpy(site), high_cap)`。
   - `skip_comps` 語意同 `watch_only`（不 refresh，只 `load_from_store`——7 折判定要用行情）。
   - 不跑賣家輪替監控、不跑 canary、不跑 refill（與 `watch_only` 同款排除，理由相同：
     那些是低價帶完整輪的事，高價帶輪要維持零額外請求預算）。
   - 狙擊比對**照跑**（`_collect_candidates` 內建掛鉤，天然生效——高價帶正是
     ¥8,624 以上狙擊卡唯一的發現管道，這是本功能的隱藏紅利）。
   - obs 批次**不得建立離場地平線**：高價帶與低價帶共用關鍵字（如「PSA 初期」），
     若兩帶的批次都以 (source, keyword) 建地平線，低價帶商品缺席於高價帶批次會被
     誤讀成離場（第三節第八事故的形狀）。作法比照賣家頁監控的 `exit_scope=False`
     （見 `store.record_listing_scan` docstring）。
3. signals 落庫：`_migrate_signals` 補 `band TEXT DEFAULT 'std'`；upsert 時
   高價帶輪寫 `band='high'`、一般輪寫 `'std'`（後見覆蓋前見：邊界隨匯率漂移時，
   最後一次看到它的帶決定它適用哪組推播規則）。
4. `Pipeline.scan` 公開入口加 `high_band: bool = False` 透傳。

**Steps:**

- [ ] 紅燈測試 1（同源）：monkeypatch 一個記錄參數的假 buyee 來源＋假
  `_price_ceiling_jpy` 回 8624，跑 `scan(high_band=True, dry_run=True)`，
  斷言收到 `min_price == 8624` 且 `max_price == 50000`，**且** min 與低價帶輪
  的 max 是同一個函式的回傳值（測試裡兩帶各跑一次、比對同值）。
- [ ] 紅燈測試 2（隔離）：`scan(high_band=True)` 不觸發 comps refresh、canary、
  賣家監控（以呼叫記錄斷言）。
- [ ] 紅燈測試 3（band 欄位）：高價帶輪落庫的 signal `band='high'`；舊庫遷移後
  既有列 `band='std'`。
- [ ] 紅燈測試 4（地平線）：高價帶批次不產生 `window_exit_at`／`disappeared_at`
  的候選判定（比照 `tests/test_venue_study.py` 既有離場測試的寫法）。
- [ ] 實作，逐測試轉綠。
- [ ] `make test` 全綠（低價帶行為零變化的總閘門）。

**驗收（主線程親跑）：**
```bash
make test 2>&1 | tail -3
sqlite3 data/sniper.db "PRAGMA table_info(signals);" | grep band
```

---

### Task 4（@inline）：推播規則 5——高價帶折價

**Files:**
- Modify: `src/ygo_sniper/notify_rules.py`
- Modify: `config/settings.yaml`（`notify: rules:` 加 `high_band_max_price_ratio`）
- Test: `tests/` 內 notify_rules 既有測試檔

**設計：**

1. 常數（比照既有規則的寫法與註解密度）：
   ```python
   RULE_HIGH_BAND = "high_band_discount"   # 進 notify_log.rule，改字串＝清空去重帳
   DEFAULT_HIGH_BAND_MAX_PRICE_RATIO = 0.70  # ≤7 折才推（使用者 2026-08-22 定案）
   ```
   `RULE_LABEL` 加 `規則 5 高價帶折價`。
2. band 閘門：`band='high'` 的 signal **只有**規則 4（狙擊）與規則 5 有資格評估；
   規則 1/2/3 對 high band 一律跳過（Mercari 定價商品規則 1 天然不適用，但閘門要
   顯式寫——未來高價帶掛 Yahoo 時不能靠「剛好不適用」）。`band` 缺失或 `'std'`
   → 行為與現行完全相同（舊測試零改動就該全綠）。
3. 規則 5 判準（**只讀 signal 上既有的估價欄位**，不得另算）：
   - 估價等級為 L1/L2（同卡成交池）——欄位在哪裡讀，builder 從規則 2 的現行
     實作找同一來源；找不到明確等級欄位就停下回報，不要用 `comps_n` 湊
     （第七節：樣本數不是證據強度）。
   - `price / comps_median ≤ high_band_max_price_ratio`（用與規則 3 同基準的
     價格欄位；比率與 `discount_pct` 若同時存在，以既有欄位定義為準）。
   - 任一條件不滿足 → 完全靜默（dashboard 仍看得到，這是通知閘門不是過濾）。
   - 訊息文案要標「判定來源：同卡成交 × N 筆中位」＋帶價格帶徽章（比照規則 3
     把 SOURCE_PEER 走上訊息的作法）。
4. `NotifyRules.from_config` 讀新門檻（打錯字印警告不靜默，沿用現行機制）。

**Steps:**

- [ ] 紅燈測試組：
  (a) high band＋L1＋ratio 0.65 → 規則 5 命中；
  (b) high band＋L1＋ratio 0.75 → 不推；
  (c) high band＋L3 → 不推（不管 ratio 多低）；
  (d) high band 的 signal 餵給規則 1/2/3 的評估 → 全部跳過；
  (e) `band='std'` 的 signal → 規則 1/2/3 行為與現行測試完全一致（不新增斷言，
  以既有測試全綠為證）；
  (f) 狙擊命中的 high band signal → 規則 4 照推。
- [ ] 實作，逐測試轉綠；`settings.yaml` 加註解說明 0.70 的出處（使用者定案）。
- [ ] notify_rules 相關測試檔全綠。

**驗收（主線程親跑）：**
```bash
.venv/bin/pytest tests/ -k "notify" -v 2>&1 | tail -5
```

---

### Task 5（@inline）：CLI `daily-high`／`scan-high`

**Files:**
- Modify: `src/ygo_sniper/cli.py`
- Test: 既有 CLI 測試檔（若有；沒有就以 Task 6 的實跑驗收為準）

**設計（比照 `daily`／`scan` 現行寫法，cli.py:46-64、290-297）：**
```python
@app.command()
def daily_high(no_notify: bool = typer.Option(False, help="只掃不推播")):
    """高價帶那一鍵：掃 ¥8,624～50,000 帶 → 只推 ≤7 折＋狙擊命中。獨立排程跑這個。"""
    pipe = Pipeline()
    try:
        result = pipe.scan(high_band=True)
        _print_scan(result)
        if not no_notify:
            _run_notifications(pipe, result)
    finally:
        pipe.close()

@app.command()
def scan_high(dry_run: bool = typer.Option(False, help="不寫入資料庫")):
    """高價帶只掃不推播。"""
    pipe = Pipeline()
    try:
        _print_scan(pipe.scan(high_band=True, dry_run=dry_run))
    finally:
        pipe.close()
```
（`daily_high` 不跑 `_mine_snipes_daily`——狙擊挖掘一天一次由低價帶 `daily` 負責，
高價帶重複跑只是加倍請求。`_run_notifications`／`_print_scan` 若對 result dict 的
欄位有假設，builder 要確認高價帶輪的 result 形狀相容，不相容就停下回報。）

**Steps:**

- [ ] 實作兩個指令。
- [ ] `ygo-sniper --help` 出現 `daily-high` 與 `scan-high`。

**驗收（主線程親跑）：**
```bash
.venv/bin/ygo-sniper --help 2>&1 | grep -E "daily-high|scan-high"
.venv/bin/ygo-sniper scan-high --dry-run 2>&1 | tail -20
```
第二條要在 `[req]` log 裡看到 `price_min=8624`（或當日動態值）與 `price_max=50000`
同時掛在 Buyee 搜尋 URL 上，且解析出的標的價格全部落在帶內。

---

### Task 6（@inline）：獨立排程——plist、run_high.sh、排程監督

**Files:**
- Create: `scripts/com.jim.ygosniper.high.plist`（label `com.jim.ygosniper.high`）
- Create: `scripts/run_high.sh`
- Modify: `Makefile`（`schedule-high`／`unschedule-high`，比照 `schedule-dashboard` 一組）
- Modify: `src/ygo_sniper/schedule_watch.py`（高價帶自己的網格）
- Test: `tests/test_schedule_watch.py`

**排程時刻（與低價帶所有觸發分鐘零重疊——低價帶佔白天 :30 與晚上 :00/:30）：**
```
10:00  12:00  14:00  16:00   （白天偶數整點）
18:15  20:15  22:15          （晚間 :15，避開低價帶每 30 分的 :00/:30）
```
共 7 次/日 × 4 查詢 × 1 來源 ＝ 28 請求/日。plist 用 `StartCalendarInterval` 陣列
逐時刻列出（比照既有 `com.jim.ygosniper.plist` 的寫法）。

**run_high.sh：** 以 `run_daily.sh` 為模板改四處，其餘結構（鎖、網路等待、watchdog、
失敗通知、帳本覆寫）**逐段保留**：
- `LOCK_DIR=data/run_high.lock`（獨立鎖——兩帶允許並行，互不搶）
- `LOG_FILE=data/logs/high-$(date +%Y%m%d).log`（log 輪替 find pattern 同步改 `high-*.log`）
- `LAST_RUN_FILE=data/last_run_exit_high`
- 執行 `ygo-sniper daily-high`；失敗通知文案帶「高價帶」字樣以便區分

**排程監督：** `schedule_watch.py` 加 `_HIGH_WINDOWS`（上表 7 個時刻的表示法比照
`_WINDOWS` 的網格語意；不規則時刻用「顯式時刻清單」表示，builder 看現行 `_ALL_SLOTS`
的展開邏輯決定最小改法——能參數化就參數化，不能就平行一份，**不准動到現行
`_WINDOWS` 的值與行為**）。`daily-high` 輪起跑時用自己的網格＋自己的基準鍵＋自己的
pending 帳本鍵做空窗比對（鍵名與低價帶的分開，兩本帳互不污染——兩帳語意不同，
永遠不要合併）。告警照既有「送達才消耗」的規則。

**Steps:**

- [ ] 紅燈測試 1：`test_high_windows_match_high_plist`——比照
  `test_windows_match_plist`，把新 plist 的觸發時刻與 `_HIGH_WINDOWS` 釘死。
- [ ] 紅燈測試 2：高價帶網格的漏格偵測——漂移後漏格必須出聲（比照既有
  `test_schedule_watch.py` 的漂移情境測試，換上高價帶網格）。
- [ ] 紅燈測試 3：兩帶的基準鍵／pending 鍵互不相同（防止一帶的告警消耗掉另一帶的）。
- [ ] 實作 plist＋run_high.sh＋Makefile targets＋schedule_watch 擴充。
- [ ] `bash -n scripts/run_high.sh` 語法檢查過；`plutil -lint scripts/com.jim.ygosniper.high.plist` 過。
- [ ] `tests/test_schedule_watch.py` 全檔綠。

**驗收（主線程親跑）：**
```bash
.venv/bin/pytest tests/test_schedule_watch.py -v 2>&1 | tail -5
plutil -lint scripts/com.jim.ygosniper.high.plist && bash -n scripts/run_high.sh && echo OK
```
（**不執行** `make schedule-high`——裝排程是使用者的決定，交付時提示指令即可。）

---

### Task 7（@inline）：文件＋端到端驗收

**Files:**
- Modify: `CLAUDE.md`（第九節指令表加 `daily-high`／`scan-high`／`make schedule-high`；
  排程時段段落補高價帶時刻；第五節「已復活兩本帳」表**不動**）
- Modify: `config/settings.yaml` 註解（若 Task 4 尚未寫齊）

**Steps:**

- [ ] 文件更新：指令、排程時刻、7 折門檻與其設定鍵、「高價帶第 1 頁時間深度」的
  觀察提醒（上線一週後拿 `data/logs/high-*.log` 的 parsed_count 對照掃描間隔，
  滿頁率高就要加頁數或加密時段——這是預註冊的觀察項，不是憑感覺調）。
- [ ] `make test` 全綠（全案總閘門）。
- [ ] 端到端：`ygo-sniper scan-high --dry-run` 完整跑一輪，確認：
  (a) `[req]` URL 帶 `price_min`＋`price_max`；(b) 有解析出帶內標的；
  (c) 無任何 exception 路徑輸出。

**驗收（主線程親跑）：**
```bash
make test 2>&1 | tail -3
.venv/bin/ygo-sniper scan-high --dry-run 2>&1 | grep -E "\[req\]|parsed|候選" | head -10
```

---

## Self-Review 紀錄

- 規格覆蓋：上限 ¥50,000（T2）、只掛 Mercari（T2）、獨立模組（T3/T5 旗＋獨立指令）、
  錯開排程（T6）、7 折推播（T4）、事故卡形狀能被撈到（「PSA 初期」＋分類命中原標題，
  ¥22,222 ∈ (8624, 50000]）——全數有對應 task。
- 同源檢查：帶邊界（T3 測試 1 顯式斷言）、7 折比價（T4 只讀既有估價欄位）。
- 型別一致：`load_high_band_queries` 回傳 tuple 在 T2 定義、T3 引用一致；
  `RULE_HIGH_BAND` 字串 T4 單一定義。
- 已知未做（刻意）：Yahoo/PayPay 高價帶（等 Mercari 淨增量數據）、dashboard band
  篩選 UI、高價帶專屬 canary（低價帶 canary 已覆蓋管道健康）。
