# 多來源抓取層實作計畫

目標：讓 Yahoo! Auctions 直抓、Buyee/Mercari、Buyee/PayPay 三條來源同時供料，
彼此完全獨立失敗；解析出 0 筆時能區分「真的沒貨」與「解析壞了」並告警且不洗版。

前提事實（已實測，不重新驗證）：
Buyee 搜尋頁被 AWS WAF 擋（202 + `x-amzn-waf-action`）；Yahoo 直抓 200 且無 WAF；
Mercari 官方 API 需 DPoP，直抓不可行；Playwright 取得的 `aws-waf-token` 可餵給 httpx，
但硬性 TTL 約 5 分鐘不滑動續期（282s 通、327s 擋）；Yahoo 拍賣 ID 與
`buyee.jp/item/yahoo/auction/{id}` 同一個 ID 空間。

---

## 0. 六個設計問題的決定與理由

### Q1 Site enum 與成本路徑

**現況診斷：`Site` 目前同時是「發現管道」與「購買路徑」，兩者被混用。**

證據：
- `costs.quote_all_routes()` → `cfg.routes_for_site(listing.site.value)`，比對
  `settings.yaml` 中每條 route 的 `sites` 清單。`mercari_tw` 只列 `buyee_mercari`，
  這是**購買路徑**的限制（Mercari TW 只能買 Mercari 貨）。
- `buyee.py` 的 `_SITE_SPEC` 用 `site` 決定商品連結 regex 與搜尋 base URL，
  這是**發現管道**的區分。
- `comps.sold_queries()` 用 `site_name.startswith("buyee")` 決定要不要跑已售出搜尋，
  這是**發現管道**。
- `Listing.key = f"{site}:{external_id}"`，這是**身分／去重**。

在「Buyee 發現 ⇒ Buyee 購買」1:1 的世界裡混用無害。Yahoo 直抓打破 1:1
（Yahoo 發現、Buyee 購買），繼續混用就會出錯。

**決定**

1. `Site` 從此**只表示「購買路徑／市集身分」**。文件字串要改寫講清楚。
2. **不新增 Yahoo 直抓專用的 Site 值**。Yahoo 直抓產生的 `Listing` 掛
   `site=Site.BUYEE_YAHOO`。理由三條：
   - `routes_for_site("buyee_yahoo")` 立刻回傳 `buyee_consolidated` +
     `buyee_single`，成本模型一行都不用改，也不可能算錯。
   - external_id 同一個空間，`Listing.key` 因此穩定：
     同一筆標的無論從哪條管道發現，都是 `signals` 表裡同一列，
     不會重複推播、不會在 comps 裡被算兩次。
   - 若新增 `Site.YAHOO_DIRECT`，就必須在 `settings.yaml` 的兩條 Buyee route
     的 `sites` 裡再加一個字串。漏加的話 `quote_all_routes` 回空 list，
     `scoring.evaluate()` 的 `if not routes: return None` 會**靜默丟掉所有
     Yahoo 標的**——這正是最該避免的失敗型態。
3. **新增發現管道維度**：`Listing.source: str`（例如 `"yahoo_direct"`、
   `"buyee_mercari"`）。這個欄位只做觀測、除錯、告警歸因，
   **絕不參與任何成本計算**。
4. **PayPay 是真正的新市集**（獨立 ID 空間、獨立賣場），所以**新增
   `Site.BUYEE_PAYPAY`**，並同步在 `settings.yaml` 把 `buyee_paypay`
   加進 `buyee_consolidated.sites` 與 `buyee_single.sites`，
   **不得**加進 `mercari_tw.sites`（PayPay 貨走不了 Mercari 台灣）。
5. **加一條防呆測試**：每個 `Site` 成員都必須至少對應一條 route，
   否則測試失敗。

**放棄了什麼**：放棄「用 Site 就能看出資料是誰抓來的」這個便利。
歸因改看 `Listing.source`。這是刻意的：讓成本模型只看一個維度，比省一個欄位重要。

### Q2 Listing 的 url 欄位

**決定**：`Listing.url` **一律放購買端 URL（Buyee）**；新增
`origin_url: str | None` 放發現端原生 URL（Yahoo/PayPay 商品頁）。

