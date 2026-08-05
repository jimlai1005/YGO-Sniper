"""告警引擎：0 筆判定的聚合、去重、冷卻與復原通知。

排程工具最致命的失敗模式是「安靜地壞掉」：爬蟲被改版或被 WAF 擋三週，
使用者只看到三週的「今天沒好貨」。sources/health.py 負責判定單次搜尋
「為什麼是 0 筆」，這裡負責把判定變成不洗版的聲音：
指紋去重（`{source}:{kind}`）、冷卻序列、FETCH_FAILED 連續門檻、
復原通知、單次則數上限（PLAN Q5）。

發送責任刻意留在呼叫端（pipeline.send_alerts）：`evaluate()` 只落
「觀測帳」（occurrences / first_seen / last_seen）並回傳到期訊息；
「通知帳」（notify_count / last_notified_at——冷卻的依據）只在真的
送出成功後由 `mark_sent()` 落帳。這樣 scan 指令預覽告警不會燒掉
daily 的冷卻窗口，Telegram 送失敗也不會被當成已通知而靜默吞掉
（工程原則 3：安全關鍵動作失敗不能吞）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from .config import Config
from .sources.health import ParseHealth, SearchResult
from .store import Store

#: 「誰比較嚴重」的全域排序——pipeline 的摘要聚合與這裡的告警聚合
#: 共用同一份定義（工程原則 1：被比較的值必須同源）。
HEALTH_SEVERITY = {
    ParseHealth.OK: 0,
    ParseHealth.EMPTY_CONFIRMED: 1,
    ParseHealth.FETCH_FAILED: 2,
    ParseHealth.BLOCKED: 3,
    ParseHealth.PARSER_BROKEN: 4,
}

#: 給人看的中文標籤（告警訊息、summary 來源行、health 指令共用）。
HEALTH_LABEL = {
    "ok": "正常",
    "empty": "無結果",
    "fetch_failed": "連線失敗",
    "blocked": "被擋",
    "parser_broken": "解析壞了",
}

#: 冷卻序列（小時）：第 1 次立發，之後 3 天、7 天、7 天…（索引取 notify_count，
#: 超出取尾）。前密後疏：壞掉當下要立刻知道，長期沒修則週報式提醒即可。
COOLDOWN_HOURS = [0, 72, 168, 168]

_ALERTABLE = {ParseHealth.PARSER_BROKEN, ParseHealth.BLOCKED, ParseHealth.FETCH_FAILED}

#: 可直接照抄的排錯指令（依告警類型）。
_DEBUG_CMD = {
    "parser_broken": '.venv/bin/ygo-sniper probe "{url}"',
    "blocked": '.venv/bin/ygo-sniper probe "{url}"',
    "fetch_failed": 'curl -sS -o /dev/null -w "%{{http_code}}\\n" "{url}"',
}


class Alert(str):
    """告警訊息：本體就是要發送的文字（str 子類，可直接印、直接送）。

    額外欄位是「送達之後要落帳什麼」：fingerprints 記 notified、
    recovery_source 非空代表這是復原通知（送達後清掉該 source 的列）。
    source/label/occurrences 供併列統計時引用。
    """

    fingerprints: tuple[str, ...]
    recovery_source: str
    source: str
    label: str
    occurrences: int

    def __new__(
        cls,
        text: str,
        *,
        fingerprints: tuple[str, ...] = (),
        recovery_source: str = "",
        source: str = "",
        label: str = "",
        occurrences: int = 0,
    ) -> Alert:
        obj = super().__new__(cls, text)
        obj.fingerprints = tuple(fingerprints)
        obj.recovery_source = recovery_source
        obj.source = source
        obj.label = label
        obj.occurrences = occurrences
        return obj


class AlertEngine:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    # ------------------------------------------------------------------
    def evaluate(
        self, results: list[SearchResult], now: datetime | None = None
    ) -> list[Alert]:
        """吃整輪 scan 的 SearchResult，回傳「現在該發」的訊息文字。

        同一 source 多條 query 先聚合成最嚴重的那次再判定；
        超過 notify.max_alerts_per_run 的部分併成一則統計。
        會更新觀測帳（occurrences 等），但**不**記 notified——
        呼叫端送出成功後要再呼叫 mark_sent()。
        """
        now = now or datetime.now(UTC)
        due: list[Alert] = []
        recoveries: list[Alert] = []

        for source, rep in self._aggregate(results).items():
            if rep.health in _ALERTABLE:
                alert = self._observe_failure(source, rep, now)
                if alert is not None:
                    due.append(alert)
            else:
                recovery = self._observe_ok(source, now)
                if recovery is not None:
                    recoveries.append(recovery)

        max_n = int(self.cfg.notify.get("max_alerts_per_run", 3))
        if len(due) > max_n:
            due = due[: max_n - 1] + [self._combined(due[max_n - 1 :])]
        return due + recoveries  # 復原是好消息且罕見，不佔告警額度

    # ------------------------------------------------------------------
    def mark_sent(self, alerts: list[Alert], now: datetime | None = None) -> None:
        """送出成功後落「通知帳」：記 notified、清復原來源的列。"""
        now = now or datetime.now(UTC)
        for alert in alerts:
            for fp in alert.fingerprints:
                row = self.store.get_alert(fp)
                if row is None:  # 理論上不會發生；發生了也不該炸掉發送流程
                    continue
                self.store.upsert_alert(
                    fp,
                    source=row["source"],
                    kind=row["kind"],
                    first_seen=row["first_seen"],
                    last_seen=row["last_seen"],
                    occurrences=row["occurrences"],
                    last_notified_at=now.isoformat(),
                    notify_count=row["notify_count"] + 1,
                    detail=row["detail"],
                )
            if alert.recovery_source:
                for row in self.store.list_alerts():
                    if row["source"] == alert.recovery_source:
                        self.store.clear_alert(row["fingerprint"])

    # ------------------------------------------------------------------
    @staticmethod
    def _aggregate(results: list[SearchResult]) -> dict[str, SearchResult]:
        """每個 source 取最嚴重的那次當代表——被擋一次比其他 query 正常嚴重。"""
        best: dict[str, SearchResult] = {}
        for res in results:
            cur = best.get(res.source)
            if cur is None or HEALTH_SEVERITY[res.health] > HEALTH_SEVERITY[cur.health]:
                best[res.source] = res
        return best

    def _observe_failure(
        self, source: str, rep: SearchResult, now: datetime
    ) -> Alert | None:
        """落觀測帳，依 FETCH_FAILED 連續門檻與冷卻決定要不要發。"""
        kind = rep.health.value
        fingerprint = f"{source}:{kind}"
        row = self.store.get_alert(fingerprint)
        occurrences = (row["occurrences"] if row else 0) + 1
        first_seen = row["first_seen"] if row else now.isoformat()
        notify_count = row["notify_count"] if row else 0
        last_notified_at = row["last_notified_at"] if row else None

        self.store.upsert_alert(
            fingerprint,
            source=source,
            kind=kind,
            first_seen=first_seen,
            last_seen=now.isoformat(),
            occurrences=occurrences,
            last_notified_at=last_notified_at,
            notify_count=notify_count,
            detail=rep.detail,
        )
        # 同 source 其他 kind 的「從未通知過」觀測列已不是現況，清掉——
        # 這也讓 FETCH_FAILED 的「連續 2 次」是真的連續（中間換了病就重數）。
        for other in self.store.list_alerts():
            if (
                other["source"] == source
                and other["kind"] != kind
                and other["notify_count"] == 0
            ):
                self.store.clear_alert(other["fingerprint"])

        # transient 斷線單次不吵：連續 2 次 scan 都失敗才算病（PLAN Q5）
        if rep.health is ParseHealth.FETCH_FAILED and occurrences < 2:
            return None

        cooldown_h = COOLDOWN_HOURS[min(notify_count, len(COOLDOWN_HOURS) - 1)]
        if last_notified_at is not None:
            since_h = (now - datetime.fromisoformat(last_notified_at)).total_seconds() / 3600
            if since_h < cooldown_h:
                return None
        return self._format_failure(fingerprint, source, rep, occurrences, first_seen)

    def _observe_ok(self, source: str, now: datetime) -> Alert | None:
        """這次 OK：有通知過的舊帳 → 發復原；只有沒吵過的觀測 → 靜默歸零。"""
        rows = [r for r in self.store.list_alerts() if r["source"] == source]
        if not rows:
            return None
        if not any(r["notify_count"] > 0 for r in rows):
            for r in rows:  # 從未驚動使用者的觀測（如單次斷線），無聲清掉即可
                self.store.clear_alert(r["fingerprint"])
            return None
        first = min(datetime.fromisoformat(r["first_seen"]) for r in rows)
        hours = (now - first).total_seconds() / 3600
        duration = f"{hours / 24:.0f} 天" if hours >= 24 else f"{hours:.0f} 小時"
        return Alert(
            f"✅ {source} 已恢復（壞了 {duration}）",
            recovery_source=source,
            source=source,
        )

    # ------------------------------------------------------------------
    def _format_failure(
        self,
        fingerprint: str,
        source: str,
        rep: SearchResult,
        occurrences: int,
        first_seen: str,
    ) -> Alert:
        kind = rep.health.value
        label = HEALTH_LABEL[kind]
        lines = [f"🚨 來源告警：{source} {label}（{kind}）"]
        if rep.query:
            lines.append(f"query：{rep.query}")
        if rep.url:
            lines.append(f"URL：{rep.url}")
        if rep.detail:
            lines.append(f"詳情：{rep.detail}")
        if rep.url:
            lines.append("排錯：" + _DEBUG_CMD[kind].format(url=rep.url))
        else:
            lines.append("排錯：.venv/bin/ygo-sniper health 看現況，再 scan 重跑觀察")
        lines.append(f"第 {occurrences} 次發生（首見 {first_seen[:10]}）")
        return Alert(
            "\n".join(lines),
            fingerprints=(fingerprint,),
            source=source,
            label=label,
            occurrences=occurrences,
        )

    def _combined(self, rest: list[Alert]) -> Alert:
        """超出單次上限的告警併成一則統計——告警本身不能變成洗版源。"""
        lines = [f"🚨 另有 {len(rest)} 項來源告警（超出單次上限，併列統計）："]
        lines += [f"- {a.source}：{a.label}（第 {a.occurrences} 次）" for a in rest]
        lines.append("詳情：.venv/bin/ygo-sniper health")
        fps = tuple(fp for a in rest for fp in a.fingerprints)
        return Alert("\n".join(lines), fingerprints=fps)
