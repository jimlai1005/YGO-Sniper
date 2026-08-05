# ygo-sniper

每天一鍵掃 Buyee（Mercari / Yahoo! Auctions）與 eBay 上的 **1998–2004 年 PSA / ARS 鑑定遊戲王卡**，找出到手成本低於鑑定費、或顯著低於近期成交價的標的，推播到 Telegram，並提供 dashboard 做人工決策。

---

## 這個工具的核心觀念

**掛牌價毫無意義。** 同一張 ¥1,500 的卡，走不同路徑到手成本可以差兩倍。所以每張卡都會用所有可行路徑各算一次，取最便宜的當判斷基準。

三條日本路徑的關鍵差異：

| 路徑 | 每筆固定成本 | 可攤提成本 | 能否合併 |
|---|---|---|---|
| Buyee 集運 | 代購 ¥500 + 國內運費 ¥300 | 集中包裝 ¥700 + 國際運費 ¥2,200 | ✅ |
| Buyee 單張直寄 | 同上 | 國際運費 ¥1,400（不攤提） | ❌ |
| Mercari 台灣 | 服務費 ¥500 | Global Shipping ¥900 | ❌ **一件一件直送** |

推論很直接：**買單張 → Mercari TW 勝；湊多張 → Buyee 集運勝。** 所以不該二選一，兩條都算。

> Mercari 台灣底層其實就是 Buyee 代購，但因為走 7-11 取貨的優惠費率，單張成本反而比 Buyee 官網低。代價是完全無法合併寄送。

---

## 安裝

```bash
cd ~/projects/ygo-sniper
make setup                    # 建 venv + 裝套件 + 產生 .env
```

`.env` 裡的 Telegram token 已經填好。eBay 憑證等審核下來再補，**空著不會壞** —— eBay 來源會自動停用，其他照常跑。

---

## 第一次使用（照這個順序）

```bash
make test                     # 1. 先確認成本模型沒算錯
.venv/bin/ygo-sniper test-telegram   # 2. 確認推播通
make breakeven                # 3. 看破口價：每條路徑最多能出多少錢
make comps                    # 4. 累積行情，先跑幾天再看訊號
.venv/bin/ygo-sniper scan --dry-run  # 5. 空跑，確認 parser 有抓到東西
```

**第 4 步不要跳過。** 沒有 comps，「折價」這個字就沒有意義 —— 你只是在買便宜的東西，不是在買被低估的東西。建議先連跑三天 `make comps` 再開始信任訊號。

---

## 日常

```bash
make daily      # 那一鍵：更新行情 → 掃描 → 推播 Telegram
.venv/bin/ygo-sniper notify-preview   # 只算不送：這一輪會推播什麼（調門檻用）
make serve      # 開 dashboard → http://127.0.0.1:8321（用法見 docs/dashboard.md）
make schedule   # 掛上 launchd，每天 09:30 自動跑
make logs       # 看今天的執行紀錄
```

**Telegram 只送兩種訊號**（清單請開 dashboard，推播只回答「現在要不要動手」）：

1. **競標急件** —— 有出價上限 ＋ 現價仍低於上限 ＋ 24 小時內結標。就是 dashboard 的「⚡ 現在就該看」那一梯隊（同一個判定，門檻在 `bidding.actionable_window_hours`）。
2. **高信心標的** —— P(值得買) > 70%，但**排除普卡**（便宜不等於撿漏；讀不出稀有度的**不算**普卡）、**排除價格還沒被競價發現的競標**（¥1 起標、0 次出價或還剩好幾天的標的，P 值是拿一個你成交不到的價格算的）。

門檻全部在 `config/settings.yaml` 的 `notify.rules`。來源健康告警（parser 壞掉、被擋）不受上面兩條規則與靜默設定影響，永遠照送 —— 「今天沒好貨」與「爬蟲壞了」外顯一樣，但只有後者需要你去修。

---

## Dashboard 怎麼用

開在 `http://127.0.0.1:8321`。分頁對應人工決策的狀態機：

**待處理 → 已詢問 / 已出價 / 湊單籃 / 觀察中 → 已買 / 略過**

每天重掃**不會**洗掉你標過的狀態。這是刻意的 —— 沒有狀態機，你第三天就會重複私訊同一個日本賣家，然後被封鎖。

### 湊單籃

這是整個工具最有用的功能，也是通知做不到的事。

把候選丟進籃子，它會即時重算攤提後的每張成本，並告訴你「分開寄要多少、合併寄省多少、其中幾張到手低於鑑定費」。以你的價位帶（¥3,000 以下），**單張買幾乎永遠不划算**，湊單是硬需求。