理由：
- `notify.format_signal()` 與 dashboard 的卡片連結都直接用 `url`。
  使用者收到推播的下一個動作是「下單」，不是「看日本原站」。
  把 `url` 當購買端，這兩處零修改。
- `origin_url` 的用途：原生頁的照片與賣家評價較完整、拍賣結束時間只有原生頁準、
  排錯時要能回頭看原始來源。次要動作，放第二個欄位剛好。

**schema 影響：不動 `signals` 表**。
- `store.upsert_signal()` 寫固定欄位清單，新欄位不在其中；但 `payload` 欄存
  `sig.to_dict()` 完整 JSON，`asdict()` 自動帶上 `source` 與 `origin_url`。
- 不需要 ALTER TABLE、不需要遷移、既有資料列照常可讀。
- `notify` 要用時從 `row["payload"]` 解 JSON 取，舊資料一律 `.get()`。
- `domain.Listing` 是 `@dataclass(slots=True)`；新欄位必須**加在既有欄位之後
  且帶預設值**。

### Q3 模組切分

**新增檔案**

| 檔案 | 職責 | 允許依賴 |
|---|---|---|
| `sources/yahoo.py` | `YahooAuctionSource`：組 URL、解析 `li.Product`、產出掛 `Site.BUYEE_YAHOO` 的 Listing、判定頁面健康 | `..config`、`..domain`、`.base`、`.health`、bs4 |
| `sources/waf.py` | `WafSession`：Playwright 取 `aws-waf-token`、管理 TTL、產出帶 cookie 的 fetcher | `..config`、`.base`；**Playwright 只在函式內 import** |
| `sources/health.py` | `ParseHealth` enum、`SearchResult` dataclass | `..domain` |
| `alerts.py` | 告警指紋、冷卻、格式化、復原通知 | `.config`、`.store`、`.notify`、`.sources.health` |

**PayPay 不獨立成檔**：仍是 `buyee.jp/paypayfleamarket/...`，parser 與 Mercari 同構，
在 `buyee.py` 的 `_SITE_SPEC` 多一組即可。**分開的是 fetcher，不是 parser**。

**Playwright 選用性三道保證**：
1. `waf.py` 模組頂層不 import playwright，只在函式內 import。
2. `build_sources()` 不建立 WafSession，只把 factory 交給 Buyee/PayPay source。
3. Playwright 缺席時 ImportError 轉成 `BlockedError`，只影響 Buyee/PayPay，
   Yahoo 照常產出。
4. `pyproject.toml` optional group：`browser = ["playwright>=1.40"]`。

**registry 從 Site-keyed 改成 name-keyed**（`dict[str, Source]`）：

| source name | class | fetcher | `.site`（購買路徑） | `.supports_sold` |
|---|---|---|---|---|
| `yahoo_direct` | `YahooAuctionSource` | 純 `CachedFetcher` | `BUYEE_YAHOO` | False |
| `buyee_mercari` | `BuyeeSource` | WAF fetcher | `BUYEE_MERCARI` | True |
| `buyee_paypay` | `BuyeeSource` | WAF fetcher | `BUYEE_PAYPAY` | True（待驗） |
| `ebay` | `EbaySource` | 自帶 httpx | `EBAY` | False |

**移除 `buyee_yahoo` 發現管道**（Yahoo 直抓完全取代，少一條需要 Playwright 的路）。
`Site.BUYEE_YAHOO` 保留（購買路徑）。
`comps.sold_queries()` 的 `startswith("buyee")` hack 改讀 `supports_sold`。
comps 的索引鍵是 `card_signature`，跨站台共用，Mercari 成交價可評估 Yahoo 在架標的。

### Q4 WAF token 的 5 分鐘 TTL

`WafSession` 管理，上層 source 只拿到「會動的 fetcher」。

```
TTL_BUDGET_SECONDS = 240        # 實測 282s 通 / 327s 擋 → 留 42s 邊際
MAX_REFRESHES_PER_RUN = 4       # 防失控迴圈
```

