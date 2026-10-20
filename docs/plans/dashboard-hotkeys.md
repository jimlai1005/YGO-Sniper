# Dashboard 卡片快捷鍵 Implementation Plan

> **狀態：已完成並上線（2026-10-19 收工）。** Task 1-6 全部落地，三輪 reviewer 審查的 finding 全數處理
> （裁決見各 Task 節）；使用者實際使用後回饋「很順手」。使用說明在 `docs/dashboard.md` §二-1。

> **For agentic workers:** 逐 task 執行，TDD（先寫紅燈測試再實作）。每個 task 完成後主線程親跑驗收指令。
> Steps use checkbox (`- [ ]`) syntax for tracking。所有 task 標 `@inline`（builder／sonnet）：
> 要改的是一支 3,200 行的單檔前端（`web/static/index.html`），必須讀懂既有的樂觀更新
> （`setState`／`setBucket`／`renderSignals`）才能接得對。遇到本檔沒寫的判斷題→停下回報。

**Goal:** 在 dashboard 的標的清單上，按鍵盤一個鍵就對「焦點卡片」做狀態／分類動作，
不必用滑鼠瞄按鈕；焦點由滑鼠懸停或 j/k 決定、有外框；`u` 復原上一個狀態動作。

**Architecture:** 全部在 `web/static/index.html` 內完成，不動後端（API 不變、狀態語意不變）。
按鍵→動作與焦點遷移寫成**純函式**，放在 `/* ==== HOTKEY-LOGIC:BEGIN/END ==== */` 標記區塊，
`tests/test_hotkeys.py` 沿用 `tests/test_optimistic_state.py` 的做法抽出來丟 node 跑。
DOM 這一層只做三件事：keydown 守門、mouseover 設焦點、render 後補焦點外框。
動作本身**一律走既有的 `setState`／`setBucket`**（樂觀更新、作廢載入、失敗對帳那套不重寫）。

**Tech Stack:** 原生 JS（無框架）、pytest + node 子行程（既有 harness）。

**背景（為什麼做，遇到歧義往這裡靠）：** 使用者在「待處理」逐張處理卡片，略過鍵在每張卡的
位置不固定（按鈕列會換行、競標卡少兩顆），滑鼠每次都要重新瞄。目標是**手不離鍵盤、眼不離卡片**。
使用者裁決（2026-10-15）：按鍵表 zxcvbnm,、焦點＝懸停為主＋j/k 為輔＋外框、`u` 復原都做。

## 按鍵表（使用者定案，不要改）

| 鍵 | 動作 | 走哪支 | 競標卡（`auctionCard`） |
|---|---|---|---|
| `z` | 觀察中 `watching` | `setState` | 有 |
| `x` | 加入湊單 `in_bundle` | `setState` | **無**→toast「這張卡沒有「加入湊單」」 |
| `c` | 已詢問 `asked_seller` | `setState` | **無**→toast「這張卡沒有「已詢問」」 |
| `v` | 已出價 `offer_sent` | `setState` | 有 |
| `b` | 已購買／已標到 `bought` | `setState` | 有（按鈕叫「已標到」，動作相同） |
| `n` | 略過 `skipped` | `setState` | 有 |
| `m` | 💎 高價卡 `high_value`（開關） | `setBucket` | 有 |
| `,` | 🌟 稀有卡 `rare`（開關） | `setBucket` | 有 |
| `j` / `↓` | 焦點往下一張 | 本地 | — |
| `k` / `↑` | 焦點往上一張 | 本地 | — |
| `u` | 復原上一個**狀態**動作（分類開關不在復原範圍，再按一次同鍵就是復原） | `setState` | — |

鍵綁的是**動作**，不是按鈕位置：競標卡少了 x、c，`n` 仍是略過。

## 守門（任一成立就不攔截，讓瀏覽器正常處理）

1. `ev.target` 是 `input`／`textarea`／`select`，或 `isContentEditable`。
2. `ev.metaKey || ev.ctrlKey || ev.altKey`（Shift 不算：鍵值先 `toLowerCase()`）。
3. `ev.isComposing`（中文輸入法組字中）。
4. 燈箱 `#lightbox` 帶 `show` class（既有 Esc 處理照舊）。
5. `#signals-view` 不在畫面上（`display:none`，使用者在別的分頁）。
6. 沒有焦點卡片時：`j`／`k` 把焦點放到第一張／最後一張；其他動作鍵 toast
   「先把滑鼠移到卡片上，或按 j 選一張」。

## 焦點模型

- 模組級 `let focusKey = null;`（`data-key`），`let lastShownKeys = [];`，
  `let lastStateAction = null;`（`{key, prevState}`）。
- `#grid` 上委派 `mouseover`：`ev.target.closest(".card")` 的 `data-key` ≠ `focusKey` 才呼叫 `setFocus(key)`。
- `setFocus(key, scroll)`：移除舊 `.card.focus`，加到新的；`scroll` 為真時
  `node.scrollIntoView({block:"nearest"})`（鍵盤移動才捲，滑鼠懸停不捲）。
- `renderSignals` 畫完 `#grid` 後：`focusKey = resolveFocus(lastShownKeys, shownKeys, focusKey)`；
  `lastShownKeys = shownKeys`；然後 `setFocus(focusKey, false)`。
  略過後卡片淡出（`setTimeout(renderSignals,160)`），焦點就落到原位置的下一張；連按 `n` 即逐張處理。
