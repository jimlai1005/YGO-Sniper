# 指定卡狙擊：各平台「已售出檔案」的真實可及範圍（2026-08-09 實測）

研究問題：對一張特定的卡（測試對象 `魔法の筒` / Magic Cylinder / `P4-06`，
目標型態 ARS10 鑑定），我們能從各平台自己的成交檔案挖回多少歷史？

所有實測都是 read-only GET、browser UA、不登入、不送表單、每站 ≤3-4 個請求、
請求間隔 2-3 秒。原始探針腳本在 session scratchpad（`probe_yahoo_closed.py` /
`probe_paypay.py` / `probe_generic.py`），本檔記結論與證據。

---

## 0. 結論（三句話）

1. **Yahoo 落札相場（`closedsearch`）是唯一值得打的成交檔案**：純 httpx、
   HTTP 200、無 JS、`__NEXT_DATA__` 直接吐結構化 JSON，實測 `魔法の筒`
   **2 個請求（0.85s + 0.56s）挖回 126 筆成交、涵蓋 179 天**
   （最舊 `2026-02-11T19:02:58+09:00`，今天 2026-08-09），每筆都帶
   落札價／落札時刻／出價數／賣家 ID／競標 vs 一口價旗標。
2. **它同時涵蓋 Yahoo!フリマ**（`isFleamarketItem`，實測第一頁 100 筆裡 22 筆），
   所以「ヤフオク＋フリマ」是同一個請求就拿到的兩個池；`paypay_direct` 的
   一般搜尋只是低產出率的補丁（實測 100 筆裡 8 筆 SOLD），且其 SOLD 樣本
   大多已被 closedsearch 涵蓋。
3. **Mercari 與 eBay 的成交檔案，無 JS 一律拿不到**：Buyee 鏡像回
   `HTTP 202 + x-amzn-waf-action: challenge`（要 Playwright 解 WAF，且頁面
   本來就沒有成交時間）；Mercari 原站回 200 但是**空殼**（0 個商品 ID，資料
   靠前端 API 取）；eBay `/sch/` 一律 `HTTP 403`（Akamai，1831 bytes 攔截頁），
   **連不帶 `LH_Sold` 的一般搜尋也 403**——不是 sold 篩選被擋，是整條路被擋。

**合計可挖回的歷史：日本側（ヤフオク＋Yahoo!フリマ）180 天，且一張卡通常
2-3 個請求內翻完；Mercari 需付 Playwright 成本且拿不到成交時間；eBay 為零。**

---

## 1. repo 內既有的已售出基礎設施（第一部分）

### 1.1 `CompsEngine.sold_queries`

| 位置 | 說明 |
|---|---|
| `src/ygo_sniper/comps.py:540` | `sold_queries(sources) -> list[(source_name, keyword)]`：兩個來路合併去重——watchlist `queries[]`（只取 `supports_sold=True` 的來源）＋ `comps_queries` 的屬性組合展開 |
| `src/ygo_sniper/comps.py:566` | 判準是 source 自己宣告的 `supports_sold`，不是名稱前綴 |
| `src/ygo_sniper/comps.py:571-575` | `comps_queries.sources` 必須明列（防請求數 × 管道數爆炸） |
| `config/watchlist.yaml:207-252` | 目前展開規則：`遊戯王 {era} {rarity} {grader}`，6×6×2＋6 條 extra，`max_queries: 120`，`sources: [yahoo_closed]` |
| `src/ygo_sniper/pipeline.py:265` | 唯一的呼叫點：`refresh_comps` 逐 (source, keyword) 打 `src.search(keyword, sold=True, pages=…)` |

### 1.2 各 source 的已售出能力

