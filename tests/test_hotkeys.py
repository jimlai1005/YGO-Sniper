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