- 已知限制（寫進註解即可）：滑鼠不動時 `mouseover` 不會再觸發，焦點以鍵盤為準；滑鼠一動就回到懸停。

**明確不做（YAGNI）：** `?` 說明浮層、可自訂按鍵、復原分類動作、多選批次、Enter 開商品頁。

**檔案總覽：**
- Modify: `web/static/index.html`（CSS、`card()`、`auctionCard()`、`bucketActs()`、`renderSignals()`、新增 HOTKEY 區塊與事件接線）
- Create: `tests/test_hotkeys.py`
- Modify: `CLAUDE.md` 第九節 `serve` 那一行補快捷鍵一句

---

### Task 1 `@inline`：純邏輯區塊 `hotkeyAction` / `resolveFocus` ＋ node 測試

**Files:**
- Modify: `web/static/index.html`（緊接在 `/* ==== LOCAL-STATE-LOGIC:END ==== */` 之後，約第 1334 行）
- Create: `tests/test_hotkeys.py`

- [ ] **Step 1: 寫紅燈測試**

```python
"""卡片快捷鍵：按鍵→動作（hotkeyAction）與焦點遷移（resolveFocus）是純函式，
抽出 index.html 的 HOTKEY-LOGIC 標記區塊丟 node 實跑，斷言留在 pytest。
作法沿用 tests/test_optimistic_state.py。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
BEGIN = "/* ==== HOTKEY-LOGIC:BEGIN"
END = "/* ==== HOTKEY-LOGIC:END"

HARNESS = """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = input.fn === "hotkeyAction"
  ? hotkeyAction(input.key, input.ctx)
  : resolveFocus(input.prevKeys, input.nextKeys, input.focusKey);
console.log(JSON.stringify(out === undefined ? null : out));
"""


def extract_block() -> str:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(BEGIN), text.index(END)
    block = text[start:end]
    assert "function hotkeyAction(" in block, "區塊裡找不到 hotkeyAction"
    assert "function resolveFocus(" in block, "區塊裡找不到 resolveFocus"
    return block


def run_js(payload: dict):
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("找不到 node，無法執行前端邏輯測試")
    proc = subprocess.run(
        [node, "-e", extract_block() + HARNESS],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 執行失敗：{proc.stderr}"
    return json.loads(proc.stdout)


def action(key: str, *, is_auction: bool = False, bucket: str | None = None):
    return run_js({"fn": "hotkeyAction", "key": key,
                   "ctx": {"isAuction": is_auction, "bucket": bucket}})


def focus(prev: list[str], nxt: list[str], focus_key: str | None):
    return run_js({"fn": "resolveFocus", "prevKeys": prev, "nextKeys": nxt, "focusKey": focus_key})


# ---- hotkeyAction：按鍵表（使用者定案 zxcvbnm,）----------------------------

@pytest.mark.parametrize("key,state", [
    ("z", "watching"), ("x", "in_bundle"), ("c", "asked_seller"),
    ("v", "offer_sent"), ("b", "bought"), ("n", "skipped"),
])
def test_state_keys_follow_button_order(key, state):
    assert action(key) == {"kind": "state", "state": state}


def test_skip_is_n_on_auction_card_too():
    # 綁的是動作不是位置：競標卡少兩顆按鈕，n 仍然是略過
    assert action("n", is_auction=True) == {"kind": "state", "state": "skipped"}


@pytest.mark.parametrize("key,label", [("x", "加入湊單"), ("c", "已詢問")])
def test_auction_card_lacks_bundle_and_asked(key, label):
    assert action(key, is_auction=True) == {"kind": "unsupported", "label": label}


def test_bucket_key_toggles_on():
    assert action("m", bucket=None) == {"kind": "bucket", "bucket": "high_value"}
    assert action(",", bucket=None) == {"kind": "bucket", "bucket": "rare"}


def test_bucket_key_toggles_off_when_already_set():
    # 與 bucketActs 同一個開關語意：已亮著就送空字串＝清除
    assert action("m", bucket="high_value") == {"kind": "bucket", "bucket": ""}
    assert action(",", bucket="rare") == {"kind": "bucket", "bucket": ""}


def test_bucket_key_switches_between_buckets():
    assert action("m", bucket="rare") == {"kind": "bucket", "bucket": "high_value"}


@pytest.mark.parametrize("key,delta", [("j", 1), ("arrowdown", 1), ("k", -1), ("arrowup", -1)])
def test_nav_keys(key, delta):
    assert action(key) == {"kind": "nav", "delta": delta}


def test_undo_key():
    assert action("u") == {"kind": "undo"}


@pytest.mark.parametrize("key", ["a", "1", "escape", "enter", " ", "N", "Z"])
def test_unknown_or_uppercase_keys_do_nothing(key):
    # 呼叫端負責 toLowerCase；純函式對大寫一律回 null，不猜
    assert action(key) is None


# ---- resolveFocus：焦點遷移 ---------------------------------------------------

def test_focus_kept_when_still_shown():
    assert focus(["a", "b", "c"], ["a", "b", "c"], "b") == "b"


def test_focus_moves_to_next_after_removal():
    # 略過 b 之後焦點落到原位置的下一張（c），連按 n 就是逐張處理
    assert focus(["a", "b", "c"], ["a", "c"], "b") == "c"


def test_focus_moves_to_previous_when_last_removed():
    assert focus(["a", "b", "c"], ["a", "b"], "c") == "b"


def test_focus_skips_over_multiple_removed():
    assert focus(["a", "b", "c", "d"], ["a", "d"], "b") == "d"


def test_focus_null_when_nothing_left():
    assert focus(["a"], [], "a") is None


def test_focus_null_when_no_overlap_with_previous_list():
    # 換分頁：舊清單與新清單無交集，不要亂指一張
    assert focus(["a", "b"], ["x", "y"], "a") is None


def test_focus_null_stays_null():
    assert focus(["a", "b"], ["a", "b"], None) is None


# ---- 按鈕上的 <kbd> 提示要與按鍵表同步 ---------------------------------------

@pytest.mark.parametrize("key,label", [
    ("z", "觀察中"), ("x", "加入湊單"), ("c", "已詢問"), ("v", "已出價"),
    ("b", "已購買"), ("n", "略過"), ("b", "已標到"),
])
def test_buttons_show_their_hotkey(key, label):
    html = INDEX.read_text(encoding="utf-8")
    assert f"{label}<kbd>{key}</kbd>" in html, f"按鈕「{label}」沒有 <kbd>{key}</kbd> 提示"


def test_bucket_buttons_show_their_hotkey():
    # 分類按鈕的 <kbd> 是模板展開（bucketActs 用 BUCKET_KEYS[k]），原文不會有字面的
    # <kbd>m</kbd>；所以釘兩件事：來源表的值＋模板真的把它畫進按鈕。
    # （2026-10-15 裁決：原版斷言字面 <kbd>m</kbd> 與實作矛盾，改成這樣。）
    html = INDEX.read_text(encoding="utf-8")
    assert 'BUCKET_KEYS = {high_value: "m", rare: ","}' in html
    assert "<kbd>${BUCKET_KEYS[k]}</kbd></button>" in html
```

