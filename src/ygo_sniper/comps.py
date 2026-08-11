"""行情資料（comps）。

沒有 comps，「discount」這個字就沒有意義 —— 你只是在買便宜的東西，
不是在買被低估的東西。這兩件事差很多。

三個來源，信心度不同：

  high    Buyee 的 Mercari「已售出」搜尋 —— 日本市場真實成交價。
          對日版 / ARS 卡，這比 eBay 準很多。
  medium  自建 snapshot：每天記錄在架標的，某天消失了就當作「可能成交」。
          有明顯偏誤（下架 ≠ 賣掉），只能當補充。
  low     樣本數 < min_comps，只給你看，不要拿來當判斷依據。

comps 一律用「不含刷卡手續費」的匯率換算，跟成本用不同口徑。
把 markup 加進行情會讓兩邊一起往上漂，折價率就假了。
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .domain import CardInfo, CompStats, Listing, SaleKind
from .parsers import is_candidate, normalize, parse_card

_TOKEN_RE = re.compile(r"[ぁ-んァ-ヶ一-龯A-Za-z0-9]+")
_STOP = {
    "遊戯王", "YUGIOH", "YU", "GI", "OH", "CARD", "カード", "美品", "即決",
    "送料無料", "匿名配送", "PSA", "ARS", "BGS", "鑑定", "鑑定品", "鑑定書",
}


def card_signature(title: str, info: CardInfo) -> str:
    """把標題壓成一個可比對的鍵。

    優先用卡號（最可靠）。沒有卡號就用「機構+分數+前三個有意義的詞」，
    這會有誤判，所以 CompStats 一定要把 samples 帶回前端讓你自己看一眼。
    """
    grade_part = f"{info.grader.value}{info.grade or ''}"
    if info.set_code:
        return f"{info.set_code}|{grade_part}"

    tokens = [
        t for t in _TOKEN_RE.findall(normalize(title).upper())
        if t not in _STOP and len(t) >= 2 and not t.isdigit()
    ]
    return f"{'-'.join(tokens[:3])}|{grade_part}"


# ---------------------------------------------------------------------------
# 成交型態（sale_kind）：競標結標 vs 定價成交
# ---------------------------------------------------------------------------
#: **平台上根本沒有競標機制**的站台。這是比逐筆旗標更硬的事實，所以它先判：
#: Mercari 與 Yahoo!フリマ（PayPay）只有「賣家開價、買家按下去」一種成交方式。
#: 2026-08-06 對帳過沒有衝突——快取的 closedsearch 快照裡 2,553 筆
#: `isFleamarketItem=True` 的標的 `isFixedPrice` **全部**是 True（2553/2553）。
NO_AUCTION_SITES: frozenset[str] = frozenset({
    "buyee_mercari", "buyee_paypay", "mercari_tw",
})


def sale_kind_for(site: str | None, raw: Mapping | None = None) -> SaleKind:
    """這個價格是**買家喊上去的**還是**賣家開的**。判定只有這一處。

    證據順序（強→弱），拿不到證據就是 `UNKNOWN`：

    1. **平台事實**（`NO_AUCTION_SITES`）——那些站台不存在競標，不需要逐筆證據。
    2. **來源逐筆旗標** `raw["is_fixed_price"]`（`yahoo_closed` 從 closedsearch
       的 `isFixedPrice` 帶回來：False＝競標落札、True＝一口價即決）。
    3. **eBay 的 `buyingOptions`**——兩種都有的站台必須看逐筆欄位。純 AUCTION
       ＝競標；含 FIXED_PRICE（含「競標帶 BIN」）＝我們取的是 BIN 價，屬定價
       （與 `sources.ebay.read_price` 的判準同一套，見該檔頂註的對照表）。
    4. 其他 → `UNKNOWN`。**絕不猜 `FIXED`**：猜錯的方向是「這個賣家好便宜」，
       使用者的直覺攔不下來（本專案第三節）。
    """
    if (site or "") in NO_AUCTION_SITES:
        return SaleKind.FIXED
    raw = raw or {}
    flag = raw.get("is_fixed_price")
    if isinstance(flag, bool):
        return SaleKind.FIXED if flag else SaleKind.AUCTION
    opts = raw.get("buyingOptions")
    if isinstance(opts, (list, tuple, set, frozenset)):
        names = {str(o) for o in opts}
        if "FIXED_PRICE" in names:
            return SaleKind.FIXED
        if "AUCTION" in names:
            return SaleKind.AUCTION
    return SaleKind.UNKNOWN


def sale_kind_of(listing: Listing) -> SaleKind:
    """`Listing` → 成交型態。旗標抽不到一律 `UNKNOWN`（不猜）。"""
    return sale_kind_for(listing.site.value, listing.raw)


def _external_id_of(url: str | None) -> str:
    """comps 的 `url` → 站台的商品 ID（尾段）。

    三條路徑實測都是「ID 在最後一段」：
    `buyee.jp/item/yahoo/auction/p1229439831`、
    `buyee.jp/paypayfleamarket/item/z654154608`、`buyee.jp/mercari/item/m19094616319`。
    """
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# 同時出品去重（comps.dup_of_id）
# ---------------------------------------------------------------------------
#: 日本賣家可以把同一件實體商品同時掛在ヤフオク!與 Yahoo!フリマ（PayPay）——
#: 賣掉一邊，另一邊自動下架。**只有這兩站可能是同一實體商品**：跨公司
#: （Mercari／eBay）不存在這種同時出品機制，一律不判重複。
_DUAL_LISTING_SITES: frozenset[str] = frozenset({"buyee_yahoo", "buyee_paypay"})

#: 同時出品的成交時間窗（分鐘）。2026-08-05 全語料掃描（2,566 筆 comps）：
#: 真案例（同商品、同原幣金額、標題逐字相同）時間差 26.2 分鐘；同一次掃描
#: 找到的次接近候選（同價格但**不同卡**）時間差 157.1 分鐘。60 分鐘落在兩者
#: 正中間，給真案例 2.3 倍餘裕、離最近的假候選還有 2.6 倍空間——調這個數字
#: 不會讓已知的任何一組候選變動判定。
_DUAL_LISTING_WINDOW_MINUTES = 60.0

#: 兩邊都有值卻衝突就不判重（防禦性檢查，正常情況下標題逐字相同時這些欄位
#: 本來就該一致——這裡是防漏網，不是主要判準）。
_DUAL_LISTING_IDENTITY_FIELDS: tuple[str, ...] = (
    "card_name", "set_code", "rarity", "grader", "grade",
)


def _parse_sold_at(value: Any) -> datetime | None:
    """`comps.sold_at` → 帶時區的 datetime。解析不出來回 None（呼叫端不猜）。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def find_dual_listing_duplicates(rows: Iterable[Mapping]) -> list[dict]:
    """把 `rows`（comps 原始列）裡「同一實體商品跨 Yahoo 家族同時出品」的
    重複成交抓出來。**純函式，不碰 db**——寫入是另一步（`mark_dual_listing_duplicates`）。

    判準（全部同時成立，任一項有疑慮就不判重——寧可漏抓，不可誤殺真成交，
    見本專案 CLAUDE.md 第一節）：

    1. 一邊 `buyee_yahoo`、一邊 `buyee_paypay`（`_DUAL_LISTING_SITES`）。
       跨公司（Mercari／eBay）一律不判——同時出品不可能跨公司。
    2. `price_native` 與 `currency` 完全相同——原幣同額，不是換算後湊巧撞上
       （工程原則 1：混源比較會產生無聲的誤判）。
    3. `sold_at` 相差在 `_DUAL_LISTING_WINDOW_MINUTES` 分鐘內。
    4. 標題正規化（`parsers.normalize`）後逐字相同——同一份刊登文案，
       是全部判準裡最強的單一訊號。
    5. 已解析的卡片屬性（`_DUAL_LISTING_IDENTITY_FIELDS`）沒有任何一項衝突
       （防禦性複查；標題相同時這些欄位本來就該一致）。

    留哪筆：一律留 `buyee_yahoo`——那一站的 `sold_at` 100% 是真實成交時間，
    `buyee_paypay` 混著入庫時間（見 `store._COMPS_ATTR_COLUMNS` 的
    `sold_at_is_ingest` 註記）。回傳的每一組都帶著判定依據，方便人工複核。
    """
    yahoo = [r for r in rows if r.get("site") == "buyee_yahoo"]
    paypay = [r for r in rows if r.get("site") == "buyee_paypay"]
    matches: list[dict] = []
    for y in yahoo:
        y_price = y.get("price_native")
        y_title = y.get("title")
        y_sold = _parse_sold_at(y.get("sold_at"))
        if y_price is None or not y_title or y_sold is None:
            continue
        y_norm = normalize(y_title)
        for p in paypay:
            if p.get("price_native") != y_price:
                continue
            if not (y.get("currency") or "") or (p.get("currency") or "") != (y.get("currency") or ""):
                continue
            p_title = p.get("title")
            if not p_title or normalize(p_title) != y_norm:
                continue
            p_sold = _parse_sold_at(p.get("sold_at"))
            if p_sold is None:
                continue
            delta_minutes = abs((y_sold - p_sold).total_seconds()) / 60.0
            if delta_minutes > _DUAL_LISTING_WINDOW_MINUTES:
                continue
            conflict = any(
                y.get(f) is not None and p.get(f) is not None and y.get(f) != p.get(f)
                for f in _DUAL_LISTING_IDENTITY_FIELDS
            )
            if conflict:
                continue
            matches.append({
                "keep_id": y.get("id"),
                "dup_id": p.get("id"),
                "keep_site": "buyee_yahoo",
                "dup_site": "buyee_paypay",
                "price_native": y_price,
                "currency": y.get("currency"),
                "delta_minutes": round(delta_minutes, 1),
                "keep_sold_at": y.get("sold_at"),
                "dup_sold_at": p.get("sold_at"),
                "title": y_title,
                "keep_url": y.get("url"),
                "dup_url": p.get("url"),
            })
    return matches


