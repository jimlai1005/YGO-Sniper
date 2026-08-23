"""Telegram 推播。

推播的目的不是給你完整資訊 —— 那是 dashboard 的工作。
推播只回答一件事：「現在值不值得打開 dashboard」。
所以每則訊息刻意壓短，重點是金額、折價、旗標，加兩個連結。
"""

from __future__ import annotations

import json
import textwrap
import time

import httpx

from .alerts import HEALTH_LABEL
from .bidding import EVIDENCE_TIERS, evidence_label, evidence_tier
from .config import Config

#: Bot API 的端點模板。**method 是參數**：同一組 token／節流／429 處理要同時
#: 服務 `sendMessage` 與 `sendPhoto`，兩支各寫一條 URL 常數就會有兩套失敗處理
#: （工程原則 5：所有外呼走同一個 resilience boundary）。
_API = "https://api.telegram.org/bot{token}/{method}"

#: Telegram 的兩個長度上限（2026-08-04 官方文件）。差了 4 倍，這正是
#: 「附圖」不能無腦套用在每一則訊息上的原因：caption 只有 1024 字元。
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

#: 模組層的睡眠函式。**故意不直接叫 `time.sleep`**：測試要能換掉它而不必去
#: monkeypatch 整個 stdlib 的 time 模組（那會影響同一個 session 裡的其他測試）。
_sleep = time.sleep

#: 兩則之間的預設間隔（秒）。Telegram 對同一個 chat 約 20 則/分鐘，3 秒剛好貼齊。
DEFAULT_SEND_INTERVAL_SECONDS = 3.0

#: 被 429 擋下時，最多依 `retry_after` 等這麼久再重送一次。
#: 超過就放棄（回 False → 不落已通知帳 → 下一輪重排隊），不要讓一則訊息
#: 把整輪掃描卡住幾分鐘。
MAX_RETRY_AFTER_SECONDS = 60.0

_FLAG_ICON = {
    "free_card": "🔥 白撿",
    "discount": "📉 折價",
    "shipping_kills_it": "🚚 運費吃掉（比行情）",
    "high_overhead": "🚚 運費佔比超標",
    "needs_shipping_ask": "❓ 需問寄送",
    "needs_bundle_ask": "💬 可問合併",
    "offer_chance": "🎯 可丟 offer",
    "thin_comps": "⚠️ 行情樣本少",
    "suspicious_cheap": "🚨 便宜得可疑",
    "live_auction": "🔨 競標中",
    "bid_worth": "✅ 低於出價上限",
    "bid_no_ceiling": "🚫 樣本不足·無上限",
    "us_ship_option": "🇺🇸 可寄美國地址",
}


def _interval_setting(notify: dict | None) -> float:
    """`notify.send_interval_seconds`。非法值印警告並退回預設（不靜默）：
    一個打錯的間隔會讓推播整批吃 429 而使用者只看到「今天沒收到通知」。"""
    raw = (notify or {}).get("send_interval_seconds", DEFAULT_SEND_INTERVAL_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"[warn] notify.send_interval_seconds={raw!r} 不是數字，"
              f"改用 {DEFAULT_SEND_INTERVAL_SECONDS}")
        return DEFAULT_SEND_INTERVAL_SECONDS
    if value < 0:
        print(f"[warn] notify.send_interval_seconds={value} 是負數，"
              f"改用 {DEFAULT_SEND_INTERVAL_SECONDS}")
        return DEFAULT_SEND_INTERVAL_SECONDS
    return value


