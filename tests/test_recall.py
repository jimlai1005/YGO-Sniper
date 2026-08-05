"""召回率量尺（`recall.py`）的純函式驗算。

這些函式的產出會被拿去**砍 query**，所以每一個都用手算得出來的固定資料釘住。
覆蓋率算錯的後果不是「數字難看」，是照著錯數字把一條有獨有貢獻的查詢刪掉，
而刪掉之後那一類標的就再也不會出現——與這次事故同一種無聲失敗。
"""

from __future__ import annotations

import json

import pytest

from ygo_sniper.recall import (
    Variant,
    VariantOutcome,
    coverage_report,
    greedy_order,
    group_union,
    load_variants,
    marginal_gains,
    run_variant,
    save_report,
    sequential_net_new,
    union_size,
)

#: 手算基準：
#:   A = {1,2,3}  B = {3,4}  C = {1,2,3}（完全被 A 蓋住）  D = {5}
#:   聯集 = {1,2,3,4,5} → 5
#:   邊際：A 0（C 一樣有 1,2,3）／B 1（只有它有 4）／C 0／D 1
SETS = {
    "A": {"1", "2", "3"},
    "B": {"3", "4"},
    "C": {"1", "2", "3"},
    "D": {"5"},
}


def test_union_size():
    assert union_size(SETS.values()) == 5
    assert union_size([]) == 0


def test_marginal_gain_is_zero_for_a_fully_covered_variant():
    """A 與 C 互為備份 → **兩個的邊際都是 0**，但不能兩個一起砍。

    這是邊際貢獻這個指標的已知陷阱，也是為什麼還要看 `greedy_order`：
    貪婪會選其中一個（+3），另一個才真的變成 0。
    """
    assert marginal_gains(SETS) == {"A": 0, "B": 1, "C": 0, "D": 1}


def test_marginal_gain_single_variant_is_its_whole_set():
    assert marginal_gains({"A": {"1", "2", "3"}}) == {"A": 3}


def test_sequential_net_new_is_order_sensitive():
    assert sequential_net_new(["A", "B", "C", "D"], SETS) == {"A": 3, "B": 1, "C": 0, "D": 1}
    # 換順序，同一組資料的答案不同——這正是它與 marginal_gains 的差別
    assert sequential_net_new(["C", "B", "A", "D"], SETS) == {"C": 3, "B": 1, "A": 0, "D": 1}


def test_sequential_net_new_ignores_unknown_labels():
    assert sequential_net_new(["A", "沒這個"], SETS) == {"A": 3, "沒這個": 0}


def test_greedy_order_picks_biggest_first_and_is_deterministic():
    order = greedy_order(SETS)
    assert order[0] == ("A", 3)          # 平手時取名稱排序 → A 先於 C
    assert sum(gain for _, gain in order) == 5
    assert dict(order)["C"] == 0
    assert greedy_order(SETS) == order   # 可重現


def test_greedy_order_respects_k():
    assert greedy_order(SETS, k=2) == [("A", 3), ("B", 1)]


def test_greedy_order_on_empty_input():
    assert greedy_order({}) == []


# ---------------------------------------------------------------------------
def _outcome(label, keys, *, group="", observable=True, listings=None, health="ok"):
    items = {k: f"標題 {k}" for k in keys}
    return VariantOutcome(
        variant=Variant(label=label, source="spy", keyword="kw", group=group),
        health=health,
        observable=observable,
        parsed=listings if listings is not None else len(keys),
        listings=listings if listings is not None else len(keys),
        items=items,
        requests=1,
    )


def test_noise_rate_distinguishes_zero_from_unknown():
    """回 0 筆時雜訊率是 None，不是 0——否則什麼都沒撈到的查詢看起來最乾淨。"""
    assert _outcome("x", ["a"], listings=4).noise_rate == 0.75
    assert _outcome("x", ["a", "b"], listings=2).noise_rate == 0.0
    assert _outcome("x", [], listings=0).noise_rate is None


def test_titles_without_needle_is_the_incident_evidence():
    """標題不含「遊戯王」的候選 = 舊組合結構上撈不到的那一類。"""
    out = VariantOutcome(
        variant=Variant("x", "spy", "kw"), health="ok", observable=True,
        parsed=2, listings=2,
        items={"k1": "遊戯王 PSA9 青眼の白龍 初期", "k2": "【PSA9】カエルスライム 初期 プレミアムパック"},
    )
    assert out.titles_without("遊戯王") == ["【PSA9】カエルスライム 初期 プレミアムパック"]


def test_unobservable_variants_are_excluded_from_coverage():
    """被擋的那一次是「不知道」，不是「沒有貢獻」。

    算進去的話它的邊際貢獻是 0，而 0 的意思是「可以砍」——一次 WAF 挑戰
    就會讓一條好 query 被刪掉。
    """
    outs = [
        _outcome("ok1", ["a", "b"], group="new"),
        _outcome("blocked", ["a", "b", "c"], group="new", observable=False, health="blocked"),
    ]
    cov = coverage_report(outs)
    assert cov["union_candidates"] == 2          # c 不算數
    assert cov["unobservable"] == ["blocked"]
    assert "blocked" not in cov["marginal_gains"]
    assert group_union(outs, "new") == {"a", "b"}


