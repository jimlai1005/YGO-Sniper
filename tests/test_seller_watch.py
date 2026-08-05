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
    上線），所以未支援那一半改用 `buyee_mercari`——而 Yahoo 賣家必須真的
    進 due，那正是這次要修的覆蓋缺口。
    """
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    add_watch(store, "ebay:a", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    add_watch(store, "buyee_yahoo:zzz", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    add_watch(store, "buyee_mercari:m1", source=SOURCE_MANUAL, reason="t", params=PARAMS)
    b_ebay = batch_of("ebay:a", 4)
    b_yahoo = batch_of("buyee_yahoo:zzz", 4)
    b_mercari = batch_of("buyee_mercari:m1", 4)

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

    _due, skipped = due_sellers(store, PARAMS, b_mercari, now=now)
    assert any("Mercari" in reason for _r, reason in skipped)


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
        (_Est(n_eff=20), "校準已知壞掉"),          # 10-49 破口桶
        (_Est(cal_n=10), "校準殘差"),
    ],
)
def test_evidence_gate_blocks_the_model_fallback(stub_model, est, expect):
    """閘門擋下的**仍然不推折價**——改走規則 3b，而且說得出被哪一道擋下。

    這是「模型 fallback 不是什麼都推」的結構性保證：閘門引用的是
    `bidding.EvidenceGate`（出價那一側量過實測依據的那一份），不是另訂的標準。
    """
    val = stub_model(est)
    out = _run_model(_ctx(item=None), valuator=val)
    assert out.seller_new == []                  # 沒有折價數字
    assert len(out.seller_unpriced) == 1
    assert expect in (out.seller_unpriced[0].unpriced_reason or "")


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