| source | `supports_sold` | 方法 | 證據 |
|---|---|---|---|
| `yahoo_closed`（落札相場） | **True** | `search(keyword, *, max_price, sold=True, pages)` → `list[Listing]`；`search_detailed(keyword, *, max_price, sold, pages, first_page)` → `SearchResult` | `sources/yahoo_closed.py:164`（旗標）、`:192`（search）、`:215`（search_detailed，`first_page` 讓回填續翻） |
| `paypay_direct`（Yahoo!フリマ 直抓） | **True** | `search(..., sold=True, ...)`、`search_seller(seller_id, *, pages, sold)`、`search_detailed(..., sold, seller=…)` | `sources/paypay.py:159`、`:209`、`:235`、`:258`；沒有 sold 篩選參數，靠 `itemStatus=="SOLD"` 分流（模組 docstring `paypay.py:35-58`） |
| `buyee_mercari` | **True** | `search(..., sold=True)` / `search_detailed(..., sold=True)`；URL 帶 `status=sold_out&sold=1` | `sources/buyee.py:103`（旗標）、`:129-133`（URL 參數）、`:145`、`:169` |
| `yahoo_direct`（拍賣搜尋頁） | **False** | — | `sources/yahoo.py:171`；賣家頁 `search_seller(sold=True)` 明確回空＋`EMPTY_CONFIRMED`＋警告（`yahoo.py:517-539`），**不靜默回在架** |
| `ebay` | **False** | `search(sold=True)` 一律回 `[]` | `sources/ebay.py:316`、`:442-447`；理由見 `history.py:36-52`（Insights API 403／scope 400、`/sch/` 403） |
| `ruten` | True（台幣，刻意不進 comps 查詢） | — | `sources/ruten.py:129`、`:52` |

### 1.3 回傳型別、成交時間、`sold_at_is_ingest`

- 型別：`SearchResult`（`sources/health.py:31-69`）——`listings` / `health`
  （`ParseHealth` 五態）/ `pages_fetched` / `parsed_count` / `archive_exhausted`。
- `yahoo_closed` 每筆 `Listing.raw` 帶（`sources/yahoo_closed.py:380-397`）：
  `sold_at`（`endTime` 換算 UTC，`to_utc_iso()` at `:100`）、`bid_count`、
  `end_time`、`is_fixed_price`、`start_price`、`price_kind="sold_price"`、
  `seller_rating`、`seller_is_store`；`Listing.seller_id` = `seller.userId`。
  守門：`bidCount >= 1` 才產出（`:355-359`），否則顯示的是開始価格。
- `sold_at_is_ingest = 1` 設定點：**`src/ygo_sniper/comps.py:461`**
  （`ingest_sold` 內 `0 if has_real_time else 1`，`has_real_time` 判 `raw["sold_at"]`）。
  回填舊資料的規則在 `store.py:369-390`；下游視窗過濾在 `store.py:857-901`。
- 「成交型態」`sale_kind`（auction/fixed/unknown）同時落庫（`comps.py:466-469`）。

### 1.4 節流與請求數

| 位置 | 內容 |
|---|---|
| `comps.py:602`（`claim_sold_run`） | `comps_queries.every_n_runs`（現值 12＝一天兩次），計數器落 store meta，`force=True` 可蓋過但仍消耗配額 |
| `comps.py:591`（`sold_pages`） | `comps_queries.pages`（現值 4），與在架掃描的 `fetch.max_pages_per_query=1` 刻意分開 |
| `sources/yahoo_closed.py:73`（`_PAGE_SIZE=100`） | 一頁 100 筆；不滿一頁 → `archive_exhausted=True` |
| `config/settings.yaml:390` | `fetch.delay_seconds: 2.0`（`sources/base.py:108-111` 強制），`cache_ttl_hours: 0.25`，`max_attempts: 3` |
| `refill.py:66`（`REFILL_PAGES = 1`） | 需求驅動回補每卡每來源固定 1 頁；`config/watchlist.yaml:255-278`：`max_cards_per_run: 10`、`cooldown_days: 7`、`sources: [yahoo_closed, paypay_direct, buyee_mercari]` |

**一次 sold 查詢的實際請求數** = `pages`（≤4），但 `yahoo_closed` 冷門查詢
不滿 100 筆就停 → 1 個請求。全輪成本：約 78 個查詢 × 1-4 頁 ≈ 200-250 請求／輪，
每輪間隔 2 秒 ≈ 8 分鐘（`config/watchlist.yaml:200-206` 的預算註記）。

### 1.5 可直接重用的「任意關鍵字打一次已售出查詢」