def test_coverage_report_groups_are_scoped_to_their_own_group():
    """組內邊際貢獻必須在**組內**算。

    混在一起算的話，new 裡一條好 query 的邊際會被 old 裡的重複條目吃掉、
    看起來像 0，然後被照著砍掉——而它正是要拿來取代 old 那條的。
    """
    outs = [
        _outcome("old-1", ["a", "b"], group="old"),
        _outcome("new-1", ["a", "b"], group="new"),
        _outcome("new-2", ["c"], group="new"),
    ]
    cov = coverage_report(outs)
    assert cov["union_candidates"] == 3
    # 全體來看 new-1 的邊際是 0（old-1 一樣有 a,b）……
    assert cov["marginal_gains"]["new-1"] == 0
    # ……但在 new 這一組裡它是 2
    assert cov["groups"]["new"]["marginal_gains"]["new-1"] == 2
    assert cov["groups"]["new"]["union_candidates"] == 3
    assert cov["groups"]["old"]["union_candidates"] == 2
    assert cov["groups"]["new"]["requests"] == 2


# ---------------------------------------------------------------------------
class _FakeSource:
    """回固定清單的 source，讓 `run_variant` 可以在沒有網路的情況下驗算。"""

    from ygo_sniper.domain import Site as _Site

    site = _Site.BUYEE_MERCARI
    supports_category = True

    def __init__(self, titles):
        self.titles = titles
        self.seen_kwargs: dict = {}

    def search_detailed(self, keyword, **kw):
        from conftest import make_listing

        from ygo_sniper.sources.health import SearchResult

        self.seen_kwargs = kw
        res = SearchResult(source="fake", site=self.site.value, query=keyword)
        for i, t in enumerate(self.titles):
            res.listings.append(make_listing(title=t, external_id=f"id{i}"))
        res.parsed_count = len(self.titles)
        return res


def test_run_variant_counts_only_is_candidate_survivors(cfg):
    """覆蓋率的分子是「工具真的會留下來的東西」，不是「解析出幾筆」。"""
    src = _FakeSource([
        "【PSA9】カエルスライム 初期 プレミアムパック",   # 候選（無「遊戯王」）
        "遊戯王 PSA10 青眼の白龍 25th",                  # 排除字 25th
        "遊戯王 青眼の白龍 美品",                        # 沒有鑑定機構
    ])
    out = run_variant(cfg, {"spy": src}, Variant("v", "spy", "PSA 初期", category="1152"))
    assert out.candidates == 1
    assert out.listings == 3
    assert out.noise_rate == pytest.approx(2 / 3, abs=1e-4)
    assert out.titles_without("遊戯王") == ["【PSA9】カエルスライム 初期 プレミアムパック"]
    assert sum(out.rejects.values()) == 2
    # 分類真的傳到 source（原 bug 的迴歸點）
    assert src.seen_kwargs["category"] == "1152"


def test_run_variant_on_missing_source_is_unobservable(cfg):
    out = run_variant(cfg, {}, Variant("v", "不存在的來源", "kw"))
    assert not out.observable and out.candidates == 0


def test_load_variants_rejects_duplicate_labels():
    spec = {"variants": [
        {"label": "x", "source": "s", "keyword": "a"},
        {"label": "x", "source": "s", "keyword": "b"},
    ]}
    with pytest.raises(ValueError, match="label 重複"):
        load_variants(spec)


@pytest.mark.parametrize("bad", [
    {"source": "s", "keyword": "a"},          # 缺 label
    {"label": "x", "keyword": "a"},           # 缺 source
    {"label": "x", "source": "s"},            # 缺 keyword（空字串合法，但要寫出來）
])
def test_load_variants_is_strict(bad):
    """研究要花真實請求，一條寫錯卻被靜默跳過的話，覆蓋率表會少一列而沒人發現。"""
    with pytest.raises(ValueError):
        load_variants({"variants": [bad]})


def test_load_variants_normalises_category_and_pages():
    v = load_variants({"variants": [
        {"label": "x", "source": "s", "keyword": "", "category": "", "pages": 0},
        {"label": "y", "source": "s", "keyword": "k", "category": 1152, "group": "new"},
    ]})
    assert v[0] == Variant("x", "s", "", None, "", 1)
    assert v[1] == Variant("y", "s", "k", "1152", "new", 1)


def test_save_report_round_trips(tmp_path):
    outs = [_outcome("a", ["k1"], group="new")]
    report = {"variants": [o.to_dict() for o in outs], "coverage": coverage_report(outs)}
    path = save_report(report, tmp_path / "sub" / "r.json")
    blob = json.loads(path.read_text(encoding="utf-8"))
    # 去重鍵要落檔，報告才能離線重算集合運算
    assert blob["variants"][0]["candidate_keys"] == ["k1"]
    assert blob["coverage"]["union_candidates"] == 1
