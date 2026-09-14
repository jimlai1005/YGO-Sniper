"""估價時間加權：半衰期 None 時行為必須與等權逐位元相同；有半衰期時近期成交主導。"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from ygo_sniper.valuation import Obs, Params, _weights, _wmedian

AS_OF = datetime(2026, 9, 14, tzinfo=UTC)


def _obs(price, days_ago, name="A", rarity="ultra", grade=9.0, key=None):
    sold = (AS_OF - timedelta(days=days_ago)).isoformat()
    return Obs(price, name, rarity, grade, key=key or f"{name}-{price}-{days_ago}", sold_at=sold)


class TestParams:
    def test_default_is_none(self):
        assert Params().half_life_days is None

    def test_from_config_reads_null_and_number(self):
        class C:
            valuation = {"half_life_days": None}
        assert Params.from_config(C()).half_life_days is None

        class D:
            valuation = {"half_life_days": 45}
        assert Params.from_config(D()).half_life_days == 45.0


class TestWeights:
    def test_none_half_life_is_all_ones(self):
        rows = [_obs(100, 0), _obs(100, 400)]
        assert _weights(rows, None, AS_OF) == [1.0, 1.0]

    def test_half_life_halves_per_period(self):
        rows = [_obs(100, 0), _obs(100, 45), _obs(100, 90)]
        w = _weights(rows, 45, AS_OF)
        assert w == pytest.approx([1.0, 0.5, 0.25])

    def test_undated_row_gets_full_weight(self):
        rows = [Obs(100.0, "A", "ultra", 9.0, key="x")]
        assert _weights(rows, 45, AS_OF) == [1.0]

    def test_future_dated_row_clamps_to_weight_one(self):
        rows = [_obs(100, -3)]
        assert _weights(rows, 45, AS_OF) == [1.0]


class TestWeightedMedian:
    def test_equal_weights_match_statistics_median_exactly(self):
        vals = [3.0, 1.0, 4.0, 1.5]
        assert _wmedian(vals, [1.0] * 4) == statistics.median(vals)
        assert _wmedian(vals, [0.3] * 4) == statistics.median(vals)

    def test_heavy_weight_dominates(self):
        assert _wmedian([1.0, 10.0], [0.1, 1.0]) == 10.0

    def test_exact_half_boundary_averages_neighbours(self):
        # 權重 1,1,2：累積到第二個剛好 = 一半（2/4）→ 取第二、第三平均
        assert _wmedian([1.0, 2.0, 3.0], [1.0, 1.0, 2.0]) == 2.5

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _wmedian([], [])


from ygo_sniper.valuation import ValuationModel, Valuator  # noqa: E402

PRIOR = Params(
    confidence=0.80, shrink_k=4, min_calibration=10, calibration_fraction=0.4, min_cell=3,
    grade_premium_prior={10.0: 4.0, 9.0: 1.0, 8.0: 0.5, 7.0: 0.25},
    rarity_premium_prior={"ultimate": 4.7, "secret": 3.5, "ultra": 2.2, "normal": 1.0},
)


def _rows():
    return [
        _obs(1000.0, 200, "A"), _obs(1200.0, 100, "A"), _obs(4000.0, 150, "A", grade=10.0),
        _obs(600.0, 120, "B"), _obs(700.0, 90, "C", grade=8.0), _obs(900.0, 60, "D", grade=10.0),
        _obs(300.0, 30, "E", rarity="normal"), _obs(350.0, 10, "F", rarity="normal"),
    ]


class TestModelWeighting:
    def test_none_half_life_identical_to_unweighted(self):
        rows = _rows()
        base = ValuationModel(rows, PRIOR)
        same = ValuationModel(rows, PRIOR, as_of=AS_OF)
        assert same.predict_log("A", "ultra", 9.0) == base.predict_log("A", "ultra", 9.0)
        assert same.predict_log(None, "ultra", 10.0) == base.predict_log(None, "ultra", 10.0)
        assert same.base == base.base

    def test_recent_sale_dominates_l1_with_short_half_life(self):
        rows = [_obs(1000.0, 200, "A", key="old1"), _obs(1000.0, 190, "A", key="old2"),
                _obs(1000.0, 180, "A", key="old3"), _obs(500.0, 1, "A", key="new"),
                _obs(800.0, 100, "B"), _obs(900.0, 100, "C"), _obs(700.0, 100, "D")]
        import dataclasses
        p_short = dataclasses.replace(PRIOR, half_life_days=10.0)
        eq = ValuationModel(rows, PRIOR, as_of=AS_OF).predict_log("A", "ultra", 9.0)
        wt = ValuationModel(rows, p_short, as_of=AS_OF).predict_log("A", "ultra", 9.0)
        assert wt[0] < eq[0]                      # 近期 500 把估計往下拉
        assert wt[1] == "L1" and eq[1] == "L1"
        assert wt[2] < eq[2]                      # 有效樣本數是權重和，老樣本幾乎不算
        assert eq[2] == 4

    def test_n_eff_is_rounded_weight_sum(self):
        import dataclasses
        rows = [_obs(1000.0, 0, "A", key="a0"), _obs(1000.0, 45, "A", key="a45"),
                _obs(1000.0, 45, "A", key="a45b"), _obs(800.0, 100, "B")]
        p = dataclasses.replace(PRIOR, half_life_days=45.0)
        _, lvl, n = ValuationModel(rows, p, as_of=AS_OF).predict_log("A", "ultra", 9.0)
        assert lvl == "L1" and n == 2             # 1 + 0.5 + 0.5 = 2.0

    def test_valuator_passes_as_of_and_stays_deterministic(self):
        import dataclasses
        rows = _rows()
        p = dataclasses.replace(PRIOR, half_life_days=45.0)
        v1 = Valuator(rows, p, as_of=AS_OF)
        v2 = Valuator(rows, p, as_of=AS_OF)
        assert v1.model.predict_log("A", "ultra", 9.0) == v2.model.predict_log("A", "ultra", 9.0)
        assert v1.model.as_of == AS_OF
        assert v1.cal_model is None or v1.cal_model.as_of == AS_OF


from ygo_sniper.valuation import coverage_time_split  # noqa: E402


class TestCoverageTimeSplitTails:
    def _rows(self):
        out = []
        for i in range(60):
            out.append(_obs(1000.0 + (i % 7) * 50, 200 - i * 3, "A", key=f"a{i}"))
            out.append(_obs(600.0 + (i % 5) * 40, 200 - i * 3, "B", key=f"b{i}"))
        return out

    def test_reports_lower_and_upper_tail_and_as_of(self):
        res = coverage_time_split(self._rows(), PRIOR, test_fraction=0.25)
        assert 0.0 <= res["lower_tail"] <= 1.0 and 0.0 <= res["upper_tail"] <= 1.0
        assert res["empirical"] == pytest.approx(1.0 - res["lower_tail"] - res["upper_tail"])
        assert res["as_of"] == res["train_until"]

    def test_half_life_changes_result_but_not_split(self):
        import dataclasses
        base = coverage_time_split(self._rows(), PRIOR, test_fraction=0.25)
        wt = coverage_time_split(self._rows(), dataclasses.replace(PRIOR, half_life_days=30.0),
                                 test_fraction=0.25)
        assert wt["n_tested"] == base["n_tested"] and wt["train_until"] == base["train_until"]
        assert wt["half_life_days"] == 30.0 and base["half_life_days"] is None

    def test_unparseable_train_until_raises(self):
        # 創建行，其 sold_at 都是無法解析的字符串；train_until 會被設成最大的那個，
        # _parse_obs_date 無法解析之，觸發 ValueError。
        rows = [Obs(1000.0 + i*10, "A", "ultra", 9.0, key=f"invalid{i}", sold_at=f"bad-{i:02d}")
                for i in range(25)]
        with pytest.raises(ValueError, match="as_of"):
            coverage_time_split(rows, PRIOR, test_fraction=0.25)
