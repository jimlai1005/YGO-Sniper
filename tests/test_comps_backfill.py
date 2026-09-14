"""行情回補：卡名清單、節流、BLOCKED 即停、入庫計數。"""

from __future__ import annotations

import pytest

from ygo_sniper.comps_backfill import BackfillReport, known_card_names, run_backfill
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


class _Index:
    available = True

    class _M:
        def __init__(self, name, in_era=True):
            self.name_ja = name
            self.in_era = in_era

    def match(self, title):
        if title.startswith("card:"):
            return self._M(title[5:])
        if title.startswith("modern:"):
            return self._M(title[7:], in_era=False)
        return None


class _StoreTitles:
    def __init__(self, comps, signals):
        self._c = comps
        self._s = signals

    def comps_by(self, **kw):
        return [{"title": t} for t in self._c]

    def all_signal_titles(self):
        return [{"title": t} for t in self._s]


class TestKnownCardNames:
    def test_union_dedup_sorted_in_era_only(self):
        st = _StoreTitles(["card:b", "card:a", "junk", "modern:z"], ["card:a", "card:c"])
        assert known_card_names(st, _Index()) == ["a", "b", "c"]


class _Src:
    supports_sold = True

    def __init__(self, health=ParseHealth.OK, n=3):
        self.health = health
        self.n = n
        self.calls = []

    def search_detailed(self, keyword, *, sold, pages):
        self.calls.append((keyword, pages))
        return SearchResult(source="x", site="buyee_yahoo", query=keyword,
                            listings=[object()] * self.n, parsed_count=self.n,
                            pages_fetched=pages, health=self.health)


class _Comps:
    def __init__(self, kept=2):
        self.kept = kept
        self.batches = []

    def ingest_sold(self, listings):
        self.batches.append(len(listings))

        class R:
            kept = self.kept
            rejected = 0
        return R()


class TestRunBackfill:
    def test_queries_each_source_with_its_pages_and_records_ledger(self, store):
        y, p = _Src(n=3), _Src(n=1)
        comps = _Comps(kept=2)
        rep = run_backfill(store=store, sources={"yahoo_closed": y, "paypay_direct": p}, comps=comps,
                           names=["a", "b"], cooldown_days=7, limit=None,
                           pages={"yahoo_closed": 3, "paypay_direct": 1}, log=lambda s: None)
        assert y.calls == [("a", 3), ("b", 3)] and p.calls == [("a", 1), ("b", 1)]
        assert comps.batches == [3, 1, 3, 1]
        assert (rep.cards_total, rep.cards_done, rep.found, rep.kept) == (2, 2, 8, 8)
        assert store.refill_cooldown_active(7) == {"a", "b"}

    def test_cooldown_and_limit(self, store):
        store.record_refill("a", found=1, kept=1)
        y = _Src()
        rep = run_backfill(store=store, sources={"yahoo_closed": y}, comps=_Comps(),
                           names=["a", "b", "c"], cooldown_days=7, limit=1,
                           pages={"yahoo_closed": 1}, log=lambda s: None)
        assert rep.cards_skipped_cooldown == 1
        assert [c[0] for c in y.calls] == ["b"]
        assert rep.cards_done == 1

    def test_blocked_stops_whole_run_and_does_not_record(self, store):
        y = _Src(health=ParseHealth.BLOCKED)
        rep = run_backfill(store=store, sources={"yahoo_closed": y}, comps=_Comps(),
                           names=["a", "b"], cooldown_days=7, limit=None,
                           pages={"yahoo_closed": 1}, log=lambda s: None)
        assert rep.blocked is True and rep.cards_done == 0
        assert len(y.calls) == 1
        assert store.refill_cooldown_active(7) == set()

    def test_fetch_failed_source_is_skipped_but_card_still_recorded_if_other_source_ok(self, store):
        y = _Src(health=ParseHealth.FETCH_FAILED)
        p = _Src(n=2)
        rep = run_backfill(store=store, sources={"yahoo_closed": y, "paypay_direct": p}, comps=_Comps(kept=1),
                           names=["a"], cooldown_days=7, limit=None,
                           pages={"yahoo_closed": 1, "paypay_direct": 1}, log=lambda s: None)
        assert rep.cards_done == 1 and rep.found == 2 and rep.kept == 1
        assert len(rep.problems) == 1 and "FETCH_FAILED" in rep.problems[0]
        assert store.refill_cooldown_active(7) == {"a"}

    def test_all_sources_failed_does_not_record(self, store):
        y = _Src(health=ParseHealth.FETCH_FAILED)
        rep = run_backfill(store=store, sources={"yahoo_closed": y}, comps=_Comps(),
                           names=["a"], cooldown_days=7, limit=None,
                           pages={"yahoo_closed": 1}, log=lambda s: None)
        assert rep.cards_done == 0 and store.refill_cooldown_active(7) == set()

    def test_dry_run_touches_nothing(self, store):
        y = _Src()
        rep = run_backfill(store=store, sources={"yahoo_closed": y}, comps=_Comps(),
                           names=["a"], cooldown_days=7, limit=None,
                           pages={"yahoo_closed": 1}, dry_run=True, log=lambda s: None)
        assert y.calls == [] and rep.cards_total == 1 and store.refill_cooldown_active(7) == set()
