"""競標 vs 即決的分流測試。

打開 `include_live_auctions` 之後最容易出、而且**看起來像成功**的錯是：
¥1 起標的卡集體被標成 FREE_CARD／DISCOUNT（現在価格當成可成交價），
於是清單上全是「撿到寶」，而那些寶你一個也買不到。

所以這個檔案守兩個方向：
  (a) 競標標的**不得**拿到任何以到手成本為前提的旗標；
  (b) 即決／定價標的的判斷與分數**完全不受競標功能影響**（沒有退化）。
"""

from datetime import UTC, datetime

import pytest
from conftest import make_listing

from ygo_sniper.domain import CardInfo, CompStats, Currency, Flag, Grader, Site
from ygo_sniper.scoring import TRIGGER_FLAGS, evaluate, is_triggered
from ygo_sniper.valuation import Estimate


def comps(median=None, n=0, p25=None, p40=None, conf="low"):
    return CompStats(n=n, median_twd=median, p25_twd=p25, p40_twd=p40, p75_twd=None,
                     window_days=90, confidence=conf)


def card():
    return CardInfo(grader=Grader.PSA, grade=10, in_era=True, era_evidence=["jp_kw:初期"])


def est(lo=9000.0, hi=25000.0, fair=15000.0):
    return Estimate(
        fair_twd=fair, level="L1", level_label="同卡 × 同稀有度", n_effective=9,
        lo_twd=lo, hi_twd=hi, calibration_n=80, venue="buyee_yahoo", venue_adjusted=True,
        # grade 一定要給：`bidding.EvidenceGate` 的第一道閘門是「分數未知不給上限」
        # （模型會把 None 當成基準分數 9，那是一個沒說出口的假設）。
        grade=10.0, calibration_group="L1/3-9", calibration_group_n=71,
    )


def thin_est():
    """校準集不足：模型不給區間 → 不准有出價上限。"""
    return Estimate(fair_twd=15000.0, level="L3", level_label="稀有度層",
                    n_effective=2, calibration_n=4)


def auction(price, *, bids=3, end=None, **kw):
    return make_listing(
        price=price, site=Site.BUYEE_YAHOO, currency=Currency.JPY,
        external_id=kw.pop("external_id", "a1"),
        raw={"price_kind": "current_bid", "current_bid": price},
        ships_to_tw=True, bids=bids,
        end_time=end or datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        **kw,
    )


def buyout(price, **kw):
    return make_listing(
        price=price, site=Site.BUYEE_YAHOO, currency=Currency.JPY,
        raw={"price_kind": "buyout", "current_bid": 1.0}, ships_to_tw=True, **kw,
    )


# ---------------------------------------------------------------------------
# 1. 競標不得污染「買現在就走」的判斷
# ---------------------------------------------------------------------------
def test_cheap_auction_never_gets_free_card(cfg, fx):
    """¥1 起標：到手成本看起來遠低於鑑定費，但它**不是**白撿。"""
    sig = evaluate(auction(1), card(), comps(), cfg, fx, keep_all=True, estimate=est())
    assert sig is not None
    assert Flag.FREE_CARD not in sig.flags
    assert Flag.LIVE_AUCTION in sig.flags


def test_cheap_auction_never_gets_discount_or_suspicious(cfg, fx):
    """同一個現在価格，即決會拿到 DISCOUNT／SUSPICIOUS_CHEAP，競標一個都不能有。"""
    st = comps(median=20000, n=15, p25=15000, p40=17000, conf="high")

    a = evaluate(auction(300), card(), st, cfg, fx, keep_all=True, estimate=est())
    b = evaluate(buyout(300), card(), st, cfg, fx, keep_all=True)

    assert {Flag.DISCOUNT, Flag.SUSPICIOUS_CHEAP} <= set(b.flags)
    assert not ({Flag.DISCOUNT, Flag.SUSPICIOUS_CHEAP} & set(a.flags))
    assert a.discount_pct is not None  # 欄位照舊算得出來，只是不拿它當 trigger


