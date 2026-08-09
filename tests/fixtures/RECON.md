# Phase 0 現場勘查結論（RECON）

實測日期：2026-08-01。Phase 2/4/5/6 實作者只讀本檔即可動工。
所有 selector／字串／regex 都可直接照抄。fixture 皆為當日實抓原始 HTML。

## Fixture 清單（7 個）

| 檔案 | 內容 | 取得方式 | HTTP 狀態 |
|---|---|---|---|
| `yahoo_closed_ok.html` | Yahoo 落札相場「遊戯王 PSA 初期」，50 筆成交（見 §6） | httpx 直抓 | 200 |
| `yahoo_closed_empty.html` | Yahoo 落札相場查無結果頁 | httpx 直抓 | **404** |
| `yahoo_search_ok.html` | Yahoo 搜尋「遊戯王 psa 初期」，53 個 `li.Product` | httpx 直抓 | 200 |
| `yahoo_search_empty.html` | Yahoo 查無結果頁（含無結果字串） | httpx 直抓 | **404** |
| `buyee_mercari_ok.html` | Buyee Mercari「遊戯王 PSA」lang=ja，96 個商品 | **httpx＋WAF token** | 200 |
| `buyee_mercari_empty.html` | Buyee Mercari 亂碼關鍵字，0 商品 | **httpx＋WAF token** | 200 |
| `buyee_paypay_ok.html` | Buyee PayPay「遊戯王 PSA」lang=ja，40 個商品 | **httpx＋WAF token** | 200 |

2026-08-04 追加（Yahoo 拍賣**賣家頁**列舉，見 §1b）：

| 檔案 | 內容 | 取得方式 | HTTP 狀態 |
|---|---|---|---|
| `yahoo_seller_ok.html` | 賣家 `53dyMh3X…`，5 筆**全部有即決価格**（且同時有現在価格＋出價數） | httpx（`CachedFetcher` 生產路徑） | 200 |
| `yahoo_seller_bid_only.html` | 賣家 `8m1fe2VP…`（名單第一名），6 筆**全部純競標**（無即決） | httpx（同上） | 200 |
| `yahoo_seller_empty.html` | 賣家 `AfpCqXQp…`，`totalResultsAvailable: 0`（目前沒有在架商品） | httpx（同上） | 200 |

> ⚠️ **2026-08-02 重抓：三份 Buyee fixture 從 Playwright 換成 httpx。**
> Playwright 會把 lazyload 的 JS 跑完，真圖已經被塞進 `img.src`；但生產環境
> （`WafSession` 只用瀏覽器換 token、頁面一律 httpx 抓）看到的是
> `src=".../loading-spinner.gif"` ＋ `data-bind="lazyload: { imagePath: '//…' }"`。
> 差異的後果：解析測試永遠綠，db 裡 24/28 筆 Buyee 訊號的縮圖全是轉圈圈動畫。
> **fixture 必須用生產環境的抓法取得**，否則測到的是一個線上永遠不存在的頁面。
> 重抓方式：`WafSession._refresh(seed)` 拿 token → `waf.fetcher.get(url, use_cache=False)`。
> `tests/test_buyee_parse.py::test_fixtures_are_the_html_production_actually_sees` 守這件事。

---

## 1. Yahoo! Auctions 搜尋頁

### URL 格式
```
https://auctions.yahoo.co.jp/search/search?p={kw}&va={kw}&b={offset}&n=50[&aucmaxprice={N}]
```
- `b` 是 **1-based 商品 offset**（不是頁碼）：`b=1` 第 1 頁、`b=51&n=50` 第 2 頁。
  實測第 2 頁顯示「51件〜100件を表示」，與第 1 頁商品重疊僅 4 筆（皆為推廣位重複曝光）
  → **解析後必須以 auction id 去重**。
- `aucmaxprice=5000` 實測生效：回 33 件、所有現在価格 ≤ 5000（含 5000，閉區間）。
- 無 WAF，純 httpx 可抓。

### 查無結果（EMPTY_CONFIRMED）
- **HTTP 404** ＋完整頁面（197KB）。`CachedFetcher` 需 `allow_statuses=(404,)`（見 PLAN Phase 2）。
- 無結果字串原文（一字不差）：`条件に一致する商品は見つかりませんでした。`
  - selector：`h2.Empty__title`（外層 `div.Empty`）。
  - 此時 `li.Product` 為 0。

