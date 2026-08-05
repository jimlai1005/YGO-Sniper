"""解析健康判定的資料型別。

為什麼「0 筆」必須分類：對每天只跑一次的掃描器來說，「解析壞了」與
「今天真的沒貨」的外顯行為一模一樣——都是安靜的一天。如果不區分，
被 WAF 擋三週或對方改版三週，你只會看到三週的「今天沒推薦」，
以為市場冷清，實際上是工具瞎了。所以 source 層必須在產出 0 筆時
說清楚**為什麼**是 0 筆，讓告警層（alerts.py）能對「壞了」發聲、
對「沒貨」保持安靜。

判定發生在 source 層（只有 parser 自己看得到頁面地標與命中數），
聚合與去重發送在 alerts.py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..domain import Listing


class ParseHealth(str, Enum):
    OK = "ok"
    EMPTY_CONFIRMED = "empty"        # 頁面明確說沒有結果 → 不告警
    PARSER_BROKEN = "parser_broken"  # 頁面正常但解析 0 筆 → 告警
    BLOCKED = "blocked"              # WAF / 無 token → 告警
    FETCH_FAILED = "fetch_failed"    # 連線層 transient → 連續 2 次才告警


@dataclass(slots=True)
class SearchResult:
    """一次 (source, query) 搜尋的完整結果：標的清單＋健康判定＋排錯線索。

    listings 與 health 是主角；其餘欄位是告警與排錯的歸因材料——
    告警訊息裡有 url 與 html_bytes，人才能一眼判斷是被擋還是改版。
    """

    source: str                       # 發現管道名（如 "yahoo_direct"）
    site: str                         # 購買路徑（Site.value，如 "buyee_yahoo"）
    query: str
    listings: list[Listing] = field(default_factory=list)
    health: ParseHealth = ParseHealth.OK
    pages_fetched: int = 0
    url: str = ""                     # 第一頁的搜尋 URL，方便人工重現
    html_bytes: int = 0               # 回應大小；被擋的頁通常異常小
    detail: str = ""                  # 判定依據的一句話（例如命中數 vs 解析數）
    #: **解析器解出幾個商品**——商業篩選之前的數量。
    #:
    #: 為什麼要跟 `len(listings)` 分開存（2026-08-01 事故）：canary 想問的是
    #: 「解析器還活著嗎」，但它原本數的是 `len(listings)`，而 listings 是
    #: **商業篩選後**的結果（Yahoo 的 `include_live_auctions=false` 會丟掉所有
    #: 純競標標的）。改成新着排序後，「遊戯王」最新上架的 50 筆大多是剛開的
    #: ¥1 起標、沒有即決価格，於是同一個查詢連續三次得到 22 / 18 / 1 筆——
    #: 波動 22 倍，而解析器每次都健康地解出 50 個商品區塊。結果是
    #: `yahoo_direct:parser_broken` 累積了 12 次假警報。
    #:
    #: 教訓：**量錯東西的指標比沒有指標更糟**——它會把「市場今天長這樣」
    #: 誤報成「工具壞了」，而假警報吵久了，真的壞掉那次你會直接忽略。
    #: 所以健康判定一律看 `parsed_count`（管道活著嗎），商業判斷才看
    #: `listings`（有沒有我買得下去的東西）。
    parsed_count: int = 0
    #: **這個查詢的存量已經翻完了嗎**（最後一頁不滿一頁）。
    #:
    #: 預設 `False` 的語意是「不知道／沒翻完」，不是「還有很多」——只有真的
    #: 看到「不滿一頁」的來源才會設 True（目前只有 `yahoo_closed`，歷史回填
    #: 靠它決定「這個關鍵字不必再深挖了」）。分不出「翻完了」與「被錯誤中斷」
    #: 的話，回填會把一次連線失敗記成「這個查詢沒東西了」而永遠不再回來看
    #: ——那是安靜地少收資料，最難發現的那一種。
    archive_exhausted: bool = False
