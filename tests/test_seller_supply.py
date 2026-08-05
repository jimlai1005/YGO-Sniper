"""Supply Fit 的測試。這組測試守的是這幾種病：
1. 維度不可得被當成 0 分（等於說「這賣家很差」，但事實是「我們不知道」）
2. Supply Fit 與 Alpha 被相加或互相 fallback（第四節紅線的變種）
3. 跨站絕對比較（eBay 拿不到成交數，直接比大小 = 分數變成站台代理變數）
4. 裸數字輸出（每個維度必須帶得出依據字串）
"""
from ygo_sniper.seller_supply import SupplyDimension


def test_unavailable_dimension_has_no_score_and_says_what_is_missing():
    d = SupplyDimension.unavailable("sold_depth", "ebay 拿不到歷史成交（Insights API 403）")
    assert d.available is False
    assert d.raw is None
    assert d.score is None          # 不是 0.0
    assert "Insights" in d.missing


def test_available_dimension_must_carry_its_evidence():
    d = SupplyDimension.of("grade_profile", raw=0.62, detail="8/9 分佔 62%（31/50 筆）")
    assert d.available is True
    assert d.raw == 0.62
    assert d.detail                  # 不准是空字串


import pytest

from ygo_sniper.seller_alpha import SellerMetrics
from ygo_sniper.seller_supply import (
    dim_grade_profile, dim_listing_rhythm, dim_series_focus,
    dim_sold_depth, dim_supply_scale,
)


def metrics(**kw) -> SellerMetrics:
    """只填測試關心的欄位，其餘吃 dataclass 預設。"""
    base = dict(seller_key="ebay:t", site="ebay", seller_id="t")
    base.update(kw)
    return SellerMetrics(**base)


def test_grade_profile_counts_8_and_9_across_graders():
    """ARS 也算——grade_mix 裡 ARS 佔比很高，只認 PSA 會漏掉大半市場。"""
    m = metrics(grade_mix={"PSA 8": 3, "ARS 9": 2, "PSA 10": 5})
    d = dim_grade_profile(m)
    assert d.available is True
    assert d.raw == pytest.approx(0.5)          # (3+2)/10
    assert "5/10" in d.detail


def test_grade_profile_unavailable_below_three_samples():
    d = dim_grade_profile(metrics(grade_mix={"PSA 9": 2}))
    assert d.available is False
    assert d.score is None
    assert "3" in d.missing


def test_grade_profile_counts_unparseable_keys_in_denominator_only():
    """壞掉的 key 不能靜默消失——計入分母並在 detail 講出來。"""
    m = metrics(grade_mix={"PSA 8": 3, "BGS": 2, "PSA 10": 5})
    d = dim_grade_profile(m)
    assert d.raw == pytest.approx(3 / 10)
    assert "2" in d.detail                       # 有講出 2 筆解析不了


def test_series_focus_needs_three_known_series_rows():
    ok = dim_series_focus(metrics(series_top1_share=0.8, series_known_n=5))
    assert ok.available is True and ok.raw == pytest.approx(0.8)
    thin = dim_series_focus(metrics(series_top1_share=1.0, series_known_n=2))
    assert thin.available is False      # 兩筆全同系列不算「固定賣某系列」


def test_series_focus_unavailable_when_share_is_none():
    assert dim_series_focus(metrics(series_top1_share=None, series_known_n=9)).available is False


def test_sold_depth_unavailable_when_site_cannot_yield_sold_prices():
    """eBay 的 Marketplace Insights 是 403，成交數在這一站是「不知道」不是「0」。"""
    d = dim_sold_depth(metrics(site="ebay", n_sold=0))
    assert d.available is False
    assert "403" in d.missing or "Insights" in d.missing


def test_sold_depth_zero_on_a_site_that_can_yield_sold_is_available():
    """能拿到成交的站，n_sold=0 是真的沒賣過——這是可得的資訊，不是不可得。"""
    d = dim_sold_depth(metrics(site="buyee_yahoo", n_sold=0))
    assert d.available is True and d.raw == 0.0


def test_sold_depth_available_on_sites_with_history():
    d = dim_sold_depth(metrics(site="buyee_yahoo", n_sold=184))
    assert d.available is True and d.raw == 184.0


def test_supply_scale_is_always_available_and_says_it_is_cumulative():
    d = dim_supply_scale(metrics(n_rows=117, observation_span_days=3.2))
    assert d.available is True and d.raw == 117.0
    assert "累積" in d.detail            # 不是速率


def test_listing_rhythm_measures_concentration_not_quality():
    m = metrics(listing_hour_hist={20: 8, 21: 2})
    d = dim_listing_rhythm(m)
    assert d.available is True
    assert d.raw == pytest.approx(0.8)   # top1 時段佔比


def test_listing_rhythm_unavailable_below_five_observations():
    assert dim_listing_rhythm(metrics(listing_hour_hist={20: 3})).available is False
