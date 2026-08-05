"""估價模型：退化階梯、部分池化、conformal 區間、推播格式。

這個模組是整個工具的判斷力所在，而它的錯**不會有任何外顯症狀**：
一個沒有樣本支撐的點估計看起來跟一個有 20 筆成交支撐的一模一樣。
所以這裡釘的每一條都是「數字背後的宣稱強度」：用了哪一層、幾筆樣本、
校準夠不夠。任何一條被放寬，這個工具就從「估價」退化成「說一個數字」。
"""

from __future__ import annotations

import dataclasses
import json
import math

import pytest

from ygo_sniper.notify import format_signal
from ygo_sniper.valuation import (
    BASELINE_GRADE,
    Estimate,
    Obs,
    Params,
    ValuationModel,
    Valuator,
    _shrink,
    _time_split,
    coverage_check,
    coverage_time_split,
)

DASH = "http://127.0.0.1:8321"

#: 測試一律給滿先驗，模型才是決定性的（不會因為 settings.yaml 改了就漂）。
PRIOR = Params(
    confidence=0.80,
    shrink_k=4,
    min_calibration=10,
    calibration_fraction=0.4,
    min_cell=3,
    grade_premium_prior={10.0: 4.0, 9.0: 1.0, 8.0: 0.5, 7.0: 0.25},
    rarity_premium_prior={"ultimate": 4.7, "secret": 3.5, "ultra": 2.2, "normal": 1.0},
)


def _rows() -> list[Obs]:
    """一份刻意稀疏的樣本，長得像真實資料：多數桶只有 1-2 筆。"""
    raw = [
        # 卡 A / ultra：9 分兩筆、10 分一筆 → 撐得起 L1 與 L2
        (1000.0, "A", "ultra", 9.0),
        (1200.0, "A", "ultra", 9.0),
        (4000.0, "A", "ultra", 10.0),
        # 其他 ultra 卡：撐得起 L3
        (600.0, "B", "ultra", 9.0),
        (700.0, "C", "ultra", 8.0),
        (900.0, "D", "ultra", 10.0),
        # 別的稀有度
        (300.0, "E", "normal", 9.0),
        (350.0, "F", "normal", 9.0),
    ]
    return [Obs(p, c, r, g, key=f"{c}-{r}-{g}") for p, c, r, g in raw]


# ---------------------------------------------------------------------------
class TestShrinkWeight:
    """w = n/(n+k)。這條公式是「樣本少就往先驗靠」的全部內容，兩個極端要釘死。"""

    def test_zero_samples_uses_parent_entirely(self):
        assert _shrink(999.0, 5.0, n=0, k=4) == 5.0

    def test_large_n_almost_ignores_parent(self):
        got = _shrink(10.0, 0.0, n=10_000, k=4)
        assert got == pytest.approx(10.0, rel=1e-3)

    def test_k_controls_how_fast_data_wins(self):
        """n=k 時剛好一半一半——這是 k 的物理意義，不要讓它悄悄漂掉。"""
        assert _shrink(10.0, 0.0, n=4, k=4) == pytest.approx(5.0)

    def test_k_zero_means_no_shrinkage(self):
        assert _shrink(10.0, 0.0, n=1, k=0) == pytest.approx(10.0)


class TestLadderLevels:
    """三層各自要能被觸發，而且要**報出自己是哪一層**。"""

    @pytest.fixture
    def v(self):
        return Valuator(_rows(), PRIOR)

    def test_L1_same_card_rarity_grade(self, v):
        e = v.estimate(card_name="A", rarity="ultra", grade=9.0)
        assert e.level == "L1" and e.n_effective == 2
        assert "卡名×稀有度×分數" in e.level_label

    def test_L2_same_card_other_grade_only(self, v):
        """卡 A 的 ultra 沒有 8 分成交，但有 9 分與 10 分——用分數曲線換算過來。"""
        e = v.estimate(card_name="A", rarity="ultra", grade=8.0)
        assert e.level == "L2" and e.n_effective == 3

    def test_L3_unknown_card_but_known_rarity(self, v):
        e = v.estimate(card_name="從沒見過的卡", rarity="ultra", grade=9.0)
        assert e.level == "L3" and e.n_effective == 6

    def test_L0_when_even_rarity_is_unseen(self, v):
        e = v.estimate(card_name="從沒見過的卡", rarity="parallel", grade=9.0)
        assert e.level == "L0" and e.n_effective == 0
        assert e.fair_twd is not None          # 仍給得出數字，但層級誠實標成 L0

    def test_unmatched_card_falls_back_to_rarity_layer(self, v):
        """卡名比對不到（card_name=None）不能讓估價整個失效，只是少了兩層。"""
        assert v.estimate(card_name=None, rarity="ultra", grade=9.0).level == "L3"


