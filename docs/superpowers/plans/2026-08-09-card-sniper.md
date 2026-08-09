# 指定卡狙擊（Card Sniper）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用者登錄一張特定卡（鑑定機構＋分數＋日英文卡名＋卡號），系統**當下就去挖市場自己的成交檔案**建立這張卡的完整檔案（ARS 鑑定量、市場成交史、等待建議），之後每輪掃描在**商業過濾之前**比對所有原始 listing，一命中就推 Telegram（終身去重、不受每輪則數上限裁切），並在 dashboard 新增「🎯 狙擊」tab。

**Architecture:** 形狀類比釘選賣家（seller_watch），但**資料來源的主從關係相反**：四張新表（`card_watch`／`card_watch_sale`／`card_watch_hit`／`card_watch_evidence`，只有 CRUD 在 store.py）＋ 政策模組 `card_snipe.py`（比對、登錄、挖掘、檔案組裝——判準只有一份，CLI 與 web 共用）＋ 三個抓取模組（`ars_census.py`、`yahoo_auction_page.py`，成交檔案**重用既有的 `sources/yahoo_closed.py`**，全部走 `CachedFetcher` 這條既有 resilience boundary）＋ pipeline 掛鉤（過濾前比對＋自動加狙擊關鍵字查詢）＋ notify 規則 4 ＋ CLI 群組 `snipe` ＋ dashboard tab。

**資料來源的主從（本計劃最重要的一條）：市場的成交檔案才是資料庫，我們的庫是它的「記憶體」。**
我們自己的 `comps`／`listing_obs` 只有 181 天、31k 筆，而且是「碰巧掃到」的樣本——實測目標卡
（ARS10 魔法の筒 P4-06）在三張本地表裡**一筆都沒有**。同一張卡去打 Yahoo 落札相場，
**一個請求（0.98 秒、100 筆）就涵蓋 150 天**，而且使用者手上那兩筆成交都在裡面。
所以：**登錄當下就挖，之後定期再挖**；挖到的永久存進 `card_watch_sale`——
因為 Yahoo 的檔案只保留約 150-180 天就滾掉，我們的庫存在的意義是讓那些紀錄不隨之消失。
本地 `comps`／`listing_obs` 降級為**標明出處的補充桶**，永遠不與市場檔案混在同一個數字裡。

**Tech Stack:** Python 3.12 / stdlib sqlite3 / typer / FastAPI / 單檔 SPA（無 build step）／pytest。

---

## 0. 執行紀律（每個 task 的 subagent 都要遵守）

1. **TDD**：先寫測試 → 跑一次看它紅 → 實作 → 跑一次看它綠 → commit。每步的指令與預期輸出都寫在 task 裡。
2. 測試指令一律用 `.venv/bin/pytest`；全量回歸用 `make test`。
3. **不改任何既有過濾規則**（`config/watchlist.yaml` 的 exclude_keywords、`parsers/card.py` 的閘門一行都不動），所以**不需要跑 corpus-diff**。本功能的比對是「額外多看」，不是「改變過濾」——如果你發現自己在改 watchlist 或 parsers 的過濾邏輯，停下來，那超出本計劃範圍。
4. 測試絕不碰真實網路：conftest.py 已有 autouse fixture 靜音 Telegram 與 eBay OAuth；本計劃所有抓取測試用 fixture 檔＋FakeFetcher。
5. 檔案裡引用的行號是 commit `ac65673` 時點的錨點；若行號漂移，用引文內容定位。
6. Commit message 用繁體中文，格式照 git log 既有慣例（`feat(snipe): …`）。

**設計決策（已定案，不要在執行時重新辯論）：**

- 三個 tier 的通知政策——寧可多報不可漏報，但音量可控：
  - `exact` 🎯（卡名或卡號命中＋機構分數全符＋無現代版標記）→ Telegram，**不受** `max_items_per_run` 裁切；
  - `partial` 👀（機構相符但分數不明／不同；或**命中了現代版標記**；或分數全符但標題只明示別的卡號）→ Telegram，自己的每輪小上限 5 則；
  - `near`（卡名/卡號命中但機構不符、或完全未鑑定）→ **只入帳＋dashboard 顯示，不推播**。near 不推播不是丟棄——dashboard 狙擊分頁每一筆都看得到（誤殺靜默、雜訊可見）。
- `parse_grade` 把 `ARS10+` 折成 `10.0`（既定行為，不改）：目標設 10 時 10+ 也會以 🎯 通知——10+ 更稀，通知是對的，訊息裡看標題原文分辨。
- **現代版標記降級為 `partial`（仍然通知），不是 `near`。** 這一條在 2026-08-09 實測後改過：
  原設計把現代重印壓到 near（不通知），但實測落札檔案的 4 筆 ARS 命中裡有 2 筆是現代版
  （`プリズマティック`「世界に2枚」、`ブラックマジシャンガール 25th WCS 2023`），
  而**目標卡的兩筆真成交標題裡根本沒有卡號 P4-06**（只有 `魔法の筒 Magic Cylinder ウルトラ`）。
  也就是說：真標的靠的是「卡名＋機構分數」，與現代版的差別只在那幾個標記詞。
  一份手寫的標記詞表只要多寫一個詞，就會靜默地把真標的降到不通知——所以標記詞只把它
  降到 👀（照樣推播、訊息上註明「疑似現代版」），讓使用者一眼分辨。**標記詞永遠不會讓
  一筆命中變成不通知。**
- 現代版標記（fold 後比對，中点／半形自動吸收）：`プリズマティック`、`プリシク`、
  `ラッシュデュエル`、`RUSH DUEL`、`25th`、`WCS`、`クォーターセンチュリー`、
  `QUARTER CENTURY`、`レアリティコレクション`。全部來自實際觀測到的標題，不是憑空想的。
- 通知去重鍵是 `"{watch_id}:{listing_key}"`（獨立命名空間，不與 signals 的 key 相撞），rule 名 `card_snipe`，終身一次（同 seller_new）。
- 使用者提供的歷史 URL **入庫當下就抓快照**：Yahoo 已結束頁約 120 天會刪（實證下界 74 天）。抓不到就存 `status='unverifiable'` 大聲標記——讀不到 ≠ 不存在。
- census 只支援 ARS（頁面 server-rendered、無 JS，2026-08-09 實測）；PSA pop 誠實說「未支援」。
- **成交檔案的查詢只打卡名，絕不把鑑定詞加進伺服器端查詢。** 實測 `魔法の筒 PSA` 只回 5 筆，
  雖然目標卡的三筆都在裡面，但那純屬僥倖——賣家剛好把 `PSA` 塞進標題。伺服器端多一個詞
  ＝ AND 過濾 ＝ 靜默誤殺（只寫「ARS鑑定10」的賣家就完全看不到）。正解是**查卡名拿全量
  （126 筆／2 請求），在本地用我們自己的 tier 比對**。
- 本地歷史（dossier 的補充桶）**現場重跑標題比對**，不信 `comps.card_name`／`set_code` 欄位（實測兩筆相關 comps 的 card_name 都是空）。呈現逐筆列出、競標／定價分開標示，**不做任何跨筆的聚合統計**——避開混池。
- **市場檔案的成交型態必須逐筆標明。** 落札檔案裡 100 筆有 22 筆是 Yahoo!フリマ（定價成交），
  而 Yahoo 把定價成交的 `bidCount` 也記成 `1`——那不是「有一個人出價」，是佔位值。
  型態一律看 `isFixedPrice`／`isFleamarketItem`，不看 bidCount（CLAUDE.md 第三節第七項）。

**外部頁面實測事實（2026-08-09，計劃內程式碼與測試期望值的依據）：**

| 事實 | 值 |
|---|---|
| ARS census 頁 | HTTP 200、11.9KB、無 JS；`<div class="grade-entry" data-grade="10"><span>Grade 10</span><span>5（0）</span></div>`；`鑑定総数 TOTAL GRADING 11` |
| ARS 卡名搜尋 | `https://ars-grading.com/grading/searchName?name={urlencode}&page=1`，「魔法の筒」6 件，每筆 `<a href="/grading/searchNameDetail?id=…">` 內 5 個 `<span>`：卡名/收錄/型番/稀有度/年份 |
| Yahoo 結標頁 | `<script id="__NEXT_DATA__" type="application/json">` → `props.pageProps.initialState.item.detail.item`；n1235105710：price 6350、endTime `2026-07-01T22:53:03+09:00`、bids 15、status `closed`、seller.aucUserId `AiUkMq1pEUfNxvPeCv5PnfGpsFLrx`、displayName `Natural Cards` |
| **Yahoo 落札相場（成交檔案，本計劃的主要資料源）** | `https://auctions.yahoo.co.jp/closedsearch/closedsearch?p={keyword}&n=100`；HTTP 200、1.39MB、**0.98 秒**、無 JS；資料在 `__NEXT_DATA__`。實測 `魔法の筒`：**一頁 100 筆、涵蓋 2026-03-11 → 08-08（150 天）**，第二頁再往回到 179 天（126 筆／2 請求後檔案翻完） |
| 落札檔案單筆欄位 | `auctionId`／`title`／`price`(落札價)／`endTime`／`bidCount`／`isFixedPrice`／`isFleamarketItem`／`seller.userId`／`seller.goodRating`／`imageUrl`。**賣家在巢狀 `seller.userId`**（頂層 `sellerId` 是 0 筆，別找錯） |
| 落札檔案的 4 筆 ARS 命中（查 `魔法の筒`） | `2026-07-01 ¥6,350`（15 出價）✅目標卡、`2026-05-27 ¥7,750`（10 出價）✅目標卡——**正是使用者提供的那兩個 URL，且 `seller.userId` = `AiUkMq1pEUfNxvPeCv5PnfGpsFLrx` 與使用者給的賣家頁完全一致**；另兩筆是現代版假陽性：`2026-07-08 ¥4,600`（プリズマティック「世界に2枚」）、`2026-06-03 ¥168,150`（ブラックマジシャンガール 25th WCS 2023） |
| 成交型態分佈 | 100 筆中 78 筆競標、22 筆フリマ（定價）；**フリマ 的 `bidCount` 全是 1（佔位值，不是真出價數）**；既有 `bidCount>=1` 守門對這 100 筆丟棄 0 筆 |
| 其他平台成交檔案 | Mercari（Buyee 鏡像）`HTTP 202 + WAF challenge`、Mercari 原站 0 個商品 ID（RSC 空殼）、eBay `/sch/` **403 Akamai**（不帶 `LH_Sold` 也一樣 403）→ **無 JS 一律拿不到，計劃不打這三條** |
| 解析行為 | `parse_grade('魔法の筒 ARS鑑定品 遊戯王')` → `(ARS, None)`；`parse_grade('ARS9 …')` → `(ARS, 9.0)`；`extract_title_codes(' P4-06 ')` → `['P4-6']`；`fold('マジック・シリンダー') in fold('ARS10 マジックシリンダー 初期')` → True |
| domain | `Site.BUYEE_YAHOO.value == 'buyee_yahoo'`、`Currency.JPY.value == 'JPY'`；`NotifyRules` 是 dataclass、有 `from_config` |
| 過濾 | `parse_card('【ARS10】魔法の筒 P4-06 ポケモンカード', wl)` → `is_candidate` = `(False, '排除字 ポケモン')`（拿來證明「過濾前比對」用） |
| 卡名主檔 | `DEFAULT_MASTER_PATH` 是**str 相對路徑** `data/cards_1998_2004.json`；魔法の筒：aliases `["マジック・シリンダー"]`、set_codes 含 `P4-6`（無前導零） |

---

## File Structure

```
建立  src/ygo_sniper/card_snipe.py          # 政策：比對 tier、登錄、市場檔案挖掘、dossier、通知脈絡
建立  src/ygo_sniper/ars_census.py          # ARS 鑑定量抓取＋解析（純函式＋fetcher 注入）
建立  src/ygo_sniper/yahoo_auction_page.py  # Yahoo 商品頁（含結標）__NEXT_DATA__ 快照
重用  src/ygo_sniper/sources/yahoo_closed.py # 落札檔案來源（不新寫 parser；已含 seller.userId 抽取）
重用  src/ygo_sniper/refill.py:286 _sold_search # 吃任意關鍵字的成交查詢，不外拋、帶 health
修改  src/ygo_sniper/store.py               # _SCHEMA 四張新表＋CRUD（政策不進 SQL）
修改  src/ygo_sniper/pipeline.py            # 過濾前比對掛鉤＋狙擊查詢注入＋near 回收＋通知脈絡
修改  src/ygo_sniper/notify_rules.py        # RULE_CARD_SNIPE、Outcome.card_snipe、evaluate、去重與上限
修改  src/ygo_sniper/notify.py              # format_card_snipe ＋ render 分派
修改  src/ygo_sniper/cli.py                 # snipe add/list/report/remove 群組
修改  web/app.py                            # GET/POST /api/snipe（與 CLI 共用 card_snipe 政策）
修改  web/static/index.html                 # 🎯 狙擊 tab（data-view="snipe"）＋ snipe-view ＋ JS
修改  CLAUDE.md、docs/dashboard.md          # 指令與動線文件
新增  tests/test_card_snipe.py              # 比對＋store＋pipeline 掛鉤＋CLI（43 tests）
新增  tests/test_card_snipe_mine.py         # 市場成交檔案挖掘（6 tests）
新增  tests/test_ars_census.py
新增  tests/test_yahoo_auction_page.py
新增  tests/test_card_snipe_notify.py
新增  tests/test_card_snipe_web.py
新增  tests/fixtures/ars_census_p4_06.html / ars_search_magic_cylinder.html / yahoo_closed_n1235105710.html
```

---

### Task 0: 抓取測試 fixture（生產路徑：無 JS 的 HTTP GET）

**Files:**
- Create: `tests/fixtures/ars_census_p4_06.html`
- Create: `tests/fixtures/ars_search_magic_cylinder.html`
- Create: `tests/fixtures/yahoo_closed_n1235105710.html`
- Modify: `tests/fixtures/RECON.md`（追加出處紀錄）

- [ ] **Step 1: 用 curl 抓三個 fixture（與生產同路徑：不執行 JS）**

```bash
cd /Users/jim/projects/ygo-sniper
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
curl -sS -A "$UA" "https://ars-grading.com/grading/searchNameDetail?id=001202208090020007" -o tests/fixtures/ars_census_p4_06.html
curl -sS -A "$UA" "https://ars-grading.com/grading/searchName?name=%E9%AD%94%E6%B3%95%E3%81%AE%E7%AD%92&page=1" -o tests/fixtures/ars_search_magic_cylinder.html
curl -sS -A "$UA" "https://auctions.yahoo.co.jp/jp/auction/n1235105710" -o tests/fixtures/yahoo_closed_n1235105710.html
```

若網路失敗，改用本 session 已抓好的副本（同樣是 curl 無 JS 抓的）：

```bash
SCRATCH=/private/tmp/claude-501/-Users-jim-projects-ygo-sniper/be36422f-fbb7-4bc5-8b84-cf5ee1e2c006/scratchpad
cp $SCRATCH/ars.html        tests/fixtures/ars_census_p4_06.html
cp $SCRATCH/ars_search.html tests/fixtures/ars_search_magic_cylinder.html
cp $SCRATCH/yahoo_n.html    tests/fixtures/yahoo_closed_n1235105710.html
```

- [ ] **Step 2: 驗證抓到的是真頁面不是被擋頁**

```bash
grep -c 'grade-entry' tests/fixtures/ars_census_p4_06.html          # 預期 >= 12
grep -c 'searchNameDetail' tests/fixtures/ars_search_magic_cylinder.html  # 預期 >= 6
grep -c '__NEXT_DATA__' tests/fixtures/yahoo_closed_n1235105710.html      # 預期 1
```

任何一個是 0 → 停止，回報「fixture 抓取失敗（被擋或改版）」，不要繼續。

- [ ] **Step 3: 在 `tests/fixtures/RECON.md` 末尾追加**

```markdown
## card sniper（2026-08-09）
- `ars_census_p4_06.html` — https://ars-grading.com/grading/searchNameDetail?id=001202208090020007，curl + browser UA（無 JS，同生產路徑）。魔法の筒 P4-06 的鑑定量頁：Grade 9=5、10=5、10+=1、総数 11。
- `ars_search_magic_cylinder.html` — searchName?name=魔法の筒&page=1，同上。6 件結果。
- `yahoo_closed_n1235105710.html` — 已結束拍賣頁（2026-07-01 結標 ¥6,350）。資料在 __NEXT_DATA__ JSON。
```

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/
git commit -m "test(snipe): 狙擊功能的三個 fixture（ARS census／搜尋／Yahoo 結標頁，無 JS 生產路徑抓取）"
```

---

### Task 1: store 四張新表＋CRUD

> **執行期修訂（2026-08-09，審查後）**：Task 1 完成後由獨立 reviewer 找出五個正確性問題，已修正。**實際落地的 API 與下面的程式碼區塊有五處差異**，後面的 task 一律以實際程式碼為準：
> 1. `prune_card_watch_near_hits(days)` → **`prune_card_watch_hits(days, *, tier)`**（tier 必填，保留政策不進 SQL）
> 2. `update_card_watch_census(...)` 現在**回傳 `bool`**（False ＝ 那個 watch 不存在，不再靜默無作用）
> 3. `upsert_card_watch_sale` 的「是不是新紀錄」改成**先查存在性**，不再比較 `first_mined_at == stamp`（呼叫端傳固定 `now=` 時會高報新成交）
> 4. 新增模組級常數 **`store.CARD_SNIPE_RULE`**，`list_card_watch_hits` 用參數綁定帶入，SQL 裡不留 rule 字面量；一致性由 Task 6 的 `test_store_and_notify_rules_agree_on_the_rule_name` 強制
> 5. `card_watch_evidence.sold_at` 補上時間基準宣告（**一律存 UTC**，與 `card_watch_sale.sold_at` 同基準）——Task 5 寫入時要 `to_utc_iso()`

**Files:**
- Modify: `src/ygo_sniper/store.py`（`_SCHEMA` 內 `seller_watch` 區塊之後；CRUD 加在 seller_watch CRUD 之後，約 :1408 附近）
- Test: `tests/test_card_snipe.py`（新檔，先放 store 測試）

store.py 頂部 import 已有 `json`、`sqlite3`、`datetime(UTC/datetime/timedelta)`、`Path`，不需要加。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_card_snipe.py`：

```python
"""指定卡狙擊：store CRUD、比對 tier、pipeline 掛鉤、CLI。"""
from __future__ import annotations

import json

import pytest

from ygo_sniper.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


WATCH_KW = dict(
    grader="ARS", grade=10.0, grade_label="10",
    name_ja="魔法の筒", name_en="Magic Cylinder",
    aliases=["マジック・シリンダー"], code_raw="P4-06", code_norm="P4-6",
)


class TestCardWatchStore:
    def test_insert_and_list_roundtrip(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        rows = store.list_card_watch(active_only=True)
        assert len(rows) == 1 and rows[0]["id"] == wid
        w = rows[0]
        assert w["grader"] == "ARS" and w["grade"] == 10.0 and w["grade_label"] == "10"
        assert w["code_norm"] == "P4-6"
        assert json.loads(w["aliases"]) == ["マジック・シリンダー"]
        assert w["active"] == 1 and w["added_at"]

    def test_deactivate_is_soft_delete(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        assert store.deactivate_card_watch(wid) is True
        assert store.deactivate_card_watch(wid) is False          # 已經不在了
        assert store.list_card_watch(active_only=True) == []
        rows = store.list_card_watch(active_only=False)
        assert rows[0]["active"] == 0 and rows[0]["removed_at"]

    def test_census_update(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        store.update_card_watch_census(
            wid, census_url="https://ars-grading.com/x",
            census_json='{"9": 5, "10": 5, "10+": 1}', census_total=11,
        )
        w = store.get_card_watch(wid)
        assert json.loads(w["census_json"])["10+"] == 1
        assert w["census_total"] == 11 and w["census_fetched_at"]

    def test_hit_upsert_is_idempotent_and_updates_last_seen(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        kw = dict(tier="exact", title="t", url="u", site="buyee_yahoo",
                  seller_id="s1", price_native=6350.0, currency="JPY")
        store.upsert_card_watch_hit(wid, "buyee_yahoo:x1", now="2026-08-09T01:00:00+00:00", **kw)
        store.upsert_card_watch_hit(wid, "buyee_yahoo:x1", now="2026-08-09T02:00:00+00:00", **kw)
        hits = store.list_card_watch_hits(watch_id=wid)
        assert len(hits) == 1
        assert hits[0]["first_seen"] == "2026-08-09T01:00:00+00:00"
        assert hits[0]["last_seen"] == "2026-08-09T02:00:00+00:00"
        assert hits[0]["sent_at"] is None                          # notify_log join，還沒送過

    def test_hit_sent_at_comes_from_notify_log(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        store.upsert_card_watch_hit(wid, "buyee_yahoo:x1", tier="exact", title="t",
                                    url="u", site="buyee_yahoo", seller_id="",
                                    price_native=None, currency="")
        store.mark_rule_notified([(f"{wid}:buyee_yahoo:x1", "card_snipe")])
        hits = store.list_card_watch_hits(watch_id=wid)
        assert hits[0]["sent_at"] is not None

    def test_prune_only_touches_old_near(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        old = "2020-01-01T00:00:00+00:00"
        for key, tier, now in (("a:1", "near", old), ("a:2", "exact", old), ("a:3", "near", None)):
            store.upsert_card_watch_hit(wid, key, tier=tier, title="t", url="u",
                                        site="a", seller_id="", price_native=None,
                                        currency="", now=now)
        assert store.prune_card_watch_near_hits(90) == 1           # 只清舊的 near
        left = {h["listing_key"] for h in store.list_card_watch_hits(watch_id=wid)}
        assert left == {"a:2", "a:3"}

    def test_evidence_upsert_and_list(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        store.upsert_card_watch_evidence(
            wid, "https://example.test/a", status="ok", title="t",
            price_native=6350.0, sold_at="2026-07-01T22:53:03+09:00",
            bids=15, seller_id="S", seller_name="Natural Cards",
        )
        store.upsert_card_watch_evidence(wid, "https://example.test/a", status="ok",
                                         title="t2", price_native=6350.0)
        ev = store.list_card_watch_evidence(wid)
        assert len(ev) == 1 and ev[0]["title"] == "t2"             # 同 URL 更新不重複

    def test_sale_upsert_reports_new_then_not_new(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        kw = dict(tier="exact", title="【ARS10】魔法の筒", url="u", site="buyee_yahoo",
                  seller_id="AiUkMq1pEUfNxvPeCv5PnfGpsFLrx", price_native=6350.0,
                  currency="JPY", sold_at="2026-07-01T13:53:03+00:00",
                  bid_count=15, sale_kind="auction")
        assert store.upsert_card_watch_sale(wid, "buyee_yahoo:n1", **kw) is True
        assert store.upsert_card_watch_sale(wid, "buyee_yahoo:n1", **kw) is False
        sales = store.list_card_watch_sales(wid)
        assert len(sales) == 1 and sales[0]["bid_count"] == 15
        assert sales[0]["sale_kind"] == "auction"

    def test_sale_seller_id_is_filled_never_wiped(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        base = dict(tier="exact", title="t", url="u", site="buyee_yahoo",
                    price_native=1.0, currency="JPY", sold_at="2026-07-01T00:00:00+00:00",
                    bid_count=1, sale_kind="auction")
        store.upsert_card_watch_sale(wid, "s:1", seller_id="SELLER", **base)
        store.upsert_card_watch_sale(wid, "s:1", seller_id="", **base)   # 之後挖到沒賣家
        assert store.list_card_watch_sales(wid)[0]["seller_id"] == "SELLER"

    def test_title_rows_accessors_exist(self, store):
        assert store.comps_title_rows() == []
        assert store.listing_obs_title_rows() == []
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_card_snipe.py -x
```

