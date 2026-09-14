"""行情回補：對已知卡名挖各平台的已售出檔案，把有真實成交日的成交填進 comps。

為什麼要有它（2026-09-14）：comps 是我們對市場檔案的**記憶體**——Yahoo 落札相場只保留
約 180 天，Mercari 的已售出頁根本沒有日期。漂移指數與估價都吃 comps，而日常掃描只對
屬性空間抽樣（`sold_shard`），單張卡的歷史要靠針對性查詢才會進來。這裡對「卡名主檔
比對得到的每一張卡」各挖一次（首次回補），之後靠節流帳輪替保鮮。

三條鐵律：
1. 入庫只走 `CompsEngine.ingest_sold`（同一組過濾、同一個去重，工程原則 1）。
2. **BLOCKED 立刻停整輪**：升級封鎖會讓 daily 掃描一起瞎掉（第五節）。
3. 「至少一個來源完成觀測」才落節流帳；全滅不落帳，下輪重試（讀不到 ≠ 沒有）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: 來源 → 頁數。yahoo_closed 3 頁≈300 筆，對單張卡通常已到檔案盡頭（約 180 天）；
#: paypay 1 頁只有 5 筆、只有近幾天，多翻沒有意義。
DEFAULT_PAGES: dict[str, int] = {"yahoo_closed": 3, "paypay_direct": 1}


@dataclass(slots=True)
class BackfillReport:
    cards_total: int = 0
    cards_done: int = 0
    cards_skipped_cooldown: int = 0
    found: int = 0
    kept: int = 0
    blocked: bool = False
    dry_run: bool = False
    problems: list[str] = field(default_factory=list)
    per_card: list[tuple[str, int, int]] = field(default_factory=list)

    def summary(self) -> str:
        head = (
            f"回補 {self.cards_done}/{self.cards_total} 張（冷卻跳過 {self.cards_skipped_cooldown}）"
            f"｜找到 {self.found} 筆、入庫 {self.kept} 筆"
        )
        if self.dry_run:
            head += "｜dry-run（未打外網）"
        if self.blocked:
            head += "｜⚠️ 被擋，已中止"
        if self.problems:
            head += f"｜問題 {len(self.problems)} 條"
        return head


def known_card_names(store: Any, index: Any) -> list[str]:
    """comps 與 signals 的標題經卡名主檔比對出的 in_era 卡名，去重、排序（決定性）。"""
    names: set[str] = set()
    titles = [r.get("title") or "" for r in store.comps_by(limit=20000)]
    titles += [r.get("title") or "" for r in store.all_signal_titles()]
    for t in titles:
        m = index.match(t)
        if m and m.in_era and m.name_ja:
            names.add(m.name_ja)
    return sorted(names)


def run_backfill(
    *,
    store: Any,
    sources: Mapping[str, Any],
    comps: Any,
    names: Iterable[str],
    cooldown_days: float,
    limit: int | None,
    pages: Mapping[str, int] | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> BackfillReport:
    from .refill import _sold_search  # noqa: PLC0415 - 延後匯入避免循環相依

    pages = dict(pages or DEFAULT_PAGES)
    report = BackfillReport(dry_run=dry_run)
    names = list(names)
    report.cards_total = len(names)
    cooling = store.refill_cooldown_active(cooldown_days)
    todo = [n for n in names if n not in cooling]
    report.cards_skipped_cooldown = len(names) - len(todo)
    if limit is not None:
        todo = todo[:limit]
    for name in todo:
        if dry_run:
            report.per_card.append((name, 0, 0))
            continue
        found = kept = 0
        observed = False
        for source_name, n_pages in pages.items():
            src = sources.get(source_name)
            if src is None or not getattr(src, "supports_sold", False):
                continue
            res = _sold_search(src, source_name, name, pages=n_pages)
            health = getattr(res.health, "name", str(res.health))
            if health == "BLOCKED":
                report.blocked = True
                report.problems.append(f"{source_name}／{name}：BLOCKED——中止本輪，避免升級封鎖")
                return report
            if health not in ("OK", "EMPTY_CONFIRMED"):
                report.problems.append(
                    f"{source_name}／{name}：{health}（{getattr(res, 'detail', '') or '沒有細節'}）"
                )
                continue
            observed = True
            ingest = comps.ingest_sold(res.listings)
            found += len(res.listings)
            kept += int(getattr(ingest, "kept", 0))
        if not observed:
            continue
        store.record_refill(name, found=found, kept=kept)
        report.cards_done += 1
        report.found += found
        report.kept += kept
        report.per_card.append((name, found, kept))
        log(f"[backfill] {name}：找到 {found} 筆、入庫 {kept} 筆")
    return report
