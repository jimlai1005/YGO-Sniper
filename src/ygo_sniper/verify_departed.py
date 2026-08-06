"""「這筆還買得到嗎」的商品頁實證分類——清除已離場標的的唯一證據來源。

背景（2026-08-07 事故）：`disappeared_at` 由「觀測窗地平線」推論產生，
但賣家頁輪替（每 4 小時）挖回的標的會在關鍵字掃描（每 30 分）的盲區反覆
「假消失」。使用者一鍵清除 43 筆，實際一大半仍在架上——誤殺率 100%。
本模組是指定的修法：**驗證取代推論**——清除當下真的打開商品頁，
只有拿到實證才准清。

分類表（結構性強制，寫死在 `VerifyResult.clears`，呼叫端不自己記）：

| 頁面結果                                   | verdict      | 清除？ |
|--------------------------------------------|--------------|--------|
| `ItemPage.is_sold == True`                 | SOLD         | ✅     |
| 404（非 transient）／`EbayItemNotFound`    | DELISTED     | ✅     |
| 頁面正常且未售出                           | STILL_LIVE   | ❌ 誤判，要修帳 |
| 被擋／逾時／不支援的站（含 mercari_tw）    | UNVERIFIABLE | ❌ 讀不到 ≠ 賣光 |

UNVERIFIABLE 那一格是整張表的存在理由：被 WAF 擋、連線失敗、或站台
根本沒有可靠的售出標記（Mercari 台灣，`appraise.py:552-555` 明講不判斷），
全部都是「讀不到」，而**讀不到絕不當成已離場**——全域工程原則事故 #4
（讀不到錢 ≠ 錢虧光）的同構。

IO 全部走注入的 `fetch_page`（生產時包 `appraise.fetch_item_page`，
appraise.py:694-739 的四路分流已存在）；本模組自己不出網，
分類邏輯因此可以完整單測。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Literal

from .appraise import ItemPage, Target, UnsupportedUrlError, parse_target
from .sources.base import BlockedError, FetchError
from .sources.ebay import EbayItemNotFound

Verdict = Literal["SOLD", "DELISTED", "STILL_LIVE", "UNVERIFIABLE"]

#: 允許清除的 verdict。這份清單只有這裡一份——store 層看 `VerifyResult.clears`，
#: 不自己另組一份（判準散成兩份，遲早漂移成兩種答案）。
CLEARABLE_VERDICTS: tuple[Verdict, ...] = ("SOLD", "DELISTED")


@dataclass(frozen=True)
class VerifyResult:
    """單筆驗證結果。`clears` 把「這個 verdict 准不准清」跟著結果一起旅行，
    呼叫端拿到什麼就照做什麼，沒有第二個地方需要記得分類表。"""

    key: str
    verdict: Verdict
    detail: str  # 給人看的一句話（含錯誤分類）

    #: verdict → 准不准清。也是建構時的合法值檢查表。
    _CLEARS: ClassVar[dict[str, bool]] = {
        "SOLD": True,
        "DELISTED": True,
        "STILL_LIVE": False,
        "UNVERIFIABLE": False,
    }

    def __post_init__(self) -> None:
        # 打錯字要在建構當下爆，不是批次清除跑到一半才 KeyError。
        if self.verdict not in self._CLEARS:
            raise ValueError(
                f"未知的 verdict {self.verdict!r}；"
                f"合法值：{sorted(self._CLEARS)}"
            )

    @property
    def clears(self) -> bool:
        return self._CLEARS[self.verdict]


def verify_listing(
    key: str, url: str, *, fetch_page: Callable[[Target], ItemPage]
) -> VerifyResult:
    """開一次商品頁，把結果分類成 verdict。

    `fetch_page` 拋出的例外在這裡分類——分類邏輯是本模組的全部價值。
    分類表**之外**的例外原樣往上拋：那是 bug，不准吞成 UNVERIFIABLE
    （靜默失敗是這個專案的頭號敵人，CLAUDE.md 第五節）。
    """
    try:
        target = parse_target(url)
    except UnsupportedUrlError:
        return VerifyResult(
            key, "UNVERIFIABLE", f"不支援的網址，無法開頁驗證：{url}"
        )

    try:
        page = fetch_page(target)
    except EbayItemNotFound as exc:
        return VerifyResult(key, "DELISTED", f"eBay 回報商品不存在：{exc}")
    except BlockedError as exc:
        return VerifyResult(key, "UNVERIFIABLE", f"被擋（WAF／驗證頁）：{exc}")
    except FetchError as exc:
        if exc.status == 404 and not exc.transient:
            return VerifyResult(key, "DELISTED", "連結已失效（HTTP 404）")
        kind = "暫時性失敗" if exc.transient else "抓取失敗"
        return VerifyResult(
            key, "UNVERIFIABLE", f"{kind}（status={exc.status}）：{exc}"
        )

    if target.fetch_mode == "mercari_tw":
        # 頁面開得起來也回答不了「還買得到嗎」：Mercari 台灣的「已售出」字樣
        # 全部來自 i18n 字典（每一頁都有），`is_sold` 恆 False（appraise.py:552-555
        # 刻意不猜）。當成 STILL_LIVE 會把真的賣掉的標的判成誤殺、還去修
        # `disappeared_at` 的帳——所以成功開頁只有 404 那條路是實證（上面已分類），
        # 其餘一律 UNVERIFIABLE。
        return VerifyResult(
            key,
            "UNVERIFIABLE",
            "Mercari 台灣頁沒有可靠的售出標記，本工具不判斷（請自己看頁面）",
        )
    if page.is_sold:
        return VerifyResult(
            key, "SOLD", f"頁面標示已售出／已結束（status={page.status}）"
        )
    return VerifyResult(
        key, "STILL_LIVE", "頁面正常且未售出——這筆是離場推論的誤判"
    )
