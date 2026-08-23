"""規則 5（高價帶折價）與 band 閘門。

band='high' 的 signal 只有規則 4（狙擊）與規則 5 有資格評估；規則 1/2/3
一律顯式跳過。band 缺失或 'std' 時完全不影響既有三條規則——這一份是新檔，
`tests/test_notify_rules.py` 全部零改動、全綠就是那條紅線的證據，不在這裡
重複斷言。

修正回合 Task 9（2026-08-22）：閘門與分母改成同一個 `Estimate` 物件
（`estimate.fair_twd`／`estimate.n_effective`），`comps_median`／
`discount_pct` 不再進規則 5 的任何判定或文案（那個池子來自
`comps.stats_for`，不分平台、不分 sale_kind，且會混機構混分數——見
`notify_rules._match_high_band` 的 docstring）。`hb_row` 仍然把
`comps_median`／`discount_pct` 寫進 row（真實 signals 列也有這兩欄，
給其他讀者用），但規則 5 的判準與文案刻意不讀它們，且有專門的紅燈測試
證明改寫 `comps_median` 不影響命中與文案數字。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ygo_sniper.card_snipe import build_notify_context
from ygo_sniper.notify_rules import (
    DEFAULT_HIGH_BAND_MAX_PRICE_RATIO,
    HIGH_BAND_BADGE,
    HIGH_BAND_DEEP_DISCOUNT_WARNING,
    RULE_CARD_SNIPE,
    RULE_HIGH_BAND,
    NotifyRules,
    evaluate,
)
from ygo_sniper.seller_alpha import (
    BASIS_ASK,
    TIER_LABEL,
    TIER_STRICT,
    MarketRow,
    PeerMatch,
    SellerItem,
)
from ygo_sniper.seller_watch import SellerNotifyContext
from ygo_sniper.store import Store

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
RULES = NotifyRules()


# ---------------------------------------------------------------------------
# 估價模型打樁（同 tests/test_notify_rules.py 的作法：測的是規則，不是模型）
# ---------------------------------------------------------------------------
#: 預設公允價——與 `hb_row` 的 `ratio` 相乘反推 `landed_twd`，讓 `price_ratio`
#: 精確等於呼叫端指定的 `ratio`（Task 9：ratio 現在讀 `landed_twd / estimate.fair_twd`，
#: 不再讀 `comps_median`／`discount_pct`）。
FAIR_TWD = 9000.0


class FakeEstimate:
    def __init__(
        self, level: str = "L1", fair_twd: float | None = FAIR_TWD,
        n_effective: int = 3,
    ):
        self.level = level
        self.level_label = "卡名×稀有度×分數" if level in ("L1", "L2") else "稀有度×分數"
        self.fair_twd = fair_twd
        self.lo_twd = 3000.0
        self.hi_twd = 12000.0
        self.n_effective = n_effective
        self.p_worth_buying = 0.9

    @property
    def has_card_specific_evidence(self) -> bool:
        return self.level in ("L1", "L2")

    @property
    def has_interval(self) -> bool:
        return self.lo_twd is not None and self.hi_twd is not None


class FakeValuator:
    """打樁對象的存在證明，兩支估價函式都被 monkeypatch 掉。"""


VALUATOR = FakeValuator()
#: 每個 signal key 可覆寫的 `FakeEstimate` 建構參數（`level`／`fair_twd`／
#: `n_effective`），沒覆寫就用預設值（L1、9000.0、3）。
_ESTIMATE_OVERRIDES_BY_KEY: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _stub_valuation(monkeypatch):
    from ygo_sniper import valuation as val_mod

    def _est(_valuator, r):
        return FakeEstimate(**_ESTIMATE_OVERRIDES_BY_KEY.get(r["key"], {}))

    def _attrs(_valuator, r):
        payload = json.loads(r.get("payload") or "{}")
        return None, ((payload.get("card") or {}).get("rarity")), 10.0

    monkeypatch.setattr(val_mod, "estimate_signal_row", _est)
    monkeypatch.setattr(val_mod, "card_attrs_from_row", _attrs)
    yield
    _ESTIMATE_OVERRIDES_BY_KEY.clear()


class _FX:
    """規則 5 分子換匯用的假匯率（修正回合二 Task 13）。JPY:TWD 固定 1:1——
    讓 `hb_row()` 的 `ratio` 參數可以直接反推 `price_native`。

    ⚠️ 對 `apply_markup` **不是盲的**：市價基準的不變式是「分子必須以
    `apply_markup=False` 換匯」（與 comps.price_twd 同尺，comps.py 的
    `to_twd(..., apply_markup=False)`）。真 `FxRates.to_twd` 的預設是
    `apply_markup=True`——日後重構若少寫這個 kwarg，會靜默退回加價口徑、
    比率整體膨脹（少推播＝本專案最貴的錯誤方向）。這裡直接拋錯釘死。"""

    def to_twd(self, amount: float, currency: str, *, apply_markup: bool = True) -> float:
        if apply_markup:
            raise AssertionError(
                "規則 5 的市價分子必須以 apply_markup=False 換匯"
                "（與 comps.price_twd 同一把尺）——少寫 kwarg 會靜默退回加價口徑"
            )
        if str(currency).upper() != "JPY":
            raise ValueError(f"未知幣別: {currency}")
        return float(amount)  # 1:1


FX = _FX()


def run(rows, *, rules: NotifyRules = RULES, notified=None, valuator=VALUATOR,
        seller_ctx=None, snipe_ctx=None, fx=FX):
    return evaluate(
        rows, rules=rules, valuator=valuator, now=NOW, notified=notified,
        seller_ctx=seller_ctx, snipe_ctx=snipe_ctx, fx=fx,
    )


# ---------------------------------------------------------------------------
# 樣本工廠——規則 5
# ---------------------------------------------------------------------------
def hb_row(
    key: str = "buyee_mercari:h1",
    *,
    band: str | None = "high",
    comps_median: float | None = 10000.0,
    comps_n: int = 3,
    ratio: float = 0.65,
    landed_ratio: float | None = None,
    price_native: float | None = None,
    currency: str = "JPY",
    rarity: str | None = "ultra",
) -> dict:
    """一筆高價帶 signal。**修正回合二 Task 13**：規則 5 的比率改讀
    `market_twd / estimate.fair_twd`，`market_twd` 由 `price_native`／`currency`
    經 `FX`（1:1、不加價）換算——`ratio` 反推 `price_native`，讓呼叫端指定的
    `ratio` 精確等於 `Match.price_ratio`。

    `landed_ratio`（預設 ＝ `ratio`，向後相容 Task 9 時期的呼叫端）另外反推
    `landed_twd`——**不參與比率計算**，只顯示在訊息裡。要驗證「分子是市價
    不是到手成本」時把它調成跟 `ratio` 不同即可（見
    `test_ratio_uses_market_basis_not_landed_cost`）。

    `price_native` 可直接覆寫（不經 `ratio` 反推），用於缺值／換匯失敗的紅燈
    測試。

    `comps_median`／`comps_n`／`discount_pct` 仍然寫進 row（真實 signals 列
    本來就有這幾欄），但**刻意**保留可以跟 `FAIR_TWD` 不一致——規則 5 不該讀
    它們，`test_ratio_is_computed_from_estimate_not_comps_median` 就是拿一個
    刻意不一致的 `comps_median` 來證明這件事。`discount_pct` 算式沿用
    `domain.Signal.discount_pct` 的定義（`(median - price) / median`），
    只是它現在對規則 5 而言是死欄位。
    """
    market_native = round(ratio * FAIR_TWD, 2) if price_native is None else price_native
    landed = round((ratio if landed_ratio is None else landed_ratio) * FAIR_TWD, 2)
    discount_pct = (comps_median - landed) / comps_median if comps_median else None
    payload = {
        "listing": {"site": "buyee_mercari", "seller_id": "s1"},
        "card": {"grader": "PSA", "grade": 9.0, "rarity": rarity},
    }
    return {
        "key": key,
        "title": "PSA9 封印されしエクゾディア 初期 ウルトラ",
        "url": f"https://example.test/{key}",
        "landed_twd": landed,
        "price_native": market_native,
        "currency": currency,
        "route": "buyee_consolidated",
        "score": 30.0,
        "flags": json.dumps([]),
        "band": band,
        "comps_median": comps_median,
        "comps_n": comps_n,
        "discount_pct": discount_pct,
        "payload": json.dumps(payload, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# (a)(b)(c) 規則 5 的兩道閘門
# ---------------------------------------------------------------------------
def test_a_high_band_hits_with_l1_and_deep_discount():
    """L1 ＋ ratio 0.65（≤ 0.70 門檻）→ 命中。"""
    r = hb_row(ratio=0.65)
    out = run([r])
    assert [m.rule for m in out.high_band] == [RULE_HIGH_BAND]
    m = out.high_band[0]
    assert m.price_ratio == pytest.approx(0.65)
    assert m.fair_twd == pytest.approx(FAIR_TWD)
    assert m.sample_n == 3


def test_b_high_band_not_sent_when_ratio_above_threshold():
    """L1 ＋ ratio 0.75（> 0.70 門檻）→ 不推。"""
    r = hb_row(ratio=0.75)
    assert run([r]).high_band == []


def test_c_high_band_not_sent_without_card_specific_level():
    """估價等級 L3（不是這張卡自己的成交）→ 不管折價多深都不推。"""
    r = hb_row(ratio=0.30)  # 折價極深
    _ESTIMATE_OVERRIDES_BY_KEY[r["key"]] = {"level": "L3"}
    assert run([r]).high_band == []


def test_high_band_not_sent_without_fair_twd():
    """估價過了層級閘門但給不出公允價（`estimate.fair_twd is None`）
    → 沒有比價基準，不推。取代舊版「`comps_median` 缺值不推」——
    Task 9 之後 `comps_median` 已經不是規則 5 的比價基準，`fair_twd` 才是。
    修正回合二 Task 13：這是**缺值**不是「沒過門檻」，要留痕在 skipped。"""
    r = hb_row(ratio=0.65)
    _ESTIMATE_OVERRIDES_BY_KEY[r["key"]] = {"fair_twd": None}
    out = run([r])
    assert out.high_band == []
    assert out.skips_for("缺值不送：公允價"), "缺值要留痕，不能靜默略過"


# ---------------------------------------------------------------------------
# 修正回合 Task 9：閘門與分母同源——ratio 只讀 estimate，comps_median 是死欄位
# ---------------------------------------------------------------------------
def test_ratio_is_computed_from_estimate_not_comps_median():
    """comps_median 刻意寫成跟 estimate.fair_twd 差好幾個數量級——
    若規則 5 還在讀 comps_median／discount_pct，這一筆的 ratio 會被算成
    離譜的正數（遠超過門檻）而不推；讀 estimate 才會正確算出 0.65 並命中。
    這是修復前會紅、修復後會綠的錨定測試（C2 的紅燈證據）。"""
    r = hb_row(ratio=0.65, comps_median=1.0)
    out = run([r])
    assert [m.rule for m in out.high_band] == [RULE_HIGH_BAND]
    m = out.high_band[0]
    assert m.price_ratio == pytest.approx(0.65)
    assert m.fair_twd == pytest.approx(FAIR_TWD)


# ---------------------------------------------------------------------------
# 修正回合二 Task 13（W3）：分子改市價基準——不是到手成本
# ---------------------------------------------------------------------------
def test_ratio_uses_market_basis_not_landed_cost():
    """市價比 0.68（≤0.70 門檻，該推）、到手成本比 0.74（>0.70 門檻，該擋）
    ——同一筆。分子若還是 `landed_twd` 就不會推；分子改市價基準才會推。
    這是修復前會紅、修復後會綠的錨定測試（W3 的紅燈證據 (a)）。"""
    r = hb_row(ratio=0.68, landed_ratio=0.74)
    out = run([r])
    assert [m.rule for m in out.high_band] == [RULE_HIGH_BAND], (
        "分子是市價基準（0.68 ≤ 0.70），必須推播——若還在用到手成本（0.74 > "
        "0.70）這一筆會被漏推"
    )
    m = out.high_band[0]
    assert m.price_ratio == pytest.approx(0.68)
    # 到手成本仍然要顯示在訊息裡，只是不參與比率——見 notify.format_high_band。
    assert m.row["landed_twd"] == pytest.approx(0.74 * FAIR_TWD)


def test_ratio_not_triggered_by_landed_cost_alone():
    """市價比 0.75（> 門檻，不該推）、到手成本比 0.60（看起來很便宜）
    ——若分子誤用到手成本會誤推；用市價基準則正確不推。"""
    r = hb_row(ratio=0.75, landed_ratio=0.60)
    assert run([r]).high_band == []


# ---------------------------------------------------------------------------
# 修正回合二 Task 13（S2）：分子或分母缺值 → 可見 Skip，不是靜默略過
# ---------------------------------------------------------------------------
def test_high_band_skip_when_fx_not_provided():
    """`evaluate(..., fx=None)`（呼叫端沒把換匯物件帶進來）
    → 規則 5 對 band='high' 的候選一律留下可見的 Skip，理由寫明缺 fx。
    這是修復前會紅、修復後會綠的錨定測試（S2 的紅燈證據 (b)）。"""
    r = hb_row(ratio=0.65)
    out = run([r], fx=None)
    assert out.high_band == []
    skips = out.skips_for("缺值不送：換匯物件（fx）")
    assert len(skips) == 1
    assert skips[0].key == r["key"]


def test_high_band_skip_when_price_native_missing():
    """標價（`price_native`）缺值 → 可見 Skip，理由寫明缺標價。"""
    r = hb_row(ratio=0.65)
    r["price_native"] = None
    out = run([r])
    assert out.high_band == []
    assert out.skips_for("缺值不送：標價")


def test_high_band_skip_when_currency_missing():
    """幣別缺值 → 可見 Skip，理由寫明缺標價（與 price_native 共用同一句，
    兩者都是「市價換不出來」的同一種病）。"""
    r = hb_row(ratio=0.65)
    r["currency"] = None
    out = run([r])
    assert out.high_band == []
    assert out.skips_for("缺值不送：標價")


def test_high_band_skip_when_currency_unconvertible():
    """幣別存在但 `fx.to_twd` 不認得（例如打錯字）→ 可見 Skip，理由帶原始錯誤。"""
    r = hb_row(ratio=0.65, currency="XXX")
    out = run([r])
    assert out.high_band == []
    skips = out.skips_for("缺值不送：市價換匯失敗")
    assert len(skips) == 1


# ---------------------------------------------------------------------------
# 修正回合 Task 9（W5）：ratio < 0.5 不擋，但文案追加深折價警語
# ---------------------------------------------------------------------------
def test_high_band_deep_discount_warning_appears_below_half():
    r = hb_row(ratio=0.4)
    m = run([r]).high_band[0]
    assert HIGH_BAND_DEEP_DISCOUNT_WARNING in m.high_band_source_note


def test_high_band_no_deep_discount_warning_between_half_and_threshold():
    r = hb_row(ratio=0.6)
    m = run([r]).high_band[0]
    assert HIGH_BAND_DEEP_DISCOUNT_WARNING not in m.high_band_source_note


def test_high_band_ratio_threshold_is_configurable():
    r = hb_row(ratio=0.72)
    assert run([r]).high_band == []
    rules = NotifyRules(high_band_max_price_ratio=0.75)
    assert len(run([r], rules=rules).high_band) == 1


def test_high_band_default_ratio_matches_documented_value():
    assert NotifyRules().high_band_max_price_ratio == pytest.approx(
        DEFAULT_HIGH_BAND_MAX_PRICE_RATIO
    )
    assert DEFAULT_HIGH_BAND_MAX_PRICE_RATIO == pytest.approx(0.70)


# ---------------------------------------------------------------------------
# 訊息文案：判定來源＋價格帶徽章（驗收條件 4）
# ---------------------------------------------------------------------------
def test_high_band_match_carries_source_note_and_badge():
    r = hb_row(ratio=0.65)
    _ESTIMATE_OVERRIDES_BY_KEY[r["key"]] = {"n_effective": 7}
    m = run([r]).high_band[0]
    assert m.high_band_source_note == "判定來源：同卡成交 × 7 筆估值"
    assert m.price_band_label == HIGH_BAND_BADGE
    assert "高價帶" in m.price_band_label


# ---------------------------------------------------------------------------
# (d) band 閘門：規則 1/2/3 全部跳過
# ---------------------------------------------------------------------------
def urgent_row(band: str | None) -> dict:
    """會命中規則 1（競標急件）的一筆，band 可調——證明閘門真的擋住評估，
    不是這一筆本來就不符合規則 1。"""
    listing = {
        "site": "buyee_yahoo",
        "external_id": "a1",
        "title": "urgent probe",
        "url": "https://example.test/a1",
        "price": 20000.0,
        "currency": "JPY",
        "raw": {"price_kind": "current_bid"},
        "end_time": (NOW + timedelta(hours=3)).isoformat(),
        "bids": 3,
    }
    bid = {
        "ok": True, "reason": "可出價", "max_bid_jpy": 50000.0,
        "landed_at_ceiling_twd": 3120.0,
        "evidence_label": "L1 卡名×稀有度×分數｜同層成交 3 筆",
        "evidence_tier": "strong", "evidence_tier_label": "證據強",
    }
    payload = {
        "listing": listing,
        "card": {"grader": "ARS", "grade": 10.0, "rarity": "ultra"},
        "best_route": {"label": "Buyee 集運"},
        "bid": bid,
    }
    return {
        "key": "buyee_yahoo:a1",
        "title": listing["title"], "url": listing["url"],
        "price_native": 20000.0, "currency": "JPY", "landed_twd": 8000.0,
        "route": "buyee_consolidated", "score": 30.0, "flags": json.dumps([]),
        "band": band,
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def test_d_high_band_skips_rule1_auction_urgent():
    assert run([urgent_row(band="std")]).urgent != [], "positive control：std 應該命中"
    assert run([urgent_row(band="high")]).urgent == []


def high_p_style_row(band: str | None) -> dict:
    """會命中規則 2（高信心標的）的一筆：定價、稀有度非普卡、P 值高、有區間。"""
    listing = {
        "site": "buyee_mercari",
        "external_id": "m1",
        "title": "high p probe",
        "url": "https://example.test/m1",
        "price": 5000.0,
        "currency": "JPY",
        "raw": {"price_kind": "fixed"},
        "end_time": None,
        "bids": None,
    }
    payload = {
        "listing": listing,
        "card": {"grader": "PSA", "grade": 9.0, "rarity": "ultra"},
        "best_route": {"label": "Buyee 集運"},
        "bid": None,
    }
    return {
        "key": "buyee_mercari:m1",
        "title": listing["title"], "url": listing["url"],
        "price_native": 5000.0, "currency": "JPY", "landed_twd": 2000.0,
        "route": "buyee_consolidated", "score": 30.0, "flags": json.dumps([]),
        "band": band,
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def test_d_high_band_skips_rule2_high_p():
    assert run([high_p_style_row(band="std")]).high_p != [], "positive control：std 應該命中"
    assert run([high_p_style_row(band="high")]).high_p == []


SELLER_KEY = "ebay:9999"


def seller_row(band: str | None) -> dict:
    return {
        "key": SELLER_KEY,
        "title": "青眼の白龍",
        "url": "https://www.ebay.com/itm/9999",
        "landed_twd": 1800.0,
        "price_native": 1000.0,
        "currency": "TWD",
        "route": "ebay_direct",
        "band": band,
        "payload": json.dumps({"listing": {"site": "ebay", "seller_id": "psa"}}),
    }


def seller_ctx_with_new_cheap_item() -> SellerNotifyContext:
    """一筆會命中規則 3 的監控賣家新上架（同儕便宜 33%）。"""
    row_ = MarketRow(
        key=SELLER_KEY, site="ebay", basis=BASIS_ASK, price_twd=1000.0,
        title="青眼の白龍", seller_key="ebay:psa", card_name="青眼の白龍",
    )
    peer = PeerMatch(
        tier=TIER_STRICT, tier_label=TIER_LABEL[TIER_STRICT],
        peer_median_twd=1500.0, peer_n=3, peer_sellers=2,
        peer_unknown_seller_n=0, sources=(),
    )
    item = SellerItem(row=row_, peer=peer)
    item.ratio = 1000.0 / 1500.0
    ctx = SellerNotifyContext()
    ctx.watch = {"ebay:psa": {"seller_key": "ebay:psa", "source": "manual",
                               "score": None, "batch": 0}}
    ctx.items = {SELLER_KEY: item}
    ctx.obs = {SELLER_KEY: {"key": SELLER_KEY, "seen_count": 1}}
    return ctx


def test_d_high_band_skips_rule3_seller_new():
    rules = NotifyRules(auction_urgent_enabled=False, high_p_enabled=False,
                         seller_new_enabled=True)
    ctx = seller_ctx_with_new_cheap_item()
    out_std = run([seller_row(band="std")], rules=rules, seller_ctx=ctx)
    assert out_std.seller_new != [], "positive control：std 應該命中"
    out_high = run([seller_row(band="high")], rules=rules, seller_ctx=ctx)
    assert out_high.seller_new == []


# ---------------------------------------------------------------------------
# (f) 狙擊命中不受 band 閘門影響
# ---------------------------------------------------------------------------
WATCH_KW = dict(
    grader="PSA", grade=9.0, grade_label="9",
    name_ja="封印されしエクゾディア", name_en="Exodia the Forbidden One",
    aliases=[], code_raw="P4-01", code_norm="P4-1",
)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def test_f_card_snipe_still_fires_for_high_band_signal(store, cfg):
    """band 閘門管的是規則 1/2/3；規則 4 從 `snipe_ctx.pending` 進來，
    跟 signals 候選池的 band 完全無關——這一筆的 signals 列即使是 band='high'，
    狙擊命中照樣要送。"""
    wid = store.insert_card_watch(**WATCH_KW)
    store.upsert_card_watch_hit(
        wid, "buyee_mercari:h1", tier="exact",
        title="PSA9 封印されしエクゾディア 初期 ウルトラ",
        url="https://example.test/buyee_mercari:h1",
        site="buyee_mercari", seller_id="s1", price_native=22222.0, currency="JPY",
        end_time=None,
    )
    from dataclasses import replace

    rules = replace(NotifyRules.from_config(cfg))
    r = hb_row(key="buyee_mercari:h1", ratio=0.90)  # 折價不夠深，規則 5 不會命中
    out = run(
        [r], rules=rules, notified=store.notify_log_map(),
        snipe_ctx=build_notify_context(store),
    )
    assert out.high_band == [], "折價不夠深，規則 5 這一筆本來就不該命中"
    assert [m.rule for m in out.to_send] == [RULE_CARD_SNIPE]
    assert out.to_send[0].key == f"{wid}:buyee_mercari:h1"


# ---------------------------------------------------------------------------
# from_config：讀新門檻
# ---------------------------------------------------------------------------
def test_from_config_reads_high_band_settings():
    class Cfg:
        notify = {
            "rules": {
                "high_band_discount": {"enabled": False, "max_price_ratio": 0.5},
            },
        }

    rules = NotifyRules.from_config(Cfg())
    assert rules.high_band_enabled is False
    assert rules.high_band_max_price_ratio == pytest.approx(0.5)


def test_from_config_default_settings_yaml_ratio_is_070(cfg):
    """settings.yaml 的 `notify.rules.high_band_discount.max_price_ratio`
    真的是 0.70（使用者 2026-08-22 定案），不是文件寫一套、設定檔又是另一套。"""
    rules = NotifyRules.from_config(cfg)
    assert rules.high_band_max_price_ratio == pytest.approx(0.70)
    assert rules.high_band_enabled is True


# ---------------------------------------------------------------------------
# 送達接線（主線程裁決追加）：規則 5 的 match 不能停在 Outcome.high_band——
# 必須真的走到 to_send／規則計數／訊息格式化，否則「命中卻不送」＝靜默失敗
# （CLAUDE.md 第五節頭號紅線）。
# ---------------------------------------------------------------------------
def test_high_band_match_reaches_to_send():
    """規則 5 命中後要進 `out.to_send`（不是停在 `out.high_band` 沒人接手）。"""
    from ygo_sniper.notify_rules import RULE_HIGH_BAND

    r = hb_row(ratio=0.65)
    out = run([r])
    assert out.high_band, "前提：規則 5 這一筆要先命中"
    assert [m.rule for m in out.to_send] == [RULE_HIGH_BAND]
    assert out.to_send[0] is out.high_band[0]


def test_high_band_count_shows_in_cli_rule_counts(capsys):
    """`cli._print_rule_counts` 要印出規則 5 的命中數——0 與「沒在跑」不能長一樣
    （比照 `test_card_snipe_notify.py::test_rule4_appears_in_the_cli_counts`）。"""
    import ygo_sniper.cli as cli_mod

    r = hb_row(ratio=0.65)
    out = run([r])
    cli_mod._print_rule_counts(out)
    printed = capsys.readouterr().out
    assert "規則 5 高價帶折價" in printed
    assert "命中 1 筆" in printed


def test_format_high_band_message_carries_source_note_and_badge():
    """送出文字含「判定來源：同卡成交 ×」與「高價帶」字樣（驗收條件 2）。"""
    from ygo_sniper.notify import format_high_band

    r = hb_row(ratio=0.65)
    _ESTIMATE_OVERRIDES_BY_KEY[r["key"]] = {"n_effective": 7}
    m = run([r]).high_band[0]
    text = format_high_band(m, "http://127.0.0.1:8321")
    assert "判定來源：同卡成交 × 7 筆估值" in text
    assert "高價帶" in text
    assert "封印されしエクゾディア" in text
    assert m.row["url"] in text


def test_render_dispatches_high_band_to_its_own_formatter():
    """`Notifier.render` 不能讓規則 5 落進規則 2 的 fallback（`format_high_p`
    會找 `match.estimate.venue` 之類規則 5 沒有意義的欄位，訊息會失真或炸掉）。"""
    from ygo_sniper.notify import TelegramNotifier

    r = hb_row(ratio=0.65)
    m = run([r]).high_band[0]
    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier.dashboard_url = "http://127.0.0.1:8321"
    text = notifier.render(m)
    assert "判定來源：同卡成交" in text
    assert "🏷️ 高價帶" in text


def test_format_overflow_counts_high_band_separately():
    """規則 5 超量時也要在 `format_overflow` 的分類統計裡看得到，
    不是被吃進「另有 N 筆」卻沒人知道是哪一條規則。"""
    from ygo_sniper.notify import format_overflow

    r = hb_row(ratio=0.65)
    m = run([r]).high_band[0]
    text = format_overflow([m], "http://127.0.0.1:8321")
    assert "高價帶折價 1 筆" in text


# ---------------------------------------------------------------------------
# `notify-preview` CLI（高價帶掃描 plan Task 14）：這是調 `max_price_ratio`
# 門檻用的工具，看不到規則 5 的表格就調不了——打**真正的 `notify-preview`
# 指令**（CLAUDE.md 第六節：驗證使用者實際會打的指令，不是元件會不會動）。
# 用 `run()` 算出來的**真實** Outcome（走過 `evaluate`／`_match_high_band`
# 本人，不是手拼一個 Match）餵給 monkeypatch 過的 `Pipeline.notification_outcome`
# ——這樣既不必為了 L1/L2 估價搭一整套真實 comps／DB，也不是在測試裡
# 重新實作 CLI 的表格渲染邏輯（那才是假守衛）。
# ---------------------------------------------------------------------------
def test_notify_preview_prints_rule5_table_and_skip_reasons(tmp_path, monkeypatch):
    from dataclasses import replace as dc_replace

    from typer.testing import CliRunner

    import ygo_sniper.cli as cli_mod
    import ygo_sniper.config as config_mod
    import ygo_sniper.pipeline as pipeline_mod

    db = tmp_path / "preview_hb.db"
    config_mod.load_config.cache_clear()
    test_cfg = dc_replace(
        config_mod.load_config(),
        storage={**config_mod.load_config().storage, "db_path": str(db)},
    )
    # `notify_preview` 自己 `Pipeline()`（不帶 cfg）→ 走 pipeline 模組的
    # load_config。承重斷言：這條測試絕不能碰正式庫。
    monkeypatch.setattr(pipeline_mod, "load_config", lambda: test_cfg)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FX)
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: {})
    assert pipeline_mod.load_config().db_path == db, "preview 的 cfg 沒有指到 tmp db"

    hit = hb_row(ratio=0.62)
    missing = hb_row(key="buyee_mercari:h2", ratio=0.65)
    missing["price_native"] = None
    outcome = run([hit, missing])
    assert len(outcome.high_band) == 1  # 命中；缺值那筆進 outcome.skipped
    assert any(s.rule == RULE_HIGH_BAND for s in outcome.skipped)
    monkeypatch.setattr(
        pipeline_mod.Pipeline, "notification_outcome", lambda self, *a, **kw: outcome
    )

    try:
        r = CliRunner().invoke(cli_mod.app, ["notify-preview"])
        assert r.exit_code == 0, f"{r.output}\n{r.exception!r}"
        assert "規則 5 高價帶折價：命中 1 筆" in r.output
        assert f"標價/市價 {outcome.high_band[0].price_ratio:.0%}" in r.output
        assert f"公允價 NT${FAIR_TWD:,.0f}" in r.output
        # 缺值那筆要在「被排除」表格裡看得見理由，不能靜默消失
        assert "缺值不送" in r.output
    finally:
        config_mod.load_config.cache_clear()
