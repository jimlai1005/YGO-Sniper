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
# 3. 已售出查詢分片（游標輪替，取代整批節流）
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def _sold_sources():
    return {"buyee_mercari": _SoldSource("buyee_mercari")}


def _kw(shard):
    return [k for _, k in shard.queries]


def test_sold_shard_walks_list_and_wraps(cfg, tmp_path):
    store = Store(tmp_path / "comps.db")
    eng = _engine(
        cfg,
        {"sources": ["buyee_mercari"], "extra": ["kw0", "kw1", "kw2", "kw3", "kw4"], "every_n_runs": 2},
        store=store,
    )
    s1 = eng.sold_shard(_sold_sources())
    assert _kw(s1) == ["kw0", "kw1", "kw2"]  # ceil(5/2)=3
    eng.commit_sold_shard(s1, any_success=True)
    s2 = eng.sold_shard(_sold_sources())
    assert _kw(s2) == ["kw3", "kw4"]
    eng.commit_sold_shard(s2, any_success=True)
    assert _kw(eng.sold_shard(_sold_sources())) == ["kw0", "kw1", "kw2"]  # 繞回


def test_sold_shard_cursor_survives_process_restart(cfg, tmp_path):
    """跨行程才是真的節流：CLI 每輪都是新的 python 行程、新的 CompsEngine。

    游標若放在記憶體，每輪都會從 0 開始 → 每輪都跑同一片，
    輪替參數看起來有設、實際上完全沒生效。
    """
    store = Store(tmp_path / "comps.db")
    eng = _engine(cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    eng.commit_sold_shard(eng.sold_shard(_sold_sources()), any_success=True)
    # 新行程 = 新 engine，同一個 store
    eng2 = _engine(cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    assert _kw(eng2.sold_shard(_sold_sources())) == ["c", "d"]


def test_sold_shard_does_not_advance_on_total_failure_then_force_advances(cfg, tmp_path, capsys):
    """工程原則 2：transient 失敗原地重試，但連續 3 輪整片全失敗代表片本身
    壞了（不是被擋，是別的原因，例如逾時），強制推進避免輪替永遠卡死在同一片。

    「大聲」是本專案的頭號規則（見 CLAUDE.md 第五節）——不只測游標真的動了，
    還要測 ⚠️ 那行真的印出來，而且**只在**第 3 次才印（前兩次原地重試不出聲，
    出聲的話操作者會誤以為每次失敗都是異常，而不是設計內的重試）。
    """
    store = Store(tmp_path / "comps.db")
    eng = _engine(cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    first = eng.sold_shard(_sold_sources())
    eng.commit_sold_shard(first, any_success=False)
    assert _kw(eng.sold_shard(_sold_sources())) == _kw(first)  # 第 1 次失敗：原地重試
    assert "⚠️" not in capsys.readouterr().out
    eng.commit_sold_shard(first, any_success=False)
    assert _kw(eng.sold_shard(_sold_sources())) == _kw(first)  # 第 2 次失敗：仍原地
    assert "⚠️" not in capsys.readouterr().out
    eng.commit_sold_shard(first, any_success=False)            # 第 3 次：強制推進
    assert _kw(eng.sold_shard(_sold_sources())) != _kw(first)
    out = capsys.readouterr().out
    assert "⚠️" in out and "3" in out


def test_sold_shard_blocked_failure_advances_on_first_commit(cfg, tmp_path, capsys):
    """`blocked=True`（整片全是 BlockedError）是 semantic 失敗：對方剛拒絕過
    我們，重試同一片沒有意義，第一次就要推進，不能像 transient 失敗一樣
    原地卡三輪——那正是「拿槍指自己」：對著剛擋我們的來源連打三輪。
    """
    store = Store(tmp_path / "comps.db")
    eng = _engine(cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    first = eng.sold_shard(_sold_sources())
    eng.commit_sold_shard(first, any_success=False, blocked=True)
    second = eng.sold_shard(_sold_sources())
    assert _kw(second) != _kw(first)  # 第一次 commit 就推進，不是第三次
    out = capsys.readouterr().out
    assert "整片被擋" in out
    # stall 帳沒被動過：之後若換成 transient 失敗，還是從 0 開始算三振
    from ygo_sniper.comps import SOLD_STALL_KEY

    assert store.get_meta(SOLD_STALL_KEY) in (None, "0")


def test_sold_shard_blocked_streak_escalates_message_at_limit(cfg, tmp_path, capsys):
    """被擋每輪都會推進游標（不像 transient 卡在原地），所以「連續被擋幾輪」
    要另立一本帳（`SOLD_BLOCKED_STREAK_KEY`）才數得出來。門檻之前只講當輪
    的事實（第幾輪被擋），到門檻才升級成「整體失效」＋要查什麼——不能一被擋
    就講「來源整體失效」，那是喊假警報；也不能永遠不升級，那是原始發現
    指出的問題（訊息宣稱了一個永遠不會發生的偵測）。
    """
    from ygo_sniper.comps import SOLD_BLOCKED_STREAK_LIMIT

    store = Store(tmp_path / "comps.db")
    eng = _engine(
        cfg,
        {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d", "e", "f"], "every_n_runs": 2},
        store=store,
    )

    for i in range(1, SOLD_BLOCKED_STREAK_LIMIT):
        shard = eng.sold_shard(_sold_sources())
        eng.commit_sold_shard(shard, any_success=False, blocked=True)
        out = capsys.readouterr().out
        assert "整片被擋" in out and str(i) in out
        assert "整體失效" not in out  # 門檻之前不能提前喊狼來了

    shard = eng.sold_shard(_sold_sources())
    eng.commit_sold_shard(shard, any_success=False, blocked=True)
    out = capsys.readouterr().out
    assert "整體失效" in out and str(SOLD_BLOCKED_STREAK_LIMIT) in out
    assert "health" in out  # 要講清楚該查什麼，不能只講「壞了」


def test_sold_shard_blocked_streak_resets_on_any_success(cfg, tmp_path):
    from ygo_sniper.comps import SOLD_BLOCKED_STREAK_KEY

    store = Store(tmp_path / "comps.db")
    eng = _engine(
        cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store
    )
    eng.commit_sold_shard(eng.sold_shard(_sold_sources()), any_success=False, blocked=True)
    assert store.get_meta(SOLD_BLOCKED_STREAK_KEY) == "1"
    eng.commit_sold_shard(eng.sold_shard(_sold_sources()), any_success=True)
    assert store.get_meta(SOLD_BLOCKED_STREAK_KEY) == "0"


def test_sold_shard_force_returns_full_list_and_resets_cursor(cfg, tmp_path):
    """人工「我現在就要更新行情」的逃生門：force 回全量，且完成後游標歸零，
    不然下一輪照常節奏又會跳到某個中間位置，跟人工跑過一次的直覺不符。"""
    store = Store(tmp_path / "comps.db")
    eng = _engine(cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    eng.commit_sold_shard(eng.sold_shard(_sold_sources()), any_success=True)  # 游標→2
    forced = eng.sold_shard(_sold_sources(), force=True)
    assert _kw(forced) == ["a", "b", "c", "d"]
    eng.commit_sold_shard(forced, any_success=True)
    assert _kw(eng.sold_shard(_sold_sources())) == ["a", "b"]  # 游標已歸零


def test_sold_shard_without_store_runs_everything(cfg):
    """沒有 store（單元測試／dry-run）→ 不分片，但也不會炸。"""
    eng = _engine(cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c"], "every_n_runs": 2}, store=None)
    shard = eng.sold_shard(_sold_sources())
    assert _kw(shard) == ["a", "b", "c"]
    eng.commit_sold_shard(shard, any_success=True)  # 不得炸


def test_sold_shard_disabled_by_every_n_runs_1(cfg, store):
    eng = _engine(cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c"], "every_n_runs": 1}, store=store)
    assert _kw(eng.sold_shard(_sold_sources())) == ["a", "b", "c"]


def test_sold_shard_recovers_from_corrupt_cursor(cfg, store):
    from ygo_sniper.comps import SOLD_CURSOR_KEY

    store.set_meta(SOLD_CURSOR_KEY, "not-a-number")
    eng = _engine(cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    assert _kw(eng.sold_shard(_sold_sources())) == ["a", "b"]  # 壞值當 0，不拋例外


def test_sold_shard_empty_query_list_is_a_noop(cfg, store):
    eng = _engine(cfg, {"every_n_runs": 2}, store=store)
    shard = eng.sold_shard(_sold_sources())
    assert shard.queries == [] and shard.next_cursor is None
    eng.commit_sold_shard(shard, any_success=True)  # 不得炸


def test_sold_shard_survives_list_length_change_mid_walk(cfg, tmp_path):
    """watchlist 設定可能在輪替途中被改（extra 清單增刪）。游標是用位置存的，
    不是用查詢內容存的，所以清單一變短，舊游標可能落在新清單範圍外
    ——這條釘住「不會永久餓死某條查詢」：清單縮小後游標會被 modulo 拉回
    合法範圍，繼續往下走終究會覆蓋到全部剩下的查詢，不會卡在一個洞裡出不來。
    """
    store = Store(tmp_path / "comps.db")
    eng = _engine(
        cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d", "e"], "every_n_runs": 2},
        store=store,
    )
    eng.commit_sold_shard(eng.sold_shard(_sold_sources()), any_success=True)  # 游標 0→3
    # 清單縮短成三個：舊游標 3 現在指向清單外
    eng2 = _engine(
        cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c"], "every_n_runs": 2}, store=store
    )
    seen: set[str] = set()
    shard = eng2.sold_shard(_sold_sources())
    for kw in _kw(shard):
        assert kw in {"a", "b", "c"}  # 游標必須落在合法範圍內，不拋例外
    seen.update(_kw(shard))
    eng2.commit_sold_shard(shard, any_success=True)
    shard2 = eng2.sold_shard(_sold_sources())
    seen.update(_kw(shard2))
    eng2.commit_sold_shard(shard2, any_success=True)
    shard3 = eng2.sold_shard(_sold_sources())
    seen.update(_kw(shard3))
    assert seen == {"a", "b", "c"}, "縮短清單後繼續走幾輪，應該覆蓋到全部查詢"


# ---------------------------------------------------------------------------
# 4. 分片游標對 SIGKILL 免疫（sold_shard 交出去卻等不到 commit）
# ---------------------------------------------------------------------------
def test_sold_shard_force_advances_after_repeated_uncommitted_attempts(cfg, tmp_path, capsys):
    """模擬 watchdog SIGKILL：`sold_shard` 交出分片之後，呼叫端直接消失
    （不呼叫 `commit_sold_shard`）。連續交出去 N 次都沒等到 commit，
    第 N+1 次呼叫必須跳過這個卡死的游標，並且大聲講清楚——不然同一段
    （實測分片 0 正是 Playwright/WAF 那條路）會被反覆殺死、永遠卡在原地，
    後面 yahoo_closed 的 80 條查詢永遠輪不到。
    """
    from ygo_sniper.comps import SOLD_ATTEMPT_LIMIT

    store = Store(tmp_path / "comps.db")
    eng = _engine(
        cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2},
        store=store,
    )
    first = eng.sold_shard(_sold_sources())
    for _ in range(SOLD_ATTEMPT_LIMIT - 1):
        # 每次都「交出去就消失」：完全不呼叫 commit_sold_shard
        again = eng.sold_shard(_sold_sources())
        assert _kw(again) == _kw(first)  # 還沒到門檻，原地重發同一片

    # 第 N 次已經把次數推到門檻，這次呼叫（第 N+1 次）要強制跳過
    moved = eng.sold_shard(_sold_sources())
    assert _kw(moved) != _kw(first)
    out = capsys.readouterr().out
    assert "⚠️" in out and "游標 0" in out  # 卡死的游標是 0，訊息要點名
    assert "SIGKILL" in out


def test_sold_shard_commit_clears_attempt_marker(cfg, tmp_path):
    """一輪正常跑完（不管成敗，只要真的呼叫了 commit）就代表這一輪沒被殺死，
    之前的嘗試次數不該延續到下一次真正的 crash——不然「乾淨地失敗過幾次」
    會跟「被殺過幾次」混進同一個計數器，是另一種混源比較。
    """
    from ygo_sniper.comps import SOLD_ATTEMPT_KEY, SOLD_ATTEMPT_LIMIT, _parse_sold_attempt

    store = Store(tmp_path / "comps.db")
    eng = _engine(
        cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2},
        store=store,
    )
    shard = eng.sold_shard(_sold_sources())
    _, count = _parse_sold_attempt(store.get_meta(SOLD_ATTEMPT_KEY))
    assert count == 1
    eng.commit_sold_shard(shard, any_success=False)  # 跑完了，只是查詢乾淨地失敗
    assert store.get_meta(SOLD_ATTEMPT_KEY) == ""

    # 之後即使真的連續被殺，也是從 0 開始算，不是從舊次數接著算
    for _ in range(SOLD_ATTEMPT_LIMIT):
        again = eng.sold_shard(_sold_sources())
    assert _kw(again) == _kw(shard)  # 剛好卡在門檻，還沒被強制跳過


def test_sold_shard_normal_path_sets_then_clears_attempt_marker(cfg, tmp_path):
    """正常路徑（沒有 crash）：`sold_shard` 交出分片時寫下嘗試標記，
    `commit_sold_shard` 一跑完就清掉，游標只前進一次——crash-safety 補丁
    不能改變原本「一輪一次前進」的行為。
    """
    from ygo_sniper.comps import SOLD_ATTEMPT_KEY, SOLD_CURSOR_KEY

    store = Store(tmp_path / "comps.db")
    eng = _engine(
        cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2},
        store=store,
    )
    shard = eng.sold_shard(_sold_sources())
    assert store.get_meta(SOLD_ATTEMPT_KEY) == "0:1"
    eng.commit_sold_shard(shard, any_success=True)
    assert store.get_meta(SOLD_ATTEMPT_KEY) == ""
    assert store.get_meta(SOLD_CURSOR_KEY) == "2"  # 只前進了一次


def test_sold_shard_recovers_from_corrupt_attempt_marker(cfg, tmp_path):
    from ygo_sniper.comps import SOLD_ATTEMPT_KEY

    store = Store(tmp_path / "comps.db")
    store.set_meta(SOLD_ATTEMPT_KEY, "garbage-not-a-marker")
    eng = _engine(
        cfg, {"sources": ["buyee_mercari"], "extra": ["a", "b", "c", "d"], "every_n_runs": 2},
        store=store,
    )
    shard = eng.sold_shard(_sold_sources())  # 壞值當「沒有進行中的嘗試」，不拋例外
    assert _kw(shard) == ["a", "b"]
    assert store.get_meta(SOLD_ATTEMPT_KEY) == "0:1"


def test_pipeline_refresh_comps_walks_shards_across_calls(monkeypatch, tmp_path, cfg, capsys):
    """端到端：連續呼叫 `refresh_comps` 兩次，第二次要打的是**剩下那一份**，
    不是重打第一份、也不是什麼都不打——分片是輪替，不是節流開關。
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
                "every_n_runs": 2,
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
        calls.clear()
        pipe.refresh_comps()
        second = list(calls)
    finally:
        pipe.close()

    assert first == ["遊戯王 初期"]
    assert second == ["遊戯王 二期"]
    assert "游標" in capsys.readouterr().out


def test_pipeline_refresh_comps_advances_cursor_immediately_on_blocked_source(
    monkeypatch, tmp_path, cfg, capsys
):
    """CLAUDE.md 第六節（測試路徑必須等於生產路徑）：`commit_sold_shard` 的
    blocked 契約已經在別的測試裡直接呼叫釘住了，但那樣測不到真正新加的邏輯
    ——`pipeline.refresh_comps` 裡 `isinstance(exc, BlockedError)` 那段判斷。
    這條逼真來源丟出 `BlockedError`，走 `refresh_comps` 整條鏈路，
    確認只跑一輪游標就推進了（不是像 transient 失敗那樣卡三輪才推進）。
    """
    import dataclasses

    import ygo_sniper.pipeline as pipeline_mod
    from ygo_sniper.comps import SOLD_CURSOR_KEY
    from ygo_sniper.pipeline import Pipeline
    from ygo_sniper.sources.base import BlockedError

    class _BlockedSource(_SoldSource):
        def search(self, keyword, **_kw):
            raise BlockedError("waf challenge", url="https://example.test/search")

    registry = {"yahoo_closed": _BlockedSource("yahoo_closed")}
    test_cfg = dataclasses.replace(
        cfg,
        root=tmp_path,
        watchlist={
            **cfg.watchlist,
            "queries": [],
            "comps_queries": {
                "sources": ["yahoo_closed"],
                "every_n_runs": 2,
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
        cursor_after_one_run = pipe.store.get_meta(SOLD_CURSOR_KEY)
    finally:
        pipe.close()

    assert cursor_after_one_run == "1"  # 一輪就推進，不是三振後才推進
    out = capsys.readouterr().out
    assert "整片被擋" in out


def test_sold_pages_defaults_and_floor(cfg):
    assert _engine(cfg, {}).sold_pages == 2
    assert _engine(cfg, {"pages": 5}).sold_pages == 5
    assert _engine(cfg, {"pages": 0}).sold_pages == 1   # 0 頁 = 什麼都不抓，擋掉
