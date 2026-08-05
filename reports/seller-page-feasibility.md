# Seller Alpha Phase 2：賣家頁列舉可行性實測

實測日期：2026-08-03。只實測與記錄，不實作監控排程（下一棒）。
所有請求 httpx（生產 UA）、間隔 ≥2s；本階段對外請求 9 個（含 Phase 0 併跑的部分）。

## 結論表

| 站 | 能列舉單一賣家全部在架？ | 方法 | 實測筆數 | 賣家頁有已售出清單？ |
|---|---|---|---|---|
| **eBay** | ✅ | Browse API `filter=sellers:{username}`（需搭 `q` 或 `category_ids`） | `ebay:psa`＋`q=yugioh` → **total 6,091**、一頁 200；`category_ids=183454`（CCG 單卡）→ **total 111,235**，回傳筆數 sellers 全部＝psa | ❌（Browse API 只有在架；已售出要 Marketplace Insights API，另一個授權） |
| **Yahoo 拍賣** | ✅ | `https://auctions.yahoo.co.jp/seller/{混淆ID}`，純 httpx 200（256KB） | 賣家 `AiUkMq…`（Natural Cards）→ `totalResultsAvailable: 38`、items 38 筆 | ❌ 頁上無「終了分」入口（`終了したオークション／過去の出品／売却済` 字串 0 命中）；`closedsearch?sellerID={混淆ID}` 無效（結果節點不存在）。評價頁 `jp/show/rating?auc_user_id={id}` 可當成交**次數** proxy（無價格） |
| **PayPay（Yahoo!フリマ）** | ✅ | `https://paypayfleamarket.yahoo.co.jp/user/{sellerId}`，純 httpx 200（181KB） | 賣家 `p245246` → `totalResultsAvailable: 38`、items 38 筆、`sellerId` 38/38 相符 | ✅ **有，且帶真實成交時間**：38 筆中 `itemStatus=SOLD` 32 筆、OPEN 6 筆；SOLD 樣本 `z621971244` `endTime=2026-06-08T21:55:54+09:00`（openTime 兩天後成交） |

## 逐站細節

### eBay（Browse API）

- `filter=sellers:{psa}` 語法實測有效：`q=yugioh` 那趟回傳 200 筆的 seller username
  全部是 `psa`；只帶 `category_ids`（不帶 q）也有效（total 111,235，抽樣 10 筆全 psa）。
- 分頁走既有的 `limit`/`offset`（一頁最多 200），與 `ebay.py` 現行搜尋同一條路。
- 已售出：Browse API 不提供。賣家評價數（feedbackScore）可當長期活躍度 proxy。

### Yahoo 拍賣賣家頁

- URL：`/seller/{28-29 字混淆 ID}`（搜尋頁 `data-auction-auc-seller-id` 直接可用）。
- `__NEXT_DATA__` 結構與 closedsearch **同構**：
  `props.pageProps.initialState.search.items.listing.{items,totalResultsAvailable}`，
  item 欄位同一組（auctionId/title/price/bidCount/endTime/isFleamarketItem…）
  → 解析可完全重用 `yahoo_closed.py` 的 `_dig`＋路徑常數模式。
- 額外紅利：`initialState.user.seller` 有完整賣家檔案——
  `displayName`（未遮罩，如 "Natural Cards"）、`rating.total: 646`、
  `rating.goodRatio: 0.997`、`isStore`、`isEkycVerified`。
  評分模組要的風險維度這裡一次拿齊。
- 已售出清單：無。成交歷史仍靠 yahoo_closed（落札相場帶 seller.userId，
  Phase 1 已把它記進 comps.seller_id——折價歷史有主人，等於不需要賣家頁的已售出清單）。

### PayPay 賣家頁

- URL：`/user/{p\d+}`（`/user/profile/{id}` 是 404）。
- `__NEXT_DATA__` 結果節點與搜尋頁**同一路徑**
  （`props.initialState.searchState.search.result`）→ 解析可完全重用
  `paypay.py` 的 `_RESULT_PATH`＋`_extract_listings`（含 SOLD/OPEN 分流與真實 endTime）。
- **已售出升級**：賣家頁直接列 SOLD 品項＋真實成交時間，售出率從「消失 proxy」
  升級成實測——這是三站裡唯一能直接量測單一賣家售出率的。

## 第三棒實作後的修正（2026-08-04）

實作 `seller_watch.py` ＋ 輪替掃描時，兩件事與本報告當初的推論不同：