class TestLadderNumbers:
    def test_no_samples_below_L3_gives_identical_estimate(self):
        """n=0 的層必須**完全**等於上層——不是「差不多」，是同一個數字。"""
        v = Valuator(_rows(), PRIOR)
        a = v.estimate(card_name="從沒見過的卡", rarity="ultra", grade=9.0)
        b = v.estimate(card_name=None, rarity="ultra", grade=9.0)
        assert a.fair_twd == b.fair_twd

    def test_many_samples_pull_estimate_onto_the_layer_median(self):
        """本層樣本很多時，估計值幾乎就是本層中位數（先驗被淹掉）。"""
        rows = [Obs(5000.0, "X", "ultra", 9.0, key=f"x{i}") for i in range(200)]
        rows += [Obs(100.0, "Y", "ultra", 9.0, key=f"y{i}") for i in range(3)]
        v = Valuator(rows, PRIOR)
        e = v.estimate(card_name="X", rarity="ultra", grade=9.0)
        assert e.level == "L1"
        assert e.fair_twd == pytest.approx(5000, rel=0.05)

    def test_grade_premium_moves_estimate_in_the_right_direction(self):
        v = Valuator(_rows(), PRIOR)
        hi = v.estimate(card_name="A", rarity="ultra", grade=10.0).fair_twd
        mid = v.estimate(card_name="A", rarity="ultra", grade=9.0).fair_twd
        lo = v.estimate(card_name="A", rarity="ultra", grade=7.0).fair_twd
        assert lo < mid < hi

    def test_baseline_grade_needs_no_coefficient(self):
        m = ValuationModel(_rows(), PRIOR)
        assert m.g(BASELINE_GRADE) == 0.0
        assert m.grade_is_estimated(BASELINE_GRADE)

    def test_unknown_grade_is_not_guessed(self):
        """分數不明就當基準分數，不從標題猜——猜出來的分數會直接變成假樣本。"""
        m = ValuationModel(_rows(), PRIOR)
        assert m.g(None) == 0.0


class TestCalibration:
    """校準集不足時**不給區間**。這是誠實與有用之間唯一不能妥協的地方。"""

    def _many(self, n=200):
        rows = []
        for i in range(n):
            rows.append(Obs(1000.0 * (1 + (i % 7) / 10), f"C{i % 20}", "ultra", 9.0, f"k{i}"))
        return rows

    def test_thin_calibration_refuses_to_give_an_interval(self):
        v = Valuator(_rows(), PRIOR)          # 8 筆 → 校準集必然 < 10
        e = v.estimate(card_name="A", rarity="ultra", grade=9.0)
        assert not v.calibrated
        assert e.lo_twd is None and e.hi_twd is None and not e.has_interval
        assert any("樣本不足以校準" in n for n in e.notes)

    def test_thin_calibration_also_refuses_the_probability(self):
        """沒校準過的殘差分布算出來的機率一樣不可信，要一起擋掉。"""
        v = Valuator(_rows(), PRIOR)
        e = v.estimate(card_name="A", rarity="ultra", grade=9.0, landed_twd=500)
        assert e.p_worth_buying is None

    def test_enough_calibration_gives_a_bracketing_interval(self):
        v = Valuator(self._many(), PRIOR)
        assert v.calibrated
        e = v.estimate(card_name="C1", rarity="ultra", grade=9.0)
        assert e.has_interval
        assert e.lo_twd < e.fair_twd < e.hi_twd
        assert e.calibration_n >= PRIOR.min_calibration

    def test_probability_moves_with_landed_cost(self):
        """到手成本越低，P(公允價 > 到手成本) 必須越高。反過來就是模型接錯線。"""
        v = Valuator(self._many(), PRIOR)
        cheap = v.estimate(card_name="C1", rarity="ultra", grade=9.0, landed_twd=100)
        dear = v.estimate(card_name="C1", rarity="ultra", grade=9.0, landed_twd=50_000)
        assert cheap.p_worth_buying > dear.p_worth_buying
        assert 0.0 <= dear.p_worth_buying <= 1.0 <= cheap.p_worth_buying + 1e-9

    def test_min_calibration_threshold_is_configurable_and_enforced(self):
        strict = dataclasses.replace(PRIOR, min_calibration=10_000)
        v = Valuator(self._many(), strict)
        assert not v.calibrated
        assert not v.estimate(card_name="C1", rarity="ultra", grade=9.0).has_interval

    def test_empty_comps_does_not_crash(self):
        e = Valuator([], PRIOR).estimate(card_name="A", rarity="ultra", grade=9.0)
        assert e.fair_twd is None and e.level is None

    def test_split_is_deterministic(self):
        """同一份資料每次跑要切在同一刀上——今天有區間、明天沒有會讓人不再相信數字。"""
        rows = self._many()
        assert Valuator(rows, PRIOR).calibration_n == Valuator(rows, PRIOR).calibration_n


