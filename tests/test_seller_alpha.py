"""Seller Alpha 指標層與評分引擎的測試（Phase 3-4，2026-08-04）。

這一組測試守的是**方法論**，不是「函式會不會跑」。每一條都對應一種
「壞了看不出來、而使用者會照著它花錢」的病：

1. **同儕折價要手算得出來**——固定資料、固定答案。算錯了只會偏一點點，
   眼睛看不出來，但排行榜順序整個變。
2. **不同基準不准互比**（定價 vs 競標中出價 vs 成交價）。這是本模組最容易
   無聲出錯的地方：不分基準時可比數從 31 暴增到 91，多出來的全是假折價。
3. **模型偏誤不准變成賣家 alpha**——同儕相對的做法在「專賣便宜普卡」的賣家上
   必須回「跟大家一樣」，即使模型說他打三折。**這條是整個模組存在的理由。**
4. **收縮與門檻**：樣本 0／少／多的行為、低於門檻不給分數且說得出缺什麼。
5. **持續性 > 偶然**：八次穩定小折價必須勝過一次大折價。
6. **風險是扣分**，而且看得見。
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from ygo_sniper.seller_alpha import (
    BASIS_ASK,
    BASIS_BID,
    BASIS_SOLD,
    PERSISTENCE_MIN_DAYS,
    SALE_AUCTION,
    SALE_FIXED,
    SALE_UNKNOWN,
    SCORING_TIERS,
    TIER_STRATUM,
    TIER_STRICT,
    AlphaParams,
    MarketRow,
    PeerIndex,
    build_seller_items,
    edition_of,
    score_seller,
    seller_metrics,
)

# ---------------------------------------------------------------------------
# 造資料的小工具
# ---------------------------------------------------------------------------
_SEQ = {"n": 0}


def row(
    *,
    price: float,
    seller: str | None = None,
    card: str | None = "青眼の白龍",
    site: str = "ebay",
    basis: str = BASIS_ASK,
    rarity: str | None = "ultra",
    grade: float | None = 9.0,
    grader: str | None = "PSA",
    edition: str = "1st",
    landed: float | None = None,
    observed_at: str | None = "2026-08-01T00:00:00+00:00",
    title: str | None = None,
) -> MarketRow:
    _SEQ["n"] += 1
    return MarketRow(
        key=f"k{_SEQ['n']}",
        site=site,
        basis=basis,
        price_twd=price,
        title=title or f"{card} PSA{grade}",
        seller_key=f"{site}:{seller}" if seller else None,
        card_name=card,
        edition=edition,
        rarity=rarity,
        grader=grader,
        grade=grade,
        landed_twd=landed,
        observed_at=observed_at,
    )


def metrics_for(rows: list[MarketRow], seller_key: str, **kw):
    peers = PeerIndex(rows)
    items = build_seller_items(rows, peers)
    return seller_metrics(seller_key, items.get(seller_key, []), **kw)


# ---------------------------------------------------------------------------
# 1. 同儕折價：手算驗證
# ---------------------------------------------------------------------------
def test_peer_discount_is_hand_checkable():
    """三個同儕 900／1000／1100 → 中位 1000；賣家開 800 → 0.800×、折價 20%。"""
    rows = [
        row(price=800, seller="target"),
        row(price=900, seller="a"),
        row(price=1000, seller="b"),
        row(price=1100, seller="c"),
    ]
    m = metrics_for(rows, "ebay:target")
    item = m.items[0]
    assert item.peer is not None
    assert item.peer.tier == TIER_STRICT
    assert item.peer.peer_median_twd == pytest.approx(1000.0)
    assert item.peer.peer_n == 3
    assert item.peer.peer_sellers == 3
    assert item.ratio == pytest.approx(0.8)
    assert item.discount_pct == pytest.approx(20.0)
    assert m.discount_ratio_median == pytest.approx(0.8)


def test_own_listings_never_count_as_peers():
    """同賣家的其他標的一律排除——不然自賣自比永遠是 1.0×。"""
    rows = [
        row(price=800, seller="target"),
        row(price=800, seller="target"),
        row(price=1000, seller="other"),
    ]
    m = metrics_for(rows, "ebay:target")
    assert all(i.peer and i.peer.peer_n == 1 for i in m.items)
    assert all(i.ratio == pytest.approx(0.8) for i in m.items)


def test_unknown_seller_rows_may_serve_as_peers():
    """賣家未知的列可以當同儕（混進自己的列只會低估 alpha，方向保守）。"""
    rows = [row(price=800, seller="target"), row(price=1000, seller=None)]
    m = metrics_for(rows, "ebay:target")
    peer = m.items[0].peer
    assert peer is not None
    assert (peer.peer_n, peer.peer_sellers, peer.peer_unknown_seller_n) == (1, 0, 1)


def test_no_peers_means_no_discount_not_a_model_fallback():
    """同儕不足**不准**退回模型估值頂替——沒有比值就是沒有比值。"""
    m = metrics_for([row(price=800, seller="lonely")], "ebay:lonely")
    assert m.items[0].peer is None
    assert m.items[0].ratio is None
    assert m.discount_ratio_median is None
    assert m.n_comparable == 0


# ---------------------------------------------------------------------------
# 2. 不同基準／不同站／不同版次不准互比
# ---------------------------------------------------------------------------
def test_auction_current_bid_never_compares_against_asking_prices():
    """競標中出價不進指標。不擋這道，每個賣競標的賣家都會憑空長出巨大折價。"""
    rows = [
        row(price=300, seller="target", basis=BASIS_BID),
        row(price=1000, seller="a", basis=BASIS_ASK),
        row(price=1000, seller="b", basis=BASIS_ASK),
    ]
    m = metrics_for(rows, "ebay:target")
    assert m.items[0].peer is None
    assert m.n_bid_excluded == 1
    assert m.n_comparable == 0


def test_sold_prices_only_compare_against_sold_prices():
    rows = [
        row(price=800, seller="target", basis=BASIS_SOLD),
        row(price=2000, seller="a", basis=BASIS_ASK),
        row(price=1000, seller="b", basis=BASIS_SOLD),
    ]
    m = metrics_for(rows, "ebay:target")
    assert m.items[0].ratio == pytest.approx(0.8)   # 只跟 1000 那筆成交比


def test_cross_site_rows_are_not_peers():
    """Yahoo 與 Mercari 實測價格水準差 2 倍以上——跨站比＝把平台差算成賣家 alpha。"""
    rows = [
        row(price=800, seller="target", site="buyee_yahoo"),
        row(price=1000, seller="a", site="buyee_mercari"),
    ]
    m = metrics_for(rows, "buyee_yahoo:target")
    assert m.items[0].peer is None


def test_first_edition_is_part_of_the_peer_key():
    """1st Ed 與 Unlimited 差價可達數倍，不能互為同儕。

    版次不同時只會落到不計分的 `TIER_STRATUM`（比得到但不進指標），
    版次相同才升到 `TIER_STRICT`。
    """
    rows = [
        row(price=800, seller="target", edition="1st"),
        row(price=1000, seller="a", edition="unl"),
    ]
    item = metrics_for(rows, "ebay:target").items[0]
    assert item.peer is not None and item.peer.tier == TIER_STRATUM
    assert item.scoring is False

    rows.append(row(price=1000, seller="b", edition="1st"))
    item = metrics_for(rows, "ebay:target").items[0]
    assert item.peer is not None and item.peer.tier == TIER_STRICT
    assert item.scoring is True
    assert item.ratio == pytest.approx(0.8)


def test_edition_detection_defaults_to_non_first():
    assert edition_of("LOB-001 1st Edition PSA 9") == "1st"
    assert edition_of("遊戯王 初版 青眼") == "1st"
    assert edition_of("LOB-001 Unlimited PSA 9") == "unl"
    assert edition_of(None) == "unl"


def test_stratum_tier_is_never_scoring():
    """沒有卡名時只比得到「同稀有度×同分數」——那一層量的是卡種組合，不計分。"""
    rows = [
        row(price=100, seller="target", card=None),
        row(price=5000, seller="a", card=None),
        row(price=5000, seller="b", card=None),
    ]
    m = metrics_for(rows, "ebay:target")
    item = m.items[0]
    assert item.peer is not None and item.peer.tier == TIER_STRATUM
    assert item.peer.scoring is False
    assert item.scoring is False
    assert m.n_comparable == 0
    assert TIER_STRATUM not in SCORING_TIERS


# ---------------------------------------------------------------------------
# 3. 反向檢查：模型偏誤不准變成賣家 alpha（本模組存在的理由）
# ---------------------------------------------------------------------------
class _BiasedValuator:
    """模擬「對便宜普卡整段高估」的模型（實測 L3×Mercari 高估 5.9 倍）。"""

    def __init__(self, fair: float) -> None:
        self.fair = fair

    def estimate(self, **_kw):
        class _E:
            fair_twd = self.fair
            level = "L3"
            n_effective = 40

        return _E()


def test_cheap_commons_seller_is_not_scored_as_alpha_despite_model_bias():
    """**本棒最重要的一條。**

    一個專賣便宜普卡的賣家，開價與同儕完全一致（1.0×），但模型把這一段
    高估了 5.9 倍——模型絕對法會說他打 1.7 折（看起來是天大的 alpha）。
    同儕相對法必須回「沒有折價」，而且分數必須是 0。
    """
    rows = [
        row(price=500, seller="cheapo", card=f"普卡{i}", grade=8.0) for i in range(4)
    ] + [
        row(price=500, seller="other", card=f"普卡{i}", grade=8.0) for i in range(4)
    ]
    peers = PeerIndex(rows)
    items = build_seller_items(rows, peers, valuator=_BiasedValuator(fair=2950.0))
    m = seller_metrics("ebay:cheapo", items["ebay:cheapo"])

    # 模型輔助欄位確實看起來像巨大折價……
    assert m.model_ratio_median == pytest.approx(500 / 2950, rel=1e-3)
    assert m.model_ratio_median < 0.2
    # ……但主指標（同儕相對）是 1.0×，分數是 0。
    assert m.discount_ratio_median == pytest.approx(1.0)
    score = score_seller(m)
    assert score.ok is True
    assert score.total == pytest.approx(0.0)
    assert "沒有 alpha" in score.reason
    assert all(c.points <= 0 for c in score.components)


def test_model_ratio_never_enters_the_score():
    """就算模型說 0.1×，只要同儕說 1.0×，分數就是 0。"""
    rows = [row(price=500, seller="x", card=f"c{i}") for i in range(3)]
    rows += [row(price=500, seller="y", card=f"c{i}") for i in range(3)]
    peers = PeerIndex(rows)
    items = build_seller_items(rows, peers, valuator=_BiasedValuator(fair=5000.0))
    m = seller_metrics("ebay:x", items["ebay:x"])
    assert m.model_ratio_median == pytest.approx(0.1)
    assert score_seller(m).total == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 4. 收縮與門檻
# ---------------------------------------------------------------------------
def _seller_with(n_cards: int, ratio: float, *, span_days: float = 0.0):
    """造一個「n 張相異卡，每張都比同儕便宜 ratio 倍」的賣家。"""
    rows: list[MarketRow] = []
    for i in range(n_cards):
        day = 1 + int(span_days * i / max(1, n_cards - 1)) if n_cards > 1 else 1
        stamp = f"2026-08-{day:02d}T00:00:00+00:00"
        rows.append(row(price=1000 * ratio, seller="t", card=f"卡{i}", observed_at=stamp))
        rows.append(row(price=1000, seller="peer", card=f"卡{i}", observed_at=stamp))
    return metrics_for(rows, "ebay:t")


def test_zero_samples_get_no_score_and_say_what_is_missing():
    m = metrics_for([row(price=800, seller="t")], "ebay:t")
    s = score_seller(m)
    assert s.ok is False
    assert s.total is None            # 不是 0——0 會被讀成「這個賣家很差」
    assert s.missing
    assert "缺的是" in s.reason
    assert "門檻" in s.reason


def test_below_threshold_seller_gets_no_score():
    """兩張卡（門檻 3）→ 不給分數，並說得出還差什麼。"""
    m = _seller_with(2, 0.5)
    s = score_seller(m)
    assert (s.ok, s.total) == (False, None)
    assert "可比標的只有 2 筆" in s.reason


def test_repeat_listings_of_one_card_do_not_pass_the_threshold():
    """同一張卡刊四次不算四個證據（相異卡門檻）。"""
    rows = [row(price=500, seller="t", card="同一張") for _ in range(4)]
    rows.append(row(price=1000, seller="peer", card="同一張"))
    m = metrics_for(rows, "ebay:t")
    assert m.n_comparable == 4
    assert m.n_distinct_cards == 1
    s = score_seller(m)
    assert s.ok is False
    assert "張相異卡" in s.reason


def test_shrinkage_pulls_small_samples_toward_zero_alpha():
    """樣本少 → 折價被拉向「零 alpha」；樣本多 → 逼近原值。

    手算：raw = -ln(0.6) = 0.5108。n=3 → ×3/7 = 0.2189；n=20 → ×20/24 = 0.4257。
    """
    p = AlphaParams()
    small, big = _seller_with(3, 0.6), _seller_with(20, 0.6)
    assert small.discount_ratio_median == pytest.approx(0.6)
    assert big.discount_ratio_median == pytest.approx(0.6)

    raw = -math.log(0.6)
    s_small, s_big = score_seller(small, p), score_seller(big, p)
    depth_small = next(c for c in s_small.components if c.name == "depth")
    depth_big = next(c for c in s_big.components if c.name == "depth")
    # 深度分數 = 40 × min(1, shrunk / ln(1/0.75))
    full = math.log(1 / 0.75)
    assert depth_small.points == pytest.approx(
        round(40 * min(1.0, raw * 3 / 7 / full), 1), abs=0.11
    )
    assert depth_big.points == pytest.approx(
        round(40 * min(1.0, raw * 20 / 24 / full), 1), abs=0.11
    )
    assert depth_small.points < depth_big.points
    assert s_small.total < s_big.total


# ---------------------------------------------------------------------------
# 5. 持續性 > 偶然
# ---------------------------------------------------------------------------
def test_eight_steady_small_discounts_beat_one_big_discount():
    """八次穩定 12% 折價 vs 一筆 70% 折價（其餘持平）——穩定的必須贏。"""
    steady = _seller_with(8, 0.88)

    lucky_rows: list[MarketRow] = []
    for i, r in enumerate([0.30, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]):
        lucky_rows.append(row(price=1000 * r, seller="l", card=f"卡{i}"))
        lucky_rows.append(row(price=1000, seller="peer", card=f"卡{i}"))
    lucky = metrics_for(lucky_rows, "ebay:l")

    s_steady, s_lucky = score_seller(steady), score_seller(lucky)
    assert s_steady.ok and s_lucky.ok
    assert s_steady.total > s_lucky.total
    # 而且不是險勝：偶然那個連折價深度都拿不到分（中位數不動）
    assert next(c for c in s_lucky.components if c.name == "depth").points == 0.0
    assert next(c for c in s_lucky.components if c.name == "consistency").points == 0.0


def test_single_extreme_discount_is_winsorized():
    """單筆 95% 折價只算到 50%——極端單筆多半是「那張卡有問題」，不是定價行為。"""
    rows: list[MarketRow] = []
    for i, r in enumerate([0.05, 0.05, 0.05]):
        rows.append(row(price=1000 * r, seller="w", card=f"卡{i}"))
        rows.append(row(price=1000, seller="peer", card=f"卡{i}"))
    m = metrics_for(rows, "ebay:w")
    assert m.discount_ratio_median == pytest.approx(0.05)          # 原始比值照實記
    assert m.discount_log_median == pytest.approx(-math.log(2.0))  # 但入分數的被夾住


def test_repeated_listings_of_one_card_cannot_dominate_the_median():
    """同一張卡刊四次的極端折價不准主宰中位——先按卡收斂再跨卡取中位。"""
    rows: list[MarketRow] = []
    for _ in range(4):
        rows.append(row(price=100, seller="t", card="灌水卡"))
    rows.append(row(price=1000, seller="peer", card="灌水卡"))
    for i in range(3):
        rows.append(row(price=1000, seller="t", card=f"正常{i}"))
        rows.append(row(price=1000, seller="peer", card=f"正常{i}"))
    m = metrics_for(rows, "ebay:t")
    assert m.n_comparable == 7
    assert m.n_distinct_cards == 4
    # 逐筆取中位會是 0.1（4 票灌水 vs 3 票正常）；按卡收斂之後是 1.0
    assert m.discount_ratio_median == pytest.approx(1.0)


def test_being_more_expensive_earns_nothing_anywhere():
    """比同儕貴的賣家不准靠「廣度」爬上排行榜（2026-08-04 實測到的排序錯誤）。"""
    m = _seller_with(8, 1.5, span_days=25)
    s = score_seller(m)
    assert s.ok is True
    assert s.total == pytest.approx(0.0)
    assert {c.name: c.points for c in s.components} == {
        "depth": 0.0, "consistency": 0.0, "breadth": 0.0, "risk": -0.0
    }


# ---------------------------------------------------------------------------
# 6. 風險扣分
# ---------------------------------------------------------------------------
def _flagged_metrics(n_flagged: int, n_total: int = 5):
    """造一個賣家：n_total 張卡都比同儕便宜 30%，其中 n_flagged 筆被標可疑。"""
    rows: list[MarketRow] = []
    for i in range(n_total):
        rows.append(row(price=700, seller="r", card=f"卡{i}"))
        rows.append(row(price=1000, seller="peer", card=f"卡{i}"))
    own = [r.key for r in rows if r.seller_key == "ebay:r"]
    flags = {k: ("suspicious_cheap",) for k in own[:n_flagged]}
    items = build_seller_items(rows, PeerIndex(rows), flags_by_key=flags)
    return seller_metrics("ebay:r", items["ebay:r"])


def test_suspicious_cheap_flags_are_a_deduction_and_visible_on_their_own():
    risky, clean = _flagged_metrics(3, 5), _flagged_metrics(0, 5)
    assert risky.suspicious_cheap_n == 3
    assert risky.suspicious_cheap_share == pytest.approx(0.6)
    assert clean.suspicious_cheap_n == 0

    s_risky, s_clean = score_seller(risky), score_seller(clean)
    risk_row = next(c for c in s_risky.components if c.name == "risk")
    assert risk_row.points < 0
    assert "suspicious_cheap" in risk_row.detail
    assert s_risky.total < s_clean.total


def test_bad_feedback_is_a_deduction_and_missing_feedback_is_not_safety():
    m_bad = _seller_with(5, 0.7)
    m_bad.feedback_pct, m_bad.feedback_score = 92.0, 800
    m_bad.risk_known = True
    m_unknown = _seller_with(5, 0.7)
    m_unknown.risk_known = False

    s_bad, s_unknown = score_seller(m_bad), score_seller(m_unknown)
    assert next(c for c in s_bad.components if c.name == "risk").points == -15.0
    # 讀不到評價**不扣分**，但必須大聲說「未知 ≠ 安全」
    assert next(c for c in s_unknown.components if c.name == "risk").points == 0.0
    assert "未知" in next(c for c in s_unknown.components if c.name == "risk").detail
    assert any("風險維度是未知" in c for c in s_unknown.caveats)


def test_ingest_timestamps_never_count_as_observation_span():
    """入庫時間不准撐起「持續性」。

    Buyee 系已售出頁沒有成交時間，那批 comps 的 `sold_at` 是我們入庫的時間
    （`store` 的 `sold_at_is_ingest`）。拿它算跨度＝量到自己的抓取排程，
    然後把一個從來沒被觀察過的持續性憑空變出來。
    """
    import dataclasses

    rows: list[MarketRow] = []
    for i, day in enumerate(("01", "20")):
        r = row(
            price=700, seller="t", card=f"卡{i}", basis=BASIS_SOLD,
            observed_at=f"2026-07-{day}T00:00:00+00:00",
        )
        rows.append(dataclasses.replace(r, observed_at_is_real=False))
        rows.append(row(price=1000, seller="peer", card=f"卡{i}", basis=BASIS_SOLD))
    m = metrics_for(rows, "ebay:t")
    assert m.n_fake_timestamps == 2
    assert m.observation_span_days == 0.0
    assert "入庫的時間" in m.persistence_note


def test_single_peer_seller_raises_a_loud_caveat():
    """全部同儕來自同一個賣家＝一次 1v1 比價，分數照給但必須講出來。"""
    m = _seller_with(4, 0.7)
    s = score_seller(m)
    assert s.ok and s.total > 0
    assert m.peer_seller_pool == 1
    assert any("1 個賣家" in c for c in s.caveats)


# ---------------------------------------------------------------------------
# 7. 可解釋性：絕不輸出裸數字
# ---------------------------------------------------------------------------
def test_every_component_carries_its_own_evidence():
    s = score_seller(_seller_with(6, 0.7))
    assert s.ok
    assert {c.name for c in s.components} == {"depth", "consistency", "breadth", "risk"}
    for c in s.components:
        assert c.detail.strip(), f"{c.name} 沒有依據——裸數字不准輸出"
    assert s.total == pytest.approx(round(sum(c.points for c in s.components), 1), abs=0.05)


# ---------------------------------------------------------------------------
# 8. 持續性：兩種跨度分開報（2026-08-04 的方法論修正）
# ---------------------------------------------------------------------------
def test_coverage_reports_ask_and_sold_spans_separately():
    """在架帳跨度與成交帳跨度**不可以合成一個數字**。

    在架帳說的是「我們盯了多久」，成交帳說的是「平台留著的歷史有多長」。
    取大值當一個數字報，畫面就會顯示 180 天而實際上我們只盯了 2 天
    ——那正是先前「跨度顯示好幾個月、持續性卻對所有人 0 分」的矛盾。
    """
    import dataclasses

    from ygo_sniper.seller_alpha import (
        AlphaParams,
        AlphaReport,
        coverage_report,
        score_seller,
    )

    rows: list[MarketRow] = []
    for i in range(4):
        sold = row(
            price=700, seller="t", card=f"卡{i}", basis=BASIS_SOLD,
            observed_at=f"2026-{2 + i:02d}-01T00:00:00+00:00",
        )
        rows.append(dataclasses.replace(sold, source_table="comps"))
        peer = row(
            price=1000, seller="peer", card=f"卡{i}", basis=BASIS_SOLD,
            observed_at=f"2026-{2 + i:02d}-01T00:00:00+00:00",
        )
        rows.append(dataclasses.replace(peer, source_table="comps"))
    # 在架帳只有一天（同一天兩筆）
    rows.append(row(price=900, seller="t", card="在架卡", observed_at="2026-08-01T00:00:00+00:00"))
    rows.append(row(price=900, seller="u", card="在架卡", observed_at="2026-08-01T00:00:00+00:00"))

    peers = PeerIndex(rows)
    items = build_seller_items(rows, peers)
    rep = AlphaReport(params=AlphaParams())
    for key, its in items.items():
        m = seller_metrics(key, its)
        rep.metrics[key] = m
        rep.scores[key] = score_seller(m)
    cov = coverage_report(rows, rep, AlphaParams())

    assert cov["comps_span_days"] > 80, "成交帳跨度來自 comps 的真實成交時間"
    assert cov["listing_obs_span_days"] == 0.0, "在架帳只有一天，不得沾成交帳的光"
    assert cov["sellers_persistent"] >= 1
    assert cov["persistence_min_days"] == PERSISTENCE_MIN_DAYS


def test_persistence_note_flips_once_the_span_is_long_enough():
    short = _seller_with(4, 0.7, span_days=2)
    long = _seller_with(4, 0.7, span_days=25)

    assert "不足以判定" in short.persistence_note
    assert "足以談持續性" in long.persistence_note
    # 而且分數裡的時間那一半真的動了
    s_short = score_seller(short)
    s_long = score_seller(long)
    day_short = next(c for c in s_short.components if c.name == "breadth").points
    day_long = next(c for c in s_long.components if c.name == "breadth").points
    assert day_long > day_short


def test_sold_and_ask_never_share_a_peer_pool_even_with_history():
    """紅線：歷史成交進來之後，成交價仍然不得與在架價配對。"""
    ask = row(price=1000, seller="a", basis=BASIS_ASK)
    sold = row(price=300, seller="b", basis=BASIS_SOLD)
    index = PeerIndex([ask, sold])

    assert index.match(ask) is None
    assert index.match(sold) is None


# ---------------------------------------------------------------------------
# 7. 成交型態（sale_kind）：競標結標 ≠ 定價成交（2026-08-06）
#
# `sold` 這一桶原本混著兩種語意：ヤフオク落札價是**買家搶到多高**，
# フリマ／Mercari／一口價是**賣家開多少**。Seller Alpha 問的是後者，
# 拿前者當同儕量到的是熱度不是定價行為——而且方向是「這個賣家好便宜」。
# ---------------------------------------------------------------------------
def _sold_row(price, seller, kind, **kw):
    r = row(price=price, seller=seller, basis=BASIS_SOLD, site="buyee_yahoo", **kw)
    return dataclasses.replace(r, sale_kind=kind)


def test_auction_closes_never_compare_against_fixed_price_sales():
    """實測動機：13 筆 Yahoo 一口價的標的，同儕 24 筆裡有 22 筆是競標結標。"""
    rows = [
        _sold_row(800, "target", SALE_FIXED),
        _sold_row(2000, "a", SALE_AUCTION),
        _sold_row(2200, "b", SALE_AUCTION),
    ]
    m = metrics_for(rows, "buyee_yahoo:target")
    assert m.items[0].peer is None
    assert m.n_comparable == 0


def test_fixed_price_sales_compare_against_fixed_price_sales():
    rows = [
        _sold_row(800, "target", SALE_FIXED),
        _sold_row(2000, "a", SALE_AUCTION),
        _sold_row(1000, "b", SALE_FIXED),
    ]
    m = metrics_for(rows, "buyee_yahoo:target")
    assert m.items[0].ratio == pytest.approx(0.8)     # 只跟同型態的 1000 比


def test_auction_closes_compare_against_auction_closes():
    rows = [
        _sold_row(800, "target", SALE_AUCTION),
        _sold_row(1000, "a", SALE_AUCTION),
        _sold_row(5000, "b", SALE_FIXED),
    ]
    m = metrics_for(rows, "buyee_yahoo:target")
    assert m.items[0].ratio == pytest.approx(0.8)


def test_unknown_sale_kind_is_neither_scored_nor_used_as_a_peer():
    """證據不足就不給分——與「湊不到同儕就拒答」同一個哲學。

    **`unknown` 不准被當成 `fixed`**：那會讓一筆沒有證據的成交安靜地
    變成同儕，而混池的錯誤方向永遠是「看起來很划算」。
    """
    rows = [
        _sold_row(800, "target", SALE_UNKNOWN),
        _sold_row(1000, "a", SALE_FIXED),
        _sold_row(1000, "b", SALE_UNKNOWN),
        _sold_row(900, "victim", SALE_FIXED),
    ]
    target = metrics_for(rows, "buyee_yahoo:target")
    assert target.items[0].peer is None, "unknown 不當標的"
    assert target.n_sale_kind_unknown == 1

    victim = metrics_for(rows, "buyee_yahoo:victim")
    assert victim.items[0].peer is not None
    assert victim.items[0].peer.peer_n == 1, "unknown 不當同儕（只剩 a 那筆）"
    assert victim.items[0].peer.peer_median_twd == pytest.approx(1000.0)


def test_ask_rows_are_seller_set_prices_and_still_pair_up():
    """在架定價本來就是「賣家開的價」，加了這個維度不該影響 ask 那一池。"""
    rows = [row(price=800, seller="target"), row(price=1000, seller="a")]
    m = metrics_for(rows, "ebay:target")
    assert m.items[0].ratio == pytest.approx(0.8)


def test_market_rows_from_store_never_guesses_a_missing_sale_kind(tmp_path):
    """db 欄位是 NULL（回填前的舊列）→ `unknown`，**不是** `fixed`。

    這是整條路徑上唯一會出現「沒有值」的地方，也是最容易靜默出錯的地方。
    """
    import sqlite3

    from ygo_sniper.seller_alpha import market_rows_from_store
    from ygo_sniper.store import Store

    db = tmp_path / "rows.db"
    Store(db)
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO comps (signature, title, url, site, sold_at, price_twd,"
            " sale_kind, seller_id) VALUES"
            " ('S1','t','u1','buyee_yahoo','2026-03-01T00:00:00+00:00',100,NULL,'s1')"
        )
        c.execute(
            "INSERT INTO comps (signature, title, url, site, sold_at, price_twd,"
            " sale_kind, seller_id) VALUES"
            " ('S2','t','u2','buyee_yahoo','2026-03-01T00:00:00+00:00',100,'auction','s1')"
        )
        c.execute(
            "INSERT INTO listing_obs (key, site, title, url, price_twd, price_kind,"
            " seller_id, first_seen) VALUES"
            " ('k1','ebay','t','u3',100,'buyout','s2','2026-03-01T00:00:00+00:00')"
        )
    rows = market_rows_from_store(Store(db), None)
    by_key = {r.key: r for r in rows}
    assert [r.sale_kind for r in rows if r.source_table == "comps"].count(SALE_UNKNOWN) == 1
    assert SALE_AUCTION in [r.sale_kind for r in rows]
    assert by_key["k1"].sale_kind == SALE_FIXED, "在架定價＝賣家開的價"


def test_coverage_reports_the_sale_kind_split():
    """覆蓋率必須說得出「競標結標 N 筆／定價成交 M 筆」——不然使用者只會看到
    可比數變少，卻不知道是修好了還是壞了。"""
    from ygo_sniper.seller_alpha import AlphaReport, coverage_report

    rows = [
        _sold_row(800, "target", SALE_AUCTION),
        _sold_row(1000, "a", SALE_AUCTION),
        _sold_row(900, "b", SALE_FIXED),
        _sold_row(950, "c", SALE_FIXED),
        _sold_row(950, "d", SALE_UNKNOWN),
    ]
    peers = PeerIndex(rows)
    items = build_seller_items(rows, peers)
    rep = AlphaReport(params=AlphaParams())
    for key, its in items.items():
        rep.metrics[key] = seller_metrics(key, its)
        rep.scores[key] = score_seller(rep.metrics[key])
    cov = coverage_report(rows, rep, AlphaParams())

    assert cov["rows_sale_kind"] == {SALE_AUCTION: 2, SALE_FIXED: 2, SALE_UNKNOWN: 1}
    assert cov["rows_sale_kind_unknown"] == 1
    assert cov["comparable_sale_kind"] == {SALE_AUCTION: 2, SALE_FIXED: 2}
