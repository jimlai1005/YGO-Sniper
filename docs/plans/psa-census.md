# PSA 存世量（census）抓取 Implementation Plan

> **For agentic workers:** 逐 task 執行，TDD（先寫紅燈測試再實作）。每個 task 完成後主線程親跑驗收指令。
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `snipe add` / `snipe report --refresh-census` 對 `grader=PSA` 的狙擊卡抓到各級張數與鑑定總數，落進既有的 `census_json` / `census_total` 欄位（與 ARS 同形），不再回「鑑定量查詢未支援」。

**Architecture:** 走 PSA 官方 public API（`api.psacard.com/publicapi`）。網頁版 pop report（`www.psacard.com/pop`）被 Cloudflare challenge 擋（2026-08-24 實測 httpx/curl 403 "Just a moment..."），刮不了；API 可達、無 challenge（實測回 JSON）。API **沒有卡名搜尋端點**，所以 PSA census 的入口是**卡磚憑證編號（cert number）**：`GET /cert/GetByCertNumber/{cert}` 回 `SpecID`（外加卡片身分可做 sanity check），`GET /pop/GetPSASpecPopulation/{specID}` 回各級張數。`census_url` 存 pop API URL，之後 refresh 不需要再給 cert。

**Tech Stack:** 既有 `CachedFetcher`（resilience boundary，只加 headers 透傳）、httpx、pytest。

**認證（使用者前置作業，工具內只讀環境變數）：**
- bearer token：使用者在 https://www.psacard.com/publicapi 免費註冊產生，放 `.env` 的 `PSA_API_TOKEN`
- 額度 100 次/天（一次完整 census＝cert 1 次＋pop 1 次＝2 次；refresh 只要 pop 1 次）
- 匿名呼叫共用額度且已滿（2026-08-24 實測 429 "maximum admitted 100 per Day"）→ 一律要求 token

**Swagger 依據（2026-08-24 從 `https://api.psacard.com/publicapi/swagger.json` 實抓）：**

- `GET /cert/GetByCertNumber/{certNumber}` → `PublicCertificationModel`：
  `{"PSACert": {"CertNumber", "SpecID"(int), "SpecNumber", "Year", "Brand", "Category",
  "CardNumber", "Subject", "Variety", "GradeDescription", "CardGrade",
  "TotalPopulation", "PopulationHigher", ...}, "DNACert": {...}}`
- `GET /pop/GetPSASpecPopulation/{specID}` → `PSASpecPopulationModel`：
  `{"SpecID"(int), "Description"(str), "PSAPop": {"Total", "Auth", "Grade1", "Grade1Q",
  "Grade1_5", "Grade1_5Q", … "Grade10"}, "PSADNAPop": {...}}`（用 `PSAPop`；DNA 是簽名鑑定）

⚠️ **欄位名依 swagger（API 伺服器自己的契約），尚未對過真實 payload**（匿名額度當日已滿、
無 token 可實測）。工程原則「欄位名是假設不是事實」：拿到 token 實跑第一次之後，把真實回應
存成 fixture 取代手寫 fixture，並複驗解析（見 Task 5）。

**明確不做（YAGNI）：**
- 不做 PSA 卡名自動搜尋（做不到：API 無端點、網頁被 Cloudflare 擋；不要試 Playwright 過
  Cloudflare——那是與 AWS WAF 不同的擋法，headless 過不去且違反「測試路徑＝生產路徑」）
- 不動 web 的 snipe 登錄表單欄位（web 端要用可貼 pop API URL 進既有 census_url 欄）
- 不動 BGS（沒有可用資料源，維持「未支援」訊息）
- 不改 `parsers/grade.py`（CLAUDE.md 第二節第 5 項那個獨立案子，與本案無關）

---

### Task 1 @inline：`CachedFetcher.get` 支援 per-request headers

PSA API 要 `Authorization: bearer <token>`。外呼必須走同一個 resilience boundary
（工程原則 5），所以擴充 `get()` 而不是在 psa 模組自開 httpx client。
**不可以**把 Authorization 設成 client 預設 header——那會把 token 送給所有主機。

**Files:**
- Modify: `src/ygo_sniper/sources/base.py`（`get()`，約 270-334 行）
- Test: `tests/test_fetcher.py`（append）

- [x] **Step 1: 紅燈測試**（appended 到 `tests/test_fetcher.py`，沿用該檔既有的 fetcher fixture／monkeypatch 慣例；下面是行為，寫法照檔內既有 style）

