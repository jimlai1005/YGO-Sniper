"""單一網址鑑價（appraise.py）的零網路測試。

這個檔案釘住的是四件會**錯得看起來像成功**的事：

1. **價格語意**（與 test_yahoo_source.py 同一條紅線，但這裡是商品頁）：
   即決価格才是可成交價，現在価格是別人的出價。沒有即決價時報告必須明講
   「實際成交價會更高」，否則成本估算系統性偏低而且毫無錯誤訊息。
2. **判決分級**：三種判決各一，且 CAUTION 必須是「無法判斷」而不是「還好」。
   資料不足時給出中性語氣，等於把一個沒有意見的模型包裝成有意見。
3. **不支援的網址**：要拋型別明確的 UnsupportedUrlError（API 對應 400），
   而不是在解析階段炸一個沒人接得住的例外。
4. **可比樣本的分級**：報告列出來的樣本必須跟模型實際用的分層同一套判準，
   否則使用者拿著一份「假可比清單」去驗證模型。

HTML 全部是 2026-08-01 實抓的商品頁 fixture，所有抓取都用假物件注入，零網路。
"""

import copy
import json
from pathlib import Path

import pytest

from ygo_sniper.appraise import (
    MIN_COMPARABLES,
    VERDICT_AVOID,
    VERDICT_CAUTION,
    VERDICT_WORTH,
    UnsupportedUrlError,
    appraise,
    collect_comparables,
    decide_verdict,
    parse_buyee_item,
    parse_ebay_item,
    parse_mercari_tw_item,
    parse_target,
    parse_yahoo_item,
)
from ygo_sniper.bidding import LIVE_AUCTION_KIND, is_live_auction
from ygo_sniper.cards import CardIndex
from ygo_sniper.domain import CardInfo, Currency, Grader, Listing, Site
from ygo_sniper.sources.base import FetchError
from ygo_sniper.valuation import Estimate

FIXTURES = Path(__file__).parent / "fixtures"
YAHOO_BUYOUT = (FIXTURES / "yahoo_item_buyout.html").read_text(encoding="utf-8")
YAHOO_BID_ONLY = (FIXTURES / "yahoo_item_bid_only.html").read_text(encoding="utf-8")
BUYEE_MERCARI = (FIXTURES / "buyee_mercari_item.html").read_text(encoding="utf-8")
BUYEE_PAYPAY = (FIXTURES / "buyee_paypay_item.html").read_text(encoding="utf-8")
MERCARI_TW = (FIXTURES / "mercari_tw_item.html").read_text(encoding="utf-8")

# RECON §2 實測比對表的樣本（與 Buyee 商品頁逐円核對過）
BUYOUT_ID = "s1238539612"      # 現在 3,440 / 即決 30,000
BID_ONLY_ID = "m1238496717"    # 現在 20,305 / 無即決


# ---------------------------------------------------------------------------
class TestParseTarget:
    """網址 → 抓取計畫。認不得就要拋，絕不猜。"""

    def test_yahoo_native_url(self):
        t = parse_target(f"https://auctions.yahoo.co.jp/jp/auction/{BUYOUT_ID}")
        assert t.site is Site.BUYEE_YAHOO
        assert t.external_id == BUYOUT_ID
        assert t.fetch_mode == "yahoo_native"
        # 購買端一律 Buyee：成本模型與去重 key 都認這個
        assert t.buy_url == f"https://buyee.jp/item/yahoo/auction/{BUYOUT_ID}"

    def test_buyee_yahoo_url_reroutes_to_native_page(self):
        """Buyee 的 Yahoo 商品頁改抓原生頁（同 ID 空間、價格逐円一致），省開瀏覽器。"""
        t = parse_target(f"https://buyee.jp/item/yahoo/auction/{BUYOUT_ID}")
        assert t.fetch_mode == "yahoo_native"
        assert t.fetch_url == f"https://auctions.yahoo.co.jp/jp/auction/{BUYOUT_ID}"
        assert t.site is Site.BUYEE_YAHOO

    def test_buyee_mercari_url_needs_waf(self):
        t = parse_target("https://buyee.jp/mercari/item/m48967074463")
        assert t.site is Site.BUYEE_MERCARI
        assert t.fetch_mode == "buyee_waf"
        assert t.fetch_url == t.buy_url

    def test_buyee_paypay_url_needs_waf(self):
        t = parse_target("https://buyee.jp/paypayfleamarket/item/z654154608")
        assert t.site is Site.BUYEE_PAYPAY
        assert t.fetch_mode == "buyee_waf"

    def test_base62_mercari_id_survives(self):
        """RECON §3：新版 id 是 22 字 base62，硬編 `m\\d+` 會整批漏掉。"""
        t = parse_target("https://buyee.jp/mercari/item/2JUaqAqRfVZv8txc7wBYEY")
        assert t.external_id == "2JUaqAqRfVZv8txc7wBYEY"

    def test_query_string_is_ignored(self):
        t = parse_target("https://buyee.jp/mercari/item/m123?conversionType=x")
        assert t.external_id == "m123"

    @pytest.mark.parametrize(
        "url",
        [
            "https://buyee.jp/mercari/search?keyword=%E9%81%8A%E6%88%AF%E7%8E%8B",
            "https://jp.mercari.com/item/m48967074463",
            # eBay 的**商品頁**（/itm/{數字}）2026-08-03 起支援，但它的搜尋頁／
            # 分類頁／賣家頁一樣不算商品頁——支援一個站不等於支援它整個網域。
            "https://www.ebay.com/sch/i.html?_nkw=yugioh+psa",
            "https://www.ebay.com/b/Yu-Gi-Oh-TCG-Individual-Cards/183454",
            "https://www.ebay.com/usr/carddealer",
            "https://www.ebay.com/itm/abc123",          # 商品號一定是純數字
            "https://auctions.yahoo.co.jp/closedsearch/closedsearch?p=x",
            "not-a-url",
            "",
        ],
    )
    def test_unsupported_urls_raise_typed_error(self, url):
        with pytest.raises(UnsupportedUrlError) as exc:
            parse_target(url)
        # 錯誤訊息要能自我說明：使用者看完就知道該貼什麼
        assert "buyee.jp/mercari/item/" in str(exc.value)
        assert "auctions.yahoo.co.jp" in str(exc.value)


