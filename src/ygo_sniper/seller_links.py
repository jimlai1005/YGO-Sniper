"""賣家鍵 → 原站賣場頁 URL。純函式、無相依，供 CLI／dashboard 共用。

**一律連原站，不連 Buyee**，兩個理由：

1. Buyee 的賣家頁 HEAD 會撞 AWS WAF 挑戰（`202` + 空 body），跟
   CLAUDE.md 第五節那個坑是同一個——連過去大機率是一片空白，不是賣家頁。
2. 這個連結的用途是**評估賣家**（看評價、是不是商店、庫存全貌），
   這些只有原站頁看得到；實際下單走的是 `Listing.url`，本來就是
   Buyee 連結，不受這個功能影響。

未知的 `site`（目前表上沒有的值，例如未來新增 `mercari_tw` / `ruten`）
一律回 `None`，**不猜 URL**：猜錯的連結點下去是 404，使用者會以為賣家
不見了，比沒有連結更糟（見 CLAUDE.md 第五節「靜默失敗」的鏡像版本——
這裡是「看起來對但其實是錯的」，一樣要避免）。
"""

from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit

#: site → URL 模板，`{seller_id}` 是唯一的插槽。
#: 每一列都已用 `curl -sI` 對一個真實 seller_id 實測過（2026-08-05）。
_SELLER_PAGE_TEMPLATES: dict[str, str] = {
    # Yahoo! 拍賣（透過 Buyee 代標，但賣家頁本身走 Yahoo 原站）。HEAD 200。
    "buyee_yahoo": "https://auctions.yahoo.co.jp/seller/{seller_id}",
    # Yahoo!フリマ（PayPay フリマ改名後的原站）。HEAD 200。
    "buyee_paypay": "https://paypayfleamarket.yahoo.co.jp/user/{seller_id}",
    # Mercari 日本原站。HEAD 200。
    "buyee_mercari": "https://jp.mercari.com/user/profile/{seller_id}",
    # eBay 原站。HEAD 302（導去 store 頁，不是 404——賣家頁存在時 eBay
    # 常見的正常行為，不代表連結壞掉）。
    "ebay": "https://www.ebay.com/usr/{seller_id}",
}


def seller_page_url(seller_key: str | None) -> str | None:
    """`{site}:{seller_id}` → 原站賣場頁 URL；未知站台或格式錯誤一律回 `None`。

    `seller_id` 本身可能含 `:`（目前資料沒有，但格式沒有禁止），所以只切
    第一個冒號（`partition`，等同 `split(":", 1)`），不是 `split(":")`。
    `seller_id` 會用 `urllib.parse.quote`（`safe=""`）escape 之後才插進
    URL——直接插入的話，含空格或特殊字元的 ID 會把路徑弄壞。
    """
    if not seller_key:
        return None
    site, sep, seller_id = seller_key.partition(":")
    if not sep or not site or not seller_id:
        return None
    template = _SELLER_PAGE_TEMPLATES.get(site)
    if template is None:
        return None
    return template.format(seller_id=quote(seller_id, safe=""))


# ---------------------------------------------------------------------------
# 反方向：使用者貼的賣家頁 URL → seller_key（釘選軌的入口）
# ---------------------------------------------------------------------------

#: 使用者訊息用的「支援哪些 URL」清單。**只在這裡維護一份**：
#: 錯誤訊息、CLI help、dashboard 提示都引用它，不要在別處手打。
SUPPORTED_URL_FORMS: tuple[str, ...] = (
    "auctions.yahoo.co.jp/seller/{賣家ID}",
    "paypayfleamarket.yahoo.co.jp/user/{賣家ID}",
    "jp.mercari.com/user/profile/{賣家ID}",
    "tw.mercari.com/seller/{賣家ID}（可帶語系前綴，如 /zh-hant/seller/…）",
    "www.ebay.com/usr/{帳號}",
    "www.ebay.com/str/{店鋪}（CLI／dashboard 會連網解析出真實帳號）",
)


class SellerUrlError(ValueError):
    """賣家頁 URL 認不得（或明確不支援）。

    **一定要拋，不准回 None**：回 None 的話，「貼錯網址」與「釘選成功」
    在呼叫端只差一個沒人檢查的空值（CLAUDE.md 第五節——靜默失敗）。
    訊息必須直接可行動：說得出「這個 URL 為什麼不行、支援哪些形式」。
    """


def _supported_forms_text() -> str:
    return "支援的賣家頁 URL：" + "、".join(SUPPORTED_URL_FORMS)


