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
                 ingest=0, era="1999", dup_of=None, currency="JPY", site="buyee_yahoo",
                 sale_kind="auction", set_code="LOB-001"):
    with store._conn() as c:
        c.execute(
            "INSERT INTO comps (signature, title, price_twd, price_native, currency, site, "
            "sold_at, grader, grade, era_evidence, sold_at_is_ingest, dup_of_id, sale_kind, set_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (signature, f"t {signature}", price * 0.22, price, currency, site,
             sold_at, grader, grade, era, ingest, dup_of, sale_kind, set_code),
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

    def test_requires_known_sale_kind_and_returns_title(self, store):
        _insert_comp(store, signature="ok", sold_at="2026-09-01T00:00:00+00:00", price=1000, set_code="")
        _insert_comp(store, signature="nokind", sold_at="2026-09-01T00:00:00+00:00", price=1000, sale_kind="unknown")
        rows = store.comps_for_drift()
        assert [r["signature"] for r in rows] == ["ok"]
        assert rows[0]["sale_kind"] == "auction" and rows[0]["title"] == "t ok" and "rarity" in rows[0]


from ygo_sniper.market_drift import (  # noqa: E402
    ALL_BAND,
    BANDS,
    DriftPoint,
    band_of,
    drift_series,
    evaluate_alert,
    repeat_sale_ratio,
)


def _row(sig, day, price, grader="PSA", grade=10.0, sale_kind="auction", card_name="卡A"):
    return {"signature": sig, "sold_at": f"2026-{day}T12:00:00+00:00", "price_native": price,
            "currency": "JPY", "grader": grader, "grade": grade, "site": "buyee_yahoo",
            "sale_kind": sale_kind, "card_name": card_name, "rarity": "ultra"}


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
            _row("a", "07-10", 1000, card_name="卡a"), _row("a", "09-01", 800, card_name="卡a"),
            # 卡 b：前 56 天 2000（兩筆中位 2000）、近 28 天 1800 → ×0.9
            _row("b", "07-20", 1900, card_name="卡b"), _row("b", "08-01", 2100, card_name="卡b"), _row("b", "09-05", 1800, card_name="卡b"),
            # 卡 c：只有近期 → 不配對
            _row("c", "09-02", 500, card_name="卡c"),
            # 卡 d：太舊（超過 84 天）→ 不進 base
            _row("d", "06-01", 100, card_name="卡d"), _row("d", "09-03", 50, card_name="卡d"),
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

    def test_cross_mechanism_sales_are_not_paired(self):
        rows = [_row("a", "07-10", 1000, sale_kind="auction"), _row("a", "09-01", 800, sale_kind="fixed")]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].n_pairs == 0
        assert pts["PSA10"].n_recent == 1 and pts["PSA10"].n_base == 1

    def test_same_card_two_mechanisms_give_two_pairs(self):
        rows = [_row("a", "07-10", 1000, sale_kind="auction"), _row("a", "09-01", 800, sale_kind="auction"),
                _row("a", "07-12", 2000, sale_kind="fixed"), _row("a", "09-02", 2000, sale_kind="fixed")]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].n_pairs == 2
        assert pts["PSA10"].ratio == pytest.approx(math.exp((math.log(0.8) + 0.0) / 2))

    def test_unknown_sale_kind_rows_are_skipped(self):
        rows = [_row("a", "07-10", 1000, sale_kind="unknown"), _row("a", "09-01", 800, sale_kind="unknown")]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].n_pairs == 0 and pts["PSA10"].n_recent == 0

    def test_same_card_same_kind_different_sites_are_not_paired(self):
        a = _row("a", "07-10", 1000, sale_kind="fixed"); a["site"] = "buyee_paypay"
        b = _row("a", "09-01", 800, sale_kind="fixed"); b["site"] = "buyee_yahoo"
        pts = {p.band: p for p in repeat_sale_ratio([a, b], as_of=AS_OF)}
        assert pts["PSA10"].n_pairs == 0

    def test_pairs_by_card_identity_not_signature(self):
        rows = [_row("sigX", "07-10", 1000, card_name="青眼の白龍"), _row("sigY", "09-01", 800, card_name="青眼の白龍")]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].n_pairs == 1 and pts["PSA10"].ratio == pytest.approx(0.8)

    def test_rows_without_card_name_are_skipped(self):
        rows = [_row("a", "07-10", 1000, card_name=None), _row("a", "09-01", 800, card_name=None)]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].n_pairs == 0 and pts["PSA10"].n_recent == 0

    def test_different_rarity_same_name_not_paired(self):
        a = _row("a", "07-10", 1000); b = _row("b", "09-01", 800); b["rarity"] = "secret"
        pts = {p.band: p for p in repeat_sale_ratio([a, b], as_of=AS_OF)}
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
        assert "2026-09-14" in text
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