def mark_dual_listing_duplicates(store, *, dry_run: bool = False) -> dict:
    """對 `store` 裡的既有 comps 跑 `find_dual_listing_duplicates`，把結果寫進
    `comps.dup_of_id`。**不刪除任何列**——標記可以撤回，刪除不可逆。

    已經標記過的列不再參與偵測（既不當 keep 也不當 dup 的候選）：避免重跑時
    同一組重複配對，也避免鏈狀誤標（A 標成 B 的重複、B 又被標成 C 的重複）。
    這使得整支函式**冪等**：第二次起 `matches` 必為空。

    回傳每一組的完整判定依據（`matches`），供 CLI 印出來給人看——沒有這個
    列表就等於靜默過濾（本專案第一節）。
    """
    rows = store.comps_by(limit=1_000_000)
    candidates = [r for r in rows if r.get("dup_of_id") is None]
    matches = find_dual_listing_duplicates(candidates)
    if matches and not dry_run:
        store.mark_comps_duplicates([(m["dup_id"], m["keep_id"]) for m in matches])
    return {
        "rows": len(rows),
        "candidates": len(candidates),
        "matches": matches,
        "dry_run": dry_run,
    }


def backfill_sale_kind(
    store, evidence: Mapping[str, bool] | None = None, *, dry_run: bool = False
) -> dict:
    """把既有 comps 的 `sale_kind` 補起來。**只吃證據，冪等，不增不減列。**

    `evidence` 是「站台商品 ID → isFixedPrice」（來源：
    `sources.yahoo_closed.sale_flags_from_cache`，走生產解析路徑挖快取快照）。
    查不到證據的列**一律 `unknown`**——不准用「多數是競標所以猜競標」這種推論，
    那是拿統計當個案證據，錯了完全看不出來。

    寫入規則（冪等的來源）：
    - 還沒有值（NULL／空字串）→ 寫。
    - 已經是 `unknown`、而這次算得出真實型態 → **升級**。第一次跑的時候快取
      剛好被清，這批就會永遠卡在 unknown，而且完全看不出來。
    - 已經是 `auction`／`fixed` → 一律不碰（證據消失不該把已知抹成未知）。

    回傳逐站逐型態的帳（`by_site_kind`）與實際寫入列數（`updated`）。
    """
    evidence = evidence or {}
    rows = store.comps_by(limit=1_000_000)
    pending: list[tuple[int, str]] = []
    by_site_kind: dict[tuple[str, str], int] = {}
    unchanged = 0
    for r in rows:
        site = str(r.get("site") or "")
        flag = evidence.get(_external_id_of(r.get("url")))
        kind = sale_kind_for(site, {"is_fixed_price": flag} if isinstance(flag, bool) else {})
        current = (r.get("sale_kind") or "").strip()
        # 只有「還沒有值」與「已經是 unknown」兩種狀態可以被這次的結果蓋過
        writable = current in ("", SaleKind.UNKNOWN.value)
        if writable and kind.value != current:
            pending.append((int(r["id"]), kind.value))
            final = kind.value
        else:
            unchanged += 1
            final = current or kind.value
        by_site_kind[(site, final)] = by_site_kind.get((site, final), 0) + 1
    if pending and not dry_run:
        store.set_sale_kinds(pending)
    return {
        "rows": len(rows),
        "updated": len(pending),
        "unchanged": unchanged,
        "with_evidence": sum(
            1 for r in rows if isinstance(evidence.get(_external_id_of(r.get("url"))), bool)
        ),
        "by_site_kind": by_site_kind,
        "dry_run": dry_run,
    }