# ---------------------------------------------------------------------------
class TestPriceSemantics:
    """整個模組最危險的一段。錯了不會有任何錯誤訊息，只會每筆都像撿到寶。"""

    def test_buyout_price_is_the_transactable_one(self):
        item = parse_yahoo_item(YAHOO_BUYOUT, "https://x.test")
        assert item.price_kind == "buyout"
        assert item.buyout_jpy == 30000
        assert item.current_bid_jpy == 3440
        # 算成本用的是即決価格，不是現在価格
        assert item.price == 30000
        assert item.currency is Currency.JPY
        assert "即決" in item.price_note
        assert "不是你付得出去的價格" in item.price_note

    def test_bid_only_item_says_final_price_will_be_higher(self):
        item = parse_yahoo_item(YAHOO_BID_ONLY, "https://x.test")
        assert item.price_kind == "current_bid"
        assert item.buyout_jpy is None
        assert item.current_bid_jpy == 20305
        assert item.price == 20305
        assert item.currency is Currency.JPY
        assert "實際成交價會更高" in item.price_note

    def test_bid_only_warning_reaches_the_report(self, appraise_env):
        """語意警告只留在 ItemPage 裡等於沒有——它必須浮到報告最上層。"""
        report = appraise_env(YAHOO_BID_ONLY, BID_ONLY_ID)
        assert report.item["price_kind"] == "current_bid"
        assert any("實際成交價會更高" in w for w in report.warnings)

    def test_buyout_item_has_no_bid_warning(self, appraise_env):
        report = appraise_env(YAHOO_BUYOUT, BUYOUT_ID)
        assert report.item["price_kind"] == "buyout"
        assert not any("實際成交價會更高" in w for w in report.warnings)

    def test_missing_next_data_fails_loudly(self):
        from ygo_sniper.sources.base import FetchError

        with pytest.raises(FetchError, match="__NEXT_DATA__"):
            parse_yahoo_item("<html><body>nothing here</body></html>", "https://x.test")


# ---------------------------------------------------------------------------
class TestBuyeeItemParsing:
    def test_mercari_item(self):
        item = parse_buyee_item(BUYEE_MERCARI, "https://x.test")
        assert item.title == "【大人気/ARS9】ブラックマジシャンガール 初期 ウルトラ P4-01"
        assert item.price == 8299
        assert item.currency is Currency.JPY
        assert item.price_kind == "fixed"
        assert item.is_sold is False

    def test_paypay_item_uses_its_own_selectors(self):
        """Mercari 與 PayPay 的**商品頁** DOM 不同構（只有搜尋頁同構）。"""
        item = parse_buyee_item(BUYEE_PAYPAY, "https://x.test")
        assert item.price == 17000
        assert item.currency is Currency.JPY
        assert item.price_kind == "fixed"
        assert item.is_sold is True     # 這筆已售出，soldOut 標記在

    def test_blocked_page_fails_loudly(self):
        from ygo_sniper.sources.base import FetchError

        with pytest.raises(FetchError):
            parse_buyee_item("<html><body>challenge</body></html>", "https://x.test")


# ---------------------------------------------------------------------------
def _comp(price, *, card="はにわ", rarity="ultra", grade=9.0, sold="2026-07-01",
          site="buyee_mercari"):
    return {
        "title": f"{card} {rarity} PSA{grade:g}",
        "price_twd": price,
        "rarity": rarity,
        "grade": grade,
        "card_name": card,
        "url": f"https://buyee.jp/mercari/item/m{int(price)}",
        "sold_at": sold,
        "era_evidence": "jp_kw:初期",
        "site": site,
    }


class TestComparables:
    """報告裡最重要的一欄。分級必須與 valuation 的分層同判準。"""

    def test_tiers_are_ordered_by_how_comparable_they_are(self):
        rows = [
            _comp(100, card="はにわ", rarity="ultra", grade=9.0),   # T1
            _comp(200, card="はにわ", rarity="ultra", grade=10.0),  # T2
            _comp(300, card="森", rarity="ultra", grade=9.0),       # T3
            _comp(400, card="森", rarity="ultra", grade=8.0),       # T4
            _comp(500, card="森", rarity="secret", grade=9.0),      # 不可比
        ]
        shown, stats = collect_comparables(
            rows, None, card_name="はにわ", rarity="ultra", grade=9.0
        )
        assert [c.tier for c in shown] == [1, 2, 3, 4]
        assert [c.price_twd for c in shown] == [100, 200, 300, 400]
        # 統計只看最可比那一層，不是整份清單
        assert stats["tier"] == 1
        assert stats["n"] == 1
        assert stats["median_twd"] == 100

    def test_rarity_mismatch_is_never_comparable(self):
        rows = [_comp(500, rarity="secret")]
        shown, stats = collect_comparables(
            rows, None, card_name="はにわ", rarity="ultra", grade=9.0
        )
        assert shown == []
        assert stats["n"] == 0

    def test_none_rarity_matches_only_none_rarity(self):
        """稀有度抽不到就是 None，不猜——None 只跟 None 同格（與模型同判準）。"""
        rows = [_comp(100, rarity=None), _comp(200, rarity="ultra")]
        shown, _ = collect_comparables(
            rows, None, card_name=None, rarity=None, grade=9.0
        )
        assert [c.price_twd for c in shown] == [100]

    def test_shown_list_is_capped_and_stats_are_not(self):
        rows = [_comp(100 + i, sold=f"2026-07-{i + 1:02d}") for i in range(25)]
        shown, stats = collect_comparables(
            rows, None, card_name="はにわ", rarity="ultra", grade=9.0, limit=10
        )
        assert len(shown) == 10
        # 判決引用的範圍必須是整層的，不能被顯示上限截斷
        assert stats["n"] == 25
        assert (stats["min_twd"], stats["max_twd"]) == (100, 124)

    def test_card_index_decides_the_name_not_the_cached_column(self):
        """卡名在查詢時重算（與 valuation.obs_from_comps 同源），不信 card_name 欄。"""
        index = CardIndex([{"id": 1, "name_ja": "はにわ", "name_en": "Hane-Hane"}], {})
        rows = [_comp(100, card="はにわ") | {"card_name": "完全錯的名字"}]
        shown, stats = collect_comparables(
            rows, index, card_name="はにわ", rarity="ultra", grade=9.0
        )
        assert stats["tier"] == 1
        assert shown[0].card_name == "はにわ"


class TestComparableVenues:
    """可比清單必須標出每筆成交在哪個平台——三個平台的價位差 2 倍以上，
    一份沒標平台的清單會讓人把 Yahoo 的出清價當成 Mercari 的行情。"""

    def test_each_row_carries_its_venue(self):
        rows = [
            _comp(100, site="buyee_yahoo"),
            _comp(300, site="buyee_mercari"),
        ]
        shown, _ = collect_comparables(
            rows, None, card_name="はにわ", rarity="ultra", grade=9.0,
            venue="buyee_mercari",
        )
        by_price = {c.price_twd: c for c in shown}
        assert by_price[300].site_label == "Mercari（定價）"
        assert by_price[300].same_venue is True
        assert by_price[100].site_label == "Yahoo 拍賣（競價）"
        assert by_price[100].same_venue is False
        # 價格一律是原始成交價，不做換算：換算過的數字對不上你點進去看到的頁面
        assert sorted(by_price) == [100, 300]

    def test_stats_expose_the_venue_mix_and_same_venue_median(self):
        rows = [_comp(100, site="buyee_yahoo"), _comp(200, site="buyee_yahoo"),
                _comp(600, site="buyee_mercari")]
        _shown, stats = collect_comparables(
            rows, None, card_name="はにわ", rarity="ultra", grade=9.0,
            venue="buyee_mercari",
        )
        assert stats["venue_mix"] == {"buyee_yahoo": 2, "buyee_mercari": 1}
        assert stats["same_venue_n"] == 1
        assert stats["same_venue_median_twd"] == 600
        assert stats["median_twd"] == 200          # 整層中位仍是混合平台的
        assert stats["target_venue_label"] == "Mercari（定價）"

    def test_landed_line_warns_when_the_tier_mixes_venues(self):
        from ygo_sniper.appraise import _landed_line

        line = _landed_line(1000.0, {
            "n": 3, "tier_label": "同卡 × 同稀有度 × 同分數",
            "min_twd": 100, "max_twd": 600, "median_twd": 200,
            "venue_mix": {"buyee_yahoo": 2, "buyee_mercari": 1},
            "target_venue_label": "Mercari（定價）", "same_venue_n": 1,
            "same_venue_median_twd": 600,
        })
        assert "橫跨多個平台" in line
        assert "Mercari（定價） 1 筆" in line


