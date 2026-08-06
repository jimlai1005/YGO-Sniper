# 清除已離場標的（Clear Expired Listings）設計

**日期**：2026-08-06
**分支**：`feat/clear-expired-listings`
**狀態**：設計已確認，待實作

---

## 1. 問題

使用者回報：dashboard 的「觀察中」分頁底下常出現已售出或競標結束的卡牌，希望能清除。

### 根因（三個獨立問題疊在一起）

1. **已結標自動移出的邏輯存在，但只在競標檢視生效**
   `web/static/index.html:1235-1257` 有 `const ended = shown.filter(...)` 會把 `end_time` 已過的移出並顯示「已結標 N 筆已移出」，但整段包在 `if (auctionView)` 裡（`index.html:1224`：`filterMode === "auction" || quick.ceiling || quick.room`）。切到觀察中分頁時 `auctionView` 為 false，過期項目照常列出。

2. **觀察中分頁的卡片不顯示任何時效資訊**
   `card()`（`index.html:827`）不顯示 `end_time` 也不顯示 `last_seen`——使用者連「這筆已經結標了」都看不出來。競標卡片 `auctionCard()`（`index.html:1370`）才有倒數與 `.cd.ended` 刪除線樣式。

3. **自動清理機制刻意不碰人工狀態**
   `expire_stale_signals`（`store.py:1600`）與 `purge_signals`（`store.py:1631`）都硬性只動 `state='new'`，理由寫在 `store.py:1607-1609` / `store.py:1639-1641`：人工決策程式不准覆蓋。`watching` 是人工標的，所以自動清理永遠碰不到它。**這條紅線本設計不打破。**

---

## 2. 實測數據

**量測環境**：`data/sniper.db`，2026-08-05 19:36 UTC（台灣時間 2026-08-06 03:36），唯讀查詢。
量測方式：`sqlite3 "file:data/sniper.db?mode=ro"`，`signals` LEFT JOIN `listing_obs` ON `key`。

### 2.1 觀察中的實際輪廓

| 項目 | 筆數 |
|---|---|
| watching 總數 | 81 |
| 有 `end_time`（競標） | 32 |
| **`end_time` 已過（已結標）** | **0** |
| 無 `end_time`（一口價） | 49 |
| **`listing_obs.disappeared_at` 非空（疑似已離場）** | **47（58%）** |
| `window_exit_at` 非空（只是被擠出觀測窗，無結論） | 3 |
| 仍在架上 | 31 |

**關鍵發現**：使用者說的「競標結束」在資料上**不是**以 `end_time` 過期呈現的。已結標的商品直接從 buyee 搜尋結果消失，所以它表現為 `disappeared_at`。全庫 92 筆 `end_time` 已過的 signal **全部是 `skipped` 狀態**，沒有一筆在 watching。

因此本功能的主力判準是 `disappeared_at`，`end_time` 只是輔助（但仍要做，它是唯一的確定事實）。

### 2.2 `disappeared_at` 的可靠度（本設計最重要的數字）

`listing_obs` 全表 598 筆，其中 148 筆 `revived_count > 0`（24.75%），最大復活次數 7。

**曾被判離場的 262 筆（`disappeared_at IS NOT NULL OR revived_count > 0`）中，148 筆後來又出現 = 56.5% 誤判率。**

依來源拆解：

| 來源 | 曾判離場 | 復活過 | 復活率 |
|---|---|---|---|
| ebay | 113 | 75 | 66.4% |
| buyee_paypay | 81 | 57 | 70.4% |
| buyee_yahoo | 67 | 16 | 23.9% |
| buyee_mercari | 1 | 0 | 0.0%（樣本不足） |

觀察中那 47 筆的來源分佈：**buyee_paypay 32 筆（17 筆曾復活）**、buyee_yahoo 8 筆（0 筆）、ebay 6 筆（3 筆）。
**最需要清的來源，判定最不可靠。**

依消失時長看（目前仍標記 disappeared 的全表）：

| 消失時長 | 筆數 | 曾復活 | 復活率 |
|---|---|---|---|
| <1 天 | 132 | 71 | 53.8% |
| 1-3 天 | 83 | 36 | 43.4% |
| 3-7 天 | 11 | 5 | 45.5% |

**復活率不隨時間顯著下降**——「等久一點再清」不是有效的降險手段。這排除了「消失超過 N 天才清」的設計。

