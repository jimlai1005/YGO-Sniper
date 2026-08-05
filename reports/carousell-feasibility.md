# Carousell HK / SG 現場勘查（Phase 0）

日期：2026-08-05　範圍：`carousell.com.hk` / `carousell.sg`
探測腳本與樣本：scratchpad `pw_probe.py` / `pw_detail.py` / `pw_sgdetail.py` / `pw_count.py`，
HTML 樣本 `detail_00..24.html`（HK 25 筆）、`sgdet_00..21.html`（SG 22 筆）。
**未寫任何產品程式碼、未改 `src/`、未動 DB。**

## 結論：不可接（NO-GO）

1. **貨到不了台灣。實測 47 筆遊戲王鑑定卡 listing，可寄台灣的是 0 筆。**
   兩站的官方寄送依政策都只送本地地址；抽樣中最大宗的交易方式是**實體面交**
   （HK 10/25、SG 19/22），其餘是本地取件點／本地宅配。
2. **沒有「可否國際寄送」這個可解析欄位——因為平台上不存在這個概念。**
   47 筆的 `deliveryPoint.addressCountry` 全部是 `HK` 或 `SG`，無一例外。
3. **要判斷可否寄送，必須逐筆開商品頁**（搜尋頁完全不含交易方式），
   而全站被 Cloudflare managed challenge 擋住、只有 Playwright 進得去
   ——等於每個關鍵字每輪要跑約 48 次瀏覽器頁載，只為了確認答案是「不能寄」。

**這正是 `CLAUDE.md` 第三節「套利路徑」事故的形狀**：價差存在（SG 的 1999 Premium Pack
Exodia PSA 9 只要 S$300），但那批貨拿不到，價差是假的。

---

## 1. 貨到得了台灣嗎？（GO/NO-GO 關鍵）

### 官方寄送機制

