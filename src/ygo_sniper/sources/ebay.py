"""eBay Browse API。

App ID 還沒下來也沒關係：`enabled` 是 False 時不會打任何 API，
但**會回一個 BLOCKED 的 SearchResult**（不是安靜的空清單）——watchlist 裡
排了 eBay 查詢卻沒有憑證是設定沒做完，那要看得見，不是每小時安靜地跳過。

只用 Browse API（免費、申請即可）。成交價要 Marketplace Insights API，
那個要另外審核而且常被拒，所以 comps 走自建 snapshot（見 comps.py），
`supports_sold = False`。

**2026-08-02：補上 `search_detailed()`，把 eBay 接進健康監測系統。**
在那之前它是唯一一條只有舊 `search()` 介面的管道，後果是：不參與 ParseHealth
判定、不跑 canary、**壞掉時不會告警**——API 401、eBay 改版、憑證過期，外顯
全都是「安靜地回 0 筆」，而 0 筆和「今天美國沒好貨」長得一模一樣。
那正是整個告警系統要解的病，卻有一條管道站在系統外面。

交叉比對用 Browse API 回應的 `total` 欄位（等同其他來源的「命中數」）：
API 說有 N 筆、我們卻解析出 0 筆 → 必定是我們這邊壞了，不可能是市場問題。

**2026-08-03：價格語意（本檔第二條紅線，與 sources/yahoo.py 同一件事）。**
在那之前 `_to_listing` 是 `price = item.get("price") or {}`、取不到 value 就回 None
——而 eBay 的**純競標標的 summary 端點 price 就是 null**（實測 100/100 筆），
價格在 `currentBidPrice`。後果：掃描端把 eBay 的競標整批當成「欄位不全」靜默丟掉，
外顯是「eBay 今天沒貨」。三種形狀都是 2026-08-03 實測（fixtures/ebay_api_items.json）：

  buyingOptions          summary.price   summary.currentBidPrice  語意
  FIXED_PRICE(+BEST_OFFER)  有            無                       定價，可立即成交
  AUCTION(+BEST_OFFER)      **null**      有（＋bidCount）         純競標，會漲
  AUCTION+FIXED_PRICE       BIN 價        目前出價                 競標帶 BIN，BIN 可成交

⚠️ **單品端點（/item/v1|{id}|0）不一樣**：純競標標的的 `price` 有值，而且
**等於 `currentBidPrice`**（實測兩筆：71.00/71.00、27,783/27,783）。所以
「有沒有 price」只在 summary 端點是有效判準，兩個端點共用的唯一安全判準是
**`buyingOptions`**（見 `read_price()`）——這也是為什麼那個函式是共用的一份。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from ..bidding import LIVE_AUCTION_KIND, is_live_auction
from ..config import Config
from ..domain import Currency, Listing, Site
from .base import _log_request
from .health import ParseHealth, SearchResult

_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
#: 單品端點。`{item_id}` 吃的是 RESTful id（`v1|{網址上的數字}|0`），不是純數字。
_ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{item_id}"
_SCOPE = "https://api.ebay.com/oauth/api_scope"

#: 遊戲王卡片在 eBay 的分類（CCG Individual Cards 底下的 Yu-Gi-Oh! 格）。
#: watchlist 沒指定 `category` 時的預設值——**這條管道不帶分類就會撈到整個
#: eBay**，所以預設不是 None。
_CATEGORY_YUGIOH = "183454"

_PAGE_LIMIT = 100


class EbayAuthError(RuntimeError):
    """憑證被拒（401/403）。**語意失敗，重試沒有意義**——要人去換金鑰。"""


class EbayTransientError(RuntimeError):
    """連線層失敗（逾時、5xx、429）。下一輪可能就好了。"""


class EbayItemNotFound(RuntimeError):
    """單品端點回 404：這個商品號不存在或已下架。**語意失敗**，重試沒有意義。"""


def item_id_v1(raw_id: str) -> str:
    """網址上的數字 id → Browse API 的 RESTful item id（`v1|{id}|0`）。

    已經是 `v1|…` 形狀就原樣回傳（呼叫端可能拿的是 summary 的 `itemId`）。
    """
    raw = str(raw_id).strip()
    return raw if raw.startswith("v1|") else f"v1|{raw}|0"


def item_api_url(raw_id: str) -> str:
    """網址上的商品號 → Browse API 單品端點網址（錯誤訊息與 Target.fetch_url 用）。"""
    return _ITEM_URL.format(item_id=item_id_v1(raw_id))


#: 預設的使用者情境：寄台灣。**掃描與鑑價的主路徑都是這一份**。
_TW_CONTEXT = "contextualLocation=country=TW"


def us_context(zip_code: str) -> str:
    """美國地址的 ENDUSERCTX（鑑價的替代路徑用，見 settings.yaml `buying.us_ship_zip`）。

    帶了它 eBay 會回「寄到該 zip」的美國境內運費、價格以賣家原幣（不再換台幣）。
    只做字串組裝，**預設路徑仍然是台灣**——這是使用者的明確要求。
    """
    return f"contextualLocation=country=US,zip={zip_code}"


def browse_headers(token: str, *, context: str | None = None) -> dict[str, str]:
    """Browse API 的共用 header。**只有這一份**。

    `X-EBAY-C-ENDUSERCTX: contextualLocation=country=TW` 是關鍵：帶了它 eBay 才會
    回「寄到台灣」的實際運費，而且會把價格**換算成台幣**（實測 `price` 帶
    `convertedFromValue`／`convertedFromCurrency`）。搜尋端與單品端各寫一份的話，
    哪天有人只改其中一邊，兩條路徑就會拿到不同幣別的價格（工程原則 1）。

    `context` 只給鑑價的美國地址替代路徑覆寫（`us_context()`）；不傳＝台灣。
    """
    return {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "X-EBAY-C-ENDUSERCTX": context or _TW_CONTEXT,
    }


@dataclass(frozen=True, slots=True)
class EbayPrice:
    """一筆 eBay 標的的價格語意。**value 與 currency 永遠一起讀。**

    `kind` 只有兩個值，而且 `LIVE_AUCTION_KIND` 是從 `bidding` import 進來的，
    不是抄一份字面值——下游（`bidding.is_live_auction`、scoring 的競標分流、
    出價上限、推播規則）全部認那個常數，抄字串就會有兩份定義。
    """

    kind: str                    # "buyout"（可立即成交）| LIVE_AUCTION_KIND（會漲）
    value: float                 # 拿來算成本的那個數字
    currency: str
    current_bid: float | None    # 目前出價（competing 的那個價，可能同時存在）
    bid_count: int | None
    end_time: datetime | None
    is_auction: bool             # buyingOptions 含 AUCTION
    has_buy_now: bool            # buyingOptions 含 FIXED_PRICE（＝可立即成交）


BUYOUT_KIND = "buyout"


def _money(node: object) -> tuple[float, str] | None:
    """eBay 的金額物件 → (值, 幣別)。缺任一半就回 None（**不預設幣別**）。"""
    if not isinstance(node, dict):
        return None
    try:
        value = float(node["value"])
    except (KeyError, TypeError, ValueError):
        return None
    currency = node.get("currency")
    if not currency:
        return None
    return value, str(currency)


def parse_ebay_datetime(raw: object) -> datetime | None:
    """`itemEndDate` 之類的 ISO 8601（帶 Z）→ 帶時區的 UTC datetime。解不出來回 None。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def read_price(item: dict) -> EbayPrice | None:
    """商品（summary 或單品端點皆可）→ 價格語意。抽不到任何可用價格回 None。

    **判準是 `buyingOptions`，不是「price 欄位有沒有值」**（2026-08-03 實測）：
    單品端點的純競標標的 `price` 有值且等於 `currentBidPrice`，拿它當可成交價
    就是把「別人的出價」當成「你付得出去的價格」——那個方向的偏差是系統性低估，
    每一筆看起來都像撿到寶（與 Yahoo 現在価格／即決価格是同一條紅線）。

    - AUCTION 且**沒有** FIXED_PRICE → 純競標：`value` 取 `currentBidPrice`
      （沒有就退回 `price`，兩者在單品端點實測相等），`kind=LIVE_AUCTION_KIND`。
    - 其他（FIXED_PRICE、或 AUCTION+FIXED_PRICE 的競標帶 BIN）→ `value` 取
      `price`，`kind="buyout"`：BIN 價點下去就能成交，那才是你付得出去的價格。
      競標帶 BIN 時 `current_bid` 仍然帶回來（報告要說得出「同時在競標」）。
    """
    if not isinstance(item, dict):
        return None
    opts = {str(o) for o in (item.get("buyingOptions") or [])}
    is_auction = "AUCTION" in opts
    has_buy_now = "FIXED_PRICE" in opts

    priced = _money(item.get("price"))
    bid = _money(item.get("currentBidPrice"))
    raw_bids = item.get("bidCount")
    bid_count = int(raw_bids) if isinstance(raw_bids, (int, float)) else None
    end_time = parse_ebay_datetime(item.get("itemEndDate"))

    if is_auction and not has_buy_now:
        chosen = bid or priced
        if chosen is None:
            return None
        return EbayPrice(
            kind=LIVE_AUCTION_KIND,
            value=chosen[0],
            currency=chosen[1],
            current_bid=chosen[0],
            bid_count=bid_count,
            end_time=end_time,
            is_auction=True,
            has_buy_now=False,
        )

    if priced is None:
        return None
    return EbayPrice(
        kind=BUYOUT_KIND,
        value=priced[0],
        currency=priced[1],
        current_bid=bid[0] if bid else None,
        bid_count=bid_count,
        end_time=end_time,
        is_auction=is_auction,
        has_buy_now=has_buy_now,
    )