### 2.3 一個被排除的守門條件（記錄以防日後重犯）

`seen_count` 看起來是完美的判準：`seen_count = 1` 的 61 筆復活率 **0.0%**。

**這是假的。** 復活的定義就是「又被看到」，會使 `seen_count` +1，所以 `seen_count = 1` 在定義上不可能有復活紀錄。這兩個欄位機械耦合，`seen_count` 不能拿來驗證 `revived_count`，也不能當守門條件。

（此即 CLAUDE.md 第三節的變體：比較的兩個值必須獨立，機械耦合的欄位互相驗證等於自證。）

---

## 3. 設計決策

| 決策 | 選擇 | 理由 |
|---|---|---|
| 清除語意 | 改 `state='expired'`，資料留著 | 誤殺可回溯；「已過期」分頁已存在（`index.html:374`） |
| 判定範圍 | 競標結標與疑似離場**一起清** | 使用者決定。合併只發生在**操作層**，判定層兩者分開計算 |
| 觸發方式 | 手動按鈕 + 確認對話框 | 不塞進排程，保住「程式不覆蓋人工狀態」的紅線 |
| 誤殺處理 | **自動還原** | 56.5% 誤判率下，靠人工發現誤殺等於沒有防線 |
| 來源差異 | 不分批清除，但徽章標信心度 | 使用者要一顆按鈕；資訊仍要給到 |
| 適用分頁 | 觀察中、已詢問、已出價 | 使用者決定（實測 watching 81、offer_sent 16、asked_seller 1） |

### 3.1 「兩者一起清」為什麼不違反 CLAUDE.md 第三節

第三節禁止的是**把不同基準的數字合成一個值**。本設計：

- 判定層：`ended`（確定事實）與 `gone`（推論）是 `ExpiryStatus.kind` 的兩個不同值，各自帶自己的信心度，**永不合成布林值**
- 顯示層：兩種徽章文案不同
- 操作層：清除按鈕同時作用於兩者——這是**使用者的動作選擇**，不是數值比較

### 3.2 已出價（`offer_sent`）的語意差異

已出價的標的消失，**很可能代表使用者標下了**，正確歸宿是 `bought` 而不是 `expired`。

處理：
- 徽章文案改為「已離場 · 確認是否標下？」（不是「疑似已售出」）
- 確認對話框對 `offer_sent` 額外顯示一行提醒
- 清除動作本身照常執行（使用者的決定），但 `cleared_from='offer_sent'` 保留了還原路徑

---

## 4. 實作規格

### 4.1 判定層：新檔 `src/ygo_sniper/expiry.py`

純函式模組，無 IO，是「這筆是否已離場」的**唯一真相來源**。前端不自己算。

```python
@dataclass(frozen=True)
class ExpiryStatus:
    kind: Literal["ended", "gone", "live"]
    confidence: Literal["certain", "medium", "low"]
    detail: str          # 給使用者看的文案，例：「疑似已售出 · 消失 6 小時」
    note: str | None     # 信心度的理由，例：「paypay 復活率 70%（n=81）」
```

判定順序（**確定事實壓過推論**）：

1. `end_time` 存在且 `<= now` → `kind="ended"`, `confidence="certain"`
2. `disappeared_at` 非空 → `kind="gone"`, `confidence` 依來源查表
3. 其餘 → `kind="live"`, `confidence="certain"`, `detail=""`, `note=None`
   （`live` 的 `confidence` 不帶語意，固定填 `certain` 只是為了型別完整；UI 對 `live` 不顯示任何徽章）

`window_exit_at` **不觸發任何過期判定**——它的語意是「只是被擠出第 1 頁」，本來就無結論（`store.py:1004-1008`）。

時間比較一律用 UTC aware datetime，與 `end_time` 的儲存格式（`2026-08-08T13:04:49+00:00`）同基準。**naive 輸入一律當作 UTC**（實測庫內所有 `end_time` 與 `disappeared_at` 都帶 `+00:00`，naive 只可能來自測試或未來的新來源；當成本地時間會產生 8 小時的靜默偏移）。

### 4.2 來源信心度：寫進 `config/settings.yaml`

