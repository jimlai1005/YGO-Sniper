# 排程空窗補跑模式 ＋ yahoo_direct 改版修復 Implementation Plan

> **For agentic workers:** 主線程逐 task 派 `builder`（@inline），每個 task 的驗收指令由主線程親跑確認後才派下一個。Steps 用 `- [ ]` 追蹤。
> 實作者只讀本檔，不依賴規劃對話。專案紅線在 `CLAUDE.md`（自動載入），動手前讀第一、二、三、五、六節。

**Goal:** (A) 修好自 2026-10-05 起每輪 `parser_broken` 的 `yahoo_direct` 搜尋頁解析（Yahoo 改成 `__NEXT_DATA__`）；(B) 排程漏跑之後，系統自動以「補跑模式」掃描（多翻一頁、強制重挖狙擊成交、推一則「漏跑期間狙擊卡成交回顧」），並在 dashboard 提供一顆走生產路徑的「補跑 daily」按鈕與漏跑狀態顯示。

**Architecture:**
- A 部分：搜尋頁與賣家頁現在是**同一種 JSON 節點**（路徑 `props.pageProps.initialState.search.items.listing`，欄位 `auctionId/title/price/buyNowPrice/bidCount/endTime/isFleamarketItem/seller.userId/imageUrl`）。把 `yahoo.py` 既有的賣家頁解析（`_seller_nodes`／`_judge_seller_health`／`_extract_seller_listings`）泛化成兩頁共用，刪掉 `li.Product` 那一套 CSS 解析。舊結構的頁面必須判成 `PARSER_BROKEN`（不是靜默 0 筆）。
- B 部分：`schedule_watch` 新增一本**漏跑視窗帳**（meta 鍵 `schedule_gap_window`，JSON），由 `pipeline._update_schedule_state` 在偵測到漏格時寫入；`Pipeline.scan()` 在「帳上有未補跑的視窗且漏格數 ≥ 門檻」或 `catchup=True` 時進入補跑模式（每個發現查詢多翻 N 頁）；`cli.daily` 在補跑模式下強制重挖狙擊成交、推回顧訊息，**推播確認送達後**才在帳上標 `caught_up_at`（與既有「送達才消耗」規則一致）。dashboard 按鈕用 `subprocess` 跑 `scripts/run_daily.sh --catchup`——走與 launchd 完全相同的生產路徑（鎖、watchdog、log、ledger 全部免費繼承）。
- **同基準條款**：排程帳的時間是**本地 naive** ISO（`datetime.now()`），`card_watch_sale.sold_at` 是 **UTC aware** ISO（`+00:00`）。篩「視窗內成交」前必須把視窗轉成 UTC aware 再比，不得直接比字串。

**Tech Stack:** Python 3.12、BeautifulSoup（只用來找 `<script id="__NEXT_DATA__">`）、httpx `MockTransport` 測試、FastAPI + `TestClient`、SQLite meta 表、bash（run_daily.sh）。

**預先量好的事實（實作時不要重量，直接引用）：**
- Yahoo 搜尋頁 2026-10-20 實抓（httpx，282,961 bytes）：`li.Product` 0 個；`__NEXT_DATA__` 存在；`search.items.listing.items` 50 筆；`listing.totalResultsAvailable = 7999`。查無結果頁：HTTP **404**＋完整頁面，`listing.items = []`、`totalResultsAvailable = 0`（與 `yahoo_closed.py:42` 的描述一致）。
- 各來源「每小時新上架（首見）」近 14 天：中位 1–2 筆／時、p90 ≤ 13、最大 29（ebay）；每日最大 43（ebay）、24（yahoo）、27（paypay）、19（mercari）。頁容量 Yahoo 50／Mercari 100／PayPay 40。→ 補跑模式**多翻 1 頁**就足以覆蓋一整天的空窗，不需要更多。
- `card_watch_sale` 的 `sold_at`：`yahoo_closed`／`paypay_direct` 全有值；`buyee_mercari`／`ruten` **全為空字串**（1,358／16 筆），篩選時必須排除空值，不得補假時間。
- tier 分布：`exact` 29、`partial` 93、`near` 2,624。回顧訊息只列 `exact`＋`partial`，`near` 只給一個筆數。
- 2026-10-19 的空窗（本地 09:30 ～ 20:35）內：`exact/partial` 成交 0 筆；`near` 3 筆（去重 `sale_key` 後；watch 1 與 9 是同卡兩個鑑定目標，各記一次）。2026-10-17 本地整天：watch 2 有 1 筆 `partial`（`sold_at = 2026-10-17T13:27:11+00:00`，¥6,250）。這兩個是 Task 5 的驗收錨值。

---

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `src/ygo_sniper/sources/yahoo.py` | Yahoo 搜尋頁＋賣家頁 source | 修改：搜尋頁改 JSON，與賣家頁共用節點／健康／抽取；刪 li 解析 |
| `tests/fixtures/yahoo_search_ok.html`、`yahoo_search_empty.html` | 生產路徑實抓的新頁面 | 覆寫（重抓） |
| `tests/fixtures/yahoo_search_legacy_li_product.html` | 舊結構（改版前）頁面 | 新增（由舊 `yahoo_search_ok.html` 改名而來） |
| `tests/test_yahoo_source.py` | 搜尋頁測試 | 修改：樣本 id 換新 fixture、新增「舊結構＝PARSER_BROKEN」「JSON 交叉比對」 |
| `src/ygo_sniper/schedule_watch.py` | 排程網格與漏跑偵測（純函式） | 修改：抽出 `detect_missed`，新增漏跑視窗帳的純函式與鍵名 |
| `src/ygo_sniper/pipeline.py` | 掃描管線 | 修改：寫漏跑視窗帳、`scan(catchup=)`、補跑模式多翻頁、result 帶 `catchup` |
| `src/ygo_sniper/catchup.py` | 補跑模式的「視窗內成交」查詢與訊息格式（純函式＋一個 store 查詢） | 新增 |
| `src/ygo_sniper/cli.py` | `daily --catchup`、強制重挖、推回顧、標記已補跑 | 修改 |
| `config/settings.yaml` | `scan.catchup_extra_pages`、`scan.catchup_min_missed_slots` | 修改 |
| `scripts/run_daily.sh` | 透傳參數給 `ygo-sniper daily` | 修改（一行） |
| `web/app.py` | `POST /api/daily-run`、`GET /api/schedule/gap` | 修改 |
| `web/static/index.html` | 第三顆按鈕＋漏跑狀態 pill | 修改 |
| `tests/test_schedule_watch.py`、`tests/test_catchup.py`、`tests/test_daily_run_web.py`、`tests/test_pipeline_catchup.py` | 測試 | 新增／修改 |
| `CLAUDE.md`、`docs/run_daily.md` | 文件 | 修改 |

---

## Part A — yahoo_direct 搜尋頁改版修復

### Task 1 @inline：用生產路徑重抓 fixture，保留舊結構頁面

**Files:**
- Rename: `tests/fixtures/yahoo_search_ok.html` → `tests/fixtures/yahoo_search_legacy_li_product.html`
- Create（覆寫）: `tests/fixtures/yahoo_search_ok.html`、`tests/fixtures/yahoo_search_empty.html`
- Delete: 舊 `tests/fixtures/yahoo_search_empty.html`（直接被新檔覆寫即可）

背景：CLAUDE.md 第六節——fixture 一律用**生產路徑**抓。`yahoo_direct` 的生產路徑是 `CachedFetcher.get()`（httpx，不跑 JS），所以用它抓，不用瀏覽器、不用 curl。

- [ ] **Step 1: 改名舊 fixture**

```bash
cd /Users/jim/projects/ygo-sniper
git mv tests/fixtures/yahoo_search_ok.html tests/fixtures/yahoo_search_legacy_li_product.html
```

- [ ] **Step 2: 用 CachedFetcher 抓新頁面**（腳本放 scratchpad，不進 repo）

