"""出價上限引擎測試。

這個檔案守的是一件事：**使用者會照著 `max_bid_jpy` 下真錢的單**。
所以最重要的不是「有沒有回傳數字」，而是兩條不變式：

  1. 反解與正算同源：算出來的上限拿去正算 `quote_route`，到手成本必須
     ≤ 保守公允價 ×(1-margin)。對不上就是「照著出價之後成本超過公允價」
     的無聲錯誤（工程原則 1）。
  2. 樣本不足時**不給上限**（紅線）：沒有 `lo_twd` 就回 ok=False、
     max_bid_jpy=None，絕不退化成用點估計猜一個數字。
"""

import dataclasses

import pytest
from conftest import make_listing

from ygo_sniper.bidding import (
    DEFAULT_TARGET_MARGIN,
    PROXY_BID_FINDING,
    BidCeiling,
    is_live_auction,
    max_bid_jpy,
    target_margin_from,
)
from ygo_sniper.costs import quote_route
from ygo_sniper.domain import Currency, Site
from ygo_sniper.valuation import Estimate


def est(lo=4000.0, hi=12000.0, fair=7000.0, **kw):
    """一份「校準過、有區間、四道閘門都過得了」的估價。

    `grade` / `calibration_group` / `calibration_group_n` / `level` 不是裝飾：
    它們正是 `EvidenceGate` 五道閘門各自要看的東西。少任何一個，這份估價就
    **不夠格**拿到上限——那正是本檔要守的紅線。所以每一項都可以用關鍵字覆寫，
    讓每條測試自己說清楚「我拿掉的是哪一項證據」。

    `n_effective` 刻意落在 `3-9` 桶：`10-49` 是實測校準壞掉、預設被
    `reject_n_buckets` 直接拒絕的那一桶，拿它當「全部閘門都過」的基準
    自相矛盾（2026-08-02）。
    """
    defaults = dict(
        fair_twd=fair, level="L1", level_label="同卡 × 同稀有度", n_effective=5,
        lo_twd=lo, hi_twd=hi, confidence=0.8, calibration_n=80,
        venue="buyee_yahoo", venue_adjusted=True, grade=10.0, grade_source="title",
        calibration_group="L1/3-9", calibration_group_requested="L1/3-9",
        calibration_group_n=71,
    )
    return Estimate(**{**defaults, **kw})


def auction(price, **kw):
    return make_listing(
        price=price, site=Site.BUYEE_YAHOO, currency=Currency.JPY,
        raw={"price_kind": "current_bid", "current_bid": price}, **kw,
    )


# ---------------------------------------------------------------------------
# 1. 核心保證：反解 → 正算 → 到手成本 ≤ 預算
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lo", [1500, 3000, 4500, 8000, 20000, 60000])
@pytest.mark.parametrize("margin", [0.0, 0.15, 0.30, 0.5])
def test_ceiling_round_trips_through_quote_route(cfg, fx, lo, margin):
    """**本模組的核心測試**：上限代回正算，到手成本不得超過預算。

    容差只給捨入誤差（`quote_route` 每個欄位捨到分）。這裡不是「差不多就好」
    ——超過容差代表反解與正算不同源，那正是「照著上限出價、實際成本卻超過
    公允價」的無聲錯誤。
    """
    c = max_bid_jpy(est(lo=lo), cfg, fx, site=Site.BUYEE_YAHOO, target_margin=margin)
    if not c.ok:
        # 預算被固定成本吃光是合法結果，但那時必須完全不給數字
        assert c.max_bid_jpy is None
        return

    budget = lo * (1 - margin)
    route = cfg.routes[c.route]
    probe = make_listing(price=c.max_bid_jpy, site=Site.BUYEE_YAHOO, currency=Currency.JPY)
    q = quote_route(probe, route, fx, bundle_size=c.bundle_size)

    assert q.landed_twd <= budget + 0.01, (
        f"上限 ¥{c.max_bid_jpy} 的到手成本 NT${q.landed_twd} 超過預算 NT${budget}"
    )
    # 引擎自己回報的正算結果必須跟外部重算的一致（不是另外算一份）
    assert c.landed_at_ceiling_twd == q.landed_twd
    assert c.budget_twd == pytest.approx(budget, abs=0.01)