def _reason_bucket(why: str) -> str:
    """把 is_candidate 的原因收斂成可統計的桶。

    分數原因帶著實際數值（「分數 5.0 低於 7」），逐字統計會炸成一堆
    只有 1 筆的桶，所以只有這一類收斂；排除字與機構保留原文，
    因為「被哪個字擋掉」正是調 watchlist 時要看的東西。
    """
    # 年份否決同理：原因帶著實際年份（「標題年份 2007 不在 1998-2004」），
    # 逐字統計會炸成一堆只有 1-3 筆的桶。
    if why.startswith("標題年份"):
        return "標題年份不在 1998-2004"
    return "分數低於門檻" if why.startswith("分數") else why


# ---------------------------------------------------------------------------
# 已售出查詢的組合展開與請求節流
# ---------------------------------------------------------------------------
#: 分片游標：下一輪從展開後清單的第幾個 (source, keyword) 開始跑。
#: 存 store meta（跨行程、跨重啟都要記得住，用記憶體變數等於沒節流）。
SOLD_CURSOR_KEY = "comps_sold_cursor"
#: 同一分片連續整片失敗的次數；達 3 次就強制推進游標並出聲，避免壞片卡死輪替。
SOLD_STALL_KEY = "comps_sold_stall"
SOLD_STALL_LIMIT = 3

#: 連續幾輪「整片被擋」（跟上面的 stall 是不同的帳——被擋不重試，
#: 每輪都會前進，所以不能用 SOLD_STALL_KEY 數）。與 SOLD_STALL_LIMIT 同值
#: 只是巧合對齊操作者心智模型（連三次都不對勁），兩個帳本各自獨立記帳。
SOLD_BLOCKED_STREAK_KEY = "comps_sold_blocked_streak"
SOLD_BLOCKED_STREAK_LIMIT = 3

#: 「正在嘗試哪個游標、嘗試了幾次」，值是 "<cursor>:<count>"。
#:
#: 這本帳補的是一個真實的 crash-safety 破洞：這個分支把「發哪個游標的片」
#: （`sold_shard`）跟「跑完了嗎、要不要前進」（`commit_sold_shard`）拆開，
#: 換來精準（只有真的做完事才前進），但代價是不再對「跑到一半被殺」免疫。
#: 舊制 `claim_sold_run` 是「問之前先扣」——一問就把配額算掉，天然扛得住
#: crash（配額已經扣了，不會重複問）；這裡的新制是「先問再扣」，中途被殺
#: （這個 repo 另外加了 25 分鐘 watchdog，逾時 SIGKILL 整棵行程樹）就會
#: 兩個帳本都沒寫到：游標沒動、SOLD_STALL_KEY 也沒動（stall 只數「跑完但
#: 全失敗」，不數「根本沒跑完」）。實測分片 0 剛好是 buyee_mercari／
#: paypay_direct（Playwright/WAF 那條路），也就是全部歷史 hang 都出在的
#: 那一段——會活生生撞上這個洞：卡在分片 0 → 被殺 → 下一輪原地重跑分片 0
#: → 再卡 → 再殺，永遠到不了後面 80 條 yahoo_closed，comps 靜默停更新，
#: 而 watchdog 的 Telegram 只會說「殺了一次」，不會說「comps 已經連續
#: 幾天沒進度」。
#:
#: 這裡記的是「正要試游標 N」，不是「已經吃掉游標 N 的配額」——`sold_shard`
#: 寫這個標記本身**不會**推進 `SOLD_CURSOR_KEY`，一輪正常跑完
#: （`commit_sold_shard` 真的被叫到）就會清掉它，不干擾原本 commit-on-success
#: 的精準度。只有「連續交出去卻沒等到 commit」累積到門檻，才由 `sold_shard`
#: 自己強制把游標推過去（因為呼叫端是 `pipeline.refresh_comps`，這次改動
#: 刻意不動它——分片選擇與「發現卡死該怎麼辦」被迫擠進同一個入口）。
SOLD_ATTEMPT_KEY = "comps_sold_attempt"
#: 比 SOLD_STALL_LIMIT（3）小：兩者量的不是同一件事，不能共用同一個門檻
#: （那會是另一種混源比較）。stall 數的是「跑完了，但查詢乾淨地失敗」，
#: 失敗一次很便宜（幾秒鐘），多容忍幾輪換一次網路狀況的成本很低；這裡數的
#: 是「整個行程被 25 分鐘 watchdog SIGKILL」，一次就是 25 分鐘蒸發，連續
#: 兩次已經是強訊號（同一段真的卡死，不是雜訊），沒必要拖到 3 次再放手，
#: 那只是讓 comps 多停擺一輪。
SOLD_ATTEMPT_LIMIT = 2


