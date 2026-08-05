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

from urllib.parse import quote

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


__all__ = ["seller_page_url"]
