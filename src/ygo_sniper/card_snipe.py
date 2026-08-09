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
    source_name: str = "", write: bool = True,
) -> int:
    """對一批**未過濾**的原始 listing 跑狙擊比對，命中寫進 hit 帳（冪等）。
    回傳命中筆數（`write=True` 時等於寫入／更新筆數）。

    `write=False`（`scan --dry-run`）**照樣比對、照樣回報命中數，只是不落庫**。
    比對本身是純字串運算——零網路、零副作用——沒有理由在 dry-run 時跳過，
    而跳過的代價很具體：dry-run 會印出「比對 0 筆」，與「掛鉤壞了」外顯一模一樣
    （CLAUDE.md 第五節）。dry-run 正是使用者用來確認東西有在動的那個指令，
    它必須證明得了比對有在跑。要跳過的只有 `upsert_card_watch_hit`。
    """
    n = 0
    for lst in listings:
        title = getattr(lst, "title", "") or ""
        for m in matchers:
            tier = match_tier(m, title)
            if tier is None:
                continue
            n += 1
            if not write:
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
                # 通知附圖（notify.photo_url_of）。抓不到就傳空字串——store 那層
                # 「只補不抹」，不會把先前抓到的圖覆蓋掉。
                image_url=getattr(lst, "image_url", None) or "",
            )
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


# ---------------------------------------------------------------------------
# 登錄與檔案（dossier）。CLI 與 web 都呼叫這裡——判準只有一份。
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AddResult:
    watch_id: int
    messages: list[str]