@dataclass(frozen=True, slots=True)
class NativePrice:
    """一筆 eBay 標的的**原幣**資訊（出價欄要填的那個幣別）。

    `rate` 是「這筆 listing 自己的」eBay 換匯比率：`value / convertedFromValue`
    （listing 顯示幣別 / 原幣）。出價上限反解出台幣之後**必須用這個比率**換回
    原幣，不可以用我們自己的 fx 表——eBay 顯示的台幣是它自己的匯率換出來的，
    拿另一張匯率表換回去就是兩邊各一個匯率的混源比較（工程原則 1）。
    """

    value: float          # 原幣價格（純競標＝目前出價、其他＝price）
    currency: str         # 原幣幣別（"USD"／"GBP"…；無換算資訊時＝顯示幣別）
    rate: float           # 顯示幣別 / 原幣。無換算資訊時 = 1.0


def native_price_info(item: dict) -> NativePrice | None:
    """商品 dict → 原幣資訊。**選節點的規則與 `read_price()` 完全一致**：
    純競標看 `currentBidPrice`、其他看 `price`——兩個函式對同一筆標的
    必須指著同一個金額節點，否則「上限」與「現價」會各自出自不同的價格。

    沒有 `convertedFrom*`（listing 本來就以原幣顯示）→ rate=1.0、幣別照抄。
    節點壞掉、比率非正 → None（**不猜**，呼叫端要把「換匯資訊缺失」講出來）。
    """
    if not isinstance(item, dict):
        return None
    opts = {str(o) for o in (item.get("buyingOptions") or [])}
    pure_auction = "AUCTION" in opts and "FIXED_PRICE" not in opts
    node = item.get("currentBidPrice") if pure_auction else item.get("price")
    if node is None and pure_auction:
        node = item.get("price")  # 單品端點兩者實測相等（模組頂註）
    shown = _money(node)
    if shown is None:
        return None
    raw_from = node.get("convertedFromValue")
    from_ccy = node.get("convertedFromCurrency")
    if raw_from is None or not from_ccy:
        return NativePrice(value=shown[0], currency=shown[1], rate=1.0)
    try:
        native = float(raw_from)
    except (TypeError, ValueError):
        return None
    if native <= 0 or shown[0] <= 0:
        return None
    return NativePrice(
        value=native, currency=str(from_ccy), rate=shown[0] / native
    )


