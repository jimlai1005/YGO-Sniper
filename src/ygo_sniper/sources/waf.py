"""AWS WAF token 管理（Buyee 系來源專用）。

Buyee 搜尋頁被 AWS WAF 擋（202 + `x-amzn-waf-action`），httpx 直抓拿不到內容；
但 Playwright 解完 JS 挑戰後的 `aws-waf-token` cookie 可以餵回 httpx 繼續用。
實測 token 是**硬性 TTL 約 5 分鐘、不滑動續期**（282s 通、327s 擋），
所以這裡是一台小小的狀態機：算 token 年齡、超預算就事前重取、
被擋就反應式補救一次、重取次數有上限（防失控迴圈）。

設計約束（PLAN Q4）：
- Playwright **只在 `_acquire()` 內 import**——沒裝 playwright 的環境
  `import ygo_sniper.sources.waf` 必須零成本成功，只有真的要抓 Buyee 才會失敗，
  而且失敗是 BlockedError（告警層看得見），不是 ImportError（沒人接得住）。
- token 絕不落地持久化；不常駐瀏覽器（取完就關，重取重開）。
- UA 必須與 CachedFetcher 同一字串（token 綁 UA 指紋）——兩邊都讀
  `cfg.fetch["user_agent"]`，同源同值。
"""

from __future__ import annotations

import time

from ..config import Config
from .base import BlockedError, CachedFetcher

#: 實測 282s 通 / 327s 擋 → 預算 240s 留 42s 邊際。
#: 連兩天觸發反應式重取就降 180（PLAN 風險 3）。
TTL_BUDGET_SECONDS = 240

#: 一輪掃描最多重取幾次 token。超過代表 token 根本無效（UA 不符、IP 被封），
#: 再開瀏覽器只是燒時間，直接大聲失敗讓告警層看見。
MAX_REFRESHES_PER_RUN = 4

_PLAYWRIGHT_HINT = (
    "未安裝 playwright，Buyee 系來源不可用；"
    "uv pip install -e '.[browser]' && .venv/bin/playwright install chromium"
)