#: 平台先驗：定價賣場（mercari/paypay）比競價拍賣（yahoo）貴一倍。
VENUE_PRIOR = dataclasses.replace(
    PRIOR, venue_premium_prior={"yahoo": 1.0, "mercari": 2.0, "paypay": 2.0}
)


def _venue_rows(mercari_ratio: float = 3.0, n: int = 12) -> list[Obs]:
    """同一批卡在兩個平台各成交，Mercari 一律貴 `mercari_ratio` 倍。

    真實世界的 2 倍價差在這裡放大成 3 倍，是為了讓「有沒有校正」在數字上
    分得開——測的是機制對不對，不是倍率是多少。
    Yahoo 刻意多兩筆：參照平台是「樣本最多的那個」，樣本數打平時由字典序決定
    （mercari < yahoo → 會變成 mercari），測試不該建立在那個 tie-break 上。
    """
    rows: list[Obs] = []
    for i in range(n):
        base = 1000.0 * (1 + (i % 5) / 10)
        rows.append(Obs(base, f"C{i % 4}", "ultra", 9.0, f"y{i}", "yahoo", grader="PSA"))
        if i < n - 2:
            rows.append(
                Obs(base * mercari_ratio, f"C{i % 4}", "ultra", 9.0, f"m{i}", "mercari",
                    grader="PSA")
            )
    return rows


class TestVenueCoefficients:
    """平台效應：三個平台的成交價水準差 2 倍以上，混在同一個池子取中位數
    就是拿「混合市場」的數字去評估「某一個市場」的標的（工程原則 1）。"""

    def test_venue_multiplier_recovers_the_real_gap(self):
        """樣本夠多時，先驗被淹掉、係數收斂到資料說的真實倍率。"""
        m = ValuationModel(_venue_rows(mercari_ratio=3.0, n=200), VENUE_PRIOR)
        assert m.baseline_venue == "yahoo"          # 樣本最多的平台，係數恆為 0
        assert m.v("yahoo") == 0.0
        assert m.venue_multiplier("mercari") == pytest.approx(3.0, rel=0.05)
        assert m.venue_is_estimated("mercari")

    def test_moderate_sample_sits_between_prior_and_data(self):
        """n 不大不小時，係數落在先驗（2.0）與資料（3.0）之間——這就是收縮。"""
        m = ValuationModel(_venue_rows(mercari_ratio=3.0, n=12), VENUE_PRIOR)
        assert 2.0 < m.venue_multiplier("mercari") < 3.0

    def test_unknown_venue_gets_no_correction_rather_than_a_guess(self):
        """沒學過的平台係數是 0（＝不校正），不是拿別人的倍率來套。"""
        m = ValuationModel(_venue_rows(), VENUE_PRIOR)
        assert m.v("從沒見過的平台") == 0.0
        assert not m.venue_is_estimated("從沒見過的平台")

    def test_thin_venue_is_shrunk_toward_the_prior_not_the_raw_median(self):
        """**係數本身也要吃收縮**：3 筆樣本說 10 倍，不該直接推翻先驗的 2 倍。

        min_cell=3 剛好讓這個平台有一格可比分層（support=3），
        w = 3/(3+4) = 0.43 → 估計值只有不到一半的話語權。
        """
        rows = _venue_rows()
        rows += [
            Obs(10_000.0, "C0", "ultra", 9.0, f"p{i}", "paypay", grader="PSA")
            for i in range(3)
        ]
        m = ValuationModel(rows, VENUE_PRIOR)
        assert m.baseline_venue == "yahoo"
        raw = 10_000.0 / 1150.0                     # 本層原始比值（yahoo 該格中位 1150）
        prior = 2.0                                 # 先驗
        got = m.venue_multiplier("paypay")
        assert prior < got < raw, f"收縮後應落在先驗與原始值之間，實得 {got}"
        assert m.venue_weight["paypay"] == pytest.approx(3 / 7, rel=1e-6)
        assert not m.venue_is_estimated("paypay")   # w < 0.5 → 誠實標成「先驗為主」

    def test_no_venue_data_leaves_the_model_exactly_as_before(self):
        """樣本完全沒有平台資訊時，係數全空、預測與舊行為逐位元相同。"""
        m = ValuationModel(_rows(), PRIOR)
        assert m.venue_delta == {} and m.baseline_venue is None
        assert m.base == m.base_venue


