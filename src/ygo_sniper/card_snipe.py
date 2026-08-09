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
import re
from dataclasses import dataclass
from typing import Any

from .cards import extract_title_codes, fold
from .parsers.grade import normalize, parse_grade
from .queries import QuerySpec

TIER_EXACT = "exact"
TIER_PARTIAL = "partial"
TIER_NEAR = "near"

#: 標題裡「目標機構自己的 token」是否出現（例：目標 ARS → `ARS10`／`ARS 鑑定 9`）。
#: **用 lookaround 不用 `\b`**：漢字與假名在 Python re 裡算 `\w`，CJK 沒有 word
#: boundary（CLAUDE.md 第二節，這個 repo 為此出過四次事故）。
_GRADER_TOKEN_TMPL = r"(?<![A-Za-z0-9]){g}\s*(?:鑑定)?\s*(?:10\+*|[0-9](?:\.5)?)(?!\d)"

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
#: fold 後為空的標記詞是災難：`"" in folded` 恆為真，會讓**每一筆** exact 靜默
#: 降級成 partial。先濾掉空字串，再 assert 一個都沒被濾掉——真的有詞 fold 成空
#: （例如日後有人只寫了一個中点）就在載入時當場炸掉，而不是安靜地少一個標記詞。
_MODERN_FOLDED = tuple(f for f in (fold(t) for t in _MODERN_MARKERS) if f)
assert len(_MODERN_FOLDED) == len(_MODERN_MARKERS), "現代版標記詞 fold 後不得為空字串"

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
        # `aliases` 兩種形態都要收：store 回的是 JSON 字串，web/CLI 層可能直接
        # 傳已解碼的 list。只認字串的話 json.loads 會拋 TypeError，別名整組消失，
        # 而「只寫片假名的標的」就靜默漏掉——所以壞掉要**印出來**，不是 pass。
        raw = row.get("aliases") or []
        try:
            if isinstance(raw, str):
                raw = json.loads(raw)
            names += [str(a) for a in raw]
        except (TypeError, ValueError) as exc:
            print(f"[warn] card_watch#{row.get('id')} 的 aliases 解析失敗（{exc}），"
                  f"這張卡的別名比對整組失效：{raw!r}")
        folded = tuple(sorted({fold(n) for n in names if n and fold(n)}))
        return cls(
            row=row,
            names_folded=folded,
            code_norm=str(row.get("code_norm") or ""),
            # 縱深防禦：小寫的 grader 會讓每一筆都判成 near，訊息還印
            # 「鑑定機構是 ARS，不是 ars」。CLI 層會 upper()，這裡零成本再一道。
            grader=str(row.get("grader") or "").strip().upper(),
            grade=float(row.get("grade") or 0.0),
        )


def match_tier(m: WatchMatcher, title: str) -> str | None:
    """標題 → tier（None ＝ 與這張卡無關）。順序即政策，見模組 docstring。"""
    return classify(m, title)[0]


def classify(m: WatchMatcher, title: str) -> tuple[str | None, str]:
    """→ (tier, 一句話理由)。理由會走到訊息與 dashboard 上——降級了要說得出為什麼。

    順序即政策（這份清單必須與下面的實作逐步對應——註解描述的是意圖、code 才是
    行為，兩者不符時改註解，不要改行為）：
    1. 名／號都沒中 → None（與這張卡無關）
    2. `parse_grade` 的機構與目標不符：
       2a. 但標題自己寫了「目標機構＋分數」→ partial（賣家的宣稱寫法，見下）
       2b. 否則 → near（只入帳，不推播）
    3. 分數不明 → partial
    4. 分數與目標不同 → partial
    5. 機構分數全符 ＋ 卡號命中 → exact（卡號是決定性的，標記詞推翻不了它）
    6. 機構分數全符 ＋ 現代版標記 → partial（**照樣推播**，註明疑似現代版）
    7. 機構分數全符 ＋ 標題只明示別張卡號 → partial
    8. 其餘（機構分數全符、卡名命中）→ exact
    """
    folded = fold(title)
    name_hit = any(n in folded for n in m.names_folded)
    codes = extract_title_codes(title)
    code_hit = bool(m.code_norm) and m.code_norm in codes
    if not (name_hit or code_hit):
        return None, ""

    grader, grade = parse_grade(title)
    if grader.value != m.grader:
        # 賣家常寫「ARS 鑑定品，相當於 PSA10 以上」，而 parse_grade 只回一個勝者，
        # PSA 的 pattern 又排在 ARS 前面（`以上` 不在它的 _CLAIM_SUFFIX 裡）。
        # 實測 data/sniper.db 的 3,239 個真實標題：998 筆自己寫了 ARS＋分數，
        # 其中 79 筆（7.9%）被讀成 PSA——直接判 near 等於靜默漏掉 7.9% 的目標卡。
        # 標題自己寫了目標機構就至少推播（👀），讓使用者一眼決定。
        # **不升到 exact**：分數的權威只有 parse_grade 一份，這裡不另立第二把尺。
        if re.search(_GRADER_TOKEN_TMPL.format(g=re.escape(m.grader)),
                     normalize(title)):
            return TIER_PARTIAL, (
                f"標題同時出現 {m.grader} 與 {grader.value} 的標記"
                f"（賣家常寫『相當於 {grader.value}』）——需人工確認"
            )
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