預期：`AttributeError: 'Store' object has no attribute 'insert_card_watch'`。

- [ ] **Step 3: 實作 schema**

在 `src/ygo_sniper/store.py` 的 `_SCHEMA` 字串內、`CREATE INDEX IF NOT EXISTS idx_seller_watch_batch …;` 那行之後追加（新表走 `IF NOT EXISTS`，`Store.__init__` 每次 executescript，舊 db 自動長出新表，**不需要** migration dict）：

```sql

-- 指定卡狙擊（政策層只有 card_snipe.py 一份；這裡只有 CRUD）。
-- grade 存 float（與 parse_grade 同基準：ARS 10+ 折成 10.0）；grade_label 存
-- 使用者輸入原樣（'10'／'10+'），顯示與 census 查表用——兩欄職責不同，不互相推導。
CREATE TABLE IF NOT EXISTS card_watch (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    grader        TEXT NOT NULL,
    grade         REAL NOT NULL,
    grade_label   TEXT NOT NULL,
    name_ja       TEXT NOT NULL,
    name_en       TEXT DEFAULT '',
    aliases       TEXT DEFAULT '[]',
    code_raw      TEXT DEFAULT '',
    code_norm     TEXT DEFAULT '',
    census_url    TEXT DEFAULT '',
    census_json   TEXT DEFAULT '',
    census_total  INTEGER,
    census_fetched_at TEXT,
    note          TEXT DEFAULT '',
    active        INTEGER DEFAULT 1,
    added_at      TEXT,
    removed_at    TEXT
);

-- 狙擊命中帳：每 (watch, listing) 一列，重掃只更新 last_seen（冪等）。
-- tier 存最新判定（標題被賣家改掉時允許升降級）。
CREATE TABLE IF NOT EXISTS card_watch_hit (
    watch_id      INTEGER NOT NULL,
    listing_key   TEXT NOT NULL,
    tier          TEXT NOT NULL,
    title         TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    site          TEXT DEFAULT '',
    seller_id     TEXT DEFAULT '',
    price_native  REAL,
    currency      TEXT DEFAULT '',
    end_time      TEXT DEFAULT '',
    first_seen    TEXT,
    last_seen     TEXT,
    PRIMARY KEY (watch_id, listing_key)
);
CREATE INDEX IF NOT EXISTS idx_card_watch_hit_tier ON card_watch_hit(watch_id, tier);

-- 市場成交檔案（從 Yahoo 落札相場挖回來的「別人賣掉了」）。
-- ⚠️ 與 card_watch_hit 是**兩本帳，永遠不合併**：這裡是「已成交」（買不到了，
--    是行情），hit 是「在架中」（買得到，是機會）。分母與用途都不同。
-- ⚠️ sale_kind 必須逐筆存：フリマ 定價成交的 bidCount 也是 1（佔位值），
--    競標結標價（買家喊上去）與定價成交（賣家開的）不是同一把尺（CLAUDE.md 第三節第七項）。
-- ⚠️ 這張表是市場檔案的**永久記憶體**：Yahoo 只保留約 150-180 天，挖到就留著。
CREATE TABLE IF NOT EXISTS card_watch_sale (
    watch_id      INTEGER NOT NULL,
    sale_key      TEXT NOT NULL,        -- '{site}:{external_id}'
    tier          TEXT NOT NULL,
    title         TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    origin_url    TEXT DEFAULT '',
    site          TEXT DEFAULT '',
    seller_id     TEXT DEFAULT '',
    price_native  REAL,
    currency      TEXT DEFAULT 'JPY',
    sold_at       TEXT DEFAULT '',      -- 真實落札時刻（UTC ISO），不是入庫時間
    bid_count     INTEGER,
    sale_kind     TEXT DEFAULT 'unknown',  -- auction / fixed / unknown
    source        TEXT DEFAULT '',      -- 哪條管道挖到的
    first_mined_at TEXT,
    last_mined_at  TEXT,
    PRIMARY KEY (watch_id, sale_key)
);
CREATE INDEX IF NOT EXISTS idx_card_watch_sale_sold ON card_watch_sale(watch_id, sold_at);

-- 使用者提供的歷史證據（結標頁快照）。抓不到也入列（status='unverifiable'）
-- 並大聲標記——讀不到 ≠ 不存在（CLAUDE.md 第五節）。
CREATE TABLE IF NOT EXISTS card_watch_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id      INTEGER NOT NULL,
    url           TEXT NOT NULL,
    status        TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    price_native  REAL,
    currency      TEXT DEFAULT 'JPY',
    sold_at       TEXT DEFAULT '',
    bids          INTEGER,
    seller_id     TEXT DEFAULT '',
    seller_name   TEXT DEFAULT '',
    site          TEXT DEFAULT 'buyee_yahoo',
    note          TEXT DEFAULT '',
    fetched_at    TEXT,
    UNIQUE (watch_id, url)
);
```

- [ ] **Step 4: 實作 CRUD**

在 `store.py` 的 `mark_seller_watch_scanned` 方法（約 :1387-1408）結束之後、下一個區塊之前，加入：

```python
    # ------------------------------------------------------------------
    # 指定卡狙擊（card_watch）。**這一層只做 CRUD**：tier 判準、通知政策、
    # dossier 組裝全部在 `card_snipe.py`——與 seller_watch 同一個立場。
    def insert_card_watch(
        self, *, grader: str, grade: float, grade_label: str, name_ja: str,
        name_en: str = "", aliases: list[str] | None = None,
        code_raw: str = "", code_norm: str = "", note: str = "",
        now: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO card_watch
                   (grader, grade, grade_label, name_ja, name_en, aliases,
                    code_raw, code_norm, note, active, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (grader, float(grade), grade_label, name_ja, name_en,
                 json.dumps(aliases or [], ensure_ascii=False),
                 code_raw, code_norm, note, now or _now_iso()),
            )
            return int(cur.lastrowid)

    def list_card_watch(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM card_watch"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY id"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q).fetchall()]

    def get_card_watch(self, watch_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM card_watch WHERE id = ?", (int(watch_id),)
            ).fetchone()
        return dict(r) if r else None

    def deactivate_card_watch(self, watch_id: int, *, now: str | None = None) -> bool:
        """軟刪除（命中帳與證據留著——「當時為什麼登錄它」是之後的判斷依據）。"""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE card_watch SET active = 0, removed_at = ? "
                "WHERE id = ? AND active = 1",
                (now or _now_iso(), int(watch_id)),
            )
            return cur.rowcount > 0

    def update_card_watch_census(
        self, watch_id: int, *, census_url: str, census_json: str,
        census_total: int | None = None, now: str | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE card_watch SET census_url = ?, census_json = ?,
                   census_total = ?, census_fetched_at = ? WHERE id = ?""",
                (census_url, census_json, census_total,
                 now or _now_iso(), int(watch_id)),
            )

    def upsert_card_watch_hit(
        self, watch_id: int, listing_key: str, *, tier: str, title: str, url: str,
        site: str, seller_id: str, price_native: float | None, currency: str,
        end_time: str = "", now: str | None = None,
    ) -> None:
        stamp = now or _now_iso()
        with self._conn() as c:
            c.execute(
                """INSERT INTO card_watch_hit
                   (watch_id, listing_key, tier, title, url, site, seller_id,
                    price_native, currency, end_time, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(watch_id, listing_key) DO UPDATE SET
                     tier = excluded.tier,
                     title = excluded.title,
                     url = excluded.url,
                     price_native = excluded.price_native,
                     currency = excluded.currency,
                     end_time = excluded.end_time,
                     last_seen = excluded.last_seen,
                     -- seller_id 只補不抹：搜尋頁解析器抓不到賣家（sources/buyee.py
                     -- :315），只有賣家頁列舉補得上。無條件覆寫會讓例行掃描把挖回來
                     -- 的賣家抹成空字串——listing_obs 踩過這個坑（CLAUDE.md 第五節）。
                     seller_id = CASE WHEN excluded.seller_id != ''
                                      THEN excluded.seller_id
                                      ELSE card_watch_hit.seller_id END""",
                (int(watch_id), listing_key, tier, title, url, site, seller_id,
                 price_native, currency, end_time, stamp, stamp),
            )

    def list_card_watch_hits(
        self, *, watch_id: int | None = None,
        tiers: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """附 `sent_at`（notify_log 的 card_snipe 帳；沒送過為 NULL）。
        去重帳只有 notify_log 一本——hit 表不自己記 notified，兩本帳必歧。"""
        q = (
            "SELECT h.*, n.sent_at FROM card_watch_hit h "
            "LEFT JOIN notify_log n ON n.rule = 'card_snipe' "
            "AND n.key = CAST(h.watch_id AS TEXT) || ':' || h.listing_key "
            "WHERE 1=1"
        )
        params: list[Any] = []
        if watch_id is not None:
            q += " AND h.watch_id = ?"
            params.append(int(watch_id))
        if tiers:
            q += f" AND h.tier IN ({','.join('?' * len(tiers))})"
            params.extend(tiers)
        q += " ORDER BY h.last_seen DESC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    def prune_card_watch_near_hits(self, days: int) -> int:
        """near tier 會被現代重印與未鑑定貨洗出大量列，回收舊列；
        exact／partial 永久保留（那是這張卡的出現史）。"""
        if days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM card_watch_hit WHERE tier = 'near' AND last_seen < ?",
                (cutoff,),
            )
            return cur.rowcount

    def upsert_card_watch_sale(
        self, watch_id: int, sale_key: str, *, tier: str, title: str, url: str,
        site: str, seller_id: str, price_native: float | None, currency: str,
        sold_at: str, bid_count: int | None, sale_kind: str,
        origin_url: str = "", source: str = "", now: str | None = None,
    ) -> bool:
        """市場成交一筆（冪等）。回傳 True ＝ 這次是**新**紀錄（之前沒挖到過）。

        `sold_at`／`price_native` 用 excluded 覆寫（重挖到同一筆時以最新解析為準），
        但 `first_mined_at` 保留——「我們什麼時候第一次知道這筆」是檔案完整度的證據。
        `seller_id` 只補不抹（同 hit 表的立場）。
        """
        stamp = now or _now_iso()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO card_watch_sale
                   (watch_id, sale_key, tier, title, url, origin_url, site,
                    seller_id, price_native, currency, sold_at, bid_count,
                    sale_kind, source, first_mined_at, last_mined_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(watch_id, sale_key) DO UPDATE SET
                     tier = excluded.tier,
                     title = excluded.title,
                     url = excluded.url,
                     origin_url = excluded.origin_url,
                     price_native = excluded.price_native,
                     currency = excluded.currency,
                     sold_at = excluded.sold_at,
                     bid_count = excluded.bid_count,
                     sale_kind = excluded.sale_kind,
                     source = excluded.source,
                     last_mined_at = excluded.last_mined_at,
                     seller_id = CASE WHEN excluded.seller_id != ''
                                      THEN excluded.seller_id
                                      ELSE card_watch_sale.seller_id END""",
                (int(watch_id), sale_key, tier, title, url, origin_url, site,
                 seller_id, price_native, currency, sold_at, bid_count,
                 sale_kind, source, stamp, stamp),
            )
            # rowcount 在 upsert 的 UPDATE 分支也是 1，所以用 first_mined_at 判新舊
            row = c.execute(
                "SELECT first_mined_at FROM card_watch_sale "
                "WHERE watch_id = ? AND sale_key = ?",
                (int(watch_id), sale_key),
            ).fetchone()
        return bool(row) and row["first_mined_at"] == stamp

    def list_card_watch_sales(
        self, watch_id: int, *, tiers: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM card_watch_sale WHERE watch_id = ?"
        params: list[Any] = [int(watch_id)]
        if tiers:
            q += f" AND tier IN ({','.join('?' * len(tiers))})"
            params.extend(tiers)
        q += " ORDER BY sold_at DESC"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    def upsert_card_watch_evidence(
        self, watch_id: int, url: str, *, status: str, title: str = "",
        price_native: float | None = None, currency: str = "JPY",
        sold_at: str = "", bids: int | None = None, seller_id: str = "",
        seller_name: str = "", site: str = "buyee_yahoo", note: str = "",
        now: str | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO card_watch_evidence
                   (watch_id, url, status, title, price_native, currency, sold_at,
                    bids, seller_id, seller_name, site, note, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(watch_id, url) DO UPDATE SET
                     status = excluded.status, title = excluded.title,
                     price_native = excluded.price_native,
                     currency = excluded.currency,
                     sold_at = excluded.sold_at, bids = excluded.bids,
                     seller_id = excluded.seller_id,
                     seller_name = excluded.seller_name,
                     site = excluded.site, note = excluded.note,
                     fetched_at = excluded.fetched_at""",
                (int(watch_id), url, status, title, price_native, currency,
                 sold_at, bids, seller_id, seller_name, site, note,
                 now or _now_iso()),
            )

    def list_card_watch_evidence(self, watch_id: int) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM card_watch_evidence WHERE watch_id = ? "
                "ORDER BY sold_at",
                (int(watch_id),),
            ).fetchall()]

    # 狙擊檔案（dossier）重建歷史用的標題掃描。欄位挑過、不整列撈；
    # card_name／set_code 欄位不可信（實測相關列全空），一律現場重比對標題。
    def comps_title_rows(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT title, price_native, currency, price_twd, sold_at,
                          sold_at_is_ingest, site, seller_id, sale_kind, url
                   FROM comps WHERE dup_of_id IS NULL"""
            ).fetchall()]

    def listing_obs_title_rows(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT key, title, price_native, currency, site, seller_id,
                          url, first_seen, last_seen, disappeared_at
                   FROM listing_obs"""
            ).fetchall()]
```

- [ ] **Step 5: 跑測試看它綠**

```bash
.venv/bin/pytest tests/test_card_snipe.py -x
```

預期：`10 passed`。

- [ ] **Step 6: 既有測試沒被 schema 弄壞**

```bash
.venv/bin/pytest tests/ -k "store or seller_watch"
```

預期：全綠。

- [ ] **Step 7: Commit**

```bash
git add src/ygo_sniper/store.py tests/test_card_snipe.py
git commit -m "feat(snipe): card_watch 四張表與 CRUD（成交檔案與在架命中分兩本帳、seller_id 只補不抹）"
```

---

### Task 2: 比對核心 `card_snipe.py`（tier 判定＋掃描掛鉤函式＋狙擊查詢）

> **執行期修訂（2026-08-09，審查後）——新增一條機構競合分支，理由是實測到的誤殺**：
>
> `classify` 原本在 `parse_grade` 回的機構與目標不符時直接回 `near`（不推播）。用 repo 真實語料實測發現這會**靜默漏掉 7.9% 的目標卡**：3,239 個真實標題裡，998 筆含 `ARS＋分數` token，其中 **79 筆被 `parse_grade` 判成 PSA**——因為日本賣家慣用「ARS 鑑定品，相當於 PSA10 **以上**」的宣稱寫法，而 PSA 的 pattern 在 `_GRADE_PATTERNS` 裡排在 ARS 前面，`以上` 又不在 `parsers/grade.py` 的 `_CLAIM_SUFFIX`（只有 `相当|相當|並み|並|級|クラス|レベル`）。實例：
> ```
> ARS9 マジシャン・オブ・ブラックカオス 初期 ウルトラレア UR 遊戯王 極美品　PSA9以上相当
> 【ARS7】ブラックマジシャン　初期ウルトラレア　vol.1 PSA7以上
> ```
> **修法**：機構不符時，先看標題自己有沒有寫目標機構的 token（`(?<![A-Za-z0-9])ARS\s*(?:鑑定)?\s*(?:10\+*|[0-9](?:\.5)?)(?!\d)`，用 lookaround 不用 `\b`），有就回 `partial`（👀 照樣推播＋理由說明競合），沒有才回 `near`。
> **不升到 `exact`**：分數的權威只有 `parse_grade` 一份，這裡不另立第二把尺（CLAUDE.md 第三節）。
> **刻意不改 `parsers/grade.py`**：那是全域過濾規則，改動需要跑全語料雙向驗證，且會改變主管線對那 79 筆的既有判定——超出本功能範圍。這裡只讓**狙擊**更寬容。
>
> 同批修正還有：docstring 的判定順序改成與實作一致（註解描述意圖、code 才是行為）；`aliases` 接受 str 與 list 兩種形態且解析失敗要出聲（原本靜默吞掉＝別名整組消失）；`_MODERN_FOLDED` 過濾空字串（空字串 `in` 任何字串恆真＝所有 exact 靜默降級）；`from_row` 對 grader 做 `.strip().upper()`；刪掉一條零覆蓋的重複測試（兩個「真實成交標題」逐位元組相同，同賣家重複刊登）。
>
> ⚠️ **順帶發現一個既有問題（不在本計劃範圍，未修）**：同樣那 79 筆在**主管線**也會被貼上 PSA 標籤，於是拿去跟 PSA 的 comps 比價——ARS 與 PSA 的價格水準不同，這是一次混源比較（CLAUDE.md 第三節）。修它要走第一節的全語料雙向驗證協定，應該獨立成案。

**Files:**
- Create: `src/ygo_sniper/card_snipe.py`
- Test: `tests/test_card_snipe.py`（追加）

- [ ] **Step 1: 追加失敗測試到 `tests/test_card_snipe.py`**

```python
from ygo_sniper.card_snipe import (
    TIER_EXACT,
    TIER_NEAR,
    TIER_PARTIAL,
    WatchMatcher,
    load_matchers,
    match_tier,
    observe_listings,
    scan_queries,
)

WATCH_ROW = {
    "id": 1, "grader": "ARS", "grade": 10.0, "grade_label": "10",
    "name_ja": "魔法の筒", "name_en": "Magic Cylinder",
    "aliases": '["マジック・シリンダー"]', "code_raw": "P4-06", "code_norm": "P4-6",
}


@pytest.fixture
def matcher():
    return WatchMatcher.from_row(WATCH_ROW)


class TestMatchTier:
    def test_exact_on_the_real_sold_title(self, matcher):
        # 2026-07-01 ¥6,350 真實結標標題
        t = "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品"
        assert match_tier(matcher, t) == TIER_EXACT

    def test_exact_via_code_without_name(self, matcher):
        assert match_tier(matcher, "遊戯王 ARS10 P4-06 ウルトラ") == TIER_EXACT

    def test_exact_via_katakana_alias_without_nakaten(self, matcher):
        # 中点なし也要中：fold 會把中点丟掉、片假名折平假名
        assert match_tier(matcher, "ARS10 マジックシリンダー 初期") == TIER_EXACT

    def test_ars10_plus_is_exact_by_design(self, matcher):
        # parse_grade 把 10+ 折成 10.0（既定行為）；10+ 全球只有 1 張、比 10 更稀，通知是對的
        assert match_tier(matcher, "ARS10+ 魔法の筒 P4-06") == TIER_EXACT

    def test_the_other_real_sold_title_is_exact(self, matcher):
        # 2026-05-27 ¥7,750 真實結標標題（與上一筆同賣家、同樣沒有卡號）
        t = "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品"
        assert match_tier(matcher, t) == TIER_EXACT

    def test_rush_duel_same_name_is_demoted_to_partial_still_notified(self, matcher):
        # 現代版只降到 👀（照樣推播）——降到不推播的話，詞表寫錯一個字就靜默漏標的
        t = "【ARS10】世界に1枚 魔法の筒 Magic Cylinder ラッシュデュエル 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品"
        assert match_tier(matcher, t) == TIER_PARTIAL

    def test_real_prismatic_false_positive_is_partial(self, matcher):
        # 2026-07-08 ¥4,600 落札檔案實例：ARS10＋魔法の筒，但是現代 プリズマティック
        t = "【ARS10】世界に2枚 魔法の筒 Magic cylinder 限定品 プリズマティック 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品"
        assert match_tier(matcher, t) == TIER_PARTIAL

    def test_real_wcs_bundle_false_positive_is_partial(self, matcher):
        # 2026-06-03 ¥168,150 落札檔案實例：魔法の筒 只是同捆物之一，且是 25th/WCS
        t = "ARS10　遊戯王　ブラックマジシャンガール 25th　魔法の筒　WCS 2023　封筒　鑑定書付き プリズマ プリシク"
        assert match_tier(matcher, t) == TIER_PARTIAL

    def test_code_beats_modern_marker(self, matcher):
        # 卡號是決定性證據：現代版不會印 P4-06，所以標記詞不能推翻它
        assert match_tier(matcher, "ARS10 魔法の筒 P4-06 プリズマティック") == TIER_EXACT

    def test_classify_explains_every_demotion(self, matcher):
        from ygo_sniper.card_snipe import classify

        tier, why = classify(matcher, "PSA8 遊戯王　魔法の筒　P4-06　第２期")
        assert tier == TIER_NEAR and "PSA" in why and "ARS" in why
        tier, why = classify(matcher, "【ARS10】魔法の筒 プリズマティック")
        assert tier == TIER_PARTIAL and "現代版" in why

    def test_grader_without_score_is_partial(self, matcher):
        # parse_grade('…ARS鑑定品…') → (ARS, None)：機構對、分數不明 → 👀 通知
        assert match_tier(matcher, "魔法の筒 ARS鑑定品 遊戯王") == TIER_PARTIAL

    def test_same_grader_wrong_grade_is_partial(self, matcher):
        assert match_tier(matcher, "ARS9 魔法の筒 P4-06") == TIER_PARTIAL

    def test_other_code_only_is_partial(self, matcher):
        # 機構分數全符、但標題只明示別張卡號（同捆／別版本）→ 降半級仍通知
        assert match_tier(matcher, "ARS10 魔法の筒 LON-104") == TIER_PARTIAL

    def test_psa_copy_is_near(self, matcher):
        # comps 裡的真實 PSA8 標題（全形空白）
        assert match_tier(matcher, "PSA8 遊戯王　魔法の筒　ウルトラレア！　P4-06　第２期") == TIER_NEAR

    def test_ungraded_raw_card_is_near(self, matcher):
        assert match_tier(matcher, "遊戯王 魔法の筒 P4-06 ウルトラレア") == TIER_NEAR

    def test_unrelated_title_is_none(self, matcher):
        assert match_tier(matcher, "PSA10 ブラック・マジシャン 初期") is None


class TestObserveAndQueries:
    def test_observe_writes_hits_and_is_idempotent(self, store):
        from ygo_sniper.domain import Currency, Listing, Site

        wid = store.insert_card_watch(**WATCH_KW)
        lst = Listing(site=Site.BUYEE_YAHOO, external_id="x1",
                      title="【ARS10】魔法の筒 P4-06", url="https://example.test/x1",
                      price=50000.0, currency=Currency.JPY, seller_id="s1")
        matchers = load_matchers(store)
        assert observe_listings(store, matchers, [lst]) == 1
        assert observe_listings(store, matchers, [lst]) == 1       # 再跑：冪等
        hits = store.list_card_watch_hits(watch_id=wid)
        assert len(hits) == 1
        h = hits[0]
        assert h["tier"] == "exact" and h["listing_key"] == "buyee_yahoo:x1"
        assert h["site"] == "buyee_yahoo" and h["seller_id"] == "s1"
        assert h["price_native"] == 50000.0 and h["currency"] == "JPY"

    def test_scan_queries_reuse_base_sources(self, store):
        from ygo_sniper.queries import QuerySpec

        store.insert_card_watch(**WATCH_KW)
        base = [QuerySpec(name="q1", keyword="遊戯王 PSA", sources=("a", "b")),
                QuerySpec(name="q2", keyword="遊戯王 ARS", sources=("b", "c"))]
        qs = scan_queries(load_matchers(store), base)
        assert [q.keyword for q in qs] == ["魔法の筒", "Magic Cylinder"]
        assert all(q.sources == ("a", "b", "c") for q in qs)
        assert all(q.category is None for q in qs)
        assert scan_queries(load_matchers(store), []) == []        # base 空就不跑
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_card_snipe.py -x
```