def test_ceiling_is_tight_not_merely_safe(cfg, fx):
    """上限不能保守到沒有用：再多出 1 円就會超過預算，才叫「反解」。

    只驗「≤ 預算」的話，永遠回 ¥1 也會通過——那是一個安全但無用的引擎。
    """
    c = max_bid_jpy(est(lo=6000), cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.ok
    route = cfg.routes[c.route]
    over = make_listing(price=c.max_bid_jpy + 1, site=Site.BUYEE_YAHOO, currency=Currency.JPY)
    assert quote_route(over, route, fx, bundle_size=c.bundle_size).landed_twd > c.budget_twd


# ---------------------------------------------------------------------------
# 2. 紅線：沒有區間就不給上限
# ---------------------------------------------------------------------------
def test_no_interval_means_no_ceiling(cfg, fx):
    """校準集不足（lo_twd=None）→ **不准輸出上限**，也不准退回點估計。"""
    thin = Estimate(
        fair_twd=9000.0, level="L3", level_label="稀有度層", n_effective=2,
        lo_twd=None, hi_twd=None, calibration_n=3,
    )
    c = max_bid_jpy(thin, cfg, fx, site=Site.BUYEE_YAHOO)

    assert c.ok is False
    assert c.max_bid_jpy is None
    assert c.max_bid_twd is None
    assert "不提供出價上限" in c.reason
    # 點估計仍然帶回來給人看，但它**不是**上限
    assert c.fair_twd == 9000.0
    assert c.conservative_fair_twd is None


def test_no_estimate_at_all_means_no_ceiling(cfg, fx):
    c = max_bid_jpy(None, cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.ok is False and c.max_bid_jpy is None


def test_nonpositive_lo_means_no_ceiling(cfg, fx):
    c = max_bid_jpy(est(lo=0.0), cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.ok is False and c.max_bid_jpy is None


def test_budget_eaten_by_overhead_gives_no_number(cfg, fx):
    """便宜到雜費就吃光預算 → 不給上限（而不是給 0，那會被讀成「出價 0 元」）。"""
    c = max_bid_jpy(est(lo=120.0), cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.ok is False
    assert c.max_bid_jpy is None
    assert "固定成本" in c.reason


# ---------------------------------------------------------------------------
# 3. 目標利潤率
# ---------------------------------------------------------------------------
def test_target_margin_lowers_the_ceiling(cfg, fx):
    """利潤率越高，上限越低——而且是**嚴格**單調，不是「差不多」。"""
    zero = max_bid_jpy(est(), cfg, fx, site=Site.BUYEE_YAHOO, target_margin=0.0)
    mid = max_bid_jpy(est(), cfg, fx, site=Site.BUYEE_YAHOO, target_margin=0.30)
    high = max_bid_jpy(est(), cfg, fx, site=Site.BUYEE_YAHOO, target_margin=0.60)
    assert zero.max_bid_jpy > mid.max_bid_jpy > high.max_bid_jpy
    assert mid.target_margin == 0.30


def test_margin_comes_from_config_when_not_given(cfg, fx):
    from_cfg = max_bid_jpy(est(), cfg, fx, site=Site.BUYEE_YAHOO)
    explicit = max_bid_jpy(
        est(), cfg, fx, site=Site.BUYEE_YAHOO, target_margin=target_margin_from(cfg)
    )
    assert from_cfg.max_bid_jpy == explicit.max_bid_jpy


def test_illegal_margin_falls_back_to_default(cfg, capsys):
    bad = dataclasses.replace(cfg, bidding={"target_margin": 1.4})
    assert target_margin_from(bad) == DEFAULT_TARGET_MARGIN
    assert "target_margin" in capsys.readouterr().out

    nonsense = dataclasses.replace(cfg, bidding={"target_margin": "很多"})
    assert target_margin_from(nonsense) == DEFAULT_TARGET_MARGIN


def test_shipped_config_has_a_margin(cfg):
    """出貨設定必須自己講明利潤率，不靠程式碼的預設值。"""
    assert 0.0 <= float(cfg.bidding["target_margin"]) < 1.0


# ---------------------------------------------------------------------------
# 4. 上限用的是區間下緣，不是點估計
# ---------------------------------------------------------------------------
def test_ceiling_uses_the_interval_floor_not_the_point_estimate(cfg, fx):
    """點估計 ×3 但下緣不動 → 上限**完全不動**。

    這是整個設計最重要的一條：拿點估計當上限，等於有一半機率出價過高
    （模型點估計的中位誤差 ×1.9）。
    """
    same_lo = max_bid_jpy(est(lo=4000, fair=7000), cfg, fx, site=Site.BUYEE_YAHOO)
    huge_point = max_bid_jpy(est(lo=4000, fair=21000), cfg, fx, site=Site.BUYEE_YAHOO)
    assert same_lo.max_bid_jpy == huge_point.max_bid_jpy
    assert same_lo.conservative_fair_twd == 4000


# ---------------------------------------------------------------------------
# 5. route 選擇與自我解釋
# ---------------------------------------------------------------------------
def test_picks_the_route_that_allows_the_highest_bid(cfg, fx):
    """挑 overhead 最低那條——而它同時就是 `costs.best_route` 會挑的那條。"""
    from ygo_sniper.costs import best_route

    c = max_bid_jpy(est(lo=9000), cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.ok
    probe = make_listing(price=c.max_bid_jpy, site=Site.BUYEE_YAHOO, currency=Currency.JPY)
    assert best_route(probe, cfg, fx).route == c.route


def test_ceiling_explains_itself(cfg, fx):
    c = max_bid_jpy(est(lo=9000), cfg, fx, site=Site.BUYEE_YAHOO)
    d = c.to_dict()
    for k in ("conservative_fair_twd", "target_margin", "budget_twd", "max_bid_jpy",
              "max_bid_twd", "landed_at_ceiling_twd", "route", "route_label",
              "overhead_twd", "fee_twd", "shipping_twd", "bundle_size"):
        assert d[k] is not None, f"{k} 沒有值，這個結構就解釋不了自己"
    assert c.notes and any("80% 區間下緣" in n for n in c.notes)


def test_ebay_is_refused_not_silently_wrong(cfg, fx):
    """eBay 的運費出自 listing，不走 route 費率表——拒絕比算錯好。"""
    c = max_bid_jpy(est(), cfg, fx, site=Site.EBAY)
    assert c.ok is False and c.max_bid_jpy is None


def test_no_route_for_site_is_refused(cfg, fx):
    empty = dataclasses.replace(cfg, routes={})
    c = max_bid_jpy(est(), empty, fx, site=Site.BUYEE_YAHOO)
    assert c.ok is False and c.max_bid_jpy is None


# ---------------------------------------------------------------------------
# 6. headroom：目前出價 vs 上限
# ---------------------------------------------------------------------------
def test_headroom_and_actionability(cfg, fx):
    c = max_bid_jpy(est(lo=9000), cfg, fx, site=Site.BUYEE_YAHOO)
    ceiling = c.max_bid_jpy

    assert c.headroom_jpy(ceiling - 500) == 500
    assert c.is_actionable(ceiling - 500) is True
    # 剛好等於上限 = 沒有空間（Buyee 有最小加價幅度，連加都加不上去）
    assert c.is_actionable(ceiling) is False
    assert c.is_actionable(ceiling + 1) is False
    assert c.headroom_pct(ceiling / 2) == pytest.approx(0.5, abs=0.01)


def test_headroom_is_none_without_a_ceiling():
    none = BidCeiling(ok=False, reason="樣本不足", site="buyee_yahoo")
    assert none.headroom_jpy(1000) is None
    assert none.headroom_pct(1000) is None
    assert none.is_actionable(1) is False


def test_headroom_is_none_when_current_bid_unknown(cfg, fx):
    """沒有目前出價就不猜 0——0 會讓「未知」看起來像「整個上限都還在」。"""
    c = max_bid_jpy(est(), cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.headroom_jpy(None) is None
    assert c.is_actionable(None) is False


# ---------------------------------------------------------------------------
# 7. 競標判別（唯一定義）
# ---------------------------------------------------------------------------
def test_is_live_auction_reads_price_kind():
    assert is_live_auction(auction(3000)) is True
    assert is_live_auction(make_listing(price=3000, raw={"price_kind": "buyout"})) is False
    assert is_live_auction(make_listing(price=3000, raw={"price_kind": "fixed"})) is False
    assert is_live_auction(make_listing(price=3000)) is False  # 沒有 raw = 定價


# ---------------------------------------------------------------------------
# 8. 代理出價查證：結論必須帶證據，不能只是一個 True
# ---------------------------------------------------------------------------
def test_proxy_bid_finding_carries_evidence():
    assert PROXY_BID_FINDING["supported"] is True
    assert PROXY_BID_FINDING["sources"], "查證結論必須附來源 URL"
    assert all(u.startswith("https://buyee.jp/") for u in PROXY_BID_FINDING["sources"])
    assert PROXY_BID_FINDING["checked_at"]


# ---------------------------------------------------------------------------
# 6. 證據閘門：不夠格就不給上限（紅線的延伸）
# ---------------------------------------------------------------------------
from ygo_sniper.bidding import (  # noqa: E402
    CARD_SPECIFIC_LEVELS,
    EVIDENCE_TIERS,
    EvidenceGate,
    evidence_label,
    evidence_tier,
    recompute_ceilings,
)


def test_unknown_grade_gets_no_ceiling(cfg, fx):
    """分數未知時模型會**當成基準分數 9**——那是一個沒說出口的假設。

    分數溢價從 7 分的 ×0.35 到 10 分的 ×3.95 橫跨 11 倍，猜錯的方向正好是
    「公允價被高估、上限開太高」。實測分數未知的估計中位誤差 ×7.50、
    區間覆蓋率只有 25%。這一筆必須完全沒有數字。
    """
    c = max_bid_jpy(est(grade=None), cfg, fx, site=Site.BUYEE_YAHOO)
    assert not c.ok and c.max_bid_jpy is None
    assert "抽不到鑑定分數" in c.reason
    assert "缺的是" in c.reason, "擋下來時要說還缺什麼，不是只說不合格"


def test_no_card_specific_evidence_gets_no_ceiling(cfg, fx):
    """L3 = 模型根本不知道這是哪張卡，公允價其實是「這種稀有度的典型價」。"""
    c = max_bid_jpy(
        est(level="L3", level_label="稀有度×分數", n_effective=325),
        cfg, fx, site=Site.BUYEE_YAHOO,
    )
    assert not c.ok and c.max_bid_jpy is None
    assert "L3" in c.reason and "這張卡" in c.reason


@pytest.mark.parametrize("level", CARD_SPECIFIC_LEVELS)
def test_card_specific_levels_pass_the_gate(cfg, fx, level):
    assert max_bid_jpy(est(level=level), cfg, fx, site=Site.BUYEE_YAHOO).ok


def test_low_n_is_not_by_itself_a_reason_to_refuse(cfg, fx):
    """**實測反直覺**：n_effective 是「所用層級的池大小」，與誤差反向相關。

    n=1 的 L1（找到同一張卡的成交）是全部分桶裡下尾違反率最低的一群（2%）。
    把它擋掉等於砍掉最好的估計、留下最差的——這條測試就是防止有人
    「照直覺」把 min_effective_samples 調高。
    """
    c = max_bid_jpy(est(level="L1", n_effective=1), cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.ok and c.max_bid_jpy is not None


def test_min_effective_samples_is_enforced_when_raised(cfg, fx):
    gate = EvidenceGate(min_effective_samples=5)
    c = max_bid_jpy(est(n_effective=2), cfg, fx, site=Site.BUYEE_YAHOO, gate=gate)
    assert not c.ok and "只有 2 筆成交" in c.reason


def test_thin_group_calibration_gets_no_ceiling(cfg, fx):
    """撐著這個區間的是**那一群**的殘差，不是全庫的。

    一個 30 筆的群算出來的區間，不能拿「全庫 532 筆」去背書（工程原則 1）。
    """
    c = max_bid_jpy(
        est(calibration_group="L1/10-49", calibration_group_n=31, calibration_n=532),
        cfg, fx, site=Site.BUYEE_YAHOO, gate=EvidenceGate(min_calibration_samples=50),
    )
    assert not c.ok and "校準殘差只有 31 筆" in c.reason


def test_calibration_backing_falls_back_to_the_global_count(cfg, fx):
    """沒有群組校準時退回全域校準集大小——不可以當成 0 而誤擋。"""
    c = max_bid_jpy(
        est(calibration_group=None, calibration_group_n=0, calibration_n=80),
        cfg, fx, site=Site.BUYEE_YAHOO,
    )
    assert c.ok


def test_gates_can_be_turned_off_but_default_is_closed(cfg):
    """預設**全部關閉放行**（fail closed）：新欄位沒填時不該悄悄給出上限。"""
    g = EvidenceGate()
    assert g.require_known_grade and g.require_card_specific_level
    assert g.min_calibration_samples == 50 and g.min_effective_samples == 1
    # config 讀出來的那份必須跟出貨設定一致
    shipped = EvidenceGate.from_config(cfg)
    assert shipped.require_known_grade and shipped.require_card_specific_level


def test_illegal_gate_values_fall_back_loudly(capsys):
    class FakeCfg:
        bidding = {"min_calibration_samples": "六十", "min_effective_samples": -3}

    g = EvidenceGate.from_config(FakeCfg())
    assert g.min_calibration_samples == 50 and g.min_effective_samples == 1
    out = capsys.readouterr().out
    assert "min_calibration_samples" in out and "min_effective_samples" in out


def test_ceiling_carries_its_evidence_to_the_ui(cfg, fx):
    """UI 要能一眼看出「這個數字背後有幾筆成交撐著」，所以來歷必須跟著數字走。"""
    c = max_bid_jpy(est(), cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.ok
    assert c.level == "L1"
    assert c.calibration_group == "L1/3-9" and c.calibration_group_n == 71
    assert c.evidence_tier in EVIDENCE_TIERS
    assert c.evidence_tier_label == EVIDENCE_TIERS[c.evidence_tier]
    assert "L1" in c.evidence_label and "71" in c.evidence_label


def test_evidence_tier_tracks_the_measured_failure_modes():
    assert evidence_tier(est()) == "strong"
    assert evidence_tier(  # 落在已知破口 10-49 桶
        est(calibration_group="L1/10-49", calibration_group_requested="L1/10-49")
    ) == "moderate"
    assert evidence_tier(est(calibration_group="L1/n<3",
                            calibration_group_requested="L1/n<3")) == "strong"
    assert evidence_tier(est(calibration_degraded=True)) == "moderate"
    assert evidence_tier(est(level="L3")) == "weak"


def test_mismatched_calibration_level_is_not_called_strong():
    """點估計是 L1、區間卻由 L3 那一群校準——那個區間偏寬（安全），但
    「由自己那一群校準出來」這句話不成立，所以不准掛 strong。

    這不是吹毛求疵：校準模型只看 fit 半邊，少四成樣本就可能找不到這張卡而
    退一層，實測 8 筆有上限的標的裡有 3 筆是這種情形。
    """
    mismatched = est(level="L1", calibration_group="L3/n>=50",
                     calibration_group_requested="L3/n>=50")
    assert evidence_tier(mismatched) == "moderate"


def test_degraded_calibration_is_visible_not_silent(cfg, fx):
    c = max_bid_jpy(est(calibration_degraded=True), cfg, fx, site=Site.BUYEE_YAHOO)
    assert c.ok and c.calibration_degraded is True
    assert "退化" in c.evidence_label


def test_failed_gate_still_reports_the_evidence(cfg, fx):
    """`ok=False` 的那些列也要帶著證據欄位——「為什麼沒有上限」同樣需要交代。"""
    c = max_bid_jpy(est(level="L3"), cfg, fx, site=Site.BUYEE_YAHOO)
    assert not c.ok
    assert c.level == "L3" and c.evidence_tier == "weak"
    assert c.n_effective == 5


# ---------------------------------------------------------------------------
# 7. 既有資料的重算
# ---------------------------------------------------------------------------
class _FakeComps:
    def stats_for(self, listing, info):
        from ygo_sniper.domain import CompStats

        return CompStats(n=0, median_twd=None, p25_twd=None, p40_twd=None,
                         p75_twd=None, window_days=90)


class _FakeValuator:
    def __init__(self, estimate):
        self._est = estimate
        self.index = None

    def estimate(self, **kw):
        return self._est


def _signal_row(**bid):
    import json

    return {
        "key": "buyee_yahoo:n1",
        "payload": json.dumps({
            "listing": {
                "site": "buyee_yahoo", "external_id": "n1", "title": "遊戯王 初期 テスト",
                "url": "https://buyee.jp/item/yahoo/auction/n1", "price": 1000.0,
                "currency": "JPY", "raw": {"price_kind": "current_bid"}, "bids": 3,
            },
            "card": {"grader": "PSA", "grade": 10.0, "in_era": True,
                     "era_evidence": ["jp_kw:初期"], "rarity": "ultra"},
            "bid": bid,
        }),
    }


def test_recompute_pulls_a_ceiling_that_no_longer_qualifies(cfg, fx):
    """舊方法給過上限、新閘門擋下來 → 重算必須把它撤掉，而且說得出原因。"""
    rows = [_signal_row(ok=True, max_bid_jpy=7383.0, conservative_fair_twd=2631.0)]
    changes = recompute_ceilings(
        rows, cfg, fx, comps_engine=_FakeComps(),
        valuator=_FakeValuator(est(level="L3", n_effective=325)),
    )
    assert len(changes) == 1
    c = changes[0]
    assert c.before_ok and not c.after_ok
    assert c.before_jpy == 7383.0 and c.after_jpy is None
    assert "證據不足" in c.reason
    assert c.sort_weight == float("inf"), "撤掉上限必須排在變動最大的位置"


def test_recompute_is_a_dry_run_unless_asked(cfg, fx):
    """沒給 store 就不准寫任何東西——重算的預設必須是「只看不動」。"""
    rows = [_signal_row(ok=True, max_bid_jpy=7383.0)]

    class RecordingStore:
        def __init__(self):
            self.written = []

        def upsert_signal(self, sig):
            self.written.append(sig)

    store = RecordingStore()
    changes = recompute_ceilings(
        rows, cfg, fx, comps_engine=_FakeComps(), valuator=_FakeValuator(est())
    )
    assert changes, "dry-run 仍然要算出對照表，只是不寫"
    assert store.written == [], "沒傳 apply_to 卻寫了東西"

    recompute_ceilings(rows, cfg, fx, comps_engine=_FakeComps(),
                       valuator=_FakeValuator(est()), apply_to=store)
    assert len(store.written) == 1, "傳了 apply_to 就必須真的寫回"


def test_recompute_is_idempotent(cfg, fx):
    """同一份資料重跑第二次不該再有變動——不然使用者永遠不知道哪一版才算數。"""
    import json

    rows = [_signal_row(ok=True, max_bid_jpy=7383.0, conservative_fair_twd=2631.0)]
    valuator = _FakeValuator(est())
    first = recompute_ceilings(rows, cfg, fx, comps_engine=_FakeComps(), valuator=valuator)

    # 把第一次的結果寫回 payload，模擬 --apply 之後的 db 狀態
    payload = json.loads(rows[0]["payload"])
    payload["bid"] = {"ok": first[0].after_ok, "max_bid_jpy": first[0].after_jpy,
                      "conservative_fair_twd": first[0].after_lo_twd}
    rows[0]["payload"] = json.dumps(payload)

    second = recompute_ceilings(rows, cfg, fx, comps_engine=_FakeComps(), valuator=valuator)
    assert second[0].before_jpy == second[0].after_jpy
    assert second[0].delta_jpy == 0
    assert second[0].before_ok == second[0].after_ok


def test_recompute_skips_fixed_price_listings(cfg, fx):
    """定價標的沒有出價上限這回事，重算不該碰它們。"""
    import json

    row = _signal_row(ok=True, max_bid_jpy=1.0)
    payload = json.loads(row["payload"])
    payload["listing"]["raw"] = {"price_kind": "buyout"}
    row["payload"] = json.dumps(payload)
    assert recompute_ceilings(
        [row], cfg, fx, comps_engine=_FakeComps(), valuator=_FakeValuator(est())
    ) == []


def test_recompute_survives_a_broken_payload(cfg, fx):
    """壞掉的 payload 不該讓整批重算炸掉——跳過它，其他的照跑。"""
    good = _signal_row(ok=True, max_bid_jpy=7383.0)
    bad = {"key": "x", "payload": "{not json"}
    empty = {"key": "y", "payload": "{}"}
    changes = recompute_ceilings(
        [bad, empty, good], cfg, fx, comps_engine=_FakeComps(),
        valuator=_FakeValuator(est()),
    )
    assert len(changes) == 1 and changes[0].key == "buyee_yahoo:n1"


# ---------------------------------------------------------------------------
# 回歸：payload → Listing 的還原不可以掉欄位
# ---------------------------------------------------------------------------
def test_listing_from_payload_preserves_every_field():
    """事故 2026-08-02：`recalc-bids --apply` 把每一筆競標的 end_time 洗成 None。

    原因是重建 Listing 時手工列舉欄位，列了 bids 卻漏了 end_time——而漏掉的欄位
    有預設值，所以**沒有任何錯誤**，競標的倒數與排序就這樣安靜地壞掉。

    這條測試釘的是「還原後每個欄位都等於原值」，而不是列舉特定欄位——
    否則它自己也會跟著 dataclass 漂移，變成同一種 bug 的幫兇。
    """
    import dataclasses
    import json
    from datetime import UTC, datetime

    from ygo_sniper.bidding import listing_from_payload
    from ygo_sniper.domain import Currency, Listing, Site

    original = Listing(
        site=Site.BUYEE_YAHOO,
        external_id="v123456789",
        title="遊戯王 初期 PSA9",
        url="https://buyee.jp/item/yahoo/auction/v123456789",
        price=1234.0,
        currency=Currency.JPY,
        image_url="https://example.test/a.jpg",
        seller_id="seller1",
        shipping_cost=500.0,
        ships_to_tw=True,
        best_offer_enabled=True,
        listed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        is_sold=False,
        raw={"price_kind": "current_bid"},
        source="yahoo_direct",
        origin_url="https://auctions.yahoo.co.jp/jp/auction/v123456789",
        end_time=datetime(2026, 8, 7, 19, 25, 13, tzinfo=UTC),
        bids=3,
    )
    # 走一趟真實的序列化路徑（payload 存的就是 asdict + default=str）
    round_tripped = listing_from_payload(
        json.loads(json.dumps(dataclasses.asdict(original), default=str))
    )

    for f in dataclasses.fields(Listing):
        assert getattr(round_tripped, f.name) == getattr(original, f.name), (
            f"欄位 {f.name} 在 payload 還原後遺失或改變——"
            "新增 Listing 欄位時不需要改這條測試，但必須確保還原邏輯是反射式的"
        )


# ---------------------------------------------------------------------------
# 8. 校準已知壞掉的分桶：直接不給上限（reject_n_buckets）
#
# 依據見 bidding.py 頂註第 2 道閘門：`10-49` 桶實測下尾違反率 29-35%（名目 10%），
# 六種分群鍵全部修不好。「下尾違反」＝真實成交價低於區間下緣，也就是
# **上限開得比市場還高**——會付錢的那一邊。所以這一桶改成拒絕輸出。
# ---------------------------------------------------------------------------
def test_the_broken_bucket_gets_no_ceiling_by_default(cfg, fx):
    c = max_bid_jpy(est(n_effective=12), cfg, fx, site=Site.BUYEE_YAHOO)
    assert not c.ok and c.max_bid_jpy is None
    assert "10-49" in c.reason and "校準已知壞掉" in c.reason


def test_neighbouring_buckets_are_untouched(cfg, fx):
    """只擋那一桶。把相鄰的桶一起擋掉是「安全過頭」——實測 3-9 桶下尾只有 3%。"""
    for n in (2, 9, 50, 325):
        c = max_bid_jpy(est(n_effective=n, level="L1"), cfg, fx, site=Site.BUYEE_YAHOO)
        assert c.ok, f"n_effective={n} 不在破口桶裡，不該被擋"


def test_the_requested_calibration_group_also_triggers_it(cfg, fx):
    """兩個地方都要看：使用者看到的 `n_effective` 桶，與校準模型判定的那個桶。

    兩者可能不同（校準模型少看四成樣本）。取聯集是刻意往保守的方向——
    少擋一筆的代價是真的付錢，多擋一筆只是少一次出價機會。
    """
    c = max_bid_jpy(
        est(n_effective=5, calibration_group_requested="L1/10-49"),
        cfg, fx, site=Site.BUYEE_YAHOO,
    )
    assert not c.ok and "10-49" in c.reason


def test_the_gate_can_be_turned_off_explicitly(cfg, fx):
    """設成空 tuple ＝「我知道風險、我要關掉」。關掉之後那一桶又拿得到上限。"""
    c = max_bid_jpy(
        est(n_effective=12), cfg, fx, site=Site.BUYEE_YAHOO,
        gate=EvidenceGate(reject_n_buckets=()),
    )
    assert c.ok


def test_config_bucket_names_are_validated_loudly(capsys):
    """打錯桶名會讓這道閘門安靜地變成空門——所以錯的一律警告＋忽略。"""
    class FakeCfg:
        bidding = {"reject_n_buckets": ["10-49", "n=42"]}

    g = EvidenceGate.from_config(FakeCfg())
    assert g.reject_n_buckets == ("10-49",)
    assert "n=42" in capsys.readouterr().out


def test_shipped_config_actually_closes_the_broken_bucket(cfg):
    """出貨的 settings.yaml 必須真的擋著那一桶（不是只有程式預設擋）。"""
    assert "10-49" in EvidenceGate.from_config(cfg).reject_n_buckets


# ---------------------------------------------------------------------------
# 8b. 檔位（profile）：出價檔 vs 通知檔
#
# 通知與出價的錯誤代價不對稱：通知錯了使用者看一眼就知道，出價上限錯了會
# 花錯真錢。所以通知檔只放寬「校準政策」那兩道（破口桶、校準殘差門檻），
# 語意閘門（分數已知、L1/L2）兩檔一樣硬。**bidding 檔是紅線：行為一項都不准變。**
# ---------------------------------------------------------------------------
from ygo_sniper.bidding import (  # noqa: E402
    DEFAULT_NOTIFY_MIN_CALIBRATION_SAMPLES,
    PROFILE_BIDDING,
    PROFILE_NOTIFY,
    bidding_gate_note,
)


def test_bidding_profile_is_the_default_and_unchanged(cfg):
    """回歸釘子 1：不指定檔位 ＝ bidding 檔，且每一項門檻與改動前一模一樣。"""
    default = EvidenceGate.from_config(cfg)
    explicit = EvidenceGate.from_config(cfg, profile=PROFILE_BIDDING)
    assert default == explicit
    assert default.require_known_grade is True
    assert default.require_card_specific_level is True
    assert default.min_effective_samples == 1
    assert default.min_calibration_samples == 50
    assert default.reject_n_buckets == ("10-49",)


def test_bidding_path_still_rejects_broken_bucket_and_thin_backing(cfg, fx):
    """回歸釘子 2：**出價路徑**（max_bid_jpy）對兩種校準政策失格的行為不變。

    落在 `10-49` 桶 → 仍拒絕；校準殘差 30（< 50）→ 仍拒絕。
    這正是通知檔放行的那兩種輸入——出價那一側一步都不能跟著鬆。
    """
    c = max_bid_jpy(est(n_effective=12), cfg, fx, site=Site.BUYEE_YAHOO)
    assert not c.ok and c.max_bid_jpy is None
    assert "10-49" in c.reason and "校準已知壞掉" in c.reason

    c2 = max_bid_jpy(est(calibration_group_n=30), cfg, fx, site=Site.BUYEE_YAHOO)
    assert not c2.ok and c2.max_bid_jpy is None
    assert "校準殘差只有 30 筆" in c2.reason


def test_notify_profile_relaxes_only_the_calibration_policy(cfg):
    """通知檔與出價檔的差集必須**恰好**是那兩道校準政策，一項不多。"""
    notify = EvidenceGate.from_config(cfg, profile=PROFILE_NOTIFY)
    bid = EvidenceGate.from_config(cfg)
    # 語意閘門：兩檔一樣硬
    assert notify.require_known_grade == bid.require_known_grade is True
    assert notify.require_card_specific_level == bid.require_card_specific_level is True
    assert notify.min_effective_samples == bid.min_effective_samples
    # 校準政策：通知檔放寬
    assert notify.reject_n_buckets == ()
    assert notify.min_calibration_samples == DEFAULT_NOTIFY_MIN_CALIBRATION_SAMPLES == 30


def test_notify_profile_passes_what_bidding_rejects_on_calibration_policy(cfg):
    notify = EvidenceGate.from_config(cfg, profile=PROFILE_NOTIFY)
    bid = EvidenceGate.from_config(cfg)
    broken = est(n_effective=12)               # 10-49 破口桶
    thin = est(calibration_group_n=30)         # 殘差 30 < 出價門檻 50
    assert bid.check(broken) is not None and notify.check(broken) is None
    assert bid.check(thin) is not None and notify.check(thin) is None


def test_notify_profile_keeps_the_semantic_gates_hard(cfg):
    """分數未知、L3——估不出分數或連卡都認不出來的，通知檔也不給數字。"""
    notify = EvidenceGate.from_config(cfg, profile=PROFILE_NOTIFY)
    assert notify.check(est(grade=None)) is not None
    assert notify.check(est(level="L3", level_label="稀有度層", n_effective=325)) is not None
    # 30 是門檻不是擺設：殘差 29 仍然擋
    assert notify.check(est(calibration_group_n=29)) is not None


def test_bidding_gate_note_names_the_reason_briefly(cfg):
    """「放寬的必須看得見」的原料：一句短話講出出價檔為什麼會拒。"""
    bid = EvidenceGate.from_config(cfg)
    assert bidding_gate_note(bid, est()) is None            # 出價檔也收 → 不標
    note = bidding_gate_note(bid, est(n_effective=12))
    assert note is not None and "10-49" in note and "拒絕桶" in note
    note2 = bidding_gate_note(bid, est(calibration_group_n=30))
    assert note2 is not None and "30" in note2 and "50" in note2


def test_unknown_profile_falls_back_to_bidding_loudly(cfg, capsys):
    """打錯檔位名必須退回**最嚴**的 bidding 檔並大聲警告——fail closed。"""
    g = EvidenceGate.from_config(cfg, profile="yolo")
    assert g == EvidenceGate.from_config(cfg)
    assert "yolo" in capsys.readouterr().out


def test_notify_profile_reads_and_validates_its_own_config_keys(capsys):
    """notify 檔的兩個參數來自 `notify.evidence_gate`，非法值大聲退回 notify 預設。"""
    class FakeCfg:
        bidding = {}
        notify = {"evidence_gate": {
            "min_calibration_samples": "卅",
            "reject_n_buckets": ["10-49", "bogus"],
        }}

    g = EvidenceGate.from_config(FakeCfg(), profile=PROFILE_NOTIFY)
    assert g.min_calibration_samples == DEFAULT_NOTIFY_MIN_CALIBRATION_SAMPLES
    assert g.reject_n_buckets == ("10-49",)     # 合法桶保留、亂寫的丟掉
    out = capsys.readouterr().out
    assert "notify.evidence_gate" in out and "bogus" in out


def test_notify_config_keys_do_not_leak_into_the_bidding_profile():
    """`notify.evidence_gate` 就算設得再鬆，也**碰不到** bidding 檔（紅線）。"""
    class FakeCfg:
        bidding = {}
        notify = {"evidence_gate": {
            "min_calibration_samples": 0, "reject_n_buckets": [],
        }}

    g = EvidenceGate.from_config(FakeCfg())
    assert g.min_calibration_samples == 50
    assert g.reject_n_buckets == ("10-49",)


def test_a_description_sourced_grade_cannot_be_called_strong():
    """分數從商品描述撈來的 → 證據等級降到 moderate。

    同一個數字，來源不同，可信度就不同：描述是賣家自由文字，尾巴還常有
    SEO 關鍵字堆（見 parsers/grade.resolve_grade）。UI 不可以把它畫得跟
    標題寫的分數一樣。
    """
    assert evidence_tier(est(grade_source="title")) == "strong"
    assert evidence_tier(est(grade_source="description")) == "moderate"
    label = evidence_label(est(grade_source="description"))
    assert "商品描述" in label