```yaml
scan:
  # 「疑似已離場」（listing_obs.disappeared_at）的可信度，依來源分級。
  # ⚠️ 這些是**實測值**，不是猜的，而且會隨爬蟲改進漂移——
  # 要調之前先跑 `ygo-sniper revive-rate` 重新量，不要憑印象改。
  #
  # 量測定義：分母 = 曾被判離場的列（disappeared_at 非空 OR revived_count > 0），
  # 分子 = 其中 revived_count > 0 的列。2026-08-06 實測（data/sniper.db，598 列）：
  #   ebay 66.4% (n=113) / buyee_paypay 70.4% (n=81)
  #   buyee_yahoo 23.9% (n=67) / buyee_mercari 0% (n=1，樣本不足)
  #
  # 分級規則：復活率 < 35% 且 n >= 20 → medium；其餘一律 low。
  # 沒有任何來源夠格拿 "certain"——那個等級只給 end_time 已過。
  gone_confidence:
    buyee_yahoo: medium
    buyee_paypay: low
    ebay: low
    buyee_mercari: low      # n=1，樣本不足，保守給 low
    _default: low           # 未列舉的新來源一律 low
```

新增 CLI `ygo-sniper revive-rate`：重跑上述量測並印出表格（含 n），讓門檻可以被重新校準而不是憑記憶。

### 4.3 資料層

**Migration**（照 `store.py:405` 的 `_migrate_signals` 既有模式，`PRAGMA table_info` 逐欄補齊）：

`signals` 新增三欄：

| 欄位 | 型別 | 用途 |
|---|---|---|
| `cleared_at` | TEXT | 被清除的時間戳 |
| `cleared_from` | TEXT | 清除前的狀態（`watching` / `asked_seller` / `offer_sent`） |
| `restored_count` | INTEGER DEFAULT 0 | 自動還原次數 |

**`list_signals` 加 JOIN**（`store.py:557`）：

```sql
SELECT s.*,
       o.disappeared_at AS obs_disappeared_at,
       o.window_exit_at AS obs_window_exit_at,
       o.revived_count  AS obs_revived_count,
       o.site           AS obs_site
FROM signals s LEFT JOIN listing_obs o ON o.key = s.key
WHERE s.score >= ? ...
```

`s.*` 必須保留——`store.py:551` 的註解說明 `SELECT *` 是刻意的（新欄位自動帶上，避免手寫清單漂移）。額外欄位一律用 `obs_` 前綴，因為兩表都有 `last_seen` / `first_seen` / `title` / `url` / `price_twd` 等同名欄位，不加前綴會被靜默覆蓋。

### 4.4 自動還原（本設計的防線）

> ⚠️ **2026-08-06 修正（獨立審查 finding #1／#2）**：本節原本的設計已被推翻，
> 下面先記原設計、再記為什麼錯與實際做法。**實作以「修正後」那段為準。**

**原設計（已廢棄）**：`upsert_signal` 的 `if existing:` 分支加一個窄例外——
`state == 'expired' AND cleared_from IS NOT NULL` 就放回 `cleared_from`。
理由寫的是「`cleared_from` 非空只可能由本功能寫入，使用者手動標的 `expired`
沒有 `cleared_from`」。

**為什麼錯（兩個獨立的洞，都已實跑重現）**：

1. **觸發條件量錯了東西**。掛在 `upsert_signal` 上等於「有人寫了一筆 Signal」
   就還原，而不是「標的回來了」。`recalc-bids --apply`（取 `state="all"`）與
   `resolve-grades --apply`（`WHERE grade IS NULL`，無 state 過濾）都會對
   expired 列重跑 upsert：實測連跑四圈 `restored_count` 變成 4，而
   `listing_obs.disappeared_at` 從頭到尾都在，`expiry_status` 仍判 `gone`。
   於是它會被 banner 再列出來、再被清一次——flip-flop。更糟的是
   `expiry-stats` 把這個被灌水的數字當「誤殺率」印給使用者，並要他據此調
   `gone_confidence`（分子與分母不同源，CLAUDE.md 第三節）。
2. **「只可能由本功能寫入」不成立**。`update_state` 從不清 `cleared_at` /
   `cleared_from`，所以被程式清過一次的標的永久帶著 `cleared_from`。使用者
   把它拉回 watching、幾天後**自己**標成 expired，守衛就又成立了——下一輪
   掃描把使用者的決定靜默改回 watching，零告警。

**修正後的做法**：

