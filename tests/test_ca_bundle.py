"""`data/certs` 的補充信任錨——檔案本身，以及 CachedFetcher 的降級路徑。

背景（完整版見 `data/certs/README.md`）：`ars-grading.com` 送出不完整的憑證鏈
（leaf 送兩次、中間憑證一張都沒送），所以 certifi 驗不過，census 抓不到，
而 census 是狙擊卡功能的四大賣點之一。修法是把那張公開的中間憑證存進 repo，
建 SSL context 時疊在 certifi 之上。

這個檔案釘住三件事：
1. **那張憑證還是原來那張**（subject／issuer／有效期），而且**沒有偷偷擴大信任**
   ——它的簽發者必須是 certifi 本來就有的根。
2. **沒有憑證檔時行為不變**（純 certifi）。憑證是補丁不是前提，
   一個 fresh clone（`data/` 在 .gitignore 裡有大半不進版控）不該整個抓取壞掉。
3. **憑證到期前 90 天就紅燈**。不然到期那天的外顯是「census 又空了」，
   跟「這張卡沒人送鑑定」長得一模一樣——正是本專案第五節在防的靜默失敗。

全部只讀本機檔案，不發出任何網路請求。
"""

import ssl
import time
from dataclasses import replace

import certifi
import pytest

from ygo_sniper.config import project_root
from ygo_sniper.sources.base import (
    EXTRA_CA_DIR,
    CachedFetcher,
    build_ssl_context,
    extra_ca_files,
)

#: ars-grading.com 漏送的那一張。改檔名要連這裡一起改，不然測試會直接告訴你。
ARS_INTERMEDIATE = "digicert-global-g2-tls-rsa-sha256-2020-ca1.pem"
EXPECTED_SUBJECT_CN = "DigiCert Global G2 TLS RSA SHA256 2020 CA1"
EXPECTED_ISSUER_CN = "DigiCert Global Root G2"
#: 到期前這麼多天就開始紅燈，留足夠時間換新的（見 README 的重新取得步驟）。
EXPIRY_WARNING_DAYS = 90


def _cn(rdns) -> str:
    """`get_ca_certs()` 的 subject/issuer 是巢狀 tuple，挑出 commonName。"""
    for rdn in rdns:
        for key, value in rdn:
            if key == "commonName":
                return value
    return ""


