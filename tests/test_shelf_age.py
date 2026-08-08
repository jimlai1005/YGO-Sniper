"""「在架 ≥N 天」的純函式：N = floor((now − obs_first_seen)/一天)，N ≥ 1 才顯示。

時間**全部在前端用同一個時鐘算**（後端只給 UTC ISO 字串）——後端另算天數
就是兩個時鐘源，時區偏移下會與畫面上其他倒數對不上（CLAUDE.md 第三節）。
「≥」是誠實標示：first_seen 是首次**觀測**（觀測自 2026-08-01 開始），
實際上架時間只會更早，所以它是下界。

作法沿用 tests/test_expiry_banner.py：從 index.html 抽出標記區塊丟進 node
執行，斷言留在 pytest 這一側。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
BEGIN = "/* ==== SHELF-AGE-LOGIC:BEGIN"
END = "/* ==== SHELF-AGE-LOGIC:END"

#: node 端的驅動器：讀 stdin 的 {item, now_ms}，印出 shelfAge 的 HTML 字串。
#: **不含任何斷言**——判斷留在 pytest。
HARNESS = """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(shelfAge(input.item, input.now_ms)));
"""

#: 固定的 now：2026-08-08T00:00:00Z（測試不得依賴真實時鐘）。
#: 錨定驗算：python -c "from datetime import datetime,UTC;
#:   print(int(datetime(2026,8,8,tzinfo=UTC).timestamp())*1000)" → 1786147200000
NOW_MS = 1786147200000


def extract_block() -> str:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(BEGIN), text.index(END)
    block = text[start:end]
    assert "function shelfAge(" in block, "區塊裡找不到 shelfAge"
    return block


def run_js(item: dict, now_ms: int = NOW_MS) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - 開發機沒有 node 時
        pytest.skip("找不到 node，無法執行前端邏輯測試")
    proc = subprocess.run(
        [node, "-e", extract_block() + HARNESS],
        input=json.dumps({"item": item, "now_ms": now_ms}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node 執行失敗：{proc.stderr}"
    return json.loads(proc.stdout)


def test_seven_full_days_shows_at_least_seven():
    out = run_js({"obs_first_seen": "2026-08-01T00:00:00+00:00"})
    assert "在架 ≥7 天" in out


def test_partial_day_floors_down():
    """6 天 23 小時 → ≥6 天：floor 是刻意的，「≥」的方向不能因進位變成高估。"""
    out = run_js({"obs_first_seen": "2026-08-01T01:00:00+00:00"})
    assert "在架 ≥6 天" in out


def test_less_than_one_day_shows_nothing():
    assert run_js({"obs_first_seen": "2026-08-07T12:00:00+00:00"}) == ""


def test_missing_first_seen_shows_nothing():
    """沒有觀測列（obs_first_seen 是 null）→ 不顯示，**不猜**。"""
    assert run_js({"obs_first_seen": None}) == ""
    assert run_js({}) == ""


def test_unparsable_first_seen_shows_nothing():
    assert run_js({"obs_first_seen": "not-a-date"}) == ""


def test_title_declares_the_lower_bound_semantics():
    """title 要把「下界」講清楚——這個數字的極限必須標註在它自己身上。"""
    out = run_js({"obs_first_seen": "2026-08-01T00:00:00+00:00"})
    assert "自首次觀測起算的下界" in out
    assert "2026-08-01" in out
    assert "實際上架時間只會更早" in out


def test_style_is_muted_not_a_badge():
    """低調小字（.shelf-age），不是徽章（.xp）——不能搶過既有徽章。"""
    out = run_js({"obs_first_seen": "2026-08-01T00:00:00+00:00"})
    assert 'class="shelf-age"' in out
    assert "xp" not in out