```python
def test_get_passes_per_request_headers(...):
    """get(headers=...) 要把 headers 傳給 httpx client 的這一次請求；
    沒給 headers 時傳 None（維持 client 預設）。"""
    captured = {}
    # monkeypatch fetcher._client.get 記下 kwargs，回一個 200、body 夠長的假 Response
    html = fetcher.get("https://example.com/x", use_cache=False,
                       headers={"Authorization": "bearer T"})
    assert captured["headers"] == {"Authorization": "bearer T"}
```

- [x] **Step 2: 跑測試確認紅**：`.venv/bin/pytest tests/test_fetcher.py -k headers -v` → FAIL（get() 沒有 headers 參數）

- [x] **Step 3: 實作**——`get()` 簽名加 `headers: dict[str, str] | None = None`，
  `resp = self._client.get(url)` 改 `resp = self._client.get(url, headers=headers)`。
  docstring 補一句：「headers 只作用在這一次請求（PSA API 的 Authorization 用）；
  不進快取鍵——帶 auth 的呼叫端應自行 `use_cache=False`」。

- [x] **Step 4: 綠**：`.venv/bin/pytest tests/test_fetcher.py -v` 全綠

- [x] **Step 5: Commit** `feat(fetch): CachedFetcher.get 支援 per-request headers（PSA API 認證用）`

---

### Task 2 @inline：新模組 `psa_census.py`

**Files:**
- Create: `src/ygo_sniper/psa_census.py`
- Test: `tests/test_psa_census.py`（new；fixture 是 swagger-shaped 手寫 JSON，檔頭註明「尚未對過真實 payload」）

模組完整骨架（實作以此為準；docstring 要把「為什麼不刮網頁」「欄位名未實證」寫進去，
參考本 plan 的 Architecture 段）：

```python
"""PSA 鑑定量（population）抓取——走官方 public API。…（背景見 docs/plans/psa-census.md）"""
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
        counts["AU"] = int(pop.get("Auth") or 0)
    for key, val in pop.items():
        m = _GRADE_KEY_RE.match(key)
        if not m:
            continue
        label = m.group(1) + (".5" if m.group(2) else "") + ("Q" if m.group(3) else "")
        counts[label] = int(val or 0)
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
    return _counts_from_pop(pop), (int(total) if total is not None else None), \
        str(data.get("Description") or "")
```

- [ ] **Step 1: 紅燈測試** `tests/test_psa_census.py`（FakeFetcher 記錄 url/headers、
  按 url 回預先塞好的 JSON 字串；**絕不碰網路**——工程原則 4）：

```python
"""PSA census 解析測試。

⚠️ fixture 是照 swagger.json（2026-08-24 實抓）手寫的 swagger-shaped JSON，
**尚未對過真實 payload**（見 docs/plans/psa-census.md）。拿到 PSA_API_TOKEN
實跑後，用真實回應取代這些 dict 並保留同一組斷言。
"""
import json
import pytest
from ygo_sniper.ars_census import CensusParseError
from ygo_sniper.psa_census import (
    POP_URL, cert_identity_line, fetch_cert, fetch_spec_population,
    spec_id_from_pop_url,
)
from ygo_sniper.sources.base import FetchError


class FakeFetcher:
    def __init__(self, responses):  # {url 子字串: body 或 FetchError}
        self.responses = responses
        self.calls = []  # [(url, headers)]

    def get(self, url, *, use_cache=True, min_bytes=None, headers=None, **kw):
        self.calls.append((url, headers))
        for frag, body in self.responses.items():
            if frag in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected url {url}")


CERT_BODY = json.dumps({"PSACert": {
    "CertNumber": "12345678", "SpecID": 987654, "Year": "1999",
    "Brand": "Yu-Gi-Oh Japanese Premium Pack", "CardNumber": "",
    "Subject": "COSMO QUEEN", "Variety": "", "GradeDescription": "MINT",
    "CardGrade": "9", "TotalPopulation": 41, "PopulationHigher": 12}})

POP_BODY = json.dumps({"SpecID": 987654, "Description": "1999 Yu-Gi-Oh COSMO QUEEN",
    "PSAPop": {"Total": 60, "Auth": 1, "Grade1": 0, "Grade1Q": 0, "Grade1_5": 0,
               "Grade7": 3, "Grade8": 10, "Grade8Q": 1, "Grade8_5": 2,
               "Grade9": 30, "Grade10": 12},
    "PSADNAPop": {"Total": 0}})


def test_fetch_cert_returns_specid_and_sends_bearer():
    f = FakeFetcher({"GetByCertNumber/12345678": CERT_BODY})
    cert = fetch_cert("12345678", fetcher=f, token="T")
    assert cert["SpecID"] == 987654
    assert f.calls[0][1] == {"Authorization": "bearer T"}
    assert "COSMO QUEEN" in cert_identity_line(cert)
    assert "9" in cert_identity_line(cert)


def test_fetch_cert_without_specid_raises_loudly():
    f = FakeFetcher({"GetByCertNumber": json.dumps({"PSACert": None})})
    with pytest.raises(CensusParseError):
        fetch_cert("999", fetcher=f, token="T")


def test_population_maps_to_ars_census_shape():
    f = FakeFetcher({"GetPSASpecPopulation/987654": POP_BODY})
    counts, total, desc = fetch_spec_population(987654, fetcher=f, token="T")
    assert total == 60 and "COSMO QUEEN" in desc
    assert counts["AU"] == 1 and counts["9"] == 30 and counts["10"] == 12
    assert counts["8.5"] == 2 and counts["8Q"] == 1 and counts["1"] == 0


def test_population_without_grade_fields_raises():
    body = json.dumps({"SpecID": 1, "PSAPop": {"Total": 5}})
    f = FakeFetcher({"GetPSASpecPopulation": body})
    with pytest.raises(CensusParseError):
        fetch_spec_population(1, fetcher=f, token="T")


def test_quota_429_translated_to_readable_message():
    err = FetchError("HTTP 429", url="u", status=429, transient=True)
    f = FakeFetcher({"GetPSASpecPopulation": err})
    with pytest.raises(FetchError, match="額度"):
        fetch_spec_population(1, fetcher=f, token="T")


def test_spec_id_roundtrips_through_pop_url():
    assert spec_id_from_pop_url(POP_URL.format(spec_id=42)) == 42
    assert spec_id_from_pop_url("https://ars-grading.com/x") is None
    assert spec_id_from_pop_url("") is None
```

