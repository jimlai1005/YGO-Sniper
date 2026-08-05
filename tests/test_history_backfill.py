"""歷史成交回填：續跑帳本、冪等、請求預算（2026-08-04）。零網路。

回填是一次性的幾十到上百個請求，而它會被 Ctrl-C、被網路中斷、被預算截斷。
這個檔案釘住的是三件「壞了看不出來」的事：

1. **續跑**：第二輪要從上次的下一頁接下去，不是從第 1 頁重抓
   （重抓的症狀是「跑很久、資料沒變多」——看起來像市場沒東西，其實是白跑）。
2. **翻完了就別再翻**：`archive_exhausted` 的查詢直接跳過；但**錯誤中斷
   不算翻完**——把一次連線失敗記成「這個查詢沒東西了」會永遠少收那批資料。
3. **請求預算是硬的**：超過就停下來記帳，不是靜默截斷尾端。

另外驗一條 `yahoo_closed` 的 `first_page`：沒有它，深挖只能整個查詢重抓。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from ygo_sniper.history import (
    HISTORY_META_KEY,
    HistoryParams,
    load_ledger,
    mine_paypay_seller,
    next_page_for,
    reset_ledger,
    run_yahoo_backfill,
)
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
@dataclass
class FakeListing:
    seller_id: str | None = "s1"
    raw: dict = field(default_factory=lambda: {"sold_at": "2026-05-01T00:00:00+00:00"})


class FakeSource:
    """記下每次被要求的 (query, pages, first_page)，回可控的結果。"""

    def __init__(self, *, per_page=3, total_pages=99, exhaust_at=None, fail_on=()):
        self.calls: list[tuple[str, int, int]] = []
        self.per_page = per_page
        self.total_pages = total_pages
        self.exhaust_at = exhaust_at
        self.fail_on = set(fail_on)

    def search_detailed(self, query, *, pages=1, first_page=1, **_):
        self.calls.append((query, pages, first_page))
        if query in self.fail_on:
            raise httpx.ConnectError("連線失敗")
        last = min(first_page + pages - 1, self.total_pages)
        fetched = max(0, last - first_page + 1)
        res = SearchResult(source="yahoo_closed", site="buyee_yahoo", query=query)
        res.pages_fetched = fetched
        res.listings = [FakeListing() for _ in range(fetched * self.per_page)]
        res.archive_exhausted = bool(
            self.exhaust_at is not None and last >= self.exhaust_at
        )
        return res


class FakeComps:
    """只記下被餵了幾筆；入庫的冪等由 store 自己保證（另有測試）。"""

    def __init__(self):
        self._index: dict = {}
        self.seen = 0

    def ingest_sold(self, listings):
        self.seen += len(listings)

        class R:
            kept = len(listings)
            rejected = 0

        return R()


# ---------------------------------------------------------------------------
# 1. 續跑
# ---------------------------------------------------------------------------
def test_second_run_resumes_from_the_next_page(store):
    src, comps = FakeSource(), FakeComps()
    p = HistoryParams(pages=3, max_requests=100)

    run_yahoo_backfill(store=store, comps=comps, source=src, queries=["A"], params=p)
    assert src.calls == [("A", 3, 1)]
    assert load_ledger(store)["A"]["pages_done"] == 3

    # 同樣的深度 → 什麼都不用做（不是重抓）
    run_yahoo_backfill(store=store, comps=comps, source=src, queries=["A"], params=p)
    assert src.calls == [("A", 3, 1)]

    # 想挖更深 → 從第 4 頁接下去，**只抓新的那 2 頁**
    deeper = HistoryParams(pages=5, max_requests=100)
    run_yahoo_backfill(store=store, comps=comps, source=src, queries=["A"], params=deeper)
    assert src.calls[-1] == ("A", 2, 4)
    assert load_ledger(store)["A"]["pages_done"] == 5


def test_next_page_for_is_one_when_nothing_recorded(store):
    assert next_page_for({}, "A") == 1
    assert next_page_for({"A": {"pages_done": 4}}, "A") == 5
    assert next_page_for({"A": {"pages_done": "壞掉的值"}}, "A") == 1


def test_broken_ledger_json_does_not_raise(store):
    store.set_meta(HISTORY_META_KEY, "{不是 JSON")
    assert load_ledger(store) == {}


def test_reset_ledger_starts_over(store):
    src, comps = FakeSource(), FakeComps()
    p = HistoryParams(pages=2, max_requests=100)
    run_yahoo_backfill(store=store, comps=comps, source=src, queries=["A"], params=p)
    reset_ledger(store)

    assert load_ledger(store) == {}
    run_yahoo_backfill(store=store, comps=comps, source=src, queries=["A"], params=p)
    assert src.calls[-1] == ("A", 2, 1)


# ---------------------------------------------------------------------------
# 2. 翻完了 vs 被錯誤中斷
# ---------------------------------------------------------------------------
def test_exhausted_query_is_skipped_next_time(store):
    src, comps = FakeSource(exhaust_at=2, total_pages=2), FakeComps()
    p = HistoryParams(pages=9, max_requests=100)

    first = run_yahoo_backfill(store=store, comps=comps, source=src, queries=["A"], params=p)
    assert first.outcomes[0].exhausted
    assert load_ledger(store)["A"]["exhausted"] is True

    second = run_yahoo_backfill(store=store, comps=comps, source=src, queries=["A"], params=p)
    assert len(src.calls) == 1, "翻完的查詢不該再打請求"
    assert second.skipped and "翻完" in second.skipped[0][1]


def test_redo_exhausted_reopens_the_query(store):
    src, comps = FakeSource(exhaust_at=2, total_pages=2), FakeComps()
    run_yahoo_backfill(
        store=store, comps=comps, source=src, queries=["A"],
        params=HistoryParams(pages=9, max_requests=100),
    )
    run_yahoo_backfill(
        store=store, comps=comps, source=src, queries=["A"],
        params=HistoryParams(pages=9, max_requests=100, redo_exhausted=True),
    )
    assert len(src.calls) == 2


def test_a_failed_query_is_not_recorded_as_exhausted(store):
    """一次連線失敗**不得**被記成「這個查詢沒東西了」——那是安靜地少收資料。"""
    src, comps = FakeSource(fail_on=["A"]), FakeComps()
    p = HistoryParams(pages=3, max_requests=100)

    report = run_yahoo_backfill(
        store=store, comps=comps, source=src, queries=["A", "B"], params=p
    )
    assert report.errors and "A" in report.errors[0]
    assert "A" not in load_ledger(store), "失敗的查詢不進帳本，下次照樣從第 1 頁重來"
    assert load_ledger(store)["B"]["pages_done"] == 3

    run_yahoo_backfill(store=store, comps=comps, source=src, queries=["A"], params=p)
    assert src.calls[-1] == ("A", 3, 1)


# ---------------------------------------------------------------------------
# 3. 請求預算
# ---------------------------------------------------------------------------
def test_request_budget_stops_and_records(store):
    src, comps = FakeSource(), FakeComps()
    p = HistoryParams(pages=4, max_requests=6)

    report = run_yahoo_backfill(
        store=store, comps=comps, source=src, queries=["A", "B", "C"], params=p
    )
    assert report.requests <= 6
    assert report.budget_hit
    # 第三個查詢完全沒打（預算用完），而且說得出為什麼
    assert any("預算" in why for _kw, why in report.skipped)

    # 續跑：沒做完的那個從它自己的下一頁開始
    ledger = load_ledger(store)
    assert ledger["A"]["pages_done"] == 4
    assert next_page_for(ledger, "C") == 1


def test_dry_run_touches_nothing(store):
    src, comps = FakeSource(), FakeComps()
    report = run_yahoo_backfill(
        store=store, comps=comps, source=src, queries=["A"],
        params=HistoryParams(pages=3, max_requests=100), dry_run=True,
    )
    assert src.calls == []
    assert comps.seen == 0
    assert load_ledger(store) == {}
    assert report.requests == 3 and report.dry_run


# ---------------------------------------------------------------------------
# 4. 入庫冪等（真的 store，真的 comps 引擎）
# ---------------------------------------------------------------------------
def test_ingesting_the_same_sold_row_twice_does_not_duplicate(store, cfg, fx):
    from ygo_sniper.comps import CompsEngine
    from ygo_sniper.domain import Currency, Listing, Site

    lst = Listing(
        site=Site.BUYEE_YAHOO, external_id="l1", title="遊戯王 初期 ウルトラ PSA9 青眼の白龍",
        url="https://buyee.jp/item/yahoo/auction/l1", price=20000.0, currency=Currency.JPY,
        seller_id="seller-a", is_sold=True,
        raw={"sold_at": "2026-05-01T00:00:00+00:00", "price_kind": "sold_price"},
    )
    engine = CompsEngine(cfg, fx, store)
    engine.ingest_sold([lst])
    engine._index = {}
    engine.ingest_sold([lst])

    rows = store.comps_by(limit=100)
    assert len(rows) == 1
    assert rows[0]["seller_id"] == "seller-a"


def test_seller_id_is_backfilled_onto_rows_that_had_none(store, cfg, fx):
    """賣家欄位上線前入庫的那批列，重掃時要被補上賣家（**只補 NULL**）。

    這是回填的主要收穫：庫裡 796 筆 Yahoo 成交只有 43 筆帶賣家，
    重掃的價值有一大半不是新資料，是把賣家補回去。
    """
    from ygo_sniper.comps import CompsEngine
    from ygo_sniper.domain import Currency, Listing, Site

    base = dict(
        site=Site.BUYEE_YAHOO, external_id="l2", title="遊戯王 初期 ウルトラ PSA9 青眼の白龍",
        url="https://buyee.jp/item/yahoo/auction/l2", price=20000.0, currency=Currency.JPY,
        is_sold=True, raw={"sold_at": "2026-05-01T00:00:00+00:00"},
    )
    engine = CompsEngine(cfg, fx, store)
    engine.ingest_sold([Listing(**base, seller_id=None)])
    assert store.comps_by(limit=10)[0]["seller_id"] is None

    engine._index = {}
    engine.ingest_sold([Listing(**base, seller_id="seller-b")])
    rows = store.comps_by(limit=10)
    assert len(rows) == 1
    assert rows[0]["seller_id"] == "seller-b"


# ---------------------------------------------------------------------------
# 5. yahoo_closed 的 first_page（沒有它就只能整個查詢重抓）
# ---------------------------------------------------------------------------
def test_yahoo_closed_first_page_shifts_the_offset(cfg, tmp_path):
    from dataclasses import replace

    from ygo_sniper.sources.base import CachedFetcher
    from ygo_sniper.sources.yahoo_closed import _PAGE_SIZE, YahooClosedSource

    seen: list[str] = []
    body = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({
            "props": {"pageProps": {"initialState": {"search": {"items": {"listing": {
                "totalResultsAvailable": 999, "items": [],
            }}}}}}
        })
        + "</script>"
        # 撐過 CachedFetcher 的「2xx 但 body 太短 ＝ 被擋」門檻（512 bytes），
        # 不然這條測到的會是 BLOCKED 而不是我們要驗的頁碼位移
        + f"<!--{'.' * 600}-->"
        + "</body></html>"
    )

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=body)

    scoped = replace(
        cfg,
        storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
        fetch={**cfg.fetch, "delay_seconds": 0.0, "backoff_seconds": 0.0},
    )
    fetcher = CachedFetcher(scoped)
    fetcher._client.close()
    fetcher._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        src = YahooClosedSource(scoped, fetcher)
        result = src.search_detailed("kw", pages=1, first_page=4)
    finally:
        fetcher.close()

    assert f"b={3 * _PAGE_SIZE + 1}" in seen[0]
    # 第 4 頁抓不到東西時仍然是「解析健康判定」說了算，不是靜默 0 筆
    assert result.health is ParseHealth.PARSER_BROKEN


# ---------------------------------------------------------------------------
# 6. PayPay 賣家頁歷史
# ---------------------------------------------------------------------------
class FakeSellerSource:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    def search_seller(self, seller_id, *, sold=False, pages=None):
        self.calls.append((seller_id, sold))
        res = SearchResult(source="paypay_direct", site="buyee_paypay", query=f"seller:{seller_id}")
        res.pages_fetched = 1
        res.listings = [
            FakeListing(seller_id=seller_id, raw={"sold_at": "2026-02-06T00:00:00+00:00"}),
            FakeListing(seller_id=seller_id, raw={"sold_at": "2026-08-02T00:00:00+00:00"}),
        ]
        return res


def test_mine_paypay_seller_asks_for_sold_and_reports_the_span():
    src, comps = FakeSellerSource(), FakeComps()
    out = mine_paypay_seller(comps=comps, source=src, seller_id="p1")

    assert src.calls == [("p1", True)], "挖歷史必須明講 sold=True，不然拿回來的是在架"
    assert out.ok and out.found == 2 and out.requests == 1
    assert out.span_days > 170, "一個請求就該換回半年的成交紀錄"


def test_mine_paypay_seller_survives_a_source_failure():
    class Boom:
        def search_seller(self, *_a, **_k):
            raise httpx.ConnectError("斷線")

    out = mine_paypay_seller(comps=FakeComps(), source=Boom(), seller_id="p1")
    assert out.ok is False and "ConnectError" in out.note