| 站 | 機制 | destination 限制 | 來源 |
|---|---|---|---|
| SG | Carousell Official Delivery（J&T / SPX / SingPost / Ninja Van） | 「shipping is restricted to addresses in **Singapore only**」 | [Singapore Shipping Policy](https://support.carousell.com/hc/en-us/articles/360001535808--Singapore-Shipping-Policy) |
| HK | Carousell 官方合作快遞＝**順豐速運**，由 HK$16 起 | 商品頁原文「**任何順豐速運提取點**」＋「3-5 個工作天 · 可追蹤」——買家到 HK 的順豐點自取 | `detail_00.html` 交易方式區塊（實測） |

HK 的機制字面是「自取: 順豐速運」——**是取件點自取，不是寄送到府**，
買家必須人在香港才取得到。順豐本身雖有港→台線路，但那不是 Carousell 結帳流程的選項。

### 抽樣實測（這是驗收條件 1 要求的實際數字）

**HK 25 筆**（`遊戲王 PSA` 搜尋結果前 25 筆商品頁，`detail_summary.json`）：

| 交易方式（`交易方式` 欄位原文） | 筆數 | 可寄台灣 |
|---|---|---|
| `自取: 順豐速運`（Carousell 官方合作快遞，HK 取件點） | 12 | ✗ |
| `面交`（免費，港島／九龍／新界地點） | 10 | ✗ |
| `賣家自訂送貨方式`（1-2 日可追蹤 / 6-7 日未追蹤，皆本地） | 2 | ✗（未標國際） |
| 無此區塊（一筆日本代購服務廣告，非實體卡） | 1 | n/a |
| **合計** | **25** | **0** |

**SG 22 筆**（`yugioh psa 1999` 全部 12 筆 ＋ `yugioh psa` 前 10 筆，`sgdet_*.html`）：

| 交易方式（`Deal method` 欄位原文） | 筆數 | 可寄台灣 |
|---|---|---|
| `Meet-up`（Farrer Park MRT、Bugis MRT、blk 37 Bedok…） | 19 | ✗ |
| `Doorstep delivery / Carousell Official Delivery S$3.60` | 3 | ✗（政策限 SG 地址） |
| **合計** | **22** | **0** |

**47 筆合計，可寄國際 0 筆。** 全部 47 筆的 `addressCountry` ∈ {HK, SG}。

### 可解析欄位（任務問的「它在 HTML/JSON 裡叫什麼」）

好消息是欄位存在且結構化，壞消息是它回答的問題不是我們要的：

- **`application/ld+json` 的 `Product.offers.deliveryPoint[].address.addressCountry`**
  ——但它是**面交／取件地點的國別**，不是「可寄達的國別」。實測恆為 HK 或 SG。
- DOM 上的 `交易方式` / `Deal method` 區塊（HK 中文、SG 英文），
  文字值即 `面交` / `自取: 順豐速運` / `賣家自訂送貨方式` / `Meet-up` / `Doorstep delivery`。
  **class 名是雜湊過的**（`D_btU M_cQc`），每次前端部署都會變 → 只能靠文字錨點，脆弱。
- **這個區塊只在商品頁，不在搜尋頁**（搜尋頁全語料 `面交`/`郵寄`/`順豐`/`Meetup` 命中數皆為 0）。

### 轉運方案？

無官方國際寄送，只剩兩條都不可自動化的路：
(a) 買家在 HK/SG 有代收地址（forwarder），但 `面交`（29/47）連地址都不收，只能當面交易；
(b) 私訊賣家議定寄送——只有 2/47 是 `賣家自訂送貨方式`，且需逐筆人工聊天。
**推測**：即使全部改用 forwarder，可觸及的池子上限也只有那 15/47 的取件點／宅配類，
且要先解決「取件點自取需本人在港」的問題。

---

## 2. 抓得到嗎？

**實跑證據（驗收條件 2）**：

| 目標 | 工具 | 結果 |
|---|---|---|
| `carousell.com.hk/search/yugioh psa` | curl | **403**, 5,766 B, `<title>Just a moment...</title>` |
| HK / SG 搜尋頁 | httpx + HTTP/2 + 完整瀏覽器 headers | **403** 兩站皆 Cloudflare challenge |
| `carousell.sg/sitemap.xml` | httpx | **403** cf challenge |
| `carousell.com.hk/`（首頁） | httpx | **403** cf challenge |
| `/api-service/filter/search/*`（robots 明示 Allow） | httpx | **403** cf challenge |
| `api.carousell.com/*` | httpx | 404（非 challenge，但無可用路徑） |
| `support.carousell.com`（說明中心） | httpx | **403** cf challenge |
| 任一 Carousell URL | **WebFetch** | **403 Forbidden** |
| `robots.txt` | httpx | **200**（唯一放行的路徑） |
| HK 搜尋頁 | **Playwright headless chromium** | **200**，509 KB，標題正常，48 筆 |

- **反爬：Cloudflare managed challenge，全站啟用**（首頁、sitemap、api-service、
  說明中心一律 403）。httpx 直抓完全不可行——比 Buyee 的 AWS WAF 更嚴格，
  因為 Buyee 至少 token 拿到後可以用 httpx 續跑，Carousell 這裡連 sitemap 都進不去。
- **Playwright 可以解開**（headless + `navigator.webdriver` 遮蔽即通過，25/25 商品頁皆 200）。
  但這代表**每一次抓取都要付瀏覽器成本**，且沒有 token 可以轉交給 httpx。
- **無公開／內部 JSON API 可取**：搜尋頁**沒有** `__NEXT_DATA__`、`__INITIAL_STATE__`、
  `__APOLLO_STATE__`、`window.__*`（全部命中 0），是 SSR React ＋ 雜湊 class 名。
  監聽整個頁載期間的 `api-service`/`graphql` XHR：**0 個**。資料只能從 HTML 硬解。
- 搜尋 URL 格式：`https://www.carousell.com.hk/search/{urlencoded keyword}`，
  分類頁 `.../categories/toys-collectibles-12/trading-cards-9001/graded-singles-9012/`。
  **有專屬「鑑定單卡」分類（id 9012）與 `Tcg Game: Yu-Gi-Oh 遊戲王` 結構化屬性**，
  篩選能力其實不錯——這是本次勘查唯一明確的正面發現。
- 分頁：每頁約 47-48 筆，未驗證翻頁參數（因結論已定，未續查）。

**⚠️ robots.txt 政策阻斷（獨立於技術可行性）**：兩站 `User-agent: *` 皆含
`Disallow: /search/` 與 `Disallow: /*?`。也就是**搜尋頁明文禁止爬取**。
（`Allow: /api-service/*?` 有放行，但該路徑實測被 Cloudflare 403。）
SG 的 robots 另有一組把 `ClaudeBot` / `GPTBot` 等列為 `Allow: /`，但本專案的 scraper 不屬於那組。

---

## 3. 價格語意

- **一口價，無競標機制**。ld+json `offers.price` 是現售價，
  `offers.priceSpecification.price` 是原價（劃線價）。
  實例 `sgdet_00`：`price: 900, priceCurrency: SGD` ＋ `priceSpecification.price: 1000`
  → 卡片顯示「S$900 S$1,000」。**取 `offers.price`，不是 priceSpecification**（後者是原價）。
- **議價文化強**（Carousell 以 chat 議價為主），所以標價是「賣家開價」而非成交價。
  未發現結構化的 "or nearest offer" 欄位（`ono` 一詞的命中經查證是 CSS `monospace` 的子字串，
  **非議價訊號**——這條差點被誤讀成證據）。
- **沒有已售出資料可當 comps**：47 筆樣本的 `offers.availability` **全部是 `InStock`**
  （49/49 命中，0 個 SoldOut）。Carousell 售出即下架，無公開成交價查詢。
  → 這個來源 **`supports_sold = False`**，無法餵 `comps.py`，只能當單向的「發現管道」。
- **幣別 HKD / SGD，兩者都不在 `Currency` enum**（`domain.py:67-70` 只有 JPY/USD/TWD）。
  依 `costs.py:85` 的判準（「判準不是幣別，是這筆錢會不會以外幣請款」）：
  台灣買家若真能買，必然是刷卡以 HKD/SGD 請款 → **要套 `card_markup`**，
  與 eBay 同類、與 Mercari TW 相反。**推測**：實務上台灣買家根本走不完結帳流程
  （需本地地址或面交），所以這個問題目前是純理論的。

---

## 4. 量夠不夠

實測筆數（Playwright，各查詢實際列出的商品連結去重數）。
**≤45 筆代表未觸及分頁上限＝該查詢的完整結果集**；48/47 筆是頁面容量上限、被截斷。

| 站 | 查詢 | 筆數 | 說明 |
|---|---|---|---|
| HK | `遊戲王 PSA` | 48 | 截斷 |
| HK | `遊戲王 PSA 1999` | **14** | 完整集 |
| HK | `遊戲王 PSA 初期` | **7** | 完整集 |
| HK | `遊戲王 PSA 2002` | **7** | 完整集 |
| HK | `遊戲王 ARS` | 48 | 截斷 |
| SG | `yugioh psa` | 47 | 截斷 |
| SG | `yugioh psa 1999` | **12** | 完整集，**12/12 都在 1998-2004 window** |
| SG | `yugioh psa vintage` | **15** | 完整集 |
| SG | `yugioh psa 2002` | **9** | 完整集 |
| SG | `yugioh ars` | **11** | 完整集 |

年代命中率：HK `遊戲王 PSA` 那 48 筆逐筆讀過，真正落在 1998-2004 的約 **6-7 筆（≈13%）**，
其中一筆還是 `《純分享》1999年 青眼白龍 初期無角 PSA 10`、開價 HK$1,999,999
——**炫耀貼不是賣品**（Carousell 特有的噪音類型，需額外排除規則）。

**SG 的老卡池明顯優於 HK**，且成色具體：1999 Premium Pack Exodia PSA 9 S$300、
1999 Dancing Elf Premium Pack PSA 8 S$30、1999 Meteor Dragon PSA 9 S$65、
1999 Black Skull Dragon PSA 10 S$2,500、PSA8 1998-1999 Bandai 1st Gen S$218、
2002 Dark Magician 1st Ed SDY-006 PSA 9 S$550。**這批貨確實是使用者要的東西，價格也不貴。**

各查詢重疊嚴重，**推測**兩站的「隨時在架的 1998-2004 鑑定卡」各約 30-60 筆量級。
量本身不是致命傷——致命傷是第 1 題。

---

## 5. 接進去要動哪些地方（若日後條件改變）

| 檔案:行號 | 要改什麼 |
|---|---|
| `src/ygo_sniper/domain.py:18-45` | `Site` enum 加 `CAROUSELL_HK` / `CAROUSELL_SG`（ID 空間不同、幣別不同，必須分兩個，理由同 `MERCARI_TW` 那段註記） |
| `src/ygo_sniper/domain.py:67-70` | `Currency` 加 `HKD` / `SGD` |
| `config/settings.yaml:11-18` | `fx:` 加 `hkd_twd` / `sgd_twd`；`fx.py` 線上更新來源要跟著加 |
| `config/settings.yaml:25-` | `routes:` 新增路徑。**注意結構性障礙**：現有 route 的費用欄位全是 `*_jpy`（`purchase_fee_jpy`/`intl_ship_jpy`…），而 `costs.py:55-56` 把它們寫死用 `Currency.JPY` 換算。HK/SG 路徑的費用是 HKD/SGD，**不是加一條 route 就好，要先把 route 費用欄位改成帶幣別的** |
| `tests/test_source_registry.py:18-19` | 該測試 `for site in Site` 斷言每個 Site 都有 route——新增 Site 而不配 route 會直接紅燈（這道防呆是對的，別繞過） |
| `src/ygo_sniper/sources/base.py:20-25` | `Source` protocol；新來源需 `supports_sold = False`（見第 3 題） |
| 新增 `sources/carousell.py` | 參考 `sources/yahoo.py`；但**不能只用 `CachedFetcher`**，必須走 `sources/waf.py` 那種瀏覽器路徑 |
| `src/ygo_sniper/sources/waf.py` | 現有是 AWS WAF token 模型（拿 token 給 httpx 用）。Cloudflare 這裡**沒有可轉交的 token**，整個抓取都要在瀏覽器內完成——這是新的失敗模式，不是既有模組的參數調整 |
| `costs.py:69-100` | HK/SG 需要 markup（外幣請款），語意與 `_quote_ebay` 同類 |

---

## 最強的反面證據（驗收條件 5）

**支持「不接」的證據**（依強度排序）：

1. **47/47 筆不可寄台灣**，且不是抽樣誤差——是平台的產品設計（C2C 本地市集）。
2. **平台政策白紙黑字**：SG「Singapore only」、HK 官方快遞是本地取件點自取。
3. **robots.txt 兩站都 `Disallow: /search/`**——即使技術上繞得過，這是明示禁止。
4. **成本／收益極不對稱**：搜尋頁沒有交易方式欄位 → 每筆都要開商品頁 →
   每關鍵字每輪約 48 次 Playwright 頁載，而排程一天 15 個觸發點。
   付出全專案最高的抓取成本，換到的每一筆結論都是「不能寄」。
5. **無成交價資料**（availability 恆為 InStock），連當純 comps 來源的退路都沒有。
6. **解析面脆弱**：雜湊 class 名（`D_btU M_cQc`）＋ 純文字錨點，
   對方一次前端部署就會壞——而依 `CLAUDE.md` 第五節，這種壞法是**靜默的**。
7. 額外噪音類型：`《純分享》` 炫耀貼、代購服務廣告、卡套／收納盒等周邊，
   接進來就要為它們寫新的排除規則，而每條排除規則都有誤殺真卡的風險（第一節）。

**支持「接」的證據（誠實列出，這是這份報告裡最該被質疑的部分）**：

1. **貨真的好也真的便宜**：SG 的 1999 Premium Pack、Bandai 1st Gen、SDY-006 1st Ed
   都是目標品，S$30-550 的價位對照日本市場並不貴。使用者說「市場很熱絡」是對的。
2. **結構化資料品質意外地高**：`Tcg Grade Cert Number`（PSA 認證編號！）、
   `Tcg Card Number`（如 `DDS-004`）、`Tcg Game`、`Tcg Language` 都是 ld+json 欄位，
   比現有任何一個來源都乾淨——若能抓，比對品質會很好。
3. **有專屬「鑑定單卡」分類（9012）**，篩選精度優於關鍵字。
4. **Playwright 實測 47/47 頁面 200**，技術上並非不可能，只是貴。

**這些反面證據不足以推翻結論**，理由是它們全部建立在「貨拿得到」這個前提上，
而那個前提被第 1 題的 47/47 直接否定。**買不到的便宜貨不是 alpha，是噪音。**

---

## 若要重啟這個評估，觸發條件是

1. 使用者在 HK 或 SG 有可收貨的地址／代收人（則 15/47 的取件點＋宅配類變成可觸及）；或
2. Carousell 推出跨境寄送（屆時 `deliveryPoint.addressCountry` 會出現 TW，可自動偵測）；或
3. 使用者本人要去 HK/SG——那時這份報告的第 4 節就是現成的採買清單，
   SG 的 vintage 池特別值得看。

**建議的低成本替代**：不接成自動來源，改成需要時人工開 `carousell.sg/search/yugioh psa 1999`
看一眼（完整集只有 12 筆，人工掃描成本極低）。
