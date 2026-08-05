"""全語料載入器 —— `CLAUDE.md` 第一節那條驗收協定的執行體。

## 為什麼要有這支模組

第一節的紅線是「改任何過濾／解析規則，必須對全語料跑雙向比對，誤殺數必須是 0」。
在這支模組出現之前，repo 裡**沒有**可以執行那條紅線的東西：每個要驗證的人（人或
agent）都得臨時自己寫 SQL ＋ 掃 cache，於是實務上很容易被跳過，寫出來的兩次結果
也不可比。規則寫了但無法執行，等於沒有規則。

所以這裡只做一件事：**把「全語料的每一個標題」變成一個可重複取得的清單**，
讓判定函式（`parse_grade` / `parse_card`+`is_candidate`）可以在它上面跑兩次。

## 語料的三個桶

1. `data/sniper.db` 的 `signals` / `comps` / `listing_obs` 三張表的 `title`
2. `data/cache/` 的抓取快取 —— **標題的大宗在這裡**（約佔 99%）
3. `data/cards_1998_2004.json` 的**卡名主檔**（卡名／別名／英文名／套組名）

第 3 桶容易被忘記，但它是**誤殺證據最強的那一桶**：`watchlist.yaml` 裡那幾個
「實測後剔除的候選排除詞」，當初的判準就是「這個詞命中了幾個真實卡名」——
`サッカー` 命中 `ブラッド・サッカー` 等 5 個卡名。而市場上**不是每天都有**
那張卡在賣，所以只掃在架／成交標題會漏掉這種誤殺（實測：今天的 31,005 個標題
裡含 `サッカー` 的只有 3 筆，全是球員卡與雜物，一筆真卡都沒有）。
少了卡名主檔，`corpus-diff` 對這一整類誤殺是瞎的。

兩桶的檢查方式不同，所以**分開量、分開報**：

- 標題 → 完整判定（機構／分數／收不收）
- 卡名 → 只看**排除字有沒有命中**（裸卡名沒有年代與機構證據，談「收不收」
  沒有意義）。年代內卡名被排除字命中 = 誤殺的鐵證，不是「待人工確認」。

## 解析規則刻意「跟著生產路徑走」

每一種快取檔用的取標題方式，都對應它在 `sources/` 裡的生產解析路徑
（`__NEXT_DATA__` 的節點路徑直接 import 生產常數，Buyee 直接呼叫
`BuyeeSource._parse_title`）。理由是 `CLAUDE.md` 第六節：測試路徑必須等於
生產路徑——另寫一套「看起來也能抓到標題」的規則，會抓到生產抓不到的東西，
比對結果就不再代表生產行為。

## 「解不出標題」一律報數，絕不靜默跳過（第五節）

語料悄悄縮水的後果特別惡劣：**比對結果會假性乾淨**（被誤殺的那幾筆根本沒進語料，
自然不會出現在 diff 裡），而外顯行為與「規則真的沒問題」一模一樣。所以每個檔案
都要落進一個具名的 kind，且分成兩類：

- `expects_titles=True` 的 kind 抓不到標題 → **解析失敗**，逐檔列出來
- `expects_titles=False` 的 kind（露天的「只回 id」搜尋回應、查無結果頁、
  單一商品頁）→ 本來就沒有清單，計數但不算失敗

⚠️ 快取是會輪替的（`CachedFetcher` 有 TTL），所以語料是**快照**，不是常數。
比對一定要用同一份快照的兩側：`CorpusDiff` 只比對兩側都存在的標題，語料本身的
增減另外列（`only_in_baseline` / `only_in_current`）——不然快取輪替會被讀成
「判定改變了」，正是第三節「比較的兩個值必須同源」的翻版。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .parsers import is_candidate, parse_card, parse_grade
from .sources.buyee import _SITE_SPEC as _BUYEE_SPECS
from .sources.buyee import BuyeeSource
from .sources.paypay import _RESULT_PATH as _PAYPAY_RESULT_PATH
from .sources.yahoo_closed import _LISTING_PATH as _YAHOO_CLOSED_PATH
from .sources.yahoo_closed import _dig

#: DB 裡帶標題的三張表。少一張就是語料悄悄縮水，所以寫成常數而不是散在查詢裡。
CORPUS_TABLES = ("signals", "comps", "listing_obs")

#: 快取檔的 kind → 這個 kind 是否**應該**解出標題。
#: False 的那些是「本來就沒有清單」，不是解析失敗（見模組 docstring）。
_KINDS_WITHOUT_TITLES = {
    "ruten_search_ids": "露天搜尋回應只有 Id，標題在詳情呼叫裡",
    "ruten_empty": "露天 TotalRows=0：確認查無結果",
    "buyee_empty": "Buyee 頁面自述查無結果",
    "search_empty": "totalResultsAvailable=0：確認查無結果",
    "item_page": "單一商品頁，不是清單頁",
}


class CorpusError(RuntimeError):
    """語料載入本身壞掉（例如 DB 不存在）。絕不回空清單假裝成功。"""


@dataclass(frozen=True, slots=True)
class CacheFileResult:
    path: Path
    kind: str
    titles: tuple[str, ...]

    @property
    def expects_titles(self) -> bool:
        return self.kind not in _KINDS_WITHOUT_TITLES

    @property
    def failed(self) -> bool:
        return self.expects_titles and not self.titles


@dataclass(frozen=True, slots=True)
class Corpus:
    """一份語料快照。所有清單都已去重並排序（比對要可重現）。"""

    titles: tuple[str, ...]
    card_names: tuple[str, ...]
    era_card_names: frozenset[str]
    n_db_titles: int
    n_cache_titles: int
    n_files: int
    kind_counts: dict[str, int]
    failures: tuple[CacheFileResult, ...]
    db_path: Path
    cache_dir: Path
    master_path: Path
    taken_at: str

    @property
    def n_failures(self) -> int:
        return len(self.failures)


# ---------------------------------------------------------------------------
# 標題抽取：逐格式，各自對應 sources/ 的生產路徑
# ---------------------------------------------------------------------------
#: `__NEXT_DATA__` 的內容用 regex 切，不走 BeautifulSoup——這幾種頁面單檔
#: 300-700KB 且佔快取六成，html.parser 走完一輪要 40 秒以上。切出來的字串
#: 再 `json.loads`，得到的 payload 與生產的 soup 取法逐位元相同；regex 沒切到
#: 但字串確實出現時會退回 soup（絕不把「我的 regex 沒對上」讀成「頁面沒有它」）。
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


def _titles_from_next_data(text: str) -> tuple[str, tuple[str, ...]] | None:
    """`__NEXT_DATA__` 系（yahoo closedsearch／Yahoo!フリマ）。

    節點路徑直接用生產常數，頁面改版時兩邊一起壞（一起壞看得見，
    各自壞才是災難）。
    """
    if "__NEXT_DATA__" not in text:
        return None
    m = _NEXT_DATA_RE.search(text)
    if m is not None:
        body = m.group(1)
    else:
        tag = BeautifulSoup(text, "html.parser").find("script", id="__NEXT_DATA__")
        body = tag.get_text() if tag is not None else None
    if body is None:
        return "nextdata_unreadable", ()
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return "nextdata_unreadable", ()
    for path, kind in (
        (_YAHOO_CLOSED_PATH, "yahoo_closed"),
        (_PAYPAY_RESULT_PATH, "paypay"),
    ):
        node = _dig(payload, path)
        if not isinstance(node, dict):
            continue
        items = node.get("items")
        if isinstance(items, list) and items:
            # 抓得到清單但一個標題都抽不出來 → 回空，由 kind 判成解析失敗
            return kind, tuple(
                str(it["title"])
                for it in items
                if isinstance(it, dict) and it.get("title")
            )
        # 與生產的 `_judge_health` 同一組欄位：頁面自述 0 件 = 查無結果，
        # 沒有 `items` 鍵 = 這根本不是搜尋頁（商品詳情頁共用同一個 initialState 殼）。
        if node.get("totalResultsAvailable") == 0:
            return "search_empty", ()
        if items is None:
            return "item_page", ()
        return kind, ()
    return "nextdata_unknown", ()


def _titles_from_yahoo_search(soup: BeautifulSoup) -> tuple[str, ...]:
    """Yahoo 拍賣在架搜尋頁：`a.Product__titleLink`（與 yahoo.py 同一個地標）。"""
    return tuple(
        t for a in soup.select("a.Product__titleLink") if (t := a.get_text(strip=True))
    )


def _titles_from_buyee(soup: BeautifulSoup) -> tuple[str, ...]:
    """Buyee 搜尋頁：走 `BuyeeSource` 自己的 item_path ＋ `_parse_title`。

    刻意呼叫生產的靜態方法而不是自己寫一版——標題怎麼取（img alt > a title >
    a 文字）是有事故背景的優先序，複製一份就會漂移。
    """
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        for spec in _BUYEE_SPECS.values():
            m = spec["item_path"].search(a["href"])
            if m is None or m.group(1) in found:
                continue
            title = BuyeeSource._parse_title(a, BuyeeSource._nearest_container(a))
            if title:
                found[m.group(1)] = title
            break
    return tuple(found.values())


def _titles_from_json(text: str) -> tuple[str, tuple[str, ...]]:
    """露天：詳情回應是 `[{ProdId, ProdName, …}]`，搜尋回應只有 `{Rows:[{Id}]}`。"""
    try:
        payload = json.loads(text)
    except ValueError:
        return "json_unreadable", ()
    rows = payload.get("Rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return "json_unknown", ()
    titles = tuple(
        str(r["ProdName"]) for r in rows if isinstance(r, dict) and r.get("ProdName")
    )
    if titles:
        return "ruten_detail", titles
    if not rows:
        return "ruten_empty", ()
    return "ruten_search_ids", ()


def extract_titles(text: str) -> tuple[str, tuple[str, ...]]:
    """一個快取檔 → (kind, titles)。kind 一定有值，絕不回「不知道就算了」。"""
    stripped = text.lstrip()
    if not stripped:
        return "empty_file", ()
    if stripped[0] in "[{":
        return _titles_from_json(stripped)

    nxt = _titles_from_next_data(text)
    if nxt is not None and nxt[0] != "nextdata_unknown":
        return nxt

    soup = BeautifulSoup(text, "html.parser")
    titles = _titles_from_yahoo_search(soup)
    if titles:
        return "yahoo_search", titles
    titles = _titles_from_buyee(soup)
    if titles:
        return "buyee_search", titles

    # 這裡開始是「HTML 但一筆商品都沒有」——四種情況必須分得出來（第五節）
    if "該当する商品" in text or "見つかりません" in text:
        return "buyee_empty", ()
    if "Mercari Japan" in text or "loading-spinner" in text:
        # 商品詳情頁（單筆、無清單）與 JS 未執行的骨架，兩者都拿不到清單標題；
        # 前者是設計如此，後者是抓取路徑的已知極限（第六節的 spinner 事故）。
        return ("item_page" if "Mercari Japan" in text else "unrendered_skeleton"), ()
    return "unknown_page", ()


# ---------------------------------------------------------------------------
# 載入
# ---------------------------------------------------------------------------
def load_db_titles(db_path: Path) -> set[str]:
    if not db_path.exists():
        raise CorpusError(f"語料 DB 不存在：{db_path}")
    out: set[str] = set()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        existing = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'"
        )}
        missing = [t for t in CORPUS_TABLES if t not in existing]
        if missing:
            # 表不見了就是語料少一塊，安靜地少收＝假性乾淨的比對結果
            raise CorpusError(f"語料表不存在：{missing}（{db_path}）")
        for table in CORPUS_TABLES:
            out |= {
                str(r[0])
                for r in con.execute(
                    f"select distinct title from {table} where title is not null"
                )
                if r[0]
            }
    finally:
        con.close()
    return out


def load_cache_files(cache_dir: Path) -> list[CacheFileResult]:
    if not cache_dir.exists():
        raise CorpusError(f"快取目錄不存在：{cache_dir}")
    results: list[CacheFileResult] = []
    for path in sorted(cache_dir.iterdir()):
        if not path.is_file() or path.name == "fx.json":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        kind, titles = extract_titles(text)
        results.append(CacheFileResult(path=path, kind=kind, titles=titles))
    return results


def load_card_names(master_path: Path) -> tuple[set[str], set[str]]:
    """卡名主檔 → (年代內卡名, 年代外卡名＋套組名)。

    走主檔的原始欄位而不是 `CardIndex`：`CardIndex` 存的是**折疊過**的比對鍵
    （`fold()` 會拿掉分隔符與大小寫），拿折疊鍵去跑排除字等於在量另一條路徑上
    的行為（第六節：測試路徑要等於生產路徑——排除字是對**原始標題**比對的）。
    """
    if not master_path.exists():
        raise CorpusError(
            f"卡名主檔不存在：{master_path}——缺了它，排除字誤殺真卡的那一類迴歸就量不到"
        )
    data = json.loads(master_path.read_text(encoding="utf-8"))
    era: set[str] = set()
    for card in data.get("cards") or []:
        for name in [card.get("name_ja"), card.get("name_en"), *(card.get("aliases") or [])]:
            if name:
                era.add(str(name))
    other = {str(n) for n in (data.get("out_of_era") or {}) if n}
    other |= {str(n) for n in (data.get("set_names") or []) if n}
    return era, other - era


def load_corpus(*, db_path: Path, cache_dir: Path, master_path: Path) -> Corpus:
    """DB ＋ 快取 ＋ 卡名主檔 → 一份去重排序的語料快照。"""
    db_titles = load_db_titles(db_path)
    files = load_cache_files(cache_dir)
    era_names, other_names = load_card_names(master_path)

    cache_titles: set[str] = set()
    kind_counts: dict[str, int] = {}
    for f in files:
        kind_counts[f.kind] = kind_counts.get(f.kind, 0) + 1
        cache_titles |= set(f.titles)

    return Corpus(
        titles=tuple(sorted(db_titles | cache_titles)),
        card_names=tuple(sorted(era_names | other_names)),
        era_card_names=frozenset(era_names),
        n_db_titles=len(db_titles),
        n_cache_titles=len(cache_titles),
        n_files=len(files),
        kind_counts=dict(sorted(kind_counts.items(), key=lambda kv: -kv[1])),
        failures=tuple(f for f in files if f.failed),
        db_path=db_path,
        cache_dir=cache_dir,
        master_path=master_path,
        taken_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Verdict:
    """一個標題在**現行規則**下的判定。欄位就是我們承諾不會意外改變的東西。"""

    grader: str
    grade: float | None
    candidate: bool
    reason: str

    def as_row(self) -> list[Any]:
        return [self.grader, self.grade, self.candidate, self.reason]

    @classmethod
    def from_row(cls, row: list[Any]) -> Verdict:
        grader, grade, candidate, reason = row
        return cls(str(grader), grade, bool(candidate), str(reason))


def judge(title: str, watchlist: dict) -> Verdict:
    """判定函式的**最終出口**：機構／分數（`parse_grade`）＋ 收不收（`is_candidate`）。

    兩者都要：只看 `is_candidate` 會漏掉「還是收，但分數讀錯了」的迴歸，
    而分數直接乘進估價與出價上限。
    """
    grader, grade = parse_grade(title)
    ok, why = is_candidate(parse_card(title, watchlist), watchlist)
    return Verdict(grader=grader.value, grade=grade, candidate=ok, reason=why)


def judge_all(titles: tuple[str, ...] | list[str], watchlist: dict) -> dict[str, Verdict]:
    return {t: judge(t, watchlist) for t in titles}


def name_hits(names: tuple[str, ...] | list[str], watchlist: dict) -> dict[str, str]:
    """卡名 → 命中的排除字（沒命中就是空字串）。走生產的 `parse_card`。"""
    return {n: (parse_card(n, watchlist).excluded_by or "") for n in names}


# ---------------------------------------------------------------------------
# 基準快照與比對
# ---------------------------------------------------------------------------
BASELINE_VERSION = 2


def save_baseline(
    path: Path,
    corpus: Corpus,
    verdicts: dict[str, Verdict],
    hits: dict[str, str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": BASELINE_VERSION,
        "taken_at": corpus.taken_at,
        "n_titles": len(corpus.titles),
        "n_db_titles": corpus.n_db_titles,
        "n_cache_titles": corpus.n_cache_titles,
        "n_card_names": len(corpus.card_names),
        "n_files": corpus.n_files,
        "n_failures": corpus.n_failures,
        "verdicts": {t: v.as_row() for t, v in verdicts.items()},
        # 只存有命中的：15,000 個卡名裡命中排除字的是少數，全存等於把
        # 基準檔撐大一倍換 0 資訊（缺鍵 = 沒命中，語意由 diff 那側統一補）
        "name_hits": {n: w for n, w in hits.items() if w},
        "name_universe": sorted(hits),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Baseline:
    verdicts: dict[str, Verdict]
    #: 卡名 → 命中的排除字。**只含有命中的**，取值一律走 `hit_for()`
    name_hits: dict[str, str]
    #: 基準當時掃過的卡名全集。沒掃過的卡名不能被讀成「當時沒命中」
    name_universe: frozenset[str]
    meta: dict[str, Any]

    def hit_for(self, name: str) -> str:
        return self.name_hits.get(name, "")


def load_baseline(path: Path) -> Baseline:
    if not path.exists():
        raise CorpusError(
            f"找不到基準快照：{path}\n"
            "改規則**之前**先跑 `ygo-sniper corpus-diff --save-baseline`。"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != BASELINE_VERSION:
        raise CorpusError(
            f"基準快照版本 {payload.get('version')} 與現行 {BASELINE_VERSION} 不符，"
            "請重新產生（還原規則 → --save-baseline）。"
        )
    skip = {"verdicts", "name_hits", "name_universe"}
    return Baseline(
        verdicts={t: Verdict.from_row(row) for t, row in payload["verdicts"].items()},
        name_hits=dict(payload.get("name_hits") or {}),
        name_universe=frozenset(payload.get("name_universe") or []),
        meta={k: v for k, v in payload.items() if k not in skip},
    )


@dataclass(frozen=True, slots=True)
class Change:
    title: str
    before: Verdict
    after: Verdict


@dataclass(frozen=True, slots=True)
class NameChange:
    """卡名被排除字命中／解除命中。`in_era` = 年代內真卡（誤殺的鐵證）。"""

    name: str
    before: str
    after: str
    in_era: bool


@dataclass(frozen=True, slots=True)
class CorpusDiff:
    """雙向比對結果。

    `newly_blocked` 是**唯一真正重要的那一欄**：誤殺是靜默的（第一節），
    所以它必須逐筆列出來由人判斷，工具不替它宣稱「誤殺 0」。
    """

    n_compared: int
    newly_blocked: tuple[Change, ...] = ()
    newly_allowed: tuple[Change, ...] = ()
    grade_changed: tuple[Change, ...] = ()
    only_in_baseline: tuple[str, ...] = ()
    only_in_current: tuple[str, ...] = ()
    n_names_compared: int = 0
    #: 排除字**新命中**的真實卡名。`in_era=True` 的每一個都是誤殺，沒有例外
    names_newly_excluded: tuple[NameChange, ...] = ()
    names_no_longer_excluded: tuple[NameChange, ...] = ()
    baseline_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def n_changed(self) -> int:
        return (
            len(self.newly_blocked)
            + len(self.newly_allowed)
            + len(self.grade_changed)
            + len(self.names_newly_excluded)
            + len(self.names_no_longer_excluded)
        )

    @property
    def n_era_names_killed(self) -> int:
        return sum(1 for c in self.names_newly_excluded if c.in_era)


def diff_verdicts(
    baseline: Baseline,
    current: dict[str, Verdict],
    *,
    current_name_hits: dict[str, str] | None = None,
    era_names: frozenset[str] = frozenset(),
) -> CorpusDiff:
    """只比對兩側都在的標題／卡名；語料本身的增減分開列，不混進「判定改變」。"""
    names_before, names_after = [], []
    hits = current_name_hits or {}
    shared_names = sorted(baseline.name_universe & set(hits))
    for name in shared_names:
        b, c = baseline.hit_for(name), hits[name]
        if b == c:
            continue
        change = NameChange(name=name, before=b, after=c, in_era=name in era_names)
        (names_after if c else names_before).append(change)
    # 年代內卡名排最前面：它們是誤殺的鐵證，不該被幾百筆年代外的雜訊淹掉
    names_after.sort(key=lambda c: (not c.in_era, c.name))
    names_before.sort(key=lambda c: (not c.in_era, c.name))

    baseline_verdicts = baseline.verdicts
    shared = sorted(set(baseline_verdicts) & set(current))
    blocked: list[Change] = []
    allowed: list[Change] = []
    graded: list[Change] = []
    for title in shared:
        b, c = baseline_verdicts[title], current[title]
        if b == c:
            continue
        if b.candidate and not c.candidate:
            blocked.append(Change(title, b, c))
        elif not b.candidate and c.candidate:
            allowed.append(Change(title, b, c))
        elif (b.grader, b.grade) != (c.grader, c.grade):
            graded.append(Change(title, b, c))
        # 收不收與機構分數都沒變、只有 reason 字串變了 → 不是行為改變
    return CorpusDiff(
        n_compared=len(shared),
        newly_blocked=tuple(blocked),
        newly_allowed=tuple(allowed),
        grade_changed=tuple(graded),
        only_in_baseline=tuple(sorted(set(baseline_verdicts) - set(current))),
        only_in_current=tuple(sorted(set(current) - set(baseline_verdicts))),
        n_names_compared=len(shared_names),
        names_newly_excluded=tuple(names_after),
        names_no_longer_excluded=tuple(names_before),
        baseline_meta=baseline.meta,
    )