# ---------------------------------------------------------------------------
def _estimate(**kw):
    base = dict(
        fair_twd=1000.0,
        level="L1",
        level_label="卡名×稀有度×分數",
        n_effective=8,
        lo_twd=700.0,
        hi_twd=1400.0,
        confidence=0.8,
        calibration_n=80,
        p_worth_buying=0.6,
        card_name="はにわ",
        venue="buyee_mercari",
        venue_adjusted=True,
        venue_is_estimated=True,
    )
    base.update(kw)
    return Estimate(**base)


def _card(in_era=True, grade=9.0):
    return CardInfo(
        grader=Grader.PSA, grade=grade, in_era=in_era,
        era_evidence=["jp_kw:初期"] if in_era else [], rarity="ultra",
    )


def _match(in_era=True, name="はにわ", ocg="1999-01-01"):
    from ygo_sniper.cards import CardMatch

    return CardMatch(name_ja=name, in_era=in_era, ocg_date=ocg)


def _stats(n=6, lo=800, hi=1200, med=1000, tier=1):
    from ygo_sniper.appraise import TIER_LABELS

    return {
        "n": n, "tier": tier, "tier_label": TIER_LABELS[tier],
        "min_twd": lo, "max_twd": hi, "median_twd": med, "n_all_tiers": n,
    }


class TestVerdictStatesTheVenue:
    """判決引用的公允價必須說出它是哪個平台的價格水準。

    不說的話，「NT$1,000 的公允價」在 Yahoo 與 Mercari 是兩個完全不同的宣稱，
    而使用者無從分辨——那正是這次要修的混源比較。
    """

    def test_avoid_reason_says_which_venue(self):
        _v, reasons, numbers = decide_verdict(
            landed_twd=2500.0, estimate=_estimate(), comparables=[],
            comp_stats=_stats(), card=_card(), card_match=_match(),
        )
        assert "Mercari（定價）" in " ".join(reasons)
        assert numbers["venue"] == "buyee_mercari" and numbers["venue_adjusted"] is True

    def test_unadjusted_estimate_is_flagged_loudly(self):
        _v, reasons, numbers = decide_verdict(
            landed_twd=2500.0,
            estimate=_estimate(venue=None, venue_adjusted=False, venue_is_estimated=None),
            comparables=[], comp_stats=_stats(), card=_card(), card_match=_match(),
        )
        assert "沒有做平台校正" in " ".join(reasons)
        assert numbers["venue_adjusted"] is False

    def test_prior_only_venue_coefficient_is_disclosed(self):
        _v, reasons, _n = decide_verdict(
            landed_twd=2500.0, estimate=_estimate(venue_is_estimated=False),
            comparables=[], comp_stats=_stats(), card=_card(), card_match=_match(),
        )
        assert "係數是先驗" in " ".join(reasons)


class TestVerdict:
    """三級判決。否決器的語氣是設計的一部分，不是文案。"""

    def test_avoid_when_landed_above_interval_top(self):
        verdict, reasons, numbers = decide_verdict(
            landed_twd=2500.0, estimate=_estimate(), comparables=[],
            comp_stats=_stats(), card=_card(), card_match=_match(),
        )
        assert verdict == VERDICT_AVOID
        blob = " ".join(reasons)
        assert "不要買" in blob
        # 判決依據必須帶具體數字：同層成交範圍 + 你要付多少
        assert "NT$800–NT$1,200" in blob
        assert "NT$2,500" in blob
        assert numbers["landed_twd"] == 2500
        assert numbers["hi_twd"] == 1400

    def test_avoid_when_p_worth_below_threshold(self):
        verdict, reasons, numbers = decide_verdict(
            landed_twd=1300.0, estimate=_estimate(p_worth_buying=0.20),
            comparables=[], comp_stats=_stats(), card=_card(), card_match=_match(),
        )
        assert verdict == VERDICT_AVOID
        assert "20%" in " ".join(reasons)
        assert numbers["p_worth_buying"] == 0.20

    def test_worth_a_look_is_hedged_not_confident(self):
        verdict, reasons, _ = decide_verdict(
            landed_twd=500.0, estimate=_estimate(), comparables=[],
            comp_stats=_stats(), card=_card(), card_match=_match(),
        )
        assert verdict == VERDICT_WORTH
        blob = " ".join(reasons)
        assert "值得看一眼" in blob
        # 這條是定位的落實：說「值得買」時必須自曝信心有限
        assert "信心有限" in blob
        assert "NT$500" in blob

    def test_caution_says_cannot_judge_not_fine(self):
        verdict, reasons, _ = decide_verdict(
            landed_twd=1000.0, estimate=_estimate(), comparables=[],
            comp_stats=_stats(), card=_card(), card_match=_match(),
        )
        assert verdict == VERDICT_CAUTION
        assert "無法判斷" in " ".join(reasons)

    @pytest.mark.parametrize(
        "kwargs, needle",
        [
            ({"comp_stats": _stats(n=MIN_COMPARABLES - 1)}, "樣本太少"),
            ({"card_match": None}, "比對不到"),
            ({"card_match": _match(in_era=False, ocg="2020-03-07")}, "不是 1998-2004"),
            ({"card": _card(in_era=False)}, "年代證據"),
            ({"card": _card(grade=None)}, "抽不到鑑定分數"),
            ({"estimate": _estimate(lo_twd=None, hi_twd=None)}, "不給 80% 區間"),
            ({"estimate": _estimate(fair_twd=None)}, "沒有可用樣本"),
        ],
    )
    def test_data_gaps_force_caution_even_when_price_looks_great(self, kwargs, needle):
        """便宜到爆也不能因此升級成 WORTH——資料不足就是無法判斷，沒有折衷。"""
        args = dict(
            landed_twd=1.0, estimate=_estimate(), comparables=[],
            comp_stats=_stats(), card=_card(), card_match=_match(),
        )
        args.update(kwargs)
        verdict, reasons, _ = decide_verdict(**args)
        assert verdict == VERDICT_CAUTION
        blob = " ".join(reasons)
        assert "無法判斷" in blob
        assert needle in blob


