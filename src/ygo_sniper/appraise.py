"""單一網址鑑價：貼一個商品網址，回一份判決報告。

**定位（決定了本模組每一個設計）：這是否決器，不是推薦器。**

估價模型（valuation.py）的證據強度是不對稱的：
- 說「不要買」時證據硬——因為根據是「同卡同稀有度同分數的實際成交價」，
  你要付的錢明顯高過每一筆可比成交，這件事不需要模型也看得出來。
- 說「值得買」時信心有限——便宜的原因可能是照片沒拍到的傷、可能是假鑑定殼、
  可能是模型沒有的樣本剛好都貴。模型看不到這些。

所以判決只有三級、且**刻意不給分數**：AVOID / CAUTION / WORTH_A_LOOK。
沒有 0-100 的評分，因為分數會讓人以為 73 分比 71 分好，而這個模型分不出來。
CAUTION 一律措辭為「無法判斷」而不是「還好」——樣本不足時裝作有意見是最糟的錯。

報告裡最重要的欄位是 `comparables`（可比成交樣本清單），不是 `estimate`。
使用者要能一眼看出那些「可比」到底可不可比：卡名對不對、稀有度對不對、
分數對不對。模型只是把那些數字排好，判斷還是人做的。

── 兩個價格語意的紅線 ──────────────────────────────────────────────
1. **Yahoo 拍賣的「現在価格」不是你付得出去的價格**（RECON §2 實證）。
   有即決価格 → 用即決価格當成交依據（`price_kind="buyout"`）。
   純競標 → 用現在価格算，但報告必須明說「實際成交價會更高」
   （`price_kind="current_bid"`），否則成本模型會系統性偏低。
2. **到手成本與行情樣本不同基準，這是刻意的**：comps 的 price_twd 是卡本身的
   成交價（`apply_markup=False`，不含代購費與國際運費），到手成本含全部費用。
   兩者相減不是「賺賠」，而是「你付的總價 vs 卡本身的市場價」。這個比較
   天生偏保守（偏向不要買），對否決器來說是正確的方向，但報告必須講出來。

4. **eBay 的競標價也不是你付得出去的價格**（2026-08-03 實測）。eBay 有三種形狀：
   FIXED_PRICE（定價）、AUCTION（純競標）、AUCTION+FIXED_PRICE（競標帶 BIN）。
   單品端點的**純競標標的 `price` 有值、而且等於 `currentBidPrice`**——直接讀
   `price` 就是把別人的出價當成售價，與紅線 1 是同一個錯（見 sources/ebay.py
   的 `read_price()`，那是唯一一份判準，掃描端與鑑價端共用）。
5. **eBay 的台幣是 eBay 幫你換算的估算值，不是在地請款**（costs._quote_ebay 有
   完整說明）：實際請款走賣家幣別，所以到手成本**要**套刷卡加成——這與 Mercari
   台灣的台幣標價（在地請款、不套）方向相反，兩者不可互相參照。

── 抓取路徑 ────────────────────────────────────────────────────────
  auctions.yahoo.co.jp/jp/auction/{id}   純 httpx（Yahoo 原生頁無 WAF）
  buyee.jp/item/yahoo/auction/{id}       **改抓 Yahoo 原生頁**，省掉開瀏覽器
  buyee.jp/mercari/item/{id}             WafSession（Playwright 解 WAF 挑戰）
  buyee.jp/paypayfleamarket/item/{id}    WafSession
  tw.mercari.com/{locale}/items/{uuid}   純 httpx（Cloudflare 後面的 SSR 頁）
  ebay.com/itm/{id}                      Browse API 單品端點（共用 EbaySource 的 OAuth）

3. **Mercari 台灣標的是新台幣，不是日圓**（2026-08-02 實測）。這是本模組唯一
   一個非日圓來源，所以 `ItemPage.price` 一定要跟 `ItemPage.currency` 一起讀。
   把 NT$5,751 當成 ¥5,751 的話到手成本會顯示 NT$1,2xx——低估 4.7 倍，
   而且偏差方向正好是「看起來超便宜」。`costs.quote_route` 因此改成用
   `listing.currency` 換算商品那一段（費用仍是 settings.yaml 的日圓費率）。

Buyee→Yahoo 的改道是 2026-08-01 實測確認過的，不是推論：
`buyee.jp/item/yahoo/auction/n1238185137` 經 Playwright 取得的頁面顯示
Buyout 2,980 YEN／Current 1,480 YEN，同一時間 httpx 直抓
`auctions.yahoo.co.jp/jp/auction/n1238185137` 的 `__NEXT_DATA__` 是
`price=1480, bidorbuy=2980`——逐円一致（與 RECON §2 的五筆比對結論相同）。
同一支 httpx 打 buyee 商品頁則是 202 + `x-amzn-waf-action: challenge`、body 0 bytes。
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .bidding import LIVE_AUCTION_KIND
from .cards import CardIndex, CardMatch
from .config import Config
from .costs import quote_all_routes
from .domain import CardInfo, Currency, Grader, Listing, RouteQuote, Site
from .parsers.card import parse_card
from .parsers.grade import GradeResolution, resolve_grade
from .sources.base import CachedFetcher, FetchError

#: 判決門檻。寫成常數是為了讓「調鬆一點」變成一次看得見的改動，
#: 而不是散落在 if 條件裡被人順手改掉。
P_WORTH_AVOID = 0.35        #: P(值得買) 低於此 → AVOID
MIN_COMPARABLES = 3         #: 可比樣本少於此 → 一律 CAUTION（無法判斷）
MAX_COMPARABLES_SHOWN = 10  #: 報告裡最多列幾筆樣本

VERDICT_AVOID = "AVOID"
VERDICT_CAUTION = "CAUTION"
VERDICT_WORTH = "WORTH_A_LOOK"

#: 這句話會出現在每一份報告裡。定位講一次不夠，它必須跟著數字一起被看到。
STANCE = (
    "這個模型當否決器用：它說「不要買」時證據較硬（同卡同稀有度同分數的直接比較），"
    "說「值得看」時信心有限。任何一筆都請自己看下面的可比成交清單再決定。"
)

_BASIS_NOTE = (
    "行情樣本是卡本身的成交價（未含代購費與國際運費），到手成本含全部費用——"
    "兩者不同基準，這個比較刻意偏保守（偏向不要買）。"
)

# ---------------------------------------------------------------------------
# URL 解析
# ---------------------------------------------------------------------------
SUPPORTED_URL_FORMS = (
    "https://auctions.yahoo.co.jp/jp/auction/{拍賣ID}",
    "https://buyee.jp/item/yahoo/auction/{拍賣ID}",
    "https://buyee.jp/mercari/item/{商品ID}",
    "https://buyee.jp/paypayfleamarket/item/{商品ID}",
    "https://tw.mercari.com/zh-hant/items/{商品UUID}",
    "https://www.ebay.com/itm/{商品號}",
)

_YAHOO_ITEM_URL = "https://auctions.yahoo.co.jp/jp/auction/{id}"
_MERCARI_TW_ITEM_URL = "https://tw.mercari.com/zh-hant/items/{id}"
_EBAY_ITEM_URL = "https://www.ebay.com/itm/{id}"
#: Site → **購買端**網址模板（成本模型與去重 key 都認這個）。
_BUY_URL_TEMPLATE = {
    Site.BUYEE_YAHOO: "https://buyee.jp/item/yahoo/auction/{id}",
    Site.BUYEE_MERCARI: "https://buyee.jp/mercari/item/{id}",
    Site.BUYEE_PAYPAY: "https://buyee.jp/paypayfleamarket/item/{id}",
    Site.MERCARI_TW: _MERCARI_TW_ITEM_URL,
    Site.EBAY: _EBAY_ITEM_URL,
}

#: (host 後綴, path regex, site)。順序即優先序。
#: id 一律寬鬆到 `[A-Za-z0-9]+`：Yahoo 有純數字 id、Mercari 有 22 字 base62
#: （RECON §1/§3），硬編形狀就是重蹈覆轍。
#: Mercari 台灣是唯一的例外：它的 id 是 UUID（含 `-`），`[A-Za-z0-9]+` 會在
#: 第一個 `-` 截斷，於是同一件商品每次都得到不同的 external_id。
_URL_PATTERNS: tuple[tuple[str, re.Pattern[str], Site], ...] = (
    ("buyee.jp", re.compile(r"^/item/yahoo/auction/([A-Za-z0-9]+)"), Site.BUYEE_YAHOO),
    ("buyee.jp", re.compile(r"^/mercari/item/([A-Za-z0-9]+)"), Site.BUYEE_MERCARI),
    ("buyee.jp", re.compile(r"^/paypayfleamarket/item/([A-Za-z0-9]+)"), Site.BUYEE_PAYPAY),
    (
        "auctions.yahoo.co.jp",
        re.compile(r"^/(?:jp/)?auction/([A-Za-z0-9]+)"),
        Site.BUYEE_YAHOO,
    ),
    # 語系段是選擇性的（/zh-hant/items/…、/ja/items/…、/items/…都實際存在）
    (
        "tw.mercari.com",
        re.compile(r"^/(?:[a-zA-Z-]{2,10}/)?items/([0-9a-fA-F-]{8,64})"),
        Site.MERCARI_TW,
    ),
    # eBay 商品頁。兩種形狀都在用：`/itm/407031244912` 與舊的
    # `/itm/{標題-slug}/407031244912`（商品號永遠是**最後**那段純數字）。
    # query string 不在 path 裡（`?_skw=…&hash=…` 由 urlparse 切走），結尾斜線允許。
    # 這一條**必須錨定結尾**：`/sch/…`（搜尋）、`/b/…`（分類）、`/usr/…`（賣家頁）
    # 本來就不會命中 `/itm/`，但錨定可以擋掉 `/itm/123/more/stuff` 這種猜測。
    ("ebay.com", re.compile(r"^/itm/(?:[^/]+/)?(\d{6,})/?$"), Site.EBAY),
)


class UnsupportedUrlError(ValueError):
    """網址不屬於任何支援的商品頁。

    刻意獨立成型別（而不是丟 ValueError）：API 層要把它對應成 400
    「你貼錯網址」，跟 502「抓取失敗」是完全不同的使用者動作。
    """

    def __init__(self, url: str) -> None:
        forms = "\n".join(f"  · {f}" for f in SUPPORTED_URL_FORMS)
        super().__init__(
            f"不支援這個網址：{url}\n目前支援這些商品頁格式：\n{forms}\n"
            "只列格式不說原因的錯誤訊息幫不上忙：本工具只認得上面這幾個站的"
            "**商品頁**（搜尋頁、賣家頁、分類頁都不算）——因為每個站的價格語意"
            "都要單獨對過帳才敢拿來算成本。想看的站不在清單上，"
            "可以先在 Buyee 或 Mercari 台灣找同一件商品再貼過來。"
        )
        self.url = url


@dataclass(slots=True, frozen=True)
class Target:
    """網址解析結果：要去哪裡抓、抓回來算哪個站的成本。"""

    site: Site
    external_id: str
    buy_url: str                 # 購買端，成本模型與去重都認這個
    fetch_url: str               # 實際要抓的頁面
    fetch_mode: str              # "yahoo_native" | "buyee_waf" | "mercari_tw"
    origin_url: str | None = None


def parse_target(url: str) -> Target:
    """網址 → 抓取計畫。不認得就拋 UnsupportedUrlError（絕不猜）。"""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UnsupportedUrlError(url)
    host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
    path = parsed.path

    for host_suffix, pattern, site in _URL_PATTERNS:
        if host != host_suffix and not host.endswith("." + host_suffix):
            continue
        m = pattern.match(path)
        if not m:
            continue
        item_id = m.group(1)
        buy_url = _BUY_URL_TEMPLATE[site].format(id=item_id)
        if site is Site.EBAY:
            # 抓的是 Browse API 單品端點（不是 HTML 商品頁）：eBay 的商品頁是
            # 重度 JS ＋ bot 防護，而 API 我們本來就有憑證（掃描端在用同一組）。
            from .sources.ebay import item_api_url

            return Target(site, item_id, buy_url, item_api_url(item_id), "ebay_api", None)
        if site is Site.BUYEE_YAHOO:
            # Buyee 的 Yahoo 商品頁與 Yahoo 原生頁同一個 ID 空間、價格逐円一致
            # （模組 docstring 有實測證據），所以一律改抓原生頁：省一次瀏覽器啟動。
            native = _YAHOO_ITEM_URL.format(id=item_id)
            return Target(site, item_id, buy_url, native, "yahoo_native", native)
        if site is Site.MERCARI_TW:
            # tw.mercari.com 是 Cloudflare 後面的 SSR 頁，httpx 直接 200
            # （2026-08-02 實測 471KB、含 og:* 與 data-testid="main-price"），
            # 不需要 WAF session、不需要瀏覽器。
            return Target(site, item_id, buy_url, buy_url, "mercari_tw", None)
        return Target(site, item_id, buy_url, buy_url, "buyee_waf", None)
    raise UnsupportedUrlError(url)


# ---------------------------------------------------------------------------
# 商品頁解析
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ItemPage:
    """商品頁解析結果。

    三個價格欄位刻意分開存：`price` 是**拿來算成本的那個**，
    `buyout_jpy` / `current_bid_jpy` 是原始語意。合成一個欄位的話，
    下游永遠分不出「¥3,440 是能付的錢」還是「¥3,440 是別人的出價」。

    `price` **不叫 price_jpy**：Mercari 台灣標的是新台幣，欄名寫死幣別而值
    不是那個幣別，是這個專案最貴的一種 bug（成本差 4-5 倍且方向偏樂觀）。
    幣別放在 `currency`，兩個欄位一起看才有意義，永遠不准分開引用。
    `buyout_jpy` / `current_bid_jpy` 保留 `_jpy` 尾巴是對的——它們只有
    Yahoo 拍賣會有，而 Yahoo 一定是日圓。
    """

    title: str
    price: float
    currency: Currency
    price_kind: str                 # "buyout" | LIVE_AUCTION_KIND | "fixed"
    price_note: str                 # 給人看的價格語意說明
    buyout_jpy: float | None = None
    current_bid_jpy: float | None = None
    image_url: str | None = None
    bids: int | None = None
    end_time: str | None = None
    status: str | None = None
    is_sold: bool = False
    #: 賣家自己寫的商品描述（純文字）。**只用來補抓鑑定分數**，不參與價格。
    #: None ＝ 這個平台的商品頁根本不帶描述（Buyee 的代購頁就是，2026-08-02 實測），
    #: 與「有描述但沒寫分數」是兩件事——前者要叫使用者去看原站，後者要叫他看照片。
    description: str | None = None
    # --- 以下是 eBay 才有的欄位（2026-08-03）。新欄位一律加在尾端且帶預設值：
    #     slots dataclass 的欄位順序就是位置參數順序，插在中間會讓既有呼叫錯位。---
    #: 平台已知的國際運費，**單位是 `currency`**（與 price 同幣別，不同幣別一律留 None）。
    #: eBay 的運費常常佔到手成本三到五成，所以它不是細節，是判決的一部分。
    shipping_cost: float | None = None
    #: 運費之所以是 None 的原因（沒有運送選項／幣別對不上／賣家不報價）。
    #: 「未知」與「零元」差很多，說不出原因的未知等於偷偷當成便宜。
    shipping_note: str | None = None
    #: 目前出價，**單位是 `currency`**（`current_bid_jpy` 是 Yahoo 專用的日圓欄位，
    #: 不可以拿來裝台幣——欄名寫死幣別而值不是那個幣別是本專案最貴的一種 bug）。
    current_bid: float | None = None
    seller: str | None = None
    #: 寄不寄台灣：True／False／None（＝API 沒說，判斷不了）。
    ships_to_tw: bool | None = None
    #: 平台自己標的品項狀態（eBay 的 `condition`，例如 "Graded"／"Ungraded"）。
    condition: str | None = None
    #: 原始幣別與金額（eBay 的 `convertedFromCurrency`/`convertedFromValue`），
    #: 例如 "GBP 14.99"。台幣是 eBay 換算的估算值，說得出原幣才看得懂那個估算。
    converted_from: str | None = None
    #: 賣家的顯示名稱（Mercari 的 `sellerName`）。**只給人看，不當鍵**：
    #: 顯示名稱使用者隨時可改，`seller` 才是穩定的 ID（工程原則 1 的同型——
    #: 拿會變的東西當鍵，同一個賣家會裂成好幾個）。
    seller_name: str | None = None


_PRICE_YEN_RE = re.compile(r"([\d,]{3,})\s*(?:YEN|円|¥)", re.I)
#: Yahoo 商品頁的 `__NEXT_DATA__` 路徑（RECON §6 同一套 Next.js 慣例）
_YAHOO_ITEM_PATH = ("props", "pageProps", "initialState", "item", "detail", "item")


def _dig(obj: Any, path: tuple[str, ...]) -> Any | None:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def parse_yahoo_item(html: str, url: str) -> ItemPage:
    """Yahoo 原生商品頁 → ItemPage。走 `__NEXT_DATA__`，不用 CSS selector。

    2026-08-01 實測欄位：`price`=現在価格、`bidorbuy`=即決価格（無即決時整個鍵不存在）、
    `bids`=出價次數、`status`、`endTime`、`img[0].image`。
    class 名是 build 雜湊，selector 撐不過一次改版；JSON 鍵撐得住。
    """
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None:
        raise FetchError("Yahoo 商品頁找不到 __NEXT_DATA__：頁面結構已改版", url=url)
    try:
        node = _dig(json.loads(tag.get_text()), _YAHOO_ITEM_PATH)
    except (ValueError, TypeError) as exc:
        raise FetchError(f"Yahoo 商品頁 __NEXT_DATA__ 解析失敗：{exc}", url=url) from exc
    if not isinstance(node, dict) or not node.get("title"):
        raise FetchError("Yahoo 商品頁 __NEXT_DATA__ 沒有商品節點：頁面結構已改版", url=url)

    current = _as_price(node.get("price"))
    buyout = _as_price(node.get("bidorbuy"))
    bids = node.get("bids")
    images = node.get("img") or []
    image = images[0].get("image") if images and isinstance(images[0], dict) else None

    if buyout:
        price, kind = buyout, "buyout"
        note = f"即決価格 ¥{buyout:,.0f}（點下去就能成交，成本以此計算）。"
        if current and current != buyout:
            # 兩個價格相等時不提醒——那是定額出品，沒有「別人的出價」這件事，
            # 硬提醒只會讓真正該警戒的那句話變成雜訊
            note += f"現在価格 ¥{current:,.0f} 只是目前出價，不是你付得出去的價格。"
    elif current:
        price, kind = current, LIVE_AUCTION_KIND
        note = (
            f"⚠️ 這是競標中標的，沒有即決価格。下面的成本估算以目前出價 "
            f"¥{current:,.0f} 計算，實際成交價會更高（{bids or 0} 次出價）。"
        )
    else:
        raise FetchError("Yahoo 商品頁抓不到任何價格", url=url)

    return ItemPage(
        title=str(node["title"]),
        price=price,
        currency=Currency.JPY,
        price_kind=kind,
        price_note=note,
        buyout_jpy=buyout,
        current_bid_jpy=current,
        image_url=image,
        bids=int(bids) if isinstance(bids, (int, float)) else None,
        end_time=node.get("endTime"),
        status=node.get("status"),
        is_sold=str(node.get("status") or "").lower() in ("closed", "sold"),
        description=_yahoo_description(node),
    )


def _yahoo_description(node: dict[str, Any]) -> str | None:
    """Yahoo 商品頁的賣家描述（純文字）。

    `descriptionHtml` 是含 `<BR>` 的 HTML 字串、`description` 是已經切好的行陣列，
    兩個都實測存在（2026-08-02，3/3 筆）。優先用 HTML 那份並自己拆行——
    行陣列偶爾把一句話拆在兩個元素裡，接起來反而會黏成一行。
    """
    html = node.get("descriptionHtml")
    if isinstance(html, str) and html.strip():
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", html, flags=re.I)
        return BeautifulSoup(text, "html.parser").get_text("\n").strip() or None
    lines = node.get("description")
    if isinstance(lines, list):
        joined = "\n".join(str(x) for x in lines).strip()
        return joined or None
    if isinstance(lines, str) and lines.strip():
        return lines.strip()
    return None


#: Buyee 商品頁的標題／價格容器。兩站 DOM 不同構（搜尋頁才同構），
#: 所以兩組 selector 都列，依序試。2026-08-01 實測：
#:   Mercari `h1.m-goodsName` + `div.m-goodsDetail__price`（"8,299 YEN (NT$1,782)"）
#:   PayPay  `h1.flmIdp__itemName` + `div.flmIdp__itemPrice`（"17,000 YEN (NT$3,650)"）
_BUYEE_TITLE_SELECTORS = ("h1.m-goodsName", "h1.flmIdp__itemName", "h1")
_BUYEE_PRICE_SELECTORS = (".m-goodsDetail__price", ".flmIdp__itemPrice")

#: Buyee 的 Mercari 商品頁上，這件商品自己的賣家連結：
#: `href="/mercari/search?seller=901019808"`（2026-08-04 實測）。
#:
#: **這是 Mercari 賣家 ID 的第二條路，而且與 Mercari 台灣同一個 ID 空間**
#: ——2026-08-04 逐筆對帳：同一件商品 `m38347072251`，
#:   Buyee 鏡像頁 `/mercari/search?seller=901019808`
#:   Mercari 台灣（UUID `2a2f632e-1b44-4864-b5cd-e684f2db8db1`，
#:   其圖片檔名正是 `m38347072251_1.jpg`）`"sellerId":"901019808"`
#: 兩邊逐位相同。所以 `buyee_mercari` 與 `mercari_tw` 的 `seller_id` **可以直接
#: 互相對照**（`seller_key` 仍分兩個站，因為標價幣別與價格水準不同，
#: 同儕比對不可跨站——見 `seller_alpha` 的基準對齊規則）。
#:
#: ⚠️ 頁尾的推薦區塊有一堆**別人的** `partnerSellerId`（實測 13 個不同的值），
#: 所以判準只認 `href` 形式的賣家連結，不認 JSON 裡的 partnerSellerId：
#: 後者抓第一個命中就會把推薦商品的賣家掛到這件商品上。
#: PayPay 的 Buyee 鏡像頁**沒有**這種連結（實測 0 個命中）——那條路的賣家
#: 由 `paypay_direct` 直接給，這裡抽不到就是 None，不猜。
_BUYEE_MERCARI_SELLER_RE = re.compile(r'href="/mercari/search\?seller=(\d+)"')


def parse_buyee_item(html: str, url: str) -> ItemPage:
    """Buyee Mercari／PayPay 商品頁 → ItemPage。這兩站都是定價（無競標）。

    **`description` 一律留 None，這是實測結論不是偷懶**（2026-08-02，兩個平台
    各抓一頁）：Buyee 的代購頁只轉載標題、價格與圖片，賣家描述整段不在頁面上——
    Mercari 那頁自己寫著「表示されていない画像や情報がある可能性がありますので、
    詳細は元のサイトでご確認ください」。頁面上唯一像描述的 `meta[name=description]`
    是 Buyee 自己的行銷文案，`[class*=escription]` 命中的也是。
    更糟的是頁尾的 `recommendItem__productTitle` 會列出**別的商品**的標題
    （實測含「PSA10 …」「PSA8 …」），整頁掃 regex 會抓到別張卡的分數。
    所以這兩站的分數補抓路徑是「沒有」，報告要據實叫使用者去看照片或原站。
    """
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    for sel in _BUYEE_TITLE_SELECTORS:
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            title = node.get_text(" ", strip=True)
            break
    if not title:
        og = soup.select_one("meta[property='og:title']")
        # og:title 帶 Buyee 的行銷尾巴，切掉才是商品名
        title = (og.get("content") or "").split(" | ")[0].strip() if og else ""
    if not title:
        raise FetchError("Buyee 商品頁抓不到標題：頁面結構已改版或被擋", url=url)

    price: float | None = None
    for sel in _BUYEE_PRICE_SELECTORS:
        node = soup.select_one(sel)
        if node is None:
            continue
        # 日圓價一定排在換算的 NT$ 之前，取第一個帶 YEN／円 的數字即可
        m = _PRICE_YEN_RE.search(node.get_text(" ", strip=True))
        if m:
            price = float(m.group(1).replace(",", ""))
            break
    if not price:
        raise FetchError("Buyee 商品頁抓不到價格：頁面結構已改版或被擋", url=url)

    og_img = soup.select_one("meta[property='og:image']")
    sold = any(
        "soldout" in c.lower()
        for node in soup.find_all(class_=True)
        for c in (node.get("class") or [])
    )
    #: 賣家連結**只有 Mercari 鏡像頁有**（見 `_BUYEE_MERCARI_SELLER_RE`）。
    #: 多個不同值代表頁面結構變了（推薦區也開始用這個 href 形式），
    #: 那時候正確答案是「抽不到」——挑一個等於擲骰子決定賣家是誰。
    ids = set(_BUYEE_MERCARI_SELLER_RE.findall(html))
    seller = ids.pop() if len(ids) == 1 else None
    return ItemPage(
        title=title,
        price=price,
        currency=Currency.JPY,
        price_kind="fixed",
        price_note=f"定價 ¥{price:,.0f}（點下去就能成交，成本以此計算）。",
        image_url=og_img.get("content") if og_img else None,
        is_sold=sold,
        seller=seller,
    )


#: Mercari 台灣商品頁（tw.mercari.com）。2026-08-02 實測：Cloudflare 後面的
#: Next.js App Router SSR，httpx 直抓 200／471KB，**不需要瀏覽器也沒有 WAF**。
#: 標題有兩個可靠來源：主圖容器的 `name` 屬性（原文，無尾巴）與 `og:title`
#: （帶「 ‐ Mercari Japan …」行銷尾巴，用 U+2010 連字號分隔）。
#: 價格在 `[data-testid="main-price"]`，文字是 `NT$ 5,751`——**新台幣**。
_TW_TITLE_SELECTOR = "[data-testid=item-carousel-main-image]"
_TW_PRICE_SELECTOR = "[data-testid=main-price]"
_TW_TITLE_SUFFIX_RE = re.compile(r"\s*[‐\-–—|]\s*Mercari.*$")
#: 幣別**從頁面上讀，不假設**。同一個 selector 哪天改成顯示日圓（Mercari 台灣
#: 站有語系／幣別切換），寫死 TWD 會讓成本差 4-5 倍且完全沒有錯誤訊息。
_TW_PRICE_RE = re.compile(r"(NT\$|TWD|¥|￥|JPY|円)\s*([\d,]+(?:\.\d+)?)", re.I)
_TW_CURRENCY = {
    "nt$": Currency.TWD, "twd": Currency.TWD,
    "¥": Currency.JPY, "￥": Currency.JPY, "jpy": Currency.JPY, "円": Currency.JPY,
}
#: 頁面自己列出的額外費用（`+NT$122 運費`）。只當交叉核對用的註記，
#: **不參與成本計算**——成本一律走 settings.yaml 的 route 模型，
#: 兩個來源各算一半的話，沒有人說得出最後那個數字是怎麼來的。
_TW_EXTRA_FEE_RE = re.compile(r"\+\s*NT\$\s*([\d,]+)\s*(運費|服務費)")

#: 賣家（Seller Alpha）。2026-08-04 實測：Mercari 台灣的 Next.js flight payload
#: 裡有一個 `view-detail-link` 元件，內容是
#: `{"itemType":1,"sellerId":"901019808","sellerName":"りり"}`（HTML 原文裡引號
#: 是跳脫的 `\"`，所以每個引號都要寫成 `\\?"`）。
#:
#: **必須錨定在 `itemType` 上**，不可以只抓 `sellerId`：頁面下方還有「這個賣家
#: 的其他商品」「你可能也喜歡」等區塊，哪天它們也帶 sellerId，單抓第一個命中
#: 就會把別人的 ID 掛到這件商品上——而那種錯是靜默的（照樣有個看起來合理的
#: 賣家）。實測目前整頁只有這一個 sellerId（live 與 fixture 皆然），所以錨定
#: 不會少收；真的哪天錨點消失，正確答案是「抽不到」而不是「猜一個」。
_TW_SELLER_RE = re.compile(
    r'\\?"itemType\\?"\s*:\s*\d+\s*,\s*'
    r'\\?"sellerId\\?"\s*:\s*\\?"(\d+)\\?"'
    r'(?:\s*,\s*\\?"sellerName\\?"\s*:\s*\\?"(.*?)\\?"[,}\]])?'
)


def parse_mercari_tw_item(html: str, url: str) -> ItemPage:
    """Mercari 台灣商品頁 → ItemPage。**價格是新台幣，不是日圓。**

    這是本模組唯一一個非日圓的來源，所以 `currency` 是從頁面讀出來的、
    不是寫死的：讀不到幣別符號就直接失敗，絕不預設一個幣別繼續往下算
    （猜錯的方向是「看起來便宜 4.7 倍」，正是會讓人按下去買的方向）。
    """
    soup = BeautifulSoup(html, "html.parser")

    node = soup.select_one(_TW_TITLE_SELECTOR)
    title = (node.get("name") or "").strip() if node else ""
    if not title:
        og = soup.select_one("meta[property='og:title']")
        title = _TW_TITLE_SUFFIX_RE.sub("", (og.get("content") or "").strip()) if og else ""
    if not title:
        raise FetchError(
            "Mercari 台灣商品頁抓不到標題：頁面結構已改版或該商品已下架", url=url
        )

    price_node = soup.select_one(_TW_PRICE_SELECTOR)
    m = _TW_PRICE_RE.search(price_node.get_text(" ", strip=True)) if price_node else None
    if not m:
        raise FetchError(
            "Mercari 台灣商品頁抓不到價格（找不到 data-testid=main-price 或幣別符號）："
            "頁面結構已改版或該商品已下架",
            url=url,
        )
    currency = _TW_CURRENCY[m.group(1).lower()]
    price = float(m.group(2).replace(",", ""))

    og_img = soup.select_one("meta[property='og:image']")
    symbol = "NT$" if currency is Currency.TWD else "¥"
    note = (
        f"定價 {symbol}{price:,.0f}（{currency.value}，點下去就能成交，成本以此價計算）。"
        "⚠️ Mercari 台灣標的是**新台幣含站內換匯後的價格**，"
        "本工具不會再對它套一次匯率。"
        if currency is Currency.TWD
        else f"定價 {symbol}{price:,.0f}（{currency.value}，成本以此價計算）。"
    )
    # 同一組費用在 SSR payload 裡會出現兩次（桌面版與手機版各一份），去重保序
    fees = list(dict.fromkeys(_TW_EXTRA_FEE_RE.findall(html)))
    if fees:
        listed = "、".join(f"{kind} NT${amount}" for amount, kind in fees)
        note += (
            f"｜頁面另列：{listed}（僅供對帳；下面的到手成本走的是 settings.yaml "
            "的 route 費率，兩者不混用）。"
        )
    seller_id, seller_name = parse_mercari_seller(html)
    return ItemPage(
        title=title,
        price=price,
        currency=currency,
        price_kind="fixed",
        price_note=note,
        image_url=og_img.get("content") if og_img else None,
        # 售出狀態刻意不猜：頁面上的「已售出／售完」全部來自 i18n 字典
        # （每一頁都有，跟這件商品的狀態無關），沒有可靠的結構性標記。
        # 猜錯會直接翻轉判決，寧可在 warnings 裡明說「本工具判斷不了」。
        is_sold=False,
        status=None,
        seller=seller_id,
        seller_name=seller_name,
    )


def parse_mercari_seller(html: str) -> tuple[str | None, str | None]:
    """Mercari 台灣商品頁 → `(sellerId, sellerName)`。抽不到一律 `(None, None)`。

    抽出來成獨立函式，是為了讓「Mercari 賣家 ID 長什麼樣」只有一份定義：
    `parse_buyee_item` 從 Buyee 鏡像頁抽的是**同一個 ID 空間**（見
    `_BUYEE_MERCARI_SELLER_RE` 的實測證據），兩邊各寫一份 regex 遲早漂掉。
    """
    m = _TW_SELLER_RE.search(html)
    if m is None:
        return None, None
    name = (m.group(2) or "").strip() or None
    return m.group(1), name


#: 幣別 → 顯示符號。**只有這一份**：報告裡的每一個金額都要帶得出幣別，
#: 而「NT$ 還是 US$」寫錯一次就是差 30 倍（見 Mercari 台灣那條紅線）。
_CURRENCY_SYMBOL = {Currency.TWD: "NT$", Currency.USD: "US$", Currency.JPY: "¥"}


def _converted_from(node: Any) -> str | None:
    """eBay 的金額物件 → "GBP 14.99"（原幣別與原價）。沒有換算資訊就 None。"""
    if not isinstance(node, dict):
        return None
    cur = node.get("convertedFromCurrency")
    val = node.get("convertedFromValue")
    if not cur or val is None:
        return None
    try:
        return f"{cur} {float(val):,.2f}"
    except (TypeError, ValueError):
        return None


def parse_ebay_item(blob: dict[str, Any], url: str) -> ItemPage:
    """eBay Browse API 單品回應 → ItemPage。**價格語意走共用的 `read_price()`。**

    為什麼不在這裡自己讀 `price`：單品端點的純競標標的 `price` 有值、而且等於
    `currentBidPrice`（2026-08-03 實測兩筆）。自己讀就會把「別人的出價」當成
    「你付得出去的價格」，而那個方向的偏差是系統性低估——與 Yahoo 現在価格
    那條紅線一模一樣，只是這次沒有欄位名可以提醒你。判準只有
    `sources/ebay.py:read_price()` 一份，掃描端與這裡共用。

    幣別**從回應讀，不預設**：帶 `contextualLocation=country=TW` 時 eBay 回的是
    台幣（`convertedFromCurrency` 記著原幣），但那是 eBay 的估算值——實際請款走
    賣家幣別，所以成本模型會對它套刷卡加成（見 `costs._quote_ebay`）。
    """
    from .sources.ebay import read_price, read_shipping, ships_to_tw

    if not isinstance(blob, dict):
        raise FetchError("eBay 單品端點回的不是 JSON 物件", url=url)
    title = str(blob.get("title") or "").strip()
    if not title:
        raise FetchError(
            "eBay 單品端點沒有 title：商品號不對，或回應格式已改版", url=url
        )

    price = read_price(blob)
    if price is None:
        raise FetchError(
            "eBay 這筆抓不到任何可用價格（price 與 currentBidPrice 都沒有值）："
            "商品可能已結標，或回應格式已改版",
            url=url,
        )
    try:
        currency = Currency(price.currency)
    except ValueError as exc:
        # 猜一個幣別繼續算，就是把 US$ 當 NT$（差 30 倍）那類 bug 的來源。
        raise FetchError(
            f"eBay 回的幣別是 {price.currency}，成本模型只認得 JPY／USD／TWD——"
            "拒絕用猜的幣別計算成本",
            url=url,
        ) from exc

    shipping, shipping_note = read_shipping(blob, price.currency)
    symbol = _CURRENCY_SYMBOL[currency]
    origin = _converted_from(
        blob.get("currentBidPrice") if price.kind == LIVE_AUCTION_KIND else blob.get("price")
    )
    origin_txt = f"（原幣 {origin}，台幣是 eBay 的換算估算值）" if origin else ""

    if price.kind == LIVE_AUCTION_KIND:
        bids_txt = f"{price.bid_count} 次出價" if price.bid_count is not None else "出價次數未知"
        end_txt = (
            f"，結標 {price.end_time:%Y-%m-%d %H:%M} UTC" if price.end_time else ""
        )
        note = (
            f"⚠️ 這是 eBay **競標中**標的（沒有 Buy It Now）。下面的成本估算以"
            f"**目前出價** {symbol}{price.value:,.0f} 計算，**這不是你付得出去的價格**，"
            f"最終成交價會更高（{bids_txt}{end_txt}）{origin_txt}。"
        )
    elif price.is_auction:
        note = (
            f"Buy It Now {symbol}{price.value:,.0f}（{currency.value}，點下去就能成交，"
            f"成本以此價計算）{origin_txt}。"
        )
        if price.current_bid is not None:
            note += (
                f"⚠️ 這筆**同時在競標**，目前出價 {symbol}{price.current_bid:,.0f}"
                "——那個價會漲，而且不是你付得出去的價格。"
            )
    else:
        note = (
            f"定價 {symbol}{price.value:,.0f}（{currency.value}，點下去就能成交，"
            f"成本以此價計算）{origin_txt}。"
        )

    return ItemPage(
        title=title,
        price=price.value,
        currency=currency,
        price_kind=price.kind,
        price_note=note,
        image_url=(blob.get("image") or {}).get("imageUrl"),
        bids=price.bid_count,
        end_time=price.end_time.isoformat() if price.end_time else None,
        status=blob.get("condition"),
        # 結標／售出狀態不猜：eBay 對已結束的商品多半直接回 404，能解析到這裡
        # 通常代表還在架上。真的結標了，價格語意那句話仍然成立。
        is_sold=False,
        # eBay 的 `description` 是整段 HTML 賣家模板（常夾別的商品與 SEO 關鍵字堆），
        # 拿去補抓鑑定分數會抓到別張卡的分數——與 Buyee 代購頁同一個理由，一律 None。
        description=None,
        shipping_cost=shipping,
        shipping_note=shipping_note,
        current_bid=price.current_bid,
        seller=(blob.get("seller") or {}).get("username"),
        ships_to_tw=ships_to_tw(blob),
        condition=blob.get("condition"),
        converted_from=origin,
    )


def fetch_item_page(
    cfg: Config,
    target: Target,
    *,
    fetcher: Any = None,
    waf: Any = None,
    ebay: Any = None,
) -> ItemPage:
    """把 `Target` 抓成 `ItemPage`。抓取路徑的選擇只有這一份。

    抽出來是因為現在有第二個呼叫端（`cli.resolve_grades` 要批次補抓分數）。
    兩邊各寫一份的話，哪天 Buyee 的 Yahoo 商品頁不能再改道原生頁，
    只會有一邊被修好——而另一邊的失敗是靜默的（拿到別的頁面照樣解析成功）。

    **本函式只關掉自己建立的資源**：傳進來的 fetcher／waf 由呼叫端負責，
    批次補抓才能共用同一顆 WAF token（一顆 token TTL 只有約 5 分鐘）。

    `ebay` 可注入一個 `EbaySource`（測試與批次補抓共用同一顆 OAuth token）。
    """
    owns_fetcher = fetcher is None
    owns_waf = waf is None
    the_fetcher = fetcher
    the_waf = waf
    try:
        if target.fetch_mode == "ebay_api":
            return _fetch_ebay_item_page(cfg, target, ebay)
        if target.fetch_mode == "yahoo_native":
            if the_fetcher is None:
                the_fetcher = CachedFetcher(cfg)
            return parse_yahoo_item(the_fetcher.get(target.fetch_url), target.fetch_url)
        if target.fetch_mode == "mercari_tw":
            if the_fetcher is None:
                the_fetcher = CachedFetcher(cfg)
            return parse_mercari_tw_item(
                the_fetcher.get(target.fetch_url), target.fetch_url
            )
        if the_waf is None:
            from .sources.waf import WafSession

            the_waf = WafSession(cfg)
        return parse_buyee_item(the_waf.get(target.fetch_url), target.fetch_url)
    finally:
        if owns_fetcher and the_fetcher is not None:
            the_fetcher.close()
        if owns_waf and the_waf is not None:
            the_waf.close()


def _fetch_ebay_item_page(cfg: Config, target: Target, ebay: Any) -> ItemPage:
    """eBay 單品端點 → ItemPage。失敗一律轉成 `FetchError` 並**保留分類**。

    憑證沒設定／被拒是語意失敗（`transient=False`，重試沒有意義，要人去補設定），
    連線與 5xx 是 transient。API 層照這個旗標決定 502 還是 503——把兩者混成
    同一種錯，使用者就分不出「等一下再試」與「你少設了一組金鑰」。
    """
    from .sources.ebay import (
        EbayAuthError,
        EbayItemNotFound,
        EbaySource,
        EbayTransientError,
    )

    source = ebay if ebay is not None else EbaySource(cfg)
    try:
        blob = source.get_item(target.external_id)
    except EbayAuthError as exc:
        raise FetchError(
            f"eBay API 用不了：{exc}（鑑價 eBay 網址需要 .env 裡的 "
            "EBAY_CLIENT_ID／EBAY_CLIENT_SECRET，與掃描用的是同一組）",
            url=target.fetch_url,
            transient=False,
        ) from exc
    except EbayItemNotFound as exc:
        raise FetchError(str(exc), url=target.fetch_url, status=404, transient=False) from exc
    except EbayTransientError as exc:
        raise FetchError(
            f"eBay 單品端點抓取失敗：{exc}", url=target.fetch_url, transient=True
        ) from exc
    return parse_ebay_item(blob, target.fetch_url)


def apply_grade_resolution(card: CardInfo, item: ItemPage) -> GradeResolution:
    """用商品描述補抓分數，就地寫回 `CardInfo`，並回傳完整的判定來歷。

    三種結果都會改到 `card`，而且**都是往安全的方向**：

      - 描述補到分數 → `card.grade` 從 None 變成數字、`grade_source="description"`。
        這一類標的原本被 `bidding.EvidenceGate.require_known_grade` 全部擋掉，
        補到之後才可能拿得到出價上限。
      - 描述與標題矛盾 → `card.grade` 被**抹回 None**（即使標題原本有分數）。
        兩個來源打架時我們分不出誰對，兩個都不採信。
      - 什麼都沒抓到 → 維持 None，`note` 告訴使用者去看照片上的鑑定殼。

    ⚠️ 這是唯一一個會**降級**已知分數的地方，所以它必須在估價之前跑：
    估價、判決、出價上限全部吃 `card.grade`，晚一步就會有一半的下游用舊值。
    """
    resolution = resolve_grade(item.title, item.description)
    card.grade = resolution.grade
    card.grade_source = resolution.source
    if resolution.grader is not Grader.UNKNOWN:
        card.grader = resolution.grader
    return resolution


def _as_price(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# ---------------------------------------------------------------------------
# 可比成交樣本
# ---------------------------------------------------------------------------
#: 可比程度分級。數字小 = 更可比。這是報告裡最重要的一欄：
#: 使用者要看得出這筆「可比」到底比的是同一張卡，還是只是同一個稀有度。
TIER_LABELS = {
    1: "同卡 × 同稀有度 × 同分數",
    2: "同卡 × 同稀有度（跨分數）",
    3: "同稀有度 × 同分數（跨卡）",
    4: "同稀有度（跨卡跨分數）",
}


@dataclass(slots=True)
class Comparable:
    """一筆可比成交。價格是卡本身的成交價，不含代購費與運費。

    `price_twd` **一律是原始成交價，不做平台換算**。理由：這一欄的用途是讓人
    去點連結核對，換算過的數字在真實世界不存在，對不上就會讓整份報告失信。
    平台差異改成外顯——`site_label` 標出它成交在哪個平台、`same_venue` 標出
    它跟你要買的那個平台是不是同一個。要看「換算到同一平台之後多少錢」，
    那是模型公允價的工作（estimate.venue_adjusted）。

    `sold_at` 同一套辦法：**它不保證是成交時間**（Buyee 系的已售出頁不給日期，
    那批列存的是我們的入庫時間——2026-08-06 實測 Mercari 1046/1046 筆），
    所以 `sold_at_is_ingest` 跟著一起送出去，畫面上要標出來。把入庫日當成交日
    印給使用者看，是把一個假的事實講得跟真的一樣（CLAUDE.md 第五節）。
    """

    price_twd: float
    rarity: str | None
    grade: float | None
    title: str
    url: str | None
    sold_at: str | None
    card_name: str | None
    tier: int
    tier_label: str
    site: str | None = None
    site_label: str = ""
    same_venue: bool | None = None
    #: True ＝ 上面那個 `sold_at` 是**我們入庫的時間**，不是成交時間。
    #: 畫面必須把它標出來（`web/static/index.html` 的「成交日」欄）。
    sold_at_is_ingest: bool = False


def _tier_of(
    *,
    target_card: str | None,
    target_rarity: str | None,
    target_grade: float | None,
    row_card: str | None,
    row_rarity: str | None,
    row_grade: float | None,
) -> int | None:
    """回傳可比等級；完全不可比回 None。

    稀有度／分數一律用**完全相等**比對（含 None == None），與 valuation.py 的
    分層判準同一套規則——兩邊用不同的「算不算同一格」定義，報告裡列出來的樣本
    就不是模型實際用的那些，那比沒有樣本更誤導。
    """
    if row_rarity != target_rarity:
        return None
    same_card = bool(target_card) and row_card == target_card
    same_grade = row_grade == target_grade
    if same_card:
        return 1 if same_grade else 2
    return 3 if same_grade else 4


def collect_comparables(
    rows: list[dict[str, Any]],
    index: CardIndex | None,
    *,
    card_name: str | None,
    rarity: str | None,
    grade: float | None,
    venue: str | None = None,
    limit: int = MAX_COMPARABLES_SHOWN,
) -> tuple[list[Comparable], dict[str, Any]]:
    """從成交列挑出可比樣本，回傳 (要顯示的清單, 最可比那一層的統計)。

    統計只取**最可比那一層的全部樣本**（不是顯示清單的前 N 筆）：判決引用的
    數字必須是整層的範圍，不能是被截斷後的範圍，否則「NT$X–Y」會隨顯示筆數變動。
    卡名在這裡才決定（重跑 index.match(title)），與 valuation.obs_from_comps 同源。

    `venue` 是**你要買的那個平台**。平台不參與可比分級（tier）——三個平台的
    樣本本來就都是同一張卡的真實成交，全丟掉太浪費——但每一筆都會標出自己
    成交在哪個平台，統計裡另外附上同平台那一小撮的中位數。這是刻意的分工：
    tier 回答「像不像同一張卡」，平台回答「這個價位是哪個市場的」，
    兩件事混成一個分數就看不出哪個維度不可比。
    """
    from .valuation import venue_label

    buckets: dict[int, list[Comparable]] = {}
    for r in rows:
        price = r.get("price_twd")
        if not price:
            continue
        title = r.get("title") or ""
        row_card = None
        if index is not None and index.available:
            m = index.match(title)
            row_card = m.name_ja if (m and m.in_era) else None
        else:
            row_card = r.get("card_name") or None
        row_grade = float(r["grade"]) if r.get("grade") is not None else None
        tier = _tier_of(
            target_card=card_name,
            target_rarity=rarity,
            target_grade=grade,
            row_card=row_card,
            row_rarity=r.get("rarity"),
            row_grade=row_grade,
        )
        if tier is None:
            continue
        row_site = r.get("site") or None
        buckets.setdefault(tier, []).append(
            Comparable(
                price_twd=float(price),
                rarity=r.get("rarity"),
                grade=row_grade,
                title=title,
                url=r.get("url"),
                sold_at=r.get("sold_at"),
                card_name=row_card,
                tier=tier,
                tier_label=TIER_LABELS[tier],
                site=row_site,
                site_label=venue_label(row_site),
                same_venue=(row_site == venue) if venue else None,
                sold_at_is_ingest=bool(r.get("sold_at_is_ingest")),
            )
        )

    shown: list[Comparable] = []
    for tier in sorted(buckets):
        # ⚠️ 這個排序**不是**「最近成交的排前面」：Buyee 系的 `sold_at` 是入庫
        # 時間（2026-08-06 實測 Mercari 100%），所以那些列排的是入庫先後。
        # 這裡刻意不改排序也不排除它們——可比程度（tier）才是這張表的主軸，
        # 時間只是同一層內的次要順序，而扔掉 Mercari 等於扔掉最大的一批可比成交。
        # 承重的防線是**每一列都帶著 `sold_at_is_ingest` 送到畫面上標記**。
        group = sorted(buckets[tier], key=lambda c: (c.sold_at or ""), reverse=True)
        shown.extend(group[: max(0, limit - len(shown))])
        if len(shown) >= limit:
            break

    stats: dict[str, Any] = {
        "n": 0, "tier": None, "tier_label": None,
        "target_venue": venue, "target_venue_label": venue_label(venue) if venue else None,
    }
    if buckets:
        best = min(buckets)
        group = buckets[best]
        prices = sorted(c.price_twd for c in group)
        mix: dict[str, int] = {}
        for c in group:
            mix[c.site or "unknown"] = mix.get(c.site or "unknown", 0) + 1
        same = sorted(c.price_twd for c in group if c.same_venue)
        stats |= {
            "n": len(prices),
            "tier": best,
            "tier_label": TIER_LABELS[best],
            "min_twd": round(prices[0]),
            "max_twd": round(prices[-1]),
            "median_twd": round(statistics.median(prices)),
            "n_all_tiers": sum(len(v) for v in buckets.values()),
            # 平台組成：同一層的中位數是「哪個市場的中位數」要看得見
            "venue_mix": mix,
            "same_venue_n": len(same),
            "same_venue_median_twd": round(statistics.median(same)) if same else None,
        }
    return shown, stats


# ---------------------------------------------------------------------------
# 判決
# ---------------------------------------------------------------------------
def decide_verdict(
    *,
    landed_twd: float,
    estimate: Any,
    comparables: list[Comparable],
    comp_stats: dict[str, Any],
    card: CardInfo,
    card_match: CardMatch | None,
) -> tuple[str, list[str], dict[str, Any]]:
    """三級判決 ＋ 逐條理由（每條都帶數字）。

    順序是刻意的：**先問「資料夠不夠」，再問「貴不貴」**。
    先算貴不貴、資料不足時再降級，會讓一個只有 1 筆樣本的「便宜」
    偷偷保留 WORTH_A_LOOK 的語氣。資料不足就是無法判斷，沒有折衷。
    """
    fair = estimate.fair_twd
    lo, hi = estimate.lo_twd, estimate.hi_twd
    p_worth = estimate.p_worth_buying
    numbers = {
        "landed_twd": round(landed_twd),
        "fair_twd": round(fair) if fair else None,
        "lo_twd": round(lo) if lo else None,
        "hi_twd": round(hi) if hi else None,
        "p_worth_buying": p_worth,
        "level": estimate.level,
        "level_label": estimate.level_label,
        "n_effective": estimate.n_effective,
        "calibration_n": estimate.calibration_n,
        "n_comparables": comp_stats.get("n", 0),
        "comparable_tier": comp_stats.get("tier_label"),
        "comparable_min_twd": comp_stats.get("min_twd"),
        "comparable_max_twd": comp_stats.get("max_twd"),
        "comparable_median_twd": comp_stats.get("median_twd"),
        "p_worth_avoid_threshold": P_WORTH_AVOID,
        "min_comparables": MIN_COMPARABLES,
        # 公允價是哪個平台的水準——判決數字被引用時必須跟著這個標籤走
        "venue": getattr(estimate, "venue", None),
        "venue_adjusted": getattr(estimate, "venue_adjusted", False),
        "venue_is_estimated": getattr(estimate, "venue_is_estimated", None),
        "comparable_venue_mix": comp_stats.get("venue_mix"),
        "comparable_same_venue_n": comp_stats.get("same_venue_n"),
        "comparable_same_venue_median_twd": comp_stats.get("same_venue_median_twd"),
    }

    # --- 第一關：資料夠不夠下判斷 -------------------------------------
    gaps: list[str] = []
    if fair is None:
        gaps.append("行情庫沒有可用樣本，模型給不出公允價")
    if card_match is None:
        gaps.append("標題比對不到 1998-2004 卡片主檔的任何卡名（分桶只能退到稀有度層）")
    elif not card_match.in_era:
        gaps.append(
            f"比對到的卡是「{card_match.name_ja}」，OCG 首發 "
            f"{card_match.ocg_date or '未知'}，不是 1998-2004 目標年代"
        )
    if not card.in_era:
        gaps.append("標題沒有 1998-2004 的年代證據（期別／舊卡號／年份都沒抓到）")
    if card.grade is None:
        # 模型對 grade=None 的處理是「當成基準分數 9，不猜」（valuation.ValuationModel.g）。
        # 那是估價時的合理降級，但**判決不能建立在一個沒說出口的假設上**：
        # 真的是 PSA5 的話公允價會被高估好幾倍，而 WORTH_A_LOOK 正是被高估的那個方向。
        gaps.append(
            "標題抽不到鑑定分數，模型只能當成基準分數 9 處理——真實分數更低的話公允價會被高估"
        )
    n_comp = comp_stats.get("n", 0)
    if n_comp < MIN_COMPARABLES:
        gaps.append(
            f"最可比那一層只有 {n_comp} 筆成交（門檻 {MIN_COMPARABLES} 筆），樣本太少"
        )
    if not estimate.has_interval:
        gaps.append(
            f"校準集 {estimate.calibration_n} 筆不足以校準，模型不給 80% 區間"
        )

    if gaps:
        reasons = [f"**無法判斷**（不是「還好」，是資料不足以下結論）：{'；'.join(gaps)}。"]
        reasons.append(_landed_line(landed_twd, comp_stats))
        if fair is not None:
            reasons.append(
                f"模型的點估計是 NT${fair:,.0f}（{estimate.level_label}，有效樣本 "
                f"{estimate.n_effective} 筆）——**這個數字在上述缺口下不足以支撐買或不買**。"
                + _venue_line(estimate)
            )
        return VERDICT_CAUTION, reasons, numbers

    # --- 第二關：貴不貴（此時才有資格說話）----------------------------
    reasons: list[str] = []
    if landed_twd > hi:
        over = landed_twd / hi - 1
        reasons.append(
            f"**不要買**：到手成本 NT${landed_twd:,.0f} 高於 80% 區間上緣 "
            f"NT${hi:,.0f}（高出 {over:.0%}）。"
        )
        reasons.append(_landed_line(landed_twd, comp_stats))
        reasons.append(_model_line(estimate))
        reasons.append(_BASIS_NOTE)
        return VERDICT_AVOID, reasons, numbers

    if p_worth is not None and p_worth < P_WORTH_AVOID:
        reasons.append(
            f"**不要買**：P(公允價 > 到手成本) 只有 {p_worth:.0%}，低於 "
            f"{P_WORTH_AVOID:.0%} 門檻——同一批殘差分布下，你這個價格買貴的機率是 "
            f"{1 - p_worth:.0%}。"
        )
        reasons.append(_landed_line(landed_twd, comp_stats))
        reasons.append(_model_line(estimate))
        reasons.append(_BASIS_NOTE)
        return VERDICT_AVOID, reasons, numbers

    if landed_twd < lo:
        reasons.append(
            f"**值得看一眼**：到手成本 NT${landed_twd:,.0f} 低於 80% 區間下緣 "
            f"NT${lo:,.0f}。"
        )
        reasons.append(_landed_line(landed_twd, comp_stats))
        reasons.append(_model_line(estimate))
        reasons.append(
            "⚠️ **這是模型判斷，信心有限**：便宜的原因模型看不到——照片沒拍到的傷、"
            "假鑑定殼、賣家評價、標題寫錯稀有度都不在資料裡。"
            "請自己看完下面的可比成交清單與商品照片再決定。"
        )
        reasons.append(_BASIS_NOTE)
        return VERDICT_WORTH, reasons, numbers

    reasons.append(
        f"**無法判斷**（不是「還好」）：到手成本 NT${landed_twd:,.0f} 落在 80% 區間 "
        f"NT${lo:,.0f}–NT${hi:,.0f} 之內，這個模型分不出它是貴還是便宜。"
    )
    reasons.append(_landed_line(landed_twd, comp_stats))
    reasons.append(_model_line(estimate))
    return VERDICT_CAUTION, reasons, numbers


def _landed_line(landed_twd: float, stats: dict[str, Any]) -> str:
    if not stats.get("n"):
        return f"你要付 NT${landed_twd:,.0f}（到手成本），但沒有任何可比成交樣本。"
    line = (
        f"{stats['tier_label']}的 {stats['n']} 筆成交落在 NT${stats['min_twd']:,.0f}–"
        f"NT${stats['max_twd']:,.0f}（中位 NT${stats['median_twd']:,.0f}），"
        f"你要付 NT${landed_twd:,.0f}。"
    )
    mix = stats.get("venue_mix") or {}
    if len(mix) > 1:
        from .valuation import venue_label

        parts = "、".join(f"{venue_label(k)} {v} 筆" for k, v in sorted(mix.items()))
        line += f"（⚠️ 這 {stats['n']} 筆橫跨多個平台：{parts}——平台間的價格水準差 2 倍以上）"
    same_n = stats.get("same_venue_n") or 0
    if stats.get("target_venue_label") and same_n:
        line += (
            f"｜其中 {stats['target_venue_label']} {same_n} 筆，"
            f"中位 NT${stats['same_venue_median_twd']:,.0f}。"
        )
    return line


def _model_line(estimate: Any) -> str:
    line = (
        f"模型公允價 NT${estimate.fair_twd:,.0f}（{estimate.level_label}，"
        f"有效樣本 {estimate.n_effective} 筆）"
    )
    if estimate.has_interval:
        line += f"，80% 區間 NT${estimate.lo_twd:,.0f}–NT${estimate.hi_twd:,.0f}"
    if estimate.p_worth_buying is not None:
        line += f"，P(值得買) {estimate.p_worth_buying:.0%}"
    return line + "。" + _venue_line(estimate)


def _venue_line(estimate: Any) -> str:
    """公允價是**哪個平台**的價格水準——沒說清楚的公允價會被拿去跟別的市場比。"""
    from .valuation import venue_label

    if not getattr(estimate, "venue_adjusted", False):
        return (
            "⚠️ 這個公允價**沒有做平台校正**，是混合平台的價格水準"
            "（Yahoo 競價出清價與 Mercari/PayPay 定價零售價差 2 倍以上）。"
        )
    label = venue_label(estimate.venue)
    line = f"這是 **{label}** 的價格水準（其他平台的同款成交價可以差到 2 倍以上）。"
    if estimate.venue_is_estimated is False:
        line += f"⚠️ 但 {label} 的平台係數是先驗、不是估計值（可比分層不足）。"
    return line


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AppraisalReport:
    input_url: str
    site: str
    external_id: str
    buy_url: str
    origin_url: str | None
    fetched_via: str
    item: dict[str, Any]
    card: dict[str, Any]
    routes: list[dict[str, Any]]
    best_route: dict[str, Any]
    estimate: dict[str, Any]
    comparables: list[dict[str, Any]]
    comparable_stats: dict[str, Any]
    verdict: str
    verdict_reasons: list[str]
    verdict_numbers: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    stance: str = STANCE
    #: eBay 專用：寄到美國地址（buying.us_ship_zip）的**替代路徑**。
    #: None ＝ 不適用（非 eBay／寄得到台灣／沒設美國地址）。內容見
    #: `_us_ship_alternative`——它永遠帶著「不含美國→台灣轉運」的警告一起出門。
    us_ship_option: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _route_dict(q: RouteQuote) -> dict[str, Any]:
    d = asdict(q)
    d["overhead_ratio"] = round(q.overhead_ratio, 4)
    return d


def _ebay_warnings(item: ItemPage, best: RouteQuote, cfg: Config) -> list[str]:
    """eBay 專屬警告。三件事沒說清楚，這份報告就會誤導人：

    1. **運費佔比**：實測一筆 NT$653 的卡運費 NT$435——到手成本有四成是運送。
       使用者先前就是因為只看到一個總數而困惑，所以拆解一律外顯，不等門檻。
    2. **寄不寄得到台灣**：`shipToLocations` 不含台灣的話，算出來的到手成本是
       一個不存在的交易。
    3. **台幣是估算值**：eBay 幫忙換算顯示，實際請款走賣家幣別（所以成本模型
       有套刷卡加成）——這與 Mercari 台灣的台幣標價方向相反，不可互相參照。
    """
    from .scoring import overhead_threshold

    out: list[str] = []
    ratio = best.overhead_ratio
    line = (
        f"💸 **運費佔到手成本 {ratio:.0%}**：到手 NT${best.landed_twd:,.0f} ＝ "
        f"商品 NT${best.item_twd:,.0f} ＋ 國際運費 NT${best.shipping_twd:,.0f}"
        "（eBay 直寄台灣，無代購費）。"
    )
    threshold = overhead_threshold(cfg)
    if ratio >= threshold:
        line += f"⚠️ 已超過 {threshold:.0%} 的運費佔比告警門檻——這一單有很大一塊在買運送。"
    out.append(line)

    if item.shipping_cost is None:
        symbol = _CURRENCY_SYMBOL[item.currency]
        out.append(
            f"⚠️ **運費是未知的，不是零**（{item.shipping_note or '原因未知'}）："
            f"成本模型用保守佔位值 {symbol}25 頂替，而 eBay 寄台灣的實際運費"
            "常在 NT$400-600——也就是說上面那個到手成本是**低估**的。"
            "下單前請自己到商品頁確認運費。"
        )

    if item.ships_to_tw is False:
        out.append(
            "🚫 **這個賣家的 shipToLocations 不含台灣**（regionIncluded 沒有 TW／"
            "WORLDWIDE，或 regionExcluded 明確排除了台灣）：就算成本算得出來，"
            "這筆也可能根本寄不到——下單前先私訊賣家確認，或改用轉運地址。"
        )
    elif item.ships_to_tw is None:
        out.append(
            "eBay 這筆沒有回報 shipToLocations，本工具**判斷不了**寄不寄台灣"
            "（不是「寄得到」）。"
        )

    if item.currency is Currency.TWD:
        origin = f"（原幣 {item.converted_from}）" if item.converted_from else ""
        out.append(
            f"eBay 顯示的台幣是**它幫你換算的估算值**{origin}，不是在地請款："
            "實際會以賣家幣別請款，你的卡片再用自己的匯率換算並收海外手續費，"
            "所以到手成本**有**套刷卡加成與匯率緩衝。"
            "⚠️ 這與 Mercari 台灣的台幣標價（在地請款、不套加成）方向相反，"
            "兩邊的台幣數字不是同一種東西。"
        )
    return out


#: 兩次對外請求之間的最小間隔（秒）。模組層掛鉤：測試換掉它，不必等真的 2 秒。
_us_query_sleep = None  # 延遲繫結（見 _us_ship_alternative），避免 import time 散落


def _us_ship_alternative(
    cfg: Config, target: Target, item: ItemPage, ebay: Any, fx: Any
) -> tuple[dict[str, Any] | None, list[str]]:
    """eBay 標的寄不到台灣（或判斷不了）時的**替代路徑**：寄到美國地址。

    只在 `buying.us_ship_zip` 有設、且 `ships_to_tw is not True` 時多打**一次**
    API（`contextualLocation=country=US,zip=…`）——寄得到台灣的標的不查，
    不把每筆鑑價的請求數翻倍。兩次請求之間隔 2 秒（對外禮貌，全專案一致）。

    回傳 (結構化結果 | None, 要塞進 warnings 的行)。任何失敗都不准拖垮主報告
    （隔離邊界）：替代路徑是加分項，「查不到」也要說清楚，而不是安靜消失。

    ⚠️ 誠實邊界（每一行輸出都要帶著）：這個數字是「貨到美國地址」的成本，
    **不含美國→台灣的轉運**——貨會留在美國，後續要自己安排。它不可以與
    寄台灣的到手成本直接比大小。
    """
    zip_code = str((getattr(cfg, "buying", None) or {}).get("us_ship_zip") or "").strip()
    if target.site is not Site.EBAY or not zip_code:
        return None, []
    if item.ships_to_tw is True:
        return None, []

    import time as _time

    from .sources.ebay import (
        EbaySource,
        native_price_info,
        read_price,
        read_shipping,
        us_context,
    )

    sleep = _us_query_sleep or _time.sleep
    try:
        source = ebay if ebay is not None else EbaySource(cfg)
        sleep(2.0)  # 主查詢剛打過同一個 API，對外請求間隔 ≥2 秒
        blob = source.get_item(target.external_id, context=us_context(zip_code))
    except Exception as exc:  # noqa: BLE001 - 隔離邊界，見 docstring
        return None, [
            f"（曾嘗試用美國地址（{zip_code}）重查替代路徑，失敗："
            f"{type(exc).__name__}: {exc}——替代路徑無法評估，不影響上面的判斷）"
        ]

    price = read_price(blob)
    if price is None:
        return None, [f"用美國地址（{zip_code}）重查：抓不到價格，替代路徑無法評估。"]
    shipping, ship_note = read_shipping(blob, price.currency)
    if shipping is None:
        return None, [
            f"用美國地址（{zip_code}）重查：eBay 沒有回報寄到該地址的運費"
            f"（{ship_note or '原因未知'}）——替代路徑**不可行或無法報價**。"
        ]

    landed_us = price.value + shipping
    native = native_price_info(blob)
    sym = {"USD": "US$", "GBP": "£", "EUR": "€", "TWD": "NT$"}.get(
        price.currency, price.currency + " "
    )
    # 台幣等值只是量級參考（用我們的 fx 表、含刷卡加成——這筆會以外幣請款）。
    try:
        twd_approx = round(fx.to_twd(landed_us, price.currency), 0)
        twd_txt = f" ≈ NT${twd_approx:,.0f}"
    except Exception:  # noqa: BLE001 - 幣別不在表上就不硬算
        twd_approx = None
        twd_txt = ""
    option = {
        "zip": zip_code,
        "currency": price.currency,
        "item_price": price.value,
        "shipping": shipping,
        "landed_us": round(landed_us, 2),
        "landed_us_twd_approx": twd_approx,
        "price_kind": price.kind,
        "native": (
            {"value": native.value, "currency": native.currency, "rate": native.rate}
            if native is not None else None
        ),
        "note": "不含美國→台灣轉運成本；貨會留在美國",
    }
    lines = [
        f"📦 替代路徑：寄到美國地址（{zip_code}）——商品 {sym}{price.value:,.2f} ＋ "
        f"美國段運費 {sym}{shipping:,.2f} ＝ 到手（美國）{sym}{landed_us:,.2f}{twd_txt}。"
        "⚠️ **不含美國→台灣的轉運成本，貨會留在美國**——後續轉運要自己安排，"
        "這個數字不可與上面「寄台灣」的到手成本直接比較。"
        + ("（此筆是競標，商品價取目前出價，會漲。）"
           if price.kind == LIVE_AUCTION_KIND else "")
    ]
    return option, lines


def appraise(
    cfg: Config,
    url: str,
    *,
    store: Any = None,
    fetcher: Any = None,
    waf: Any = None,
    fx: Any = None,
    index: CardIndex | None = None,
    ebay: Any = None,
) -> AppraisalReport:
    """貼一個商品網址，回一份判決報告。

    `store` / `fetcher` / `waf` / `fx` / `index` 可注入（測試與 web 層共用長物件）。
    **本函式只關掉自己建立的資源**——傳進來的一律由呼叫端負責，
    否則 dashboard 第二次呼叫就會拿到一個關掉的 client。
    """
    from .fx import FxRates
    from .store import Store
    from .valuation import build_valuator, load_comps_rows

    target = parse_target(url)
    item = fetch_item_page(cfg, target, fetcher=fetcher, waf=waf, ebay=ebay)

    the_store = store if store is not None else Store(cfg.db_path)
    the_fx = fx if fx is not None else FxRates(cfg)
    idx = index if index is not None else CardIndex.load()

    # --- 卡片屬性 -----------------------------------------------------
    card = parse_card(item.title, cfg.watchlist)
    resolution = apply_grade_resolution(card, item)
    card_match = idx.match(item.title) if idx.available else None
    card_name = card_match.name_ja if (card_match and card_match.in_era) else None

    # --- 到手成本（所有可行路徑）--------------------------------------
    listing = Listing(
        site=target.site,
        external_id=target.external_id,
        title=item.title,
        url=target.buy_url,
        price=item.price,
        currency=item.currency,
        image_url=item.image_url,
        seller_id=item.seller,
        # eBay 的運費在 listing 上（`costs._quote_ebay` 讀它），日本路徑則是
        # route 的固定費率——所以這裡不能給預設值，None 就是「未知」。
        shipping_cost=item.shipping_cost,
        # 日本路徑走 Buyee／Mercari 台灣一定寄得到；eBay 是逐筆不同，
        # 而「判斷不了」要保持 None，寫成 True 就是拿一個猜測當事實。
        ships_to_tw=item.ships_to_tw if target.site is Site.EBAY else True,
        source="appraise",
        origin_url=target.origin_url,
        # `raw["price_kind"]` 是 `bidding.is_live_auction()` 唯一的判準。
        # 掃描端寫它、鑑價端不寫的話，同一筆競標標的在兩條路上會被判成不同語意。
        raw={"price_kind": item.price_kind, "current_bid": item.current_bid},
    )
    routes = quote_all_routes(listing, cfg, the_fx)
    if not routes:
        raise ValueError(f"{target.site.value} 沒有設定任何可用 route")
    best = routes[0]

    # --- 估價 ---------------------------------------------------------
    # 目標平台 = 這個標的自己的平台。評估一個 Mercari 標的就要跟 Mercari 的
    # 價格水準比——拿混合平台（含 Yahoo 競標出清價）的中位數比會系統性高估折價。
    valuator = build_valuator(cfg, the_store, idx)
    estimate = valuator.estimate(
        card_name=card_name,
        rarity=card.rarity,
        grade=card.grade,
        grade_source=card.grade_source,
        landed_twd=best.landed_twd,
        venue=target.site.value,
    )
    comparables, comp_stats = collect_comparables(
        load_comps_rows(the_store),
        idx,
        card_name=card_name,
        rarity=card.rarity,
        grade=card.grade,
        venue=target.site.value,
    )

    verdict, reasons, numbers = decide_verdict(
        landed_twd=best.landed_twd,
        estimate=estimate,
        comparables=comparables,
        comp_stats=comp_stats,
        card=card,
        card_match=card_match,
    )

    warnings: list[str] = []
    # 分數的來歷永遠要說。排在最前面：它會直接乘進公允價，是所有數字的地基。
    warnings.append(resolution.note)
    if card.grade is None:
        warnings.append(
            "⚠️ **這張卡的鑑定分數目前是未知的**，模型只能當成基準分數 9 處理，"
            "而分數溢價從 7 分的 ×0.35 到 10 分的 ×3.95 橫跨 11 倍——"
            f"出價上限因此不會提供。**請自己打開商品頁看照片上的鑑定殼**："
            f"{target.buy_url}"
            + (f"（原站：{target.origin_url}）" if target.origin_url else "")
        )
    elif item.description is None:
        warnings.append(
            # 「沒有描述可用」在兩個平台是不同的事實，說錯了會讓使用者去看一個
            # 根本不存在的東西：Buyee 是頁面上真的沒有，eBay 是有但**不可信**。
            "eBay 的商品描述是賣家自己的 HTML 模板（常夾別的商品與 SEO 關鍵字堆），"
            "本工具刻意**不從描述抽分數**——所以分數只能靠標題，"
            "標題與殼上不一致時本工具看不出來。"
            if target.site is Site.EBAY
            else "這個平台的商品頁不提供賣家描述（Buyee 代購頁只轉載標題／價格／圖片），"
            "所以分數只能靠標題——標題與殼上不一致時本工具看不出來。"
        )
    if item.price_kind == LIVE_AUCTION_KIND:
        warnings.append(item.price_note)
    if item.is_sold:
        warnings.append("這個標的看起來已經結標／售出，價格只能當歷史參考。")
    if target.site is Site.EBAY:
        warnings.extend(_ebay_warnings(item, best, cfg))
        if item.price_kind == LIVE_AUCTION_KIND:
            warnings.append(
                "🔨 eBay 競標的**出價上限在掃描端計算**（bidding.max_bid_ebay：台幣"
                "反解、用這筆 listing 自己的 eBay 匯率換回原幣、扣 listing 上的實際"
                "運費，證據閘門與 Yahoo 同一套）——看 dashboard 的競標分頁或推播。"
                "本報告的成本是「如果現在就以這個出價成交」的參考，不要當可買入價。"
                "eBay 原生支援自動出價（automatic bidding）：出價欄填上限即可離開。"
            )
        us_option, us_warnings = _us_ship_alternative(cfg, target, item, ebay, the_fx)
        warnings.extend(us_warnings)
    else:
        us_option = None
    if target.site is Site.MERCARI_TW:
        warnings.append(
            "Mercari 台灣商品頁沒有可靠的「已售出」結構標記，本工具**不判斷**售出狀態——"
            "請自己看頁面上還有沒有「立即購買」。"
        )
        if item.currency is Currency.TWD:
            warnings.append(
                "這筆的標價是**新台幣**（站內已換過匯）：到手成本沒有再套一次匯率，"
                "只加上 route 的服務費與運費。跟日圓標價的 Buyee 標的直接比數字是有效的，"
                "但要記得 Mercari 台灣的標價本身已含它自己的換匯加成。"
            )
    if not idx.available:
        warnings.append(
            "卡片主檔不存在（跑 `ygo-sniper refresh-cards` 建立），"
            "本次比對完全沒有卡名維度。"
        )
    if card.excluded_by:
        warnings.append(f"標題命中排除字「{card.excluded_by}」，掃描時這筆會被丟掉。")
    warnings.extend(estimate.notes)

    return AppraisalReport(
        input_url=url,
        site=target.site.value,
        external_id=target.external_id,
        buy_url=target.buy_url,
        origin_url=target.origin_url,
        fetched_via=target.fetch_mode,
        item={
            "title": item.title,
            "price": item.price,
            "currency": item.currency.value,
            "price_kind": item.price_kind,
            "price_note": item.price_note,
            "buyout_jpy": item.buyout_jpy,
            "current_bid_jpy": item.current_bid_jpy,
            "image_url": item.image_url,
            "bids": item.bids,
            "end_time": item.end_time,
            "status": item.status,
            "is_sold": item.is_sold,
            "has_description": item.description is not None,
            # eBay 才有的欄位。`shipping_cost`／`current_bid` 的幣別是上面那個
            # `currency`，前端顯示時**必須跟著它走**（永遠不准分開引用）。
            "shipping_cost": item.shipping_cost,
            "shipping_note": item.shipping_note,
            "current_bid": item.current_bid,
            "seller": item.seller,
            "ships_to_tw": item.ships_to_tw,
            "condition": item.condition,
            "converted_from": item.converted_from,
        },
        card={
            "grader": card.grader.value,
            "grade": card.grade,
            # 分數的來歷（title / description / None）＋ 為什麼。前端要能把
            # 「標題寫的」與「從描述撈的」畫成不同的樣子——可信度不同。
            "grade_source": card.grade_source,
            "grade_note": resolution.note,
            "grade_conflict": resolution.conflict,
            "has_description": item.description is not None,
            "rarity": card.rarity,
            "in_era": card.in_era,
            "era_evidence": card.era_evidence,
            "set_code": card.set_code,
            "language": card.language,
            "excluded_by": card.excluded_by,
            "card_name": card_name,
            "matched_name": card_match.name_ja if card_match else None,
            "matched_in_era": card_match.in_era if card_match else None,
            "matched_ocg_date": card_match.ocg_date if card_match else None,
        },
        routes=[_route_dict(q) for q in routes],
        best_route=_route_dict(best),
        estimate=estimate.to_dict(),
        comparables=[asdict(c) for c in comparables],
        comparable_stats=comp_stats,
        verdict=verdict,
        verdict_reasons=reasons,
        verdict_numbers=numbers,
        warnings=warnings,
        us_ship_option=us_option,
    )


# ---------------------------------------------------------------------------
# 批次補抓：分數不明的既有訊號
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class GradeRecovery:
    """一筆「分數不明」訊號的補抓結果。**沒補到也是結果**，而且要說得出為什麼。

    三種沒補到分不清就沒有價值：抓不到頁面（工具的問題）、頁面沒有描述
    （平台的問題，Buyee 代購頁就是）、描述有寫但互相矛盾（賣家的問題）。
    使用者的下一步各不相同，所以三者分開記在 `fetch_error` / `has_description`
    / `conflict` 三個欄位，不是壓成一個布林值。
    """

    key: str
    title: str
    site: str
    url: str
    #: True ＝ 真的補到一個可用的分數
    recovered: bool
    grade: float | None
    grader: str
    grade_source: str | None
    conflict: bool
    note: str
    has_description: bool
    fetch_error: str | None = None
    #: 補抓前後的出價上限。`before_ok` 對這一批**必然是 False**
    #: （grade=None 過不了 `require_known_grade`），列出來是為了讓
    #: 「多了幾筆可行動標的」這句話有前後對照，而不是一個孤零零的數字。
    before_bid_ok: bool = False
    after_bid_ok: bool = False
    after_bid_jpy: float | None = None
    bid_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def recover_missing_grades(
    cfg: Config,
    rows: list[dict[str, Any]],
    *,
    fx: Any,
    comps_engine: Any,
    valuator: Any,
    fetcher: Any = None,
    waf: Any = None,
    ebay: Any = None,
    apply_to: Any = None,
) -> list[GradeRecovery]:
    """對「分數不明」的既有訊號逐筆開商品頁，從描述補抓分數。

    補到之後走**完整的出貨路徑**（`scoring.evaluate`）重算，理由與
    `bidding.recompute_ceilings` 一樣：分數一變，公允價、旗標、分數與出價上限
    全部要跟著變，只改其中一個會留下自相矛盾的列（工程原則 1）。

    `apply_to` 給了 Store 才寫回（`upsert_signal`，人工狀態與筆記不會被洗掉）；
    沒給就是純 dry-run。

    **紅線**：補抓到的分數會直接乘進公允價並反推出價上限，使用者照著它下真錢
    的單。所以這裡完全依賴 `parsers.grade.resolve_grade` 的保守判定——矛盾、
    機構對不上、關鍵字堆，一律回 None 而不是猜一個。抓錯比抓不到危險得多。
    """
    from .bidding import listing_from_payload
    from .domain import Grader
    from .scoring import evaluate
    from .valuation import estimate_listing

    out: list[GradeRecovery] = []
    # eBay 的補抓走 Browse API，而每個 EbaySource 各自持有一顆 OAuth token
    # ——逐列各建一個等於一列一次 OAuth。整批共用一顆（TTL 2 小時）。
    the_ebay = ebay
    for row in rows:
        try:
            payload = json.loads(row.get("payload") or "{}") or {}
        except (TypeError, ValueError):
            continue
        listing_d = payload.get("listing") or {}
        if not listing_d:
            continue
        try:
            listing = listing_from_payload(listing_d)
        except (KeyError, ValueError, TypeError):
            continue

        card_d = payload.get("card") or {}
        info = CardInfo(
            grader=Grader(card_d.get("grader") or Grader.UNKNOWN.value),
            grade=card_d.get("grade"),
            in_era=bool(card_d.get("in_era")),
            era_evidence=list(card_d.get("era_evidence") or []),
            set_code=card_d.get("set_code"),
            language=card_d.get("language"),
            excluded_by=card_d.get("excluded_by"),
            rarity=card_d.get("rarity"),
            grade_source=card_d.get("grade_source"),
        )
        before = payload.get("bid") or {}
        base = dict(
            key=row.get("key") or listing.key, title=listing.title,
            site=listing.site.value, url=listing.url,
            before_bid_ok=bool(before.get("ok")),
        )

        try:
            target = parse_target(listing.url)
        except UnsupportedUrlError:
            out.append(GradeRecovery(
                recovered=False, grade=None, grader=info.grader.value,
                grade_source=None, conflict=False, has_description=False,
                note=(
                    "本工具沒有這個站的商品頁抓取路徑，補抓不了——"
                    "**請自己打開連結看照片上的鑑定殼**"
                ),
                fetch_error="不支援的商品頁網址", **base,
            ))
            continue

        if target.fetch_mode == "ebay_api" and the_ebay is None:
            from .sources.ebay import EbaySource

            the_ebay = EbaySource(cfg)
        try:
            item = fetch_item_page(cfg, target, fetcher=fetcher, waf=waf, ebay=the_ebay)
        except (FetchError, ValueError) as exc:
            out.append(GradeRecovery(
                recovered=False, grade=None, grader=info.grader.value,
                grade_source=None, conflict=False, has_description=False,
                note="商品頁抓取失敗，這一筆的分數仍然不明——請自己打開連結看鑑定殼",
                fetch_error=str(exc), **base,
            ))
            continue

        resolution = apply_grade_resolution(info, item)
        rec = GradeRecovery(
            recovered=info.grade is not None,
            grade=info.grade,
            grader=info.grader.value,
            grade_source=info.grade_source,
            conflict=resolution.conflict,
            note=resolution.note,
            has_description=item.description is not None,
            **base,
        )
        if rec.recovered:
            estimate = estimate_listing(valuator, listing, info)
            sig = evaluate(
                listing, info, comps_engine.stats_for(listing, info), cfg, fx,
                keep_all=True, estimate=estimate,
            )
            if sig is not None:
                rec.after_bid_ok = bool(sig.bid and sig.bid.ok)
                rec.after_bid_jpy = sig.bid.max_bid_jpy if sig.bid else None
                rec.bid_reason = sig.bid.reason if sig.bid else ""
                if apply_to is not None:
                    apply_to.upsert_signal(sig)
        out.append(rec)
    return out


__all__ = [
    "MIN_COMPARABLES",
    "P_WORTH_AVOID",
    "STANCE",
    "SUPPORTED_URL_FORMS",
    "VERDICT_AVOID",
    "VERDICT_CAUTION",
    "VERDICT_WORTH",
    "AppraisalReport",
    "Comparable",
    "GradeRecovery",
    "ItemPage",
    "Target",
    "UnsupportedUrlError",
    "appraise",
    "apply_grade_resolution",
    "fetch_item_page",
    "collect_comparables",
    "decide_verdict",
    "parse_buyee_item",
    "parse_ebay_item",
    "parse_mercari_seller",
    "parse_mercari_tw_item",
    "parse_target",
    "parse_yahoo_item",
    "recover_missing_grades",
]