- `upsert_signal` **拿掉整個還原分支**，回到「不覆寫人工狀態，沒有例外」。
- `update_state` 一併把 `cleared_at` / `cleared_from` 設回 NULL——
  **使用者手動改狀態＝重新接管這一筆**，程式不該再記著它曾被自動清過
  （`restored_count` 不歸零，那是錯誤帳本不是狀態）。
- 新增 `Store.restore_revived_signals() -> {"restored": n, "keys": [...]}`：
  條件是 `state='expired' AND cleared_from IS NOT NULL`
  **AND 有 listing_obs 觀測列（INNER JOIN）AND `disappeared_at IS NULL`**
  ——「回來了」的定義只有一個：離場標記被 `_upsert_listing_obs` 清掉。
  沒有觀測列是「我們不知道」不是「它回來了」（`prune_listing_obs` 刪掉舊列
  之後 LEFT JOIN 也會給 NULL）。最後再過一次 `expiry_status`，與清除**同源**
  ——少了這一條，「已結標但還留在搜尋結果裡」的競標會被清掉→立刻還原→
  再被清掉，同一個 flip-flop 換入口重演。
- `pipeline.scan` 在 `record_listing_scan` **之後**呼叫它（那裡才是清掉
  `disappeared_at` 的地方，放前面還原永遠慢一輪），還原筆數印出來並進
  掃描報告的 `restored` 欄（還原必須看得見，CLAUDE.md 第五節）。

### 4.5 可觀測性（對抗「誤殺是靜默的」）

`restored_count` 累積起來就是**這個功能自己的誤殺率**。沒有它，功能會安靜地錯下去，正如 CLAUDE.md 第一節說的「沒有工具就沒人會發現」。

新增 CLI `ygo-sniper expiry-stats`：

- 累計清除筆數（依 `cleared_from` 與來源拆）
- 累計自動還原筆數與比率
- 目前各分頁的待清筆數

還原發生時在 dashboard 顯示「已復活」徽章——讓還原這件事**看得見**，而不是只有統計。

### 4.6 API

**`GET /api/signals`**：每列多帶一個 `expiry` 物件（`ExpiryStatus` 的 dict 形式）。

**`POST /api/signals/clear-expired`**（新增，專案第一支批次寫入端點）：

- Request：`{"state": "watching"}`——只接受 `watching` / `asked_seller` / `offer_sent` 三者，其他值回 400
- 行為：把該 state 底下 `kind != "live"` 的列改成 `expired`，寫入 `cleared_at` / `cleared_from`
- Response：`{"cleared": 47, "keys": [...], "by_source": {"buyee_paypay": 32, ...}}`
- **冪等**：清完就不在原 state，重按第二次回 `cleared: 0`（工程原則 2：非冪等寫入不可重試，此端點刻意設計成冪等）
- 失敗一律拋出並分類，不回傳空成功（CLAUDE.md 第五節）

參考既有寫法：`web/app.py:261` 的 `POST /api/signals/{key}/state`（Pydantic model、enum 驗證、404 檢查）。

### 4.7 UI（`web/static/index.html`）

**徽章**（加在 `card()`，`index.html:827`）：

| 情況 | 文案 |
|---|---|
| `ended` | `已結標` |
| `gone`，watching / asked_seller | `疑似已售出 · 消失 6 小時` + 低信心時附 `· paypay 復活率高` |
| `gone`，offer_sent | `已離場 · 確認是否標下？` |
| `restored_count > 0` | `已復活`（額外徽章，與上述並存） |

過期項目**預設仍然列出**，只是灰掉（沿用 `.cd.ended` 的視覺語彙，`index.html:247`）——看得到才敢清。

**分頁頂端 banner**：`47 筆疑似已離場　[清除]`

**確認對話框**：

```
將清除 47 筆（buyee_paypay 32、buyee_yahoo 8、ebay 6）
→ 移到「已過期」分頁，可隨時查看
→ 若重新上架會自動放回觀察中

［其中 buyee_paypay 的離場判定實測復活率約 70%］

              [取消]  [清除]
```

`offer_sent` 分頁額外多一行：`已出價的標的消失可能代表你標下了，請先確認`。

---

## 5. 測試計畫