class WafSession:
    """管理一顆 `aws-waf-token`，對外只暴露 `get()`（與 CachedFetcher 同介面）。

    一顆 token 解鎖**全部** Buyee 路徑（Mercari／PayPay／Yahoo 商品頁，
    RECON §4 實證），所以 buyee_mercari 與 buyee_paypay 共用同一個實例——
    分開建只會多開一次瀏覽器、多燒一次重取次數。
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.fetcher = CachedFetcher(cfg)
        self._token: str | None = None
        self._acquired_at = 0.0
        self._refreshes = 0
        self._seed_url: str | None = None
        self._seed_html: str | None = None
        # 可注入的時鐘（測試用假時鐘）；monotonic 不受系統校時影響，算年齡專用
        self._clock = time.monotonic

    # ------------------------------------------------------------------
    def _acquire(self, seed_url: str) -> tuple[str, str]:
        """開 headless chromium、載入 seed_url、取回 (token, 該頁 HTML)。

        測試 monkeypatch 這個方法，狀態機測試完全不碰瀏覽器。
        """
        try:
            from playwright.sync_api import sync_playwright  # 刻意函式內 import，見模組 docstring
        except ImportError as exc:
            raise BlockedError(_PLAYWRIGHT_HINT, url=seed_url) from exc

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=self.cfg.fetch["user_agent"])
                page = context.new_page()
                page.goto(seed_url, wait_until="domcontentloaded")
                try:
                    # 等結果骨架渲染完（也等掉 WAF 過場）；等不到不算致命——
                    # 有些頁（如查無結果）骨架是空的，token 照樣拿得到
                    page.wait_for_selector("ul.item-lists", timeout=20_000)
                except Exception:  # noqa: BLE001 - 逾時只影響 seed_html 重用，token 才是主角
                    pass
                html = page.content()
                token = next(
                    (c["value"] for c in context.cookies() if c["name"] == "aws-waf-token"),
                    None,
                )
            finally:
                browser.close()

        if not token:
            raise BlockedError(
                "Playwright 開了 seed 頁但沒拿到 aws-waf-token cookie——"
                "挑戰沒解開或 WAF 行為變了",
                url=seed_url,
            )
        return token, html

    # ------------------------------------------------------------------
    def _refresh(self, seed_url: str) -> None:
        if self._refreshes >= MAX_REFRESHES_PER_RUN:
            raise BlockedError(
                f"WAF token 重取已達上限 {MAX_REFRESHES_PER_RUN} 次，"
                "本輪放棄 Buyee 系來源（token 可能整體失效：UA 不符或 IP 被封）",
                url=seed_url,
            )
        age = self._clock() - self._acquired_at if self._token is not None else 0.0
        self._refreshes += 1
        # 年齡進 log：這是 TTL_BUDGET_SECONDS=240 的校準依據（PLAN 風險 3）
        print(f"[waf] token 重取（前一顆用了 {age:.0f}s，第 {self._refreshes} 次）")

        try:
            token, html = self._acquire(seed_url)
        except ImportError as exc:
            # _acquire 被替換（測試）或 playwright 深處炸 ImportError 時的最後防線：
            # 統一轉 BlockedError，告警層才接得住
            raise BlockedError(_PLAYWRIGHT_HINT, url=seed_url) from exc

        self._token = token
        self._acquired_at = self._clock()
        self.fetcher.set_cookie("aws-waf-token", token)
        # seed 頁 HTML 重用：瀏覽器已經渲染好第一頁，別再打一次。
        # 太短的（挑戰沒解開）不重用也不進快取，走 fetcher 讓失敗大聲拋出。
        if len(html.strip()) >= CachedFetcher.MIN_BODY_BYTES:
            self._seed_url = seed_url
            self._seed_html = html
            self.fetcher._write_cache(self.fetcher._cache_path(seed_url), html)
        else:
            self._seed_url = None
            self._seed_html = None

    def _pop_seed(self, url: str) -> str | None:
        if url == self._seed_url and self._seed_html is not None:
            html, self._seed_html = self._seed_html, None
            return html
        return None

    def _cache_would_hit(self, url: str) -> bool:
        """與 CachedFetcher.get 的快取命中條件**同一判準**（同源同基準）。

        快取會命中時完全不碰 token——第二次跑同一輪掃描必須數秒完成、
        不開瀏覽器；反過來說這裡判會中而 fetcher 判不中，就會裸著沒 token
        去打網路，所以兩邊條件必須一致。
        """
        cp = self.fetcher._cache_path(url)
        if not (cp.exists() and (time.time() - cp.stat().st_mtime) < self.fetcher.ttl):
            return False
        cached = cp.read_text(encoding="utf-8", errors="ignore")
        return len(cached.strip()) >= CachedFetcher.MIN_BODY_BYTES

    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        use_cache: bool = True,
        allow_statuses: tuple[int, ...] = (),
        min_bytes: int | None = None,
    ) -> str:
        """與 CachedFetcher.get 同介面。token 生命週期完全在這裡管理。"""
        kw = {"allow_statuses": allow_statuses, "min_bytes": min_bytes}
        if use_cache and self._cache_would_hit(url):
            return self.fetcher.get(url, use_cache=True, **kw)

        # 事前重取：超過預算的 token 幾乎必被擋，先重取省一次注定失敗的請求
        if self._token is None or (self._clock() - self._acquired_at) > TTL_BUDGET_SECONDS:
            self._refresh(url)
            seed = self._pop_seed(url)
            if seed is not None:
                return seed

        try:
            return self.fetcher.get(url, use_cache=use_cache, **kw)
        except BlockedError:
            # 反應式補救：token 自認新鮮仍被擋 → 重取一次、重試一次；
            # 再擋就往上拋（_refresh 達上限時也會在這裡拋），絕不迴圈
            self._refresh(url)
            seed = self._pop_seed(url)
            if seed is not None:
                return seed
            return self.fetcher.get(url, use_cache=use_cache, **kw)

    def close(self) -> None:
        self.fetcher.close()

    def __enter__(self) -> WafSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