### 商品項目
- 容器：`li.Product`（一頁 50 顯示 + 0〜3 個推廣重複 → fixture 有 53 個）。
- 標題連結：`a.Product__titleLink`，href 含 `/auction/{id}`。
- **id regex：`/auction/([A-Za-z0-9]+)`**。觀察到兩種形狀：`[a-z]\d{9,10}`（絕大多數，
  如 `s1238539612`）與**純數字**（fixture 實例 `1239234527`）。
  ⚠️ 既有 `_SITE_SPEC` 的 `([a-zA-Z]\d+)` 會漏掉純數字 id。

### 價格（現在価格 vs 即決価格）
```html
<div class="Product__priceInfo">
  <span class="Product__price">
    <span class="Product__label">現在</span>
    <span class="Product__priceValue u-textRed">3,440円</span>
  </span>
  <span class="Product__price">          <!-- 只有可即決的商品才有第二組 -->
    <span class="Product__label">即決</span>
    <span class="Product__priceValue">30,000円</span>
  </span>
  <p class="Product__postage">＋送料230円</p>
</div>
```
- 判別方式：迭代該 li 的 `.Product__price`，看 `.Product__label` 文字是 `現在` 或 `即決`。
- 純競標商品只有一組（`現在`）；有即決的有兩組。`u-textRed` 只是樣式，不可當判別依據。
- 價格文字格式：`3,440円`（千分位逗號＋円）。
- fixture 分佈：53 筆 = 14 有即決 + 39 純競標。

### 命中數（交叉比對用）
- **總命中數：`li.Tab__item--current span.Tab__subText`** → 文字 `142件`（格式 `{N}件`，N 可含千分位逗號）。
  三個 Tab：すべて／オークション／定額，current 預設是すべて。
- 輔助：`p.Options__resultCount` → `1件〜50件を表示`。
- ⚠️ 干擾源：側欄 `p.FilterItem__count`（`2件出品中`）是分類 facet 數，不要抓。
- ⚠️ 實測第 2 頁（b=51）的 Tab 數字與第 1 頁不一致（144 vs 310，疑似模糊比對擴大）
  → **命中數交叉比對只用第 1 頁、且與商品數同一次抓取**。

## 1b. Yahoo! Auctions 賣家頁（2026-08-04 實測）

```
https://auctions.yahoo.co.jp/seller/{28-29 字混淆 ID}[?b={offset}&n=50]
```
- **解析走 `__NEXT_DATA__`，不是 CSS selector**——與**搜尋頁**不同構，而與
  **closedsearch 同構**：商品節點路徑
  `props.pageProps.initialState.search.items.listing.{items,totalResultsAvailable}`
  與 `yahoo_closed._LISTING_PATH` 逐字相同，item 欄位也是同一組
  （`auctionId`／`title`／`price`／`buyNowPrice`／`bidCount`／`endTime`／`isFleamarketItem`）。
- **價格語意與搜尋頁一致**：`price` 是現在価格、`buyNowPrice` 是即決価格
  （沒設即決時為 `null`）。實測賣家 `53dyMh3X…`：現在 81,000／即決 298,000
  ——抓錯欄位會讓成本模型低估 3.7 倍。
- **分頁有效**：賣家 `9RdswzR6…`（`totalResultsAvailable` 89）第 1 頁 50 筆、
  `?b=51&n=50` 39 筆，**兩頁交集 0 筆**、50+39=89。（這個站對未知參數是靜默
  忽略的，所以這組鍵值是實測過才敢用。）
- **賣家檔案**在 `props.pageProps.initialState.user.seller`：
  `rating.total`（評價筆數）、`rating.goodRatio`（**比例**，如 `0.978`）、
  `isStore`、`isEkycVerified`。
  ⚠️ `goodRatio` 是比例不是百分比，而 PayPay 的同名欄位是百分比——
  存進 `sellers.feedback_pct` 之前必須 ×100（見 `yahoo.seller_feedback`）。
- **沒有已售出清單**（頁上無「終了分」入口）。成交歷史仍然走 `yahoo_closed`。
- 「這個賣家目前沒有在架商品」是常態：2026-08-04 實測名單上 7 個 Yahoo 賣家
  有 3 個 `totalResultsAvailable: 0` → 必須判成 EMPTY_CONFIRMED，不是解析壞掉。

## 2. 價格語意實證（最重要）

同時抓 Yahoo 原生商品頁（httpx）與 Buyee 商品頁（Playwright），五筆全數一致：

