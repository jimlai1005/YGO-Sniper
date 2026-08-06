"""待清 banner／確認框的筆數：**必須跟後端真的會清的那一批同源**。

後端 `clear_expired_signals` 自己重查整個分頁（`min_score=0`、不套任何前端
篩選），所以前端數 `shown`（篩選後、且競標視圖已把已結標的移出）就是拿兩把
不同的尺量同一件事——確認框說「將清除 2 筆」，實際改 6 列。而那個數字是
使用者按下去之前**唯一的反悔依據**（CLAUDE.md 第三節）。

作法沿用 tests/test_signal_filters.py：從 index.html 抽出標記區塊丟進 node
執行，斷言留在 pytest 這一側。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ygo_sniper.store import Store

INDEX = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
BEGIN = "/* ==== EXPIRY-BANNER-LOGIC:BEGIN"
END = "/* ==== EXPIRY-BANNER-LOGIC:END"

#: node 端的驅動器：讀 stdin 的 {state, items, shown}，把純函式的輸出印成 JSON。
#: **不含任何斷言**——判斷留在 pytest。
HARNESS = """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(
  expiryBanner(input.state, input.items, input.shown)
));
"""


def extract_block() -> str:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(BEGIN), text.index(END)
    block = text[start:end]
    # 抽出來的要真的是那段邏輯：標記被搬走而函式沒跟上時這裡必須紅。
    assert "function expiryBanner(" in block, "區塊裡找不到 expiryBanner"
    return block


def run_js(state: str, items: list[dict], shown: list[dict]) -> dict | None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - 開發機沒有 node 時
        pytest.skip("找不到 node，無法執行前端邏輯測試")
    proc = subprocess.run(
        [node, "-e", extract_block() + HARNESS],
        input=json.dumps({"state": state, "items": items, "shown": shown}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node 執行失敗：{proc.stderr}"
    return json.loads(proc.stdout)


def _item(key: str, kind: str, site: str = "buyee_paypay") -> dict:
    """`expiry` 直接來自後端（判定只有 expiry.py 一份），前端不自己算。"""
    return {"key": key, "site": site, "expiry": {"kind": kind, "confidence": "low"}}


#: 一個分頁的全部（後端 /api/signals 回的 items）：6 筆非 live、2 筆 live。
PAGE = [
    _item("a", "gone"), _item("b", "gone"), _item("c", "gone"),
    _item("d", "ended", site="buyee_yahoo"), _item("e", "ended", site="buyee_yahoo"),
    _item("f", "gone", site="ebay"),
    _item("g", "live"), _item("h", "live"),
]


def test_banner_counts_the_whole_page_not_just_what_is_shown():
    """使用者開著「🔨 只看競標」時，`shown` 只剩兩筆；後端照樣清六筆。

    這條紅掉＝確認框又在數畫面上的筆數，使用者會在不知情下多清四筆。
    """
    shown = [PAGE[0], PAGE[1]]
    out = run_js("watching", PAGE, shown)
    assert out["count"] == 6


def test_banner_says_how_many_the_filter_is_hiding():
    """差額要**講出來**，不能只把數字換掉了事——使用者得知道他要清的東西
    比畫面上看得到的多幾筆，否則只是換一個他無法核對的數字。"""
    out = run_js("watching", PAGE, [PAGE[0], PAGE[1]])
    assert out["hidden"] == 4


def test_no_filter_means_nothing_is_hidden():
    out = run_js("watching", PAGE, PAGE)
    assert out["count"] == 6
    assert out["hidden"] == 0


def test_sources_are_counted_over_the_whole_page_too():
    """來源明細與筆數必須同源，否則「6 筆（paypay 2）」這種對不起來的文案。"""
    out = run_js("watching", PAGE, [PAGE[0]])
    assert out["sources"] == "buyee_paypay 3、buyee_yahoo 2、ebay 1"


def test_nothing_expired_means_no_banner():
    out = run_js("watching", [PAGE[6], PAGE[7]], [PAGE[6], PAGE[7]])
    assert out is None


def test_banner_only_appears_on_clearable_states():
    """清除按鈕只在三個分頁有效（`Store.CLEARABLE_STATES`）——
    其他分頁連 banner 都不該出現，免得按下去拿 400。"""
    for state in ("new", "bought", "skipped", "expired", "all", "in_bundle"):
        assert run_js(state, PAGE, PAGE) is None
    for state in Store.CLEARABLE_STATES:
        assert run_js(state, PAGE, PAGE)["count"] == 6


def test_clearable_states_match_the_backend():
    """前端的清單是硬編的，後端改了要能被抓到（否則按鈕會安靜地回 400）。"""
    block = extract_block()
    for state in Store.CLEARABLE_STATES:
        assert f'"{state}"' in block, f"前端的 CLEARABLE_STATES 少了 {state}"


def test_missing_expiry_field_is_not_counted():
    """舊回應沒有 `expiry` 欄位時不能把它算成待清——那會清掉還在架上的東西。"""
    out = run_js("watching", [{"key": "old", "site": "ebay"}], [])
    assert out is None


# ---------------------------------------------------------------------------
# 確認框的其餘兩條：跨分頁存活、差額要出現在文案裡
# ---------------------------------------------------------------------------
def test_confirm_dialog_does_not_survive_a_tab_switch():
    """確認框開著時切分頁 → 再按「清除」，清掉的是**另一個分頁**。

    `clearExpired()` 是在按下確認的當下才讀狀態，而分頁切換的 handler
    以前不關這個框。配合「已出價」那一格（清錯了自動還原永遠救不回來），
    這正好落在最不該誤清的地方。
    """
    text = INDEX.read_text(encoding="utf-8")
    handler = text[text.index('document.querySelectorAll(".tab")'):]
    handler = handler[: handler.index("document.getElementById(\"xp-clear-btn\")")]
    assert "xp-confirm" in handler, "分頁切換沒有關掉確認框"


def test_clear_uses_the_state_the_dialog_was_opened_with():
    """第二道防線：就算框沒被關掉，送出的也必須是**框上描述的那個分頁**，
    不是按下去當下的 `currentState`。"""
    text = INDEX.read_text(encoding="utf-8")
    body = text[text.index("async function clearExpired()"):]
    body = body[: body.index("\n}")]
    assert "currentState" not in body, "clearExpired 仍在讀當下的 currentState"
    assert "dataset.state" in body, "clearExpired 沒有用確認框記下來的分頁"


def test_auction_cards_show_the_expiry_badges_too():
    """競標視圖以前完全看不到「已離場」徽章（`auctionCard` 內零命中），
    而清除按鈕照樣會清掉那些標的——**看不到就不該被清掉**。
    兩種卡片共用同一份 `expiryBadges`，避免兩份定義各自漂移。
    """
    text = INDEX.read_text(encoding="utf-8")
    start = text.index("function auctionCard(it)")
    body = text[start : text.index("\nfunction ", start + 10)]
    assert "expiryBadges(it)" in body, "競標卡沒有離場／已復活徽章"


def test_confirm_text_mentions_the_hidden_rows():
    """被篩選隱藏的筆數要出現在確認框裡（不是只出現在 banner）。"""
    text = INDEX.read_text(encoding="utf-8")
    body = text[text.index("function askClearExpired()"):]
    body = body[: body.index("\n}")]
    assert "hidden" in body, "確認框沒有講出被篩選隱藏的筆數"