1. 每次 `get()` 前算 `age = monotonic() - acquired_at`，超過預算就**事前重取**。
2. 反應式補救：token 自認新鮮仍被擋 → 重取一次、重試一次，再擋就往上拋。
3. 次數用完 → 後續全拋 `BlockedError`，讓告警層看得見。
4. **token 絕不落地持久化**（4 分鐘壽命，存了只會多一個失敗模式）。
5. **不常駐瀏覽器**：取完就關，重取重開（3-5 秒）。
6. **UA 必須與 CachedFetcher 同一字串**（token 綁 UA 指紋；同源同值同處取得）。
7. `acquire(seed_url)` 回傳 `(token, html)`，第一頁 HTML 由瀏覽器帶回重用。
8. token 年齡進 log（240 的校準依據）。

### Q5 0 筆的判定與告警

判定在 source 層，聚合發送在 `alerts.py`。

```python
class ParseHealth(str, Enum):
    OK = "ok"
    EMPTY_CONFIRMED = "empty"        # 頁面明確說沒有結果 → 不告警
    PARSER_BROKEN = "parser_broken"  # 頁面正常但解析 0 筆 → 告警
    BLOCKED = "blocked"              # WAF / 無 token → 告警
    FETCH_FAILED = "fetch_failed"    # 連線層 transient → 連續 2 次才告警
```

**Yahoo 判定三層**：
1. 命中數交叉比對（最強）：頁面印「約 N 件」，`hits > 0 and parsed == 0` → 必定 PARSER_BROKEN。
   同源同頁同一次抓取的兩個值互相對照。
2. 「一致する商品は見つかりませんでした」→ EMPTY_CONFIRMED。
3. 地標檢查：無 li.Product、無無結果字串、無結果容器 → PARSER_BROKEN。

**Buyee**：`lang=ja` 的日文無結果字串（Phase 0 實地取得）＋命中數交叉比對。
被 WAF 擋走不到 parser（`BlockedError` → BLOCKED），與 0 筆嚴格分開。

**canary**：每來源每天一次 canary 關鍵字（`遊戯王`，`canary_min_results: 10`），
低於門檻直接 PARSER_BROKEN。唯一不會跟著對方改版一起壞的判準。

**告警去重**：`alerts` 表（fingerprint = `{source}:{kind}`），
冷卻序列 `[0h, 72h, 168h, 168h]`；復原必發通知並清列；
FETCH_FAILED 連續 2 次 scan 才告警；每天最多 3 則，超過併成一則。
scan 摘要加一行各來源筆數。

### Q6 測試策略

零網路（MockTransport + 真實 HTML fixture）涵蓋：
build_url、正常頁解析、無結果頁→EMPTY、改壞頁→BROKEN（Yahoo 與 Buyee 各一套）、
canary、route 覆蓋防呆、成本不受 source 影響、ID 正規化一致、
來源隔離（A 拋 BlockedError 時 B 照常）、告警去重與復原（假時鐘）、
WafSession TTL 狀態機（假時鐘＋假 _acquire，不啟動瀏覽器）。

**測不到、只能實跑**：Playwright 是否解得開挑戰、240s 邊際夠不夠、
線上 DOM 漂移、Buyee 無結果字串是否認對、IP 封鎖風險、總耗時。

---

## 1. 檔案異動清單

新增：`sources/yahoo.py`、`sources/waf.py`、`sources/health.py`、`alerts.py`、
`tests/fixtures/*.html` ×5、`tests/test_yahoo_source.py`、`test_source_health.py`、
`test_alerts.py`、`test_source_registry.py`、`test_waf_session.py`。

