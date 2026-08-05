"""Site ↔ route 覆蓋防呆，以及「發現管道不影響成本」不變量。

這兩條測試守的是同一種靜默失敗：quote_all_routes 對查不到 route 的 site
回空 list，scoring.evaluate 遇到空 list 直接丟棄標的——不會報錯、
不會告警，只是那個市集的標的從此人間蒸發。
"""

import dataclasses

from conftest import make_listing

from ygo_sniper.costs import quote_all_routes
from ygo_sniper.domain import Site


def test_every_site_has_at_least_one_route(cfg):
    """每個 Site 成員都必須至少對應一條運送路徑。"""
    for site in Site:
        assert cfg.routes_for_site(site.value), (
            f"Site.{site.name}（{site.value!r}）沒有對應任何 route——"
            "新增 Site 成員時忘了改 settings.yaml 的 routes.sites。"
            "漏掉的話 quote_all_routes 回空 list，該市集的標的會被靜默丟棄。"
        )
    # 反向防呆：PayPay 貨走不了 Mercari 台灣，這條路徑限制不准被 settings 改壞
    assert "buyee_paypay" not in cfg.routes["mercari_tw"].sites


def test_cost_is_independent_of_discovery_source(cfg, fx):
    """同一筆標的，無論從哪條管道發現，每條 route 的報價逐欄位相同。

    source 只做觀測歸因；它一旦影響成本，就等於同一張卡有兩種價格。
    """
    base = make_listing(price=1500, site=Site.BUYEE_YAHOO)
    via_yahoo = dataclasses.replace(base, source="yahoo_direct")
    via_buyee = dataclasses.replace(base, source="buyee_mercari")

    quotes_a = quote_all_routes(via_yahoo, cfg, fx)
    quotes_b = quote_all_routes(via_buyee, cfg, fx)

    assert quotes_a  # 防止「兩邊都是空 list」的假通過
    assert len(quotes_a) == len(quotes_b)
    for qa, qb in zip(quotes_a, quotes_b, strict=True):
        for f in dataclasses.fields(qa):
            assert getattr(qa, f.name) == getattr(qb, f.name), (
                f"route {qa.route} 的欄位 {f.name} 受 source 影響："
                f"{getattr(qa, f.name)!r} != {getattr(qb, f.name)!r}"
            )
