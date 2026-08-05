"""前端篩選的純函式（「🙈 隱藏競標中」），用 node 真的跑一遍。

為什麼要測這種看起來像一行的東西：**篩選錯誤的症狀是「東西不見了」**。
畫面上「被開關藏起來」與「今天本來就沒有」長得一模一樣，沒有紅字、沒有例外——
CLAUDE.md 第一節說的「誤殺是靜默的」在前端這一側是同一件事。

「是不是競標」的判準不在這裡另外寫一份：一律看後端寫進 db 的 `live_auction`
旗標（scoring.py → bidding.py，唯一判準是 price_kind == "current_bid"）。
測試自己判一次 price_kind 的話，後端改了判準測試還是綠的——那等於沒測到。

作法沿用 tests/test_auction_view.py：從 index.html 抽出標記區塊丟進 node 執行，
斷言留在 pytest 這一側。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ygo_sniper.domain import Flag

INDEX = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
BEGIN = "/* ==== SIGNAL-FILTER-LOGIC:BEGIN"
END = "/* ==== SIGNAL-FILTER-LOGIC:END"

#: node 端的驅動器：讀 stdin 的 {items, hideAuction, filterMode}，把純函式的
#: 輸出印成 JSON。**不含任何斷言**——判斷留在 pytest。
HARNESS = """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const {items, hideAuction, filterMode} = input;
console.log(JSON.stringify({
  isAuction: items.map(isAuction),
  hidden: items.map(it => hiddenByAuctionFilter(it, hideAuction)),
  conflict: auctionFiltersConflict(filterMode, hideAuction),
}));
"""


def extract_block() -> str:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(BEGIN), text.index(END)
    block = text[start:end]
    # 抽出來的要真的是那段邏輯：標記被搬走而函式沒跟上時這裡必須紅，
    # 而不是讓 node 跑一段空殼再報 ReferenceError。
    for fn in ("hiddenByAuctionFilter", "auctionFiltersConflict"):
        assert f"function {fn}(" in block, f"區塊裡找不到 {fn}"
    assert "isAuction" in block, "區塊裡找不到 isAuction"
    return block


def run_js(items: list[dict], *, hide_auction: bool, filter_mode: str = "all") -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - 開發機沒有 node 時
        pytest.skip("找不到 node，無法執行前端邏輯測試")
    proc = subprocess.run(
        [node, "-e", extract_block() + HARNESS],
        input=json.dumps(
            {"items": items, "hideAuction": hide_auction, "filterMode": filter_mode}
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node 執行失敗：{proc.stderr}"
    return json.loads(proc.stdout)


#: 旗標字串從 domain.Flag 拿，不是手打字面值——後端改了旗標名這裡要跟著紅。
AUCTION_FLAG = Flag.LIVE_AUCTION.value

ITEMS = [
    {"key": "auction", "flags": [AUCTION_FLAG]},
    {"key": "auction-plus", "flags": ["discount", AUCTION_FLAG]},
    {"key": "fixed", "flags": ["discount"]},
    {"key": "no-flags", "flags": []},
    {"key": "flags-missing"},                       # 舊回應：連 flags 都沒有
]


def test_switch_off_hides_nothing():
    """關著的時候一筆都不能少——**預設不過濾**是這個 repo 的第一條規則。"""
    out = run_js(ITEMS, hide_auction=False)
    assert out["hidden"] == [False] * len(ITEMS)


def test_switch_on_hides_exactly_the_auctions():
    out = run_js(ITEMS, hide_auction=True)
    assert out["hidden"] == [True, True, False, False, False]
    # 判準只有 live_auction 旗標；沒有 flags 欄位的舊列不得被誤殺
    assert out["isAuction"] == [True, True, False, False, False]


def test_conflict_is_reported_so_an_empty_list_is_explainable():
    """「🔨 只看競標」＋「🙈 隱藏競標」＝空清單。那是使用者自己選的合理結果，
    但畫面必須說得出為什麼空，否則跟壞掉沒兩樣（第五節：靜默失敗）。"""
    assert run_js(ITEMS, hide_auction=True, filter_mode="auction")["conflict"] is True
    assert run_js(ITEMS, hide_auction=False, filter_mode="auction")["conflict"] is False
    assert run_js(ITEMS, hide_auction=True, filter_mode="all")["conflict"] is False


def test_filter_state_is_wired_into_all_four_persistence_points():
    """localStorage 的四個接點少接一個，症狀是「開了關了但重整後失效」——
    使用者只會覺得這個開關時靈時不靈，不會有任何錯誤訊息。

    四個接點：saveFilters（存）／loadFilters（讀）／syncFilterUI（回寫 UI）／
    filter-reset（清除）。這裡用結構檢查釘住它們。
    """
    text = INDEX.read_text(encoding="utf-8")
    # 1. 按鈕存在，且走既有的可疊加開關模式（.quick[data-quick]）
    assert 'class="quick" data-quick="noauction"' in text
    # 2. 預設值宣告 + 3. loadFilters 還原 + 4. filter-reset 清除
    assert text.count("noauction:false") == 2, "宣告與 filter-reset 各要有一處"
    assert "noauction: !!s.quick.noauction" in text, "loadFilters 沒有還原這個開關"
    # saveFilters 存的是整個 quick 物件、syncFilterUI 依 data-quick 逐顆回寫，
    # 兩者都是泛用的——這裡確認那份泛用寫法還在（被改成逐欄列舉就會漏掉新開關）。
    assert "{filterMode, flagFilter, quick, pHide, pThreshold, sortMode}" in text
    assert 'b.classList.toggle("on", !!quick[b.dataset.quick])' in text
    # 5. 真的有被套用（rejectReason）＋ 有清除篩選的入口
    assert 'hiddenByAuctionFilter(it, quick.noauction)) return "競標中"' in text
    assert "|| quick.ceiling || quick.room || quick.noauction" in text


def test_bucket_ui_is_on_both_card_types():
    """競標卡漏掉分類按鈕的話，使用者最想分類的那一批（正在競標的貴卡）
    恰好分不了類——而畫面上看不出少了東西。"""
    text = INDEX.read_text(encoding="utf-8")
    assert text.count("${bucketActs(it)}") == 2, "一般卡與競標卡都要有分類按鈕"
    assert text.count("${bucketChip(it)}") == 2, "兩種卡片都要看得出已指派的分類"
    assert text.count('data-bucket="high_value"') == 1
    assert text.count('data-bucket="rare"') == 1