# ---------------------------------------------------------------------------
class FakeFetcher:
    """把 fixture 當成網路回應。零網路的承重點。"""

    def __init__(self, html: str) -> None:
        self.html = html
        self.urls: list[str] = []
        self.closed = False

    def get(self, url, **_kw):
        self.urls.append(url)
        return self.html

    def close(self):
        self.closed = True


class FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def comps_by(self, **_kw):
        return list(self._rows)


@pytest.fixture
def appraise_env(cfg, fx):
    """組出一個零網路的 appraise 呼叫：假抓取 + 假行情庫 + 小卡表。

    行情列刻意生成 200 筆同卡同稀有度同分數：校準集才會過
    `min_calibration=50` 的門檻，區間與 P(值得買) 才會真的被算出來。
    """
    index = CardIndex(
        [{"id": 1, "name_ja": "はにわ", "name_en": "Hane-Hane", "ocg_date": "1999-04-01"}],
        {},
    )
    rows = [
        {
            "id": i,
            "title": f"ARS10 遊戯王 はにわ 初期 Vol.1 #{i}",
            "price_twd": 900.0 + (i % 11) * 20,
            "rarity": None,
            "grade": 10.0,
            "card_name": "はにわ",
            "url": f"https://buyee.jp/mercari/item/m{i}",
            "sold_at": f"2026-07-{i % 28 + 1:02d}",
            "era_evidence": "jp_kw:初期",
        }
        for i in range(200)
    ]

    def _run(html: str, auction_id: str):
        return appraise(
            cfg,
            f"https://auctions.yahoo.co.jp/jp/auction/{auction_id}",
            store=FakeStore(rows),
            fetcher=FakeFetcher(html),
            fx=fx,
            index=index,
        )

    return _run