1. **eBay 的賣家頁列舉必須帶關鍵字**（本報告當初只說「需搭 `q` 或 `category_ids`」，
   實作時一度只帶 `category_ids`）。eBay **沒有遊戲王專屬分類**：`ebay:psa` 的商品
   分類 100/100 都是 `183454 CCG Individual Cards`，寶可夢與遊戲王同一格。而 psa
   在那一格底下有 **135,638** 件、`q=yugioh` 只有 **6,241** 件——不帶關鍵字、
   只抓第 1 頁（價格升冪）實測回來 100 筆全是寶可夢，**他的遊戲王庫存一筆都看不到**。
   現行設定：`sources.ebay.seller_page_keyword: "yugioh"`（覆蓋率決定，不是收不收的判準）。
   附帶抓到的判準漏洞：`exclude_keywords` 只有「ポケモン」沒有 "POKEMON"，
   1998-2004 的寶可夢 PSA 卡因此完全合乎候選條件（已補，雙向實測誤殺 0）。
2. **Yahoo 拍賣賣家頁的解析器還沒寫**。頁面本身確實可列舉（本報告實測），但
   `__NEXT_DATA__` 的欄位與現行搜尋頁解析器不同構，第三棒沒有實作它——
   名單上的 Yahoo 賣家會被明確記成「來源尚未支援」（`seller_watch.UNSUPPORTED_SITE_NOTE`），
   不是安靜跳過。這是目前最大的覆蓋缺口（賣家帳本裡 Yahoo 賣家最多）。

## Yahoo 賣家頁解析器上線（2026-08-04，補完上面第 2 點）

`YahooAuctionSource.search_seller()` 已實作，`SELLER_PAGE_SOURCE` 補上
`buyee_yahoo → yahoo_direct`。實跑證據（監控名單上的賣家）：

| 賣家 | 在架 | 價格種類 | 好評率 |
|---|---|---|---|
| `8m1fe2VP…`（名單第一名） | 6 | 全 `current_bid`（純競標） | 562 筆／97.8% |
| `AiUkMq1p…` | 38 | 全 `current_bid` | 646 筆／99.7% |
| `9RdswzR6…` | 50（total 89） | 全 `buyout` | 903 筆／99.6% |
| `53dyMh3X…` | 5 | 全 `buyout`（現在価格另存 raw） | 448 筆／99.3% |
| `AfpCqXQp…`／`7EkiPkgk…`／`7nnRkQvA…` | 0 | — | EMPTY_CONFIRMED |

三個當初推論之外的實測結論（細節見 `tests/fixtures/RECON.md` §1b）：

1. **與搜尋頁不同構、但與 closedsearch 同構**——所以「解析器尚未實作」的真正
   成本只是幾十行，不是另一套解析框架。當初把它列為缺口時高估了工作量。
2. **分頁真的有效**（`?b=51&n=50`，兩頁交集 0 筆）。原本假設「一頁裝得下」，
   實測 `9RdswzR6…` 有 89 件——一頁會**靜默截掉 39 件**。`watch_pages` 仍是 1
   （請求預算），但介面已經支援，要提高覆蓋率只要改設定。
3. **`rating.goodRatio` 是比例（0.978）不是百分比**，與 PayPay 的同名欄位不同單位。
   不換算會讓每個 Yahoo 賣家被判成「好評率 0.98%」→ 扣 15 分，沒有任何外顯症狀。

風險維度（`risk_known`）從 9/34 個賣家升到 12/34。分數變化只有一個：
`8m1fe2VP…` 79.8 → 69.8（好評率 97.8% < 98 → −10）——**名單第一名原本的高分
有一部分來自「風險未知不扣分」**，這正是這次補洞的價值。

PayPay 的部分與本報告一致：`/user/{id}` 純 httpx 可讀，結果節點與搜尋頁同路徑，
解析完全重用 `_extract_listings`（實測兩個賣家：38 筆中在架 3 / 6 筆，其餘 SOLD）。

## 給下一棒的監控參數（使用者已定，僅記錄不實作）

- 監控名單上限 **30 個賣家**；每賣家每 **240 分鐘**掃一次。
- 建議實作形式：**拆 4 批、每小時輪一批**（30÷4≈8 賣家/小時，每賣家 1 請求，
  加上既有掃描約 11 請求/輪，總量遠低於節流上限）。
- 已寫進 `config/settings.yaml` 的 `seller_alpha` 註解區塊。
