"""規則 4（指定卡狙擊）：終身去重、🎯 不受總量上限、👀 有小上限、formatter。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from ygo_sniper.card_snipe import build_notify_context
from ygo_sniper.notify_rules import (
    RULE_CARD_SNIPE,
    NotifyRules,
    evaluate,
)
from ygo_sniper.store import Store

WATCH_KW = dict(
    grader="ARS", grade=10.0, grade_label="10",
    name_ja="魔法の筒", name_en="Magic Cylinder",
    aliases=["マジック・シリンダー"], code_raw="P4-06", code_norm="P4-6",
)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def _hit(store, wid, key, tier="exact", title="【ARS10】魔法の筒 P4-06"):
    store.upsert_card_watch_hit(
        wid, key, tier=tier, title=title, url=f"https://example.test/{key}",
        site="buyee_yahoo", seller_id="s1", price_native=50000.0, currency="JPY",
        end_time="2026-08-09T22:00:00+09:00",
    )


def _rules(cfg, **overrides):
    return replace(NotifyRules.from_config(cfg), **overrides)


class TestRule4:
    def test_exact_hit_flows_to_send_and_dedupes_for_life(self, store, cfg):
        wid = store.insert_card_watch(**WATCH_KW)
        _hit(store, wid, "buyee_yahoo:x1")
        rules = _rules(cfg)
        out = evaluate([], rules=rules, notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        assert [m.rule for m in out.to_send] == [RULE_CARD_SNIPE]
        assert out.to_send[0].key == f"{wid}:buyee_yahoo:x1"
        # 模擬送成功落帳 → 之後每一輪都不再送（終身一次）
        store.mark_rule_notified([(out.to_send[0].key, RULE_CARD_SNIPE)])
        out2 = evaluate([], rules=rules, notified=store.notify_log_map(),
                        snipe_ctx=build_notify_context(store))
        assert out2.to_send == []

    def test_exact_is_exempt_from_global_cap(self, store, cfg):
        wid = store.insert_card_watch(**WATCH_KW)
        for i in range(3):
            _hit(store, wid, f"buyee_yahoo:x{i}")
        out = evaluate([], rules=_rules(cfg, max_items_per_run=1),
                       notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        # cap=1 也擋不住狙擊命中：三筆全部要送
        assert len([m for m in out.to_send if m.rule == RULE_CARD_SNIPE]) == 3

    def test_partial_has_its_own_small_cap(self, store, cfg):
        from ygo_sniper.card_snipe import PARTIAL_MAX_PER_RUN

        wid = store.insert_card_watch(**WATCH_KW)
        for i in range(PARTIAL_MAX_PER_RUN + 2):
            _hit(store, wid, f"buyee_yahoo:p{i}", tier="partial")
        out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        sent = [m for m in out.to_send if m.rule == RULE_CARD_SNIPE]
        assert len(sent) == PARTIAL_MAX_PER_RUN
        # 溢出的有講（skipped），沒落帳 → 下輪還會排隊
        assert len(out.skips_for("疑似命中已達上限")) == 2

    def test_no_ctx_no_crash(self, cfg):
        out = evaluate([], rules=_rules(cfg), notified={}, snipe_ctx=None)
        assert out.card_snipe == [] and out.to_send == []


class TestFormatter:
    def test_message_contains_the_essentials(self, store, cfg):
        wid = store.insert_card_watch(**WATCH_KW)
        store.update_card_watch_census(
            wid, census_url="u", census_json='{"9": 5, "10": 5, "10+": 1}',
            census_total=11)
        _hit(store, wid, "buyee_yahoo:x1")
        out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        from ygo_sniper.notify import format_card_snipe

        text = format_card_snipe(out.to_send[0], "http://127.0.0.1:8321")
        assert "🎯" in text
        assert "ARS10 魔法の筒 P4-06" in text
        assert "【ARS10】魔法の筒 P4-06" in text          # 標題原文
        assert "全世界 5 張" in text                       # census
        assert "https://example.test/buyee_yahoo:x1" in text
        assert "JPY 50,000" in text
        assert "非成交價" in text                          # 現在価格語意講清楚

    def test_partial_message_is_marked(self, store, cfg):
        wid = store.insert_card_watch(**WATCH_KW)
        _hit(store, wid, "buyee_yahoo:p1", tier="partial",
             title="魔法の筒 ARS鑑定品")
        out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        from ygo_sniper.notify import format_card_snipe

        text = format_card_snipe(out.to_send[0], "http://x")
        assert "👀" in text and "未全符" in text


def test_rule4_appears_in_the_cli_counts(store, cfg, capsys):
    """規則 4 的命中數必須印得出來——0 與「沒在跑」不能長一樣。"""
    import ygo_sniper.cli as cli_mod

    wid = store.insert_card_watch(**WATCH_KW)
    _hit(store, wid, "buyee_yahoo:x1")
    out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                   snipe_ctx=build_notify_context(store))
    cli_mod._print_rule_counts(out)
    printed = capsys.readouterr().out
    assert "指定卡狙擊" in printed
    assert "🎯 1" in printed


def test_store_and_notify_rules_agree_on_the_rule_name():
    """`store.CARD_SNIPE_RULE` 與 `notify_rules.RULE_CARD_SNIPE` 必須是同一個值。

    兩份定義漂移的話，`list_card_watch_hits` 的 `sent_at` 會恆為 NULL
    → 每輪都判定「這筆沒送過」而重複推播，而且**壞掉的樣子與「真的還沒送過」
    完全一樣**（CLAUDE.md 第五節）。這條測試就是那個結構性守門員——
    不能改成註解或提醒（CLAUDE.md 的 meta-rule：別用更多流程補流程漏洞）。
    """
    from ygo_sniper.store import CARD_SNIPE_RULE

    assert CARD_SNIPE_RULE == RULE_CARD_SNIPE


def test_preview_table_renders_a_snipe_hit_without_crashing(tmp_path, monkeypatch):
    """打**真正的 `notify-preview` 指令**，命中帳裡有一筆狙擊。

    規則 4 的 Match 沒有 `p_worth`，掉進 preview 那個 `else` 就是
    `TypeError: unsupported format string passed to NoneType.__format__`。
    命中 0 筆時那個迴圈根本不執行 → 沒有狙擊命中的日子它一路綠，
    直到「真的有一張卡上架」那天才炸——所以只有這條測試擋得住它。

    ⚠️ 這條測試必須**呼叫 `notify_preview` 本人**。把 cli 的格式化邏輯抄一份
    進測試本體是假守衛：實作那段整個刪掉，抄本照樣算得出 detail、照樣全綠
    （CLAUDE.md 第六節：驗證使用者實際會打的那個指令，不是元件會不會動）。
    """
    from dataclasses import replace as dc_replace

    from conftest import FakeFx
    from typer.testing import CliRunner

    import ygo_sniper.cli as cli_mod
    import ygo_sniper.config as config_mod
    import ygo_sniper.pipeline as pipeline_mod

    db = tmp_path / "preview.db"
    config_mod.load_config.cache_clear()
    test_cfg = dc_replace(config_mod.load_config(),
                          storage={**config_mod.load_config().storage,
                                   "db_path": str(db)})
    # `notify_preview` 自己 `Pipeline()`（不帶 cfg）→ 走 pipeline 模組的
    # load_config。承重斷言：這條測試絕不能碰正式庫。
    monkeypatch.setattr(pipeline_mod, "load_config", lambda: test_cfg)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: {})
    assert pipeline_mod.load_config().db_path == db, "preview 的 cfg 沒有指到 tmp db"

    store = Store(db)
    wid = store.insert_card_watch(**WATCH_KW)
    _hit(store, wid, "buyee_yahoo:x1")

    try:
        r = CliRunner().invoke(cli_mod.app, ["notify-preview"])
        assert r.exit_code == 0, f"{r.output}\n{r.exception!r}"
        assert "規則 4 指定卡狙擊：命中 1 筆" in r.output
        # 狙擊那一列真的被排出來了（P= 那個分支會在這裡炸）
        assert "🎯" in r.output and "ARS10" in r.output
        assert "JPY 50,000" in r.output
    finally:
        config_mod.load_config.cache_clear()