預期：`ModuleNotFoundError: No module named 'ygo_sniper.card_snipe'`。

- [ ] **Step 3: 建立 `src/ygo_sniper/card_snipe.py`**

```python
"""指定卡狙擊（card watch）：等一根特定的針。

使用者指定「鑑定機構＋分數＋卡名＋卡號」的單卡；比對走在商業過濾**之前**
（pipeline._collect_candidates 開頭）——排除字／年代閘門／min_grade 是為
「大海撈針」設計的，狙擊是「等一根已知的針」，被它們誤殺一次可能就是等半年
（CLAUDE.md 第一節：誤殺是靜默的，雜訊是看得見的）。

三個 tier 的通知政策（寧可多報不漏報，但音量可控）：
- exact   🎯 名/號命中＋機構分數全符 → Telegram，不受每輪總量上限裁切
- partial 👀 名/號命中＋機構相符但分數不明/不同 → Telegram，自己的小上限
- near       名/號命中但機構不符/未鑑定/現代重印 → 只入帳＋dashboard，不推播
near 不推播不是丟棄：dashboard 狙擊分頁每一筆都看得到。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cards import extract_title_codes, fold
from .parsers.grade import parse_grade
from .queries import QuerySpec

TIER_EXACT = "exact"
TIER_PARTIAL = "partial"
TIER_NEAR = "near"

#: 現代版標記：命中就從 🎯 降到 👀（**照樣推播**，訊息上註明「疑似現代版」）。
#: 全部來自 2026-08-09 落札檔案的實際觀測，不是憑空想的。
#:
#: ⚠️ 為什麼是降到 partial 而不是 near（不推播）——這是本模組最重要的一條：
#: 目標卡的兩筆真成交，標題裡**根本沒有卡號**（`【ARS10】魔法の筒 Magic Cylinder
#: ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品`）。真標的與現代版假陽性的唯一
#: 差別就是這幾個標記詞。一份手寫的詞表只要多寫一個詞，就會靜默地讓真標的不通知，
#: 而誤殺是靜默的、雜訊是看得見的（CLAUDE.md 第一節）。所以標記詞的最大權力就是
#: 「降到 👀」——**永遠不能讓一筆命中變成不通知**。
#: fold 後比對：中点與全形自動吸收（`ラッシュ・デュエル`、`ＷＣＳ` 同樣命中）。
_MODERN_MARKERS = (
    "プリズマティック", "プリシク", "ラッシュデュエル", "RUSH DUEL",
    "25th", "WCS", "クォーターセンチュリー", "QUARTER CENTURY",
    "レアリティコレクション",
)
_MODERN_FOLDED = tuple(fold(t) for t in _MODERN_MARKERS)

#: partial 每輪推播上限（同 seller_unpriced 的思路：真品類稀少，
#: 一輪超過這個數多半是比對出了狀況，別讓它洗版）。
PARTIAL_MAX_PER_RUN = 5

#: near 命中帳的保留天數（exact/partial 永久保留）。
NEAR_HIT_RETAIN_DAYS = 90


@dataclass(slots=True)
class WatchMatcher:
    """一個 card_watch 列的預折疊比對器：fold 一次、每個標題重用。"""

    row: dict[str, Any]
    names_folded: tuple[str, ...]
    code_norm: str
    grader: str
    grade: float

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> WatchMatcher:
        names = [row.get("name_ja") or "", row.get("name_en") or ""]
        try:
            names += [str(a) for a in json.loads(row.get("aliases") or "[]")]
        except (TypeError, ValueError):
            pass
        folded = tuple(sorted({fold(n) for n in names if n and fold(n)}))
        return cls(
            row=row,
            names_folded=folded,
            code_norm=str(row.get("code_norm") or ""),
            grader=str(row.get("grader") or ""),
            grade=float(row.get("grade") or 0.0),
        )


def match_tier(m: WatchMatcher, title: str) -> str | None:
    """標題 → tier（None ＝ 與這張卡無關）。順序即政策，見模組 docstring。"""
    return classify(m, title)[0]


def classify(m: WatchMatcher, title: str) -> tuple[str | None, str]:
    """→ (tier, 一句話理由)。理由會走到訊息與 dashboard 上——降級了要說得出為什麼。

    順序即政策：
    1. 名／號都沒中 → None（與這張卡無關）
    2. 卡號命中 → 卡號是決定性的，現代版標記不能推翻它
    3. 機構不符／完全沒鑑定 → near（只入帳，不推播）
    4. 分數不明或不同 → partial
    5. 現代版標記 → partial（**照樣推播**，註明疑似現代版）
    6. 標題只明示別張卡號 → partial
    7. 其餘 → exact
    """
    folded = fold(title)
    name_hit = any(n in folded for n in m.names_folded)
    codes = extract_title_codes(title)
    code_hit = bool(m.code_norm) and m.code_norm in codes
    if not (name_hit or code_hit):
        return None, ""

    grader, grade = parse_grade(title)
    if grader.value != m.grader:
        got = grader.value if grader.value != "UNKNOWN" else "未鑑定"
        return TIER_NEAR, f"鑑定機構是 {got}，不是 {m.grader}"
    if grade is None:
        return TIER_PARTIAL, f"標題只寫 {m.grader}、沒寫分數"
    if abs(grade - m.grade) > 1e-9:
        return TIER_PARTIAL, f"分數是 {grade:g}，目標是 {m.grade:g}"

    # 機構與分數都符合。卡號命中就是決定性證據——現代版標記不能推翻它
    # （現代版不會印 P4-06）。
    if code_hit:
        return TIER_EXACT, f"卡號 {m.code_norm} ＋ {m.grader}{grade:g} 全符"

    modern = [
        raw for raw, f in zip(_MODERN_MARKERS, _MODERN_FOLDED, strict=True)
        if f in folded
    ]
    if modern:
        return TIER_PARTIAL, f"疑似現代版（標題含 {'／'.join(modern)}）"
    if codes and m.code_norm:
        # 機構分數全符，但標題明示的卡號全是別張——多半是同捆或別版本，降半級仍通知
        return TIER_PARTIAL, f"標題的卡號是 {'／'.join(codes)}，不是 {m.code_norm}"
    return TIER_EXACT, f"卡名 ＋ {m.grader}{grade:g} 全符"


def load_matchers(store: Any) -> list[WatchMatcher]:
    return [WatchMatcher.from_row(r) for r in store.list_card_watch(active_only=True)]


def observe_listings(
    store: Any, matchers: list[WatchMatcher], listings: list, *,
    source_name: str = "",
) -> int:
    """對一批**未過濾**的原始 listing 跑狙擊比對，命中寫進 hit 帳（冪等）。
    回傳寫入（含更新）筆數。"""
    n = 0
    for lst in listings:
        title = getattr(lst, "title", "") or ""
        for m in matchers:
            tier = match_tier(m, title)
            if tier is None:
                continue
            end = getattr(lst, "end_time", None)
            currency = getattr(lst, "currency", "")
            store.upsert_card_watch_hit(
                int(m.row["id"]), lst.key,
                tier=tier, title=title, url=lst.url,
                site=lst.site.value,
                seller_id=getattr(lst, "seller_id", None) or "",
                price_native=float(lst.price) if lst.price is not None else None,
                currency=str(getattr(currency, "value", currency) or ""),
                end_time=end.isoformat() if end is not None else "",
            )
            n += 1
    return n


def scan_queries(
    matchers: list[WatchMatcher], base_queries: list[QuerySpec]
) -> list[QuerySpec]:
    """每張狙擊卡加自己的關鍵字查詢（日文名＋英文名），來源沿用既有查詢的聯集。

    不猜來源名——base 用哪些來源，狙擊查詢就用哪些；base 是空的
    （watch_only 模式）狙擊查詢也不跑。不帶分類（category=None）：分類是
    收斂雜訊用的，狙擊要的是最大召回，雜訊由 tier 政策吸收。
    """
    srcs = tuple(dict.fromkeys(s for q in base_queries for s in q.sources))
    if not srcs:
        return []
    out: list[QuerySpec] = []
    seen: set[str] = set()
    for m in matchers:
        for kw in (m.row.get("name_ja") or "", m.row.get("name_en") or ""):
            kw = kw.strip()
            if not kw or kw.lower() in seen:
                continue
            seen.add(kw.lower())
            out.append(QuerySpec(
                name=f"snipe:{m.row['id']}", keyword=kw, sources=srcs, category=None,
            ))
    return out
```

- [ ] **Step 4: 跑測試看它綠**

```bash
.venv/bin/pytest tests/test_card_snipe.py -x
```

預期：`42 passed`（14 store ＋ 26 tier/機構競合 ＋ 2 observe/queries；含審查後新增的 11 條、刪除的 1 條重複測試）。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/card_snipe.py tests/test_card_snipe.py
git commit -m "feat(snipe): 比對核心——三 tier 判定（過濾前比對、現代重印降級不丟棄）與狙擊查詢注入"
```

---

### Task 3: `ars_census.py`（ARS 鑑定量抓取＋解析）

**Files:**
- Create: `src/ygo_sniper/ars_census.py`
- Test: `tests/test_ars_census.py`

- [ ] **Step 1: 寫失敗測試 `tests/test_ars_census.py`**

```python
"""ARS census 頁解析。fixture 是 2026-08-09 生產路徑（無 JS）抓的真頁面。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest

from ygo_sniper.ars_census import (
    SEARCH_URL,
    CensusParseError,
    census_total,
    find_census_url,
    page_mentions,
    parse_census,
    parse_search_results,
)

FIXTURES = Path(__file__).parent / "fixtures"
CENSUS_HTML = (FIXTURES / "ars_census_p4_06.html").read_text(encoding="utf-8")
SEARCH_HTML = (FIXTURES / "ars_search_magic_cylinder.html").read_text(encoding="utf-8")


class FakeFetcher:
    """CachedFetcher.get 同形（測試注入；生產傳真的）。"""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def get(self, url: str, **kw) -> str:
        return self.pages[url]


def test_parse_census_counts_from_the_real_page():
    counts = parse_census(CENSUS_HTML)
    assert counts["9"] == 5
    assert counts["10"] == 5
    assert counts["10+"] == 1
    assert census_total(CENSUS_HTML) == 11


def test_parse_census_raises_loudly_on_shape_change():
    with pytest.raises(CensusParseError):
        parse_census("<html><body>改版了</body></html>")


def test_page_mentions_strips_tags():
    assert page_mentions(CENSUS_HTML, "P4-06")
    assert page_mentions(CENSUS_HTML, "魔法の筒")
    assert not page_mentions(CENSUS_HTML, "青眼の白龍")


def test_parse_search_results_shape():
    entries = parse_search_results(SEARCH_HTML)
    assert len(entries) == 6
    codes = [e["code"] for e in entries]
    assert "P4-06" in codes
    hit = next(e for e in entries if e["code"] == "P4-06")
    assert hit["url"] == (
        "https://ars-grading.com/grading/searchNameDetail?id=001202208090020007"
    )
    assert hit["name"] == "魔法の筒"


def test_find_census_url_narrows_by_normalized_code():
    fetcher = FakeFetcher({SEARCH_URL.format(name=quote("魔法の筒")): SEARCH_HTML})
    url, entries = find_census_url("魔法の筒", "P4-6", fetcher=fetcher)
    assert url is not None and url.endswith("id=001202208090020007")
    assert len(entries) == 6


def test_find_census_url_returns_candidates_when_ambiguous():
    fetcher = FakeFetcher({SEARCH_URL.format(name=quote("魔法の筒")): SEARCH_HTML})
    url, entries = find_census_url("魔法の筒", "", fetcher=fetcher)   # 沒卡號 → 收不斂
    assert url is None and len(entries) == 6
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_ars_census.py -x
```

預期：`ModuleNotFoundError: No module named 'ygo_sniper.ars_census'`。

- [ ] **Step 3: 建立 `src/ygo_sniper/ars_census.py`**

```python
"""ARS 鑑定量（census）抓取與解析。

頁面是 server-rendered HTML、無 JS 需求（2026-08-09 實測）。數字形如
`<div class="grade-entry" data-grade="10"><span>Grade 10</span><span>5（0）</span></div>`
——括號內是含ミスプリント的數字，我們存主數字。改版時 parse_census 大聲拋
CensusParseError，絕不回空 dict 假裝成功（0 張與抓不到是兩回事）。
"""
from __future__ import annotations

import re
from urllib.parse import quote

_GRADE_ENTRY_RE = re.compile(
    r'data-grade="(?P<grade>[^"]+)"\s*>\s*<span>[^<]*</span>\s*'
    r"<span>\s*(?P<count>\d+)（"
)
# ⚠️ 真實頁面是 `鑑定総数 TOTAL GRADING&nbsp;11`——數字前是**字面的 `&nbsp;` 實體**，
# `\s` 抓不到（實測用 `\s*` 會回 None）。三種寫法都吃下來。
_TOTAL_RE = re.compile(r"鑑定総数\s*TOTAL\s*GRADING(?:&nbsp;|&#160;|\s)*(\d+)")

SEARCH_URL = "https://ars-grading.com/grading/searchName?name={name}&page=1"

_SEARCH_ENTRY_RE = re.compile(
    r'href="(?P<path>/grading/searchNameDetail\?id=[^"]+)"(?P<body>.*?)</a>',
    re.S,
)
_SPAN_RE = re.compile(r"<span>\s*([^<]*?)\s*</span>", re.S)


class CensusParseError(RuntimeError):
    """頁面抓到了但不是預期形狀（版型改了）。"""


def parse_census(html: str) -> dict[str, int]:
    """→ `{"AU": 0, "1": 0, …, "9": 5, "10": 5, "10+": 1}`（key 是頁面原樣）。"""
    counts = {
        m.group("grade"): int(m.group("count"))
        for m in _GRADE_ENTRY_RE.finditer(html)
    }
    if not counts:
        raise CensusParseError("頁面上找不到任何 grade-entry——ARS 版型可能改了")
    return counts


def census_total(html: str) -> int | None:
    m = _TOTAL_RE.search(html)
    return int(m.group(1)) if m else None


def page_mentions(html: str, needle: str) -> bool:
    """去標籤後的純文字裡有沒有這個字串（登錄時的同卡 sanity check）。"""
    return needle in re.sub(r"<[^>]+>", " ", html)


def parse_search_results(html: str) -> list[dict[str, str]]:
    """卡名搜尋頁 → 候選清單。span 順序（實測）：卡名/收錄/型番/稀有度/年份。"""
    out: list[dict[str, str]] = []
    for m in _SEARCH_ENTRY_RE.finditer(html):
        spans = [s.strip() for s in _SPAN_RE.findall(m.group("body"))]
        out.append({
            "url": "https://ars-grading.com" + m.group("path"),
            "name": spans[0] if len(spans) > 0 else "",
            "expansion": spans[1] if len(spans) > 1 else "",
            "code": spans[2] if len(spans) > 2 else "",
            "rarity": spans[3] if len(spans) > 3 else "",
            "year": spans[4] if len(spans) > 4 else "",
        })
    return out


def find_census_url(
    name_ja: str, code_norm: str, *, fetcher
) -> tuple[str | None, list[dict[str, str]]]:
    """卡名搜尋 → 用正規化卡號唯一定位 census 頁。

    收斂不了就回 (None, 候選清單) 讓呼叫端把候選攤給使用者——寧可要使用者
    確認，不猜（識別碼是命名空間不是字串，猜錯會安靜地追蹤錯的卡）。
    """
    from .cards import extract_title_codes

    html = fetcher.get(SEARCH_URL.format(name=quote(name_ja)), use_cache=False)
    entries = parse_search_results(html)
    if code_norm:
        matched = [
            e for e in entries
            if code_norm in extract_title_codes(f" {e['code']} ")
        ]
        if len(matched) == 1:
            return matched[0]["url"], entries
    return None, entries


def fetch_census(url: str, *, fetcher) -> tuple[dict[str, int], int | None, str]:
    """→ (各級張數, 鑑定總數, 原始 html)。html 給呼叫端做同卡 sanity check。"""
    html = fetcher.get(url, use_cache=False)
    return parse_census(html), census_total(html), html
```

- [ ] **Step 4: 跑測試看它綠**

```bash
.venv/bin/pytest tests/test_ars_census.py -x
```

預期：`6 passed`。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/ars_census.py tests/test_ars_census.py
git commit -m "feat(snipe): ARS census 解析——改版大聲拋錯、卡號收斂不了就攤候選不猜"
```

---

### Task 4: `yahoo_auction_page.py`（結標頁快照）

**Files:**
- Create: `src/ygo_sniper/yahoo_auction_page.py`
- Test: `tests/test_yahoo_auction_page.py`

- [ ] **Step 1: 寫失敗測試 `tests/test_yahoo_auction_page.py`**

```python
"""Yahoo 商品頁（含已結束）快照解析。fixture 是真結標頁（2026-07-01 ¥6,350）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from ygo_sniper.yahoo_auction_page import (
    AuctionPageError,
    fetch_auction_snapshot,
    parse_auction_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = (FIXTURES / "yahoo_closed_n1235105710.html").read_text(encoding="utf-8")
URL = "https://auctions.yahoo.co.jp/jp/auction/n1235105710"


def test_parse_closed_auction_snapshot():
    snap = parse_auction_page(PAGE, url=URL)
    assert snap.url == URL
    assert snap.title.startswith("【ARS10】魔法の筒")
    assert snap.price == 6350
    assert snap.currency == "JPY"
    assert snap.end_time == "2026-07-01T22:53:03+09:00"
    assert snap.bids == 15
    assert snap.status == "closed"
    assert snap.seller_id == "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"
    assert snap.seller_name == "Natural Cards"


def test_parse_raises_loudly_without_next_data():
    with pytest.raises(AuctionPageError):
        parse_auction_page("<html><body>WAF page</body></html>")


def test_fetch_uses_the_injected_fetcher():
    class FakeFetcher:
        def get(self, url, **kw):
            assert url == URL
            return PAGE

    snap = fetch_auction_snapshot(URL, fetcher=FakeFetcher())
    assert snap.price == 6350
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_yahoo_auction_page.py -x
```

預期：`ModuleNotFoundError`。

- [ ] **Step 3: 建立 `src/ygo_sniper/yahoo_auction_page.py`**

```python
"""Yahoo 拍賣商品頁（含已結束）快照：資料在 `<script id="__NEXT_DATA__">` JSON。

已結束頁約 120 天後刪除（慣例值；實證下界 74 天，2026-08-09 驗）——所以
使用者提供的歷史 URL 必須**入庫當下就抓快照**，不能只存連結。
JSON 路徑（2026-08-09 實測）：props.pageProps.initialState.item.detail.item。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class AuctionPageError(RuntimeError):
    """頁面抓到了但不是預期形狀（被擋、已刪除、或版型改了）。"""


@dataclass(slots=True)
class AuctionSnapshot:
    url: str
    title: str
    price: float | None
    currency: str        # Yahoo 拍賣一律 JPY
    end_time: str        # ISO8601 頁面原樣（含 +09:00）——不轉時區，存原文
    bids: int | None
    status: str          # 'open' / 'closed' 頁面原樣
    seller_id: str       # seller.aucUserId（= /seller/ URL 的 token，同一命名空間）
    seller_name: str