| 入口 | 位置 | 說明 |
|---|---|---|
| **函式（最直接）** | `refill.py:286` `_sold_search(src, source_name, keyword) -> SearchResult` | 任何情況都回 `SearchResult` 不外拋，走 `search_detailed(sold=True, pages=REFILL_PAGES)`。**這就是「指定卡狙擊」該重用的那一顆** |
| 函式（深挖） | `history.py` `run_yahoo_backfill(store, comps, source, queries, params, dry_run)` | 帶續跑帳本＋請求硬上限＋`archive_exhausted` |
| CLI | `cli.py:264` `ygo-sniper mine-history --pages N --max-requests N --dry-run` | **但關鍵字寫死來自 `comps_queries` 展開**（`cli.py:305-309`），不吃任意關鍵字 |
| CLI | `cli.py:561` `ygo-sniper refill-comps --dry-run --limit N` | 關鍵字來自 signals 反推的卡名，也不吃任意關鍵字 |
| CLI | `cli.py:1710` `ygo-sniper probe <url>` | 單一 URL 除錯用 |

**缺口**：目前沒有任何 CLI 能「給我一個關鍵字，對 sold 來源查一次」。
指定卡狙擊要嘛加一個薄 CLI 包住 `_sold_search`／`YahooClosedSource.search_detailed`，
要嘛沿用 `run_yahoo_backfill` 但把 `queries` 改成外部傳入（它已經是參數，
只是 CLI 沒開放）。`market_search.py` 是**在架**關鍵字搜尋，不是成交查詢。

---

## 2. 各平台實測（第二部分）

### 2.1 Yahoo 落札相場 `closedsearch` — ✅ 唯一完整可用的成交檔案

```
GET https://auctions.yahoo.co.jp/closedsearch/closedsearch?p=魔法の筒&va=魔法の筒&b=1&n=100
→ HTTP 200, 1,345,247 bytes, 0.85s, __NEXT_DATA__ present
   totalResultsAvailable=126  items=100
GET …&b=101&n=100
→ HTTP 200, 448,879 bytes, 0.56s, items=26（不滿一頁＝archive 翻完）
```

- **無 JS 可抓**：純 httpx + browser UA，資料在
  `props.pageProps.initialState.search.items.listing.items`（與生產路徑
  `yahoo_closed._LISTING_PATH` 逐字相同）。
- **欄位（實測 items[0].keys()，41 個）**：`auctionId` / `title` / `price`（落札價）
  / `bidCount` / `endTime`（落札時刻，含 `+09:00`）/ `startTime` / `initPriceNoTax`
  （開始価格）/ `buyNowPrice` / `isFixedPrice` / `isFleamarketItem` / `seller`
  （`userId` / `goodRating` / `isStore`）/ `category` + `categoryPath`（六層，
  含 `2084005059 遊戯王（コナミ）`）/ `itemCondition` / `watchCount` / `imageUrl` /
  `prefectureCode` / `etc`（含 `pv=` 瀏覽數）。
- **往回涵蓋多久：179 天（實證）**。`魔法の筒` 126 筆的最舊一筆
  `2026-02-11T19:02:58+09:00`，今天 2026-08-09 → 179 天，與官方宣稱的
  「180 日間の落札相場」一致。第二頁不滿 100 筆＝**這張卡的整個檔案已翻完**。
- **資料品質**：兩頁共 126 筆 `bidCount == 0` 的有 **0 筆**（與 2026-08-01
  的 200 筆實測一致）；`seller.userId` 覆蓋率 **126/126 = 100%**；
  第一頁 `isFixedPrice` 56/100、`isFleamarketItem` 22/100。
