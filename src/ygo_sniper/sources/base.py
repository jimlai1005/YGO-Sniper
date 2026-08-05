"""資料源介面。

新增一個站台只要實作 `search()`，pipeline 完全不用動。
Buyee 哪天改版壞掉，換掉 buyee.py 一個檔案就好。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Protocol

import httpx

from ..config import Config
from ..domain import Listing, Site


class Source(Protocol):
    site: Site

    def search(self, keyword: str, *, max_price: float | None = None,
               sold: bool = False, pages: int = 1) -> list[Listing]:
        ...


class FetchError(RuntimeError):
    """抓取失敗。

    `transient=True` 代表值得重試（連線中斷、逾時、5xx、429）；
    `transient=False` 是語意失敗（404、被擋），重試只是白費力氣還更容易被封。
    """

    def __init__(
        self, message: str, *, url: str, status: int | None = None, transient: bool = False
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.transient = transient


class BlockedError(FetchError):
    """被 bot 防護擋下（WAF challenge、或 2xx 但 body 空白）。

    這個要跟「真的沒有結果」嚴格分開。混在一起的話，
    爬蟲被擋整整三週你只會看到「今天沒好貨」，然後以為市場很冷。
    """

    def __init__(self, message: str, *, url: str, status: int | None = None) -> None:
        super().__init__(message, url=url, status=status, transient=False)


class CachedFetcher:
    """帶快取與節流的 HTTP client。

    快取不只是為了快 —— 開發階段你會反覆調 parser，
    沒有快取就是反覆去敲人家伺服器，這不禮貌也很容易被擋。

    所有外呼都必須經過這裡，失敗一律以 FetchError 分類拋出，
    不允許回傳空字串假裝成功（見全域工程原則 2、3、5）。
    """

    #: 2xx 但短於這個長度的 body 一律當成挑戰頁／被擋，不當成有效結果。
    #:
    #: ⚠️ 這個門檻是為**整頁 HTML**訂的（正常搜尋頁 100KB 起跳，挑戰頁只有幾百
    #: bytes，中間有兩個數量級的空隙）。JSON API 沒有這個空隙：露天的
    #: 「查無結果」是合法回應但只有 49 bytes（`{"TotalRows":0,"Rows":[],…}`），
    #: 用 512 判它就會把「確認沒貨」誤診成「被擋」——正是 health.py 開頭那段
    #: 事故的反面（把市場現象誤報成工具壞了）。所以呼叫端可以用 `get(min_bytes=…)`
    #: 逐次覆寫，但**必須是呼叫端明講**：預設值永遠是保守的那個。
    MIN_BODY_BYTES = 512

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.delay = float(cfg.fetch["delay_seconds"])
        self.ttl = float(cfg.fetch["cache_ttl_hours"]) * 3600
        self.cache_dir: Path = cfg.cache_dir
        # 下限 1：設成 0 的話重試迴圈一次都不跑，會走到「沒有任何錯誤可拋」的死角
        self.max_attempts = max(1, int(cfg.fetch.get("max_attempts", 3)))
        self.backoff_seconds = float(cfg.fetch.get("backoff_seconds", 2.0))
        self._last_call = 0.0
        self._client = httpx.Client(
            timeout=float(cfg.fetch["timeout_seconds"]),
            follow_redirects=True,
            headers={
                "User-Agent": cfg.fetch["user_agent"],
                "Accept-Language": "ja,en;q=0.8,zh-TW;q=0.6",
            },
        )

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode()).hexdigest()[:20]
        return self.cache_dir / f"{h}.html"

    def _write_cache(self, cp: Path, body: str) -> None:
        """先寫暫存檔再 rename —— rename 在同一個檔案系統上是原子的。

        直接 write_text 的話，寫到一半被 Ctrl-C／當機打斷會留下一個截斷的檔案，
        而快取命中路徑不做內容檢查，那個殘檔會被當成有效頁面回傳整整 12 小時。
        """
        tmp = cp.with_suffix(cp.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(cp)

    def _throttle(self) -> None:
        """每次請求之間至少隔 delay 秒。"""
        elapsed = time.time() - self._last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_call = time.time()

    def _check(
        self,
        resp: httpx.Response,
        url: str,
        *,
        skip_status: bool = False,
        min_bytes: int | None = None,
    ) -> None:
        """在呼叫點強制分類失敗，回不了頭地決定「這個要不要重試」。

        skip_status=True 只跳過「狀態碼」那兩條判斷；WAF header 與 body 長度
        照常檢查——Yahoo 的 404 無結果頁是完整頁面，但 404 的挑戰頁／空頁
        仍然必須被攔下來，不能因為狀態碼被豁免就整包放行。

        min_bytes 覆寫 `MIN_BODY_BYTES`（JSON API 用，見該常數的註記）。
        """
        status = resp.status_code
        if not skip_status:
            if status == 429 or status >= 500:
                raise FetchError(f"HTTP {status}", url=url, status=status, transient=True)
            if status >= 400:
                raise FetchError(f"HTTP {status}", url=url, status=status, transient=False)

        # 被擋的訊號 1：WAF 明講。AWS WAF 的挑戰回應是 202 + 這個 header，
        # status code 本身是 2xx，raise_for_status() 抓不到。
        waf = resp.headers.get("x-amzn-waf-action")
        if waf:
            raise BlockedError(
                f"AWS WAF 回應 {waf}：這個 URL 需要瀏覽器解 JS 挑戰才拿得到內容",
                url=url,
                status=status,
            )
        # 被擋的訊號 2：2xx 但根本沒有內容
        floor = self.MIN_BODY_BYTES if min_bytes is None else min_bytes
        body = resp.text
        if len(body.strip()) < floor:
            raise BlockedError(
                f"HTTP {status} 但 body 只有 {len(body)} bytes，不是正常頁面",
                url=url,
                status=status,
            )

    def get(
        self,
        url: str,
        *,
        use_cache: bool = True,
        allow_statuses: tuple[int, ...] = (),
        min_bytes: int | None = None,
    ) -> str:
        """抓一個 URL。失敗一律拋 FetchError —— 絕不回傳空字串。

        GET 是冪等的，所以 transient 失敗可以安全重試；
        語意失敗（4xx、被擋）立刻拋，重試只會更快被封 IP。

        allow_statuses：名單內的狀態碼不當失敗（Yahoo 查無結果回 404＋完整頁面，
        對呼叫端那是「查過了、沒貨」的有效答案）。但這種回應**不寫快取**——
        「當下沒有結果」不是穩定內容，存 12 小時會把後來上架的貨蓋掉。
        WAF header 與 body 長度檢查照常執行，狀態碼豁免不等於內容豁免。

        min_bytes：覆寫「body 太短 = 被擋」的門檻（見 `MIN_BODY_BYTES`）。
        快取命中路徑用**同一個**值，不然同一個 URL 會因為走哪條路而有兩套判準。
        """
        floor = self.MIN_BODY_BYTES if min_bytes is None else min_bytes
        cp = self._cache_path(url)
        if use_cache and cp.exists() and (time.time() - cp.stat().st_mtime) < self.ttl:
            cached = cp.read_text(encoding="utf-8", errors="ignore")
            # 快取內容也要過同一道門檻。舊版本存下來的挑戰頁、或任何殘檔，
            # 不能因為「它在快取裡」就免檢查（比較基準必須同源同標準）。
            if len(cached.strip()) >= floor:
                return cached

        last_error: FetchError | None = None
        for attempt in range(self.max_attempts):
            self._throttle()
            try:
                resp = self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = FetchError(
                    f"連線失敗: {type(exc).__name__}: {exc}", url=url, transient=True
                )
            else:
                allowed = resp.status_code in allow_statuses
                try:
                    self._check(resp, url, skip_status=allowed, min_bytes=min_bytes)
                except FetchError as exc:
                    if not exc.transient:
                        raise
                    last_error = exc
                else:
                    # 只有確認過的成功回應才進快取，避免把挑戰頁存起來毒害後續 12 小時；
                    # allow_statuses 命中的（如 404 無結果頁）也不進——那是暫態，不是內容
                    if not allowed:
                        self._write_cache(cp, resp.text)
                    return resp.text

            if attempt < self.max_attempts - 1:
                time.sleep(self.backoff_seconds * (2**attempt))

        if last_error is None:  # 理論上到不了，但絕不容許靜默回傳空值
            raise FetchError("重試迴圈結束但沒有任何回應", url=url, transient=True)
        raise last_error

    def set_cookie(self, name: str, value: str) -> None:
        """設定 cookie（WafSession 餵 `aws-waf-token` 用）。

        只進記憶體中的 client，不落地——token 壽命約 4 分鐘，
        持久化只會多一個「讀到過期 token」的失敗模式。
        """
        self._client.cookies.set(name, value)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CachedFetcher:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
