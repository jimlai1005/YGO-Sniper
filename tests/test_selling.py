"""賣方成本模型與跨平台淨價差測試。

這裡最該守住的不是算術（算術錯了會馬上被發現），是**兩個結構性紅線**：
  1. 兩個不同物理位置的價格不准直接相減（貨在台灣就不能在 Mercari JP 賣）；
  2. 送不到的地方成本不准是 0——「不可行」與「免費」必須是兩種不同的答案。
兩者錯掉的方向都是「看起來有套利空間」，也就是會讓人真的下單的方向。
"""

from dataclasses import replace

import pytest
from conftest import make_listing

from ygo_sniper.config import SellVenueConfig, TransferConfig
from ygo_sniper.domain import Currency, HoldingLocation, Site
from ygo_sniper.selling import (
    breakeven_sell_price_native,
    feasibility_matrix,
    net_proceeds,
    round_trip,
    round_trips_for,
    sell_price_for,
    transfer_quote,
)


# ---------------------------------------------------------------------------
# 測試用的小工具
# ---------------------------------------------------------------------------
def with_jp_presence(cfg, enabled=True):
    """打開／關掉「日本收款身分」。

    很多結構性判斷（回送日本不可行）平常被身分閘門擋在前面看不到，
    要驗那一層就得先把前面那道門打開——否則測到的是身分閘門，不是地點模型。
    """
    return replace(cfg, resale=replace(cfg.resale, jp_presence=enabled))


def buy_quote(cfg, fx, *, route="buyee_consolidated", price=10000, site=Site.BUYEE_YAHOO):
    from ygo_sniper.costs import quote_route

    return quote_route(make_listing(price=price, site=site), cfg.routes[route], fx)


class FakeEstimate:
    def __init__(self, lo, fair, hi=None):
        self.lo_twd = lo
        self.fair_twd = fair
        self.hi_twd = hi
        self.level_label = "L1 同卡"
        self.n_effective = 3


# ---------------------------------------------------------------------------
# A. 各賣場費率計算
# ---------------------------------------------------------------------------
def test_mercari_jp_fee_breakdown(cfg, fx):
    """メルカリ ¥10,000：抽成 10% ＋ 運費 300 ＋ 提領 200 ＋ 匯回 500（湊 10 張）。"""
    v = cfg.resale.venues["mercari_jp"]
    q = net_proceeds(10000, v, cfg, fx)

    assert q.ok
    assert q.commission_native == pytest.approx(1000)
    assert q.shipping_native == pytest.approx(300)
    assert q.payout_fee_native == pytest.approx(200)
    assert q.remit_fee_native == pytest.approx(500)      # 5000 / batch 10
    assert q.net_native == pytest.approx(8000)
    # 收入側走中價（0.21），再扣 2% 匯差
    assert q.net_twd == pytest.approx(8000 * 0.21 * 0.98, rel=1e-9)
    assert q.gross_twd == pytest.approx(2100)
    assert q.take_rate == pytest.approx(1646.4 / 2100, rel=1e-6)


def test_yahoo_furima_has_lowest_commission(cfg, fx):
    """Yahoo!フリマ 5%（實地查證）低於 Mercari／ヤフオク 的 10%。"""
    price = 20000
    furima = net_proceeds(price, cfg.resale.venues["yahoo_furima"], cfg, fx)
    mercari = net_proceeds(price, cfg.resale.venues["mercari_jp"], cfg, fx)
    yahoo = net_proceeds(price, cfg.resale.venues["yahoo_auction"], cfg, fx)

    assert furima.commission_native == pytest.approx(price * 0.05)
    assert mercari.commission_native == pytest.approx(price * 0.10)
    assert yahoo.commission_native == pytest.approx(price * 0.10)
    assert furima.net_twd > mercari.net_twd


def test_twd_venue_has_no_fx_cost(cfg, fx):
    """台幣賣場不換匯：匯差與匯回手續費都必須是 0，不是「很小」。"""
    q = net_proceeds(3000, cfg.resale.venues["shopee_tw"], cfg, fx)
    assert q.ok
    assert q.fx_haircut_twd == 0.0
    assert q.remit_fee_native == 0.0
    # 5.5% 成交 + 2.5% 金流
    assert q.net_native == pytest.approx(3000 * (1 - 0.055 - 0.025))
    assert q.net_twd == pytest.approx(2760)


