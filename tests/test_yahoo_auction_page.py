"""Yahoo 商品頁（含已結束）快照解析。fixture 是真結標頁（2026-07-01 ¥6,350）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from ygo_sniper.yahoo_auction_page import (
    AuctionPageError,
    fetch_auction_snapshot,
    parse_auction_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = (FIXTURES / "yahoo_closed_n1235105710.html").read_text(encoding="utf-8")
URL = "https://auctions.yahoo.co.jp/jp/auction/n1235105710"


def test_parse_closed_auction_snapshot():
    snap = parse_auction_page(PAGE, url=URL)
    assert snap.url == URL
    assert snap.title.startswith("【ARS10】魔法の筒")
    assert snap.price == 6350
    assert snap.currency == "JPY"
    assert snap.end_time == "2026-07-01T22:53:03+09:00"
    assert snap.bids == 15
    assert snap.status == "closed"
    assert snap.seller_id == "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"
    assert snap.seller_name == "Natural Cards"


def test_parse_raises_loudly_without_next_data():
    with pytest.raises(AuctionPageError):
        parse_auction_page("<html><body>WAF page</body></html>")


def test_fetch_uses_the_injected_fetcher():
    class FakeFetcher:
        def get(self, url, **kw):
            assert url == URL
            return PAGE

    snap = fetch_auction_snapshot(URL, fetcher=FakeFetcher())
    assert snap.price == 6350