class TestVenueTargeting:
    """估價**必須**指定目標平台，否則就是拿混合水準的數字去比單一市場。"""

    @pytest.fixture
    def v(self):
        return Valuator(_venue_rows(), VENUE_PRIOR)

    def test_different_target_venues_give_different_fair_values(self, v):
        """同一個標的換一個目標平台，公允價必須跟著平台倍率走——
        兩個平台拿到同一個數字，等於平台維度根本沒接上。"""
        cheap = v.estimate(card_name="C0", rarity="ultra", grade=9.0, venue="yahoo")
        dear = v.estimate(card_name="C0", rarity="ultra", grade=9.0, venue="mercari")
        assert dear.fair_twd > cheap.fair_twd
        # 比值就是平台係數本身（每一層都同樣換算，所以不管落在哪一層都成立）
        assert dear.fair_twd / cheap.fair_twd == pytest.approx(
            v.model.venue_multiplier("mercari"), rel=0.01
        )
        assert dear.fair_twd / cheap.fair_twd > 2.0

    def test_target_venue_is_reported_back(self, v):
        e = v.estimate(card_name="C0", rarity="ultra", grade=9.0, venue="mercari")
        assert e.venue == "mercari"
        assert e.venue_adjusted is True
        assert e.venue_is_estimated is True

    def test_unspecified_venue_is_flagged_not_silently_mixed(self, v):
        """未指定平台不是錯誤，但**必須外顯**——上層才知道自己拿到的是什麼。"""
        e = v.estimate(card_name="C0", rarity="ultra", grade=9.0)
        assert e.venue is None
        assert e.venue_adjusted is False
        assert e.venue_is_estimated is None
        assert any("未指定目標平台" in n for n in e.notes)

    def test_unadjusted_estimate_sits_between_the_two_venues(self, v):
        """沒校正時拿到的是混合水準：對 Yahoo 標的偏高、對 Mercari 標的偏低。
        這正是它危險的地方——兩邊都錯，而且錯的方向相反。"""
        mixed = v.estimate(card_name="C0", rarity="ultra", grade=9.0).fair_twd
        lo = v.estimate(card_name="C0", rarity="ultra", grade=9.0, venue="yahoo").fair_twd
        hi = v.estimate(card_name="C0", rarity="ultra", grade=9.0, venue="mercari").fair_twd
        assert lo < mixed < hi

    def test_prior_only_venue_says_so(self):
        """平台係數靠先驗時要標出來，不能讓它看起來跟估出來的一樣可信。"""
        rows = _venue_rows() + [
            Obs(9000.0, "C0", "ultra", 9.0, f"p{i}", "paypay", grader="PSA")
            for i in range(3)
        ]
        e = Valuator(rows, VENUE_PRIOR).estimate(
            card_name="C0", rarity="ultra", grade=9.0, venue="paypay"
        )
        assert e.venue_is_estimated is False
        assert any("以先驗為主" in n for n in e.notes)

    def test_interval_uses_the_matching_residual_set(self):
        """校正過的點估計要配校正過的殘差——否則區間寬度裡混進了平台價差，
        而那正是我們剛剛才扣掉的東西（同一件事不能算兩次）。"""
        rows = _venue_rows(n=60)
        v = Valuator(rows, VENUE_PRIOR)
        assert v.calibrated
        assert len(v.residuals) == len(v.residuals_venue)
        spread = lambda xs: max(xs) - min(xs)       # noqa: E731
        assert spread(v.residuals_venue) < spread(v.residuals)
        e = v.estimate(card_name="C0", rarity="ultra", grade=9.0, venue="mercari")
        wide = v.estimate(card_name="C0", rarity="ultra", grade=9.0)
        assert (e.hi_twd / e.lo_twd) < (wide.hi_twd / wide.lo_twd)