class TestAppraiseEndToEnd:
    def test_full_report_on_buyout_item(self, appraise_env):
        r = appraise_env(YAHOO_BUYOUT, BUYOUT_ID)

        assert r.site == Site.BUYEE_YAHOO.value
        assert r.external_id == BUYOUT_ID
        assert r.fetched_via == "yahoo_native"
        assert r.buy_url == f"https://buyee.jp/item/yahoo/auction/{BUYOUT_ID}"

        # 卡片屬性：機構／分數／年代證據／卡名全部要在
        assert r.card["grader"] == "ARS"
        assert r.card["grade"] == 10.0
        assert r.card["in_era"] is True
        assert r.card["card_name"] == "はにわ"

        # 成本：每條可行路徑都要算，最便宜的那條要是清單第一筆
        assert len(r.routes) >= 2
        assert r.best_route["landed_twd"] == min(q["landed_twd"] for q in r.routes)

        # 估價：有樣本就要有層級與有效樣本數（點估計沒有這兩者就是誤導）
        assert r.estimate["fair_twd"] is not None
        assert r.estimate["level"] == "L1"
        assert r.estimate["n_effective"] > 0

        # 可比樣本：最重要的一欄，必須真的有東西且分級正確
        assert 0 < len(r.comparables) <= 10
        assert all(c["tier"] == 1 for c in r.comparables)
        assert r.comparable_stats["n"] == 200

        # 判決：即決 ¥30,000 遠高於 NT$900 級的行情
        assert r.verdict == VERDICT_AVOID
        assert r.verdict_numbers["landed_twd"] > r.verdict_numbers["hi_twd"]
        assert "否決器" in r.stance

    def test_injected_fetcher_is_not_closed_by_appraise(self, cfg, fx):
        """傳進來的 client 由呼叫端負責——關掉它會讓 dashboard 第二次呼叫就爆。"""
        fetcher = FakeFetcher(YAHOO_BUYOUT)
        appraise(
            cfg,
            f"https://auctions.yahoo.co.jp/jp/auction/{BUYOUT_ID}",
            store=FakeStore([]),
            fetcher=fetcher,
            fx=fx,
            index=CardIndex([], {}),
        )
        assert fetcher.closed is False

    def test_unsupported_url_never_touches_the_network(self, cfg, fx):
        fetcher = FakeFetcher(YAHOO_BUYOUT)
        with pytest.raises(UnsupportedUrlError):
            appraise(
                cfg, "https://jp.mercari.com/item/m123",
                store=FakeStore([]), fetcher=fetcher, fx=fx, index=CardIndex([], {}),
            )
        assert fetcher.urls == []

    def test_empty_comps_degrades_to_caution(self, cfg, fx):
        r = appraise(
            cfg,
            f"https://auctions.yahoo.co.jp/jp/auction/{BUYOUT_ID}",
            store=FakeStore([]),
            fetcher=FakeFetcher(YAHOO_BUYOUT),
            fx=fx,
            index=CardIndex([], {}),
        )
        assert r.verdict == VERDICT_CAUTION
        assert "無法判斷" in " ".join(r.verdict_reasons)
        # 沒有卡表時要明講，不能讓使用者以為比對過了
        assert any("卡片主檔" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# Mercari 台灣（tw.mercari.com）——本專案唯一一個**不是日圓**的來源
#
# 這一整段守的是同一條紅線：**標價幣別必須跟著價格一起走**。
# NT$5,751 被當成 ¥5,751 的話，到手成本會顯示 NT$1,2xx（低估 4.7 倍），
# 而且偏差方向正是「看起來超便宜、快按下去」。沒有任何錯誤訊息會出現。
# ---------------------------------------------------------------------------
TW_ID = "81e2722f-4c47-49e4-bec7-4f55e83f877a"


class TestMercariTw:
    def test_url_is_supported_and_id_keeps_its_dashes(self):
        t = parse_target(f"https://tw.mercari.com/zh-hant/items/{TW_ID}")
        assert t.site is Site.MERCARI_TW
        # UUID 含 `-`；別的站用的 `[A-Za-z0-9]+` 會在第一個 `-` 截斷成 "81e2722f"
        assert t.external_id == TW_ID
        assert t.fetch_mode == "mercari_tw"      # 純 httpx，不開瀏覽器
        assert t.buy_url == f"https://tw.mercari.com/zh-hant/items/{TW_ID}"

    @pytest.mark.parametrize(
        "url",
        [
            f"https://tw.mercari.com/items/{TW_ID}",
            f"https://tw.mercari.com/ja/items/{TW_ID}",
            f"https://tw.mercari.com/zh-hant/items/{TW_ID}?ref=search",
        ],
    )
    def test_locale_segment_is_optional(self, url):
        assert parse_target(url).external_id == TW_ID

    def test_price_is_twd_not_jpy(self):
        item = parse_mercari_tw_item(MERCARI_TW, "https://x.test")
        assert item.title == "【超稀少/蝕刻錯版】昆蟲女王 蝕刻 DL4【PSA3】"
        assert item.price == 5751
        # 這一行是整個功能的重點：幣別是從頁面讀的，不是預設的
        assert item.currency is Currency.TWD
        assert item.price_kind == "fixed"
        assert item.image_url and item.image_url.startswith("https://static.mercdn.net/")

    def test_price_note_says_no_second_fx_conversion(self):
        note = parse_mercari_tw_item(MERCARI_TW, "https://x.test").price_note
        assert "新台幣" in note and "不會再對它套一次匯率" in note

    def test_missing_price_fails_loudly(self):
        from ygo_sniper.sources.base import FetchError

        with pytest.raises(FetchError, match="抓不到價格"):
            parse_mercari_tw_item(
                "<html><head><meta property='og:title' content='x ‐ Mercari Japan'>"
                "</head><body>no price here</body></html>",
                "https://x.test",
            )

    def test_landed_cost_does_not_double_convert_currency(self, cfg, fx):
        """到手成本 = 台幣標價 + route 費用（日圓換台幣），**不再乘一次匯率**。"""
        from ygo_sniper.costs import quote_all_routes
        from ygo_sniper.domain import Listing

        lst = Listing(
            site=Site.MERCARI_TW, external_id=TW_ID, title="t",
            url="https://tw.mercari.com/zh-hant/items/" + TW_ID,
            price=5751, currency=Currency.TWD,
        )
        quotes = quote_all_routes(lst, cfg, fx)

        assert quotes, "Site.MERCARI_TW 沒有對應 route，標的會被靜默丟棄"
        assert [q.route for q in quotes] == ["mercari_tw"]  # 走不了 Buyee 集運
        q = quotes[0]
        # 商品那一段一比一（台幣付台幣，沒有匯差、沒有海外刷卡手續費）
        assert q.item_twd == 5751
        # 全部成本 = 商品 + 費用；把台幣當日圓的話這裡會掉到 1,300 上下
        assert q.landed_twd == pytest.approx(5751 + q.fee_twd + q.shipping_twd)
        assert 5900 < q.landed_twd < 6500

    def test_jpy_listing_cost_is_unchanged_by_the_twd_path(self, cfg, fx):
        """幣別分流不可以動到既有日圓標的的任何數字（回歸防線）。"""
        from ygo_sniper.costs import quote_route
        from ygo_sniper.domain import Listing

        lst = Listing(
            site=Site.BUYEE_MERCARI, external_id="m1", title="t",
            url="https://buyee.jp/mercari/item/m1", price=5751, currency=Currency.JPY,
        )
        q = quote_route(lst, cfg.routes["buyee_single"], fx)

        assert q.item_twd == pytest.approx(fx.to_twd(5751, Currency.JPY), abs=0.01)
        assert q.item_twd < 1500      # 日圓路徑仍然要換匯


# ---------------------------------------------------------------------------
# 從商品描述補抓鑑定分數（缺口一）
#
# 這一段守的是「補抓到的分數會不會安靜地錯」：分數直接乘進公允價，
# 而分數溢價橫跨 11 倍（PSA7 ×0.35 到 PSA10 ×3.95）。所以每一條測的都是
# **降級路徑**（矛盾／沒有描述／抓不到頁面）有沒有留下可行動的訊息。
# ---------------------------------------------------------------------------
class TestGradeRecoveryInAppraise:
    def test_yahoo_item_carries_the_seller_description(self):
        """描述是補抓的唯一原料。抓不到它，整條路徑就只是個空殼。"""
        item = parse_yahoo_item(YAHOO_BUYOUT, "https://x.test")
        assert item.description and "ARS鑑定" in item.description

    def test_buyee_pages_have_no_description_and_say_so(self):
        """Buyee 代購頁**不轉載賣家描述**（2026-08-02 實測兩個平台各一頁）。

        `description is None` 與「有描述但沒寫分數」是兩件事：前者要叫使用者
        去原站看，後者要叫他看照片。壓成同一個布林值就分不出下一步。
        """
        assert parse_buyee_item(BUYEE_MERCARI, "https://x.test").description is None
        assert parse_buyee_item(BUYEE_PAYPAY, "https://x.test").description is None

    def test_description_grade_fills_the_card_and_labels_its_source(self):
        from ygo_sniper.appraise import ItemPage, apply_grade_resolution

        card = CardInfo(grader=Grader.PSA, grade=None)
        item = ItemPage(
            title="PSA 遊戯王 暗黒の竜王 初期", price=1000.0, currency=Currency.JPY,
            price_kind="fixed", price_note="", description="PSA5ですが、美品です",
        )
        resolution = apply_grade_resolution(card, item)

        assert card.grade == 5.0 and card.grade_source == "description"
        assert resolution.source == "description"

    def test_a_contradicting_description_wipes_the_title_grade(self):
        """**唯一會降級已知分數的地方**：矛盾時連標題的分數都不採信。"""
        from ygo_sniper.appraise import ItemPage, apply_grade_resolution

        card = CardInfo(grader=Grader.PSA, grade=10.0, grade_source="title")
        item = ItemPage(
            title="【PSA10】遊戯王 青眼の白龍 初期", price=1000.0, currency=Currency.JPY,
            price_kind="fixed", price_note="", description="実際はPSA8です",
        )
        apply_grade_resolution(card, item)

        assert card.grade is None and card.grade_source is None

    def test_report_exposes_the_grade_provenance(self, appraise_env):
        r = appraise_env(YAHOO_BUYOUT, BUYOUT_ID)
        assert r.card["grade"] == 10.0
        assert r.card["grade_source"] == "title"
        assert r.card["grade_conflict"] is False
        assert r.card["has_description"] is True
        assert any("分數來自" in w for w in r.warnings)

    def test_unknown_grade_report_points_at_the_photo_and_the_link(self, cfg, fx):
        """分數補不到時，報告要給**下一步**（看照片上的殼）＋商品頁連結。"""
        html = YAHOO_BUYOUT.replace("ARS10 遊戯王 はにわ", "遊戯王 PSA鑑定品 はにわ")
        report = appraise(
            cfg, f"https://auctions.yahoo.co.jp/jp/auction/{BUYOUT_ID}",
            store=FakeStore([]), fetcher=FakeFetcher(html), fx=fx, index=CardIndex([], {}),
        )
        assert report.card["grade"] is None
        assert any("鑑定殼" in w and report.buy_url in w for w in report.warnings)

    def test_estimate_flags_a_description_sourced_grade(self, cfg):
        """描述來的分數要在估價的 notes 裡自己說出來（可信度低於標題）。"""
        from ygo_sniper.valuation import Obs, Params, Valuator

        rows = [
            Obs(price_twd=1000.0 + i, card_name="はにわ", rarity="ultra",
                grade=10.0, key=f"k{i}", venue="buyee_yahoo")
            for i in range(200)
        ]
        v = Valuator(rows, Params())
        est = v.estimate(card_name="はにわ", rarity="ultra", grade=10.0,
                         grade_source="description", venue="buyee_yahoo")
        assert est.grade_source == "description"
        assert any("商品描述" in n for n in est.notes)


class TestRecoverMissingGrades:
    """批次補抓（`recover_missing_grades`）。零網路。

    重點在**降級路徑**：抓不到頁面、頁面沒有描述、描述與標題矛盾，
    三者的下一步不同，所以不可以壓成一個「失敗」。
    """

    @staticmethod
    def _row(title="遊戯王 PSA鑑定品 青眼の白龍 初期", url=None):
        import json

        url = url or "https://buyee.jp/item/yahoo/auction/n1"
        return {
            "key": "buyee_yahoo:n1",
            "payload": json.dumps({
                "listing": {
                    "site": "buyee_yahoo", "external_id": "n1", "title": title,
                    "url": url, "price": 1000.0, "currency": "JPY",
                    "raw": {"price_kind": "current_bid"},
                },
                "card": {"grader": "PSA", "grade": None, "in_era": True,
                         "era_evidence": ["jp_kw:初期"], "rarity": "ultra"},
                "bid": {"ok": False},
            }),
        }

    class _Comps:
        def stats_for(self, listing, info):
            from ygo_sniper.domain import CompStats

            return CompStats(n=0, median_twd=None, p25_twd=None, p40_twd=None,
                             p75_twd=None, window_days=90)

    class _Valuator:
        index = None

        def estimate(self, **kw):
            return Estimate(fair_twd=5000.0, level="L1", level_label="同卡",
                            n_effective=5, lo_twd=4000.0, hi_twd=9000.0,
                            calibration_n=80, grade=kw.get("grade"),
                            grade_source=kw.get("grade_source"),
                            calibration_group="L1/3-9", calibration_group_n=71,
                            calibration_group_requested="L1/3-9",
                            venue="buyee_yahoo", venue_adjusted=True)

    def _run(self, cfg, fx, html, rows, apply_to=None):
        from ygo_sniper.appraise import recover_missing_grades

        return recover_missing_grades(
            cfg, rows, fx=fx, comps_engine=self._Comps(), valuator=self._Valuator(),
            fetcher=FakeFetcher(html), apply_to=apply_to,
        )

    def test_recovered_grade_unlocks_the_bid_ceiling(self, cfg, fx):
        """補到分數 → `require_known_grade` 這道閘門才有可能過。

        這條就是「多了幾筆可行動標的」那句話的機制證明。
        """
        html = YAHOO_BID_ONLY.replace("PSA10 ゼラ", "PSA鑑定品 ゼラ")
        [r] = self._run(cfg, fx, html, [self._row()])

        assert r.recovered and r.grade == 10.0 and r.grade_source == "description"
        assert r.before_bid_ok is False and r.after_bid_ok is True
        assert r.after_bid_jpy and r.after_bid_jpy > 0

    def test_apply_writes_back_only_when_asked(self, cfg, fx):
        html = YAHOO_BID_ONLY.replace("PSA10 ゼラ", "PSA鑑定品 ゼラ")

        class _Sink:
            def __init__(self):
                self.saved = []

            def upsert_signal(self, sig):
                self.saved.append(sig)

        self._run(cfg, fx, html, [self._row()])          # dry-run
        sink = _Sink()
        self._run(cfg, fx, html, [self._row()], apply_to=sink)
        assert len(sink.saved) == 1
        assert sink.saved[0].card.grade == 10.0
        assert sink.saved[0].card.grade_source == "description"

    def test_unsupported_site_says_so_and_points_at_the_photo(self, cfg, fx):
        # 用一個**真的不支援**的站（jp.mercari.com 直站）：eBay 商品頁自
        # 2026-08-03 起走 Browse API，拿它當「不支援」的例子會讓這個測試
        # 安靜地變成在測別的東西（而且會真的打網路）。
        rows = [self._row(url="https://jp.mercari.com/item/m48967074463")]
        [r] = self._run(cfg, fx, YAHOO_BUYOUT, rows)

        assert not r.recovered and r.fetch_error == "不支援的商品頁網址"
        assert "鑑定殼" in r.note and not r.has_description

    def test_a_contradicting_description_is_reported_as_a_conflict(self, cfg, fx):
        """矛盾要**看得見**——不是安靜地當成「沒補到」。"""
        html = YAHOO_BID_ONLY.replace("PSA10 ゼラ", "PSA8 ゼラ")   # 標題 8、描述 10
        [r] = self._run(cfg, fx, html, [self._row()])

        assert not r.recovered and r.conflict and r.grade is None
        assert r.after_bid_ok is False


# ---------------------------------------------------------------------------
# eBay（ebay.com/itm/{商品號}）——2026-08-03 起支援
#
# 這一段守兩條紅線，兩條都是「錯了看起來像成功」的那種：
#
# 1. **價格語意**：eBay 有三種形狀（定價／純競標／競標帶 BIN），而**單品端點的
#    純競標標的 `price` 有值、且等於 `currentBidPrice`**（實測）。直接讀 price
#    就是把別人的出價當成售價——與 Yahoo 現在価格是同一個錯，方向一樣是
#    系統性低估（每筆都像撿到寶）。判準只能是 `buyingOptions`。
# 2. **運費與幣別**：eBay 的國際運費常佔到手成本三到五成（實測 653 + 435），
#    而顯示的台幣是 eBay 換算的估算值（實際以賣家幣別請款）——兩件事沒說清楚，
#    使用者看到的就是一個孤零零、而且比實際便宜的數字。
#
# fixture 是 2026-08-03 實抓的 Browse API 回應（tests/fixtures/ebay_api_items.json），
# 所有抓取都用假 source 注入，零網路。
# ---------------------------------------------------------------------------
EBAY_ITEMS = json.loads((FIXTURES / "ebay_api_items.json").read_text(encoding="utf-8"))
EBAY_FIXED = EBAY_ITEMS["item_fixed"]                    # 407031244912，653 + 435 TWD
EBAY_AUCTION = EBAY_ITEMS["item_auction"]                # 純競標，price == currentBidPrice
EBAY_AUCTION_BIDS = EBAY_ITEMS["item_auction_with_bids"]  # 純競標、5 次出價、無運費報價
EBAY_AUCTION_BIN = EBAY_ITEMS["item_auction_bin"]        # 競標帶 BIN
EBAY_ID = "407031244912"


class FakeEbay:
    """假的 EbaySource：回 fixture，不打網路（token 也不取）。

    `us_blob` 是「帶美國地址 context 重查」時要回的 fixture（鑑價替代路徑）；
    沒給就回同一份。`contexts` 記下每次呼叫帶的 ENDUSERCTX，測試靠它驗證
    「只對 TW 不可寄的標的多查一次、而且查的是美國 context」。
    """

    def __init__(self, blob, error=None, us_blob=None):
        self.blob = blob
        self.error = error
        self.us_blob = us_blob
        self.calls: list[str] = []
        self.contexts: list[str | None] = []

    def get_item(self, item_id, *, context=None):
        self.calls.append(item_id)
        self.contexts.append(context)
        if self.error is not None:
            raise self.error
        if context is not None and self.us_blob is not None:
            return self.us_blob
        return self.blob


class TestEbayUrl:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://www.ebay.com/itm/{EBAY_ID}",
            f"https://ebay.com/itm/{EBAY_ID}",
            f"https://www.ebay.com/itm/{EBAY_ID}/",
            f"https://www.ebay.com/itm/{EBAY_ID}?_skw=yugioh&hash=item5ec0f1b2f0",
            # 舊式含標題 slug 的商品網址（商品號永遠是最後那段純數字）
            f"https://www.ebay.com/itm/Yugioh-Terrorking-Archfiend-DCR-072/{EBAY_ID}",
        ],
    )
    def test_item_urls_are_supported(self, url):
        t = parse_target(url)
        assert t.site is Site.EBAY
        assert t.external_id == EBAY_ID
        assert t.fetch_mode == "ebay_api"
        # 購買端是人看得懂的商品頁；抓取端是 Browse API 的單品端點
        assert t.buy_url == f"https://www.ebay.com/itm/{EBAY_ID}"
        assert t.fetch_url.endswith(f"item/v1|{EBAY_ID}|0")

    def test_supported_forms_mention_ebay(self):
        """錯誤訊息要把 eBay 列進去——不然使用者不知道現在貼得了。"""
        with pytest.raises(UnsupportedUrlError) as exc:
            parse_target("https://www.ebay.com/sch/i.html?_nkw=yugioh")
        assert "ebay.com/itm/" in str(exc.value)


