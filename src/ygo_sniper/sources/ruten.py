"""露天拍賣（ruten.com.tw）直抓——**專案裡第一個台灣境內市場的行情來源**。

## 為什麼需要它（讀這段就懂整個檔案的取捨）

在此之前所有 venue 係數（Yahoo ×1.00 / Mercari ×2.14 / PayPay ×2.60）都是
**日本境內**市場，而買方成本模型假設貨要運到台灣。「在日本 A 站買、在日本
B 站賣」看起來有 2 倍價差，但那條路徑要求貨留在日本；貨一旦運到台灣，要賣
回日本就得再付一次國際運費。所以物理上單純可行的路徑其實是
「在日本買、在台灣賣」——而我們對台灣的價格完全沒有資料。這個檔案補的是
那一塊，`Site.RUTEN` 也是唯一一個買賣雙方都在台灣的 Site。

## 三個平台的可行性實測（2026-08-02，全部以 httpx 生產路徑實抓）

| 平台 | 結果 | 證據 |
|---|---|---|
| **露天** ruten.com.tw | ✅ 可抓 | 公開 JSON API、無 WAF、`遊戲王 PSA` 命中 8392 筆、一頁 100 筆 |
| 蝦皮 shopee.tw | ❌ 不可抓 | 搜尋頁是 SPA 外殼（163KB 無商品資料）；`/api/v4/search/search_items` 回 **HTTP 403**（`error: 90309999`，需登入態與簽章） |
| Carousell TW | ❌ 樣本不足 | SSR 資料抓得到（`window.initialState` 前一個 `<script>`），但 `遊戲王 PSA` 全站只有 **numberOfResults=3** 筆，且無成交價 |

## 露天的 API 形狀（兩段式，這是本檔最容易做錯的地方）

搜尋 API **只回 ID**，商品欄位在另一支 API：

    搜尋  https://rtapi.ruten.com.tw/api/search/v3/index.php/core/prod
          ?q={kw}&type=direct&sort={sort}&offset={1-based}&limit={<=100}
          → {"TotalRows": 8392, "Rows": [{"Id": "2233…"}, …]}   ← 只有 Id 有用
    詳情  https://rtapi.ruten.com.tw/api/prod/v2/index.php/prod?id={id1,id2,…}
          → [{"ProdId","ProdName","Currency","PriceRange","SoldQty",
              "StockQty","SaleType","PostTime","SellerId","ShippingCost",…}]

所以**一頁 = 兩個請求**（實測 100 個 id 一次詳情可以拿完，66KB）。

## 四個實測得到、憑印象一定會做錯的地方

1. **`Currency` 不一定是 TWD**：實測 100 筆有 6 筆是 `"USD"`（海外賣家的英文
   標題）。硬當台幣會把 US$79.8 算成 NT$79.8，低估 31 倍。所以幣別一律讀
   `Currency` 欄位，**認不得的幣別整筆丟掉**（算 malformed，不猜）。
   反過來，台幣標價**絕不可以再套一次匯率或刷卡加成**——那正是 `costs.py`
   註記裡 Mercari 台灣踩過的坑（NT$5,751 當日圓 → 低估 4.7 倍）。
2. **價格篩選參數會被靜默忽略**：`prc.now` / `priceRange` / `min_price` 三種
   寫法實測 `TotalRows` 與第一筆完全不變（另一方面未知的 `sort` 值會回
   HTTP 400——所以「沒報錯」不代表「有生效」）。⇒ `max_price` 只能**解析後
   在本地過濾**，不可以塞一個參數然後假裝平台幫我們濾了。
3. **查無結果是合法的 200，但 body 只有 49 bytes**
   （`{"TotalRows":0,"Rows":[],"LimitedTotalRows":0}`）。`CachedFetcher` 的
   `MIN_BODY_BYTES=512` 會把它判成 BLOCKED——「確認沒貨」被誤診成「被擋」，
   正是 `health.py` 開頭那段事故的反面。所以本檔的 fetch 一律帶
   `min_bytes=_MIN_JSON_BYTES`。
4. **`PriceRange` 是區間**（多規格商品，實測 2/100）。取下緣當「現在買得到的
   最低價」，上緣留在 `raw["price_max"]`——不留的話下游永遠看不出這筆是區間。

## 「成交價」抓得到什麼、抓不到什麼（`supports_sold=True` 的依據與極限）

- **沒有已售出／已結束的搜尋**：`type=finished`、`type=all`、`type=auction`
  三種值實測全部回 **HTTP 400**；`type` 只吃 `direct`（定價直購）。
- **但每筆商品自己帶 `SoldQty`（累計已賣出件數）**。`SoldQty >= 1` 代表
  「這個價格在台灣真的成交過」——這比在架價強得多，在架價只是賣家開的價。
- ⚠️ **極限（不可忽略）**：`SoldQty` **沒有時間戳**。商品頁的 `PostTime` 是
  上架時間（實測有 2021 年的），ld+json 的「價格更新時間」是改價時間，兩者
  都不是成交時間。所以走 `sold=True` 產出的 Listing **不帶 `raw["sold_at"]`**，
  `comps.ingest_sold` 會蓋上入庫時間並標 `sold_at_is_ingest=1`——與 Buyee 系
  同一個降級路徑。寧可讓下游看得見「這個時間是假的」，也不要猜一個成交日期。
- 產出率：實測相關度排序第 1 頁 100 筆裡 `SoldQty>0` 的只有 4 筆。

## 這條管道**刻意不進**在架掃描與 comps 累積

`build_sources()` 有註冊它（canary 會每輪自檢解析器還活著），但
`watchlist.queries[].sources` 與 `comps_queries.sources` 都**沒有**列它。
理由是 `valuation.venue_premium_prior` 裡沒有 `ruten` 的係數，而台灣價格水準
與日本三站不同：讓 TWD 成交價混進同一個 comps 索引，等於用台灣行情去估
日本標的的公允價（工程原則 1 的混源比較），而且方向不明、沒有外顯症狀。
要啟用之前必須先量出 ruten 的 venue 係數並寫進先驗——那是另一個決定。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

from ..config import Config
from ..domain import Currency, Listing, Site
from .base import BlockedError, CachedFetcher, FetchError
from .health import ParseHealth, SearchResult

_SEARCH_BASE = "https://rtapi.ruten.com.tw/api/search/v3/index.php/core/prod"
_PROD_BASE = "https://rtapi.ruten.com.tw/api/prod/v2/index.php/prod"
#: 人看得到、也買得到的商品頁（`Listing.url` 的約定：一律放買得到的那一端）
_ITEM_URL = "https://www.ruten.com.tw/item/show?{id}"
#: 圖片 CDN。詳情 API 的 `Image` 是 `/g1/f/03/29/{id}_608_m.jpg` 這種相對路徑。
_IMAGE_BASE = "https://gcs.rimg.com.tw"

#: 一頁筆數。實測 `limit=100` 生效（100 筆），且 100 個 id 一次詳情拿得完。
_PAGE_SIZE = 100

#: JSON API 的「body 太短 = 被擋」門檻。查無結果的合法回應只有 49 bytes，
#: 用 HTML 的 512 判它會把「確認沒貨」誤診成「被擋」（見模組 docstring 第 3 點）。
#: 20 bytes 仍然擋得住空字串與截斷的殘檔——最短的合法回應是 46 bytes。
_MIN_JSON_BYTES = 20

#: 只吃 `direct`（定價直購）。`auction`／`all`／`finished` 實測全回 HTTP 400。
_TYPE = "direct"
#: 新上架優先。實測 `sort=new/dc` 生效（第 1 筆與 `rnk/dc` 不同）；
#: ⚠️ 未知排序鍵會回 **HTTP 400**（不是靜默忽略），所以改值一定會當場炸，
#: 不會像 Yahoo 的 `s1=start` 那樣安靜地什麼都沒做。
_SORT_NEWEST = "new/dc"
_SORT_DEFAULT = "rnk/dc"

#: 露天商品 ID：14 位純數字（實測 `22332436825897`、`30263123555117`）。
#: 只收看得懂的形狀——猜錯 ID 空間會產出買不到的連結（與 paypay.py 同一條教訓）。
_PROD_ID_RE = re.compile(r"^\d{10,20}$")

#: `PostTime` 是台北時間的 `2023/08/12 01:31:13`（無時區標記）。
_TAIPEI = timezone(timedelta(hours=8))

#: 平台幣別字串 → 專案 Currency。**認不得的一律丟掉，不預設台幣。**
_CURRENCY = {"TWD": Currency.TWD, "USD": Currency.USD, "JPY": Currency.JPY}


class RutenSource:
    """露天拍賣直抓 source。發現端與購買端都是 ruten.com.tw（貨在台灣）。"""

    name = "ruten"
    site = Site.RUTEN
    #: `SoldQty >= 1` = 這個價格在台灣真的成交過。**但沒有成交時間**
    #: （見模組 docstring 的極限說明），下游會標 `sold_at_is_ingest=1`。
    supports_sold = True

    def __init__(self, cfg: Config, fetcher: CachedFetcher | None = None) -> None:
        self.cfg = cfg
        self.fetcher = fetcher or CachedFetcher(cfg)
        src_cfg = cfg.sources.get(self.name, {})
        self.sort_newest = bool(src_cfg.get("sort_newest", True))

    # ------------------------------------------------------------------
    def build_url(self, keyword: str, *, page: int = 1, max_price: float | None = None) -> str:
        """組搜尋網址。`offset` 是 **1-based 商品 offset**（第 2 頁 = 101）。

        `max_price` 刻意**不進 URL**：實測平台側的價格參數會被靜默忽略
        （模組 docstring 第 2 點），塞一個假裝有效的參數比不塞更危險——
        下游會以為結果已經篩過了。過濾在 `_extract_listings` 本地做。
        """
        params: dict[str, str | int] = {
            "q": keyword,
            "type": _TYPE,
            "sort": _SORT_NEWEST if self.sort_newest else _SORT_DEFAULT,
            "offset": (page - 1) * _PAGE_SIZE + 1,
            "limit": _PAGE_SIZE,
        }
        return f"{_SEARCH_BASE}?{urlencode(params, quote_via=quote)}"

    @staticmethod
    def build_detail_url(ids: list[str]) -> str:
        return f"{_PROD_BASE}?id={','.join(ids)}"

    # ------------------------------------------------------------------
    def search(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        sold: bool = False,
        pages: int | None = None,
        **_,
    ) -> list[Listing]:
        """相容包裝。`sold=True` 只回 `SoldQty >= 1` 的標的（comps 用）。

        抓取層失敗**往上拋**，與 PayPayDirectSource 同一約定：靜默回空清單
        的話，被擋三週你只會看到三週的「台灣沒行情」。
        """
        result = self.search_detailed(keyword, max_price=max_price, sold=sold, pages=pages)
        if not result.listings:
            if result.health is ParseHealth.BLOCKED:
                raise BlockedError(result.detail or "被擋", url=result.url)
            if result.health is ParseHealth.FETCH_FAILED:
                raise FetchError(result.detail or "抓取失敗", url=result.url, transient=True)
        return result.listings

    def search_detailed(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        sold: bool = False,
        pages: int | None = None,
    ) -> SearchResult:
        pages = pages or self.cfg.max_pages_for(self.name)
        first_url = self.build_url(keyword, page=1, max_price=max_price)
        result = SearchResult(
            source=self.name, site=self.site.value, query=keyword, url=first_url
        )

        seen: set[str] = set()
        skipped_status = 0   # 商業篩選：狀態不符（在架模式碰到只想要的已售出、或反過來）
        over_price = 0       # 商業篩選：超過 max_price（平台側濾不了，只能本地濾）
        malformed = 0        # 欄位不全／幣別認不得＝解析失敗（**不計入 parsed_count**）

        for page in range(1, pages + 1):
            url = self.build_url(keyword, page=page, max_price=max_price)
            try:
                body = self._get(url)
            except BlockedError as exc:
                if page == 1:
                    result.health = ParseHealth.BLOCKED
                    result.detail = f"被擋：{exc}"
                    return result
                print(f"[warn] {self.name} p{page} 被擋，只回傳前 {page - 1} 頁: {exc}")
                break
            except FetchError as exc:
                # 第一頁抓不到 = 對這個關鍵字「什麼都不知道」，跟「沒貨」是兩件事
                if page == 1:
                    result.health = ParseHealth.FETCH_FAILED
                    result.detail = f"抓取失敗：{exc}"
                    return result
                print(f"[warn] {self.name} p{page} 抓取失敗，只回傳前 {page - 1} 頁: {exc}")
                break

            result.pages_fetched = page
            node = _load_json(body)
            ids = self._extract_ids(node)

            if page == 1:
                result.html_bytes = len(body)
                health, detail = self._judge_search_health(node, len(ids))
                if health is not ParseHealth.OK:
                    result.health = health
                    result.detail = detail
                    return result
            elif not ids:
                print(f"[warn] {self.name} p{page} 搜尋結果無 id，只回傳前 {page - 1} 頁")
                break

            # 第二段請求：ID → 商品欄位。這一段失敗跟搜尋失敗一樣是「不知道」，
            # 不是「沒貨」——不能靜靜地把這一頁當成 0 筆。
            try:
                detail_body = self._get(self.build_detail_url(ids))
            except BlockedError as exc:
                if page == 1:
                    result.health = ParseHealth.BLOCKED
                    result.detail = f"詳情 API 被擋：{exc}"
                    return result
                print(f"[warn] {self.name} p{page} 詳情被擋，只回傳前 {page - 1} 頁: {exc}")
                break
            except FetchError as exc:
                if page == 1:
                    result.health = ParseHealth.FETCH_FAILED
                    result.detail = f"詳情抓取失敗：{exc}"
                    return result
                print(f"[warn] {self.name} p{page} 詳情抓取失敗，只回傳前 {page - 1} 頁: {exc}")
                break

            items = _detail_rows(_load_json(detail_body))
            batch, stats = self._extract_listings(
                items, seen, sold=sold, max_price=max_price
            )
            if page == 1 and stats["parsed"] == 0:
                # 搜尋 API 給了 N 個 id、詳情 API 一筆也解不出來 → 必定是詳情
                # 改版，不可能是市場問題（同一次抓取、同一批 id，同源同基準）。
                result.health = ParseHealth.PARSER_BROKEN
                result.detail = f"搜尋回 {len(ids)} 個 id 但詳情 API 解析出 0 筆"
                return result

            skipped_status += stats["status"]
            over_price += stats["over_price"]
            malformed += stats["malformed"]
            # parsed_count 的語意全來源一致：**商業篩選之前**解析器認得的商品數。
            # 狀態不符與價格上限都是商業決定（解析器好好的），計入；
            # 欄位不全／幣別認不得才是解析失敗，不計入。
            result.parsed_count += stats["parsed"]
            result.listings.extend(batch)
            if len(ids) < _PAGE_SIZE:
                break  # 已是最後一頁

        notes = []
        if skipped_status:
            notes.append(f"排除{'未成交過' if sold else '已售出/無庫存'} {skipped_status} 筆")
        if over_price:
            notes.append(f"排除超過價格上限 {over_price} 筆")
        if malformed:
            notes.append(f"排除欄位不全或幣別不明 {malformed} 筆")
        result.detail = "、".join(notes)
        return result

    # ------------------------------------------------------------------
    def _get(self, url: str) -> str:
        """所有外呼的唯一出口。JSON API 的 body 門檻在這裡一處宣告。"""
        return self.fetcher.get(url, min_bytes=_MIN_JSON_BYTES)

    @staticmethod
    def _extract_ids(node: Any) -> list[str]:
        """搜尋回應 → 商品 id 清單（保序、去重、只收看得懂的形狀）。"""
        rows = node.get("Rows") if isinstance(node, dict) else None
        if not isinstance(rows, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("Id") or "")
            if _PROD_ID_RE.match(pid) and pid not in seen:
                seen.add(pid)
                out.append(pid)
        return out

    def _judge_search_health(self, node: Any, n_ids: int) -> tuple[ParseHealth, str]:
        """三層判定，與 paypay.py／yahoo_closed.py 同構。只在第 1 頁執行。"""
        # 第 3 層先判：連 JSON 都不是 = 對方換了東西（或我們拿到一頁 HTML）
        if not isinstance(node, dict):
            return ParseHealth.PARSER_BROKEN, "搜尋回應不是 JSON 物件：API 格式已改"

        total = node.get("TotalRows")

        # 第 1 層（最強）：命中數交叉比對，同一份 JSON、同源同基準
        if isinstance(total, int) and total > 0 and n_ids == 0:
            return ParseHealth.PARSER_BROKEN, f"API 標示 {total} 件但解析出 0 個 id"

        # 第 2 層：API 自己說沒有結果（實測查無結果 TotalRows=0、200、49 bytes）
        if total == 0:
            return ParseHealth.EMPTY_CONFIRMED, "TotalRows 0 件：確認查無結果"

        if n_ids == 0:
            return ParseHealth.PARSER_BROKEN, "Rows 為空且無 TotalRows 可比對：API 格式已改"

        return ParseHealth.OK, ""

    # ------------------------------------------------------------------
    def _extract_listings(
        self,
        items: list,
        seen: set[str],
        *,
        sold: bool,
        max_price: float | None,
    ) -> tuple[list[Listing], dict[str, int]]:
        """詳情 API 的列 → Listing。回傳 (listings, 計數)。

        `sold=False` 只收「還買得到」的（`StockQty > 0`）；
        `sold=True` 只收「賣掉過」的（`SoldQty >= 1`），而且**不帶成交時間**
        （露天沒有這個欄位，見模組 docstring）。
        """
        out: list[Listing] = []
        stats = {"parsed": 0, "status": 0, "over_price": 0, "malformed": 0}
        for it in items:
            if not isinstance(it, dict):
                stats["malformed"] += 1
                continue
            pid = str(it.get("ProdId") or "")
            title = it.get("ProdName")
            price, price_max = _price_range(it.get("PriceRange"))
            currency = _CURRENCY.get(str(it.get("Currency") or "").upper())
            if not _PROD_ID_RE.match(pid) or not title or price is None or currency is None:
                # 幣別認不得也算解析失敗：猜一個幣別就是猜一個匯率（見頂註第 1 點）
                stats["malformed"] += 1
                continue
            stats["parsed"] += 1
            if pid in seen:
                continue

            sold_qty = _as_int(it.get("SoldQty")) or 0
            stock_qty = _as_int(it.get("StockQty")) or 0
            if sold:
                if sold_qty < 1:
                    stats["status"] += 1
                    continue
            elif stock_qty < 1:
                stats["status"] += 1
                continue

            # 平台側濾不掉，只能在這裡濾——而且要用**標價自己的幣別**比，
            # 不是換算過的台幣：max_price 的呼叫端給的是什麼單位，這裡就用什麼。
            if max_price is not None and price > max_price:
                stats["over_price"] += 1
                continue

            seen.add(pid)
            out.append(
                Listing(
                    site=self.site,
                    external_id=pid,
                    title=str(title),
                    url=_ITEM_URL.format(id=pid),
                    price=price,
                    currency=currency,
                    image_url=_image_url(it.get("Image")),
                    seller_id=str(it.get("SellerId")) if it.get("SellerId") else None,
                    # 露天的運費是逐筆標示的（實測 NT$60），但 `shipping_cost`
                    # 只有 eBay 那條成本路徑會讀（costs._quote_ebay），這裡填了
                    # 也不會進台灣路徑的報價——所以放 raw，不放這個欄位，
                    # 避免「有值但沒人用」看起來像已經算進去了。
                    ships_to_tw=True,          # 貨就在台灣
                    best_offer_enabled=False,  # 露天沒有標準化的議價 API
                    listed_at=_parse_post_time(it.get("PostTime")),
                    is_sold=sold,
                    raw={
                        # 定價直購（`type=direct`），沒有競標語意，所以永遠是 fixed。
                        # ⚠️ sold 模式的價格是「賣掉過的那個定價」，不是拍賣落槌價。
                        "price_kind": "sold_price" if sold else "fixed",
                        "sold_qty": sold_qty,
                        "stock_qty": stock_qty,
                        "shipping_cost_native": _as_number(it.get("ShippingCost")),
                        "sale_type": it.get("SaleType"),
                        # 區間商品（多規格）：price 取的是下緣，上緣留在這裡，
                        # 不留的話下游看不出「這筆的價格有兩個」。
                        **({"price_max": price_max} if price_max != price else {}),
                        # **刻意不放 `sold_at`**：露天沒有成交時間欄位，
                        # 放一個猜的值會讓 comps 的 90 天視窗變成裝飾品。
                    },
                    source=self.name,
                )
            )
        return out, stats


# ---------------------------------------------------------------------------
# 純函式（可單測、不碰網路）
# ---------------------------------------------------------------------------
def _load_json(body: str) -> Any:
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _detail_rows(node: Any) -> list:
    """詳情 API 的回應 → 列清單。實測是裸 list，但包一層 dict 也認得。"""
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        for key in ("data", "Rows", "rows"):
            if isinstance(node.get(key), list):
                return node[key]
    return []


def _as_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_number(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _price_range(raw: Any) -> tuple[float | None, float | None]:
    """`[5000, 5000]` → (5000.0, 5000.0)。取不到或非正數一律 (None, None)。

    下緣＝「現在最少要付多少」，是與其他平台「可立即成交價」對齊的那個口徑。
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None, None
    vals = [v for v in (_as_number(x) for x in raw) if v is not None and v > 0]
    if not vals:
        return None, None
    return min(vals), max(vals)


def _image_url(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    return raw if raw.startswith("http") else _IMAGE_BASE + raw


def _parse_post_time(raw: Any) -> datetime | None:
    """`2023/08/12 01:31:13`（台北時間、無時區標記）→ UTC datetime。

    抽不到一律 None，**絕不用 now() 頂替**：上架時間會被拿去算「掛了多久」，
    塞一個今天的時間會讓每一筆看起來都是剛上架的。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        naive = datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=_TAIPEI).astimezone(UTC)