from ygo_sniper.market_drift import fetch_relief_index, parse_relief_weekly_index  # noqa: E402


class TestReliefIndex:
    def test_parse_real_snippet(self):
        html = (FIXTURES / "relief_trends_snippet.html").read_text(encoding="utf-8")
        parsed = parse_relief_weekly_index(html)
        weeks = parsed["weeks"]
        assert len(weeks) == 37
        assert weeks[0]["week_start"] == "2025-12-29" and weeks[0]["all"] == 100.0
        last = weeks[-1]
        assert last["week_start"] == "2026-09-07"
        assert last["all"] == 124.43 and last["psa10"] == 138.12
        assert last["psa9"] == 125.46 and last["psa8_below"] == 109.7
        assert last["updated"] == 72 and last["components"] == 1205
        assert parsed["meta"]["representativePrice"] == "weekly_median_carry_forward"

    def test_missing_block_raises(self):
        with pytest.raises(ValueError):
            parse_relief_weekly_index("<html><body>nothing</body></html>")

    def test_fetch_uses_fetcher_and_returns_last_12_weeks(self):
        html = (FIXTURES / "relief_trends_snippet.html").read_text(encoding="utf-8")

        class FakeFetcher:
            def __init__(self):
                self.calls = []

            def get(self, url, **kw):
                self.calls.append((url, kw))
                return html

        f = FakeFetcher()
        out = fetch_relief_index(f, "https://yugioh-relief.com/psa-market-daily-trends/")
        assert f.calls[0][0] == "https://yugioh-relief.com/psa-market-daily-trends/"
        assert f.calls[0][1] == {"use_cache": False}
        assert len(out["weeks"]) == 12
        assert out["weeks"][-1]["week_start"] == "2026-09-07"
        assert "fetched_at" in out


from ygo_sniper.market_drift import (  # noqa: E402
    DRIFT_LATEST_KEY,
    DRIFT_PENDING_KEY,
    DRIFT_SENT_KEY,
    RELIEF_ERROR_KEY,
    RELIEF_LATEST_KEY,
    deliver_drift_alert,
    refresh_market_drift,
)
from ygo_sniper.sources.base import FetchError  # noqa: E402


class _FakeIndex:
    """title → 卡名：把 `t a` 讀成卡 a、`t b` 讀成卡 b（in_era 恆 True）。"""
    available = True

    class _M:
        def __init__(self, name):
            self.name_ja = name
            self.in_era = True

    def match(self, title):
        return self._M(title.replace("t ", "", 1)) if title.startswith("t ") else None


class _Cfg:
    def __init__(self, external_enabled=True):
        self.market_drift = {
            "recent_days": 28, "base_days": 56, "alert_ratio": 0.90, "min_pairs": 1,
            "external_enabled": external_enabled,
            "external_url": "https://yugioh-relief.com/psa-market-daily-trends/",
        }


class _FetcherOK:
    def get(self, url, **kw):
        return (FIXTURES / "relief_trends_snippet.html").read_text(encoding="utf-8")


class _FetcherDown:
    def get(self, url, **kw):
        raise FetchError("boom", url=url, status=503)


class _Notifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def send_alert(self, text):
        self.sent.append(text)
        return self.ok


def _seed_drop(store):
    """PSA10：a、b 兩張卡各跌到 0.8 → 帶倍率 0.8，配對 2。"""
    for sig in ("a", "b"):
        _insert_comp(store, signature=sig, sold_at="2026-07-20T00:00:00+00:00", price=1000)
        _insert_comp(store, signature=sig, sold_at="2026-09-01T00:00:00+00:00", price=800)


