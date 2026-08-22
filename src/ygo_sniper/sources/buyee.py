"""Buyee 抓取（Mercari + Yahoo! Auctions）。

**設計前提：這個 parser 一定會壞。**

Buyee 的 HTML class 名稱會改，所以我刻意不依賴任何 CSS class。
做法是「用商品連結當錨點」：先用 regex 找出所有 /mercari/item/mXXXX 這種連結，
再往上走 DOM 找最近的容器，從容器文字裡撈價格、從 img 撈圖。
class 全部改名這招還是會活。

真的壞掉的時候跑：
    ygo-sniper probe "https://buyee.jp/mercari/search?keyword=..."
它會印出「找到幾個連結、解析出什麼」，兩分鐘就知道是哪一層爛掉。

抓取頻率請維持 config 的 2 秒節流。改成每小時跑之後這件事更重要，不是更不重要：
同樣的節流，一天的請求數已經是原本的 24 倍。省請求要靠「新着排序＋只抓第 1 頁」
（`fetch.max_pages_per_query: 1`），不是靠把 delay 調小。
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from ..config import Config
from ..domain import Currency, Listing, Site
from .base import BlockedError, CachedFetcher, FetchError
from .health import ParseHealth, SearchResult
from .waf import WafSession

_PRICE_RE = re.compile(r"([\d,]{3,})\s*(?:YEN|円|¥)", re.I)
_YEN_PREFIX_RE = re.compile(r"[¥￥]\s*([\d,]{3,})")

#: RECON §3：`section.category` 內裸文字「1〜100 件目」——Buyee 沒有總命中數，
#: 命中數交叉比對退化為「件目範圍文字存在但解析 0 筆 → PARSER_BROKEN」。
_RANGE_RE = re.compile(r"\d+〜\d+\s*件目")

#: 縮圖是 **lazyload**：真圖藏在 `data-bind="lazyload: { imagePath: '//…' }"`，
#: `src` 永遠是佔位動畫。這件事只有用 httpx 抓原始 HTML 才看得見——
#: Playwright 會把 JS 跑完、真圖已經被塞進 src，於是 fixture 是綠的、線上全是轉圈圈
#: （2026-08-02 事故：db 裡 24/28 筆 Buyee 訊號的 image_url 是 loading-spinner.gif）。
#: 只抓 imagePath，**不可**放寬成任意 `'…'`：同一個 data-bind 裡緊跟著 `onError`，
#: 它的值正是佔位圖，抓錯欄位等於什麼都沒修。
_LAZY_IMAGE_RE = re.compile(r"imagePath\s*:\s*['\"]([^'\"]+)['\"]")

#: 已知佔位圖。抓到它**等於沒抓到**——必須回 None 讓下一個候選有機會，
#: 而不是存進 db 讓前端顯示一張永遠在轉的 gif。
_PLACEHOLDER_IMAGE_RE = re.compile(
    r"loading-spinner|placeholder|blank\.gif|spacer\.gif|no[-_]?image|noimage", re.I
)

#: id regex 刻意寬鬆到 `[A-Za-z0-9]+`（錨定在完整的 /xxx/item/ 路徑前綴上，
#: 不會誤抓路徑其他段）：Mercari 新版 id 是 22 字 base62（RECON 實測 sold 頁
#: 98 筆佔 82，比例只會越來越高），舊的 `(m\d+)` 會整批漏掉；PayPay 現況全是
#: `z`+數字，但硬編 `z` 就是重蹈覆轍。href 帶 query（?conversionType=...），
#: `[A-Za-z0-9]+` 在 `?` 前自然停下，regex 不需含 query。
#: ⚠️ `sort_newest` 的值**兩站不一樣，而且不可互換**（2026-08-01 實測）。
#: 兩邊排序選單的 name 都是 `serch_item[order-sort]`（Buyee 自己的拼字），送出的
#: query 參數是 `order-sort=`，但 option 值不同：
#:   Mercari 的「新着順に並び替え」= `desc-created_time`
#:   PayPay  的「新しい順」        = `created_time`（沒有 desc- 前綴）
#: 把 Mercari 的值送給 PayPay 的後果**不是**退回預設排序，而是整個頁面壞掉：
#: 實測回一份 89KB 的頁面、連 `ul.item-lists` 骨架都沒有 → 我們的健康判定會
#: 判成 PARSER_BROKEN，於是每小時發一則假告警，而真正的問題是參數打錯。
#: 所以這個值綁在 site spec 上，不是共用常數。
_SITE_SPEC = {
    Site.BUYEE_MERCARI: {
        "item_path": re.compile(r"/mercari/item/([A-Za-z0-9]+)"),
        "item_url": "https://buyee.jp/mercari/item/{id}",
        "search_base": "https://buyee.jp/mercari/search",
        "sort_newest": "desc-created_time",
    },
    Site.BUYEE_PAYPAY: {
        "item_path": re.compile(r"/paypayfleamarket/item/([A-Za-z0-9]+)"),
        "item_url": "https://buyee.jp/paypayfleamarket/item/{id}",
        "search_base": "https://buyee.jp/paypayfleamarket/search",
        "sort_newest": "created_time",
    },
    # Site.BUYEE_YAHOO 的搜尋 spec 已移除：Yahoo 的發現管道由 yahoo_direct
    # （sources/yahoo.py，純 httpx）完全取代，少一條需要 Playwright 的路。
    # Site.BUYEE_YAHOO 本身仍在 domain（它是購買路徑，不是發現管道）。
}


class BuyeeSource:
    def __init__(
        self, cfg: Config, site: Site, fetcher: CachedFetcher | WafSession | None = None
    ) -> None:
        if site not in _SITE_SPEC:
            raise ValueError(f"BuyeeSource 不支援 {site}")
        self.cfg = cfg
        self.site = site
        self.spec = _SITE_SPEC[site]
        self.fetcher = fetcher or CachedFetcher(cfg)
        # 發現管道名與購買路徑在 Buyee 系是 1:1，直接沿用 site.value
        self.name = site.value
        # 吃得下 `category`（→ URL 的 `category_id`）。pipeline 讀這個屬性決定
        # 要不要把分類傳進來；宣告成屬性而不是靠 inspect 猜簽名，是因為
        # 「有這個參數」與「這個參數真的會生效」是兩件事。
        self.supports_category = True
        # Mercari 與 PayPay 都實測 status=sold_out 有效（RECON §3/§4）
        self.supports_sold = True
        # 高價帶掃描（2026-08-22）：Buyee Mercari 的 `price_min` 已對照組實測
        # 生效且閉區間（見 plan docs/superpowers/plans/2026-08-22-high-band-scan.md）。
        # 宣告成屬性讓 `run_source_search` 能判斷「這個來源真的吃得下 min_price」，
        # 而不是無條件傳下去讓不支援的來源安靜漏接（category 那次事故的重演）。
        self.supports_min_price = True
        # 新上架優先（見 _SITE_SPEC 的排序值註解）。實測與 status=sold_out 併用
        # 不衝突（sold 搜尋 98/98 仍帶 SOLD 標記），所以 comps 那條也一併吃到。
        self.sort_newest = bool((cfg.sources.get(self.name) or {}).get("sort_newest", True))

    # ------------------------------------------------------------------
    def build_url(
        self,
        keyword: str,
        *,
        page: int = 1,
        max_price: float | None = None,
        min_price: float | None = None,
        sold: bool = False,
        category: str | None = None,
    ) -> str:
        """組搜尋網址。

        max_price 是效率關鍵：由 costs.max_item_price_jpy() 算出來的破口價，
        直接讓平台幫我們過濾掉不可能中的標的，省下大量抓取與解析。

        min_price（高價帶掃描，2026-08-22）：與 max_price 同一寫法、同一位置——
        `price_min` 已對照組實測生效且閉區間（plan
        docs/superpowers/plans/2026-08-22-high-band-scan.md）。
        """
        params: dict[str, str | int] = {"lang": "ja", "page": page}
        if max_price:
            params["price_max"] = int(max_price)
        if min_price:
            params["price_min"] = int(min_price)
        if category:
            # Buyee 的分類 ID 是純數字（`categories.buyee_mercari.yugioh_ocg: 1152`）。
            # 值以字串在管線裡流動（見 queries.py），這裡原樣送出——Buyee 收字串。
            params["category_id"] = category
        if sold:
            # Mercari 側是「已售出」篩選；欄位名 Buyee 改過幾次，
            # 兩個都塞，多餘的參數會被忽略。
            params["status"] = "sold_out"
            params["sold"] = 1
        if self.sort_newest:
            # 參數名是 `order-sort`（實測：在排序選單選「新着順」後瀏覽器導向的
            # URL 就是這個），值逐站不同，見 _SITE_SPEC 註解。
            params["order-sort"] = self.spec["sort_newest"]

        # Mercari 與 PayPay 的搜尋端點同構（RECON §4）：都是 ?keyword=&lang=ja
        params["keyword"] = keyword
        return f"{self.spec['search_base']}?{urlencode(params)}"

    # ------------------------------------------------------------------
    def search(
        self,
        keyword: str,
        *,
        max_price: float | None = None,
        min_price: float | None = None,
        sold: bool = False,
        pages: int | None = None,
        category: str | None = None,
    ) -> list[Listing]:
        """相容包裝：只要清單的人（comps 的 sold 搜尋）用這個。

        抓取層失敗維持舊行為**往上拋**——refresh_comps 靠例外印警告，
        這裡靜默回空清單的話，被擋三週你只會看到三週的「comps 沒新資料」。
        """
        result = self.search_detailed(
            keyword, max_price=max_price, min_price=min_price, sold=sold,
            pages=pages, category=category,
        )
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
        min_price: float | None = None,
        sold: bool = False,
        pages: int | None = None,
        category: str | None = None,
    ) -> SearchResult:
        """與 yahoo.py 同介面：清單＋健康判定一起回，pipeline 據此告警。"""
        pages = pages or int(self.cfg.fetch["max_pages_per_query"])
        first_url = self.build_url(
            keyword, page=1, max_price=max_price, min_price=min_price,
            sold=sold, category=category,
        )
        result = SearchResult(
            source=self.name, site=self.site.value, query=keyword, url=first_url
        )
        seen: set[str] = set()

        for page in range(1, pages + 1):
            url = self.build_url(
                keyword, page=page, max_price=max_price, min_price=min_price,
                sold=sold, category=category,
            )
            try:
                html = self.fetcher.get(url)
            except BlockedError as exc:
                # 被 WAF 擋走不到 parser，與「0 筆」嚴格分開（PLAN Q5）
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
            batch = self._parse_soup(soup, sold=sold)

            if page == 1:
                result.html_bytes = len(html)
                health, detail = self._judge_health(soup, len(batch))
                if health is not ParseHealth.OK:
                    result.health = health
                    result.detail = detail
                    return result

            if not batch:
                break  # 第 2 頁以後空頁 = 翻完了
            for lst in batch:
                if lst.external_id not in seen:
                    seen.add(lst.external_id)
                    result.listings.append(lst)
                    # Buyee 兩站都是定價出售，沒有 Yahoo 那種「純競標要丟掉」的
                    # 商業篩選，所以「解析出幾個商品」＝「產出幾筆 listing」。
                    # 仍然要**明確填**而不是讓 canary 回頭去數 listings：
                    # 哪天這裡加了任何篩選（例如排除 sold），健康判定不該跟著位移。
                    result.parsed_count += 1
        return result

    # ------------------------------------------------------------------
    # 賣家頁列舉（釘選軌 Phase 2，2026-08-09）
    # ------------------------------------------------------------------
    def build_seller_url(self, seller_id: str) -> str:
        """賣家頁網址：`/mercari/search?seller={純數字ID}`。

        這個形態是從生產 cache 的商品頁連結實證出來的（2026-08-09 用生產路徑
        WafSession＋httpx 實測 seller=448657621：598KB、75 個唯一商品、
        版型與搜尋頁同構 `ul.item-lists`）。**刻意不帶任何其他參數**
        （lang／page／order-sort 都沒有）：實測過的 URL 就是這一個形，而這
        個站對沒實測過的參數組合可能靜默忽略、也可能整頁壞掉（PayPay 排序
        值事故，見 `_SITE_SPEC` 註解）——沒實測就不加。

        `urlencode`：seller_id 目前全是純數字，但髒 ID 要被 encode 而
        不是靜默改寫 query 結構（與 yahoo.build_seller_url 同一立場）。
        """
        return f"{self.spec['search_base']}?{urlencode({'seller': str(seller_id)})}"

    def search_seller(
        self, seller_id: str, *, pages: int | None = None, sold: bool = False
    ) -> SearchResult:
        """單一賣家的在架清單（釘選軌／輪替監控）。

        介面與 `yahoo_direct.search_seller`／`paypay_direct.search_seller`
        對齊（回 `SearchResult`、帶 `parsed_count`、健康判定），呼叫端是
        `pipeline._scan_watched_sellers` 經 `run_source_search`。
        抓取走 self.fetcher——生產環境那是 WafSession（見 sources/__init__.py），
        與關鍵字搜尋完全同一條路：被 WAF 擋会拋 BlockedError，這裡轉成
        BLOCKED 健康碼，絕不與「0 筆」混同。

        `sold=True` 學 yahoo：**警告＋回空**。Buyee 鏡像的賣家頁沒有已售
        清單（`status=sold_out` 搭 `seller=` 未實測，未實測的參數組合不准
        憑印象送出）；靜默回在架等於把「有人開的價」當成交寫進行情。

        **固定 1 頁**：實測賣家頁顯示「1〜100 件目」＝一頁 100 筆，遠大於
        單一賣家的在架量（實測 75 筆）；翻頁參數搭 `seller=` 未實測，
        `pages` 收在介面上是為了與其他 source 同形，>1 會降回 1 並印警告
        （與 paypay_direct 同一做法）。
        """
        if sold:
            print(f"[warn] {self.name} 賣家頁沒有已售出清單，sold=True 回空清單")
            return SearchResult(
                source=self.name, site=self.site.value,
                query=f"seller:{seller_id}", url=self.build_seller_url(seller_id),
                health=ParseHealth.EMPTY_CONFIRMED,
                detail="Buyee Mercari 鏡像無賣家已售清單（sold 搭 seller 未實測，不猜）",
            )
        if pages and int(pages) > 1:
            print(f"[warn] {self.name} 賣家頁翻頁尚未實測，pages={pages} 降為 1")

        url = self.build_seller_url(seller_id)
        result = SearchResult(
            source=self.name, site=self.site.value,
            query=f"seller:{seller_id}", url=url,
        )
        try:
            html = self.fetcher.get(url)
        except BlockedError as exc:
            # 被 WAF 擋走不到 parser，與「這個賣家沒貨」嚴格分開
            result.health = ParseHealth.BLOCKED
            result.detail = f"被擋：{exc}"
            return result
        except FetchError as exc:
            # 抓不到 = 對這個賣家「什麼都不知道」，跟「他沒上架」是兩件事
            result.health = ParseHealth.FETCH_FAILED
            result.detail = f"抓取失敗：{exc}"
            return result

        result.pages_fetched = 1
        result.html_bytes = len(html)
        soup = BeautifulSoup(html, "html.parser")
        batch = self._parse_soup(soup, sold=False)
        health, detail = self._judge_health(soup, len(batch))
        if health is not ParseHealth.OK:
            result.health = health
            result.detail = detail
            return result

        for lst in batch:
            # 賣家頁上每一筆都屬於這個賣家——搜尋頁解析器抓不到 seller_id，
            # 但這裡我們**確知**主人是誰。填上它，觀測與 Seller Alpha 的
            # 折價帳才有歸屬（listing_obs.seller_id 是同儕比較的鍵）。
            lst.seller_id = str(seller_id)
            result.listings.append(lst)
            # 與 search_detailed 同一立場：明確填 parsed_count，不讓 canary
            # 回頭數 listings（哪天加了篩選，健康判定不該跟著位移）。
            result.parsed_count += 1
        if result.parsed_count >= 100:
            # 2026-08-09 審查 F5：一頁滿 100 筆時，第 2 頁的存在與否無從得知
            # ——固定 1 頁是紀律（翻頁搭 seller= 未實測），但「可能漏抓」
            # 必須說出來，不能讓大賣家的長尾靜默消失（CLAUDE.md 第五節）。
            result.detail = (
                "已達單頁上限 100 筆，可能有下一頁未抓"
                "（翻頁搭 seller= 未實測，暫不翻）"
            )
        return result

    # ------------------------------------------------------------------
    def _judge_health(self, soup: BeautifulSoup, parsed: int) -> tuple[ParseHealth, str]:
        """Buyee 的 0 筆判定只能是**結構性**的（RECON §3 實測含截圖）：

        無結果頁沒有任何「無結果」文字，主內容區就是空白；而側欄
        `div.sideFilterItemBody__noResults` 的「検索結果 0件」是品牌 facet，
        ok 頁也有，絕不可當判定依據。唯一可靠的判準是
        「`ul.item-lists` 骨架存在（＝不是 WAF 頁）但 0 個直接 li 子節點」。
        """
        item_list = soup.select_one("ul.item-lists")
        if item_list is not None and not item_list.find_all("li", recursive=False):
            return ParseHealth.EMPTY_CONFIRMED, "ul.item-lists 存在但 0 個 li：確認查無結果"

        if parsed == 0:
            # 命中數交叉比對的退化版：Buyee 沒有總命中數元素，只有
            # `section.category` 的「1〜100 件目」範圍文字——它在、商品卻解析
            # 出 0 筆，必定是 selector／regex 過期，不可能是市場問題。
            cat_section = soup.select_one("section.category")
            if cat_section and _RANGE_RE.search(cat_section.get_text(" ", strip=True)):
                return ParseHealth.PARSER_BROKEN, "頁面標示件目範圍但解析出 0 筆"
            return (
                ParseHealth.PARSER_BROKEN,
                "無商品、無 item-lists 空骨架、無件目文字：頁面結構已改版",
            )
        return ParseHealth.OK, ""

    # ------------------------------------------------------------------
    def parse_search_page(self, html: str, *, sold: bool = False) -> list[Listing]:
        return self._parse_soup(BeautifulSoup(html, "html.parser"), sold=sold)

    def _parse_soup(self, soup: BeautifulSoup, *, sold: bool = False) -> list[Listing]:
        pattern: re.Pattern[str] = self.spec["item_path"]

        found: dict[str, Listing] = {}
        for a in soup.find_all("a", href=True):
            m = pattern.search(a["href"])
            if not m:
                continue
            item_id = m.group(1)
            if item_id in found:
                continue
            container = self._nearest_container(a)
            listing = self._extract(item_id, a, container, sold=sold)
            if listing:
                found[item_id] = listing
        return list(found.values())

    # ------------------------------------------------------------------
    @staticmethod
    def _nearest_container(anchor: Tag) -> Tag:
        """往上走最多 4 層，找出「看起來像一張卡片」的容器。

        判準是容器裡有 img 且文字量不會爆炸（爆炸代表已經走到整個 list 了）。
        """
        node: Tag = anchor
        best: Tag = anchor
        for _ in range(4):
            parent = node.parent
            if not isinstance(parent, Tag):
                break
            text_len = len(parent.get_text(" ", strip=True))
            if text_len > 600:
                break
            best = parent
            node = parent
            if parent.find("img") and _PRICE_RE.search(parent.get_text(" ", strip=True)):
                break
        return best

    def _extract(
        self, item_id: str, anchor: Tag, container: Tag, *, sold: bool
    ) -> Listing | None:
        text = container.get_text(" ", strip=True)

        price = self._parse_price(text)
        if price is None:
            return None

        title = self._parse_title(anchor, container)
        if not title:
            return None

        return Listing(
            site=self.site,
            external_id=item_id,
            title=title,
            url=self.spec["item_url"].format(id=item_id),
            price=price,
            currency=Currency.JPY,
            image_url=self._parse_image(container),
            ships_to_tw=True,  # 走 Buyee/Mercari TW 一定寄得到，這是這條路的最大優勢
            best_offer_enabled=False,  # 代購買不到日本的「値下げ交渉」
            is_sold=sold,
            source=self.name,
            # origin_url 留 None：Buyee 商品頁本身就是購買端，url 已經是它；
            # 這個欄位只給「發現端 ≠ 購買端」的管道（yahoo_direct）放原生頁。
            # 兩個值相同還重複塞，下游會誤以為有第二個可看的原生頁。
            origin_url=None,
        )

    @staticmethod
    def normalize_image_url(raw: str | None) -> str | None:
        """協定相對網址補 https；**佔位圖一律當成沒有圖**。

        回 None 而不是回佔位圖，是為了讓呼叫端的「下一個候選」有機會被試到——
        佔位圖存進 db 之後，前端沒有任何辦法分辨它和真圖，只會顯示一張轉圈圈。
        """
        if not raw:
            return None
        url = raw.strip()
        if not url:
            return None
        if _PLACEHOLDER_IMAGE_RE.search(url):
            return None
        if url.startswith("//"):
            url = "https:" + url
        return url

    @classmethod
    def _parse_image(cls, container: Tag) -> str | None:
        """縮圖網址。優先序：`data-src` → `data-bind` 的 imagePath → `src`。

        `src` 排最後而不是拿掉，是因為它在非 lazyload 的頁面（以及未來改回
        直出 img 的版本）仍然是對的；排最後又過濾佔位圖，兩種頁面都能活。
        一張 img 的三個候選全是佔位圖時才換下一張 img（SOLD 徽章之類的小圖
        通常沒有這些屬性，不會誤取）。
        """
        for img in container.find_all("img"):
            if not isinstance(img, Tag):
                continue
            lazy = _LAZY_IMAGE_RE.search(str(img.get("data-bind") or ""))
            for raw in (
                img.get("data-src"),
                lazy.group(1) if lazy else None,
                img.get("data-original"),
                img.get("src"),
            ):
                url = cls.normalize_image_url(raw)
                if url:
                    return url
        return None

    @staticmethod
    def _parse_price(text: str) -> float | None:
        """取容器內最小的合理日圓數字。

        為什麼取最小？因為 Buyee 的卡片區塊常常同時出現商品價與
        「US$xx」換算價、甚至促銷文案裡的金額。實測商品本身的日圓價
        通常是最小的那個四位數以上數字。
        """
        candidates: list[float] = []
        for m in list(_PRICE_RE.finditer(text)) + list(_YEN_PREFIX_RE.finditer(text)):
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if 100 <= v <= 5_000_000:
                candidates.append(v)
        return min(candidates) if candidates else None

    @staticmethod
    def _parse_title(anchor: Tag, container: Tag) -> str:
        # 優先序：img alt > a title 屬性 > a 文字 > 容器文字第一段
        img = container.find("img")
        if isinstance(img, Tag) and img.get("alt"):
            alt = img["alt"].strip()
            if len(alt) > 4:
                return alt
        if anchor.get("title"):
            return str(anchor["title"]).strip()
        txt = anchor.get_text(" ", strip=True)
        if len(txt) > 4:
            # 搜尋頁的連結文字常常是「標題 + With Authentication + 標題 + 價格」，
            # 砍掉價格尾巴就好，重複的部分交給 parser 處理（不影響關鍵字比對）
            return _PRICE_RE.sub("", txt).strip()
        return container.get_text(" ", strip=True)[:120]


def parse_item_image(html: str) -> str | None:
    """Buyee **商品頁**的縮圖（搜尋頁走 `BuyeeSource._parse_image`）。

    商品頁的 `og:image` 是伺服器端直出的真實網址（httpx 抓得到，實測
    Mercari `static.mercdn.net/item/detail/orig/...`、PayPay
    `cdnyauction.buyee.jp/...`），所以優先用它；拿不到才退回頁內的
    lazyload imagePath。兩條路都過同一個 `normalize_image_url`——
    佔位圖的判準只有一份，回填與抓取用的是同一把尺。
    """
    soup = BeautifulSoup(html, "html.parser")
    og = soup.select_one("meta[property='og:image']")
    url = BuyeeSource.normalize_image_url(og.get("content") if og else None)
    if url:
        return url
    m = _LAZY_IMAGE_RE.search(html)
    return BuyeeSource.normalize_image_url(m.group(1) if m else None)


def probe(cfg: Config, url: str) -> dict:
    """壞掉時的除錯入口。印出抓到什麼，兩分鐘定位問題。"""
    fetcher = CachedFetcher(cfg)
    try:
        try:
            html = fetcher.get(url, use_cache=False)
        except FetchError as exc:
            # 抓取層失敗要跟「抓到了但解析出 0 筆」分得一清二楚，
            # 不然你會拿著一個網路問題去調 parser，調一整天。
            return {
                "url": url,
                "ok": False,
                "stage": "fetch",
                "error": type(exc).__name__,
                "status": exc.status,
                "transient": exc.transient,
                "detail": str(exc),
            }

        result = {"url": url, "ok": True, "stage": "parse", "html_bytes": len(html), "sites": {}}
        for site in _SITE_SPEC:
            src = BuyeeSource(cfg, site, fetcher)
            listings = src.parse_search_page(html)
            result["sites"][site.value] = {
                "count": len(listings),
                "sample": [
                    {"id": x.external_id, "title": x.title[:70], "price_jpy": x.price}
                    for x in listings[:5]
                ],
            }
        return result
    finally:
        fetcher.close()