def test_auction_never_gets_shipping_kills_it(cfg, fx):
    """「運費吃掉優勢」也是以現在的價格為前提的判斷，競標一樣不能有。"""
    st = comps(median=500, n=10, p25=90, p40=120, conf="high")
    assert Flag.SHIPPING_KILLS_IT in evaluate(
        buyout(300), card(), st, cfg, fx, keep_all=True).flags
    assert Flag.SHIPPING_KILLS_IT not in evaluate(
        auction(300), card(), st, cfg, fx, keep_all=True, estimate=est()).flags


def test_buyout_path_is_untouched_by_the_estimate_argument(cfg, fx):
    """即決標的傳不傳 estimate 都必須得到**完全一樣**的旗標、分數、理由。

    這是「既有行為未退化」的機器版判準：新功能的入口參數不可以偷偷改到舊路徑。
    """
    lst = buyout(3000)
    st = comps(median=9000, n=20, conf="high")
    before = evaluate(lst, card(), st, cfg, fx)
    after = evaluate(lst, card(), st, cfg, fx, estimate=est())

    assert before.flags == after.flags
    assert before.score == after.score
    assert before.reason == after.reason
    assert before.best_route.landed_twd == after.best_route.landed_twd
    assert before.bid is None and after.bid is None


# ---------------------------------------------------------------------------
# 2. 競標的判準：目前出價 vs 出價上限
# ---------------------------------------------------------------------------
def test_bid_worth_when_current_bid_is_below_ceiling(cfg, fx):
    sig = evaluate(auction(500), card(), comps(n=9, conf="high"), cfg, fx,
                   keep_all=True, estimate=est())
    assert Flag.BID_WORTH in sig.flags
    assert sig.bid.ok and sig.bid.max_bid_jpy > 500
    assert sig.bid.is_actionable(500)
    assert is_triggered(sig.flags), "低於上限的競標是可以主動打擾你的理由"


def test_no_bid_worth_when_current_bid_is_above_ceiling(cfg, fx):
    ref = evaluate(auction(1), card(), comps(n=9), cfg, fx, keep_all=True, estimate=est())
    too_high = ref.bid.max_bid_jpy + 1_000

    sig = evaluate(auction(too_high), card(), comps(n=9), cfg, fx,
                   keep_all=True, estimate=est())
    assert Flag.BID_WORTH not in sig.flags
    assert Flag.LIVE_AUCTION in sig.flags
    assert "放掉它" in sig.reason


def test_auction_is_dropped_when_gate_is_on_and_bid_is_too_high(cfg, fx):
    """keep_all=False（Telegram 時代的閘門）：超過上限的競標不該吵你。"""
    ref = evaluate(auction(1), card(), comps(), cfg, fx, keep_all=True, estimate=est())
    assert evaluate(auction(ref.bid.max_bid_jpy + 5000), card(), comps(), cfg, fx,
                    estimate=est()) is None


def test_reason_always_carries_the_current_bid(cfg, fx):
    """上限旁邊一定要有目前出價——它是那個**會漲**的數字。"""
    sig = evaluate(auction(2500, bids=7), card(), comps(), cfg, fx,
                   keep_all=True, estimate=est())
    assert "¥2,500" in sig.reason
    assert "7 次出價" in sig.reason


# ---------------------------------------------------------------------------
# 3. 紅線：樣本不足 → 不給上限、不給分數
# ---------------------------------------------------------------------------
def test_thin_sample_auction_gets_no_ceiling(cfg, fx):
    sig = evaluate(auction(500), card(), comps(n=1), cfg, fx,
                   keep_all=True, estimate=thin_est())
    assert Flag.BID_NO_CEILING in sig.flags
    assert Flag.BID_WORTH not in sig.flags
    assert sig.bid.ok is False
    assert sig.bid.max_bid_jpy is None
    assert sig.score == 0.0, "沒有依據的候選不該跟有依據的排在一起"
    assert not is_triggered(sig.flags)


