"""競標視圖的可行動性梯隊（前端 JS，用 node 真的跑一遍）。

為什麼要這樣測：排序邏輯住在 `web/static/index.html` 裡，而「排錯順序」的症狀
是**你錯過了唯一能出手的那一筆**——畫面不會壞、不會有錯誤訊息，只是最該看的
東西沉在第 40 筆。這種 bug 眼睛看不出來，所以那段 JS 被標成一個可抽出的區塊
（`AUCTION-VIEW-LOGIC:BEGIN/END`，純函式、不碰 DOM），這裡把它抽出來丟進 node
執行，斷言留在 pytest 這一側。

門檻與文案不在測試裡另外拍一組：一律從 `bidding.auction_view_config()` 拿，
前端拿到的也是同一份（web/app.py 的 /api/signals）。測試自己寫一個 24 小時的話，
後端改了門檻測試還是綠的——那就等於沒有測到（工程原則 1）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ygo_sniper.bidding import (
    ACTIONABLE_WINDOW_HOURS,
    PRICE_DYNAMICS_HOURS,
    PRICE_DYNAMICS_NOTE,
    auction_view_config,
)

INDEX = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
BEGIN = "/* ==== AUCTION-VIEW-LOGIC:BEGIN"
END = "/* ==== AUCTION-VIEW-LOGIC:END"

NOW = 1_800_000_000_000  # 固定的「現在」（epoch ms）：測試不依賴真實時鐘
HOUR = 3_600_000

#: node 端的驅動器：讀 stdin 的 {items, now, cfg}，把三個純函式的輸出印成 JSON。
#: **不含任何斷言**——判斷留在 pytest，這裡只負責執行前端那份程式碼。
HARNESS = """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const {items, now, cfg} = input;
console.log(JSON.stringify({
  tiers: items.map(it => auctionTier(it, now, cfg)),
  order: sortAuctions(items, now, cfg).map(it => it.key),
  notes: items.map(it => priceDynamicsNote(it, now, cfg)),
  rooms: items.map(it => auctionRoom(it)),
}));
"""


def extract_block() -> str:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(BEGIN), text.index(END)
    block = text[start:end]
    # 抽出來的東西要真的是那段邏輯，不是一個空殼——標記被搬走而函式沒跟上時，
    # 這裡必須紅，而不是讓 node 跑一段沒有函式的程式碼再報 ReferenceError。
    for fn in ("auctionTier", "sortAuctions", "priceDynamicsNote", "auctionRoom"):
        assert f"function {fn}(" in block, f"區塊裡找不到 {fn}"
    return block


def run_js(items: list[dict], *, cfg: dict | None) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - 開發機沒有 node 時
        pytest.skip("找不到 node，無法執行前端邏輯測試")
    proc = subprocess.run(
        [node, "-e", extract_block() + HARNESS],
        input=json.dumps({"items": items, "now": NOW, "cfg": cfg}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node 執行失敗：{proc.stderr}"
    return json.loads(proc.stdout)


def item(key: str, *, ends_in_h: float | None, cur: float | None, ceiling: float | None):
    """一筆競標的最小 payload（欄位名與 /api/signals 回的形狀一致）。"""
    end = None if ends_in_h is None else _iso(NOW + int(ends_in_h * HOUR))
    bid = {"ok": ceiling is not None, "max_bid_jpy": ceiling}
    return {
        "key": key,
        "price_native": cur,
        "flags": ["live_auction"],
        "payload": {"listing": {"end_time": end}, "bid": bid},
    }


def ebay_item(key: str, *, ends_in_h: float | None, cur: float | None,
              ceiling_twd: float | None):
    """一筆 **eBay** 競標：上限在 `max_bid_listing`（與現價同為台幣）、
    `max_bid_jpy` 是 None——前後端都必須認得這個形狀，不能只看日圓欄位。"""
    end = None if ends_in_h is None else _iso(NOW + int(ends_in_h * HOUR))
    bid = {
        "ok": ceiling_twd is not None,
        "max_bid_jpy": None,
        "max_bid_listing": ceiling_twd,
        "listing_currency": "TWD",
        "max_bid_native": None if ceiling_twd is None else round(ceiling_twd / 32.3, 2),
        "native_currency": "USD",
    }
    return {
        "key": key,
        "price_native": cur,
        "site": "ebay",
        "flags": ["live_auction"],
        "payload": {"listing": {"end_time": end}, "bid": bid},
    }


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


CFG = auction_view_config()


# ---------------------------------------------------------------------------
# 1. 梯隊判定
# ---------------------------------------------------------------------------
def test_tiers_and_ordering():
    """第一梯隊＝有空間＋24h 內結標，必須排在最前面（其餘按梯隊、再按時間）。"""
    items = [
        item("far_room", ends_in_h=100, cur=1000, ceiling=5000),      # 2：有空間但還早
        item("no_ceiling", ends_in_h=1, cur=1000, ceiling=None),      # 3：沒有上限
        item("over", ends_in_h=2, cur=9000, ceiling=5000),            # 3：已越過上限
        item("urgent", ends_in_h=3, cur=1000, ceiling=5000),          # 1：現在就該看
        item("unknown_end", ends_in_h=None, cur=1000, ceiling=5000),  # 2：不知何時結標
        item("edge_in", ends_in_h=ACTIONABLE_WINDOW_HOURS - 0.5, cur=100, ceiling=900),
        item("edge_out", ends_in_h=ACTIONABLE_WINDOW_HOURS + 0.5, cur=100, ceiling=900),
    ]
    out = run_js(items, cfg=CFG)

    assert out["tiers"] == [2, 3, 3, 1, 2, 1, 2]
    # 第一梯隊在最前、內部照結標時間；第二梯隊次之（未知結標時間沉底）；第三梯隊最後
    assert out["order"] == [
        "urgent", "edge_in",                       # 梯隊 1（3h < 23.5h）
        "edge_out", "far_room", "unknown_end",     # 梯隊 2（24.5h < 100h < ∞）
        "no_ceiling", "over",                      # 梯隊 3（1h < 2h）
    ]


def test_room_is_never_guessed():
    """沒有上限／沒有現價一律 None，不退化成 0——0 會被讀成「剛好沒空間」。"""
    out = run_js(
        [
            item("no_ceiling", ends_in_h=1, cur=1000, ceiling=None),
            item("no_price", ends_in_h=1, cur=None, ceiling=5000),
            item("ok", ends_in_h=1, cur=1000, ceiling=5000),
        ],
        cfg=CFG,
    )
    assert out["rooms"] == [None, None, 4000]


def test_no_config_means_no_tiers_and_no_note():
    """後端沒給門檻時**不猜**：全部落到第三梯隊、不標註（前端不自己拍 24 小時）。"""
    out = run_js(
        [
            item("urgent", ends_in_h=3, cur=1000, ceiling=5000),
            item("far", ends_in_h=100, cur=1000, ceiling=5000),
        ],
        cfg=None,
    )
    assert out["tiers"] == [3, 3]
    assert out["notes"] == ["", ""]


# ---------------------------------------------------------------------------
# 2. >48h 的誠實標註
# ---------------------------------------------------------------------------
def test_price_dynamics_note_only_for_far_out_auctions():
    """結標 >48h 才標「現價會漲」——近的不標（那時候的現價是有意義的）。"""
    out = run_js(
        [
            item("far", ends_in_h=PRICE_DYNAMICS_HOURS + 1, cur=1, ceiling=5840),
            item("edge", ends_in_h=PRICE_DYNAMICS_HOURS - 1, cur=1, ceiling=5840),
            item("soon", ends_in_h=2, cur=1, ceiling=5840),
            item("unknown", ends_in_h=None, cur=1, ceiling=5840),
        ],
        cfg=CFG,
    )
    assert out["notes"][0] == PRICE_DYNAMICS_NOTE
    assert out["notes"][1:] == ["", "", ""]
    # 標註本身要真的講出那件事，不是一句「請注意」
    assert "現價會漲" in PRICE_DYNAMICS_NOTE


def test_far_out_bargain_looking_listing_is_annotated():
    """使用者踩過的坑：¥1 → 上限 ¥5,840 看起來像撿到寶，其實是競價還沒開始。"""
    out = run_js([item("bait", ends_in_h=120, cur=1, ceiling=5840)], cfg=CFG)

    assert out["tiers"] == [2], "還有 5 天的標的不可以排進「現在就該看」"
    assert out["notes"][0] == PRICE_DYNAMICS_NOTE


# ---------------------------------------------------------------------------
# 3. 前端拿到的門檻與後端是同一份
# ---------------------------------------------------------------------------
def test_backend_and_frontend_agree_on_every_tier():
    """後端 `bidding.auction_tier`（推播用）與前端 `auctionTier()`（畫面用）
    對同一批標的必須給出**一模一樣**的梯隊。

    兩份實作是刻意的（一個要在 python 決定要不要發訊息，一個要在瀏覽器排序），
    但它們是同一個判定：Telegram 說「現在就該看」的那筆，打開 dashboard 必須
    也排在第一梯隊。分岔了就是使用者在兩個地方看到兩套事實（工程原則 1）。
    """
    from datetime import UTC, datetime

    from ygo_sniper.bidding import auction_tier

    items = [
        item("urgent", ends_in_h=3, cur=1000, ceiling=5000),
        item("far_room", ends_in_h=100, cur=1000, ceiling=5000),
        item("no_ceiling", ends_in_h=1, cur=1000, ceiling=None),
        item("over", ends_in_h=2, cur=9000, ceiling=5000),
        item("exactly_at_ceiling", ends_in_h=2, cur=5000, ceiling=5000),
        item("unknown_end", ends_in_h=None, cur=1000, ceiling=5000),
        item("ended", ends_in_h=-2, cur=1000, ceiling=5000),
        item("no_price", ends_in_h=1, cur=None, ceiling=5000),
        item("edge_in", ends_in_h=ACTIONABLE_WINDOW_HOURS - 0.5, cur=100, ceiling=900),
        item("edge_out", ends_in_h=ACTIONABLE_WINDOW_HOURS + 0.5, cur=100, ceiling=900),
        # eBay：上限在 max_bid_listing（台幣，與現價同基準）、max_bid_jpy=None。
        # 只認日圓欄位的實作會把這兩筆全部判成「沒有上限」——那正是要抓的分岔。
        ebay_item("ebay_urgent", ends_in_h=3, cur=700, ceiling_twd=1500),
        ebay_item("ebay_over", ends_in_h=3, cur=2000, ceiling_twd=1500),
        ebay_item("ebay_no_ceiling", ends_in_h=3, cur=700, ceiling_twd=None),
    ]
    js = run_js(items, cfg=CFG)["tiers"]
    now = datetime.fromtimestamp(NOW / 1000, UTC)
    py = [
        auction_tier(
            it["payload"]["bid"], it["price_native"],
            it["payload"]["listing"]["end_time"], now,
            window_hours=CFG["actionable_window_hours"],
        )
        for it in items
    ]
    assert py == js, dict(zip([i["key"] for i in items], zip(py, js, strict=True), strict=True))


def test_backend_serves_the_thresholds_the_frontend_reads():
    cfg = auction_view_config()
    assert cfg["actionable_window_hours"] == ACTIONABLE_WINDOW_HOURS
    assert cfg["price_dynamics_hours"] == PRICE_DYNAMICS_HOURS
    assert [t["tier"] for t in cfg["tiers"]] == [1, 2, 3]
    # 前端只上色、不寫文案：每一梯隊都要有可顯示的標籤
    assert all(t["label"] and t["hint"] for t in cfg["tiers"])
    # index.html 不可以自己再寫一份門檻數字
    text = INDEX.read_text(encoding="utf-8")
    block = extract_block()
    assert "actionable_window_hours" in block and "price_dynamics_hours" in block
    assert "res.auction_view" in text, "前端沒有從 /api/signals 讀 auction_view"