**範本**：`tests/test_card_bucket.py`（282 行）——`bucket` 欄位當初就是用同一套「migration＋store＋web API」三段結構加進來的，本功能照抄它的骨架（`_legacy_db` / `_columns` / `_insert` / `store` fixture / `client` fixture）。

⚠️ `tests/test_store.py` 與 `tests/test_web.py` **不存在**，不要往那裡加。

新檔 `tests/test_expiry.py`（判定層純函式）：

1. `end_time` 已過 → `kind="ended"`, `confidence="certain"`
2. `end_time` 未到但 `disappeared_at` 非空 → `kind="gone"`
3. 兩者都成立 → `kind="ended"`（確定事實優先）
4. 只有 `window_exit_at` → `kind="live"`（不觸發過期）
5. 信心度依來源查表；未列舉的來源落到 `_default: low`
6. `end_time` 的時區處理：naive 與 aware 輸入都得到同一結論

新檔 `tests/test_expiry_clear.py`（migration + store + API，照 `test_card_bucket.py` 骨架）：

7. Migration 加三欄且既有列不受影響（列數不變、`state` 沒被動、新欄位 NULL）
8. Migration 冪等（連開三次 `Store(db)` 不炸）
9. 清除寫入 `cleared_at` / `cleared_from`，state 變 `expired`
10. **自動還原**：清除後 `upsert_signal` 同一個 key → 回到 `cleared_from`，`restored_count` +1，`cleared_*` 清空
11. **手動標的 `expired`（`cleared_from IS NULL`）不被自動還原**——這是紅線測試
    （2026-08-06 補強：**被程式清過、使用者拉回、再由使用者自己標成 expired**
    的那條路徑也要測——原本的測試只涵蓋「從沒被清過」的列，擋不住它）
12. `list_signals` 的 JOIN 不讓 `listing_obs` 的同名欄位覆蓋 signals 的值（兩表有 11 個同名欄位，含 `last_seen` / `landed_twd` / `grade`）
13. `POST /api/signals/clear-expired` 冪等：連按兩次第二次回 `cleared: 0`
14. 不合法的 state 回 400
15. `GET /api/signals` 每列都帶 `expiry`

依 CLAUDE.md 第六節（測試路徑=生產路徑）與全域原則 4（測試不碰真實世界）：所有測試用臨時 db，不碰 `data/sniper.db`，不發任何外呼。

---

## 6. 已知限制（誠實標註）

1. **`disappeared_at` 的 56.5% 誤判率是本功能的地板**，自動還原只能讓誤殺自癒，不能讓它不發生。使用者按下清除後短期內會看到一批項目跳回觀察中——這是預期行為，不是 bug。
2. **復活率的量測有存活偏差**：分母裡目前仍標記 disappeared 的列，未來還可能復活，所以 56.5% 是**下界**，真實誤判率只會更高。
3. **`buyee_mercari` 的信心度沒有證據**（n=1），保守給 `low`。累積樣本後用 `revive-rate` 重量。
4. **本設計不改善 `disappeared_at` 判定本身**。`record_listing_scan`（`store.py:898`）的觀測窗地平線機制是另一個題目；若日後要降低誤判率，那裡才是根因所在。
5. **`end_time` 路徑在 watching 幾乎不會觸發**（實測 0 筆）。它仍然實作，因為它是唯一的確定事實，且 `skipped` 分頁有 92 筆——但不要期待它在觀察中產生效果。
6. **自動還原對「已出價且真的標下」無效**。真的標下的標的不會再出現在搜尋結果，所以永遠不會觸發還原，它會靜靜留在「已過期」分頁。這是 `offer_sent` 需要確認框額外提醒的原因——那個提醒是唯一的防線，還原機制在這個情境幫不上忙。

---

## 7. 驗收條件

1. `make test` 全綠，且新增的 14 條測試都在
2. 對 `data/sniper.db` 的副本跑一次完整流程：清除 → 確認 47 筆進 `expired` → 手動改一筆的 `last_seen` 觸發 upsert → 確認自動還原且 `restored_count = 1`
3. `ygo-sniper serve` 實跑，三個分頁的徽章與按鈕都可見（依 CLAUDE.md 第六節：驗證使用者實際會打的指令）
4. `ygo-sniper revive-rate` 與 `ygo-sniper expiry-stats` 都能跑出數字
5. 手動標為 `expired` 的列在重新掃到後**不會**被還原（紅線）
