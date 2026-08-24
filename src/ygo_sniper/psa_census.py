"""PSA 鑑定量（population）抓取——走官方 public API。

**為什麼不刮網頁**：網頁版 pop report（`www.psacard.com/pop`）被 Cloudflare
challenge 擋（2026-08-24 實測 httpx/curl 403 "Just a moment..."），刮不了；
不要試 Playwright 過 Cloudflare——那是與 AWS WAF 不同的擋法，headless 過不去，
且會違反「測試路徑＝生產路徑」（CLAUDE.md 第六節）。API（`api.psacard.com/publicapi`）
可達、無 challenge（2026-08-24 實測回 JSON）。

**入口是卡磚憑證編號（cert number），不是卡名**：API 沒有卡名搜尋端點。
`GET /cert/GetByCertNumber/{cert}` 回 `SpecID`（外加卡片身分做 sanity check），
`GET /pop/GetPSASpecPopulation/{specID}` 回各級張數。`census_url` 存 pop API
URL，之後 refresh 不需要再給 cert。

⚠️ **欄位名依 swagger.json（2026-08-24 從 `https://api.psacard.com/publicapi/swagger.json`
實抓），尚未對過真實 payload**（匿名額度當日已滿、無 token 可實測）。工程原則
「欄位名是假設不是事實」：拿到 token 實跑第一次之後，把真實回應存成 fixture
取代手寫 fixture，並複驗解析（見 docs/plans/psa-census.md Task 5）。

背景見 docs/plans/psa-census.md。
"""
from __future__ import annotations

import json
import os
import re

from .ars_census import CensusParseError
from .sources.base import FetchError

CERT_URL = "https://api.psacard.com/publicapi/cert/GetByCertNumber/{cert}"
POP_URL = "https://api.psacard.com/publicapi/pop/GetPSASpecPopulation/{spec_id}"
TOKEN_ENV = "PSA_API_TOKEN"
TOKEN_HINT = ("PSA census 需要 API token：到 https://www.psacard.com/publicapi 免費註冊產生，"
              "寫進 .env 的 PSA_API_TOKEN=…（額度 100 次/天，一張卡首抓用 2 次）")

_POP_URL_RE = re.compile(r"api\.psacard\.com/publicapi/pop/GetPSASpecPopulation/(\d+)")
_GRADE_KEY_RE = re.compile(r"^Grade(\d+)(_5)?(Q)?$")


def api_token() -> str | None:
    """load_config() 已把 .env 灌進 os.environ，這裡直接讀。"""
    return os.getenv(TOKEN_ENV) or None


def spec_id_from_pop_url(url: str) -> int | None:
    m = _POP_URL_RE.search(url or "")
    return int(m.group(1)) if m else None


def _as_int(value: object, *, what: str) -> int:
    """數值欄位的統一轉型。欄位型別是假設不是事實（swagger 未對過真實 payload）——
    轉不動就拋 CensusParseError，讓它落回呼叫端「URL 照存、舊資料不動」的失敗
    路徑，而不是讓裸 ValueError 炸穿 `except (FetchError, CensusParseError)`。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CensusParseError(
            f"{what} 不是數字（拿到 {value!r}）——API 回應型別與 swagger 假設不符"
        ) from exc


def _get_json(url: str, *, fetcher, token: str) -> dict:
    """帶 bearer token 抓 JSON。429（額度用完）翻成看得懂的訊息再拋。

    min_bytes=2：JSON API 沒有「挑戰頁 vs 正常頁」的長度空隙（base.py MIN_BODY_BYTES 註記）。
    use_cache=False：census 是 refresh 語意，永遠要新鮮的。
    """
    try:
        body = fetcher.get(url, use_cache=False, min_bytes=2,
                           headers={"Authorization": f"bearer {token}"})
    except FetchError as exc:
        if exc.status == 429:
            raise FetchError("PSA API 當日額度用完（100 次/天）——明天再 --refresh-census",
                             url=url, status=429, transient=True) from exc
        raise
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise CensusParseError(f"PSA API 回的不是 JSON（前 80 字：{body[:80]!r}）") from exc
    if not isinstance(data, dict):
        raise CensusParseError("PSA API 回的 JSON 不是物件——API 形狀可能改了")
    return data


def fetch_cert(cert_number: str, *, fetcher, token: str) -> dict:
    """→ PSACert dict（含 SpecID 與卡片身分）。查無此 cert 大聲拋，絕不回空 dict。"""
    data = _get_json(CERT_URL.format(cert=cert_number), fetcher=fetcher, token=token)
    cert = data.get("PSACert") or {}
    if not cert.get("SpecID"):
        raise CensusParseError(
            f"cert {cert_number} 查不到 SpecID——編號打錯、或 API 形狀改了"
            f"（回應 keys：{sorted(data)}）")
    # 呼叫端會拿 SpecID 去組 URL，這裡就把型別釘死，壞值走同一條解析失敗路徑
    cert["SpecID"] = _as_int(cert["SpecID"], what=f"cert {cert_number} 的 SpecID")
    return cert


def cert_identity_line(cert: dict) -> str:
    """給使用者眼睛驗的一行（同卡 sanity check 的 PSA 版）。"""
    parts = [str(cert.get(k) or "").strip()
             for k in ("Year", "Brand", "CardNumber", "Subject", "Variety")]
    ident = " ".join(p for p in parts if p)
    grade = str(cert.get("CardGrade") or "").strip()
    return f"{ident}（{cert.get('GradeDescription') or ''} {grade}）".strip()


def _counts_from_pop(pop: dict) -> dict[str, int]:
    """PSAPop → ARS census 同形的 {級別: 張數}。key 例：AU / 1 / 1Q / 1.5 / 10。"""
    counts: dict[str, int] = {}
    if "Auth" in pop:
        counts["AU"] = _as_int(pop.get("Auth") or 0, what="PSAPop.Auth")
    for key, val in pop.items():
        m = _GRADE_KEY_RE.match(key)
        if not m:
            continue
        label = m.group(1) + (".5" if m.group(2) else "") + ("Q" if m.group(3) else "")
        counts[label] = _as_int(val or 0, what=f"PSAPop.{key}")
    if not any(_GRADE_KEY_RE.match(k) for k in pop):
        raise CensusParseError(
            f"PSAPop 裡沒有任何 GradeN 欄位——API 形狀可能改了（keys：{sorted(pop)}）")
    return counts


def fetch_spec_population(spec_id: int, *, fetcher, token: str
                          ) -> tuple[dict[str, int], int | None, str]:
    """→ (各級張數, 總數, spec Description)。Description 給呼叫端做同卡 sanity 呈現。"""
    data = _get_json(POP_URL.format(spec_id=spec_id), fetcher=fetcher, token=token)
    pop = data.get("PSAPop")
    if not isinstance(pop, dict):
        raise CensusParseError(
            f"spec {spec_id} 的回應沒有 PSAPop——API 形狀可能改了（keys：{sorted(data)}）")
    total = pop.get("Total")
    return (_counts_from_pop(pop),
            _as_int(total, what="PSAPop.Total") if total is not None else None,
            str(data.get("Description") or ""))
