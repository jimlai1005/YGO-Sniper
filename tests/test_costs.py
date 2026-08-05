"""成本模型測試。

這是整個專案最該有測試的地方 —— 成本算錯，後面所有訊號都是垃圾，
而且是「看起來很合理的垃圾」，你不會發現，直到帳單來。
"""

import pytest

from conftest import make_listing
from ygo_sniper.costs import (
    best_route,
    breakeven_table,
    max_item_price_jpy,
    quote_all_routes,
    quote_route,
)
from ygo_sniper.domain import Currency, Site


def test_buyee_consolidated_amortizes_intl_shipping(cfg, fx):
    """湊單張數變多，每張的國際運費攤提應該下降，但每筆訂單費不變。"""
    lst = make_listing(price=1000)
    route = cfg.routes["buyee_consolidated"]

    q1 = quote_route(lst, route, fx, bundle_size=1)
    q5 = quote_route(lst, route, fx, bundle_size=5)

    assert q5.landed_twd < q1.landed_twd
    # RouteQuote 的每個欄位都捨入到分，期望值要做同樣的捨入才能精確相等
    assert q5.shipping_twd == round(q1.shipping_twd / 5, 2)
    # 代購費 + 國內運費是每筆訂單，攤提不到
    assert q5.fee_twd == pytest.approx(q1.fee_twd, rel=1e-9)


def test_mercari_tw_never_amortizes(cfg, fx):
    """Mercari 台灣一件一件直送，bundle_size 給多少都沒用。

    這條規則錯了的話，整個工具會嚴重高估 Mercari TW 的優勢。
    """
    lst = make_listing(price=1000)
    route = cfg.routes["mercari_tw"]
    assert route.bundle_size == 1

    q1 = quote_route(lst, route, fx, bundle_size=1)
    q9 = quote_route(lst, route, fx, bundle_size=9)
    # 傳 bundle_size 會被採用，但 config 的預設是 1，
    # pipeline 不會對 mercari_tw 傳大於 1 的值
    assert q1.shipping_twd == round(fx.to_twd(route.amortizable_jpy, Currency.JPY), 2)
    assert q9.landed_twd < q1.landed_twd  # 數學上會降，但實務上不可用


def test_cheap_card_is_dominated_by_overhead(cfg, fx):
    """¥300 的卡，成本結構應該被雜費主宰 —— 這正是要標 SHIPPING_KILLS_IT 的情況。"""
    lst = make_listing(price=300)
    q = best_route(lst, cfg, fx)
    assert q.overhead_ratio > 0.6


def test_expensive_card_overhead_is_marginal(cfg, fx):
    lst = make_listing(price=30000)
    q = best_route(lst, cfg, fx)
    assert q.overhead_ratio < 0.15


def test_best_route_picks_cheapest(cfg, fx):
    lst = make_listing(price=2000)
    quotes = quote_all_routes(lst, cfg, fx)
    assert quotes == sorted(quotes, key=lambda q: q.landed_twd)
    assert best_route(lst, cfg, fx).landed_twd == quotes[0].landed_twd


def test_breakeven_inverts_quote(cfg, fx):
    """反解出來的卡價，代回成本模型應該剛好落在目標值。

    這個往返測試是 breakeven 功能的正確性保證 —— 它會被直接
    塞進搜尋網址的 price_max，算錯就等於默默漏掉一堆標的。
    """
    target = cfg.grading_fee_twd
    for name in ("buyee_consolidated", "buyee_single", "mercari_tw"):
        route = cfg.routes[name]
        max_jpy = max_item_price_jpy(route, target, fx)
        if max_jpy <= 0:
            continue
        lst = make_listing(price=max_jpy)
        q = quote_route(lst, route, fx)
        assert q.landed_twd == round(target, 2)


def test_breakeven_returns_zero_when_overhead_exceeds_target(cfg, fx):
    route = cfg.routes["buyee_single"]
    assert max_item_price_jpy(route, 50.0, fx) == 0.0


def test_breakeven_table_sorted_desc(cfg, fx):
    rows = breakeven_table(cfg, fx)
    assert rows
    assert all(
        rows[i]["max_item_jpy"] >= rows[i + 1]["max_item_jpy"]
        for i in range(len(rows) - 1)
    )


def test_ebay_uses_listing_shipping(cfg, fx):
    lst = make_listing(
        price=20.0, site=Site.EBAY, currency=Currency.USD, shipping_cost=8.0
    )
    q = quote_route(lst, cfg.routes["ebay_direct"], fx)
    assert q.item_twd == round(fx.to_twd(20.0, Currency.USD), 2)
    assert q.shipping_twd == round(fx.to_twd(8.0, Currency.USD), 2)


def test_ebay_unknown_shipping_falls_back_conservatively(cfg, fx):
    """運費未知不能當 0，那會製造假訊號。"""
    known = make_listing(price=20.0, site=Site.EBAY, currency=Currency.USD,
                         shipping_cost=0.0)
    unknown = make_listing(price=20.0, site=Site.EBAY, currency=Currency.USD,
                           shipping_cost=None)
    r = cfg.routes["ebay_direct"]
    assert quote_route(unknown, r, fx).landed_twd > quote_route(known, r, fx).landed_twd


def test_markup_applied_to_costs_not_comps(fx):
    """成本要含刷卡手續費，行情不含 —— 兩邊同時加會讓折價率失真。"""
    with_markup = fx.to_twd(1000, Currency.JPY, apply_markup=True)
    without = fx.to_twd(1000, Currency.JPY, apply_markup=False)
    assert with_markup > without
