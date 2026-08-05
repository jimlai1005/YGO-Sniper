"""兩條推播規則：命中什麼、排除什麼、去重與總量上限。

這裡釘的是「手機會不會響」。推播的失敗有兩個方向，兩個都很貴：

  響太少 —— 唯一一筆快結標又有空間的標的沒有通知，你錯過它時不會有任何徵兆。
  響太多 —— 每輪都響，你會開始滑掉通知，然後真的該看的那次也一起滑掉。

所以規則的每一條件都要有「缺這一條就不送」的測試（規則 1 三個條件各一條），
每一個排除都要有「排掉的是假陽性、沒排掉真陽性」的測試（規則 2 的普卡與
未競價競標），而健康告警**永遠不受這一切影響**——那是另一條線。

判定不碰資料庫、不碰網路、不碰 Telegram：`evaluate()` 是純函式，
餵它 signals 列的形狀就好。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ygo_sniper.bidding import ACTIONABLE_WINDOW_HOURS, auction_tier
from ygo_sniper.notify import (
    format_auction_urgent,
    format_countdown,
    format_high_p,
    format_overflow,
)
from ygo_sniper.notify_rules import (
    RULE_AUCTION_URGENT,
    RULE_HIGH_P,
    NotifyRules,
    evaluate,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
DASH = "http://127.0.0.1:8321"
RULES = NotifyRules()


# ---------------------------------------------------------------------------
# 樣本工廠
# ---------------------------------------------------------------------------
def bid_dict(max_bid_jpy: float | None, *, ok: bool = True, **over) -> dict:
    d = {
        "ok": ok,
        "reason": "可出價",
        "max_bid_jpy": max_bid_jpy,
        "landed_at_ceiling_twd": 312.0,
        "evidence_label": "L1 卡名×稀有度×分數｜同層成交 3 筆｜校準群 L1/n<3／147 筆",
        "evidence_tier": "strong",
        "evidence_tier_label": "證據強",
    }
    d.update(over)
    return d


def row(
    key: str = "buyee_yahoo:a1",
    *,
    live: bool = True,
    ends_in_h: float | None = 3.0,
    price: float = 1000.0,
    bids: int | None = 3,
    ceiling: float | None = 5000.0,
    bid_ok: bool = True,
    rarity: str | None = "ultra",
    flags: list[str] | None = None,
    **bid_over,
) -> dict:
    """一筆 signals 表列（欄位名與 store.upsert_signal 寫入的一致）。"""
    end = None if ends_in_h is None else (NOW + timedelta(hours=ends_in_h)).isoformat()
    listing = {
        "site": "buyee_yahoo",
        "external_id": key.split(":")[-1],
        "title": "【ARS10】初期 ブラック・マジシャン",
        "url": "https://buyee.jp/item/yahoo/auction/a1",
        "price": price,
        "currency": "JPY",
        "raw": {"price_kind": "current_bid" if live else "fixed"},
        "end_time": end,
        "bids": bids,
    }
    payload = {
        "listing": listing,
        "card": {"grader": "ARS", "grade": 10.0, "rarity": rarity},
        "best_route": {"label": "Buyee 集運（湊單）"},
        "bid": bid_dict(ceiling, ok=bid_ok, **bid_over) if ceiling is not None or not bid_ok
        else None,
    }
    return {
        "key": key,
        "title": listing["title"],
        "url": listing["url"],
        "price_native": price,
        "currency": "JPY",
        "landed_twd": 815.0,
        "route": "buyee_consolidated",
        "score": 30.0,
        "flags": json.dumps(flags or ["discount"]),
        "payload": json.dumps(payload, ensure_ascii=False),
    }


class FakeEstimate:
    """`valuation.Estimate` 裡推播真的會讀到的那幾欄。"""

    def __init__(self, p: float | None = 0.9, *, interval: bool = True, fair=1793.0):
        self.p_worth_buying = p
        self.fair_twd = fair
        self.lo_twd = 443.0 if interval else None
        self.hi_twd = 6972.0 if interval else None
        self.confidence = 0.80
        self.level = "L1"
        self.level_label = "卡名×稀有度×分數"
        self.n_effective = 3
        self.venue = "buyee_yahoo"
        self.venue_adjusted = True
        self.venue_is_estimated = True
        self.calibration_group = "L1/n<3"
        self.calibration_group_n = 147
        self.calibration_group_requested = "L1/n<3"
        self.calibration_degraded = False
        self.grade_source = "title"

    @property
    def has_interval(self) -> bool:
        return self.lo_twd is not None and self.hi_twd is not None


class FakeValuator:
    """`estimate_signal_row` 與 `card_attrs_from_row` 都會拿到它，但兩支都被
    monkeypatch 掉——這個替身只是「有一顆模型」的存在證明。"""


@pytest.fixture(autouse=True)
def _stub_valuation(monkeypatch):
    """估價與稀有度抽取都打樁：本檔測的是**規則**，不是模型。

    稀有度回傳 payload 裡那一份（與正式路徑 `card_attrs_from_row` 的優先序相同）。
    """
    from ygo_sniper import valuation as val_mod

    def _est(_valuator, r):
        p = _P_BY_KEY.get(r["key"], 0.9)
        return FakeEstimate(p, interval=_INTERVAL_BY_KEY.get(r["key"], True))

    def _attrs(_valuator, r):
        payload = json.loads(r.get("payload") or "{}")
        return None, ((payload.get("card") or {}).get("rarity")), 10.0

    monkeypatch.setattr(val_mod, "estimate_signal_row", _est)
    monkeypatch.setattr(val_mod, "card_attrs_from_row", _attrs)


_P_BY_KEY: dict[str, float | None] = {}
_INTERVAL_BY_KEY: dict[str, bool] = {}


@pytest.fixture(autouse=True)
def _reset_overrides():
    _P_BY_KEY.clear()
    _INTERVAL_BY_KEY.clear()
    yield
    _P_BY_KEY.clear()
    _INTERVAL_BY_KEY.clear()


VALUATOR = FakeValuator()


def run(rows, *, rules: NotifyRules = RULES, notified=None, valuator=VALUATOR):
    return evaluate(rows, rules=rules, valuator=valuator, now=NOW, notified=notified)


# ---------------------------------------------------------------------------
# 1. 規則 1：三個條件缺一則不送
# ---------------------------------------------------------------------------
def test_urgent_hits_when_all_three_conditions_hold():
    out = run([row()])
    assert [m.rule for m in out.urgent] == [RULE_AUCTION_URGENT]
    m = out.urgent[0]
    assert m.room == 4000 and m.max_bid == 5000 and m.current_bid == 1000
    assert m.currency == "JPY"
    assert 2.9 < m.hours_left < 3.1


def test_urgent_needs_a_ceiling():
    """沒有出價上限（證據不足）＝ 沒有可以填進出價欄的數字，不該催你去看。"""
    out = run([row(ceiling=None, bid_ok=False)])
    assert out.urgent == []


def test_urgent_needs_price_below_ceiling():
    """現價已經越過上限：再看也不能出手（等於上限也不行，最小加價幅度會擋）。"""
    assert run([row(price=5000.0)]).urgent == []      # 剛好等於上限
    assert run([row(price=9000.0)]).urgent == []      # 已越過


def test_urgent_needs_the_auction_to_end_within_the_window():
    """離結標還久的「空間」不算數：競標價要到最後幾分鐘才跳。"""
    assert run([row(ends_in_h=ACTIONABLE_WINDOW_HOURS + 0.5)]).urgent == []
    assert run([row(ends_in_h=ACTIONABLE_WINDOW_HOURS - 0.5)]).urgent != []
    # 已結標與不知道何時結標都不算「現在就該看」
    assert run([row(ends_in_h=-1)]).urgent == []
    assert run([row(ends_in_h=None)]).urgent == []


def test_urgent_only_for_live_auctions():
    """定價標的沒有「快結標」這件事——它隨時可以買。"""
    assert run([row(live=False)]).urgent == []


def test_urgent_is_exactly_dashboard_tier_one():
    """規則 1 與 dashboard 的梯隊 1 是同一個判定，不是兩份長得像的規則。"""
    for r in (row(), row(price=9000.0), row(ends_in_h=100), row(ceiling=None, bid_ok=False)):
        payload = json.loads(r["payload"])
        tier = auction_tier(
            payload.get("bid"), r["price_native"],
            (payload["listing"] or {}).get("end_time"), NOW,
        )
        assert bool(run([r]).urgent) is (tier == 1)


def test_urgent_not_sent_when_a_displayed_number_is_missing():
    """紅線：訊息裡的每個數字都會變成真錢的出價，缺一欄就整則不送。"""
    out = run([row(landed_at_ceiling_twd=None)])
    assert out.urgent == []
    assert out.skips_for("欄位缺值不送"), "被擋下來的理由要看得見"


# ---------------------------------------------------------------------------
# 2. 規則 2：P 門檻與兩種排除
# ---------------------------------------------------------------------------
def high_p_row(key="buyee_mercari:m1", **kw):
    """一筆**定價**標的（沒有競標的價格發現問題），預設會命中規則 2。"""
    kw.setdefault("live", False)
    kw.setdefault("ends_in_h", None)
    kw.setdefault("bids", None)
    kw.setdefault("ceiling", None)
    kw.setdefault("bid_ok", False)
    return row(key, **kw)


def test_high_p_threshold_is_strictly_greater():
    _P_BY_KEY["buyee_mercari:m1"] = 0.70
    assert run([high_p_row()]).high_p == [], "剛好等於門檻不送（門檻是嚴格大於）"
    _P_BY_KEY["buyee_mercari:m1"] = 0.71
    assert len(run([high_p_row()]).high_p) == 1


def test_high_p_excludes_normal_rarity():
    """普卡便宜到 P 幾乎必然很高，但那不是撿漏。"""
    out = run([high_p_row(rarity="normal")])
    assert out.high_p == []
    assert out.skips_for("排除稀有度")[0].reason.endswith("normal")


def test_unknown_rarity_is_not_treated_as_normal():
    """⚠️ 讀不出稀有度是「不知道」，不是「便宜」——不可以跟普卡一起排掉。"""
    assert len(run([high_p_row(rarity=None)]).high_p) == 1


def test_exclude_rarities_is_configurable():
    rules = NotifyRules(exclude_rarities=("normal", "rare"))
    assert run([high_p_row(rarity="rare")], rules=rules).high_p == []
    assert run([high_p_row(rarity="rare")]).high_p != []   # 預設只排普卡


def test_high_p_excludes_auctions_whose_price_was_never_discovered():
    """¥1 起標、0 次出價：現價就是起標價，用它算出來的 P 是假的。"""
    out = run([row(price=1.0, bids=0, ends_in_h=100)])
    assert out.high_p == []
    assert "尚未被競價" in out.skips_for("尚未被競價")[0].reason


def test_high_p_excludes_auctions_that_still_have_days_to_run():
    """有出價次數也不夠：還剩 6 天的標的，價格還會漲好幾輪。"""
    out = run([row(bids=3, ends_in_h=RULES.price_discovered_within_hours + 1)])
    assert out.high_p == []
    assert out.skips_for("現價還會漲")


def test_high_p_keeps_auctions_whose_price_is_already_discovered():
    """反面：有人出過價、而且快結標了——這時候的現價是有意義的。"""
    out = run([row(bids=3, ends_in_h=RULES.price_discovered_within_hours - 1, price=4000)])
    assert len(out.high_p) == 1


def test_high_p_excludes_auction_with_unknown_bid_count():
    """來源沒給出價次數 = 無法確認價格被發現過。缺值寧可不送。"""
    out = run([row(bids=None, ends_in_h=5)])
    assert out.high_p == []
    assert out.skips_for("沒給出價次數")


def test_high_p_excludes_ended_auctions():
    out = run([row(bids=3, ends_in_h=-2, price=4000)])
    assert out.high_p == []
    assert out.skips_for("已結標")


def test_high_p_not_sent_without_an_interval():
    """沒有 80% 區間就沒有「公允價多不確定」可講——不送半套訊息。"""
    _INTERVAL_BY_KEY["buyee_mercari:m1"] = False
    out = run([high_p_row()])
    assert out.high_p == []
    assert out.skips_for("欄位缺值不送")


def test_high_p_skipped_entirely_when_the_model_is_unavailable():
    """估價模型建不起來時規則 2 沉默，但**規則 1 照跑**（它不需要模型）。"""
    out = run([row(), high_p_row()], valuator=None)
    assert out.valuation_ok is False
    assert out.high_p == []
    assert len(out.urgent) == 1


# ---------------------------------------------------------------------------
# 3. 去重與競標例外
# ---------------------------------------------------------------------------
def test_high_p_is_deduped_within_the_window():
    r = high_p_row()
    recent = (NOW - timedelta(days=1)).isoformat()
    out = run([r], notified={(r["key"], RULE_HIGH_P): recent})
    assert out.high_p and out.to_send == [] and out.deduped == 1


def test_high_p_is_sent_again_after_the_dedupe_window():
    r = high_p_row()
    old = (NOW - timedelta(days=RULES.dedupe_days + 1)).isoformat()
    out = run([r], notified={(r["key"], RULE_HIGH_P): old})
    assert [m.key for m in out.to_send] == [r["key"]]


def test_urgent_is_not_suppressed_by_an_earlier_high_p_push():
    """**本檔第二重要的一條**：同一個標的先因為 P>70 被推播過，
    進入 24 小時結標窗時仍然要響——那是另一件事實（「快結標了」）。"""
    r = row()
    out = run([r], notified={(r["key"], RULE_HIGH_P): (NOW - timedelta(hours=2)).isoformat()})
    assert [(m.key, m.rule) for m in out.to_send] == [(r["key"], RULE_AUCTION_URGENT)]


def test_urgent_is_sent_once_per_listing():
    """但同一個標的的急件本身只送一次，不會每輪重播到結標為止。"""
    r = row()
    _P_BY_KEY[r["key"]] = 0.10          # 只讓規則 1 命中，才數得清去重的來源
    sent_at = (NOW - timedelta(hours=1)).isoformat()
    out = run([r], notified={(r["key"], RULE_AUCTION_URGENT): sent_at})
    assert out.urgent and out.to_send == [] and out.deduped == 1


def test_same_listing_matching_both_rules_sends_only_the_urgent_one():
    """兩條規則同時命中同一筆時只送規則 1：它嚴格更急，內容也涵蓋證據。"""
    r = row(bids=3, ends_in_h=3, price=1000)
    out = run([r])
    assert len(out.urgent) == 1 and len(out.high_p) == 1
    assert [(m.key, m.rule) for m in out.to_send] == [(r["key"], RULE_AUCTION_URGENT)]
    assert out.skips_for("規則 1 涵蓋")


# ---------------------------------------------------------------------------
# 4. 總量上限
# ---------------------------------------------------------------------------
def test_cap_truncates_and_leaves_the_rest_queued():
    rules = NotifyRules(max_items_per_run=2)
    rows = [high_p_row(f"buyee_mercari:m{i}") for i in range(5)]
    out = run(rows, rules=rules)
    assert len(out.to_send) == 2 and len(out.overflow) == 3
    # 超量的那幾筆**沒有**被記成已送出——下一輪要繼續排隊
    assert all(m not in out.to_send for m in out.overflow)
    text = format_overflow(out.overflow, DASH)
    assert "3 筆" in text and "下一輪" in text


def test_cap_zero_means_no_limit():
    """`max_items_per_run: 0` ＝ 不限。使用者原話：「不用鎖 20 則的上限，
    可以一次發過來沒問題，反正我在外面可以慢慢看。」

    釘住兩件事：全部進 to_send，而且 overflow 是**空的**——不然 notifier
    會多送一則「另有 0 筆未列出」的統計。
    """
    rules = NotifyRules(max_items_per_run=0)
    rows = [high_p_row(f"buyee_mercari:m{i}") for i in range(40)]
    out = run(rows, rules=rules)
    assert len(out.to_send) == 40
    assert out.overflow == []


def test_cap_default_is_unlimited_and_config_can_put_it_back():
    """機制保留：預設不限，但設定填任何正整數就恢復截斷（可以再調回來）。"""
    assert NotifyRules().max_items_per_run == 0

    class Cfg:
        notify = {"max_items_per_run": 3, "rules": {}}

    from ygo_sniper.bidding import ACTIONABLE_WINDOW_HOURS as _w  # noqa: F401

    assert NotifyRules.from_config(Cfg()).max_items_per_run == 3
    # yaml 留空（null）也是「不限」，不是「一則都不送」
    class CfgNull:
        notify = {"max_items_per_run": None, "rules": {}}

    assert NotifyRules.from_config(CfgNull()).max_items_per_run == 0


def test_urgent_takes_priority_over_high_p_under_the_cap():
    """額度不夠時先給急件：它是唯一一個「現在不看就沒了」的類別。"""
    rules = NotifyRules(max_items_per_run=1)
    out = run([high_p_row("buyee_mercari:m1"), row("buyee_yahoo:a1")], rules=rules)
    assert [m.rule for m in out.to_send] == [RULE_AUCTION_URGENT]


# ---------------------------------------------------------------------------
# 5. 訊息內容：使用者拿它下真錢的單
# ---------------------------------------------------------------------------
def test_urgent_message_has_every_number_needed_to_bid():
    m = run([row()]).urgent[0]
    text = format_auction_urgent(m, DASH)
    assert "¥1,000" in text and "¥5,000" in text and "¥4,000" in text   # 現價／上限／空間
    assert "3 小時 0 分" in text                                        # 倒數
    assert "證據強" in text and "同層成交 3 筆" in text                 # 證據強度與有效 n
    assert "校準群" in text                                            # 群組校準
    assert "代理出價" in text and "設好上限就可以離開" in text          # 時效性提醒
    assert 'href="https://buyee.jp/item/yahoo/auction/a1"' in text


def test_high_p_message_has_cost_fair_value_interval_and_p():
    m = run([high_p_row()]).high_p[0]
    text = format_high_p(m, DASH)
    assert "到手 <b>NT$815</b>" in text
    assert "公允價 <b>NT$1,793</b>" in text
    assert "80% 區間 NT$443–6,972" in text
    assert "P(值得買) 90%" in text
    assert "證據強" in text
    assert "📉 折價" in text                                            # 旗標
    assert 'href="https://buyee.jp/item/yahoo/auction/a1"' in text


def test_countdown_drops_the_hour_when_there_is_none():
    assert format_countdown(3.2) == "3 小時 12 分"
    assert format_countdown(0.5) == "30 分"


# ---------------------------------------------------------------------------
# 5b. eBay 競標急件：上限顯示**原幣**（使用者要填的數字）＋台幣等值
# ---------------------------------------------------------------------------
def ebay_row(
    key: str = "ebay:398220263914",
    *,
    ends_in_h: float | None = 3.0,
    price: float = 700.0,
    ceiling_twd: float | None = 1500.0,
    max_bid_native: float | None = 46.4,
    native_currency: str | None = "USD",
    ships_to_tw: bool | None = True,
    **bid_over,
) -> dict:
    """一筆 eBay 競標的 signals 列：現價台幣（eBay 換算顯示）、上限在
    max_bid_listing／max_bid_native，`max_bid_jpy` 是 None——這正是規則 1
    必須認得的新形狀。"""
    end = None if ends_in_h is None else (NOW + timedelta(hours=ends_in_h)).isoformat()
    listing = {
        "site": "ebay",
        "external_id": key.split(":")[-1],
        "title": "Yu-Gi-Oh Dark Magician 1st Edition PSA 10",
        "url": "https://www.ebay.com/itm/398220263914",
        "price": price,
        "currency": "TWD",
        "ships_to_tw": ships_to_tw,
        "raw": {
            "price_kind": "current_bid",
            "buyingOptions": ["AUCTION"],
            "currentBidPrice": {
                "value": str(price), "currency": "TWD",
                "convertedFromValue": str(round(price / 32.3, 2)),
                "convertedFromCurrency": "USD",
            },
        },
        "end_time": end,
        "bids": 3,
    }
    bid = None
    if ceiling_twd is not None:
        bid = bid_dict(
            None,
            max_bid_listing=ceiling_twd,
            listing_currency="TWD",
            max_bid_native=max_bid_native,
            native_currency=native_currency,
            native_rate=32.3,
            **bid_over,
        )
    payload = {
        "listing": listing,
        "card": {"grader": "PSA", "grade": 10.0, "rarity": "ultra"},
        "best_route": {"label": "eBay 直寄"},
        "bid": bid,
    }
    return {
        "key": key,
        "title": listing["title"],
        "url": listing["url"],
        "price_native": price,
        "currency": "TWD",
        "landed_twd": 1250.0,
        "route": "ebay_direct",
        "score": 30.0,
        "flags": json.dumps(["live_auction"]),
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def test_ebay_urgent_matches_with_listing_currency_ceiling():
    out = run([ebay_row()])
    assert [m.rule for m in out.urgent] == [RULE_AUCTION_URGENT]
    m = out.urgent[0]
    assert m.currency == "TWD"
    assert m.max_bid == 1500.0 and m.current_bid == 700.0 and m.room == 800.0
    assert m.max_bid_native == 46.4 and m.native_currency == "USD"
    assert m.current_bid_native == pytest.approx(21.67, abs=0.01)


def test_ebay_urgent_message_shows_native_ceiling_and_taipei_time():
    """訊息裡的上限是**原幣**（要填進 eBay 出價欄的數字）＋台幣等值；
    結標時間換成台北時間；代理出價那行講的是 eBay 原生 automatic bidding。"""
    m = run([ebay_row()]).urgent[0]
    text = format_auction_urgent(m, DASH)
    assert "US$46.40" in text                      # 原幣上限（出價欄要填的）
    assert "≈NT$1,500" in text                     # 台幣等值
    assert "NT$700" in text and "US$21.67" in text  # 現價：台幣＋原幣
    assert "台北時間" in text                       # 結標時間時區註記
    assert "eBay 原生自動出價" in text and "設好上限就可以離開" in text
    assert "Buyee" not in text, "eBay 的訊息不可以教人去按 Buyee 的按鈕"
    assert 'href="https://www.ebay.com/itm/398220263914"' in text


def test_ebay_urgent_not_sent_without_the_native_ceiling():
    """紅線延伸：原幣上限是 eBay 訊息裡唯一可執行的數字，缺了整則不送。"""
    out = run([ebay_row(max_bid_native=None)])
    assert out.urgent == []
    assert out.skips_for("原幣上限")


def test_ebay_urgent_respects_the_tier_conditions():
    assert run([ebay_row(price=1500.0)]).urgent == []          # 已達上限
    assert run([ebay_row(price=2000.0)]).urgent == []          # 已越過
    assert run([ebay_row(ends_in_h=ACTIONABLE_WINDOW_HOURS + 1)]).urgent == []
    assert run([ebay_row(ceiling_twd=None)]).urgent == []      # 沒有上限


def test_high_p_excludes_ebay_that_does_not_ship_to_tw():
    """賣家不寄台灣：P 值是用「寄台灣的到手成本」算的，那筆交易不存在。"""
    r = ebay_row(ships_to_tw=False, ends_in_h=None, ceiling_twd=None)
    payload = json.loads(r["payload"])
    payload["listing"]["raw"] = {"price_kind": "fixed"}   # 定價，繞過競價發現排除
    payload["listing"]["end_time"] = None
    r["payload"] = json.dumps(payload)
    out = run([r])
    assert out.high_p == []
    assert out.skips_for("不寄台灣")


# ---------------------------------------------------------------------------
# 6. 完全靜默：兩條規則都沒命中就一則都不送
# ---------------------------------------------------------------------------
def test_nothing_matches_means_nothing_to_send():
    _P_BY_KEY["buyee_mercari:m1"] = 0.10
    out = run([high_p_row(), row(ends_in_h=200)])
    assert out.matched == 0 and out.to_send == [] and out.overflow == []


# ---------------------------------------------------------------------------
# 7. 推播帳（store）：逐規則記帳、只記送成功的
# ---------------------------------------------------------------------------
def test_notify_log_is_per_rule(tmp_path):
    """同一個 key 的兩條規則各自一列——這就是「競標例外」在資料層的形狀。"""
    from ygo_sniper.store import Store

    st = Store(tmp_path / "t.db")
    st.mark_rule_notified([("k1", RULE_HIGH_P)])
    assert (("k1", RULE_HIGH_P)) in st.notify_log_map()
    assert ("k1", RULE_AUCTION_URGENT) not in st.notify_log_map()

    st.mark_rule_notified([("k1", RULE_AUCTION_URGENT)])
    assert len(st.notify_log_map()) == 2
    # 重複落帳不炸（同一 key+rule 覆寫時間戳即可）
    st.mark_rule_notified([("k1", RULE_AUCTION_URGENT)])
    assert len(st.notify_log_map()) == 2


def test_notification_candidates_skip_decided_states(tmp_path):
    """使用者已經做過決定的狀態（skipped／bought）不再被推播打擾。"""
    from ygo_sniper.store import Store

    st = Store(tmp_path / "t.db")
    with st._conn() as c:  # noqa: SLF001 - 直接塞列，不必跑整條掃描
        for key, state in [("a", "new"), ("b", "watching"), ("c", "skipped"),
                           ("d", "bought"), ("e", "expired")]:
            c.execute(
                "INSERT INTO signals (key, site, external_id, title, url, state, score)"
                " VALUES (?, 'buyee_yahoo', ?, 't', 'u', ?, 1)",
                (key, key, state),
            )
    assert sorted(r["key"] for r in st.notification_candidates()) == ["a", "b"]
