"""高價帶掃描修正回合 Task 11（W2／W3／W4）：兩帶並行時彼此不互相污染。

三個獨立的隔離面，各自對應 plan「修正回合」Task 11 的一點：

  W3 推播候選分帶——`daily`（std）與 `daily-high` 各自只評估自己那一帶的
     候選，不消耗對方的 per-run 上限。驗證分兩層：
     (a) `store.notification_candidates(band=...)` 真的按 band 篩選
         （這是修法的核心：候選在進 `evaluate()` 之前就已經分帶）；
     (b) `Pipeline.notification_outcome`／`notify`／CLI 的 `_run_notifications`
         把 band 一路透傳到 (a)，band=None 時維持舊呼叫形狀（不傳關鍵字），
         相容尚未升級的呼叫端。

  W2 健康告警帳本分帶——高價帶輪的 alerts 指紋帶 `@high` 標記，std 指紋
     不變；一帶的成功／失敗都不動另一帶的 occurrences／notify_count。

  W4 sqlite WAL——`Store._conn()` 開 `journal_mode=WAL`／`busy_timeout=30000`，
     兩條獨立排程（run_daily.sh／run_high.sh）並行寫同一顆 db 不會立刻鎖死。

見 `docs/superpowers/plans/2026-08-22-high-band-scan.md` 修正回合 Task 11。
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta

import pytest

from ygo_sniper.alerts import AlertEngine
from ygo_sniper.cli import _run_notifications
from ygo_sniper.notify_rules import Outcome
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.store import Store

T0 = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


# ===========================================================================
# W3(a) — store 層：候選查詢真的按 band 篩選
# ===========================================================================
def _seed_signal(store: Store, key: str, *, band: str, state: str = "new") -> None:
    with store._conn() as c:  # noqa: SLF001 - 直接塞列，不必跑整條掃描
        c.execute(
            "INSERT INTO signals (key, site, external_id, title, url, state, score, band)"
            " VALUES (?, 'buyee_yahoo', ?, 't', 'u', ?, 1, ?)",
            (key, key, state, band),
        )


def test_notification_candidates_band_filter_isolates_pools(tmp_path):
    st = Store(tmp_path / "cand.db")
    _seed_signal(st, "std1", band="std")
    _seed_signal(st, "high1", band="high")

    assert [r["key"] for r in st.notification_candidates(band="std")] == ["std1"]
    assert [r["key"] for r in st.notification_candidates(band="high")] == ["high1"]
    # band=None（notify-preview 等手動指令）＝全帶，除錯要看得到全貌
    assert sorted(r["key"] for r in st.notification_candidates()) == ["high1", "std1"]


# ===========================================================================
# W3(b) — Pipeline 層：band 一路透傳到候選查詢
# ===========================================================================
@pytest.fixture
def no_fx_network(monkeypatch):
    """`Pipeline()` 建 FxRates 時若 fx.json 過期會發真實 httpx 請求，測試絕不碰網路。"""
    from ygo_sniper.fx import FxRates

    monkeypatch.setattr(FxRates, "refresh", lambda self: None)


@pytest.fixture
def pipeline(tmp_path, no_fx_network):
    """真的 `Pipeline`，db 在 tmp_path、不碰網路（比照 test_card_snipe.py 的同名 fixture）。"""
    import ygo_sniper.config as config_mod
    from ygo_sniper.pipeline import Pipeline

    config_mod.load_config.cache_clear()
    base = config_mod.load_config()
    cfg = dc_replace(base, storage={**base.storage, "db_path": str(tmp_path / "p.db")})
    pipe = Pipeline(cfg)
    try:
        yield pipe
    finally:
        pipe.close()
        config_mod.load_config.cache_clear()


def test_notification_outcome_forwards_band_to_candidate_query(pipeline, monkeypatch):
    calls: list[str | None] = []
    orig = pipeline.store.notification_candidates

    def spy(*a, **kw):
        calls.append(kw.get("band"))
        return orig(*a, **kw)

    monkeypatch.setattr(pipeline.store, "notification_candidates", spy)

    pipeline.notification_outcome(band="high")
    pipeline.notification_outcome(band="std")
    pipeline.notification_outcome()

    assert calls == ["high", "std", None]


def test_notify_forwards_band_to_notification_outcome(pipeline, monkeypatch):
    calls: list[str | None] = []
    orig = pipeline.notification_outcome

    def spy(*a, **kw):
        calls.append(kw.get("band"))
        return orig(*a, **kw)

    monkeypatch.setattr(pipeline, "notification_outcome", spy)
    monkeypatch.setattr(pipeline.notifier, "send_rule_matches", lambda outcome: [])

    pipeline.notify(band="high")
    pipeline.notify(band="std")

    assert calls == ["high", "std"]


# ===========================================================================
# W3(b) — CLI 層：`_run_notifications` 把 band 透傳給 Pipeline，
# band=None 時維持舊呼叫形狀（不傳關鍵字），相容尚未升級的假 Pipeline。
# ===========================================================================
class _FakeCfg:
    def __init__(self):
        self.notify: dict = {"silent_when_empty": True, "enabled": True}


class _FakeNotifier:
    def send_summary(self, *a, **kw):
        pass

    def send_alert(self, text):
        return True

    def send_recovery(self, text):
        return True


class _FakeStore:
    @staticmethod
    def stats():
        return {"by_state": {}, "comps": 0}


class _FakeAlerts:
    def mark_sent(self, sent):
        pass


def _result(alerts=None) -> dict:
    return {"scanned": 0, "signals": 0, "sources": {}, "alerts": alerts or []}


class _NoBandFakePipeline:
    """沒有 `band` 參數的舊式假 Pipeline（模擬 test_cli_notify.py 的 FakePipeline
    介面／尚未升級的呼叫端）。`_run_notifications` 在 band=None 時絕不能傳
    `band=` 關鍵字下去，否則這個假物件會直接 TypeError——這是回歸守衛。
    """

    def __init__(self):
        self.cfg = _FakeCfg()
        self.notifier = _FakeNotifier()
        self.store = _FakeStore()
        self.alerts = _FakeAlerts()
        self.notify_calls = 0
        self.outcome_calls = 0

    def notify(self):
        self.notify_calls += 1
        return Outcome()

    def notification_outcome(self):
        self.outcome_calls += 1
        return Outcome()


def test_run_notifications_without_band_keeps_old_bare_call_shape():
    pipe = _NoBandFakePipeline()
    n = _run_notifications(pipe, _result())
    assert n == 0
    assert pipe.notify_calls == 1


def test_run_notifications_without_band_disabled_path_keeps_old_bare_call_shape():
    pipe = _NoBandFakePipeline()
    pipe.cfg.notify["enabled"] = False
    n = _run_notifications(pipe, _result())
    assert n == 0
    assert pipe.outcome_calls == 1


class _BandAwareFakePipeline:
    """接受 `band` 關鍵字的假 Pipeline，記錄每次收到的值。"""

    def __init__(self):
        self.cfg = _FakeCfg()
        self.notifier = _FakeNotifier()
        self.store = _FakeStore()
        self.alerts = _FakeAlerts()
        self.notify_band_calls: list[str | None] = []
        self.outcome_band_calls: list[str | None] = []

    def notify(self, *, band=None):
        self.notify_band_calls.append(band)
        return Outcome()

    def notification_outcome(self, *, band=None):
        self.outcome_band_calls.append(band)
        return Outcome()


def test_run_notifications_forwards_explicit_band_std_and_high():
    pipe_std = _BandAwareFakePipeline()
    _run_notifications(pipe_std, _result(), band="std")
    assert pipe_std.notify_band_calls == ["std"]

    pipe_high = _BandAwareFakePipeline()
    _run_notifications(pipe_high, _result(), band="high")
    assert pipe_high.notify_band_calls == ["high"]


def test_report_notify_disabled_forwards_band():
    pipe = _BandAwareFakePipeline()
    pipe.cfg.notify["enabled"] = False
    _run_notifications(pipe, _result(), band="high")
    assert pipe.outcome_band_calls == ["high"]


def test_report_notify_disabled_counts_rule_5_high_band(capsys):
    """S1：停用期間的輸出也要講規則 5（高價帶折價）命中幾筆，不能只講前四條規則。"""
    pipe = _BandAwareFakePipeline()
    pipe.cfg.notify["enabled"] = False

    def _outcome_with_high_band(*, band=None):
        pipe.outcome_band_calls.append(band)
        out = Outcome()
        out.high_band = [object(), object()]
        return out

    pipe.notification_outcome = _outcome_with_high_band
    _run_notifications(pipe, _result(), band="high")

    out = capsys.readouterr().out
    assert "高價帶折價 2 筆" in out


# ===========================================================================
# W2 — alerts 帳本分帶：高價帶指紋帶 @high、std 指紋不變，兩本帳互不消耗
# ===========================================================================
def _res(source="buyee_mercari", health=ParseHealth.PARSER_BROKEN, **kw) -> SearchResult:
    return SearchResult(
        source=source,
        site=kw.pop("site", "buyee_mercari"),
        query=kw.pop("query", "PSA 初期"),
        listings=[],
        health=health,
        url=kw.pop("url", "https://example.test/search"),
        detail=kw.pop("detail", ""),
        parsed_count=kw.pop("parsed", 0),
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "alerts.db")


@pytest.fixture
def engine(cfg, store):
    return AlertEngine(cfg, store)


def test_high_band_fingerprint_is_tagged_std_fingerprint_unchanged(engine):
    std_alerts = engine.evaluate([_res(health=ParseHealth.PARSER_BROKEN)], now=T0, band="std")
    assert std_alerts[0].fingerprints == ("buyee_mercari:parser_broken",)

    high_alerts = engine.evaluate(
        [_res(health=ParseHealth.PARSER_BROKEN)], now=T0, band="high"
    )
    assert high_alerts[0].fingerprints == ("buyee_mercari@high:parser_broken",)


def test_high_band_default_band_is_std_when_omitted(engine):
    """呼叫端沒有升級（不傳 band）＝ 既有行為零改動：指紋與傳 band='std' 時完全一樣。"""
    alerts = engine.evaluate([_res(health=ParseHealth.PARSER_BROKEN)], now=T0)
    assert alerts[0].fingerprints == ("buyee_mercari:parser_broken",)


def test_high_band_failure_does_not_touch_std_occurrences(engine):
    """std 先壞一次落觀測帳；高價帶輪同一來源同樣壞掉，不該疊加進 std 的列。"""
    engine.evaluate([_res(health=ParseHealth.PARSER_BROKEN)], now=T0, band="std")
    std_before = engine.store.get_alert("buyee_mercari:parser_broken")
    assert std_before["occurrences"] == 1

    engine.evaluate(
        [_res(health=ParseHealth.PARSER_BROKEN)], now=T0 + timedelta(minutes=5), band="high"
    )

    std_after = engine.store.get_alert("buyee_mercari:parser_broken")
    assert std_after["occurrences"] == 1, "高價帶輪的觀測不該疊加進 std 的 occurrences"
    high_row = engine.store.get_alert("buyee_mercari@high:parser_broken")
    assert high_row is not None and high_row["occurrences"] == 1


def test_std_failure_does_not_touch_high_band_occurrences(engine):
    """反向：high 先壞一次；std 輪同來源同樣壞掉，不該疊加進 high 的列。"""
    engine.evaluate([_res(health=ParseHealth.PARSER_BROKEN)], now=T0, band="high")
    high_before = engine.store.get_alert("buyee_mercari@high:parser_broken")
    assert high_before["occurrences"] == 1

    engine.evaluate(
        [_res(health=ParseHealth.PARSER_BROKEN)], now=T0 + timedelta(minutes=5), band="std"
    )

    high_after = engine.store.get_alert("buyee_mercari@high:parser_broken")
    assert high_after["occurrences"] == 1, "std 輪的觀測不該疊加進高價帶的 occurrences"


def test_high_band_success_does_not_clear_std_alert_row(engine):
    broken = engine.evaluate([_res(health=ParseHealth.BLOCKED)], now=T0, band="std")
    engine.mark_sent(broken, now=T0)
    assert engine.store.get_alert("buyee_mercari:blocked") is not None

    recovered = engine.evaluate(
        [_res(health=ParseHealth.OK, listings=[], parsed=5)],
        now=T0 + timedelta(hours=50),
        band="high",
    )

    assert recovered == [], "高價帶從沒吵過、這次 OK 應該靜默，不該對 std 的病史發復原"
    assert engine.store.get_alert("buyee_mercari:blocked") is not None, (
        "std 的病史被高價帶那一輪的成功誤清"
    )


def test_std_success_does_not_clear_high_band_alert_row(engine):
    broken = engine.evaluate([_res(health=ParseHealth.BLOCKED)], now=T0, band="high")
    engine.mark_sent(broken, now=T0)
    assert engine.store.get_alert("buyee_mercari@high:blocked") is not None

    recovered = engine.evaluate(
        [_res(health=ParseHealth.OK, listings=[], parsed=5)],
        now=T0 + timedelta(hours=50),
        band="std",
    )

    assert recovered == []
    assert engine.store.get_alert("buyee_mercari@high:blocked") is not None, (
        "高價帶的病史被 std 那一輪的成功誤清"
    )


# ===========================================================================
# W4 — sqlite WAL／busy_timeout：兩條獨立排程並行寫同一顆 db
# ===========================================================================
def test_store_connection_uses_wal_and_busy_timeout(tmp_path):
    st = Store(tmp_path / "wal.db")
    with st._conn() as c:  # noqa: SLF001
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = c.execute("PRAGMA busy_timeout").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert timeout == 30000