- [x] **Step 2: 紅**：`.venv/bin/pytest tests/test_psa_census.py -v` → FAIL（模組不存在）
- [x] **Step 3: 實作**上面骨架
- [x] **Step 4: 綠**：`.venv/bin/pytest tests/test_psa_census.py -v` 全 PASS
- [x] **Step 5: Commit** `feat(snipe): psa_census 模組——官方 API cert→spec→population`

---

### Task 3 @inline：`_ingest_census` 的 PSA 分支＋`psa_cert` 貫穿

**Files:**
- Modify: `src/ygo_sniper/card_snipe.py`
  - `_ingest_census`（430-500 行）：簽名加 `psa_cert: str = ""`，`grader == "PSA"` 走新分支
  - `add_card_watch`（502 行起）：簽名加 `psa_cert: str = ""`，傳給 `_ingest_census`
  - `refresh_watch_census`（623 行）：簽名加 `psa_cert: str = ""`，傳給 `_ingest_census`
- Test: `tests/test_card_snipe_psa.py`（new）

PSA 分支（放在 `_ingest_census` 開頭、ARS 自動搜之前；抽成 `_ingest_psa_census` 私有函式）：

```python
def _ingest_psa_census(store: Any, fetcher: Any, watch_id: int, *,
                       url: str, psa_cert: str) -> list[str]:
    """PSA census：cert → SpecID → population。失敗語意與 ARS 分支完全相同——
    URL 照存、**絕不覆寫既有 census_json/total**（讀不到 ≠ 不存在）。"""
    from .ars_census import CensusParseError
    from .psa_census import (
        POP_URL, TOKEN_ENV, TOKEN_HINT, api_token, cert_identity_line,
        fetch_cert, fetch_spec_population, spec_id_from_pop_url,
    )
    from .sources.base import FetchError

    msgs: list[str] = []
    spec_id = spec_id_from_pop_url(url)
    if not psa_cert and spec_id is None:
        msgs.append(f"PSA 的存世量要用卡磚憑證編號換：snipe report {watch_id} "
                    f"--refresh-census --psa-cert <標籤上的 cert 編號>"
                    f"（賣場照片的卡磚標籤、或 PSA 官網 cert 查驗頁都有）")
        if not api_token():
            msgs.append(f"（另外還沒設 token——{TOKEN_HINT}）")
        return msgs
    token = api_token()
    if not token:
        msgs.append(f"⚠️ 未設 {TOKEN_ENV}，這次沒抓。{TOKEN_HINT}")
        return msgs
    try:
        if psa_cert:
            cert = fetch_cert(psa_cert.strip(), fetcher=fetcher, token=token)
            msgs.append(f"cert 查驗：{cert_identity_line(cert)}——確認這是同一張卡")
            spec_id = int(cert["SpecID"])
            _warn_if_cert_mismatch(store, watch_id, cert, msgs)
        url = POP_URL.format(spec_id=spec_id)
        counts, total, desc = fetch_spec_population(spec_id, fetcher=fetcher, token=token)
    except (FetchError, CensusParseError) as exc:
        # 與 ARS 分支同一條紅線：只寫 URL，絕不碰 json／total（見 ARS 分支的註記）
        if url:
            store.update_card_watch_census_url(watch_id, census_url=url)
        kept = _existing_census_note(store, watch_id)
        msgs.append(f"⚠️ PSA census 抓取失敗（{exc}）——{kept}；之後 --refresh-census 重試")
        return msgs
    store.update_card_watch_census(
        watch_id, census_url=url,
        census_json=json.dumps(counts, ensure_ascii=False), census_total=total)
    shown = "、".join(f"{k}: {v} 張" for k, v in counts.items() if v)
    msgs.append(f"census：{shown}（鑑定總數 {total}）")
    if desc:
        msgs.append(f"（spec：{desc}）")
    return msgs
```

