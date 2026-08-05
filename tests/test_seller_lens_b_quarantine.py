"""視角 B（模型正規化）的隔離線：**它已知會騙人，不准悄悄被用。**

2026-08-06。B 的整個正當性建立在一句話上——「模型偏誤同時出現在分子與分母，
相除時抵消」。獨立審查證明那**只對整片乘同一個常數的偏誤成立**，而本專案的模型
偏誤是**卡的價位帶的函數**（`valuation.py` 模組頂註：平台係數在高價卡上估出來、
套到便宜卡高估 6-8 倍）。B 的同群鍵刻意不含卡名，所以同一格裡混著不同價位帶的
卡，分子分母吃到的倍率不同，除不掉。

這一組測試守兩件事：

1. **反例本身**（`test_price_band_bias_makes_b_identical_to_c_and_both_wrong`）：
   真 alpha ＝ 1.000× 的賣家被 B 算成 0.169×，而且 **B 與 C 一字不差**——
   B 相對 C 的保護是零。這是 B 被隔離的證據，不是風格意見。
2. **不准被消費**（`test_no_display_or_decision_path_reads_the_quarantined_lenses`）：
   顯示與決策路徑的原始碼裡不准出現 B／C 的任何識別名。靠人記得「不要用那一欄」
   是行不通的（CLAUDE.md 第四節那個事故就是這樣活下來的），所以做成會紅燈的測試。

── 這個檔案什麼時候可以刪 ────────────────────────────────────────
**不是**「有人想把 B 顯示出來的時候」。要滿足全部三條才可以刪：

1. 根因修好：平台係數（`valuation.py` 的 venue premium）改成**分價位帶**估計，
   或以其他方式讓模型偏誤不再是價格的函數；`ygo-sniper coverage-groups
   --diagnose` 的 `L3 × Mercari` 那一格不再是 100% 下尾違反。
2. 抵消性質**重新被驗證**：本檔的
   `test_price_band_bias_makes_b_identical_to_c_and_both_wrong` 改寫成「B 不隨
   價位帶偏誤改變」並實測通過（現在它斷言的是 B 會被騙）。
3. `LensView.trusted` 對 B 改回 True，且 `known_broken` 清空。

只做第 3 條而沒有第 1、2 條，就是把警告刪掉當作問題解決——那正是本檔要擋的事。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ygo_sniper.seller_alpha import (
    BASIS_ASK,
    LENS_MODEL_ABS,
    LENS_MODEL_NORM,
    LENS_PEER,
    LENSES_NOTE,
    SALE_FIXED,
    AlphaParams,
    MarketRow,
    ModelCohortIndex,
    PeerIndex,
    build_lenses,
    build_seller_items,
    seller_metrics,
)

ROOT = Path(__file__).resolve().parents[1]

#: 便宜卡的模型高估倍率。用 5.9——那是 CLAUDE.md 記的 `L3 × Mercari` 實測值。
CHEAP_CARD_BIAS = 5.9

_SEQ = {"n": 0}


def row(*, price: float, seller: str, card: str) -> MarketRow:
    _SEQ["n"] += 1
    return MarketRow(
        key=f"Q{_SEQ['n']}",
        site="buyee_mercari",
        basis=BASIS_ASK,
        sale_kind=SALE_FIXED,
        price_twd=price,
        title=f"{card} PSA9",
        seller_key=f"buyee_mercari:{seller}",
        card_name=card,
        edition="unl",
        rarity="ultra",
        grader="PSA",
        grade=9.0,
        observed_at="2026-08-01T00:00:00+00:00",
        source_table="listing_obs",
    )


class _PriceBandBiasedValuator:
    """**價位帶相關**的模型偏誤：便宜卡高估 K 倍，貴卡估得準。

    這正是 `valuation.py` 模組頂註量到的形狀（平台係數在高價卡上估出來，
    套到 normal／rare 這種便宜卡高估 6-8 倍），不是為了打敗 B 而編的偏誤。
    `level` 固定，讓所有列落在同一個同群格裡——B 的同群鍵不含卡名，
    所以貴卡與便宜卡本來就會被放進同一格，那正是問題所在。
    """

    def __init__(self, true_fair: dict[str, float], *, cheap_below: float = 5000.0,
                 k: float = CHEAP_CARD_BIAS, level: str = "L3") -> None:
        self.true_fair = true_fair
        self.cheap_below = cheap_below
        self.k = k
        self.level = level

    def estimate(self, *, card_name=None, **_kw):
        fair = self.true_fair.get(card_name)
        outer = self

        class _E:
            fair_twd = (
                None if fair is None
                else fair * (outer.k if fair < outer.cheap_below else 1.0)
            )
            level = outer.level
            n_effective = 40

        return _E()


def _lenses_for(rows, seller_key, *, valuator=None, params=None):
    p = params or AlphaParams()
    peers = PeerIndex(rows, min_peers=p.min_peers)
    items = build_seller_items(rows, peers, valuator=valuator)
    cohorts = ModelCohortIndex(items, min_sellers=p.min_cohort_sellers)
    m = seller_metrics(seller_key, items.get(seller_key, []))
    return build_lenses(m, cohorts=cohorts, params=p), m


def _counterexample_rows() -> tuple[list[MarketRow], dict[str, float]]:
    """目標賣家專賣便宜卡、同群其他三人專賣貴卡，**每個人開的都是市價**。

    真實公允價：便宜卡 1,000、貴卡 10,000。所有人的真 alpha 都是 1.000×
    （沒有人便宜、也沒有人貴）。模型只對便宜卡高估 5.9 倍。
    """
    rows: list[MarketRow] = []
    fair: dict[str, float] = {}
    for i in range(4):
        rows.append(row(price=1000, seller="cheapshop", card=f"便宜卡{i}"))
        fair[f"便宜卡{i}"] = 1000.0
    for who in ("a", "b", "c"):
        for i in range(4):
            rows.append(row(price=10000, seller=who, card=f"{who}的貴卡{i}"))
            fair[f"{who}的貴卡{i}"] = 10000.0
    return rows, fair


# ---------------------------------------------------------------------------
# 1. 反例：B 與 C 一樣錯
# ---------------------------------------------------------------------------
def test_price_band_bias_makes_b_identical_to_c_and_both_wrong():
    """**這一條是 B 被隔離的證據。**

    賣家真 alpha ＝ 1.000×（開價就是市價），只因為他專賣便宜卡、同群其他人賣
    貴卡，模型的價位帶偏誤就讓 B ＝ C ＝ 1/5.9 ＝ 0.169×——「便宜 83%」。
    錯的方向是「看起來很划算」，使用者的直覺攔不下來（CLAUDE.md 第三節）。

    注意 `pytest.approx` 兩側比的是 B 與 C **彼此**：重點不只是 B 錯了，
    是 B 與 C **錯得一模一樣**——B 宣稱的那一層保護根本沒有發生。
    """
    rows, fair = _counterexample_rows()
    lenses, _m = _lenses_for(
        rows, "buyee_mercari:cheapshop", valuator=_PriceBandBiasedValuator(fair)
    )

    assert lenses.model_norm.ok is True
    assert lenses.model_norm.ratio == pytest.approx(1 / CHEAP_CARD_BIAS, rel=1e-3)
    assert lenses.model_norm.ratio == pytest.approx(0.169, abs=1e-3)

    # C 是「已知會出錯」的那把尺——B 給出的是同一個錯誤答案，一位小數都沒差。
    assert lenses.model_abs.ratio == pytest.approx(lenses.model_norm.ratio)

    # A（唯一可信的尺）誠實地說「湊不出同儕」，而不是被騙——這正是設計要的：
    # 寧可沒有數字，也不要一個看起來很划算的錯數字。
    assert lenses.peer.ok is False
    assert lenses.peer.ratio is None


# ---------------------------------------------------------------------------
# 2. 隔離標記：每一個分支都要蓋到
# ---------------------------------------------------------------------------
def _all_branch_lenses():
    """三種資料情境 × 三把尺——涵蓋 B／C 的「算得出來」與「證據不足」兩條路。"""
    rows, fair = _counterexample_rows()
    out = [_lenses_for(rows, "buyee_mercari:cheapshop",
                       valuator=_PriceBandBiasedValuator(fair))[0]]
    # 沒有 valuator → B／C 走「證據不足」分支
    out.append(_lenses_for(rows, "buyee_mercari:cheapshop")[0])
    # 同群其他賣家不足 → B 走另一條「證據不足」分支
    few = [row(price=1000, seller="cheapshop", card=f"便宜卡{i}") for i in range(4)]
    out.append(_lenses_for(few, "buyee_mercari:cheapshop",
                           valuator=_PriceBandBiasedValuator({f"便宜卡{i}": 1000.0
                                                              for i in range(4)}))[0])
    return out


def test_both_model_lenses_are_untrusted_in_every_branch():
    """B 與 C 的**每一個** return 分支都要帶 `trusted=False`。

    `ok=True` 而 `trusted=False` 是最危險的一種組合（有數字、看起來能用），
    但 `ok=False` 的分支也要蓋——不然有人只要讓資料變多就拿到一把沒蓋章的尺。
    """
    for lenses in _all_branch_lenses():
        for view in (lenses.model_norm, lenses.model_abs):
            assert view.trusted is False, f"{view.name} 不該是 trusted"
            assert view.known_broken, f"{view.name} 說不出為什麼不可信"
            assert view.caveats and view.caveats[0].startswith("⚠️"), (
                f"{view.name} 的第一條 caveat 必須是隔離告示"
            )
            assert "trusted=False" in view.caveats[0]


def test_the_peer_lens_stays_trusted():
    """A 沒有被這次隔離波及——它是唯一可信的尺，也是分數唯一的來源。"""
    rows, fair = _counterexample_rows()
    rows += [row(price=1000, seller="peer1", card="便宜卡0") for _ in range(1)]
    lenses, _m = _lenses_for(rows, "buyee_mercari:cheapshop",
                             valuator=_PriceBandBiasedValuator(fair))
    assert lenses.peer.trusted is True
    assert lenses.peer.known_broken == ""
    assert lenses.peer.scoring is True


def test_the_quarantine_reason_names_the_root_cause():
    """`known_broken` 不能只寫「不可信」——要寫出**修好什麼**才能改回來。

    沒有根因的告示會在下一次有人想用 B 的時候被當成過時的警語刪掉。
    """
    lenses = _all_branch_lenses()[0]
    why = lenses.model_norm.known_broken
    assert "價位帶" in why            # 根因：偏誤是價格的函數
    assert "0.169" in why             # 反例的數字
    assert "1.000" in why
    for word in ("價位帶", "抵消"):
        assert word in why


def test_the_shared_note_says_b_is_not_usable():
    """CLI 與 dashboard 共用的那一句話也要跟著改口，不能還在推薦 B。"""
    assert "抵消不成立" in LENSES_NOTE
    assert "trusted=False" in LENSES_NOTE
    assert "只讀 A" in LENSES_NOTE


# ---------------------------------------------------------------------------
# 3. 結構性防線：沒有任何路徑可以消費 B／C
# ---------------------------------------------------------------------------
#: 顯示路徑與決策路徑。**任何一個檔案在這裡出現 B／C 的識別名就是紅燈。**
_CONSUMER_FILES = (
    "src/ygo_sniper/cli.py",            # CLI：sellers / seller drill-down
    "src/ygo_sniper/seller_watch.py",   # 監控名單自動入選
    "src/ygo_sniper/notify_rules.py",   # Telegram 通知判定
    "web/app.py",                       # dashboard 的 API
    "web/static/index.html",            # dashboard 的畫面
)

#: 三把尺共用的型別（`LensView` 等）也在名單裡：拿到 `SellerLenses` 就等於
#: 拿到 B，「我只讀 A 欄」是靠自律，不是靠結構。要接 A 就從 `SellerScore`／
#: `SellerMetrics` 走，那兩個資料結構裡沒有模型視角。
_BANNED_NAMES = (
    "model_norm",
    "model_abs",
    "LENS_MODEL_NORM",
    "LENS_MODEL_ABS",
    "LENSES_NOTE",
    "SellerLenses",
    "LensView",
    "build_lenses",
    "ModelCohortIndex",
    "known_broken",
    "lenses",
)


def test_the_guarded_files_all_exist():
    """先證明這條防線不是空轉的：檔案改名／搬家時要紅燈，不是靜靜地全過。"""
    for rel in _CONSUMER_FILES:
        p = ROOT / rel
        assert p.is_file(), f"守衛清單指向不存在的檔案：{rel}"
        assert p.read_text(encoding="utf-8").strip(), f"{rel} 是空的"


def test_no_display_or_decision_path_reads_the_quarantined_lenses():
    """B／C 目前**連顯示都不准接**——它們是已知會出錯的數字。

    要解除這條限制，先滿足本檔頂註「什麼時候可以刪」的三個條件。
    只把名字從這個清單裡拿掉，等於把警告刪掉當作問題解決。
    """
    for rel in _CONSUMER_FILES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        for name in _BANNED_NAMES:
            assert name not in src, (
                f"{rel} 出現了 `{name}`：視角 B／C 已知會出錯（真 alpha 1.000× "
                f"會被算成 0.169×），在根因修好之前不得顯示或用於決策。"
                f"理由與解除條件見 tests/{Path(__file__).name}"
            )


def test_the_lens_names_used_by_the_guard_are_the_real_ones():
    """守衛用的字串必須是**真的識別名**——打錯字的守衛永遠是綠的。"""
    assert LENS_MODEL_NORM == "model_norm"
    assert LENS_MODEL_ABS == "model_abs"
    assert LENS_PEER == "peer"
    for name in (LENS_MODEL_NORM, LENS_MODEL_ABS):
        assert name in _BANNED_NAMES
    # A 不在黑名單裡：分數與畫面本來就該讀它。
    assert LENS_PEER not in _BANNED_NAMES