class TestRefreshMarketDrift:
    def test_persists_local_series_and_external(self, store):
        _seed_drop(store)
        res = refresh_market_drift(store, _FetcherOK(), _Cfg(), index=_FakeIndex(), now=AS_OF)
        latest = json.loads(store.get_meta(DRIFT_LATEST_KEY))
        assert latest["as_of"] == "2026-09-14"
        assert latest["recent_days"] == 28 and latest["base_days"] == 56
        psa10 = [p for p in latest["latest"] if p["band"] == "PSA10"][0]
        assert psa10["ratio"] == pytest.approx(0.8) and psa10["n_pairs"] == 2
        assert len(latest["series"]) == 12 * 7
        assert latest["alert_bands"] == ["PSA10"]
        ext = json.loads(store.get_meta(RELIEF_LATEST_KEY))
        assert ext["weeks"][-1]["week_start"] == "2026-09-07"
        assert store.get_meta(RELIEF_ERROR_KEY, "") == ""
        assert res["alert_text"] is not None and "PSA10" in res["alert_text"]
        pending = json.loads(store.get_meta(DRIFT_PENDING_KEY))
        assert pending["bands"] == {"PSA10": "2026-09-14"}

    def test_external_failure_is_recorded_not_raised(self, store):
        _seed_drop(store)
        refresh_market_drift(store, _FetcherDown(), _Cfg(), index=_FakeIndex(), now=AS_OF)
        assert store.get_meta(RELIEF_LATEST_KEY) is None
        assert "boom" in store.get_meta(RELIEF_ERROR_KEY)
        assert store.get_meta(DRIFT_LATEST_KEY) is not None  # 本地指數不受外部失敗影響

    def test_external_disabled_skips_fetch(self, store):
        _seed_drop(store)
        refresh_market_drift(store, _FetcherDown(), _Cfg(external_enabled=False), index=_FakeIndex(), now=AS_OF)
        assert store.get_meta(RELIEF_ERROR_KEY, "") == ""

    def test_already_sent_this_week_does_not_re_pend(self, store):
        _seed_drop(store)
        store.set_meta(DRIFT_SENT_KEY, json.dumps({"PSA10": "2026-09-14"}))
        res = refresh_market_drift(store, _FetcherOK(), _Cfg(), index=_FakeIndex(), now=AS_OF)
        assert res["alert_text"] is None
        assert store.get_meta(DRIFT_PENDING_KEY, "") == ""


class TestDeliverDriftAlert:
    def test_nothing_pending_is_noop_true(self, store):
        n = _Notifier()
        assert deliver_drift_alert(store, n) is True
        assert n.sent == []

    def test_delivered_clears_pending_and_records_sent(self, store):
        _seed_drop(store)
        refresh_market_drift(store, _FetcherOK(), _Cfg(), index=_FakeIndex(), now=AS_OF)
        n = _Notifier(ok=True)
        assert deliver_drift_alert(store, n) is True
        assert len(n.sent) == 1 and "PSA10" in n.sent[0]
        assert store.get_meta(DRIFT_PENDING_KEY, "") == ""
        assert json.loads(store.get_meta(DRIFT_SENT_KEY)) == {"PSA10": "2026-09-14"}

    def test_failed_send_keeps_pending(self, store):
        _seed_drop(store)
        refresh_market_drift(store, _FetcherOK(), _Cfg(), index=_FakeIndex(), now=AS_OF)
        n = _Notifier(ok=False)
        assert deliver_drift_alert(store, n) is False
        assert store.get_meta(DRIFT_PENDING_KEY, "") != ""
        assert store.get_meta(DRIFT_SENT_KEY) is None
        # 下一輪重送成功才清
        n2 = _Notifier(ok=True)
        assert deliver_drift_alert(store, n2) is True
        assert store.get_meta(DRIFT_PENDING_KEY, "") == ""