def test_ebay_usd_fees(cfg, fx):
    """eBay US$100：13.25% ＋ 1.65% 國際費 ＋ US$0.40 訂單費 ＋ US$15 運費，再扣 3% 換匯。"""
    q = net_proceeds(100, cfg.resale.venues["ebay_us"], cfg, fx)
    assert q.ok
    assert q.net_native == pytest.approx(100 - 13.25 - 1.65 - 0.40 - 15.0)
    assert q.net_twd == pytest.approx(69.70 * 31.5 * 0.97, abs=0.01)


def test_selling_below_fixed_costs_is_refused(cfg, fx):
    """賣價低到扣完固定費用是負的 → ok=False，且 net_twd 是 None 不是 0。"""
    q = net_proceeds(500, cfg.resale.venues["mercari_jp"], cfg, fx)
    assert not q.ok
    assert q.net_twd is None
    assert "負" in q.reason


def test_remit_batch_amortises(cfg, fx):
    """匯回台灣是「每次匯款」的成本，湊越多張攤提越低——但抽成不會變。"""
    v = cfg.resale.venues["mercari_jp"]
    one = net_proceeds(10000, v, cfg, fx, remit_batch_size=1)
    ten = net_proceeds(10000, v, cfg, fx, remit_batch_size=10)

    assert one.remit_fee_native == pytest.approx(5000)
    assert ten.remit_fee_native == pytest.approx(500)
    assert one.commission_native == ten.commission_native
    assert ten.net_twd > one.net_twd


def test_revenue_never_uses_card_markup(cfg, fx):
    """**收入側絕不套刷卡加成**（工程原則 1 的方向問題）。

    `fx.to_twd(..., apply_markup=True)` 會把金額抬高 3.5%——那對成本是保守的，
    對收入卻是往「賺更多」的方向錯。這條測試釘死收入永遠走中價。
    """
    v = cfg.resale.venues["mercari_jp"]
    q = net_proceeds(10000, v, cfg, fx)
    inflated = fx.to_twd(10000, Currency.JPY, apply_markup=True)
    assert q.gross_twd == pytest.approx(fx.to_twd(10000, Currency.JPY, apply_markup=False))
    assert q.gross_twd < inflated


# ---------------------------------------------------------------------------
# B. 持有地點：不可行組合的判定
# ---------------------------------------------------------------------------
def test_goods_in_taiwan_cannot_be_sold_on_japanese_venue(cfg, fx):
    """【本任務的核心】貨在台灣 → Mercari JP：**不可行**，不是成本 0。

    Buyee 集運把貨送到台灣（destination=tw_home），Mercari JP 要求貨在日本
    （location=jp_warehouse）。這裡刻意把「日本收款身分」打開，讓身分閘門
    不再擋路——測的是**地點模型本身**：即使你有日本帳號，貨已經在台灣，
    回送日本這條路在設定裡被標成 feasible=false，所以整個組合不可行。
    """
    c = with_jp_presence(cfg)                      # 身分不是這條測試的爭點
    q = buy_quote(c, fx)                           # Buyee 集運 → 貨落台灣
    assert c.routes["buyee_consolidated"].destination == HoldingLocation.TW_HOME.value

    trip = round_trip(
        buy_quote=q,
        holding=HoldingLocation.TW_HOME.value,
        site=Site.BUYEE_YAHOO.value,
        sell_venue=c.resale.venues["mercari_jp"],
        sell_price_native=30000,                   # 給一個很誘人的賣價
        cfg=c, fx=fx,
    )

    assert not trip.ok
    # 不可行 = 沒有數字。給一個負數會讓它看起來像「算過了、只是不划算」。
    assert trip.net_profit_twd is None
    assert trip.transfer_twd is None
    assert trip.transfer is not None and not trip.transfer.ok
    # 理由要講得出「為什麼」，不是只說 false
    assert "回送日本" in trip.reason or "國際運費" in trip.reason
    assert "台灣" in trip.reason and "日本" in trip.reason


def test_transfer_tw_to_jp_is_infeasible_not_free(cfg, fx):
    """`tw_home → jp_warehouse` 的成本是 **None**，不是 0.0。"""
    tq = transfer_quote(
        HoldingLocation.TW_HOME.value, HoldingLocation.JP_WAREHOUSE.value, cfg, fx
    )
    assert not tq.ok
    assert tq.cost_twd is None
    assert tq.reason