| auction id | Yahoo 現在価格 / 即決価格 | Buyee 商品頁顯示 |
|---|---|---|
| s1238539612 | 3,440 / 30,000 | 即決価格 30,000円＋現在の価格 3,440円 |
| v1239182381 | 1,680 / 4,980 | 即決価格 4,980円＋現在の価格 1,680円 |
| n1239182660 | 8,980 / 19,800 | 即決価格 19,800円＋現在の価格 8,980円 |
| m1238496717 | 20,305 / （無） | 現在の価格 20,305円（即決価格 0円，隱藏） |
| k1238516579 | 4,980 / （無） | 現在の価格 4,980円（即決価格 0円，隱藏） |

- Buyee 商品頁主價區 `dl.current_price`：有即決時**先列即決価格再列現在の価格**，兩者
  皆與 Yahoo 原生逐円一致；無即決時只顯示現在の価格（DOM 裡即決価格=0 且 hidden）。
- **結論：即決価格＝可直接付款成交的價格（`price_kind=buyout`）；現在価格＝目前出價，
  不是可成交價（`price_kind=current_bid`，預設排除）。** 與 PLAN Phase 2 決策一致。
- Buyee 商品頁 URL `https://buyee.jp/item/yahoo/auction/{id}` 五筆全通（Playwright；
  第一筆 202 WAF 過場後正常，其餘 200）。

## 3. Buyee Mercari（`lang=ja`）

### URL 與商品連結
```
搜尋：https://buyee.jp/mercari/search?keyword={kw}&lang=ja
已售出：加 &status=sold_out（實測有效）
商品：https://buyee.jp/mercari/item/{id}
```
- **⚠️ id 有兩種形狀**（同一頁混出）：
  1. 傳統 `m\d+`（如 `m89871068450`）——ok fixture 92 筆中 88 筆。
  2. **新版 22 字 base62**（如 `2JUaqAqRfVZv8txc7wBYEY`）——ok fixture 4 筆；
     sold 測試頁 98 筆中佔 82 筆（新上架比例更高，會越來越多）。
- **item_path regex 必須改為：`re.compile(r"/mercari/item/([A-Za-z0-9]+)")`**
  （既有 `(m\d+)` 會漏新版 id）。href 帶 query（`?conversionType=...`），regex 不含 query 即可。

### 商品項目
- 容器：`ul.item-lists > li.list`；連結 `a[href*="/mercari/item/"]`；
  標題 `h2.name`；價格 `p.price`（格式 `458,000円`）。
- 每頁 100 筆（li 可能 92〜98，含少量推廣）。
- **縮圖是 lazyload（2026-08-02 實測，httpx 原始 HTML）**：
  `img.thumbnail` 的 `src` 恆為 `https://cdn.buyee.jp/{mercari|paypayfleamarket}/images/common/loading-spinner.gif`，
  真圖在 `data-bind="lazyload: { imagePath: '//static.mercdn.net/thumb/item/jpeg/{id}_1.jpg?ts', onError: '…' }"`
  （PayPay 的 imagePath 指向 `//cdnyauction-pctr.buyee.jp/...`）。
  ⚠️ `onError` 的值也在同一個 `data-bind` 裡、而且是佔位圖——regex 必須錨定
  `imagePath:`，放寬成任意引號字串會抓到 onError。
- **商品頁**（`/mercari/item/{id}`、`/paypayfleamarket/item/{id}`）反而**沒有** lazyload：
  `og:image` 就是伺服器直出的真實網址（httpx 抓得到），回填舊資料走這條。

### 查無結果（EMPTY_CONFIRMED）——⚠️ 沒有無結果字串
- **實測（含截圖確認）：Buyee Mercari 無結果頁完全沒有任何「無結果」文字**，
  主內容區就是空白。`見つかりません／該当／0件` 等字串在主區一律不存在。
- ⚠️ 陷阱：`div.sideFilterItemBody__noResults` 的「検索結果 0件」是**側欄品牌 facet**，
  ok 頁也有，絕不可當無結果判定。
- **結構性判定（唯一可靠）**：
  1. `ul.item-lists` 存在（頁面骨架正常＝不是 WAF 頁）且 direct `li` 子節點 = 0；
  2. 佐證：`section.category` 內**裸文字**「`1〜100 件目`」（regex `\d+〜\d+\s*件目`）
     在 ok 頁有、empty 頁無。
- **HTTP 狀態不可用於判定**：empty 頁實測一次 200、一次 202（202 = WAF 過場，與有無結果無關）。
  Yahoo 是 404、Buyee 是 200/202——兩站語意不同。

