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
from typing import Any, ClassVar, Literal

from .appraise import ItemPage, Target, UnsupportedUrlError, parse_target
from .config import Config
from .sources.base import BlockedError, CachedFetcher, FetchError
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


# ---------------------------------------------------------------------------
# 生產接線：把 verify_listing 接上真實抓取管線（web 端點與 CLI 共用這一份）
# ---------------------------------------------------------------------------
class _NoCacheGetter:
    """把 `use_cache=False` 釘死在 `.get` 上的轉接層。

    `CachedFetcher`／`WafSession` 的 12 小時快取會回舊頁——上次鑑價抓過的
    商品頁躺在快取裡，照它判「還在架上」等於拿過去證明現在。驗證要看的是
    **此刻**的頁面，所以連呼叫端明說 `use_cache=True` 都要被否決；
    `fetch_item_page` 內部不帶 use_cache 呼叫 `.get`，靠這層攔下預設值。
    節流、重試、失敗分類全部留在被包的物件裡，這裡只動快取旗標。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def get(self, url: str, *, use_cache: bool = True, **kw: Any) -> str:
        del use_cache  # 刻意忽略：驗證路徑沒有「可以用快取」這個選項
        return self._inner.get(url, use_cache=False, **kw)

    def close(self) -> None:
        self._inner.close()


class PageVerifier:
    """可重用的 `(key, url) -> VerifyResult` 接線，資源懶初始化、批次共用。

    - `CachedFetcher`：第一筆才建，整批共用（連線池＋節流狀態一份）。
    - `WafSession`：**只在遇到 buyee_waf 標的時**才建（照 `cli.resolve_grades`
      的開法——一顆 token TTL 只有約 5 分鐘，先開好再慢慢驗等於開一顆就過期；
      TTL 內的重取由 WafSession 自己管理），之後整批重用。
    - `EbaySource`：同理共用一顆 OAuth token。
    - 三者都包（或本身就是）no-cache 路徑：驗證看現在，不看 12 小時前。

    `fetch_page` 可注入（測試用假件），此時不建任何真實資源——單元測試
    只驗「組出來的 callable 會把例外分類對」，不出網（工程原則 4）。
    """

    def __init__(
        self,
        cfg: Config,
        *,
        fetch_page: Callable[[Target], ItemPage] | None = None,
    ) -> None:
        self.cfg = cfg
        self._injected = fetch_page
        self._fetcher: _NoCacheGetter | None = None
        self._waf: _NoCacheGetter | None = None
        self._ebay: Any = None

    def __call__(self, key: str, url: str) -> VerifyResult:
        return verify_listing(
            key, url, fetch_page=self._injected or self._fetch_page
        )

    # -- 生產抓取路徑 ---------------------------------------------------
    def _fetch_page(self, target: Target) -> ItemPage:
        from .appraise import fetch_item_page

        if self._fetcher is None:
            self._fetcher = _NoCacheGetter(CachedFetcher(self.cfg))
        waf = None
        if target.fetch_mode == "buyee_waf":
            if self._waf is None:
                from .sources.waf import WafSession

                self._waf = _NoCacheGetter(WafSession(self.cfg))
            waf = self._waf
        ebay = None
        if target.fetch_mode == "ebay_api":
            if self._ebay is None:
                from .sources.ebay import EbaySource

                self._ebay = EbaySource(self.cfg)
            ebay = self._ebay
        # fetcher／waf 是我們的，fetch_item_page 不會關（owns_* 邏輯）
        return fetch_item_page(self.cfg, target, fetcher=self._fetcher, waf=waf, ebay=ebay)

    def close(self) -> None:
        if self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None
        if self._waf is not None:
            self._waf.close()
            self._waf = None
        self._ebay = None  # EbaySource 沒有連線資源，丟掉即可

    def __enter__(self) -> PageVerifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def build_page_verifier(
    cfg: Config, *, fetch_page: Callable[[Target], ItemPage] | None = None
) -> PageVerifier:
    """組出接上真實抓取管線的 verifier。web 端點與 CLI 都用這一份接線——
    各接各的話，哪天 buyee 要換抓取路徑只會有一邊被修好，而另一邊的失敗
    是靜默的（CLAUDE.md 第五節）。用完要 `close()`（或當 context manager 用）。"""
    return PageVerifier(cfg, fetch_page=fetch_page)
