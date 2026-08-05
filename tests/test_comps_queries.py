"""已售出查詢的組合展開與請求節流。零網路。

這兩件事守的是同一個約束：**請求預算**。
組合展開是乘法（稀有度 × 年代 × 機構），節流是除法（每 N 輪跑一次）。
兩邊都失守的話，一個每小時跑的排程會變成每天對 Yahoo 打上萬個請求——
而且拿回來的是同一批資料（落札相場的日流量遠小於我們的抓取量）。

節流特別需要測「跨行程」：CLI 每輪都是新的 python 行程，
用記憶體變數做的節流在這個部署形態下等於沒有節流。
"""

import dataclasses

import pytest
from conftest import FakeFx

from ygo_sniper.comps import (
    DEFAULT_MAX_COMPS_QUERIES,
    SOLD_RUN_COUNTER_KEY,
    CompsEngine,
    expand_comps_queries,
)
from ygo_sniper.domain import Site
from ygo_sniper.store import Store


class _SoldSource:
    def __init__(self, name: str, supports_sold: bool = True) -> None:
        self.name = name
        self.site = Site.BUYEE_YAHOO
        self.supports_sold = supports_sold

    def search(self, keyword, **_kw):
        return []


def _engine(cfg, comps_queries, store=None, queries=None):
    test_cfg = dataclasses.replace(
        cfg,
        watchlist={
            **cfg.watchlist,
            "queries": queries if queries is not None else [],
            "comps_queries": comps_queries,
        },
    )
    return CompsEngine(test_cfg, FakeFx(), store=store)


# ---------------------------------------------------------------------------
# 1. 組合展開
# ---------------------------------------------------------------------------
def test_expand_is_cartesian_product():
    out = expand_comps_queries(
        {
            "template": "遊戯王 {era} {rarity} {grader}",
            "eras": ["初期", "二期"],
            "rarities": ["レリーフ", "シークレット"],
            "graders": ["PSA", "ARS"],
        }
    )
    assert len(out) == 2 * 2 * 2
    assert "遊戯王 初期 レリーフ PSA" in out
    assert "遊戯王 二期 シークレット ARS" in out


def test_expand_collapses_whitespace_from_empty_dimensions():
    """空維度不能留下兩個空白——那會變成兩個只差空白的查詢、兩次請求。"""
    out = expand_comps_queries(
        {"template": "遊戯王 {era} {rarity} {grader}", "eras": ["初期"], "rarities": [""]}
    )
    assert out == ["遊戯王 初期"]
    assert "  " not in out[0]


def test_expand_appends_extra_and_dedupes_preserving_order():
    out = expand_comps_queries(
        {
            "template": "遊戯王 {era} {grader}",
            "eras": ["初期"],
            "graders": ["PSA"],
            "extra": ["遊戯王 初期 PSA", "遊戯王 バンダイ 鑑定"],  # 第一個與展開結果重複
        }
    )
    assert out == ["遊戯王 初期 PSA", "遊戯王 バンダイ 鑑定"]


def test_expand_caps_query_count(capsys):
    """組合爆炸必須被截斷，而且要印出來——安靜地打 500 個請求是最糟的失敗。"""
    out = expand_comps_queries(
        {
            "template": "遊戯王 {era} {rarity}",
            "eras": [f"e{i}" for i in range(20)],
            "rarities": [f"r{i}" for i in range(20)],
            "max_queries": 30,
        }
    )
    assert len(out) == 30
    assert "截斷" in capsys.readouterr().out


def test_expand_default_cap_is_applied():
    out = expand_comps_queries(
        {
            "template": "{era}{rarity}",
            "eras": [f"e{i}" for i in range(40)],
            "rarities": [f"r{i}" for i in range(40)],
        }
    )
    assert len(out) == DEFAULT_MAX_COMPS_QUERIES


def test_expand_handles_missing_spec():
    assert expand_comps_queries(None) == []
    assert expand_comps_queries({}) == []


# ---------------------------------------------------------------------------
# 2. sold_queries 併入展開結果
# ---------------------------------------------------------------------------
def test_sold_queries_include_expanded_comps_queries(cfg):
    registry = {"yahoo_closed": _SoldSource("yahoo_closed")}
    engine = _engine(
        cfg,
        {
            "sources": ["yahoo_closed"],
            "template": "遊戯王 {era} {rarity}",
            "eras": ["初期"],
            "rarities": ["レリーフ", "シークレット"],
        },
    )
    assert engine.sold_queries(registry) == [
        ("yahoo_closed", "遊戯王 初期 レリーフ"),
        ("yahoo_closed", "遊戯王 初期 シークレット"),
    ]