def _decode(pem_path) -> dict:
    """只載入這一張憑證，回傳 ssl 解出來的欄位（不碰網路、不碰 certifi）。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(pem_path))
    certs = ctx.get_ca_certs()
    assert len(certs) == 1, f"{pem_path.name} 應該只含一張憑證，實際 {len(certs)} 張"
    return certs[0]


@pytest.fixture
def ars_pem():
    p = project_root() / EXTRA_CA_DIR / ARS_INTERMEDIATE
    if not p.exists():
        pytest.fail(
            f"{p} 不存在。ars-grading.com（census 來源）送不完整的憑證鏈，"
            "少了這張中間憑證 census 會整批抓不到——重新取得的步驟見 data/certs/README.md"
        )
    return p


def test_ars_intermediate_is_loadable_and_is_the_expected_certificate(ars_pem):
    """檔案要能被 ssl 載入，而且**是預期的那一張**。

    只斷言「檔案存在」不夠：換成別張憑證、或存成 DER（`ssl` 載不了）都會
    讓 census 壞掉，而壞掉的樣子是「存世量空白」，沒有人會聯想到憑證。
    """
    info = _decode(ars_pem)

    assert _cn(info["subject"]) == EXPECTED_SUBJECT_CN
    assert _cn(info["issuer"]) == EXPECTED_ISSUER_CN
    assert info["serialNumber"].upper() == "0CF5BD062B5602F47AB8502C23CCF066"


def test_ars_intermediate_chains_to_a_root_certifi_already_trusts(ars_pem):
    """**沒有新增信任錨**——這是「補漏送的一段」與「放寬驗證」的分界線。

    它的簽發者必須本來就在 certifi 裡；不然我們等於自己認了一個新的根，
    那是完全不同性質的決定（見 data/certs/README.md 開頭的紅字）。
    """
    issuer_cn = _cn(_decode(ars_pem)["issuer"])

    certifi_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    certifi_ctx.load_verify_locations(cafile=certifi.where())
    roots = {_cn(c["subject"]) for c in certifi_ctx.get_ca_certs()}

    assert issuer_cn in roots, f"{issuer_cn} 不在 certifi 的根憑證裡——這張不能放進 data/certs"


def test_ars_intermediate_is_not_near_expiry(ars_pem):
    """到期前 90 天先紅燈，不要等到 census 變空才發現。

    這條會在 2030-12-29 左右開始失敗（憑證 2031-03-29 到期），
    那是刻意的：失敗訊息會直接指到換發步驟。
    """
    not_after = _decode(ars_pem)["notAfter"]
    remaining_days = (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400

    assert remaining_days > EXPIRY_WARNING_DAYS, (
        f"{ARS_INTERMEDIATE} 於 {not_after} 到期（剩 {remaining_days:.0f} 天）。"
        "過期後 ars-grading.com 會驗不過、census 整批空白——"
        "換發步驟見 data/certs/README.md"
    )


def test_repo_certs_are_all_loadable():
    """目錄裡**每一張** PEM 都要能載入。

    只驗自己認識的那一張的話，日後有人丟進第三張壞檔，
    build_ssl_context 會在生產路徑上炸掉而測試全綠。
    """
    pems = extra_ca_files(project_root())
    assert pems, "data/certs 下沒有任何 PEM——ars-grading 的中間憑證不見了？"
    for pem in pems:
        _decode(pem)


def test_build_ssl_context_without_certs_dir_equals_plain_certifi(tmp_path):
    """目錄不存在時，行為與「就是 certifi」完全一樣。

    憑證檔是補丁不是前提：`data/` 大半不進版控，一個 fresh clone 或別台機器
    可能根本沒有 data/certs，那時候只該 census 抓不到，不該整個掃描器起不來。
    """
    assert extra_ca_files(tmp_path) == []

    ctx = build_ssl_context(tmp_path)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED  # 絕不可以退化成不驗證
    assert ctx.check_hostname is True

    baseline = ssl.create_default_context(cafile=certifi.where())
    assert len(ctx.get_ca_certs()) == len(baseline.get_ca_certs())


def test_build_ssl_context_with_empty_certs_dir_is_also_plain_certifi(tmp_path):
    """目錄存在但空的（例如 git 只留下目錄）也走同一條降級路徑。"""
    (tmp_path / EXTRA_CA_DIR).mkdir(parents=True)

    assert extra_ca_files(tmp_path) == []
    baseline = ssl.create_default_context(cafile=certifi.where())
    assert len(build_ssl_context(tmp_path).get_ca_certs()) == len(baseline.get_ca_certs())


def test_build_ssl_context_on_repo_root_includes_the_ars_intermediate(ars_pem):
    """真的專案根目錄建出來的 context 裡，那張中間憑證確實在。

    這條才是「修法有沒有生效」的斷言——前面那些只證明檔案沒壞。
    """
    subjects = {_cn(c["subject"]) for c in build_ssl_context(project_root()).get_ca_certs()}

    assert EXPECTED_SUBJECT_CN in subjects
    assert EXPECTED_ISSUER_CN in subjects  # certifi 的根也還在，不是被取代掉


def test_broken_pem_fails_loudly_instead_of_being_skipped(tmp_path):
    """壞掉的憑證檔要當場大聲拋，不准「跳過這一張繼續跑」。

    跳過的下場是 census 又變空的，而錯誤會出現在幾層外的 SSL 握手裡，
    沒有人會聯想到是這個檔案壞了——靜默失敗的教科書案例。
    """
    d = tmp_path / EXTRA_CA_DIR
    d.mkdir(parents=True)
    (d / "broken.pem").write_text("-----BEGIN CERTIFICATE-----\nnot base64\n")

    with pytest.raises(RuntimeError) as exc:
        build_ssl_context(tmp_path)

    assert "broken.pem" in str(exc.value)


def test_fetcher_builds_without_certs_dir(cfg, tmp_path):
    """`CachedFetcher` 在沒有 data/certs 的環境仍要能建起來（且不碰網路）。

    建構子現在會去讀憑證目錄——這條擋住「少了憑證檔就整批抓取起不來」的退步。
    """
    scoped = replace(
        cfg,
        root=tmp_path,
        storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
    )
    assert not (tmp_path / EXTRA_CA_DIR).exists()

    with CachedFetcher(scoped) as fetcher:
        assert fetcher._client is not None


def test_fetcher_uses_the_extra_ca_bundle_at_repo_root(cfg, ars_pem):
    """預設設定（root = 專案根目錄）建出來的 fetcher 帶著那張中間憑證。

    沒有這條的話，`build_ssl_context` 可以被正確實作卻沒接到 httpx 上，
    而所有 mock transport 的測試都不會察覺——線上 census 照樣抓不到。
    """
    with CachedFetcher(cfg) as fetcher:
        # httpx 沒有公開 API 讀回 client 的 SSL context，只能走內部結構
        # （httpx 0.28 實測）。哪天 httpx 改版讓這行 AttributeError，
        # 那是「換個寫法」的維護成本，不是這條斷言不該存在的理由——
        # 少了它，build_ssl_context 可以寫得完全正確卻沒接到 httpx 上。
        pool = fetcher._client._transport._pool
        ctx = pool._ssl_context
        subjects = {_cn(c["subject"]) for c in ctx.get_ca_certs()}

    assert EXPECTED_SUBJECT_CN in subjects
