"""告警引擎：去重、冷卻、復原、上限併列，以及 canary 判定。

全程零網路、零 Telegram（conftest 的 _mute_telegram 是 autouse，
真的送出會直接 AssertionError）。時間一律注入 `now=`，不 sleep——
測「三天後」不該花三天，更不該依賴機器時鐘。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_listing

from ygo_sniper.alerts import AlertEngine
from ygo_sniper.pipeline import canary_verdict
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.store import Store

T0 = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)


def _res(
    source="yahoo_direct", health=ParseHealth.OK, listings=0, parsed=None, **kw
) -> SearchResult:
    """`parsed` 預設等於 listings 筆數（沒有商業篩選的情境）；
    要測「篩選前 ≠ 篩選後」就明確傳它。"""
    return SearchResult(
        source=source,
        site=kw.pop("site", "buyee_yahoo"),
        query=kw.pop("query", "遊戯王 PSA"),
        listings=[make_listing(external_id=f"x{i}") for i in range(listings)],
        health=health,
        url=kw.pop("url", "https://example.test/search"),
        detail=kw.pop("detail", ""),
        parsed_count=listings if parsed is None else parsed,
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "alerts.db")


@pytest.fixture
def engine(cfg, store):
    return AlertEngine(cfg, store)


def _send(engine, alerts, now):
    """模擬「送出成功」——這是唯一會落冷卻帳的路徑。"""
    engine.mark_sent(alerts, now=now)


def test_telegram_is_muted_in_tests(cfg):
    """證明靜音 fixture 真的咬得到：工作樹裡有真 .env，送出點必須被封死。"""
    from ygo_sniper.notify import TelegramNotifier

    n = TelegramNotifier(cfg)
    with pytest.raises(AssertionError, match="不得送出真實 Telegram"):
        n.send_alert("這則絕不該真的送出去")
    with pytest.raises(AssertionError, match="不得送出真實 Telegram"):
        n.send_summary({"by_state": {}, "comps": 0}, 1, 1, {"yahoo_direct": {"health": "ok", "count": 9}})


# ---------------------------------------------------------------------------
# (a) 去重與冷卻序列
def test_parser_broken_alerts_once_then_cools_down(engine):
    """壞掉當天發一則；連續三天同一個 fingerprint 只發第一天；>72h 才再發。"""
    broken = [_res(health=ParseHealth.PARSER_BROKEN, detail="命中 200 筆但解析 0 筆")]

    day1 = engine.evaluate(broken, now=T0)
    assert len(day1) == 1
    assert "yahoo_direct" in day1[0] and "解析壞了" in day1[0]
    assert day1[0].fingerprints == ("yahoo_direct:parser_broken",)
    _send(engine, day1, T0)

    # 第 2、3 天：病還在，但不重複吵
    assert engine.evaluate(broken, now=T0 + timedelta(hours=24)) == []
    assert engine.evaluate(broken, now=T0 + timedelta(hours=48)) == []

    # 第 4 天（>72h）：冷卻到期，再吵一次，且帶累計次數
    day4 = engine.evaluate(broken, now=T0 + timedelta(hours=73))
    assert len(day4) == 1
    assert "第 4 次發生" in day4[0]

    row = engine.store.get_alert("yahoo_direct:parser_broken")
    assert row["occurrences"] == 4
    assert row["notify_count"] == 1  # 還沒送出 → 通知帳不動（scan 預覽不燒冷卻）


def test_evaluate_alone_does_not_burn_cooldown(engine):
    """scan 只算不發：沒有 mark_sent，第二次 evaluate 仍然回報同一則。"""
    broken = [_res(health=ParseHealth.PARSER_BROKEN)]
    assert len(engine.evaluate(broken, now=T0)) == 1
    assert len(engine.evaluate(broken, now=T0 + timedelta(hours=1))) == 1


# ---------------------------------------------------------------------------
# (b) 復原
def test_recovery_notifies_and_clears_row(engine):
    broken = [_res(health=ParseHealth.BLOCKED, detail="WAF 擋掉")]
    first = engine.evaluate(broken, now=T0)
    _send(engine, first, T0)
    assert engine.store.get_alert("yahoo_direct:blocked") is not None

    recovered = engine.evaluate([_res(health=ParseHealth.OK, listings=5)], now=T0 + timedelta(hours=50))
    assert len(recovered) == 1
    assert recovered[0].startswith("✅")
    assert recovered[0].recovery_source == "yahoo_direct"

    _send(engine, recovered, T0 + timedelta(hours=50))
    assert engine.store.get_alert("yahoo_direct:blocked") is None
    assert engine.store.list_alerts() == []


def test_recovery_is_silent_if_never_notified(engine):
    """只被觀測、從沒吵過使用者的病（例如單次斷線）復原時不發訊息，靜默歸零。"""
    engine.evaluate([_res(health=ParseHealth.FETCH_FAILED)], now=T0)  # 單次，不發
    assert engine.evaluate([_res(health=ParseHealth.OK, listings=3)], now=T0 + timedelta(hours=24)) == []
    assert engine.store.list_alerts() == []


# ---------------------------------------------------------------------------
# (c) FETCH_FAILED 連續門檻
def test_fetch_failed_needs_two_consecutive_scans(engine):
    failed = [_res(health=ParseHealth.FETCH_FAILED, detail="ReadTimeout")]
    assert engine.evaluate(failed, now=T0) == []  # transient 單次不吵

    second = engine.evaluate(failed, now=T0 + timedelta(hours=24))
    assert len(second) == 1
    assert "連線失敗" in second[0]


# ---------------------------------------------------------------------------
# (d) 單次上限併列
def test_over_limit_alerts_are_combined(engine):
    """max_alerts_per_run=3：前 2 則原樣，其餘併成一則統計（告警不能變洗版源）。"""
    assert int(engine.cfg.notify["max_alerts_per_run"]) == 3
    results = [
        _res(source=f"src_{i}", health=ParseHealth.PARSER_BROKEN) for i in range(5)
    ]
    alerts = engine.evaluate(results, now=T0)

    assert len(alerts) == 3
    combined = alerts[-1]
    assert "另有 3 項來源告警" in combined
    # 併列那則要背著全部 fingerprint，送出後三筆都要落冷卻帳
    assert len(combined.fingerprints) == 3
    _send(engine, alerts, T0)
    assert all(r["notify_count"] == 1 for r in engine.store.list_alerts())


# ---------------------------------------------------------------------------
# (e) canary 純函式
def test_canary_below_threshold_is_parser_broken():
    """「遊戯王」解析器只解出 3 個商品（門檻 10）→ 判 PARSER_BROKEN，即使抓取層說 OK。"""
    res = _res(health=ParseHealth.OK, listings=3, query="遊戯王")
    verdict = canary_verdict(res, 10)

    assert verdict is not None
    assert verdict.health is ParseHealth.PARSER_BROKEN
    assert verdict.source == "yahoo_direct"
    assert "canary" in verdict.detail and "3 個商品" in verdict.detail and "10" in verdict.detail


def test_canary_above_threshold_is_silent():
    assert canary_verdict(_res(health=ParseHealth.OK, listings=12, query="遊戯王"), 10) is None


def test_canary_counts_parsed_not_filtered_listings():
    """🚩 2026-08-01 假警報的迴歸測試：解析器好好的、商業篩選把清單洗到 1 筆。

    Yahoo 的 include_live_auctions=false 會丟掉所有純競標標的，新着排序下
    「遊戯王」最新 50 筆幾乎全是剛開的 ¥1 起標無即決——實測同一查詢連續三次
    得到 22 / 18 / 1 筆，而每次都健康地解出 50 個商品區塊。
    健康判定必須看 parsed_count，看 len(listings) 就是 12 次假 parser_broken。
    """
    res = _res(health=ParseHealth.OK, listings=1, parsed=50, query="遊戯王")
    assert canary_verdict(res, 10) is None


def test_canary_fires_when_parser_dies_even_if_listings_survive():
    """反向：parsed_count 掉到門檻以下就要告警，不因為剛好有幾筆 listing 而閉嘴。

    （這個組合在真實世界不會出現——它存在是為了釘住「判定的分子是 parsed_count」，
    而不是「兩個數字取大的」那種偷懶寫法。）
    """
    verdict = canary_verdict(_res(health=ParseHealth.OK, listings=8, parsed=2), 10)
    assert verdict is not None and verdict.health is ParseHealth.PARSER_BROKEN
    assert "剩 8 筆" in verdict.detail  # 篩選後的數字仍要出現在排錯線索裡
    assert verdict.parsed_count == 2


def test_canary_treats_empty_page_as_broken():
    """canary 關鍵字不可能真的沒貨——EMPTY_CONFIRMED 也算解析器壞了。"""
    verdict = canary_verdict(_res(health=ParseHealth.EMPTY_CONFIRMED, query="遊戯王"), 10)
    assert verdict is not None and verdict.health is ParseHealth.PARSER_BROKEN


def test_canary_passes_through_fetch_layer_diagnosis():
    """抓取層已確診（被擋／斷線）→ 原樣回傳，不做二次判定、不改判為 parser_broken。"""
    blocked = _res(health=ParseHealth.BLOCKED, detail="WAF")
    assert canary_verdict(blocked, 10) is blocked


# ---------------------------------------------------------------------------
# (f) alerts 表 CRUD 煙霧測試
def test_alerts_table_crud(store):
    assert store.get_alert("nope") is None
    assert store.list_alerts() == []

    store.upsert_alert(
        "yahoo_direct:blocked",
        source="yahoo_direct",
        kind="blocked",
        first_seen=T0.isoformat(),
        last_seen=T0.isoformat(),
        occurrences=1,
        last_notified_at=None,
        notify_count=0,
        detail="WAF 擋掉",
    )
    row = store.get_alert("yahoo_direct:blocked")
    assert row["source"] == "yahoo_direct"
    assert row["occurrences"] == 1
    assert row["last_notified_at"] is None
    assert row["notify_count"] == 0

    store.upsert_alert(
        "yahoo_direct:blocked",
        source="yahoo_direct",
        kind="blocked",
        first_seen=T0.isoformat(),
        last_seen=(T0 + timedelta(days=1)).isoformat(),
        occurrences=2,
        last_notified_at=T0.isoformat(),
        notify_count=1,
        detail="WAF 擋掉",
    )
    assert len(store.list_alerts()) == 1  # 同 fingerprint 是覆寫不是新增
    assert store.get_alert("yahoo_direct:blocked")["notify_count"] == 1

    store.clear_alert("yahoo_direct:blocked")
    assert store.get_alert("yahoo_direct:blocked") is None
    store.clear_alert("yahoo_direct:blocked")  # 重複刪除不該爆


# ---------------------------------------------------------------------------
# (g) 一次性清理：`ygo-sniper health --clear`
#
# 為什麼要有這個指令、而不是叫使用者自己下 SQL：告警列是健康判定的記憶
# （occurrences 決定 FETCH_FAILED 的連續門檻、notify_count 決定冷卻序列），
# 手寫 SQL 很容易只刪一半，而且沒有任何痕跡。
def _seed(store, fingerprint, source, kind, *, occurrences=1, notify_count=0):
    store.upsert_alert(
        fingerprint, source=source, kind=kind,
        first_seen=T0.isoformat(), last_seen=T0.isoformat(),
        occurrences=occurrences, last_notified_at=None,
        notify_count=notify_count, detail="canary 假警報",
    )


def test_clear_alerts_removes_every_row(store, capsys):
    from ygo_sniper.cli import _clear_alerts

    _seed(store, "yahoo_direct:parser_broken", "yahoo_direct", "parser_broken", occurrences=12)
    _seed(store, "buyee_mercari:blocked", "buyee_mercari", "blocked")

    n = _clear_alerts(store)

    assert n == 2 and store.list_alerts() == []
    out = capsys.readouterr().out
    # 刪之前要把刪掉什麼印出來——留得下痕跡才敢清
    assert "yahoo_direct:parser_broken" in out and "12" in out


def test_clear_alerts_can_target_one_source(store):
    from ygo_sniper.cli import _clear_alerts

    _seed(store, "yahoo_direct:parser_broken", "yahoo_direct", "parser_broken", occurrences=12)
    _seed(store, "buyee_mercari:blocked", "buyee_mercari", "blocked")

    assert _clear_alerts(store, only_source="yahoo_direct") == 1
    assert [r["fingerprint"] for r in store.list_alerts()] == ["buyee_mercari:blocked"]


def test_clear_alerts_on_empty_table_is_a_no_op(store, capsys):
    from ygo_sniper.cli import _clear_alerts

    assert _clear_alerts(store) == 0
    assert "本來就是空的" in capsys.readouterr().out


def test_clear_source_also_clears_high_band_suffixed_alerts(store):
    """`--clear-source buyee_mercari` 要同時清掉 `buyee_mercari@high`——
    高價帶輪的告警指紋帶 band 後綴（`AlertEngine.evaluate` 的
    `f"{source}@{band}"`），全等比對清不到它（修正回合二 Task 13，複審 S1）。
    這是修復前會紅、修復後會綠的錨定測試（(c) 的紅燈證據）。"""
    from ygo_sniper.cli import _clear_alerts

    _seed(store, "buyee_mercari:parser_broken", "buyee_mercari", "parser_broken")
    _seed(store, "buyee_mercari@high:parser_broken", "buyee_mercari@high", "parser_broken")
    _seed(store, "yahoo_direct:blocked", "yahoo_direct", "blocked")

    n = _clear_alerts(store, only_source="buyee_mercari")

    assert n == 2
    assert [r["fingerprint"] for r in store.list_alerts()] == ["yahoo_direct:blocked"]


def test_clear_source_does_not_over_match_similar_prefix(store):
    """`buyee_mercari` 不該連帶清掉 `buyee_mercari_paypay` 這種只是前綴相同、
    不是 band 後綴（`@`）的別的來源——`startswith` 若沒釘死 `@` 分隔字元
    會誤殺。"""
    from ygo_sniper.cli import _clear_alerts

    _seed(store, "buyee_mercari:parser_broken", "buyee_mercari", "parser_broken")
    _seed(store, "buyee_mercari_paypay:blocked", "buyee_mercari_paypay", "blocked")

    n = _clear_alerts(store, only_source="buyee_mercari")

    assert n == 1
    assert [r["fingerprint"] for r in store.list_alerts()] == [
        "buyee_mercari_paypay:blocked"
    ]