class TestTimeSplit:
    def test_split_is_stratified_by_venue_by_default(self):
        """全域切早晚會變成偽裝的平台切分（Buyee 的 sold_at 是入庫時間）。
        分層切分讓兩邊平台組成一致，時間才是唯一被切開的維度。"""
        rows = [
            Obs(1000.0, "C0", "ultra", 9.0, f"y{i}", "yahoo", sold_at=f"2026-01-{i+1:02d}")
            for i in range(20)
        ] + [
            Obs(3000.0, "C0", "ultra", 9.0, f"m{i}", "mercari", sold_at="2026-08-01")
            for i in range(20)
        ]
        train, test = _time_split(rows, 0.25, stratify_by_venue=True)
        assert {o.venue for o in test} == {"yahoo", "mercari"}
        naive_train, naive_test = _time_split(rows, 0.25, stratify_by_venue=False)
        assert {o.venue for o in naive_test} == {"mercari"}   # 全部被切成同一個平台

    def test_undated_rows_are_excluded_not_guessed(self):
        rows = [Obs(1000.0, "C0", "ultra", 9.0, f"k{i}") for i in range(40)]
        res = coverage_time_split(rows, PRIOR)
        assert res["n_undated"] == 40
        assert res["empirical"] is None and "不足以做時間切分" in res["note"]


class TestCoverageSelfCheck:
    def test_holdout_coverage_is_reported_and_roughly_nominal(self):
        """覆蓋率自檢本身要能跑，而且不能荒謬地偏離名目值。

        寬鬆的邊界是刻意的：這是合成資料的自檢，真實覆蓋率請看
        `ygo-sniper value --coverage` 印出來的實測數字。
        """
        rows = [
            Obs(1000.0 * math.exp((i % 11 - 5) / 5), f"C{i % 15}", "ultra", 9.0, f"k{i}")
            for i in range(160)
        ]
        res = coverage_check(rows, PRIOR, trials=30, seed=7)
        assert res["n_tested"] > 0
        assert 0.55 <= res["empirical"] <= 0.98
        assert res["nominal"] == 0.80


# ---------------------------------------------------------------------------
def _row(**over) -> dict:
    row = {
        "title": "【大人気/ARS9】ブラックマジシャンガール 初期 ウルトラ P4-01",
        "url": "https://buyee.jp/mercari/item/m48967074463",
        "landed_twd": 2055.0,
        "price_native": 8399.0,
        "currency": "JPY",
        "route": "buyee_consolidated",
        "comps_n": 5,
        "comps_median": 4669.0,
        "discount_pct": 0.56,
        "score": 34.0,
        "flags": json.dumps(["discount"]),
        "payload": json.dumps(
            {"comps": {"n": 5, "median_twd": 4669.0, "p25_twd": 1283.0, "p75_twd": 6090.0}}
        ),
    }
    row.update(over)
    return row


def _est(**over) -> Estimate:
    kw = dict(
        fair_twd=4496.0, level="L3", level_label="稀有度×分數", n_effective=28,
        lo_twd=2538.0, hi_twd=9094.0, confidence=0.80, calibration_n=68,
        p_worth_buying=0.87,
    )
    kw.update(over)
    return Estimate(**kw)


