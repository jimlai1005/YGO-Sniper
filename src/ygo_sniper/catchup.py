"""排程空窗補跑模式：漏跑期間「狙擊卡成交回顧」。

能補的只有「它們成交了、成交多少錢」這個事實——期間內上架又結標的商品
已不在架上，任何事後掃描都撈不回來（這是本功能存在的前提，不要試圖
把它做成「重掃過去」）。

同基準條款（CLAUDE.md 第三節）：排程帳是台北 naive ISO，`card_watch_sale.sold_at`
是 UTC aware ISO——先 `window_to_utc` 再比，不比字串。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .clock import schedule_naive_to_utc, utc_to_schedule_str

_TIER_RANK = {"exact": 2, "partial": 1}
_TIER_MARK = {"exact": "🎯", "partial": "👀"}


def window_to_utc(local_from: str, local_to: str) -> tuple[datetime, datetime]:
    """台北 naive ISO → UTC aware。排程帳的基準固定是台北（`clock.SCHEDULE_TZ`），不看系統時區。"""
    return schedule_naive_to_utc(local_from), schedule_naive_to_utc(local_to)


@dataclass
class GapSalesReport:
    graded: list[dict] = field(default_factory=list)   # exact/partial，已依 sale_key 去重、依 sold_at 排序
    near_count: int = 0                                 # near，去重後筆數
    undated_sources: list[str] = field(default_factory=list)


def gap_sales(store, local_from: str, local_to: str) -> GapSalesReport:
    """視窗 (local_from, local_to] 內成交的狙擊目標（只看 active watch）。

    - `sold_at` 為空或無時區的列不可比，一律排除（不補假時間）。
    - 同一筆成交會被同卡的多個 watch 各記一次：以 `sale_key` 去重，取較高 tier。
    """
    lo, hi = window_to_utc(local_from, local_to)
    watches = {w["id"]: w for w in store.list_card_watch(active_only=True)}
    best: dict[str, dict] = {}
    near: set[str] = set()
    for wid, w in watches.items():
        for s in store.list_card_watch_sales(wid):
            raw = s.get("sold_at") or ""
            if not raw:
                continue
            try:
                when = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if when.tzinfo is None:
                continue            # 沒有時區的時刻不可比，寧可不計也不要猜
            if not (lo < when <= hi):
                continue
            tier = s.get("tier")
            if tier == "near":
                near.add(s["sale_key"])
                continue
            if tier not in _TIER_RANK:
                continue
            cur = best.get(s["sale_key"])
            if cur is None or _TIER_RANK[tier] > _TIER_RANK[cur["tier"]]:
                best[s["sale_key"]] = {**s, "watch": w, "sold_at_dt": when}
    graded = sorted(best.values(), key=lambda r: r["sold_at_dt"])
    undated = store.card_watch_sale_undated_sources()
    return GapSalesReport(graded=graded, near_count=len(near), undated_sources=undated)


def _fmt_local(dt: datetime) -> str:
    return utc_to_schedule_str(dt)


def _fmt_window(rec: dict) -> str:
    lo = datetime.fromisoformat(rec["uncovered_from"]).strftime("%m-%d %H:%M")
    hi = datetime.fromisoformat(rec["uncovered_to"]).strftime("%m-%d %H:%M")
    return f"{lo} ～ {hi}"


def _grade_text(w: dict) -> str:
    # grade_label 是登錄時的原樣（"8.5" 不會被截成 "8"）；沒有才退回數值
    label = w.get("grade_label")
    if label:
        return f"{w.get('grader', '')}{label}"
    g = w.get("grade")
    return f"{w.get('grader', '')}{g:g}" if g is not None else str(w.get("grader", ""))


def format_gap_review(rec: dict, rep: GapSalesReport) -> str | None:
    """回顧訊息；期間內沒有任何 exact/partial/near 成交 → None（呼叫端不送）。"""
    if not rep.graded and not rep.near_count:
        return None
    lines = [f"🕳️ 漏跑期間狙擊卡成交回顧（{_fmt_window(rec)}，漏 {rec.get('missed_slots', '?')} 格）"]
    if rep.graded:
        lines.append("期間內已成交、我們來不及看到的狙擊目標：")
        for s in rep.graded:
            w = s["watch"]
            note = "（部分符合）" if s["tier"] == "partial" else ""
            price = f"¥{int(s['price_native']):,}" if s.get("price_native") is not None else "價格不明"
            link = s.get("origin_url") or s.get("url") or ""
            # 訊息格式規格：有「（部分符合）」時分隔符緊貼其後（`…PSA3（部分符合）｜ ¥6,250`），
            # 沒有時分隔符前有空格（`…ARS10 ｜ ¥3,441`）。
            sep = "｜" if note else " ｜"
            lines.append(
                f"• {_TIER_MARK[s['tier']]} {w['name_ja']} {_grade_text(w)}{note}{sep} {price} ｜ "
                f"{_fmt_local(s['sold_at_dt'])} ｜ {s.get('source', '')} ｜ {link}".rstrip(" ｜")
            )
    if rep.near_count:
        lines.append(f"另有 {rep.near_count} 筆同名但未達鑑定條件的成交（near），不逐筆列。")
    if rep.undated_sources:
        lines.append(f"⚠️ {'／'.join(rep.undated_sources)} 的成交沒有時刻，無法判定是否在期間內，未計入。")
    return "\n".join(lines)
