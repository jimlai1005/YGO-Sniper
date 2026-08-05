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
