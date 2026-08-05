"""Yahoo! Auctions 直抓（不經 Buyee 搜尋頁，不需要瀏覽器）。

這條是三個發現管道裡唯一純 httpx 就能走的，所以是主力來源。
但它有兩個很容易做錯、而且錯了**看起來像成功**的陷阱：

**陷阱 1：價格語意（本檔最重要的決定）。**
Yahoo 拍賣一筆商品有兩種價格——「現在価格」是目前的出價，**不是你付得出去
的價格**；「即決価格」才是點下去就能成交的價格。Phase 0 抽五筆與 Buyee
商品頁逐円比對證實了這個語意（見 tests/fixtures/RECON.md §2）。
如果把現在価格當成可成交價，成本模型會系統性偏低，產生大量假 FREE_CARD
——而且每一筆看起來都像撿到寶，沒有任何錯誤訊息。所以：
- 有即決価格 → 產出 Listing，`price=即決価格`，raw 標 `price_kind="buyout"`。
- 純競標 → 產出 Listing，`price=現在価格`，raw 標 `price_kind="current_bid"`
  （config `sources.yahoo_direct.include_live_auctions`，2026-08-02 起預設開）。
  **這個標記是下游分流的唯一依據**：`bidding.is_live_auction()` 讀它，
  scoring 據以改用「出價上限 vs 目前出價」而不是「到手成本 < 鑑定費」。
  分流壞掉的症狀是大量假 FREE_CARD——每一筆都看起來像撿到寶。
  純競標另外帶 `end_time`（結標時間）與 `bids`（出價數）：競標是時間敏感的，
  沒有結標時間的競標標的在清單上毫無用處。

**陷阱 2：查無結果回 HTTP 404。**
Yahoo 查無結果時回 404＋完整頁面（含「条件に一致する商品は見つかりませんでした。」）。
不處理的話 CachedFetcher 會把「真的沒貨」當成「抓取失敗」拋錯——
所以 fetch 一律帶 `allow_statuses=(404,)`，有沒有結果由 parser 看頁面判定。

selector 與 URL 格式全部出自 Phase 0 實測（tests/fixtures/RECON.md），
不要憑印象改。壞掉時的三層健康判定見 `_judge_health()`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from ..bidding import LIVE_AUCTION_KIND
from ..config import Config
from ..domain import Currency, Listing, Site
from .base import BlockedError, CachedFetcher, FetchError
from .health import ParseHealth, SearchResult

_SEARCH_BASE = "https://auctions.yahoo.co.jp/search/search"
#: 賣家頁（Seller Alpha 的輪替監控）。2026-08-04 實測：純 httpx 200、85-282KB，
#: `__NEXT_DATA__` 的商品節點與 **closedsearch 同一條路徑**（`_SELLER_LISTING_PATH`
#: 與 `yahoo_closed._LISTING_PATH` 逐字相同），欄位也是同一組
#: （auctionId／title／price／buyNowPrice／bidCount／endTime／isFleamarketItem）
#: ——與**搜尋頁**（CSS selector、`li.Product`）才是不同構的那一對。
_SELLER_BASE = "https://auctions.yahoo.co.jp/seller/"
_BUYEE_ITEM_URL = "https://buyee.jp/item/yahoo/auction/{id}"
_YAHOO_AUCTION_URL = "https://auctions.yahoo.co.jp/jp/auction/{id}"
_PAGE_SIZE = 50

#: 賣家頁 `__NEXT_DATA__` 裡到商品清單／賣家檔案的路徑。寫成常數，
#: 讓「頁面改版」在 `_seller_nodes()` 一處失敗、一處告警。
_SELLER_LISTING_PATH = ("props", "pageProps", "initialState", "search", "items", "listing")
_SELLER_PROFILE_PATH = ("props", "pageProps", "initialState", "user", "seller")


def _dig(obj: Any, path: tuple[str, ...]) -> Any | None:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _as_price(value: Any) -> float | None:
    """`price`／`buyNowPrice` → float。**0 與負數一律當成沒有這個價格**：
    賣家頁的 `buyNowPrice` 沒設即決時是 `null`，但別把 0 當成「免費」。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) if value > 0 else None