class TestEbayPriceSemantics:
    """紅線：目前出價 ≠ 你付得出去的價格。"""

    def test_fixed_price_item_is_transactable(self):
        item = parse_ebay_item(EBAY_FIXED, "https://x.test")
        assert item.price_kind == "buyout"
        assert (item.price, item.currency) == (653.0, Currency.TWD)
        assert item.shipping_cost == 435.0 and item.shipping_note is None
        assert item.current_bid is None
        assert item.seller == "reeceyt-25" or item.seller
        assert item.ships_to_tw is True
        assert item.converted_from == "GBP 14.99"
        assert "點下去就能成交" in item.price_note

    def test_pure_auction_uses_current_bid_and_says_it_will_go_higher(self):
        """**單品端點的純競標 `price` 有值且等於 currentBidPrice**——讀 price 就完蛋。"""
        assert EBAY_AUCTION_BIDS["price"]["value"] == \
            EBAY_AUCTION_BIDS["currentBidPrice"]["value"]      # 前提：實測事實還在

        item = parse_ebay_item(EBAY_AUCTION_BIDS, "https://x.test")
        assert item.price_kind == LIVE_AUCTION_KIND
        assert item.price == 27783.0 and item.current_bid == 27783.0
        assert item.bids == 5
        assert item.end_time and item.end_time.startswith("2026-08-03")
        assert "不是你付得出去的價格" in item.price_note
        assert "會更高" in item.price_note

    def test_price_kind_matches_the_bidding_engine_constant(self):
        """`price_kind` 必須是 `bidding.LIVE_AUCTION_KIND`，不是抄一份字串。

        下游（scoring 的競標分流、出價上限、推播規則 2）全部認那個常數；
        兩份定義就會有一天出現「這邊當競標、那邊當即決」。
        """
        item = parse_ebay_item(EBAY_AUCTION, "https://x.test")
        listing = Listing(
            site=Site.EBAY, external_id="1", title=item.title,
            url="https://x.test", price=item.price, currency=item.currency,
            raw={"price_kind": item.price_kind},
        )
        assert is_live_auction(listing) is True

    def test_auction_with_buy_it_now_uses_the_bin_price(self):
        """競標帶 BIN：BIN 才是可成交價，但目前出價也要說出來。"""
        item = parse_ebay_item(EBAY_AUCTION_BIN, "https://x.test")
        assert item.price_kind == "buyout"
        assert item.price == 32661.0          # BIN
        assert item.current_bid == 12922.0    # 目前出價，不是拿來算成本的那個
        assert is_live_auction(
            Listing(site=Site.EBAY, external_id="1", title="t", url="u",
                    price=item.price, currency=item.currency,
                    raw={"price_kind": item.price_kind})
        ) is False
        assert "同時在競標" in item.price_note

    def test_unknown_currency_is_refused_not_guessed(self):
        blob = copy.deepcopy(EBAY_FIXED)
        blob["price"] = {"value": "14.99", "currency": "GBP"}
        with pytest.raises(FetchError) as exc:
            parse_ebay_item(blob, "https://x.test")
        assert "GBP" in str(exc.value)