### 命中數
- **Buyee 無總命中數元素**。只有 `section.category` 內裸文字 `1〜100 件目`（範圍，無總數）。
- Q5 的「命中數交叉比對」在 Buyee 只能退化為「件目 範圍文字存在但解析 0 筆 → PARSER_BROKEN」。

### 已售出搜尋
- `status=sold_out` 實測有效：98/98 個 li 都帶 SOLD 標記。
- SOLD 標記：`div.thumbnail-area.soldOut` 內 `div.soldOut__text`（文字 `SOLD`）。
- 一般搜尋（無 status 參數）實測 0 個 sold tile，但保險起見解析時仍可用 `.soldOut` 判斷。

## 4. Buyee PayPay Fleamarket

```
搜尋：https://buyee.jp/paypayfleamarket/search?keyword={kw}&lang=ja
已售出：加 &status=sold_out（實測有效，40/40 SOLD tile）
商品：https://buyee.jp/paypayfleamarket/item/{id}
item_path regex：re.compile(r"/paypayfleamarket/item/([A-Za-z0-9]+)")
```
- id 長相：實測 40 筆全部是 `z` + 9〜10 位數字（如 `z652102594`、`z555842170`）。
  regex 建議寬鬆到 `[A-Za-z0-9]+`（與 Mercari 新版 id 的教訓同理），不要硬編 `z`。
- **DOM 與 Mercari 完全同構**：`ul.item-lists > li.list`、`h2.name`、`p.price`、
  `section.category` 裸文字 `1〜40 件目`、SOLD 標記同為 `div.soldOut__text`
  → PLAN「PayPay 不獨立成檔、_SITE_SPEC 多一組」成立。
- 每頁 40 筆。href 帶 `?conversionType=service_page_search`。
- 也被 WAF 保護（初次 202），與 Mercari 同一 session/token 通用（實測同 context 連抓皆通）。

## 5. 給 Phase 2/4/5 的落點提醒

- Phase 2（Yahoo）：`allow_statuses=(404,)`＋`h2.Empty__title` 判 EMPTY；id regex 收純數字；
  命中數用 `li.Tab__item--current span.Tab__subText`，只信第 1 頁。
- Phase 4（告警）：Buyee 的 EMPTY 判定是結構性的（`ul.item-lists` 空），沒有字串可比對——
  PLAN Q5「Buyee 日文無結果字串」一項**不存在**，以本檔結構判定取代；canary 因此更重要。
- Phase 5（WAF）：搜尋頁與商品頁同受 WAF；Playwright 首請求常回 202 但挑戰自動解掉、
  內容完整；同 context 下 Mercari/PayPay/Yahoo-item 頁面共用同一 token。

## 6. Yahoo 落札相場 closedsearch（Phase 6，2026-08-01 實測）

### URL 與狀態
```
https://auctions.yahoo.co.jp/closedsearch/closedsearch?p={kw}&va={kw}&b={offset}&n=50
```
- 與搜尋頁同構：`b` 是 1-based 商品 offset、`n=50`。無 WAF，純 httpx 200。
- **查無結果一樣回 HTTP 404 ＋完整頁面**（92KB，`totalResultsAvailable: 0`）
  → `allow_statuses=(404,)`。
- 預設排序 = `-END_TIME`（metadata 自己回報），即「終了日時が近い順」＝最近成交的在前，
  正是 comps 要的。**不需要也不該加排序參數**（RECON §1 已證實未知排序鍵被靜默忽略）。
- 視窗 **180 天**（頁面自稱「終了180日間」）；comps 視窗是 90 天，兩者不同。

### 解析：走 `__NEXT_DATA__`，不要用 CSS selector
closedsearch 是 Next.js + styled-components，class 名是每次 build 都變的雜湊
（`sc-c91b7830-5 opRIC`）——`li.Product` / `a.Product__titleLink` 在這頁**完全不存在**。
頁面內嵌 `<script id="__NEXT_DATA__" type="application/json">`（約 189KB），
路徑 `props.pageProps.initialState.search.items.listing`：
```
totalResultsAvailable: 833          # 命中數，交叉比對用
items: [ {auctionId, title, price, bidCount, initPriceNoTax,
          buyNowPrice, isFixedPrice, isFleamarketItem, endTime, imageUrl, seller…} ]
```

### ★ 得標 vs 流標（本節最重要）
流標商品的 `price` 欄顯示的是**開始価格**，不是成交價。判別依據是 **`bidCount`**：

