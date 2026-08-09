"""ARS census 頁解析。fixture 是 2026-08-09 生產路徑（無 JS）抓的真頁面。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest

from ygo_sniper.ars_census import (
    SEARCH_URL,
    CensusParseError,
    census_total,
    find_census_url,
    page_mentions,
    parse_census,
    parse_search_results,
)

FIXTURES = Path(__file__).parent / "fixtures"
CENSUS_HTML = (FIXTURES / "ars_census_p4_06.html").read_text(encoding="utf-8")
SEARCH_HTML = (FIXTURES / "ars_search_magic_cylinder.html").read_text(encoding="utf-8")


class FakeFetcher:
    """CachedFetcher.get 同形（測試注入；生產傳真的）。"""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def get(self, url: str, **kw) -> str:
        return self.pages[url]


def test_parse_census_counts_from_the_real_page():
    counts = parse_census(CENSUS_HTML)
    assert counts["9"] == 5
    assert counts["10"] == 5
    assert counts["10+"] == 1
    assert census_total(CENSUS_HTML) == 11


def test_parse_census_raises_loudly_on_shape_change():
    with pytest.raises(CensusParseError):
        parse_census("<html><body>改版了</body></html>")


def test_page_mentions_strips_tags():
    assert page_mentions(CENSUS_HTML, "P4-06")
    assert page_mentions(CENSUS_HTML, "魔法の筒")
    assert not page_mentions(CENSUS_HTML, "青眼の白龍")


def test_parse_search_results_shape():
    entries = parse_search_results(SEARCH_HTML)
    assert len(entries) == 6
    codes = [e["code"] for e in entries]
    assert "P4-06" in codes
    hit = next(e for e in entries if e["code"] == "P4-06")
    assert hit["url"] == (
        "https://ars-grading.com/grading/searchNameDetail?id=001202208090020007"
    )
    assert hit["name"] == "魔法の筒"


def test_find_census_url_narrows_by_normalized_code():
    fetcher = FakeFetcher({SEARCH_URL.format(name=quote("魔法の筒")): SEARCH_HTML})
    url, entries = find_census_url("魔法の筒", "P4-6", fetcher=fetcher)
    assert url is not None and url.endswith("id=001202208090020007")
    assert len(entries) == 6


def test_find_census_url_returns_candidates_when_ambiguous():
    fetcher = FakeFetcher({SEARCH_URL.format(name=quote("魔法の筒")): SEARCH_HTML})
    url, entries = find_census_url("魔法の筒", "", fetcher=fetcher)   # 沒卡號 → 收不斂
    assert url is None and len(entries) == 6
