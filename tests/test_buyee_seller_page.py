"""BuyeeSource.search_seller：Buyee Mercari 鏡像的賣家頁列舉（零網路）。

fixture `buyee_mercari_seller_ok.html` 是 2026-08-09 用**生產路徑**
（WafSession 取 aws-waf-token → httpx 帶 cookie）實抓的
`https://buyee.jp/mercari/search?seller=448657621` 原件：598KB、75 個唯一
商品、版型與關鍵字搜尋頁同構（`ul.item-lists`）。Playwright 渲染版抓不得
——lazyload 已被 JS 換成真圖，測不到生產環境的佔位圖（CLAUDE.md 第六節；
本檔沿用 test_buyee_parse.py 的結構性防線：fixture 必含 loading-spinner）。

這裡釘住的行為，每一條都對應一種靜默失敗：

1. 賣家頁上每一筆的 `seller_id` 都要填——這是 listing_obs 歸屬與
   Seller Alpha 同儕比較的鍵，漏填不會報錯，只會讓折價帳永遠沒有主人。
2. WAF 202 必須是 BLOCKED，不是「這個賣家沒貨」——被擋三週與賣家清空
   庫存外顯一模一樣，健康碼是唯一的分辨手段。
3. `sold=True` 回空＋警告，不是靜默回在架——把「有人開的價」當成交寫進
   行情表，每一筆都會看起來像撿到寶。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from ygo_sniper.domain import Site
from ygo_sniper.sources.base import CachedFetcher
from ygo_sniper.sources.buyee import BuyeeSource
from ygo_sniper.sources.health import ParseHealth

FIXTURES = Path(__file__).parent / "fixtures"
SELLER_OK = (FIXTURES / "buyee_mercari_seller_ok.html").read_text(encoding="utf-8")
MERCARI_EMPTY = (FIXTURES / "buyee_mercari_empty.html").read_text(encoding="utf-8")

SELLER_ID = "448657621"

#: 2026-08-09 實抓 fixture 的實際解析數（75 個唯一 /mercari/item/ 連結，
#: 逐筆都抽得出價格與標題）。這個數字變了代表 parser 或 fixture 動了，
#: 兩者都該被看見，所以斷言精確值而不是 `> 50`。
EXPECTED_COUNT = 75


@pytest.fixture
def make_source(cfg, tmp_path):
    """網路換成 MockTransport、快取寫在暫存目錄的 BuyeeSource（零網路）。"""
    created: list[CachedFetcher] = []

    def _make(status: int, body: str, *, headers=None, handler=None) -> BuyeeSource:
        scoped = replace(
            cfg,
            storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
            fetch={**cfg.fetch, "delay_seconds": 0.0, "backoff_seconds": 0.0},
        )
        fetcher = CachedFetcher(scoped)
        fetcher._client.close()
        default = lambda request: httpx.Response(  # noqa: E731
            status, text=body, headers=headers or {}
        )
        fetcher._client = httpx.Client(transport=httpx.MockTransport(handler or default))
        created.append(fetcher)
        return BuyeeSource(scoped, Site.BUYEE_MERCARI, fetcher)

    yield _make
    for f in created:
        f.close()


# ---------------------------------------------------------------------------
# 1. 正常頁：75 筆、每筆歸屬到這個賣家
# ---------------------------------------------------------------------------
def test_seller_page_parses_all_items_with_seller_attribution(make_source):
    src = make_source(200, SELLER_OK)
    result = src.search_seller(SELLER_ID)

    assert result.health is ParseHealth.OK
    assert len(result.listings) == EXPECTED_COUNT
    assert result.parsed_count == EXPECTED_COUNT
    assert result.pages_fetched == 1
    assert result.query == f"seller:{SELLER_ID}"

    for lst in result.listings:
        # 賣家頁上每一筆都屬於這個賣家；漏填的話 listing_obs 沒有主人，
        # Seller Alpha 的同儕比較永遠湊不出這個賣家的帳。
        assert lst.seller_id == SELLER_ID
        assert lst.site is Site.BUYEE_MERCARI
        assert lst.source == "buyee_mercari"
        assert lst.url == f"https://buyee.jp/mercari/item/{lst.external_id}"
        assert lst.price and 100 <= lst.price <= 5_000_000
        assert lst.title
        assert lst.is_sold is False

    titles = " ".join(x.title for x in result.listings)
    # 抽查：這個賣家專賣 PSA 鑑定遊戲王卡，標題必然大量出現這兩個詞
    assert "PSA" in titles and "遊戯王" in titles


def test_thumbnails_come_from_lazyload_not_placeholder(make_source):
    """賣家頁與搜尋頁同構，lazyload 佔位圖的教訓（2026-08-02）同樣適用。"""
    src = make_source(200, SELLER_OK)
    listings = src.search_seller(SELLER_ID).listings

    assert listings
    for lst in listings:
        assert lst.image_url and "loading-spinner" not in lst.image_url
        assert lst.image_url.startswith("https://")


# ---------------------------------------------------------------------------
# 2. URL 組法：實測過的形態，一個多餘參數都不加
# ---------------------------------------------------------------------------
def test_seller_url_is_the_field_tested_form():
    """`?seller={id}`——2026-08-09 生產路徑實測過的就是這一個形。

    不帶 lang／page／order-sort：這個站對沒實測過的參數組合可能靜默忽略、
    也可能整頁壞掉（PayPay 排序值事故），沒實測就不加。
    """
    from ygo_sniper.config import load_config

    src = BuyeeSource(load_config(), Site.BUYEE_MERCARI)
    assert (
        src.build_seller_url(SELLER_ID)
        == f"https://buyee.jp/mercari/search?seller={SELLER_ID}"
    )


def test_seller_id_is_url_escaped():
    """髒 ID 要被 encode，不是靜默改寫 query 結構。"""
    from ygo_sniper.config import load_config

    src = BuyeeSource(load_config(), Site.BUYEE_MERCARI)
    url = src.build_seller_url("a b&c")
    assert " " not in url
    assert "seller=a+b%26c" in url


def test_request_actually_hits_the_seller_url(make_source):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=SELLER_OK)

    src = make_source(200, SELLER_OK, handler=handler)
    src.search_seller(SELLER_ID)

    assert seen == [f"https://buyee.jp/mercari/search?seller={SELLER_ID}"]


# ---------------------------------------------------------------------------
# 3. sold=True：警告＋回空，一個請求都不發
# ---------------------------------------------------------------------------
def test_sold_true_returns_empty_without_touching_the_network(make_source, capsys):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=SELLER_OK)

    src = make_source(200, SELLER_OK, handler=handler)
    result = src.search_seller(SELLER_ID, sold=True)

    assert result.health is ParseHealth.EMPTY_CONFIRMED
    assert result.listings == []
    assert seen == []  # 不發請求：未實測的參數組合不准送出
    assert "sold=True 回空清單" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 4. pages>1：降回 1 並印警告（翻頁搭 seller= 未實測）
# ---------------------------------------------------------------------------
def test_pages_above_one_is_clamped_with_a_warning(make_source, capsys):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=SELLER_OK)

    src = make_source(200, SELLER_OK, handler=handler)
    result = src.search_seller(SELLER_ID, pages=3)

    assert len(seen) == 1  # 只打一頁
    assert result.health is ParseHealth.OK
    assert "降為 1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5. 失敗分類：被擋／抓不到／查無 都不准長得像「沒貨」以外的樣子
# ---------------------------------------------------------------------------
def test_a_full_page_of_100_warns_about_a_possible_next_page(make_source, monkeypatch):
    """F5 回歸（2026-08-09 審查）：一頁滿 100 筆（單頁上限）時，detail 必須
    註明「可能有下一頁未抓」。固定 1 頁是紀律（翻頁搭 seller= 未實測），
    但大賣家（使用者釘的往往正是大戶）的長尾不能靜默消失——75 筆的正常頁
    則不該出現這句話。"""
    src = make_source(200, SELLER_OK)
    real_batch = src._parse_soup(BeautifulSoup(SELLER_OK, "html.parser"), sold=False)
    assert len(real_batch) == EXPECTED_COUNT  # 前提：真解析是 75 筆
    padded = (real_batch * 2)[:100]
    monkeypatch.setattr(src, "_parse_soup", lambda soup, sold: padded)
    result = src.search_seller(SELLER_ID)
    assert result.parsed_count == 100
    assert result.health is ParseHealth.OK
    assert "可能有下一頁未抓" in result.detail


def test_a_normal_page_of_75_has_no_next_page_warning(make_source):
    src = make_source(200, SELLER_OK)
    result = src.search_seller(SELLER_ID)
    assert result.parsed_count == EXPECTED_COUNT
    assert "下一頁" not in (result.detail or "")


def test_waf_challenge_is_blocked_not_empty(make_source):
    """WafSession 失手（token 過期又補救失敗）時 fetcher 拋 BlockedError——
    健康碼必須是 BLOCKED：被擋三週與賣家清空庫存外顯一模一樣。"""
    src = make_source(
        202, "<html>" + "challenge" * 100 + "</html>",
        headers={"x-amzn-waf-action": "challenge"},
    )
    result = src.search_seller(SELLER_ID)

    assert result.health is ParseHealth.BLOCKED
    assert result.listings == []
    assert result.parsed_count == 0
    assert "被擋" in result.detail


def test_server_error_is_fetch_failed(make_source):
    src = make_source(500, "boom")
    result = src.search_seller(SELLER_ID)

    assert result.health is ParseHealth.FETCH_FAILED
    assert result.listings == []


def test_empty_seller_page_is_empty_confirmed(make_source):
    """`ul.item-lists` 骨架在、0 個 li ＝ 確認這個賣家沒有在架（結構性判定，
    與關鍵字搜尋同一份 `_judge_health`）。"""
    src = make_source(200, MERCARI_EMPTY)
    result = src.search_seller(SELLER_ID)

    assert result.health is ParseHealth.EMPTY_CONFIRMED
    assert result.listings == []


def test_renamed_item_path_is_parser_broken(make_source):
    broken = SELLER_OK.replace("/mercari/item/", "/mercari/produkt/")
    src = make_source(200, broken)
    result = src.search_seller(SELLER_ID)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert result.listings == []


# ---------------------------------------------------------------------------
# 6. fixture 紀律：必須是生產路徑（httpx）抓的原件
# ---------------------------------------------------------------------------
def test_seller_fixture_is_the_html_production_actually_sees():
    """Playwright 渲染版的 fixture 會讓佔位圖教訓（2026-08-02）重演：
    lazyload 被 JS 換成真圖，測試永遠綠、線上全是轉圈圈。"""
    assert "loading-spinner" in SELLER_OK, (
        "seller fixture 沒有佔位圖 src：它八成是 Playwright 渲染後的頁面，"
        "請用生產路徑（WafSession＋httpx）重抓"
    )
    assert "imagePath:" in SELLER_OK, "seller fixture 缺 lazyload 的 imagePath"
    # 75 個唯一商品連結（EXPECTED_COUNT 的依據，fixture 換抓要同步更新）
    ids = set(re.findall(r"/mercari/item/([A-Za-z0-9]+)", SELLER_OK))
    assert len(ids) == EXPECTED_COUNT


# ---------------------------------------------------------------------------
# 7. 接線：registry 的 buyee_mercari source 真的長出了賣家頁列舉
# ---------------------------------------------------------------------------
def test_registry_source_for_buyee_mercari_can_enumerate_sellers(cfg):
    """`SELLER_PAGE_SOURCE` 宣告誰能掃、registry 提供實例——兩邊要對得上，
    否則 pipeline 的 `_scan_watched_sellers` 會把釘選賣家記成
    「來源不在 registry 或沒有賣家頁列舉」然後跳過。"""
    from ygo_sniper.seller_watch import SELLER_PAGE_SOURCE, UNSUPPORTED_SITE_NOTE
    from ygo_sniper.sources import build_sources

    assert SELLER_PAGE_SOURCE["buyee_mercari"] == "buyee_mercari"
    assert "buyee_mercari" not in UNSUPPORTED_SITE_NOTE

    sources = build_sources(cfg)
    src = sources["buyee_mercari"]
    assert callable(getattr(src, "search_seller", None))