def read_shipping(item: dict, currency: str) -> tuple[float | None, str | None]:
    """寄到台灣的運費 → (金額, 說不出金額時的原因)。金額一律與商品同幣別。

    **幣別不同就當成未知**（工程原則 1）：商品 TWD、運費 USD 直接相加會低估
    運費 30 倍，而且沒有任何錯誤訊息。實測正常狀況兩者都是 TWD（contextualLocation
    生效時），所以幣別不一致本身就是「這個回應不對勁」的訊號，不是可以硬算的情況。
    """
    options = item.get("shippingOptions") or []
    if not options:
        return None, "eBay 沒有回報任何寄到台灣的運送選項"
    for opt in options:
        if not isinstance(opt, dict):
            continue
        cost = _money(opt.get("shippingCost"))
        if cost is None:
            continue
        if cost[1] != currency:
            return None, f"運費幣別（{cost[1]}）與商品幣別（{currency}）不同，不能直接相加"
        return cost[0], None
    return None, "運送選項裡沒有金額（賣家多半設成到貨再報價／自取）"


def ships_to_tw(item: dict) -> bool | None:
    """寄不寄台灣。True／False／None（＝API 沒說，判斷不了，**不猜**）。

    `regionExcluded` 一定要看：實測有標的是 `regionIncluded=[WORLDWIDE]` 但
    `regionExcluded=[BY, UA, RU]`——只看 included 的話，一個明確排除台灣的賣家
    會被判成「寄得到」，而那筆的到手成本是一個不存在的數字。
    """
    locs = item.get("shipToLocations")
    if not isinstance(locs, dict):
        return None
    included = {str(r.get("regionId")) for r in locs.get("regionIncluded") or []}
    excluded = {str(r.get("regionId")) for r in locs.get("regionExcluded") or []}
    if "TW" in excluded:
        return False
    if not included:
        return None
    return "TW" in included or "WORLDWIDE" in included