| 情境 | 實例 | bidCount | price | initPrice | 出現在 closedsearch？ |
|---|---|---|---|---|---|
| 落札（有得標者） | `l1238412091` | 23 | 20,000 | 1 | ✅ 是 |
| 流標（無人出價） | `g1237930015`（王宮の勅命 スーパーレア 初期） | 0 | 500 | **500** | ❌ 否 |
| 流標（無人出價） | `j1237936707`（エルフの剣士 シークレット 初期） | 0 | 1,000 | **1,000** | ❌ 否 |

- 流標樣本取得方式：開放搜尋 `s1=end&o1=a`（終了時間近い順）挑 0 入札、剩不到 1 分鐘的標的，
  等它結標後讀商品頁 `__NEXT_DATA__` → `bids=0` 且 `price == initPrice`
  （這正是「把它當成交價」會發生的事：賣家的期望價變成行情）。
  結標 3 分鐘後以其標題查 closedsearch，**兩筆都不在結果裡**。
- Yahoo 自己的措辭：頁面標題「〈kw〉**の落札された商品** 180 日間の落札相場」；
  每張卡片的價格標籤是「落札 20,000 円」，開始価格另外標「開始 1 円」。
- 抽樣 200 筆（4 個查詢 × 50 筆）**`bidCount == 0` 為 0 筆**。
- ⇒ 結論：closedsearch 本身就只列落札成功的商品。**即使如此，parser 仍硬性要求
  `bidCount >= 1`**（`sources/yahoo_closed.py::_extract_listings`）——Yahoo 哪天改行為時，
  我們要安靜地少收，不是安靜地污染行情表。
- ⚠️ 對帳過的欄位語意：`l1238412091` 在 closedsearch 是 `price=20000, bidCount=23,
  initPriceNoTax=1`；其商品頁 `__NEXT_DATA__` 是 `price=20000, bids=23, initPrice=1`
  → `price` 確定是最終落札価格。

### 混合 ID 空間
結果**同時含 Yahoo 拍賣與 Yahoo!フリマ（PayPay）**（fixture 50 筆正好各 25）。
`isFleamarketItem: true` 者的 id 是 `z`+數字，原生頁在 `paypayfleamarket.yahoo.co.jp/item/{id}`、
Buyee 端在 `buyee.jp/paypayfleamarket/item/{id}` → 逐筆 Listing 的 `site` 要跟著分流
（`BUYEE_PAYPAY` / `BUYEE_YAHOO`），否則 `Listing.key` 會把兩個 ID 空間混在一起。

### 組合展開的命中率（36 個 `遊戯王 {年代} {稀有度} PSA` 組合，每個第 1 頁 50 筆）
- 年代詞是 keep 率的主要來源（標題含年代詞 → 自然帶年代證據）：
  `初期 スーパー` 48/50、`初期 ウルトラ` 45/50、`初期 シークレット` 42/50。
- ARS 版一樣高：`初期 スーパー ARS` 45/45、`初期 ウルトラ ARS` 45/50。
- ⚠️ **Yahoo 對窄查詢會靜默退化成模糊比對**：`totalResultsAvailable` 出現 3-4 萬
  （例：`三期 スーパー PSA` = 38,794）就代表退化了，這類查詢彼此重疊率高達 80%。
  它們仍回傳合法成交資料，只是邊際效益低——調 `comps_queries` 時看這個數字。
- 冷門組合（pool < 50）只會打 1 個請求就停（不滿一頁即最後一頁），
  所以 78 個展開查詢 × pages=2 實際約 100 個請求，不是 156 個。

### fixture
| 檔案 | 內容 | HTTP |
|---|---|---|
| `yahoo_closed_ok.html` | `遊戯王 PSA 初期`，50 筆（25 拍賣 + 25 フリマ），全部 bidCount ≥ 1，total 833 | 200 |
| `yahoo_closed_empty.html` | 亂碼關鍵字，`totalResultsAvailable: 0` | **404** |

**流標 fixture 不存在**（closedsearch 不列流標，取不到）。守門條件改用
「把 ok fixture 的 `bidCount` 全改成 0」的變異測試驗證，見
`tests/test_yahoo_closed_source.py::test_unsold_items_are_excluded`。

## 7. Yahoo!フリマ（PayPay）原站直抓（2026-08-02 實測）

`paypayfleamarket.yahoo.co.jp` 是 PayPayフリマ 的現名。**取代**走 Buyee 鏡像的
`buyee_paypay` 發現管道（`sources/paypay.py`）；購買路徑不變（仍是 Buyee）。