def _parse_sold_attempt(raw: str | None) -> tuple[int | None, int]:
    """解析 `SOLD_ATTEMPT_KEY` 的 "<cursor>:<count>"。沒有標記或壞值一律當作
    「沒有進行中的嘗試」（游標 None、次數 0）——與 SOLD_CURSOR_KEY／
    SOLD_STALL_KEY 同一套容錯慣例：壞掉的 meta 值不該讓排程整個炸掉。
    """
    if not raw:
        return None, 0
    try:
        cursor_str, count_str = raw.split(":", 1)
        return int(cursor_str), int(count_str)
    except (ValueError, TypeError):
        return None, 0


def _sold_observability_warning() -> str:
    """整片失敗（被擋或其他原因）時要附的觀測指引，被擋與 stall 兩條路徑共用
    同一份文字——分開寫的話，其中一條被改了用詞、另一條沒跟上，訊息就會
    悄悄失真（這正是這份訊息第一版被抓到的問題：宣稱了 AlertEngine 涵蓋
    不到的事）。

    真話只有這幾件：印出來的 `[warn] comps …` 明細與 log 檔案才是唯一訊號，
    dashboard 的來源健康度看不到——comps_queries 展開查詢主要打
    yahoo_closed，它沒有 canary、不進 AlertEngine，壞了那個面板不會變紅。
    """
    return (
        "訊號只有這行與同一輪印出的每條 `[warn] comps <來源> 「<關鍵字>」失敗`"
        "——去 data/logs/daily-*.log 找。不要指望 dashboard 的來源健康度："
        "comps_queries 展開查詢主要打 yahoo_closed，它沒有 canary、不進 "
        "AlertEngine，壞了那個面板不會變紅。"
    )

#: 展開後的查詢數硬上限。組合展開是「乘法」，稀有度 6 × 年代 6 × 機構 2 = 72，
#: 手一滑多加兩個詞就變 120——而每個查詢最多還要翻 N 頁。這個上限不是禮貌，
#: 是**結構性的請求預算**：超出就截斷並印出來，讓人看得見自己開了多少水龍頭
#: （工程原則 5 的同型：把判準放在必經之處，而不是放在註解裡靠人記得）。
DEFAULT_MAX_COMPS_QUERIES = 120

_WS_RE = re.compile(r"\s+")


def expand_comps_queries(spec: Mapping | None) -> list[str]:
    """把 `watchlist.comps_queries` 展開成關鍵字清單（純函式，可單測）。

    `template` × `eras` × `rarities` × `graders` 的笛卡兒積，再接上 `extra`
    的固定查詢。任一維度留空就等於「這一維不參與展開」（用空字串佔位），
    所以只填 eras 也是合法設定。

    **三個維度全空時完全不展開**（不會退化成只送 template 的常數部分）：
    「設定被改空了」與「刻意只跑一個 `遊戯王` 這種超廣查詢」是兩件事，
    前者遠比後者常見，預設要往「什麼都不做」倒。

    展開後去重且保序：保序讓「哪些查詢先跑」是可預測的（截斷發生在尾端，
    不會每次砍掉不同的一批）。
    """
    spec = spec or {}
    template = str(spec.get("template") or "遊戯王 {era} {rarity} {grader}")
    out: list[str] = []

    if any(spec.get(dim) for dim in ("eras", "rarities", "graders")):
        eras = [str(x) for x in (spec.get("eras") or [""])]
        rarities = [str(x) for x in (spec.get("rarities") or [""])]
        graders = [str(x) for x in (spec.get("graders") or [""])]
        for grader in graders:
            for era in eras:
                for rarity in rarities:
                    kw = template.format(era=era, rarity=rarity, grader=grader)
                    # 空維度會留下連續空白（"遊戯王  PSA"），Yahoo 會照收但兩個
                    # 只差空白數的查詢就成了兩個 cache key、兩次請求
                    kw = _WS_RE.sub(" ", kw).strip()
                    if kw:
                        out.append(kw)

    out.extend(str(x) for x in (spec.get("extra") or []) if str(x).strip())

    seen: set[str] = set()
    deduped: list[str] = []
    for kw in out:
        if kw not in seen:
            seen.add(kw)
            deduped.append(kw)

    cap = int(spec.get("max_queries", DEFAULT_MAX_COMPS_QUERIES) or DEFAULT_MAX_COMPS_QUERIES)
    if cap > 0 and len(deduped) > cap:
        print(f"[comps] 展開出 {len(deduped)} 個已售出查詢，超過上限 {cap}，截斷尾端")
        deduped = deduped[:cap]
    return deduped


