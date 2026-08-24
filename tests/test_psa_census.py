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
