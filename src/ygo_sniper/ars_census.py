"""ARS 鑑定量（census）抓取與解析。

頁面是 server-rendered HTML、無 JS 需求（2026-08-09 實測）。數字形如
`<div class="grade-entry" data-grade="10"><span>Grade 10</span><span>5（0）</span></div>`
——括號內是含ミスプリント的數字，我們存主數字。改版時 parse_census 大聲拋
CensusParseError，絕不回空 dict 假裝成功（0 張與抓不到是兩回事）。
"""
from __future__ import annotations

import re
from urllib.parse import quote

_GRADE_ENTRY_RE = re.compile(
    r'data-grade="(?P<grade>[^"]+)"\s*>\s*<span>[^<]*</span>\s*'
    r"<span>\s*(?P<count>\d+)（"
)
# ⚠️ 真實頁面是 `鑑定総数 TOTAL GRADING&nbsp;11`——數字前是**字面的 `&nbsp;` 實體**，
# `\s` 抓不到（實測用 `\s*` 會回 None）。三種寫法都吃下來。
_TOTAL_RE = re.compile(r"鑑定総数\s*TOTAL\s*GRADING(?:&nbsp;|&#160;|\s)*(\d+)")

SEARCH_URL = "https://ars-grading.com/grading/searchName?name={name}&page=1"

_SEARCH_ENTRY_RE = re.compile(
    r'href="(?P<path>/grading/searchNameDetail\?id=[^"]+)"(?P<body>.*?)</a>',
    re.S,
)
_SPAN_RE = re.compile(r"<span>\s*([^<]*?)\s*</span>", re.S)


class CensusParseError(RuntimeError):
    """頁面抓到了但不是預期形狀（版型改了）。"""


def parse_census(html: str) -> dict[str, int]:
    """→ `{"AU": 0, "1": 0, …, "9": 5, "10": 5, "10+": 1}`（key 是頁面原樣）。"""
    counts = {
        m.group("grade"): int(m.group("count"))
        for m in _GRADE_ENTRY_RE.finditer(html)
    }
    if not counts:
        raise CensusParseError("頁面上找不到任何 grade-entry——ARS 版型可能改了")
    return counts


def census_total(html: str) -> int | None:
    m = _TOTAL_RE.search(html)
    return int(m.group(1)) if m else None


def page_mentions(html: str, needle: str) -> bool:
    """去標籤後的純文字裡有沒有這個字串（登錄時的同卡 sanity check）。"""
    return needle in re.sub(r"<[^>]+>", " ", html)


def parse_search_results(html: str) -> list[dict[str, str]]:
    """卡名搜尋頁 → 候選清單。span 順序（實測）：卡名/收錄/型番/稀有度/年份。"""
    out: list[dict[str, str]] = []
    for m in _SEARCH_ENTRY_RE.finditer(html):
        spans = [s.strip() for s in _SPAN_RE.findall(m.group("body"))]
        out.append({
            "url": "https://ars-grading.com" + m.group("path"),
            "name": spans[0] if len(spans) > 0 else "",
            "expansion": spans[1] if len(spans) > 1 else "",
            "code": spans[2] if len(spans) > 2 else "",
            "rarity": spans[3] if len(spans) > 3 else "",
            "year": spans[4] if len(spans) > 4 else "",
        })
    return out


def find_census_url(
    name_ja: str, code_norm: str, *, fetcher
) -> tuple[str | None, list[dict[str, str]]]:
    """卡名搜尋 → 用正規化卡號唯一定位 census 頁。

    收斂不了就回 (None, 候選清單) 讓呼叫端把候選攤給使用者——寧可要使用者
    確認，不猜（識別碼是命名空間不是字串，猜錯會安靜地追蹤錯的卡）。
    """
    from .cards import extract_title_codes

    html = fetcher.get(SEARCH_URL.format(name=quote(name_ja)), use_cache=False)
    entries = parse_search_results(html)
    if code_norm:
        matched = [
            e for e in entries
            if code_norm in extract_title_codes(f" {e['code']} ")
        ]
        if len(matched) == 1:
            return matched[0]["url"], entries
    return None, entries


def fetch_census(url: str, *, fetcher) -> tuple[dict[str, int], int | None, str]:
    """→ (各級張數, 鑑定總數, 原始 html)。html 給呼叫端做同卡 sanity check。"""
    html = fetcher.get(url, use_cache=False)
    return parse_census(html), census_total(html), html