### 為什麼換（同關鍵字 `遊戯王 PSA 初期`，同一天實測）
| | 走 Buyee 鏡像 | 直抓原站 |
|---|---|---|
| 一頁筆數 | 40 | **100** |
| 耗時 | 2.66s | 0.86s |
| 依賴 | Playwright＋chromium 解 AWS WAF | 純 httpx |
| 回應大小 | 212KB | 432KB |

價格逐筆相同（使用者另外對帳 6 筆，Buyee 未加成）。

### URL 與狀態
```
搜尋：https://paypayfleamarket.yahoo.co.jp/search/{關鍵字}     ← **路徑形式**
分頁：?page=N（1-based，實測 p2 的 result.offset = 100）
新着：?sort=openTime&order=DESC
價格：?maxPrice=5000（實測生效、閉區間：total 2677 → 531，最大值正好 5000）
商品：https://paypayfleamarket.yahoo.co.jp/item/{id}
```
- `?query=` 的 query 參數形式是 **404 空頁**；關鍵字一定要在路徑上。
- **查無結果回 HTTP 404 ＋完整頁面**（122KB，`isZeroMatch: true`、
  `totalResultsAvailable: 0`）→ `allow_statuses=(404,)`，與 Yahoo 拍賣同一個陷阱。
- 無 WAF。

### 解析：`__NEXT_DATA__`（Next.js + styled-components，class 名是 build 雜湊）
路徑 `props.initialState.searchState.search.result`：
```
totalResultsAvailable: 2677     # 命中數，交叉比對用
totalResultsReturned: 100       # 一頁 100 筆
offset: 0
items: [ {id, title, price(日圓整數), itemStatus, openTime, endTime,
          thumbnailImageUrl, sellerId, condition, category…} ]
```
⚠️ 同層還有 `search.auctionItemsModule.items`（20 筆ヤフオク!推薦位）——**不要抓**。

### ★ 混合 ID 空間（本節最重要）
結果**混著ヤフオク!的標的**：id 不是 `z`+數字的那些（實測每頁 1-3 筆，
例 `h1238186452`，`endTime = openTime + 3 天`＝競標結標）是 Yahoo 拍賣的貨，
購買路徑是 `buyee.jp/item/yahoo/auction/{id}`，**不是** paypayfleamarket。
照單全收會產出買不到的連結、還會把兩個 ID 空間混進同一個 `Listing.key`。
→ parser 只收 `^z\d+$`，其餘計入 `parsed_count` 但排除出 listings。

### 已售出（`supports_sold=True` 的依據與極限）
- **沒有 sold 篩選參數**：`?itemStatus=sold`／`SOLD` 伺服器端**靜默忽略**
  （SSR 的 `searchParams` 不變、結果與不帶參數逐筆相同）。前端的 enum 確實是
  `{OPEN:"open", SOLD:"sold", ALL:"all"}`（見 `_app` chunk），但那是 client-side
  才生效的篩選，httpx 拿到的 SSR 結果不吃它。
- `?order=ASC` 也**被強制成 DESC**（想用 `sort=endTime&order=ASC` 讓已結束的排前面 → 無效）。
- **但已售出標的會混在一般結果裡，而且帶真實成交時間**：`itemStatus == "SOLD"`
  者的 `endTime` 落在 `openTime` 與現在之間（實測 53/53），在架商品的 `endTime`
  一律是 `openTime + 1 年`（上架期限，45/47）——兩者一眼可分。
- 產出率：不帶價格上限約 2/100；`maxPrice=5000` 那頁是 53/100（便宜的賣得快）。
  有偏（只抓得到「最近賣掉且還留在索引裡」的），但**時間戳是真的**。
- ⚠️ 排序值 `sort=openTime` 是伺服器真的吃的那個；選單顯示的 `newer`／
  `recommended`／`reasonable`／`expensive` 是**前端 enum**，送進 URL 會被靜默忽略
  （`searchParams.sort` 仍是 `ranking`）。與 Yahoo 拍賣的 `s1=start` 同一種坑。

### Buyee 已售出頁**沒有成交時間**（sold_at 語意的證據）
同日以生產路徑（WafSession 取 token ＋ httpx）實抓 `status=sold_out`：
- Mercari 搜尋頁 668KB／95 個 SOLD tile、PayPay 搜尋頁 153KB／40 個 —— 全頁
  `売却`／`販売済`／`終了`／`日前`／`落札` 命中數 **0**，也沒有任何 `20xx-xx-xx`
  形狀的日期。單一 tile 的全部文字就是 `SOLD | 標題 | 價格 | (台幣換算)`。