class TestPendingMerge:
    def test_undelivered_band_survives_next_days_refresh(self, store):
        cfg = _Cfg(external_enabled=False)
        # day 1：PSA10 跌 → pending，送失敗
        _insert_comp(store, signature="a", sold_at="2026-07-20T00:00:00+00:00", price=1000)
        _insert_comp(store, signature="a", sold_at="2026-09-01T00:00:00+00:00", price=800)
        refresh_market_drift(store, _FetcherDown(), cfg, index=_FakeIndex(), now=datetime(2026, 9, 13, tzinfo=UTC))
        assert deliver_drift_alert(store, _Notifier(ok=False)) is False
        # day 2：PSA10 回升、PSA9 跌
        _insert_comp(store, signature="a", sold_at="2026-09-12T00:00:00+00:00", price=1300)
        _insert_comp(store, signature="b", sold_at="2026-07-20T00:00:00+00:00", price=1000, grade=9.0)
        _insert_comp(store, signature="b", sold_at="2026-09-10T00:00:00+00:00", price=700, grade=9.0)
        refresh_market_drift(store, _FetcherDown(), cfg, index=_FakeIndex(), now=datetime(2026, 9, 14, tzinfo=UTC))
        pending = json.loads(store.get_meta(DRIFT_PENDING_KEY))
        assert pending["bands"] == {"PSA10": "2026-09-13", "PSA9": "2026-09-14"}
        n = _Notifier(ok=True)
        assert deliver_drift_alert(store, n) is True
        assert "PSA10" in n.sent[0] and "PSA9" in n.sent[0]
        assert json.loads(store.get_meta(DRIFT_SENT_KEY)) == {"PSA10": "2026-09-13", "PSA9": "2026-09-14"}

    def test_same_band_newer_day_replaces_its_own_line(self, store):
        cfg = _Cfg(external_enabled=False)
        _insert_comp(store, signature="a", sold_at="2026-07-20T00:00:00+00:00", price=1000)
        _insert_comp(store, signature="a", sold_at="2026-09-01T00:00:00+00:00", price=800)
        refresh_market_drift(store, _FetcherDown(), cfg, index=_FakeIndex(), now=datetime(2026, 9, 13, tzinfo=UTC))
        refresh_market_drift(store, _FetcherDown(), cfg, index=_FakeIndex(), now=datetime(2026, 9, 14, tzinfo=UTC))
        pending = json.loads(store.get_meta(DRIFT_PENDING_KEY))
        assert pending["bands"] == {"PSA10": "2026-09-14"}
        assert pending["text"].count("PSA10") == 1

    def test_old_format_pending_without_lines_is_discarded(self, store):
        store.set_meta(DRIFT_PENDING_KEY, json.dumps({"text": "old", "bands": {"PSA10": "2026-09-01"}}))
        refresh_market_drift(store, _FetcherDown(), _Cfg(external_enabled=False),
                             index=_FakeIndex(), now=datetime(2026, 9, 14, tzinfo=UTC))
        assert store.get_meta(DRIFT_PENDING_KEY, "") in ("", None) or \
            json.loads(store.get_meta(DRIFT_PENDING_KEY))["lines"]
        n = _Notifier(ok=True)
        deliver_drift_alert(store, n)
        assert all("PSA10" in t for t in n.sent)   # 不會送出沒有任何帶的空身告警


from ygo_sniper.cli import _drift_table_rows  # noqa: E402


class TestDriftTableRows:
    def test_rows_are_human_readable(self):
        latest = {
            "as_of": "2026-09-14", "recent_days": 28, "base_days": 56, "min_pairs": 8,
            "alert_bands": ["PSA10"],
            "latest": [
                {"band": "PSA10", "as_of": "2026-09-14", "ratio": 0.85, "n_pairs": 9, "n_recent": 20, "n_base": 40},
                {"band": "PSA9", "as_of": "2026-09-14", "ratio": None, "n_pairs": 0, "n_recent": 3, "n_base": 0},
                {"band": "ARS10", "as_of": "2026-09-14", "ratio": 1.02, "n_pairs": 4, "n_recent": 8, "n_base": 9},
            ],
        }
        rows = _drift_table_rows(latest)
        assert rows[0] == ("PSA10", "×0.85", "9", "20 / 40", "📉 告警")
        assert rows[1] == ("PSA9", "—", "0", "3 / 0", "樣本不足（讀不到≠沒跌）")
        assert rows[2] == ("ARS10", "×1.02", "4", "8 / 9", "配對 <8，僅供參考")


from ygo_sniper import cli as cli_mod  # noqa: E402
from ygo_sniper.market_drift import DRIFT_COMPUTED_DATE_KEY  # noqa: E402


class _Pipe:
    def __init__(self, store):
        self.store = store
        self.fetcher = _FetcherDown()
        self.cfg = _Cfg(external_enabled=False)
        self.notifier = _Notifier(ok=True)
        self.index = _FakeIndex()