- [ ] **Step 2: 跑測試確認紅燈**

Run: `.venv/bin/pytest tests/test_hotkeys.py -v`
Expected: 全部 FAIL（`extract_block` 找不到 BEGIN 標記 → `ValueError: substring not found`）。

- [ ] **Step 3: 在 `web/static/index.html` 加入純邏輯區塊**

放在 `/* ==== LOCAL-STATE-LOGIC:END ==== */` 那一行之後（約第 1334 行）。
`BUCKETS` 常數已在第 888 行定義，這裡不重複定義，但純函式**不得**依賴它
（node 測試只抽這一段），所以 bucket 鍵值寫死在 `HOTKEY_BUCKETS`。

```js
/* ==== HOTKEY-LOGIC:BEGIN ==== */
/* 卡片快捷鍵（2026-10-15 使用者定案）：鍵盤最下排由左到右＝卡片按鈕列由左到右，
   n 剛好落在略過。鍵綁的是**動作**不是按鈕位置——競標卡沒有 x／c 兩個動作，
   但 n 在哪種卡片都是略過。純函式、不碰 DOM：tests/test_hotkeys.py 用 node 實跑。 */
const HOTKEY_STATES  = {z: "watching", x: "in_bundle", c: "asked_seller",
                        v: "offer_sent", b: "bought", n: "skipped"};
const HOTKEY_LABELS  = {new: "待處理", watching: "觀察中", in_bundle: "加入湊單",
                        asked_seller: "已詢問", offer_sent: "已出價", bought: "已購買", skipped: "略過"};
const HOTKEY_BUCKETS = {m: "high_value", ",": "rare"};
//: 競標卡（auctionCard）沒有的狀態動作：按了要說「沒有這顆按鈕」，不能靜默。
const AUCTION_UNSUPPORTED = new Set(["in_bundle", "asked_seller"]);
const HOTKEY_NAV = {j: 1, arrowdown: 1, k: -1, arrowup: -1};

/** 鍵 → 動作。`key` 由呼叫端先 toLowerCase（這裡對大寫一律回 null，不猜）。
    ctx = {isAuction, bucket}：目前焦點卡片是不是競標卡、目前分類是什麼。
    回傳 {kind:"state",state} | {kind:"bucket",bucket} | {kind:"nav",delta}
         | {kind:"undo"} | {kind:"unsupported",label} | null */
function hotkeyAction(key, ctx){
  ctx = ctx || {};
  if(key in HOTKEY_STATES){
    const state = HOTKEY_STATES[key];
    if(ctx.isAuction && AUCTION_UNSUPPORTED.has(state)){
      return {kind: "unsupported", label: HOTKEY_LABELS[state]};
    }
    return {kind: "state", state: state};
  }
  if(key in HOTKEY_BUCKETS){
    const b = HOTKEY_BUCKETS[key];
    // 與 bucketActs 同一個開關語意：已經是這個分類就送空字串＝清除
    return {kind: "bucket", bucket: ctx.bucket === b ? "" : b};
  }
  if(key in HOTKEY_NAV) return {kind: "nav", delta: HOTKEY_NAV[key]};
  if(key === "u") return {kind: "undo"};
  return null;
}

/** 重畫之後焦點該落在誰身上。prevKeys／nextKeys 是重畫前後 #grid 的 data-key 順序。
    還在 → 留著；不在（被略過、被篩掉）→ 從它原本的位置往後找第一張還在的，
    往後沒有就往前找；完全無交集（換分頁）→ null，不亂指。 */
function resolveFocus(prevKeys, nextKeys, focusKey){
  if(focusKey == null) return null;
  if(nextKeys.includes(focusKey)) return focusKey;
  const idx = prevKeys.indexOf(focusKey);
  if(idx < 0) return null;
  for(let i = idx + 1; i < prevKeys.length; i++){
    if(nextKeys.includes(prevKeys[i])) return prevKeys[i];
  }
  for(let i = idx - 1; i >= 0; i--){
    if(nextKeys.includes(prevKeys[i])) return prevKeys[i];
  }
  return null;
}
/* ==== HOTKEY-LOGIC:END ==== */
```