def _master_card(*, code_norm: str, name_ja: str) -> dict[str, Any] | None:
    """讀卡名主檔原始 JSON 找這張卡。

    不能走 CardIndex：它把 aliases 折進內部索引後就丟掉原始卡片 dict，
    而登錄需要的正是 aliases 清單。**路徑一定要 `project_root() / …`**（照抄
    cards.py:394）：`DEFAULT_MASTER_PATH` 是相對路徑字串，直接 `Path(...)` 在
    非 repo 目錄執行 console script 時會讀不到 → aliases 空 → 「マジック・
    シリンダー」那類標題永遠 tier=None、永不推播。誤殺是靜默的。
    缺檔回 None 不爆。
    """
    from .cards import DEFAULT_MASTER_PATH
    from .config import project_root

    try:
        data = json.loads(
            (project_root() / DEFAULT_MASTER_PATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    cards = data.get("cards") or []
    if code_norm:
        for c in cards:
            if code_norm in (c.get("set_codes") or []):
                return c
    if name_ja:
        for c in cards:
            if c.get("name_ja") == name_ja:
                return c
    return None


def _ingest_census(
    store: Any, fetcher: Any, watch_id: int, *, url: str,
    grader: str, name_ja: str, code_raw: str, code_norm: str,
) -> list[str]:
    """census 抓取＋落庫（add 與 refresh 共用）。url 為空時 ARS 用卡名自動搜。

    任何失敗都不擋登錄：大聲講、URL 照存，之後 `snipe report --refresh-census` 重試。
    """
    from .ars_census import (
        CensusParseError,
        fetch_census,
        find_census_url,
        page_mentions,
    )
    from .sources.base import FetchError

    msgs: list[str] = []
    if not url and grader == "ARS":
        try:
            found, entries = find_census_url(name_ja, code_norm, fetcher=fetcher)
        except FetchError as exc:
            msgs.append(f"⚠️ ARS 卡名搜尋失敗（{exc}）——之後用 snipe report "
                        f"{watch_id} --refresh-census 重試")
            return msgs
        if found:
            url = found
            msgs.append(f"census 頁自動定位：{url}")
        else:
            if entries:
                msgs.append("⚠️ census 無法唯一定位（卡號對不上），候選如下——"
                            "確認後用 --census-url 指定：")
                for e in entries[:6]:
                    msgs.append(f"   {e['name']} / {e['expansion']} / {e['code']}"
                                f" → {e['url']}")
            else:
                msgs.append("⚠️ ARS 卡名搜尋 0 件——確認卡名寫法")
            return msgs
    if not url:
        if grader != "ARS":
            msgs.append(f"（{grader} 的鑑定量查詢未支援，census 留空）")
        return msgs
    try:
        counts, total, html = fetch_census(url, fetcher=fetcher)
        # ⚠️ 各級張數解析成功、卻抓不到總數 → 多半是總數那行的版型改了。
        # `census_total` 失敗是回 None（不像 parse_census 會拋），與「頁面真的
        # 沒有總數」無法區分——所以這裡要出聲，不要讓它安靜地變成 dashboard 上的
        # 「鑑定總數 None」（CLAUDE.md 第五節）。
        if counts and total is None:
            msgs.append("⚠️ 各級張數讀到了但鑑定總數沒讀到——ARS 總數那行的版型"
                        "可能改了，請人工確認 census 頁")
    except (FetchError, CensusParseError) as exc:
        store.update_card_watch_census(watch_id, census_url=url, census_json="",
                                       census_total=None)
        msgs.append(f"⚠️ census 抓取／解析失敗（{exc}）——URL 已存，"
                    f"之後 --refresh-census 重試")
        return msgs
    store.update_card_watch_census(
        watch_id, census_url=url,
        census_json=json.dumps(counts, ensure_ascii=False), census_total=total,
    )
    shown = "、".join(f"{k}: {v} 張" for k, v in counts.items() if v)
    msgs.append(f"census：{shown}（鑑定總數 {total}）")
    for needle in (code_raw, name_ja):
        if needle and not page_mentions(html, needle):
            msgs.append(f"⚠️ census 頁上找不到 {needle!r}——確認這頁是不是同一張卡")
    return msgs


def add_card_watch(
    store: Any, fetcher: Any, *, grader: str, grade_input: str, name_ja: str,
    name_en: str = "", code: str = "", census_url: str = "",
    evidence_urls: list[str] | None = None, note: str = "",
    sources: dict[str, Any] | None = None,
) -> AddResult:
    """登錄＋立刻補齊檔案（含**當下就去挖市場成交檔案**；`sources=None` 才跳過）。

    外部抓取（census／證據頁）失敗**不擋登錄**——大聲講、標狀態、照樣入庫
    （讀不到 ≠ 不存在）。輸入格式錯誤（機構不認得、分數看不懂、卡號正規化
    失敗）是 semantic 失敗：拋 ValueError，不入庫。
    """
    from .sources.base import FetchError
    from .sources.yahoo_closed import to_utc_iso
    from .yahoo_auction_page import AuctionPageError, fetch_auction_snapshot

    msgs: list[str] = []
    g = grader.strip().upper()
    if g not in ("PSA", "ARS", "BGS"):
        raise ValueError(f"不認得的鑑定機構：{grader!r}（支援 PSA / ARS / BGS）")
    label = grade_input.strip()
    try:
        grade_val = float(label.rstrip("+"))
    except ValueError as exc:
        raise ValueError(f"分數看不懂：{grade_input!r}（例：10、10+、9.5）") from exc
    if label.endswith("+"):
        msgs.append("⚠️ 標題解析把 10+ 折成 10.0（parse_grade 既定行為）："
                    "10 與 10+ 的標的都會以 🎯 通知，看標題原文分辨。")

    code_raw = code.strip()
    code_norm = ""
    if code_raw:
        codes = extract_title_codes(f" {code_raw} ")
        if not codes:
            raise ValueError(
                f"卡號正規化失敗：{code_raw!r}（預期形如 P4-06 / LOB-018）")
        code_norm = codes[0]

    name_ja = name_ja.strip()
    name_en = name_en.strip()
    aliases: list[str] = []
    master = _master_card(code_norm=code_norm, name_ja=name_ja)
    if master is not None:
        aliases = [str(a) for a in (master.get("aliases") or []) if a]
        if not name_en and master.get("name_en"):
            name_en = str(master["name_en"])
            msgs.append(f"主檔補上英文名：{name_en}")
        if master.get("name_ja") and master["name_ja"] != name_ja:
            aliases.append(str(master["name_ja"]))
        msgs.append(f"卡名主檔命中：{master.get('name_ja')}"
                    f"（別名 {len(aliases)} 個一併比對）")
    else:
        msgs.append("⚠️ 卡名主檔沒有這張卡——比對只用你提供的名字與卡號")

    watch_id = store.insert_card_watch(
        grader=g, grade=grade_val, grade_label=label, name_ja=name_ja,
        name_en=name_en, aliases=aliases,
        code_raw=code_raw, code_norm=code_norm, note=note,
    )
    msgs.append(f"已登錄狙擊 #{watch_id}：{g}{label} {name_ja} {code_raw}".rstrip())

    msgs += _ingest_census(store, fetcher, watch_id, url=census_url.strip(),
                           grader=g, name_ja=name_ja, code_raw=code_raw,
                           code_norm=code_norm)

    # 證據頁快照：入庫當下就抓（已結束頁 ~120 天會刪，實證下界 74 天）
    for raw_url in evidence_urls or []:
        ev_url = raw_url.strip()
        if not ev_url:
            continue
        if "auctions.yahoo.co.jp" not in ev_url:
            store.upsert_card_watch_evidence(
                watch_id, ev_url, status="unsupported",
                note="目前只支援 Yahoo 拍賣商品頁快照", site="")
            msgs.append(f"⚠️ 證據 URL 不是 Yahoo 拍賣頁，只存連結不解析：{ev_url}")
            continue
        try:
            snap = fetch_auction_snapshot(ev_url, fetcher=fetcher)
        except (FetchError, AuctionPageError) as exc:
            store.upsert_card_watch_evidence(watch_id, ev_url,
                                             status="unverifiable", note=str(exc))
            msgs.append(f"⚠️ 證據頁抓不到（{exc}）——已存 URL 標 unverifiable；"
                        f"讀不到 ≠ 不存在")
            continue
        store.upsert_card_watch_evidence(
            watch_id, ev_url, status="ok", title=snap.title,
            price_native=snap.price, currency=snap.currency,
            # ⚠️ 一律轉 UTC 再存：`card_watch_sale.sold_at` 是 UTC，兩張表存的是
            # 同一類事實（什麼時候賣掉的），混時區會讓 ORDER BY 的字典序排錯
            # （CLAUDE.md 第三節：同源同基準）。頁面原文是 `+09:00`。
            sold_at=to_utc_iso(snap.end_time) or snap.end_time, bids=snap.bids,
            seller_id=snap.seller_id, seller_name=snap.seller_name,
        )
        price_s = f"¥{snap.price:,.0f}" if snap.price is not None else "價格不明"
        msgs.append(f"證據快照：{snap.end_time[:10]} {price_s}"
                    f"（{snap.bids} 出價）賣家 {snap.seller_name or snap.seller_id}")

    # 市場成交檔案：登錄當下就挖。這是檔案的主要內容——我們自己的庫只有 181 天
    # 且是碰巧掃到的，市場的檔案一個請求就 150 天（實測目標卡：本地 0 筆 vs
    # 市場檔案 2 筆 exact）。挖不到不擋登錄，但要大聲講。
    if sources is not None:
        matcher = WatchMatcher.from_row(store.get_card_watch(watch_id))
        mined = mine_sold_archive(store, sources, matcher)
        msgs.append(f"市場成交檔案：{mined.summary()}")
        if not mined.ok:
            msgs.append("⚠️ 上面有管道沒挖成功——**那幾條的「0 筆」不代表沒賣過**，"
                        "之後用 `ygo-sniper snipe mine <id>` 重試")
    else:
        msgs.append("（未提供 sources，跳過市場成交檔案挖掘）")
    return AddResult(watch_id=watch_id, messages=msgs)


def refresh_watch_census(store: Any, fetcher: Any, watch: dict[str, Any]) -> list[str]:
    """重抓 census（URL 沒存到就重新用卡名搜）。"""
    return _ingest_census(
        store, fetcher, int(watch["id"]),
        url=str(watch.get("census_url") or ""),
        grader=str(watch.get("grader") or ""),
        name_ja=str(watch.get("name_ja") or ""),
        code_raw=str(watch.get("code_raw") or ""),
        code_norm=str(watch.get("code_norm") or ""),
    )


# ---------------------------------------------------------------------------
# 檔案（dossier）：census＋實證＋本地歷史＋命中帳＋等待建議
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Dossier:
    """一張卡的完整檔案。**三個資料桶的出處不同，永遠分開呈現、不合成一個數字。**

    - `sales`：市場自己的成交檔案（主要）——別人實際賣掉了多少錢
    - `local_history`：我們自己掃到的 comps／listing_obs（補充，樣本小且偏）
    - `evidence`：使用者提供的商品頁快照（人工指定，最高可信度）
    三者的分母完全不同：市場檔案是「這個平台這段期間的全部成交」，本地是
    「我們碰巧掃到的」，證據是「使用者手動指定的」。混在一起算平均就是混池。
    """

    watch: dict[str, Any]
    census: dict[str, Any] | None
    census_total: int | None
    sales: list[dict[str, Any]]
    local_history: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    hits: list[dict[str, Any]]
    recommendation: list[str]


def _census_of(watch: dict[str, Any]) -> dict[str, Any] | None:
    raw = watch.get("census_json") or ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def build_dossier(store: Any, watch: dict[str, Any]) -> Dossier:
    """檔案 ＝ 市場成交檔案（主）＋ 本地掃描歷史（補充）＋ 使用者證據。

    本地那一桶一律**現場重跑標題比對**——`comps.card_name`／`set_code` 欄位不可信
    （實測相關列全空）。三桶都逐筆列出、不做任何跨筆聚合：競標與定價不同池，
    市場檔案與我們的樣本也不同池。
    """
    m = WatchMatcher.from_row(watch)
    watch_id = int(watch["id"])
    local: list[dict[str, Any]] = []
    for ledger, rows in (
        ("comps", store.comps_title_rows()),
        ("listing_obs", store.listing_obs_title_rows()),
    ):
        for r in rows:
            tier = match_tier(m, str(r.get("title") or ""))
            if tier is None:
                continue
            h = dict(r)
            h["ledger"] = ledger
            h["tier"] = tier
            local.append(h)
    local.sort(key=lambda h: str(h.get("sold_at") or h.get("last_seen") or ""),
               reverse=True)
    sales = store.list_card_watch_sales(watch_id)
    hits = store.list_card_watch_hits(watch_id=watch_id)
    evidence = store.list_card_watch_evidence(watch_id)
    return Dossier(
        watch=watch,
        census=_census_of(watch),
        census_total=watch.get("census_total"),
        sales=sales,
        local_history=local,
        evidence=evidence,
        hits=hits,
        recommendation=build_recommendation(watch, sales, evidence, hits),
    )


def _prices(values: list[Any]) -> str:
    return "、".join(f"{p:,.0f}" for p in sorted(values)) or "價格不明"


#: 三種 URL 形態指向同一場拍賣，差別只在購買端／原生端：
#:   card_watch_sale.url         https://buyee.jp/item/yahoo/auction/n1235105710
#:   card_watch_sale.origin_url  https://page.auctions.yahoo.co.jp/jp/auction/n1235105710
#:   card_watch_evidence.url     https://auctions.yahoo.co.jp/jp/auction/n1235105710
#: 用 lookaround 不用 `\b`（CLAUDE.md 第二節），這裡靠 `/auction/` 前綴定位。
_AUCTION_ID_RE = re.compile(r"/auction/([A-Za-z]?\d+)")


def _sale_identity(
    *, url: str, origin_url: str = "", sold_at: str = "",
    price: Any = None, seller_id: str = "",
) -> str:
    """同一場拍賣的身分。**回傳空字串 ＝ 這一筆沒有可比對的身分，不得去重。**

    使用者貼的證據 URL 常常**就是**成交檔案裡挖到的那一筆（實測目標卡的兩個
    證據 URL 正是落札檔案的兩筆 exact），但兩個桶的 URL 形態不同。不去重就把
    一次成交算成兩次，而錯誤方向是「這張卡比實際更常出現」——使用者會據此
    以為供給穩定、不必急著搶（CLAUDE.md 第三節：同一桶價值被算兩次）。

    抽不到拍賣 ID 才退回事實三元組（同賣家、同時刻、同價 ＝ 同一筆）。
    **`sold_at` 為空時回空字串**：「兩筆都不知道何時賣的」不代表它們是同一筆，
    併掉就是反方向的錯（低報出現次數，而誤殺是靜默的——第一節）。
    """
    for u in (origin_url, url):
        m = _AUCTION_ID_RE.search(u or "")
        if m:
            return f"auction:{m.group(1)}"
    if sold_at:
        return f"fact:{seller_id}|{sold_at}|{price}"
    return ""


def build_recommendation(
    watch: dict[str, Any], sales: list[dict[str, Any]],
    evidence: list[dict[str, Any]], hits: list[dict[str, Any]],
) -> list[str]:
    """「去哪等」的建議。純事實組裝，不是模型——每一行都可回溯到一筆紀錄。

    賣家歸因**以市場成交檔案為主**：那是「誰真的賣掉過這張卡」，比我們自己
    掃到什麼可靠得多（我們的庫實測 0 筆，市場檔案一個請求就 150 天）。
    """
    lines: list[str] = []
    #: 每個賣家記**三組互不相加的數字**，不是一個 `n`。
    #: `sold_n`（有成交時刻的已成交）／`undated_n`（成交了但來源沒給時刻）／
    #: `listed_n`（**目前還在架上**）。分成三個計數器是結構性的：只要共用一個
    #: 計數器，日後任何一個新來源都能無聲地混進來（CLAUDE.md 第三節的第七項就是
    #: 「同源要問到機制那一層」）。
    sellers: dict[str, dict[str, Any]] = {}

    def _seller(key: str) -> dict[str, Any]:
        return sellers.setdefault(
            key, {"sold_n": 0, "undated_n": 0, "listed_n": 0, "last": "",
                  "prices": [], "undated_prices": []},
        )

    def _note_sale(key: str, when: str, price: Any) -> None:
        """**已成交**一筆。只有這個函式會動「賣掉幾次」與成交價。

        ⚠️ 「幾次」是日期類宣稱，沒有成交時刻的那批不能算進去：實測一次挖掘
        206 筆有 77 筆（Mercari／露天）給不出落札時刻，併進同一個計數就是拿
        兩種基準的東西合成一個數字。它們照樣列出來（價格是有效資訊），
        只是自己一個桶、自己講清楚。
        """
        s = _seller(key)
        if when:
            s["sold_n"] += 1
            if when > s["last"]:
                s["last"] = when
            if price is not None:
                s["prices"].append(price)
        else:
            s["undated_n"] += 1
            if price is not None:
                s["undated_prices"].append(price)

    def _note_listing(key: str) -> None:
        """**目前在架**一筆。獨立計數器，永遠不與 `sold_n` 相加。

        在架 ≠ 賣掉：hit 是「買得到的機會」，sale 是「已經成交的行情」，分母與
        意義都不同。混起來的錯誤方向照例是「看起來很划算」——同一件貨掛在架上
        三個月沒賣掉，會被讀成「這個賣家供給穩定，不必急」，而那正好是相反的
        結論（CLAUDE.md 第三節）。
        在架標的的價格是**要價**不是成交價，所以這裡連價格都不收——收了就會
        混進上面那串「成交 …」裡。
        """
        _seller(key)["listed_n"] += 1

    #: **「我們知道的成交」只有這一份**：去重後的 `sales` ∪ `evidence`，賣家歸因
    #: 與下面的出現頻率共用它。兩邊各自再數一次就是同一個 bug 換個地方長出來——
    #: 而且遲早會當著使用者的面打架：`evidence` 存在的理由正是「那場成交已經滾出
    #: 落札檔案（Yahoo 只留 150-180 天）、挖不到了」，所以「證據有、檔案沒有」
    #: 不是邊角情境，是這個功能的正常終局。
    #: **先跑 `sales` 再跑 `evidence`**：市場檔案欄位較完整（有 origin_url、
    #: bid_count、sale_kind），evidence 只補檔案裡沒有的那幾筆。
    #: `_note_listing`（在架）不參與去重——它記的是另一組數字。
    known: list[dict[str, Any]] = []
    seen_sales: set[str] = set()

    def _remember(
        *, ident: str, seller_key: str, sold_at: str, price: Any,
        origin: str, sale_kind: str,
    ) -> None:
        if ident:                  # 空身分 ＝ 沒有可比對的依據，一律當成不同筆
            if ident in seen_sales:
                return
            seen_sales.add(ident)
        known.append({"seller_key": seller_key, "sold_at": sold_at,
                      "price": price, "origin": origin, "sale_kind": sale_kind})

    for s in sales:
        if s.get("tier") != TIER_EXACT:
            continue
        seller_id = str(s.get("seller_id") or "")
        _remember(
            ident=_sale_identity(
                url=str(s.get("url") or ""),
                origin_url=str(s.get("origin_url") or ""),
                sold_at=str(s.get("sold_at") or ""),
                price=s.get("price_native"), seller_id=seller_id,
            ),
            seller_key=f"{s.get('site') or ''}:{seller_id}" if seller_id else "",
            sold_at=str(s.get("sold_at") or ""), price=s.get("price_native"),
            origin="archive", sale_kind=str(s.get("sale_kind") or ""),
        )
    for e in evidence:
        if e.get("status") != "ok":
            continue
        seller_id = str(e.get("seller_id") or "")
        _remember(
            ident=_sale_identity(
                url=str(e.get("url") or ""), sold_at=str(e.get("sold_at") or ""),
                price=e.get("price_native"), seller_id=seller_id,
            ),
            seller_key=(f"{e.get('site') or 'buyee_yahoo'}:{seller_id}"
                        if seller_id else ""),
            sold_at=str(e.get("sold_at") or ""), price=e.get("price_native"),
            # 證據快照沒有存成交型態欄位——**不知道就是不知道**，不從 bids 推導
            # （フリマ 定價成交的 bidCount 也是 1，那是佔位值）。空字串代表
            # 「這筆不參與成交型態的宣稱」，見下面的 kinds。
            origin="evidence", sale_kind="",
        )

    for k in known:
        if k["seller_key"]:        # 沒有賣家可歸因的成交仍然算次數，只是沒人可釘
            _note_sale(k["seller_key"], k["sold_at"], k["price"])
    for h in hits:
        if h.get("tier") == TIER_EXACT and h.get("seller_id"):
            _note_listing(f"{h.get('site') or ''}:{h['seller_id']}")

    # 排序也不把三個數字加起來：依序比「賣掉過幾次 → 幾筆無時刻 → 在架幾筆」。
    for key, s in sorted(
        sellers.items(),
        key=lambda kv: (-kv[1]["sold_n"], -kv[1]["undated_n"], -kv[1]["listed_n"],
                        kv[0]),
    ):
        facts: list[str] = []
        if s["sold_n"]:
            facts.append(f"賣掉過這張卡 {s['sold_n']} 次（最近 {s['last'][:10]}；"
                         f"成交 {_prices(s['prices'])}）")
            if s["undated_n"]:
                facts.append(f"另有 {s['undated_n']} 筆同款成交沒給成交時刻"
                             f"（成交 {_prices(s['undated_prices'])}，"
                             f"不算進上面的次數）")
        elif s["undated_n"]:
            facts.append(f"賣掉過這張卡 {s['undated_n']} 筆、但來源都沒給成交時刻"
                         f"（成交 {_prices(s['undated_prices'])}——只答得出價格，"
                         f"答不出何時）")
        if s["listed_n"]:
            # 0 就不印（不洗版）；有就一定要印——在架筆數是「去哪等」的直接依據。
            facts.append(f"目前在架 {s['listed_n']} 筆（要價，不是成交價）")
        lines.append(
            f"賣家 {key} " + "；".join(facts)
            + f"——建議釘選（每批都掃、不佔名額）：ygo-sniper watch-seller pin {key}"
        )
    if not sellers:
        lines.append(
            "成交檔案裡還沒有可歸因的賣家——靠全市場關鍵字掃描等它出現"
            "（狙擊查詢已自動加入每一輪掃描）。"
        )

    if known:
        # ⚠️ **只有帶真實成交時刻的才進「什麼時候／幾次」的宣稱。**
        # Mercari／露天的搜尋頁給不出落札時間（實測 99/206 筆 sold_at 是空的），
        # 把它們算進次數或期間，就是拿兩種基準的東西合成一個數字
        # （CLAUDE.md 第三節；comps 的 sold_at_is_ingest 是同一個立場）。
        dated = [k for k in known if k["sold_at"]]
        undated_n = len(known) - len(dated)
        stamps = sorted(k["sold_at"][:10] for k in dated)
        # 成交型態只由**真的存了型態的那批**決定（證據快照沒這個欄位）——
        # 「沒記錄」不等於「是競標」，不知道的一筆不該讓宣稱變強也不該讓它消失。
        kinds = {k["sale_kind"] for k in dated if k["sale_kind"]}
        span = f"（{stamps[0]} → {stamps[-1]}）" if stamps else ""
        if dated:
            # 這句**只要有帶日期的成交就一定要講**（不是只在 >= 2 筆時）：一筆的時候
            # 更需要知道那不是全部歷史，否則會把「我們知道 1 次」讀成「一年只出現 1 次」。
            from_archive = sum(1 for k in dated if k["origin"] == "archive")
            lines.append(
                f"出現頻率：已知 {len(dated)} 次成交{span}——市場檔案挖到 "
                f"{from_archive} 筆、你提供的證據 {len(dated) - from_archive} 筆"
                f"（與上面賣家行同一份去重後的紀錄）"
                f"——**這是我們知道的次數，不是全部歷史**（Yahoo 落札相場約保留 "
                f"150-180 天，更早的已經被平台刪掉，我們挖不到；證據那幾筆正是"
                f"因此才需要你手動指定）。"
            )
        if undated_n:
            lines.append(
                f"另有 {undated_n} 筆同款成交**來源沒給成交時刻**（Mercari／露天的"
                f"搜尋頁沒有落札時間）——它們只答得出價格，不列入上面的次數與期間。"
            )
        if kinds == {"auction"}:
            lines.append(
                "全部走競標成交——結標時段（台灣 18:00-22:30）是關鍵，"
                "排程在該時段每 30 分掃一次。"
            )
    census = _census_of(watch)
    if census:
        at = census.get(str(watch.get("grade_label") or ""))
        if at is not None:
            lines.append(
                f"稀缺度：{watch['grader']}{watch['grade_label']} 全世界只有 "
                f"{at} 張——每次出現可能相隔數月，👀 疑似命中也值得點開看。"
            )
    lines.append(
        "節奏：結標高峰（台灣 18:00-22:30）每 30 分掃一次、白天每 2 小時；"
        "狙擊命中即推播且 🎯 不受每輪則數上限裁切——最壞情況約晚 30 分鐘知道。"
    )
    return lines


# ---------------------------------------------------------------------------
# 通知脈絡（規則 4）。evaluate() 只吃這個形狀，不自己查 db。
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SnipeNotifyContext:
    """規則 4（指定卡狙擊）判定所需的全部事實。

    pending 只含「還沒送過的 exact／partial」——near 在源頭就不進通知路徑
    （它的去處是 dashboard），去重帳只有 notify_log 一本。
    """

    watches: dict[int, dict[str, Any]] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)


def build_notify_context(store: Any) -> SnipeNotifyContext:
    ctx = SnipeNotifyContext()
    ctx.watches = {int(w["id"]): w for w in store.list_card_watch(active_only=True)}
    if not ctx.watches:
        return ctx
    for hit in store.list_card_watch_hits(tiers=(TIER_EXACT, TIER_PARTIAL)):
        if hit.get("sent_at"):
            continue
        w = ctx.watches.get(int(hit["watch_id"]))
        if w is None:            # watch 已停用：命中留帳，但不再通知
            continue
        row = dict(hit)
        row["watch"] = w
        ctx.pending.append(row)
    return ctx