- **成本**：一張中等熱度的卡 **2 個請求、約 1.4 秒**（不含 2 秒禮貌間隔）。
- **指定卡狙擊的關鍵發現（查詢設計）**：
  - `魔法の筒 P4-06` → `totalResultsAvailable=28`，跨 2026-02-14 → 08-02，1 個請求。
  - `魔法の筒 PSA` → `totalResultsAvailable=5`，全部是鑑定品，其中
    **3 筆正是目標型態**：
    ```
    2026-07-08 ¥4600  bids=30 【ARS10】世界に2枚 魔法の筒 …プリズマティック 鑑定書付 ARS鑑定10 PSA
    2026-07-01 ¥6350  bids=15 【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 ARS鑑定10 PSA
    2026-05-27 ¥7750  bids=10 【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 ARS鑑定10 PSA
    2026-06-03 ¥168150 bids=1 fixed=True flea=True ARS10 …BMG 25th 魔法の筒 WCS2023
    2026-03-08 ¥1009  bids=10 …魔法の筒 マジックシリンダー ウルトラ PSA8 初期 2期
    ```
  - ⚠️ **但加鑑定詞是伺服器端的 AND 過濾＝靜默誤殺風險**：那 3 筆 ARS10 之所以
    命中 `PSA`，純粹因為賣家把 `PSA` 塞進標題。只寫 `ARS鑑定10` 不寫 PSA 的
    賣家，`魔法の筒 PSA` 就看不到。**正解是查卡名（126 筆／2 請求），
    本地用既有 `parse_card` + `is_candidate` 過濾**（判準只有一份，
    與 CLAUDE.md 第一節同一個立場）。卡名查詢是鑑定詞查詢的嚴格超集
    （實測 `魔法の筒 PSA` 的 ¥1009 PSA8 那筆確實出現在 126 筆裡）。
  - 熱門卡名的成本上界要另外量：`魔法の筒` 180 天只有 126 筆＝1.3 頁，
    但 `ブラックマジシャンガール` 級的卡名可能上千筆＝十幾頁。**未實測，
    設計時要設 pages 上限**。

### 2.2 Buyee 的 Yahoo 已結束 — ❌ 這條路已被拆掉，且不需要

`sources/buyee.py:80-82` 明確記載：`Site.BUYEE_YAHOO` 的**搜尋 spec 已移除**
（Yahoo 發現端全部由直抓取代）。所以 repo 沒有「Buyee 查 Yahoo sold」的路徑可照做，
而 §2.1 的 closedsearch 已經覆蓋同一個池且更好（純 httpx、有成交時間）。

### 2.3 Mercari 已售出（經 Buyee 鏡像）— ⚠️ 需 Playwright，且無成交時間

```
GET https://buyee.jp/mercari/search?lang=ja&page=1&status=sold_out&sold=1&order-sort=desc-created_time&keyword=魔法の筒
→ HTTP 202, 2,161 bytes, 0.36s
   server: awselb/2.0    x-amzn-waf-action: challenge    body 含 awsWafCookieDomainList
```

- 無 JS **抓不到**：AWS WAF 挑戰頁（正是 CLAUDE.md 第五節「202＋空 body」那個坑）。
  生產路徑靠 `WafSession`（`sources/waf.py`，Playwright + chromium）取 token。
- 即使解了 WAF，**頁面上沒有任何成交時間**（`comps.py:427-433` 的實測註記：
  搜尋頁 tile 只有 SOLD／標題／價格，商品頁也沒有），所以這批進庫一律
  `sold_at_is_ingest=1`——對「這張卡半年的成交曲線」這個用途幾乎無用。
- **往回涵蓋多久：未能確定**（沒有時間欄位可量；Mercari 的 sold 檔案本身
  沒有公開的截止宣告）。
- 附帶實測：**Mercari 原站也不行**。
  `GET https://jp.mercari.com/search?keyword=魔法の筒&status=sold_out`
  → HTTP 200、376,668 bytes，但 `/item/m…` 商品 ID **0 個**、無 `__NEXT_DATA__`、
  無 `ld+json`（只有 `self.__next_f` × 15 的 RSC 串流殼）——商品資料由前端
  另打 API 取得。無頭抓取＝空殼。

### 2.4 PayPay フリマ／Yahoo!フリマ 已售出 — ⚠️ 可查但低產出、且已被 §2.1 涵蓋

```
GET https://paypayfleamarket.yahoo.co.jp/search/魔法の筒
→ HTTP 200, 409,811 bytes, 0.95s, __NEXT_DATA__ present, 100 items
   itemStatus: {OPEN: 92, SOLD: 8}
   SOLD endTime 範圍：2026-03-28 .. 2026-08-03（128 天）
```

- **無 sold 篩選參數**（`paypay.py:35-40` 實測 `itemStatus=sold` 被靜默忽略），
  只能抓一般搜尋再靠 `itemStatus == "SOLD"` 分流。本次實測產出率 **8/100**。