class TestFormatSignal:
    def test_new_valuation_lines(self):
        msg = format_signal(_row(), DASH, _est())
        assert "公允價 <b>NT$4,496</b>（L3 稀有度×分數，有效 n=28）" in msg
        assert "80% 區間 NT$2,538–9,094 ｜ P(值得買) 87%" in msg

    def test_listing_anchor_format_is_unchanged(self):
        """使用者明確要求過的格式。連結呈現方式不准跟著估價改版一起漂掉。"""
        msg = format_signal(_row(), DASH, _est())
        assert '<a href="https://buyee.jp/mercari/item/m48967074463">看標的</a>' in msg
        assert f'<a href="{DASH}">開 dashboard</a>' in msg

    def test_level_and_n_are_never_dropped(self):
        """點估計不准單獨出現：L0 的 4,496 與 L1 的 4,496 是完全不同的宣稱。"""
        msg = format_signal(_row(), DASH, _est(level="L0", level_label="全域先驗", n_effective=0))
        assert "L0 全域先驗，有效 n=0" in msg

    def test_no_interval_says_so_instead_of_faking_one(self):
        msg = format_signal(
            _row(), DASH, _est(lo_twd=None, hi_twd=None, p_worth_buying=None)
        )
        assert "樣本不足以校準" in msg
        assert "區間 NT$" not in msg

    def test_venue_is_shown_next_to_the_number(self):
        """平台必須跟公允價同一行：Yahoo 與 Mercari 的同款差 2 倍以上，
        一個沒標平台的數字會被拿去跟另一個市場比。"""
        msg = format_signal(_row(), DASH, _est(venue="buyee_mercari", venue_adjusted=True,
                                               venue_is_estimated=True))
        assert "Mercari（定價） 水準" in msg

    def test_unadjusted_valuation_is_flagged_in_the_message(self):
        msg = format_signal(_row(), DASH, _est())      # 預設沒指定平台
        assert "混合平台水準（未校正）" in msg

    def test_prior_only_venue_coefficient_is_disclosed(self):
        msg = format_signal(_row(), DASH, _est(venue="buyee_paypay", venue_adjusted=True,
                                               venue_is_estimated=False))
        assert "平台係數用先驗" in msg

    def test_no_samples_at_all(self):
        msg = format_signal(_row(), DASH, _est(fair_twd=None, level=None))
        assert "無足夠樣本" in msg
        assert "只根據到手成本" in msg

    def test_without_valuation_the_old_comps_format_survives(self):
        """估價還沒建起來時（缺主檔、行情庫空）要退回舊顯示，不可以整批發不出去。"""
        msg = format_signal(_row(), DASH)
        assert "行情 NT$4,669（n=5）" in msg
        assert "合理區間 NT$1,283–6,090（P25–P75）" in msg
        assert "公允價" not in msg


