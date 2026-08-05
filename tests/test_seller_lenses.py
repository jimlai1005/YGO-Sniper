"""三個並排的比價視角（2026-08-06）。

使用者知道 CLAUDE.md 第四節那個事故（`ebay:collectiblemore` 模型絕對法 0.57×
vs 同儕相對法 1.59×，**符號相反**），仍然要求把模型視角加回來解決同儕覆蓋率
問題。他選的方案是「三種都算、並排顯示、**不合成**」。

所以這一組測試守的是三條紅線，不是「函式會不會跑」：

1. **B 的偏誤抵消性質**（本檔存在的理由）：模型在視角 B 裡是**控制變數**不是
   真值。同一個模型偏誤同時出現在分子（這個賣家）與分母（同群其他賣家），
   相除時抵消。測法是把模型對整個切片的高估倍率 K 從 1 拉到 5.9，
   **B 不准動**（而 C 必須跟著動 1/K）——B 若會隨 K 改變，它就只是 C 的變形，
   沒有存在的理由。
2. **三把尺不准合成**：沒有任何欄位／函式把 A、B、C 相加、平均、或挑一個當
   最終分數。分數（`SellerScore.total`）永遠只讀 A。
3. **B/C 不進決策路徑**：監控名單入選（`seller_watch.sync_auto_watch`）與
   Telegram 通知（`notify_rules`）只能看 A。B、C 是純顯示。
"""

from __future__ import annotations

import inspect

import pytest

from ygo_sniper import notify_rules, seller_watch
from ygo_sniper.seller_alpha import (
    BASIS_ASK,
    BASIS_BID,
    BASIS_SOLD,
    LENS_MODEL_ABS,
    LENS_MODEL_NORM,
    LENS_ORDER,
    LENS_PEER,
    LENSES_NOTE,
    SALE_AUCTION,
    SALE_FIXED,
    AlphaParams,
    MarketRow,
    ModelCohortIndex,
    PeerIndex,
    SellerLenses,
    build_lenses,
    build_seller_items,
    score_seller,
    seller_metrics,
)

# ---------------------------------------------------------------------------
# 造資料
# ---------------------------------------------------------------------------
_SEQ = {"n": 0}


def row(
    *,
    price: float,
    seller: str | None,
    card: str,
    site: str = "ebay",
    basis: str = BASIS_ASK,
    sale_kind: str = SALE_FIXED,
    grade: float | None = 9.0,
    source_table: str = "listing_obs",
) -> MarketRow:
    _SEQ["n"] += 1
    return MarketRow(
        key=f"L{_SEQ['n']}",
        site=site,
        basis=basis,
        sale_kind=sale_kind,
        price_twd=price,
        title=f"{card} PSA{grade}",
        seller_key=f"{site}:{seller}" if seller else None,
        card_name=card,
        edition="unl",
        rarity="ultra",
        grader="PSA",
        grade=grade,
        observed_at="2026-08-01T00:00:00+00:00",
        source_table=source_table,
    )


class _ScaledValuator:
    """對整個切片系統性高估 K 倍的模型。

    `fair = true_fair × K`。K=5.9 就是 CLAUDE.md 記的那個 `L3 × Mercari`
    實測偏誤。`level` 固定成一個值，讓所有列落在同一個群裡（群的定義本身
    另有測試）。
    """

    def __init__(self, true_fair: dict[str, float], k: float, *, level: str = "L3") -> None:
        self.true_fair = true_fair
        self.k = k
        self.level = level

    def estimate(self, *, card_name=None, **_kw):
        fair = self.true_fair.get(card_name)
        outer = self

        class _E:
            fair_twd = None if fair is None else fair * outer.k
            level = outer.level
            n_effective = 40

        return _E()


def _lenses_for(
    rows: list[MarketRow],
    seller_key: str,
    *,
    valuator=None,
    params: AlphaParams | None = None,
    sites_without_comps: set[str] | None = None,
) -> tuple[SellerLenses, object]:
    """跑完整條路徑（同 `analyze` 的組裝順序），回 (lenses, metrics)。"""
    p = params or AlphaParams()
    peers = PeerIndex(rows, min_peers=p.min_peers)
    items = build_seller_items(rows, peers, valuator=valuator)
    cohorts = ModelCohortIndex(items, min_sellers=p.min_cohort_sellers)
    m = seller_metrics(seller_key, items.get(seller_key, []))
    lenses = build_lenses(
        m, cohorts=cohorts, params=p, sites_without_comps=sites_without_comps or set()
    )
    return lenses, m