class TestEbayShipping:
    def test_ship_to_locations_excluding_tw_is_false(self):
        blob = copy.deepcopy(EBAY_FIXED)
        blob["shipToLocations"] = {
            "regionIncluded": [{"regionId": "WORLDWIDE"}],
            "regionExcluded": [{"regionId": "TW"}],
        }
        assert parse_ebay_item(blob, "https://x.test").ships_to_tw is False

    def test_ship_to_locations_without_tw_is_false(self):
        blob = copy.deepcopy(EBAY_FIXED)
        blob["shipToLocations"] = {"regionIncluded": [{"regionId": "US"}]}
        assert parse_ebay_item(blob, "https://x.test").ships_to_tw is False

    def test_missing_ship_to_locations_is_unknown_not_true(self):
        blob = copy.deepcopy(EBAY_FIXED)
        blob.pop("shipToLocations")
        assert parse_ebay_item(blob, "https://x.test").ships_to_tw is None

    def test_shipping_in_another_currency_is_unknown_not_added(self):
        """商品 TWD、運費 USD 直接相加會低估運費 30 倍（工程原則 1）。"""
        blob = copy.deepcopy(EBAY_FIXED)
        blob["shippingOptions"] = [{"shippingCost": {"value": "9.99", "currency": "USD"}}]
        item = parse_ebay_item(blob, "https://x.test")
        assert item.shipping_cost is None
        assert "幣別" in (item.shipping_note or "")


