"""運費佔比告警：**不需要行情樣本**也能判斷的那一條。

為什麼要有這組測試（真實的缺口）：`SHIPPING_KILLS_IT` 在有 comps 時走的是
p25／p40 比較，一筆「商品 US$30、國際運費 US$32」的 eBay 標的只要行情中位數
夠高就完全不會被標起來——但它在結構上就不可能划算（一半的錢在買運送）。
comps 要等樣本累積，成本結構現在就看得見，所以這條規則不能綁在行情上。
"""

import dataclasses

import pytest
from conftest import make_listing

from ygo_sniper.domain import CardInfo, CompStats, Currency, Flag, Grader, RouteQuote, Site
from ygo_sniper.scoring import (
    evaluate,
    overhead_alert,
    overhead_threshold,
    shipping_alert_for_row,
)


def comps(median=None, n=0, p25=None, p40=None, conf="low"):
    return CompStats(
        n=n, median_twd=median, p25_twd=p25, p40_twd=p40, p75_twd=None,
        window_days=90, confidence=conf,
    )


def card():
    return CardInfo(grader=Grader.PSA, grade=10, in_era=True, era_evidence=["jp_kw:初期"])


def quote(item, fee, ship):
    return RouteQuote(
        route="r", label="路徑", landed_twd=round(item + fee + ship, 2),
        item_twd=item, fee_twd=fee, shipping_twd=ship, bundle_size=1,
    )


# ---------------------------------------------------------------------------
# 判準本身
# ---------------------------------------------------------------------------
def test_overhead_alert_fires_at_threshold(cfg):
    """門檻是「大於等於」：50/50 的標的必須被標出來。"""
    th = overhead_threshold(cfg)
    assert 0 < th <= 1
    assert overhead_alert(quote(50.0, 0.0, 50.0), cfg) is not None
    assert overhead_alert(quote(99.0, 0.0, 1.0), cfg) is None


def test_overhead_alert_reports_the_numbers_it_used(cfg):
    a = overhead_alert(quote(30.0, 10.0, 60.0), cfg)
    assert a is not None
    assert a["ratio"] == pytest.approx(0.70)
    assert a["overhead_twd"] == pytest.approx(70.0)
    assert a["item_twd"] == pytest.approx(30.0)
    assert a["threshold"] == overhead_threshold(cfg)


def test_overhead_threshold_rejects_garbage_and_falls_back(cfg):
    """門檻被打錯不可以讓工具安靜地變寬鬆。"""
    from ygo_sniper.scoring import DEFAULT_OVERHEAD_RATIO_ALERT

    for bad in ("abc", -0.2, 0.0, 5):
        broken = dataclasses.replace(cfg, scoring={**cfg.scoring, "overhead_ratio_alert": bad})
        assert overhead_threshold(broken) == DEFAULT_OVERHEAD_RATIO_ALERT


def test_shipped_config_has_overhead_threshold(cfg):
    """出貨設定真的有這個鍵（不是靠程式碼裡的預設值撐著）。"""
    assert cfg.scoring["overhead_ratio_alert"] == pytest.approx(0.40)
    assert cfg.scoring["p_worth_hide_default"] == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# 與 comps 的關係：這才是這條規則存在的理由
# ---------------------------------------------------------------------------
def test_high_overhead_fires_without_any_comps(cfg, fx):
    """完全沒有行情樣本（n=0、沒有分位數）照樣標得出來。"""
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(), cfg, fx, keep_all=True)
    assert sig is not None
    assert sig.comps.n == 0 and sig.comps.p25_twd is None
    assert Flag.HIGH_OVERHEAD in sig.flags
    assert sig.best_route.overhead_ratio >= overhead_threshold(cfg)


def test_high_overhead_fires_where_the_comps_rule_stays_silent(cfg, fx):
    """使用者的實例：eBay 單張直寄，商品 US$30、國際運費 US$32。

    行情中位數夠高（NT$8,000）時 `SHIPPING_KILLS_IT` 完全不會觸發——
    卡價確實在 P25 以下，但到手成本也還在 P40 以下，那條規則說「這很划算」。
    可是這一單有一半的錢在買運送，結構上就吃掉了大半的空間。
    """
    lst = make_listing(price=30, site=Site.EBAY, currency=Currency.USD,
                       shipping_cost=32, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(median=8000, n=12, p25=4000, p40=5000, conf="high"),
                   cfg, fx, keep_all=True)
    assert sig is not None
    assert Flag.SHIPPING_KILLS_IT not in sig.flags, "前提：舊規則在這裡是沉默的"
    assert Flag.HIGH_OVERHEAD in sig.flags
    assert sig.best_route.overhead_ratio == pytest.approx(32 / 62, abs=1e-6)
    assert "不需行情即可判斷" in sig.reason