class EbaySource:
    name = "ebay"
    site = Site.EBAY
    #: Browse API 沒有成交紀錄；Marketplace Insights 要另外審核。
    supports_sold = False
    #: 吃得下 `category`（→ Browse API 的 `category_ids`，可逗號分隔多格）。
    supports_category = True

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.enabled = cfg.ebay_enabled
        #: 競標標的要不要進掃描結果。出貨設定（settings.yaml）已改為 **true**
        #: （2026-08-03）：先前擋著它的兩個正確性問題都已修掉——
        #: (1) 出價上限改走 `bidding.max_bid_ebay`（幣別鏈完全跟著 listing 走：
        #:     台幣反解、用 listing 自己的換匯比率換回原幣，運費用 listing 上的
        #:     實際運費）；(2) scoring 的 headroom 比較改成「與 listing 同幣別」
        #:     （`BidCeiling.headroom_value`），不再拿日圓上限比台幣現價。
        #: 程式碼層的預設值**維持 False**：settings 沒寫這個鍵時寧可少看，
        #: 也不要在設定不完整的環境放行一條幣別敏感的管道。
        self.include_live_auctions = bool(
            (cfg.sources.get(self.name) or {}).get("include_live_auctions", False)
        )
        #: 賣家頁列舉要帶的關鍵字（見 `search_seller` 的實測依據）。
        #: 空字串＝不帶關鍵字，那會讓大型綜合賣家的第 1 頁完全看不到遊戲王。
        self.seller_page_keyword = str(
            (cfg.sources.get(self.name) or {}).get("seller_page_keyword", "yugioh")
        )
        self._token: str | None = None
        self._token_exp = 0.0

    # ------------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        creds = f"{self.cfg.ebay_client_id}:{self.cfg.ebay_client_secret}"
        auth = base64.b64encode(creds.encode()).decode()
        with httpx.Client(timeout=20) as client:
            t0 = time.monotonic()
            try:
                r = client.post(
                    _OAUTH_URL,
                    headers={
                        "Authorization": f"Basic {auth}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"grant_type": "client_credentials", "scope": _SCOPE},
                )
            except httpx.HTTPError as exc:
                # _OAUTH_URL 不帶查詢字串、憑證在 header 不在 URL，記下來不會外洩
                _log_request(_OAUTH_URL, type(exc).__name__, t0)
                raise EbayTransientError(f"OAuth 連線失敗: {type(exc).__name__}: {exc}") from exc
            _log_request(_OAUTH_URL, r.status_code, t0)
            _raise_for_status(r, "OAuth")
            blob = r.json()
        self._token = blob["access_token"]
        self._token_exp = time.time() + float(blob.get("expires_in", 7200))
        return self._token

    # ------------------------------------------------------------------
    def get_item(self, raw_item_id: str, *, context: str | None = None) -> dict:
        """單品端點：一個商品號 → 完整商品 JSON。**鑑價（appraise）走這條。**

        與搜尋共用 `_get_token()` 與 `browse_headers()`——另寫一份 OAuth 就會有
        兩份憑證處理、兩種過期行為，而其中一份不會被 canary 測到。

        `context` 只給鑑價的美國地址替代路徑用（`us_context(zip)`）——它會讓
        eBay 回美國境內運費與原幣價格。**預設不傳＝台灣**，掃描永遠不碰這個參數。

        失敗一律分類（工程原則 2/5）：憑證問題 → `EbayAuthError`（重試無意義）、
        404 → `EbayItemNotFound`（商品不存在／已下架，語意失敗）、
        連線與 5xx → `EbayTransientError`。
        """
        if not self.enabled:
            raise EbayAuthError(
                "未設定 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET，打不了 eBay API"
            )
        token = self._get_token()
        url = _ITEM_URL.format(item_id=item_id_v1(raw_item_id))
        with httpx.Client(timeout=30) as client:
            t0 = time.monotonic()
            try:
                r = client.get(url, headers=browse_headers(token, context=context))
            except httpx.HTTPError as exc:
                # token 在 header（Bearer）不在 url，記下來不會外洩
                _log_request(url, type(exc).__name__, t0)
                raise EbayTransientError(
                    f"連線失敗: {type(exc).__name__}: {exc}"
                ) from exc
            _log_request(url, r.status_code, t0)
            if r.status_code == 404:
                raise EbayItemNotFound(
                    f"eBay 找不到這個商品（HTTP 404）：{raw_item_id}"
                    "——商品號打錯，或這筆已經下架／結標"
                )
            _raise_for_status(r, "Item")
            try:
                blob = r.json()
            except ValueError as exc:
                raise EbayTransientError(f"回應不是 JSON: {exc}") from exc
        if not isinstance(blob, dict):
            raise EbayTransientError(f"回應不是 JSON 物件，而是 {type(blob).__name__}")
        return blob

    # ------------------------------------------------------------------
    def _max_price_usd(self, max_price_jpy: float | None) -> float | None:
        """pipeline 傳進來的價格上限是**日圓**（`_price_ceiling_jpy`），
        但 eBay 的過濾器吃美元。

        以前這裡直接把日圓數字塞進 `priceCurrency:USD`，等於把上限放大約 140 倍
        ——過濾器形同不存在，而且完全沒有錯誤訊息（工程原則 1：被比較的兩個值
        必須同單位）。換算走 config 的手動匯率（`fx.jpy_twd` / `fx.usd_twd`），
        不打網路：這只是「別抓太貴的」的粗篩，不需要即時匯率。
        """
        if not max_price_jpy:
            return None
        fx = self.cfg.fx or {}
        try:
            jpy_twd = float(fx["jpy_twd"])
            usd_twd = float(fx["usd_twd"])
        except (KeyError, TypeError, ValueError):
            return None
        if usd_twd <= 0:
            return None
        return round(max_price_jpy * jpy_twd / usd_twd, 2)

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
        """相容包裝：只要清單的人用這個。

        `sold=True` 一律回空清單（`supports_sold=False`，Browse API 沒有成交紀錄）。
        """
        if sold:
            return []
        return self.search_detailed(
            keyword, max_price=max_price, pages=pages, category=category
        ).listings

    def search_seller(self, seller_id: str, *, pages: int | None = None) -> SearchResult:
        """單一賣家的在架清單（Seller Alpha 的輪替監控）。

        走 Browse API 的 `filter=sellers:{username}`（2026-08-03 實測有效，見
        reports/seller-page-feasibility.md）。

        ⚠️ **必須帶關鍵字，這是 eBay 特有的**（2026-08-04 實測）：eBay 沒有
        遊戲王專屬分類——`ebay:psa` 的每一筆商品分類都是 `183454 CCG Individual
        Cards`（100/100），寶可夢與遊戲王在同一格。而 psa 是「什麼都鑑定什麼都
        賣」的賣家：那格底下他有 **135,638** 件，其中 `q=yugioh` 只有 **6,241**
        件。不帶關鍵字、只抓第 1 頁（依價格升冪）的結果實測是 100 筆寶可夢
        ——**他的遊戲王庫存一筆都看不到**。
        也就是說關鍵字在這條路徑上是「看得到什麼」的覆蓋率決定，不是判準：
        年代與候選判定仍然完全交給 pipeline 的 `parse_card` → `is_candidate`
        （判準只有一份）。關鍵字可用 `sources.ebay.seller_page_keyword` 覆寫。
        `yugioh` 對 `YU-GI-OH!` 寫法的標題實測命中（eBay 會正規化連字號）。
        """
        return self.search_detailed(
            self.seller_page_keyword, pages=pages, seller=seller_id
        )

    def search_detailed(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        pages: int | None = None,
        category: str | None = None,
        seller: str | None = None,
    ) -> SearchResult:
        pages = pages or int(self.cfg.fetch["max_pages_per_query"])
        # 沒指定就用遊戲王那一格。**不可以退化成「不帶分類」**：
        # Browse API 的 category_ids 缺席等於搜整個 eBay，回來的雜訊會淹掉一切。
        category_ids = category or _CATEGORY_YUGIOH
        result = SearchResult(
            source=self.name, site=self.site.value,
            query=keyword or (f"seller:{seller}" if seller else ""), url=_BROWSE_URL,
        )
        if not self.enabled:
            # 「沒有憑證」與「今天沒好貨」必須分得開：後者安靜、前者要有人去補設定。
            result.health = ParseHealth.BLOCKED
            result.detail = "未設定 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET，這條管道等於沒有在跑"
            return result

        # 競標要進結果的話，filter 也得放它進來——不然只會撈到「剛好也開放議價」
        # 的那些競標（AUCTION+BEST_OFFER），而那是一群沒有理由的樣本。
        buying = ["FIXED_PRICE", "BEST_OFFER"]
        if self.include_live_auctions:
            buying.append("AUCTION")
        filters = ["deliveryCountry:TW", "buyingOptions:{" + "|".join(buying) + "}"]
        price_usd = self._max_price_usd(max_price)
        if price_usd:
            filters.append(f"price:[..{price_usd:.2f}],priceCurrency:USD")
        if seller:
            # `sellers:{a|b}` 是清單語法；單一賣家也照這個形狀送（實測有效）。
            filters.append("sellers:{" + str(seller) + "}")

        try:
            token = self._get_token()
        except EbayAuthError as exc:
            result.health = ParseHealth.BLOCKED
            result.detail = f"憑證被拒：{exc}"
            return result
        except EbayTransientError as exc:
            result.health = ParseHealth.FETCH_FAILED
            result.detail = f"取 token 失敗：{exc}"
            return result

        headers = browse_headers(token)

        seen: set[str] = set()
        malformed = 0
        excluded_auctions = 0
        total: int | None = None

        with httpx.Client(timeout=30) as client:
            for page in range(pages):
                params = {
                    "category_ids": category_ids,
                    "limit": _PAGE_LIMIT,
                    "offset": page * _PAGE_LIMIT,
                    "filter": ",".join(filters),
                    "sort": "price",
                }
                # 空關鍵字**不送 `q`**：Browse API 要求 q／category_ids 至少有一個，
                # 而 `q=` 空字串會被當成語法錯（HTTP 400）。賣家頁列舉走的正是
                # 「只有分類、沒有關鍵字」這條（見 `search_seller`）。
                if keyword:
                    params["q"] = keyword
                if page == 0:
                    result.url = str(httpx.URL(_BROWSE_URL, params=params))
                try:
                    blob, nbytes = self._get_page(client, headers, params)
                except EbayAuthError as exc:
                    if page == 0:
                        result.health = ParseHealth.BLOCKED
                        result.detail = f"憑證被拒：{exc}"
                        return result
                    print(f"[warn] {self.name} p{page + 1} 憑證被拒，只回傳前 {page} 頁: {exc}")
                    break
                except EbayTransientError as exc:
                    # 第一頁抓不到 = 對這個關鍵字「什麼都不知道」，跟「沒貨」是兩件事
                    if page == 0:
                        result.health = ParseHealth.FETCH_FAILED
                        result.detail = f"抓取失敗：{exc}"
                        return result
                    print(f"[warn] {self.name} p{page + 1} 抓取失敗，只回傳前 {page} 頁: {exc}")
                    break

                result.pages_fetched = page + 1
                items = blob.get("itemSummaries") or []
                if page == 0:
                    raw_total = blob.get("total")
                    total = raw_total if isinstance(raw_total, int) else None
                    result.html_bytes = nbytes

                for item in items:
                    listing = self._to_listing(item)
                    if listing is None:
                        malformed += 1
                        continue
                    if listing.external_id in seen:
                        continue
                    seen.add(listing.external_id)
                    # parsed_count 的語意是「**商業篩選之前**、解析器真的認得的
                    # 商品數」（與 sources/yahoo.py 同一套）。競標被 config 擋掉
                    # 是商業決定，與解析器活不活著無關——混在一起的話，
                    # 「今天上架的全是競標」會被誤報成「解析器壞了」。
                    result.parsed_count += 1
                    if not self.include_live_auctions and is_live_auction(listing):
                        # 純競標：目前出價不是付得出去的價格。丟掉但**數出來**，
                        # 之前這一批是被當成「欄位不全」靜默丟掉的（price 是 null）。
                        excluded_auctions += 1
                        continue
                    result.listings.append(listing)

                if len(items) < _PAGE_LIMIT:
                    break  # 已是最後一頁

        if result.parsed_count == 0:
            result.health, result.detail = self._judge_empty(total, malformed)
            return result

        notes = [f"API 命中 {total} 筆" if total is not None else "API 未回報 total"]
        if malformed:
            notes.append(f"排除欄位不全 {malformed} 筆")
        if excluded_auctions:
            notes.append(f"排除純競標 {excluded_auctions} 筆")
        result.detail = "、".join(notes) if (malformed or excluded_auctions) else ""
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _judge_empty(total: int | None, malformed: int) -> tuple[ParseHealth, str]:
        """0 筆的三選一。判準與其他來源同構：先看命中數交叉比對，再看確認空。

        `total` 是 Browse API 自己回報的命中數，與 items 出自**同一個回應**
        （同源同基準）——它說有貨、我們卻一筆都沒解出來，那必定是我們壞了。
        """
        if total is None:
            return (
                ParseHealth.PARSER_BROKEN,
                "回應沒有 total 欄位、也沒有任何商品：API 格式已改版",
            )
        if total > 0:
            return (
                ParseHealth.PARSER_BROKEN,
                f"API 標示 {total} 筆但解析出 0 筆"
                + (f"（其中欄位不全 {malformed} 筆）" if malformed else ""),
            )
        return ParseHealth.EMPTY_CONFIRMED, "API 回報 total=0：確認查無結果"

    @staticmethod
    def _get_page(client: httpx.Client, headers: dict, params: dict) -> tuple[dict, int]:
        """回 (JSON 物件, 回應 bytes)。回應大小是排錯線索——被擋的回應通常異常小。"""
        # 完整 URL（含 query）才記得出「這頁在查什麼」；params 只有關鍵字／分類／
        # 分頁這類公開參數，token 在 headers 裡的 Authorization，不會被印出來
        full_url = str(httpx.URL(_BROWSE_URL, params=params))
        t0 = time.monotonic()
        try:
            r = client.get(_BROWSE_URL, headers=headers, params=params)
        except httpx.HTTPError as exc:
            _log_request(full_url, type(exc).__name__, t0)
            raise EbayTransientError(f"連線失敗: {type(exc).__name__}: {exc}") from exc
        _log_request(full_url, r.status_code, t0)
        _raise_for_status(r, "Browse")
        try:
            blob = r.json()
        except ValueError as exc:
            raise EbayTransientError(f"回應不是 JSON: {exc}") from exc
        if not isinstance(blob, dict):
            raise EbayTransientError(f"回應不是 JSON 物件，而是 {type(blob).__name__}")
        return blob, len(r.content)

    # ------------------------------------------------------------------
    @staticmethod
    def _to_listing(item: dict) -> Listing | None:
        """一筆 API 商品 → Listing。**競標也產出**，丟不丟由呼叫端決定。

        分流全部走共用的 `read_price()`：`raw["price_kind"]` 是下游唯一的判準
        （`bidding.is_live_auction`），純競標另外帶 `end_time`／`bids`
        ——競標是時間敏感的，沒有結標時間的競標在清單上毫無用處。
        """
        if not isinstance(item, dict):
            return None
        item_id = item.get("itemId")
        if not item_id:
            return None
        price = read_price(item)
        if price is None:
            return None
        try:
            currency = Currency(price.currency)
        except ValueError:
            # 成本模型只認得 JPY/USD/TWD。硬塞一個不認得的幣別會在 fx 那層爆掉，
            # 在這裡當成「這筆解析不出來」比較誠實（會被計進 malformed）。
            return None

        shipping, _ship_reason = read_shipping(item, price.currency)

        listed_at = parse_ebay_datetime(item.get("itemCreationDate"))

        return Listing(
            site=Site.EBAY,
            external_id=str(item_id),
            title=item.get("title", ""),
            url=item.get("itemWebUrl", ""),
            price=price.value,
            currency=currency,
            image_url=(item.get("image") or {}).get("imageUrl"),
            seller_id=(item.get("seller") or {}).get("username"),
            shipping_cost=shipping,
            ships_to_tw=ships_to_tw(item),
            best_offer_enabled="BEST_OFFER" in (item.get("buyingOptions") or []),
            listed_at=listed_at or datetime.now(UTC),
            raw={**item, "price_kind": price.kind, "current_bid": price.current_bid},
            source=EbaySource.name,
            end_time=price.end_time,
            bids=price.bid_count,
        )


def _raise_for_status(resp: httpx.Response, stage: str) -> None:
    """在呼叫點強制分類失敗（工程原則 2/5）：語意失敗不重試、transient 才重試。

    401/403 是「憑證不對」——重試一萬次也一樣，要人去換金鑰，所以升成
    BLOCKED（告警看得見）。429／5xx 是對方現在忙，下一輪可能就好了。
    """
    status = resp.status_code
    if status in (401, 403):
        raise EbayAuthError(f"{stage} HTTP {status}: {resp.text[:200]}")
    if status == 429 or status >= 500:
        raise EbayTransientError(f"{stage} HTTP {status}")
    if status >= 400:
        # 400（filter 語法錯之類）不是 transient，但它也不是憑證問題——
        # 歸在 FETCH_FAILED 這一類，detail 帶原文讓人一眼看出是參數打錯。
        raise EbayTransientError(f"{stage} HTTP {status}: {resp.text[:200]}")