def test_missing_estimate_gets_no_ceiling(cfg, fx):
    """estimate 完全沒傳（例如估價模型建不起來）也必須安全降級，不是拋例外。"""
    sig = evaluate(auction(500), card(), comps(), cfg, fx, keep_all=True)
    assert sig.bid.ok is False and sig.bid.max_bid_jpy is None
    assert Flag.BID_NO_CEILING in sig.flags


# ---------------------------------------------------------------------------
# 4. 分數與排序
# ---------------------------------------------------------------------------
def test_more_headroom_scores_higher(cfg, fx):
    st = comps(n=9, conf="high")
    ref = evaluate(auction(1), card(), st, cfg, fx, keep_all=True, estimate=est())
    ceiling = ref.bid.max_bid_jpy

    lots = evaluate(auction(round(ceiling * 0.2)), card(), st, cfg, fx,
                    keep_all=True, estimate=est())
    little = evaluate(auction(round(ceiling * 0.95)), card(), st, cfg, fx,
                      keep_all=True, estimate=est())
    assert lots.score > little.score > 0


def test_bid_worth_is_a_trigger_flag():
    assert Flag.BID_WORTH in TRIGGER_FLAGS
    # 但「競標中」本身不是 trigger：它只是在描述這筆的性質
    assert Flag.LIVE_AUCTION not in TRIGGER_FLAGS
    assert Flag.BID_NO_CEILING not in TRIGGER_FLAGS


# ---------------------------------------------------------------------------
# 5. 結標時間與出價數要進得了 Signal / payload
# ---------------------------------------------------------------------------
def test_end_time_and_bids_survive_into_the_payload(cfg, fx):
    end = datetime(2026, 8, 3, 12, 34, 56, tzinfo=UTC)
    sig = evaluate(auction(500, bids=4, end=end), card(), comps(), cfg, fx,
                   keep_all=True, estimate=est())
    d = sig.to_dict()

    # ISO 8601（含 T 與時區）——瀏覽器的 new Date() 必須解得出來
    assert d["listing"]["end_time"] == "2026-08-03T12:34:56+00:00"
    assert d["listing"]["bids"] == 4
    assert d["bid"]["max_bid_jpy"] == sig.bid.max_bid_jpy


def test_fixed_price_listing_has_no_end_time(cfg, fx):
    sig = evaluate(buyout(300), card(), comps(), cfg, fx, keep_all=True)
    assert sig.to_dict()["listing"]["end_time"] is None


def test_signals_sort_by_end_time_nearest_first(cfg, fx):
    """競標視圖的排序判準：**結標近的排前面**，跟分數無關。

    這裡釘的是排序鍵本身（前端照它排）——結標時間必須是可比較的絕對時間，
    而不是「1日／10時間」這種四捨五入過的相對文字。
    """
    st = comps(n=9, conf="high")
    far = evaluate(auction(500, end=datetime(2026, 8, 9, tzinfo=UTC),
                           external_id="far"), card(), st, cfg, fx,
                   keep_all=True, estimate=est())
    near = evaluate(auction(500, end=datetime(2026, 8, 3, tzinfo=UTC),
                            external_id="near"), card(), st, cfg, fx,
                    keep_all=True, estimate=est())

    by_end = sorted([far, near], key=lambda s: s.listing.end_time)
    assert [s.listing.external_id for s in by_end] == ["near", "far"]
    # 兩筆分數一樣 → 只有結標時間能決定順序，這正是要有絕對時間的理由
    assert far.score == near.score


@pytest.mark.parametrize("ended,label", [(True, "已結標"), (False, "還在跑")])
def test_ended_auctions_are_detectable_from_the_payload(cfg, fx, ended, label):
    """已結標的判定只需要 payload 的 end_time ＋ 現在時間，不需要再打一次網路。"""
    end = datetime(2020, 1, 1, tzinfo=UTC) if ended else datetime(2099, 1, 1, tzinfo=UTC)
    sig = evaluate(auction(500, end=end), card(), comps(), cfg, fx,
                   keep_all=True, estimate=est())
    parsed = datetime.fromisoformat(sig.to_dict()["listing"]["end_time"])
    assert (parsed <= datetime.now(UTC)) is ended, label