def test_two_shipping_rules_are_distinguishable(cfg, fx):
    """兩者可以並存，而且 reason 要講得出是哪一種觸發。"""
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(median=500, n=10, p25=90, p40=120, conf="high"),
                   cfg, fx, keep_all=True)
    assert Flag.SHIPPING_KILLS_IT in sig.flags
    assert Flag.HIGH_OVERHEAD in sig.flags
    assert "行情 P25／P40 比較" in sig.reason
    assert "不需行情即可判斷" in sig.reason


def test_comps_rule_says_structural_when_it_has_no_comps(cfg, fx):
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(), cfg, fx, keep_all=True)
    assert Flag.SHIPPING_KILLS_IT in sig.flags
    assert "無行情，純看成本結構" in sig.reason


def test_auction_never_gets_high_overhead(cfg, fx):
    """競標的目前出價會漲，用它算出來的佔比描述的是一個不會發生的世界。"""
    lst = make_listing(price=300, site=Site.BUYEE_YAHOO, ships_to_tw=True,
                       raw={"price_kind": "current_bid"})
    sig = evaluate(lst, card(), comps(), cfg, fx, keep_all=True)
    assert sig is not None
    assert Flag.LIVE_AUCTION in sig.flags
    assert Flag.HIGH_OVERHEAD not in sig.flags


def test_high_overhead_does_not_change_score(cfg, fx):
    """佔比高 ≠ 不划算：到手 NT$294 的卡有 98% 是雜費，但它低於鑑定費。

    這個旗標只負責讓人看見錢花在哪，一旦扣分它就變成第二個折價判準了。
    """
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(), cfg, fx, keep_all=True)
    loose = dataclasses.replace(cfg, scoring={**cfg.scoring, "overhead_ratio_alert": 0.99})
    without = evaluate(lst, card(), comps(), loose, fx, keep_all=True)
    assert Flag.HIGH_OVERHEAD in sig.flags and Flag.HIGH_OVERHEAD not in without.flags
    assert sig.score == without.score


# ---------------------------------------------------------------------------
# dashboard 走的那條路：db 的列 → 同一個判準
# ---------------------------------------------------------------------------
def test_row_alert_matches_the_scan_time_flag(cfg, fx):
    """舊資料沒有 high_overhead 旗標也要標得出來，而且數字要跟掃描當下一致。"""
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(), cfg, fx, keep_all=True)
    payload = sig.to_dict()
    row = {"flags": [f.value for f in sig.flags if f is not Flag.HIGH_OVERHEAD],
           "payload": payload}

    alert = shipping_alert_for_row(row, cfg)
    assert alert is not None
    assert alert["ratio"] == pytest.approx(sig.best_route.overhead_ratio)
    assert alert["landed_twd"] == pytest.approx(sig.best_route.landed_twd)


def test_row_alert_skips_live_auctions(cfg, fx):
    row = {
        "flags": ["live_auction"],
        "payload": {"best_route": quote(10.0, 40.0, 50.0).to_dict()},
    }
    assert shipping_alert_for_row(row, cfg) is None


def test_row_alert_survives_a_broken_payload(cfg):
    """一列壞掉的 payload 不可以把整個清單打掉。"""
    assert shipping_alert_for_row({"flags": [], "payload": {}}, cfg) is None
    assert shipping_alert_for_row({"flags": [], "payload": {"best_route": {}}}, cfg) is None
    assert shipping_alert_for_row({}, cfg) is None


# ---------------------------------------------------------------------------
# 序列化：dashboard 的成本拆解要看得到佔比，而且只能有一份定義
# ---------------------------------------------------------------------------
def test_route_quote_serialises_overhead(cfg, fx):
    q = quote(40.0, 10.0, 50.0)
    d = q.to_dict()
    assert d["overhead_twd"] == pytest.approx(60.0)
    assert d["overhead_ratio"] == pytest.approx(0.60)
    # 算出來的欄位不會被灌回去，from_dict 之後重新算
    back = RouteQuote.from_dict(d)
    assert back == q
    assert back.overhead_ratio == pytest.approx(0.60)


def test_signal_payload_carries_overhead_for_every_route(cfg, fx):
    lst = make_listing(price=1200, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(), cfg, fx, keep_all=True)
    d = sig.to_dict()
    assert d["best_route"]["overhead_ratio"] is not None
    assert len(d["all_routes"]) == len(sig.all_routes)
    for route in d["all_routes"]:
        assert "overhead_ratio" in route and "overhead_twd" in route
        # 三項相加要等於到手成本（拆解畫面就是照這個排的）
        total = route["item_twd"] + route["fee_twd"] + route["shipping_twd"]
        assert total == pytest.approx(route["landed_twd"], abs=0.02)