- 商品頁（`/mercari/item/{id}`、`/paypayfleamarket/item/{id}`）同樣沒有日期。
- ⇒ Buyee 系 comps 的 `sold_at` 只能是入庫時間，必須標記
  （`comps.sold_at_is_ingest`，見 `store.py`）。

### fixture
| 檔案 | 內容 | HTTP |
|---|---|---|
| `paypay_search_ok.html` | `遊戯王 PSA 初期`＋`maxPrice=5000&sort=openTime`，100 筆（45 在架フリマ／53 已售出／2 筆ヤフオク!混入） | 200 |
| `paypay_search_empty.html` | 亂數關鍵字，`isZeroMatch: true` | **404** |

兩份都是 **httpx 直抓的原始 HTML**（這條管道不需要瀏覽器，但取得方式的規矩照舊）。

## 8. 台灣二手市場（2026-08-02 實測）

台灣是唯一「貨已經在手邊、賣出不必再付一次國際運費」的市場，所以它的行情
決定了「進口到台灣後值多少」。本節是三個平台的可行性實測與結論。

### 8.1 可行性（全部以 httpx 生產路徑實抓）

| 平台 | 狀態 | bytes | WAF／登入 | SSR／內嵌 JSON | 一頁筆數 | 成交價 | 判定 |
|---|---|---|---|---|---|---|---|
| **露天** `ruten.com.tw` | 200 | 30KB(搜尋)+66KB(詳情) | 無 | 公開 JSON API | **100** | 只有 `SoldQty`（無時間） | ✅ **可抓** |
| 蝦皮 `shopee.tw` | HTML 200 / **API 403** | 163KB / 159B | 需登入態＋簽章 | SPA 外殼，無商品資料 | — | — | ❌ 不可抓 |
| Carousell TW | 200 | 876KB | 無 | `window.initialState` 前一個 `<script>` | **3** | 無 | ❌ 樣本不足 |

- 蝦皮：`/api/v4/search/search_items` 回 `{"error": 90309999, "redirect_to_error_page": true}`。
- Carousell：`遊戲王 PSA` 全站 `numberOfResults = 3`（NT$80／NT$1,350／NT$99,999）。
  資料抓得到，但 3 筆不足以構成任何行情，也沒有成交紀錄。

### 8.2 露天 API（`sources/ruten.py`）

```
搜尋 https://rtapi.ruten.com.tw/api/search/v3/index.php/core/prod
     ?q={kw}&type=direct&sort={new|rnk}/dc&offset={1-based}&limit=100
     → {"TotalRows": 8392, "Rows":[{"Id": …}]}     ← **只有 Id**
詳情 https://rtapi.ruten.com.tw/api/prod/v2/index.php/prod?id={最多 100 個 id}
     → [{ProdId, ProdName, Currency, PriceRange, SoldQty, StockQty,
         SaleType, PostTime, SellerId, ShippingCost, …}]
商品 https://www.ruten.com.tw/item/show?{id}
```
⇒ **一頁＝兩個請求**。`遊戲王 PSA` 命中 8392 筆。

實測得到的四個陷阱（全部有對應測試）：
1. `Currency` 不一定是 TWD（100 筆有 6 筆 `USD`，海外賣家）。硬當台幣會低估 31 倍。
2. **價格篩選參數被靜默忽略**：`prc.now`／`priceRange`／`min_price` 三種寫法
   `TotalRows` 與第 1 筆完全不變 ⇒ `max_price` 只能本地過濾。
   （對比：未知的 `sort` 值回 **HTTP 400**，所以「沒報錯」≠「有生效」。）
3. **查無結果是 HTTP 200 但只有 49 bytes**（`{"TotalRows":0,…}`）——
   `CachedFetcher.MIN_BODY_BYTES=512` 會把它誤診成 BLOCKED ⇒ 需 `min_bytes`。
4. `type` 只吃 `direct`；`auction`／`all`／`finished` 一律 **HTTP 400**
   ⇒ **沒有已結束／已售出的搜尋**。

### 8.3 ★ 成交價：露天實質上沒有（本節最重要）

唯一的成交訊號是每筆商品的 `SoldQty`（累計已賣出件數），而且：
- **沒有時間戳**（`PostTime` 是上架時間，實測有 2021 年的）⇒ 走 comps 只能
  標 `sold_at_is_ingest=1`。
- 樣本極稀：12 個中文關鍵字 × 2 頁、解析 653 筆商品，`SoldQty>=1` 去重後
  **只有 15 筆**，過 1998-2004＋機構篩選後 **只剩 5 筆**。