def test_sold_queries_skip_sources_not_in_registry(cfg):
    """設定寫了 yahoo_closed 但 registry 沒有它 → 靜靜跳過，不炸。"""
    engine = _engine(
        cfg,
        {"sources": ["yahoo_closed"], "eras": ["初期"], "rarities": ["レリーフ"]},
    )
    assert engine.sold_queries({}) == []


def test_sold_queries_skip_sources_that_do_not_support_sold(cfg):
    registry = {"yahoo_direct": _SoldSource("yahoo_direct", supports_sold=False)}
    engine = _engine(
        cfg,
        {"sources": ["yahoo_direct"], "eras": ["初期"], "rarities": ["レリーフ"]},
    )
    assert engine.sold_queries(registry) == []


def test_sold_queries_dedupe_across_watchlist_and_expansion(cfg):
    """watchlist 的 query 與展開結果撞在一起時只跑一次。"""
    registry = {"yahoo_closed": _SoldSource("yahoo_closed")}
    engine = _engine(
        cfg,
        {"sources": ["yahoo_closed"], "template": "遊戯王 {era}", "eras": ["初期"]},
        queries=[{"name": "t", "keyword": "遊戯王 初期", "sources": ["yahoo_closed"]}],
    )
    assert engine.sold_queries(registry) == [("yahoo_closed", "遊戯王 初期")]


def test_real_config_expands_and_targets_yahoo_closed(cfg):
    """真的 watchlist.yaml 必須展開得出查詢，而且只給 yahoo_closed。

    這條防的是「設定檔改壞了但測試全綠」：組合展開的價值全在 config 裡，
    程式碼再對、config 空著也等於沒做。
    """
    from ygo_sniper.sources import build_sources

    sources = build_sources(cfg)
    spec = cfg.watchlist["comps_queries"]
    expanded = expand_comps_queries(spec)

    assert len(expanded) >= 40, "組合展開的查詢太少，comps_queries 可能被改空了"
    assert "遊戯王 初期 レリーフ PSA" in expanded
    assert "遊戯王 三期 パラレル ARS" in expanded
    assert spec["sources"] == ["yahoo_closed"]

    pairs = CompsEngine(cfg, FakeFx(), store=None).sold_queries(sources)
    # 展開出來的關鍵字**只能**打到 yahoo_closed。Buyee 系需要 Playwright，
    # 把數十個展開查詢丟給它等於每輪開幾十次瀏覽器。
    # （Buyee 系仍會出現在 pairs 裡——那是 watchlist queries 的既有行為，
    #   每條管道各 5 條查詢，與這組展開無關。）
    for name, keyword in pairs:
        if keyword in set(expanded) - {q["keyword"] for q in cfg.watchlist["queries"]}:
            assert name == "yahoo_closed", f"展開查詢 {keyword!r} 跑到了 {name}"
    assert ("yahoo_closed", "遊戯王 初期 レリーフ PSA") in pairs
    # 每輪的查詢總數要在預算內（每個查詢最多 pages 頁 × 2 秒間隔）
    assert len(pairs) <= 120, f"已售出查詢共 {len(pairs)} 條，請求預算會爆"


def test_yahoo_closed_is_not_in_the_onshelf_scan_path(cfg):
    """yahoo_closed 只能餵 comps，不得參與在架掃描。

    兩道門都要關：watchlist 的 queries[].sources（一般掃描）
    與 settings.yaml 的 sources:（canary 只跑那一段列出的來源）。
    """
    for q in cfg.watchlist.get("queries", []):
        assert "yahoo_closed" not in q.get("sources", []), q["name"]
    assert "yahoo_closed" not in (cfg.sources or {})


# ---------------------------------------------------------------------------
# 3. 節流
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def test_throttle_runs_first_then_skips(cfg, store):
    engine = _engine(cfg, {"every_n_runs": 3}, store=store)

    assert engine.claim_sold_run()[0] is True     # 第 0 輪：跑
    assert engine.claim_sold_run()[0] is False    # 第 1 輪：跳過
    assert engine.claim_sold_run()[0] is False    # 第 2 輪：跳過
    assert engine.claim_sold_run()[0] is True     # 第 3 輪：又輪到了