# ---------------------------------------------------------------------------
# 1. B 的偏誤抵消性質——**這一條是視角 B 存在的理由**
# ---------------------------------------------------------------------------
def _bias_scenario() -> list[MarketRow]:
    """四個賣家、每人賣**不同的卡**（所以同儕相對一筆都湊不出來）。

    真實公允價一律 1000：目標賣家開 800（真的比市場便宜 20%），
    其餘三個賣家開 1000（就是市場價）。
    """
    rows: list[MarketRow] = []
    for i in range(4):
        rows.append(row(price=800, seller="target", card=f"目標卡{i}"))
    for who in ("a", "b", "c"):
        for i in range(4):
            rows.append(row(price=1000, seller=who, card=f"{who}的卡{i}"))
    return rows


_TRUE_FAIR = {f"目標卡{i}": 1000.0 for i in range(4)}
_TRUE_FAIR.update({f"{w}的卡{i}": 1000.0 for w in ("a", "b", "c") for i in range(4)})


@pytest.mark.parametrize("k", [1.0, 2.5, 5.9, 0.4])
def test_model_normalized_lens_is_invariant_to_a_uniform_model_bias(k):
    """**本檔最重要的一條。**

    模型對整個切片高估 K 倍時：
      - 視角 C（模型絕對）＝ 價 ÷ 模型估值 → 整個被 K 除，**會隨 K 亂跳**。
      - 視角 B（模型正規化）＝ 賣家的比值中位 ÷ 同群其他賣家的比值中位
        → 分子分母同樣被 K 除，**K 完全抵消**，永遠回 0.800×。

    B 若會隨 K 改變，它就只是 C 的變形，沒有存在的理由。
    """
    rows = _bias_scenario()
    lenses, m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(_TRUE_FAIR, k))

    # B：不隨 K 改變，而且答出了真話（比同群其他賣家便宜 20%）
    assert lenses.model_norm.ok is True
    assert lenses.model_norm.ratio == pytest.approx(0.8)

    # C：隨 K 走（K=5.9 時看起來像打 1.4 折，那是模型的偏誤不是賣家的 alpha）
    assert lenses.model_abs.ok is True
    assert lenses.model_abs.ratio == pytest.approx(0.8 / k)
    assert m.model_ratio_median == pytest.approx(0.8 / k)

    # A：這批資料裡每個賣家賣的卡都不一樣 → 同儕相對根本湊不出來（B 要解的就是這個）
    assert lenses.peer.ok is False
    assert lenses.peer.ratio is None


def test_model_normalized_lens_covers_a_seller_that_peer_relative_cannot():
    """B 的覆蓋率機制：同群**不必是同一張卡**，所以 A 算不出來的賣家 B 算得出來。"""
    rows = _bias_scenario()
    lenses, m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(_TRUE_FAIR, 5.9))
    assert m.n_comparable == 0                 # 同儕相對：零可比
    assert lenses.peer.ok is False
    assert lenses.model_norm.ok is True        # 模型正規化：算得出來
    assert lenses.model_norm.n == 4


def test_model_normalized_lens_does_not_cancel_a_bias_that_only_hits_one_seller():
    """誠實邊界：B 抵消的是**群內共通**的偏誤，不是每一種偏誤。

    模型只對目標賣家那幾張卡高估 5 倍（群內非共通）→ B 就會被騙。
    這條測試把 B 的極限釘在畫面上，免得有人把它當成「不受模型影響」。
    """
    rows = _bias_scenario()
    skewed = dict(_TRUE_FAIR)
    for i in range(4):
        skewed[f"目標卡{i}"] = 5000.0     # 只有目標賣家的卡被模型高估
    lenses, _m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(skewed, 1.0))
    assert lenses.model_norm.ratio == pytest.approx(0.16)   # 800/5000 ÷ 1000/1000
    assert any("群內" in c for c in lenses.model_norm.caveats)


# ---------------------------------------------------------------------------
# 2. 證據不足要拒答（None，不是 0）
# ---------------------------------------------------------------------------
def test_model_normalized_lens_refuses_when_the_cohort_has_too_few_sellers():
    """同群其他賣家不足 3 個 → 不給 B，回「證據不足」＋缺什麼，**ratio 是 None**。"""
    rows = [row(price=800, seller="target", card=f"目標卡{i}") for i in range(4)]
    rows += [row(price=1000, seller="a", card=f"a的卡{i}") for i in range(4)]
    lenses, _m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(_TRUE_FAIR, 1.0))
    assert lenses.model_norm.ok is False
    assert lenses.model_norm.ratio is None      # 不是 0——0 會被讀成「免費」
    assert lenses.model_norm.missing
    assert "3" in "".join(lenses.model_norm.missing)


