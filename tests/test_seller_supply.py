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
    dim_grade_profile, dim_listing_rhythm, dim_persistence, dim_series_focus,
    dim_supply_depth,
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


def test_supply_depth_is_always_available_and_says_it_is_cumulative():
    d = dim_supply_depth(metrics(n_rows=117, observation_span_days=3.2))
    assert d.available is True and d.raw == 117.0
    assert "累積" in d.detail            # 不是速率


def test_supply_depth_available_even_with_zero_rows():
    """0 列是「我們沒看到貨」，是可得的資訊——不可得留給算不出來的維度。"""
    d = dim_supply_depth(metrics(n_rows=0))
    assert d.available is True and d.raw == 0.0


def test_persistence_unavailable_at_a_single_point_in_time():
    """跨度 0 = 只看到他一個時間點，談不了持續性。"""
    d = dim_persistence(metrics(n_rows=9, observation_span_days=0.0))
    assert d.available is False
    assert d.raw is None and d.score is None
    assert "持續" in d.missing


def test_persistence_reports_the_actual_span():
    d = dim_persistence(metrics(n_rows=20, observation_span_days=174.9))
    assert d.available is True
    assert d.raw == pytest.approx(174.9)
    assert "174.9" in d.detail


def test_supply_depth_and_persistence_are_not_the_same_number():
    """這條測試守的是本次修正的原因本身：實測 corr(n_sold, n_rows)=0.989、
    87% 的賣家兩者相等，把它們當兩個維度等於同一個訊號算兩次。
    supply_depth 量「多少」、persistence 量「多久」，必須是不同的量。"""
    m = metrics(n_rows=100, observation_span_days=2.0)
    assert dim_supply_depth(m).raw != dim_persistence(m).raw


def test_listing_rhythm_measures_concentration_not_quality():
    m = metrics(listing_hour_hist={20: 8, 21: 2})
    d = dim_listing_rhythm(m)
    assert d.available is True
    assert d.raw == pytest.approx(0.8)   # top1 時段佔比


def test_listing_rhythm_unavailable_below_five_observations():
    assert dim_listing_rhythm(metrics(listing_hour_hist={20: 3})).available is False


from ygo_sniper.seller_supply import SupplyFit, SupplyParams, supply_fit_all


def test_score_is_renormalised_over_available_dimensions_only():
    """跨度 0（persistence 20 分）與系列不明（series_focus 15 分）都不可得
    → 剩下 65 的權重要重新正規化到 100，不是讓他直接損失 35 分。"""
    ms = [metrics(seller_key=f"ebay:s{i}", site="ebay", n_rows=i * 10,
                  observation_span_days=0.0,          # persistence 不可得
                  grade_mix={"PSA 8": i, "PSA 10": 1},
                  listing_hour_hist={20: 5, 21: 1}) for i in range(1, 12)]
    out = supply_fit_all(ms, params=SupplyParams())
    top = out[ms[-1].seller_key]
    assert top.ok is True
    assert top.n_dimensions_used == 3        # persistence 與 series_focus 都不可得
    assert top.n_dimensions_total == 5
    assert 0.0 <= top.total <= 100.0
    assert any("persistence" in x for x in top.missing)


def test_supply_depth_alone_is_never_enough():
    """supply_depth 恆可得——如果它單獨就能給分數，那 223 個只有它的賣家
    會全部拿到「分數」，但那其實只是「我們手上有幾列資料」的排名。"""
    ms = [metrics(seller_key=f"ebay:s{i}", site="ebay", n_rows=i)
          for i in range(1, 12)]           # 其餘四個維度全不可得
    out = supply_fit_all(ms, params=SupplyParams())
    for fit in out.values():
        assert fit.ok is False
        assert fit.total is None           # 不是 0
        assert fit.n_dimensions_used == 1


def test_site_with_too_few_sellers_is_not_ranked():
    """站內只有 3 個賣家時百分位沒有意義——寧可不給分數。"""
    ms = [metrics(seller_key=f"m:s{i}", site="buyee_mercari", n_rows=i,
                  observation_span_days=float(i), grade_mix={"PSA 8": 3},
                  listing_hour_hist={20: 5}) for i in range(3)]
    out = supply_fit_all(ms, params=SupplyParams())
    for fit in out.values():
        assert fit.ok is False
        assert fit.total is None             # 不是 0
        assert "站內賣家數" in fit.reason


def test_percentile_is_within_site_only():
    """eBay 全站的量級比 yahoo 小兩個數量級，但 eBay 站內第一名仍該拿高分——
    n_rows 的組成隨站台而異（yahoo 多半是挖回來的成交帳、eBay 只有在架），
    跨站比大小會讓分數變成站台的代理變數。"""
    ebay = [metrics(seller_key=f"ebay:s{i}", site="ebay", n_rows=i,
                    observation_span_days=float(i), grade_mix={"PSA 8": 3},
                    listing_hour_hist={20: 5}) for i in range(1, 12)]
    yahoo = [metrics(seller_key=f"y:s{i}", site="buyee_yahoo", n_rows=i * 100,
                     observation_span_days=float(i * 10), grade_mix={"PSA 8": 3},
                     listing_hour_hist={20: 5}) for i in range(1, 12)]
    out = supply_fit_all(ebay + yahoo, params=SupplyParams())
    assert out["ebay:s11"].total > 50        # 站內第一名，不因絕對量小而被壓低


def test_every_used_dimension_carries_its_evidence():
    """裸數字不准輸出——沿用 seller_alpha 的 ScoreComponent.detail 慣例。"""
    ms = [metrics(seller_key=f"ebay:s{i}", site="ebay", n_rows=i * 10,
                  observation_span_days=float(i), grade_mix={"PSA 8": 3},
                  listing_hour_hist={20: 5}) for i in range(1, 12)]
    out = supply_fit_all(ms, params=SupplyParams())
    for d in out["ebay:s11"].dimensions:
        if d.available:
            assert d.detail, f"{d.name} 沒有帶依據"
            assert d.score is not None
        else:
            assert d.missing, f"{d.name} 不可得但沒說缺什麼"
            assert d.score is None


def test_thin_evidence_is_flagged_in_caveats():
    """只用 2 個維度算出來的分數，畫面上必須看得出來它薄。"""
    ms = [metrics(seller_key=f"ebay:s{i}", site="ebay", n_rows=i * 10,
                  observation_span_days=float(i)) for i in range(1, 12)]
    fit = supply_fit_all(ms, params=SupplyParams())["ebay:s11"]
    assert fit.ok is True and fit.n_dimensions_used == 2
    assert any("2/5" in c for c in fit.caveats)
    assert all(c.startswith("⚠️") for c in fit.caveats)
