"""seller_resolve：eBay `/str/` 店鋪頁 → 真實帳號（零網路）。

fixture 是 2026-08-09 純 httpx＋瀏覽器 UA 實抓 `/str/merrycorporation`
（200、852KB）後**只裁切、未改寫**的真實片段：

- `ebay_str_ok.html`：含全部 `"sellerId":"merry_tcg"`（×1）、
  `"username":"merry_tcg"`（×2）、`"storeName":"merrycorporation"`（×3）
  出現處的前後文。
- `ebay_str_storename_only.html`：只出現 storeName 的片段——反例，
  釘住「storeName 絕不被誤採」：它是店名不是帳號，餵給 Browse API
  就是長期追蹤一個不存在的賣家而毫無錯誤訊息。

抓取層在測試裡一律是假 fetcher（duck-typed `get()`），零真實網路。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ygo_sniper.seller_links import SellerUrlError
from ygo_sniper.seller_resolve import (
    ebay_store_slug,
    extract_store_username,
    resolve_ebay_store,
    resolve_seller_target,
)
from ygo_sniper.sources.base import FetchError

FIXTURES = Path(__file__).parent / "fixtures"
STR_OK = (FIXTURES / "ebay_str_ok.html").read_text(encoding="utf-8")
STR_STORENAME_ONLY = (FIXTURES / "ebay_str_storename_only.html").read_text(encoding="utf-8")

STORE_URL = "https://www.ebay.com/str/merrycorporation"


class FakeFetcher:
    """`CachedFetcher.get` 的最小替身：記下請求、回固定內容或拋固定例外。"""

    def __init__(self, body: str | None = None, error: Exception | None = None):
        self.body = body
        self.error = error
        self.urls: list[str] = []

    def get(self, url: str, **_kw) -> str:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.body or ""


# ---------------------------------------------------------------------------
# 1. slug 偵測（純函式）
# ---------------------------------------------------------------------------
def test_store_slug_detected_for_str_urls():
    assert ebay_store_slug(STORE_URL) == "merrycorporation"
    assert ebay_store_slug("http://ebay.com/str/merrycorporation") == "merrycorporation"
    assert ebay_store_slug(STORE_URL + "/") == "merrycorporation"
    # 店鋪連結常帶 query 垃圾與子分類段——只認第二段
    assert ebay_store_slug(STORE_URL + "?_trksid=p123#tab") == "merrycorporation"
    assert ebay_store_slug(STORE_URL + "/Yu-Gi-Oh-Cards/_i.html") == "merrycorporation"


def test_store_slug_is_none_for_everything_else():
    assert ebay_store_slug("https://www.ebay.com/usr/merry_tcg") is None
    assert ebay_store_slug("https://www.ebay.com/str/") is None
    assert ebay_store_slug("https://example.com/str/x") is None       # 別的站
    assert ebay_store_slug("https://www.ebay.com/restr/x") is None    # 子字串不偽造
    assert ebay_store_slug("ebay.com/str/x") is None                  # 沒 scheme
    assert ebay_store_slug("") is None


# ---------------------------------------------------------------------------
# 2. 帳號抽取：交叉一致才可信，storeName 永不被採用
# ---------------------------------------------------------------------------
def test_extracts_the_real_username_from_the_store_page():
    assert extract_store_username(STR_OK, slug="merrycorporation") == "merry_tcg"


def test_storename_is_never_mistaken_for_the_username():
    """反例 fixture 只有 `"storeName":"merrycorporation"`——必須拋錯，
    絕不能回店名（slug ≠ username 正是整個 resolver 存在的理由）。"""
    assert '"storeName"' in STR_STORENAME_ONLY  # 前提：反例真的含 storeName
    with pytest.raises(SellerUrlError) as exc:
        extract_store_username(STR_STORENAME_ONLY, slug="merrycorporation")
    # 最重要的斷言在 raises 本身：它拋錯了，而不是回傳 "merrycorporation"。
    assert "usr" in str(exc.value)          # 指路 /usr/
    assert "storeName" in str(exc.value)    # 說得出為什麼不能用店名


def test_inconsistent_candidates_are_rejected_not_guessed():
    html = '{"username":"seller_a","items":[],"sellerId":"seller_b"}'
    with pytest.raises(SellerUrlError) as exc:
        extract_store_username(html, slug="somestore")
    msg = str(exc.value)
    assert "seller_a" in msg and "seller_b" in msg and "usr" in msg


def test_a_lone_single_key_hit_is_rejected_not_trusted():
    """F4 回歸（2026-08-09 審查）：頁上恰好只剩**一個**單鍵命中時必須拒絕。

    情境：eBay 改版後店主的內嵌標記消失，頁上殘留一格推薦賣家模組的
    `"username"`——值是別人的帳號。舊邏輯（兩鍵聯集取一致值）會靜默採信
    這個孤證，長期追蹤錯的賣家；雙鍵交叉要求同值同時出現在 username 與
    sellerId 下，孤證進不來。
    """
    lone_username = '{"username":"someone_else","recommended":true}'
    with pytest.raises(SellerUrlError) as exc:
        extract_store_username(lone_username, slug="merrycorporation")
    assert "someone_else" in str(exc.value) and "usr" in str(exc.value)

    lone_seller_id = '{"sellerId":"someone_else"}'
    with pytest.raises(SellerUrlError):
        extract_store_username(lone_seller_id, slug="merrycorporation")


def test_no_candidates_at_all_raises_with_guidance():
    with pytest.raises(SellerUrlError) as exc:
        extract_store_username("<html>nothing embedded</html>", slug="somestore")
    assert "usr" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. resolve_ebay_store：抓取走注入的 fetcher，失敗大聲拋
# ---------------------------------------------------------------------------
def test_resolves_against_the_canonical_store_url():
    """使用者貼來的 query 垃圾不進請求——同一家店永遠打同一個正規 URL。"""
    fetcher = FakeFetcher(body=STR_OK)
    username = resolve_ebay_store(
        STORE_URL + "?_trksid=p2047675.m3561.l2559", fetcher=fetcher
    )
    assert username == "merry_tcg"
    assert fetcher.urls == [STORE_URL]


def test_fetch_failure_becomes_a_loud_seller_url_error():
    fetcher = FakeFetcher(error=FetchError("HTTP 503", url=STORE_URL, transient=True))
    with pytest.raises(SellerUrlError) as exc:
        resolve_ebay_store(STORE_URL, fetcher=fetcher)
    assert "抓取失敗" in str(exc.value) and "usr" in str(exc.value)


def test_non_store_url_is_rejected_without_fetching():
    fetcher = FakeFetcher(body=STR_OK)
    with pytest.raises(SellerUrlError):
        resolve_ebay_store("https://www.ebay.com/usr/merry_tcg", fetcher=fetcher)
    assert fetcher.urls == []  # 認不得就不打網路


# ---------------------------------------------------------------------------
# 4. resolve_seller_target：CLI 與 dashboard 共用的統一入口
# ---------------------------------------------------------------------------
def test_target_passthrough_for_plain_keys():
    assert resolve_seller_target("ebay:psa") == ("ebay:psa", None)
    assert resolve_seller_target("  ruten:abc  ") == ("ruten:abc", None)


def test_target_uses_pure_parsing_for_profile_urls():
    """非 /str/ 的 URL 走純函式，一個請求都不發。"""
    fetcher = FakeFetcher(body=STR_OK)
    key, slug = resolve_seller_target(
        "https://www.ebay.com/usr/collectiblemore", fetcher=fetcher
    )
    assert (key, slug) == ("ebay:collectiblemore", None)
    assert fetcher.urls == []


def test_target_resolves_store_urls_and_reports_the_slug():
    fetcher = FakeFetcher(body=STR_OK)
    key, slug = resolve_seller_target(STORE_URL, fetcher=fetcher)
    assert key == "ebay:merry_tcg"
    assert slug == "merrycorporation"


def test_uppercase_scheme_is_still_recognized_as_a_url():
    """F2 回歸（2026-08-09 審查）：URL 偵測必須大小寫不敏感。

    `HTTPS://…` 若照大小寫比對會走「現成鍵」passthrough，落庫成
    site=`HTTPS` 的垃圾鍵——沒有錯誤訊息，只有一個永遠掃不到的孤兒列。
    scheme 的正規化交給 urlsplit（它本來就轉小寫），這裡只要認得出來。
    """
    fetcher = FakeFetcher(body=STR_OK)
    for url in (
        "HTTPS://www.ebay.com/usr/collectiblemore",
        "Http://www.ebay.com/usr/collectiblemore",
    ):
        key, slug = resolve_seller_target(url, fetcher=fetcher)
        assert (key, slug) == ("ebay:collectiblemore", None), url
    assert fetcher.urls == []  # /usr/ 走純函式，一個請求都不發

    # /str/ 店鋪頁同樣要進解析（連網路那條），而不是被當現成鍵
    key, slug = resolve_seller_target(
        "HTTPS://www.ebay.com/str/merrycorporation", fetcher=fetcher
    )
    assert key == "ebay:merry_tcg" and slug == "merrycorporation"


def test_target_still_raises_for_unrecognized_urls():
    with pytest.raises(SellerUrlError):
        resolve_seller_target("https://example.com/whatever", fetcher=FakeFetcher())


# ---------------------------------------------------------------------------
# 5. fixture 紀律：裁切片段必須保住真實頁的三種鍵值原文
# ---------------------------------------------------------------------------
def test_fixture_still_contains_all_three_key_shapes():
    """fixture 被重抓／重裁時，三種鍵值都要在，否則反例與交叉一致的
    測試會退化成測不到東西的空殼。"""
    assert '"sellerId":"merry_tcg"' in STR_OK
    assert '"username":"merry_tcg"' in STR_OK
    assert '"storeName":"merrycorporation"' in STR_OK
