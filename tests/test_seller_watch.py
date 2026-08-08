"""賣家監控名單、輪替掃描、推播規則 3 的測試（Seller Alpha 第三棒）。

每一個測試都對應一種「壞了看不出來」的病：

1. **分批穩定性**：批次若跟著行程走（內建 `hash()` 的隨機種子），每次排程
   跑起來輪替表都重洗，「每 240 分鐘掃一次」的保證消失——而且完全無聲。
2. **輪替節流冪等**：節流帳在 db，跨行程有效；4 輪覆蓋全部批次、第 5 輪回到
   第 0 批；同一時段內重複呼叫不會把輪替往前推。
3. **名單上限與淘汰**：滿了之後 manual 不被自動淘汰；auto 只能被分數更高的
   auto 擠掉；擠不動時**拒絕並說明**（不是靜默不做事）。
4. **規則 3 的三個條件**：在監控名單 ＋ 新標的 ＋ 折價達門檻，缺一則不推。
5. **同儕算不出來時的模型 fallback**（2026-08-04 改）：改用模型估值判定，
   但必須過 `bidding.EvidenceGate`、門檻更嚴、訊息標明來源，而且
   **絕不回饋到 Seller Alpha 分數**——那條紅線由 8. 的測試釘死。
6. **手動加入不受分數門檻限制，而且不假裝它有分數**（score 是 None，不是 0）。
7. **規則 3b（估不了）**：連模型都估不了的稀有品要浮出來，但音量要被限制
   （每輪上限、自己的去重帳），而且**不宣稱它便宜**。
8. **模型只影響「要不要通知」，不影響「賣家值不值得追」**：
   `build_notify_context` 不得把 valuator 交給 `seller_alpha.analyze`。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from ygo_sniper.notify_rules import (
    RULE_SELLER_NEW,
    RULE_SELLER_UNPRICED,
    SOURCE_MODEL,
    SOURCE_PEER,
    NotifyRules,
    evaluate,
    variant_hits,
)
from ygo_sniper.seller_alpha import (
    BASIS_ASK,
    TIER_LABEL,
    TIER_STRATUM,
    TIER_STRICT,
    MarketRow,
    PeerMatch,
    SellerItem,
)
from ygo_sniper.seller_watch import (
    SOURCE_AUTO,
    SOURCE_MANUAL,
    SellerNotifyContext,
    WatchParams,
    add_watch,
    batch_of,
    claim_batch,
    due_sellers,
    remove_watch,
)
from ygo_sniper.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


PARAMS = WatchParams(max_sellers=30, per_seller_interval_minutes=240, batches=4)


# ---------------------------------------------------------------------------
# 1. 分批穩定性
# ---------------------------------------------------------------------------
def test_batch_is_stable_within_range():
    for key in ("ebay:psa", "buyee_paypay:p245246", "buyee_yahoo:AiUkMq"):
        assert batch_of(key, 4) == batch_of(key, 4)
        assert 0 <= batch_of(key, 4) < 4


def test_batch_does_not_depend_on_process_hash_seed():
    """**這一條是整個輪替的地基。**

    內建 `hash()` 對 str 是逐行程隨機的（PYTHONHASHSEED），拿它分批的話
    每次排程跑起來每個賣家都會落到不同批，外顯只是「這個賣家好像沒有每 4
    小時掃到」——沒有錯誤訊息。所以用兩個不同的 hash seed 各起一個行程，
    要求算出同一組批號。
    """
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from ygo_sniper.seller_watch import batch_of;"
        "print([batch_of(k, 4) for k in ('ebay:psa','buyee_paypay:p1','x:y','a:b')])"
    )
    outs = []
    for seed in ("0", "1", "12345"):
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout.strip())
    assert len(set(outs)) == 1, f"批號隨 PYTHONHASHSEED 改變：{outs}"


def test_batch_does_not_shift_when_list_changes(store):
    """名單增刪不得讓其他人換批（用名單索引分批就會）。"""
    keys = [f"ebay:s{i}" for i in range(10)]
    for k in keys:
        add_watch(store, k, source=SOURCE_MANUAL, reason="t", params=PARAMS)
    before = {r["seller_key"]: r["batch"] for r in store.list_seller_watch()}
    remove_watch(store, keys[0])
    remove_watch(store, keys[3])
    after = {r["seller_key"]: r["batch"] for r in store.list_seller_watch()}
    for k, b in after.items():
        assert before[k] == b


# ---------------------------------------------------------------------------
# 2. 輪替節流
# ---------------------------------------------------------------------------
def test_rotation_covers_every_batch_then_wraps(store):
    """4 輪掃完 4 批、第 5 輪回到第 0 批（每輪只認領一批）。"""
    t0 = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    seen = []
    for i in range(5):
        batch, why = claim_batch(store, PARAMS, now=t0 + timedelta(minutes=60 * i))
        assert batch is not None, why
        seen.append(batch)
    assert seen == [0, 1, 2, 3, 0]


def test_rotation_throttles_within_the_same_slot(store):
    """同一個時段內再呼叫一次**不認領**（也不把輪替往前推）。

    `scan` 除了每小時的排程還可能被 dashboard 手動按——用輪數計時的話，
    手動按兩次就把每個賣家的「4 小時一次」安靜地變成別的數字。
    """
    t0 = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    assert claim_batch(store, PARAMS, now=t0)[0] == 0
    for minutes in (1, 30, 59):
        batch, why = claim_batch(store, PARAMS, now=t0 + timedelta(minutes=minutes))
        assert batch is None
        assert "節流" in why
    assert claim_batch(store, PARAMS, now=t0 + timedelta(minutes=60))[0] == 1


def test_rotation_state_is_cross_process(tmp_path):
    """節流帳落 db（meta），另一個 Store 實例（＝另一個行程）看得到。"""
    a = Store(tmp_path / "t.db")
    t0 = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    assert claim_batch(a, PARAMS, now=t0)[0] == 0
    b = Store(tmp_path / "t.db")
    assert claim_batch(b, PARAMS, now=t0 + timedelta(minutes=1))[0] is None
    assert claim_batch(b, PARAMS, now=t0 + timedelta(minutes=61))[0] == 1


def test_rotation_disabled_by_config(store):
    batch, why = claim_batch(store, WatchParams(enabled=False))
    assert batch is None
    assert "關閉" in why


def test_due_sellers_skips_recently_scanned_and_unsupported(store):
    """跳過的兩種原因都要說得出來：剛掃過、以及**來源還沒支援賣家頁列舉**。

    2026-08-04：`buyee_yahoo` 從「尚未支援」變成**已支援**（賣家頁解析器
    上線）。2026-08-09：`buyee_mercari` 也轉正（Buyee 鏡像賣家頁上線，
    釘選軌 Phase 2）——它必須真的進 due；未支援的那一半改用 `mercari_tw`
    （台灣站自己的賣家頁仍未實測）。
    """
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    add_watch(store, "ebay:a", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    add_watch(store, "buyee_yahoo:zzz", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    add_watch(store, "buyee_mercari:m1", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    add_watch(store, "mercari_tw:m2", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    b_ebay = batch_of("ebay:a", 4)
    b_yahoo = batch_of("buyee_yahoo:zzz", 4)
    b_mercari = batch_of("buyee_mercari:m1", 4)
    b_tw = batch_of("mercari_tw:m2", 4)

    due, skipped = due_sellers(store, PARAMS, b_ebay, now=now)
    assert "ebay:a" in [r["seller_key"] for r in due]

    store.mark_seller_watch_scanned("ebay:a", result="ok", now=now.isoformat())
    due, skipped = due_sellers(store, PARAMS, b_ebay, now=now + timedelta(minutes=5))
    assert "ebay:a" not in [r["seller_key"] for r in due]
    assert any("才掃過" in reason for _r, reason in skipped)

    # Yahoo 拍賣賣家頁已實作 → 必須進 due，不准再被記成「來源尚未支援」
    due, skipped = due_sellers(store, PARAMS, b_yahoo, now=now)
    assert "buyee_yahoo:zzz" in [r["seller_key"] for r in due]
    assert not any("Yahoo" in reason for _r, reason in skipped)

    # Buyee Mercari 鏡像賣家頁已實作（2026-08-09）→ 同樣必須進 due
    due, skipped = due_sellers(store, PARAMS, b_mercari, now=now)
    assert "buyee_mercari:m1" in [r["seller_key"] for r in due]
    assert not any(r["seller_key"] == "buyee_mercari:m1" for r, _reason in skipped)

    _due, skipped = due_sellers(store, PARAMS, b_tw, now=now)
    assert any(r["seller_key"] == "mercari_tw:m2" and "Mercari" in reason
               for r, reason in skipped)


# ---------------------------------------------------------------------------
# 3. 名單上限與淘汰
# ---------------------------------------------------------------------------
def _fill(store, n, *, source=SOURCE_AUTO, score=50.0, params=PARAMS):
    for i in range(n):
        add_watch(
            store, f"ebay:auto{i}", source=source,
            reason="t", params=params, score=score + i,
        )


def test_cap_is_enforced(store):
    _fill(store, 30)
    assert len(store.list_seller_watch()) == 30
    res = add_watch(store, "ebay:new", source=SOURCE_AUTO, reason="t",
                    params=PARAMS, score=10.0)
    assert not res.ok
    assert "已滿" in res.reason
    assert len(store.list_seller_watch()) == 30


def test_higher_scored_auto_evicts_the_lowest_auto(store):
    _fill(store, 30)          # 分數 50..79
    res = add_watch(store, "ebay:better", source=SOURCE_AUTO, reason="t",
                    params=PARAMS, score=99.0)
    assert res.ok
    assert res.evicted == "ebay:auto0"     # 最低分的那個
    keys = {r["seller_key"] for r in store.list_seller_watch()}
    assert "ebay:better" in keys and "ebay:auto0" not in keys
    assert len(keys) == 30
    gone = store.get_seller_watch("ebay:auto0")
    assert gone["active"] == 0 and "擠下" in gone["reason"]   # 為什麼被移除要留著


def test_manual_is_never_auto_evicted(store):
    add_watch(store, "ebay:mine", source=SOURCE_MANUAL, reason="使用者觀察到的",
              params=PARAMS)
    _fill(store, 29)
    res = add_watch(store, "ebay:super", source=SOURCE_AUTO, reason="t",
                    params=PARAMS, score=999.0)
    # 名單滿了，唯一比它低分的都是 auto，所以淘汰的一定不是 manual
    assert res.evicted != "ebay:mine"
    assert store.get_seller_watch("ebay:mine")["active"] == 1


def test_manual_add_when_full_is_rejected_with_a_hint(store):
    """滿了的時候手動加入**拒絕並說明要先移除誰**，不自動砍別人。"""
    _fill(store, 30)
    res = add_watch(store, "ebay:mine", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    assert not res.ok
    assert "已滿" in res.reason and "remove" in res.reason
    assert store.get_seller_watch("ebay:mine") is None


def test_manual_add_ignores_the_score_threshold_and_stores_no_score(store):
    """手動加入不受分數門檻限制；而且 score 是 None，**不是 0**。

    0 分在這個模組裡有明確語意（「比同儕貴、沒有 alpha」），拿它表示
    「還沒有證據」就是把兩種完全不同的狀態壓成同一個數字。
    """
    res = add_watch(store, "ebay:unknown", source=SOURCE_MANUAL, reason="使用者直覺",
                    params=WatchParams(auto_min_score=99), score=3.0)
    assert res.ok
    row = store.get_seller_watch("ebay:unknown")
    assert row["score"] is None
    assert row["source"] == SOURCE_MANUAL


def test_add_rejects_a_malformed_key(store):
    res = add_watch(store, "psa", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    assert not res.ok and "site" in res.reason


def test_sync_auto_watch_only_takes_scores_above_threshold(store):
    from ygo_sniper.seller_alpha import AlphaReport, SellerScore
    from ygo_sniper.seller_watch import sync_auto_watch

    rep = AlphaReport()
    rep.scores = {
        "ebay:good": SellerScore("ebay:good", True, "好", total=59.2),
        "ebay:meh": SellerScore("ebay:meh", True, "普", total=3.0),
        "ebay:none": SellerScore("ebay:none", False, "證據不足", total=None),
    }
    rep.metrics = {}

    class _M:
        site = "ebay"
        discount_ratio_median = 0.65

    rep.metrics = {k: _M() for k in rep.scores}
    out = sync_auto_watch(store, rep, WatchParams(auto_min_score=25))
    assert [a["seller_key"] for a in out["added"]] == ["ebay:good"]
    assert store.get_seller_watch("ebay:meh") is None


# ---------------------------------------------------------------------------
# 4-6. 推播規則 3
# ---------------------------------------------------------------------------
KEY = "ebay:9999"


def _signal_row(key=KEY, *, site="ebay", seller_id="psa"):
    return {
        "key": key,
        "title": "遊戯王 青眼の白龍 LOB-001 PSA 9",
        "url": "https://www.ebay.com/itm/9999",
        "landed_twd": 1800.0,
        "price_native": 50.0,
        "currency": "TWD",
        "route": "ebay_direct",
        "payload": json.dumps({"listing": {"site": site, "seller_id": seller_id}}),
    }


def _item(*, price=1000.0, peer_median=1500.0, tier=TIER_STRICT, peer_n=3):
    row = MarketRow(
        key=KEY, site="ebay", basis=BASIS_ASK, price_twd=price,
        title="青眼の白龍", seller_key="ebay:psa", card_name="青眼の白龍",
    )
    peer = PeerMatch(
        tier=tier, tier_label=TIER_LABEL[tier], peer_median_twd=peer_median,
        peer_n=peer_n, peer_sellers=2, peer_unknown_seller_n=0, sources=(),
    )
    item = SellerItem(row=row, peer=peer)
    item.ratio = price / peer_median
    return item


def _item_no_peer():
    """進了市場列、但同儕池裡配不到任何同款（`PeerIndex.match` 回 None）。"""
    row = MarketRow(
        key=KEY, site="ebay", basis=BASIS_ASK, price_twd=1000.0,
        title="青眼の白龍", seller_key="ebay:psa", card_name="青眼の白龍",
    )
    return SellerItem(row=row, peer=None)


def _ctx(*, watched=True, item=None, seen_count=1, score=None):
    ctx = SellerNotifyContext()
    if watched:
        ctx.watch = {"ebay:psa": {"seller_key": "ebay:psa", "source": SOURCE_MANUAL,
                                  "score": None, "batch": 0}}
    if item is not None:
        ctx.items = {KEY: item}
    ctx.obs = {KEY: {"key": KEY, "seen_count": seen_count}}
    if score is not None:
        ctx.scores = {"ebay:psa": score}
    return ctx


def _rules(**kw):
    return NotifyRules(
        auction_urgent_enabled=False, high_p_enabled=False,
        seller_new_enabled=True, **kw,
    )


def _run(ctx, rows=None, rules=None):
    return evaluate(rows or [_signal_row()], rules=rules or _rules(), seller_ctx=ctx)


def test_rule3_fires_when_all_three_conditions_hold():
    out = _run(_ctx(item=_item()))          # 便宜 33%
    assert len(out.seller_new) == 1
    m = out.seller_new[0]
    assert m.rule == RULE_SELLER_NEW
    assert m.seller_key == "ebay:psa"
    assert round(m.peer_discount_pct) == 33
    assert m.seller_score is None          # 手動加入、未達評分門檻
    assert m.peer_n == 3


def test_rule3_needs_the_seller_on_the_watchlist():
    out = _run(_ctx(watched=False, item=_item()))
    assert out.seller_new == []
    # 不在名單上的標的**不記 skipped**（全庫幾百筆，逐筆記只會洗版）
    assert out.skipped == []


def test_rule3_needs_a_new_listing():
    out = _run(_ctx(item=_item(), seen_count=5))
    assert out.seller_new == []


def test_rule3_needs_the_discount_threshold():
    out = _run(_ctx(item=_item(price=1400.0)))     # 只便宜 6.7% < 15%
    assert out.seller_new == []


def test_rule3_does_not_fire_when_peers_are_unavailable():
    """**紅線**：同儕算不出來一律不推，不用模型絕對值頂替。"""
    out = _run(_ctx(item=None))
    assert out.seller_new == []
    assert any("同儕算不出來" in s.reason for s in out.skipped)


def test_rule3_does_not_fire_on_a_non_scoring_tier():
    """只比得到「同稀有度×同分數」那一層＝量到卡種組合，不是賣家定價。"""
    out = _run(_ctx(item=_item(tier=TIER_STRATUM)))
    assert out.seller_new == []
    assert any("同儕算不出來" in s.reason for s in out.skipped)


def test_rule3_respects_min_peers():
    out = _run(_ctx(item=_item(peer_n=1)), rules=_rules(seller_min_peers=2))
    assert out.seller_new == []
    assert any("同儕只有 1 筆" in s.reason for s in out.skipped)


def test_rule3_is_deduped_forever_per_listing():
    ctx = _ctx(item=_item())
    out = evaluate(
        [_signal_row()], rules=_rules(), seller_ctx=ctx,
        notified={(KEY, RULE_SELLER_NEW): "2026-08-01T00:00:00+00:00"},
    )
    assert out.seller_new and out.to_send == [] and out.deduped == 1


def test_rule3_is_skipped_entirely_without_context():
    """脈絡建不起來 ≠ 名單是空的：`seller_ctx_ok=False` 要看得見。"""
    out = evaluate([_signal_row()], rules=_rules(), seller_ctx=None)
    assert out.seller_new == []
    assert out.seller_ctx_ok is False


def test_rule3_message_says_manual_when_there_is_no_score():
    from ygo_sniper.notify import format_seller_new

    out = _run(_ctx(item=_item()))
    text = format_seller_new(out.seller_new[0], "http://127.0.0.1:8321")
    assert "未達評分門檻" in text and "手動加入" in text
    assert "比同儕便宜" in text and "可比 3 筆" in text
    assert "到手 <b>NT$1,800</b>" in text


def test_rule3_message_shows_the_score_when_there_is_one():
    from ygo_sniper.notify import format_seller_new
    from ygo_sniper.seller_alpha import SellerScore

    score = SellerScore("ebay:psa", True, "比同儕便宜 35%", total=59.2,
                        caveats=["⚠️ 全部同儕只來自 1 個賣家"])
    ctx = _ctx(item=_item(), score=score)
    ctx.watch["ebay:psa"]["source"] = SOURCE_AUTO
    out = _run(ctx)
    text = format_seller_new(out.seller_new[0], "http://127.0.0.1:8321")
    assert "59.2" in text and "自動入選" in text
    assert "同儕只來自 1 個賣家" in text     # caveat 進本文，不是附註


def test_rule3_message_carries_the_seller_level_verdict():
    """賣家層級的判定必須跟這一筆的折價放在一起。

    2026-08-04 實測情境：`ebay:collectiblemore` 整體**比同儕貴**（0 分），
    卻上架了一件比同儕便宜 50% 的貨。只印「0 分」會被讀成工具壞了，
    只印「便宜 50%」會被讀成「這個賣家很便宜」——兩個數字必須同時在場。
    """
    from ygo_sniper.notify import format_seller_new
    from ygo_sniper.seller_alpha import SellerScore

    score = SellerScore("ebay:psa", True, "同儕相對中位 1.538× → **沒有 alpha**：比同儕貴 53.8%",
                        total=0.0)
    out = _run(_ctx(item=_item(), score=score))
    m = out.seller_new[0]
    assert m.seller_score == 0.0
    text = format_seller_new(m, "http://127.0.0.1:8321")
    assert "比同儕貴 53.8%" in text and "比同儕便宜 <b>33%</b>" in text


def test_rule3_peer_message_labels_the_strong_source():
    """同儕路徑必須自報「同儕相對（強）」。

    模型 fallback 進來之後，訊息上不寫來源的那一則會變成兩種宣稱共用一個版面
    ——使用者無從分辨手上這則是「別的賣家真的開這個價」還是「模型猜的」。
    """
    from ygo_sniper.notify import format_seller_new

    out = _run(_ctx(item=_item()))
    m = out.seller_new[0]
    assert m.judgement_source == SOURCE_PEER
    text = format_seller_new(m, DASH)
    assert "👤" in text and "同儕相對（強）" in text
    assert "模型" not in text.split("賣家層級")[0]


# ---------------------------------------------------------------------------
# 6b. 沒有同儕時的模型 fallback（2026-08-04）
# ---------------------------------------------------------------------------
DASH = "http://127.0.0.1:8321"


class _Est:
    """`valuation.Estimate` 裡規則 3／3b 真的會讀到的那幾欄。

    預設值刻意是「過得了 `EvidenceGate` 四道閘門」的一組：L1、分數已知、
    有效樣本 3 筆（`3-9` 桶，不是破口的 `10-49`）、校準殘差 147 筆（>50）。
    要測閘門就把對應那一欄改掉——這樣每個閘門測試改的東西正好是它擋的東西。
    """

    def __init__(self, *, fair=3000.0, level="L1", n_eff=3, grade=9.0,
                 cal_n=147, interval=True):
        self.fair_twd = fair
        self.lo_twd = 900.0 if interval else None
        self.hi_twd = 9000.0 if interval else None
        self.confidence = 0.80
        self.p_worth_buying = 0.55
        self.level = level
        self.level_label = "卡名×稀有度×分數"
        self.n_effective = n_eff
        self.grade = grade
        self.grade_source = "title"
        self.venue = "ebay"
        self.venue_adjusted = True
        self.venue_is_estimated = True
        self.calibration_group = f"{level}/n<3"
        self.calibration_group_n = cal_n
        self.calibration_group_requested = f"{level}/n<3"
        self.calibration_degraded = False

    @property
    def has_interval(self) -> bool:
        return self.lo_twd is not None and self.hi_twd is not None


@pytest.fixture
def stub_model(monkeypatch):
    """把估價打樁。回傳一支 setter：`stub_model(est)` 決定每一筆拿到什麼估計。

    ⚠️ 打樁的是 `ygo_sniper.valuation` 的模組屬性，而 `_model_fallback` 是在
    **呼叫當下**才 import 它們——所以這裡換得掉。
    """
    from ygo_sniper import valuation as val_mod

    state = {"est": _Est()}

    def _set(est):
        state["est"] = est
        return object()          # 當作 valuator：真正的模型被打樁掉了

    monkeypatch.setattr(val_mod, "estimate_signal_row", lambda _v, _r: state["est"])
    monkeypatch.setattr(
        val_mod, "card_attrs_from_row", lambda _v, _r: ("青眼の白龍", "ultra", 9.0)
    )
    return _set


def _run_model(ctx, rows=None, rules=None, valuator=None):
    return evaluate(
        rows or [_signal_row()], rules=rules or _rules(),
        seller_ctx=ctx, valuator=valuator,
    )


def test_model_fallback_fires_when_there_are_no_peers(stub_model):
    """使用者要求：「如果沒有同儕，請幫我使用模型的數字替代，讓我做人工審核。」

    到手 1,800 vs 模型公允價 3,000 ＝ 便宜 40% ≥ 25% 門檻。
    """
    val = stub_model(_Est(fair=3000.0))
    out = _run_model(_ctx(item=None), valuator=val)
    assert len(out.seller_new) == 1 and out.seller_unpriced == []
    m = out.seller_new[0]
    assert m.rule == RULE_SELLER_NEW and m.judgement_source == SOURCE_MODEL
    assert round(m.model_discount_pct) == 40
    assert m.peer_discount_pct is None          # 沒有同儕就不准有同儕折價
    assert "沒有進同儕比對" in (m.peer_absent_reason or "")


def test_model_fallback_says_which_kind_of_peer_gap_it_is(stub_model):
    """「為什麼沒有同儕」有四種，措辭不能壓成一句——使用者要靠它判斷弱在哪裡。"""
    val = stub_model(_Est(fair=3000.0))
    cases = [
        (None, "沒有進同儕比對"),                     # 根本沒進市場列
        (_item_no_peer(), "同儕池裡找不到"),          # 進了，但配不到任何同儕
        (_item(tier=TIER_STRATUM), "不計分"),         # 只比得到卡種混合那一層
    ]
    for item, expect in cases:
        out = _run_model(_ctx(item=item), valuator=val)
        assert expect in (out.seller_new[0].peer_absent_reason or ""), expect


def test_model_fallback_threshold_is_stricter_than_peers(stub_model):
    """模型門檻 25% > 同儕 15%：便宜 20% 的那一筆，同儕會推、模型不推。"""
    val = stub_model(_Est(fair=2250.0))          # 1800/2250 → 便宜 20%
    out = _run_model(_ctx(item=None), valuator=val)
    assert out.seller_new == [] and out.seller_unpriced == []
    # 沒過門檻是「有判斷、判斷是不夠便宜」，不該進 skipped 洗版
    assert out.skipped == []


@pytest.mark.parametrize(
    ("est", "expect"),
    [
        (_Est(level="L3"), "估價層級"),
        (_Est(grade=None), "鑑定分數"),
        (_Est(cal_n=10), "校準殘差"),            # 10 < 通知檔門檻 30，仍擋
    ],
)
def test_evidence_gate_blocks_the_model_fallback(stub_model, est, expect):
    """閘門擋下的**仍然不推折價**——改走規則 3b，而且說得出被哪一道擋下。

    這是「模型 fallback 不是什麼都推」的結構性保證：閘門引用的是
    `bidding.EvidenceGate` 的**通知檔**（notify profile）——語意閘門（分數已知、
    L1/L2）與出價檔一樣硬，校準殘差門檻是通知檔自己的 30（出價檔是 50）。
    2026-08-07 之前這裡直接用出價檔，把全庫證據最強的標的（青眼 L1 n=25…）
    連同真正估不了的一起丟進 3b——用出價的閘門擋通知，等於把最好看的標的
    靜默丟掉（誤殺是靜默的）。破口桶案例移到下面的檔位測試（通知檔放行＋標註）。
    """
    val = stub_model(est)
    out = _run_model(_ctx(item=None), valuator=val)
    assert out.seller_new == []                  # 沒有折價數字
    assert len(out.seller_unpriced) == 1
    assert expect in (out.seller_unpriced[0].unpriced_reason or "")


# ---------------------------------------------------------------------------
# 6c. 證據閘門的通知檔位（2026-08-07）
#
# 通知與出價的錯誤代價不對稱：通知錯了使用者自己看一眼就知道，出價上限錯了
# 會花錯真錢。所以規則 3 的模型 fallback 走**通知檔**：破口桶不拒收、校準殘差
# 門檻 30——但「出價檔會拒」必須跟著訊息一起送到使用者眼前（放寬的必須看得見）。
# ---------------------------------------------------------------------------
def test_notify_profile_rescues_the_broken_bucket_with_a_visible_marker(stub_model):
    """W3：`10-49` 桶在通知檔放行——結果物件必須帶「出價檔會拒」的標記。"""
    val = stub_model(_Est(fair=3000.0, n_eff=20))       # 10-49 破口桶
    out = _run_model(_ctx(item=None), valuator=val)
    assert len(out.seller_new) == 1 and out.seller_unpriced == []
    m = out.seller_new[0]
    assert m.judgement_source == SOURCE_MODEL
    assert m.bidding_reject_note is not None
    assert "10-49" in m.bidding_reject_note


def test_notify_profile_lowers_the_calibration_floor_to_30(stub_model):
    """W5：殘差 30 在出價檔被 50 擋住，在通知檔剛好過（且帶標記）；29 仍走 3b。"""
    val = stub_model(_Est(fair=3000.0, cal_n=30))
    out = _run_model(_ctx(item=None), valuator=val)
    assert len(out.seller_new) == 1
    m = out.seller_new[0]
    assert m.bidding_reject_note is not None and "30" in m.bidding_reject_note

    val29 = stub_model(_Est(fair=3000.0, cal_n=29))
    out29 = _run_model(_ctx(item=None), valuator=val29)
    assert out29.seller_new == [] and len(out29.seller_unpriced) == 1


def test_no_marker_when_bidding_would_also_accept(stub_model):
    """出價檔也會收的估價**不准**掛標記——狼來了的 caveat 等於沒有 caveat。"""
    val = stub_model(_Est(fair=3000.0))          # 3-9 桶、殘差 147：兩檔都過
    out = _run_model(_ctx(item=None), valuator=val)
    assert len(out.seller_new) == 1
    assert out.seller_new[0].bidding_reject_note is None


def test_relaxed_message_carries_the_caveat_and_never_a_ceiling(stub_model):
    """訊息三件事：⚠️ caveat 講出出價檔會拒、🤖 弱路徑標記維持、
    **絕不包含出價上限數字**（通知給的是合理價＋折價，不是「你可以出到多少」）。"""
    from ygo_sniper.notify import format_seller_new

    val = stub_model(_Est(fair=3000.0, n_eff=20))
    out = _run_model(_ctx(item=None), valuator=val)
    m = out.seller_new[0]
    text = format_seller_new(m, DASH)
    assert "🤖" in text and "模型估值（弱）" in text     # 既有弱路徑標記維持
    assert "⚠️" in text and "不是出價依據" in text
    assert "出價拒絕桶" in text and "10-49" in text
    assert "上限" not in text and "出價欄" not in text   # 不給任何出價上限
    assert m.max_bid is None and m.max_bid_native is None


def test_strict_pass_message_has_no_bidding_caveat(stub_model):
    from ygo_sniper.notify import format_seller_new

    val = stub_model(_Est(fair=3000.0))
    out = _run_model(_ctx(item=None), valuator=val)
    text = format_seller_new(out.seller_new[0], DASH)
    assert "不是出價依據" not in text and "出價拒絕桶" not in text


def test_notify_rules_wire_the_notify_profile_and_keep_the_bidding_reference(cfg):
    """`rules.gate` 是通知檔、`rules.bidding_gate` 是出價檔——兩份都從 config 來。

    後者的存在理由：跨檔位對照（「出價檔會拒」的標記）必須拿**真的**出價檔
    來比，不能在 notify 這一側自己另拍一份門檻（工程原則 1：同源）。
    """
    rules = NotifyRules.from_config(cfg)
    assert rules.gate.reject_n_buckets == ()
    assert rules.gate.min_calibration_samples == 30
    assert rules.bidding_gate.reject_n_buckets == ("10-49",)
    assert rules.bidding_gate.min_calibration_samples == 50
    # 語意閘門兩檔一致地硬
    assert rules.gate.require_known_grade and rules.gate.require_card_specific_level


def test_model_fallback_needs_an_interval(stub_model):
    val = stub_model(_Est(interval=False))
    out = _run_model(_ctx(item=None), valuator=val)
    assert out.seller_new == [] and len(out.seller_unpriced) == 1


def test_model_fallback_can_be_turned_off(stub_model):
    val = stub_model(_Est(fair=9999.0))
    out = _run_model(
        _ctx(item=None), rules=_rules(seller_model_fallback_enabled=False), valuator=val
    )
    assert out.seller_new == [] and out.seller_unpriced == []
    assert any("模型 fallback 已關閉" in s.reason for s in out.skipped)


def test_model_fallback_message_marks_the_weak_source(stub_model):
    """訊息必須一眼分得出「這是模型猜的」，而且講得出為什麼沒有同儕。"""
    from ygo_sniper.notify import format_seller_new

    val = stub_model(_Est(fair=3000.0))
    out = _run_model(_ctx(item=None), valuator=val)
    text = format_seller_new(out.seller_new[0], DASH)
    assert "🤖" in text and "模型估值（弱）" in text
    assert "沒有同儕可比" in text and "沒有進同儕比對" in text
    assert "比模型公允價便宜 <b>40%</b>" in text
    assert "不進 Seller Alpha" in text and "人工複核" in text
    assert "比同儕便宜" not in text               # 不准借用同儕的措辭


# ---------------------------------------------------------------------------
# 8. 紅線：模型絕不回饋到 Seller Alpha 分數
# ---------------------------------------------------------------------------
def test_notify_context_never_hands_the_valuator_to_seller_alpha(store, monkeypatch):
    """**結構性的紅線**：規則 3 的脈絡永遠用不帶模型的 `analyze`。

    第二棒實測：模型絕對值會把 `ebay:collectiblemore` 從「比同儕貴 54%」
    翻成「便宜 43%」——符號相反。只要 valuator 有機會進到 `analyze`，
    那個數字就可能沿著 `SellerItem.model_ratio` 爬進分數。這條測試把
    「沒有那個機會」釘在呼叫點上。
    """
    from ygo_sniper import seller_alpha as alpha_mod
    from ygo_sniper.seller_watch import add_watch, build_notify_context

    add_watch(store, "ebay:psa", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    seen: dict = {}

    def _fake_analyze(_store, **kw):
        seen.update(kw)
        return alpha_mod.AlphaReport()

    monkeypatch.setattr(alpha_mod, "analyze", _fake_analyze)
    build_notify_context(store, None)
    assert seen.get("valuator") is None
    assert "valuator" not in seen or seen["valuator"] is None


def test_model_fallback_does_not_change_the_seller_score(stub_model):
    """同一個賣家、同一份分數：模型 fallback 命中不會動到分數或它的判定原文。

    這是行為面的鏡像測試——上面那條釘呼叫點，這條釘輸出。
    """
    from ygo_sniper.seller_alpha import SellerScore

    score = SellerScore("ebay:psa", True, "同儕相對中位 1.538× → 比同儕貴 53.8%", total=0.0)
    val = stub_model(_Est(fair=3000.0))
    out = _run_model(_ctx(item=None, score=score), valuator=val)
    m = out.seller_new[0]
    assert m.judgement_source == SOURCE_MODEL
    assert m.seller_score == 0.0                       # 分數原封不動
    assert m.seller_score_note == "同儕相對中位 1.538× → 比同儕貴 53.8%"
    assert score.total == 0.0 and score.ok is True     # 分數物件本身沒被改寫


# ---------------------------------------------------------------------------
# 7. 規則 3b：估不了的稀有品要浮出來，但音量要被限制
# ---------------------------------------------------------------------------
def _unpriced_ctx(n: int):
    ctx = SellerNotifyContext()
    ctx.watch = {"ebay:psa": {"seller_key": "ebay:psa", "source": SOURCE_MANUAL,
                              "score": None, "batch": 0}}
    ctx.obs = {f"ebay:{i}": {"key": f"ebay:{i}", "seen_count": 1} for i in range(n)}
    return ctx


def _unpriced_rows(n: int):
    rows = []
    for i in range(n):
        r = _signal_row(key=f"ebay:{i}")
        r["landed_twd"] = 1000.0 * (i + 1)     # i 越大越貴
        rows.append(r)
    return rows


def test_unpriced_has_its_own_cap_and_keeps_the_priciest(stub_model):
    """音量控制：每輪上限 ＋ 超量的保留到手成本最高的幾筆。

    估不了的東西無從比較，唯一排得出來的是「漏掉的代價」——所以留貴的。
    超量的那幾筆記進 skipped（preview 看得見），不進 overflow：overflow 的
    文案說「下一輪會繼續排隊」，而 3b 下一輪就不是新標的了，印那句話是騙人。
    """
    val = stub_model(_Est(level="L3"))         # 全部被閘門擋下 → 全部走 3b
    out = evaluate(
        _unpriced_rows(5), rules=_rules(seller_unpriced_max_per_run=2),
        seller_ctx=_unpriced_ctx(5), valuator=val,
    )
    assert len(out.seller_unpriced) == 5
    sent = [m for m in out.to_send if m.rule == RULE_SELLER_UNPRICED]
    assert [m.key for m in sent] == ["ebay:4", "ebay:3"]
    assert out.overflow == []
    assert len(out.skips_for("已達上限 2 則")) == 3


def test_unpriced_can_be_turned_off(stub_model):
    val = stub_model(_Est(level="L3"))
    out = _run_model(
        _ctx(item=None), rules=_rules(seller_unpriced_enabled=False), valuator=val
    )
    assert out.seller_unpriced == []
    assert any("規則 3b 已關閉" in s.reason for s in out.skipped)


def test_unpriced_keeps_a_separate_dedupe_ledger(stub_model):
    """3b 的去重帳與規則 3 分開：今天估不了、下週估得出來時要能再送一次。"""
    val = stub_model(_Est(level="L3"))
    ctx = _ctx(item=None)
    out = evaluate(
        [_signal_row()], rules=_rules(), seller_ctx=ctx, valuator=val,
        notified={(KEY, RULE_SELLER_NEW): "2026-08-01T00:00:00+00:00"},
    )
    # 規則 3 送過不影響 3b（不同的 rule 鍵）
    assert [m.rule for m in out.to_send] == [RULE_SELLER_UNPRICED]

    out2 = evaluate(
        [_signal_row()], rules=_rules(), seller_ctx=ctx, valuator=val,
        notified={(KEY, RULE_SELLER_UNPRICED): "2026-08-01T00:00:00+00:00"},
    )
    assert out2.seller_unpriced and out2.to_send == [] and out2.deduped == 1


def test_unpriced_message_does_not_claim_it_is_cheap(stub_model):
    """3b 唯一的宣稱是「有這麼一件我們估不了的東西」——不准長得像撿漏通知。"""
    from ygo_sniper.notify import format_seller_unpriced

    val = stub_model(_Est(level="L3"))
    out = _run_model(_ctx(item=None), valuator=val)
    text = format_seller_unpriced(out.seller_unpriced[0], DASH)
    assert "🔍" in text and "估不了" in text
    assert "不是撿漏通知" in text
    assert "估價層級" in text                    # 為什麼估不了
    assert "卡名 青眼の白龍" in text and "稀有度 ultra" in text and "分數 9" in text
    assert "到手 <b>NT$1,800</b>" in text
    # 沒有任何折價宣稱：兩種折價的措辭都不准出現
    assert "比模型公允價便宜" not in text and "比同儕便宜" not in text


# ---------------------------------------------------------------------------
# 7b. 變體卡：模型估值不適用（使用者舉的 missing foil error 例子）
# ---------------------------------------------------------------------------
def test_variant_title_goes_to_unpriced_instead_of_a_misleading_discount(stub_model):
    """變體卡的卡名比對會配到**母卡**，模型會用正常版的行情估它。

    方向是**低估變體的價值**（變體通常貴得多），外顯成「太貴、不值得」——
    正好會讓使用者錯過夢幻逸品。所以有變體線索時不給折價數字，改走 3b。
    """
    val = stub_model(_Est(fair=99999.0))         # 折價爆表，仍然不准送折價
    row = _signal_row()
    row["title"] = "【唯一無二個体】印刷error Vol.3 真紅眼の黒竜 UR PSA7 鑑定品"
    out = _run_model(_ctx(item=None), rows=[row], valuator=val)
    assert out.seller_new == []
    m = out.seller_unpriced[0]
    assert m.variant_hits == ("error",)
    assert "變體" in (m.unpriced_reason or "") and "母卡" in (m.unpriced_reason or "")


def test_variant_words_do_not_match_lookalikes():
    """實測反例（本庫真的有這些標題）：純子字串比對會把正常卡判成變體。

    - `Terrorking Archfiend` 裡有 `error`（signals 1 筆）
    - `コレクターズレア`／`レッドアイズレリーフ` 裡有 `ズレ`（comps 2 筆）
    - `レリーフ` 是正規稀有度（アルティメットレア，comps 361 筆），不是變體
    """
    assert variant_hits("Yugioh Terrorking Archfiend DCR-072 PSA 8") == ()
    assert variant_hits("遊戯王 灰流うらら コレクターズレア 絵違い psa9") == ()
    assert variant_hits("遊戯王 初期PSA8真紅眼の黒竜レッドアイズレリーフ") == ()
    assert variant_hits("【PSA8】遊戯王 サイバーバリアドラゴン 旧レリーフ") == ()
    # 真的該抓到的
    assert variant_hits("印刷error Vol.3 真紅眼の黒竜") == ("error",)
    assert variant_hits("世界に1枚 遊戯王 風の精霊 エラー品 初期 ノーマル") == ("エラー",)
    assert variant_hits("YUGIOH BLUE EYES missing foil error PSA 9") == (
        "error", "missing", "missing foil",
    )
    assert variant_hits("遊戯王 印刷ズレ 初期") == ("印刷ズレ",)


def test_variant_clue_is_flagged_even_on_the_peer_path():
    """同儕也是用卡名配的——配到的一樣可能是母卡。

    同儕證據比模型強，所以**不改判定**（照樣推），但前提要講出來。
    """
    from ygo_sniper.notify import format_seller_new

    row = _signal_row()
    row["title"] = "遊戯王 青眼の白龍 印刷ミス LOB-001 PSA 9"
    out = _run(_ctx(item=_item()), rows=[row])
    m = out.seller_new[0]
    assert m.judgement_source == SOURCE_PEER
    assert m.variant_hits == ("印刷ミス",)
    assert "變體線索" in format_seller_new(m, DASH)


# ---------------------------------------------------------------------------
# 7c. 賣家頁批次不得參與離場判定
# ---------------------------------------------------------------------------
def test_seller_page_batch_does_not_drive_exit_judgement(store):
    """賣家頁只看得到一個賣家的貨——拿它當地平線會把整個站判成消失。"""
    def row(key, seller):
        return {"key": key, "source": "ebay", "site": "ebay", "title": key,
                "url": f"https://e/{key}", "price_native": 10.0, "currency": "TWD",
                "price_twd": 300.0, "landed_twd": 320.0, "rarity": None,
                "grader": "none", "grade": None, "card_name": None,
                "era_evidence": "初期", "price_kind": "fixed", "seller_id": seller}

    store.record_listing_scan([
        {"source": "ebay", "site": "ebay", "healthy": True,
         "rows": [row("ebay:1", "alice"), row("ebay:2", "bob")]}
    ])
    # 只有賣家頁那一批（alice），bob 的標的不在裡面
    report = store.record_listing_scan([
        {"source": "ebay", "site": "ebay", "healthy": True, "exit_scope": False,
         "rows": [row("ebay:1", "alice")]}
    ])
    assert report["disappeared"] == 0 and report["window_exit"] == 0
    rows = {r["key"]: r for r in store.listing_obs()}
    assert rows["ebay:2"]["disappeared_at"] is None


# ---------------------------------------------------------------------------
# 9. 雙軌入選（Alpha ∪ Supply Fit）
#
# 雞生蛋：Alpha 幾乎只能從成交價算出來（實測可比 sold 439 筆 vs ask 24 筆），
# 而在架帳要變厚只能靠密集掃賣家庫存——那正是監控名單在做的事。supply 軌
# 用「值不值得盯」入選來打破它。
#
# **淘汰只在同軌內比分數**：Alpha 的 25 分與 Supply 的 70 分是兩把不同的尺，
# 拿來比大小沒有意義（CLAUDE.md 第三節），而且錯的方向是靜默的——名單看起來
# 運作正常，只是踢錯人。
# ---------------------------------------------------------------------------
from ygo_sniper.seller_alpha import AlphaReport, SellerMetrics, SellerScore  # noqa: E402
from ygo_sniper.seller_supply import SupplyFit  # noqa: E402
from ygo_sniper.seller_watch import SOURCE_SUPPLY, sync_auto_watch  # noqa: E402

CHEAP = "buyee_yahoo:cheap"
BIG = "buyee_yahoo:bigsupply"


def _two_track_report():
    """cheap 有 Alpha 沒 Supply、bigsupply 有 Supply 沒 Alpha——兩軌各一個代表。"""
    scores = {
        CHEAP: SellerScore(seller_key=CHEAP, ok=True, reason="同儕相對便宜", total=40.0),
        BIG: SellerScore(seller_key=BIG, ok=False, reason="證據不足", total=None),
    }
    metrics = {k: SellerMetrics(seller_key=k, site="buyee_yahoo", seller_id=k.split(":")[1])
               for k in scores}
    report = AlphaReport(metrics=metrics, scores=scores)
    supply = {
        CHEAP: SupplyFit(seller_key=CHEAP, site="buyee_yahoo", ok=False,
                         reason="只有 1 個維度算得出來", total=None),
        BIG: SupplyFit(seller_key=BIG, site="buyee_yahoo", ok=True,
                       reason="", total=70.0, n_dimensions_used=3),
    }
    return report, supply


def test_seller_with_no_alpha_but_high_supply_fit_gets_watched(store):
    """打破雞生蛋：Alpha 算不出來的高供給賣家必須進得了名單。"""
    report, supply = _two_track_report()
    result = sync_auto_watch(store, report, WatchParams(), supply=supply)
    assert BIG in [a["seller_key"] for a in result["added"]]
    row = store.get_seller_watch(BIG)
    assert row["source"] == SOURCE_SUPPLY
    assert "供給" in row["reason"]


def test_watch_reason_distinguishes_the_two_tracks(store):
    """名單裡必須看得出誰是「便宜」進來的、誰是「值得盯」進來的——
    否則使用者會以為名單上每個都是便宜賣家。"""
    report, supply = _two_track_report()
    sync_auto_watch(store, report, WatchParams(), supply=supply)
    assert store.get_seller_watch(CHEAP)["source"] == SOURCE_AUTO
    assert "Alpha" in store.get_seller_watch(CHEAP)["reason"]
    assert store.get_seller_watch(BIG)["source"] == SOURCE_SUPPLY
    assert "供給" in store.get_seller_watch(BIG)["reason"]


def test_supply_entry_never_evicts_an_alpha_entry(store):
    """Supply 的 70 分與 Alpha 的 25 分是兩把不同的尺，比大小沒有意義。
    位子不夠時，假設（supply）不得擠掉實證（alpha）。"""
    params = WatchParams(max_sellers=1)
    add_watch(store, CHEAP, source=SOURCE_AUTO, reason="Alpha 25.0 分",
              params=params, score=25.0)
    res = add_watch(store, BIG, source=SOURCE_SUPPLY, reason="供給 70.0 分",
                    params=params, score=70.0)
    assert res.ok is False              # 被拒絕，不是擠掉 alpha
    assert res.evicted is None
    assert store.get_seller_watch(CHEAP)["active"] == 1


def test_alpha_entry_evicts_a_supply_entry_without_comparing_scores(store):
    """反向：實證可以擠掉假設，而且**不比分數**（不同尺）——
    Alpha 25 分擠得掉 Supply 90 分。"""
    params = WatchParams(max_sellers=1)
    add_watch(store, BIG, source=SOURCE_SUPPLY, reason="供給 90.0 分",
              params=params, score=90.0)
    res = add_watch(store, CHEAP, source=SOURCE_AUTO, reason="Alpha 25.0 分",
                    params=params, score=25.0)
    assert res.ok is True
    assert res.evicted == BIG
    assert store.get_seller_watch(BIG)["active"] == 0


def test_supply_entry_evicts_only_lower_scoring_supply(store):
    """同軌內才比分數。"""
    params = WatchParams(max_sellers=1)
    add_watch(store, "buyee_yahoo:low", source=SOURCE_SUPPLY, reason="供給 30.0 分",
              params=params, score=30.0)
    res = add_watch(store, BIG, source=SOURCE_SUPPLY, reason="供給 70.0 分",
                    params=params, score=70.0)
    assert res.ok is True and res.evicted == "buyee_yahoo:low"


def test_manual_is_still_never_evicted_by_either_track(store):
    """既有紅線不得被新軌道破壞。"""
    params = WatchParams(max_sellers=1)
    add_watch(store, "ebay:psa", source=SOURCE_MANUAL, reason="使用者指定", params=params)
    for src, sc in ((SOURCE_AUTO, 99.0), (SOURCE_SUPPLY, 99.0)):
        res = add_watch(store, f"ebay:x{src}", source=src, reason="x",
                        params=params, score=sc)
        assert res.ok is False
        assert store.get_seller_watch("ebay:psa")["active"] == 1


def test_supply_track_respects_its_own_threshold(store):
    """低於 supply 門檻的不入選，而且不會被 alpha 門檻誤判。"""
    report, supply = _two_track_report()
    params = WatchParams(supply_min_score=80.0)      # BIG 是 70 分，不該入選
    result = sync_auto_watch(store, report, params, supply=supply)
    assert BIG not in [a["seller_key"] for a in result["added"]]


def test_sync_auto_watch_without_supply_argument_still_works(store):
    """向後相容：既有呼叫端沒傳 supply 時行為不變。"""
    report, _ = _two_track_report()
    result = sync_auto_watch(store, report, WatchParams())
    assert CHEAP in [a["seller_key"] for a in result["added"]]
    assert BIG not in [a["seller_key"] for a in result["added"]]


# ---------------------------------------------------------------------------
# 10. 拒絕訊息：預期內的摘要、非預期的照吼（2026-08-05）
#
# 兩個相反的失敗方向，這一節同時釘住：
#   洗版 —— 排程一天 15 次、每輪 50 行 `[warn] 名單已滿`，真正的告警被淹死。
#   吞掉 —— 為了不洗版而把 rejected 整個不印，就變成 CLAUDE.md 第五節的
#           頭號敵人（「賣家鍵組錯了」與「今天沒人過門檻」外顯一模一樣）。
# 分類依據是 `code` 不是 `reason` 字串：訊息改一個字就讓字串比對失效，而
# 失效的方向會是「非預期被當成預期吞掉」——所以未知 code 一律歸非預期。
# ---------------------------------------------------------------------------
from ygo_sniper.seller_watch import (  # noqa: E402
    EXPECTED_REJECT_CODES,
    REJECT_LIST_FULL,
    REJECT_MALFORMED_KEY,
    summarize_rejections,
)


def test_list_full_rejection_carries_the_expected_code(store):
    """`add_watch` 必須帶出 code——摘要分類完全靠它，斷了就整條鏈失效。"""
    params = WatchParams(max_sellers=1)
    add_watch(store, "ebay:psa", source=SOURCE_MANUAL, reason="使用者指定", params=params)
    res = add_watch(store, "ebay:other", source=SOURCE_SUPPLY, reason="供給",
                    params=params, score=90.0)
    assert res.ok is False
    assert res.code == REJECT_LIST_FULL
    assert REJECT_LIST_FULL in EXPECTED_REJECT_CODES


def test_malformed_key_rejection_carries_its_own_code(store):
    res = add_watch(store, "psa", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    assert res.ok is False
    assert res.code == REJECT_MALFORMED_KEY
    assert REJECT_MALFORMED_KEY not in EXPECTED_REJECT_CODES   # 非預期＝要吼


def test_sync_auto_watch_puts_the_code_on_every_rejected_row(store):
    """端到端：pipeline 拿到的 rejected 列必須帶 code（沒有 code 會被當非預期）。"""
    report, supply = _two_track_report()
    params = WatchParams(max_sellers=1, supply_min_score=60.0)
    out = sync_auto_watch(store, report, params, supply=supply)
    assert out["rejected"], "名單只有 1 個位子，supply 候選人應該被擋下"
    assert all(r["code"] == REJECT_LIST_FULL for r in out["rejected"])


def test_many_expected_rejections_collapse_to_at_most_two_lines():
    """50 個「名單已滿」只准變成兩行——這就是洗版的修法。"""
    rejected = [
        {"seller_key": f"buyee_yahoo:s{i}", "reason": "監控名單已滿（30/30）且沒有可淘汰的對象：…",
         "track": "supply", "code": REJECT_LIST_FULL}
        for i in range(50)
    ]
    digest = summarize_rejections(rejected)
    assert digest.total == 50
    assert digest.n_expected == 50 and digest.n_unexpected == 0
    assert len(digest.summary_lines) <= 2
    assert digest.alert_lines == []


def test_summary_names_the_total_the_top_reason_and_its_count():
    """摘要不准只寫「擋下 N 個」——要說得出最常見原因與它佔幾個，否則沒有診斷力。"""
    rejected = [
        {"seller_key": f"ebay:s{i}", "reason": "名單已滿…", "code": REJECT_LIST_FULL}
        for i in range(7)
    ]
    digest = summarize_rejections(rejected)
    head = digest.summary_lines[0]
    assert "7" in head                      # 總數
    assert "名單已滿" in head                # 最常見原因
    # 代表例保留完整脈絡（賣家鍵），不是只有一個數字
    assert any("ebay:s0" in line for line in digest.summary_lines)


def test_unexpected_rejection_is_alerted_individually_and_never_truncated():
    """1 個賣家鍵格式錯誤混在 50 個「名單已滿」裡，也必須單獨、全文印出來。"""
    long_reason = "賣家鍵格式應為 `{site}:{seller_id}`（例：ebay:psa），收到 " + "x" * 400
    rejected = [
        {"seller_key": f"ebay:s{i}", "reason": "名單已滿…", "code": REJECT_LIST_FULL}
        for i in range(50)
    ] + [{"seller_key": "brokenkey", "reason": long_reason, "code": REJECT_MALFORMED_KEY}]
    digest = summarize_rejections(rejected)

    assert digest.n_unexpected == 1
    assert len(digest.alert_lines) == 1
    line = digest.alert_lines[0]
    assert "brokenkey" in line
    assert long_reason in line              # **不截斷**：非預期的要看得到全文
    # 而且不准混進摘要裡被稀釋掉
    assert "brokenkey" not in " ".join(digest.summary_lines)


def test_unknown_or_missing_reject_code_defaults_to_loud():
    """未來新增的拒絕路徑忘了分類時，預設要吵不要安靜——安靜的預設會讓
    一個全新的失敗模式從第一天起就看不見。"""
    digest = summarize_rejections([
        {"seller_key": "ebay:a", "reason": "某種新的拒絕", "code": "brand_new_reason"},
        {"seller_key": "ebay:b", "reason": "沒有 code 的舊格式"},
    ])
    assert digest.n_expected == 0
    assert digest.n_unexpected == 2
    assert len(digest.alert_lines) == 2
    assert all("ebay:" in line for line in digest.alert_lines)


def test_no_rejections_prints_nothing():
    """沒有人被擋下時不要憑空生一行雜訊。"""
    for empty in ([], None):
        digest = summarize_rejections(empty)
        assert digest.total == 0
        assert digest.summary_lines == [] and digest.alert_lines == []


# ---------------------------------------------------------------------------
# 10. 釘選軌（pinned）：使用者貼 URL 明講要追蹤的賣家（2026-08-09）
#
# 三個結構性承諾，每一條各自釘死：不佔 30 名額、永不被自動淘汰、
# 進每一批輪替（＝每個輪替時段掃一次，比其他軌快 4 倍）。
# ---------------------------------------------------------------------------
from ygo_sniper.seller_watch import SOURCE_PINNED  # noqa: E402


def test_pinned_ignores_the_cap(store):
    """名單滿 30 之後釘選照樣進得去——使用者明講要追蹤 > 名額規則。"""
    _fill(store, 30)
    res = add_watch(store, "ebay:pinme", source=SOURCE_PINNED,
                    reason="使用者釘選", params=PARAMS)
    assert res.ok and not res.already
    assert res.evicted is None                       # 也沒有擠掉任何人
    row = store.get_seller_watch("ebay:pinme")
    assert row["active"] == 1 and row["source"] == SOURCE_PINNED
    assert row["score"] is None                      # 釘選不假裝有分數
    assert len(store.list_seller_watch()) == 31      # 30 名額 + 1 釘選


def test_pinned_does_not_eat_quota(store):
    """先釘 3 個，30 個 auto 名額必須**一個都不少**；名額競爭照常運作。"""
    for i in range(3):
        add_watch(store, f"ebay:pin{i}", source=SOURCE_PINNED,
                  reason="使用者釘選", params=PARAMS)
    _fill(store, 30)                                 # 分數 50..79
    active = store.list_seller_watch()
    assert len(active) == 33                         # 3 釘選 + 30 auto，無人被拒
    # 第 31 個 auto 進來：名額邏輯照常（擠掉最低分 auto），與釘選無關
    res = add_watch(store, "ebay:better", source=SOURCE_AUTO, reason="t",
                    params=PARAMS, score=999.0)
    assert res.ok and res.evicted == "ebay:auto0"
    # 低分 auto 候選則照常被拒，拒絕訊息裡的名額計數不含釘選（30/30 不是 33/30）
    res = add_watch(store, "ebay:worse", source=SOURCE_AUTO, reason="t",
                    params=PARAMS, score=1.0)
    assert not res.ok and "30/30" in res.reason


def test_pinned_is_never_evicted(store):
    """任何軌、任何分數的候選人都碰不到釘選列。"""
    params = WatchParams(max_sellers=1)
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="使用者釘選",
              params=params)
    add_watch(store, "buyee_yahoo:sup", source=SOURCE_SUPPLY, reason="供給",
              params=params, score=10.0)
    # auto 高分候選：唯一合法 victim 是 supply，絕不是 pinned
    res = add_watch(store, "ebay:hot", source=SOURCE_AUTO, reason="t",
                    params=params, score=999.0)
    assert res.ok and res.evicted == "buyee_yahoo:sup"
    assert store.get_seller_watch("ebay:pinme")["active"] == 1
    # 名額被 auto 佔滿後，supply 候選找不到 victim → 拒絕，而不是動釘選
    res = add_watch(store, "ebay:sup2", source=SOURCE_SUPPLY, reason="t",
                    params=params, score=999.0)
    assert not res.ok
    assert store.get_seller_watch("ebay:pinme")["active"] == 1


def test_pinning_an_existing_auto_upgrades_it(store):
    """已在名單上（演算法選的）再 pin ＝ 升級成釘選：
    使用者明講要追蹤 > 演算法入選。批次用 batch_of 重算＝不變。"""
    add_watch(store, "ebay:seller1", source=SOURCE_AUTO, reason="自動入選",
              params=PARAMS, score=55.0)
    before_batch = store.get_seller_watch("ebay:seller1")["batch"]
    res = add_watch(store, "ebay:seller1", source=SOURCE_PINNED,
                    reason="使用者釘選", params=PARAMS)
    assert res.ok and res.already                    # 是升級，不是新增
    row = store.get_seller_watch("ebay:seller1")
    assert row["source"] == SOURCE_PINNED
    assert row["score"] is None                      # 升級後不保留舊軌的分數
    assert row["reason"] == "使用者釘選"
    assert row["batch"] == before_batch == batch_of("ebay:seller1", PARAMS.batches)


def test_pinning_twice_updates_the_reason(store):
    """已是釘選再 pin ＝「修改備註」。"""
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="第一版備註",
              params=PARAMS)
    res = add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="第二版備註",
                    params=PARAMS)
    assert res.ok and res.already
    row = store.get_seller_watch("ebay:pinme")
    assert row["source"] == SOURCE_PINNED and row["reason"] == "第二版備註"


def test_repinning_preserves_the_scan_bookkeeping(store):
    """F3 回歸（2026-08-09 審查）：對既有列再 pin（改備註或升級軌道）不得
    抹掉掃描簿記。先前 upsert 是 INSERT OR REPLACE：`last_scanned_at` 變
    NULL（防重掃護欄當它「從沒掃過」）、`last_result` 清空、`added_at`
    重寫——dashboard 的「上次掃描」憑空消失，而測試只斷言 reason 有更新
    所以一直是綠的。"""
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="第一版",
              params=PARAMS)
    store.mark_seller_watch_scanned("ebay:pinme", result="OK 75 筆")
    before = store.get_seller_watch("ebay:pinme")
    assert before["last_scanned_at"] and before["last_result"]  # 前提：簿記有值

    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="第二版",
              params=PARAMS)
    after = store.get_seller_watch("ebay:pinme")
    assert after["last_scanned_at"] == before["last_scanned_at"]
    assert after["last_result"] == before["last_result"]
    assert after["added_at"] == before["added_at"]
    assert after["reason"] == "第二版"


def test_pinned_is_not_downgraded_by_a_later_auto_candidate(store):
    """釘選之後，同一個賣家又被演算法選中——不得降級回 auto（沿用 already 路徑）。"""
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="使用者釘選",
              params=PARAMS)
    res = add_watch(store, "ebay:pinme", source=SOURCE_AUTO, reason="自動入選",
                    params=PARAMS, score=80.0)
    assert res.ok and res.already
    row = store.get_seller_watch("ebay:pinme")
    assert row["source"] == SOURCE_PINNED and row["score"] is None


def test_pinned_enters_every_batch_and_comes_first(store):
    """釘選列進**每一批**且排最前——這就是「優先權更高」的實作
    （每個輪替時段掃一次＝60 分，其他軌 240 分）。"""
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="使用者釘選",
              params=PARAMS)
    add_watch(store, "ebay:normal", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    for batch in range(PARAMS.batches):
        due, _skipped = due_sellers(store, PARAMS, batch)
        keys = [r["seller_key"] for r in due]
        assert "ebay:pinme" in keys, f"第 {batch} 批少了釘選列"
        assert keys[0] == "ebay:pinme", f"第 {batch} 批釘選列沒有排最前：{keys}"
    # 而 normal 只出現在自己 sha1 到的那一批
    own = batch_of("ebay:normal", PARAMS.batches)
    for batch in range(PARAMS.batches):
        due, _ = due_sellers(store, PARAMS, batch)
        assert ("ebay:normal" in [r["seller_key"] for r in due]) == (batch == own)


def test_pinned_is_not_duplicated_in_its_own_batch(store):
    """釘選列 sha1 剛好落在本批時，合併必須去重（不是掃兩次）。"""
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="t", params=PARAMS)
    own = batch_of("ebay:pinme", PARAMS.batches)
    due, _ = due_sellers(store, PARAMS, own)
    assert [r["seller_key"] for r in due].count("ebay:pinme") == 1


def test_pinned_respects_the_rescan_guard(store):
    """防重複掃護欄對釘選一樣生效：同一輪替時段內不重掃（force 連按不暴衝）。"""
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="t", params=PARAMS)
    store.mark_seller_watch_scanned("ebay:pinme", result="ok", now=now.isoformat())
    for batch in range(PARAMS.batches):
        due, skipped = due_sellers(store, PARAMS, batch, now=now + timedelta(minutes=5))
        assert "ebay:pinme" not in [r["seller_key"] for r in due]
        assert any(r["seller_key"] == "ebay:pinme" and "才掃過" in reason
                   for r, reason in skipped)
    # 過了輪替時段（60 分）就該再掃
    due, _ = due_sellers(store, PARAMS, 0, now=now + timedelta(minutes=61))
    assert "ebay:pinme" in [r["seller_key"] for r in due]


def test_pinned_is_due_next_slot_even_when_mark_lags_claim(store):
    """F1 回歸（2026-08-09 審查）：釘選的目標節奏（每批 60 分）與護欄門檻
    貼死在一起時，掃描節奏會靜默退化成兩輪一次。

    真實時序：claim 在 T、掃完 mark 在 T+幾秒（mark 恆晚於 claim）。下一輪
    claim 在 T+60 分，此時 age ≈ 59.9 分——若門檻是整整 60 分，釘選列被跳過；
    而 pipeline 的 skip 路徑又會用 mark 重寫 `last_scanned_at`，把資格再推走
    一整輪。修法是給 pinned 列 0.9 倍的餘裕門檻。
    """
    claim0 = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="t", params=PARAMS)
    # 上一輪：claim 於 12:00，掃完 mark 於 12:00:05（晚 5 秒）
    store.mark_seller_watch_scanned(
        "ebay:pinme", result="ok", now=(claim0 + timedelta(seconds=5)).isoformat()
    )
    # 下一輪：claim 整整 60 分後（排程準點）。age ≈ 59.92 分，必須是 due。
    due, skipped = due_sellers(store, PARAMS, 0, now=claim0 + timedelta(minutes=60))
    assert "ebay:pinme" in [r["seller_key"] for r in due], (
        f"釘選列在下一輪整點被護欄跳過（掃描頻率靜默退化）：skipped={skipped}"
    )


def test_rescan_guard_margin_applies_only_to_pinned(store):
    """0.9 餘裕只給 pinned：非 pinned 列的間隔是 240 分、門檻 60 分，餘裕
    本來就充足——55 分鐘前掃過的 manual 列仍然要被跳過（門檻維持 60 分）。"""
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    add_watch(store, "ebay:a", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    store.mark_seller_watch_scanned(
        "ebay:a", result="ok", now=(now - timedelta(minutes=55)).isoformat()
    )
    due, skipped = due_sellers(store, PARAMS, batch_of("ebay:a", PARAMS.batches), now=now)
    assert "ebay:a" not in [r["seller_key"] for r in due]
    assert any(r["seller_key"] == "ebay:a" and "才掃過" in reason
               for r, reason in skipped)


def test_pinned_on_an_unsupported_site_is_kept_and_skipped_loudly(store):
    """釘一個還沒有列舉實作的站台：留在名單、每批都報「為什麼沒掃」，
    不是安靜消失（安靜跳過與「賣家沒上架」外顯一模一樣）。

    2026-08-09：`buyee_mercari` 已轉正（賣家頁列舉上線），改用 `mercari_tw`
    當未支援樣本——它是舊資料可能殘留的孤兒站台。
    """
    add_watch(store, "mercari_tw:448657621", source=SOURCE_PINNED,
              reason="t", params=PARAMS)
    due, skipped = due_sellers(store, PARAMS, 0)
    assert "mercari_tw:448657621" not in [r["seller_key"] for r in due]
    assert any(r["seller_key"] == "mercari_tw:448657621" and "Mercari" in reason
               for r, reason in skipped)


def test_pinned_buyee_mercari_seller_is_now_scannable(store):
    """釘選軌 Phase 2 的驗收點：`buyee_mercari` 釘選列不再被跳過，
    每一批都進 due（釘選列進每一批＋站台已有列舉實作）。"""
    add_watch(store, "buyee_mercari:448657621", source=SOURCE_PINNED,
              reason="t", params=PARAMS)
    for batch in range(PARAMS.batches):
        due, skipped = due_sellers(store, PARAMS, batch)
        assert "buyee_mercari:448657621" in [r["seller_key"] for r in due]
        assert not any(r["seller_key"] == "buyee_mercari:448657621"
                       for r, _reason in skipped)


def test_remove_watch_works_on_pinned(store):
    """釘選只有使用者能移除——remove_watch 對它照常可用（手動刪除）。"""
    add_watch(store, "ebay:pinme", source=SOURCE_PINNED, reason="t", params=PARAMS)
    assert remove_watch(store, "ebay:pinme", reason="手動解除釘選")
    row = store.get_seller_watch("ebay:pinme")
    assert row["active"] == 0 and "解除釘選" in row["reason"]


# ---------------------------------------------------------------------------
# 11. dashboard 的釘選端點（POST /api/sellers/pin）
#     fixture 照抄 tests/test_card_bucket.py:189-220（web.app 在 import 時就
#     開 db，load_config 必須先換到 tmp，換完 assert 真的換到了）。
# ---------------------------------------------------------------------------
import importlib  # noqa: E402
from dataclasses import replace  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    import ygo_sniper.config as config_mod

    db = tmp_path / "web.db"
    real_load = config_mod.load_config

    def _tmp_config(*a, **kw):
        c = real_load(*a, **kw)
        return replace(c, storage={**c.storage, "db_path": str(db)})

    monkeypatch.setattr(config_mod, "load_config", _tmp_config)
    monkeypatch.syspath_prepend(str(ROOT))
    for mod in ("web.app", "web"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("web.app")
    try:
        # 承重的斷言，不是裝飾：這一行紅掉就代表測試正在開正式庫。
        assert app_mod.store.db_path == db, (
            f"web.app 的 store 沒有指到 tmp（{app_mod.store.db_path}）——"
            "測試絕不能碰正式庫 data/sniper.db"
        )
        from fastapi.testclient import TestClient

        yield TestClient(app_mod.app), app_mod
    finally:
        for mod in ("web.app", "web"):
            sys.modules.pop(mod, None)


def test_pin_endpoint_parses_the_url_and_pins(client):
    c, app_mod = client
    r = c.post("/api/sellers/pin",
               json={"url": "https://www.ebay.com/usr/collectiblemore"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["seller_key"] == "ebay:collectiblemore"
    assert body["unsupported_note"] is None          # ebay 有列舉實作
    row = app_mod.store.get_seller_watch("ebay:collectiblemore")
    assert row["active"] == 1 and row["source"] == SOURCE_PINNED
    assert row["score"] is None


def test_pin_endpoint_no_longer_flags_buyee_mercari_as_unlistable(client):
    """2026-08-09 賣家頁列舉上線後，tw.mercari 釘選（→ buyee_mercari 鍵）
    必須自然轉正：不再帶「掃不到」的注記。"""
    c, _app_mod = client
    r = c.post("/api/sellers/pin",
               json={"url": "https://tw.mercari.com/zh-hant/seller/448657621"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seller_key"] == "buyee_mercari:448657621"
    assert body["unsupported_note"] is None


def test_pin_endpoint_reports_an_unlistable_site_loudly(client):
    """站台還掃不到必須明講，不是回 ok 就當沒事。四個 URL 站台都已支援
    列舉，所以未支援樣本改走「現成鍵」路徑（endpoint 也收 site:id）。"""
    c, _app_mod = client
    r = c.post("/api/sellers/pin", json={"url": "ruten:someone"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seller_key"] == "ruten:someone"
    assert body["unsupported_note"] and "露天" in body["unsupported_note"]


def test_pin_endpoint_rejects_a_bad_url_with_400_and_the_reason(client):
    c, _app_mod = client
    r = c.post("/api/sellers/pin", json={"url": "https://example.com/whatever"})
    assert r.status_code == 400
    assert "auctions.yahoo.co.jp/seller" in r.json()["detail"]   # 列出支援形式


def test_pin_endpoint_resolves_an_ebay_store_url(client, monkeypatch):
    """/str/ 店鋪頁：連網解析出真實帳號（這裡 mock 掉抓取，零網路），
    釘的是 `ebay:{username}` 不是店名，reason 註明來源店鋪 slug。"""
    import ygo_sniper.seller_resolve as resolve_mod

    calls: list[str] = []

    def _fake_resolve(url, cfg=None, *, fetcher=None):
        calls.append(url)
        return "merry_tcg"

    monkeypatch.setattr(resolve_mod, "resolve_ebay_store", _fake_resolve)
    c, app_mod = client
    r = c.post("/api/sellers/pin",
               json={"url": "https://www.ebay.com/str/merrycorporation"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seller_key"] == "ebay:merry_tcg"       # 帳號，不是店鋪 slug
    assert body["unsupported_note"] is None
    assert "merrycorporation" in body["message"]        # 出處看得見
    assert calls == ["https://www.ebay.com/str/merrycorporation"]
    row = app_mod.store.get_seller_watch("ebay:merry_tcg")
    assert row["active"] == 1 and row["source"] == SOURCE_PINNED
    assert "merrycorporation" in row["reason"]          # 出處也留在名單上


def test_pin_endpoint_store_resolution_failure_is_400_pointing_to_usr(client, monkeypatch):
    """解析失敗（抽不到帳號／被擋）→ 400＋原文，訊息保留 /usr/ 指引；
    絕不退回「拿 slug 當帳號」。"""
    import ygo_sniper.seller_resolve as resolve_mod
    from ygo_sniper.seller_links import SellerUrlError

    def _fail(url, cfg=None, *, fetcher=None):
        raise SellerUrlError(
            "店鋪頁 merrycorporation 讀不到賣家帳號。請改貼 ebay.com/usr/帳號 頁"
        )

    monkeypatch.setattr(resolve_mod, "resolve_ebay_store", _fail)
    c, app_mod = client
    r = c.post("/api/sellers/pin",
               json={"url": "https://www.ebay.com/str/merrycorporation"})
    assert r.status_code == 400
    assert "usr" in r.json()["detail"]
    # 失敗就是失敗：不准偷偷用 slug 釘一個幽靈賣家
    assert app_mod.store.get_seller_watch("ebay:merrycorporation") is None


def test_unpin_reuses_the_existing_watch_remove_endpoint(client):
    c, app_mod = client
    c.post("/api/sellers/pin", json={"url": "https://www.ebay.com/usr/somebody"})
    r = c.post("/api/sellers/ebay:somebody/watch", json={"action": "remove"})
    assert r.status_code == 200 and r.json()["removed"]
    assert app_mod.store.get_seller_watch("ebay:somebody")["active"] == 0


# ---------------------------------------------------------------------------
# 12. CLI 的 /str/ 釘選路徑（watch-seller pin，mock 抓取、零網路）
#     fixture 照抄 tests/test_expiry_clear.py 的 cli_env 模式。
# ---------------------------------------------------------------------------
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """臨時 db 的 CLI 環境。承重斷言：CLI 測試絕不能碰正式庫。"""
    import ygo_sniper.cli as cli_mod
    import ygo_sniper.config as config_mod

    db = tmp_path / "cli.db"
    base = config_mod.load_config()
    test_cfg = replace(base, storage={**base.storage, "db_path": str(db)})
    monkeypatch.setattr(cli_mod, "load_config", lambda: test_cfg)
    assert test_cfg.db_path == db, "CLI 測試的 cfg 沒有指到 tmp db"

    from typer.testing import CliRunner

    return CliRunner(), Store(db), cli_mod


def test_cli_pin_resolves_an_ebay_store_url(cli_env, monkeypatch):
    import ygo_sniper.seller_resolve as resolve_mod

    monkeypatch.setattr(
        resolve_mod, "resolve_ebay_store",
        lambda url, cfg=None, *, fetcher=None: "merry_tcg",
    )
    runner, store, cli_mod = cli_env
    result = runner.invoke(
        cli_mod.app,
        ["watch-seller", "pin", "https://www.ebay.com/str/merrycorporation"],
    )
    assert result.exit_code == 0, result.output
    assert "merry_tcg" in result.output
    assert "merrycorporation" in result.output      # 店鋪 → 帳號的解析要說出來
    row = store.get_seller_watch("ebay:merry_tcg")
    assert row and row["active"] == 1 and row["source"] == SOURCE_PINNED
    assert "merrycorporation" in row["reason"]      # 出處留在名單上


def test_cli_pin_store_resolution_failure_exits_loudly(cli_env, monkeypatch):
    import ygo_sniper.seller_resolve as resolve_mod
    from ygo_sniper.seller_links import SellerUrlError

    def _fail(url, cfg=None, *, fetcher=None):
        raise SellerUrlError("店鋪頁 x 抓取失敗。請改貼 ebay.com/usr/帳號 頁")

    monkeypatch.setattr(resolve_mod, "resolve_ebay_store", _fail)
    runner, store, cli_mod = cli_env
    result = runner.invoke(
        cli_mod.app, ["watch-seller", "pin", "https://www.ebay.com/str/x"]
    )
    assert result.exit_code == 1
    assert "usr" in result.output                   # 指路 /usr/
    assert store.list_seller_watch() == []          # 失敗不落任何一筆
