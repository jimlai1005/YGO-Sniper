"""指定卡狙擊（card watch）：等一根特定的針。

使用者指定「鑑定機構＋分數＋卡名＋卡號」的單卡；比對走在商業過濾**之前**
（pipeline._collect_candidates 開頭）——排除字／年代閘門／min_grade 是為
「大海撈針」設計的，狙擊是「等一根已知的針」，被它們誤殺一次可能就是等半年
（CLAUDE.md 第一節：誤殺是靜默的，雜訊是看得見的）。

三個 tier 的通知政策（寧可多報不漏報，但音量可控）：
- exact   🎯 名/號命中＋機構分數全符 → Telegram，不受每輪總量上限裁切
- partial 👀 名/號命中＋機構相符但分數不明/不同 → Telegram，自己的小上限
- near       名/號命中但機構不符/未鑑定/現代重印 → 只入帳＋dashboard，不推播
near 不推播不是丟棄：dashboard 狙擊分頁每一筆都看得到。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .cards import extract_title_codes, fold
from .parsers.grade import parse_grade
from .queries import QuerySpec

TIER_EXACT = "exact"
TIER_PARTIAL = "partial"
TIER_NEAR = "near"

#: 現代版標記：命中就從 🎯 降到 👀（**照樣推播**，訊息上註明「疑似現代版」）。
#: 全部來自 2026-08-09 落札檔案的實際觀測，不是憑空想的。
#:
#: ⚠️ 為什麼是降到 partial 而不是 near（不推播）——這是本模組最重要的一條：
#: 目標卡的兩筆真成交，標題裡**根本沒有卡號**（`【ARS10】魔法の筒 Magic Cylinder
#: ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品`）。真標的與現代版假陽性的唯一
#: 差別就是這幾個標記詞。一份手寫的詞表只要多寫一個詞，就會靜默地讓真標的不通知，
#: 而誤殺是靜默的、雜訊是看得見的（CLAUDE.md 第一節）。所以標記詞的最大權力就是
#: 「降到 👀」——**永遠不能讓一筆命中變成不通知**。
#: fold 後比對：中点與全形自動吸收（`ラッシュ・デュエル`、`ＷＣＳ` 同樣命中）。
_MODERN_MARKERS = (
    "プリズマティック", "プリシク", "ラッシュデュエル", "RUSH DUEL",
    "25th", "WCS", "クォーターセンチュリー", "QUARTER CENTURY",
    "レアリティコレクション",
)
_MODERN_FOLDED = tuple(fold(t) for t in _MODERN_MARKERS)

#: partial 每輪推播上限（同 seller_unpriced 的思路：真品類稀少，
#: 一輪超過這個數多半是比對出了狀況，別讓它洗版）。
PARTIAL_MAX_PER_RUN = 5

#: near 命中帳的保留天數（exact/partial 永久保留）。
NEAR_HIT_RETAIN_DAYS = 90


@dataclass(slots=True)
class WatchMatcher:
    """一個 card_watch 列的預折疊比對器：fold 一次、每個標題重用。"""

    row: dict[str, Any]
    names_folded: tuple[str, ...]
    code_norm: str
    grader: str
    grade: float

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> WatchMatcher:
        names = [row.get("name_ja") or "", row.get("name_en") or ""]
        try:
            names += [str(a) for a in json.loads(row.get("aliases") or "[]")]
        except (TypeError, ValueError):
            pass
        folded = tuple(sorted({fold(n) for n in names if n and fold(n)}))
        return cls(
            row=row,
            names_folded=folded,
            code_norm=str(row.get("code_norm") or ""),
            grader=str(row.get("grader") or ""),
            grade=float(row.get("grade") or 0.0),
        )


def match_tier(m: WatchMatcher, title: str) -> str | None:
    """標題 → tier（None ＝ 與這張卡無關）。順序即政策，見模組 docstring。"""
    return classify(m, title)[0]


def classify(m: WatchMatcher, title: str) -> tuple[str | None, str]:
    """→ (tier, 一句話理由)。理由會走到訊息與 dashboard 上——降級了要說得出為什麼。

    順序即政策：
    1. 名／號都沒中 → None（與這張卡無關）
    2. 卡號命中 → 卡號是決定性的，現代版標記不能推翻它
    3. 機構不符／完全沒鑑定 → near（只入帳，不推播）
    4. 分數不明或不同 → partial
    5. 現代版標記 → partial（**照樣推播**，註明疑似現代版）
    6. 標題只明示別張卡號 → partial
    7. 其餘 → exact
    """
    folded = fold(title)
    name_hit = any(n in folded for n in m.names_folded)
    codes = extract_title_codes(title)
    code_hit = bool(m.code_norm) and m.code_norm in codes
    if not (name_hit or code_hit):
        return None, ""

    grader, grade = parse_grade(title)
    if grader.value != m.grader:
        got = grader.value if grader.value != "UNKNOWN" else "未鑑定"
        return TIER_NEAR, f"鑑定機構是 {got}，不是 {m.grader}"
    if grade is None:
        return TIER_PARTIAL, f"標題只寫 {m.grader}、沒寫分數"
    if abs(grade - m.grade) > 1e-9:
        return TIER_PARTIAL, f"分數是 {grade:g}，目標是 {m.grade:g}"

    # 機構與分數都符合。卡號命中就是決定性證據——現代版標記不能推翻它
    # （現代版不會印 P4-06）。
    if code_hit:
        return TIER_EXACT, f"卡號 {m.code_norm} ＋ {m.grader}{grade:g} 全符"

    modern = [
        raw for raw, f in zip(_MODERN_MARKERS, _MODERN_FOLDED, strict=True)
        if f in folded
    ]
    if modern:
        return TIER_PARTIAL, f"疑似現代版（標題含 {'／'.join(modern)}）"
    if codes and m.code_norm:
        # 機構分數全符，但標題明示的卡號全是別張——多半是同捆或別版本，降半級仍通知
        return TIER_PARTIAL, f"標題的卡號是 {'／'.join(codes)}，不是 {m.code_norm}"
    return TIER_EXACT, f"卡名 ＋ {m.grader}{grade:g} 全符"


def load_matchers(store: Any) -> list[WatchMatcher]:
    return [WatchMatcher.from_row(r) for r in store.list_card_watch(active_only=True)]


def observe_listings(
    store: Any, matchers: list[WatchMatcher], listings: list, *,
    source_name: str = "",
) -> int:
    """對一批**未過濾**的原始 listing 跑狙擊比對，命中寫進 hit 帳（冪等）。
    回傳寫入（含更新）筆數。"""
    n = 0
    for lst in listings:
        title = getattr(lst, "title", "") or ""
        for m in matchers:
            tier = match_tier(m, title)
            if tier is None:
                continue
            end = getattr(lst, "end_time", None)
            currency = getattr(lst, "currency", "")
            store.upsert_card_watch_hit(
                int(m.row["id"]), lst.key,
                tier=tier, title=title, url=lst.url,
                site=lst.site.value,
                seller_id=getattr(lst, "seller_id", None) or "",
                price_native=float(lst.price) if lst.price is not None else None,
                currency=str(getattr(currency, "value", currency) or ""),
                end_time=end.isoformat() if end is not None else "",
            )
            n += 1
    return n


def scan_queries(
    matchers: list[WatchMatcher], base_queries: list[QuerySpec]
) -> list[QuerySpec]:
    """每張狙擊卡加自己的關鍵字查詢（日文名＋英文名），來源沿用既有查詢的聯集。

    不猜來源名——base 用哪些來源，狙擊查詢就用哪些；base 是空的
    （watch_only 模式）狙擊查詢也不跑。不帶分類（category=None）：分類是
    收斂雜訊用的，狙擊要的是最大召回，雜訊由 tier 政策吸收。
    """
    srcs = tuple(dict.fromkeys(s for q in base_queries for s in q.sources))
    if not srcs:
        return []
    out: list[QuerySpec] = []
    seen: set[str] = set()
    for m in matchers:
        for kw in (m.row.get("name_ja") or "", m.row.get("name_en") or ""):
            kw = kw.strip()
            if not kw or kw.lower() in seen:
                continue
            seen.add(kw.lower())
            out.append(QuerySpec(
                name=f"snipe:{m.row['id']}", keyword=kw, sources=srcs, category=None,
            ))
    return out