# ---------------------------------------------------------------------------
class TestGroupConditionalCalibration:
    """Mondrian（群組條件）conformal。

    vanilla split conformal 只保證**邊際**覆蓋率，症狀是全庫每一筆估計的區間
    寬度比例完全相同——一筆 L1 的估計與一筆 L3 的估計，下緣／點估計都是同一個
    數字。這一類守的是「區間寬度必須跟著證據品質走，而且退化要看得見」。

    ⚠️ 這裡**不**釘「低 n 的區間比較寬」：實測（見 valuation.py 頂註）
    n_effective 是「所用層級的池大小」，與誤差**反向**相關。把直覺釘成測試
    就等於把錯的方向鎖死。
    """

    def _mixed(self, n=400):
        """刻意做出兩種截然不同的族群：

        `T*` 是「同卡多筆、價格很集中」（撐得起 L1/L2，殘差小）；
        `W*` 是「每張卡只出現一次、價格散得很開」（只撐得起 L3，殘差大）。
        兩群的殘差分布不同，正是分群要抓出來的東西。
        """
        rows = []
        for i in range(n // 2):
            rows.append(Obs(1000.0 * (1 + (i % 3) / 50), f"T{i % 10}", "ultra", 9.0, f"t{i}"))
        for i in range(n // 2):
            rows.append(Obs(1000.0 * (1 + (i % 40) / 3), f"W{i}", "secret", 9.0, f"w{i}"))
        return rows

    def test_groups_are_built_per_level_and_bucket(self):
        v = Valuator(self._mixed(), dataclasses.replace(PRIOR, min_group_calibration=5))
        assert v.cal_model is not None
        # 至少要有「細群」與「合併層級」兩種鍵，合併階梯才有東西可退
        assert any("/" in k for k in v.residual_groups)
        assert any("/" not in k for k in v.residual_groups)
        # 每個細群的殘差都必須同時被算進它的層級鍵（階梯的第二階）
        for key, vals in v.residual_groups.items():
            if "/" in key:
                assert len(v.residual_groups[key.split("/")[0]]) >= len(vals)

    def test_different_groups_get_different_widths(self):
        """分群的全部意義：兩個證據品質不同的估計不該拿到同一個寬度比例。"""
        v = Valuator(self._mixed(), dataclasses.replace(PRIOR, min_group_calibration=5))
        tight = v.estimate(card_name="T1", rarity="ultra", grade=9.0)
        loose = v.estimate(card_name="W7", rarity="secret", grade=9.0)
        assert tight.has_interval and loose.has_interval
        assert tight.calibration_group != loose.calibration_group
        ratio = lambda e: (e.lo_twd / e.fair_twd, e.hi_twd / e.fair_twd)  # noqa: E731
        assert ratio(tight) != ratio(loose)

    def test_turning_grouping_off_restores_one_global_width(self):
        """`group_conformal: false` 必須真的退回 vanilla：全部同一個寬度比例。"""
        p = dataclasses.replace(PRIOR, group_conformal=False, min_group_calibration=5)
        v = Valuator(self._mixed(), p)
        a = v.estimate(card_name="T1", rarity="ultra", grade=9.0)
        b = v.estimate(card_name="W7", rarity="secret", grade=9.0)
        # 容差只給 `Estimate` 對台幣的整數捨入（fair≈1000、lo≈435 → 相對誤差 ~1e-3）。
        # 這不是「差不多就好」：分群真的沒關掉的話，兩者會差好幾成而不是千分之一。
        assert a.lo_twd / a.fair_twd == pytest.approx(b.lo_twd / b.fair_twd, rel=2e-3)
        assert a.calibration_group == b.calibration_group == "全域（未分群）"
        assert a.calibration_degraded and b.calibration_degraded

    def test_thin_group_degrades_and_says_so(self):
        """群樣本不足**不可以靜默**：要退化、要標記、要在 notes 講出來。"""
        # 門檻拉到天上 → 每一群都不夠 → 全部退到全域
        p = dataclasses.replace(PRIOR, min_group_calibration=10_000)
        e = Valuator(self._mixed(), p).estimate(card_name="T1", rarity="ultra", grade=9.0)
        assert e.has_interval
        assert e.calibration_degraded is True
        assert e.calibration_group == "全域（未分群）"
        assert e.calibration_group_requested and "/" in e.calibration_group_requested
        assert any("未經群組校準" in n for n in e.notes)

    def test_group_calibrated_estimate_is_not_marked_degraded(self):
        p = dataclasses.replace(PRIOR, min_group_calibration=5)
        e = Valuator(self._mixed(), p).estimate(card_name="T1", rarity="ultra", grade=9.0)
        assert e.calibration_group == e.calibration_group_requested
        assert e.calibration_degraded is False
        assert e.calibration_group_n >= 5

    def test_lower_bound_is_never_more_aggressive_than_vanilla(self):
        """**下緣安全夾**：分群後的下緣永遠 ≤ 不分群時的下緣。

        往寬的方向錯是輸掉競標（成本零），往窄的方向錯是真的付錢——
        這條不變式一旦被拿掉，分群就會變成「把上限一次調高五成」的機制。
        """
        rows = self._mixed()
        grouped = Valuator(rows, dataclasses.replace(PRIOR, min_group_calibration=5))
        vanilla = Valuator(rows, dataclasses.replace(PRIOR, group_conformal=False))
        for name, rarity in (("T1", "ultra"), ("T5", "ultra"), ("W7", "secret")):
            g = grouped.estimate(card_name=name, rarity=rarity, grade=9.0)
            v = vanilla.estimate(card_name=name, rarity=rarity, grade=9.0)
            assert g.lo_twd <= v.lo_twd + 1e-6, f"{name} 的下緣被分群抬高了（上限會變激進）"

    def test_floor_flag_is_set_when_the_clamp_bites(self):
        v = Valuator(self._mixed(), dataclasses.replace(PRIOR, min_group_calibration=5))
        flags = [
            v.estimate(card_name=n, rarity=r, grade=9.0).interval_floor_applied
            for n, r in (("T1", "ultra"), ("W7", "secret"))
        ]
        assert any(flags), "兩個群的下緣都沒被夾過，安全夾等於沒有在運作"

    def test_grade_is_echoed_back_on_the_estimate(self):
        """`grade` 必須跟著估價一起回去：出價那一側要看得見「分數是不是猜的」。"""
        v = Valuator(self._mixed(), dataclasses.replace(PRIOR, min_group_calibration=5))
        assert v.estimate(card_name="T1", rarity="ultra", grade=8.0).grade == 8.0
        assert v.estimate(card_name="T1", rarity="ultra", grade=None).grade is None
        assert Valuator([], PRIOR).estimate(rarity="ultra", grade=7.0).grade == 7.0

    def test_group_key_and_ladder_are_consistent(self):
        from ygo_sniper.valuation import group_key, group_ladder, n_bucket

        assert n_bucket(0) == "n<3" and n_bucket(2) == "n<3"
        assert n_bucket(3) == "3-9" and n_bucket(9) == "3-9"
        assert n_bucket(10) == "10-49" and n_bucket(49) == "10-49"
        assert n_bucket(50) == "n>=50" and n_bucket(9999) == "n>=50"
        assert group_key("L1", 1) == "L1/n<3"
        assert group_ladder("L1", 1) == ("L1/n<3", "L1")
        assert group_ladder(None, 99) == ("L0/n>=50", "L0")

    def test_conditional_coverage_reports_both_arms(self):
        """`coverage_by_group` 是這次修正的核心證據，它必須同時給兩隻手臂。"""
        from ygo_sniper.valuation import coverage_by_group

        rows = [
            dataclasses.replace(o, sold_at=f"2026-0{1 + i % 8}-01T00:00:00")
            for i, o in enumerate(self._mixed(200))
        ]
        res = coverage_by_group(
            rows, dataclasses.replace(PRIOR, min_group_calibration=5),
            test_fractions=(0.25,), venue_aware=False,
        )
        assert res["n_tested"] > 0 and res["buckets"]
        for b in res["buckets"]:
            for k in ("coverage_vanilla", "coverage_group",
                      "lower_tail_vanilla", "lower_tail_group"):
                assert 0.0 <= b[k] <= 1.0
        assert res["nominal_lower_tail"] == pytest.approx(0.10)

    def test_diagnose_explains_what_a_bucket_is_made_of(self):
        """診斷結論必須落在工具裡，不是落在一次性腳本裡。

        `10-49` 這一桶為什麼壞，是「要不要拒絕給上限」這個決定的唯一依據。
        答案只存在某人某天的 scratchpad 裡的話，下次資料變了就得重寫一份，
        而重寫的人不會知道上次看的是哪幾個維度。
        """
        from ygo_sniper.valuation import coverage_by_group

        rows = [
            dataclasses.replace(o, sold_at=f"2026-0{1 + i % 8}-01T00:00:00")
            for i, o in enumerate(self._mixed(200))
        ]
        params = dataclasses.replace(PRIOR, min_group_calibration=5)
        plain = coverage_by_group(rows, params, test_fractions=(0.25,), venue_aware=False)
        res = coverage_by_group(
            rows, params, test_fractions=(0.25,), venue_aware=False, diagnose=True,
        )
        # diagnose 只是**加印**，不准改動任何量測值
        assert [b["n_tested"] for b in res["buckets"]] == \
            [b["n_tested"] for b in plain["buckets"]]
        assert all("composition" not in b for b in plain["buckets"])

        comp = res["buckets"][0]["composition"]
        assert set(comp) >= {"level", "venue", "rarity", "grade", "slices"}
        assert sum(comp["level"].values()) == res["buckets"][0]["n_tested"]
        # 子切片按「最壞的排前面」——這才看得出整桶的違反率是不是某一格拖垮的
        tails = [s["lower_tail_group"] for s in comp["slices"]]
        assert tails == sorted(tails, reverse=True)
        for s in comp["slices"]:
            assert s["median_error_ratio"] > 0

    def test_calibration_group_is_shown_next_to_the_interval(self):
        """推播上也要看得見「這個區間是哪一群校準的」——分群後每筆寬度不同了。"""
        msg = format_signal(_row(), DASH, _est(
            lo_twd=1000.0, hi_twd=9000.0, calibration_group="L1/n<3",
            calibration_group_n=128,
        ))
        assert "校準群 L1/n<3／128 筆" in msg
        assert "退化" not in msg

    def test_degraded_calibration_is_flagged_in_the_message(self):
        msg = format_signal(_row(), DASH, _est(
            lo_twd=1000.0, hi_twd=9000.0, calibration_group="全域（未分群）",
            calibration_group_n=532, calibration_degraded=True,
        ))
        assert "退化：未經該群校準" in msg