- 那 5 筆裡有 3 筆是天價佔位標（NT$6,600,999／NT$3,000,000／NT$42,000,999），
  `SoldQty` 是累計值，成交多半發生在改價之前 ⇒ 價格與成交對不起來。

⇒ **露天的成交價不可用於行情。** 能拿到的只有在架開價。

### 8.4 中文標題的解析命中率（實測，不美化）

12 個中文關鍵字、585 筆去重在架標的：

| 指標 | 命中 | 說明 |
|---|---|---|
| 過 `parse_card`＋`is_candidate` | **186/585（31.8%）** | 擋掉最多的是「無 1998-2004 年代證據」310 筆 |
| `CardIndex.match`（全部標題） | **226/585（38.6%）** | via `code` 88／via `name` 138 |
| `CardIndex.match`（候選子集） | **93/186（50.0%）** | |

比預期高很多，原因有二（不是運氣）：
- `_YUGIOH_MARKERS` 早就含「遊戲王」⇒ **卡號反查對中文標題直接生效**
  （`SM-51`／`302-055`／`MRL-045` 這種寫法在露天很常見）。
- 露天有一批海外賣家用**英文**標題 ⇒ 走 `name_en` 索引。
- `一期／二期／三期／凸版` 與 `era_markers.jp_keywords` 剛好同字。

**讀不出來的是稀有度**：候選 186 筆有 **179 筆 `rarity=None`（96%）**——中文寫的是
`浮雕／凸版／金字／金亮／全鑽／半鑽`，`parsers/rarity.py` 是日文封閉詞表。
（JP 側同一批調查只有 192/627 = 31% 是 None。）

### 8.5 台灣 vs 日本的價格對照 → **不足以判定**

方法論與 `venue_study.py` 相同（控制稀有度×機構×分數，只比可立即成交的價格：
日本即決／定價 vs 露天直購）。TW 186 筆候選、JP 627 筆候選。結果不可用，三個理由：

1. **分層維度在 TW 側是壞的**：96% 的 TW 候選 `rarity=None`，所以「可比分層」
   全部落在「稀有度不明」那一格——那一格在 TW 是「讀不出稀有度」、在 JP 是
   「賣家沒寫稀有度」，**不是同一個母體**（工程原則 1 的混源比較）。
   實際跑出來的比值也自證：ruten/mercari 6 個分層的比值從 ×0.20 到 ×9.93。
2. **退用「卡名×機構×分數」控制也不夠**：可比組合只有 20 個、其中 17 個
   至少一邊 n=1；中位 ×0.30、範圍 ×0.02–×4.95。而且這個鍵**沒有控制印刷版本**：
   同樣是「青眼の白龍 PSA9」，露天上同時有 `SM-51 浮雕` NT$108,000–180,000、
   `LB-01 金亮` NT$3,400–12,800、西班牙版 SDK NT$23,747——**545 倍**的內部價差。
3. **沒有成交價可對齊**（見 8.3）。在架開價是賣家的期望，不是市場出清價。

⇒ **不提供任何「台灣 ÷ 日本」倍率。** 要讓這個問題可回答，缺的第一塊是
中文稀有度詞表（浮雕／金字／全鑽…→ `parsers/rarity.py`）與印刷版本鍵，
不是更多樣本。

### fixture
| 檔案 | 內容 | HTTP |
|---|---|---|
| `ruten_search_ok.json` | 搜尋 API `遊戲王 PSA`＋`sort=new/dc`，100 個 id | 200 |
| `ruten_prod_ok.json` | 詳情 API，**同一批** 100 個 id（含 6 筆 USD 標價） | 200 |
| `ruten_search_empty.json` | 亂數關鍵字，`TotalRows: 0`，**49 bytes** | 200 |

三份都是 `RutenSource` 自己的 `CachedFetcher` 抓的原始 JSON（生產路徑，無瀏覽器）。

## card sniper（2026-08-09）
- `ars_census_p4_06.html` — https://ars-grading.com/grading/searchNameDetail?id=001202208090020007，curl + browser UA（無 JS，同生產路徑）。魔法の筒 P4-06 的鑑定量頁：Grade 9=5、10=5、10+=1、総数 11。
- `ars_search_magic_cylinder.html` — searchName?name=魔法の筒&page=1，同上。6 件結果。
- `yahoo_closed_n1235105710.html` — 已結束拍賣頁（2026-07-01 結標 ¥6,350）。資料在 __NEXT_DATA__ JSON。
