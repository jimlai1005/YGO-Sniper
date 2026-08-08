"""eBay `/str/` 店鋪頁 → 真實帳號（釘選軌的網路解析層，2026-08-09）。

`parse_seller_url`（seller_links.py）是純函式、永不碰網路，所以它對 `/str/`
一律拋錯——store slug 與 Browse API 的 username 是**兩個命名空間**，猜
「slug 就是 username」猜錯時會長期追蹤一個不存在的賣家而毫無錯誤訊息。
但使用者手上常常只有店鋪 URL。這個模組補上缺的那一步：抓店鋪頁、從內嵌
JSON 讀出真實帳號。

2026-08-09 實測（`/str/merrycorporation`，純 httpx＋瀏覽器 UA，200、852KB）：

- 帳號藏在內嵌 JSON：`"sellerId":"merry_tcg"`（×1）與 `"username":"merry_tcg"`（×2）。
- 另有 `"storeName":"merrycorporation"`——那是**店名不是帳號**，
  絕不能拿去餵 Browse API（本例兩者完全不同，餵錯就是追蹤幽靈賣家）。

失敗分類（全域工程原則 2／5）：

- 抓取走 `CachedFetcher`（專案唯一的 resilience boundary）：GET、冪等，
  transient（5xx／連線失敗）由它自動重試，語意失敗直接拋。
- 這一層的所有失敗（網路耗盡重試、抽不到帳號、多個候選不一致）統一轉
  `SellerUrlError` **大聲拋出**，訊息保留「請改貼 ebay.com/usr/帳號 頁」
  的指引——對呼叫端（CLI pin／dashboard pin）它們都是「這個 URL 解析不出
  可信的賣家鍵」，而使用者永遠有 /usr/ 這條不必解析的路。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .seller_links import SellerUrlError, parse_seller_url

_EBAY_HOSTS = frozenset({"www.ebay.com", "ebay.com"})

#: 帳號要**同一個值同時出現在 `username` 與 `sellerId` 兩種鍵下**才採信
#: （2026-08-09 審查 F4；實測店鋪頁兩種鍵都有、值相同）。單鍵命中不夠：
#: eBay 改版後店主標記若消失，頁上恰好殘留的單一 `"username"`（例如推薦
#: 賣家模組）會是**別人的帳號**——單值無從識別真假、也不會觸發「多值不
#: 一致」的拒絕，雙鍵交叉是唯一擋得住它的結構性防線。
#: **絕不加 `storeName`**——它是店名。
_USERNAME_KEY_RE = re.compile(r'"username"\s*:\s*"([^"]+)"')
_SELLER_ID_KEY_RE = re.compile(r'"sellerId"\s*:\s*"([^"]+)"')

_USR_HINT = "請改貼 ebay.com/usr/帳號 頁（賣家 profile 頁，不需要解析）"


def ebay_store_slug(url: str) -> str | None:
    """URL 是 eBay `/str/` 店鋪頁 → 店鋪 slug；否則 None。純函式、不打網路。

    路徑逐段切割（與 `parse_seller_url` 同一做法，不用 regex、不會被
    `/restr/` 之類的子字串偽造）。`/str/{slug}` 後面可以再跟分類段或
    query（實際店鋪連結常帶 `?_trksid=…`），只認第二段。
    """
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower().rpartition("@")[2].partition(":")[0]
    if parts.scheme not in ("http", "https") or host not in _EBAY_HOSTS:
        return None
    segs = [unquote(s) for s in parts.path.split("/") if s]
    if len(segs) >= 2 and segs[0] == "str" and segs[1]:
        return segs[1]
    return None


def extract_store_username(html: str, *, slug: str) -> str:
    """店鋪頁 HTML → 帳號。抽不到、單鍵孤證、或不一致一律拋 `SellerUrlError`。

    採信條件：同一個值同時出現在 `username` 與 `sellerId` 兩種鍵下
    （理由見 `_USERNAME_KEY_RE` 上方的 F4 註記）。
    與抓取拆開成純函式，fixture 測試（含「只出現 storeName」的反例）
    不必碰任何抓取層。
    """
    usernames = set(_USERNAME_KEY_RE.findall(html))
    seller_ids = set(_SELLER_ID_KEY_RE.findall(html))
    if not usernames and not seller_ids:
        raise SellerUrlError(
            f"店鋪頁 {slug} 讀不到賣家帳號（頁面內嵌 JSON 沒有 username/sellerId；"
            f"storeName 是店名不是帳號，不能拿來追蹤）。{_USR_HINT}"
        )
    consistent = usernames & seller_ids
    if not consistent:
        raise SellerUrlError(
            f"店鋪頁 {slug} 的帳號候選在兩種鍵下沒有共同值"
            f"（username={sorted(usernames)}、sellerId={sorted(seller_ids)}）——"
            f"單鍵孤證可能是頁上殘留的別家賣家標記，拒絕亂猜。{_USR_HINT}"
        )
    if len(consistent) > 1:
        raise SellerUrlError(
            f"店鋪頁 {slug} 出現多個不一致的帳號候選 {sorted(consistent)}，"
            f"無法確定哪個才是店主，拒絕亂猜。{_USR_HINT}"
        )
    return consistent.pop()


def resolve_ebay_store(url: str, cfg: Any = None, *, fetcher: Any = None) -> str:
    """eBay `/str/` 店鋪頁 URL → 真實帳號（username）。失敗拋 `SellerUrlError`。

    `fetcher` 只需有 `CachedFetcher.get` 同形的 `get(url)`（測試注入假的；
    生產不傳則用 `cfg` 建一個真的並負責關掉）。抓的是**重組後的正規 URL**
    `https://www.ebay.com/str/{slug}`——使用者貼來的 query 垃圾
    （`?_trksid=…`）不進快取鍵，同一家店永遠同一個 URL。
    """
    from .sources.base import CachedFetcher, FetchError

    slug = ebay_store_slug(url)
    if slug is None:
        raise SellerUrlError(
            f"這不是 eBay 店鋪頁（/str/）URL：{url!r}。{_USR_HINT}"
        )
    canonical = f"https://www.ebay.com/str/{quote(slug, safe='')}"

    own_fetcher = None
    if fetcher is None:
        if cfg is None:
            from .config import load_config

            cfg = load_config()
        own_fetcher = fetcher = CachedFetcher(cfg)
    try:
        try:
            html = fetcher.get(canonical)
        except FetchError as exc:
            # CachedFetcher 已對 transient 重試完畢；到這裡就是這條路走不通。
            # 統一轉 SellerUrlError：對釘選流程而言它是「解析不出賣家鍵」，
            # 而且訊息要給使用者一條不必解析的活路（/usr/）。
            raise SellerUrlError(
                f"店鋪頁 {slug} 抓取失敗（{exc}）。{_USR_HINT}"
            ) from exc
    finally:
        if own_fetcher is not None:
            own_fetcher.close()
    return extract_store_username(html, slug=slug)


def resolve_seller_target(
    target: str, cfg: Any = None, *, fetcher: Any = None
) -> tuple[str, str | None]:
    """釘選入口的統一解析：URL 或現成鍵 → `(seller_key, 店鋪 slug 或 None)`。

    CLI（`watch-seller pin`／`unpin`）與 dashboard 的 pin endpoint 共用這一份，
    判準只有一處：

    - 非 `http` 開頭 → 當現成的 `site:id` 鍵原樣回傳（合法性由 `add_watch` 把關）。
    - eBay `/str/` 店鋪頁 → **走網路**解析真實帳號，回 `("ebay:{username}", slug)`
      ——slug 給呼叫端寫進 reason／輸出訊息，使用者日後看得出這個鍵是哪來的。
    - 其他 URL → 純函式 `parse_seller_url`（零網路），slug 為 None。

    失敗一律拋 `SellerUrlError`（兩條路的例外型別相同，呼叫端接一種就好）。
    """
    raw = (target or "").strip()
    # 大小寫不敏感（2026-08-09 審查 F2）：`HTTPS://…` 若照大小寫比對會被
    # 當成「現成鍵」落庫成 site=`HTTPS` 的垃圾鍵。scheme 的大小寫交給
    # urlsplit 正規化（它本來就會轉小寫），這裡只負責認出「這是 URL」。
    if not raw.lower().startswith("http"):
        return raw, None
    slug = ebay_store_slug(raw)
    if slug is not None:
        username = resolve_ebay_store(raw, cfg, fetcher=fetcher)
        return f"ebay:{username}", slug
    return parse_seller_url(raw), None


__all__ = [
    "ebay_store_slug",
    "extract_store_username",
    "resolve_ebay_store",
    "resolve_seller_target",
]