def test_unconfigured_transfer_is_infeasible_not_free(cfg, fx):
    """設定裡沒寫的路線一律不可行。**「沒設定」不等於「免費」**。"""
    tq = transfer_quote("jp_warehouse", "mars_colony", cfg, fx)
    assert not tq.ok
    assert tq.cost_twd is None
    assert "不可行" in tq.reason


def test_same_location_transfer_is_the_only_zero(cfg, fx):
    """唯一一個 0 是對的情況：貨已經在賣場所在地。"""
    tq = transfer_quote(
        HoldingLocation.TW_HOME.value, HoldingLocation.TW_HOME.value, cfg, fx
    )
    assert tq.ok and tq.cost_twd == 0.0


def test_jp_to_tw_transfer_costs_money_and_amortises(cfg, fx):
    """日本 → 台灣是可行的，但**要錢**，而且可以湊單攤提。"""
    single = transfer_quote("jp_warehouse", "tw_home", cfg, fx, bundle_size=1)
    bundled = transfer_quote("jp_warehouse", "tw_home", cfg, fx, bundle_size=5)
    assert single.ok and single.cost_twd > 0
    assert bundled.cost_twd == pytest.approx(single.cost_twd / 5, rel=1e-6)


def test_jp_venue_blocked_without_jp_presence(cfg, fx):
    """沒有日本收款身分時，日本賣場一律不可行——而且理由要說得出是身分問題。"""
    assert cfg.resale.jp_presence is False        # 這是目前的真實狀態
    q = buy_quote(cfg, fx)
    trip = round_trip(
        buy_quote=q, holding=HoldingLocation.TW_HOME.value, site=Site.BUYEE_YAHOO.value,
        sell_venue=cfg.resale.venues["mercari_jp"], sell_price_native=30000, cfg=cfg, fx=fx,
    )
    assert not trip.ok
    assert "日本收款身分" in trip.reason
    assert trip.net_profit_twd is None


def test_jp_hold_route_is_not_in_buy_side_routes(cfg):
    """「貨留日本」那條路徑**不准出現在 `routes:` 裡**。

    一旦進去，`costs.best_route()` 會挑到它（它沒有國際運費，永遠最便宜），
    整個買方模型的到手成本會被系統性低估，而且方向是「看起來更划算」。
    """
    assert cfg.resale.jp_hold_route is not None
    assert cfg.resale.jp_hold_route.name not in cfg.routes
    assert cfg.resale.jp_hold_route.destination == HoldingLocation.JP_WAREHOUSE.value


def test_venue_without_market_data_refuses_to_quote(cfg, fx):
    """沒有成交樣本的賣場（台灣賣場、eBay）**拒絕給淨利**，不猜一個數字。"""
    for name in ("shopee_tw", "ruten_tw", "ebay_us"):
        assert cfg.resale.venues[name].valuation_venue is None

    lst = make_listing(price=10000, site=Site.BUYEE_YAHOO)
    trips = round_trips_for(lst, cfg, fx, estimate_for=lambda v: FakeEstimate(5000, 9000))
    tw = [t for t in trips if t.sell_venue == "shopee_tw"]
    assert tw and all(not t.ok for t in tw)
    assert all(t.net_profit_twd is None for t in tw)
    assert any("成交樣本" in t.reason for t in tw)


def test_feasibility_matrix_covers_every_combination(cfg, fx):
    """盤點表要**含不可行的組合**：把它們濾掉，使用者只會反覆自己重新想一次。"""
    rows = feasibility_matrix(cfg, fx)
    n_routes = len(cfg.routes) + (1 if cfg.resale.jp_hold_route else 0)
    assert len(rows) == n_routes * len(cfg.resale.venues)
    assert all(r["why"] for r in rows)                 # 每一格都要有理由
    assert any(not r["feasible"] for r in rows)


# ---------------------------------------------------------------------------
# C. 淨價差：反解與正算同源
# ---------------------------------------------------------------------------
def test_breakeven_price_round_trips_to_zero_profit(cfg, fx):
    """**反解 → 正算 → 淨利 ≈ 0**。與 `bidding` 的正算回頭驗證同一個紀律。

    反解與正算若不同源，兩邊會安靜地分岔，而分岔的方向沒有人保證安全。
    """
    c = with_jp_presence(cfg)
    for name in ("mercari_jp", "yahoo_furima", "shopee_tw", "ebay_us"):
        v = c.resale.venues[name]
        total_cost = 2500.0
        price = breakeven_sell_price_native(total_cost, v, c, fx)
        assert price is not None and price > 0
        q = net_proceeds(price, v, c, fx)
        assert q.ok
        assert q.net_twd == pytest.approx(total_cost, abs=0.02), name