def parse_auction_page(html: str, *, url: str = "") -> AuctionSnapshot:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise AuctionPageError("頁面沒有 __NEXT_DATA__——可能被擋、已刪除或版型改了")
    try:
        item = json.loads(m.group(1))["props"]["pageProps"]["initialState"][
            "item"]["detail"]["item"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AuctionPageError(f"__NEXT_DATA__ JSON 路徑不符：{exc}") from exc
    seller = item.get("seller") or {}
    price = item.get("price")
    return AuctionSnapshot(
        url=url,
        title=str(item.get("title") or ""),
        price=float(price) if price is not None else None,
        currency="JPY",
        end_time=str(item.get("endTime") or ""),
        bids=item.get("bids"),
        status=str(item.get("status") or ""),
        seller_id=str(seller.get("aucUserId") or ""),
        seller_name=str(seller.get("displayName") or ""),
    )


def fetch_auction_snapshot(url: str, *, fetcher) -> AuctionSnapshot:
    """fetcher 只需有 CachedFetcher.get 同形的 get(url)。FetchError 由呼叫端分類。"""
    return parse_auction_page(fetcher.get(url), url=url)
```

- [ ] **Step 4: 跑測試看它綠**

```bash
.venv/bin/pytest tests/test_yahoo_auction_page.py -x
```

預期：`3 passed`。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/yahoo_auction_page.py tests/test_yahoo_auction_page.py
git commit -m "feat(snipe): Yahoo 結標頁快照解析（__NEXT_DATA__；頁面會過期所以入庫即存證）"
```

---

### Task 4b: 市場成交檔案挖掘（本計劃的主要資料源）

**Files:**
- Modify: `src/ygo_sniper/card_snipe.py`（檔尾追加；沿用既有 `sources/yahoo_closed.py`，**不新寫 parser**）
- Test: `tests/test_card_snipe_mine.py`

實測依據（2026-08-09）：查 `魔法の筒` 一個請求 0.98 秒回 100 筆、涵蓋 150 天；把本 Task 的 tier 邏輯套上去得到 **🎯 2 筆（正是使用者手上那兩筆）／👀 2 筆（現代版）／near 96 筆／誤報 🎯 0 筆**。

- [ ] **Step 1: 寫失敗測試 `tests/test_card_snipe_mine.py`**

```python
"""市場成交檔案挖掘：市場的檔案才是資料庫，我們的庫是它的記憶體。"""
from __future__ import annotations

import pytest

from ygo_sniper.card_snipe import WatchMatcher, mine_sold_archive
from ygo_sniper.domain import Currency, Listing, Site
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.store import Store

WATCH_KW = dict(
    grader="ARS", grade=10.0, grade_label="10",
    name_ja="魔法の筒", name_en="Magic Cylinder",
    aliases=["マジック・シリンダー"], code_raw="P4-06", code_norm="P4-6",
)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def _sold(ext_id, title, price, sold_at, *, bids=1, fixed=False, seller="S1"):
    return Listing(
        site=Site.BUYEE_YAHOO, external_id=ext_id, title=title,
        url=f"https://buyee.jp/item/yahoo/auction/{ext_id}",
        price=float(price), currency=Currency.JPY, seller_id=seller, is_sold=True,
        source="yahoo_closed",
        origin_url=f"https://page.auctions.yahoo.co.jp/jp/auction/{ext_id}",
        raw={"sold_at": sold_at, "bid_count": bids, "is_fixed_price": fixed,
             "price_kind": "sold_price"},
    )


class FakeSource:
    """`_sold_search` 只需要 search_detailed 同形。"""

    name = "yahoo_closed"
    site = Site.BUYEE_YAHOO
    supports_sold = True

    def __init__(self, listings, health=ParseHealth.OK):
        self.listings = listings
        self.health = health
        self.queries: list[str] = []

    def search_detailed(self, keyword, *, sold=False, pages=1, **kw):
        self.queries.append(keyword)
        return SearchResult(
            source=self.name, site=self.site.value, query=keyword,
            listings=list(self.listings), parsed_count=len(self.listings),
            health=self.health, pages_fetched=pages,
        )


REAL = [
    # 落札檔案的四筆 ARS 命中（2026-08-09 實測原文）
    _sold("n1235105710", "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品",
          6350, "2026-07-01T13:53:03+00:00", bids=15, seller="AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"),
    _sold("l1230920412", "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品",
          7750, "2026-05-27T13:27:38+00:00", bids=10, seller="AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"),
    _sold("x111", "【ARS10】世界に2枚 魔法の筒 Magic cylinder 限定品 プリズマティック 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品",
          4600, "2026-07-08T12:00:00+00:00", bids=30),
    _sold("x222", "ARS10　遊戯王　ブラックマジシャンガール 25th　魔法の筒　WCS 2023　封筒　鑑定書付き プリズマ プリシク",
          168150, "2026-06-03T12:00:00+00:00", bids=1, fixed=True),
    # 同卡他家鑑定與未鑑定：入帳但不是通知級
    _sold("y1", "PSA8 遊戯王　魔法の筒　ウルトラレア！　P4-06　第２期", 1900,
          "2026-04-01T12:00:00+00:00", fixed=True),
    # 完全無關：不進帳
    _sold("z1", "遊戯王 青眼の白龍 初期 PSA10", 99999, "2026-04-02T12:00:00+00:00"),
]


class TestMineSoldArchive:
    def test_mines_and_classifies_the_real_archive(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = FakeSource(REAL)
        res = mine_sold_archive(store, {"yahoo_closed": src}, m)

        assert res.ok is True
        assert res.new_sales == 5           # 6 筆裡「青眼の白龍」不相關，不入帳
        # ⚠️ 這條同時釘住「跨關鍵字去重」：兩個關鍵字（日文名＋英文名）各跑一次查詢、
        #    各回同一份清單，沒有去重的話每個數字都會變兩倍。實測真實檔案：
        #    未去重 exact 4／partial 3，去重後 exact 2／partial 2。
        assert res.tier_counts == {"exact": 2, "partial": 2, "near": 1}
        assert len(res.queries) == 2        # 確實打了兩次查詢（不是靠少查來避免重複）
        sales = store.list_card_watch_sales(wid)
        assert len(sales) == 5
        exact = [s for s in sales if s["tier"] == "exact"]
        assert {s["price_native"] for s in exact} == {6350.0, 7750.0}
        assert all(s["seller_id"] == "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx" for s in exact)
        assert all(s["sale_kind"] == "auction" for s in exact)
        assert {s["sold_at"][:10] for s in exact} == {"2026-07-01", "2026-05-27"}

    def test_fixed_price_sale_kind_is_from_the_flag_not_bid_count(self, store):
        """フリマ 定價成交的 bidCount 也是 1（佔位值）——型態只看 is_fixed_price。"""
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        mine_sold_archive(store, {"yahoo_closed": FakeSource(REAL)}, m)
        wcs = [s for s in store.list_card_watch_sales(wid)
               if s["price_native"] == 168150.0][0]
        assert wcs["sale_kind"] == "fixed" and wcs["bid_count"] == 1

    def test_queries_card_names_only_never_grader_terms(self, store):
        """伺服器端多一個鑑定詞 ＝ AND 過濾 ＝ 靜默誤殺（只寫 ARS鑑定10 的賣家就消失）。"""
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = FakeSource(REAL)
        mine_sold_archive(store, {"yahoo_closed": src}, m)
        assert src.queries == ["魔法の筒", "Magic Cylinder"]
        assert all("ARS" not in q and "PSA" not in q for q in src.queries)

    def test_remining_is_idempotent_and_counts_only_new(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = {"yahoo_closed": FakeSource(REAL)}
        first = mine_sold_archive(store, src, m)
        again = mine_sold_archive(store, src, m)
        assert first.new_sales == 5 and again.new_sales == 0
        assert again.total_sales == 5
        assert len(store.list_card_watch_sales(wid)) == 5

    def test_blocked_source_is_loud_and_not_reported_as_zero(self, store):
        """0 筆有兩種讀法：真的沒賣過／被擋。分不出來就是靜默失敗。"""
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = FakeSource([], health=ParseHealth.BLOCKED)
        res = mine_sold_archive(store, {"yahoo_closed": src}, m)
        assert res.ok is False
        assert res.new_sales == 0
        assert any("BLOCKED" in p or "被擋" in p for p in res.problems)

    def test_source_without_sold_support_is_skipped(self, store):
        class NoSold(FakeSource):
            supports_sold = False

        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = NoSold(REAL)
        res = mine_sold_archive(store, {"nosold": src}, m)
        assert src.queries == [] and res.total_sales == 0
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_card_snipe_mine.py -x
```

預期：`ImportError: cannot import name 'mine_sold_archive'`。

- [ ] **Step 3a: 讓 `refill._sold_search` 收 `pages`（現在寫死 1 頁，`pages` 參數會變裝飾品）**

`refill.py:297-301` 目前把 `REFILL_PAGES`（＝1，`refill.py:66`）寫死進三個位置。不改的話 `snipe mine --pages 5` 靜默無效，而且檔案只挖得到 1 頁（≈150 天）而不是計劃承諾的 2 頁（≈179 天）。加一個**帶預設值**的關鍵字參數，既有呼叫端行為完全不變：

```python
def _sold_search(
    src: Any, source_name: str, keyword: str, *, pages: int = REFILL_PAGES
) -> SearchResult:
```

並把函式體內的三個 `REFILL_PAGES` 全部換成 `pages`（`search_detailed(..., pages=pages)`、`src.search(..., pages=pages)`、`pages_fetched=pages`）。**不要改 `REFILL_PAGES` 常數本身**——回填路徑刻意維持 1 頁（`refill.py:28` 有寫理由）。

驗證既有回填行為沒被改變：

```bash
.venv/bin/pytest tests/ -k "refill"
```

預期：全綠。

- [ ] **Step 3: 在 `card_snipe.py` 檔尾追加挖掘模組**

```python


# ---------------------------------------------------------------------------
# 市場成交檔案挖掘。**市場的檔案才是資料庫，這張表是它的記憶體。**
# ---------------------------------------------------------------------------
#: 每個關鍵字翻幾頁。實測 `魔法の筒` 第 1 頁 100 筆已涵蓋 150 天、第 2 頁翻完
#: 整個檔案（126 筆／179 天）。冷門卡 2 頁綽綽有餘；熱門卡名多翻也只是多 1 秒。
MINE_PAGES = 2


@dataclass(slots=True)
class MineResult:
    """一次挖掘的完整結果。**命中數與健康是兩件事**：0 筆可能是真的沒賣過，
    也可能是被擋——分不出來就是靜默失敗（CLAUDE.md 第五節）。"""

    ok: bool = True
    queries: list[str] = field(default_factory=list)
    new_sales: int = 0
    total_sales: int = 0
    tier_counts: dict[str, int] = field(default_factory=dict)
    oldest: str = ""
    newest: str = ""
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        span = f"{self.oldest[:10]} → {self.newest[:10]}" if self.oldest else "無成交"
        parts = [
            f"挖到 {self.total_sales} 筆成交（新增 {self.new_sales}）",
            f"涵蓋 {span}",
            "／".join(f"{k} {v}" for k, v in sorted(self.tier_counts.items())) or "—",
        ]
        if not self.ok:
            parts.append("⚠️ " + "；".join(self.problems))
        return "｜".join(parts)


def _sale_kind_of(lst: Any) -> str:
    """競標結標價（買家喊上去）vs 定價成交（賣家開的）——兩種價格形成機制。
    **只看 is_fixed_price 旗標**：Yahoo!フリマ 的 bidCount 也是 1（佔位值），
    拿它判型態會把定價成交讀成競標（CLAUDE.md 第三節第七項）。"""
    raw = getattr(lst, "raw", None) or {}
    if "is_fixed_price" not in raw:
        return "unknown"
    return "fixed" if raw.get("is_fixed_price") else "auction"


def mine_sold_archive(
    store: Any, sources: dict[str, Any], m: WatchMatcher, *, pages: int = MINE_PAGES,
) -> MineResult:
    """去各平台的成交檔案挖這張卡的過去，逐筆 tier 分類後永久存進 card_watch_sale。

    **查詢只打卡名，絕不加鑑定詞**：伺服器端多一個詞就是 AND 過濾，只寫
    「ARS鑑定10」的賣家會整批消失（實測 `魔法の筒 PSA` 只回 5 筆 vs 卡名 126 筆）。
    收斂是我們自己在本地用 tier 做的——那才看得見、才改得動。
    """
    from .refill import _sold_search   # ⚠️ 需先照 Step 3a 加上 pages 參數

    res = MineResult()
    keywords = [k.strip() for k in
                (m.row.get("name_ja") or "", m.row.get("name_en") or "") if k.strip()]
    watch_id = int(m.row["id"])
    #: 同一筆成交會被多個關鍵字撈到（日文名與英文名常同時出現在標題裡）。
    #: **tier_counts 要數的是「這張卡的成交筆數」，不是「查詢×命中」的事件數**——
    #: 少了這個去重，兩個關鍵字就讓每個數字都變兩倍（同源同基準）。
    seen: set[str] = set()
    for source_name, src in sources.items():
        if not getattr(src, "supports_sold", False):
            continue
        for kw in keywords:
            res.queries.append(kw)
            out = _sold_search(src, source_name, kw, pages=pages)
            health = getattr(out, "health", None)
            health_name = getattr(health, "name", str(health))
            if health_name not in ("OK", "EMPTY_CONFIRMED"):
                res.ok = False
                res.problems.append(
                    f"{source_name}／{kw}：{health_name}"
                    f"（{getattr(out, 'detail', '') or '沒有細節'}）——"
                    "這一條的 0 筆不代表沒賣過"
                )
                continue
            for lst in out.listings:
                tier, _why = classify(m, getattr(lst, "title", "") or "")
                if tier is None:
                    continue
                if lst.key in seen:
                    continue          # 另一個關鍵字已經收過這一筆
                seen.add(lst.key)
                raw = getattr(lst, "raw", None) or {}
                currency = getattr(lst, "currency", "")
                is_new = store.upsert_card_watch_sale(
                    watch_id, lst.key,
                    tier=tier, title=lst.title, url=lst.url,
                    origin_url=getattr(lst, "origin_url", None) or "",
                    site=lst.site.value,
                    seller_id=getattr(lst, "seller_id", None) or "",
                    price_native=float(lst.price) if lst.price is not None else None,
                    currency=str(getattr(currency, "value", currency) or ""),
                    sold_at=str(raw.get("sold_at") or ""),
                    bid_count=raw.get("bid_count"),
                    sale_kind=_sale_kind_of(lst),
                    source=source_name,
                )
                res.new_sales += int(is_new)
                res.tier_counts[tier] = res.tier_counts.get(tier, 0) + 1
    sales = store.list_card_watch_sales(watch_id)
    res.total_sales = len(sales)
    stamps = sorted(s["sold_at"] for s in sales if s["sold_at"])
    if stamps:
        res.oldest, res.newest = stamps[0], stamps[-1]
    return res
```

- [ ] **Step 4: 跑測試看它綠**

```bash
.venv/bin/pytest tests/test_card_snipe_mine.py -x
```

預期：`6 passed`。

- [ ] **Step 5: 實跑一次真實挖掘（會打網路，約 2 個請求）**

```bash
cd /Users/jim/projects/ygo-sniper && .venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from ygo_sniper.config import load_config
from ygo_sniper.sources import build_sources
from ygo_sniper.sources.base import CachedFetcher
from ygo_sniper.card_snipe import WatchMatcher, mine_sold_archive
from ygo_sniper.store import Store
import tempfile, pathlib
cfg = load_config()
store = Store(pathlib.Path(tempfile.mkdtemp()) / 'probe.db')
wid = store.insert_card_watch(grader='ARS', grade=10.0, grade_label='10',
    name_ja='魔法の筒', name_en='Magic Cylinder',
    aliases=['マジック・シリンダー'], code_raw='P4-06', code_norm='P4-6')
m = WatchMatcher.from_row(store.get_card_watch(wid))
with CachedFetcher(cfg) as f:
    res = mine_sold_archive(store, build_sources(cfg, f), m)
print(res.summary())
for s in store.list_card_watch_sales(wid, tiers=('exact','partial')):
    print(f\"  {s['tier']:8} {s['sold_at'][:10]} JPY {s['price_native']:>8,.0f} \"
          f\"{s['sale_kind']:8} seller={s['seller_id'][:16]}  {s['title'][:46]}\")
"
```

預期：`tier_counts` 含 `exact 2`；兩筆 exact 是 `2026-07-01 JPY 6,350` 與 `2026-05-27 JPY 7,750`，賣家皆為 `AiUkMq1pEUfNxvPeCv5PnfGpsFLrx`、`sale_kind=auction`。**對不上就停下來回報，不要繼續**（代表 closedsearch 改版或被擋）。

- [ ] **Step 6: Commit**

```bash
git add src/ygo_sniper/card_snipe.py tests/test_card_snipe_mine.py
git commit -m "feat(snipe): 市場成交檔案挖掘——查卡名不加鑑定詞、逐筆 tier 分類、成交型態看旗標不看出價數"
```

---

### Task 5: 政策層——登錄、census 併入、dossier、等待建議、通知脈絡

**Files:**
- Modify: `src/ygo_sniper/card_snipe.py`（改 import 區＋檔尾追加）
- Test: `tests/test_card_snipe.py`（追加）

- [ ] **Step 1: 追加失敗測試到 `tests/test_card_snipe.py`**

```python
from urllib.parse import quote
from pathlib import Path

from ygo_sniper.ars_census import SEARCH_URL
from ygo_sniper.card_snipe import (
    add_card_watch,
    build_dossier,
    build_notify_context,
)

FIXTURES = Path(__file__).parent / "fixtures"


class PageFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.pages[url]


def _fixture_pages():
    return {
        SEARCH_URL.format(name=quote("魔法の筒")):
            (FIXTURES / "ars_search_magic_cylinder.html").read_text(encoding="utf-8"),
        "https://ars-grading.com/grading/searchNameDetail?id=001202208090020007":
            (FIXTURES / "ars_census_p4_06.html").read_text(encoding="utf-8"),
        "https://auctions.yahoo.co.jp/jp/auction/n1235105710":
            (FIXTURES / "yahoo_closed_n1235105710.html").read_text(encoding="utf-8"),
    }


class TestAddCardWatch:
    def test_full_flow_offline(self, store):
        res = add_card_watch(
            store, PageFetcher(_fixture_pages()),
            grader="ars", grade_input="10", name_ja="魔法の筒", code="P4-06",
            evidence_urls=["https://auctions.yahoo.co.jp/jp/auction/n1235105710"],
        )
        w = store.get_card_watch(res.watch_id)
        assert w["grader"] == "ARS" and w["grade"] == 10.0 and w["grade_label"] == "10"
        assert w["code_norm"] == "P4-6"
        assert "マジック・シリンダー" in w["aliases"]          # 主檔 enrich
        assert w["name_en"] == "Magic Cylinder"                 # 主檔補英文名
        assert json.loads(w["census_json"])["10"] == 5          # census 自動搜到＋抓到
        assert w["census_total"] == 11
        ev = store.list_card_watch_evidence(res.watch_id)
        assert len(ev) == 1 and ev[0]["status"] == "ok"
        assert ev[0]["price_native"] == 6350.0
        assert ev[0]["sold_at"] == "2026-07-01T22:53:03+09:00"
        assert ev[0]["seller_id"] == "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"
        # sources 沒給就不挖市場檔案，而且要講出來（靜默跳過＝之後查不出為什麼是空的）
        assert any("跳過市場成交檔案挖掘" in m for m in res.messages)

    def test_bad_grader_raises(self, store):
        with pytest.raises(ValueError):
            add_card_watch(store, PageFetcher({}), grader="CGC",
                           grade_input="10", name_ja="x")

    def test_unfetchable_evidence_is_kept_loudly(self, store):
        class BoomFetcher:
            def get(self, url, **kw):
                from ygo_sniper.sources.base import FetchError
                raise FetchError("404", url=url, status=404)

        res = add_card_watch(
            store, BoomFetcher(), grader="PSA", grade_input="10", name_ja="魔法の筒",
            evidence_urls=["https://auctions.yahoo.co.jp/jp/auction/dead1"],
        )
        ev = store.list_card_watch_evidence(res.watch_id)
        assert ev[0]["status"] == "unverifiable"                # 讀不到 ≠ 不存在
        assert any("unverifiable" in m or "抓不到" in m for m in res.messages)

    def test_non_yahoo_evidence_is_stored_as_unsupported(self, store):
        res = add_card_watch(
            store, PageFetcher({}), grader="PSA", grade_input="10", name_ja="魔法の筒",
            evidence_urls=["https://www.ebay.com/itm/12345"],
        )
        ev = store.list_card_watch_evidence(res.watch_id)
        assert ev[0]["status"] == "unsupported"


class TestDossier:
    def test_three_buckets_stay_separate_and_recommendation_names_the_seller(self, store):
        res = add_card_watch(
            store, PageFetcher(_fixture_pages()),
            grader="ars", grade_input="10", name_ja="魔法の筒", code="P4-06",
            evidence_urls=["https://auctions.yahoo.co.jp/jp/auction/n1235105710"],
        )
        # 市場成交檔案桶：直接寫一筆（挖掘本身在 test_card_snipe_mine.py 測）
        store.upsert_card_watch_sale(
            res.watch_id, "buyee_yahoo:l1230920412", tier="exact",
            title="【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品",
            url="https://buyee.jp/item/yahoo/auction/l1230920412", site="buyee_yahoo",
            seller_id="AiUkMq1pEUfNxvPeCv5PnfGpsFLrx", price_native=7750.0,
            currency="JPY", sold_at="2026-05-27T13:27:38+00:00", bid_count=10,
            sale_kind="auction", source="yahoo_closed",
        )
        # 本地補充桶：一筆 comps（PSA8 真實標題）
        with store._conn() as c:
            c.execute(
                "INSERT INTO comps (signature, title, price_native, currency, url,"
                " site, sold_at, sale_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("sig", "PSA8 遊戯王　魔法の筒　ウルトラレア！　P4-06　第２期",
                 1900.0, "JPY", "https://example.test/c1", "buyee_mercari",
                 "2026-08-03T10:01:36Z", "fixed"),
            )
        w = store.get_card_watch(res.watch_id)
        d = build_dossier(store, w)

        assert d.census["10"] == 5 and d.census_total == 11
        # 三個桶各自獨立，沒有被合併成一個數字
        assert len(d.sales) == 1 and d.sales[0]["tier"] == "exact"
        assert d.sales[0]["sale_kind"] == "auction"
        assert len(d.evidence) == 1 and d.evidence[0]["status"] == "ok"
        assert len(d.local_history) == 1
        assert d.local_history[0]["tier"] == "near"             # PSA8 是同卡他家鑑定
        assert d.local_history[0]["ledger"] == "comps"

        joined = "\n".join(d.recommendation)
        # 賣家歸因要指名道姓、給可執行指令，並講出成交價
        assert "watch-seller pin buyee_yahoo:AiUkMq1pEUfNxvPeCv5PnfGpsFLrx" in joined
        assert "6,350" in joined and "7,750" in joined
        assert "全世界" in joined                                # census 稀缺度有講
        # 檔案期間的極限要誠實標註（不能讓使用者以為那是全部歷史）
        assert "不是全部歷史" in joined
        assert "競標" in joined                                  # 成交型態的等待策略

    def test_undated_sales_never_inflate_the_frequency_claim(self, store):
        """來源給不出落札時間的成交（Mercari／露天）**不得**進入「幾次／期間」。

        實測一次挖掘 206 筆裡有 77 筆沒有成交時刻。把它們算進次數，就是拿兩種
        基準的東西合成一個數字（CLAUDE.md 第三節；comps 的 sold_at_is_ingest
        是同一個立場）。它們照樣入帳、照樣顯示，只是不進日期類宣稱。
        """
        res = add_card_watch(
            store, PageFetcher({}), grader="ars", grade_input="10",
            name_ja="魔法の筒", code="P4-06",
        )
        common = dict(tier="exact", title="【ARS10】魔法の筒", url="u",
                      site="buyee_yahoo", seller_id="S", price_native=7000.0,
                      currency="JPY", bid_count=None, sale_kind="unknown")
        store.upsert_card_watch_sale(res.watch_id, "y:dated",
                                     sold_at="2026-05-27T13:27:38+00:00", **common)
        for i in range(3):                       # 三筆無日期（Mercari 形態）
            store.upsert_card_watch_sale(res.watch_id, f"m:{i}", sold_at="", **common)

        d = build_dossier(store, store.get_card_watch(res.watch_id))
        assert len(d.sales) == 4                 # 四筆都留著，一筆都沒丟
        joined = "\n".join(d.recommendation)
        assert "成交檔案裡 1 次" in joined        # 次數只算有日期的那一筆
        assert "3 筆" in joined and "沒給成交時刻" in joined   # 缺口要說出來
        assert "4 次" not in joined              # 絕不把無日期的算進次數


class TestNotifyContext:
    def test_pending_only_contains_unsent_exact_and_partial(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        for key, tier in (("a:1", "exact"), ("a:2", "partial"), ("a:3", "near")):
            store.upsert_card_watch_hit(wid, key, tier=tier, title="t", url="u",
                                        site="a", seller_id="", price_native=None,
                                        currency="")
        store.mark_rule_notified([(f"{wid}:a:1", "card_snipe")])   # a:1 已送過
        ctx = build_notify_context(store)
        keys = [h["listing_key"] for h in ctx.pending]
        assert keys == ["a:2"]                                   # near 不進、已送不進
        assert ctx.pending[0]["watch"]["id"] == wid              # watch 附在 hit 上

    def test_inactive_watch_hits_are_excluded(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        store.upsert_card_watch_hit(wid, "a:1", tier="exact", title="t", url="u",
                                    site="a", seller_id="", price_native=None,
                                    currency="")
        store.deactivate_card_watch(wid)
        assert build_notify_context(store).pending == []
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_card_snipe.py -x
```

預期：`ImportError: cannot import name 'add_card_watch'`。

- [ ] **Step 3: 在 `card_snipe.py` 檔尾追加政策層**

（import 區不用改——本段用到的 `json`／`Path`／`dataclass`／`field` 都已在頂部。）

```python


# ---------------------------------------------------------------------------
# 登錄與檔案（dossier）。CLI 與 web 都呼叫這裡——判準只有一份。
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AddResult:
    watch_id: int
    messages: list[str]


def _master_card(*, code_norm: str, name_ja: str) -> dict[str, Any] | None:
    """讀卡名主檔原始 JSON 找這張卡。

    不能走 CardIndex：它把 aliases 折進內部索引後就丟掉原始卡片 dict，
    而登錄需要的正是 aliases 清單。**路徑一定要 `project_root() / …`**（照抄
    cards.py:394）：`DEFAULT_MASTER_PATH` 是相對路徑字串，直接 `Path(...)` 在
    非 repo 目錄執行 console script 時會讀不到 → aliases 空 → 「マジック・
    シリンダー」那類標題永遠 tier=None、永不推播。誤殺是靜默的。
    缺檔回 None 不爆。
    """
    from .cards import DEFAULT_MASTER_PATH
    from .config import project_root

    try:
        data = json.loads(
            (project_root() / DEFAULT_MASTER_PATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    cards = data.get("cards") or []
    if code_norm:
        for c in cards:
            if code_norm in (c.get("set_codes") or []):
                return c
    if name_ja:
        for c in cards:
            if c.get("name_ja") == name_ja:
                return c
    return None


def _ingest_census(
    store: Any, fetcher: Any, watch_id: int, *, url: str,
    grader: str, name_ja: str, code_raw: str, code_norm: str,
) -> list[str]:
    """census 抓取＋落庫（add 與 refresh 共用）。url 為空時 ARS 用卡名自動搜。

    任何失敗都不擋登錄：大聲講、URL 照存，之後 `snipe report --refresh-census` 重試。
    """
    from .ars_census import (
        CensusParseError, fetch_census, find_census_url, page_mentions,
    )
    from .sources.base import FetchError

    msgs: list[str] = []
    if not url and grader == "ARS":
        try:
            found, entries = find_census_url(name_ja, code_norm, fetcher=fetcher)
        except FetchError as exc:
            msgs.append(f"⚠️ ARS 卡名搜尋失敗（{exc}）——之後用 snipe report "
                        f"{watch_id} --refresh-census 重試")
            return msgs
        if found:
            url = found
            msgs.append(f"census 頁自動定位：{url}")
        else:
            if entries:
                msgs.append("⚠️ census 無法唯一定位（卡號對不上），候選如下——"
                            "確認後用 --census-url 指定：")
                for e in entries[:6]:
                    msgs.append(f"   {e['name']} / {e['expansion']} / {e['code']}"
                                f" → {e['url']}")
            else:
                msgs.append("⚠️ ARS 卡名搜尋 0 件——確認卡名寫法")
            return msgs
    if not url:
        if grader != "ARS":
            msgs.append(f"（{grader} 的鑑定量查詢未支援，census 留空）")
        return msgs
    try:
        counts, total, html = fetch_census(url, fetcher=fetcher)
        # ⚠️ 各級張數解析成功、卻抓不到總數 → 多半是總數那行的版型改了。
        # `census_total` 失敗是回 None（不像 parse_census 會拋），與「頁面真的
        # 沒有總數」無法區分——所以這裡要出聲，不要讓它安靜地變成 dashboard 上的
        # 「鑑定總數 None」（CLAUDE.md 第五節）。
        if counts and total is None:
            msgs.append("⚠️ 各級張數讀到了但鑑定總數沒讀到——ARS 總數那行的版型"
                        "可能改了，請人工確認 census 頁")
    except (FetchError, CensusParseError) as exc:
        store.update_card_watch_census(watch_id, census_url=url, census_json="",
                                       census_total=None)
        msgs.append(f"⚠️ census 抓取／解析失敗（{exc}）——URL 已存，"
                    f"之後 --refresh-census 重試")
        return msgs
    store.update_card_watch_census(
        watch_id, census_url=url,
        census_json=json.dumps(counts, ensure_ascii=False), census_total=total,
    )
    shown = "、".join(f"{k}: {v} 張" for k, v in counts.items() if v)
    msgs.append(f"census：{shown}（鑑定總數 {total}）")
    for needle in (code_raw, name_ja):
        if needle and not page_mentions(html, needle):
            msgs.append(f"⚠️ census 頁上找不到 {needle!r}——確認這頁是不是同一張卡")
    return msgs


def add_card_watch(
    store: Any, fetcher: Any, *, grader: str, grade_input: str, name_ja: str,
    name_en: str = "", code: str = "", census_url: str = "",
    evidence_urls: list[str] | None = None, note: str = "",
    sources: dict[str, Any] | None = None,
) -> AddResult:
    """登錄＋立刻補齊檔案（含**當下就去挖市場成交檔案**；`sources=None` 才跳過）。

    外部抓取（census／證據頁）失敗**不擋登錄**——大聲講、標狀態、照樣入庫
    （讀不到 ≠ 不存在）。輸入格式錯誤（機構不認得、分數看不懂、卡號正規化
    失敗）是 semantic 失敗：拋 ValueError，不入庫。
    """
    from .sources.base import FetchError
    from .sources.yahoo_closed import to_utc_iso
    from .yahoo_auction_page import AuctionPageError, fetch_auction_snapshot

    msgs: list[str] = []
    g = grader.strip().upper()
    if g not in ("PSA", "ARS", "BGS"):
        raise ValueError(f"不認得的鑑定機構：{grader!r}（支援 PSA / ARS / BGS）")
    label = grade_input.strip()
    try:
        grade_val = float(label.rstrip("+"))
    except ValueError as exc:
        raise ValueError(f"分數看不懂：{grade_input!r}（例：10、10+、9.5）") from exc
    if label.endswith("+"):
        msgs.append("⚠️ 標題解析把 10+ 折成 10.0（parse_grade 既定行為）："
                    "10 與 10+ 的標的都會以 🎯 通知，看標題原文分辨。")

    code_raw = code.strip()
    code_norm = ""
    if code_raw:
        codes = extract_title_codes(f" {code_raw} ")
        if not codes:
            raise ValueError(
                f"卡號正規化失敗：{code_raw!r}（預期形如 P4-06 / LOB-018）")
        code_norm = codes[0]

    name_ja = name_ja.strip()
    name_en = name_en.strip()
    aliases: list[str] = []
    master = _master_card(code_norm=code_norm, name_ja=name_ja)
    if master is not None:
        aliases = [str(a) for a in (master.get("aliases") or []) if a]
        if not name_en and master.get("name_en"):
            name_en = str(master["name_en"])
            msgs.append(f"主檔補上英文名：{name_en}")
        if master.get("name_ja") and master["name_ja"] != name_ja:
            aliases.append(str(master["name_ja"]))
        msgs.append(f"卡名主檔命中：{master.get('name_ja')}"
                    f"（別名 {len(aliases)} 個一併比對）")
    else:
        msgs.append("⚠️ 卡名主檔沒有這張卡——比對只用你提供的名字與卡號")

    watch_id = store.insert_card_watch(
        grader=g, grade=grade_val, grade_label=label, name_ja=name_ja,
        name_en=name_en, aliases=aliases,
        code_raw=code_raw, code_norm=code_norm, note=note,
    )
    msgs.append(f"已登錄狙擊 #{watch_id}：{g}{label} {name_ja} {code_raw}".rstrip())

    msgs += _ingest_census(store, fetcher, watch_id, url=census_url.strip(),
                           grader=g, name_ja=name_ja, code_raw=code_raw,
                           code_norm=code_norm)

    # 證據頁快照：入庫當下就抓（已結束頁 ~120 天會刪，實證下界 74 天）
    for ev_url in evidence_urls or []:
        ev_url = ev_url.strip()
        if not ev_url:
            continue
        if "auctions.yahoo.co.jp" not in ev_url:
            store.upsert_card_watch_evidence(
                watch_id, ev_url, status="unsupported",
                note="目前只支援 Yahoo 拍賣商品頁快照", site="")
            msgs.append(f"⚠️ 證據 URL 不是 Yahoo 拍賣頁，只存連結不解析：{ev_url}")
            continue
        try:
            snap = fetch_auction_snapshot(ev_url, fetcher=fetcher)
        except (FetchError, AuctionPageError) as exc:
            store.upsert_card_watch_evidence(watch_id, ev_url,
                                             status="unverifiable", note=str(exc))
            msgs.append(f"⚠️ 證據頁抓不到（{exc}）——已存 URL 標 unverifiable；"
                        f"讀不到 ≠ 不存在")
            continue
        store.upsert_card_watch_evidence(
            watch_id, ev_url, status="ok", title=snap.title,
            price_native=snap.price, currency=snap.currency,
            # ⚠️ 一律轉 UTC 再存：`card_watch_sale.sold_at` 是 UTC，兩張表存的是
            # 同一類事實（什麼時候賣掉的），混時區會讓 ORDER BY 的字典序排錯
            # （CLAUDE.md 第三節：同源同基準）。頁面原文是 `+09:00`。
            sold_at=to_utc_iso(snap.end_time) or snap.end_time, bids=snap.bids,
            seller_id=snap.seller_id, seller_name=snap.seller_name,
        )
        price_s = f"¥{snap.price:,.0f}" if snap.price is not None else "價格不明"
        msgs.append(f"證據快照：{snap.end_time[:10]} {price_s}"
                    f"（{snap.bids} 出價）賣家 {snap.seller_name or snap.seller_id}")

    # 市場成交檔案：登錄當下就挖。這是檔案的主要內容——我們自己的庫只有 181 天
    # 且是碰巧掃到的，市場的檔案一個請求就 150 天（實測目標卡：本地 0 筆 vs
    # 市場檔案 2 筆 exact）。挖不到不擋登錄，但要大聲講。
    if sources is not None:
        matcher = WatchMatcher.from_row(store.get_card_watch(watch_id))
        mined = mine_sold_archive(store, sources, matcher)
        msgs.append(f"市場成交檔案：{mined.summary()}")
        if not mined.ok:
            msgs.append("⚠️ 上面有管道沒挖成功——**那幾條的「0 筆」不代表沒賣過**，"
                        "之後用 `ygo-sniper snipe mine <id>` 重試")
    else:
        msgs.append("（未提供 sources，跳過市場成交檔案挖掘）")
    return AddResult(watch_id=watch_id, messages=msgs)


def refresh_watch_census(store: Any, fetcher: Any, watch: dict[str, Any]) -> list[str]:
    """重抓 census（URL 沒存到就重新用卡名搜）。"""
    return _ingest_census(
        store, fetcher, int(watch["id"]),
        url=str(watch.get("census_url") or ""),
        grader=str(watch.get("grader") or ""),
        name_ja=str(watch.get("name_ja") or ""),
        code_raw=str(watch.get("code_raw") or ""),
        code_norm=str(watch.get("code_norm") or ""),
    )


# ---------------------------------------------------------------------------
# 檔案（dossier）：census＋實證＋本地歷史＋命中帳＋等待建議
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Dossier:
    """一張卡的完整檔案。**三個資料桶的出處不同，永遠分開呈現、不合成一個數字。**

    - `sales`：市場自己的成交檔案（主要）——別人實際賣掉了多少錢
    - `local_history`：我們自己掃到的 comps／listing_obs（補充，樣本小且偏）
    - `evidence`：使用者提供的商品頁快照（人工指定，最高可信度）
    三者的分母完全不同：市場檔案是「這個平台這段期間的全部成交」，本地是
    「我們碰巧掃到的」，證據是「使用者手動指定的」。混在一起算平均就是混池。
    """

    watch: dict[str, Any]
    census: dict[str, Any] | None
    census_total: int | None
    sales: list[dict[str, Any]]
    local_history: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    hits: list[dict[str, Any]]
    recommendation: list[str]


def _census_of(watch: dict[str, Any]) -> dict[str, Any] | None:
    raw = watch.get("census_json") or ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def build_dossier(store: Any, watch: dict[str, Any]) -> Dossier:
    """檔案 ＝ 市場成交檔案（主）＋ 本地掃描歷史（補充）＋ 使用者證據。

    本地那一桶一律**現場重跑標題比對**——`comps.card_name`／`set_code` 欄位不可信
    （實測相關列全空）。三桶都逐筆列出、不做任何跨筆聚合：競標與定價不同池，
    市場檔案與我們的樣本也不同池。
    """
    m = WatchMatcher.from_row(watch)
    watch_id = int(watch["id"])
    local: list[dict[str, Any]] = []
    for ledger, rows in (
        ("comps", store.comps_title_rows()),
        ("listing_obs", store.listing_obs_title_rows()),
    ):
        for r in rows:
            tier = match_tier(m, str(r.get("title") or ""))
            if tier is None:
                continue
            h = dict(r)
            h["ledger"] = ledger
            h["tier"] = tier
            local.append(h)
    local.sort(key=lambda h: str(h.get("sold_at") or h.get("last_seen") or ""),
               reverse=True)
    sales = store.list_card_watch_sales(watch_id)
    hits = store.list_card_watch_hits(watch_id=watch_id)
    evidence = store.list_card_watch_evidence(watch_id)
    return Dossier(
        watch=watch,
        census=_census_of(watch),
        census_total=watch.get("census_total"),
        sales=sales,
        local_history=local,
        evidence=evidence,
        hits=hits,
        recommendation=build_recommendation(watch, sales, evidence, hits),
    )


def build_recommendation(
    watch: dict[str, Any], sales: list[dict[str, Any]],
    evidence: list[dict[str, Any]], hits: list[dict[str, Any]],
) -> list[str]:
    """「去哪等」的建議。純事實組裝，不是模型——每一行都可回溯到一筆紀錄。

    賣家歸因**以市場成交檔案為主**：那是「誰真的賣掉過這張卡」，比我們自己
    掃到什麼可靠得多（我們的庫實測 0 筆，市場檔案一個請求就 150 天）。
    """
    lines: list[str] = []
    sellers: dict[str, dict[str, Any]] = {}

    def _note(key: str, when: str, price: Any, src: str) -> None:
        s = sellers.setdefault(key, {"n": 0, "last": "", "prices": [], "src": src})
        s["n"] += 1
        if when > s["last"]:
            s["last"] = when
        if price is not None:
            s["prices"].append(price)

    for s in sales:
        if s.get("tier") == TIER_EXACT and s.get("seller_id"):
            _note(f"{s.get('site') or ''}:{s['seller_id']}",
                  str(s.get("sold_at") or ""), s.get("price_native"), "成交檔案")
    for e in evidence:
        if e.get("status") == "ok" and e.get("seller_id"):
            _note(f"{e.get('site') or 'buyee_yahoo'}:{e['seller_id']}",
                  str(e.get("sold_at") or ""), e.get("price_native"), "使用者證據")
    for h in hits:
        if h.get("tier") == TIER_EXACT and h.get("seller_id"):
            _note(f"{h.get('site') or ''}:{h['seller_id']}",
                  str(h.get("last_seen") or ""), h.get("price_native"), "在架命中")

    for key, s in sorted(sellers.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        prices = "、".join(f"{p:,.0f}" for p in sorted(s["prices"])) or "價格不明"
        lines.append(
            f"賣家 {key} 賣掉過這張卡 {s['n']} 次（最近 {s['last'][:10]}；"
            f"成交 {prices}）——建議釘選（每批都掃、不佔名額）："
            f"ygo-sniper watch-seller pin {key}"
        )
    if not sellers:
        lines.append(
            "成交檔案裡還沒有可歸因的賣家——靠全市場關鍵字掃描等它出現"
            "（狙擊查詢已自動加入每一輪掃描）。"
        )

    exact_sales = [s for s in sales if s.get("tier") == TIER_EXACT]
    if exact_sales:
        # ⚠️ **只有帶真實成交時刻的才進「什麼時候／幾次」的宣稱。**
        # Mercari／露天的搜尋頁給不出落札時間（實測 99/206 筆 sold_at 是空的），
        # 把它們算進次數或期間，就是拿兩種基準的東西合成一個數字
        # （CLAUDE.md 第三節；comps 的 sold_at_is_ingest 是同一個立場）。
        dated = [s for s in exact_sales if s.get("sold_at")]
        undated_n = len(exact_sales) - len(dated)
        stamps = sorted(str(s.get("sold_at"))[:10] for s in dated)
        kinds = {s.get("sale_kind") for s in dated}
        span = f"（{stamps[0]} → {stamps[-1]}）" if stamps else ""
        if dated:
            # 這句**只要有帶日期的成交就一定要講**（不是只在 >= 2 筆時）：一筆的時候
            # 更需要知道那不是全部歷史，否則會把「檔案裡只有 1 次」讀成「一年只出現 1 次」。
            lines.append(
                f"出現頻率：成交檔案裡 {len(dated)} 次{span}"
                f"——**這是檔案涵蓋期間內的次數，不是全部歷史**（Yahoo 落札相場約"
                f"保留 150-180 天，更早的已經被平台刪掉，我們挖不到）。"
            )
        if undated_n:
            lines.append(
                f"另有 {undated_n} 筆同款成交**來源沒給成交時刻**（Mercari／露天的"
                f"搜尋頁沒有落札時間）——它們只答得出價格，不列入上面的次數與期間。"
            )
        if kinds == {"auction"}:
            lines.append(
                "全部走競標成交——結標時段（台灣 18:00-22:30）是關鍵，"
                "排程在該時段每 30 分掃一次。"
            )
    census = _census_of(watch)
    if census:
        at = census.get(str(watch.get("grade_label") or ""))
        if at is not None:
            lines.append(
                f"稀缺度：{watch['grader']}{watch['grade_label']} 全世界只有 "
                f"{at} 張——每次出現可能相隔數月，👀 疑似命中也值得點開看。"
            )
    lines.append(
        "節奏：結標高峰（台灣 18:00-22:30）每 30 分掃一次、白天每 2 小時；"
        "狙擊命中即推播且 🎯 不受每輪則數上限裁切——最壞情況約晚 30 分鐘知道。"
    )
    return lines


# ---------------------------------------------------------------------------
# 通知脈絡（規則 4）。evaluate() 只吃這個形狀，不自己查 db。
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SnipeNotifyContext:
    """規則 4（指定卡狙擊）判定所需的全部事實。

    pending 只含「還沒送過的 exact／partial」——near 在源頭就不進通知路徑
    （它的去處是 dashboard），去重帳只有 notify_log 一本。
    """

    watches: dict[int, dict[str, Any]] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)


def build_notify_context(store: Any) -> SnipeNotifyContext:
    ctx = SnipeNotifyContext()
    ctx.watches = {int(w["id"]): w for w in store.list_card_watch(active_only=True)}
    if not ctx.watches:
        return ctx
    for hit in store.list_card_watch_hits(tiers=(TIER_EXACT, TIER_PARTIAL)):
        if hit.get("sent_at"):
            continue
        w = ctx.watches.get(int(hit["watch_id"]))
        if w is None:            # watch 已停用：命中留帳，但不再通知
            continue
        row = dict(hit)
        row["watch"] = w
        ctx.pending.append(row)
    return ctx
```

- [ ] **Step 4: 跑測試看它綠**

```bash
.venv/bin/pytest tests/test_card_snipe.py -x
```

預期：`51 passed`（42 ＋ 政策層 8 ＋ Task 2 審查新增 1）。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/card_snipe.py tests/test_card_snipe.py
git commit -m "feat(snipe): 登錄／census 併入／dossier／等待建議／通知脈絡（抓取失敗不擋登錄、大聲標記）"
```

---

### Task 6: 通知——規則 4、去重與上限、formatter

**Files:**
- Modify: `src/ygo_sniper/notify_rules.py`（RULE 常數 :112、`Outcome` :459、`evaluate` :499、`_apply_dedupe_and_cap` :1026、`__all__` :1136）
- Modify: `src/ygo_sniper/notify.py`（`render` :788、`format_seller_unpriced` 之後加 formatter）
- Test: `tests/test_card_snipe_notify.py`

- [ ] **Step 1: 寫失敗測試 `tests/test_card_snipe_notify.py`**

```python
"""規則 4（指定卡狙擊）：終身去重、🎯 不受總量上限、👀 有小上限、formatter。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from ygo_sniper.card_snipe import build_notify_context
from ygo_sniper.notify_rules import (
    RULE_CARD_SNIPE,
    NotifyRules,
    evaluate,
)
from ygo_sniper.store import Store

WATCH_KW = dict(
    grader="ARS", grade=10.0, grade_label="10",
    name_ja="魔法の筒", name_en="Magic Cylinder",
    aliases=["マジック・シリンダー"], code_raw="P4-06", code_norm="P4-6",
)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def _hit(store, wid, key, tier="exact", title="【ARS10】魔法の筒 P4-06"):
    store.upsert_card_watch_hit(
        wid, key, tier=tier, title=title, url=f"https://example.test/{key}",
        site="buyee_yahoo", seller_id="s1", price_native=50000.0, currency="JPY",
        end_time="2026-08-09T22:00:00+09:00",
    )


def _rules(cfg, **overrides):
    return replace(NotifyRules.from_config(cfg), **overrides)


class TestRule4:
    def test_exact_hit_flows_to_send_and_dedupes_for_life(self, store, cfg):
        wid = store.insert_card_watch(**WATCH_KW)
        _hit(store, wid, "buyee_yahoo:x1")
        rules = _rules(cfg)
        out = evaluate([], rules=rules, notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        assert [m.rule for m in out.to_send] == [RULE_CARD_SNIPE]
        assert out.to_send[0].key == f"{wid}:buyee_yahoo:x1"
        # 模擬送成功落帳 → 之後每一輪都不再送（終身一次）
        store.mark_rule_notified([(out.to_send[0].key, RULE_CARD_SNIPE)])
        out2 = evaluate([], rules=rules, notified=store.notify_log_map(),
                        snipe_ctx=build_notify_context(store))
        assert out2.to_send == []

    def test_exact_is_exempt_from_global_cap(self, store, cfg):
        wid = store.insert_card_watch(**WATCH_KW)
        for i in range(3):
            _hit(store, wid, f"buyee_yahoo:x{i}")
        out = evaluate([], rules=_rules(cfg, max_items_per_run=1),
                       notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        # cap=1 也擋不住狙擊命中：三筆全部要送
        assert len([m for m in out.to_send if m.rule == RULE_CARD_SNIPE]) == 3

    def test_partial_has_its_own_small_cap(self, store, cfg):
        from ygo_sniper.card_snipe import PARTIAL_MAX_PER_RUN

        wid = store.insert_card_watch(**WATCH_KW)
        for i in range(PARTIAL_MAX_PER_RUN + 2):
            _hit(store, wid, f"buyee_yahoo:p{i}", tier="partial")
        out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        sent = [m for m in out.to_send if m.rule == RULE_CARD_SNIPE]
        assert len(sent) == PARTIAL_MAX_PER_RUN
        # 溢出的有講（skipped），沒落帳 → 下輪還會排隊
        assert len(out.skips_for("疑似命中已達上限")) == 2

    def test_no_ctx_no_crash(self, cfg):
        out = evaluate([], rules=_rules(cfg), notified={}, snipe_ctx=None)
        assert out.card_snipe == [] and out.to_send == []


class TestFormatter:
    def test_message_contains_the_essentials(self, store, cfg):
        wid = store.insert_card_watch(**WATCH_KW)
        store.update_card_watch_census(
            wid, census_url="u", census_json='{"9": 5, "10": 5, "10+": 1}',
            census_total=11)
        _hit(store, wid, "buyee_yahoo:x1")
        out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        from ygo_sniper.notify import format_card_snipe

        text = format_card_snipe(out.to_send[0], "http://127.0.0.1:8321")
        assert "🎯" in text
        assert "ARS10 魔法の筒 P4-06" in text
        assert "【ARS10】魔法の筒 P4-06" in text          # 標題原文
        assert "全世界 5 張" in text                       # census
        assert "https://example.test/buyee_yahoo:x1" in text
        assert "JPY 50,000" in text
        assert "非成交價" in text                          # 現在価格語意講清楚

    def test_partial_message_is_marked(self, store, cfg):
        wid = store.insert_card_watch(**WATCH_KW)
        _hit(store, wid, "buyee_yahoo:p1", tier="partial",
             title="魔法の筒 ARS鑑定品")
        out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                       snipe_ctx=build_notify_context(store))
        from ygo_sniper.notify import format_card_snipe

        text = format_card_snipe(out.to_send[0], "http://x")
        assert "👀" in text and "未全符" in text
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_card_snipe_notify.py -x
```

預期：`ImportError: cannot import name 'RULE_CARD_SNIPE'`。

- [ ] **Step 3: 改 `notify_rules.py`（四處）**

(a) RULE 常數區（:112，`RULE_SELLER_UNPRICED = "seller_unpriced"` 之後）加：

```python
RULE_CARD_SNIPE = "card_snipe"
```

`RULE_LABEL` dict 加一行：

```python
    RULE_CARD_SNIPE: "規則 4 指定卡狙擊",
```

(b) `Outcome` dataclass（:459）：在 `seller_unpriced: list[Match] = field(default_factory=list)` 之後加欄位：

```python
    #: 規則 4：指定卡狙擊命中（exact＋partial；near 在脈絡層就不進來）。
    card_snipe: list[Match] = field(default_factory=list)
```

`matched` property 的回傳改成（狙擊是有判斷的命中）：

```python
        return (len(self.urgent) + len(self.high_p) + len(self.seller_new)
                + len(self.card_snipe))
```

(c) `evaluate()`（:499）：簽名加一個 keyword 參數（放在 `seller_ctx` 之後）：

```python
    seller_ctx: Any = None,
    snipe_ctx: Any = None,
) -> Outcome:
```

主迴圈 `for row in rows:` 結束之後、`_apply_dedupe_and_cap(out, rules, notified, now)` 之前插入：

```python
    # 規則 4：狙擊命中不來自 signals 候選池（它們在商業過濾前就被記帳），
    # 由 snipe_ctx 帶進來。pending 已在脈絡層濾掉 near 與已送過的。
    if snipe_ctx is not None:
        for hit in snipe_ctx.pending:
            # ⚠️ 不要傳 title=：`Match.title` 是唯讀 property（notify_rules.py:441），
            # 從 row["title"] 推導。傳了會 TypeError。
            out.card_snipe.append(Match(
                key=f"{hit['watch_id']}:{hit['listing_key']}",
                rule=RULE_CARD_SNIPE,
                row=hit,
            ))
```

(d) `_apply_dedupe_and_cap`（:1026）：在 `sendable.extend(_unpriced_sendable(out, rules, notified))` 之後、`cap = rules.max_items_per_run` 之前插入：

```python
    # --- 規則 4：終身一次；🎯 exact 不受總量上限，👀 partial 有自己的小上限 ---
    from .card_snipe import PARTIAL_MAX_PER_RUN, TIER_EXACT

    snipe_exact: list[Match] = []
    snipe_partial: list[Match] = []
    for m in out.card_snipe:
        if (m.key, RULE_CARD_SNIPE) in notified:
            out.deduped += 1
            continue
        if str(m.row.get("tier") or "") == TIER_EXACT:
            snipe_exact.append(m)
        else:
            snipe_partial.append(m)
    if len(snipe_partial) > PARTIAL_MAX_PER_RUN:
        for m in snipe_partial[PARTIAL_MAX_PER_RUN:]:
            out.skipped.append(Skip(
                m.key, m.title, RULE_CARD_SNIPE,
                f"本輪 👀 疑似命中已達上限 {PARTIAL_MAX_PER_RUN} 則——"
                "未送、沒落帳，下輪重新排隊；dashboard 狙擊分頁看得到全部",
            ))
        snipe_partial = snipe_partial[:PARTIAL_MAX_PER_RUN]
    snipe_sendable = snipe_exact + snipe_partial
```

同函式結尾的 cap 段改成（狙擊排最前、exact/partial 都不吃 cap——它們有自己的音量控制）：

```python
    cap = rules.max_items_per_run
    if cap <= 0:
        out.to_send = snipe_sendable + sendable
        out.overflow = []
        return
    out.to_send = snipe_sendable + sendable[:cap]
    out.overflow = sendable[cap:]
```

(e) `__all__`（:1136）加 `"RULE_CARD_SNIPE",`（照字母序放在 `RULE_AUCTION_URGENT` 之後）。

- [ ] **Step 4: 改 `notify.py`（兩處）**

(a) 在 `format_seller_unpriced`（:433）的函式結束之後、`format_overflow`（:468）之前插入：

```python
def format_card_snipe(match, dashboard_url: str) -> str:
    """規則 4：指定卡狙擊。回答「你在等的那張卡出現了：在哪、多少錢、多快結標」。

    exact 與 partial 共用一支：差別只在標頭（🎯／👀）。價格語意要講清楚——
    競標的「現在価格」會漲，不是可成交價（第一課的教訓）。
    """
    row = match.row
    w = row.get("watch") or {}
    tier = str(row.get("tier") or "")
    label = f"{w.get('grader', '')}{w.get('grade_label', '')} {w.get('name_ja', '')}"
    code = w.get("code_raw") or w.get("code_norm") or ""
    if code:
        label += f" {code}"
    head = "🎯 狙擊命中" if tier == "exact" else "👀 狙擊疑似（同卡，條件未全符）"
    lines = [f"<b>{head}</b>｜{_esc(label)}", _esc(str(row.get("title") or ""))]
    price = row.get("price_native")
    if price is not None:
        lines.append(
            f"價格：{_esc(str(row.get('currency') or ''))} {price:,.0f}"
            "（現在価格／售價，非成交價）"
        )
    venue = _esc(str(row.get("site") or ""))
    seller = _esc(str(row.get("seller_id") or ""))
    lines.append(f"平台：{venue}" + (f"｜賣家：{seller}" if seller else ""))
    if row.get("end_time"):
        lines.append(f"結標：{_esc(str(row.get('end_time'))[:16].replace('T', ' '))}")
    census = _snipe_census_line(w)
    if census:
        lines.append(census)
    url = str(row.get("url") or "")
    if url:
        lines.append(f'<a href="{url}">商品頁</a>')
    lines.append(f'<a href="{dashboard_url}">dashboard</a> 🎯 狙擊分頁看完整檔案')
    return "\n".join(lines)


def _snipe_census_line(watch: dict) -> str:
    """census_json → 一行存世量。沒抓過就回空字串——證據不足不硬湊數字。"""
    import json as _json

    raw = watch.get("census_json") or ""
    if not raw:
        return ""
    try:
        counts = _json.loads(raw)
    except ValueError:
        return ""
    tgt = str(watch.get("grade_label") or "")
    at = counts.get(tgt)
    if at is None:
        return ""
    total = watch.get("census_total")
    tail = f"（鑑定總數 {total}）" if total else ""
    return f"存世量：{watch.get('grader', '')}{tgt} 全世界 {at} 張{tail}"
```

(b) `render()`（:788）：import 區加 `RULE_CARD_SNIPE`，並在 `if match.rule == RULE_AUCTION_URGENT:` 之前加分派：

```python
        from .notify_rules import (
            RULE_AUCTION_URGENT,
            RULE_CARD_SNIPE,
            RULE_SELLER_NEW,
            RULE_SELLER_UNPRICED,
        )

        if match.rule == RULE_CARD_SNIPE:
            return format_card_snipe(match, self.dashboard_url)
```

- [ ] **Step 4b: 讓規則 4 出現在計數表與 notify-preview（不做這步＝靜默失敗）**

`cli.py:65` 的 `_print_rule_counts` 與 `cli.py:3111` 的 `notify-preview` 規則表都**硬列舉**四條既有規則。不補這一步，規則 4 的命中數永遠不印——「命中 0 筆」與「掛鉤根本沒跑」外顯一模一樣，正是 `_print_rule_counts` 自己的 docstring 要防的事（CLAUDE.md 第五節）。

(a) `cli.py:71` 的 import 加 `RULE_CARD_SNIPE,`（照字母序在 `RULE_AUCTION_URGENT` 之後）。

(b) `_print_rule_counts` 的 `console.print(...)` f-string，在 `｜ 送出 {len(outcome.sent)} 則` 之前插入一段：

```python
        f"[bold]{RULE_LABEL[RULE_CARD_SNIPE]}[/bold] 命中 "
        f"{len(outcome.card_snipe)} 筆"
        f"（🎯 {sum(1 for m in outcome.card_snipe if m.row.get('tier') == 'exact')}"
        f"／👀 {sum(1 for m in outcome.card_snipe if m.row.get('tier') == 'partial')}）"
        f" ｜ "
```

(c) `notify-preview`（`cli.py:3111`）的 `for rule, matches in (...)` tuple 末尾加一行：

```python
            (RULE_CARD_SNIPE, outcome.card_snipe),
```

並確認該函式的 import 區含 `RULE_CARD_SNIPE`。

⚠️ **同時必須加 detail 分支，否則第一次真的命中就整支炸掉。** 該迴圈的 `else`
分支是 `detail = f"P={m.p_worth:.0%}｜稀有度 …"`（`cli.py:3151`），而狙擊的 Match
只帶 `key/rule/row`，`p_worth` 是 `None`——實測 `f"{None:.0%}"` 直接
`TypeError: unsupported format string passed to NoneType.__format__`。
命中 0 筆時迴圈體不執行，所以**驗收會全綠、等真的有一張 ARS10 P4-06 上架那天才爆**。
在 `else:` 之前插入：

```python
                elif rule == RULE_CARD_SNIPE:
                    w = m.row.get("watch") or {}
                    price = m.row.get("price_native")
                    price_s = (f"{m.row.get('currency') or ''} {price:,.0f}"
                               if price is not None else "價格不明")
                    mark = "🎯" if m.row.get("tier") == "exact" else "👀"
                    detail = (f"{mark} {w.get('grader', '')}{w.get('grade_label', '')} "
                              f"{w.get('name_ja', '')}｜{price_s}")

(d) 在 `tests/test_card_snipe_notify.py` 末尾追加測試：

```python
def test_rule4_appears_in_the_cli_counts(store, cfg, capsys):
    """規則 4 的命中數必須印得出來——0 與「沒在跑」不能長一樣。"""
    import ygo_sniper.cli as cli_mod

    wid = store.insert_card_watch(**WATCH_KW)
    _hit(store, wid, "buyee_yahoo:x1")
    out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                   snipe_ctx=build_notify_context(store))
    cli_mod._print_rule_counts(out)
    printed = capsys.readouterr().out
    assert "指定卡狙擊" in printed
    assert "🎯 1" in printed


def test_store_and_notify_rules_agree_on_the_rule_name():
    """`store.CARD_SNIPE_RULE` 與 `notify_rules.RULE_CARD_SNIPE` 必須是同一個值。

    兩份定義漂移的話，`list_card_watch_hits` 的 `sent_at` 會恆為 NULL
    → 每輪都判定「這筆沒送過」而重複推播，而且**壞掉的樣子與「真的還沒送過」
    完全一樣**（CLAUDE.md 第五節）。這條測試就是那個結構性守門員——
    不能改成註解或提醒（CLAUDE.md 的 meta-rule：別用更多流程補流程漏洞）。
    """
    from ygo_sniper.store import CARD_SNIPE_RULE

    assert CARD_SNIPE_RULE == RULE_CARD_SNIPE


def test_preview_table_renders_a_snipe_hit_without_crashing(store, cfg, capsys):
    """規則 4 的 Match 沒有 p_worth——preview 的 else 分支會 TypeError。
    命中 0 筆時這個迴圈根本不執行，所以只有這條測試擋得住它。"""
    import ygo_sniper.cli as cli_mod
    from ygo_sniper.notify_rules import RULE_CARD_SNIPE, RULE_LABEL

    wid = store.insert_card_watch(**WATCH_KW)
    _hit(store, wid, "buyee_yahoo:x1")
    out = evaluate([], rules=_rules(cfg), notified=store.notify_log_map(),
                   snipe_ctx=build_notify_context(store))
    m = out.to_send[0]
    assert m.p_worth is None            # 正是會炸的前提
    w = m.row.get("watch") or {}
    price = m.row.get("price_native")
    price_s = (f"{m.row.get('currency') or ''} {price:,.0f}"
               if price is not None else "價格不明")
    mark = "🎯" if m.row.get("tier") == "exact" else "👀"
    detail = (f"{mark} {w.get('grader', '')}{w.get('grade_label', '')} "
              f"{w.get('name_ja', '')}｜{price_s}")
    assert "🎯" in detail and "ARS10" in detail and "JPY 50,000" in detail
    assert RULE_LABEL[RULE_CARD_SNIPE] == "規則 4 指定卡狙擊"
```

- [ ] **Step 5: 跑測試看它綠＋既有通知測試回歸**

```bash
.venv/bin/pytest tests/test_card_snipe_notify.py -x
.venv/bin/pytest tests/ -k "notify"
.venv/bin/ygo-sniper notify-preview 2>&1 | grep -c "指定卡狙擊"   # 預期 >= 1
```

預期：新測試 `9 passed`（含 Step 4b 的計數表、rule 名一致性、preview 不炸三條）；既有 notify 相關測試全綠（`evaluate` 新參數有預設值、`Outcome` 新欄位預設空、cap 語意對既有規則不變）。

- [ ] **Step 6: Commit**

```bash
git add src/ygo_sniper/notify_rules.py src/ygo_sniper/notify.py tests/test_card_snipe_notify.py
git commit -m "feat(snipe): 通知規則 4——終身去重、🎯 不受總量上限、👀 每輪小上限、價格語意標明非成交價"
```

---

### Task 7: pipeline 掛鉤（過濾前比對＋查詢注入＋回收＋通知脈絡）

**Files:**
- Modify: `src/ygo_sniper/pipeline.py`（`__init__` :202、`_collect_candidates` :448、`_scan` :699/:732/入庫段 :860 附近、`notification_outcome` :958）
- Test: `tests/test_card_snipe.py`（追加）

- [ ] **Step 1: 追加失敗測試到 `tests/test_card_snipe.py`**

```python
@pytest.fixture
def no_fx_network(monkeypatch):
    """`Pipeline()` 會建 FxRates，而 FxRates.__init__ → _load() 在 fx.json 過期時
    會發**真實 httpx 請求**（fx.py:34→47，且失敗會靜靜吞掉）。測試絕不碰真實世界。"""
    from ygo_sniper.fx import FxRates

    monkeypatch.setattr(FxRates, "refresh", lambda self: None)


class TestPipelineHook:
    def test_hits_are_recorded_before_business_filter(self, tmp_path, no_fx_network):
        """被排除字丟掉的標的，狙擊照樣入帳——比對在過濾之前。"""
        from dataclasses import replace as dc_replace

        import ygo_sniper.config as config_mod
        from ygo_sniper.domain import Currency, Listing, Site
        from ygo_sniper.pipeline import Pipeline

        config_mod.load_config.cache_clear()
        base = config_mod.load_config()
        cfg = dc_replace(base, storage={**base.storage,
                                        "db_path": str(tmp_path / "p.db")})
        pipe = Pipeline(cfg)
        try:
            wid = pipe.store.insert_card_watch(**WATCH_KW)
            # 排除字 ポケモン：is_candidate 一定丟掉它（實測 '排除字 ポケモン'）
            lst = Listing(site=Site.BUYEE_YAHOO, external_id="k1",
                          title="【ARS10】魔法の筒 P4-06 ポケモンカード",
                          url="https://example.test/k1",
                          price=1000.0, currency=Currency.JPY, seller_id="s9")
            candidates: list = []
            pipe._snipe_write = True
            pipe._collect_candidates([lst], "test", candidates)
            assert candidates == []                              # 商業過濾確實丟了它
            hits = pipe.store.list_card_watch_hits(watch_id=wid)
            assert len(hits) == 1 and hits[0]["tier"] == "exact"  # 但狙擊帳有
        finally:
            pipe.close()
            config_mod.load_config.cache_clear()

    def test_dry_run_does_not_write_hits(self, tmp_path, no_fx_network):
        from dataclasses import replace as dc_replace

        import ygo_sniper.config as config_mod
        from ygo_sniper.domain import Currency, Listing, Site
        from ygo_sniper.pipeline import Pipeline

        config_mod.load_config.cache_clear()
        base = config_mod.load_config()
        cfg = dc_replace(base, storage={**base.storage,
                                        "db_path": str(tmp_path / "p.db")})
        pipe = Pipeline(cfg)
        try:
            wid = pipe.store.insert_card_watch(**WATCH_KW)
            lst = Listing(site=Site.BUYEE_YAHOO, external_id="k1",
                          title="【ARS10】魔法の筒 P4-06",
                          url="https://example.test/k1",
                          price=1000.0, currency=Currency.JPY)
            pipe._snipe_write = False                            # dry-run 的旗標
            pipe._collect_candidates([lst], "test", [])
            assert pipe.store.list_card_watch_hits(watch_id=wid) == []
        finally:
            pipe.close()
            config_mod.load_config.cache_clear()
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_card_snipe.py::TestPipelineHook -x
```

預期：第一個測試 fail（hits 是空的——掛鉤還不存在）。

- [ ] **Step 3: 實作 pipeline 掛鉤（五處小改）**

(a) `Pipeline.__init__`（:202）：在 `self._valuator = None` 之後加：

```python
        #: 狙擊比對器（一輪掃描建一次；lazy——沒有 active watch 時是空 list）。
        self._snipe_cache = None
        #: dry_run 時不寫狙擊帳（_scan 設定；預設 True 讓單獨呼叫也能寫）。
        self._snipe_write = True
```

(b) 新增方法（放在 `_collect_candidates` 前面）：

```python
    def _snipe_matchers(self):
        if self._snipe_cache is None:
            from .card_snipe import load_matchers

            self._snipe_cache = load_matchers(self.store)
        return self._snipe_cache
```

(c) `_collect_candidates`（:448）：在 docstring 之後、`wl = self.cfg.watchlist` 之前插入：

```python
        # 狙擊比對走在商業過濾之前：狙擊目標不能被排除字／年代／min_grade 吃掉
        # （關鍵字掃描與賣家頁列舉都走這一支，掛在這裡兩條路都蓋到）。
        matchers = self._snipe_matchers()
        if matchers and self._snipe_write:
            from .card_snipe import observe_listings

            observe_listings(self.store, matchers, listings,
                             source_name=source_name)
```

(d) `_scan`（:699）：在 `wl = self.cfg.watchlist`（:723）之後加一行：

```python
        self._snipe_write = not dry_run
```

`for query in (load_queries(wl) if not watch_only else []):`（:732）改成：

```python
        base_queries = load_queries(wl) if not watch_only else []
        if base_queries:
            from .card_snipe import scan_queries

            base_queries = base_queries + scan_queries(
                self._snipe_matchers(), base_queries
            )
        for query in base_queries:
```

入庫段（:860 附近）`obs_pruned = self.store.prune_listing_obs(…)` 之後加：

```python
            from .card_snipe import NEAR_HIT_RETAIN_DAYS, TIER_NEAR

            # tier 是必填關鍵字參數：保留政策屬於 card_snipe，store 只做 CRUD
            self.store.prune_card_watch_hits(NEAR_HIT_RETAIN_DAYS, tier=TIER_NEAR)
```

(e) `notification_outcome`（:958）：`evaluate(...)` 呼叫加一個參數（`seller_ctx=…` 之後）：

```python
            seller_ctx=self._seller_notify_context(),
            snipe_ctx=self._snipe_notify_context(),
        )
```

並在 `_seller_notify_context` 方法之後新增：

```python
    def _snipe_notify_context(self):
        """規則 4 的資料脈絡。建不起來就跳過該規則，不拖垮整輪推播。"""
        from .card_snipe import build_notify_context

        try:
            return build_notify_context(self.store)
        except Exception as exc:  # noqa: BLE001 - 同 _seller_notify_context 的立場
            print(f"[warn] 狙擊脈絡建立失敗，本輪規則 4 跳過："
                  f"{type(exc).__name__}: {exc}")
            return None
```

- [ ] **Step 4: 跑測試看它綠＋pipeline 回歸**

```bash
.venv/bin/pytest tests/test_card_snipe.py -x
.venv/bin/pytest tests/ -k "pipeline or scan"
```

預期：`53 passed`（51 ＋ pipeline 掛鉤 2）；既有 pipeline 測試全綠。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/pipeline.py tests/test_card_snipe.py
git commit -m "feat(snipe): pipeline 掛鉤——過濾前比對、狙擊查詢自動注入、dry-run 不寫帳、near 定期回收"
```

---

### Task 8: CLI 群組 `snipe`

**Files:**
- Modify: `src/ygo_sniper/cli.py`（watch-seller 群組之後，約 :1434 `watch_scan` 之前）
- Test: `tests/test_card_snipe.py`（追加）

- [ ] **Step 1: 追加失敗測試**

```python
class TestCli:
    @pytest.fixture
    def cli_env(self, tmp_path, monkeypatch):
        """臨時 db 的 CLI 環境（照抄 test_seller_watch.py 的模式）。"""
        from dataclasses import replace as dc_replace

        import ygo_sniper.cli as cli_mod
        import ygo_sniper.config as config_mod

        db = tmp_path / "cli.db"
        config_mod.load_config.cache_clear()
        base = config_mod.load_config()
        test_cfg = dc_replace(base, storage={**base.storage, "db_path": str(db)})
        monkeypatch.setattr(cli_mod, "load_config", lambda: test_cfg)
        assert test_cfg.db_path == db, "CLI 測試的 cfg 沒有指到 tmp db"

        from typer.testing import CliRunner

        yield CliRunner(), Store(db), cli_mod
        config_mod.load_config.cache_clear()

    def test_add_list_report_remove_roundtrip(self, cli_env):
        runner, store, cli_mod = cli_env
        # --no-mine ＋ PSA ＋ 無 evidence → 完全不打網路
        r = runner.invoke(cli_mod.app, [
            "snipe", "add", "魔法の筒", "--grader", "PSA", "--grade", "10",
            "--code", "P4-06", "--no-mine",
        ])
        assert r.exit_code == 0, r.output
        assert "已登錄狙擊 #1" in r.output
        assert "PSA 的鑑定量查詢未支援" in r.output
        assert "跳過市場成交檔案挖掘" in r.output

        r = runner.invoke(cli_mod.app, ["snipe", "list"])
        assert r.exit_code == 0 and "魔法の筒" in r.output

        r = runner.invoke(cli_mod.app, ["snipe", "report", "1"])
        assert r.exit_code == 0
        assert "等待建議" in r.output

        r = runner.invoke(cli_mod.app, ["snipe", "remove", "1"])
        assert r.exit_code == 0
        assert store.list_card_watch(active_only=True) == []

    def test_add_rejects_bad_grader_loudly(self, cli_env):
        runner, _store, cli_mod = cli_env
        r = runner.invoke(cli_mod.app, [
            "snipe", "add", "x", "--grader", "CGC", "--grade", "10", "--no-mine",
        ])
        assert r.exit_code == 1
        assert "不認得的鑑定機構" in r.output

    def test_mine_reports_blocked_channels_loudly_and_exits_1(self, cli_env, monkeypatch):
        """挖不到必須大聲＋非 0 離開碼——「0 筆」與「被擋」不能長一樣。"""
        import ygo_sniper.card_snipe as snipe_mod
        import ygo_sniper.sources as sources_mod

        runner, store, cli_mod = cli_env
        store.insert_card_watch(**WATCH_KW)
        monkeypatch.setattr(sources_mod, "build_sources", lambda cfg, f=None: {})
        monkeypatch.setattr(
            snipe_mod, "mine_sold_archive",
            lambda *a, **kw: snipe_mod.MineResult(
                ok=False, problems=["yahoo_closed／魔法の筒：BLOCKED（WAF）"]),
        )
        r = runner.invoke(cli_mod.app, ["snipe", "mine"])
        assert r.exit_code == 1
        assert "BLOCKED" in r.output
        assert "不代表沒賣過" in r.output

    def test_report_unknown_id_exits_1(self, cli_env):
        runner, _store, cli_mod = cli_env
        r = runner.invoke(cli_mod.app, ["snipe", "report", "99"])
        assert r.exit_code == 1
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_card_snipe.py::TestCli -x
```

預期：fail（`snipe` 指令不存在，exit code 2）。

- [ ] **Step 3: 在 `cli.py` 實作（放在 `watch_seller_unpin` 之後、`watch_scan`（:1434）之前）**

```python
# ---------------------------------------------------------------------------
# 指定卡狙擊（card_watch）
# ---------------------------------------------------------------------------
snipe_app = typer.Typer(
    add_completion=False,
    help="指定卡狙擊：登錄特定卡（鑑定機構＋分數＋卡名＋卡號），出現就推播",
)
app.add_typer(snipe_app, name="snipe")


def _print_dossier(store: Store, watch_id: int) -> None:
    from .card_snipe import build_dossier

    w = store.get_card_watch(watch_id)
    d = build_dossier(store, w)
    console.print(
        f"\n[bold]🎯 狙擊 #{watch_id}：{w['grader']}{w['grade_label']} "
        f"{w['name_ja']} {w['code_raw']}[/bold]".rstrip()
    )
    if d.census:
        parts = "、".join(f"{k}: {v} 張" for k, v in d.census.items() if v)
        console.print(f"存世量（ARS census）：{parts}（鑑定總數 {d.census_total}）")
    else:
        console.print("存世量：未抓到（snipe report --refresh-census 重試）")
    if d.evidence:
        console.print("\n[bold]實證紀錄（你提供的結標頁快照）[/bold]")
        for e in d.evidence:
            if e["status"] == "ok":
                price = (f"¥{e['price_native']:,.0f}"
                         if e["price_native"] is not None else "價格不明")
                console.print(
                    f"  {str(e['sold_at'])[:10]} {price}（{e['bids']} 出價）"
                    f"賣家 {e['seller_name'] or e['seller_id']}  {e['url']}"
                )
            else:
                console.print(f"  ⚠️ [{e['status']}] {e['url']}（{e['note']}）")
    # ── 主要資料桶：市場自己的成交檔案 ──
    if d.sales:
        exact = [s for s in d.sales if s["tier"] == "exact"]
        console.print(
            f"\n[bold]💰 市場成交檔案[/bold]（共 {len(d.sales)} 筆命中，"
            f"其中 🎯 同款 {len(exact)} 筆）"
        )
        console.print("[dim]這是平台自己的落札紀錄，不是我們掃到的。"
                      "Yahoo 落札相場約保留 150-180 天，更早的平台已刪除。[/dim]")
        for s in d.sales:
            if s["tier"] == "near":
                continue     # near 量大（實測一次挖掘 202/206），只在 dashboard 全量顯示
            mark = "🎯" if s["tier"] == "exact" else "👀"
            price = s.get("price_native")
            price_s = (f"{s.get('currency', '')} {price:,.0f}"
                       if price is not None else "—")
            kind = {"auction": "競標", "fixed": "定價"}.get(s.get("sale_kind"), "型態不明")
            # 沒有成交時刻的來源（Mercari／露天）不要印一個空日期讓人以為是資料壞了，
            # 明講「日期不明」——它只答得出價格。
            when = str(s["sold_at"])[:10] if s.get("sold_at") else "日期不明　"
            console.print(
                f"  {mark} {when} {price_s:>12}（{kind}"
                f"{'／' + str(s['bid_count']) + ' 出價' if s.get('bid_count') and s.get('sale_kind') == 'auction' else ''}）"
                f" 賣家 {str(s.get('seller_id') or '—')[:16]}"
            )
            console.print(f"      {str(s['title'])[:66]}")
        near_n = sum(1 for s in d.sales if s["tier"] == "near")
        if near_n:
            console.print(f"  [dim]（另有 {near_n} 筆同名但機構／分數不符，"
                          f"dashboard 狙擊分頁看得到全部）[/dim]")
    else:
        console.print("\n[yellow]💰 市場成交檔案：還沒挖過，或檔案期間內沒有成交"
                      "——跑 `ygo-sniper snipe mine <id>` 確認是哪一種[/yellow]")

    # ── 補充桶：我們自己掃到的（樣本小且偏，與上面不同池）──
    if d.local_history:
        console.print("\n[bold]我們自己掃到的[/bold]"
                      "[dim]（補充桶：只有本地掃描期間、碰巧掃到的，"
                      "與市場檔案分母不同，不可相加）[/dim]")
        for h in d.local_history:
            kind = h.get("sale_kind") or h.get("ledger") or ""
            date = str(h.get("sold_at") or h.get("last_seen") or "")[:10]
            price = h.get("price_native")
            price_s = (f"{h.get('currency', '')} {price:,.0f}"
                       if price is not None else "—")
            console.print(f"  [{h['tier']}] {date} {price_s}（{kind}）"
                          f"{str(h['title'])[:56]}")
    notable = [h for h in d.hits if h["tier"] in ("exact", "partial")]
    if notable:
        console.print("\n[bold]狙擊命中帳[/bold]")
        for h in notable:
            sent = "已推播" if h.get("sent_at") else "未推播"
            console.print(
                f"  [{h['tier']}] {str(h['last_seen'])[:16]} "
                f"{str(h['title'])[:60]}（{sent}）  {h['url']}"
            )
    console.print("\n[bold]等待建議[/bold]")
    for line in d.recommendation:
        console.print(f"  • {line}")


@snipe_app.command("add")
def snipe_add(
    name_ja: str = typer.Argument(..., help="日文卡名，如 魔法の筒"),
    grader: str = typer.Option(..., help="鑑定機構：ARS / PSA / BGS"),
    grade: str = typer.Option(..., help="目標分數，如 10 或 10+（10+ 與 10 比對同分）"),
    name_en: str = typer.Option("", help="英文卡名（主檔有就自動補）"),
    code: str = typer.Option("", help="卡號，如 P4-06"),
    census_url: str = typer.Option("", help="ARS census 頁 URL；ARS 不給會用卡名自動搜"),
    evidence: list[str] = typer.Option(
        [], help="過去出現的商品頁 URL（可重複）；登錄當下抓快照存證"),
    pin_seller: str = typer.Option("", help="順手釘選賣家（貼賣家頁 URL 或 site:id）"),
    no_mine: bool = typer.Option(False, "--no-mine", help="不挖市場成交檔案（離線登錄）"),
    note: str = typer.Option("", help="備註"),
):
    """登錄狙擊卡 → 挖市場成交檔案、抓 census、抓證據頁快照、輸出完整檔案。"""
    from .card_snipe import add_card_watch
    from .sources import build_sources
    from .sources.base import CachedFetcher

    cfg = load_config()
    store = Store(cfg.db_path)
    try:
        with CachedFetcher(cfg) as fetcher:
            result = add_card_watch(
                store, fetcher,
                grader=grader, grade_input=grade, name_ja=name_ja,
                name_en=name_en, code=code, census_url=census_url,
                evidence_urls=list(evidence), note=note,
                sources=None if no_mine else build_sources(cfg, fetcher),
            )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    for line in result.messages:
        console.print(line)
    if pin_seller:
        from .seller_watch import SOURCE_PINNED, add_watch

        key, store_slug = _resolve_seller_key(pin_seller, cfg)
        _cfg2, store2, params = _watch_ctx()
        reason = f"狙擊 #{result.watch_id} 的歷史賣家（snipe add --pin-seller）"
        if store_slug:
            reason += f"（從店鋪頁 {store_slug} 解析）"
        res = add_watch(store2, key, source=SOURCE_PINNED, reason=reason,
                        params=params)
        if res.ok:
            console.print(f"[green]📌 已釘選 {res.seller_key}[/green]：{res.reason}")
            _warn_if_unlistable(res.seller_key)
        else:
            console.print(f"[red]釘選失敗 {res.seller_key}[/red]：{res.reason}")
    _print_dossier(store, result.watch_id)


@snipe_app.command("list")
def snipe_list():
    """狙擊清單＋每張的命中統計。"""
    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_card_watch(active_only=True)
    if not rows:
        console.print("狙擊清單是空的。用 ygo-sniper snipe add 登錄第一張。")
        return
    t = Table(title="🎯 狙擊清單")
    for col in ("#", "目標", "卡號", "census", "成交檔案", "在架🎯", "在架👀", "登錄於"):
        t.add_column(col)
    for w in rows:
        hits = store.list_card_watch_hits(watch_id=int(w["id"]))
        n = {tier: sum(1 for h in hits if h["tier"] == tier)
             for tier in ("exact", "partial", "near")}
        sales = store.list_card_watch_sales(int(w["id"]), tiers=("exact",))
        sale_col = f"{len(sales)} 筆" if sales else "—"
        if sales:
            stamps = sorted(s["sold_at"][:10] for s in sales if s["sold_at"])
            if stamps:
                sale_col += f"（最近 {stamps[-1]}）"
        census = "—"
        raw = w.get("census_json") or ""
        if raw:
            try:
                at = json.loads(raw).get(str(w["grade_label"]))
                census = f"{at} 張" if at is not None else "—"
            except ValueError:
                pass
        t.add_row(str(w["id"]), f"{w['grader']}{w['grade_label']} {w['name_ja']}",
                  w["code_raw"] or "—", census, sale_col,
                  str(n["exact"]), str(n["partial"]),
                  str(w["added_at"] or "")[:10])
    console.print(t)


@snipe_app.command("report")
def snipe_report(
    watch_id: int = typer.Argument(..., help="狙擊編號（snipe list 的 #）"),
    refresh_census: bool = typer.Option(False, "--refresh-census",
                                        help="重抓 ARS 鑑定量"),
):
    """單張狙擊卡的完整檔案：census、實證、本地歷史、命中帳、等待建議。"""
    from .card_snipe import refresh_watch_census
    from .sources.base import CachedFetcher

    cfg = load_config()
    store = Store(cfg.db_path)
    w = store.get_card_watch(watch_id)
    if w is None:
        console.print(f"[red]沒有狙擊 #{watch_id}[/red]")
        raise typer.Exit(1)
    if refresh_census:
        with CachedFetcher(cfg) as fetcher:
            for line in refresh_watch_census(store, fetcher, w):
                console.print(line)
    _print_dossier(store, watch_id)


@snipe_app.command("mine")
def snipe_mine(
    watch_id: int = typer.Argument(
        0, help="狙擊編號；0 ＝ 全部（定期重挖，把要滾掉的檔案存下來）"),
    pages: int = typer.Option(0, help="每個關鍵字翻幾頁（0 ＝ 用預設 2）"),
):
    """去市場的成交檔案挖這張卡的過去。

    **市場的檔案才是資料庫，我們的表只是它的記憶體**——Yahoo 落札相場約保留
    150-180 天就滾掉，定期重挖才留得住。冪等：同一筆成交重挖只更新，不重複。
    """
    from .card_snipe import MINE_PAGES, WatchMatcher, mine_sold_archive
    from .sources import build_sources
    from .sources.base import CachedFetcher

    cfg = load_config()
    store = Store(cfg.db_path)
    rows = ([store.get_card_watch(watch_id)] if watch_id
            else store.list_card_watch(active_only=True))
    rows = [r for r in rows if r]
    if not rows:
        console.print(f"[red]沒有狙擊 #{watch_id}[/red]" if watch_id
                      else "狙擊清單是空的")
        raise typer.Exit(1)
    bad = 0
    with CachedFetcher(cfg) as fetcher:
        sources = build_sources(cfg, fetcher)
        for w in rows:
            m = WatchMatcher.from_row(w)
            res = mine_sold_archive(store, sources, m,
                                    pages=pages or MINE_PAGES)
            tone = "green" if res.ok else "yellow"
            console.print(f"[{tone}]#{w['id']} {w['grader']}{w['grade_label']} "
                          f"{w['name_ja']}[/{tone}]：{res.summary()}")
            if not res.ok:
                bad += 1
                for p in res.problems:
                    console.print(f"  [yellow]⚠️ {p}[/yellow]")
    if bad:
        # 挖不到就大聲——「0 筆」與「被擋」外顯一樣，這是本專案的頭號敵人
        console.print(f"\n[bold yellow]{bad} 張卡有管道沒挖成功；"
                      f"那幾條的「0 筆」不代表沒賣過[/bold yellow]")
        raise typer.Exit(1)


@snipe_app.command("remove")
def snipe_remove(watch_id: int = typer.Argument(..., help="狙擊編號")):
    """移出狙擊清單（軟刪除，命中帳與證據留著）。"""
    cfg = load_config()
    store = Store(cfg.db_path)
    if store.deactivate_card_watch(watch_id):
        console.print(f"[green]已移出狙擊 #{watch_id}[/green]")
    else:
        console.print(f"[yellow]#{watch_id} 本來就不在清單上[/yellow]")
```

注意：`cli.py:12` 已有 `import json`、`:18` 已有 `from rich.table import Table`（`snipe_list` 用得到），import 區不用改。

- [ ] **Step 3b: 讓 `daily` 每天重挖一次成交檔案（檔案會滾掉，不重挖就永遠失去）**

Yahoo 落札相場只保留約 150-180 天。**我們的表存在的唯一意義，就是在那些紀錄被平台刪掉之前把它們留下來**——所以重挖不是選配。用 `meta` 表節流成一天一次（同 `comps.claim_sold_run` 的既有做法），一張卡 2 個請求，成本可忽略。

在 `cli.py` 的 `_run_notifications` 定義之前加：

```python
#: 狙擊成交檔案的重挖節流（一天一次就夠——Yahoo 的檔案以天為單位變動）
_SNIPE_MINE_META_KEY = "snipe_last_mined_date"


def _mine_snipes_daily(pipe) -> None:
    """每天重挖一次市場成交檔案。**失敗只警告，絕不影響掃描與推播。**

    重挖的理由不是「怕漏新成交」（新成交在架時就會被規則 4 抓到），而是
    **平台的檔案會滾掉**：150-180 天前的成交會被 Yahoo 刪除，沒存下來就
    永遠沒有了。這張表是市場檔案的記憶體。
    """
    from datetime import UTC, datetime

    from .card_snipe import WatchMatcher, mine_sold_archive

    watches = pipe.store.list_card_watch(active_only=True)
    if not watches:
        return
    today = datetime.now(UTC).date().isoformat()
    if pipe.store.get_meta(_SNIPE_MINE_META_KEY) == today:
        return
    problems = 0
    for w in watches:
        try:
            res = mine_sold_archive(pipe.store, pipe.sources, WatchMatcher.from_row(w))
        except Exception as exc:  # noqa: BLE001 - 重挖失敗不能拖垮 daily
            print(f"[warn] 狙擊 #{w['id']} 成交檔案重挖失敗：{type(exc).__name__}: {exc}")
            problems += 1
            continue
        if res.new_sales:
            print(f"[snipe] #{w['id']} {w['name_ja']}：成交檔案新增 {res.new_sales} 筆")
        if not res.ok:
            problems += 1
            for p in res.problems:
                print(f"[warn] 狙擊 #{w['id']} {p}")
    # 只有全部成功才記帳：有管道被擋就讓下一輪再試（否則今天就再也不挖了）
    if not problems:
        pipe.store.set_meta(_SNIPE_MINE_META_KEY, today)
```

在 `daily`（`cli.py:44`）的 `_print_scan(result)` 之後、`if not no_notify:` 之前插入一行：

```python
        _mine_snipes_daily(pipe)
```

追加測試到 `tests/test_card_snipe.py` 的 `TestPipelineHook`：

```python
    def test_daily_mining_is_throttled_to_once_a_day(self, tmp_path, no_fx_network,
                                                     monkeypatch):
        from dataclasses import replace as dc_replace

        import ygo_sniper.card_snipe as snipe_mod
        import ygo_sniper.cli as cli_mod
        import ygo_sniper.config as config_mod
        from ygo_sniper.pipeline import Pipeline

        config_mod.load_config.cache_clear()
        base = config_mod.load_config()
        cfg = dc_replace(base, storage={**base.storage,
                                        "db_path": str(tmp_path / "p.db")})
        pipe = Pipeline(cfg)
        try:
            pipe.store.insert_card_watch(**WATCH_KW)
            calls = []
            monkeypatch.setattr(
                snipe_mod, "mine_sold_archive",
                lambda *a, **kw: (calls.append(1), snipe_mod.MineResult(ok=True))[1],
            )
            cli_mod._mine_snipes_daily(pipe)
            cli_mod._mine_snipes_daily(pipe)
            assert len(calls) == 1            # 第二次被節流擋下
        finally:
            pipe.close()
            config_mod.load_config.cache_clear()

    def test_daily_mining_does_not_mark_done_when_a_channel_failed(
        self, tmp_path, no_fx_network, monkeypatch
    ):
        """被擋就不記帳——記了的話今天就再也不挖，而檔案正在滾掉。"""
        from dataclasses import replace as dc_replace

        import ygo_sniper.card_snipe as snipe_mod
        import ygo_sniper.cli as cli_mod
        import ygo_sniper.config as config_mod
        from ygo_sniper.pipeline import Pipeline

        config_mod.load_config.cache_clear()
        base = config_mod.load_config()
        cfg = dc_replace(base, storage={**base.storage,
                                        "db_path": str(tmp_path / "p.db")})
        pipe = Pipeline(cfg)
        try:
            pipe.store.insert_card_watch(**WATCH_KW)
            monkeypatch.setattr(
                snipe_mod, "mine_sold_archive",
                lambda *a, **kw: snipe_mod.MineResult(ok=False, problems=["BLOCKED"]),
            )
            cli_mod._mine_snipes_daily(pipe)
            assert pipe.store.get_meta(cli_mod._SNIPE_MINE_META_KEY) is None
        finally:
            pipe.close()
            config_mod.load_config.cache_clear()
```

- [ ] **Step 4: 跑測試看它綠**

```bash
.venv/bin/pytest tests/test_card_snipe.py -x
```

預期：`59 passed`（53 ＋ daily 重挖節流 2 ＋ CLI 4）。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/cli.py tests/test_card_snipe.py
git commit -m "feat(snipe): CLI 群組 snipe add/list/report/remove（add 可順手釘選歷史賣家）"
```

---

### Task 9: dashboard——🎯 狙擊 tab

**Files:**
- Modify: `web/app.py`（pin route（:1066-1115）之後加四個 route；`/api/snipe/{id}` 是數字參數不與其他 route 衝突）
- Modify: `web/static/index.html`（tab :391 後、view 容器 :592 `</main>` 前、`setView` :2351、JS 函式加在 `loadSellers` 附近）
- Test: `tests/test_card_snipe_web.py`

- [ ] **Step 1: 寫失敗測試 `tests/test_card_snipe_web.py`**

```python
"""狙擊 tab 的 API。client fixture 照抄 test_seller_watch.py:1407 的模式。"""
from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    import ygo_sniper.config as config_mod

    db = tmp_path / "web.db"
    config_mod.load_config.cache_clear()
    real_load = config_mod.load_config

    def _tmp_config(*a, **kw):
        c = real_load(*a, **kw)
        return replace(c, storage={**c.storage, "db_path": str(db)})

    monkeypatch.setattr(config_mod, "load_config", _tmp_config)
    monkeypatch.syspath_prepend(str(ROOT))
    for mod in ("web.app", "web"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("web.app")
    try:
        assert app_mod.store.db_path == db, (
            "web.app 的 store 沒有指到 tmp——測試絕不能碰正式庫 data/sniper.db"
        )
        from fastapi.testclient import TestClient

        yield TestClient(app_mod.app), app_mod
    finally:
        # ⚠️ 這裡**不要**呼叫 config_mod.load_config.cache_clear()：此刻它還是被
        # monkeypatch 換上去的純函式（monkeypatch 是上游 fixture，teardown 在後），
        # 沒有 cache_clear → AttributeError。既有的 test_seller_watch.py 原版就沒有這行。
        for mod in ("web.app", "web"):
            sys.modules.pop(mod, None)


def test_snipe_list_empty(client):
    c, _app = client
    r = c.get("/api/snipe")
    assert r.status_code == 200 and r.json() == {"watches": []}


def test_snipe_add_detail_remove_roundtrip(client):
    c, app_mod = client
    # mine=False ＋ PSA ＋ 無 evidence → 完全不打網路
    r = c.post("/api/snipe", json={
        "name_ja": "魔法の筒", "grader": "PSA", "grade": "10", "code": "P4-06",
        "mine": False,
    })
    assert r.status_code == 200, r.text
    wid = r.json()["watch_id"]
    assert any("已登錄狙擊" in m for m in r.json()["messages"])

    r = c.get("/api/snipe")
    ws = r.json()["watches"]
    assert len(ws) == 1 and ws[0]["code_norm"] == "P4-6"
    assert ws[0]["hit_counts"] == {"exact": 0, "partial": 0, "near": 0}

    r = c.get(f"/api/snipe/{wid}")
    body = r.json()
    assert body["watch"]["id"] == wid
    assert isinstance(body["recommendation"], list) and body["recommendation"]
    # 三個桶必須各自獨立回傳（出處不同的數字不可合併）
    assert body["sales"] == [] and body["local_history"] == []
    assert "evidence" in body and "hits" in body

    r = c.post(f"/api/snipe/{wid}/remove")
    assert r.status_code == 200
    assert c.get("/api/snipe").json()["watches"] == []


def test_snipe_add_rejects_bad_input_with_400(client):
    c, _app = client
    r = c.post("/api/snipe", json={"name_ja": "x", "grader": "CGC", "grade": "10"})
    assert r.status_code == 400
    assert "不認得的鑑定機構" in r.text


def test_snipe_tab_is_wired_in_the_spa(client):
    """SPA 是單檔——tab 按鈕、view 容器、loadSnipe 三件都要在。"""
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-view="snipe"' in html
    assert 'id="snipe-view"' in html
    assert "function loadSnipe" in html
    assert "function snipeMine" in html
    assert "市場成交檔案" in html          # 主要資料桶要畫在使用者臉上
    assert "snipe-view" in html.split("function setView")[1].split("}")[0] or \
           'getElementById("snipe-view")' in html
```

- [ ] **Step 2: 跑測試看它紅**

```bash
.venv/bin/pytest tests/test_card_snipe_web.py -x
```

預期：`404`／assert 失敗。

- [ ] **Step 3: `web/app.py` 加四個 route（放在 `pin_seller` 函式結束之後）**

```python
class SnipeAddRequest(BaseModel):
    name_ja: str
    grader: str
    grade: str
    name_en: str = ""
    code: str = ""
    census_url: str = ""
    evidence_urls: list[str] = []
    note: str = ""
    #: 預設登錄當下就挖市場成交檔案（那是檔案的主要內容）。測試傳 False 免網路。
    mine: bool = True


@app.get("/api/snipe")
def snipe_list_api():
    """狙擊清單＋命中統計（輕量；完整檔案在 /api/snipe/{id}）。"""
    out = []
    for w in store.list_card_watch(active_only=True):
        hits = store.list_card_watch_hits(watch_id=int(w["id"]))
        counts = {t: sum(1 for h in hits if h["tier"] == t)
                  for t in ("exact", "partial", "near")}
        out.append({**w, "hit_counts": counts,
                    "recent_hits": [h for h in hits if h["tier"] != "near"][:10]})
    return {"watches": out}


@app.get("/api/snipe/{watch_id}")
def snipe_detail(watch_id: int):
    """完整檔案：census＋實證＋本地歷史（現場重比對）＋命中帳＋等待建議。"""
    from ygo_sniper.card_snipe import build_dossier

    w = store.get_card_watch(watch_id)
    if w is None:
        raise HTTPException(404, f"沒有狙擊 #{watch_id}")
    d = build_dossier(store, w)
    return {
        "watch": d.watch, "census": d.census, "census_total": d.census_total,
        # 三個桶分開回，前端也分開畫——出處不同的數字不可相加
        "sales": d.sales, "local_history": d.local_history,
        "evidence": d.evidence, "hits": d.hits,
        "recommendation": d.recommendation,
    }


@app.post("/api/snipe/{watch_id}/mine")
def snipe_mine_api(watch_id: int):
    """重挖市場成交檔案（會連網，數秒）。挖不到要把問題原文回給前端顯示。"""
    from ygo_sniper.card_snipe import WatchMatcher, mine_sold_archive
    from ygo_sniper.sources import build_sources
    from ygo_sniper.sources.base import CachedFetcher

    w = store.get_card_watch(watch_id)
    if w is None:
        raise HTTPException(404, f"沒有狙擊 #{watch_id}")
    with CachedFetcher(cfg) as fetcher:
        res = mine_sold_archive(store, build_sources(cfg, fetcher),
                                WatchMatcher.from_row(w))
    return {"ok": res.ok, "summary": res.summary(), "new_sales": res.new_sales,
            "total_sales": res.total_sales, "problems": res.problems}


@app.post("/api/snipe")
def snipe_add_api(body: SnipeAddRequest):
    """與 CLI 的 snipe add 同一支政策（card_snipe.add_card_watch）——判準只有一份。
    會連網抓 census 與證據頁（幾秒），與釘選解析店鋪頁同一種等待。"""
    from ygo_sniper.card_snipe import add_card_watch
    from ygo_sniper.sources import build_sources
    from ygo_sniper.sources.base import CachedFetcher

    try:
        with CachedFetcher(cfg) as fetcher:
            res = add_card_watch(
                store, fetcher,
                grader=body.grader, grade_input=body.grade, name_ja=body.name_ja,
                name_en=body.name_en, code=body.code, census_url=body.census_url,
                evidence_urls=body.evidence_urls, note=body.note,
                sources=build_sources(cfg, fetcher) if body.mine else None,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "watch_id": res.watch_id, "messages": res.messages}


@app.post("/api/snipe/{watch_id}/remove")
def snipe_remove_api(watch_id: int):
    if not store.deactivate_card_watch(watch_id):
        raise HTTPException(404, f"#{watch_id} 不在清單上")
    return {"ok": True}
```

（`BaseModel`、`HTTPException` 在 `web/app.py` 頂部已 import——`PinRequest` 就在用。）

- [ ] **Step 4: `web/static/index.html` 三處修改**

(a) tab 按鈕：在 :391 `👤 賣家` 那顆 button 之後、`</div>` 之前加：

```html
    <button class="tab" data-view="snipe" title="指定卡狙擊：登錄特定卡（鑑定機構＋分數＋卡名＋卡號），出現就推播；比對走在所有過濾規則之前">🎯 狙擊</button>
```

（tab click handler **不用改**——`data-view` 分支已是泛用的 `setView(btn.dataset.view)`。`.tab` 沒有專屬 CSS，樣式來自泛用 button 規則，也不用加。）

(b) `setView()`（:2351）整支改成三個 view 的切換：

```javascript
function setView(view){
  const v = view === "sellers" ? "sellers" : (view === "snipe" ? "snipe" : "signals");
  document.getElementById("signals-view").style.display = v === "signals" ? "" : "none";
  document.getElementById("seller-view").style.display  = v === "sellers" ? "" : "none";
  document.getElementById("snipe-view").style.display   = v === "snipe"   ? "" : "none";
  document.getElementById("filterbar").style.display    = v === "signals" ? "" : "none";
  if(v === "sellers" && !svData) loadSellers();
  if(v === "snipe" && !snipeData) loadSnipe();
}
```

(c) view 容器：在 `</main>`（:593，`seller-view` 的 `</div>` 之後）之前插入：

```html
<div id="snipe-view" style="display:none">
  <div class="panel">
    <h2>🎯 指定卡狙擊 — 等一根特定的針</h2>
    <div class="hint" style="margin-bottom:8px">
      登錄「鑑定機構＋分數＋卡名＋卡號」，比對走在所有過濾規則<b>之前</b>：
      🎯 精確命中與 👀 同機構疑似會推 Telegram（每筆終身一次）；
      near（未鑑定／別家機構／現代重印）只記帳在這裡，不推播——不推播不是丟棄，這頁看得到全部。
    </div>
    <form class="apr-form" onsubmit="snipeAdd(event)">
      <input id="sn-name-ja" type="text" autocomplete="off" spellcheck="false"
             placeholder="日文卡名（必填），如 魔法の筒">
      <button type="submit" id="sn-add-btn">➕ 登錄狙擊</button>
    </form>
    <div class="row" style="gap:6px;margin-top:6px">
      <input id="sn-grader" type="text" style="width:70px" placeholder="ARS">
      <input id="sn-grade" type="text" style="width:60px" placeholder="10">
      <input id="sn-code" type="text" style="width:100px" placeholder="P4-06">
      <input id="sn-name-en" type="text" style="width:180px" placeholder="英文卡名（主檔有會自動補）">
    </div>
    <textarea id="sn-evidence" rows="2" style="width:100%;margin-top:6px;box-sizing:border-box"
              placeholder="過去出現的商品頁 URL（一行一個）。登錄當下就抓快照存證——Yahoo 結標頁約 120 天會刪"></textarea>
    <div class="hint">
      ARS 的鑑定量（census）會用卡名自動搜；唯一定位不了會列候選讓你確認。
      登錄會連網抓 census 與證據頁，<b>約需數秒</b>。
    </div>
  </div>
  <div class="panel">
    <h2>狙擊清單</h2>
    <div class="scroll-x"><table id="sn-list"><tbody><tr><td>載入中…</td></tr></tbody></table></div>
  </div>
  <div class="panel" id="sn-detail-panel" style="display:none">
    <h2 id="sn-detail-title">狙擊檔案</h2>
    <div id="sn-detail">載入中…</div>
  </div>
</div>
```

(d) JS：在 `loadSellers()` 函式定義之前（:2387 附近）插入：

```javascript
let snipeData = null;

async function loadSnipe(){
  let d;
  try{ d = await api("/api/snipe"); }
  catch(e){
    document.getElementById("sn-list").innerHTML =
      `<tr><td style="color:var(--danger)">載入失敗：${escapeHtml(e.message)}</td></tr>`;
    return;
  }
  snipeData = d;
  const rows = (d.watches || []).map(w => {
    const c = w.hit_counts || {};
    let census = "—";
    try{
      const j = JSON.parse(w.census_json || "");
      const at = j[w.grade_label];
      if(at != null) census = `全球 ${at} 張`;
    }catch(e){}
    return `<tr>
      <td><a href="#" onclick="openSnipe(${w.id});return false">#${w.id} ${escapeHtml(w.grader)}${escapeHtml(w.grade_label)} ${escapeHtml(w.name_ja)}</a></td>
      <td>${escapeHtml(w.code_raw || "—")}</td>
      <td>${census}</td>
      <td>🎯 ${c.exact||0}｜👀 ${c.partial||0}｜near ${c.near||0}</td>
      <td>${escapeHtml(String(w.added_at||"").slice(0,10))}</td>
      <td><button onclick="snipeRemove(${w.id})">移出</button></td>
    </tr>`;
  }).join("");
  document.getElementById("sn-list").innerHTML =
    rows || `<tr><td class="empty">清單是空的——用上面的表單登錄第一張。</td></tr>`;
}

async function snipeAdd(ev){
  ev.preventDefault();
  const btn = document.getElementById("sn-add-btn");
  const body = {
    name_ja: document.getElementById("sn-name-ja").value.trim(),
    grader: (document.getElementById("sn-grader").value.trim() || "ARS").toUpperCase(),
    grade: document.getElementById("sn-grade").value.trim() || "10",
    code: document.getElementById("sn-code").value.trim(),
    name_en: document.getElementById("sn-name-en").value.trim(),
    evidence_urls: document.getElementById("sn-evidence").value
      .split("\n").map(s => s.trim()).filter(Boolean),
  };
  if(!body.name_ja){ toast("日文卡名必填"); return; }
  btn.disabled = true; btn.textContent = "登錄中…（抓 census 與證據頁）";
  try{
    const r = await api("/api/snipe", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    toast(`已登錄狙擊 #${r.watch_id}`, 3000);
    snipeData = null; loadSnipe(); openSnipe(r.watch_id);
  }catch(e){ toast(`登錄失敗：${e.message}`, 4200); }
  finally{ btn.disabled = false; btn.textContent = "➕ 登錄狙擊"; }
}

async function snipeRemove(id){
  try{ await api(`/api/snipe/${id}/remove`, {method: "POST"}); }
  catch(e){ toast(`移出失敗：${e.message}`); return; }
  toast(`已移出狙擊 #${id}（命中帳與證據留著）`);
  snipeData = null; loadSnipe();
  document.getElementById("sn-detail-panel").style.display = "none";
}

async function snipeMine(id){
  const btn = document.getElementById("sn-mine-btn");
  if(btn){ btn.disabled = true; btn.textContent = "挖掘中…"; }
  try{
    const r = await api(`/api/snipe/${id}/mine`, {method: "POST"});
    // 挖不到要說出來——「0 筆」與「被擋」不能長一樣
    toast(r.ok ? r.summary : `⚠️ ${r.summary}｜${(r.problems || []).join("；")}`,
          r.ok ? 3200 : 6000);
    openSnipe(id);
    snipeData = null; loadSnipe();
  }catch(e){ toast(`挖掘失敗：${e.message}`, 4200); }
  finally{ if(btn){ btn.disabled = false; btn.textContent = "重新挖掘"; } }
}

async function openSnipe(id){
  const panel = document.getElementById("sn-detail-panel");
  panel.style.display = "";
  document.getElementById("sn-detail").textContent = "載入中…";
  let d;
  try{ d = await api(`/api/snipe/${id}`); }
  catch(e){
    document.getElementById("sn-detail").textContent = `載入失敗：${e.message}`;
    return;
  }
  const w = d.watch;
  document.getElementById("sn-detail-title").textContent =
    `狙擊檔案 #${w.id}：${w.grader}${w.grade_label} ${w.name_ja} ${w.code_raw || ""}`;
  const census = d.census
    ? Object.entries(d.census).filter(([k, v]) => v > 0)
        .map(([k, v]) => `<span class="pill">${escapeHtml(k)}: <b>${v}</b> 張</span>`)
        .join(" ") + `　（鑑定總數 ${d.census_total ?? "?"}）`
    : `<span class="caveat">未抓到 census（CLI：ygo-sniper snipe report ${w.id} --refresh-census）</span>`;
  const ev = (d.evidence || []).map(e => e.status === "ok"
    ? `<tr><td>${escapeHtml(String(e.sold_at || "").slice(0, 10))}</td>
       <td>¥${(e.price_native || 0).toLocaleString()}</td>
       <td>${e.bids ?? "—"} 出價</td>
       <td>${escapeHtml(e.seller_name || e.seller_id)}</td>
       <td><a href="${escapeHtml(e.url)}" target="_blank">頁面</a></td></tr>`
    : `<tr><td colspan="5">⚠️ [${escapeHtml(e.status)}] ${escapeHtml(e.url)}（${escapeHtml(e.note || "")}）</td></tr>`
  ).join("");
  const sales = (d.sales || []).map(s => {
    const mark = s.tier === "exact" ? "🎯" : (s.tier === "partial" ? "👀" : "·");
    const kind = {auction: "競標", fixed: "定價"}[s.sale_kind] || "型態不明";
    return `<tr><td>${mark}</td>
     <td>${escapeHtml(String(s.sold_at || "").slice(0, 10))}</td>
     <td>${s.price_native != null ? escapeHtml(s.currency || "") + " " + s.price_native.toLocaleString() : "—"}</td>
     <td>${kind}${s.sale_kind === "auction" && s.bid_count ? "／" + s.bid_count + " 出價" : ""}</td>
     <td>${escapeHtml(String(s.seller_id || "—").slice(0, 16))}</td>
     <td style="white-space:normal"><a href="${escapeHtml(s.url)}" target="_blank">${escapeHtml(s.title)}</a></td></tr>`;
  }).join("");
  const hist = (d.local_history || []).map(h =>
    `<tr><td>[${escapeHtml(h.tier)}]</td>
     <td>${escapeHtml(String(h.sold_at || h.last_seen || "").slice(0, 10))}</td>
     <td>${h.price_native != null ? escapeHtml(h.currency || "") + " " + h.price_native.toLocaleString() : "—"}</td>
     <td>${escapeHtml(h.sale_kind || h.ledger || "")}</td>
     <td style="white-space:normal">${escapeHtml(h.title)}</td></tr>`).join("");
  const hits = (d.hits || []).map(h =>
    `<tr><td>${h.tier === "exact" ? "🎯" : h.tier === "partial" ? "👀" : "·"}</td>
     <td>${escapeHtml(String(h.last_seen || "").slice(0, 16).replace("T", " "))}</td>
     <td>${h.price_native != null ? escapeHtml(h.currency || "") + " " + h.price_native.toLocaleString() : "—"}</td>
     <td>${h.sent_at ? "已推播" : "未推播"}</td>
     <td style="white-space:normal"><a href="${escapeHtml(h.url)}" target="_blank">${escapeHtml(h.title)}</a></td></tr>`).join("");
  const rec = (d.recommendation || []).map(l => `<li>${escapeHtml(l)}</li>`).join("");
  document.getElementById("sn-detail").innerHTML = `
    <div class="cmp">${census}</div>
    <h2>💰 市場成交檔案 <button onclick="snipeMine(${w.id})" id="sn-mine-btn">重新挖掘</button></h2>
    <div class="hint" style="margin-bottom:6px">
      這是<b>平台自己的落札紀錄</b>，不是我們掃到的——一個請求就涵蓋約 150 天。
      Yahoo 落札相場只保留 150-180 天，更早的平台已刪除，所以這裡看到的
      <b>不是這張卡的全部歷史</b>。競標結標價與定價成交是兩種價格形成機制，欄位有標，不要平均。
    </div>
    <div class="scroll-x"><table><tbody>${sales || '<tr><td class="empty">還沒挖過，或檔案期間內沒有成交——按「重新挖掘」確認是哪一種</td></tr>'}</tbody></table></div>
    <h2>實證紀錄（你指定的商品頁快照）</h2>
    <div class="scroll-x"><table><tbody>${ev || '<tr><td class="empty">沒有</td></tr>'}</tbody></table></div>
    <h2>我們自己掃到的</h2>
    <div class="hint" style="margin-bottom:6px">補充桶：只有本地掃描期間、碰巧掃到的，與上面市場檔案<b>分母不同，不可相加</b>。</div>
    <div class="scroll-x"><table><tbody>${hist || '<tr><td class="empty">comps 與 listing_obs 都沒有紀錄</td></tr>'}</tbody></table></div>
    <h2>命中帳（near 只記帳不推播）</h2>
    <div class="scroll-x"><table><tbody>${hits || '<tr><td class="empty">還沒有命中</td></tr>'}</tbody></table></div>
    <h2>等待建議</h2><ul class="hint">${rec}</ul>`;
}
```

- [ ] **Step 5: 跑測試看它綠＋web 回歸**

```bash
.venv/bin/pytest tests/test_card_snipe_web.py -x
.venv/bin/pytest tests/ -k "seller_watch or card_bucket or expiry"
```

預期：新測試 `4 passed`；既有 web 測試全綠。

- [ ] **Step 6: 端到端煙測（使用者實際會打的指令）**

```bash
cd /Users/jim/projects/ygo-sniper && .venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from fastapi.testclient import TestClient
import web.app as m
c = TestClient(m.app)
assert c.get('/').status_code == 200
assert c.get('/api/snipe').status_code == 200
print('serve smoke OK')
"
```

預期：`serve smoke OK`（⚠️ 這個煙測會用**正式庫**做唯讀 GET，不寫入——只確認 route 掛上了）。

- [ ] **Step 7: Commit**

```bash
git add web/app.py web/static/index.html tests/test_card_snipe_web.py
git commit -m "feat(snipe): dashboard 🎯 狙擊 tab——清單／登錄表單／完整檔案（與 CLI 共用同一支政策）"
```

---

### Task 10: 文件＋全量回歸

**Files:**
- Modify: `CLAUDE.md`（「九、常用指令」code block，:258-264 賣家指令之後）
- Modify: `docs/dashboard.md`（:15 tab 示意行；「二、一個晚上的動線」新增小節）

- [ ] **Step 1: `CLAUDE.md` 指令區加**

```bash
ygo-sniper snipe add <日文卡名> --grader ARS --grade 10 --code P4-06 \
    --evidence <結標頁URL> --pin-seller <賣家頁URL>
                               # 登錄狙擊卡：當下就去挖**市場自己的成交檔案**
                               #   （Yahoo 落札相場，1 請求≈150 天）＋ARS census
                               #   ＋證據頁快照＋等待建議。--no-mine 可離線登錄
                               #   比對走在所有過濾規則之前；🎯/👀 推 Telegram（終身一次）
ygo-sniper snipe list          # 狙擊清單＋成交檔案筆數＋在架命中統計
ygo-sniper snipe report <id>   # 單卡完整檔案（--refresh-census 重抓鑑定量）
ygo-sniper snipe mine [<id>]   # 重挖市場成交檔案（省略 id ＝ 全部）。冪等
                               #   **daily 已自動每天挖一次**——平台檔案 150-180 天
                               #   會滾掉，我們的表是它的記憶體，不重挖就永遠失去
ygo-sniper snipe remove <id>   # 移出（軟刪除，成交檔案與命中帳留著）
```

- [ ] **Step 2: `docs/dashboard.md` 兩處**

:15 的 tab 示意行末尾把 `👤賣家` 改成 `👤賣家 🎯狙擊`。

「二、一個晚上的動線」的 `### 4. …` 小節之後加：

```markdown
### 5. 在等一張特定的卡，到「🎯 狙擊」登錄它

貼上鑑定機構＋分數＋卡名＋卡號。**登錄當下就會去挖市場自己的成交檔案**
（Yahoo 落札相場，一個請求約涵蓋 150 天），所以你不必等我們累積資料——
實測目標卡在我們自己的庫裡 0 筆，在市場檔案裡一挖就有。

檔案頁分三個桶，**出處不同所以永遠分開畫、不可相加**：
「💰 市場成交檔案」（平台自己的落札紀錄，主要）、「實證紀錄」（你指定的
商品頁快照）、「我們自己掃到的」（補充，樣本小且偏）。成交型態（競標／
定價）逐筆標明——競標結標價是買家喊上去的，定價是賣家開的，不是同一把尺。

⚠️ 市場檔案只涵蓋約 150-180 天，更早的平台已刪除，所以那**不是全部歷史**。
`daily` 每天會自動重挖一次，把即將滾掉的紀錄留在我們庫裡。

之後每輪掃描都比對在**所有過濾規則之前**：🎯 精確命中與 👀 疑似（分數不明／
現代版仿冒）推 Telegram（每筆終身一次、🎯 不受每輪則數上限）；near（別家機構
／未鑑定）只記帳在狙擊分頁，不吵你。「去哪等」建議會指名賣家並給一鍵釘選指令。
```

- [ ] **Step 3: 全量回歸**

```bash
make test
```

預期：全綠。**把輸出末尾的 `N passed` 抄下來**（改動前基線是 1561 passed）。

- [ ] **Step 4: 更新 `CLAUDE.md` 的測試數**

`CLAUDE.md` :252 的 `make test` 註解改成實跑出來的數字與今天日期（**用 Step 3 的真實輸出，不准編**）：

```bash
make test                      # pytest（目前 <N> passed，2026-08-09 實測）
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/dashboard.md
git commit -m "docs(snipe): 狙擊指令與 dashboard 動線；測試數更新"
```

---

### Task 11: 實際登錄使用者的目標卡（⚠️ 由主對話執行，不派 subagent——會打真實網路）

這是使用者這次要求的實際交付：ARS10 魔法の筒 P4-06。

- [ ] **Step 1: 登錄（真實抓取 census 與兩筆結標證據，並釘選歷史賣家）**

```bash
cd /Users/jim/projects/ygo-sniper
.venv/bin/ygo-sniper snipe add "魔法の筒" --grader ARS --grade 10 --code P4-06 \
  --census-url "https://ars-grading.com/grading/searchNameDetail?id=001202208090020007" \
  --evidence "https://auctions.yahoo.co.jp/jp/auction/n1235105710" \
  --evidence "https://auctions.yahoo.co.jp/jp/auction/l1230920412" \
  --pin-seller "https://auctions.yahoo.co.jp/seller/AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"
```

預期輸出包含（每一項都是 2026-08-09 實測過的值，對不上就停下來回報）：

1. 主檔命中，別名 `マジック・シリンダー` 一併比對
2. census `9: 5 張、10: 5 張、10+: 1 張（鑑定總數 11）`
3. 兩筆證據快照：`2026-07-01 ¥6,350（15 出價）`、`2026-05-27 ¥7,750（10 出價）`，賣家 `Natural Cards`
4. **市場成交檔案：`tier_counts` 含 `exact 2`**，兩筆 exact 就是上面那兩筆（同價同日、`sale_kind=auction`、賣家 `AiUkMq1pEUfNxvPeCv5PnfGpsFLrx`）；另有 `partial 2`（`2026-07-08 ¥4,600` プリズマティック 與 `2026-06-03 ¥168,150` 25th/WCS，兩筆都是現代版仿冒，理由欄要寫得出來）
5. 📌 釘選成功
6. 等待建議含 `watch-seller pin buyee_yahoo:AiUkMq1pEUfNxvPeCv5PnfGpsFLrx`、成交價 `6,350` 與 `7,750`、以及「不是全部歷史」的檔案期間標註

**驗收重點**：第 4 項的兩筆 exact **必須與第 3 項的兩筆證據是同兩筆成交**。這證明「挖市場檔案」這條路真的能獨立找回使用者手動找到的東西——如果只挖到 1 筆或 0 筆，代表 closedsearch 的涵蓋期間已經滾過那個日期，要立刻回報（那會改變功能的價值判斷）。

- [ ] **Step 2: 驗證通知判定路徑（只算不送）**

```bash
.venv/bin/ygo-sniper notify-preview 2>&1 | grep -A2 "指定卡狙擊"
.venv/bin/ygo-sniper snipe list
.venv/bin/ygo-sniper snipe report 1
.venv/bin/ygo-sniper watch-seller list
```

預期：notify-preview 的規則表**印得出「規則 4 指定卡狙擊」那一列**（這是 Step 4b 的重點：0 與「沒在跑」必須分得出來；目前市場上沒有 ARS10 P4-06 在架，命中 0 筆是正確的）；snipe list 顯示 #1 且「成交檔案」欄是 `2 筆（最近 2026-07-01）`；snipe report 印出三個資料桶；watch-seller list 的 📌 區塊有那個賣家。

- [ ] **Step 3: 跑一輪真實掃描確認狙擊查詢有注入**

```bash
.venv/bin/ygo-sniper scan --dry-run 2>&1 | head -40
```

預期：掃描輸出裡看得到 `snipe:1` 的查詢（keyword 魔法の筒 與 Magic Cylinder）。dry-run 不寫庫。

---

## Self-review 核對表（計劃作者已做；executor 不需重做）

- 規格覆蓋：1. 鑑定量（Task 3/5，ARS census 自動抓）✓；2. 過去出現時間（**Task 4b 挖市場成交檔案為主**＋Task 4 證據快照＋本地補充桶）✓；3. 成交價（同上，實測挖回 ¥6,350／¥7,750 兩筆 exact）✓；4. 等待建議（Task 5 build_recommendation，賣家歸因以成交檔案為主＋釘選整合）✓；5. 出現即 Telegram（Task 2/6/7，過濾前比對＋規則 4）✓；新 tab（Task 9）✓。
- 使用者的核心修正（市場檔案才是資料庫）：主要來源 = `card_watch_sale`（Task 4b 挖掘、Task 8 Step 3b 每日重挖）；本地 comps/listing_obs 降為 `local_history` 補充桶，CLI 與 dashboard 都分開畫並標明分母不同 ✓；「多快抓到」= 一個請求 0.98 秒／150 天，登錄當下就有 ✓；「多準確使用」= 100 筆實測 tier 分類 🎯2／👀2／near96、誤報 🎯 0 筆，且每個降級都有可讀的理由字串 ✓。
- 工程原則：同源同基準（三個桶分開呈現、價格逐筆列、競標定價依 `is_fixed_price` 旗標分開而不看 bidCount、不做任何跨筆聚合）✓；transient/semantic（FetchError 分類、ValueError 不入庫、抓不到標 unverifiable）✓；大聲失敗（CensusParseError／AuctionPageError／⚠️ 訊息）✓；測試不碰真實世界（fixture＋FakeFetcher；conftest 已靜音 Telegram）✓；resilience boundary（全部走 CachedFetcher）✓。
- 型別一致：`match_tier` 回傳 str|None；`Match(key=…, rule=…, row=…)` **不傳 title**（`Match.title` 是唯讀 property，`notify_rules.py:441`，傳了會 TypeError）；notify_log 鍵 `"{watch_id}:{listing_key}"` 在 store join、context、evaluate 三處一致 ✓。