def parse_seller_url(url: str) -> str:
    """賣家 profile 頁 URL → `{site}:{seller_id}`。認不得就拋 `SellerUrlError`。

    - query string（`?_trksid=…`）與 fragment 由 `urlsplit` 天然剝掉；
      seller_id 一律 `unquote`（URL-encoded 的 ID 要還原成真實 ID，
      seller_watch 存的是解碼後的鍵）。
    - 路徑比對用**逐段切割**（`path.split("/")`），不用 regex——
      這裡沒有任何 `\\b` 可以踩（CLAUDE.md 第二節），逐段比對也不會被
      子字串偽造（`/seller/` 不會命中 `/reseller/`）。
    - eBay 店鋪頁 `/str/{slug}` **在這個純函式裡不支援**：store slug 與
      Browse API 的 username 是兩個命名空間，猜「slug 就是 username」猜錯時
      會長期追蹤一個不存在的賣家而毫無錯誤訊息。解析店鋪頁需要網路
      （`seller_resolve.resolve_ebay_store`；CLI 與 dashboard 的釘選入口走
      `seller_resolve.resolve_seller_target`，已內建這一步）。直接呼叫本函式
      碰到 /str/ 一律拋例外並指路 `/usr/`——這裡不猜、也不偷偷打網路。
    - `tw.mercari.com` 的賣家收進 **`buyee_mercari`**，不是自成一站：
      Mercari 台灣與 Mercari 日本共用同一個賣家 ID 空間（見
      `seller_seed.py` 頂部 2026-08-04 的觀察註記），而本專案能實際列舉
      這個賣家在售商品的管道是 Buyee 的 Mercari 鏡像
      （`buyee.jp/mercari/search?seller={id}`，2026-08-09 已用生產路徑
      WafSession＋httpx 實測成功）。收進 `mercari_tw` 會製造一個永遠
      掃不到的孤兒鍵；收進 `buyee_mercari` 則與賣家頁列舉實作同鍵，
      dashboard 的原站連結模板（jp.mercari.com/user/profile/{id}）也天然可用。
    """
    raw = (url or "").strip()
    parts = urlsplit(raw)
    host = parts.netloc.lower().rpartition("@")[2].partition(":")[0]
    if parts.scheme not in ("http", "https") or not host:
        raise SellerUrlError(
            f"這不是一個可解析的網址：{raw!r}。{_supported_forms_text()}"
        )
    # 逐段切路徑；剝掉空段（開頭的 / 與尾隨的 /）。
    segs = [unquote(s) for s in parts.path.split("/") if s]

    if host == "auctions.yahoo.co.jp" and len(segs) == 2 and segs[0] == "seller" and segs[1]:
        return f"buyee_yahoo:{segs[1]}"
    if host == "paypayfleamarket.yahoo.co.jp" and len(segs) == 2 and segs[0] == "user" and segs[1]:
        return f"buyee_paypay:{segs[1]}"
    if (host == "jp.mercari.com" and len(segs) == 3
            and segs[0] == "user" and segs[1] == "profile" and segs[2]):
        return f"buyee_mercari:{segs[2]}"
    if host == "tw.mercari.com":
        # 兩種形：/seller/{id} 與 /{語系前綴}/seller/{id}（如 /zh-hant/seller/…）。
        # 對映到 buyee_mercari（不是 mercari_tw）——理由見 docstring。
        if len(segs) == 2 and segs[0] == "seller" and segs[1]:
            return f"buyee_mercari:{segs[1]}"
        if len(segs) == 3 and segs[1] == "seller" and segs[2]:
            return f"buyee_mercari:{segs[2]}"
    if host == "www.ebay.com" or host == "ebay.com":
        if len(segs) == 2 and segs[0] == "usr" and segs[1]:
            return f"ebay:{segs[1]}"
        if segs and segs[0] == "str":
            raise SellerUrlError(
                "eBay 店鋪頁（/str/）需要連網解析出真實帳號"
                "（店鋪 slug 不等於帳號名，猜錯會長期追蹤一個不存在的賣家）——"
                "請走 CLI／dashboard 的釘選入口（seller_resolve），"
                "或改貼 ebay.com/usr/帳號 頁。"
                + _supported_forms_text()
            )
    raise SellerUrlError(
        f"認不得這個賣家頁 URL：{raw!r}。{_supported_forms_text()}"
    )


__all__ = [
    "SUPPORTED_URL_FORMS",
    "SellerUrlError",
    "parse_seller_url",
    "seller_page_url",
]
