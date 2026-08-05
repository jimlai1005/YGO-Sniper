"""eBay 出價上限：幣別跟著 listing 走的反解（bidding.max_bid_ebay）。

這個檔案守的合約與 test_bidding.py 同一條——**使用者會照著上限下真錢的單**——
但幣別鏈完全不同，所以要另外釘住四件事：

  1. 反解與正算同源（核心保證）：原幣上限 → 用 listing 自己的換匯比率換回
     台幣 → 加 listing 的實際運費 → 乘刷卡加成 → 到手成本 ≤ 預算。
     資料用 2026-08-03 實抓的 eBay API fixture（真實的換匯比率與運費）。
  2. 換匯比率**同源**：換回原幣用的是這筆 listing 自己的 `value/convertedFromValue`，
     不是我們的 fx 表——eBay 的台幣是它自己的匯率換出來的（工程原則 1）。
  3. 運費未知／不寄台灣／換匯資訊缺失 → **不給上限**（一個沒依據的數字比
     沒有數字危險）。
  4. 證據閘門對 eBay **原樣適用**——不為它另開標準。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_listing

from ygo_sniper.bidding import (
    EBAY_PROXY_BID_FINDING,
    FORWARD_CHECK_TOLERANCE_TWD,
    EvidenceGate,
    auction_room_value,
    auction_tier,
    ceiling_value_of,
    max_bid_ebay,
)
from ygo_sniper.costs import quote_route
from ygo_sniper.domain import Currency, Flag, Site
from ygo_sniper.sources.ebay import EbaySource, native_price_info
from ygo_sniper.valuation import Estimate

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "ebay_api_items.json").read_text()
)

#: 帶著真實運費報價的五筆實抓標的（原幣各異：GBP／EUR／USD）。
#: 反向驗證表（驗收條件 3）就是跑這五筆。
PRICED_KEYS = (
    "item_fixed",            # GBP，運費 NT$435
    "item_auction",          # EUR，運費 NT$521
    "item_auction_bin",      # USD，運費 NT$2,591
    "summary_auction",       # EUR，運費 NT$521.35
    "summary_auction_bin",   # USD，運費 NT$2,590.94
)


def est(lo=8000.0, hi=24000.0, fair=14000.0, **kw):
    """與 test_bidding.est 同構：四道閘門都過得了的估價（venue 換成 ebay）。"""
    defaults = dict(
        fair_twd=fair, level="L1", level_label="同卡 × 同稀有度", n_effective=5,
        lo_twd=lo, hi_twd=hi, confidence=0.8, calibration_n=80,
        venue="ebay", venue_adjusted=True, grade=10.0, grade_source="title",
        calibration_group="L1/3-9", calibration_group_requested="L1/3-9",
        calibration_group_n=71,
    )
    return Estimate(**{**defaults, **kw})


def listing_of(key: str):
    lst = EbaySource._to_listing(FIXTURES[key])
    assert lst is not None, f"fixture {key} 解析不出 Listing"
    return lst


# ---------------------------------------------------------------------------
# 1. 核心保證：原幣上限 → 正算 → 到手成本 ≤ 預算（真實 fixture、參數化）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", PRICED_KEYS)
@pytest.mark.parametrize("lo", [4000, 8000, 20000, 60000])
@pytest.mark.parametrize("margin", [0.0, 0.30, 0.5])
def test_native_ceiling_round_trips_through_quote_route(cfg, fx, key, lo, margin):
    """**本模組的核心測試**（驗收條件 3 的機器版）。

    正算走 `costs.quote_route`（= `_quote_ebay`，掃描端算成本用的同一支），
    換回台幣走 listing 自己的比率——兩步都與出貨路徑同源，不另外拼一份公式。
    """
    lst = listing_of(key)
    c = max_bid_ebay(est(lo=lo), cfg, fx, listing=lst, target_margin=margin)
    if not c.ok:
        # 運費吃光預算是合法結果，但那時必須完全沒有數字
        assert c.max_bid_jpy is None and c.max_bid_native is None
        return

    budget = lo * (1 - margin)
    assert c.max_bid_jpy is None, "eBay 的上限不是日圓，日圓欄位必須是 None"
    assert c.max_bid_native is not None and c.native_currency in ("GBP", "EUR", "USD")

    # 原幣 → listing 幣別：用**同一個**比率（這筆 listing 自己的 eBay 匯率）
    native = native_price_info(lst.raw)
    assert c.native_rate == pytest.approx(native.rate)
    listing_value = round(c.max_bid_native * native.rate, 2)
    assert c.max_bid_listing == pytest.approx(listing_value, abs=0.01)

    probe = make_listing(
        price=listing_value, site=Site.EBAY, currency=lst.currency,
        shipping_cost=lst.shipping_cost,
    )
    route = cfg.routes_for_site("ebay")[0]
    q = quote_route(probe, route, fx)
    assert q.landed_twd <= budget + FORWARD_CHECK_TOLERANCE_TWD, (
        f"{key}: 上限 {c.native_currency} {c.max_bid_native} 的到手 "
        f"NT${q.landed_twd} 超過預算 NT${budget}"
    )
    # 引擎自己回報的正算結果必須跟外部重算一致（不是另外算一份）
    assert c.landed_at_ceiling_twd == q.landed_twd
    assert c.budget_twd == pytest.approx(budget, abs=0.01)


@pytest.mark.parametrize("key", PRICED_KEYS)
def test_native_ceiling_is_tight_not_merely_safe(cfg, fx, key):
    """原幣再加 0.01 就會超過預算——只驗「≤ 預算」的話回 0.01 也會過。"""
    lst = listing_of(key)
    c = max_bid_ebay(est(lo=20000), cfg, fx, listing=lst)
    assert c.ok
    native = native_price_info(lst.raw)
    over = make_listing(
        price=round((c.max_bid_native + 0.01) * native.rate, 2),
        site=Site.EBAY, currency=lst.currency, shipping_cost=lst.shipping_cost,
    )
    route = cfg.routes_for_site("ebay")[0]
    assert quote_route(over, route, fx).landed_twd > c.budget_twd + FORWARD_CHECK_TOLERANCE_TWD


def test_shipping_is_actually_subtracted(cfg, fx):
    """運費高的 listing 上限必須更低——「ebay_direct 雜費 0 → 運費被忽略」
    正是先前擋著這條管道的 bug，這條測試釘死它不會回來。"""
    cheap_ship = listing_of("item_auction")        # 運費 NT$521
    dear_ship = listing_of("item_auction_bin")     # 運費 NT$2,591
    lo = 20000
    a = max_bid_ebay(est(lo=lo), cfg, fx, listing=cheap_ship)
    b = max_bid_ebay(est(lo=lo), cfg, fx, listing=dear_ship)
    assert a.ok and b.ok
    # 同預算下，台幣基準的上限差 ≈ 運費差（各自含刷卡加成，容差給捨入）
    diff = a.max_bid_listing - b.max_bid_listing
    ship_gap = (2591.0 - 521.0)
    assert diff == pytest.approx(ship_gap, rel=0.02)


# ---------------------------------------------------------------------------
# 2. 三條 eBay 專屬拒絕：未知運費／不寄台灣／換匯資訊缺失
# ---------------------------------------------------------------------------
def test_unknown_shipping_means_no_ceiling(cfg, fx):
    """運費未知 → 不給上限。掃描端的 US$25 佔位值只配當參考成本，
    反解裡少扣一筆佔三到五成的錢，上限必然偏高——那是會付錢的方向。"""
    lst = listing_of("item_auction_with_bids")     # 實抓：沒有 shippingOptions
    assert lst.shipping_cost is None
    c = max_bid_ebay(est(lo=60000), cfg, fx, listing=lst)
    assert c.ok is False
    assert c.max_bid_native is None and c.max_bid_listing is None
    assert "運費" in c.reason and "未知" in c.reason


def test_not_shipping_to_tw_means_no_ceiling(cfg, fx):
    """賣家不寄台灣：寄台灣的到手成本是一個不存在的交易（US 路徑另計，
    但轉運成本未建模——不給數字是誠實邊界）。"""
    lst = listing_of("item_auction")
    lst.ships_to_tw = False
    c = max_bid_ebay(est(lo=20000), cfg, fx, listing=lst)
    assert c.ok is False and c.max_bid_native is None
    assert "不寄台灣" in c.reason


def test_twd_without_conversion_info_is_refused(cfg, fx):
    """顯示幣別是台幣、又沒有原幣資訊 → 給不出能填進 eBay 出價欄的數字。"""
    lst = listing_of("item_auction")
    raw = dict(lst.raw)
    node = dict(raw["currentBidPrice"])
    node.pop("convertedFromValue"), node.pop("convertedFromCurrency")
    raw["currentBidPrice"] = node
    lst.raw = raw
    c = max_bid_ebay(est(lo=20000), cfg, fx, listing=lst)
    assert c.ok is False and "換匯資訊缺失" in c.reason


def test_usd_native_listing_without_conversion_works(cfg, fx):
    """listing 本來就以原幣（USD）顯示：比率=1、原幣＝listing 幣別，照樣可算。"""
    lst = make_listing(
        price=100.0, site=Site.EBAY, currency=Currency.USD, shipping_cost=20.0,
        raw={"price_kind": "current_bid", "buyingOptions": ["AUCTION"],
             "currentBidPrice": {"value": "100.0", "currency": "USD"}},
    )
    c = max_bid_ebay(est(lo=20000), cfg, fx, listing=lst)
    assert c.ok
    assert c.native_currency == "USD" and c.native_rate == 1.0
    assert c.max_bid_native == pytest.approx(c.max_bid_listing)
    # 正算（fx 表的 USD→TWD，與正算 quote 同一顆 fx——這種情形兩邊本來就同源）
    probe = make_listing(price=c.max_bid_listing, site=Site.EBAY,
                         currency=Currency.USD, shipping_cost=20.0)
    q = quote_route(probe, cfg.routes_for_site("ebay")[0], fx)
    assert q.landed_twd <= c.budget_twd + FORWARD_CHECK_TOLERANCE_TWD


# ---------------------------------------------------------------------------
# 3. 證據閘門原樣適用（不為 eBay 另開標準）
# ---------------------------------------------------------------------------
def test_unknown_grade_gets_no_ceiling_on_ebay(cfg, fx):
    c = max_bid_ebay(est(grade=None), cfg, fx, listing=listing_of("item_auction"))
    assert not c.ok and c.max_bid_native is None
    assert "抽不到鑑定分數" in c.reason


def test_no_card_specific_evidence_gets_no_ceiling_on_ebay(cfg, fx):
    c = max_bid_ebay(
        est(level="L3", level_label="稀有度×分數", n_effective=325),
        cfg, fx, listing=listing_of("item_auction"),
    )
    assert not c.ok and "L3" in c.reason


def test_broken_bucket_is_rejected_on_ebay_too(cfg, fx):
    c = max_bid_ebay(est(n_effective=12), cfg, fx, listing=listing_of("item_auction"))
    assert not c.ok and "10-49" in c.reason


def test_no_interval_means_no_ceiling_on_ebay(cfg, fx):
    c = max_bid_ebay(est(lo=None, hi=None), cfg, fx, listing=listing_of("item_auction"))
    assert not c.ok and c.max_bid_native is None and "不提供出價上限" in c.reason


def test_gate_override_applies_to_ebay(cfg, fx):
    gate = EvidenceGate(min_effective_samples=9)
    c = max_bid_ebay(est(n_effective=5), cfg, fx,
                     listing=listing_of("item_auction"), gate=gate)
    assert not c.ok and "只有 5 筆成交" in c.reason


# ---------------------------------------------------------------------------
# 4. headroom／梯隊：現價與上限同幣別（listing 幣別），日圓欄位不參戰
# ---------------------------------------------------------------------------
def test_headroom_compares_in_listing_currency(cfg, fx):
    lst = listing_of("item_auction")               # 現價 NT$71
    c = max_bid_ebay(est(lo=20000), cfg, fx, listing=lst)
    assert c.ok
    assert c.comparison_ceiling() == c.max_bid_listing
    assert c.is_actionable(lst.price) is (lst.price < c.max_bid_listing)
    assert c.headroom_value(lst.price) == pytest.approx(
        c.max_bid_listing - lst.price, abs=0.01
    )
    # 日圓專用介面對 eBay 一律 None——不是 0，0 是一個假的事實
    assert c.headroom_jpy(lst.price) is None


def test_ceiling_value_of_reads_ebay_dicts():
    d = {"ok": True, "max_bid_jpy": None, "max_bid_listing": 1500.0}
    assert ceiling_value_of(d) == 1500.0
    assert auction_room_value(d, 700.0) == 800.0
    # Yahoo 形狀不受影響
    y = {"ok": True, "max_bid_jpy": 5000.0}
    assert ceiling_value_of(y) == 5000.0
    assert ceiling_value_of({"ok": False, "max_bid_listing": 1500.0}) is None


def test_auction_tier_works_for_ebay_bids():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    soon = (now + timedelta(hours=3)).isoformat()
    bid = {"ok": True, "max_bid_jpy": None, "max_bid_listing": 1500.0}
    assert auction_tier(bid, 700.0, soon, now) == 1
    assert auction_tier(bid, 2000.0, soon, now) == 3
    assert auction_tier({"ok": False}, 700.0, soon, now) == 3


# ---------------------------------------------------------------------------
# 5. scoring 整合：分流、US 旗標、ships_to_tw=False 不被丟棄
# ---------------------------------------------------------------------------
def _card():
    from ygo_sniper.domain import CardInfo, Grader

    return CardInfo(grader=Grader.PSA, grade=10.0, in_era=True,
                    era_evidence=["kw"], rarity="ultra")


def _comps():
    from ygo_sniper.domain import CompStats

    return CompStats(n=9, median_twd=4000.0, p25_twd=2000.0, p40_twd=2500.0,
                     p75_twd=6000.0, window_days=90, confidence="high")


def test_evaluate_gives_ebay_auction_a_native_ceiling(cfg, fx):
    from ygo_sniper.scoring import evaluate

    lst = listing_of("item_auction")               # 純競標、現價 NT$71、運費已知
    sig = evaluate(lst, _card(), _comps(), cfg, fx, keep_all=True, estimate=est(lo=20000))
    assert Flag.LIVE_AUCTION in sig.flags
    assert sig.bid is not None and sig.bid.ok
    assert sig.bid.max_bid_native is not None and sig.bid.native_currency == "EUR"
    assert Flag.BID_WORTH in sig.flags             # NT$71 遠低於上限
    assert "出價欄填" in sig.reason and "EUR" in sig.reason
    # payload 要把幣別欄位帶出門（dashboard／推播讀這份）
    d = sig.to_dict()["bid"]
    assert d["max_bid_native"] == sig.bid.max_bid_native
    assert d["max_bid_listing"] == sig.bid.max_bid_listing
    assert d["native_currency"] == "EUR" and d["listing_currency"] == "TWD"


def test_ships_to_tw_false_is_kept_flagged_and_not_bid_worthy(cfg, fx):
    """D 的掃描端合約：不丟掉、標旗標、不給上限（→ 也進不了推播規則 1）。"""
    from ygo_sniper.scoring import evaluate, is_triggered

    lst = listing_of("item_auction")
    lst.ships_to_tw = False
    sig = evaluate(lst, _card(), _comps(), cfg, fx, keep_all=True, estimate=est(lo=20000))
    assert sig is not None, "ships_to_tw=False 的標的不可以被丟掉"
    assert Flag.US_SHIP_OPTION in sig.flags
    assert "91762" in sig.reason
    assert sig.bid is not None and sig.bid.ok is False
    assert Flag.BID_WORTH not in sig.flags
    assert not is_triggered(sig.flags), "US 路徑只是資訊，不是出手理由"


def test_us_flag_needs_a_configured_zip(cfg, fx):
    """沒設 buying.us_ship_zip 就沒有這面旗——「可寄美國地址」必須是事實不是假設。"""
    import dataclasses

    from ygo_sniper.scoring import evaluate

    bare = dataclasses.replace(cfg, buying={})
    lst = listing_of("item_auction")
    lst.ships_to_tw = False
    sig = evaluate(lst, _card(), _comps(), bare, fx, keep_all=True, estimate=est(lo=20000))
    assert Flag.US_SHIP_OPTION not in sig.flags


def test_source_keeps_auctions_and_carries_end_time_and_bids():
    """A 的合約：競標標的帶 end_time 與 bids 進 Listing（掃描端資料完整性）。"""
    lst = listing_of("item_auction_with_bids")
    assert lst.raw["price_kind"] == "current_bid"
    assert lst.end_time is not None and lst.end_time.tzinfo is not None
    assert lst.bids == 5


def test_shipped_config_enables_ebay_live_auctions(cfg):
    """A 的合約：出貨設定真的打開了 eBay 競標掃描（不是只有程式碼支援）。"""
    assert (cfg.sources.get("ebay") or {}).get("include_live_auctions") is True
    assert str(cfg.buying.get("us_ship_zip")) == "91762"


# ---------------------------------------------------------------------------
# 6. eBay 代理出價查證：結論必須帶證據（與 Buyee 那份同一個標準）
# ---------------------------------------------------------------------------
def test_ebay_proxy_bid_finding_carries_evidence():
    assert EBAY_PROXY_BID_FINDING["supported"] is True
    assert EBAY_PROXY_BID_FINDING["sources"], "查證結論必須附來源 URL"
    assert all(
        u.startswith("https://www.ebay.com/") for u in EBAY_PROXY_BID_FINDING["sources"]
    )
    assert EBAY_PROXY_BID_FINDING["checked_at"] == "2026-08-03"
    # UI 要引用的那句話必須在：設好上限就可以離開
    assert "設好上限就可以離開" in EBAY_PROXY_BID_FINDING["summary"]