- [ ] **Step 4: 跑測試**

Run: `.venv/bin/pytest tests/test_hotkeys.py -v`
Expected: `hotkeyAction`／`resolveFocus` 全部 PASS；`test_buttons_show_their_hotkey`
與 `test_bucket_buttons_show_their_hotkey` 仍 FAIL（Task 3 才加 `<kbd>`）。

- [ ] **Step 5: Commit**

```bash
git add tests/test_hotkeys.py web/static/index.html
git commit -m "feat(dashboard): 快捷鍵純邏輯 hotkeyAction／resolveFocus＋node 測試"
```

---

### Task 2 `@inline`：DOM 接線——焦點外框、mouseover、keydown、復原

**Files:**
- Modify: `web/static/index.html`
  - CSS：`.card.hot` 那一段附近（約第 56 行）加 `.card.focus`
  - `renderSignals()`（約第 1671 行 `document.getElementById("grid").innerHTML = …` 之後）
  - 既有 `document.addEventListener("keydown", …)`（約第 1275 行，只有 Esc）**改成**完整處理器
  - 在 HOTKEY-LOGIC 區塊之後新增 `setFocus`／`runHotkey`／`undoLastState` 與 mouseover 接線

- [ ] **Step 1: CSS**

在 `.card.trig{…}` 之後加：

```css
  /* 鍵盤焦點：按鍵會打到的那一張。用 outline 不用 border——border 已被 hot／trig／expired
     三種狀態佔用，疊上去會互相覆蓋。 */
  .card.focus{outline:2px solid var(--accent);outline-offset:2px}
```

- [ ] **Step 2: 模組級狀態與 `setFocus`**

緊接在 `/* ==== HOTKEY-LOGIC:END ==== */` 之後：

```js
let focusKey = null;          // 按鍵會作用的卡片（data-key）；null＝沒有焦點
let lastShownKeys = [];       // 上一次 renderSignals 畫出的 data-key 順序（給 resolveFocus）
let lastStateAction = null;   // {key, prevState}：u 復原用，只記**狀態**動作，分類開關不記

/** 把焦點外框移到 key 那張；scroll 為真才捲到可見（鍵盤移動才捲，滑鼠懸停不捲）。 */
function setFocus(key, scroll){
  focusKey = key || null;
  document.querySelectorAll("#grid .card.focus").forEach(n => n.classList.remove("focus"));
  if(!focusKey) return;
  const node = document.querySelector(`#grid [data-key="${CSS.escape(focusKey)}"]`);
  if(!node){ focusKey = null; return; }
  node.classList.add("focus");
  if(scroll) node.scrollIntoView({block: "nearest"});
}

// 滑鼠停在卡片任何位置就是焦點——解決「略過按鈕位置會跑」：手不用瞄按鈕。
// 滑鼠不動時 mouseover 不再觸發，此時焦點以鍵盤（j/k、略過後自動落到下一張）為準。
document.getElementById("grid").addEventListener("mouseover", ev => {
  const node = ev.target.closest?.(".card");
  if(node && node.dataset.key !== focusKey) setFocus(node.dataset.key, false);
});
```

- [ ] **Step 3: `renderSignals` 畫完後補焦點**

在 `document.getElementById("grid").innerHTML = shown.map(auctionView ? auctionCard : card).join("");`
之後、`const emptyEl = …` 之前插入：

```js
  // 重畫會把 .focus class 洗掉：依重畫前後的順序決定焦點落在誰身上（略過一張 → 下一張）。
  const shownKeys = shown.map(i => i.key);
  const nextFocus = resolveFocus(lastShownKeys, shownKeys, focusKey);
  lastShownKeys = shownKeys;
  setFocus(nextFocus, false);
