"""「這筆標的還在不在架上」的唯一判定來源。

兩種判準的語意完全不同，本模組刻意讓它們**不合成同一個布林值**：

- `end_time` 已過 → 確定事實（競標結標了就是結標了）
- `listing_obs.disappeared_at` → 推論（掃描看不到它了，但可能只是分頁抖動）

2026-08-06 實測 `data/sniper.db`：曾被判離場的 262 列裡有 148 列後來又出現，
誤判率 56.5%；分來源更懸殊——buyee_paypay 70.4%、ebay 66.4%，而 buyee_yahoo
只有 23.9%。所以推論那一側一律附信心度，永遠不假裝它跟結標一樣可靠。

`window_exit_at`（只是被擠出觀測窗）**不觸發任何過期判定**——它的語意是右設限，
本來就沒有結論（`store.py:114-119`）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Kind = Literal["ended", "gone", "live"]
Confidence = Literal["certain", "medium", "low"]

#: 沒給設定時的保守預設：一律 low。寧可標示得比實際不確定，不要反過來。
DEFAULT_GONE_CONFIDENCE: dict[str, str] = {"_default": "low"}


@dataclass(frozen=True)
class ExpiryStatus:
    kind: Kind
    confidence: Confidence
    detail: str
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_iso(value: Any) -> datetime | None:
    """naive 一律當 UTC。

    庫裡所有時間戳都帶 `+00:00`，naive 只可能來自測試或未來的新來源；
    當成本地時間會產生 8 小時的靜默偏移（台灣 UTC+8），而且不會有人發現。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _humanize(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(int(seconds // 60), 1)} 分鐘"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小時"
    return f"{int(seconds // 86400)} 天"


def _end_time_of(row: dict[str, Any]) -> Any:
    """從 signals 列裡挖出 `payload.listing.end_time`。

    payload 在 store 層是 JSON 字串、在 web 層已經被 `json.loads` 展開過，
    兩種都要吃——判定層被兩邊呼叫，不能只認一種形狀。
    """
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    listing = payload.get("listing")
    if not isinstance(listing, dict):
        return None
    return listing.get("end_time")


def expiry_status(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
    gone_confidence: dict[str, str] | None = None,
) -> ExpiryStatus:
    """判定一列 signals（已 LEFT JOIN listing_obs）的在架狀態。

    `row` 需要的欄位：`payload`、`state`、`site`、`obs_disappeared_at`。
    缺欄位不會炸——缺就當作沒有那個證據。
    """
    now = now or datetime.now(UTC)
    table = gone_confidence or DEFAULT_GONE_CONFIDENCE

    end_time = _parse_iso(_end_time_of(row))
    if end_time is not None and end_time <= now:
        return ExpiryStatus(kind="ended", confidence="certain", detail="已結標")

    disappeared = _parse_iso(row.get("obs_disappeared_at"))
    if disappeared is not None:
        site = str(row.get("site") or "")
        confidence = table.get(site) or table.get("_default", "low")
        ago = _humanize(max((now - disappeared).total_seconds(), 0))
        if row.get("state") == "offer_sent":
            # 已出價的標的消失，很可能代表**你標下了**，正確歸宿是 bought
            # 而不是 expired。文案不能寫「疑似已售出」誤導使用者按下清除。
            detail = f"已離場 · 確認是否標下？（{ago}前）"
        else:
            detail = f"疑似已售出 · 消失 {ago}"
        note = None
        if confidence == "low":
            note = f"{site} 的離場判定復活率偏高，這筆可能還在架上"
        return ExpiryStatus(kind="gone", confidence=confidence, detail=detail, note=note)

    return ExpiryStatus(kind="live", confidence="certain", detail="", note=None)
