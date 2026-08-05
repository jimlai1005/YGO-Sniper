"""Mercari 賣家抽取 ＋ **賣家鍵的 ID 空間一致性**（2026-08-04）。

這個檔案釘住兩件事：

1. **解析端**：Mercari 台灣商品頁（flight payload 的 `sellerId`／`sellerName`）
   與 Buyee 的 Mercari 鏡像頁（`/mercari/search?seller=…` 連結）都抽得到賣家。
   Mercari 先前是唯一一條完全沒有賣家 ID 的管道，而使用者「觀察中」的標的
   有三分之二在這條管道上——那批商品的賣家維度先前是全盲的。

2. **兩個平台的 seller_id 是不是同一個 ID 空間**。這是工程原則 1 的直球題：
   如果不是同一個空間，把兩邊的 ID 當同一個賣家就是把兩個人的行為合成一個
   人的 alpha。答案（實測）是**同一個空間**，證據落在
   `fixtures/mercari_id_space.json`：同一件日本商品 `m38347072251`，
   兩個平台各自報出 `901019808`（連顯示名稱都同樣是「りり」）。

   ⚠️ 但 `seller_key` 仍然是 `{site}:{seller_id}`、兩個站不合併——標價幣別與
   價格水準不同，同儕比對不可跨站。ID 空間相同讓我們**知道**兩個鍵指向同一
   個人，不代表可以把價格丟進同一個池子。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ygo_sniper.appraise import (
    _BUYEE_MERCARI_SELLER_RE,
    parse_buyee_item,
    parse_mercari_seller,
    parse_mercari_tw_item,
)

FIXTURES = Path(__file__).parent / "fixtures"
TW_HTML = (FIXTURES / "mercari_tw_item.html").read_text(encoding="utf-8")
BUYEE_HTML = (FIXTURES / "buyee_mercari_item.html").read_text(encoding="utf-8")
ID_SPACE = json.loads((FIXTURES / "mercari_id_space.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. 解析端
# ---------------------------------------------------------------------------
def test_mercari_tw_item_page_carries_the_seller():
    item = parse_mercari_tw_item(TW_HTML, "https://tw.mercari.com/zh-hant/items/x")

    assert item.seller == "707214741"
    assert item.seller_name == "グレード9以下まとめ買い5%オフ｜スラリ"
    # 賣家不得污染既有欄位：價格語意仍是新台幣定價
    assert item.price_kind == "fixed"
    assert item.currency.value == "TWD"


def test_buyee_mercari_item_page_carries_the_seller():
    item = parse_buyee_item(BUYEE_HTML, "https://buyee.jp/mercari/item/x")

    assert item.seller == "707214741"


def test_buyee_seller_regex_ignores_the_recommendation_blocks():
    """頁尾推薦區有一堆**別人的** partnerSellerId——判準只認賣家連結。

    抓第一個命中的 partnerSellerId 會把推薦商品的賣家掛到這件商品上，
    而那種錯是靜默的（照樣有個看起來合理的賣家）。
    """
    partners = set(re.findall(r'partnerSellerId":"(\d+)"', BUYEE_HTML))
    assert len(partners) > 5, "fixture 應該含多個推薦賣家，否則這條測試沒在測東西"
    assert set(_BUYEE_MERCARI_SELLER_RE.findall(BUYEE_HTML)) == {"707214741"}


def test_ambiguous_buyee_page_yields_no_seller():
    """出現兩個不同的賣家連結 ＝ 頁面結構變了 → **抽不到**，不是挑一個。"""
    html = BUYEE_HTML.replace(
        'href="/mercari/search?seller=707214741"',
        'href="/mercari/search?seller=999999999"',
        1,
    )
    assert parse_buyee_item(html, "u").seller is None


def test_missing_seller_is_none_not_a_guess():
    stripped = re.sub(r'\\?"sellerId\\?"\s*:\s*\\?"\d+\\?"', '\\\\"x\\\\":1', TW_HTML)
    assert parse_mercari_seller(stripped) == (None, None)


# ---------------------------------------------------------------------------
# 2. ID 空間一致性（本檔的核心）
# ---------------------------------------------------------------------------
def test_the_same_japanese_item_reports_the_same_seller_on_both_platforms():
    """同一件商品在 Buyee 鏡像與 Mercari 台灣報出**同一個 seller_id**。

    綁定兩個平台的是圖片檔名：Mercari 台灣的商品 ID 是 UUID，但圖片走
    `static.mercdn.net/…/photos/{日本站商品ID}_1.jpg`，所以它自己說得出
    「我是哪一件日本商品」。
    """
    jp = ID_SPACE["jp_item_id"]
    expected = ID_SPACE["expected_seller_id"]

    tw_id, _tw_name = parse_mercari_seller(ID_SPACE["tw_fragment"])
    buyee_ids = set(_BUYEE_MERCARI_SELLER_RE.findall(ID_SPACE["buyee_fragment"]))

    assert tw_id == expected
    assert buyee_ids == {expected}
    # 兩個片段講的真的是同一件商品（不然「同一個 ID」只是巧合）
    assert re.findall(r"photos/(m\d{11})", ID_SPACE["tw_photo_fragment"]) == [jp]
    assert re.findall(r"photos/(m\d{11})", ID_SPACE["buyee_photo_fragment"]) == [jp]


def test_seller_key_still_separates_the_two_sites():
    """ID 空間相同 ≠ 可以合併鍵。**同儕比對必須同站**（幣別與價格水準不同）。

    這條測試的存在是為了讓「以後有人想省事把兩個站合併」時先看到理由：
    合併之後 Mercari 台灣的新台幣標價會跟 Buyee 的日圓標價進同一個池子。
    """
    from ygo_sniper.seller_alpha import MarketRow, PeerIndex

    common = dict(
        basis="ask", price_twd=1000.0, title="遊戯王 初期 ウルトラ PSA9",
        card_name="青眼白龍", grader="PSA", grade=9.0,
    )
    tw = MarketRow(key="a", site="mercari_tw", seller_key="mercari_tw:901019808", **common)
    jp = MarketRow(
        key="b", site="buyee_mercari", seller_key="buyee_mercari:901019808",
        **{**common, "price_twd": 400.0},
    )
    index = PeerIndex([tw, jp])

    assert index.match(tw) is None, "跨站不得配對——那會把平台差算成賣家 alpha"