```

- [ ] **Step 4: keydown 處理器（取代既有只處理 Esc 的那個）**

把第 1275-1277 行的

```js
document.addEventListener("keydown", ev => {
  if(ev.key === "Escape") document.getElementById("lightbox").classList.remove("show");
});
```

改成

```js
document.addEventListener("keydown", ev => {
  const lightbox = document.getElementById("lightbox");
  if(ev.key === "Escape"){ lightbox.classList.remove("show"); return; }
  // 守門：在輸入框打字、按著 Cmd/Ctrl/Alt、輸入法組字中、燈箱開著、不在標的分頁 → 不攔截
  const t = ev.target;
  if(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  if(ev.metaKey || ev.ctrlKey || ev.altKey || ev.isComposing) return;
  if(lightbox.classList.contains("show")) return;
  const view = document.getElementById("signals-view");
  if(!view || view.style.display === "none") return;
  const action = hotkeyAction(ev.key.toLowerCase(), focusContext());
  if(!action) return;
  ev.preventDefault();
  runHotkey(action);
});
```

（`signals-view` 的切換就是 `style.display`，見第 2586 行 `document.getElementById("signals-view").style.display = …`。）

- [ ] **Step 5: `focusContext`／`runHotkey`／`undoLastState`**

放在 `setFocus` 之後：

```js
/** 給 hotkeyAction 的 ctx：焦點卡片是不是競標卡、目前分類。沒有焦點回 {}。 */
function focusContext(){
  const it = focusKey ? items.find(i => i.key === focusKey) : null;
  // 競標卡的判準與 renderSignals 同源：競標視圖畫的全是 auctionCard
  const auctionView = filterMode === "auction" || quick.ceiling || quick.room;
  return it ? {isAuction: auctionView, bucket: it.bucket || null} : {};
}

/** 執行一個 hotkeyAction 的結果。狀態／分類一律走既有 setState／setBucket
    （樂觀更新、作廢載入、失敗對帳那套不重寫）。 */
function runHotkey(action){
  if(action.kind === "nav"){
    const keys = [...document.querySelectorAll("#grid .card")].map(n => n.dataset.key);
    if(!keys.length) return;
    const idx = keys.indexOf(focusKey);
    let next;
    if(idx < 0) next = action.delta > 0 ? 0 : keys.length - 1;     // 沒焦點：第一張／最後一張
    else next = Math.min(keys.length - 1, Math.max(0, idx + action.delta));
    setFocus(keys[next], true);
    return;
  }
  if(action.kind === "undo"){ undoLastState(); return; }
  if(!focusKey){ toast("先把滑鼠移到卡片上，或按 j 選一張"); return; }
  if(action.kind === "unsupported"){ toast(`這張卡沒有「${action.label}」`); return; }
  const it = items.find(i => i.key === focusKey);
  if(!it){ toast("焦點卡片已不在清單上"); setFocus(null, false); return; }
  if(action.kind === "state"){
    lastStateAction = {key: it.key, prevState: it.state};
    setState(it.key, action.state);
  }else if(action.kind === "bucket"){
    setBucket(it.key, action.bucket);
  }
}

/** u：把上一個狀態動作送回原狀態。被略過的卡片本地已經移除，setState 的樂觀更新
    找不到它（applyLocalState idx<0 → 不變）；POST 落庫後重抓一次清單讓它回來。 */
async function undoLastState(){
  if(!lastStateAction){ toast("沒有可復原的動作"); return; }
  const {key, prevState} = lastStateAction;
  lastStateAction = null;                       // 只復原一步，不做復原的復原
  await setState(key, prevState);
  if(!items.some(i => i.key === key)){
    await loadSignals().catch(e => toast("載入失敗：" + e.message));
  }
  setFocus(key, true);
  toast(`已復原為「${HOTKEY_LABELS[prevState] || prevState}」`);
}
```

- [ ] **Step 6: 跑全測試確認沒弄壞既有 node 測試**

Run: `.venv/bin/pytest tests/test_hotkeys.py tests/test_optimistic_state.py tests/test_expiry_banner.py -v`
Expected: 除了 Task 1 留下的兩個 `<kbd>` 測試外全部 PASS。

- [ ] **Step 7: Commit**

```bash
git add web/static/index.html
git commit -m "feat(dashboard): 卡片快捷鍵接線——懸停／j k 焦點外框、zxcvbnm, 動作、u 復原"
```

---

### Task 3 `@inline`：按鈕 `<kbd>` 提示＋清單上方一行說明＋CLAUDE.md

**Files:**
- Modify: `web/static/index.html`：`card()` 的 `.acts`（約 1060-1067 行）、`auctionCard()` 的 `.acts`
  （約 1882-1885 行）、`bucketActs()`（約 1309-1317 行）、`#grid` 上方（約 568 行）、CSS
- Modify: `CLAUDE.md` 第九節 `ygo-sniper serve` 那一行

- [ ] **Step 1: CSS**

在 `.acts button{…}` 之後加：

```css
  .acts kbd{font-family:var(--mono);font-size:10px;color:var(--dim);margin-left:5px;
            border:1px solid var(--line);border-radius:3px;padding:0 3px;line-height:1.4}
  .acts button.on kbd{color:inherit;border-color:currentColor}
```

- [ ] **Step 2: 按鈕加 `<kbd>`（測試要求的字面是 `標籤<kbd>鍵</kbd>`，中間不能有空白）**

`card()`：

```js
        <button onclick="setState('${it.key}','watching')">觀察中<kbd>z</kbd></button>
        <button onclick="setState('${it.key}','in_bundle')">加入湊單<kbd>x</kbd></button>
        <button onclick="setState('${it.key}','asked_seller')">已詢問<kbd>c</kbd></button>
        <button onclick="setState('${it.key}','offer_sent')">已出價<kbd>v</kbd></button>
        <button onclick="setState('${it.key}','bought')">已購買<kbd>b</kbd></button>
        <button onclick="setState('${it.key}','skipped')">略過<kbd>n</kbd></button>
```

`auctionCard()`：

```js
        <button onclick="setState('${it.key}','watching')">觀察中<kbd>z</kbd></button>
        <button onclick="setState('${it.key}','offer_sent')">已出價<kbd>v</kbd></button>
        <button onclick="setState('${it.key}','bought')">已標到<kbd>b</kbd></button>
        <button onclick="setState('${it.key}','skipped')">略過<kbd>n</kbd></button>
```

`bucketActs()`：把 `>${label}</button>` 改成 `>${label}<kbd>${BUCKET_KEYS[k]}</kbd></button>`，
並在 `BUCKETS` 常數（第 888 行）旁加 `const BUCKET_KEYS = {high_value: "m", rare: ","};`
（與 HOTKEY-LOGIC 的 `HOTKEY_BUCKETS` 是同一張表的反向；兩邊都寫死，`test_bucket_buttons_show_their_hotkey` 釘住）。

- [ ] **Step 3: 清單上方一行說明**

在 `<div class="rows" id="grid"></div>` 之前加：

```html
  <div class="hint" style="margin:0 0 8px">鍵盤：滑鼠停在卡片上或按 <b>j</b>／<b>k</b> 選卡，
    按鈕旁的字母就是快捷鍵（<b>n</b> 略過），<b>u</b> 復原上一個狀態動作。</div>
```

- [ ] **Step 4: 修正 Task 1 寫的 `test_bucket_buttons_show_their_hotkey`，再跑測試**

把 `tests/test_hotkeys.py` 裡該測試換成上面 Task 1 Step 1 現在列的版本（釘 `BUCKET_KEYS` 來源表
＋ `bucketActs` 模板使用，不再斷言字面 `<kbd>m</kbd>`）。

Run: `.venv/bin/pytest tests/test_hotkeys.py -v`
Expected: 全部 PASS（39 passed）。

Run: `make test`
Expected: 末行 `N passed`（N ≥ 2000 ＋ 本案新增；不得有 failed）。

- [ ] **Step 5: CLAUDE.md**

第九節 `ygo-sniper serve` 那一行下方加一行註解：

```
                               #   卡片快捷鍵 zxcvbn=觀察/湊單/詢問/出價/購買/略過、m ,=💎🌟、
                               #   j k 移焦點、u 復原（焦點＝滑鼠懸停或 j/k；見 docs/plans/dashboard-hotkeys.md）
```

- [ ] **Step 6: Commit**

```bash
git add web/static/index.html CLAUDE.md
git commit -m "feat(dashboard): 按鈕 <kbd> 快捷鍵提示＋說明列；CLAUDE.md 第九節補快捷鍵"
```

---

### Task 4 `@inline`：審查修正（2026-10-15 reviewer 第一輪，主線程已逐條核實）

**Files:**
- Modify: `web/static/index.html`（keydown 守門、`setState`、`setFocus` 附近的 mouseover、`runHotkey`、`undoLastState`）
- Modify: `tests/test_hotkeys.py`（補兩條 grep 回歸）

主線程裁決（寫在這裡就是定案）：
- **C1（Critical）** `setState` 的 catch 只 toast 不 rethrow，`undoLastState` 的 `await setState(...)` 永遠成功，
  失敗時仍 toast「已復原為…」把「失敗：…」蓋掉（違反工程原則 #3、專案第五節）。
  修法：`setState` **回傳布林**（成功 `true`、catch 分支 `false`），`undoLastState` 失敗就不說復原。
- **W1** 沒擋 `ev.repeat`：按住 n 會連略過下一張。修法：守門加 `if(ev.repeat) return;`（主線程實測確認會觸發）。
- **W2** 略過後 160ms 淡出期間 `items` 已移除、DOM 還在、`focusKey` 還指著它，再按一鍵會走到
  「焦點卡片已不在清單上」把焦點清空，連按 n 斷掉。修法：卡片**開始離場的當下**就把焦點移走——
  `setState` 的 `res.removed && node` 分支加 `.leaving` 之後呼叫 `focusLeaving(key)`。
- **W3** 守門漏了 `#xp-confirm`（清除確認框，`style.display` 切換）：框開著按 n 會改到框後面的卡。修法：守門加一條。
- **W4** `lastStateAction` 只記快捷鍵，滑鼠點按鈕不記；且永不過期，中間用滑鼠改過狀態再按 u 會覆寫。
  修法：改在 **`setState` 裡**記 `{key, prevState, newState}`（滑鼠、快捷鍵、復原本身都經過它），
  `runHotkey` 不再自己記；`undoLastState` 若本地找得到該卡且 `it.state !== newState`（之後又被改過）
  → toast「這張卡之後又被改過，不復原」並清掉。副作用：復原本身也會被記，**再按一次 u ＝ 取消復原**，
  說明列文字改成「u 復原上一個狀態動作（再按一次取消）」。
- **W5** 捲動後瀏覽器補發 mouseover 把焦點拉回游標底下：主線程 Chrome 實測**未重現**（j 三次、捲 850px、
  游標下已換卡，mouseover 零次）。但防護只要兩行：mouseover 的 `clientX/Y` 與上一次相同就忽略（補發事件
  座標不變、真實移動座標必變）。做。
- **S1**（n 之後立刻 u，兩個 POST 併發、落庫順序無保證）：不做，記為觀察；SQLite 鎖住時的時序問題
  要靠後端序列化，不在本案。

- [ ] **Step 1: 補兩條 grep 回歸測試**（`tests/test_hotkeys.py` 末尾）

```python
# ---- DOM 守門的文字回歸（DOM 層沒有 node 測試，至少釘住這兩條不被順手刪掉）------

def test_keydown_guard_blocks_repeat_and_confirm_dialog():
    html = INDEX.read_text(encoding="utf-8")
    assert "if(ev.repeat) return;" in html, "按住連發必須擋掉（2026-10-15 審查 W1）"
    assert 'if(dlg && dlg.style.display !== "none") return;' in html, \
        "keydown 守門必須檢查 #xp-confirm 確認框（審查 W3）"


def test_set_state_reports_failure_to_caller():
    # undoLastState 靠這個布林決定要不要說「已復原」（審查 C1）
    html = INDEX.read_text(encoding="utf-8")
    body = html.split("async function setState(key, state){", 1)[1].split("\n}\n", 1)[0]
    assert "return true;" in body and "return false;" in body
```

- [ ] **Step 2: 跑測試確認這兩條紅燈**

Run: `.venv/bin/pytest tests/test_hotkeys.py -v`
Expected: 新的兩條 FAIL，其餘 39 PASS。

- [ ] **Step 3: keydown 守門（W1、W3）**

在 `if(lightbox.classList.contains("show")) return;` 之後加：

```js
  if(ev.repeat) return;                              // 按住連發：一次按鍵只做一件事
  const dlg = document.getElementById("xp-confirm"); // 清除確認框開著：框後面的卡不能被改
  if(dlg && dlg.style.display !== "none") return;
```

- [ ] **Step 4: `setState` 回傳布林＋記錄動作＋離場即移焦（C1、W4、W2）**

`async function setState(key, state){` 開頭（`const droppedLoad` 之前）加：

```js
  // u 復原用的帳：記在這裡而不是 runHotkey，滑鼠點按鈕、快捷鍵、復原本身都經過這一支。
  const before = items.find(i => i.key === key);
  lastStateAction = {key, prevState: before ? before.state : null, newState: state};
```

`if(res.removed && node){ node.classList.add("leaving"); setTimeout(renderSignals, 160); }` 改成：

```js
  if(res.removed && node){
    node.classList.add("leaving");                 // 150ms 淡出，看得出是哪張走了
    focusLeaving(key);                             // 焦點現在就移走，不等 160ms（連按 n 才不會斷）
    setTimeout(renderSignals, 160);
  }
```

try 區塊末尾（`loadStats().catch(...)` 之後）加 `return true;`；catch 區塊末尾加 `return false;`。
`prevState` 為 null（本地找不到那張卡）時 `undoLastState` 視為沒有可復原的動作。

- [ ] **Step 5: `focusLeaving`、mouseover 防補發（W2、W5）**

`setFocus` 之後加：

```js
/** 焦點卡片開始離場（淡出中）：立刻把焦點交給它後面那張。DOM 上它還在、但已帶 .leaving。 */
function focusLeaving(key){
  if(focusKey !== key) return;
  const all  = [...document.querySelectorAll("#grid .card")].map(n => n.dataset.key);
  const live = [...document.querySelectorAll("#grid .card:not(.leaving)")].map(n => n.dataset.key);
  setFocus(resolveFocus(all, live, key), false);
}
```

mouseover 接線改成：

```js
let lastMouse = null;   // 瀏覽器在捲動／重排後可能補發座標相同的 mouseover；座標沒變就不是使用者在動
document.getElementById("grid").addEventListener("mouseover", ev => {
  if(lastMouse && lastMouse.x === ev.clientX && lastMouse.y === ev.clientY) return;
  lastMouse = {x: ev.clientX, y: ev.clientY};
  const node = ev.target.closest?.(".card");
  if(node && node.dataset.key !== focusKey) setFocus(node.dataset.key, false);
});
```

- [ ] **Step 6: `runHotkey` 不再記帳、`undoLastState` 看布林與過期（C1、W4）**

`runHotkey` 的 state 分支刪掉 `lastStateAction = {…};` 那一行，只留 `setState(it.key, action.state);`。

`undoLastState` 整個換成：

```js
/** u：把上一個狀態動作送回原狀態。帳由 setState 記（滑鼠與快捷鍵都算）。
    被略過的卡片本地已移除，setState 的樂觀更新找不到它；POST 落庫後重抓一次讓它回來。
    復原本身也會被 setState 記下來，所以再按一次 u ＝ 取消復原。 */
async function undoLastState(){
  const act = lastStateAction;
  if(!act || act.prevState == null){ toast("沒有可復原的動作"); return; }
  const cur = items.find(i => i.key === act.key);
  if(cur && cur.state !== act.newState){
    lastStateAction = null;
    toast("這張卡之後又被改過，不復原");
    return;
  }
  const ok = await setState(act.key, act.prevState);
  if(!ok) return;                                   // setState 已 toast 失敗並重抓對帳，這裡不能再說成功
  // 被略過的卡本地找不到，setState 記下的 prevState 會是 null → 第二次 u 就取消不了。
  // 這裡自己知道它剛才是什麼（act.newState），用它覆寫成正確的「取消復原」帳。
  //（2026-10-15 builder 回報的缺口，主線程補上。）
  lastStateAction = {key: act.key, prevState: act.newState, newState: act.prevState};
  if(!items.some(i => i.key === act.key)){
    try{ await loadSignals(); }
    catch(e){ toast("已復原，但重載清單失敗：" + e.message); return; }
  }
  setFocus(act.key, true);
  toast(`已復原為「${HOTKEY_LABELS[act.prevState] || act.prevState}」`);
}
```

- [ ] **Step 7: 說明列文字**

`.hint` 裡 `<b>u</b> 復原上一個狀態動作。` 改成 `<b>u</b> 復原上一個狀態動作（再按一次取消）。`

- [ ] **Step 8: 跑測試**

Run: `.venv/bin/pytest tests/test_hotkeys.py tests/test_optimistic_state.py -v`
Expected: 全部 PASS（41 + 4）。

Run: `make test`
Expected: 末行 `N passed`，0 failed。

- [ ] **Step 9: Commit**

```bash
git add web/static/index.html tests/test_hotkeys.py
git commit -m "fix(dashboard): 快捷鍵審查修正——setState 回報失敗、擋連發與確認框、離場即移焦、u 帳改記在 setState"
```

---

### Task 5：第二輪審查修正（2026-10-16，主線程自己動手——單檔小修＋一支只讀端點）

reviewer 第二輪 finding 與裁決（已全部實作在 commit 中）：
- **W-1** u 復原的 POST 等待期間對別張卡按 n → 復原回來後無條件覆寫帳，B 的帳被 A 的「取消復原」帳蓋掉。
  修：只在 `lastStateAction` 仍是這次 setState 記的那筆時才覆寫。
- **W-2** 復原後 `loadSignals` 被作廢（卡還沒畫出來）→ `setFocus` 把焦點清成 null，連按節奏斷掉。
  修：DOM 有那張卡才 `setFocus`，否則讓焦點留在 `focusLeaving` 交出去的位置。
- **W-3** 帳過期的判定只看本地 `items`；被略過的卡本地看不到，中間若被清除實證／CLI／另一視窗改掉，
  u 會把實證已售出的卡拉回待處理並歸零 `cleared_verified`（CLAUDE.md 第五節第 5 項同型）。
  修：**新增只讀端點 `GET /api/signals/{key}/state`**（`web/app.py`，測試 `tests/test_hotkeys_web.py`），
  復原前先問後端；與帳上 `newState` 不同 → 不復原並說出現在是什麼；讀不到 → 不復原（讀不到 ≠ 可以改）。
  這是本案唯一的後端改動，純讀取。
- **S-1** setState POST 失敗後帳仍留著沒落庫的動作 → catch 分支把自己那筆收回（還原成呼叫前的帳），別張卡期間記的帳不動。
- **S-2** setBucket 連動 new→watching 不經 setState → 連動改了狀態時清帳，避免 u 復原到別張卡卻像在說這張回來了。
- **S-3** `lastStateAction` 宣告處註解更新。
- **S-4** plan 裡「grep 計數因此是 2」那句無對應測試，刪除。
- 觀察（不做）：會補發 mouseover 的瀏覽器（Firefox／Safari 未實測）在滾輪捲動後焦點可能留在捲出畫面的卡上；Chrome 實測不補發。

### Task 6：第三輪審查修正（2026-10-16，主線程自己動手，使用者要求第三輪確認品質）

- **W-A** `undoLastState` 無重入鎖：連按 u u 兩次讀到同一筆帳，第二下不是「取消復原」而是重送同值或被判成「外部改過」。
  修：模組級 `undoInFlight` 旗標，進行中再按 u → toast「復原進行中」。
- **W-B** 復原收尾無條件把焦點拉回被復原的卡：等待期間使用者已 j/n 到下一張，焦點被拉回、下一下 n 打在剛救回的卡上。
  修：開始時記 `focusAtStart`，收尾只在 `focusKey === focusAtStart` 時 `setFocus`。
- **S-A** j/k 的卡片清單含 `.leaving` → 改 `:not(.leaving)`，與 `focusLeaving` 同源。
- **S-B** 空清單按 ↑↓ 被 `preventDefault` 成什麼都不做 → nav 而無卡時放行給瀏覽器捲頁。
- **S-C**（不改，記錄）後端全掛、A B 連續失敗時帳可能退回沒落庫的 A；被 W-3 的 GET 檢查擋住，只會多一則「之後又被改成…」訊息，不寫錯資料。
- reviewer 結論：修完不需整輪複審，主線程在 Chrome 重跑「連按 u u」與「u 之後立刻 n」兩個情境即可。

## 主線程驗收（builder 回報後親跑）

1. `.venv/bin/pytest tests/test_hotkeys.py -v` 全綠；`make test` 末行 `N passed`。
2. `grep -c "HOTKEY-LOGIC:BEGIN" web/static/index.html` → 1；`grep -c "<kbd>n</kbd>" web/static/index.html` → 2（一般卡＋競標卡）。
3. 瀏覽器實測（`ygo-sniper serve` → http://127.0.0.1:8321）：
   - 滑鼠移到卡片上出現 accent 外框；`j`／`k` 外框移動並捲到可見。
   - 在搜尋框打 `n` 不會略過任何卡。
   - 對一張待處理卡按 `n` → 淡出、焦點落到下一張；按 `u` → 卡片回來、toast「已復原為「待處理」」。
   - 競標視圖按 `x` → toast「這張卡沒有「加入湊單」」，資料不變。
