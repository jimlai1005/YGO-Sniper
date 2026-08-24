"""略過的樂觀更新：`applyLocalState` 決定卡片留下還是本地移除。

判定只有一份（純函式），前端 setState 與這裡的 node 實跑共用同一段標記
區塊——不是各自維護一份邏輯。作法沿用 tests/test_expiry_banner.py：從
index.html 抽出標記區塊丟進 node 執行，斷言留在 pytest 這一側。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
BEGIN = "/* ==== LOCAL-STATE-LOGIC:BEGIN"
END = "/* ==== LOCAL-STATE-LOGIC:END"

#: node 端的驅動器：讀 stdin 的 {items, key, newState, currentState}，
#: 把純函式的輸出印成 JSON。**不含任何斷言**——判斷留在 pytest。
HARNESS = """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(
  applyLocalState(input.items, input.key, input.newState, input.currentState)
));
"""


def extract_block() -> str:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(BEGIN), text.index(END)
    block = text[start:end]
    # 抽出來的要真的是那段邏輯：標記被搬走而函式沒跟上時這裡必須紅。
    assert "function applyLocalState(" in block, "區塊裡找不到 applyLocalState"
    return block


def run_js(items: list[dict], key: str, new_state: str, current_state: str) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - 開發機沒有 node 時
        pytest.skip("找不到 node，無法執行前端邏輯測試")
    proc = subprocess.run(
        [node, "-e", extract_block() + HARNESS],
        input=json.dumps(
            {"items": items, "key": key, "newState": new_state, "currentState": current_state}
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node 執行失敗：{proc.stderr}"
    return json.loads(proc.stdout)


def test_skip_removes_item_from_state_tab():
    out = run_js(
        items=[{"key": "a", "state": "new"}, {"key": "b", "state": "new"}],
        key="a",
        new_state="skipped",
        current_state="new",
    )
    assert out["removed"] is True
    assert [i["key"] for i in out["items"]] == ["b"]


def test_all_tab_keeps_item_but_updates_state():
    out = run_js(
        items=[{"key": "a", "state": "new"}],
        key="a",
        new_state="skipped",
        current_state="all",
    )
    assert out["removed"] is False
    assert out["items"][0]["state"] == "skipped"


def test_same_state_stays():
    out = run_js(
        items=[{"key": "a", "state": "watching"}],
        key="a",
        new_state="watching",
        current_state="watching",
    )
    assert out["removed"] is False


def test_unknown_key_is_noop():
    out = run_js(
        items=[{"key": "a", "state": "new"}],
        key="ghost",
        new_state="skipped",
        current_state="new",
    )
    assert out["removed"] is False and len(out["items"]) == 1
