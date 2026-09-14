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