class TestDailyHook:
    def test_daily_refreshes_once_per_day_but_delivers_every_run(self, store, monkeypatch):
        calls = {"refresh": 0, "deliver": 0}
        real_refresh = cli_mod.refresh_market_drift
        real_deliver = cli_mod.deliver_drift_alert

        def spy_refresh(*a, **kw):
            calls["refresh"] += 1
            return real_refresh(*a, **kw)

        def spy_deliver(*a, **kw):
            calls["deliver"] += 1
            return real_deliver(*a, **kw)

        monkeypatch.setattr(cli_mod, "refresh_market_drift", spy_refresh)
        monkeypatch.setattr(cli_mod, "deliver_drift_alert", spy_deliver)
        pipe = _Pipe(store)
        cli_mod._refresh_market_drift_daily(pipe)
        cli_mod._refresh_market_drift_daily(pipe)
        assert calls == {"refresh": 1, "deliver": 2}
        assert store.get_meta(DRIFT_COMPUTED_DATE_KEY) is not None

    def test_daily_sends_alert_when_band_drops(self, store):
        _insert_comp(store, signature="a", sold_at="2026-07-20T00:00:00+00:00", price=1000)
        _insert_comp(store, signature="a", sold_at="2026-09-01T00:00:00+00:00", price=800)
        pipe = _Pipe(store)
        cli_mod._refresh_market_drift_daily(pipe, now=datetime(2026, 9, 14, tzinfo=UTC))
        assert any("PSA10" in t for t in pipe.notifier.sent)

    def test_daily_never_raises_when_refresh_explodes(self, store, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("db on fire")
        monkeypatch.setattr(cli_mod, "refresh_market_drift", boom)
        cli_mod._refresh_market_drift_daily(_Pipe(store))   # 不拋就是通過
        assert store.get_meta(DRIFT_COMPUTED_DATE_KEY) is None


class TestDailyCommandWiring:
    def test_daily_command_calls_market_drift_hook(self, monkeypatch, tmp_path):
        """拿掉 cli.daily() 裡的 `_refresh_market_drift_daily(pipe)` 這一行，這條要紅。"""
        from typer.testing import CliRunner

        seen = {}

        class FakePipe:
            def __init__(self):
                seen["pipe"] = self

            def scan(self, **kw):
                return {"scanned": 0, "candidates": 0, "signals": 0}

            def close(self):
                seen["closed"] = True

        monkeypatch.setattr(cli_mod, "Pipeline", FakePipe)
        monkeypatch.setattr(cli_mod, "_print_scan", lambda result: None)
        monkeypatch.setattr(cli_mod, "_mine_snipes_daily", lambda pipe: None)
        monkeypatch.setattr(cli_mod, "_run_notifications", lambda pipe, result, band=None: 0)
        monkeypatch.setattr(cli_mod, "_backfill_comps_daily", lambda pipe, **kw: seen.setdefault("backfill_pipe", pipe))
        monkeypatch.setattr(cli_mod, "_refresh_market_drift_daily",
                            lambda pipe, **kw: seen.setdefault("drift_pipe", pipe))
        result = CliRunner().invoke(cli_mod.app, ["daily", "--no-notify"])
        assert result.exit_code == 0, result.output
        assert seen.get("backfill_pipe") is seen["pipe"]
        assert seen.get("drift_pipe") is seen["pipe"]
        assert seen.get("closed") is True


class TestDailyBackfillHook:
    def test_daily_calls_backfill_slice_once_per_day(self, store, monkeypatch):
        calls = []
        monkeypatch.setattr(cli_mod, "run_backfill", lambda **kw: calls.append(kw) or type("R", (), {
            "summary": lambda self: "ok", "blocked": False, "problems": [], "cards_done": 0})())
        monkeypatch.setattr(cli_mod, "known_card_names", lambda store, index: ["a", "b"])
        pipe = _Pipe(store)
        pipe.cfg.market_drift.update({"backfill_daily_limit": 5, "backfill_cooldown_days": 21})
        pipe.sources = {}
        pipe.comps = object()
        cli_mod._backfill_comps_daily(pipe, now=datetime(2026, 9, 14, tzinfo=UTC))
        cli_mod._backfill_comps_daily(pipe, now=datetime(2026, 9, 14, tzinfo=UTC))
        assert len(calls) == 1
        assert calls[0]["limit"] == 5 and calls[0]["cooldown_days"] == 21
        assert calls[0]["pages"] == {"yahoo_closed": 1, "paypay_direct": 1}

    def test_daily_backfill_disabled_by_zero_limit(self, store, monkeypatch):
        calls = []
        monkeypatch.setattr(cli_mod, "run_backfill", lambda **kw: calls.append(kw))
        pipe = _Pipe(store)
        pipe.cfg.market_drift.update({"backfill_daily_limit": 0})
        cli_mod._backfill_comps_daily(pipe, now=datetime(2026, 9, 14, tzinfo=UTC))
        assert calls == []
