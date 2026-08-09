# 額外信任錨（補對方伺服器漏送的中間憑證）

這個目錄裡的每一個 `.pem` 都會在 `CachedFetcher` 建立時被載進 SSL context
（`src/ygo_sniper/sources/base.py` 的 `build_ssl_context`），**疊在 certifi 之上**。

**這不是「關掉憑證驗證」。** 這裡只放公開的、由已受信任的根憑證簽發的中間憑證——
補上對方伺服器該送而沒送的那一段。信任決策沒有任何改變：驗證照常做，
鏈的終點照樣是 certifi 本來就信任的根。

> ⚠️ 任何要放進這裡的憑證，必須先能通過
> `openssl verify -CAfile $(python -c 'import certifi;print(certifi.where())') <檔案>`。
> 過不了 = 它不鏈到 certifi 的根 = 放進來就是自己新增一個信任錨，那是另一回事，不准做。

---

## digicert-global-g2-tls-rsa-sha256-2020-ca1.pem

| 項目 | 值 |
|---|---|
| Subject | `C=US, O=DigiCert Inc, CN=DigiCert Global G2 TLS RSA SHA256 2020 CA1` |
| Issuer | `DigiCert Global Root G2`（certifi 內建，本來就信任） |
| SHA256 指紋 | `C8:02:5F:9F:C6:5F:DF:C9:5B:3C:A8:CC:78:67:B9:A5:87:B5:27:79:73:95:79:17:46:3F:C8:13:D0:B6:25:A9` |
| 序號 | `0CF5BD062B5602F47AB8502C23CCF066` |
| 有效期 | 2021-03-30 ～ **2031-03-29** |
| 來源 | leaf 憑證 AIA 的 `CA Issuers`：`http://cacerts.digicert.com/DigiCertGlobalG2TLSRSASHA2562020CA1-1.crt`（DER） |
| 加入日期 | 2026-08-09 |

### 為什麼需要它

`ars-grading.com`（ARS 鑑定量 census 的唯一來源）**送出不完整的憑證鏈**：
它把自己的 leaf 送了兩次，中間憑證一張都沒送。2026-08-09 實測：

```
$ echo | openssl s_client -connect ars-grading.com:443 -servername ars-grading.com
 0 s:/C=JP/ST=Tokyo/L=Chiyoda-ku/O=ARSALES, K.K./CN=ars-grading.com
 1 s:/C=JP/ST=Tokyo/L=Chiyoda-ku/O=ARSALES, K.K./CN=ars-grading.com   ← 又是 leaf，不是中間憑證
```

於是同一個網址：

- `curl` **過得了**——macOS 系統憑證庫裡剛好有這張中間憑證（所以手動 debug 時一切正常，
  最容易得出「程式碼有問題」的錯誤結論）
- `httpx`（我們的生產路徑，走 certifi，**只含根憑證**）**過不了**：
  `ConnectError [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate`

補上這一張之後 httpx 就通了（實測 `HTTP 200`、12 個 `grade-entry`）。

### 到期後（或 DigiCert 換鏈、或對方修好伺服器）會怎麼壞

**壞法是一樣的：census 抓不到，卡片的「存世量」永遠是空的。**
好消息是它會大聲壞——`ars_census` 走 `CachedFetcher`，SSL 失敗會變成
`FetchError(transient=True)`，訊息長這樣：

```
連線失敗: ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate (_ssl.c:1010)
```

看到這行就照下面重新取得（**不要**改成 `verify=False`，那會連「對方被中間人換掉」
都一起接受；也不要靠 `SSL_CERT_FILE` 環境變數，那是機器設定，換一台機器就靜默失效）：

```bash
# 1. 從當下 leaf 的 AIA 讀出正確的中間憑證網址（不要照抄舊網址，鏈可能已經換了）
echo | openssl s_client -connect ars-grading.com:443 -servername ars-grading.com 2>/dev/null \
  | openssl x509 | openssl x509 -noout -text | grep -A1 'Authority Information Access'

# 2. 抓下來轉成 PEM（DigiCert 給的是 DER）
curl -sS -o /tmp/inter.crt http://cacerts.digicert.com/<上一步得到的檔名>.crt
openssl x509 -inform DER -in /tmp/inter.crt -out data/certs/<描述性檔名>.pem

# 3. 確認它真的鏈到 certifi 的根——這一步不可省略（見本檔開頭的紅字）
openssl verify -CAfile "$(.venv/bin/python -c 'import certifi;print(certifi.where())')" \
  data/certs/<描述性檔名>.pem      # 必須印出 OK

# 4. 端到端驗一次（tests/test_ca_bundle.py 只驗檔案，不打網路）
.venv/bin/python -c "
from ygo_sniper.config import load_config
from ygo_sniper.sources.base import CachedFetcher
from ygo_sniper.ars_census import fetch_census
with CachedFetcher(load_config()) as f:
    print(fetch_census('https://ars-grading.com/grading/searchNameDetail?id=001202208090020007', fetcher=f))
"
```

**對方哪天修好伺服器設定（開始送完整的鏈）時**：什麼都不會壞，這張憑證變成冗餘——
多一份一模一樣的中間憑證在 store 裡不影響驗證結果。可以留著（成本是零），
到期時直接刪掉即可。

**`tests/test_ca_bundle.py` 會在憑證過期前 90 天開始失敗**，
所以到期不會以「census 又空了」的形式被發現——會先被測試打斷。