- 可取得欄位：`id` / `title` / `price` / `itemStatus` / `openTime` / `endTime`
  （SOLD 者的 `endTime` 是真實成交時刻）/ `sellerId` / `seller` / `condition` /
  `category` / `likeCount` / `thumbnailImageUrl`。
- **往回涵蓋多久：實測 128 天，但這是「還留在搜尋索引裡的已售出」的有偏樣本**，
  不是檔案深度——平台沒有「已售出檔案」的概念。
- **反面證據（最強的一條）**：本次 8 筆 SOLD 裡至少 2 筆
  （`2026-08-03 ¥1294 遊戯王カード 4枚セット…`、`2026-08-01 ¥580 遊戯王カード
  まとめ売り54枚…`）**逐字出現在 §2.1 的 closedsearch 結果裡**——
  closedsearch 的 `isFleamarketItem` 那 22% 就是這個池。所以對「指定卡狙擊」
  而言，`paypay_direct` 是**冗餘請求**，除非要抓 closedsearch 沒收錄的
  フリマ 標的（未量化）。
- 成本：1 個請求 0.95s／100 筆。

### 2.5 eBay 已售出 — ❌ 零

```
GET https://www.ebay.com/sch/i.html?_nkw=magic+cylinder+psa&LH_Sold=1&LH_Complete=1
→ HTTP 403, 1,831 bytes, 0.16s, server: AkamaiGHost, title "Error Page | eBay"
GET https://www.ebay.com/sch/i.html?_nkw=magic+cylinder+psa      （不帶 sold 篩選）
→ HTTP 403, 1,831 bytes                                          （完全一樣）
```

- 兩個請求 **逐位相同的 403**：被擋的不是 sold 篩選，是整條 `/sch/` 抓取路徑
  （Akamai bot manager，回應設 `bm_s` cookie）。與 `history.py:47-49`
  2026-08-04 的實測（403、1.8KB 攔截頁）**完全複現**。
- 官方管道也走不通（`history.py:38-46`，本次未重測）：Marketplace Insights API
  用現有 client credentials 回 **403**，直接申請 `buy.marketplace.insights`
  scope 回 **400 invalid_scope**——需要 eBay 逐案審核的合作夥伴權限。
- **往回涵蓋多久：0 天。** eBay 只有在架價（ask basis）。

---

## 3. 對「指定卡狙擊」的直接含意

1. **登錄一張卡時該打的查詢**：`yahoo_closed` × 卡名（日文），`pages` 開到
   足以看到 `archive_exhausted=True`（`魔法の筒` 是 2 頁）。**不要**在伺服器端
   加鑑定詞或卡號窄化——那是靜默誤殺（§2.1 的 ARS10 例子）。
2. **一次登錄的成本**：熱度中等的卡 2-3 個請求／約 2 秒（＋2 秒間隔），
   換 180 天、含賣家 ID 與競標/定價旗標的完整成交序列。這個成本低到可以
   「使用者輸入一張卡 → 當場回一張半年價格分布」。
3. **`paypay_direct` 對指定卡是可選的第二請求**（產出率 8%、且與 closedsearch
   高度重疊）；`buyee_mercari` 只有在願意付 Playwright 成本、且接受
   `sold_at_is_ingest=1` 時才值得；`ebay` 這條線在資料權限層面就是死的。
4. **既有可重用的最小單元**：`refill._sold_search()`（`refill.py:286`）＋
   `YahooClosedSource.search_detailed(first_page=…)`（`yahoo_closed.py:215`）＋
   `CompsEngine.ingest_sold()`（`comps.py:406`）。缺的只是一個吃任意關鍵字的
   CLI 入口——`run_yahoo_backfill` 的 `queries` 早就是參數，CLI 沒開放而已。
5. **兩個尚未量化的風險**：(a) 熱門卡名的 archive 可能是十幾頁，要設頁數上限；
   (b) `ingest_sold` 會用生產濾網把「不是鑑定卡」的成交全擋掉——對指定卡狙擊
   要的是**同卡全部成交的價格分布**（含未鑑定品當底價參考），兩者要不要用
   同一張表，是一個設計決定，不是實作細節。
