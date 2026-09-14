"""市場漂移監視器：同卡重複成交倍率、外部指數解析、告警帳本。"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ygo_sniper.config import load_config
from ygo_sniper.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


class TestConfig:
    def test_market_drift_section_is_loaded(self):
        cfg = load_config()
        md = cfg.market_drift
        assert md["recent_days"] == 28
        assert md["base_days"] == 56
        assert md["alert_ratio"] == 0.90
        assert md["min_pairs"] == 8
        assert md["external_url"].startswith("https://yugioh-relief.com/")
        assert md["external_enabled"] is True


def _insert_comp(store, *, signature, sold_at, price, grader="PSA", grade=10.0,
                 ingest=0, era="1999", dup_of=None, currency="JPY", site="buyee_yahoo"):
    with store._conn() as c:
        c.execute(
            "INSERT INTO comps (signature, title, price_twd, price_native, currency, site, "
            "sold_at, grader, grade, era_evidence, sold_at_is_ingest, dup_of_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (signature, f"t {signature}", price * 0.22, price, currency, site,
             sold_at, grader, grade, era, ingest, dup_of),
        )


class TestCompsForDrift:
    def test_only_real_dated_era_verified_non_dup_rows(self, store):
        _insert_comp(store, signature="a", sold_at="2026-09-01T00:00:00+00:00", price=1000)
        _insert_comp(store, signature="b", sold_at="2026-09-01T00:00:00+00:00", price=1000, ingest=1)
        _insert_comp(store, signature="c", sold_at="2026-09-01T00:00:00+00:00", price=1000, era="")
        _insert_comp(store, signature="d", sold_at="2026-09-01T00:00:00+00:00", price=1000, dup_of=1)
        _insert_comp(store, signature="e", sold_at="2026-09-01T00:00:00+00:00", price=0)
        rows = store.comps_for_drift()
        assert [r["signature"] for r in rows] == ["a"]
        assert set(rows[0]) >= {"signature", "sold_at", "price_native", "currency", "grader", "grade"}


from ygo_sniper.market_drift import (  # noqa: E402
    ALL_BAND,
    BANDS,
    DriftPoint,
    band_of,
    drift_series,
    evaluate_alert,
    repeat_sale_ratio,
)


def _row(sig, day, price, grader="PSA", grade=10.0):
    return {"signature": sig, "sold_at": f"2026-{day}T12:00:00+00:00", "price_native": price,
            "currency": "JPY", "grader": grader, "grade": grade, "site": "buyee_yahoo"}


AS_OF = datetime(2026, 9, 14, tzinfo=UTC)


class TestBandOf:
    def test_bands(self):
        assert band_of("PSA", 10.0) == "PSA10"
        assert band_of("psa", 9.0) == "PSA9"
        assert band_of("PSA", 8.0) == "PSA<=8"
        assert band_of("ARS", 10.0) == "ARS10"
        assert band_of("ARS", 9.5) == "ARS9"
        assert band_of("ARS", 7.0) == "ARS<=8"

    def test_unknown_grade_or_grader_is_excluded(self):
        assert band_of("PSA", None) is None
        assert band_of("BGS", 10.0) is None
        assert band_of(None, 10.0) is None


class TestRepeatSaleRatio:
    def test_pairs_same_signature_recent_vs_base(self):
        rows = [
            # 卡 a：前 56 天 1000、近 28 天 800 → ×0.8
            _row("a", "07-10", 1000), _row("a", "09-01", 800),
            # 卡 b：前 56 天 2000（兩筆中位 2000）、近 28 天 1800 → ×0.9
            _row("b", "07-20", 1900), _row("b", "08-01", 2100), _row("b", "09-05", 1800),
            # 卡 c：只有近期 → 不配對
            _row("c", "09-02", 500),
            # 卡 d：太舊（超過 84 天）→ 不進 base
            _row("d", "06-01", 100), _row("d", "09-03", 50),
        ]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        p = pts["PSA10"]
        assert p.n_pairs == 2
        assert p.ratio == pytest.approx(math.exp((math.log(0.8) + math.log(0.9)) / 2))
        assert p.n_recent == 4 and p.n_base == 3
        assert pts[ALL_BAND].n_pairs == 2
        assert p.as_of == "2026-09-14"

    def test_no_pairs_gives_none_ratio_not_one(self):
        rows = [_row("a", "09-01", 800)]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].ratio is None
        assert pts["PSA10"].n_pairs == 0

    def test_every_band_is_reported_even_when_empty(self):
        pts = repeat_sale_ratio([], as_of=AS_OF)
        assert [p.band for p in pts] == list(BANDS) + [ALL_BAND]

    def test_non_jpy_and_nonpositive_rows_are_skipped(self):
        rows = [_row("a", "07-10", 1000), _row("a", "09-01", 800)]
        rows[1]["currency"] = "USD"
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].n_pairs == 0


class TestDriftSeries:
    def test_series_is_weekly_ascending_and_ends_at_as_of(self):
        rows = [_row("a", "07-10", 1000), _row("a", "09-01", 800)]
        pts = drift_series(rows, as_of=AS_OF, weeks=3)
        as_ofs = sorted({p.as_of for p in pts})
        assert as_ofs == ["2026-08-31", "2026-09-07", "2026-09-14"]
        assert len(pts) == 3 * (len(BANDS) + 1)


class TestEvaluateAlert:
    def _pt(self, band, ratio, n):
        return DriftPoint(band=band, as_of="2026-09-14", ratio=ratio, n_pairs=n,
                          n_recent=n, n_base=n)

    def test_alert_when_ratio_at_or_below_threshold_with_enough_pairs(self):
        pts = [self._pt("PSA10", 0.85, 9), self._pt("PSA9", 0.97, 20)]
        text, bands = evaluate_alert(pts, alert_ratio=0.90, min_pairs=8)
        assert bands == ["PSA10"]
        assert "PSA10" in text and "×0.85" in text and "n=9" in text
        assert "PSA9" not in text

    def test_insufficient_pairs_never_alerts(self):
        pts = [self._pt("PSA10", 0.50, 7)]
        assert evaluate_alert(pts, alert_ratio=0.90, min_pairs=8) == (None, [])

    def test_none_ratio_never_alerts(self):
        pts = [self._pt("PSA10", None, 0)]
        assert evaluate_alert(pts, alert_ratio=0.90, min_pairs=8) == (None, [])

    def test_all_band_is_not_used_for_alert(self):
        pts = [self._pt(ALL_BAND, 0.5, 50)]
        assert evaluate_alert(pts, alert_ratio=0.90, min_pairs=8) == (None, [])