def test_model_lenses_refuse_when_there_is_no_model_at_all():
    """沒有 valuator（dashboard／通知路徑的預設）→ B 與 C 都誠實地說算不出來。"""
    lenses, _m = _lenses_for(_bias_scenario(), "ebay:target", valuator=None)
    for view in (lenses.model_norm, lenses.model_abs):
        assert view.ok is False
        assert view.ratio is None
        assert view.missing


def test_model_absolute_lens_refuses_below_the_row_threshold():
    """模型只估得出 2 筆（門檻 3）→ C 拒答，不給一個 2 筆撐的數字。"""
    rows = [row(price=800, seller="target", card=f"目標卡{i}") for i in range(4)]
    rows += [row(price=1000, seller=w, card=f"{w}的卡{i}")
             for w in ("a", "b", "c") for i in range(4)]
    partial = {f"目標卡{i}": 1000.0 for i in range(2)}      # 只有兩張估得出來
    partial.update({f"{w}的卡{i}": 1000.0 for w in ("a", "b", "c") for i in range(4)})
    lenses, _m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(partial, 1.0))
    assert lenses.model_abs.ok is False
    assert lenses.model_abs.ratio is None
    assert "2" in "".join(lenses.model_abs.missing)


def test_every_lens_reports_its_availability():
    """B、C 都要說得出「幾筆算得出模型估值」——估不出來的標的不計入，
    而那件事必須看得見，不是靜默地少算幾筆。"""
    rows = _bias_scenario()
    partial = {f"目標卡{i}": 1000.0 for i in range(3)}      # 4 筆裡只有 3 筆估得出來
    partial.update({f"{w}的卡{i}": 1000.0 for w in ("a", "b", "c") for i in range(4)})
    lenses, m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(partial, 1.0))
    assert m.n_rows == 4
    assert lenses.model_abs.ok is True and lenses.model_abs.n == 3
    assert any("4" in c and "3" in c for c in lenses.model_abs.caveats)
    assert any("4" in c and "3" in c for c in lenses.model_norm.caveats)


# ---------------------------------------------------------------------------
# 3. 方向相反要大聲標出來（`ebay:collectiblemore` 是活教材）
# ---------------------------------------------------------------------------
def _collectiblemore_scenario() -> tuple[list[MarketRow], dict[str, float]]:
    """重現 CLAUDE.md 第四節那筆：同儕說「比同儕貴」、模型說「打折」。

    目標賣家開 1590，同儕（賣同一張卡）開 1000 → A ≈ 1.59×（貴 59%）。
    模型把 eBay 的公允價估成 2789（平台係數估不出來，整站高估）
    → C ≈ 0.57×（看起來像 43% 折價）。
    """
    rows: list[MarketRow] = []
    for i in range(4):
        rows.append(row(price=1590, seller="collectiblemore", card=f"卡{i}"))
        rows.append(row(price=1000, seller="peer1", card=f"卡{i}"))
        rows.append(row(price=1000, seller="peer2", card=f"卡{i}"))
        rows.append(row(price=1000, seller="peer3", card=f"卡{i}"))
    return rows, {f"卡{i}": 2789.0 for i in range(4)}


def test_lenses_flag_a_sign_conflict_between_peer_and_model_absolute():
    """A 說貴、C 說便宜 → **必須有明確警示**，而且要寫出「以同儕相對為準」。"""
    rows, fair = _collectiblemore_scenario()
    lenses, _m = _lenses_for(rows, "ebay:collectiblemore",
                             valuator=_ScaledValuator(fair, 1.0))

    assert lenses.peer.ok and lenses.peer.ratio == pytest.approx(1.59)
    assert lenses.model_abs.ok and lenses.model_abs.ratio == pytest.approx(1590 / 2789)
    assert lenses.peer.direction == "pricier"
    assert lenses.model_abs.direction == "cheaper"

    assert lenses.has_conflict is True
    text = "".join(lenses.conflicts)
    assert "⚠️" in text
    assert "同儕相對為準" in text
    assert "模型絕對" in text


