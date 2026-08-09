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
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------------------
# 市場成交檔案挖掘。**市場的檔案才是資料庫，這張表是它的記憶體。**
# ---------------------------------------------------------------------------
#: 每個關鍵字翻幾頁。實測 `魔法の筒` 第 1 頁 100 筆已涵蓋 150 天、第 2 頁翻完
#: 整個檔案（126 筆／179 天）。冷門卡 2 頁綽綽有餘；熱門卡名多翻也只是多 1 秒。
MINE_PAGES = 2


@dataclass(slots=True)
class MineResult:
    """一次挖掘的完整結果。**命中數與健康是兩件事**：0 筆可能是真的沒賣過，
    也可能是被擋——分不出來就是靜默失敗（CLAUDE.md 第五節）。"""

    ok: bool = True
    queries: list[str] = field(default_factory=list)
    new_sales: int = 0
    total_sales: int = 0
    #: 挖到、但來源給不出真實成交時刻的筆數（Mercari／露天的搜尋頁沒有落札時間）。
    #: **這些筆數不得進入任何「什麼時候／多久出現一次」的宣稱**——它們只答得出
    #: 價格。與 comps 的 `sold_at_is_ingest` 是同一個立場（CLAUDE.md 第三節）。
    undated_sales: int = 0
    tier_counts: dict[str, int] = field(default_factory=dict)
    oldest: str = ""
    newest: str = ""
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        span = f"{self.oldest[:10]} → {self.newest[:10]}" if self.oldest else "無成交"
        parts = [
            f"挖到 {self.total_sales} 筆成交（新增 {self.new_sales}）",
            f"涵蓋 {span}",
            "／".join(f"{k} {v}" for k, v in sorted(self.tier_counts.items())) or "—",
        ]
        if self.undated_sales:
            # 「涵蓋 X → Y」只描述有日期的那一批。缺口不講出來，讀的人會以為
            # 那個區間蓋住了全部筆數——那就是拿兩種基準合成一個數字。
            parts.append(
                f"⚠️ 其中 {self.undated_sales} 筆來源沒給成交時刻"
                f"（只知道賣過、不知何時，不算進上面的涵蓋區間）"
            )
        if not self.ok:
            parts.append("⚠️ " + "；".join(self.problems))
        return "｜".join(parts)


def _sale_kind_of(lst: Any) -> str:
    """競標結標價（買家喊上去）vs 定價成交（賣家開的）——兩種價格形成機制。
    **只看 is_fixed_price 旗標**：Yahoo!フリマ 的 bidCount 也是 1（佔位值），
    拿它判型態會把定價成交讀成競標（CLAUDE.md 第三節第七項）。"""
    raw = getattr(lst, "raw", None) or {}
    if "is_fixed_price" not in raw:
        return "unknown"
    return "fixed" if raw.get("is_fixed_price") else "auction"


def mine_sold_archive(
    store: Any, sources: dict[str, Any], m: WatchMatcher, *, pages: int = MINE_PAGES,
) -> MineResult:
    """去各平台的成交檔案挖這張卡的過去，逐筆 tier 分類後永久存進 card_watch_sale。

    **查詢只打卡名，絕不加鑑定詞**：伺服器端多一個詞就是 AND 過濾，只寫
    「ARS鑑定10」的賣家會整批消失（實測 `魔法の筒 PSA` 只回 5 筆 vs 卡名 126 筆）。
    收斂是我們自己在本地用 tier 做的——那才看得見、才改得動。
    """
    from .refill import _sold_search  # noqa: PLC0415 - 延後匯入避免循環相依

    res = MineResult()
    keywords = [k.strip() for k in
                (m.row.get("name_ja") or "", m.row.get("name_en") or "") if k.strip()]
    watch_id = int(m.row["id"])
    #: 同一筆成交會被多個關鍵字撈到（日文名與英文名常同時出現在標題裡）。
    #: **tier_counts 要數的是「這張卡的成交筆數」，不是「查詢×命中」的事件數**——
    #: 少了這個去重，兩個關鍵字就讓每個數字都變兩倍（同源同基準）。
    seen: set[str] = set()
    for source_name, src in sources.items():
        if not getattr(src, "supports_sold", False):
            continue
        for kw in keywords:
            res.queries.append(kw)
            out = _sold_search(src, source_name, kw, pages=pages)
            health = getattr(out, "health", None)
            health_name = getattr(health, "name", str(health))
            if health_name not in ("OK", "EMPTY_CONFIRMED"):
                res.ok = False
                res.problems.append(
                    f"{source_name}／{kw}：{health_name}"
                    f"（{getattr(out, 'detail', '') or '沒有細節'}）——"
                    "這一條的 0 筆不代表沒賣過"
                )
                continue
            for lst in out.listings:
                tier, _why = classify(m, getattr(lst, "title", "") or "")
                if tier is None:
                    continue
                if lst.key in seen:
                    continue          # 另一個關鍵字已經收過這一筆
                seen.add(lst.key)
                raw = getattr(lst, "raw", None) or {}
                currency = getattr(lst, "currency", "")
                sold_at = str(raw.get("sold_at") or "")
                #: 沒有落札時刻的來源（Mercari／露天搜尋頁）**照樣入帳**——
                #: 「賣過但不知何時」仍是有用的價格資訊，丟掉就是靜默誤殺。
                #: 但要數出來，讓日期類的宣稱知道自己少蓋了幾筆。
                #: **絕不塞假日期頂替**（comps 把入庫時間當 sold_at 存，讓 90 天
                #: 視窗對那批資料形同虛設——CLAUDE.md 第五節）。
                res.undated_sales += int(not sold_at)
                is_new = store.upsert_card_watch_sale(
                    watch_id, lst.key,
                    tier=tier, title=lst.title, url=lst.url,
                    origin_url=getattr(lst, "origin_url", None) or "",
                    site=lst.site.value,
                    seller_id=getattr(lst, "seller_id", None) or "",
                    price_native=float(lst.price) if lst.price is not None else None,
                    currency=str(getattr(currency, "value", currency) or ""),
                    sold_at=sold_at,
                    bid_count=raw.get("bid_count"),
                    sale_kind=_sale_kind_of(lst),
                    source=source_name,
                )
                res.new_sales += int(is_new)
                res.tier_counts[tier] = res.tier_counts.get(tier, 0) + 1
    sales = store.list_card_watch_sales(watch_id)
    res.total_sales = len(sales)
    stamps = sorted(s["sold_at"] for s in sales if s["sold_at"])
    if stamps:
        res.oldest, res.newest = stamps[0], stamps[-1]
    return res