def test_throttle_skip_reason_is_explainable(cfg, store):
    engine = _engine(cfg, {"every_n_runs": 12}, store=store)
    engine.claim_sold_run()
    due, why = engine.claim_sold_run()

    assert due is False
    # 「這輪為什麼沒有新行情」必須有一句話交代：
    # 安靜地跳過與安靜地壞掉，外顯是一模一樣的
    assert "節流" in why and "12" in why


def test_throttle_survives_a_fresh_engine(cfg, store):
    """跨行程才是真的節流：CLI 每輪都是新的 python 行程、新的 CompsEngine。

    計數器若放在記憶體，每輪都會從 0 開始 → 每輪都「第 0 輪」→ 每輪都跑，
    節流參數看起來有設、實際上完全沒生效。
    """
    assert _engine(cfg, {"every_n_runs": 4}, store=store).claim_sold_run()[0] is True
    assert _engine(cfg, {"every_n_runs": 4}, store=store).claim_sold_run()[0] is False
    assert _engine(cfg, {"every_n_runs": 4}, store=store).claim_sold_run()[0] is False


def test_force_overrides_throttle_but_still_consumes_the_slot(cfg, store):
    """人工「我現在就要更新行情」的逃生門。

    計數器照樣前進——force 若不計數，人工跑幾次就能把排程的節流洗掉，
    那節流參數就變成裝飾品。
    """
    engine = _engine(cfg, {"every_n_runs": 3}, store=store)
    assert engine.claim_sold_run()[0] is True            # 第 0 輪
    due, why = engine.claim_sold_run(force=True)         # 第 1 輪，本應跳過
    assert due is True and "force" in why
    assert engine.claim_sold_run()[0] is False           # 第 2 輪：仍照原節奏跳過
    assert engine.claim_sold_run()[0] is True            # 第 3 輪：回到正常輪次


def test_throttle_disabled_by_every_n_runs_1(cfg, store):
    engine = _engine(cfg, {"every_n_runs": 1}, store=store)
    assert all(engine.claim_sold_run()[0] for _ in range(5))


def test_throttle_without_store_always_runs(cfg):
    """沒有 store（單元測試／dry-run）→ 不節流，但也不會炸。"""
    engine = _engine(cfg, {"every_n_runs": 12}, store=None)
    assert engine.claim_sold_run()[0] is True


def test_throttle_recovers_from_corrupt_counter(cfg, store):
    store.set_meta(SOLD_RUN_COUNTER_KEY, "not-a-number")
    engine = _engine(cfg, {"every_n_runs": 3}, store=store)

    assert engine.claim_sold_run()[0] is True   # 壞值當 0，不拋例外
    assert engine.claim_sold_run()[0] is False


def test_pipeline_refresh_comps_skips_second_call(monkeypatch, tmp_path, cfg, capsys):
    """端到端：連續呼叫兩次 `refresh_comps`，第二次不得再打任何請求。

    這條是節流真正要守的東西——單測 `claim_sold_run` 只證明計數器會動，
    證明不了 pipeline 真的有問它。這裡數的是 source 的 search 被呼叫幾次。
    """
    import dataclasses

    import ygo_sniper.pipeline as pipeline_mod
    from ygo_sniper.pipeline import Pipeline

    calls: list[str] = []

    class _CountingSource(_SoldSource):
        def search(self, keyword, **_kw):
            calls.append(keyword)
            return []

    registry = {"yahoo_closed": _CountingSource("yahoo_closed")}
    test_cfg = dataclasses.replace(
        cfg,
        root=tmp_path,
        watchlist={
            **cfg.watchlist,
            "queries": [],
            "comps_queries": {
                "sources": ["yahoo_closed"],
                "every_n_runs": 12,
                "template": "遊戯王 {era}",
                "eras": ["初期", "二期"],
            },
        },
    )
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: registry)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    pipe = Pipeline(test_cfg)
    try:
        pipe.refresh_comps()
        first = list(calls)
        pipe.refresh_comps()
    finally:
        pipe.close()

    assert first == ["遊戯王 初期", "遊戯王 二期"]
    assert calls == first, "第二次 refresh_comps 還是打了請求，節流沒生效"
    assert "跳過已售出查詢" in capsys.readouterr().out


def test_sold_pages_defaults_and_floor(cfg):
    assert _engine(cfg, {}).sold_pages == 2
    assert _engine(cfg, {"pages": 5}).sold_pages == 5
    assert _engine(cfg, {"pages": 0}).sold_pages == 1   # 0 頁 = 什麼都不抓，擋掉
