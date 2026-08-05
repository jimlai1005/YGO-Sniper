"""訊號判定測試。"""

import dataclasses
from collections import Counter
from datetime import UTC, datetime, timedelta

from conftest import make_listing

from ygo_sniper.domain import CardInfo, CompStats, Currency, Flag, Grader, Site
from ygo_sniper.scoring import evaluate, is_triggered


def comps(median=None, n=0, p25=None, p40=None, conf="low"):
    return CompStats(
        n=n, median_twd=median, p25_twd=p25, p40_twd=p40, p75_twd=None,
        window_days=90, confidence=conf,
    )


def card():
    return CardInfo(grader=Grader.PSA, grade=10, in_era=True, era_evidence=["jp_kw:初期"])


def test_free_card_triggers_below_grading_fee(cfg, fx):
    """便宜到到手成本低於鑑定費 —— 這是最高優先級的訊號。"""
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(), cfg, fx)
    assert sig is not None
    assert Flag.FREE_CARD in sig.flags
    assert sig.best_route.landed_twd < cfg.grading_fee_twd


def test_no_signal_when_nothing_interesting(cfg, fx):
    """貴又沒行情資料 → 不該推播。安靜比吵鬧重要。"""
    lst = make_listing(price=50000, ships_to_tw=True)
    assert evaluate(lst, card(), comps(), cfg, fx) is None


def test_discount_requires_comps(cfg, fx):
    lst = make_listing(price=8000, ships_to_tw=True)
    assert evaluate(lst, card(), comps(), cfg, fx) is None

    sig = evaluate(lst, card(), comps(median=6000, n=12, conf="high"), cfg, fx)
    assert sig is not None
    assert Flag.DISCOUNT in sig.flags
    assert sig.discount_pct > 0.25


def test_shipping_kills_it_flag(cfg, fx):
    """卡本身在 P25 以下，但到手成本衝到 P40 以上 → 運費吃掉優勢。"""
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(median=500, n=10, p25=90, p40=120, conf="high"),
                   cfg, fx)
    assert sig is not None
    assert Flag.SHIPPING_KILLS_IT in sig.flags


def test_needs_shipping_ask_when_unknown(cfg, fx):
    lst = make_listing(price=20, site=Site.EBAY, currency=Currency.USD,
                       shipping_cost=None, ships_to_tw=None)
    sig = evaluate(lst, card(), comps(median=3000, n=10, conf="high"), cfg, fx)
    assert sig is not None
    assert Flag.NEEDS_SHIPPING_ASK in sig.flags


def test_offer_chance_for_stale_ebay_listing(cfg, fx):
    old = datetime.now(UTC) - timedelta(days=60)
    lst = make_listing(price=25, site=Site.EBAY, currency=Currency.USD,
                       shipping_cost=10, ships_to_tw=True,
                       best_offer_enabled=True, listed_at=old)
    sig = evaluate(lst, card(), comps(median=2000, n=8, conf="medium"), cfg, fx)
    assert sig is not None
    assert Flag.OFFER_CHANCE in sig.flags


def test_fresh_listing_gets_no_offer_flag(cfg, fx):
    fresh = datetime.now(UTC) - timedelta(days=2)
    lst = make_listing(price=25, site=Site.EBAY, currency=Currency.USD,
                       shipping_cost=10, ships_to_tw=True,
                       best_offer_enabled=True, listed_at=fresh)
    sig = evaluate(lst, card(), comps(median=3000, n=8, conf="medium"), cfg, fx)
    assert sig is not None
    assert Flag.OFFER_CHANCE not in sig.flags


def test_bundle_ask_when_same_seller_multiple_hits(cfg, fx):
    lst = make_listing(price=300, seller_id="seller_a", ships_to_tw=True)
    sig = evaluate(lst, card(), comps(), cfg, fx,
                   seller_counts=Counter({"seller_a": 3}))
    assert sig is not None
    assert Flag.NEEDS_BUNDLE_ASK in sig.flags


def test_suspicious_cheap_flag(cfg, fx):
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(median=20000, n=15, p25=15000, conf="high"),
                   cfg, fx)
    assert sig is not None
    assert Flag.SUSPICIOUS_CHEAP in sig.flags


def test_confidence_scales_score(cfg, fx):
    """同樣的折價幅度，行情樣本多的應該排前面。

    這條規則是刻意的：折 60% 但只有一筆 comp，很可能是我簽章比對錯了。
    """
    lst = make_listing(price=3000, ships_to_tw=True)
    high = evaluate(lst, card(), comps(median=9000, n=20, conf="high"), cfg, fx)
    low = evaluate(lst, card(), comps(median=9000, n=2, conf="low"), cfg, fx)
    assert high.score > low.score


def test_thin_comps_flag(cfg, fx):
    lst = make_listing(price=300, ships_to_tw=True)
    sig = evaluate(lst, card(), comps(n=1), cfg, fx)
    assert Flag.THIN_COMPS in sig.flags


# ---------------------------------------------------------------------------
# keep_all：Telegram 時代的閘門 vs dashboard 時代的清單
#
# 這道閘門原本是為了「不要洗版推播」。改走 dashboard 之後洗版成本消失，
# 使用者要的是「符合的都列出來，我自己決定要不要略過」。
# ---------------------------------------------------------------------------
def test_keep_all_returns_untriggered_candidate(cfg, fx):
    """同一筆標的：預設被丟掉，keep_all 時留下來。"""
    lst = make_listing(price=50000, ships_to_tw=True)
    assert evaluate(lst, card(), comps(), cfg, fx) is None

    sig = evaluate(lst, card(), comps(), cfg, fx, keep_all=True)
    assert sig is not None
    assert not is_triggered(sig.flags), "這筆本來就沒有 trigger 旗標，keep_all 不該憑空生一個"
    assert sig.score >= 0


def test_keep_all_does_not_change_flags_or_score(cfg, fx):
    """⚠️ keep_all 只改『留不留』，絕不改『值多少』。

    分數會因為顯示管道而變的話，排序就沒有意義——而排序正是這個清單唯一的
    篩選手段（一次上百筆）。
    """
    lst = make_listing(price=3000, ships_to_tw=True)
    st = comps(median=9000, n=20, conf="high")
    gated = evaluate(lst, card(), st, cfg, fx)
    kept = evaluate(lst, card(), st, cfg, fx, keep_all=True)

    assert gated is not None and kept is not None
    assert gated.score == kept.score
    assert gated.flags == kept.flags
    assert gated.reason == kept.reason


def test_keep_all_still_drops_listings_with_no_route(cfg, fx):
    """keep_all 不是『什麼都留』：算不出任何運送路徑的標的仍然沒有意義。"""
    lst = make_listing(price=300, site=Site.EBAY, currency=Currency.USD)
    no_routes = dataclasses.replace(cfg, routes={})
    assert evaluate(lst, card(), comps(), no_routes, fx, keep_all=True) is None


def test_is_triggered_accepts_flags_and_strings():
    """db 存的是字串、scoring 手上是 Flag——同一個判準要能吃兩種，
    否則 dashboard 與評分閘門會各自維護一份清單然後漂掉。"""
    assert is_triggered([Flag.DISCOUNT]) is True
    assert is_triggered(["discount"]) is True
    assert is_triggered(["thin_comps", "needs_shipping_ask"]) is False
    assert is_triggered([]) is False


def test_shipped_config_keeps_all_candidates(cfg):
    """出貨設定：預設留下全部候選（使用者明確要求）。"""
    assert cfg.scoring["keep_all_candidates"] is True
