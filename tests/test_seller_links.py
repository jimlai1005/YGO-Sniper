import pytest

from ygo_sniper.seller_links import SellerUrlError, parse_seller_url, seller_page_url


def test_each_known_site_builds_its_real_seller_page():
    assert seller_page_url("buyee_yahoo:63s28BcHAAtTbeYJosP5enfJReuFc") == \
        "https://auctions.yahoo.co.jp/seller/63s28BcHAAtTbeYJosP5enfJReuFc"
    assert seller_page_url("buyee_paypay:p10376874") == \
        "https://paypayfleamarket.yahoo.co.jp/user/p10376874"
    assert seller_page_url("buyee_mercari:448657621") == \
        "https://jp.mercari.com/user/profile/448657621"
    assert seller_page_url("ebay:collectiblemore") == \
        "https://www.ebay.com/usr/collectiblemore"


def test_unknown_site_returns_none_instead_of_guessing():
    """猜錯的 URL 點下去是 404，使用者會以為賣家不見了。
    寧可沒有連結，也不要一個看起來能用的錯連結。"""
    assert seller_page_url("mercari_tw:12345") is None
    assert seller_page_url("ruten:abc") is None


def test_malformed_seller_key_returns_none():
    for bad in ("", "noseparator", ":empty_site", "ebay:", None):
        assert seller_page_url(bad) is None


def test_seller_id_containing_a_colon_is_not_truncated():
    """seller_key 是 {site}:{seller_id}，seller_id 本身可能含冒號。"""
    assert seller_page_url("ebay:a:b") == "https://www.ebay.com/usr/a%3Ab"


def test_seller_id_is_url_escaped():
    """賣家 ID 直接插進 URL 會被特殊字元破壞路徑。"""
    url = seller_page_url("ebay:a b")
    assert url is not None and " " not in url


# ---------------------------------------------------------------------------
# 反方向：使用者貼的賣家頁 URL → seller_key（釘選軌的入口）
# ---------------------------------------------------------------------------
def test_parse_each_supported_profile_url():
    assert parse_seller_url(
        "https://auctions.yahoo.co.jp/seller/63s28BcHAAtTbeYJosP5enfJReuFc"
    ) == "buyee_yahoo:63s28BcHAAtTbeYJosP5enfJReuFc"
    assert parse_seller_url(
        "https://paypayfleamarket.yahoo.co.jp/user/p10376874"
    ) == "buyee_paypay:p10376874"
    assert parse_seller_url(
        "https://jp.mercari.com/user/profile/448657621"
    ) == "buyee_mercari:448657621"
    assert parse_seller_url(
        "https://www.ebay.com/usr/collectiblemore"
    ) == "ebay:collectiblemore"


def test_parse_tw_mercari_maps_into_buyee_mercari():
    """tw.mercari 收進 **buyee_mercari**，不是自成一站。

    Mercari 台灣與日本共用同一個賣家 ID 空間（seller_seed.py 2026-08-04 的
    觀察註記），而能實際列舉在售商品的管道是 Buyee 的 Mercari 鏡像
    （2026-08-09 生產路徑實測）。收進 `mercari_tw` 會製造一個永遠掃不到的
    孤兒鍵；收進 `buyee_mercari` 則與列舉實作同鍵，原站連結模板也天然可用。
    """
    assert parse_seller_url(
        "https://tw.mercari.com/zh-hant/seller/448657621"
    ) == "buyee_mercari:448657621"
    assert parse_seller_url(
        "https://tw.mercari.com/seller/448657621"
    ) == "buyee_mercari:448657621"
    # 解析出來的鍵要能連回原站賣家頁（jp.mercari 模板既有、curl 實測過）。
    assert seller_page_url(
        parse_seller_url("https://tw.mercari.com/zh-hant/seller/448657621")
    ) == "https://jp.mercari.com/user/profile/448657621"


def test_parse_strips_query_string_and_fragment():
    assert parse_seller_url(
        "https://www.ebay.com/usr/merrycorp?_trksid=p2047675.m3561.l2559#reviews"
    ) == "ebay:merrycorp"


def test_parse_url_decodes_the_seller_id():
    assert parse_seller_url(
        "https://auctions.yahoo.co.jp/seller/abc%2Bdef"
    ) == "buyee_yahoo:abc+def"


def test_parse_tolerates_a_trailing_slash():
    assert parse_seller_url(
        "https://paypayfleamarket.yahoo.co.jp/user/p10376874/"
    ) == "buyee_paypay:p10376874"


def test_ebay_store_url_raises_and_points_to_usr():
    """/str/ 的 slug ≠ Browse API 的 username，**不猜**——猜錯會長期追蹤一個
    不存在的賣家而毫無錯誤訊息。訊息要指路 /usr/。"""
    with pytest.raises(SellerUrlError) as exc:
        parse_seller_url("https://www.ebay.com/str/merrycorporation")
    assert "/str/" in str(exc.value) and "/usr/" in str(exc.value)


def test_unrecognized_url_raises_listing_supported_forms():
    """認不得就拋，**不回 None 假裝成功**（CLAUDE.md 第五節）。
    訊息要列出支援形式，使用者才知道下一步貼什麼。"""
    for bad in (
        "https://example.com/seller/x",
        "https://auctions.yahoo.co.jp/item/x123",   # 商品頁不是賣家頁
        "https://jp.mercari.com/item/m123",
        "not-a-url",
        "",
    ):
        with pytest.raises(SellerUrlError) as exc:
            parse_seller_url(bad)
        assert "auctions.yahoo.co.jp/seller" in str(exc.value)


def test_lookalike_path_segments_do_not_match():
    """逐段比對不會被子字串偽造：/reseller/ 不是 /seller/。"""
    with pytest.raises(SellerUrlError):
        parse_seller_url("https://auctions.yahoo.co.jp/reseller/abc")
    with pytest.raises(SellerUrlError):
        parse_seller_url("https://www.ebay.com/usrx/abc")