`_warn_if_cert_mismatch`（**只警告不擋**——pop 是整個 spec 全分數的，cert 分數不同不影響
census 正確性；卡名對不上才是大事，但 Subject 是英文、watch 主鍵是日文卡名，機器只能
比對 name_en，比不動時交給使用者的眼睛）：

```python
def _warn_if_cert_mismatch(store: Any, watch_id: int, cert: dict,
                           msgs: list[str]) -> None:
    w = store.get_card_watch(watch_id) or {}
    want = str(w.get("grade_label") or "").rstrip("+")
    got = re.search(r"\d+(?:\.\d+)?", str(cert.get("CardGrade") or ""))
    if want and got and got.group(0) != want:
        msgs.append(f"（cert 這顆是 PSA{got.group(0)}、狙擊目標是 PSA{want}——"
                    f"pop 涵蓋全部分數所以資料照樣有效，只要確認是同一張卡）")
    name_en = str(w.get("name_en") or "").strip()
    subject = str(cert.get("Subject") or "")
    if name_en and name_en.upper() not in subject.upper():
        msgs.append(f"⚠️ cert 的卡名是 {subject!r}、登錄的英文名是 {name_en!r}——"
                    f"對不上就換一顆 cert 編號重跑")
```

`_ingest_census` 本體的改動（PSA 走新分支；BGS 維持原訊息）：

```python
    msgs: list[str] = []
    if grader == "PSA":
        return _ingest_psa_census(store, fetcher, watch_id, url=url, psa_cert=psa_cert)
    if not url and grader == "ARS":
        ...  # 原樣不動
```

（原 469 行 `msgs.append(f"（{grader} 的鑑定量查詢未支援，census 留空）")` 從此只剩
BGS 會走到——訊息保留原樣。）

**裁決（2026-08-24，主線程）**：既有測試 `tests/test_card_snipe.py::TestCli::
test_add_list_report_remove_roundtrip`（~1105 行）斷言 PSA 會印「PSA 的鑑定量查詢未支援」
——那正是本 task 刻意改掉的行為。授權把該行斷言改成 `assert "--psa-cert" in r.output`
（新提示的關鍵字）。這是「更新過時斷言以反映預期行為變更」，不是放寬驗收；
除此之外不得動 `tests/test_card_snipe.py` 的任何其他內容。

- [x] **Step 1: 紅燈測試** `tests/test_card_snipe_psa.py`。用既有 `tests/test_card_snipe.py`
  的 Store 建法慣例（先讀它，照抄 in-memory store fixture 的寫法）＋ Task 2 的 FakeFetcher
  （import 或複製皆可，照該檔慣例）。監視點全部走真 Store（tmp_path 的 sqlite），
  斷言看 `store.get_card_watch(id)` 的落庫值：