```python
# /private/tmp/claude-501/-Users-jim-projects-ygo-sniper/1b833219-b2a6-45a1-ab6c-0b3f294bfa64/scratchpad/yahoo/grab_fixtures.py
from dataclasses import replace
from pathlib import Path
from ygo_sniper.config import load_config
from ygo_sniper.sources.base import CachedFetcher
from ygo_sniper.sources.yahoo import YahooAuctionSource

cfg = load_config()
cfg = replace(cfg, storage={**cfg.storage, "cache_dir": "/tmp/ygo-fixture-cache-ignore"})
f = CachedFetcher(cfg)
src = YahooAuctionSource(cfg, f)
out = Path("tests/fixtures")
ok_url = src.build_url("遊戯王 psa 初期", page=1)
empty_url = src.build_url("zzqxv9988nonsense", page=1)
ok = f.get(ok_url, allow_statuses=(404,))
empty = f.get(empty_url, allow_statuses=(404,))
assert '__NEXT_DATA__' in ok and '"totalResultsAvailable"' in ok, "ok 頁沒有 NEXT_DATA"
assert '__NEXT_DATA__' in empty and '"totalResultsAvailable":0' in empty.replace(" ", ""), "empty 頁不是 total=0"
(out / "yahoo_search_ok.html").write_text(ok, encoding="utf-8")
(out / "yahoo_search_empty.html").write_text(empty, encoding="utf-8")
print("ok bytes", len(ok), "empty bytes", len(empty))
f.close()
```

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/python /private/tmp/claude-501/-Users-jim-projects-ygo-sniper/1b833219-b2a6-45a1-ab6c-0b3f294bfa64/scratchpad/yahoo/grab_fixtures.py`
Expected: 兩個 bytes 數都 > 50,000，無 assert 失敗。若 `empty` 抓回來不是 404 完整頁（例如被擋），停下來回報，不要用別的方法湊。

- [ ] **Step 3: 從新 ok fixture 挑三個樣本 id 寫進測試常數**（下一個 task 要用）

```bash
cd /Users/jim/projects/ygo-sniper && .venv/bin/python - <<'EOF'
import json, re
html = open("tests/fixtures/yahoo_search_ok.html", encoding="utf-8").read()
d = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S).group(1))
items = d["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]["items"]
buyout = next(i for i in items if i.get("buyNowPrice") and not i.get("isFleamarketItem"))
pure = next(i for i in items if not i.get("buyNowPrice") and i.get("price") and not i.get("isFleamarketItem"))
print("BUYOUT_ID", buyout["auctionId"], buyout["price"], buyout["buyNowPrice"], buyout.get("endTime"), buyout.get("bidCount"))
print("PURE_BID_ID", pure["auctionId"], pure["price"], pure.get("endTime"), pure.get("bidCount"))
print("total", d["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]["totalResultsAvailable"], "n", len(items))
EOF
```

把印出的 id／價格記下來，Task 2 的測試常數用這些值（不要沿用舊的 `s1238539612`／`m1238496717`，那是舊 fixture 的）。

- [ ] **Step 4: 確認 fixture 守門測試仍綠**（有一條防 Playwright fixture 的結構性測試只管 Buyee，這裡只是確認沒有殃及）

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_fixtures_guard.py -q 2>/dev/null || .venv/bin/pytest tests -q -k "fixture and buyee" --co -q | tail -3`
Expected: 不紅（若檔名不同，以 `grep -rl loading-spinner tests/*.py` 找到那支測試跑它）。

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/yahoo_search_ok.html tests/fixtures/yahoo_search_empty.html tests/fixtures/yahoo_search_legacy_li_product.html
git commit -m "test(yahoo): 以生產路徑重抓改版後的搜尋頁 fixture，舊結構頁面改名保留"
```

---

### Task 2 @inline：搜尋頁改用 `__NEXT_DATA__`，與賣家頁共用解析

**Files:**
- Modify: `src/ygo_sniper/sources/yahoo.py`（`search_detailed` 302-371、`_judge_health` 374-395、`_parse_hits` 397-409、`_extract_listings` 412-479、`_seller_nodes` 606-623、`_judge_seller_health` 626-650、`_extract_seller_listings` 677-746、li 版的 `_parse_auction_meta`／`_parse_seller`／`_parse_prices`／`_parse_image` 749-821）
- Modify: `tests/test_yahoo_source.py`

**必須保住的行為（既有測試名稱，改寫但不能刪）：**
1. 即決 vs 純競標分流走 `_classify_price`（`test_parse_ok_page_buyout_only`、`test_include_live_auctions_emits_current_bid`）
2. 404 查無結果 → `EMPTY_CONFIRMED` 且**不進快取**（`test_empty_404_page_is_empty_confirmed`、`test_404_response_is_not_cached`）
3. 頁面說有 N 件但解析 0 筆 → `PARSER_BROKEN`（`test_hits_crosscheck_catches_dead_selector`：改成 JSON 版——`totalResultsAvailable > 0` 但 `items` 為空）
4. 結構全變 → `PARSER_BROKEN`，`parsed_count = 0`（`test_renamed_product_classes_is_parser_broken` 改名為 `test_legacy_li_product_page_is_parser_broken`，餵 `yahoo_search_legacy_li_product.html`；`test_parsed_count_is_zero_when_parser_dies`）
5. `parsed_count` 在商業篩選之前（`test_parsed_count_is_before_commercial_filter`、`test_parsed_count_unchanged_when_filter_opens`）
6. `end_time`／`bids` 來自 JSON 欄位、抽不到是 None 不猜（`test_end_time_and_bids_are_parsed`、`test_missing_end_time_is_none_not_guessed`）
7. `external_id` 與 Buyee 的抽取一致（`test_external_id_matches_buyee_extraction`）
8. 排序參數上線（`test_sort_param_actually_reaches_the_wire` 等 build_url 測試）不變

- [ ] **Step 1: 寫失敗測試——新 fixture 解析得出商品、舊結構判 PARSER_BROKEN、JSON 交叉比對**

在 `tests/test_yahoo_source.py` 加（常數用 Task 1 Step 3 印出的值）：

```python
LEGACY_HTML = (FIXTURES / "yahoo_search_legacy_li_product.html").read_text(encoding="utf-8")


def _nextdata_payload(html: str) -> dict:
    import json, re
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def _html_with_listing(html: str, **overrides) -> str:
    """把 ok fixture 的 listing 節點改掉再塞回頁面（測交叉比對用）。"""
    import json
    payload = _nextdata_payload(html)
    node = payload["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]
    node.update(overrides)
    head, _, rest = html.partition('<script id="__NEXT_DATA__"')
    _, _, tail = rest.partition("</script>")
    return (head + '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False) + "</script>" + tail)


def test_nextdata_page_parses_listings(make_source, cfg):
    src = make_source(lambda req: httpx.Response(200, text=OK_HTML))
    res = src.search_detailed(KEYWORD, pages=1)
    assert res.health is ParseHealth.OK, res.detail
    assert res.parsed_count >= 40
    ids = {l.external_id for l in res.listings}
    assert BUYOUT_ID in ids
    assert all(l.url.startswith(BUYEE_PREFIX) for l in res.listings)


def test_legacy_li_product_page_is_parser_broken(make_source):
    src = make_source(lambda req: httpx.Response(200, text=LEGACY_HTML))
    res = src.search_detailed(KEYWORD, pages=1)
    assert res.health is ParseHealth.PARSER_BROKEN
    assert "__NEXT_DATA__" in res.detail
    assert res.parsed_count == 0


def test_json_hits_crosscheck_catches_empty_items(make_source):
    html = _html_with_listing(OK_HTML, items=[], totalResultsAvailable=7999)
    src = make_source(lambda req: httpx.Response(200, text=html))
    res = src.search_detailed(KEYWORD, pages=1)
    assert res.health is ParseHealth.PARSER_BROKEN
    assert "7999" in res.detail


def test_empty_404_page_is_empty_confirmed(make_source):
    src = make_source(lambda req: httpx.Response(404, text=EMPTY_HTML))
    res = src.search_detailed(KEYWORD, pages=1)
    assert res.health is ParseHealth.EMPTY_CONFIRMED
    assert res.listings == []
```

既有測試裡所有用 `BUYOUT_ID`／`PURE_BID_ID`／`NUMERIC_ID` 的斷言，改成新 fixture 的樣本 id 與價格；`NUMERIC_ID`（純數字 id）新 fixture 若沒有就刪掉該常數與對應斷言，並在 commit message 說明。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_yahoo_source.py -q 2>&1 | tail -15`
Expected: 新增的三條 FAIL（目前 `li.Product` 解析對新 fixture 回 0 筆 → PARSER_BROKEN，`test_nextdata_page_parses_listings` 紅；legacy 頁面目前反而會 OK，`test_legacy_li_product_page_is_parser_broken` 紅）。

- [ ] **Step 3: 實作——兩頁共用 JSON 節點／健康／抽取**

在 `yahoo.py`：

1. 把 `_SELLER_LISTING_PATH` 改名為 `_LISTING_PATH`（註解寫明：搜尋頁與賣家頁同一條路徑，2026-10-20 實測；與 `yahoo_closed._LISTING_PATH` 逐字相同）。`_SELLER_PROFILE_PATH` 不動。
2. `_seller_nodes(html)` 改名 `_page_nodes(html) -> tuple[dict | None, dict | None]`，實作不變（賣家頁兩個節點都有；搜尋頁 profile 會是 None，正常）。
3. `_judge_seller_health(node)` 改名 `_judge_json_health(node, *, label: str)`，訊息把「賣家頁」換成 `label`（搜尋頁傳 `"搜尋頁"`、賣家頁傳 `"賣家頁"`），三層判定邏輯逐字不變。**找不到節點時的 detail 必須含 `__NEXT_DATA__` 字樣**（測試斷言用）。
4. `_extract_seller_listings(items, seen, *, seller_id, profile)` 改名 `_extract_json_listings(items, seen, *, seller_id: str | None = None, profile: dict | None = None)`。唯一行為差異：`seller_id=None` 時 Listing 的 `seller_id` 用 `raw_seller.get("userId")`，抽不到就 `None`（不要填空字串、不要猜）。其餘逐字不變。
5. `search_detailed` 的迴圈主體改成與 `search_seller` 同構：

```python
            result.pages_fetched = page
            node, _profile = self._page_nodes(html)

            if page == 1:
                result.html_bytes = len(html)
                health, detail = self._judge_json_health(node, label="搜尋頁")
                if health is not ParseHealth.OK:
                    result.health = health
                    result.detail = detail
                    return result
            elif node is None:
                print(f"[warn] {self.name} p{page} 找不到 __NEXT_DATA__，只回傳前 {page - 1} 頁")
                break

            raw_items = (node or {}).get("items") or []
            batch, stats = self._extract_json_listings(raw_items, seen)
            excluded_bids += stats["bid_only"]
            result.parsed_count += stats["parsed"]
            result.listings.extend(batch)
            if len(raw_items) < _PAGE_SIZE:
                break
```

   `result.detail` 收尾照 `search_seller` 的寫法（排除純競標／排除フリマ標的兩個計數）。
6. 刪除：`_judge_health`（li 版）、`_parse_hits`、`_extract_listings`、`_parse_auction_meta`、`_parse_seller`、`_parse_prices`、`_parse_image`、`_HITS_RE`、`_EMPTY_TEXT`，以及只剩它們在用的 import（`Tag` 等）。刪之前 `grep -rn "_parse_hits\|_extract_listings\|_parse_prices\|_judge_health" src/ tests/ web/` 確認沒有其他呼叫端（`yahoo_closed` 有自己的 `_judge_health`，不要誤刪那個）。
7. 更新 `yahoo.py:48-52` 那段註解：搜尋頁已不再是「不同構的那一對」，三種頁面（搜尋／賣家／closedsearch）現在同一條 JSON 路徑。

- [ ] **Step 3b（2026-10-20 主線程裁決，builder 回報 plan 未涵蓋）: `tests/test_sellers.py` 兩條測試改寫到 JSON 路徑**

`test_yahoo_search_cards_carry_the_seller_attr` 與 `test_yahoo_search_listing_has_seller_id` 直接呼叫被刪掉的 `_parse_seller`／`_extract_listings`。意圖不變、載體換成 JSON：

```python
def test_yahoo_search_cards_carry_the_seller_attr():
    """搜尋頁 JSON 每一筆都帶 seller.userId（2026-10-20 新 fixture 50/50）。掉到 0 代表 Yahoo 又改版。"""
    from ygo_sniper.sources.yahoo import YahooAuctionSource
    html = (FIXTURES / "yahoo_search_ok.html").read_text(encoding="utf-8")
    node, _profile = YahooAuctionSource._page_nodes(html)
    items = node["items"]
    sellers = [(it.get("seller") or {}).get("userId") for it in items]
    assert len(sellers) == 50
    assert all(sellers), "每一筆都該有賣家 ID——掉到 0 代表 Yahoo 改版了"
    assert {len(s) for s in sellers} <= {28, 29}     # 混淆 ID 的形狀不變


def test_yahoo_search_listing_has_seller_id(cfg):
    """走完整 `_extract_json_listings`，Listing.seller_id 真的被填上（不傳 seller_id，逼它從 seller.userId 取）。"""
    from dataclasses import replace
    from ygo_sniper.sources.yahoo import YahooAuctionSource
    scoped = replace(cfg, sources={**cfg.sources, "yahoo_direct": {"include_live_auctions": True}})
    src = YahooAuctionSource(scoped, fetcher=object())
    html = (FIXTURES / "yahoo_search_ok.html").read_text(encoding="utf-8")
    node, _profile = YahooAuctionSource._page_nodes(html)
    listings, stats = src._extract_json_listings(node["items"], set())
    assert stats["parsed"] > 0 and listings
    assert all(lst.seller_id for lst in listings)
```

另兩個裁決：(1) builder 把 `_extract_json_listings` 的去重移到 `parsed` 計數前、被排除者也進 `seen`——**接受**，這是搜尋頁原本就有的不變式（跨頁推廣位不重複計入 `parsed_count`），賣家頁一併受惠；(2) `seller_watch.py:114` 註解裡的 `_SELLER_LISTING_PATH` 順手改成 `_LISTING_PATH`（純註解，一行）。

- [ ] **Step 4: 跑搜尋頁與賣家頁測試**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_yahoo_source.py tests/test_yahoo_seller_page.py tests/test_yahoo_closed_source.py -q 2>&1 | tail -5`
Expected: 全綠。

- [ ] **Step 5: 全量測試**

Run: `cd /Users/jim/projects/ygo-sniper && make test 2>&1 | tail -3`
Expected: `N passed`（N ≥ 2043 + 新增條數），0 failed。

- [ ] **Step 6: Commit**

```bash
git add src/ygo_sniper/sources/yahoo.py tests/test_yahoo_source.py
git commit -m "fix(yahoo): 搜尋頁改讀 __NEXT_DATA__（2026-10-05 起改版致每輪 parser_broken），與賣家頁共用 JSON 解析；舊結構頁面判 PARSER_BROKEN"
```

---

> **進度（2026-10-20）**：Task 1 `6c1a4c7`、Task 2 `fccc43e` 完成；Task 3 主線程實跑 `scan --dry-run` → `yahoo_direct: ok count=396｜解析 50 筆、排除フリマ標的 17 筆`，通過。

### Task 3 @inline：實網煙霧測試（主線程親跑，builder 不做）

- [ ] **Step 1:** `cd /Users/jim/projects/ygo-sniper && .venv/bin/ygo-sniper scan --dry-run 2>&1 | grep -E "yahoo_direct:|🎯 狙擊"`
Expected: `yahoo_direct: ok  count=<正整數>`，不再出現 `parser_broken`。
- [ ] **Step 2:** `cd /Users/jim/projects/ygo-sniper && .venv/bin/ygo-sniper health 2>&1 | grep -i yahoo_direct`
Expected: 狀態不是 parser_broken（下一輪 daily 會送 recovery 告警，屬既有行為）。

---

## Part B — 排程空窗補跑模式

### Task 4 @inline：漏跑視窗帳（schedule_watch 純函式 ＋ pipeline 寫帳 ＋ 補跑模式翻頁）

**Files:**
- Modify: `src/ygo_sniper/schedule_watch.py`
- Modify: `src/ygo_sniper/pipeline.py`（`_scan_source` 416-489、`scan` 820-860、`_update_schedule_state` 937-992、`_scan` 回傳 dict 的組裝處）
- Modify: `config/settings.yaml`（`scan:` 區塊）
- Test: `tests/test_schedule_watch.py`（加）、`tests/test_pipeline_catchup.py`（新）

**資料結構（meta 鍵 `schedule_gap_window`，JSON）：**

```json
{"uncovered_from": "2026-10-19T09:00:01", "uncovered_to": "2026-10-19T20:35:34",
 "missed_slots": 10, "first_slot": "2026-10-19T09:30:00", "last_slot": "2026-10-19T20:00:00",
 "detected_at": "2026-10-19T20:35:34", "caught_up_at": null}
```

全部本地 naive ISO（與 `RUN_STARTED_KEY` 同基準）。`uncovered_from` = 上一輪起跑時刻（那一輪掃到的東西到那一刻為止），`uncovered_to` = 這一輪起跑時刻。**只記低價帶**（狙擊挖掘只在低價帶 `daily` 跑，高價帶的漏跑不進這本帳；docstring 寫明）。

**設定（`config/settings.yaml` 的 `scan:` 區塊末尾加）：**

```yaml
  # 補跑模式（排程漏跑之後的那一輪，或 `ygo-sniper daily --catchup`）：
  # 每個發現查詢多翻幾頁。2026-10-20 實測各來源每日新上架最多 43 筆、最小頁容量 40，
  # 多 1 頁就蓋得住一整天的空窗；設 0 = 補跑模式不多翻頁（其餘行為照常）。
  catchup_extra_pages: 1
  # 漏跑幾格以上才自動進入補跑模式。1 格常見於上一輪超時佔住鎖，不值得多翻頁。
  catchup_min_missed_slots: 2
```

- [ ] **Step 1: 寫失敗測試（schedule_watch 純函式）**

在 `tests/test_schedule_watch.py` 加：

```python
from datetime import datetime
import json
from ygo_sniper import schedule_watch as sw


def test_detect_missed_returns_slots_and_tolerates_bad_input():
    now = datetime(2026, 10, 19, 20, 35, 34)
    assert sw.detect_missed("2026-10-19T09:00:01", now) == [
        datetime(2026, 10, 19, 9, 30), datetime(2026, 10, 19, 11, 30),
        datetime(2026, 10, 19, 13, 30), datetime(2026, 10, 19, 15, 30),
        datetime(2026, 10, 19, 17, 30), datetime(2026, 10, 19, 18, 0),
        datetime(2026, 10, 19, 18, 30), datetime(2026, 10, 19, 19, 0),
        datetime(2026, 10, 19, 19, 30), datetime(2026, 10, 19, 20, 0),
    ]
    assert sw.detect_missed(None, now) == []
    assert sw.detect_missed("garbage", now) == []


def test_gap_record_written_when_missed_and_untouched_when_not():
    prev = datetime(2026, 10, 19, 9, 0, 1)
    now = datetime(2026, 10, 19, 20, 35, 34)
    missed = sw.detect_missed(prev.isoformat(), now)
    raw = sw.resolve_gap_record(None, missed, prev, now)
    rec = json.loads(raw)
    assert rec["missed_slots"] == 10
    assert rec["uncovered_from"] == prev.isoformat()
    assert rec["uncovered_to"] == now.isoformat()
    assert rec["first_slot"] == "2026-10-19T09:30:00"
    assert rec["last_slot"] == "2026-10-19T20:00:00"
    assert rec["caught_up_at"] is None
    # 沒漏格：原帳原封不動（不論是 None 還是舊帳）
    assert sw.resolve_gap_record(None, [], now, now) is None
    assert sw.resolve_gap_record(raw, [], now, now) == raw


def test_gap_record_extends_uncaught_window():
    """前一個視窗還沒補跑、又漏一段：合併成一個視窗，from 取最早、格數相加。"""
    prev1, now1 = datetime(2026, 10, 19, 9, 0), datetime(2026, 10, 19, 20, 35)
    raw1 = sw.resolve_gap_record(None, sw.detect_missed(prev1.isoformat(), now1), prev1, now1)
    prev2, now2 = datetime(2026, 10, 19, 20, 35), datetime(2026, 10, 19, 22, 36)
    missed2 = sw.detect_missed(prev2.isoformat(), now2)   # 21:00 21:30 22:00
    rec = json.loads(sw.resolve_gap_record(raw1, missed2, prev2, now2))
    assert rec["uncovered_from"] == prev1.isoformat()
    assert rec["uncovered_to"] == now2.isoformat()
    assert rec["missed_slots"] == 13
    assert rec["last_slot"] == "2026-10-19T22:00:00"


def test_gap_record_replaced_after_caught_up():
    prev1, now1 = datetime(2026, 10, 19, 9, 0), datetime(2026, 10, 19, 20, 35)
    raw1 = sw.resolve_gap_record(None, sw.detect_missed(prev1.isoformat(), now1), prev1, now1)
    done = sw.mark_gap_caught_up(raw1, datetime(2026, 10, 19, 21, 15))
    assert json.loads(done)["caught_up_at"] == "2026-10-19T21:15:00"
    prev2, now2 = datetime(2026, 10, 19, 20, 35), datetime(2026, 10, 19, 22, 36)
    rec = json.loads(sw.resolve_gap_record(done, sw.detect_missed(prev2.isoformat(), now2), prev2, now2))
    assert rec["uncovered_from"] == prev2.isoformat()
    assert rec["missed_slots"] == 3


def test_decode_gap_record_is_lenient():
    assert sw.decode_gap_record(None) is None
    assert sw.decode_gap_record("not json") is None
    assert sw.decode_gap_record('{"missed_slots": "x"}') is None


def test_gap_needs_catchup_threshold():
    rec = {"missed_slots": 1, "caught_up_at": None}
    assert sw.gap_needs_catchup(rec, min_missed_slots=2) is False
    rec["missed_slots"] = 2
    assert sw.gap_needs_catchup(rec, min_missed_slots=2) is True
    rec["caught_up_at"] = "2026-10-19T21:15:00"
    assert sw.gap_needs_catchup(rec, min_missed_slots=2) is False
    assert sw.gap_needs_catchup(None, min_missed_slots=2) is False
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_schedule_watch.py -q 2>&1 | tail -5`
Expected: 新測試 `AttributeError`（函式不存在）。

- [ ] **Step 3: 實作 schedule_watch 純函式**

```python
#: 漏跑視窗帳（只記低價帶——狙擊重挖與回顧只在 `daily` 跑；高價帶漏跑不進這本帳）。
#: 本地 naive ISO，與 RUN_STARTED_KEY 同基準。
GAP_WINDOW_KEY = "schedule_gap_window"


def detect_missed(prev_started: str | None, now: datetime, slots: list[int] | None = None) -> list[datetime]:
    """`schedule_health` 第 2 項判定的可重用版本：回傳漏跑的格子，輸入壞掉一律空 list。"""
    if not prev_started:
        return []
    try:
        prev = datetime.fromisoformat(prev_started)
    except (TypeError, ValueError):
        return []
    try:
        return missed_slots_between(prev, now, slots)
    except TypeError:
        return []


def decode_gap_record(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("missed_slots"), int):
        return None
    if not isinstance(data.get("uncovered_from"), str) or not isinstance(data.get("uncovered_to"), str):
        return None
    return data


def resolve_gap_record(existing_raw: str | None, missed: list[datetime], prev: datetime, now: datetime) -> str | None:
    """沒漏格 → 回傳原值（呼叫端據此不寫）。有漏格 → 新帳；若舊帳尚未補跑則合併
    （from 取舊的、格數相加），已補跑的舊帳直接換掉。"""
    if not missed:
        return existing_raw
    prev_rec = decode_gap_record(existing_raw)
    merge = prev_rec is not None and prev_rec.get("caught_up_at") is None
    rec = {
        "uncovered_from": prev_rec["uncovered_from"] if merge else prev.isoformat(),
        "uncovered_to": now.isoformat(),
        "missed_slots": (prev_rec["missed_slots"] if merge else 0) + len(missed),
        "first_slot": prev_rec.get("first_slot") if merge else missed[0].isoformat(),
        "last_slot": missed[-1].isoformat(),
        "detected_at": now.isoformat(),
        "caught_up_at": None,
    }
    return json.dumps(rec, ensure_ascii=False)


def mark_gap_caught_up(existing_raw: str | None, when: datetime) -> str | None:
    rec = decode_gap_record(existing_raw)
    if rec is None:
        return existing_raw
    rec["caught_up_at"] = when.isoformat()
    return json.dumps(rec, ensure_ascii=False)


def gap_needs_catchup(rec: dict | None, *, min_missed_slots: int) -> bool:
    return bool(rec) and rec.get("caught_up_at") is None and int(rec.get("missed_slots", 0)) >= int(min_missed_slots)
```

`schedule_health` 內部的 `missed = ...` 那段改成呼叫 `detect_missed(prev_started, now, slots)`（既有測試輸出必須逐字不變）。

- [ ] **Step 4: 跑 schedule_watch 測試**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_schedule_watch.py -q 2>&1 | tail -3`
Expected: 全綠（含既有的 plist 對齊測試）。

- [ ] **Step 5: 寫失敗測試（pipeline 補跑模式）**

新檔 `tests/test_pipeline_catchup.py`。照 `tests/test_pipeline_isolation.py` 既有的 Pipeline fixture 建法（臨時 db、來源換假物件）；若那裡沒有現成可用的 fixture，用 `tests/test_card_snipe.py` 裡呼叫 `pipeline.scan(...)` 的建法。測三件事：

```python
def test_catchup_flag_adds_extra_pages_to_discovery_queries(pipe_factory):
    """scan(catchup=True) → 每個發現查詢 pages = 設定值 + catchup_extra_pages；canary 仍是 1。"""
    calls = []
    pipe = pipe_factory(record_search_calls=calls, scan_overrides={"catchup_extra_pages": 1})
    pipe.scan(catchup=True, dry_run=True)
    discovery = [c for c in calls if not c["is_canary"]]
    assert discovery and all(c["pages"] == pipe.cfg.max_pages_for(c["source"]) + 1 for c in discovery)
    canary = [c for c in calls if c["is_canary"]]
    assert all(c["pages"] == 1 for c in canary)


def test_catchup_auto_activates_from_uncaught_gap_record(pipe_factory):
    """帳上有 ≥ 門檻的未補跑視窗 → 不帶 catchup=True 也進補跑模式，result['catchup'] 帶視窗。"""
    pipe = pipe_factory(scan_overrides={"catchup_min_missed_slots": 2})
    pipe.store.set_meta(sw.GAP_WINDOW_KEY, json.dumps({
        "uncovered_from": "2026-10-19T09:00:01", "uncovered_to": "2026-10-19T20:35:34",
        "missed_slots": 10, "first_slot": "2026-10-19T09:30:00", "last_slot": "2026-10-19T20:00:00",
        "detected_at": "2026-10-19T20:35:34", "caught_up_at": None}))
    result = pipe.scan()
    assert result["catchup"]["active"] is True
    assert result["catchup"]["record"]["missed_slots"] == 10


def test_catchup_not_activated_below_threshold_or_after_caught_up(pipe_factory):
    pipe = pipe_factory(scan_overrides={"catchup_min_missed_slots": 2})
    pipe.store.set_meta(sw.GAP_WINDOW_KEY, json.dumps({
        "uncovered_from": "x", "uncovered_to": "y", "missed_slots": 1,
        "first_slot": None, "last_slot": None, "detected_at": "z", "caught_up_at": None}))
    assert pipe.scan()["catchup"]["active"] is False


def test_gap_record_is_written_by_update_schedule_state(pipe_factory):
    """上一輪 09:00 起跑、這一輪 20:35 起跑 → 帳上出現 10 格的視窗。"""
    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY, "2026-10-19T09:00:01")
    pipe.store.set_meta(sw.RUN_FINISHED_KEY, "2026-10-19T10:15:23")
    pipe._update_schedule_state(dry_run=False, watch_only=False, now=datetime(2026, 10, 19, 20, 35, 34))
    rec = sw.decode_gap_record(pipe.store.get_meta(sw.GAP_WINDOW_KEY))
    assert rec["missed_slots"] == 10 and rec["caught_up_at"] is None


def test_high_band_never_touches_gap_record(pipe_factory):
    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY_HIGH, "2026-10-19T10:00:00")
    pipe._update_schedule_state(dry_run=False, watch_only=False, high_band=True,
                                now=datetime(2026, 10, 19, 20, 35))
    assert pipe.store.get_meta(sw.GAP_WINDOW_KEY) is None
```

`pipe_factory` 的 `record_search_calls`：把 `run_source_search`（或 `_scan_source`）換成記錄 `{"source", "pages", "is_canary"}` 的替身；canary 的判準是 `price_ceiling=False`（見 `_scan_source` docstring）。做法由 builder 決定，但**不得打網路**（CLAUDE.md 第六節）。

- [ ] **Step 6: 實作 pipeline**

1. `_update_schedule_state(self, *, dry_run, watch_only, high_band=False, now: datetime | None = None)`：加 `now` 參數（預設 `datetime.now()`，測試注入——CLAUDE.md 第六節「時效類判斷一律要能注入現在」）。在 `self.store.set_meta(started_key, now.isoformat())` **之前**、非 `high_band` 時：

```python
            if not high_band:
                missed = detect_missed(self.store.get_meta(started_key), now, slots)
                prev_raw = self.store.get_meta(GAP_WINDOW_KEY)
                prev_dt = None
                try:
                    prev_dt = datetime.fromisoformat(self.store.get_meta(started_key) or "")
                except (TypeError, ValueError):
                    pass
                new_raw = resolve_gap_record(prev_raw, missed, prev_dt or now, now)
                if new_raw is not None and new_raw != prev_raw:
                    self.store.set_meta(GAP_WINDOW_KEY, new_raw)
```

   （`prev_dt` 讀不到卻有漏格的情況在 `detect_missed` 裡已經回空 list，所以 `prev_dt or now` 只是型別保險。）

2. `scan(..., catchup: bool = False)`：在 `_update_schedule_state` 之後、`_scan` 之前決定旗子並記在 `self`：

```python
        rec = decode_gap_record(self.store.get_meta(GAP_WINDOW_KEY)) if not high_band else None
        auto = gap_needs_catchup(rec, min_missed_slots=int(self.cfg.scan.get("catchup_min_missed_slots", 2)))
        self._catchup_active = bool(catchup or auto) and not watch_only and not high_band
        self._catchup_record = rec
        if self._catchup_active:
            extra = max(0, int(self.cfg.scan.get("catchup_extra_pages", 1)))
            why = "--catchup" if catchup else f"漏跑 {rec['missed_slots']} 格（{rec['uncovered_from']} ～ {rec['uncovered_to']} 未覆蓋）"
            print(f"[catchup] 補跑模式：{why}；每個發現查詢多翻 {extra} 頁")
```

   `__init__` 要先設 `self._catchup_active = False`、`self._catchup_record = None`（`_scan_source` 會讀）。

3. `_scan_source(...)` 加關鍵字參數 `allow_catchup: bool = True`；頁數那一行改成：

```python
        pages = pages if pages is not None else self.cfg.max_pages_for(source_name)
        if allow_catchup and self._catchup_active:
            pages += max(0, int(self.cfg.scan.get("catchup_extra_pages", 1)))
```

   `_run_canaries` 裡那個 `pages=1, price_ceiling=False` 的呼叫加 `allow_catchup=False`。`_scan_watched_sellers` 的賣家頁呼叫（`pipeline.py:734`）也加 `allow_catchup=False`——賣家頁一頁是完整列舉（CLAUDE.md 第五節第 8 項），多翻沒有意義。

4. `_scan` 回傳的 dict 加一鍵：`result["catchup"] = {"active": self._catchup_active, "record": self._catchup_record}`。`dry_run` 與 `watch_only` 也要帶（值為 `active: False`），讓呼叫端不用判 key 存不存在。

- [ ] **Step 7: 跑測試**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_pipeline_catchup.py tests/test_schedule_watch.py tests/test_pipeline_isolation.py -q 2>&1 | tail -3`
Expected: 全綠。

- [ ] **Step 8: 全量 ＋ Commit**

Run: `cd /Users/jim/projects/ygo-sniper && make test 2>&1 | tail -3` → 全綠。

```bash
git add src/ygo_sniper/schedule_watch.py src/ygo_sniper/pipeline.py config/settings.yaml tests/test_schedule_watch.py tests/test_pipeline_catchup.py
git commit -m "feat(schedule): 漏跑視窗帳 + 補跑模式（漏格達門檻或 --catchup 時每個發現查詢多翻一頁）"
```

---

> **進度（2026-10-20）**：Task 4 `0ff6ff7` 完成。兩處接受的偏離：(1) 賣家頁呼叫直接走 `run_source_search`、不經 `_scan_source`，所以沒有 `allow_catchup=False` 可加，以測試釘住頁數不受 catchup 影響；(2) 視窗帳寫入抽成 `_record_gap_window` 獨立 try/except，壞掉不連坐告警與基準。`Pipeline.scan()` 的決策函式叫 `_decide_catchup`。

### Task 5 @inline：`catchup.py`（視窗內狙擊成交回顧）＋ `daily --catchup`

**Files:**
- Create: `src/ygo_sniper/catchup.py`
- Modify: `src/ygo_sniper/cli.py`（`daily` 55-75、`_mine_snipes_daily` 149-182）
- Test: `tests/test_catchup.py`（新）、`tests/test_cli_notify.py`（加一條）

**訊息格式（Telegram 純文字，與 `send_alert` 同通道）：**

```
🕳️ 漏跑期間狙擊卡成交回顧（10-19 09:00 ～ 10-19 20:35，漏 10 格）
期間內已成交、我們來不及看到的狙擊目標：
• 🎯 魔法の筒 ARS10 ｜ ¥3,441 ｜ 10-19 22:18 ｜ yahoo_closed ｜ <origin_url 或 url>
• 👀 インセクト女王 missing foil PSA3（部分符合）｜ ¥6,250 ｜ …
另有 3 筆同名但未達鑑定條件的成交（near），不逐筆列。
⚠️ buyee_mercari／ruten 的成交沒有時刻，無法判定是否在期間內，未計入。
```

- `exact` 標 🎯、`partial` 標 👀（與規則 4 的符號一致）。
- 時刻顯示換成**本地**時間（`sold_at` UTC → `astimezone()`）。
- 沒有任何 exact/partial 也沒有 near → 回 `None`（不送訊息，但仍標 caught_up）。只有 near → 仍送（一行筆數）。
- 最後一行只在 `card_watch_sale` 裡**確實存在** `sold_at=''` 的來源時才加（查 `SELECT DISTINCT source FROM card_watch_sale WHERE sold_at = ''`，不要硬編來源名）。

- [ ] **Step 1: 寫失敗測試**

`tests/test_catchup.py`（用 repo 既有的臨時 `Store` fixture；`upsert`／寫入成交列用 `store` 既有的方法——`grep -n "def upsert_card_watch_sale\|def add_card_watch\|def insert_card_watch" src/ygo_sniper/store.py` 找名字）：

```python
from datetime import datetime
from ygo_sniper import catchup


def test_window_to_utc_converts_local_naive_to_aware_utc():
    lo, hi = catchup.window_to_utc("2026-10-19T09:00:01", "2026-10-19T20:35:34")
    assert lo.tzinfo is not None and hi.tzinfo is not None
    assert lo.utcoffset().total_seconds() == 0
    # 本地（Asia/Taipei，UTC+8）09:00 → 01:00Z
    assert lo.hour == 1 and hi.hour == 12


def test_gap_sales_filters_by_window_and_tier_and_dedupes(store_with_watches):
    store = store_with_watches   # 兩個 active watch（同一張卡、不同鑑定目標）＋一個 inactive
    # 視窗 2026-10-19 本地 09:00～20:35 ＝ 01:00Z～12:35Z
    put = store_with_watches.put_sale   # fixture 提供的薄包裝，寫 card_watch_sale 一列
    put(watch_id=1, sale_key="buyee_yahoo:a1", tier="exact",   sold_at="2026-10-19T03:00:00+00:00", price=3441)
    put(watch_id=9, sale_key="buyee_yahoo:a1", tier="partial", sold_at="2026-10-19T03:00:00+00:00", price=3441)  # 同一筆、另一個 watch
    put(watch_id=1, sale_key="buyee_yahoo:b2", tier="near",    sold_at="2026-10-19T05:00:00+00:00", price=100)
    put(watch_id=1, sale_key="buyee_yahoo:c3", tier="exact",   sold_at="2026-10-19T13:00:00+00:00", price=9999)  # 視窗外
    put(watch_id=1, sale_key="buyee_mercari:d4", tier="exact", sold_at="", price=5000)                          # 無時刻
    put(watch_id=4, sale_key="buyee_yahoo:e5", tier="exact",   sold_at="2026-10-19T03:30:00+00:00", price=1)    # inactive watch
    rep = catchup.gap_sales(store, "2026-10-19T09:00:01", "2026-10-19T20:35:34")
    assert [s["sale_key"] for s in rep.graded] == ["buyee_yahoo:a1"]
    assert rep.graded[0]["tier"] == "exact"          # 同一筆取較高 tier
    assert rep.near_count == 1
    assert rep.undated_sources == ["buyee_mercari"]


def test_format_gap_review_none_when_nothing(store_with_watches):
    rec = {"uncovered_from": "2026-10-19T09:00:01", "uncovered_to": "2026-10-19T20:35:34", "missed_slots": 10}
    rep = catchup.gap_sales(store_with_watches, rec["uncovered_from"], rec["uncovered_to"])
    assert catchup.format_gap_review(rec, rep) is None


def test_format_gap_review_lists_graded_and_counts_near(store_with_watches):
    put = store_with_watches.put_sale
    put(watch_id=1, sale_key="buyee_yahoo:a1", tier="exact", sold_at="2026-10-19T03:00:00+00:00", price=3441)
    put(watch_id=1, sale_key="buyee_yahoo:b2", tier="near",  sold_at="2026-10-19T05:00:00+00:00", price=100)
    rec = {"uncovered_from": "2026-10-19T09:00:01", "uncovered_to": "2026-10-19T20:35:34", "missed_slots": 10}
    text = catchup.format_gap_review(rec, catchup.gap_sales(store_with_watches, rec["uncovered_from"], rec["uncovered_to"]))
    assert text.startswith("🕳️ 漏跑期間狙擊卡成交回顧")
    assert "🎯" in text and "3,441" in text and "另有 1 筆" in text
    assert "10-19 11:00" in text     # 03:00Z → 本地 11:00
```

`tests/test_cli_notify.py` 加一條：`daily --catchup` 時 (a) `_mine_snipes_daily` 被以 `force=True` 呼叫（即使 meta 已是今天）；(b) 回顧訊息經 `pipe.notifier.send_alert` 送出且回 True 時，`GAP_WINDOW_KEY` 的 `caught_up_at` 被寫入；(c) `send_alert` 回 False 時 `caught_up_at` 仍為 None。照該檔既有的假 Pipeline／假 notifier 樣式寫。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_catchup.py -q 2>&1 | tail -3`
Expected: `ModuleNotFoundError: ygo_sniper.catchup`。

- [ ] **Step 3: 實作 `catchup.py`**

```python
"""排程空窗補跑模式：漏跑期間「狙擊卡成交回顧」。

能補的只有「它們成交了、成交多少錢」這個事實——期間內上架又結標的商品
已不在架上，任何事後掃描都撈不回來（這是本功能存在的前提，不要試圖
把它做成「重掃過去」）。

同基準條款（CLAUDE.md 第三節）：排程帳是本地 naive ISO，`card_watch_sale.sold_at`
是 UTC aware ISO——先 `window_to_utc` 再比，不比字串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

_TIER_RANK = {"exact": 2, "partial": 1}
_TIER_MARK = {"exact": "🎯", "partial": "👀"}


def window_to_utc(local_from: str, local_to: str) -> tuple[datetime, datetime]:
    """本地 naive ISO → UTC aware。`astimezone()` 對 naive 值採系統時區，這正是排程帳的基準。"""
    lo = datetime.fromisoformat(local_from).astimezone(UTC)
    hi = datetime.fromisoformat(local_to).astimezone(UTC)
    return lo, hi


@dataclass
class GapSalesReport:
    graded: list[dict] = field(default_factory=list)   # exact/partial，已依 sale_key 去重、依 sold_at 排序
    near_count: int = 0                                 # near，去重後筆數
    undated_sources: list[str] = field(default_factory=list)


def gap_sales(store, local_from: str, local_to: str) -> GapSalesReport:
    lo, hi = window_to_utc(local_from, local_to)
    watches = {w["id"]: w for w in store.list_card_watch(active_only=True)}
    best: dict[str, dict] = {}
    near: set[str] = set()
    for wid, w in watches.items():
        for s in store.list_card_watch_sales(wid):
            raw = s.get("sold_at") or ""
            if not raw:
                continue
            try:
                when = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if when.tzinfo is None:
                continue            # 沒有時區的時刻不可比，寧可不計也不要猜
            if not (lo < when <= hi):
                continue
            tier = s.get("tier")
            if tier == "near":
                near.add(s["sale_key"])
                continue
            if tier not in _TIER_RANK:
                continue
            cur = best.get(s["sale_key"])
            if cur is None or _TIER_RANK[tier] > _TIER_RANK[cur["tier"]]:
                best[s["sale_key"]] = {**s, "watch": w, "sold_at_dt": when}
    graded = sorted(best.values(), key=lambda r: r["sold_at_dt"])
    undated = store.card_watch_sale_undated_sources()
    return GapSalesReport(graded=graded, near_count=len(near), undated_sources=undated)


def _fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%m-%d %H:%M")


def _fmt_window(rec: dict) -> str:
    lo = datetime.fromisoformat(rec["uncovered_from"]).strftime("%m-%d %H:%M")
    hi = datetime.fromisoformat(rec["uncovered_to"]).strftime("%m-%d %H:%M")
    return f"{lo} ～ {hi}"


def format_gap_review(rec: dict, rep: GapSalesReport) -> str | None:
    if not rep.graded and not rep.near_count:
        return None
    lines = [f"🕳️ 漏跑期間狙擊卡成交回顧（{_fmt_window(rec)}，漏 {rec.get('missed_slots', '?')} 格）"]
    if rep.graded:
        lines.append("期間內已成交、我們來不及看到的狙擊目標：")
        for s in rep.graded:
            w = s["watch"]
            grade = f"{w.get('grader', '')}{int(w['grade']) if w.get('grade') is not None else ''}"
            note = "（部分符合）" if s["tier"] == "partial" else ""
            price = f"¥{int(s['price_native']):,}" if s.get("price_native") is not None else "價格不明"
            link = s.get("origin_url") or s.get("url") or ""
            lines.append(f"• {_TIER_MARK[s['tier']]} {w['name_ja']} {grade}{note} ｜ {price} ｜ "
                         f"{_fmt_local(s['sold_at_dt'])} ｜ {s.get('source', '')} ｜ {link}".rstrip(" ｜"))
    if rep.near_count:
        lines.append(f"另有 {rep.near_count} 筆同名但未達鑑定條件的成交（near），不逐筆列。")
    if rep.undated_sources:
        lines.append(f"⚠️ {'／'.join(rep.undated_sources)} 的成交沒有時刻，無法判定是否在期間內，未計入。")
    return "\n".join(lines)
```

`store.py` 加一個小查詢：

```python
    def card_watch_sale_undated_sources(self) -> list[str]:
        """成交表裡 `sold_at` 為空字串的來源清單（排序）。回顧訊息據此誠實標註哪些管道算不進去。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT source FROM card_watch_sale WHERE sold_at = '' ORDER BY source"
            ).fetchall()
        return [r[0] for r in rows if r[0]]
```

- [ ] **Step 4: 實作 cli**

1. `_mine_snipes_daily(pipe, *, force: bool = False)`：`if not force and pipe.store.get_meta(...) == today: return`。force 時印 `[catchup] 強制重挖狙擊成交檔案（補跑模式）`。
2. `daily` 加 `catchup: bool = typer.Option(False, "--catchup", help="補跑模式：每個查詢多翻一頁、強制重挖狙擊成交、推漏跑期間成交回顧")`；`pipe.scan(skip_comps=skip_comps, catchup=catchup)`；之後：

```python
        cu = result.get("catchup") or {}
        _mine_snipes_daily(pipe, force=bool(cu.get("active")))
        _backfill_comps_daily(pipe)
        _refresh_market_drift_daily(pipe)
        if not no_notify:
            _run_notifications(pipe, result, band="std")
            if cu.get("active"):
                _send_gap_review(pipe, cu.get("record"))
```

3. 新函式：

```python
def _send_gap_review(pipe, rec: dict | None) -> None:
    """補跑模式的收尾：推「漏跑期間狙擊卡成交回顧」，**送達才標 caught_up**。

    沒有帳（`--catchup` 手動跑、帳上沒視窗）→ 什麼都不送、不標。
    訊息為 None（期間內沒有任何狙擊卡成交）→ 不送，但標 caught_up（沒有東西需要送達）。
    送失敗 → 不標，下一輪 `daily` 會因帳上仍是未補跑而再次進補跑模式重試
    （與 `_send_schedule_alert`「送達才消耗」同一條規則）。
    """
    from datetime import datetime
    from .catchup import format_gap_review, gap_sales
    from .schedule_watch import GAP_WINDOW_KEY, mark_gap_caught_up

    if not rec:
        return
    if not bool(pipe.cfg.notify.get("enabled", True)):
        console.print("[yellow]補跑回顧未送出（notify.enabled=false），視窗保留為未補跑[/yellow]")
        return
    text = format_gap_review(rec, gap_sales(pipe.store, rec["uncovered_from"], rec["uncovered_to"]))
    if text is None:
        console.print("[dim]漏跑期間沒有狙擊卡成交，無回顧可送；視窗標記為已補跑[/dim]")
        pipe.store.set_meta(GAP_WINDOW_KEY, mark_gap_caught_up(pipe.store.get_meta(GAP_WINDOW_KEY), datetime.now()))
        return
    if pipe.notifier.send_alert(text):
        pipe.store.set_meta(GAP_WINDOW_KEY, mark_gap_caught_up(pipe.store.get_meta(GAP_WINDOW_KEY), datetime.now()))
        console.print("[yellow]已推播漏跑期間狙擊卡成交回顧，視窗標記為已補跑[/yellow]")
    else:
        console.print("[red]漏跑回顧送出失敗——視窗保留為未補跑，下一輪會重試[/red]")
```

4. 修 `daily` 的 docstring：「每小時」是過時描述，改成「排程那一鍵（時段見 scripts/com.jim.ygosniper.plist）」。

- [ ] **Step 5: 跑測試**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_catchup.py tests/test_cli_notify.py -q 2>&1 | tail -3`
Expected: 全綠。

- [ ] **Step 6: 真實資料錨值（唯讀，主線程親跑）**

```bash
cd /Users/jim/projects/ygo-sniper && .venv/bin/python - <<'EOF'
from ygo_sniper.config import load_config
from ygo_sniper.store import Store
from ygo_sniper.catchup import gap_sales, format_gap_review
st = Store(load_config())
r = gap_sales(st, "2026-10-19T09:00:01", "2026-10-19T20:35:34")
print("10-19 gap: graded", len(r.graded), "near", r.near_count, "undated", r.undated_sources)
r2 = gap_sales(st, "2026-10-17T00:00:00", "2026-10-18T00:00:00")
print("10-17 day: graded", [(s['watch']['id'], s['tier'], s['sold_at'], s['price_native']) for s in r2.graded])
print(format_gap_review({"uncovered_from": "2026-10-17T00:00:00", "uncovered_to": "2026-10-18T00:00:00", "missed_slots": 0}, r2))
EOF
```

Expected：第一行 `graded 0 near 3 undated ['buyee_mercari', 'ruten']`；第二行含 `(2, 'partial', '2026-10-17T13:27:11+00:00', 6250.0)`；第三行訊息含 `👀 インセクト女王 missing foil PSA3（部分符合）｜ ¥6,250 ｜ 10-17 21:27`。（`Store` 的建構方式若不是 `Store(cfg)`，照 `web/app.py` 建 store 的寫法。）

- [ ] **Step 7: 全量 ＋ Commit**

```bash
git add src/ygo_sniper/catchup.py src/ygo_sniper/cli.py src/ygo_sniper/store.py tests/test_catchup.py tests/test_cli_notify.py
git commit -m "feat(catchup): daily --catchup 與自動補跑收尾——強制重挖狙擊成交、推漏跑期間成交回顧、送達才標已補跑"
```

---

> **進度（2026-10-20）**：Task 5 `39a1958` 完成。接受的偏離：非補跑輪維持 `_mine_snipes_daily(pipe)` 舊呼叫形狀（既有測試替身）；鑑定等級顯示用 `grade_label`。錨值腳本的 `Store(load_config())` 應為 `Store(load_config().db_path)`。
> **重要發現**：這台機器 `/etc/localtime` 自 2026-10-17 21:24 起是 **America/Chicago**，錨值只在 `TZ=Asia/Taipei` 下成立（機器時區下 10-19 視窗 near=0）。排程帳、`_WINDOWS`、launchd 時刻表全都是機器本地時間——時區切換後整個排程網格平移，10-19 的「漏跑 10 格」告警有一部分是這個造成的。**待使用者裁決排程該釘哪個時區，獨立成案。**

### Task 6 @inline：dashboard「補跑 daily」按鈕 ＋ 漏跑狀態

**Files:**
- Modify: `scripts/run_daily.sh:164-165`
- Modify: `web/app.py`（在 `/api/scan-high` 之後加兩條 route）
- Modify: `web/static/index.html`（`renderScanStatus` 2219-2260、`scanHighNow` 之後加 `catchupNow`、按鈕 HTML 加一顆、新增 `pollGap`）
- Test: `tests/test_daily_run_web.py`（新，client fixture 照抄 `tests/test_scan_high_web.py`）

**設計要點：**
- 按鈕走 **`scripts/run_daily.sh --catchup`**（subprocess），不是行程內 `Pipeline`：這樣鎖（`data/run_daily.lock`）、watchdog、`data/logs/daily-*.log`、`data/last_run_exit` 全部與 launchd 同一條路（CLAUDE.md 第六節：測試路徑＝生產路徑）。dashboard 既有的 scan pill 讀 meta 的 `scan_status`，跨行程，會自動顯示「掃描中（cli 觸發）」。
- route 先做兩個**唯讀**預檢：`store.scan_status` 在跑 → `started:false`；鎖目錄存在且 pid 活著 → `started:false`（鎖的權威仍是腳本自己；這裡只是讓使用者得到「已有一輪在跑」而不是「已啟動」然後什麼都沒發生——那正是靜默失敗）。
- `subprocess.Popen` 經模組層變數 `_spawn_daily = subprocess.Popen` 注入，測試換成假函式，**絕不真的跑腳本**。

- [ ] **Step 1: run_daily.sh 透傳參數**

`scripts/run_daily.sh:164-165` 改成：

```bash
.venv/bin/python scripts/run_with_timeout.py "${YGO_CYCLE_TIMEOUT:-1500}" \
    ygo-sniper daily "$@" >> "$LOG_FILE" 2>&1
```

並在檔頭註解加一行：「額外參數原樣透傳給 `ygo-sniper daily`（dashboard 的補跑按鈕用 `--catchup`）」。

- [ ] **Step 2: 寫失敗測試**

`tests/test_daily_run_web.py`：

```python
def test_daily_run_spawns_run_daily_with_catchup(client, monkeypatch):
    c, app_mod = client
    calls = []
    monkeypatch.setattr(app_mod, "_spawn_daily", lambda argv, **kw: calls.append((argv, kw)) or object())
    r = c.post("/api/daily-run")
    assert r.status_code == 200 and r.json()["started"] is True
    argv, kw = calls[0]
    assert argv[-2:] == ["scripts/run_daily.sh", "--catchup"] or argv[-1] == "--catchup"
    assert str(kw.get("cwd")).endswith("ygo-sniper")
    assert kw.get("start_new_session") is True


def test_daily_run_refuses_when_scan_running(client, monkeypatch):
    c, app_mod = client
    app_mod.store.begin_scan(trigger="cli")
    called = []
    monkeypatch.setattr(app_mod, "_spawn_daily", lambda *a, **k: called.append(1))
    r = c.post("/api/daily-run")
    assert r.json()["started"] is False and not called


def test_daily_run_refuses_when_lock_held_by_live_pid(client, monkeypatch, tmp_path):
    import os
    c, app_mod = client
    lock = app_mod.cfg.db_path.parent / "run_daily.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    called = []
    monkeypatch.setattr(app_mod, "_spawn_daily", lambda *a, **k: called.append(1))
    r = c.post("/api/daily-run")
    assert r.json()["started"] is False and "鎖" in r.json()["message"] and not called


def test_schedule_gap_endpoint_returns_record_or_null(client):
    import json
    from ygo_sniper.schedule_watch import GAP_WINDOW_KEY
    c, app_mod = client
    assert c.get("/api/schedule/gap").json() == {"gap": None}
    rec = {"uncovered_from": "2026-10-19T09:00:01", "uncovered_to": "2026-10-19T20:35:34",
           "missed_slots": 10, "first_slot": "2026-10-19T09:30:00", "last_slot": "2026-10-19T20:00:00",
           "detected_at": "2026-10-19T20:35:34", "caught_up_at": None}
    app_mod.store.set_meta(GAP_WINDOW_KEY, json.dumps(rec))
    assert c.get("/api/schedule/gap").json()["gap"]["missed_slots"] == 10
```

鎖路徑用 `cfg.db_path.parent / "run_daily.lock"`——與 `pipeline._read_watchdog_ledger` 同一個隔離點理由（測試 fixture 覆寫 `storage.db_path`，所以測試永遠碰不到正式的 `data/run_daily.lock`）。

- [ ] **Step 3: 跑測試確認失敗**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_daily_run_web.py -q 2>&1 | tail -3`
Expected: 404 / AttributeError。

- [ ] **Step 4: 實作 web/app.py**

```python
import os
import subprocess

_spawn_daily = subprocess.Popen   # 測試注入點


def _lock_holder_alive(lock_dir: Path) -> int | None:
    """`data/run_daily.lock/pid` 的持有者還活著就回 pid，否則 None（含鎖不存在）。只讀不清——清殘鎖是腳本的事。"""
    try:
        pid = int((lock_dir / "pid").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


@app.post("/api/daily-run")
def trigger_daily_run():
    """從 dashboard 跑一輪完整的 `daily`（補跑模式）——走 `scripts/run_daily.sh --catchup`。

    與 `/api/scan` 的差別：那顆只掃不推、不挖狙擊成交（行程內 Pipeline）；這顆要的是
    「排程漏跑之後把該做的全做一遍並推 Telegram」，所以直接跑 launchd 用的那支腳本，
    鎖／watchdog／log／ledger 一個都不少（CLAUDE.md 第六節：測試路徑＝生產路徑）。
    用途：關機重開、或鎖被卡住手動清掉之後。睡眠醒來的情境 launchd 會自己補觸發，
    那一輪本身就會進補跑模式，不需要按這顆。
    """
    st = store.scan_status(timeout_seconds=cfg.scan_timeout_seconds)
    if st["running"]:
        return {"ok": True, "started": False, "running": True,
                "message": f"已經有一輪掃描在跑（{st.get('trigger') or '?'} 觸發），這次不重複啟動", "status": st}
    lock_dir = cfg.db_path.parent / "run_daily.lock"
    pid = _lock_holder_alive(lock_dir)
    if pid is not None:
        return {"ok": True, "started": False, "running": True,
                "message": f"run_daily.sh 的鎖仍由 pid {pid} 持有（上一輪還在跑），這次不重複啟動"}
    _spawn_daily(
        ["/bin/bash", "scripts/run_daily.sh", "--catchup"],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "started": True, "running": True,
            "message": "補跑已在背景啟動（走 run_daily.sh，log 在 data/logs/daily-*.log），完成後會推 Telegram"}


@app.get("/api/schedule/gap")
def schedule_gap():
    """漏跑視窗帳（`schedule_watch.GAP_WINDOW_KEY`）。null = 沒有紀錄。"""
    from ygo_sniper.schedule_watch import GAP_WINDOW_KEY, decode_gap_record
    return {"gap": decode_gap_record(store.get_meta(GAP_WINDOW_KEY))}
```

- [ ] **Step 5: 前端**

1. 在現有兩顆按鈕旁加 `<button id="catchup-btn" onclick="catchupNow()" title="走 run_daily.sh --catchup：完整 daily（掃描＋狙擊重挖＋推播），給關機重開或鎖卡住後用">補跑 daily</button>` 與一個 `<span id="gap-pill" class="pill" hidden></span>`。
2. `renderScanStatus`：`st.running` 時 `catchup-btn` 一起 disable；否則放行、文字 `補跑 daily`。
3. 新增：

```js
async function pollGap(){
  let g; try{ g = (await api("/api/schedule/gap")).gap; }catch(e){ return; }
  const pill = document.getElementById("gap-pill");
  if(!g){ pill.hidden = true; return; }
  const win = `${g.uncovered_from.slice(5,16).replace("T"," ")} ～ ${g.uncovered_to.slice(5,16).replace("T"," ")}`;
  pill.hidden = false;
  if(g.caught_up_at){
    pill.className = "pill";
    pill.textContent = `上次漏跑 ${g.missed_slots} 格（${win}），已於 ${g.caught_up_at.slice(5,16).replace("T"," ")} 補跑`;
  }else{
    pill.className = "pill stale";
    pill.textContent = `⚠️ 漏跑 ${g.missed_slots} 格（${win}），尚未補跑`;
  }
}

async function catchupNow(){
  const btn = document.getElementById("catchup-btn");
  btn.disabled = true; btn.textContent = "啟動中…";
  try{
    const r = await api("/api/daily-run", {method:"POST"});
    toast(r.message || (r.started ? "補跑已啟動" : "已經有一輪在跑"));
    wasRunning = true;
  }catch(e){
    toast("啟動失敗：" + e.message);
    btn.disabled = false; btn.textContent = "補跑 daily";
    return;
  }
  clearTimeout(scanPollTimer);
  scanPollTimer = setTimeout(pollScanStatus, 1500);
}
```

4. `pollScanStatus` 每次執行結尾呼叫 `pollGap()`（跑完那一輪會把帳標成已補跑，pill 要跟著變）。頁面初始化處呼叫一次 `pollGap()`。

- [ ] **Step 6: 跑測試 ＋ 端到端（主線程親跑，另開 8322，不動使用者的 8321）**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_daily_run_web.py tests/test_scan_high_web.py -q 2>&1 | tail -3` → 全綠。
E2E：`cd /Users/jim/projects/ygo-sniper && (.venv/bin/uvicorn web.app:app --port 8322 &) ; sleep 2; curl -s localhost:8322/api/schedule/gap; echo; curl -s -X POST localhost:8322/api/daily-run; echo` → 第二個回應 `started` 為 true 或帶明確 message；隨後 `tail -3 data/logs/daily-$(date +%Y%m%d).log` 看到新的「開始」行與 `[catchup] 補跑模式：--catchup`。看完 `pkill -f "uvicorn web.app:app --port 8322"`。
（注意：E2E 這一步會真的跑一輪 daily 並推 Telegram，是生產路徑；主線程決定要不要跑，builder 不跑。）

- [ ] **Step 7: Commit**

```bash
git add scripts/run_daily.sh web/app.py web/static/index.html tests/test_daily_run_web.py
git commit -m "feat(dashboard): 補跑 daily 按鈕（走 run_daily.sh --catchup）與漏跑視窗狀態"
```

---

### Task 7 @inline：文件

**Files:** `CLAUDE.md`、`docs/run_daily.md`、`docs/dashboard.md`

- [ ] `CLAUDE.md` 第五節末尾加一段「漏跑視窗帳」：鍵名、誰寫、誰消耗（送達才標 caught_up）、只記低價帶、自動補跑的兩個門檻鍵；第三節第八項把 `schedule_watch.gap_alert` 改成 `schedule_watch.schedule_health`（現名）；第九節指令表加 `ygo-sniper daily --catchup`，`make test` 的數字改成實測值。
- [ ] `docs/run_daily.md` 加「參數透傳」與「dashboard 按鈕走這支腳本」。
- [ ] `docs/dashboard.md` 加按鈕說明一段（三顆按鈕各做什麼、何時該按哪顆）。
- [ ] Commit：`docs: 補跑模式與漏跑視窗帳；yahoo_direct 改版備註`

---

> **E2E 實跑（2026-10-20 22:20–22:28 CDT，主線程）**：8322 dashboard `POST /api/daily-run` → `run_daily.sh --catchup` 被拉起（鎖 pid 80375、行程樹帶 `--catchup`）；跑步中第二次 POST 回 `started:false`（scan_status 預檢）；日誌有 `[catchup] 補跑模式：--catchup；每個發現查詢多翻 1 頁`、`yahoo_direct: ok count=559`、`[catchup] 強制重挖狙擊成交檔案`、`#9 魔法の筒：成交檔案新增 11 筆`、**推播 19 則**（Yahoo 來源恢復後的第一輪）、`漏跑期間沒有狙擊卡成交，無回顧可送；視窗標記為已補跑`，`exit=0`，7.5 分鐘；`/api/schedule/gap` 回帳上 `caught_up_at` 已填。
> 注意這一輪的「漏跑 2 個時段（21:00、21:30）」是 Chicago 時區基準下的假漏格（見上方重要發現），帳因此被建立並立刻標已補跑，行為正確但輸入是假的。

## Task 8 @inline：複審修正（2026-10-20 reviewer 三條 Warning，主線程裁決）

**Files:** `src/ygo_sniper/pipeline.py`（`_decide_catchup`）、`src/ygo_sniper/schedule_watch.py`（`resolve_gap_record`）、`src/ygo_sniper/cli.py`（`daily`）、`tests/test_cli_notify.py`（`_catchup_env`＋新測試）、`tests/test_schedule_watch.py`、`tests/test_pipeline_catchup.py`、`docs/run_daily.md:120`、`docs/dashboard.md:166`、`web/static/index.html:2265` 附近註解。

- [ ] **W1 已補跑過的視窗被手動 `--catchup` 再推一次**：`_decide_catchup` 改成
  `self._catchup_record = rec if (rec and rec.get("caught_up_at") is None) else None`
  （已補跑的帳不再是「這一輪要回顧的對象」；`--catchup` 仍會多翻頁與強制重挖，只是沒有回顧可送）。
  測試（`tests/test_cli_notify.py`）：帳上 `caught_up_at` 非 None 時跑 `daily --catchup` → `send_alert` 呼叫數 0、`caught_up_at` 原值不變。
- [ ] **W2 未達門檻的視窗永遠不消耗、跨天合併**：兩個修正——
  1. `resolve_gap_record` 的合併條件改成**連續**才合併：`merge = prev_rec is not None and prev_rec.get("caught_up_at") is None and prev_rec.get("uncovered_to") == prev.isoformat()`（上一筆的 `uncovered_to` 正是這一輪的 `prev_started` ＝ 中間沒有任何正常輪次）。不連續就換新帳。
     測試（`tests/test_schedule_watch.py`）：10-12 漏 1 格、10-14 正常跑、10-16 又漏 1 格 → 10-16 的帳 `uncovered_from` 是 10-16 的 prev、`missed_slots == 1`（不合併）；既有 `test_gap_record_extends_uncaught_window` 的情境（連續）仍合併。
  2. 正常輪次收尾時把**未達門檻**的帳標掉：`cli.daily` 在 `_run_notifications` 之後、非補跑模式且帳存在、`caught_up_at is None`、`missed_slots < catchup_min_missed_slots` → `mark_gap_caught_up(now)` 並印 `[dim]漏跑 N 格未達補跑門檻 M，由本輪正常掃描收尾[/dim]`。語意：沒有要送達的東西，所以不違反「送達才消耗」。
     測試（`tests/test_cli_notify.py`）：`missed_slots=1`、門檻 2、非補跑 → 收尾後 `caught_up_at` 非 None、`send_alert` 呼叫數 0。
- [ ] **W3 測試時區外洩**：`_catchup_env` 的 `monkeypatch.setenv("TZ", ...)` 後加 `monkeypatch.undo` 鉤子或 `request.addfinalizer(time.tzset)`——照 `tests/test_catchup.py` 既有 fixture 的寫法，undo 之後再呼叫一次 `time.tzset()`。
  驗證：`.venv/bin/pytest tests/test_cli_notify.py -p no:randomly` 跑完後，在同一行程再跑一支臨時測試印 `time.tzname`，必須是機器時區（這台是 `('CST', 'CDT')`）而不是 Taipei；臨時測試不進 repo。
- [ ] **S1 `_decide_catchup` 包 try/except**：設定壞掉（例如 `catchup_min_missed_slots:` 留空）→ 印 `[warn] 補跑模式判定失敗（本輪不補跑）：…`，`_catchup_active=False`、`_catchup_record=None`，不讓 `scan_status` 卡在 running。測試（`tests/test_pipeline_catchup.py`）：`scan_overrides={"catchup_min_missed_slots": None}` → `scan()` 不拋、`result["catchup"]["active"] is False`。
- [ ] **S3 裁決**：`dry_run` 進補跑模式多翻頁**維持現狀**（dry-run 的語意是「不寫庫」，不是「不照正式路徑抓」），plan Task 4 Step 6.4 那句「dry_run 也要帶 active: False」作廢，以測試為準。
- [ ] **文件順序**：`docs/run_daily.md:120`、`docs/dashboard.md:166`、`web/static/index.html` 補跑寬限期註解——腳本實際是**先 `mkdir` 取鎖、再 ping 等網路**，三處改正。
- [ ] 驗收：`.venv/bin/pytest tests/test_cli_notify.py tests/test_schedule_watch.py tests/test_pipeline_catchup.py tests/test_catchup.py` 全綠；`make test` 全綠（前景）；commit：`fix(catchup): 複審修正——已補跑帳不重推、視窗只在連續時合併、未達門檻由正常輪收尾、測試時區不外洩、補跑判定不拖垮掃描`。

未採納（reviewer S2「重送回顧與多翻頁／強制重挖拆開」）：送不出去的期間每輪多翻一頁與重挖是有界成本（實測重挖約 1–2 分鐘），而把「帳未消耗」拆成兩種狀態會讓 pill 與回顧的語意變複雜；先觀察，預註冊觀察項已含「每天都觸發就要看」。

## 驗收總表（主線程親跑）

| # | 指令 | 預期 |
|---|---|---|
| 1 | `make test \| tail -3` | 全綠，數字 ≥ 2043 |
| 2 | `ygo-sniper scan --dry-run \| grep yahoo_direct:` | `ok count=<正整數>` |
| 3 | Task 5 Step 6 的唯讀錨值腳本 | 三行輸出符合 |
| 4 | `grep -n "catchup" config/settings.yaml scripts/run_daily.sh` | 兩個鍵＋`"$@"` 都在 |
| 5 | `git diff --stat main@{1}` 範圍 | 只有 File Structure 表列的檔案 |

## 預註冊的觀察項（上線後一週看，不要憑感覺調）

- 補跑模式實際觸發次數與每次的 `[catchup]` 行（`grep -h "\[catchup\]" data/logs/daily-*.log`）；若每天都觸發，`catchup_min_missed_slots` 太低或 launchd 時段本身有問題，先看 log 再調。
- 回顧訊息是否有 exact/partial 命中過；連續兩週全是「另有 N 筆 near」就把 near 的門檻資訊交給使用者決定是否也列。