class TestEbayAppraiseEndToEnd:
    def _run(self, cfg, fx, blob, url=None):
        ebay = FakeEbay(blob)
        report = appraise(
            cfg,
            url or f"https://www.ebay.com/itm/{EBAY_ID}",
            store=FakeStore([]),
            fetcher=FakeFetcher(""),        # eBay 這條路不該碰 fetcher
            fx=fx,
            index=CardIndex([], {}),
            ebay=ebay,
        )
        return report, ebay

    def test_report_uses_the_ebay_direct_route_and_shows_the_breakdown(self, cfg, fx):
        report, ebay = self._run(cfg, fx, EBAY_FIXED)

        assert ebay.calls == [EBAY_ID]                      # 走 API，沒開瀏覽器
        assert report.site == Site.EBAY.value
        assert report.fetched_via == "ebay_api"
        assert report.best_route["route"] == "ebay_direct"
        # 到手成本 = 商品 + 運費（eBay 直寄沒有代購費），三個數字必須自洽
        best = report.best_route
        assert best["fee_twd"] == 0.0
        assert round(best["item_twd"] + best["shipping_twd"], 2) == best["landed_twd"]
        assert report.item["shipping_cost"] == 435.0
        assert report.item["currency"] == "TWD"

    def test_shipping_share_is_spelled_out_in_the_warnings(self, cfg, fx):
        """運費佔 40% 這件事要看得見——使用者先前就是因為只看到總數而困惑。"""
        report, _ = self._run(cfg, fx, EBAY_FIXED)
        line = next(w for w in report.warnings if "運費佔到手成本" in w)
        ratio = report.best_route["overhead_ratio"]
        assert 0.35 < ratio < 0.45                          # 653 + 435 → 約四成
        assert f"{ratio:.0%}" in line
        # 拆解的三個數字都要出現在同一句話裡（分開讀就會被拿去跟別的基準比）
        for key in ("landed_twd", "item_twd", "shipping_twd"):
            assert f"{report.best_route[key]:,.0f}" in line

    def test_twd_is_flagged_as_an_ebay_estimate_not_local_billing(self, cfg, fx):
        """與 Mercari 台灣方向相反：eBay 的台幣要套刷卡加成，不可互相參照。"""
        report, _ = self._run(cfg, fx, EBAY_FIXED)
        note = next(w for w in report.warnings if "估算值" in w and "刷卡加成" in w)
        assert "Mercari 台灣" in note and "GBP 14.99" in note

    def test_auction_report_says_the_price_will_go_higher(self, cfg, fx):
        report, _ = self._run(cfg, fx, EBAY_AUCTION_BIDS)

        assert report.item["price_kind"] == LIVE_AUCTION_KIND
        blob = " ".join(report.warnings)
        assert "不是你付得出去的價格" in blob and "會更高" in blob
        # 2026-08-03 起 eBay 競標的上限在掃描端計算（max_bid_ebay），報告要指路
        # 而不是說「不提供」；也要講出 eBay 原生自動出價（設好上限即可離開）。
        assert "掃描端" in blob and "automatic bidding" in blob
        # 這一筆沒有運費報價：未知不是零，而且要說是低估
        assert any("運費是未知的，不是零" in w and "低估" in w for w in report.warnings)

    def test_no_shipping_to_tw_is_a_loud_warning(self, cfg, fx):
        blob = copy.deepcopy(EBAY_FIXED)
        blob["shipToLocations"] = {"regionIncluded": [{"regionId": "US"}]}
        report, _ = self._run(cfg, fx, blob)
        assert report.item["ships_to_tw"] is False
        assert any("不含台灣" in w for w in report.warnings)

    # --- 美國地址（buying.us_ship_zip）替代路徑 -----------------------------
    @staticmethod
    def _us_blob():
        """帶美國 context 重查時 eBay 會回的形狀：原幣（USD）、美國境內運費。"""
        return {
            "itemId": f"v1|{EBAY_ID}|0",
            "title": EBAY_FIXED["title"],
            "price": {"value": "14.99", "currency": "USD"},
            "buyingOptions": ["FIXED_PRICE"],
            "shippingOptions": [
                {"shippingCost": {"value": "4.50", "currency": "USD"}}
            ],
            "shipToLocations": {"regionIncluded": [{"regionId": "US"}]},
        }

    def test_tw_shippable_item_does_not_trigger_a_us_query(self, cfg, fx):
        """成本控制：寄得到台灣就**不多打** API——替代路徑只查用得上的。"""
        report, ebay = self._run(cfg, fx, EBAY_FIXED)   # WORLDWIDE → 寄得到
        assert ebay.calls == [EBAY_ID]
        assert report.us_ship_option is None

    def test_us_alternative_path_is_reported_with_the_transship_warning(self, cfg, fx):
        """TW 不可寄 → 用 zip 91762 重查一次，列出替代路徑＋**轉運警告**。"""
        blob = copy.deepcopy(EBAY_FIXED)
        blob["shipToLocations"] = {"regionIncluded": [{"regionId": "US"}]}
        ebay = FakeEbay(blob, us_blob=self._us_blob())
        report = appraise(
            cfg, f"https://www.ebay.com/itm/{EBAY_ID}",
            store=FakeStore([]), fetcher=FakeFetcher(""), fx=fx,
            index=CardIndex([], {}), ebay=ebay,
        )
        # 第二次呼叫帶的是美國 context（zip 來自 settings.yaml buying.us_ship_zip）
        assert ebay.calls == [EBAY_ID, EBAY_ID]
        assert ebay.contexts == [None, "contextualLocation=country=US,zip=91762"]
        opt = report.us_ship_option
        assert opt is not None
        assert opt["zip"] == "91762" and opt["currency"] == "USD"
        assert opt["item_price"] == 14.99 and opt["shipping"] == 4.50
        assert opt["landed_us"] == pytest.approx(19.49)
        line = next(w for w in report.warnings if "91762" in w and "替代路徑" in w)
        assert "不含美國→台灣的轉運成本" in line and "貨會留在美國" in line
        assert "US$14.99" in line and "US$4.50" in line and "US$19.49" in line

    def test_us_query_failure_does_not_break_the_report(self, cfg, fx):
        """替代路徑是加分項：US 重查炸掉，主報告照出，而且「查失敗」要說出來。"""
        blob = copy.deepcopy(EBAY_FIXED)
        blob["shipToLocations"] = {"regionIncluded": [{"regionId": "US"}]}

        class FlakyEbay(FakeEbay):
            def get_item(self, item_id, *, context=None):
                if context is not None:
                    raise RuntimeError("US query boom")
                return super().get_item(item_id, context=context)

        report = appraise(
            cfg, f"https://www.ebay.com/itm/{EBAY_ID}",
            store=FakeStore([]), fetcher=FakeFetcher(""), fx=fx,
            index=CardIndex([], {}), ebay=FlakyEbay(blob, us_blob=self._us_blob()),
        )
        assert report.us_ship_option is None
        assert any("替代路徑無法評估" in w for w in report.warnings)
        assert report.verdict  # 主報告完好

    def test_us_alternative_reports_infeasibility_when_us_shipping_is_absent(self, cfg, fx):
        """US 重查回來也沒有運費 → 明說「不可行或無法報價」，不是安靜消失。"""
        blob = copy.deepcopy(EBAY_FIXED)
        blob["shipToLocations"] = {"regionIncluded": [{"regionId": "US"}]}
        us = self._us_blob()
        us.pop("shippingOptions")
        report = appraise(
            cfg, f"https://www.ebay.com/itm/{EBAY_ID}",
            store=FakeStore([]), fetcher=FakeFetcher(""), fx=fx,
            index=CardIndex([], {}), ebay=FakeEbay(blob, us_blob=us),
        )
        assert report.us_ship_option is None
        assert any("不可行或無法報價" in w for w in report.warnings)

    def test_api_failures_keep_their_classification(self, cfg, fx):
        from ygo_sniper.sources.ebay import EbayAuthError, EbayItemNotFound

        for exc, transient in ((EbayAuthError("no creds"), False),
                               (EbayItemNotFound("gone"), False)):
            with pytest.raises(FetchError) as raised:
                appraise(
                    cfg, f"https://www.ebay.com/itm/{EBAY_ID}",
                    store=FakeStore([]), fx=fx, index=CardIndex([], {}),
                    ebay=FakeEbay(None, error=exc),
                )
            assert raised.value.transient is transient