def _parse_iso(raw: Any) -> datetime | None:
    """`2026-08-09T21:06:05+09:00` → UTC datetime。抽不到一律 None，絕不猜。

    競標的結標時間是時間敏感資訊，猜一個會讓整個「快結標」排序說謊。
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)

#: RECON：id 有 `[a-z]\d{9,10}` 與純數字（如 1239234527）兩種形狀，
#: 既有 buyee.py 的 `([a-zA-Z]\d+)` 會漏掉純數字——這裡必須兩種都收。
_AUCTION_ID_RE = re.compile(r"/auction/([A-Za-z0-9]+)")
_PRICE_TEXT_RE = re.compile(r"([\d,]+)\s*円")
_HITS_RE = re.compile(r"([\d,]+)\s*件")

#: 無結果字串（RECON 實測原文，一字不差）
_EMPTY_TEXT = "条件に一致する商品は見つかりませんでした"

#: 排序模式 → URL 參數。**這張表是實測結果，config 只能選模式、不能改參數。**
#: `s1` 是排序鍵、`o1` 是方向（d=降冪／a=升冪）。
#: - `newest`（s1=new&o1=d）：新着降冪。2026-08-01 實測前 5 筆「開始日時」全部落在
#:   當天 14:03～16:48 且嚴格遞減；不加參數那組是 7/26～8/1 混排，重疊 0/5。
#: - `ending_soon`（s1=end&o1=a）：結束時間近→遠。2026-08-02 實測（「遊戯王 PSA 初期」、
#:   不帶價格上限）：回傳順序的結標時間**嚴格遞增**（12.1h → 64.1h），中位 45h、
#:   ≤24h 有 20/50 筆；同一時刻的新着那趟是中位 115h、≤24h 只有 2/50 筆。
#: ⚠️ `s1=start` 實測**無效**（結果與不加參數完全相同，Yahoo 靜默忽略未知排序鍵）
#: ——所以這些值不可以憑印象改，改了要重測「前幾筆是否真的變了」。
SORT_PARAMS: dict[str, dict[str, str]] = {
    "default": {},
    "newest": {"s1": "new", "o1": "d"},
    "ending_soon": {"s1": "end", "o1": "a"},
}
SORT_DEFAULT = "default"
SORT_NEWEST = "newest"
SORT_ENDING_SOON = "ending_soon"


@dataclass(slots=True, frozen=True)
class ScanPass:
    """一趟抓取：用哪個排序模式、翻幾頁。

    存在的理由：pool 大於一頁時，新着與即將結標抓到的是**互斥的兩群標的**
    （2026-08-02 實測不帶價格上限：兩趟各 50 筆、去重後 97 筆，只重疊 3 筆）。
    競標價在結標前幾分鐘才會跳，所以「還有五天、現價 ¥1」不是機會——
    單靠新着會把快結標那一端整段截掉，而**截斷是無聲的**。

    ⚠️ 誠實標註：同日實跑掃描（帶 aucmaxprice）時，四條 query 的 pool 都 ≤50，
    一頁就裝得下整個 pool，兩趟拿到同一批、ending_soon 新增 0 筆。
    這條通道現在是**保險**，不是每輪都有產出——理由與代價寫在 settings.yaml。
    """

    mode: str
    pages: int


def _pages_setting(block: dict, mode: str, source_name: str, default: int) -> int:
    """一趟的頁數。非法值（非整數、< 1）印警告並退回 `Config.max_pages_for` 的值。"""
    raw = block.get("pages")
    if raw is None:
        return default
    try:
        pages = int(raw)
    except (TypeError, ValueError):
        print(
            f"[warn] sources.{source_name}.scan_passes.{mode}.pages={raw!r} "
            f"不是整數，改用 {default}"
        )
        return default
    if pages < 1:
        print(
            f"[warn] sources.{source_name}.scan_passes.{mode}.pages={pages} < 1，"
            f"改用 {default}"
        )
        return default
    return pages


class YahooAuctionSource:
    """Yahoo 直抓 source。發現管道是 Yahoo，購買路徑仍是 Buyee——

    拍賣 ID 與 buyee.jp/item/yahoo/auction/{id} 同一個 ID 空間，
    所以 Listing 掛 `site=Site.BUYEE_YAHOO`（成本模型零修改、去重 key 穩定），
    歸因看 `source="yahoo_direct"`。
    """

    name = "yahoo_direct"
    site = Site.BUYEE_YAHOO
    supports_sold = False
    #: 一頁幾筆。pipeline 用它判斷「第一趟有沒有裝滿」——裝滿代表 pool 可能
    #: 被截斷（視野外可能還有快結標的標的），才需要補跑 ending_soon 那一趟。
    page_size = _PAGE_SIZE
    #: 吃得下 `category`（→ URL 的 `auccat`）。2026-08-03 實測：
    #: 「遊戯王 PSA 初期」的分類 facet 一路是
    #: 25464 おもちゃ、ゲーム → 27727 ゲーム → 25826 トレーディングカードゲーム
    #: → **2084005059 遊戯王（コナミ）**（131/132 件落在這一格）。
    supports_category = True

    def __init__(self, cfg: Config, fetcher: CachedFetcher | None = None) -> None:
        self.cfg = cfg
        self.fetcher = fetcher or CachedFetcher(cfg)
        src_cfg = cfg.sources.get(self.name, {})
        self._src_cfg = src_cfg
        self.include_live_auctions = bool(src_cfg.get("include_live_auctions", False))
        # 新上架優先。每小時掃描的整個價值就在於搶先看到剛上架的定價錯誤，
        # 預設的綜合排序會讓同一批舊貨每小時重現一次。
        self.sort_newest = bool(src_cfg.get("sort_newest", True))
        #: 沒有指定 sort 時用哪個模式（舊鍵 `sort_newest` 的相容出口）。
        self.sort_mode = SORT_NEWEST if self.sort_newest else SORT_DEFAULT

    # ------------------------------------------------------------------
    def scan_passes(self) -> list[ScanPass]:
        """在架掃描要跑哪幾趟（pipeline 讀這個）。config：

            sources.yahoo_direct.scan_passes:
              newest:      {enabled: true, pages: 1}
              ending_soon: {enabled: true, pages: 1}

        沒設 `scan_passes` → 退回單趟（`sort_newest` 決定模式、頁數用
        `Config.max_pages_for`），也就是這個功能上線前的行為。

        **設定壞掉時寧可多抓一趟，也不要安靜地變成 0 趟**：未知模式、頁數非法、
        甚至兩個通道都被關掉，都會印警告並退回單趟預設，而不是回空清單。
        回空清單的話 dashboard 會顯示「Yahoo 0 筆」——與「今天沒貨」外顯一模一樣，
        而只有前者需要你去改設定。要真的停掉這條管道請從 watchlist 移除它。
        """
        default_pages = self.cfg.max_pages_for(self.name)
        fallback = [ScanPass(self.sort_mode, default_pages)]

        spec = self._src_cfg.get("scan_passes")
        if not spec:
            return fallback
        if not isinstance(spec, dict):
            print(f"[warn] sources.{self.name}.scan_passes 不是對映表，改用單趟預設")
            return fallback

        out: list[ScanPass] = []
        for mode, raw_block in spec.items():
            block = raw_block if isinstance(raw_block, dict) else {}
            if not block.get("enabled", True):
                continue
            if mode not in SORT_PARAMS:
                print(
                    f"[warn] sources.{self.name}.scan_passes.{mode} 是未知排序模式"
                    f"（可用：{', '.join(SORT_PARAMS)}），跳過這一趟"
                )
                continue
            out.append(ScanPass(mode, _pages_setting(block, mode, self.name, default_pages)))
        if not out:
            print(
                f"[warn] sources.{self.name}.scan_passes 沒有任何啟用的通道，"
                f"改用單趟預設（{self.sort_mode}）——要停掉這條管道請從 watchlist 移除"
            )
            return fallback
        return out

    # ------------------------------------------------------------------
    def _sort_params(self, sort: str | None) -> dict[str, str]:
        """排序模式 → URL 參數。未知模式**印警告**並退回這個 source 的預設模式。

        不靜默的理由：Yahoo 對未知排序鍵是靜默忽略的（`s1=start` 就是這樣死的），
        所以打錯一個模式名的症狀會是「抓回來的還是新着，但你以為抓的是即將結標」
        ——沒有任何錯誤訊息，而整個功能的價值就在那個差別上。
        """
        if sort is None:
            sort = self.sort_mode
        if sort not in SORT_PARAMS:
            print(
                f"[warn] {self.name} 未知排序模式 {sort!r}"
                f"（可用：{', '.join(SORT_PARAMS)}），改用 {self.sort_mode}"
            )
            sort = self.sort_mode
        return SORT_PARAMS[sort]

    def build_url(
        self,
        keyword: str,
        *,
        page: int = 1,
        max_price: float | None = None,
        sort: str | None = None,
        category: str | None = None,
    ) -> str:
        """組搜尋網址。`b` 是 1-based 商品 offset（不是頁碼）：第 2 頁 = b=51。

        `category` → `auccat`（Yahoo 自己的分類編號，與 Buyee／PayPay 的
        編號系統完全無關，見 `queries.py`）。空關鍵字 ＋ auccat 是合法組合：
        那就是「整個遊戲王分類的新上架」。
        """
        params: dict[str, str | int] = {
            "p": keyword,
            "va": keyword,
            "b": (page - 1) * _PAGE_SIZE + 1,
            "n": _PAGE_SIZE,
        }
        if category:
            params["auccat"] = category
        if max_price:
            # 平台側過濾（閉區間、對現在価格生效），省掉抓回來再丟的流量
            params["aucmaxprice"] = int(max_price)
        params.update(self._sort_params(sort))
        return f"{_SEARCH_BASE}?{urlencode(params)}"

    # ------------------------------------------------------------------
    def search(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        sold: bool = False,
        pages: int | None = None,
        sort: str | None = None,
        category: str | None = None,
    ) -> list[Listing]:
        """相容包裝：只要清單的人用這個。sold 參數僅為介面相容（supports_sold=False）。"""
        return self.search_detailed(
            keyword, max_price=max_price, pages=pages, sort=sort, category=category
        ).listings

    def search_detailed(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        pages: int | None = None,
        sort: str | None = None,
        category: str | None = None,
    ) -> SearchResult:
        pages = pages or int(self.cfg.fetch["max_pages_per_query"])
        first_url = self.build_url(
            keyword, page=1, max_price=max_price, sort=sort, category=category
        )
        result = SearchResult(
            source=self.name, site=self.site.value, query=keyword, url=first_url
        )

        # RECON：跨頁有推廣位重複，必須以 auction id 去重。
        # 這個集合含**被排除的純競標**，所以 parsed_count 不會把跨頁重複的
        # 同一個商品數兩次（parsed_count 是健康指標，重複計數就失去意義）。
        seen: set[str] = set()
        excluded_bids = 0

        for page in range(1, pages + 1):
            url = self.build_url(
                keyword, page=page, max_price=max_price, sort=sort, category=category
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
            soup = BeautifulSoup(html, "html.parser")
            products = soup.select("li.Product")

            if page == 1:
                result.html_bytes = len(html)
                health, detail = self._judge_health(soup, len(products))
                if health is not ParseHealth.OK:
                    result.health = health
                    result.detail = detail
                    return result

            batch, excluded, parsed = self._extract_listings(products, seen)
            excluded_bids += excluded
            result.parsed_count += parsed
            result.listings.extend(batch)
            if len(products) < _PAGE_SIZE:
                break  # 已是最後一頁，不用再打下一頁

        result.detail = (
            f"解析 {result.parsed_count} 筆、排除純競標 {excluded_bids} 筆"
            if excluded_bids
            else ""
        )
        return result

    # ------------------------------------------------------------------
    def _judge_health(self, soup: BeautifulSoup, parsed: int) -> tuple[ParseHealth, str]:
        """三層判定（PLAN Q5）。只在第 1 頁執行——RECON 實測第 2 頁的

        Tab 命中數會被模糊比對擴大（144 vs 310），跨頁比對不可信；
        命中數與商品數必須出自**同一次抓取的同一頁**（同源同基準）。
        """
        # 第 1 層（最強）：命中數交叉比對。頁面自己說有 N 件，我們卻解析出 0 筆
        # → 不可能是市場問題，必定是 selector 過期。
        hits = self._parse_hits(soup)
        if hits is not None and hits > 0 and parsed == 0:
            return ParseHealth.PARSER_BROKEN, f"頁面標示 {hits} 件但解析出 0 筆"

        # 第 2 層：頁面明講沒有結果
        empty = soup.select_one("h2.Empty__title")
        if empty and _EMPTY_TEXT in empty.get_text(strip=True):
            return ParseHealth.EMPTY_CONFIRMED, "頁面顯示查無結果"

        # 第 3 層：地標檢查——無商品、無無結果字串、無結果容器 → 頁面長相全變了
        if parsed == 0:
            return ParseHealth.PARSER_BROKEN, "無 li.Product、無無結果字串：頁面結構已改版"

        return ParseHealth.OK, ""

    @staticmethod
    def _parse_hits(soup: BeautifulSoup) -> int | None:
        """總命中數：`li.Tab__item--current span.Tab__subText` → `142件`。

        側欄 `p.FilterItem__count` 是分類 facet 數，長得很像，絕不能抓。
        """
        node = soup.select_one("li.Tab__item--current span.Tab__subText")
        if node is None:
            return None
        m = _HITS_RE.search(node.get_text(strip=True))
        if not m:
            return None
        return int(m.group(1).replace(",", ""))

    # ------------------------------------------------------------------
    def _extract_listings(
        self, products: list[Tag], seen: set[str] | None = None
    ) -> tuple[list[Listing], int, int]:
        """逐筆解析。回傳 (listings, 被排除的純競標筆數, **解析成功的商品數**)。

        第三個回傳值是健康指標的分子，語意嚴格是「**商業篩選之前**、解析器
        真的認得的商品數」：拿到 id、標題、至少一個價格就算數，接下來
        `include_live_auctions` 丟不丟它是商業決定，與解析器活不活著無關。
        少了這個分離，「今天新上架的全是 ¥1 起標」會被誤報成「解析器壞了」
        （見 health.SearchResult.parsed_count 的事故註記）。

        `seen` 傳進來就跨頁共用（含被排除的），不傳則只在本頁去重
        （RECON：一頁 50 筆裡有 0〜3 個推廣位重複）。
        """
        out: list[Listing] = []
        seen = seen if seen is not None else set()
        excluded = 0
        parsed = 0
        for li in products:
            anchor = li.select_one("a.Product__titleLink")
            if anchor is None or not anchor.get("href"):
                continue
            m = _AUCTION_ID_RE.search(anchor["href"])
            if m is None:
                continue
            auction_id = m.group(1)
            if auction_id in seen:
                continue
            seen.add(auction_id)

            current, buyout = self._parse_prices(li)
            title = anchor.get_text(strip=True)
            if not title or (current is None and buyout is None):
                # 有商品區塊卻抽不到標題或任何價格 = 解析失敗，不能算「解析成功」，
                # 也不能算「排除純競標」——兩者混在一起，selector 過期時
                # parsed_count 會被排除數撐住，健康判定就瞎了。
                continue
            parsed += 1

            price, price_kind = self._classify_price(current, buyout)
            if price is None:
                # 純競標且沒開放收：現在価格不是付得出去的價格，混進去 =
                # 系統性偏低的假訊號。**這是商業篩選，不是解析失敗**
                # ——上面已經計進 parsed。
                excluded += 1
                continue

            end_time, bids = self._parse_auction_meta(li)
            out.append(
                Listing(
                    site=self.site,
                    external_id=auction_id,
                    title=title,
                    url=_BUYEE_ITEM_URL.format(id=auction_id),  # 購買端，推播直接可下單
                    price=price,
                    currency=Currency.JPY,
                    image_url=self._parse_image(li),
                    seller_id=self._parse_seller(li),
                    ships_to_tw=True,  # 走 Buyee 一定寄得到台灣
                    best_offer_enabled=False,  # 代購買不到「値下げ交渉」
                    raw={"price_kind": price_kind, "current_bid": current},
                    source=self.name,
                    origin_url=urljoin("https://auctions.yahoo.co.jp/", anchor["href"]),
                    end_time=end_time,
                    bids=bids,
                )
            )
        return out, excluded, parsed

    def _classify_price(
        self, current: float | None, buyout: float | None
    ) -> tuple[float | None, str]:
        """(現在価格, 即決価格) → (要用的價格, `price_kind`)。**只有這一份分流。**

        搜尋頁（CSS selector）與賣家頁（`__NEXT_DATA__`）抽欄位的方式完全不同，
        但「哪個價格是付得出去的」這個判斷必須同一份：分流壞掉的症狀是大量假
        FREE_CARD，而每一筆看起來都像撿到寶（見模組 docstring 陷阱 1）。
        `price_kind` 是下游 `bidding.is_live_auction()` 的唯一依據。

        回傳價格為 None ＝ 這筆不收（純競標而 `include_live_auctions` 是關的）。
        """
        if buyout is not None:
            return buyout, "buyout"
        if self.include_live_auctions and current is not None:
            return current, LIVE_AUCTION_KIND
        return None, ""

    # ------------------------------------------------------------------
    # 賣家頁列舉（Seller Alpha 的輪替監控）
    # ------------------------------------------------------------------
    def build_seller_url(self, seller_id: str, *, page: int = 1) -> str:
        """賣家頁網址。`b` 是 1-based 商品 offset（與搜尋頁同一組參數）。

        2026-08-04 實測（賣家 `9RdswzR6…`，`totalResultsAvailable` 89）：
        第 1 頁回 50 筆，`?b=51&n=50` 回 39 筆，**兩頁交集 0 筆**、50+39=89
        ——分頁是真的生效，不是被靜默忽略。（這個站對未知參數是靜默忽略的，
        所以沒實測過的參數一律不准憑印象加，見 `SORT_PARAMS` 的註記。）
        """
        # `safe=""`：賣家 ID 是路徑的一段，`/` 必須被 encode 掉，
        # 否則一個帶斜線的髒 ID 會靜默變成「別的路徑」而不是報錯。
        base = _SELLER_BASE + quote(str(seller_id), safe="")
        if page <= 1:
            return base
        return f"{base}?{urlencode({'b': (page - 1) * _PAGE_SIZE + 1, 'n': _PAGE_SIZE})}"

    def search_seller(
        self, seller_id: str, *, pages: int | None = None, sold: bool = False
    ) -> SearchResult:
        """單一賣家的在架清單（Seller Alpha 的輪替監控）。

        介面與 `paypay_direct.search_seller` 對齊（回 `SearchResult`、帶
        `parsed_count`、三層健康判定），呼叫端是 `pipeline._scan_watched_sellers`。

        `sold` 只為介面相容：**Yahoo 拍賣的賣家頁沒有已售出清單**
        （reports/seller-page-feasibility.md 實測：頁上無「終了分」入口，
        `closedsearch?sellerID=` 也無效）。成交歷史走 `yahoo_closed`
        ——那條路的 comps 已經帶 `seller_id`，折價歷史有主人。
        傳 `sold=True` 會印警告並回**空清單＋EMPTY_CONFIRMED**，不是靜默回在架
        （靜默回在架就等於把「有人開的價」當成「有人付的錢」寫進行情表）。
        """
        if sold:
            print(f"[warn] {self.name} 賣家頁沒有已售出清單，sold=True 回空清單")
            return SearchResult(
                source=self.name, site=self.site.value,
                query=f"seller:{seller_id}", url=self.build_seller_url(seller_id),
                health=ParseHealth.EMPTY_CONFIRMED,
                detail="Yahoo 拍賣賣家頁無已售出清單（成交歷史走 yahoo_closed）",
            )

        pages = max(1, int(pages or 1))
        first_url = self.build_seller_url(seller_id, page=1)
        result = SearchResult(
            source=self.name, site=self.site.value,
            query=f"seller:{seller_id}", url=first_url,
        )

        seen: set[str] = set()
        excluded_bids = 0
        foreign = 0
        for page in range(1, pages + 1):
            url = self.build_seller_url(seller_id, page=page)
            try:
                html = self.fetcher.get(url, allow_statuses=(404,))
            except BlockedError as exc:
                if page == 1:
                    result.health = ParseHealth.BLOCKED
                    result.detail = f"被擋：{exc}"
                    return result
                print(f"[warn] {self.name} 賣家頁 p{page} 被擋，只回傳前 {page - 1} 頁: {exc}")
                break
            except FetchError as exc:
                # 第一頁抓不到 = 對這個賣家「什麼都不知道」，跟「他沒貨」是兩件事
                if page == 1:
                    result.health = ParseHealth.FETCH_FAILED
                    result.detail = f"抓取失敗：{exc}"
                    return result
                print(f"[warn] {self.name} 賣家頁 p{page} 抓取失敗，只回傳前 {page - 1} 頁: {exc}")
                break

            result.pages_fetched = page
            node, profile = self._seller_nodes(html)

            if page == 1:
                result.html_bytes = len(html)
                health, detail = self._judge_seller_health(node)
                if health is not ParseHealth.OK:
                    result.health = health
                    result.detail = detail
                    return result
            elif node is None:
                print(f"[warn] {self.name} 賣家頁 p{page} 找不到 __NEXT_DATA__，"
                      f"只回傳前 {page - 1} 頁")
                break

            raw_items = (node or {}).get("items") or []
            batch, stats = self._extract_seller_listings(
                raw_items, seen, seller_id=str(seller_id), profile=profile
            )
            excluded_bids += stats["bid_only"]
            foreign += stats["foreign"]
            result.parsed_count += stats["parsed"]
            result.listings.extend(batch)
            if len(raw_items) < _PAGE_SIZE:
                break  # 已是最後一頁

        notes = []
        if excluded_bids:
            notes.append(f"排除純競標 {excluded_bids} 筆")
        if foreign:
            notes.append(f"排除フリマ標的 {foreign} 筆")
        result.detail = "、".join(notes)
        return result

    @staticmethod
    def _seller_nodes(html: str) -> tuple[dict | None, dict | None]:
        """挖出賣家頁的 (商品清單節點, 賣家檔案節點)。任一步失敗回 (None, None)
        ／(node, None)，由 `_judge_seller_health` 統一判成 PARSER_BROKEN 並告警。
        """
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag is None:
            return None, None
        try:
            payload = json.loads(tag.get_text())
        except (ValueError, TypeError):
            return None, None
        node = _dig(payload, _SELLER_LISTING_PATH)
        profile = _dig(payload, _SELLER_PROFILE_PATH)
        return (
            node if isinstance(node, dict) else None,
            profile if isinstance(profile, dict) else None,
        )

    @staticmethod
    def _judge_seller_health(node: dict | None) -> tuple[ParseHealth, str]:
        """三層判定，與 `yahoo_closed._judge_health` 同構（同一種 JSON 節點）。

        ⚠️ 交叉比對用的是 **`items` 的原始長度**，不是過濾後的筆數：
        「這個賣家在架的 50 筆全是純競標」是市場現象，不是解析壞掉。
        """
        if node is None:
            return (
                ParseHealth.PARSER_BROKEN,
                "找不到 __NEXT_DATA__ 或 search.items.listing 節點：賣家頁結構已改版",
            )
        total = node.get("totalResultsAvailable")
        raw = node.get("items")
        raw_n = len(raw) if isinstance(raw, list) else 0

        # 第 1 層（最強）：命中數交叉比對，同一份 JSON、同源同基準
        if isinstance(total, int) and total > 0 and raw_n == 0:
            return ParseHealth.PARSER_BROKEN, f"賣家頁標示 {total} 件但解析出 0 筆"
        # 第 2 層：頁面自己說這個賣家現在沒有在架商品
        #（2026-08-04 實測：名單上 7 個 Yahoo 賣家裡有 3 個當下 total=0）
        if total == 0:
            return ParseHealth.EMPTY_CONFIRMED, "賣家目前沒有在架商品（0 件）"
        if raw_n == 0:
            return ParseHealth.PARSER_BROKEN, "items 為空且無件數可比對：賣家頁結構已改版"
        return ParseHealth.OK, ""

    @staticmethod
    def seller_feedback(profile: dict | None) -> dict[str, Any] | None:
        """賣家檔案 → `raw["seller"]`（`venue_study._seller_feedback` 認得的形狀）。

        ⚠️ **`goodRatio` 要乘 100**：Yahoo 給的是**比例**（`0.978`），而
        PayPay 給的同名欄位是**百分比**（`99.7`）——`sellers.feedback_pct` 只有
        一個欄位、`seller_alpha` 的門檻是 `< 95` / `< 98`。不換算的話 Yahoo 賣家
        會全部被判成「好評率 0.98% → -15 分」，而且完全沒有外顯症狀
        （只是分數低了 15，看起來像個很爛的賣家）。同一欄位必須同單位——工程原則 1。

        抽不到一律 None（不猜）：`risk_known=False` 是「風險未知」，
        比一個編出來的 100% 誠實。
        """
        rating = (profile or {}).get("rating")
        if not isinstance(rating, dict):
            return None
        total = rating.get("total")
        ratio = rating.get("goodRatio")
        out: dict[str, Any] = {}
        if isinstance(total, int):
            out["numRating"] = total
        if isinstance(ratio, (int, float)):
            out["goodRatio"] = round(float(ratio) * 100.0, 2)
        return out or None

    def _extract_seller_listings(
        self,
        items: list,
        seen: set[str],
        *,
        seller_id: str,
        profile: dict | None,
    ) -> tuple[list[Listing], dict[str, int]]:
        """賣家頁的 `items` → Listing。價格分流走 `_classify_price`（與搜尋頁同一份）。

        `parsed` 的語意與全來源一致：**商業篩選之前**、解析器真的認得的商品數。
        排除純競標與排除フリマ標的都是商業／路由決定（解析器好好的），計入 parsed；
        欄位不全才是解析失敗，不計入。
        """
        out: list[Listing] = []
        stats = {"parsed": 0, "bid_only": 0, "foreign": 0}
        feedback = self.seller_feedback(profile)
        for it in items:
            if not isinstance(it, dict):
                continue
            auction_id = it.get("auctionId")
            title = it.get("title")
            current = _as_price(it.get("price"))
            buyout = _as_price(it.get("buyNowPrice"))
            if not auction_id or not title or (current is None and buyout is None):
                continue
            stats["parsed"] += 1
            if str(auction_id) in seen:
                continue

            if it.get("isFleamarketItem"):
                # ヤフオク!の賣家頁上混進フリマ標的：ID 空間與購買路徑都不是
                # Yahoo 拍賣的（`buyee.jp/paypayfleamarket/...`）。整批 batch
                # 掛的是同一個 site，收下去就會讓 listing_obs 的 site 說謊。
                stats["foreign"] += 1
                continue

            price, price_kind = self._classify_price(current, buyout)
            if price is None:
                stats["bid_only"] += 1
                continue

            seen.add(str(auction_id))
            raw_seller = it.get("seller") if isinstance(it.get("seller"), dict) else {}
            out.append(
                Listing(
                    site=self.site,
                    external_id=str(auction_id),
                    title=str(title),
                    url=_BUYEE_ITEM_URL.format(id=auction_id),
                    price=price,
                    currency=Currency.JPY,
                    image_url=it.get("imageUrl"),
                    seller_id=str(raw_seller.get("userId") or seller_id),
                    ships_to_tw=True,   # 走 Buyee 一定寄得到台灣
                    best_offer_enabled=False,   # 代購買不到「値下げ交渉」
                    raw={
                        "price_kind": price_kind,
                        "current_bid": current,
                        # 賣家好評率（Seller Alpha 的風險維度）。放 raw，
                        # 由 `venue_study.listing_row` 捎給 sellers 表。
                        **({"seller": feedback} if feedback else {}),
                    },
                    source=self.name,
                    origin_url=_YAHOO_AUCTION_URL.format(id=auction_id),
                    end_time=_parse_iso(it.get("endTime")),
                    bids=it.get("bidCount") if isinstance(it.get("bidCount"), int) else None,
                )
            )
        return out, stats

    @staticmethod
    def _parse_auction_meta(li: Tag) -> tuple[datetime | None, int | None]:
        """回傳 (結標時間 UTC, 出價數)。抽不到一律 None，**絕不用「現在＋殘り文字」猜**。

        來源選擇是刻意的：`div.Product__bonus[data-auction-endtime]` 是 **epoch 秒**
        （2026-08-02 實測 53/53 筆都有），沒有時區歧義。同一格還有一個看得見的
        「残り 1日／10時間」文字（`dd.Product__time`），但那是**四捨五入過的相對
        時間**——拿它反推絕對時間會有數小時誤差，而競標最後五分鐘才是決勝點，
        誤差幾小時等於這個欄位沒有用。抽不到 epoch 就回 None，讓上層顯示「未知」。
        """
        end_time: datetime | None = None
        node = li.select_one(".Product__bonus")
        raw_end = node.get("data-auction-endtime") if isinstance(node, Tag) else None
        if raw_end:
            try:
                end_time = datetime.fromtimestamp(int(raw_end), UTC)
            except (TypeError, ValueError, OSError, OverflowError):
                end_time = None

        bids: int | None = None
        bid_node = li.select_one("dd.Product__bid")
        if bid_node is not None:
            m = re.search(r"\d+", bid_node.get_text(strip=True))
            if m:
                bids = int(m.group(0))
        return end_time, bids

    @staticmethod
    def _parse_seller(li: Tag) -> str | None:
        """賣家 ID：`div.Product__bonus[data-auction-auc-seller-id]`。

        值是 28-29 字的混淆 ID（不是舊式帳號名），2026-08-03 實測：搜尋頁
        53/53 筆都有、與 closedsearch 的 `seller.userId` 同一空間、跨日穩定
        26/26（reports/seller-id-availability.md）。抽不到一律 None，不猜。
        """
        node = li.select_one(".Product__bonus")
        if not isinstance(node, Tag):
            return None
        sid = node.get("data-auction-auc-seller-id")
        return str(sid) if sid else None

    @staticmethod
    def _parse_prices(li: Tag) -> tuple[float | None, float | None]:
        """回傳 (現在価格, 即決価格)。判別只看 `.Product__label` 文字——

        `u-textRed` 只是樣式 class，不可當語意依據（RECON 明講）。
        """
        current: float | None = None
        buyout: float | None = None
        for span in li.select(".Product__price"):
            label = span.select_one(".Product__label")
            value = span.select_one(".Product__priceValue")
            if label is None or value is None:
                continue
            m = _PRICE_TEXT_RE.search(value.get_text(strip=True))
            if not m:
                continue
            amount = float(m.group(1).replace(",", ""))
            kind = label.get_text(strip=True)
            if kind == "現在":
                current = amount
            elif kind == "即決":
                buyout = amount
        return current, buyout

    @staticmethod
    def _parse_image(li: Tag) -> str | None:
        img = li.select_one("img")
        if not isinstance(img, Tag):
            return None
        src = img.get("data-src") or img.get("src") or img.get("data-original")
        if src and src.startswith("//"):
            src = "https:" + src
        return src