修改：
| 檔案 | 改什麼 |
|---|---|
| `domain.py` | +`Site.BUYEE_PAYPAY`；`Listing` 尾端加 `source=""`、`origin_url=None`；改 Site docstring |
| `sources/base.py` | `CachedFetcher.set_cookie()`；`Source` protocol 加 `name`/`site`/`supports_sold`/`search_detailed()`（純加法） |
| `sources/buyee.py` | `_SITE_SPEC` +PayPay；移除 BUYEE_YAHOO 搜尋 base（item_url 樣板保留）；`search_detailed()`；無結果判定；設 source/origin_url |
| `sources/__init__.py` | `build_sources()` → name-keyed；WAF factory lazy 注入 |
| `pipeline.py` | 每 (source, query) 包 try/except；`search_detailed()`；watchlist 讀 `sources`；收集 SearchResult 交 alerts；canary |
| `comps.py` | `sold_queries()` 改用 `supports_sold` |
| `notify.py` | +`send_alert()`/`send_recovery()`；summary 加來源行 |
| `store.py` | `_SCHEMA` +alerts 表；三個存取方法 |
| `cli.py` | +`health` 指令；`probe --source` |
| `config.py` | `Config` +`sources: dict`（**必須有預設**，否則 test_fetcher 的 `replace(cfg,...)` 會壞） |
| `settings.yaml` | routes +`buyee_paypay`；+`sources:` 段；notify +告警參數 |
| `watchlist.yaml` | `queries[].sites` 改名 `sources`，值換 `[yahoo_direct, buyee_mercari, buyee_paypay]` |
| `pyproject.toml` | +optional `browser = ["playwright>=1.40"]` |
| `Makefile` | +health target；setup 提示 playwright install |

**既有 57 測試：預期全過、不需修改**。唯一地雷是 `Config` 新欄位沒預設值。
每 Phase 結束跑 `.venv/bin/pytest -q`，failed 必須為 0。

---

## 2. 分階段實作

### Phase 0 — 現場勘查（不寫產品程式碼）
抓 Yahoo 正常頁／無結果頁存 fixture；**確認價格語意**（現在価格 vs 即決価格，
人工比對 5 筆 Buyee 商品頁）；確認命中數 selector、`b=` 分頁、`aucmaxprice`；
Playwright 抓 Buyee `lang=ja` 正常頁與無結果頁、記下日文無結果字串；
確認 PayPay URL 格式與 regex；確認已售出搜尋參數。
完成判準：fixtures 5 個檔到位（ok 頁 `grep -c Product__titleLink` ≥40、
empty 頁含無結果字串）、價格語意結論成文。

### Phase 1 — domain/config 骨架（零網路）
domain/settings/config/health.py＋route 覆蓋與成本不變性測試。
完成判準：pytest 60 passed、ruff 綠、breakeven 數字不變。

### Phase 2 — Yahoo 直抓 source ⚠️ 最高風險

**Phase 0 實測修正（2026-08-01）**：Yahoo 查無結果時回 **HTTP 404 + 完整頁面**
（197KB，含「一致する商品は見つかりませんでした」）。`CachedFetcher._check` 會把
404 當語意失敗拋 `FetchError`，於是「真的沒貨」會被誤判成「抓取失敗」。
修法：`CachedFetcher.get()` 加 `allow_statuses: tuple[int, ...] = ()` 參數，
名單內的狀態碼跳過狀態檢查、照走 body 長度檢查後回傳 body；
Yahoo source 呼叫時帶 `allow_statuses=(404,)`，並由 parser 層用無結果字串判定
EMPTY_CONFIRMED。其他來源不受影響（預設空 tuple 行為不變）。
404 頁**不進快取**（它是「當下沒有結果」，不是穩定內容）——實作時 allow_statuses
命中的回應跳過 `_write_cache`。
價格決策：即決価格 → `price`＋`price_kind=buyout`；
純競標 → `price_kind=current_bid` 且**預設排除**（`include_live_auctions: false`）
——現在出價不是付得出去的價格，混進去會產生系統性偏低的假 FREE_CARD。
放棄競標便宜貨：一天跑一次本來就趕不上競標尾盤。
完成判準：test_yahoo_source 全綠；probe 實跑 count≥20、url 是 buyee、
價格 300–5M；人工抽 3 筆比對兩邊同商品同價格。

### Phase 3 — pipeline 來源隔離與 registry 改造
build_sources name-keyed；`_scan_source` 整段 try/except；comps 改 supports_sold；
watchlist 改 sources。
完成判準：pytest 全綠；`scan --dry-run` 在 Buyee 全拋 BlockedError 時
scanned 仍 >0、exit 0。**這步就滿足「任一來源壞、其他照跑」。**

