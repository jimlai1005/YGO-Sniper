"""賣家供給契合度（Supply Fit）——回答「這賣家值不值得盯」。

**這個分數與 Seller Alpha 是兩件事，永不相加、永不互相 fallback。**
Alpha 回答「他比市場便宜多少」（同儕相對，湊不到就拒答）；
Supply Fit 回答「盯著他，未來會不會產出可買的機會」（跟便宜與否無關）。
把兩者加總會讓「賣很多 PSA9 又固定賣 Vol 系列」的賣家看起來像有 alpha，
那是 CLAUDE.md 第四節（不要拿自己的模型當尺）的變種。

維度不可得時一律 score=None 並從權重分母移除，**不是給 0 分**——
0 分的語意是「這方面很差」，但事實是「我們不知道」。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SupplyDimension:
    """一個維度的量測結果。不可得時 `raw` 與 `score` 都是 None，不是 0。"""

    name: str
    available: bool
    raw: float | None = None
    score: float | None = None      # 0-100 站內百分位，Task 3 才填
    detail: str = ""
    missing: str = ""

    @classmethod
    def of(cls, name: str, *, raw: float, detail: str) -> SupplyDimension:
        if not detail:
            raise ValueError(f"維度 {name} 沒有帶依據字串——裸數字不准輸出")
        return cls(name=name, available=True, raw=raw, detail=detail)

    @classmethod
    def unavailable(cls, name: str, missing: str) -> SupplyDimension:
        if not missing:
            raise ValueError(f"維度 {name} 不可得，但沒說缺什麼")
        return cls(name=name, available=False, missing=missing)