```python
# 六個行為，各一個測試：
# 1. PSA、無 cert、無 URL → 訊息含 "--psa-cert"，census_json 未寫入
# 2. PSA、有 cert、有 token（monkeypatch.setenv("PSA_API_TOKEN", "T")）→
#    census_json 落庫（"9": 30）、census_total=60、census_url == POP_URL.format(spec_id=987654)、
#    訊息含 "cert 查驗"
# 3. refresh：census_url 已是 pop URL、不給 cert → 只打 pop 端點（FakeFetcher.calls 裡
#    沒有 GetByCertNumber）、census 更新
# 4. 失敗不清舊資料：先塞好舊 census_json，再讓 pop 端點回 FetchError →
#    census_json 保持舊值、訊息含 "保留"
# 5. 無 token（monkeypatch.delenv）→ 訊息含 TOKEN_ENV 字樣、舊資料不動
# 6. cert 分數 9 vs 狙擊 10 → 訊息含 "PSA9"「照樣有效」警語；census 照落庫
```

- [x] **Step 2: 紅**：`.venv/bin/pytest tests/test_card_snipe_psa.py -v` → FAIL
- [x] **Step 3: 實作**（含 `add_card_watch` / `refresh_watch_census` 簽名貫穿）
- [x] **Step 4: 綠**：`.venv/bin/pytest tests/test_card_snipe_psa.py tests/test_card_snipe.py -v` 全 PASS（既有 ARS 測試不能壞）
- [x] **Step 5: Commit** `feat(snipe): _ingest_census PSA 分支——cert 換 SpecID、失敗不清舊資料`

---

### Task 4 @inline：CLI 與文案

**Files:**
- Modify: `src/ygo_sniper/cli.py`
  - `snipe_add`（~1698 行）：加 `psa_cert: str = typer.Option("", "--psa-cert", help="PSA 卡磚憑證編號（PSA 的 census 用它換 SpecID；需 .env 設 PSA_API_TOKEN）")`，傳給 `add_card_watch(..., psa_cert=psa_cert)`；`census_url` 的 help 改「ARS census 頁 URL 或 PSA pop API URL；ARS 不給會用卡名自動搜」
  - `snipe_report`（~1787 行）：加同一個 `--psa-cert` option，傳給 `refresh_watch_census(store, fetcher, w, psa_cert=psa_cert)`；`--refresh-census` help 改「重抓鑑定量（ARS 用卡名自動搜；PSA 配 --psa-cert）」
  - `_print_dossier`：`存世量（ARS census）` 改 `f"存世量（{w['grader']} census）"`；
    「未抓到」那行（1621）改成依 grader 給提示：PSA 提示 `--psa-cert`、其他維持原文
- Modify: `web/app.py:1099` 附近 add 端點的 request body：加 `psa_cert: str = ""` 欄位並傳給
  `add_card_watch`（web 表單本身不加輸入框——CLI 為主，欄位先通）
- Modify: `web/static/index.html:642` 那段說明文案，補一句：
  「PSA 的鑑定量要在 CLI 用 `--psa-cert 卡磚憑證編號` 抓（需 PSA_API_TOKEN）。」
- Modify: `.env.example`：加

```
# PSA public API（狙擊卡 PSA 存世量用；https://www.psacard.com/publicapi 免費註冊，100 次/天）
PSA_API_TOKEN=
```

- [ ] **Step 1:** 改 CLI／web／.env.example（此 task 無新單元測試；行為已在 Task 3 蓋住，
  CLI 只是傳參。若 `tests/test_card_snipe_web.py` 有打 add 端點的測試，跑一次確認沒壞）
- [ ] **Step 2: 驗收（主線程親跑）**：
  - `.venv/bin/ygo-sniper snipe add --help` 與 `snipe report --help` 看得到 `--psa-cert`
  - `.venv/bin/ygo-sniper snipe report 5 --refresh-census`（**不給 cert、不設 token**）→
    輸出教你 `--psa-cert` 與 token 註冊，**不再是**「未支援」
  - `make test` 全綠
- [ ] **Step 3: Commit** `feat(cli): snipe --psa-cert——PSA 存世量入口＋dossier 文案`

---

### Task 5（使用者拿到 token 後的實測回合，本次不做）

- [ ] 使用者：註冊 https://www.psacard.com/publicapi 、`.env` 加 `PSA_API_TOKEN=…`
- [ ] 使用者：找一顆 コスモクイーン PSA 卡磚的 cert 編號（賣場照片標籤、或已存的證據頁）
- [ ] 主線程：`ygo-sniper snipe report 5 --refresh-census --psa-cert <編號>` 實跑，
  對照 PSA 官網 cert 頁驗數字（同源對帳）
- [ ] 把真實 cert/pop 回應（去掉個資後）存進 `tests/fixtures/`，取代 swagger-shaped
  手寫 fixture，斷言不變——這一步做完，「欄位名未實證」的警語才可以從
  `psa_census.py` docstring 與測試檔頭拿掉
