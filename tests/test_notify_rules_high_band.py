"""規則 5（高價帶折價）與 band 閘門。

band='high' 的 signal 只有規則 4（狙擊）與規則 5 有資格評估；規則 1/2/3
一律顯式跳過。band 缺失或 'std' 時完全不影響既有三條規則——這一份是新檔，
`tests/test_notify_rules.py` 全部零改動、全綠就是那條紅線的證據，不在這裡
重複斷言。

判準只讀 signal 上既有的估價欄位（估價等級、`comps_median`、
`discount_pct`），見 `notify_rules._match_high_band` 的 docstring。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ygo_sniper.card_snipe import build_notify_context
from ygo_sniper.notify_rules import (
    DEFAULT_HIGH_BAND_MAX_PRICE_RATIO,
    HIGH_BAND_BADGE,
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
class FakeEstimate:
    def __init__(self, level: str = "L1"):
        self.level = level
        self.level_label = "卡名×稀有度×分數" if level in ("L1", "L2") else "稀有度×分數"
        self.fair_twd = 9000.0
        self.lo_twd = 3000.0
        self.hi_twd = 12000.0
        self.n_effective = 3
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
_LEVEL_BY_KEY: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_valuation(monkeypatch):
    from ygo_sniper import valuation as val_mod

    def _est(_valuator, r):
        return FakeEstimate(_LEVEL_BY_KEY.get(r["key"], "L1"))

    def _attrs(_valuator, r):
        payload = json.loads(r.get("payload") or "{}")
        return None, ((payload.get("card") or {}).get("rarity")), 10.0

    monkeypatch.setattr(val_mod, "estimate_signal_row", _est)
    monkeypatch.setattr(val_mod, "card_attrs_from_row", _attrs)
    yield
    _LEVEL_BY_KEY.clear()


def run(rows, *, rules: NotifyRules = RULES, notified=None, valuator=VALUATOR,
        seller_ctx=None, snipe_ctx=None):
    return evaluate(
        rows, rules=rules, valuator=valuator, now=NOW, notified=notified,
        seller_ctx=seller_ctx, snipe_ctx=snipe_ctx,
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
    rarity: str | None = "ultra",
) -> dict:
    """一筆高價帶 signal。`landed_twd` 由 `ratio × comps_median` 反推，
    `discount_pct` 用與 `domain.Signal.discount_pct` 相同的算式
    `(median - price) / median` 算出——測試餵的欄位跟 scoring 端真的會
    寫進去的值同一條公式，不是湊出來的數字。
    """
    price = round(ratio * comps_median, 2) if comps_median else 0.0
    discount_pct = (comps_median - price) / comps_median if comps_median else None
    payload = {
        "listing": {"site": "buyee_mercari", "seller_id": "s1"},
        "card": {"grader": "PSA", "grade": 9.0, "rarity": rarity},
    }
    return {
        "key": key,
        "title": "PSA9 封印されしエクゾディア 初期 ウルトラ",
        "url": f"https://example.test/{key}",
        "landed_twd": price,
        "price_native": price,
        "currency": "JPY",
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
    assert m.comps_median_twd == pytest.approx(10000.0)
    assert m.comps_n == 3


def test_b_high_band_not_sent_when_ratio_above_threshold():
    """L1 ＋ ratio 0.75（> 0.70 門檻）→ 不推。"""
    r = hb_row(ratio=0.75)
    assert run([r]).high_band == []


def test_c_high_band_not_sent_without_card_specific_level():
    """估價等級 L3（不是這張卡自己的成交）→ 不管折價多深都不推。"""
    r = hb_row(ratio=0.30)  # 折價極深
    _LEVEL_BY_KEY[r["key"]] = "L3"
    assert run([r]).high_band == []


def test_high_band_not_sent_without_comps_median():
    """沒有同卡成交可比（`comps_median` 缺值）→ 沒有比價基準，不推。"""
    r = hb_row(comps_median=None)
    assert run([r]).high_band == []


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
    r = hb_row(ratio=0.65, comps_n=7)
    m = run([r]).high_band[0]
    assert m.high_band_source_note == "判定來源：同卡成交 × 7 筆中位"
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