### Phase 4 — 0 筆判定與告警
alerts 表＋AlertEngine＋notify＋canary＋`health` 指令。
完成判準：測試全綠；**人工端到端**：把 li.Product 改壞 → scan → Telegram 真的
收到告警；改回 → 收到復原通知、alerts 表清空。此步不可跳過。

### Phase 5 — WafSession + Buyee/PayPay 接線
WafSession（lazy import、TTL 狀態機、次數上限、年齡 log）；set_cookie；
_SITE_SPEC +PayPay；接上 registry。
完成判準：pytest 全綠；`import ygo_sniper.sources` 後 `'playwright' not in sys.modules`；
`scan --dry-run` 兩個 Buyee 來源 count>0、log 見 token 重取、無 BlockedError 逃頂；
第二次跑走快取數秒完成不開瀏覽器。

### Phase 6 — Yahoo closedsearch 當 comps ✅ 已實作（2026-08-01）
`sources/yahoo_closed.py`：`YahooClosedSource`，`supports_sold=True`、
`site=Site.BUYEE_YAHOO`，只被 `refresh_comps` 用（不在 watchlist 的
`queries[].sources`，也不在 settings.yaml 的 `sources:` 段 → 不參與在架掃描與 canary）。

實測結論全部落在 `tests/fixtures/RECON.md §6`，三個與原計畫不同的關鍵發現：

1. **解析走 `__NEXT_DATA__` JSON，不是 CSS selector**。closedsearch 是
   Next.js + styled-components，class 名是每次 build 都變的雜湊，
   `li.Product` 在這頁根本不存在。頁面內嵌的 SSR JSON 反而穩定且有語意。
2. **得標 vs 流標靠 `bidCount >= 1`**。流標商品的 `price` 欄是**開始価格**
   （實測 `g1237930015`：bids=0、price=500=initPrice），混進行情表會系統性
   拉低中位數。實測 closedsearch 本身就只列落札成功的（200 筆抽樣 0 筆
   bidCount=0；兩筆剛結標的 0 入札標的查不到），但 parser 仍硬性把關。
3. **`sold_at` 用 `endTime`（換算 UTC），不是 now()**。closedsearch 視窗 180 天、
   comps 視窗 90 天，蓋成 now() 等於拿半年前的價格當現在的行情。

同時做的：`comps_queries` 組合展開（稀有度 × 年代 × 機構，config 在
`watchlist.yaml`）＋ `every_n_runs` 節流（計數器落在 store 的 `meta` 表，
跨行程有效）＋ comps 專屬的 `pages`（與在架掃描的 `max_pages_per_query` 分開）。

---

## 3. 風險排序

**風險 1（最高）：Yahoo 價格語意搞錯（Phase 2）**——靜默、方向偏低、看起來像成功。
現在出價當可成交價 → 假 FREE_CARD 大量誤發；若進 comps 還會污染其他來源（90 天視窗清不掉）。
偵測：Phase 0 人工比對（事前）、Phase 2 抽 3 筆（交付前）、
永久不變量「單一來源 FREE_CARD 佔比 >20% → 告警」、SUSPICIOUS_CHEAP 集體亮 → 幾乎必然價格解析錯。
隔離：include_live_auctions 預設關，實跑一週再考慮開。

**風險 2：external_id 正規化不一致**——大小寫分岔 → 同標的兩列、推播兩次。
偵測：測試釘兩個抽取函式等價；上線後 SQL 查 `GROUP BY LOWER(external_id) HAVING COUNT(*)>1`。

**風險 3：TTL 240s 不夠**——大聲失敗，log 印 token 年齡；連兩天觸發反應式重取就降 180。

**風險 4：Buyee 無結果字串認錯**——誤報靠使用者可見（health 顯示天天 EMPTY 但 canary 正常）；
漏報靠 canary 兜底。

---

## 4. 一句話總結

`Site` 只管錢（購買路徑），`source` 只管料（發現管道）；
Yahoo 走純 httpx、Buyee/PayPay 走 WafSession，兩條線在 `build_sources()` 才交會，
交會處只是一個 fetcher 物件；每個來源自己判定健康，`alerts.py` 統一去重發聲；
canary 是唯一不會跟著對方改版一起壞掉的判準。