def test_no_conflict_flag_when_every_lens_points_the_same_way():
    rows = [row(price=800, seller="t", card=f"卡{i}") for i in range(4)]
    rows += [row(price=1000, seller=w, card=f"卡{i}")
             for w in ("a", "b", "c") for i in range(4)]
    fair = {f"卡{i}": 1000.0 for i in range(4)}
    lenses, _m = _lenses_for(rows, "ebay:t", valuator=_ScaledValuator(fair, 1.0))
    assert lenses.peer.direction == "cheaper"
    assert lenses.model_abs.direction == "cheaper"
    assert lenses.has_conflict is False
    assert lenses.conflicts == ()


def test_tiny_differences_around_one_are_not_reported_as_a_conflict():
    """1.001× vs 0.999× 不是「方向相反」——沒有死區的話每一點雜訊都會告警，
    告警多到看不完就等於沒有告警。"""
    rows = [row(price=1000, seller="t", card=f"卡{i}") for i in range(4)]
    rows += [row(price=1000, seller=w, card=f"卡{i}")
             for w in ("a", "b", "c") for i in range(4)]
    fair = {f"卡{i}": 1001.0 for i in range(4)}
    lenses, _m = _lenses_for(rows, "ebay:t", valuator=_ScaledValuator(fair, 1.0))
    assert lenses.has_conflict is False


def test_model_absolute_lens_warns_when_the_site_has_no_comps_at_all():
    """comps 表一筆該站成交都沒有 → 平台係數估不出來，這件事要講出來，
    不是靜默地給一個數字（CLAUDE.md 第四節的根因）。"""
    rows, fair = _collectiblemore_scenario()
    lenses, _m = _lenses_for(
        rows, "ebay:collectiblemore",
        valuator=_ScaledValuator(fair, 1.0), sites_without_comps={"ebay"},
    )
    assert any("平台係數" in c for c in lenses.model_abs.caveats)
    # B 也要講，但講的是「這種整站共通的偏誤在相除時抵消」——兩者的傷害不同。
    assert any("抵消" in c for c in lenses.model_norm.caveats)


# ---------------------------------------------------------------------------
# 4. 三把尺不准合成
# ---------------------------------------------------------------------------
def test_no_field_or_method_ever_combines_the_three_lenses():
    """使用者要的是三個並列的觀點。任何「總分／平均／最佳」欄位都是違規。"""
    banned = ("total", "combined", "blended", "average", "mean", "best",
              "final", "consensus", "merged", "sum")
    names = {n for n in dir(SellerLenses) if not n.startswith("_")}
    for n in names:
        assert not any(b in n.lower() for b in banned), f"SellerLenses 不該有 {n}"
    rows, fair = _collectiblemore_scenario()
    lenses, _m = _lenses_for(rows, "ebay:collectiblemore",
                             valuator=_ScaledValuator(fair, 1.0))
    # 三個視角各自獨立，順序固定，`views` 只是並排不是聚合。
    assert tuple(v.name for v in lenses.views) == LENS_ORDER
    assert LENS_ORDER == (LENS_PEER, LENS_MODEL_NORM, LENS_MODEL_ABS)
    assert "不可相加" in LENSES_NOTE


def test_the_score_still_reads_only_the_peer_lens():
    """C 說「打 4 折」，A 說「比同儕貴 59%」→ 分數必須是 0（讀 A）。"""
    rows, fair = _collectiblemore_scenario()
    lenses, m = _lenses_for(rows, "ebay:collectiblemore",
                            valuator=_ScaledValuator(fair, 1.0))
    score = score_seller(m)
    assert score.ok is True
    assert score.total == pytest.approx(0.0)
    assert "沒有 alpha" in score.reason
    assert lenses.model_abs.ratio is not None and lenses.model_abs.ratio < 0.6


def test_only_the_peer_lens_is_marked_as_feeding_decisions():
    rows, fair = _collectiblemore_scenario()
    lenses, _m = _lenses_for(rows, "ebay:collectiblemore",
                             valuator=_ScaledValuator(fair, 1.0))
    assert lenses.peer.scoring is True
    assert lenses.model_norm.scoring is False
    assert lenses.model_abs.scoring is False
    assert [v.name for v in lenses.views if v.scoring] == [LENS_PEER]


