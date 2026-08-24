"""PSA census 接進 `_ingest_census`：cert 換 SpecID、失敗語意與 ARS 分支同一條紅線。

⚠️ 沿用 tests/test_psa_census.py 的 FakeFetcher／swagger-shaped fixture——
尚未對過真實 payload（見 docs/plans/psa-census.md Task 5）。
"""
from __future__ import annotations

import json

import pytest

from ygo_sniper.card_snipe import add_card_watch, refresh_watch_census
from ygo_sniper.psa_census import POP_URL
from ygo_sniper.sources.base import FetchError
from ygo_sniper.store import Store

WATCH_KW = dict(
    grader="PSA", grade=10.0, grade_label="10",
    name_ja="コスモクイーン", name_en="COSMO QUEEN",
    aliases=[], code_raw="", code_norm="",
)

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

POP_URL_987654 = POP_URL.format(spec_id=987654)


class FakeFetcher:
    """沿用 tests/test_psa_census.py 的假件慣例：{url 子字串: body 或 FetchError}。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, *, use_cache=True, min_bytes=None, headers=None, **kw):
        self.calls.append((url, headers))
        for frag, body in self.responses.items():
            if frag in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


class TestIngestPsaCensus:
    def test_no_cert_no_url_prompts_for_psa_cert(self, store, monkeypatch):
        """1. PSA、無 cert、無 URL → 訊息含 --psa-cert，census_json 未寫入。"""
        monkeypatch.setenv("PSA_API_TOKEN", "T")
        res = add_card_watch(
            store, FakeFetcher({}), grader="PSA", grade_input="10",
            name_ja="コスモクイーン",
        )
        w = store.get_card_watch(res.watch_id)
        assert not w["census_json"]
        joined = "\n".join(res.messages)
        assert "--psa-cert" in joined

    def test_cert_and_token_fetches_and_stores_census(self, store, monkeypatch):
        """2. PSA、有 cert、有 token → census_json/total/url 落庫，訊息含「cert 查驗」。"""
        monkeypatch.setenv("PSA_API_TOKEN", "T")
        f = FakeFetcher({
            "GetByCertNumber/12345678": CERT_BODY,
            "GetPSASpecPopulation/987654": POP_BODY,
        })
        wid = store.insert_card_watch(**WATCH_KW)
        from ygo_sniper.card_snipe import _ingest_census

        msgs = _ingest_census(
            store, f, wid, url="", grader="PSA", name_ja="コスモクイーン",
            code_raw="", code_norm="", psa_cert="12345678",
        )
        w = store.get_card_watch(wid)
        assert json.loads(w["census_json"])["9"] == 30
        assert w["census_total"] == 60
        assert w["census_url"] == POP_URL_987654
        joined = "\n".join(msgs)
        assert "cert 查驗" in joined

    def test_refresh_with_existing_pop_url_skips_cert_lookup(self, store, monkeypatch):
        """3. refresh：census_url 已是 pop URL、不給 cert → 只打 pop 端點。"""
        monkeypatch.setenv("PSA_API_TOKEN", "T")
        f = FakeFetcher({"GetPSASpecPopulation/987654": POP_BODY})
        wid = store.insert_card_watch(**WATCH_KW)
        store.update_card_watch_census_url(wid, census_url=POP_URL_987654)
        w = dict(store.get_card_watch(wid))

        msgs = refresh_watch_census(store, f, w)

        assert not any("GetByCertNumber" in (url or "") for url, _ in f.calls)
        w2 = store.get_card_watch(wid)
        assert json.loads(w2["census_json"])["10"] == 12
        assert w2["census_total"] == 60
        assert msgs  # 有訊息回報

    def test_pop_fetch_failure_keeps_existing_census(self, store, monkeypatch):
        """4. 失敗不清舊資料：舊 census_json 先塞好，pop 端點回 FetchError → 舊值原樣保留。"""
        monkeypatch.setenv("PSA_API_TOKEN", "T")
        wid = store.insert_card_watch(**WATCH_KW)
        store.update_card_watch_census(
            wid, census_url=POP_URL_987654,
            census_json='{"9": 5, "10": 5}', census_total=10,
            now="2026-08-01T00:00:00+00:00",
        )
        err = FetchError("connection reset", url="u", status=None)
        f = FakeFetcher({"GetPSASpecPopulation/987654": err})
        w = dict(store.get_card_watch(wid))

        msgs = refresh_watch_census(store, f, w)

        after = store.get_card_watch(wid)
        assert json.loads(after["census_json"]) == {"9": 5, "10": 5}
        assert after["census_total"] == 10
        joined = "\n".join(msgs)
        assert "保留" in joined

    def test_no_token_keeps_existing_and_mentions_token_env(self, store, monkeypatch):
        """5. 無 token → 訊息含 TOKEN_ENV 字樣，舊資料不動。"""
        monkeypatch.delenv("PSA_API_TOKEN", raising=False)
        wid = store.insert_card_watch(**WATCH_KW)
        store.update_card_watch_census(
            wid, census_url=POP_URL_987654,
            census_json='{"9": 5, "10": 5}', census_total=10,
            now="2026-08-01T00:00:00+00:00",
        )
        w = dict(store.get_card_watch(wid))

        msgs = refresh_watch_census(store, FakeFetcher({}), w, psa_cert="12345678")

        after = store.get_card_watch(wid)
        assert json.loads(after["census_json"]) == {"9": 5, "10": 5}
        assert after["census_total"] == 10
        joined = "\n".join(msgs)
        assert "PSA_API_TOKEN" in joined

    def test_cert_grade_mismatch_warns_but_still_stores(self, store, monkeypatch):
        """6. cert 分數 9 vs 狙擊目標 10 → 訊息含 PSA9「照樣有效」警語；census 照落庫。"""
        monkeypatch.setenv("PSA_API_TOKEN", "T")
        f = FakeFetcher({
            "GetByCertNumber/12345678": CERT_BODY,
            "GetPSASpecPopulation/987654": POP_BODY,
        })
        wid = store.insert_card_watch(**WATCH_KW)  # grade_label="10"
        w = dict(store.get_card_watch(wid))

        msgs = refresh_watch_census(store, f, w, psa_cert="12345678")

        joined = "\n".join(msgs)
        assert "PSA9" in joined
        assert "照樣有效" in joined
        after = store.get_card_watch(wid)
        assert json.loads(after["census_json"])["9"] == 30
        assert after["census_total"] == 60
