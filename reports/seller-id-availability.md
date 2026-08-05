# Seller Alpha Phase 0：賣家識別碼可得性實測

實測日期：2026-08-03。方法：先查 tests/fixtures 的生產路徑實抓 HTML（2026-08-01/02），
再以 httpx（生產 UA、間隔 ≥2s）實抓當日頁面交叉驗證。本輪對外請求合計 13 個。

## 結論表：來源 × 賣家 ID 可得性 × 額外欄位

| 來源 | 從已在抓的頁面拿得到？ | 欄位／位置 | ID 形狀 | 覆蓋率（實測） | 額外欄位 |
|---|---|---|---|---|---|
| **yahoo_direct**（搜尋頁） | ✅ | `li.Product` 內 `div.Product__bonus[data-auction-auc-seller-id]` | 28–29 字混淆 ID | fixture 53/53、當日實抓 53/53 | 無（搜尋卡片沒有評價數） |
| **yahoo_closed**（落札相場） | ✅ | `__NEXT_DATA__` 每筆 item 的 `seller.userId` | AUCTION 標的＝28–29 字混淆 ID；`isFleamarketItem` 標的＝`p\d+` | fixture 50/50、當日實抓 50/50 | `seller.goodRating`（"98.7%" 字串）、`isStore`、`displayName`（遮罩 `********`） |
| **paypay_direct**（搜尋頁） | ✅ | `__NEXT_DATA__` 每筆 item 的 `sellerId`（另有 `seller` 物件） | `p\d+`（7–9 位） | fixture 100/100、當日實抓 100/100 | `seller.goodRatio`（數值 %）、`seller.numRating`（評價數） |
| **buyee_mercari**（搜尋 tile） | ❌ | tile 只有 圖／標題／價格／台幣換算（fixture 96 tile 逐一確認） | — | 0/96 | — → **open item** |
| **ebay**（Browse API） | ✅（已在收） | `seller.username`（`ebay.py:639`，勿動） | username | fixture 全部 summary 皆有 | `seller.feedbackScore`（int）、`seller.feedbackPercentage`（字串）——**已隨 `raw={**item}` 整包落庫**，不需改 ebay.py |

## 關鍵實測證據

### 1. Yahoo 混淆 ID 跨日穩定、跨頁面同空間（賣家帳本成立的前提）

- 同日交叉：search fixture 的 `data-auction-auc-seller-id` 與 closedsearch fixture 的
  `seller.userId` 有 4 個賣家重疊（如 `2DTKVP64n2mCP7xRcFefhBkyrJrV5`）→ 同一 ID 空間。
- 跨日穩定：當日重抓 closedsearch「遊戯王 PSA 初期」，與 2026-08-01 fixture 共同出現的
  26 個 auctionId，**seller.userId 26/26 逐字相同**。
- 該 ID 可直接開賣家頁（見 Phase 2 報告）：`auctions.yahoo.co.jp/seller/{id}` HTTP 200。

### 2. PayPay `p\d+` 與 yahoo_closed 的フリマ賣家同空間

closedsearch 的 `isFleamarketItem` 標的 seller.userId 是 `p30841287` 這種形狀，與
paypay_direct 的 `sellerId`（如 `p245246`）同空間——按 `{site}:{seller_id}` 記帳時，
兩條管道的フリマ賣家會正確合流到 `buyee_paypay:pXXXX`（同站才合併；跨站同名不合併）。

### 3. eBay seller 物件（fixture `ebay_api_items.json`）

```json
{"username": "schloast", "feedbackPercentage": "100.0", "feedbackScore": 747}
```
`_to_listing` 已把整個 item 塞進 `raw`，所以 feedback 兩欄**現在就已經在 signals payload 裡**，
sellers 表聚合端直接從 raw 讀即可，零改動 ebay.py。

### 4. PayPay 搜尋結果混入的ヤフオク標的也帶 `p\d+` sellerId

實測 2 筆（`h1238186452` → `p67594231`）。這批本來就被 `_FLEA_ID_RE` 排除出 listings，
不影響帳本；記錄此事實是因為它證明 p-num 空間橫跨 Yahoo 生態的フリマ側帳號。

## Open items（記下即跳過，不開商品頁）

1. **buyee_mercari**：搜尋 tile 無任何賣家資訊。要拿賣家得逐商品頁
   （`/mercari/item/{id}` 商品頁未驗證是否有賣家欄），依使用者決策記入 open item 跳過。
   後果：Mercari 側的折價歷史暫時無主人；venue 實測 Mercari 係數 ×2.14，屬次要來源。
2. **yahoo_direct 搜尋卡片無評價數**：只有 ID。評價維度可由 yahoo_closed 的
   `seller.goodRating` 或賣家頁的 `rating.total`/`goodRatio` 補（Phase 2 已證實可抓），
   本棒不實作。
3. **Yahoo 混淆 ID 的長期穩定性**：已證實 ≥2 天穩定；是否為永久穩定的帳號代稱
   （而非 session 性 token）尚無多週證據。監控模組上線後若發現同賣家 ID 漂移，
   以 `sellers.note` 記錄並回報。