def test_round_trip_profit_is_proceeds_minus_total_cost(cfg, fx):
    """淨利的定義只有一份：實拿 − （到手成本 ＋ 送到賣場的成本）。"""
    c = with_jp_presence(cfg)
    q = buy_quote(c, fx, route="buyee_consolidated", price=8000)
    trip = round_trip(
        buy_quote=q, holding=HoldingLocation.TW_HOME.value, site=Site.BUYEE_YAHOO.value,
        sell_venue=c.resale.venues["shopee_tw"], sell_price_native=4000, cfg=c, fx=fx,
    )
    assert trip.ok
    assert trip.total_cost_twd == pytest.approx(q.landed_twd)      # 同地，運送 0
    assert trip.net_profit_twd == pytest.approx(trip.net_proceeds_twd - trip.total_cost_twd,
                                                abs=0.01)
    assert trip.roi == pytest.approx(trip.net_profit_twd / trip.total_cost_twd, rel=1e-9)


def test_transfer_cost_enters_the_denominator(cfg, fx):
    """把貨運到賣場的錢是**你掏出去的**，必須進總投入，否則報酬率虛高。"""
    c = with_jp_presence(cfg)
    hold = c.resale.jp_hold_route
    from ygo_sniper.costs import quote_route

    q = quote_route(make_listing(price=8000, site=Site.BUYEE_YAHOO), hold, fx)
    trip = round_trip(
        buy_quote=q, holding=HoldingLocation.JP_WAREHOUSE.value, site=Site.BUYEE_YAHOO.value,
        sell_venue=c.resale.venues["shopee_tw"], sell_price_native=4000, cfg=c, fx=fx,
    )
    assert trip.ok
    assert trip.transfer_twd > 0                                    # 日本 → 台灣要錢
    assert trip.total_cost_twd == pytest.approx(q.landed_twd + trip.transfer_twd, abs=0.01)


# ---------------------------------------------------------------------------
# D. 收入估計一律用區間下緣
# ---------------------------------------------------------------------------
def test_sell_price_uses_interval_lower_bound_not_point_estimate(cfg, fx):
    """收入用 `lo_twd`，**不是** `fair_twd`——與出價上限同一個哲學：寧可低估收入。"""
    v = cfg.resale.venues["mercari_jp"]
    est = FakeEstimate(lo=4200, fair=9000)
    price, source = sell_price_for(v, est, fx)

    assert price == pytest.approx(4200 / 0.21)          # 中價反換算，與 comps 落庫同源
    assert price < 9000 / 0.21
    assert "下緣" in source


def test_no_interval_means_no_number(cfg, fx):
    """沒有區間下緣就不給賣出價（紅線：不准猜）。"""
    assert sell_price_for(cfg.resale.venues["mercari_jp"], FakeEstimate(None, 9000), fx)[0] is None
    assert sell_price_for(cfg.resale.venues["mercari_jp"], None, fx)[0] is None


def test_sell_price_conversion_is_inverse_of_comps_conversion(cfg, fx):
    """`sell_price_for` 的換算必須是 comps 落庫換算的反函數（同源）。

    comps 存的是 `to_twd(native, ccy, apply_markup=False)`；估價出來的
    `lo_twd` 活在那個尺度上，所以要回到賣場幣別只能用同一個中價除回去。
    """
    v = cfg.resale.venues["mercari_jp"]
    native_original = 25000.0
    lo = fx.to_twd(native_original, Currency.JPY, apply_markup=False)
    price, _ = sell_price_for(v, FakeEstimate(lo=lo, fair=lo * 2), fx)
    assert price == pytest.approx(native_original, rel=1e-9)


# ---------------------------------------------------------------------------
# E. 設定的完整性（費率查證程度必須跟數字一起出門）
# ---------------------------------------------------------------------------
def test_every_venue_declares_verification_status(cfg):
    for name, v in cfg.resale.venues.items():
        assert v.verified, f"{name} 沒有標明費率查證程度"
        assert v.location in {m.value for m in HoldingLocation}, name
        assert v.currency in {c.value for c in Currency}, name