### 旗標對照

| 旗標 | 意思 | 你該做什麼 |
|---|---|---|
| 🔥 白撿 | 到手成本 < 鑑定費 NT$1,000 | 優先看，卡等於免費 |
| 📉 折價 | 顯著低於近期成交中位數 | 對照 comps 樣本自己判斷 |
| 🚚 運費吃掉 | 卡便宜但雜費吃掉優勢 | 只有湊單才划算 |
| ❓ 需問寄送 | 賣家沒列寄台灣選項 | 私訊問（僅 eBay） |
| 💬 可問合併 | 同賣家多筆命中 | 問能不能合併運費 |
| 🎯 可丟 offer | eBay 接受議價且上架 >30 天 | 直接出價 |
| ⚠️ 樣本少 | comps 不足 4 筆 | 別信折價率，自己看照片 |
| 🚨 可疑 | 遠低於 P25 | 注意假殼、裂殼、標題不符 |

---

## 重要限制（先知道，省得之後困惑）

**Mercari / Yahoo 標的無法議價。** 日本的「値下げ交渉」透過代購走不通，所以 🎯 可丟 offer 只會出現在 eBay 標的上。日本那邊要嘛買、要嘛不買。

**comps 有信心度分級。** `high` 是 Buyee「已售出」的真實成交價；`low` 是樣本不足。分數計算會乘上信心度係數 —— 折 60% 但只有一筆 comp，排序會落在折 25% 但有二十筆 comp 的後面。這是刻意的。

**卡片簽章比對會誤判。** 沒有卡號的標的會退回用關鍵字比對，可能把不同版本的卡歸在一起。所以 dashboard 一定會把 comps 樣本原文帶給你看 —— **最後判斷是你的，不是它的。**

---

## 壞掉的時候

Buyee 改版是遲早的事。parser 刻意不依賴任何 CSS class（用商品 URL 當錨點往上找容器），存活率高但不是免疫。

```bash
.venv/bin/ygo-sniper probe "https://buyee.jp/mercari/search?keyword=遊戯王+PSA&lang=ja"
```

會印出：抓到多少 bytes、找到幾個商品連結、解析出什麼。兩分鐘能定位是哪一層爛掉：

- **html_bytes 很小** → 被擋了，把 `fetch.delay_seconds` 調大
- **count = 0** → URL pattern 改了，看 `sources/buyee.py` 的 `_SITE_SPEC`
- **count 正常但 price 是 None** → 價格格式變了，看 `_parse_price`

調 parser 時記得 `make clean-cache`，否則會一直讀到舊的 HTML。

## 掃到 0 筆訊號的時候

先確認是哪一關卡住：

```bash
.venv/bin/ygo-sniper scan --dry-run
```

輸出會顯示 `掃描 N 筆 → 符合年代 M 筆 → 訊號 K 筆`。

- **N = 0** → 抓取問題，跑 `probe`
- **N 大但 M = 0** → 年代判定太嚴。看 `config/watchlist.yaml` 的 `era_markers`，補你熟悉的卡號前綴或時期詞
- **M 大但 K = 0** → 正常。表示今天沒有便宜貨，或 comps 還沒累積夠

---

## 調校參數

全部在 `config/settings.yaml`，程式碼裡沒有任何硬編碼費率。

最該調的三個：

```yaml
grading_fee_twd: 1000              # 你的判斷基準線
routes.buyee_consolidated.bundle_size: 5   # 你習慣一次湊幾張
scoring.discount_threshold: 0.25   # 折幾成才叫折價
```

**收到第一批 Buyee 帳單後，請務必回來校正 `intl_ship_jpy` 與 `domestic_ship_jpy`。** 現在的值是根據官方費率頁的估計，實際會因賣家所在地、包裹尺寸而不同。成本模型準了，訊號才準。

---

## 架構

```
config/          設定（費率、搜尋詞、排除字）
src/ygo_sniper/
  domain.py      資料模型
  costs.py       ★ 三路徑成本模型 + 破口反解
  parsers/       標題 → (卡名, 鑑定, 年代)
  sources/       Buyee / eBay，Protocol 介面，可單獨抽換
  comps.py       行情統計
  scoring.py     訊號判定
  store.py       SQLite（CLI 與 web 共用）
  pipeline.py    每日流程
  cli.py         指令入口
web/             dashboard（讀同一顆 db，沒有自己的業務邏輯）
tests/           成本模型是重點測試對象
```

新增一個站台只要實作 `sources/base.py` 的 `Source` protocol，pipeline 完全不用動。