@dataclass(slots=True)
class SoldShard:
    """一輪要跑的已售出查詢分片。`next_cursor is None` = 不推進游標
    （無 store、every_n_runs<=1 的「每輪全跑」情境；force 全量會給 0，
    仍然推進，見 `sold_shard`）。"""

    queries: list[tuple[str, str]]
    label: str
    next_cursor: int | None


@dataclass(slots=True)
class IngestReport:
    """一次入庫的結果。**擋掉幾筆、為什麼**跟收了幾筆一樣重要——

    沒有這份帳，行情表被垃圾汙染時你看不出來：實測 405 筆歷史 comps 裡
    有 175 筆（43%）是寶可夢、卡套、現代卡，就是因為當初 ingest 沒過濾也沒記帳。
    """

    kept: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        top = ", ".join(
            f"{k}×{v}" for k, v in sorted(self.reasons.items(), key=lambda kv: -kv[1])[:5]
        )
        return f"收 {self.kept} 筆、擋 {self.rejected} 筆" + (f"（{top}）" if top else "")


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class CompsEngine:
    def __init__(self, cfg: Config, fx, store=None) -> None:
        self.cfg = cfg
        self.fx = fx
        self.store = store
        self.window_days = int(cfg.scoring["comps_window_days"])
        self.min_comps = int(cfg.scoring["min_comps"])
        self._index: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    def ingest_sold(self, listings: list[Listing]) -> IngestReport:
        """把「已售出」的標的餵進行情索引。

        **每一筆都必須過 parse_card + is_candidate**，與掃描在架標的用完全
        同一組判準（同源、同基準——工程原則 1）。以前這裡只擋「排除字或
        沒有鑑定機構」，比掃描端寬鬆得多，於是寶可夢（メガリザードンXEX）、
        卡套（CARDBOARD GOLD PSA鑑定品用スリーブ）、現代卡全進了行情表，
        再回頭當「1998-2004 卡的合理價」用——那是最貴的一種靜默錯誤。

        入庫時同時落結構化屬性（rarity/grader/grade/set_code/era_evidence），
        原始 title 永久保留：分桶是查詢時的決定，不是寫入時的決定。

        `sold_at` 優先採用來源帶回的真實成交時間（`raw["sold_at"]`，必須是
        UTC ISO 字串）。來源給不出時間才退回 now()——那是「我們什麼時候看到
        它已售出」，對 Buyee 的 sold_out 搜尋是唯一能知道的近似值（實測那些
        頁面**完全沒有成交時間**：搜尋頁的 tile 只有 SOLD／標題／價格，商品頁
        也沒有），但對 Yahoo 落札相場（視窗 180 天、comps 視窗 90 天）就會把
        半年前的成交蓋成今天的行情。時間戳與視窗必須同基準（工程原則 1）。

        `sale_kind` 同時落下去（2026-08-06）：競標結標與定價成交是兩種語意，
        來源早就把 `isFixedPrice` 帶回來了，只是入庫時被丟掉——於是下游拿
        「別人搶出來的落札價」當「賣家開的價」比，量到的是熱度不是定價行為。

        **退回 now() 的那一批一律標 `sold_at_is_ingest=1`**（2026-08-02）。
        沒有這個旗標，下游分不出「這是成交時間」與「這是我們抓到的時間」，
        於是 90 天視窗對那批資料形同虛設、時間切分變成偽裝的平台切分。
        標記而不是猜：猜一個成交時間會讓所有數字看起來很正常，然後全部是錯的。
        """
        report = IngestReport()
        wl = self.cfg.watchlist
        fallback_sold_at = datetime.now(UTC).isoformat()
        for lst in listings:
            info = parse_card(lst.title, wl)
            ok, why = is_candidate(info, wl)
            if not ok:
                report.rejected += 1
                bucket = _reason_bucket(why)
                report.reasons[bucket] = report.reasons.get(bucket, 0) + 1
                continue
            sig = card_signature(lst.title, info)
            price_twd = self.fx.to_twd(lst.price, lst.currency, apply_markup=False)
            raw_sold_at = lst.raw.get("sold_at")
            has_real_time = isinstance(raw_sold_at, str) and bool(raw_sold_at)
            sold_at = raw_sold_at if has_real_time else fallback_sold_at
            self._index.setdefault(sig, []).append(
                {
                    "title": lst.title,
                    "price_twd": round(price_twd, 0),
                    "price_native": lst.price,
                    "currency": lst.currency.value,
                    "url": lst.url,
                    "site": lst.site.value,
                    "sold_at": sold_at,
                    # 0 = sold_at 是來源給的真實成交時間；1 = 是我們入庫的時間。
                    # 下游任何用到時間的查詢都必須看這一欄（見 store.load_comps）。
                    "sold_at_is_ingest": 0 if has_real_time else 1,
                    #: **這筆是「買家搶到多高」還是「賣家開多少」**（見 `sale_kind_for`）。
                    #: 兩者不可混池：Seller Alpha 的同儕比對拿它當鍵的一部分。
                    #: 抽不到證據就是 `unknown`，而 `unknown` 不進任何比較。
                    "sale_kind": sale_kind_of(lst).value,
                    "confidence": "high",
                    "rarity": info.rarity,
                    "grader": info.grader.value,
                    "grade": info.grade,
                    "set_code": info.set_code,
                    "era_evidence": ",".join(info.era_evidence),
                    # 賣掉這筆的賣家（來源給不出就是 None，不猜）。
                    # 成交紀錄帶賣家，「同一賣家持續低於行情成交」才量得出來。
                    "seller_id": lst.seller_id,
                    # 卡名抽取還沒做（下一輪）。留空欄位而不是塞猜的值：
                    # 猜出來的卡名會直接變成分桶依據，錯了看不出來。
                    "card_name": None,
                }
            )
            report.kept += 1
        if self.store and report.kept:
            self.store.save_comps(self._index)
        return report

    def load_from_store(self) -> None:
        """把視窗內的成交讀進記憶體索引。

        ⚠️ **`window_days` 這道視窗對 `sold_at_is_ingest=1` 的列形同虛設**
        （2026-08-06 實測：Mercari 1046/1046 筆、PayPay 77/563 筆都是入庫時間），
        它們的時間戳永遠落在最近，所以永遠在視窗內。這裡**刻意不開
        `real_sold_at_only`**：這批列的**價格**是真的市場成交價，而它們是
        Mercari 唯一的行情來源，濾掉等於讓 Mercari 沒有行情可比。
        代價是「過去 N 天」對它們不成立——真正的老資料混在裡面看不出來。
        要做的是把真實成交時間挖出來（來源頁面沒有，得另尋管道），
        不是在這裡靜靜地砍掉樣本。
        """
        if not self.store:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self.window_days)
        self._index = self.store.load_comps(since=cutoff)

    # ------------------------------------------------------------------
    def stats_for(self, listing: Listing, info: CardInfo) -> CompStats:
        sig = card_signature(listing.title, info)
        rows = self._index.get(sig, [])

        # 找不到完全相同的簽章時，退一步用卡號比對
        if not rows and info.set_code:
            rows = [
                r for key, lst in self._index.items()
                if key.startswith(f"{info.set_code}|") for r in lst
            ]

        prices = [float(r["price_twd"]) for r in rows if r.get("price_twd")]
        n = len(prices)

        if n >= self.min_comps * 2:
            conf = "high"
        elif n >= self.min_comps:
            conf = "medium"
        else:
            conf = "low"

        return CompStats(
            n=n,
            median_twd=round(statistics.median(prices), 0) if prices else None,
            p25_twd=round(_percentile(prices, 25), 0) if prices else None,
            p40_twd=round(_percentile(prices, 40), 0) if prices else None,
            p75_twd=round(_percentile(prices, 75), 0) if prices else None,
            window_days=self.window_days,
            confidence=conf,
            # 這個排序讀作「時間戳最新的八筆」而**不是**「最近成交的八筆」：
            # Buyee 系的 sold_at 是入庫時間（見 `load_from_store` 的警告），
            # 對它們來說這是入庫先後。只當展示用的取樣，不進任何統計
            # （上面的中位／百分位吃的是全部 rows，與這個排序無關）。
            samples=sorted(rows, key=lambda r: r.get("sold_at", ""), reverse=True)[:8],
        )

    # ------------------------------------------------------------------
    def sold_queries(self, sources: dict[str, object]) -> list[tuple[str, str]]:
        """哪些 (發現管道, 關鍵字) 要另外跑「已售出」搜尋來累積行情。

        兩個來路，合併去重：

        1. **watchlist 的 `queries`**（舊行為）——判準是 source 自己宣告的
           `supports_sold`，不是名稱前綴。Yahoo 直抓（False，搜尋頁沒有成交
           紀錄）不進來；Buyee 系（True）進來。
        2. **`comps_queries` 的組合展開**（新）——稀有度 × 年代 × 機構。
           在架掃描要的是「新上架、可能定價錯誤」，行情累積要的是「涵蓋整個
           屬性空間、每個桶都有樣本」，兩者的查詢集合本來就不該是同一組：
           改這裡之前，watchlist 的 5 條在架查詢撐出來的樣本平均每個
           card_signature 只有 1.07 筆，統計上等於沒有行情——絕大多數訊號
           只能報「無足夠樣本」。

        `comps_queries.sources` 必須明列（不預設「所有 supports_sold 的來源」）：
        展開後有數十個關鍵字，讓它自動套用到每一條管道，請求數會直接乘上管道數。
        不在 registry 或不支援 sold 的名字一律跳過。

        comps 索引鍵是 card_signature、跨站台共用，所以任一站的成交價
        都能評估其他站在架的標的。
        """
        out: list[tuple[str, str]] = []
        for q in self.cfg.watchlist.get("queries", []):
            for source_name in q.get("sources", []):
                src = sources.get(source_name)
                if src is None or not getattr(src, "supports_sold", False):
                    continue
                out.append((source_name, q["keyword"]))

        spec = self.cfg.watchlist.get("comps_queries") or {}
        targets = [
            name for name in (spec.get("sources") or [])
            if getattr(sources.get(name), "supports_sold", False)
        ]
        # 先確定有人要跑才展開：展開會印截斷警告，沒有目標來源時印它只會誤導
        if targets:
            expanded = expand_comps_queries(spec)
            for source_name in targets:
                out.extend((source_name, kw) for kw in expanded)

        seen: set[tuple[str, str]] = set()
        deduped: list[tuple[str, str]] = []
        for pair in out:
            if pair not in seen:
                seen.add(pair)
                deduped.append(pair)
        return deduped

    # ------------------------------------------------------------------
    @property
    def sold_pages(self) -> int:
        """每個已售出查詢翻幾頁。

        與在架掃描的 `fetch.max_pages_per_query`（＝1）**刻意分開**：
        在架掃描每小時跑、翻頁只會重複拿到上一輪看過的東西；行情累積一天
        跑一兩次，而 Yahoo 落札相場的視窗有 180 天，翻頁是在挖存量、
        每一頁都是新資料。兩件事的最佳頁數不同，共用一個參數就會有一邊被犧牲。
        """
        spec = self.cfg.watchlist.get("comps_queries") or {}
        return max(1, int(spec.get("pages", 2)))

    def sold_shard(self, sources, *, force: bool = False) -> SoldShard:
        """這一輪要跑哪一片已售出查詢。

        舊制是「每 every_n_runs 輪跑一次全量」——一口氣 88 查詢 × 4 頁
        ≈ 352 請求，是全 log 唯一與硬 blocked 同輪出現過的批次形態
        （daily-20260810.log:450-477）。改成每輪走 ceil(N/every) 個，
        every 輪走完一整份：對方看到的是穩定小流量，每查詢的更新頻率不變，
        而且一片塞得進一顆 WAF token 的 240s 預算。

        游標的推進者有兩個，不是一個：正常路徑由 `commit_sold_shard`
        推進（呼叫端跑完才知道成敗）；`sold_shard` 自己的強制跳過分支
        （見下方 crash-safety 說明）在同一個游標交出去 N 次都沒等到 commit
        時也會直接寫 `SOLD_CURSOR_KEY`——這不是巧合或疏漏，是刻意的：
        那個分支要處理的正是「呼叫端這次大概率永遠不會回來呼叫
        `commit_sold_shard`」，等它推進就永遠等不到。
        `force=True` 回全量並在 commit 時把游標歸零（人工逃生門）。

        ⚠️ **`force` 目前沒有接到任何 CLI／排程路徑**（`pipeline.py` 與
        `cli.py` 都呼叫不帶參數的 `refresh_comps()`）——這是死碼但不是安全碼：
        它一次回傳全量、不分片，正是這整個改動要拆掉的 352 請求尖峰形狀。
        將來要接 `ygo-sniper comps --force` 之類的手動入口之前，先想清楚
        怎麼分批（例如強制模式也照 shard size 跑但連續跑完整輪），
        不要原樣接一顆按鈕上去。

        ⚠️ **這個方法有一個刻意保留的副作用**：正常路徑上會在 store 寫入
        `SOLD_ATTEMPT_KEY`（見該常數的完整說明）。上一輪審查才剛稱讚這個
        方法把「問要跑哪片」跟「跑完了沒有」拆乾淨、不再像舊制 `claim_sold_run`
        一樣「問一次就燒一次配額」；這裡等於是把那個副作用的一小塊撿回來——
        差別在於這次寫的是「正要試游標 N」，不是「已經吃掉游標 N」。
        絕大多數輪次裡，真正的配額（`SOLD_CURSOR_KEY`）還是只由
        `commit_sold_shard` 推進；只有「同一個游標交出去 N 次都等不到
        commit」這個例外情況，才由這個方法自己出手推進——見上面的
        「游標的推進者有兩個」段落，不要只看這一句就以為排他。
        會做這個取捨，是因為 crash-safety 補丁的天然位置是「風險發生前的
        那一刻」，而這個改動被要求不能碰 `pipeline.py`（無法新增一個
        `begin_sold_shard()` 讓呼叫端額外呼叫一次）——`sold_shard` 是唯一
        剩下、每輪必經的入口，只能在這裡順便記一筆。
        """
        spec = self.cfg.watchlist.get("comps_queries") or {}
        every = int(spec.get("every_n_runs", 1) or 1)
        all_q = self.sold_queries(sources)
        if not all_q:
            return SoldShard([], "", None)
        if force:
            label = (
                f"force：整份全跑 {len(all_q)} 查詢 × {self.sold_pages} 頁"
                "——WAF 風險高，非排程用；游標歸零"
            )
            return SoldShard(all_q, label, 0 if self.store is not None else None)
        if every <= 1 or self.store is None:
            return SoldShard(all_q, "", None)
        try:
            cursor = int(self.store.get_meta(SOLD_CURSOR_KEY) or 0)
        except (TypeError, ValueError):
            cursor = 0
        cursor %= len(all_q)
        size = -(-len(all_q) // every)  # ceil

        attempted_cursor, attempted_count = _parse_sold_attempt(
            self.store.get_meta(SOLD_ATTEMPT_KEY)
        )
        if attempted_cursor == cursor and attempted_count >= SOLD_ATTEMPT_LIMIT:
            # 同一個游標連續交出去 SOLD_ATTEMPT_LIMIT 次，一次 commit 都沒等到
            # ——不是重試慢，是這個游標本身有毛病：可能是被 watchdog（25 分鐘
            # 逾時 SIGKILL 整棵行程樹）殺在半路，也可能是 fetch 完成後、
            # commit 之前的路上崩潰了（例如 pipeline.refresh_comps 裡
            # ingest_sold 丟例外——那段不在per-query 的 try 保護範圍內）。
            # 兩種都是「這片會可靠地讓整輪跑不完」，處置一樣：強制跳過，
            # 讓其餘查詢有機會繼續走，不然會在同一段（實測分片 0 正是
            # Playwright/WAF 那條路）永遠卡死。
            #
            # ⚠️ 邊界情況（目前不會發生，但改動前先看這裡）：len(all_q)==1
            # 時 nxt == cursor，這個分支會每輪都印警告卻推不動游標——現實
            # config 展開後有 88 條，摸不到這個邊界，但如果哪天查詢清單被
            # 砍到只剩 1 條，這裡要跟著補「只有一片時不算卡死」的例外。
            skipped = all_q[cursor : cursor + size]
            nxt = (cursor + len(skipped)) % len(all_q)
            print(
                f"[comps] ⚠️ 游標 {cursor} 連續交出 {attempted_count} 次都沒等到"
                " commit——這片要嘛被殺在半路（watchdog SIGKILL），要嘛是"
                "拿到資料後、commit 之前的路上崩潰（例如 ingest 階段），視為"
                f"卡死，強制跳過這片、推進到 {nxt}。{_sold_observability_warning()}"
            )
            self.store.set_meta(SOLD_CURSOR_KEY, str(nxt))
            cursor = nxt

        shard = all_q[cursor : cursor + size]
        nxt = (cursor + len(shard)) % len(all_q)
        # 記下「正要試這個游標」，不是「已經吃掉這個游標」——見 SOLD_ATTEMPT_KEY
        # 的說明。同一游標延續次數，換了游標（包含剛剛的強制跳過）就重新起算。
        next_count = attempted_count + 1 if attempted_cursor == cursor else 1
        self.store.set_meta(SOLD_ATTEMPT_KEY, f"{cursor}:{next_count}")
        # 用游標位置本身當進度顯示，不用合成的序數：config 改過（every_n_runs
        # 或查詢清單長度變了）之後，「第幾片」這種序數會立刻對不上、誤導操作者，
        # 但「游標從哪到哪／全份多大」永遠是事實。
        label = f"游標 {cursor}→{nxt}／全份 {len(all_q)} 查詢"
        return SoldShard(shard, label, nxt)

    def commit_sold_shard(
        self, shard: SoldShard, *, any_success: bool, blocked: bool = False
    ) -> None:
        """跑完一片之後推進游標，依失敗性質分兩條路（工程原則 2：
        transient 重試、semantic 不重試）：

        - **`blocked=True`**（整片失敗且全是 `BlockedError`）：對方剛拒絕過
          我們，semantic 失敗，重試同一片只是再被拒絕一次——立刻推進，
          不佔 stall 名額。**連續幾輪都被擋**落在獨立的
          `SOLD_BLOCKED_STREAK_KEY` 帳本上（被擋的路每輪都會推進，不能借用
          會被歸零的 `SOLD_STALL_KEY`），達到 `SOLD_BLOCKED_STREAK_LIMIT`
          時訊息升級成「這來源看起來整體失效了」，附上要查什麼。
          **這個帳本只被這裡增、被下面的 `any_success` 分支歸零**——不是只
          印一句「代表整體失效」卻沒人真的數——那正是這句話第一版被抓到的
          問題（宣稱了程式碼沒做的事）。
        - **其餘整片失敗**（transient，例如逾時、連線中斷）：原地重試，
          連續 `SOLD_STALL_LIMIT` 輪仍全失敗才強制推進——避免一個真的壞掉
          的查詢（不是 WAF、是自己解析壞了之類）永遠卡住輪替。
        - `any_success=True`：至少有一條查過，游標照常前進、stall 與
          blocked streak 都歸零——只要有一條成功，代表來源沒有整體掛掉。

        **無論走哪條分支都會清掉 `SOLD_ATTEMPT_KEY`**——不只在真的推進游標
        的分支清，三條分支都清。這個方法會被叫到，本身就證明這一輪的行程
        撐過去了（沒被 watchdog SIGKILL），所以「正在嘗試、還沒等到 commit」
        這件事對這個游標已經不成立，該清。故意不把它收窄成「只在寫游標的
        分支清」：如果只在寫游標時清，「transient 失敗但 stall 還沒到頂、
        游標原地不動」那條分支就會放著舊的嘗試次數不管，讓下一輪
        `sold_shard` 誤把「乾淨地失敗過幾次」跟「被殺過幾次」加在一起計數
        ——那正是把兩種不同性質的失敗混進同一把尺（工程原則 1 的同型陷阱）。
        """
        if shard.next_cursor is None or self.store is None:
            return
        self.store.set_meta(SOLD_ATTEMPT_KEY, "")
        if any_success:
            self.store.set_meta(SOLD_CURSOR_KEY, str(shard.next_cursor))
            self.store.set_meta(SOLD_STALL_KEY, "0")
            self.store.set_meta(SOLD_BLOCKED_STREAK_KEY, "0")
            return
        if blocked:
            try:
                streak = int(self.store.get_meta(SOLD_BLOCKED_STREAK_KEY) or 0) + 1
            except (TypeError, ValueError):
                streak = 1
            self.store.set_meta(SOLD_BLOCKED_STREAK_KEY, str(streak))
            if streak >= SOLD_BLOCKED_STREAK_LIMIT:
                print(
                    f"[comps] ⚠️ 連續 {streak} 輪整片被擋——不是單輪運氣不好，"
                    "這個來源看起來整體失效了（WAF 永久擋 IP、token 拿不到、"
                    "或帳號被鎖）。跑 `ygo-sniper health` 看來源健康度、"
                    f"手動開一次該來源的搜尋頁確認。{_sold_observability_warning()}"
                )
            else:
                print(
                    f"[comps] 整片被擋（semantic，非 transient，連續第 {streak} 輪）"
                    f"——不重試同一片，直接推進游標。{_sold_observability_warning()}"
                )
            self.store.set_meta(SOLD_CURSOR_KEY, str(shard.next_cursor))
            self.store.set_meta(SOLD_STALL_KEY, "0")
            return
        try:
            stall = int(self.store.get_meta(SOLD_STALL_KEY) or 0) + 1
        except (TypeError, ValueError):
            stall = 1
        if stall >= SOLD_STALL_LIMIT:
            print(
                f"[comps] ⚠️ 同一分片連續 {stall} 輪整片失敗，強制推進游標"
                f"（跳過這片。{_sold_observability_warning()}）"
            )
            self.store.set_meta(SOLD_CURSOR_KEY, str(shard.next_cursor))
            self.store.set_meta(SOLD_STALL_KEY, "0")
        else:
            self.store.set_meta(SOLD_STALL_KEY, str(stall))