def test_sell_quote_carries_verification_to_the_caller(cfg, fx):
    """未查證的費率**不准長得跟查證過的一樣**——verified 要跟著數字走。"""
    q = net_proceeds(10000, cfg.resale.venues["ebay_us"], cfg, fx)
    assert "未" in q.verified                # eBay 那組是未完全查證
    assert q.source_url


def test_config_without_resale_block_yields_no_venues():
    """缺 `resale:` 區塊時是「沒有賣場」，不是「用預設費率」。

    猜一個預設費率等於憑空生出淨利——降級方向必須是拒答。
    """
    from ygo_sniper.config import _resale_from_settings

    empty = _resale_from_settings({})
    assert empty.venues == {}
    assert empty.jp_presence is False
    assert empty.jp_hold_route is None


def test_custom_venue_and_transfer_are_honoured(cfg, fx):
    """設定驅動：加一個賣場、加一條運送方案，模型就該認得（不是寫死在程式裡）。"""
    v = SellVenueConfig(
        name="test_venue", label="測試賣場", currency="JPY",
        location=HoldingLocation.JP_WAREHOUSE.value, commission_pct=0.20,
    )
    q = net_proceeds(1000, v, cfg, fx)
    assert q.ok and q.net_native == pytest.approx(800)

    c = replace(cfg, resale=replace(
        cfg.resale,
        # 放在最前面：`ResaleConfig.transfer()` 取第一個命中的方案，
        # 所以新方案要覆蓋既有的「不可行」就得排在它前面。
        transfers=[TransferConfig(frm="tw_home", to="jp_warehouse", feasible=True,
                                  cost_jpy=4000, amortizable=False, note="測試回送"),
                   *cfg.resale.transfers],
    ))
    tq = transfer_quote("tw_home", "jp_warehouse", c, fx)
    assert tq.ok and tq.cost_twd == pytest.approx(fx.to_twd(4000, Currency.JPY), abs=0.01)


def test_live_auction_has_no_round_trip(cfg, fx):
    """競標中的標的**不給淨價差**——「目前出價」不是付得出去的價格。

    這條是實測踩出來的：第一版盤點表最賺的十筆全部是 1 円起標的競標，
    到手成本 NT$63、報酬率 9,171%。那個 NT$63 會漲，拿它去減賣出實拿就是
    混源比較（工程原則 1），而且方向是「看起來爆賺」。
    加上這道閘門之後，反事實情境的正淨利佔比從 29% 掉到 0%——
    也就是說**那 29% 全部是這個 bug**。
    """
    c = with_jp_presence(cfg)
    live = make_listing(price=1, site=Site.BUYEE_YAHOO, raw={"price_kind": "current_bid"})
    trips = round_trips_for(live, c, fx, estimate_for=lambda v: FakeEstimate(9000, 15000))

    assert trips and all(not t.ok for t in trips)
    assert all(t.net_profit_twd is None for t in trips)
    assert any("競標" in t.reason for t in trips)

    # 同一張卡、同一個價格，只差沒有 price_kind → 就算得出來（證明擋的是競標，
    # 不是「便宜的標的」）。
    fixed = make_listing(price=1, site=Site.BUYEE_YAHOO)
    ok_trips = round_trips_for(fixed, c, fx, estimate_for=lambda v: FakeEstimate(9000, 15000))
    assert any(t.ok for t in ok_trips)


def test_listing_from_signal_row_restores_price_kind(cfg):
    """還原 `Listing` 時 `raw` 必須跟著回來，否則競標閘門會整批失效。"""
    import json

    from ygo_sniper.selling import listing_from_signal_row

    row = {"payload": json.dumps({"listing": {
        "site": "buyee_yahoo", "external_id": "x1", "title": "t", "url": "u",
        "price": 1000, "currency": "JPY", "raw": {"price_kind": "current_bid"},
    }})}
    lst = listing_from_signal_row(row)
    assert lst is not None
    from ygo_sniper.bidding import is_live_auction
    assert is_live_auction(lst)


def test_listing_from_broken_payload_is_none(cfg):
    """payload 殘缺回 None，**不回一個補了預設值的假 Listing**。"""
    from ygo_sniper.selling import listing_from_signal_row

    assert listing_from_signal_row({"payload": "{}"}) is None
    assert listing_from_signal_row({"payload": "not json"}) is None
