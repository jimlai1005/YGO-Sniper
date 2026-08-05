"""Yahoo! Auctions 落札相場（closedsearch）——**只餵 comps，不參與在架掃描**。

在架價是「有人開的價」，成交價是「有人付的錢」。行情表要的是後者。
這條管道抓的是 `closedsearch`，也就是 Yahoo 自己的「落札相場」頁，
所以它是本專案唯一一個真正的日本市場成交價來源（Buyee 系的 sold_out
搜尋要 Playwright 解 WAF，這條純 httpx 就能走）。

**最重要的一件事：怎麼確定這是成交價，不是流標價。**

流標（無人出價）的商品，價格欄顯示的是**開始価格**——賣家的期望，不是成交價。
混進行情表會系統性拉低中位數，而且每一筆看起來都很正常（PLAN 風險 1 的同型）。
三道證據疊起來（2026-08-01 實測，見 tests/fixtures/RECON.md §6）：

1. Yahoo 自己的頁面標題就寫「〈關鍵字〉**の落札された商品** 180 日間の落札相場」，
   每張卡片的價格標籤是「落札 20,000 円」（對照「開始 1 円」另外列）。
   closedsearch 的產品語意就是「只列落札成功的」。
2. 實測 200 筆（4 個查詢 × 50 筆）**沒有任何一筆 `bidCount == 0`**。
3. 逐筆對帳：`l1238412091` 在 closedsearch 是 `price=20000, bidCount=23,
   initPriceNoTax=1`，去它的商品頁看 `__NEXT_DATA__` 得到 `price=20000,
   bids=23, initPrice=1`——成交價 20,000 與開始價 1 是兩個不同欄位，
   `price` 明確是最終落札価格。

即使如此，本檔仍然**硬性要求 `bidCount >= 1`** 才產出（`_extract_listings`）。
理由是工程原則的第 5 條：把判準寫在資料流的必經之處，比寫在註解裡可靠。
Yahoo 哪天改成把流標也列進來，我們是安靜地少收幾筆，不是安靜地污染行情表。
被擋掉的筆數會進 `SearchResult.detail`，數字異常時看得見。

**解析走 `__NEXT_DATA__` 而不是 CSS selector。**
closedsearch 是 Next.js + styled-components，class 名是每次 build 都會變的
雜湊（`sc-c91b7830-5 opRIC`），拿它當地標等於保證下次部署就壞。頁面同時內嵌
一份 189KB 的 `<script id="__NEXT_DATA__" type="application/json">`，裡面是
伺服器端渲染用的原始資料（auctionId / title / price / bidCount / endTime…）。
這份 JSON 是頁面自己的資料來源，比它渲染出來的 DOM 穩定得多，而且欄位有語意。

**成交時間用 `endTime`，不是 now()。**
closedsearch 的視窗是 180 天，comps 的視窗是 90 天。冷門查詢（例：`遊戯王 二期
PSA` 只有 16 筆）翻出來的是好幾個月前的成交——把它蓋上「今天成交」的時間戳，
就等於拿半年前的價格當現在的行情（同源同基準，工程原則 1）。所以 raw 裡帶
`sold_at`（已換算成 UTC，與 comps 既有的 `datetime.now(UTC).isoformat()`
同一個基準，字串比較才有意義），由 `CompsEngine.ingest_sold` 採用。

查無結果與搜尋頁同樣是 **HTTP 404 ＋完整頁面**（`totalResultsAvailable: 0`），
所以 fetch 一律帶 `allow_statuses=(404,)`。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ..config import Config
from ..domain import Currency, Listing, Site
from .base import BlockedError, CachedFetcher, FetchError
from .health import ParseHealth, SearchResult

_SEARCH_BASE = "https://auctions.yahoo.co.jp/closedsearch/closedsearch"
#: 一頁幾筆。**實測 100 有效**（2026-08-04，`遊戯王 初期 PSA`，pool 855）：
#: `n=100` 回 100 筆、`b=101` 是乾淨的第二頁（與第一頁交集 0 筆），每頁涵蓋
#: 約 25 天的成交（n=50 時是約 9 天／頁）。從 50 改成 100 是**純粹的請求數折半**
#: ——同樣的歷史深度只要一半的請求，對 180 天 archive 的回填差別是幾十個請求。
#: ⚠️ 這個常數同時是 `b` 的步長與「不滿一頁＝最後一頁」的判準，兩者必須同源
#: （改一個忘了另一個的症狀是靜默漏頁或無限翻頁）。
_PAGE_SIZE = 100

#: 購買端 URL（`Listing.url` 的約定：一律放買得到的那一端）。
#: 成交紀錄當然買不到了，但保持與 yahoo_direct 同構有兩個實際好處：
#: comps 的 `url` 欄位是 `UNIQUE(signature, url)` 的一半（去重鍵必須穩定），
#: 而人要回頭查證某筆行情時，Buyee 端與原生端兩個連結都在手上。
_BUYEE_AUCTION_URL = "https://buyee.jp/item/yahoo/auction/{id}"
_BUYEE_PAYPAY_URL = "https://buyee.jp/paypayfleamarket/item/{id}"
#: 發現端原生 URL（`Listing.origin_url`）
_YAHOO_AUCTION_URL = "https://auctions.yahoo.co.jp/jp/auction/{id}"
_PAYPAY_ITEM_URL = "https://paypayfleamarket.yahoo.co.jp/item/{id}"

#: `__NEXT_DATA__` 裡到商品清單的路徑。寫成常數是為了讓「頁面改版」在
#: `_listing_node()` 一處失敗、一處告警，而不是散在四個 `.get()` 裡各自回 None。
_LISTING_PATH = ("props", "pageProps", "initialState", "search", "items", "listing")


def _dig(obj: Any, path: tuple[str, ...]) -> Any | None:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def to_utc_iso(end_time: str | None) -> str | None:
    """`2026-08-01T22:17:22+09:00` → `2026-08-01T13:17:22+00:00`。

    **必須換算成 UTC**：comps 既有的列是 `datetime.now(UTC).isoformat()`
    寫進去的，而 `store.load_comps()` 用字串比大小篩視窗。同一欄位混著
    `+09:00` 與 `+00:00` 兩種偏移，字串比較就會給出錯誤的答案
    （`2026-08-01T22:...+09:00` 字面上大於 `2026-08-01T13:...+00:00`，
    但它們是同一個瞬間）。比較的兩個值必須同源同單位——工程原則 1。
    """
    if not end_time or not isinstance(end_time, str):
        return None
    try:
        dt = datetime.fromisoformat(end_time)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def sale_flags_from_cache(cache_dir: Path) -> dict[str, bool]:
    """從快取的 closedsearch 快照挖出 `auctionId → isFixedPrice`（回填證據）。

    為什麼證據挖得回來：`CachedFetcher` 把每次抓到的原始頁面存成
    `data/cache/<hash>.html`，而 closedsearch 的 `__NEXT_DATA__` 裡每筆商品
    都帶著 `isFixedPrice`（False＝競標落札、True＝一口價即決）。入庫時這個
    欄位被丟掉了，但**原始頁還在**——2026-08-06 實測 495 個快照回接出
    8,489 個 ID，正式庫 957 筆 buyee_yahoo 成交裡 954 筆有證據。

    解析走 `YahooClosedSource._listing_node`（＝生產路徑），不另寫一套 regex：
    另寫一套會跟生產解析器漂移，而漂移之後回填出來的型態是錯的、看起來卻很
    正常（本專案第六節：測試路徑必須等於生產路徑）。

    不是 closedsearch 的快取檔（搜尋頁、商品頁、其他站台）自然解不出節點，
    直接跳過——這裡不做健康判定，因為「這個檔案不是 closedsearch」是常態。
    """
    flags: dict[str, bool] = {}
    for path in sorted(Path(cache_dir).glob("*.html")):
        try:
            html = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # 先用便宜的字串檢查擋掉九成檔案：BeautifulSoup 解 340KB × 1200 個檔案
        # 要好幾分鐘，而其中絕大多數根本不是 closedsearch。
        if "__NEXT_DATA__" not in html or "isFixedPrice" not in html:
            continue
        node = YahooClosedSource._listing_node(html)
        if not node:
            continue
        for it in node.get("items") or []:
            if not isinstance(it, dict):
                continue
            aid = it.get("auctionId")
            fixed = it.get("isFixedPrice")
            if aid and isinstance(fixed, bool):
                flags[str(aid)] = fixed
    return flags


class YahooClosedSource:
    """Yahoo 落札相場 source。`supports_sold=True`，只被 `refresh_comps` 用。

    `site` 是 class 層級的 `BUYEE_YAHOO`（購買路徑，SearchResult 歸因用），
    但**逐筆 Listing 的 site 依 `isFleamarketItem` 分流**：closedsearch 的
    結果混著 Yahoo 拍賣與 Yahoo!フリマ（PayPay）兩個 ID 空間（實測 50 筆
    正好各半），全部掛同一個 site 會讓 comps 的 `site` 欄位說謊，而且
    `Listing.key` 會把兩個空間的 id 混在一起。
    """

    name = "yahoo_closed"
    site = Site.BUYEE_YAHOO
    supports_sold = True

    def __init__(self, cfg: Config, fetcher: CachedFetcher | None = None) -> None:
        self.cfg = cfg
        self.fetcher = fetcher or CachedFetcher(cfg)

    # ------------------------------------------------------------------
    def build_url(self, keyword: str, *, page: int = 1, max_price: float | None = None) -> str:
        """組落札相場網址。`b` 與搜尋頁同樣是 1-based 商品 offset。

        **不帶排序參數**：實測 metadata 回 `"sort": "-END_TIME"`，預設就是
        「終了日時が近い順」＝最近成交的排最前面，正是 comps 要的。
        （RECON §1 已證實 Yahoo 對未知排序鍵是靜默忽略，亂加只會給人
        「我有排序」的錯覺。）
        """
        params: dict[str, str | int] = {
            "p": keyword,
            "va": keyword,
            "b": (page - 1) * _PAGE_SIZE + 1,
            "n": _PAGE_SIZE,
        }
        if max_price:
            # 介面相容才留著。**comps 不該用它**：對成交價設價格上限＝
            # 只看便宜的那一半，行情中位數會被系統性壓低。
            params["aucmaxprice"] = int(max_price)
        return f"{_SEARCH_BASE}?{urlencode(params)}"

    # ------------------------------------------------------------------
    def search(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        sold: bool = True,
        pages: int | None = None,
    ) -> list[Listing]:
        """相容包裝（`refresh_comps` 走這條）。

        抓取層失敗**往上拋**，與 BuyeeSource.search 同一約定：
        `refresh_comps` 靠例外印警告，靜默回空清單的話，被擋三週你只會
        看到三週的「comps 沒有新資料」。`sold` 參數只為介面相容——
        closedsearch 本來就只有成交紀錄。
        """
        result = self.search_detailed(keyword, max_price=max_price, pages=pages)
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
        sold: bool = True,
        pages: int | None = None,
        first_page: int = 1,
    ) -> SearchResult:
        """`first_page` 讓歷史回填**接著上次的頁碼繼續翻**（`history.py` 用）。

        沒有它，一個已經翻到第 4 頁的查詢想再深挖 5 頁，就得從第 1 頁重抓
        ——那 4 個請求換回來的是完全一樣的資料。預設 1，一般掃描完全不受影響。
        """
        # `sold` 只為介面相容（refill 統一用 search_detailed(sold=True, …) 呼叫
        # 所有 supports_sold 來源）——closedsearch 本來就只有成交紀錄。
        del sold
        pages = pages or int(self.cfg.fetch["max_pages_per_query"])
        first_page = max(1, int(first_page))
        first_url = self.build_url(keyword, page=first_page, max_price=max_price)
        result = SearchResult(
            source=self.name, site=self.site.value, query=keyword, url=first_url
        )

        seen: set[str] = set()
        unsold = 0
        malformed = 0

        for offset, page in enumerate(range(first_page, first_page + pages), start=1):
            url = self.build_url(keyword, page=page, max_price=max_price)
            try:
                html = self.fetcher.get(url, allow_statuses=(404,))
            except BlockedError as exc:
                if offset == 1:
                    result.health = ParseHealth.BLOCKED
                    result.detail = f"被擋：{exc}"
                    return result
                print(f"[warn] {self.name} p{page} 被擋，只回傳前 {offset - 1} 頁: {exc}")
                break
            except FetchError as exc:
                # 第一頁抓不到 = 對這個關鍵字「什麼都不知道」，跟「沒成交」是兩件事
                if offset == 1:
                    result.health = ParseHealth.FETCH_FAILED
                    result.detail = f"抓取失敗：{exc}"
                    return result
                print(f"[warn] {self.name} p{page} 抓取失敗，只回傳前 {offset - 1} 頁: {exc}")
                break

            result.pages_fetched = offset
            node = self._listing_node(html)

            if offset == 1:
                result.html_bytes = len(html)
                health, detail = self._judge_health(node)
                if health is not ParseHealth.OK:
                    result.health = health
                    result.detail = detail
                    return result
            elif node is None:
                print(f"[warn] {self.name} p{page} 找不到 __NEXT_DATA__，只回傳前 {offset - 1} 頁")
                break

            raw_items = (node or {}).get("items") or []
            batch, n_unsold, n_bad = self._extract_listings(raw_items)
            unsold += n_unsold
            malformed += n_bad
            # parsed_count 的語意全來源一致：**商業篩選之前**解析器認得的商品數。
            # 「無得標者」是商業篩選（流標的價格不是成交價），欄位不全才是解析失敗，
            # 所以前者計入、後者不計——與 _judge_health 用原始 items 長度同一個立場。
            result.parsed_count += len(batch) + n_unsold
            for lst in batch:
                if lst.external_id not in seen:
                    seen.add(lst.external_id)
                    result.listings.append(lst)
            if len(raw_items) < _PAGE_SIZE:
                # 已是最後一頁＝這個關鍵字的存量翻完了（**只有走到這裡才敢說**，
                # 錯誤中斷的 break 一律不設，見 SearchResult.archive_exhausted）
                result.archive_exhausted = True
                break

        notes = []
        if unsold:
            notes.append(f"排除無得標者 {unsold} 筆")
        if malformed:
            notes.append(f"排除欄位不全 {malformed} 筆")
        result.detail = "、".join(notes)
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _listing_node(html: str) -> dict | None:
        """挖出 `__NEXT_DATA__` 裡的商品清單節點。任何一步失敗都回 None
        （由 `_judge_health` 統一判成 PARSER_BROKEN 並告警）。"""
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag is None:
            return None
        try:
            payload = json.loads(tag.get_text())
        except (ValueError, TypeError):
            return None
        node = _dig(payload, _LISTING_PATH)
        return node if isinstance(node, dict) else None

    def _judge_health(self, node: dict | None) -> tuple[ParseHealth, str]:
        """三層判定，與 yahoo.py 同構。

        ⚠️ 交叉比對用的是 **`items` 的原始長度**，不是過濾後的筆數：
        「頁面有 50 筆但全是流標」是市場現象，不是解析壞掉。拿過濾後的
        數字去對命中數，會在 Yahoo 哪天真的列出流標時發出假告警。
        """
        # 第 3 層先判：連 __NEXT_DATA__ 或商品節點都沒有 = 頁面長相全變了
        if node is None:
            return (
                ParseHealth.PARSER_BROKEN,
                "找不到 __NEXT_DATA__ 或 search.items.listing 節點：頁面結構已改版",
            )

        total = node.get("totalResultsAvailable")
        raw = node.get("items")
        raw_n = len(raw) if isinstance(raw, list) else 0

        # 第 1 層（最強）：命中數交叉比對，同一次抓取的同一份 JSON，同源同基準
        if isinstance(total, int) and total > 0 and raw_n == 0:
            return ParseHealth.PARSER_BROKEN, f"頁面標示 {total} 件但解析出 0 筆"

        # 第 2 層：頁面自己說沒有落札紀錄
        if total == 0:
            return ParseHealth.EMPTY_CONFIRMED, "落札相場 0 件：確認查無成交紀錄"

        if raw_n == 0:
            return ParseHealth.PARSER_BROKEN, "items 為空且無命中數可比對：頁面結構已改版"

        return ParseHealth.OK, ""

    # ------------------------------------------------------------------
    def _extract_listings(self, items: list) -> tuple[list[Listing], int, int]:
        """逐筆轉成 Listing。回傳 (listings, 無得標者筆數, 欄位不全筆數)。"""
        out: list[Listing] = []
        unsold = 0
        malformed = 0
        for it in items:
            if not isinstance(it, dict):
                malformed += 1
                continue
            auction_id = it.get("auctionId")
            title = it.get("title")
            price = it.get("price")
            bids = it.get("bidCount")
            if not auction_id or not title or not isinstance(price, (int, float)) or price <= 0:
                malformed += 1
                continue

            # ★ 本檔的守門條件：沒有得標者 = 顯示的是開始価格，不是成交價。
            #   `bidCount` 缺欄位也一律擋——「不知道有沒有人買」不能當成「有人買」。
            if not isinstance(bids, int) or bids < 1:
                unsold += 1
                continue

            flea = bool(it.get("isFleamarketItem"))
            site = Site.BUYEE_PAYPAY if flea else Site.BUYEE_YAHOO
            buy_url = (_BUYEE_PAYPAY_URL if flea else _BUYEE_AUCTION_URL).format(id=auction_id)
            origin = (_PAYPAY_ITEM_URL if flea else _YAHOO_AUCTION_URL).format(id=auction_id)

            # 賣家（Seller Alpha）：AUCTION 標的是 28-29 字混淆 ID（與搜尋頁的
            # data-auction-auc-seller-id 同空間、跨日穩定）；フリマ標的是 `p\d+`
            # （與 paypay_direct 的 sellerId 同空間）。成交紀錄帶賣家，
            # 折價歷史才有主人。抽不到一律 None。
            seller = it.get("seller") if isinstance(it.get("seller"), dict) else {}
            seller_id = seller.get("userId")

            out.append(
                Listing(
                    site=site,
                    external_id=str(auction_id),
                    title=str(title),
                    url=buy_url,
                    price=float(price),
                    currency=Currency.JPY,
                    image_url=it.get("imageUrl"),
                    seller_id=str(seller_id) if seller_id else None,
                    ships_to_tw=True,
                    best_offer_enabled=False,
                    is_sold=True,
                    raw={
                        # 額外欄位放 raw（"98.7%" 字串／布林），評分模組之後用
                        "seller_rating": seller.get("goodRating"),
                        "seller_is_store": seller.get("isStore"),
                        "price_kind": "sold_price",   # 落札価格；與 yahoo.py 的 buyout/current_bid 三選一
                        "bid_count": bids,
                        "end_time": it.get("endTime"),
                        # ingest_sold 會優先採用這個（UTC 正規化過）。
                        # 沒有它就會被蓋上「今天成交」，冷門查詢翻出的半年前成交
                        # 就會混進 90 天視窗裡當現在的行情。
                        "sold_at": to_utc_iso(it.get("endTime")),
                        "is_fixed_price": bool(it.get("isFixedPrice")),
                        "start_price": it.get("initPriceNoTax"),
                    },
                    source=self.name,
                    origin_url=origin,
                )
            )
        return out, unsold, malformed
