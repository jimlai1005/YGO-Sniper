from ygo_sniper.seller_links import seller_page_url


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
