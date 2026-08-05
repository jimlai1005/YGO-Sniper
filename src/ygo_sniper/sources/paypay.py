"""Yahoo!フリマ（舊名 PayPayフリマ）直抓——取代走 Buyee 鏡像的 `buyee_paypay`。

發現端是 `paypayfleamarket.yahoo.co.jp`，購買端仍然是 Buyee
（`site=Site.BUYEE_PAYPAY`，成本模型與去重鍵零修改），與 `yahoo_direct` 同一個模式。

**為什麼換掉 Buyee 鏡像**（2026-08-02 實測，同一個關鍵字 `遊戯王 PSA 初期`）：

| | 走 Buyee | 直抓原站 |
|---|---|---|
| 一頁筆數 | 40 | **100** |
| 耗時 | 2.66s | 0.86s |
| 依賴 | Playwright＋chromium 解 AWS WAF | 純 httpx |
| 回應大小 | 212KB | 432KB |

價格逐筆相同（使用者已對帳 6 筆，Buyee 未加成），所以這是純粹的覆蓋率與
依賴度改善，不影響任何成本計算。

**解析走 `__NEXT_DATA__`，不用 CSS selector**（與 `yahoo_closed.py` 同理）：
這頁是 Next.js + styled-components，class 名是每次 build 都會變的雜湊
（`sc-1cd37ae5-22 cREsOc`），拿它當地標等於保證下次部署就壞。

## 三個實測得到、憑印象一定會做錯的地方

1. **搜尋頁是路徑形式**：`/search/{關鍵字}`。query 參數形式（`/search?query=…`）
   回 404 空頁。分頁是 `?page=N`（1-based，實測 p2 的 `result.offset` = 100）。
2. **查無結果回 HTTP 404 ＋完整頁面**（實測 122KB，`isZeroMatch: true`、
   `totalResultsAvailable: 0`）——與 Yahoo 拍賣同一個陷阱，fetch 必須帶
   `allow_statuses=(404,)`，否則「今天沒貨」會被當成「抓取失敗」。
3. **結果混著ヤフオク!的標的**：id 不是 `z`+數字的那些（實測每頁 1-3 筆，
   如 `h1238186452`，endTime = openTime+3 天＝競標結標）是 Yahoo 拍賣的貨，
   購買路徑是 `buyee.jp/item/yahoo/auction/{id}`，**不是** paypayfleamarket。
   照單全收會產出買不到的連結、還會把兩個 ID 空間混進同一個 `Listing.key`。
   所以只收 `z\\d+`，其餘計入 `parsed_count`（解析器是活的）但排除出 listings。

## 「已售出」怎麼抓（實測結論，`supports_sold=True` 的依據與極限）

- **沒有 sold 篩選參數**：URL 的 `itemStatus=sold`／`itemStatus=SOLD` 伺服器端
  一律靜默忽略（SSR 的 `searchParams` 不變、結果與不帶參數逐筆相同）。
  `sort=endTime&order=ASC`（想讓已結束的排前面）也不行——`order` 被強制成 DESC。
- **但已售出的標的會混在一般搜尋結果裡，而且帶真實成交時間**：
  `itemStatus == "SOLD"` 者的 `endTime` 落在 `openTime` 與現在之間
  （實測 53/53 筆成立），而在架商品的 `endTime` 一律是 `openTime + 1 年`
  （上架期限，45/47 筆），兩者一眼可分。
- 產出率：不帶價格上限約 2/100；`maxPrice=5000` 那頁是 53/100
  （便宜的東西賣得快）。**這是有偏的樣本**（只抓得到「最近賣掉且還留在
  搜尋索引裡」的），但時間戳是真的——而 Buyee 那條路連時間都沒有
  （已售出頁與商品頁都不含任何日期，見 `store.sold_at_is_ingest` 的註記）。
  比起把入庫時間當成交時間，寧可少收、收真的。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from bs4 import BeautifulSoup

from ..config import Config
from ..domain import Currency, Listing, Site
from .base import BlockedError, CachedFetcher, FetchError
from .health import ParseHealth, SearchResult
from .yahoo_closed import to_utc_iso

_SEARCH_BASE = "https://paypayfleamarket.yahoo.co.jp/search/"
#: 賣家頁（Seller Alpha 的輪替監控）。2026-08-03 實測：純 httpx 200、181KB，
#: `__NEXT_DATA__` 的結果節點與搜尋頁**同一路徑**（`_RESULT_PATH`），
#: 所以解析完全重用 `_extract_listings`（含 SOLD/OPEN 分流與真實 endTime）。
#: ⚠️ `/user/profile/{id}` 是 404，正確形狀只有 `/user/{id}`。
_SELLER_BASE = "https://paypayfleamarket.yahoo.co.jp/user/"
#: 購買端（`Listing.url` 的約定：一律放買得到的那一端）
_BUYEE_ITEM_URL = "https://buyee.jp/paypayfleamarket/item/{id}"
#: 發現端原生頁（`Listing.origin_url`）
_ORIGIN_ITEM_URL = "https://paypayfleamarket.yahoo.co.jp/item/{id}"

#: 實測一頁 100 筆（`totalResultsReturned: 100`）。用來判斷「這已經是最後一頁」。
_PAGE_SIZE = 100

#: 走 Buyee 鏡像（舊的 `buyee_paypay` 管道）時抓到的縮圖 CDN 主機。
#: 改直抓之後 `thumbnailImageUrl` 給的是 Yahoo 原站 CDN，但**已經落庫的列不會
#: 自己更新**——庫裡因此躺著兩種主機名的同一種圖（2026-08-03 實測 4 筆舊／8 筆新）。
_BUYEE_IMAGE_HOST = "cdnyauction-pctr.buyee.jp"
#: Yahoo 原站的縮圖 CDN，也就是 `paypay_direct` 與 `yahoo_direct` 現在存的那個。
_YAHOO_IMAGE_HOST = "auc-pctr.c.yimg.jp"


def canonical_thumbnail_url(url: str | None) -> str | None:
    """把 Buyee 鏡像 CDN 的縮圖網址收斂成 Yahoo 原站 CDN。其餘一律原樣回傳。

    兩個主機的路徑結構完全相同（`/i/auctions.c.yimg.jp/…` 之後逐字相同），
    2026-08-03 逐筆實測：4 筆舊資料換主機後 HTTP 200、`content-type: image/jpeg`、
    **sha1 與原網址回來的位元組完全相同**——同一張圖的兩個門牌。

    為什麼還是要換：舊主機是 Buyee 的代理，它的存取政策（referer／session／
    hotlink）是 Buyee 說了算，而我們對它沒有任何影響力；原站 CDN 是這個欄位
    **現在**的唯一來源。讓同一種資料只有一種形狀，才不會有「舊列與新列行為
    不一樣」這種只在特定幾筆上發作的病（工程原則 1：同源同基準）。

    冪等：換過的網址主機已經是原站，再跑一次不會變。
    """
    if not url or _BUYEE_IMAGE_HOST not in url:
        return url
    return url.replace(_BUYEE_IMAGE_HOST, _YAHOO_IMAGE_HOST, 1)

#: Yahoo!フリマ 的 ID 空間。`z`+數字是フリマ商品；其他形狀是混進來的ヤフオク!標的，
#: 購買路徑完全不同（見模組 docstring 第 3 點）。這裡刻意**只收看得懂的**：
#: 猜錯 ID 空間會產出買不到的連結，而少收一筆只是少一筆。
_FLEA_ID_RE = re.compile(r"^z\d+$")

#: `__NEXT_DATA__` 裡到搜尋結果節點的路徑。寫成常數，讓「頁面改版」在
#: `_result_node()` 一處失敗、一處告警。
_RESULT_PATH = ("props", "initialState", "searchState", "search", "result")

#: 新着降冪（2026-08-02 實測）。`sort=openTime` 會被 SSR 採用（`searchParams.sort`
#: 跟著變成 `openTime`，第 1 筆的 `openTime` 是當天最新的一筆）；
#: ⚠️ 選單上顯示的 `newer`／`recommended` 那組值是**前端 enum**，送進 URL 會被
#: 靜默忽略（`searchParams.sort` 仍是 `ranking`）——兩者不可互換，改之前要重測
#: 「第 1 筆的 openTime 是不是真的變成今天」。
_SORT_NEWEST_PARAMS = {"sort": "openTime", "order": "DESC"}


#: `category` 值的前綴 → 送出的參數名。
#:
#: 為什麼要兩種：`categoryIds=2511,2420`（トレーディングカード）**不是遊戲王
#: 專屬的**——2026-08-03 實測，那個分類下的搜尋結果有 31-50/100 筆是ポケモン
#: 與ワンピース。真正遊戲王專屬的過濾器是**品牌**
#: （`brandIds=167476` ＝「遊戯王オフィシャルカードゲーム デュエルモンスターズ」，
#: 品牌頁 `/brand/167476` 自己送的就是這個參數，實測 84,620 件）。
#: 兩者都保留：分類是上位概念（未來想擴到別的 TCG 用得到），品牌才是這個
#: 專案要的那一刀。寫成前綴而不是多開一個欄位，是因為它們在管線裡佔的是
#: 同一個位置（`category`），多開欄位會讓每一層都要多帶一個參數。
_CATEGORY_PREFIXES = {"brand:": "brandIds", "category:": "categoryIds"}


def _category_params(category: str) -> dict[str, str]:
    """`"brand:167476"` → `{"brandIds": "167476"}`；無前綴 → `categoryIds`。"""
    for prefix, param in _CATEGORY_PREFIXES.items():
        if category.startswith(prefix):
            return {param: category[len(prefix):]}
    return {"categoryIds": category}


def _dig(obj: Any, path: tuple[str, ...]) -> Any | None:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


class PayPayDirectSource:
    """Yahoo!フリマ 直抓 source。發現管道是原站，購買路徑仍是 Buyee。"""

    name = "paypay_direct"
    site = Site.BUYEE_PAYPAY
    #: 有真實成交時間的已售出樣本（產出率低、有偏，見模組 docstring）
    supports_sold = True
    #: 吃得下 `category`（→ URL 的 `categoryIds`）。2026-08-03 實測：
    #: `catId`／`categoryId`／`brandId` 三個名字**全部被靜默忽略**
    #: （SSR 的 `searchParams` 完全不變、`totalResultsAvailable` 逐位相同），
    #: 只有 **`categoryIds`** 會進 `searchParams`。值是**分類路徑**、逗號分隔
    #: （分類頁 `/category/2420` 自己送的就是 `categoryIds=2511,2420`
    #: ＝ ゲーム、おもちゃ → トレーディングカード）。
    #: ⚠️ 參數名不可憑印象改：改錯的症狀是「照樣有結果、但分類根本沒生效」。
    supports_category = True

    def __init__(self, cfg: Config, fetcher: CachedFetcher | None = None) -> None:
        self.cfg = cfg
        self.fetcher = fetcher or CachedFetcher(cfg)
        src_cfg = cfg.sources.get(self.name, {})
        self.sort_newest = bool(src_cfg.get("sort_newest", True))

    # ------------------------------------------------------------------
    def build_url(
        self,
        keyword: str,
        *,
        page: int = 1,
        max_price: float | None = None,
        category: str | None = None,
        seller: str | None = None,
    ) -> str:
        """組搜尋網址。關鍵字在**路徑**上（query 參數形式是 404）。

        `category` → `categoryIds`（見類別屬性 `supports_category` 的實測註記）。
        `seller` → 改組**賣家頁**網址（`/user/{id}`）：那一頁沒有關鍵字、
        沒有價格上限的概念，所以其餘參數一律不帶——帶了也只是被靜默忽略，
        而「參數被靜默忽略」正是這個站最容易騙人的地方。
        """
        if seller:
            base = _SELLER_BASE + quote(str(seller))
            return f"{base}?page={page}" if page > 1 else base
        params: dict[str, str | int] = {}
        if category:
            params.update(_category_params(category))
        if page > 1:
            params["page"] = page
        if max_price:
            # 平台側過濾，實測生效且是閉區間（maxPrice=5000 → 最大值正好 5000）
            params["maxPrice"] = int(max_price)
        if self.sort_newest:
            params.update(_SORT_NEWEST_PARAMS)
        url = _SEARCH_BASE + quote(keyword)
        return f"{url}?{urlencode(params)}" if params else url

    # ------------------------------------------------------------------
    def search(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        sold: bool = False,
        pages: int | None = None,
        category: str | None = None,
        **_,
    ) -> list[Listing]:
        """相容包裝。`sold=True` 走已售出模式（comps 用）。

        抓取層失敗**往上拋**，與 BuyeeSource／YahooClosedSource 同一約定：
        `refresh_comps` 靠例外印警告，靜默回空清單的話，被擋三週你只會看到
        三週的「comps 沒有新資料」。
        """
        result = self.search_detailed(
            keyword, max_price=max_price, sold=sold, pages=pages, category=category
        )
        if not result.listings:
            if result.health is ParseHealth.BLOCKED:
                raise BlockedError(result.detail or "被擋", url=result.url)
            if result.health is ParseHealth.FETCH_FAILED:
                raise FetchError(result.detail or "抓取失敗", url=result.url, transient=True)
        return result.listings

    def search_seller(
        self, seller_id: str, *, pages: int | None = None, sold: bool = False
    ) -> SearchResult:
        """單一賣家的清單（Seller Alpha 的輪替監控與**歷史成交挖掘**）。

        **固定 1 頁**：實測一個賣家的全部品項是 38 筆（`totalResultsAvailable`），
        而一頁裝 100 筆——翻頁在這個量級上沒有意義，而且賣家頁的 `page` 參數
        還沒實測過（未經實測的參數在這個站上可能被靜默忽略）。`pages` 收在
        介面上是為了與其他 source 同形，傳大於 1 會被降回 1 並印警告。

        `sold=True` 只收這一頁上**已售出**的標的（`itemStatus != OPEN` 且
        `endTime` 抓得到）——賣家頁與搜尋頁是同一個結果節點，所以分流邏輯
        完全重用 `_extract_listings`，判準只有一份。

        2026-08-04 實測（`p47632545`）：**一個請求換 73 筆真實成交**
        （100 筆裡 27 OPEN／73 SOLD），成交時間橫跨 2026-02-06 → 08-02
        ＝ 178 天。這條路是「持續性」判定唯一不必等的資料來源：
        帳本不用從今天開始累積，賣家頁自己就帶著半年的歷史。
        """
        if pages and int(pages) > 1:
            print(f"[warn] {self.name} 賣家頁翻頁尚未實測，pages={pages} 降為 1")
        return self.search_detailed("", pages=1, seller=str(seller_id), sold=sold)

    def search_detailed(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        sold: bool = False,
        pages: int | None = None,
        category: str | None = None,
        seller: str | None = None,
    ) -> SearchResult:
        pages = pages or int(self.cfg.fetch["max_pages_per_query"])
        first_url = self.build_url(
            keyword, page=1, max_price=max_price, category=category, seller=seller
        )
        result = SearchResult(
            source=self.name, site=self.site.value,
            query=keyword or (f"seller:{seller}" if seller else ""), url=first_url,
        )

        seen: set[str] = set()
        skipped_status = 0   # 商業篩選：狀態不符（在架模式碰到 SOLD、或反過來）
        foreign_ids = 0      # 混進來的ヤフオク!標的（ID 空間不同，購買路徑不同）
        malformed = 0        # 欄位不全＝解析失敗（**不計入 parsed_count**）

        for page in range(1, pages + 1):
            url = self.build_url(
                keyword, page=page, max_price=max_price, category=category, seller=seller
            )
            try:
                html = self.fetcher.get(url, allow_statuses=(404,))
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
            node = self._result_node(html)

            if page == 1:
                result.html_bytes = len(html)
                health, detail = self._judge_health(node)
                if health is not ParseHealth.OK:
                    result.health = health
                    result.detail = detail
                    return result
            elif node is None:
                print(f"[warn] {self.name} p{page} 找不到 __NEXT_DATA__，只回傳前 {page - 1} 頁")
                break

            raw_items = (node or {}).get("items") or []
            batch, stats = self._extract_listings(raw_items, seen, sold=sold)
            skipped_status += stats["status"]
            foreign_ids += stats["foreign"]
            malformed += stats["malformed"]
            # parsed_count 的語意全來源一致：**商業篩選之前**解析器認得的商品數。
            # 狀態不符與ヤフオク!混入都是商業／路由決定（解析器好好的），計入；
            # 欄位不全才是解析失敗，不計入。
            result.parsed_count += stats["parsed"]
            result.listings.extend(batch)
            if len(raw_items) < _PAGE_SIZE:
                break  # 已是最後一頁

        notes = []
        if skipped_status:
            notes.append(
                f"排除{'在架' if sold else '已售出'} {skipped_status} 筆"
            )
        if foreign_ids:
            notes.append(f"排除非フリマ ID {foreign_ids} 筆")
        if malformed:
            notes.append(f"排除欄位不全 {malformed} 筆")
        result.detail = "、".join(notes)
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _result_node(html: str) -> dict | None:
        """挖出 `__NEXT_DATA__` 裡的搜尋結果節點。任何一步失敗都回 None
        （由 `_judge_health` 統一判成 PARSER_BROKEN 並告警）。"""
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag is None:
            return None
        try:
            payload = json.loads(tag.get_text())
        except (ValueError, TypeError):
            return None
        node = _dig(payload, _RESULT_PATH)
        return node if isinstance(node, dict) else None

    def _judge_health(self, node: dict | None) -> tuple[ParseHealth, str]:
        """三層判定，與 yahoo_closed.py 同構。

        ⚠️ 交叉比對用的是 **`items` 的原始長度**，不是過濾後的筆數：
        「這一頁 100 筆全是已售出」是市場現象，不是解析壞掉。
        """
        # 第 3 層先判：連 __NEXT_DATA__ 或結果節點都沒有 = 頁面長相全變了
        if node is None:
            return (
                ParseHealth.PARSER_BROKEN,
                "找不到 __NEXT_DATA__ 或 searchState.search.result 節點：頁面結構已改版",
            )

        total = node.get("totalResultsAvailable")
        raw = node.get("items")
        raw_n = len(raw) if isinstance(raw, list) else 0

        # 第 1 層（最強）：命中數交叉比對，同一份 JSON、同源同基準
        if isinstance(total, int) and total > 0 and raw_n == 0:
            return ParseHealth.PARSER_BROKEN, f"頁面標示 {total} 件但解析出 0 筆"

        # 第 2 層：頁面自己說沒有結果（實測查無結果頁 total=0 且 isZeroMatch=true）
        if total == 0:
            return ParseHealth.EMPTY_CONFIRMED, "totalResultsAvailable 0 件：確認查無結果"

        if raw_n == 0:
            return ParseHealth.PARSER_BROKEN, "items 為空且無命中數可比對：頁面結構已改版"

        return ParseHealth.OK, ""

    # ------------------------------------------------------------------
    def _extract_listings(
        self, items: list, seen: set[str], *, sold: bool
    ) -> tuple[list[Listing], dict[str, int]]:
        """逐筆轉成 Listing。回傳 (listings, 計數)。

        `sold=False` 只收 `itemStatus == "OPEN"`（其餘買不到，混進在架清單等於
        推薦一個已經賣掉的東西）；`sold=True` 只收非 OPEN 且抓得到成交時間的。
        """
        out: list[Listing] = []
        stats = {"parsed": 0, "status": 0, "foreign": 0, "malformed": 0}
        for it in items:
            if not isinstance(it, dict):
                stats["malformed"] += 1
                continue
            item_id = it.get("id")
            title = it.get("title")
            price = it.get("price")
            status = it.get("itemStatus")
            if (
                not item_id
                or not title
                or not isinstance(price, (int, float))
                or price <= 0
                or not status
            ):
                stats["malformed"] += 1
                continue
            stats["parsed"] += 1

            if not _FLEA_ID_RE.match(str(item_id)):
                # ヤフオク!的貨混進來了：ID 空間與購買路徑都不是 PayPay 的
                stats["foreign"] += 1
                continue
            if str(item_id) in seen:
                continue

            is_open = status == "OPEN"
            if sold == is_open:
                stats["status"] += 1
                continue

            sold_at = to_utc_iso(it.get("endTime")) if sold else None
            if sold and not sold_at:
                # 已售出模式的整個價值就是那個時間戳。抓不到就不要收——
                # 收了就得蓋上入庫時間，那正是這次要修掉的病。
                stats["status"] += 1
                continue

            seen.add(str(item_id))
            out.append(
                Listing(
                    site=self.site,
                    external_id=str(item_id),
                    title=str(title),
                    url=_BUYEE_ITEM_URL.format(id=item_id),
                    price=float(price),
                    currency=Currency.JPY,
                    image_url=it.get("thumbnailImageUrl"),
                    seller_id=it.get("sellerId"),
                    ships_to_tw=True,  # 走 Buyee 一定寄得到台灣
                    best_offer_enabled=False,  # 代購買不到「値下げ交渉」
                    listed_at=_parse_dt(it.get("openTime")),
                    is_sold=sold,
                    raw={
                        "price_kind": "sold_price" if sold else "fixed",
                        "item_status": status,
                        # 賣家評價（Seller Alpha 的額外欄位）：`{"id": "p245246",
                        # "goodRatio": 99.7, "numRating": 307}`（2026-08-03 實測
                        # 100/100 筆都有）。放 raw，聚合端（sellers 表）自己取用。
                        "seller": it.get("seller"),
                        "end_time": it.get("endTime"),
                        # ingest_sold 會優先採用這個（UTC 正規化過）。
                        # 只有已售出模式才有——在架商品的 endTime 是上架期限，
                        # 拿它當成交時間就是憑空造一個未來的成交紀錄。
                        **({"sold_at": sold_at} if sold_at else {}),
                    },
                    source=self.name,
                    origin_url=_ORIGIN_ITEM_URL.format(id=item_id),
                )
            )
        return out, stats


def _parse_dt(raw: str | None) -> datetime | None:
    """`2026-07-13T15:02:20+09:00` → UTC datetime。抽不到一律 None，絕不用 now() 頂替。"""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        return None