# ---------------------------------------------------------------------------
# 5. B/C 不進 watch 與 notify 的判定路徑
# ---------------------------------------------------------------------------
def test_watch_and_notify_modules_never_mention_the_model_lenses():
    """結構性防線：判定路徑的兩個模組原始碼裡不准出現 B／C 的任何名字。

    靠人記得「不要用模型」是行不通的（CLAUDE.md 第四節那個事故就是這樣
    活下來的），所以把它變成一條會失敗的測試。
    """
    banned = ("model_norm", "model_abs", "model_ratio", "LensView",
              "SellerLenses", "build_lenses", "ModelCohortIndex", "lenses")
    for mod in (seller_watch, notify_rules):
        src = inspect.getsource(mod)
        for name in banned:
            assert name not in src, f"{mod.__name__} 不該提到 {name}"


class _FakeStore:
    """`sync_auto_watch` 只需要這幾個方法。"""

    def __init__(self) -> None:
        self.added: list[str] = []

    def list_seller_watch(self, active_only: bool = False):
        return []

    def get_seller_watch(self, key: str):
        return None

    def upsert_seller_watch(self, seller_key, **kw):
        self.added.append(seller_key)
        return True


class _FakeScore:
    def __init__(self, key, total, ok=True):
        self.seller_key = key
        self.total = total
        self.ok = ok
        self.reason = "測試用"


class _FakeReport:
    def __init__(self, pairs):
        self._pairs = pairs

    def ranked(self):
        return self._pairs


def test_model_lenses_never_get_a_seller_into_the_watch_list():
    """一個 A 說「比同儕貴」（分數 0）、B／C 說「打 2 折」的賣家，
    **不准**因為模型視角被自動加進監控名單。"""
    from ygo_sniper.seller_watch import WatchParams, sync_auto_watch

    store = _FakeStore()
    params = WatchParams()
    rows, fair = _collectiblemore_scenario()
    lenses, m = _lenses_for(rows, "ebay:collectiblemore",
                            valuator=_ScaledValuator(fair, 1.0))
    assert lenses.model_abs.ratio < 0.6          # 模型說「打 4 折」
    score = score_seller(m)
    assert score.total == pytest.approx(0.0)

    out = sync_auto_watch(store, _FakeReport([(score, m)]), params)
    assert out["added"] == []
    assert store.added == []


def test_sync_auto_watch_only_ever_reads_the_alpha_total():
    """把 SellerScore.total 換成一個「模型視角算出來的高分」時名單才會動——
    反過來證明入選判準就是那一個欄位（A），沒有第二條路。"""
    from ygo_sniper.seller_watch import WatchParams, sync_auto_watch

    store = _FakeStore()
    params = WatchParams()
    rows, fair = _collectiblemore_scenario()
    _lenses, m = _lenses_for(rows, "ebay:collectiblemore",
                             valuator=_ScaledValuator(fair, 1.0))
    high = _FakeScore("ebay:collectiblemore", params.auto_min_score + 1.0)
    out = sync_auto_watch(store, _FakeReport([(high, m)]), params)
    assert [a["seller_key"] for a in out["added"]] == ["ebay:collectiblemore"]


# ---------------------------------------------------------------------------
# 6. 同源紀律：B 的群不准混基準／混型態
# ---------------------------------------------------------------------------
def test_cohorts_never_mix_basis_or_sale_kind():
    """成交列（競標結標）不可以當在架定價的「同群」——那是兩把尺
    （CLAUDE.md 第三節第七項）。"""
    rows = [row(price=800, seller="target", card=f"目標卡{i}") for i in range(4)]
    rows += [
        row(price=1000, seller=w, card=f"{w}的卡{i}",
            basis=BASIS_SOLD, sale_kind=SALE_AUCTION, source_table="comps")
        for w in ("a", "b", "c") for i in range(4)
    ]
    lenses, _m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(_TRUE_FAIR, 1.0))
    assert lenses.model_norm.ok is False        # 同群裡一個「同基準同型態」的賣家都沒有
    assert lenses.model_norm.ratio is None


def test_cohorts_never_mix_sites():
    """跨站比＝把平台差算成賣家 alpha（模組頂註第 2 點），B 也不例外。"""
    rows = [row(price=800, seller="target", card=f"目標卡{i}") for i in range(4)]
    rows += [row(price=1000, seller=w, card=f"{w}的卡{i}", site="buyee_yahoo")
             for w in ("a", "b", "c") for i in range(4)]
    lenses, _m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(_TRUE_FAIR, 1.0))
    assert lenses.model_norm.ok is False