def _retry_after(response: httpx.Response) -> float | None:
    """從 429 回應取 `parameters.retry_after`（秒）。取不到或超過上限回 None。"""
    try:
        params = (response.json() or {}).get("parameters") or {}
        wait = float(params.get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        return None
    if wait < 0 or wait > MAX_RETRY_AFTER_SECONDS:
        return None
    return wait


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _comps_range(row: dict) -> tuple[float | None, float | None]:
    """從 payload 取 P25/P75 當「合理價格區間」。

    p25_twd / p75_twd 沒有獨立欄位，但 payload 存的是完整的 sig.to_dict()，
    CompStats 整包都在裡面。舊資料沒有這些 key 一律回 None（照常降級顯示）。
    """
    try:
        comps = (json.loads(row.get("payload") or "{}") or {}).get("comps") or {}
    except (TypeError, ValueError):
        return None, None
    return comps.get("p25_twd"), comps.get("p75_twd")


def _valuation_lines(val, row: dict) -> list[str]:
    """估價模型的兩行：公允價 + 區間／機率。

    兩行的分工是刻意的：第一行是「多少錢」，第二行是「我有多不確定」。
    只給點估計會讓 L0（全域先驗，等於什麼都沒說）看起來跟 L1（同卡同分數
    真的有成交）一樣可信——所以層級與有效樣本數**必須跟數字同一行**。
    """
    from .valuation import venue_label

    if val is None or val.fair_twd is None:
        return ["公允價：無足夠樣本（下面的判斷只根據到手成本，不是行情比價）"]

    # 平台一定要跟數字同一行：Yahoo 與 Mercari 的同款成交價差 2 倍以上，
    # 一個沒標平台的公允價會被拿去跟另一個市場的價格比。
    if not val.venue_adjusted:
        venue_tail = " ｜ ⚠️ 混合平台水準（未校正）"
    else:
        venue_tail = f" ｜ {venue_label(val.venue)} 水準"
        if val.venue_is_estimated is False:
            venue_tail += "（平台係數用先驗）"
    lines = [
        f"公允價 <b>NT${val.fair_twd:,.0f}</b>"
        f"（{val.level} {val.level_label}，有效 n={val.n_effective}）{venue_tail}"
    ]
    if val.has_interval:
        tail = ""
        if val.p_worth_buying is not None:
            tail = f" ｜ P(值得買) {val.p_worth_buying:.0%}"
        # 區間寬度是**哪一群**校準出來的必須跟區間同一行：分群之後每一筆的寬度
        # 不再相同，只給區間就分不出「這是 128 筆同層樣本校準的」還是
        # 「這一群樣本不足、退到全域分位數的」。退化尤其不准靜默。
        if val.calibration_group:
            tail += f" ｜ 校準群 {val.calibration_group}／{val.calibration_group_n} 筆"
            if val.calibration_degraded:
                tail += "（⚠️ 退化：未經該群校準）"
        lines.append(
            f"{val.confidence:.0%} 區間 NT${val.lo_twd:,.0f}–{val.hi_twd:,.0f}{tail}"
        )
    else:
        lines.append("區間：樣本不足以校準（不給沒校準過的數字）")
    return lines


def format_signal(row: dict, dashboard_url: str, valuation=None) -> str:
    """一則「標的摘要」訊息。`valuation` 是 valuation.Estimate；沒傳就顯示 comps 中位數。

    ⚠️ 這**不再是**推播路徑上的格式：主動推播已改成兩條窄規則
    （`notify_rules` ＋ `format_auction_urgent` / `format_high_p`），
    舊的「今日撿漏 N 筆」全量推播連同它的標頭一起拿掉了。
    這支留著是估價層釘格式用的純函式（tests/test_valuation.py 用它驗
    「公允價／區間／平台水準」這幾行的顯示規則）。

    刻意做成參數而不是在這裡自己算：format_signal 是純格式化函式，
    測試要能不碰資料庫就釘住格式。
    """
    flags = json.loads(row.get("flags") or "[]")
    icons = " ".join(_FLAG_ICON.get(f, f) for f in flags)

    title = textwrap.shorten(row["title"], width=64, placeholder="…")
    lines = [
        f"<b>{_esc(title)}</b>",
        f"到手 <b>NT${row['landed_twd']:,.0f}</b>"
        f"（{row['price_native']:,.0f} {row['currency']} via {row['route']}）",
    ]

    if valuation is not None:
        lines.extend(_valuation_lines(valuation, row))
    elif row.get("comps_median"):
        d = row.get("discount_pct")
        pct = f"，低 {d:.0%}" if d else ""
        lines.append(f"行情 NT${row['comps_median']:,.0f}（n={row['comps_n']}）{pct}")
        # 中位數只是一個點，看不出這張卡的行情有多散。P25–P75 才回答
        # 「多少錢算合理」——到手成本低於 P25 才是真的便宜，落在區間內只是正常價。
        p25, p75 = _comps_range(row)
        if p25 and p75:
            lines.append(f"合理區間 NT${p25:,.0f}–{p75:,.0f}（P25–P75）")
    else:
        lines.append("行情：無足夠樣本（下面的判斷只根據到手成本，不是行情比價）")

    if icons:
        lines.append(icons)
    lines.append(f"分數 {row['score']:.0f}")
    lines.append(f'<a href="{row["url"]}">看標的</a> ｜ <a href="{dashboard_url}">開 dashboard</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 兩條規則的訊息。**每一個數字都會被拿去下真錢的單**，所以：
#   - 數字一律來自掃描當下存進 payload 的那一份（與 dashboard 同源），
#     推播不自己重算上限——重算就是第二個定義（工程原則 1）。
#   - 缺值的欄位在 notify_rules 那一層就已經讓整則不送，這裡不做 fallback、
#     不填「—」：能走到格式化的 Match，欄位一定是齊的。
# ---------------------------------------------------------------------------
_TAIPEI = "Asia/Taipei"


def _local_end_time(end: str | None) -> str:
    """結標時間的絕對值（台北時間）。倒數會過期，絕對時間不會。

    時區資料庫缺席時退回 UTC 並**明講是 UTC**——標錯時區等於錯過整批結標。
    """
    from datetime import UTC, datetime

    if not end:
        return ""
    try:
        dt = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo(_TAIPEI)).strftime("%m-%d %H:%M") + " 台北時間"
    except Exception:  # noqa: BLE001 - 見 docstring
        return dt.astimezone(UTC).strftime("%m-%d %H:%M") + " UTC"


def format_countdown(hours: float) -> str:
    """倒數：`3 小時 12 分`。小於一小時只給分鐘（那時候分鐘才是重點）。"""
    total_min = max(0, int(round(hours * 60)))
    h, m = divmod(total_min, 60)
    return f"{h} 小時 {m} 分" if h else f"{m} 分"


#: 幣別代碼 → 顯示符號。訊息裡的每個金額都要帶得出幣別——「NT$ 還是 US$」
#: 寫錯一次就是差 30 倍。原幣（native）可能是 GBP/EUR 等 Currency enum 之外的
#: 幣別（listing 幣別已被 eBay 換成台幣），所以這張表比 enum 寬，查不到就印代碼。
_CCY_SYMBOL = {"JPY": "¥", "TWD": "NT$", "USD": "US$", "GBP": "£", "EUR": "€"}


def _money_str(value: float, ccy: str | None, *, decimals: int = 0) -> str:
    code = (ccy or "").upper()
    sym = _CCY_SYMBOL.get(code)
    return f"{sym}{value:,.{decimals}f}" if sym else f"{code} {value:,.{decimals}f}"


def format_auction_urgent(match, dashboard_url: str) -> str:
    """規則 1：競標急件。回答「現在要不要出手、出到多少、還剩多久」。

    幣別跟著標的走：Yahoo 全日圓；eBay 的現價／空間是台幣（eBay 換算顯示），
    **上限以原幣為主**（那才是要填進 eBay 出價欄的數字）＋台幣等值。
    代理出價那一行也分兩份查證：Buyee（PROXY_BID_FINDING）與 eBay 原生
    automatic bidding（EBAY_PROXY_BID_FINDING）——講錯平台等於教人去按一個
    不存在的按鈕。
    """
    row, bid = match.row, match.bid
    title = textwrap.shorten(row["title"], width=64, placeholder="…")
    end_at = _local_end_time((_listing_of(row) or {}).get("end_time"))
    pct = match.room / match.max_bid if match.max_bid else None
    ccy = match.currency or "JPY"
    is_ebay = match.native_currency is not None and match.max_bid_native is not None

    if is_ebay:
        cur_tail = (
            f"（{_money_str(match.current_bid_native, match.native_currency, decimals=2)}）"
            if match.current_bid_native is not None else ""
        )
        price_line = (
            f"目前出價 <b>{_money_str(match.current_bid, ccy)}</b>{cur_tail} ｜ "
            f"出價欄填 <b>{_money_str(match.max_bid_native, match.native_currency, decimals=2)}</b>"
            f"（≈{_money_str(match.max_bid, ccy)}） ｜ "
            f"空間 <b>{_money_str(match.room, ccy)}</b>"
            + (f"（{pct:.0%}）" if pct is not None else "")
        )
        proxy_line = (
            "🔁 eBay 原生自動出價（automatic bidding）：出價欄填上限後 eBay 會"
            "自動替你跟價到上限為止，<b>設好上限就可以離開</b>，不必守著結標時間"
            "（官方說明 2026-08-03 查證）。"
        )
    else:
        price_line = (
            f"目前出價 <b>{_money_str(match.current_bid, ccy)}</b> ｜ "
            f"你的上限 <b>{_money_str(match.max_bid, ccy)}</b> ｜ "
            f"空間 <b>{_money_str(match.room, ccy)}</b>"
            + (f"（{pct:.0%}）" if pct is not None else "")
        )
        proxy_line = (
            "🔁 Buyee 支援代理出價：填上限後系統會自動幫你跟價到那個上限為止，"
            "<b>設好上限就可以離開</b>，不必守著結標時間。"
        )
    lines = [
        f"⚡ <b>{_esc(title)}</b>",
        f"結標倒數 <b>{format_countdown(match.hours_left)}</b>"
        + (f"（{end_at}）" if end_at else ""),
        price_line,
        f"以上限成交的到手成本 NT${float(bid['landed_at_ceiling_twd']):,.0f}",
        f"{_esc(str(bid['evidence_tier_label']))}｜{_esc(str(bid['evidence_label']))}",
        proxy_line,
        f'<a href="{row["url"]}">去出價</a> ｜ <a href="{dashboard_url}">開 dashboard</a>',
    ]
    return "\n".join(lines)


def format_high_p(match, dashboard_url: str) -> str:
    """規則 2：高信心標的。回答「這個價位買下去，划算的機率有多高」。"""
    from .valuation import venue_label

    row, est = match.row, match.estimate
    title = textwrap.shorten(row["title"], width=64, placeholder="…")
    payload = _payload_of(row)
    route_label = ((payload.get("best_route") or {}).get("label")) or row["route"]
    venue_tail = (
        f" ｜ {venue_label(est.venue)} 水準" if est.venue_adjusted
        else " ｜ ⚠️ 混合平台水準（未校正）"
    )
    tier = evidence_tier(est)
    lines = [
        f"🎯 <b>{_esc(title)}</b>",
        f"到手 <b>NT${row['landed_twd']:,.0f}</b>"
        f"（{row['price_native']:,.0f} {row['currency']} via {_esc(str(route_label))}）",
        f"公允價 <b>NT${est.fair_twd:,.0f}</b>"
        f"（{est.level} {est.level_label}，有效 n={est.n_effective}）{venue_tail}",
        f"{est.confidence:.0%} 區間 NT${est.lo_twd:,.0f}–{est.hi_twd:,.0f} ｜ "
        f"<b>P(值得買) {match.p_worth:.0%}</b>",
        f"{EVIDENCE_TIERS.get(tier, tier)}｜{_esc(evidence_label(est))}",
    ]
    icons = " ".join(
        _FLAG_ICON.get(f, f) for f in json.loads(row.get("flags") or "[]")
    )
    if icons:
        lines.append(icons)
    lines.append(
        f'<a href="{row["url"]}">看標的</a> ｜ <a href="{dashboard_url}">開 dashboard</a>'
    )
    return "\n".join(lines)


def _seller_head(match) -> list[str]:
    """規則 3／3b 共用的抬頭兩行（標題 ＋ 監控賣家與分數）。

    賣家沒有分數時**明講「手動加入、未達評分門檻」**，不印 0 分：
    0 分會被讀成「這個賣家很差」，而事實是「還沒有證據」。
    """
    from .notify_rules import SOURCE_MODEL, SOURCE_PEER

    icon = {SOURCE_PEER: "👤", SOURCE_MODEL: "🤖"}.get(match.judgement_source, "🔍")
    title = textwrap.shorten(match.row["title"], width=64, placeholder="…")
    if match.seller_score is not None:
        score_txt = f"Seller Alpha <b>{match.seller_score:.1f}</b> 分"
    else:
        score_txt = "<b>未達評分門檻</b>（證據不足，不是 0 分）"
    src = "手動加入" if match.seller_watch_source == "manual" else "自動入選"
    return [
        f"{icon} <b>{_esc(title)}</b>",
        f"監控賣家 <b>{_esc(str(match.seller_key))}</b>（{src}）｜{score_txt}",
    ]


def _landed_line(row: dict) -> str:
    return (
        f"到手 <b>NT${row['landed_twd']:,.0f}</b>"
        + (f"（{row['price_native']:,.0f} {row['currency']} via {row['route']}）"
           if row.get("price_native") and row.get("currency") and row.get("route") else "")
    )


def format_seller_new(match, dashboard_url: str) -> str:
    """規則 3：監控名單賣家的新上架。回答「你在追的這個人剛上了什麼、便宜多少」。

    三個刻意的顯示決定：
    1. **判定來源必須一眼看得出來**，因為它是兩種完全不同強度的宣稱：
         👤 同儕相對（強）＝ 同站×同卡×同版次×同分數的**其他賣家真的開這個價**
         🤖 模型估值（弱）＝ 沒有同儕，退而用模型公允價；點估計中位誤差 ×1.9
       模型那一則還必須說出**為什麼沒有同儕**（`peer_absent_reason`）——
       否則使用者無從判斷這個弱證據弱在哪裡。
    2. 賣家沒有分數時明講「未達評分門檻」，不印 0 分（見 `_seller_head`）。
    3. caveat 直接進訊息本文（不是附註）：同儕只有 1 個賣家、標題疑似變體
       這種前提，在手機上看不到就等於不存在。
    4. 通知檔位放行、但**出價檔會拒**的估價（`bidding_reject_note`）要掛
       ⚠️「不是出價依據」——放寬的必須看得見。這則訊息**永遠不含出價上限**：
       通知給的是「模型合理價＋折價幅度」，不是「你可以出到多少」。
    """
    from .notify_rules import SOURCE_MODEL
    from .valuation import venue_label

    row = match.row
    lines = _seller_head(match)
    if match.seller_score_note:
        lines.append(f"賣家層級：{_esc(str(match.seller_score_note))}")
    if match.judgement_source == SOURCE_MODEL:
        est = match.estimate
        venue_tail = (
            f" ｜ {venue_label(est.venue)} 水準" if est.venue_adjusted
            else " ｜ ⚠️ 混合平台水準（未校正）"
        )
        lines += [
            f"判定來源 <b>模型估值（弱）</b>｜"
            f"⚠️ <b>沒有同儕可比</b>：{_esc(str(match.peer_absent_reason or '—'))}",
            f"比模型公允價便宜 <b>{match.model_discount_pct:.0f}%</b>"
            f"（公允價 NT${est.fair_twd:,.0f}，{est.level} {est.level_label}，"
            f"有效 n={est.n_effective}）{venue_tail}",
            f"{est.confidence:.0%} 區間 NT${est.lo_twd:,.0f}–{est.hi_twd:,.0f}"
            + (f" ｜ P(值得買) {est.p_worth_buying:.0%}"
               if est.p_worth_buying is not None else ""),
            f"{EVIDENCE_TIERS.get(evidence_tier(est), '')}｜{_esc(evidence_label(est))}",
            _landed_line(row),
            "⚠️ 這是**模型絕對值**、不是賣家 alpha：它只回答「這一筆值不值得你看一眼」，"
            "**不進 Seller Alpha 分數**（分數永遠只用同儕相對）。請人工複核。",
        ]
        # 通知檔位放行、但出價檔會拒的估價：放寬的必須看得見。這一行不可省略
        # ——通知刻意比出價鬆（錯誤代價不對稱），鬆在哪裡要送到使用者眼前，
        # 而且要明說這個數字**不能**拿去當出價依據。
        if match.bidding_reject_note:
            lines.append(
                f"⚠️ <b>出價檔會拒收這份估價</b>（{_esc(str(match.bidding_reject_note))}）"
                "：僅供看一眼，不是出價依據",
            )
    else:
        lines += [
            "判定來源 <b>同儕相對（強）</b>",
            f"比同儕便宜 <b>{match.peer_discount_pct:.0f}%</b>"
            f"（同儕中位 NT${match.peer_median_twd:,.0f}，"
            f"可比 {match.peer_n} 筆／{match.peer_sellers} 個已知賣家）",
            _landed_line(row),
            f"可比層級：{_esc(str(match.peer_tier_label or '—'))}",
        ]
    for cv in match.seller_caveats:
        lines.append(_esc(str(cv)))
    lines.append(
        f'<a href="{row["url"]}">看標的</a> ｜ <a href="{dashboard_url}">開 dashboard</a>'
    )
    return "\n".join(lines)


def format_seller_unpriced(match, dashboard_url: str) -> str:
    """規則 3b：監控賣家上了一件**我們估不了**的東西。

    這則訊息**不宣稱它便宜**——我們不知道。它唯一的宣稱是「有這麼一件東西，
    而且我們說得出為什麼估不了」，其餘全是原始事實（卡名、稀有度、分數、
    年代證據、到手成本），讓使用者自己判斷。

    為什麼要有這條規則（使用者的顧慮）：**稀有品的價值恰恰在於它稀少，
    而稀少正是我們估不了的原因**——同卡成交只有 0-1 筆時同儕湊不出來、
    估價層級掉到 L3、校準樣本不足，四道閘門全部會擋。用「估不了就不通知」
    處理這個矛盾，等於系統性地漏掉唯一值得撿的那一類貨（missing foil error
    這種變體卡是最極端的例子：它連「同一張卡」都不存在第二筆）。
    所以這裡改成低音量浮出：每輪有上限、自己一本去重帳、措辭刻意不像撿漏通知。
    """
    row = match.row
    attrs = [
        f"卡名 {_esc(str(match.card_name or '比對不到'))}",
        f"稀有度 {_esc(str(match.rarity or '讀不出'))}",
        f"分數 {match.grade:g}" if match.grade is not None else "分數 未知",
    ]
    lines = _seller_head(match)
    lines += [
        f"❓ <b>估不了</b>：{_esc(match.unpriced_reason or '')}",
        " ｜ ".join(attrs),
        f"年代證據 {_esc('、'.join(match.era_evidence)) if match.era_evidence else '無'}",
        _landed_line(row),
        f"沒有同儕的原因：{_esc(str(match.peer_absent_reason or '—'))}",
        "這則**不是撿漏通知**：我們沒有能力判斷它貴或便宜，只是不想讓它安靜地消失。",
    ]
    lines.append(
        f'<a href="{row["url"]}">看標的</a> ｜ <a href="{dashboard_url}">開 dashboard</a>'
    )
    return "\n".join(lines)


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


def format_high_band(match, dashboard_url: str) -> str:
    """規則 5：高價帶折價。回答「這筆比同卡行情便宜多少、判定憑的是什麼」。

    `price_band_label`（🏷️ 高價帶徽章）與 `high_band_source_note`
    （「判定來源：同卡成交 × N 筆估值」，ratio < 0.5 會多帶一行深折價警語）
    **必須走到訊息上**——這是這條規則唯一的證據強度說明，漏掉就等於把
    L1/L2 判定降級成看不出來歷的數字（CLAUDE.md 第七節：樣本數不等於證據
    強度，判定來源要讓使用者自己看見）。`fair_twd`／`price_ratio` 與閘門
    讀的是同一個 `Estimate` 物件（修正回合 Task 9），不是 `comps.stats_for`
    的混池。**`price_ratio` 是標價市價比，不是到手成本比**——分子是這筆
    標價自己的市價基準 TWD，`landed_twd`（到手成本）只在「到手」那行顯示，
    不參與這個比率（修正回合二 Task 13：同幣別不同口徑不能相除）。
    """
    row = match.row
    title = textwrap.shorten(str(row.get("title") or ""), width=64, placeholder="…")
    lines = [
        f"{match.price_band_label} <b>{_esc(title)}</b>",
        f"到手 <b>NT${row['landed_twd']:,.0f}</b>"
        f"（{row.get('price_native', 0):,.0f} {row.get('currency', '')} via "
        f"{_esc(str(row.get('route') or ''))}）",
        f"估值公允價 NT${match.fair_twd:,.0f}（標價市價比 {match.price_ratio:.0%}）",
        _esc(str(match.high_band_source_note or "")),
        f'<a href="{row["url"]}">看標的</a> ｜ <a href="{dashboard_url}">開 dashboard</a>',
    ]
    return "\n".join(lines)


def format_overflow(overflow: list, dashboard_url: str) -> str:
    """超出單次上限的部分併成一則統計（與 alerts 的做法一致：不能讓好貨變洗版）。

    ⚠️ 四條規則**各自數自己的**，不用「總數減其他」倒推：多一條規則就會讓
    倒推的那一格默默地把別人的筆數算進自己頭上（工程原則 1）。
    """
    from .notify_rules import (
        RULE_AUCTION_URGENT,
        RULE_HIGH_BAND,
        RULE_HIGH_P,
        RULE_SELLER_NEW,
        RULE_SELLER_UNPRICED,
    )

    def n(rule: str) -> int:
        return sum(1 for m in overflow if m.rule == rule)

    lines = [
        f"🃏 另有 <b>{len(overflow)} 筆</b>命中但未列出（本輪推播上限）",
        f"競標急件 {n(RULE_AUCTION_URGENT)} 筆 ｜ 高信心 {n(RULE_HIGH_P)} 筆"
        f" ｜ 監控賣家新上架 {n(RULE_SELLER_NEW)} 筆"
        f" ｜ 估不了 {n(RULE_SELLER_UNPRICED)} 筆"
        f" ｜ 高價帶折價 {n(RULE_HIGH_BAND)} 筆",
        "它們**沒有**被記成已通知，下一輪會繼續排隊。",
        f'<a href="{dashboard_url}">開 dashboard</a>',
    ]
    return "\n".join(lines)


def _payload_of(row: dict) -> dict:
    try:
        return json.loads(row.get("payload") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _listing_of(row: dict) -> dict:
    return _payload_of(row).get("listing") or {}


def format_sources(sources: dict) -> str:
    """把 scan 的來源摘要壓成一行：`yahoo_direct 97 ｜ buyee_mercari ✗被擋`。

    每天的摘要必須讓「哪條管道瞎了」看得見——否則被擋三週的來源
    只會表現成三週的「今天沒好貨」（這正是 Phase 4 要解的病）。
    """
    parts = []
    for name, s in sources.items():
        health = s.get("health", "ok")
        if health in ("ok", "empty"):
            parts.append(f"{name} {s.get('count', 0)}")
        else:
            parts.append(f"{name} ✗{HEALTH_LABEL.get(health, health)}")
    return " ｜ ".join(parts)


#: 抓圖時的 UA。CDN 對「看起來像瀏覽器」與「看起來像 bot」的待遇不同，
#: 而這個病的根因就是被當成 bot 擋掉（見 `photo_url_of`）。
_IMAGE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

#: 上傳給 Telegram 的圖片大小上限（官方 sendPhoto 是 10MB，這裡取一半）。
#: 我們送的是搜尋結果縮圖，實測 12-20KB；超過這個量級代表抓錯東西了。
MAX_PHOTO_BYTES = 5 * 1024 * 1024

#: 抓圖的逾時（秒）。推播是有時效的：一張抓不動的圖不可以把整輪卡住。
PHOTO_FETCH_TIMEOUT = 15.0


def photo_url_of(row: dict) -> str | None:
    """這一則要附的圖片網址（`signals.image_url`），沒有就 None。

    為什麼要自己處理圖，而不是丟連結讓 Telegram 去抓
    （2026-08-04 實測，這是「推播只看得到 Buyee logo」的完整病因）：

    第一層 —— 讓 Telegram 抓賣場頁的 og:image：
        Buyee 商品頁對 Telegram 的 bot UA 回 **HTTP 403**（118 bytes）
          → 抓不到 og:image，退化成顯示 Buyee 的品牌佔位圖（使用者看到的那張）
        原站 paypayfleamarket.yahoo.co.jp 對同一支 UA 回 200，og:image 是真的卡片照。
        而我們掃描當下就把**真圖**存進了 `signals.image_url`——正確的圖一直在
        手上，卻交給一個會被 403 擋掉的第三方去重抓一次。

    第二層 —— 改用 `sendPhoto` 但仍然只交出網址（`photo=<url>`）：
        逐站實測（每則送出後立刻 deleteMessage，不打擾使用者）：
          static.mercdn.net（buyee_mercari）  → ok
          i.ebayimg.com（ebay）              → ok
          auc-pctr.c.yimg.jp（buyee_paypay／buyee_yahoo） → **400
            `Bad Request: failed to get HTTP URL content`**（去掉 query 也一樣）
        同一個網址我們自己 GET 是 200 image/jpeg 15-19KB。也就是說 Yahoo 的
        CDN 擋的是 **Telegram 的抓取端**，而那正是我們最大宗的兩個站。
        「有時候有圖有時候沒圖」比「都沒圖」更難察覺，所以不留這條半殘的路。

    結論（現行做法）：**圖片的位元組由我們自己抓、以 multipart 上傳**
    （`fetch_photo` ＋ `_send_photo`），四個站實測全部 ok。整條路上沒有任何
    一步依賴第三方抓得到我們的賣場或 CDN。

    只認 http(s)：協定相對網址與空字串抓不了也送不了，當成沒有圖直接走純文字
    （那條路一定送得出去）。
    """
    url = str(row.get("image_url") or "").strip()
    return url if url.startswith(("http://", "https://")) else None


def fetch_photo(url: str) -> bytes | None:
    """把圖抓成位元組。抓不到、不是圖、太大 → 回 None（**大聲**印一行原因）。

    回 None 不等於「這則不重要」：呼叫端會改送純文字，通知照樣送達
    （工程原則 3）。安靜地回 None 才是問題——那會表現成「最近的推播都沒圖」
    而沒有人知道為什麼。
    """
    try:
        r = httpx.get(
            url,
            timeout=PHOTO_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _IMAGE_UA},
        )
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - 抓圖失敗不准拖垮整則通知
        print(f"[warn] 圖片抓取失敗（{url[:70]}）：{exc}；本則改送純文字")
        return None
    ctype = (r.headers.get("content-type") or "").lower()
    if not ctype.startswith("image/"):
        print(f"[warn] 圖片網址回的不是圖片（content-type={ctype!r}）；本則改送純文字")
        return None
    if len(r.content) > MAX_PHOTO_BYTES:
        print(f"[warn] 圖片 {len(r.content) / 1e6:.1f}MB 超過上限；本則改送純文字")
        return None
    return r.content


def split_for_caption(text: str, limit: int = CAPTION_LIMIT) -> tuple[str, str]:
    """把一則訊息切成 `(caption, 續傳的餘文)`，**切在行邊界**。

    caption 只有 1024 字元而訊息可以有 4096，所以「附圖」在超長訊息上必須有
    一個不會弄丟資訊的做法。這裡刻意**不截斷**：截掉的那半通常正是到手成本、
    公允價、上限這些真的要拿去下單的數字，而使用者無從得知自己少看了什麼。

    切在行邊界有兩個理由：(1) 每一行都是自足的一件事實（「到手 NT$…」），
    切在行中間會產出半個金額；(2) 各 formatter 的 HTML 標籤都在同一行內閉合，
    行邊界切開之後兩半各自都還是合法的 HTML（切在標籤中間會讓 Telegram 整則退 400）。

    第一行就超過上限時回 `("", text)`：圖照送（無 caption），全文走 sendMessage。
    """
    if len(text) <= limit:
        return text, ""
    lines = text.split("\n")
    head: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        add = len(line) + (1 if head else 0)
        if used + add > limit:
            return "\n".join(head), "\n".join(lines[i:])
        head.append(line)
        used += add
    return text, ""  # pragma: no cover - 上面的迴圈必然會提早 return


class TelegramNotifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.token = cfg.telegram_token
        self.chat_id = cfg.telegram_chat_id
        #: 設定層的總開關（`notify.enabled`）。與「有沒有 token」是兩件不同的事：
        #: 沒 token 是「設定不完整」（要去補 .env），關掉是「使用者現在不想被吵」
        #: （.env 原封不動，改一行 yaml 就恢復）。兩者混成一個布林值的話，
        #: CLI 只能印出一種訊息，使用者會去修一個根本沒壞的東西。
        self.config_enabled = bool(cfg.notify.get("enabled", True))
        self.configured = bool(self.token and self.chat_id)
        self.enabled = self.config_enabled and self.configured
        self.dashboard_url = "http://127.0.0.1:8321"
        #: 送出節流。`notify.max_items_per_run` 拿掉上限之後，一輪可能連送數十則，
        #: 而 Telegram 的限制是 30 則/秒（全域）、同一個 chat 約 20 則/分鐘。
        self.send_interval_seconds = _interval_setting(cfg.notify)
        #: 上一則**真的送出**的時刻（monotonic）。開關擋下來的那些不算——
        #: 它們沒有打到 API，不該讓下一則多等。
        self._last_send_at: float | None = None
        #: 估價模型**不在這裡建**：規則判定那一側（notify_rules）才需要它，
        #: 而 Pipeline 已經有一顆（`Pipeline.valuator()`）。在這裡再建一顆，
        #: 同一輪就會有兩個模型算同一張卡（工程原則 1）。

    def send(self, text: str, photo: str | None = None) -> bool:
        """唯一真的打 Telegram HTTP API 的地方。`photo` 給了就走 `sendPhoto`。

        `notify.enabled=false` 在**這裡**也擋一次（CLI 已經先擋過一次）：
        推播的呼叫點有五個（訊號、摘要、告警、復原、test-telegram），
        靠每個呼叫點自己記得檢查開關，漏掉一個就會在使用者以為關掉的時候送出去。
        擋在唯一的出口才是結構性的保證（工程原則 5）——`sendPhoto` 因此也自動
        吃到同一組開關、同一組節流、同一組 429 處理，不另開一條路。

        附圖走的是 **multipart 上傳**（圖片位元組由我們自己抓，見 `fetch_photo`），
        不是把網址交給 Telegram 去抓——Yahoo 的 CDN 會擋掉 Telegram 的抓取端，
        而那是我們最大宗的兩個站。完整實測見 `photo_url_of`。

        **附圖的長度策略**（2026-08-04 實測全部四條規則的訊息長度：
        規則 2 中位 364／最長 391、規則 3 中位 776／最長 856、規則 3b 中位 540／
        最長 627，四條規則 50 則**沒有任何一則**超過 caption 的 1024）：
          - 全文塞得下 caption（現況的每一則都是）→ **一則 sendPhoto 就結束**，
            圖與所有數字在同一則裡，不切、不重複。
          - 塞不下 → caption 放前段（切在行邊界）、餘文緊接著一則 sendMessage。
            選這個而不是「caption 只放標題、全文另送」，是因為它**不重複任何一行**：
            重複的那個做法在現況（沒有任何一則超長）等於永遠多送一則沒必要的訊息，
            而超長時使用者要在兩則裡對照哪些是新的。也不選截斷——見 split_for_caption。

        回 True 只代表**整則內容都送達了**：圖送出但餘文失敗一律回 False
        （不落已通知帳，下一輪重排隊），寧可重複也不要讓半則訊息被記成已送出。
        """
        if not self.config_enabled:
            print("[warn] Telegram 已停用（config notify.enabled=false），本則訊息未送出")
            return False
        if not self.enabled:
            print("[warn] Telegram 未設定（.env 缺 TELEGRAM_BOT_TOKEN / CHAT_ID）")
            return False
        if not photo:
            return self._send_text(text, disable_preview=False)

        blob = fetch_photo(photo)
        if blob is None:
            # 抓不到圖 ≠ 這則不重要。純文字一定送得出去，通知不可以因為一張圖消失。
            return self._send_text(text, disable_preview=True)

        caption, rest = split_for_caption(text)
        ok, rejected = self._deliver(
            "sendPhoto",
            data={"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("card.jpg", blob, "image/jpeg")},
        )
        if not ok:
            # Telegram 回 4xx＝**確定沒送達**（圖片格式／尺寸不合它的規矩），
            # 退回純文字：一張圖不可以讓整則通知消失（工程原則 3）。
            # 逾時／連線中斷／5xx 則**不退回**：那些是「不知道有沒有送達」，
            # 退回就可能給使用者兩則（工程原則 2，與下面 sendMessage 的立場一致）。
            if not rejected:
                return False
            print("[warn] sendPhoto 被 Telegram 退回（圖片可能不合規），改送純文字")
            return self._send_text(text, disable_preview=True)
        if rest and not self._send_text(rest, disable_preview=True):
            print("[warn] 圖片已送出但餘文送出失敗，本則不記已通知（下輪重送整則）")
            return False
        return True

    def _send_text(self, text: str, *, disable_preview: bool) -> bool:
        """純文字那條路。

        `disable_web_page_preview`：沒有附圖時維持 False（連結預覽是那則訊息
        唯一的視覺），**附圖之後一律 True**——賣場連結的預覽正是那張 Buyee
        佔位 logo（見 `photo_url_of`），留著只會在真圖旁邊再放一次同一個 logo。
        """
        if len(text) > TEXT_LIMIT:
            # Telegram 會直接回 400。不自作主張截斷（截掉的通常正是尾巴那幾個
            # 數字），但一定要說出原因：不然日誌上只有一個沒頭沒尾的 400。
            print(f"[warn] 訊息 {len(text)} 字元超過 sendMessage 上限 {TEXT_LIMIT}，"
                  "Telegram 會拒收；這則會留在隊列裡（formatter 需要縮短）")
        ok, _ = self._deliver(
            "sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
        )
        return ok

    def _deliver(self, method: str, **kw) -> tuple[bool, bool]:
        """打一次 Bot API。回 `(送出成功, 被 Telegram 明確退回)`。

        第二個布林是給附圖降級用的：**「確定沒送達」與「不知道有沒有送達」
        是兩種失敗**，只有前者可以改用另一種方式再送一次而不會產生重複訊息。
        4xx（429 除外）＝ Telegram 在處理之前就拒收 → 確定沒送達。
        """
        self.throttle()
        try:
            r = self._post(method, **kw)
            # 429 是**唯一**可以原樣重送的失敗：Telegram 在處理訊息之前就退回，
            # 也就是「這一則確定沒有送達」——所以重送不會產生重複訊息。
            # 其他失敗（逾時、連線中斷、5xx）一律不重送：sendMessage/sendPhoto
            # 都不是冪等的，回應遺失時訊息可能已經送達，重送就是給使用者兩則
            # 一樣的東西（工程原則 2：只有確定沒落地的非冪等寫入才能重試）。
            if r.status_code == 429:
                wait = _retry_after(r)
                if wait is None:
                    print("[warn] Telegram 429 但沒給 retry_after，本則不重送")
                    return False, False
                print(f"[warn] Telegram 429（速率限制），等 {wait:.0f} 秒後重送一次")
                _sleep(wait)
                r = self._post(method, **kw)
            r.raise_for_status()
            return True, False
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            print(f"[warn] Telegram {method} 送出失敗: {exc}")
            return False, 400 <= code < 500 and code != 429
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Telegram {method} 送出失敗: {exc}")
            return False, False

    def _post(self, method: str, **kw) -> httpx.Response:
        """唯一一個 `httpx.post`。`kw` 是 `json=`（純文字）或 `data=`＋`files=`
        （上傳圖片的 multipart）——兩種都必須經過這裡，節流的時間戳才數得準。"""
        self._last_send_at = time.monotonic()
        return httpx.post(
            _API.format(token=self.token, method=method), timeout=30, **kw
        )

    def throttle(self) -> float:
        """睡到距離上一次送出至少 `send_interval_seconds` 秒。回傳實際睡了幾秒。

        擺在 `send()` 裡而不是 `send_rule_matches()` 裡：送出點有五個（訊號、
        摘要、告警、復原、test-telegram），而 Telegram 的速率限制是**整個 bot
        對整個 chat** 的，不分訊息種類。擋在唯一的出口才數得準（工程原則 5）。
        """
        if self.send_interval_seconds <= 0 or self._last_send_at is None:
            return 0.0
        wait = self.send_interval_seconds - (time.monotonic() - self._last_send_at)
        if wait <= 0:
            return 0.0
        _sleep(wait)
        return wait

    def render(self, match) -> str:
        """一則 Match 的訊息全文。**送出與預覽走同一支**——preview 印的東西
        跟真的會送出去的必須是同一份，不然調門檻時看到的是另一則訊息。"""
        from .notify_rules import (
            RULE_AUCTION_URGENT,
            RULE_CARD_SNIPE,
            RULE_HIGH_BAND,
            RULE_SELLER_NEW,
            RULE_SELLER_UNPRICED,
        )

        if match.rule == RULE_CARD_SNIPE:
            return format_card_snipe(match, self.dashboard_url)
        if match.rule == RULE_AUCTION_URGENT:
            return format_auction_urgent(match, self.dashboard_url)
        if match.rule == RULE_SELLER_NEW:
            return format_seller_new(match, self.dashboard_url)
        if match.rule == RULE_SELLER_UNPRICED:
            return format_seller_unpriced(match, self.dashboard_url)
        if match.rule == RULE_HIGH_BAND:
            return format_high_band(match, self.dashboard_url)
        return format_high_p(match, self.dashboard_url)

    def send_rule_matches(self, outcome) -> list[tuple[str, str]]:
        """送出這一輪的兩條規則命中，回傳**真的送成功**的 (key, rule)。

        沒送成功就不回傳 → 不落帳 → 下一輪還會再試（工程原則 3：
        送失敗被記成已通知，那則訊息就永遠消失了）。
        沒有任何要送的東西時**一則都不送**，連標頭都沒有（silent_when_empty
        的精神：每小時 24 輪，例行報平安會把推播訓練成雜訊）。

        四條規則**都**附圖（`photo_url_of`）：看照片是判斷一張卡值不值得點進去
        最快的一步，沒有理由只給其中幾條規則。沒有圖片網址的那幾筆自動走純文字。
        """
        sent: list[tuple[str, str]] = []
        for m in outcome.to_send:
            try:
                text = self.render(m)
            except Exception as exc:  # noqa: BLE001 - 一則格式化失敗不拖垮整批
                print(f"[warn] 推播格式化失敗（{m.key}／{m.rule}），本則不送：{exc}")
                continue
            if self.send(text, photo=photo_url_of(m.row)):
                sent.append((m.key, m.rule))
        if outcome.overflow:
            # 併列統計本身也是一則訊息，但它不落任何帳：那幾筆下輪要重新排隊。
            self.send(format_overflow(outcome.overflow, self.dashboard_url))
        outcome.sent = sent
        return sent

    # ------------------------------------------------------------------
    # 來源健康告警。刻意與 send_signals 分開：好貨與壞消息是兩種訊息，
    # 而且告警必須回傳成功與否——呼叫端要靠它決定要不要記「已通知」
    # （工程原則 3：送失敗不能被當成已送出而靜默吞掉）。
    def send_alert(self, text: str) -> bool:
        return self.send(_esc(text))

    def send_recovery(self, text: str) -> bool:
        return self.send(_esc(text))

    def send_summary(
        self, stats: dict, scanned: int, signals: int, sources: dict | None = None
    ) -> None:
        lines = [
            "✅ 掃描完成",
            f"掃 {scanned} 筆 → 訊號 {signals} 筆",
            f"待處理 {stats['by_state'].get('new', 0)} ｜ "
            f"湊單籃 {stats['by_state'].get('in_bundle', 0)}",
            f"行情庫 {stats['comps']} 筆",
        ]
        if sources:
            lines.append("來源：" + format_sources(sources))
        self.send("\n".join(lines))