def test_bid_rows_never_enter_the_cohort():
    """競標中出價不是賣家的定價決定——A 不收，B 也不准收。"""
    rows = [row(price=800, seller="target", card=f"目標卡{i}") for i in range(4)]
    rows += [row(price=1000, seller=w, card=f"{w}的卡{i}",
                 basis=BASIS_BID, sale_kind=SALE_AUCTION)
             for w in ("a", "b", "c") for i in range(4)]
    lenses, _m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(_TRUE_FAIR, 1.0))
    assert lenses.model_norm.ok is False


def test_one_prolific_seller_cannot_dominate_the_cohort_baseline():
    """同群基準取的是**各賣家中位的中位**（一人一票），不是把所有列倒成一池。

    群是「同站×同基準×同型態×同層級」，一個上架量大的賣家很容易在池裡
    占七成——那樣量到的是那一家的定價，不是同群水準。
    """
    rows = [row(price=800, seller="target", card=f"目標卡{i}") for i in range(4)]
    rows += [row(price=1000, seller="a", card=f"a的卡{i}") for i in range(2)]
    rows += [row(price=1000, seller="b", card=f"b的卡{i}") for i in range(2)]
    rows += [row(price=4000, seller="whale", card=f"whale的卡{i}") for i in range(40)]
    fair = {f"目標卡{i}": 1000.0 for i in range(4)}
    fair.update({f"a的卡{i}": 1000.0 for i in range(2)})
    fair.update({f"b的卡{i}": 1000.0 for i in range(2)})
    fair.update({f"whale的卡{i}": 1000.0 for i in range(40)})
    lenses, _m = _lenses_for(rows, "ebay:target", valuator=_ScaledValuator(fair, 1.0))
    # 三個其他賣家的比值中位是 1.0／1.0／4.0 → 中位 1.0（whale 只有一票）
    assert lenses.model_norm.ratio == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# 7. A 沒有被改動
# ---------------------------------------------------------------------------
def test_peer_lens_is_a_pure_view_of_the_existing_metric():
    """A 只是被包成一個視角，算法一個字都沒動。"""
    rows, fair = _collectiblemore_scenario()
    lenses, m = _lenses_for(rows, "ebay:collectiblemore",
                            valuator=_ScaledValuator(fair, 1.0))
    assert lenses.peer.ratio == m.discount_ratio_median
    assert lenses.peer.n == m.n_comparable


def test_peer_lens_gate_matches_the_score_gate():
    """A 的「算得出來」與 `score_seller` 的「給得出分數」是同一道閘門——
    兩個數字若各有一套門檻，畫面上的覆蓋率與排行榜長度會安靜地對不起來。"""
    rows = [row(price=800, seller="t", card="卡0"), row(price=1000, seller="p", card="卡0")]
    lenses, m = _lenses_for(rows, "ebay:t")
    assert score_seller(m).ok is False
    assert lenses.peer.ok is False
    assert lenses.peer.ratio is None


# ---------------------------------------------------------------------------
# 8. analyze 端到端
# ---------------------------------------------------------------------------
def test_analyze_reports_lens_coverage_for_all_three():
    from ygo_sniper.seller_alpha import analyze

    rows, fair = _collectiblemore_scenario()

    class _NoIndex:
        """卡名比對關掉 → `_card_name_of` 直接用列上的 `card_name`。"""

        available = False

    class _Store:
        def listing_obs(self, limit=0):
            return [
                {"key": r.key, "site": r.site, "seller_id": r.seller_key.split(":")[1],
                 "title": r.title, "price_twd": r.price_twd, "price_kind": "fixed",
                 "card_name": r.card_name, "rarity": r.rarity, "grader": r.grader,
                 "grade": r.grade, "url": None, "first_seen": r.observed_at}
                for r in rows
            ]

        def comps_by(self, limit=0):
            return []

        def list_signals(self, state="all", limit=0):
            return []

        def list_sellers(self, limit=0):
            return []

    rep = analyze(_Store(), index=_NoIndex(), valuator=_ScaledValuator(fair, 1.0))
    assert set(rep.lenses) == set(rep.metrics)
    cov = rep.coverage["lens_coverage"]
    assert set(cov) == {LENS_PEER, LENS_MODEL_NORM, LENS_MODEL_ABS}
    assert cov[LENS_PEER] >= 1
    assert cov[LENS_MODEL_ABS] >= 1
    assert rep.coverage["lens_sign_conflicts"] >= 1
