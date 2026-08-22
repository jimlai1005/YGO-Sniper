"""CLI 入口。

日常只會用到兩個：
    ygo-sniper daily     每天那一鍵（launchd 也是跑這個）
    ygo-sniper serve     開 dashboard

其他都是調校用的。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .alerts import HEALTH_LABEL
from .config import load_config
from .costs import breakeven_table
from .fx import FxRates
from .pipeline import Pipeline
from .schedule_watch import PENDING_ALERT_KEY
from .seller_alpha import SALE_AUCTION, SALE_FIXED, SALE_KIND_LABEL, SALE_UNKNOWN
from .seller_links import seller_page_url
from .store import Store

app = typer.Typer(add_completion=False, help="遊戲王 1998-2004 鑑定卡撿漏掃描器")
console = Console()


def _seller_cell(seller_key: str) -> str:
    """賣家鍵 → rich 表格／console.print 用的文字。

    有原站連結（見 `seller_links.seller_page_url`）就包成
    `[link=url]key[/link]`——支援的終端機可以直接點開，不支援的就顯示成
    一般文字，不會壞。未知 site 回 `None`，這裡就印純文字，**不猜連結**：
    猜錯的連結點下去是 404，比沒有連結更糟（CLAUDE.md 第五節）。
    """
    url = seller_page_url(seller_key)
    return f"[link={url}]{seller_key}[/link]" if url else seller_key


@app.command()
def daily(
    skip_comps: bool = typer.Option(False, help="跳過行情更新（快速重跑用）"),
    no_notify: bool = typer.Option(False, help="只掃不推播"),
):
    """每小時那一鍵：更新行情 → 掃描 → 推播 Telegram（沒好貨就完全靜默）。

    指令名維持 `daily` 是為了不破壞既有的 launchd plist 與任何手動習慣；
    實際排程已改成每小時（scripts/com.jim.ygosniper.plist）。
    """
    pipe = Pipeline()
    try:
        result = pipe.scan(skip_comps=skip_comps)
        _print_scan(result)
        _mine_snipes_daily(pipe)
        if not no_notify:
            _run_notifications(pipe, result)
    finally:
        pipe.close()


@app.command()
def daily_high(no_notify: bool = typer.Option(False, help="只掃不推播")):
    """高價帶那一鍵：掃 ¥8,624～50,000 帶 → 只推 ≤7 折＋狙擊命中。獨立排程跑這個。

    不跑 `_mine_snipes_daily`——狙擊挖掘一天一次由低價帶 `daily` 負責，
    高價帶重複跑只是加倍請求（見 plan Task 5）。
    """
    pipe = Pipeline()
    try:
        result = pipe.scan(high_band=True)
        _print_scan(result)
        if not no_notify:
            _run_notifications(pipe, result)
    finally:
        pipe.close()


def _print_rule_counts(outcome) -> None:
    """規則有沒有在運作，要看得出來。

    只印「已推播 N 則」的話，0 有兩種讀法：「兩條規則都沒命中」與「規則寫壞了、
    永遠命中不了」。所以命中、去重／撞號、超量、送出四個數字**分開印**。
    """
    from .notify_rules import (
        RULE_AUCTION_URGENT,
        RULE_CARD_SNIPE,
        RULE_HIGH_BAND,
        RULE_HIGH_P,
        RULE_LABEL,
        RULE_SELLER_NEW,
        RULE_SELLER_UNPRICED,
        SOURCE_MODEL,
        SOURCE_PEER,
    )

    peer = sum(1 for m in outcome.seller_new if m.judgement_source == SOURCE_PEER)
    model = sum(1 for m in outcome.seller_new if m.judgement_source == SOURCE_MODEL)
    console.print(
        f"\n[bold]{RULE_LABEL[RULE_AUCTION_URGENT]}[/bold] 命中 {len(outcome.urgent)} 筆 ｜ "
        f"[bold]{RULE_LABEL[RULE_HIGH_P]}[/bold] 命中 {len(outcome.high_p)} 筆 ｜ "
        f"[bold]{RULE_LABEL[RULE_SELLER_NEW]}[/bold] 命中 {len(outcome.seller_new)} 筆"
        f"（同儕 {peer}／模型 {model}） ｜ "
        f"[bold]{RULE_LABEL[RULE_SELLER_UNPRICED]}[/bold] 命中 "
        f"{len(outcome.seller_unpriced)} 筆 ｜ "
        f"[bold]{RULE_LABEL[RULE_CARD_SNIPE]}[/bold] 命中 "
        f"{len(outcome.card_snipe)} 筆"
        f"（🎯 {sum(1 for m in outcome.card_snipe if m.row.get('tier') == 'exact')}"
        f"／👀 {sum(1 for m in outcome.card_snipe if m.row.get('tier') == 'partial')}） ｜ "
        f"[bold]{RULE_LABEL[RULE_HIGH_BAND]}[/bold] 命中 {len(outcome.high_band)} 筆"
        f" ｜ 送出 {len(outcome.sent)} 則"
    )
    if outcome.deduped:
        console.print(f"[dim]（{outcome.deduped} 筆因去重／與規則 1 撞號未送）[/dim]")
    if outcome.overflow:
        console.print(
            f"[yellow]{len(outcome.overflow)} 筆超出單輪上限，已併成一則統計，"
            "下輪繼續排隊[/yellow]"
        )
    if not outcome.valuation_ok:
        console.print("[red]⚠️ 估價模型建不起來，本輪規則 2（P 值）沒有判定[/red]")
    if not outcome.seller_ctx_ok:
        console.print(
            "[red]⚠️ 賣家同儕脈絡建不起來，本輪規則 3（監控賣家新上架）沒有判定"
            "——這與「名單是空的」是兩件事[/red]"
        )


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


def _run_notifications(pipe, result: dict) -> int:
    """推播決策的唯一落點。回傳實際送出的訊號筆數。

    每小時跑 24 次，其中絕大多數必然沒有新貨。如果每輪都送一則「掃完了、0 筆」，
    推播很快就會被訓練成雜訊，然後真的有貨的那一次你會直接滑過去
    ——所以 `notify.silent_when_empty`（預設 true）在沒有新訊號時**連摘要都不送**。

    `notify.enabled=false` 是**另一件事**，不要跟 silent_when_empty 混為一談：
    silent 是「這一輪沒東西講，所以不出聲」；enabled=false 是「整條推播管道關掉」。
    關掉時掃描、告警判定、落庫全部照常跑（dashboard 與 alerts 表看得到一模一樣的
    東西），只是不送出——而且**要印出來說我沒送**，靜默的停用跟壞掉長得一樣。

    但這裡有一條絕不能跨過的線：**靜默只吞「沒好貨」，永遠不吞「爬蟲壞了」。**
    這兩件事的外顯行為一模一樣（都是 0 筆），差別在只有後者需要你去修；
    把告警一起靜音，就會變成「連續三週以為市場很冷，其實 parser 早就掛了」
    ——那正是這個專案整套健康判定要解的病。所以 `_send_alerts` 在
    silent 判斷**之外**無條件執行。
    """
    if not bool(pipe.cfg.notify.get("enabled", True)):
        _report_notify_disabled(pipe, result)
        return 0

    outcome = pipe.notify()
    n = len(outcome.sent)
    silent = bool(pipe.cfg.notify.get("silent_when_empty", True))

    _print_rule_counts(outcome)
    if n:
        console.print(f"[green]已推播 {n} 則到 Telegram[/green]")
    else:
        console.print("[dim]沒有要送的東西，本輪不推播訊號訊息。[/dim]")

    # 摘要是「好貨的附註」（讓你看得到各來源筆數），不是獨立的心跳訊息。
    # 沒有好貨可附註時就不該存在——除非明確關掉靜默。
    if n or not silent:
        pipe.notifier.send_summary(
            pipe.store.stats(), result["scanned"], result["signals"], result.get("sources")
        )
    elif silent:
        console.print("[dim]（靜默模式：本輪不送摘要；來源告警不受影響）[/dim]")

    # 順序刻意：先好貨、再壞消息。告警放最後，才不會把撿漏擠出視線。
    # 位置刻意：在 silent 判斷之外——見 docstring。
    _send_alerts(pipe, result.get("alerts") or [])
    _send_schedule_alert(pipe)
    return n


def _report_notify_disabled(pipe, result: dict) -> None:
    """推播關掉時的唯一輸出。**不呼叫 notifier 的任何方法。**

    為什麼不是「照送、讓 send() 自己擋下來」：那條路上每則告警都會拿到
    False（送出失敗），`_send_alerts` 會照它的語意印「N 則送出失敗，下輪會再試」
    ——把「我故意沒送」顯示成「我送不出去」。停用與故障必須看得出差別。

    待推播的訊號與到期告警都**留在庫裡不動**（不落推播帳、不落冷卻帳），
    所以恢復推播的那一輪會把這段期間的東西補送出去，不會有斷層。

    命中數走的是與真的推播**同一支判定**（`Pipeline.notification_outcome`）：
    停用期間印一個用別的算法算出來的數字，等於讓使用者看不到規則的真實狀態。
    """
    outcome = pipe.notification_outcome()
    alerts = result.get("alerts") or []
    console.print(
        f"\n[yellow]Telegram 已停用（notify.enabled=false），本輪"
        f" 競標急件 {len(outcome.urgent)} 筆 ／ 高信心 {len(outcome.high_p)} 筆"
        f" ／ 監控賣家新上架 {len(outcome.seller_new)} 筆"
        f" ／ 估不了 {len(outcome.seller_unpriced)} 筆"
        f" 只落庫不推播[/yellow]"
    )
    if alerts:
        console.print(
            f"[yellow]另有 {len(alerts)} 則來源健康告警同樣未送出"
            "（判定照跑、alerts 表照落，`ygo-sniper health` 看得到）[/yellow]"
        )
    if getattr(pipe, "_schedule_alert", None):
        console.print(
            "[yellow]另有 1 則排程監督告警同樣未送出"
            "（已留在 pending 帳裡，`notify.enabled` 恢復後下一輪會補送）[/yellow]"
        )
    console.print("[dim]要恢復：把 config/settings.yaml 的 notify.enabled 改回 true。[/dim]")


def _send_alerts(pipe: Pipeline, alerts: list) -> None:
    """送出到期告警，**只對真的送出成功的那幾則**落冷卻帳。

    送失敗就不記 notified：下一輪還會再吵一次。寧可重複也不要靜默漏掉
    ——「來源瞎了」的通知漏掉，就等於回到 Phase 4 要解的那個病。
    """
    if not alerts:
        return
    sent = []
    for a in alerts:
        ok = (
            pipe.notifier.send_recovery(a)
            if getattr(a, "recovery_source", "")
            else pipe.notifier.send_alert(a)
        )
        if ok:
            sent.append(a)
    pipe.alerts.mark_sent(sent)
    if len(sent) < len(alerts):
        console.print(f"[red]告警 {len(alerts) - len(sent)} 則送出失敗，下輪會再試[/red]")
    else:
        console.print(f"[yellow]已送出 {len(sent)} 則來源告警[/yellow]")


def _send_schedule_alert(pipe: Pipeline) -> None:
    """排程空窗／上輪未收尾告警（schedule_watch.py）的推播與清帳。

    刻意不走 `AlertEngine`／`mark_sent`：那本帳量的是「同一個 source 連續
    壞了幾次」，排程空窗是完全不同的事件源（邊緣觸發：偵測時就已經覆寫了
    `RUN_STARTED_KEY` 基準），混在一起會讓來源告警的次數統計失真
    （CLAUDE.md 第五節「兩本帳不能合併」）。

    清帳的權力只在這裡（送出成功之後），不在 `pipeline._update_schedule_state`
    ——那一步只負責偵測與合併 pending，見 `schedule_watch.resolve_alert`
    的 docstring。這是 Fix 2 的核心：**消費 pending 的是「送達確認」，不是
    「算出了訊息」**，送失敗（例如筆電剛醒來 Wi-Fi 還沒穩）就讓 pending
    留著，下一輪（不論是下一次 `daily`、還是使用者手動 `scan` 之後的
    下一次 `daily`）會把它連同新問題一起重送，不會像舊版那樣被覆寫掉。

    用 `getattr` 防呆：只有 `scan()` 真的跑過（且不是 dry-run／watch-scan）
    才會設 `_schedule_alert` 這個屬性。

    **清帳一定要清「偵測出這則告警的那本帳」**：高價帶輪（`daily-high`）
    的告警是 `_update_schedule_state(high_band=True)` 用
    `PENDING_ALERT_KEY_HIGH` 偵測出來的，送達後若清成低價帶的
    `PENDING_ALERT_KEY`，會清掉不相干的那把鍵——高價帶自己的 pending
    永遠不會被消耗，於是「先前 N 次未送達」無限疊加（兩本帳不能合併，
    CLAUDE.md 第五節）。`pipe._schedule_alert_pending_key` 由
    `_update_schedule_state` 在算 `pending_key` 的同一刻寫下，跟
    `_schedule_alert` 本身同源，不用另外猜這一輪是哪一帶。
    """
    schedule_alert = getattr(pipe, "_schedule_alert", None)
    if not schedule_alert:
        return
    pending_key = getattr(pipe, "_schedule_alert_pending_key", PENDING_ALERT_KEY)
    if pipe.notifier.send_alert(schedule_alert):
        pipe.store.set_meta(pending_key, "")
        console.print("[yellow]已推播排程監督告警[/yellow]")
    else:
        console.print("[red]排程監督告警送出失敗——留在 pending 帳裡，下一輪會一併重送[/red]")


@app.command()
def scan(dry_run: bool = typer.Option(False, help="不寫入資料庫")):
    """只掃描不推播。"""
    pipe = Pipeline()
    try:
        _print_scan(pipe.scan(dry_run=dry_run))
    finally:
        pipe.close()


@app.command()
def scan_high(dry_run: bool = typer.Option(False, help="不寫入資料庫")):
    """高價帶只掃不推播。"""
    pipe = Pipeline()
    try:
        _print_scan(pipe.scan(high_band=True, dry_run=dry_run))
    finally:
        pipe.close()


@app.command()
def comps():
    """只更新行情資料庫。第一次跑建議先執行這個累積幾天。"""
    pipe = Pipeline()
    try:
        n = pipe.refresh_comps()
        console.print(f"[green]新增 {n} 筆成交紀錄[/green]")
        console.print(pipe.store.stats())
        _print_sold_at_provenance(pipe.store)
    finally:
        pipe.close()


def _print_sold_at_provenance(store: Store) -> None:
    """把「`sold_at` 到底是不是成交時間」的帳印出來。

    這不是裝飾。Buyee 系（Mercari 鏡像）的已售出頁**沒有任何成交時間**
    （2026-08-02 實測），那批列的 `sold_at` 是我們入庫的時間，所以
    `scoring.comps_window_days` 的 90 天視窗對它們形同虛設。
    任何看時間的分析都必須知道自己手上有多少這種資料。
    """
    p = store.comps_provenance()
    if not p["total"]:
        return
    t = Table(title="comps 的 sold_at 來歷（時間可信度）")
    for col in ("站台", "真實成交時間", "入庫時間（非成交時間）", "未標記", "合計"):
        t.add_column(col, justify="left" if col == "站台" else "right")
    for row in p["by_site"]:
        n = int(row["n"] or 0)
        real = int(row["real_time"] or 0)
        t.add_row(
            str(row["site"]), str(real), str(int(row["ingest"] or 0)),
            str(n - real - int(row["ingest"] or 0)), str(n),
        )
    t.add_row("合計", str(p["real"]), str(p["ingest"]), str(p["unknown"]), str(p["total"]))
    console.print(t)
    if p["ingest"]:
        console.print(
            f"[dim]⚠️ {p['ingest']} 筆的 sold_at 是**入庫時間**，不是成交時間"
            "（來源頁面根本沒有成交時間）。它們的價格是真的，但時間不是——"
            "90 天視窗、時間切分、賣得掉率這類分析必須把它們分開看"
            "（store.load_comps(real_sold_at_only=True)）。[/dim]"
        )


@app.command()
def mine_history(
    pages: int = typer.Option(9, help="每個查詢最多翻幾頁（一頁 100 筆，約 25 天）"),
    max_requests: int = typer.Option(120, help="這一輪的請求硬上限（超過就記帳停手）"),
    dry_run: bool = typer.Option(False, help="只印會做什麼，不打外網、不入庫、不記帳"),
    reset: bool = typer.Option(False, help="清空續跑帳本（人工「我要重挖一次」）"),
    show_ledger: bool = typer.Option(False, help="只印續跑帳本現況"),
    redo_exhausted: bool = typer.Option(False, help="連「已翻完」的查詢也重來"),
):
    """**歷史成交回填**：把 Yahoo 落札相場的 180 天 archive 挖出來餵 comps。

    這不是每輪掃描該做的事（那是 `comps` 指令的節流查詢），是**一次性的深挖**：
    Seller Alpha 的「持續性」原本要等帳本累積四週，但落札相場本來就存著半年的
    成交紀錄，而且每一筆都帶賣家與真實成交時間——不必等，挖就好。

    冪等（`UNIQUE(signature, url)`）、可續跑（頁碼記在 meta）、有請求硬上限。
    """
    from .comps import CompsEngine, expand_comps_queries
    from .history import HistoryParams, load_ledger, reset_ledger, run_yahoo_backfill
    from .sources import CachedFetcher, build_sources

    cfg = load_config()
    store = Store(cfg.db_path)

    if reset:
        reset_ledger(store)
        console.print("[yellow]續跑帳本已清空——下次會從每個查詢的第 1 頁重挖。[/yellow]")
        return

    ledger = load_ledger(store)
    if show_ledger:
        if not ledger:
            console.print("[dim]續跑帳本是空的（還沒挖過）。[/dim]")
            return
        t = Table(title=f"歷史回填續跑帳本（{len(ledger)} 個查詢）")
        for col in ("查詢", "已翻到第幾頁", "翻完了嗎", "累計成交", "累計入庫", "最後一次"):
            t.add_column(col, justify="left" if col == "查詢" else "right")
        for kw, e in sorted(ledger.items(), key=lambda kv: -int(kv[1].get("pages_done") or 0)):
            t.add_row(
                kw, str(e.get("pages_done")), "是" if e.get("exhausted") else "否",
                str(e.get("found")), str(e.get("kept")), str(e.get("at") or "")[:16],
            )
        console.print(t)
        return

    spec = cfg.watchlist.get("comps_queries") or {}
    queries = expand_comps_queries(spec)
    if not queries:
        console.print("[red]watchlist 的 comps_queries 展開後是空的——沒有東西可挖。[/red]")
        raise typer.Exit(1)

    fetcher = CachedFetcher(cfg)
    registry = build_sources(cfg, fetcher)
    source = registry.get("yahoo_closed")
    fx = FxRates(cfg)
    engine = CompsEngine(cfg, fx, store)
    params = HistoryParams(pages=pages, max_requests=max_requests, redo_exhausted=redo_exhausted)
    try:
        report = run_yahoo_backfill(
            store=store, comps=engine, source=source,
            queries=queries, params=params, dry_run=dry_run,
        )
    finally:
        fetcher.close()

    t = Table(title=f"Yahoo 落札相場深挖（{'dry-run' if dry_run else '已執行'}）")
    for col in ("查詢", "起始頁", "抓了幾頁", "成交", "入庫", "帶賣家", "最舊", "最新", "翻完"):
        t.add_column(col, justify="left" if col == "查詢" else "right", overflow="fold")
    for o in report.outcomes:
        t.add_row(
            o.query, str(o.from_page), str(o.pages_fetched), str(o.found), str(o.kept),
            str(o.with_seller), (o.oldest or "—")[:10], (o.newest or "—")[:10],
            "是" if o.exhausted else "否",
        )
    console.print(t)
    console.print(f"[green]{report.summary()}[/green]")
    for kw, why in report.skipped[:10]:
        console.print(f"[dim]  跳過 {kw}：{why}[/dim]")
    if len(report.skipped) > 10:
        console.print(f"[dim]  …另有 {len(report.skipped) - 10} 個跳過[/dim]")
    for err in report.errors:
        console.print(f"[yellow]  · {err}[/yellow]")
    if dry_run:
        console.print("[yellow]dry-run：未打外網、未入庫、未記帳。[/yellow]")
        return
    _print_sold_at_provenance(store)


@app.command()
def mine_seller_history(
    site: str = typer.Option("buyee_paypay", help="只挖這個站的賣家"),
    limit: int = typer.Option(20, help="最多挖幾個賣家（每人 1 個請求）"),
    dry_run: bool = typer.Option(False, help="只列會挖誰，不打外網、不入庫"),
):
    """**賣家頁歷史成交**：對已知賣家逐一抓他的歷史成交（目前只有 Yahoo!フリマ）。

    Yahoo!フリマ 的 `/user/{id}` 一頁 100 筆、混著在架與已售出，而已售出那批
    帶**真實成交時間**（實測某賣家 73/100 筆已售出、橫跨 178 天）。
    一個請求換一個賣家半年的行為紀錄——這是「持續性」最便宜的資料來源。

    其他站台為什麼沒有：`ygo-sniper mine-seller-history --site ebay` 會告訴你
    （eBay 的已售出資料需要 Marketplace Insights 權限，實測 403）。
    """
    from .comps import CompsEngine
    from .history import (
        SELLER_HISTORY_SOURCE,
        SELLER_HISTORY_UNSUPPORTED,
        mine_paypay_seller,
    )
    from .sources import CachedFetcher, build_sources

    cfg = load_config()
    store = Store(cfg.db_path)
    source_name = SELLER_HISTORY_SOURCE.get(site)
    if source_name is None:
        why = SELLER_HISTORY_UNSUPPORTED.get(site, f"{site} 沒有賣家頁歷史的實作")
        console.print(f"[yellow]{site} 挖不了賣家歷史：{why}[/yellow]")
        return

    sellers_rows = store.list_sellers(site=site, limit=limit)
    if not sellers_rows:
        console.print(f"[dim]{site} 的賣家帳本是空的。[/dim]")
        return

    fetcher = CachedFetcher(cfg)
    registry = build_sources(cfg, fetcher)
    source = registry[source_name]
    engine = CompsEngine(cfg, FxRates(cfg), store)
    outcomes = []
    try:
        for row in sellers_rows:
            outcomes.append(
                mine_paypay_seller(
                    comps=engine, source=source,
                    seller_id=row["seller_id"], dry_run=dry_run,
                )
            )
    finally:
        fetcher.close()

    _print_seller_mine(outcomes, dry_run=dry_run)


def _print_seller_mine(outcomes: list, *, dry_run: bool) -> None:
    t = Table(title=f"賣家頁歷史成交（{'dry-run' if dry_run else '已執行'}）")
    for col in ("seller_key", "成交筆數", "入庫", "最舊", "最新", "跨度(天)", "備註"):
        t.add_column(col, justify="left" if col in ("seller_key", "備註") else "right",
                     overflow="fold")
    for o in outcomes:
        t.add_row(
            o.seller_key, str(o.found), str(o.kept), (o.oldest or "—")[:10],
            (o.newest or "—")[:10], f"{o.span_days:.0f}", o.note or "",
        )
    console.print(t)
    console.print(
        f"[green]請求 {sum(o.requests for o in outcomes)} 個 → "
        f"成交 {sum(o.found for o in outcomes)} 筆、入庫 {sum(o.kept for o in outcomes)} 筆[/green]"
    )


@app.command()
def seed_sellers(
    limit: int = typer.Option(40, help="最多補幾筆標的的賣家（每筆 1 個請求）"),
    states: str = typer.Option("watching,bought", help="從哪些狀態的標的出發"),
    dry_run: bool = typer.Option(False, help="只列會做什麼，不打外網、不寫入"),
    mine: bool = typer.Option(True, help="拿到賣家後順便挖他的歷史成交（可挖的站台）"),
    sync_watch: bool = typer.Option(False, "--sync-watch", help="分數過門檻的自動進監控名單"),
    rank_limit: int = typer.Option(15, help="排行榜最多列幾個"),
):
    """**種子策略**：從觀察中／已買的標的挖出賣家 → 挖他們的歷史 → 排出 Seller Alpha。

    使用者按過「觀察」的標的是**人工審核過的正面樣本**，這些商品的賣家先驗
    遠高於「掃描剛好撈到誰」。所以這條路是：補賣家 → 挖歷史 → 評分 → 排行榜。

    每筆標的一個請求（Mercari 的 Buyee 鏡像頁還要一顆 WAF token），所以有
    `--limit`；冪等（只補 `seller_id IS NULL` 的觀測列），可以放心重跑。
    """
    from .comps import CompsEngine
    from .history import SELLER_HISTORY_SOURCE, SELLER_HISTORY_UNSUPPORTED, mine_paypay_seller
    from .seller_seed import backfill_signal_sellers, seed_targets
    from .sources import CachedFetcher, build_sources

    cfg = load_config()
    store = Store(cfg.db_path)
    want_states = tuple(s.strip() for s in states.split(",") if s.strip())
    targets = seed_targets(store, states=want_states, limit=limit)
    console.print(
        f"[bold]種子[/bold]：狀態 {list(want_states)} 且觀測列還沒有賣家的標的 "
        f"{len(targets)} 筆（上限 {limit}）"
    )
    if not targets:
        console.print("[green]沒有需要補賣家的標的——這批的賣家都已經在帳本上了。[/green]")

    fetcher = CachedFetcher(cfg)
    registry = build_sources(cfg, fetcher)
    waf = getattr(registry.get("buyee_mercari"), "fetcher", None)
    fill_report = None
    try:
        if targets:
            fill_report = backfill_signal_sellers(
                cfg=cfg, store=store, targets=targets,
                fetcher=fetcher, waf=waf, ebay=registry.get("ebay"), dry_run=dry_run,
            )
            t = Table(title=f"補賣家（{'dry-run' if dry_run else '已執行'}）")
            for col in ("key", "站", "賣家", "名稱", "結果"):
                t.add_column(col, overflow="fold")
            for f in fill_report.fills:
                t.add_row(
                    f.key, f.site, f.seller_id or "—", f.seller_name or "—", f.note,
                )
            console.print(t)
            console.print(
                f"[green]請求 {fill_report.requests} 個 → 解出賣家 "
                f"{fill_report.resolved}／{len(fill_report.fills)} 筆，"
                f"寫入 {fill_report.written} 列[/green]"
            )

        # --- 這批標的涉及的賣家（含先前已知的）-------------------------
        obs = store.listing_obs(limit=50000)
        obs_by_key = {r["key"]: r for r in obs}
        seed_keys = set()
        for state in want_states:
            for row in store.list_signals(state=state, limit=limit * 10):
                o = obs_by_key.get(row["key"])
                if o and o.get("seller_id"):
                    seed_keys.add(f"{o['site']}:{o['seller_id']}")
        console.print(f"[bold]種子賣家[/bold]：{len(seed_keys)} 個")

        if mine and not dry_run:
            engine = CompsEngine(cfg, FxRates(cfg), store)
            outcomes = []
            for key in sorted(seed_keys):
                site, _, sid = key.partition(":")
                src_name = SELLER_HISTORY_SOURCE.get(site)
                if src_name is None:
                    continue
                outcomes.append(
                    mine_paypay_seller(comps=engine, source=registry[src_name], seller_id=sid)
                )
            if outcomes:
                _print_seller_mine(outcomes, dry_run=False)
            unsupported = sorted({k.split(":")[0] for k in seed_keys} - set(SELLER_HISTORY_SOURCE))
            for s in unsupported:
                console.print(
                    f"[dim]  {s} 的賣家挖不了歷史："
                    f"{SELLER_HISTORY_UNSUPPORTED.get(s, '尚未實作')}[/dim]"
                )
    finally:
        if waf is not None and waf is not fetcher:
            waf.close()
        fetcher.close()

    if dry_run:
        console.print("[yellow]dry-run：未打外網、未寫入。[/yellow]")
        return

    # --- 評分：走既有的 analyze，不另開一條評估路徑 --------------------
    rep = _alpha_report(cfg, store)
    _print_alpha_coverage(rep)
    ranked = [(s, m) for s, m in rep.ranked() if s.seller_key in seed_keys][:rank_limit]
    t = Table(title=f"種子賣家的 Seller Alpha（{len(ranked)} 個達門檻）")
    for col, just in (
        ("seller_key", "left"), ("分數", "right"), ("可比", "right"), ("卡", "right"),
        ("同儕比", "right"), ("成交/在架", "right"), ("跨度(天)", "right"),
    ):
        t.add_column(col, justify=just)
    for score, m in ranked:
        t.add_row(
            score.seller_key, f"{score.total:.1f}", str(m.n_comparable),
            str(m.n_distinct_cards), f"{m.discount_ratio_median:.3f}×",
            f"{m.n_sold}/{m.n_ask}", f"{m.observation_span_days:.0f}",
        )
    console.print(t)
    if not ranked:
        console.print(
            "[yellow]種子賣家目前沒有一個達到證據門檻——這是誠實的答案。"
            "缺的是同款標的的其他賣家進到帳本（同儕），不是缺這些賣家自己的資料。[/yellow]"
        )
    rejected = [(s, m) for s, m in rep.rejected() if s.seller_key in seed_keys][:rank_limit]
    for score, m in rejected:
        console.print(
            f"[dim]  {score.seller_key}：觀測 {m.n_rows} 列（成交 {m.n_sold}／在架 {m.n_ask}）、"
            f"可比 {m.n_comparable} 筆 → "
            + (score.missing[0].split("——")[0] if score.missing else score.reason) + "[/dim]"
        )
    if sync_watch:
        from .seller_watch import WatchParams, sync_auto_watch

        res = sync_auto_watch(store, rep, WatchParams.from_config(cfg))
        console.print(
            f"[bold]自動入選[/bold]（門檻 {res['threshold']:g} 分）："
            f"新加 {len(res['added'])}、已在名單 {res['already']}、擋下 {len(res['rejected'])}"
        )
        for a in res["added"]:
            console.print(f"  [green]+ {a['seller_key']}[/green]（{a['score']:.1f} 分）")


@app.command()
def refill_comps(
    dry_run: bool = typer.Option(False, help="只選卡並印出，不打外網、不記帳、不入庫"),
    limit: int = typer.Option(None, help="本輪最多回補幾張卡（覆寫 config 的 max_cards_per_run）"),
):
    """需求驅動的行情回補：對「競標標的等著、但庫裡同卡成交不足」的卡做針對性已售出查詢。

    廣撒式的 comps_queries 撈熱門屬性組合，冷門卡永遠輪不到；這裡從 signals
    裡的競標標的反推「誰缺行情」，用該卡的日文卡名去查 yahoo_closed（為主）
    與 paypay_direct／buyee_mercari 的 sold（為輔）。

    節流：每輪卡數上限（config refill.max_cards_per_run）、每卡每來源 1 頁、
    同卡 7 天冷卻（查無結果也記帳——市場上沒有的卡，短期重查只是浪費）。
    掃描（`scan`／`daily`）每輪也會自動跑同一套；這個指令是手動觸發＋看帳用。
    """
    import dataclasses as _dc

    from .bidding import is_live_auction, listing_from_payload
    from .cards import CardIndex
    from .comps import CompsEngine
    from .refill import RefillParams, comps_count_by_card, run_refill
    from .sources import CachedFetcher, build_sources

    cfg = load_config()
    store = Store(cfg.db_path)
    index = CardIndex.load()
    if not index.available:
        console.print("[red]找不到卡片主檔，先跑 `ygo-sniper refresh-cards`[/red]")
        raise typer.Exit(1)

    params = RefillParams.from_config(cfg)
    if limit is not None:
        params = _dc.replace(params, max_cards_per_run=limit)

    # 需求端＝signals 裡的競標標的（與 recalc-bids 同一個母體：回補的整個
    # 目的就是讓這批標的拿得到出價上限，兩邊看同一群才對得上帳）。
    titles: list[str] = []
    for row in store.list_signals(state="all", limit=100000):
        try:
            payload = json.loads(row.get("payload") or "{}") or {}
            lst = listing_from_payload(payload.get("listing") or {})
        except (TypeError, ValueError, KeyError):
            continue
        if is_live_auction(lst):
            titles.append(lst.title)
    console.print(f"[dim]競標標的 {len(titles)} 筆（signals），回補來源 {list(params.sources)}[/dim]")

    fx = FxRates(cfg)
    fetcher = CachedFetcher(cfg)
    registry = build_sources(cfg, fetcher)
    comps_engine = CompsEngine(cfg, fx, store)
    try:
        report = run_refill(
            store=store, sources=registry, comps=comps_engine, index=index,
            titles=titles, params=params, dry_run=dry_run,
        )
    finally:
        waf = getattr(registry.get("buyee_mercari"), "fetcher", None)
        if waf is not None and waf is not fetcher:
            waf.close()
        fetcher.close()

    if report.skipped_cooldown:
        names = "、".join(c.card_name for c in report.skipped_cooldown)
        console.print(
            f"[dim]冷卻中跳過 {len(report.skipped_cooldown)} 張"
            f"（{params.cooldown_days:g} 天內查過）：{names}[/dim]"
        )
    if not report.selected:
        console.print("[green]沒有需要回補的卡（缺樣本的都在冷卻中，或全部樣本充足）。[/green]")
        return

    after = (
        {} if dry_run
        else comps_count_by_card(store.comps_by(limit=100000), index)
    )
    t = Table(title=f"需求驅動回補（{'dry-run，只選卡' if dry_run else '已執行'}）")
    for col in ("卡名", "等待的競標", "回補前 comps", "查得", "入庫", "回補後 comps", "記帳"):
        t.add_column(col, justify="left" if col == "卡名" else "right")
    for c in report.selected:
        per = report.per_card.get(c.card_name) or {}
        t.add_row(
            c.card_name, str(c.listings_n), str(c.comps_n),
            "—" if dry_run else str(per.get("found", 0)),
            "—" if dry_run else str(per.get("kept", 0)),
            "—" if dry_run else str(after.get(c.card_name, 0)),
            "—" if dry_run else ("是" if per.get("observed") else "[red]否（來源全滅，下輪重試）[/red]"),
        )
    console.print(t)

    if dry_run:
        console.print("[yellow]dry-run：未打外網、未記帳。拿掉 --dry-run 才會真的查。[/yellow]")
        return
    console.print(f"[green]{report.summary()}[/green]｜耗時 {report.elapsed_seconds:.0f}s")
    for err in report.errors:
        console.print(f"[yellow]· {err}[/yellow]")
    still_zero = [
        c.card_name for c in report.selected
        if (report.per_card.get(c.card_name) or {}).get("observed")
        and after.get(c.card_name, 0) == 0
    ]
    if still_zero:
        console.print(
            f"[dim]回補後仍 0 筆（{len(still_zero)} 張）：{'、'.join(still_zero)}\n"
            "這些卡的結論是「無行情可依據」——來源都看過了、市場上就是沒有近期"
            "成交，不是「再多查幾次」能解的（7 天冷卻正是為此存在）。[/dim]"
        )


@app.command()
def backfill_sold_at():
    """回填 comps 的 `sold_at_is_ingest` 標記。**冪等**，可以重複跑。

    判準是確定性的、不是猜的：入庫時間由 `datetime.now(UTC).isoformat()` 寫入，
    一律帶微秒；真實成交時間出自各站的 endTime，一律是秒精度。
    只寫還沒有標記（NULL）的列，已經標過的一律不碰。
    """
    cfg = load_config()
    store = Store(cfg.db_path)
    # Store.__init__ 開機時就會跑一次，所以這裡通常回 0——那正是冪等的證據
    changed = store.backfill_sold_at_provenance()
    console.print(f"[green]回填 {changed} 列[/green]（0 代表先前已標記完，冪等）")
    _print_sold_at_provenance(store)


@app.command()
def backfill_sale_kind(
    dry_run: bool = typer.Option(False, help="只算不寫"),
):
    """回填 comps 的 `sale_kind`（競標結標／定價成交／型態未知）。**冪等**。

    為什麼要分：ヤフオク落札價反映**買家搶到多高**（賣家只設了開始価格），
    フリマ／Mercari／一口價即決反映**賣家開多少**。Seller Alpha 問的是後者，
    兩者混池量到的是熱度不是定價行為，而且方向永遠是「這個賣家好便宜」。

    證據只有兩種，**沒有猜的**：`data/cache` 的 closedsearch 快照
    （`auctionId → isFixedPrice`，走生產解析路徑）＋平台事實（Mercari 與
    Yahoo!フリマ 沒有競標機制）。查不到證據的一律 `unknown`，而 `unknown`
    不進任何同儕比較——寧可少幾筆可比，不要拿落札價當賣家的定價。
    """
    from .comps import backfill_sale_kind as _backfill
    from .sources.yahoo_closed import sale_flags_from_cache

    cfg = load_config()
    store = Store(cfg.db_path)
    flags = sale_flags_from_cache(cfg.cache_dir)
    console.print(
        f"[dim]快取證據：{len(flags)} 個商品 ID（來源 {cfg.cache_dir}）[/dim]"
    )
    rep = _backfill(store, flags, dry_run=dry_run)
    tag = "[yellow]dry-run，未寫入[/yellow] " if dry_run else ""
    console.print(
        f"{tag}comps {rep['rows']} 列：寫入 {rep['updated']}、"
        f"沿用既有值 {rep['unchanged']}（其中 {rep['with_evidence']} 列有逐筆證據）"
    )
    t = Table(title="回填後的成交型態（逐站）")
    for col in ("site", "sale_kind", "筆數"):
        t.add_column(col, justify="right" if col == "筆數" else "left")
    for (site, kind), n in sorted(rep["by_site_kind"].items()):
        t.add_row(site, f"{kind}（{SALE_KIND_LABEL.get(kind, kind)}）", str(n))
    console.print(t)
    unknown = sum(n for (_s, k), n in rep["by_site_kind"].items() if k == SALE_UNKNOWN)
    if unknown:
        console.print(
            f"[yellow]⚠️ {unknown} 筆查不出型態——它們不進同儕比較（證據不足就不比，"
            "不是當成定價）。快取被清掉的舊成交補不回來是正常的。[/yellow]"
        )


@app.command()
def dedupe_comps(
    dry_run: bool = typer.Option(False, help="只算不寫，也不受先前的標記影響"),
):
    """把 comps 裡「同一實體商品跨 Yahoo 家族同時出品」的重複成交標記出來。

    日本賣家可以把同一件實體商品同時掛在ヤフオク!（buyee_yahoo）與
    Yahoo!フリマ／PayPay フリマ（buyee_paypay）——賣掉一邊，另一邊自動下架。
    後果：同一筆實體成交在 comps 出現兩次，那張卡的同儕中位數被自己汙染
    （同一個價格算了兩票）。

    判準很嚴（寧可漏抓，不可誤殺真成交，見 CLAUDE.md 第一節）：兩邊分屬
    buyee_yahoo／buyee_paypay（跨公司一律不判）、原幣金額完全相同、
    成交時間相差 60 分鐘內、標題正規化後逐字相同、已解析的卡片屬性不衝突。
    每一組都印出來——沒有這張表就等於靜默過濾。

    **只標記（`comps.dup_of_id`），不刪除任何列**。留 buyee_yahoo、標記
    buyee_paypay 那一側：ヤフオク的 `sold_at` 100% 是真實成交時間。
    """
    from .comps import mark_dual_listing_duplicates

    cfg = load_config()
    store = Store(cfg.db_path)
    rep = mark_dual_listing_duplicates(store, dry_run=dry_run)
    tag = "[yellow]dry-run，未寫入[/yellow] " if dry_run else ""
    console.print(
        f"{tag}comps {rep['rows']} 列（{rep['candidates']} 列尚未標記、"
        f"參與這次偵測）：判出 {len(rep['matches'])} 組同時出品重複"
    )
    if not rep["matches"]:
        console.print("[dim]沒有找到符合判準的重複組——量很小是正常的，"
                       "判準本來就設計成寧可漏抓。[/dim]")
        return
    t = Table(title="同時出品重複（留 buyee_yahoo，標記 buyee_paypay）")
    for col in ("keep_id", "dup_id", "price_native", "delta_min", "sold_at (keep／dup)", "title"):
        t.add_column(col)
    for m in rep["matches"]:
        t.add_row(
            str(m["keep_id"]), str(m["dup_id"]),
            f"{m['price_native']:.0f} {m['currency']}",
            f"{m['delta_minutes']:.1f}",
            f"{m['keep_sold_at']}\n{m['dup_sold_at']}",
            m["title"][:60],
        )
    console.print(t)


@app.command()
def backfill_sellers(
    dry_run: bool = typer.Option(False, help="只算不寫"),
):
    """把 signals payload 裡既有的 seller_id 回填進 listing_obs（Seller Alpha）。

    歷史脈絡：eBay 的 seller_id 從第一天就在 payload 裡，但 listing_obs 落帳
    時沒帶。**冪等**：只寫 seller_id 還是 NULL 的列，第二次跑 updated 必為 0。
    回填後 sellers 表對觸到的賣家重算聚合（不把 last_seen 蓋成 now）。
    """
    cfg = load_config()
    store = Store(cfg.db_path)
    r = store.backfill_seller_ids(dry_run=dry_run)
    tag = "[yellow]dry-run，未寫入[/yellow] " if dry_run else ""
    console.print(
        f"{tag}payload 有 seller 的訊號 {r['payload_with_seller']} 筆："
        f"回填 {r['updated']}、已有值 {r['already_set']}、"
        f"無對應觀測列 {r['no_obs_row']}"
    )


@app.command()
def sellers(
    site: str = typer.Option(None, help="只看這個站（如 ebay / buyee_paypay）"),
    limit: int = typer.Option(20, help="最多幾列"),
    rank: bool = typer.Option(False, "--rank", help="Seller Alpha 排行榜（同儕相對折價）"),
    show_rejected: bool = typer.Option(True, help="--rank 時也列出未達門檻的賣家"),
    sync_watch: bool = typer.Option(
        False, "--sync-watch", help="把分數過門檻的賣家自動加進監控名單（只加不刪）"
    ),
    supply: bool = typer.Option(
        False, "--supply", help="供給契合度排行榜（值不值得盯，與 Alpha 是兩件事）"
    ),
):
    """賣家帳本（Seller Alpha 地基）。計數是從 listing_obs／comps 重算的聚合。

    `--rank` 走 `seller_alpha.analyze`：主指標是**同儕相對折價**
    （同站×同卡×同版次×同分數×同價格基準的其他賣家中位），不是模型公允價。

    `--supply` 走 `seller_supply.supply_fit_all`：回答「值不值得盯」，
    跟便不便宜無關，**不可與 Alpha 相加**（見 seller_supply.py 頂註）。
    """
    cfg = load_config()
    store = Store(cfg.db_path)
    if supply:
        _print_supply_fit(cfg, store, site=site, limit=limit)
        return
    if rank:
        _print_seller_rank(
            cfg, store, site=site, limit=limit,
            show_rejected=show_rejected, sync_watch=sync_watch,
        )
        return
    rows = store.list_sellers(site=site, limit=limit)
    if not rows:
        console.print("[dim]sellers 表是空的——先跑一輪 scan 或 backfill-sellers。[/dim]")
        return
    table = Table(title=f"賣家帳本（{len(rows)} 列）")
    for col in ("seller_key", "在架觀測", "成交", "feedback", "好評%", "首見", "最後活躍"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            _seller_cell(r["seller_key"]),
            str(r["listing_count"]),
            str(r["sold_count"]),
            str(r["feedback_score"] if r["feedback_score"] is not None else "—"),
            str(r["feedback_pct"] if r["feedback_pct"] is not None else "—"),
            (r["first_seen"] or "")[:10],
            (r["last_seen"] or "")[:10],
        )
    console.print(table)


def _alpha_report(cfg, store, *, with_model: bool = True):
    """跑一次 Seller Alpha 全量分析。模型輔助欄位建不起來就降級（不中斷）。"""
    from .cards import CardIndex
    from .seller_alpha import analyze

    index = CardIndex.load()
    valuator = None
    if with_model:
        try:
            from .valuation import build_valuator

            valuator = build_valuator(cfg, store, index)
        except Exception as exc:  # noqa: BLE001 - 輔助欄位而已
            console.print(f"[yellow]模型輔助欄位建不起來（{exc}）——只出同儕相對指標[/yellow]")
    return analyze(store, cfg=cfg, index=index, valuator=valuator)


def _print_alpha_coverage(rep) -> None:
    """覆蓋率先印。**沒有覆蓋率的排行榜是誤導**：使用者要先知道這是幾筆撐的。"""
    c = rep.coverage
    console.print(
        f"[bold]同儕可比覆蓋率[/bold]：有賣家的市場列 {c['rows_with_seller']} 筆中，"
        f"{c['rows_scoring_basis']} 筆屬可計分基準（定價／成交價；"
        f"另 {c['rows_bid_excluded']} 筆競標中出價刻意排除），"
        f"其中 [bold]{c['comparable_items']}[/bold] 筆算得出同儕折價"
        f"（{c['comparable_rate'] * 100:.1f}%）｜層級 {c['tier_counts'] or '—'}"
        f"｜基準 {c['basis_counts'] or '—'}"
    )
    console.print(
        f"[dim]卡名比對不到 {c['rows_no_card_name']} 筆；另有 {c['stratum_only_items']} 筆"
        "只比得到「同稀有度×同分數」層級——那一層量到的是卡種組合不是賣家定價，"
        "一律不計分。[/dim]"
    )
    # **成交型態要單獨講一句**：可比數會因為分池而下降，不講清楚的話
    # 「修好了」與「壞了」在畫面上長得一模一樣（本專案第五節）。
    kinds = c.get("comparable_sale_kind") or {}
    unknown_rows = c.get("rows_sale_kind_unknown", 0)
    console.print(
        f"[bold]成交型態[/bold]（同型態才互比）：可比的 {c['comparable_items']} 筆裡，"
        f"競標結標 [bold]{kinds.get(SALE_AUCTION, 0)}[/bold] 筆／"
        f"定價（含在架定價與定價成交）[bold]{kinds.get(SALE_FIXED, 0)}[/bold] 筆。"
    )
    console.print(
        "[dim]  為什麼分開：**競標結標價反映買家搶到多高，不是賣家開多低**"
        "（賣家只設了開始価格，最終那個數字是市場喊出來的）。"
        "拿它跟一口價／フリマ定價混在一起比，量到的是熱度不是定價行為。[/dim]"
    )
    if unknown_rows:
        console.print(
            f"[yellow]  ⚠️ 另有 {unknown_rows} 筆成交查不出型態，完全不進比較"
            "（證據不足就不比）。補證據：`ygo-sniper backfill-sale-kind`[/yellow]"
        )
    by_site = c.get("sold_sale_kind_by_site") or {}
    if by_site:
        console.print(
            "[dim]  成交列逐站×型態："
            + "、".join(f"{k}×{v}" for k, v in sorted(by_site.items()))
            + "[/dim]"
        )
    console.print(
        f"[bold]賣家[/bold]：{c['sellers_total']} 個有觀測，"
        f"[green]{c['sellers_scored']}[/green] 個達門檻"
        f"（可比 ≥{c['min_comparable']} 筆且 ≥{c['min_distinct_cards']} 張相異卡）、"
        f"[yellow]{c['sellers_insufficient']}[/yellow] 個證據不足"
    )
    # **兩種跨度分開報**：在架帳說的是「我們盯了多久」，成交帳說的是「平台
    # 留著的歷史有多長」。取大值當一個數字報就是混源（工程原則 1）。
    ask_span = c.get("listing_obs_span_days", 0.0)
    sold_span = c.get("comps_span_days", 0.0)
    floor = c.get("persistence_min_days", 14.0)
    persistent = c.get("sellers_persistent", 0)
    console.print(
        f"[bold]觀測跨度[/bold]：在架帳 {ask_span:.1f} 天（我們盯了多久）｜"
        f"成交帳 {sold_span:.1f} 天（平台歷史成交，真實成交時間）"
    )
    if persistent:
        console.print(
            f"[green]✅ 持續性判定得出來的賣家：{persistent}／{c['sellers_scored']} 個"
            f"（個人觀測跨度 ≥{floor:g} 天）[/green]"
        )
    else:
        console.print(
            f"[yellow]⚠️ 沒有任何達門檻的賣家跨度 ≥{floor:g} 天——「持續性」目前判定不了。"
            "缺的是歷史成交（`ygo-sniper mine-history` / `mine-seller-history`）。[/yellow]"
        )
    if ask_span < floor:
        console.print(
            f"[dim]  在架帳本本身只有 {ask_span:.1f} 天，所以**只有在架價（ask basis）的賣家**"
            "（eBay 全部屬此）仍然談不了持續性——那條路沒有歷史成交可挖"
            "（Marketplace Insights 403），只能等帳本累積。[/dim]"
        )


def _print_seller_rank(cfg, store, *, site, limit, show_rejected, sync_watch=False) -> None:
    rep = _alpha_report(cfg, store)
    if not rep.metrics:
        console.print("[dim]沒有任何帶賣家的市場列——先跑 scan 或 backfill-sellers。[/dim]")
        return
    _print_alpha_coverage(rep)
    if sync_watch:
        from .seller_watch import WatchParams, sync_auto_watch

        params = WatchParams.from_config(cfg)
        rep_sync = sync_auto_watch(store, rep, params)
        console.print(
            f"[bold]自動入選[/bold]（門檻 {rep_sync['threshold']:g} 分）："
            f"新加 {len(rep_sync['added'])} 個、已在名單 {rep_sync['already']} 個、"
            f"擋下 {len(rep_sync['rejected'])} 個"
        )
        for a in rep_sync["added"]:
            console.print(
                f"  [green]+ {a['seller_key']}[/green]（{a['score']:.1f} 分，批次 {a['batch']}）"
                + (f"，淘汰 {a['evicted']}" if a.get("evicted") else "")
            )
        for r in rep_sync["rejected"]:
            console.print(f"  [yellow]✗ {r['seller_key']}：{r['reason']}[/yellow]")

    #: 逐項貢獻的短代號。排行榜一列放不下整句依據，但**裸分數不准輸出**，
    #: 所以這裡至少把四項各自的貢獻攤開；完整依據在 `ygo-sniper seller <key>`。
    abbrev = {"depth": "深", "consistency": "一致", "breadth": "持續", "risk": "風險"}
    ranked = rep.ranked(site=site)[:limit]
    t = Table(title=f"Seller Alpha 排行榜（{len(ranked)} 個達門檻）")
    for col, just in (
        ("seller_key", "left"), ("分數", "right"), ("可比", "right"),
        ("卡", "right"), ("同儕比", "right"), ("P25/P75", "right"),
        ("層", "left"), ("逐項貢獻（深/一致/持續/風險）", "left"),
    ):
        t.add_column(col, justify=just, no_wrap=(col != "逐項貢獻（深/一致/持續/風險）"))
    for score, m in ranked:
        contrib = " ".join(
            f"{abbrev.get(c.name, c.name)}{c.points:+.1f}" for c in score.components
        )
        t.add_row(
            _seller_cell(score.seller_key), f"{score.total:.1f}", str(m.n_comparable),
            str(m.n_distinct_cards), f"{m.discount_ratio_median:.3f}×",
            f"{m.discount_ratio_p25:.2f}/{m.discount_ratio_p75:.2f}",
            "+".join(sorted(set(m.tier_counts) & {"T1", "T2"})) or "—", contrib,
        )
    console.print(t)
    for score, _m in ranked:
        console.print(f"[dim]  {_seller_cell(score.seller_key)}：{score.reason}[/dim]")
        for cv in score.caveats:
            console.print(f"[yellow]     {cv}[/yellow]")
    if not ranked:
        console.print("[yellow]目前沒有任何賣家達到門檻——這是誠實的答案，不是壞掉。[/yellow]")

    if show_rejected:
        rej = rep.rejected(site=site)[:limit]
        console.print(
            f"\n[bold]未達門檻[/bold]（列出 {len(rej)} / "
            f"{rep.coverage['sellers_insufficient']} 個，缺什麼寫在後面）"
        )
        for score, m in rej:
            console.print(
                f"  {_seller_cell(score.seller_key)}：觀測 {m.n_rows} 列、可比 {m.n_comparable} 筆／"
                f"{m.n_distinct_cards} 張相異卡 → "
                + (score.missing[0].split("——")[0] if score.missing else score.reason)
            )
        if rej:
            console.print(f"[dim]  缺什麼（共通）：{rej[0][0].missing[0].split('——')[-1]}[/dim]")


def _fmt_alpha_total(score) -> str:
    """Supply Fit 排行榜的 Alpha 欄：`ok=False`／沒有分數一律「證據不足」。

    **絕不可以顯示成 `0.0`**——0 分的語意是「算出來就是比同儕貴」，
    證據不足的語意是「湊不到同儕，不知道」，兩者顯示成同一個東西
    會讓使用者把「不知道」讀成「比同儕貴」，方向剛好相反。
    """
    if score is None or not score.ok or score.total is None:
        return "證據不足"
    return f"{score.total:.1f}"


def _supply_dim_raw(fit, name: str) -> float | None:
    """取 `SupplyFit.dimensions` 裡某個維度的 `raw`，不可得回 None。"""
    for d in fit.dimensions:
        if d.name == name and d.available and d.raw is not None:
            return d.raw
    return None


def _print_supply_fit(cfg, store, *, site, limit) -> None:
    from .seller_supply import SupplyParams, supply_fit_all

    rep = _alpha_report(cfg, store, with_model=False)
    if not rep.metrics:
        console.print("[dim]沒有任何帶賣家的市場列——先跑 scan 或 backfill-sellers。[/dim]")
        return

    metrics = list(rep.metrics.values())
    if site:
        metrics = [m for m in metrics if m.site == site]
    if not metrics:
        console.print(f"[dim]站 {site} 沒有任何帶賣家的市場列。[/dim]")
        return

    fits = supply_fit_all(metrics, params=SupplyParams())
    scores = rep.scores

    console.print(
        "[dim]供給契合度回答「值不值得盯」，不是「便不便宜」——"
        "這兩欄是兩把不同的尺，不可相加。[/dim]"
    )

    ok_fits = [f for f in fits.values() if f.ok]
    n_alpha_ok = sum(1 for k in fits if scores.get(k) is not None and scores[k].ok)
    console.print(
        f"[bold]{len(ok_fits)}/{len(fits)}[/bold] 個賣家算得出供給契合度"
        f"（對照：Alpha 只有 {n_alpha_ok} 個）"
    )

    ranked = sorted(ok_fits, key=lambda f: (-(f.total or 0.0), f.seller_key))[:limit]

    if not ranked:
        console.print("[yellow]目前沒有任何賣家達到供給契合度門檻。[/yellow]")
    else:
        t = Table(title=f"供給契合度排行榜（{len(ranked)} 個達門檻）")
        for col, just in (
            ("seller_key", "left"), ("供給分", "right"), ("維度", "right"),
            ("深度", "right"), ("跨度", "right"), ("8-9分", "right"),
            ("系列", "right"), ("Alpha", "right"),
        ):
            t.add_column(col, justify=just)
        for fit in ranked:
            depth = _supply_dim_raw(fit, "supply_depth")
            span = _supply_dim_raw(fit, "persistence")
            grade = _supply_dim_raw(fit, "grade_profile")
            series = _supply_dim_raw(fit, "series_focus")
            t.add_row(
                _seller_cell(fit.seller_key),
                f"{fit.total:.1f}",
                f"{fit.n_dimensions_used}/{fit.n_dimensions_total}",
                f"{depth:.0f}" if depth is not None else "—",
                f"{span:.0f}天" if span is not None else "—",
                f"{grade:.0%}" if grade is not None else "—",
                f"{series:.0%}" if series is not None else "—",
                _fmt_alpha_total(scores.get(fit.seller_key)),
            )
        console.print(t)

        n_caveat_rows = min(5, limit)
        for fit in ranked[:n_caveat_rows]:
            if fit.caveats:
                console.print(f"[dim]  {_seller_cell(fit.seller_key)}：[/dim]")
                for cv in fit.caveats:
                    console.print(f"[yellow]    {cv}[/yellow]")

    rejected = [f for f in fits.values() if not f.ok]
    if rejected:
        reason_counts = Counter(f.reason for f in rejected)
        top_reason, top_count = reason_counts.most_common(1)[0]
        console.print(
            f"\n[dim]未達門檻 {len(rejected)} 個賣家，最常見的原因"
            f"（{top_count} 個）：{top_reason}[/dim]"
        )


@app.command()
def seller(
    key: str = typer.Argument(..., help="賣家鍵，如 ebay:psa"),
    items: int = typer.Option(20, help="最多列出幾筆標的"),
    peers: int = typer.Option(3, help="每筆標的秀幾個同儕來源"),
):
    """單一賣家 drill-down：逐筆標的、同儕折價、同儕來源、分數逐項貢獻。"""
    from .seller_alpha import BASIS_LABEL, TIER_LABEL

    cfg = load_config()
    store = Store(cfg.db_path)
    rep = _alpha_report(cfg, store)
    m = rep.metrics.get(key)
    if m is None:
        console.print(f"[red]沒有這個賣家：{key}[/red]")
        near = [k for k in rep.metrics if key.lower() in k.lower()][:8]
        if near:
            console.print("[dim]你是不是要找：" + "、".join(near) + "[/dim]")
        raise typer.Exit(1)
    score = rep.scores[key]

    console.print(f"\n[bold]{_seller_cell(key)}[/bold]（{m.site}）")
    if score.ok:
        console.print(f"[bold green]Seller Alpha {score.total:.1f} / 100[/bold green]  {score.reason}")
    else:
        console.print(f"[bold yellow]不給分數[/bold yellow]\n{score.reason}")
    for cv in score.caveats:
        console.print(f"[yellow]{cv}[/yellow]")

    t = Table(title="分數逐項貢獻（絕不輸出裸數字）")
    for col in ("項目", "得分", "上限", "依據"):
        t.add_column(col, overflow="fold")
    for c in score.components:
        t.add_row(c.label, f"{c.points:+.1f}", f"{c.max_points:g}", c.detail)
    console.print(t)

    console.print(
        f"\n[bold]樣本[/bold]：觀測列 {m.n_rows}（定價 {m.n_ask}、成交 {m.n_sold}、"
        f"競標中出價 {m.n_bid_excluded} 不入指標）｜可比 {m.n_comparable} 筆／"
        f"{m.n_distinct_cards} 張相異卡｜層級 "
        + "、".join(f"{TIER_LABEL[t_]}×{n}" for t_, n in sorted(m.tier_counts.items()))
    )
    console.print(
        f"[bold]同儕來源賣家[/bold]：{m.peer_seller_pool} 個"
        + (f"（最大占比 {m.peer_seller_top_share * 100:.0f}%）{m.peer_seller_mix}"
           if m.peer_seller_top_share is not None else "")
    )
    console.print(f"[bold]分數分布[/bold]：{m.grade_mix or '—'}")
    console.print(
        "[bold]系列集中度[/bold]："
        + (
            f"top1 {m.series_top1_share * 100:.0f}%、Herfindahl {m.series_herfindahl:.2f}"
            f"（{m.series_known_n}/{m.n_rows} 筆抓得到卡號）{list(m.series_mix.items())[:5]}"
            if m.series_herfindahl is not None else "卡號抓不到，無法判定"
        )
    )
    console.print(f"[bold]刊登時段[/bold]：{m.listing_hour_hist or '—'}（UTC 小時）｜{m.persistence_note}")
    console.print(f"[bold]售出率[/bold]：{m.sold_through_note}")
    console.print(
        f"[bold]風險[/bold]：feedback {m.feedback_score if m.feedback_score is not None else '—'}"
        f"／好評 {m.feedback_pct if m.feedback_pct is not None else '—'}%"
        + ("".join(f"｜{n}" for n in m.risk_notes))
    )
    if m.model_ratio_median is not None:
        console.print(
            f"[dim][輔助・不進分數] 模型絕對法中位 {m.model_ratio_median:.3f}×"
            f"（{m.model_n} 筆）——承受模型分段偏誤（實測 L3×Mercari 高估 5.9 倍、"
            "comps 表沒有任何 eBay 成交所以 eBay 平台係數估不出來），只供對照。[/dim]"
        )

    # 計分的先列（那才是分數的依據），再列只比得到不計分層級的，最後是沒同儕的。
    shown = sorted(
        m.items, key=lambda i: (not i.scoring, i.peer is None, i.ratio or 0)
    )[:items]
    t2 = Table(title=f"逐筆標的（{len(shown)}/{len(m.items)}）")
    for col in ("標題", "基準", "價 NT$", "同儕中位", "折價", "層級", "同儕 n", "模型比[輔助]"):
        t2.add_column(col, overflow="fold")
    for i in shown:
        t2.add_row(
            (i.row.title or "")[:44],
            BASIS_LABEL.get(i.row.basis, i.row.basis),
            f"{i.row.price_twd:,.0f}",
            f"{i.peer.peer_median_twd:,.0f}" if i.peer else "—",
            f"{i.discount_pct:+.0f}%" if i.discount_pct is not None else "—",
            (i.peer.tier if i.peer else "—") + ("" if (i.peer and i.peer.scoring) else "（不計分）" if i.peer else ""),
            str(i.peer.peer_n) if i.peer else "0",
            f"{i.model_ratio:.2f}" if i.model_ratio else "—",
        )
    console.print(t2)

    console.print("\n[bold]同儕來源[/bold]（你跟誰比的）")
    for i in shown:
        if not (i.peer and i.peer.scoring):
            continue
        console.print(
            f"  [cyan]{(i.row.title or '')[:50]}[/cyan] NT${i.row.price_twd:,.0f}"
            f" → 同儕中位 NT${i.peer.peer_median_twd:,.0f}"
            f"（{i.peer.peer_n} 筆／{i.peer.peer_sellers} 個已知賣家"
            f"／{i.peer.peer_unknown_seller_n} 筆賣家未知）"
        )
        for p in i.peer.sources[:peers]:
            console.print(
                f"      · NT${p.price_twd:,.0f}  {(p.title or '')[:46]}"
                f"  [{p.source_table}／{BASIS_LABEL.get(p.basis, p.basis)}"
                f"／{p.seller_key or '賣家未知'}]"
            )


# ---------------------------------------------------------------------------
# 賣家監控名單（seller_watch）
# ---------------------------------------------------------------------------
watch_app = typer.Typer(
    add_completion=False,
    help="賣家監控名單：加入／移出／檢視（名單存 db，自動與手動共用同一張表）",
)
app.add_typer(watch_app, name="watch-seller")


def _watch_ctx():
    from .seller_watch import WatchParams

    cfg = load_config()
    return cfg, Store(cfg.db_path), WatchParams.from_config(cfg)


@watch_app.command("add")
def watch_seller_add(
    key: str = typer.Argument(..., help="賣家鍵，如 ebay:psa"),
    reason: str = typer.Option("", help="為什麼加它（會存進名單，之後回看用）"),
):
    """手動把一個賣家加進監控名單。**不受分數門檻限制**。

    自動門檻目前只選得出 1 個賣家（2026-08-04 實測：95 個有觀測的賣家裡
    只有 5 個過得了證據門檻，其中 4 個是「比同儕貴」），所以使用者自己
    觀察到的賣家要能先放進去追蹤。手動加入的賣家在清單上標 `manual`
    而且 **score 是空的**——不假裝它有分數。
    """
    from .seller_watch import SOURCE_MANUAL, add_watch

    _cfg, store, params = _watch_ctx()
    res = add_watch(
        store, key, source=SOURCE_MANUAL,
        reason=reason or "手動加入（使用者自己觀察到的賣家）", params=params,
    )
    if not res.ok:
        console.print(f"[red]沒有加入 {res.seller_key}[/red]：{res.reason}")
        raise typer.Exit(1)
    tone = "yellow" if res.already else "green"
    console.print(f"[{tone}]{res.seller_key}[/{tone}]：{res.reason}")


def _resolve_seller_key(arg: str, cfg) -> tuple[str, str | None]:
    """`pin`／`unpin` 的參數：URL（`http` 開頭）就解析，否則當現成的 site:id 鍵。

    回 `(seller_key, 店鋪 slug 或 None)`——slug 只在 eBay `/str/` 店鋪頁那條
    路上有值（那條路會連網把 slug 解析成真實帳號，見 `seller_resolve`），
    呼叫端拿它註明「這個鍵是從哪個店鋪頁解析來的」。
    解析失敗**直接紅字＋exit 1**（`SellerUrlError` 的訊息本身已列出支援形式），
    不回退、不猜——貼錯網址與釘選成功必須長得完全不一樣。
    """
    from .seller_links import SellerUrlError
    from .seller_resolve import resolve_seller_target

    try:
        return resolve_seller_target(arg, cfg)
    except SellerUrlError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _warn_if_unlistable(seller_key: str) -> None:
    """釘選成功但站台還掃不到時，**大聲**講清楚（不是安靜地讓它永遠 0 筆）。"""
    from .seller_watch import SELLER_PAGE_SOURCE, UNSUPPORTED_SITE_NOTE

    site = seller_key.partition(":")[0]
    if site in SELLER_PAGE_SOURCE:
        return
    note = UNSUPPORTED_SITE_NOTE.get(site, f"{site} 沒有賣家頁列舉實作")
    console.print(
        f"[bold yellow]⚠️ 已釘選，但此站尚無賣家頁列舉實作，暫時掃不到"
        f"（原因：{note}）。名單會留著，列舉實作上線後自動開始掃。[/bold yellow]"
    )


@watch_app.command("pin")
def watch_seller_pin(
    target: str = typer.Argument(
        ..., help="賣家頁 URL（http 開頭）或現成的賣家鍵（如 ebay:psa）"
    ),
    reason: str = typer.Option("", help="為什麼釘選（已釘選再 pin ＝ 更新這段備註）"),
):
    """釘選一個賣家：**不佔 30 名額、永不被自動淘汰、每個輪替時段都掃**。

    使用者明講要追蹤的賣家永遠優先於演算法選的——已在名單上（任何軌）的
    賣家再 pin 會直接升級成釘選。移除用 `watch-seller unpin`。
    """
    from .seller_watch import SOURCE_PINNED, add_watch

    _cfg, store, params = _watch_ctx()
    key, store_slug = _resolve_seller_key(target, _cfg)
    reason_text = reason or "使用者釘選（貼賣家頁 URL 要求長期追蹤）"
    if store_slug:
        # 店鋪頁是連網解析來的：把出處寫進 reason，日後看名單才知道
        # `ebay:merry_tcg` 這個鍵是從 /str/merrycorporation 來的。
        reason_text += f"（從店鋪頁 {store_slug} 解析）"
        console.print(f"店鋪頁 {store_slug} → 帳號 {key.partition(':')[2]}")
    res = add_watch(
        store, key, source=SOURCE_PINNED,
        reason=reason_text, params=params,
    )
    if not res.ok:
        console.print(f"[red]沒有釘選 {res.seller_key}[/red]：{res.reason}")
        raise typer.Exit(1)
    console.print(f"[green]📌 {res.seller_key}[/green]：{res.reason}")
    _warn_if_unlistable(res.seller_key)


@watch_app.command("unpin")
def watch_seller_unpin(
    target: str = typer.Argument(
        ..., help="賣家頁 URL（http 開頭）或賣家鍵（如 ebay:psa）"
    ),
):
    """解除釘選（軟刪除，歷史留著）。"""
    from .seller_watch import remove_watch

    _cfg, store, _params = _watch_ctx()
    key, _store_slug = _resolve_seller_key(target, _cfg)
    if remove_watch(store, key, reason="手動解除釘選"):
        console.print(f"[green]已解除釘選：{key}[/green]")
    else:
        console.print(f"[yellow]{key} 本來就不在（active）名單上[/yellow]")


@watch_app.command("remove")
def watch_seller_remove(
    key: str = typer.Argument(..., help="賣家鍵，如 ebay:psa"),
    reason: str = typer.Option("手動移出名單", help="移除原因（會留在名單歷史裡）"),
):
    """把賣家移出監控名單（軟刪除，歷史留著）。"""
    from .seller_watch import remove_watch

    _cfg, store, _params = _watch_ctx()
    if remove_watch(store, key, reason=reason):
        console.print(f"[green]已移出監控名單：{key}[/green]")
    else:
        console.print(f"[yellow]{key} 本來就不在（active）名單上[/yellow]")


@watch_app.command("list")
def watch_seller_list(
    all_rows: bool = typer.Option(False, "--all", help="連已移除的也列出來"),
):
    """看目前的監控名單（含批次、上次掃描時間與結果）。釘選列獨立成塊放最上面。"""
    from .seller_watch import (
        SELLER_PAGE_SOURCE,
        SOURCE_PINNED,
        UNSUPPORTED_SITE_NOTE,
        rotation_state,
    )

    _cfg, store, params = _watch_ctx()
    rows = store.list_seller_watch(active_only=not all_rows)
    active = [r for r in rows if r.get("active")]
    # 名額顯示不含 pinned（釘選不佔 30 名額——計數混進去，畫面上的 N/30
    # 就跟 add_watch 實際的上限判準不同源了）。
    pinned = [r for r in active if r.get("source") == SOURCE_PINNED]
    quota_used = [r for r in active if r.get("source") != SOURCE_PINNED]
    rows = [r for r in rows if not (r.get("active") and r.get("source") == SOURCE_PINNED)]
    st = rotation_state(store)
    console.print(
        f"[bold]監控名單[/bold] {len(quota_used)}/{params.max_sellers} 個"
        + (f"，另有釘選 {len(pinned)} 組（不佔名額）" if pinned else "")
        + f"（{params.batches} 批輪替、每批間隔 {params.batch_interval_minutes:.0f} 分"
        f" → 每賣家 {params.per_seller_interval_minutes:.0f} 分一次；"
        "釘選列每批都掃）"
        + ("" if params.enabled else "  [red]⚠️ 監控掃描已關閉（watch_enabled=false）[/red]")
    )
    if pinned:
        tp = Table(title=f"📌 釘選（不佔名額，{len(pinned)} 組）")
        for col in ("seller_key", "上次掃描", "上次結果", "理由", "站台狀態"):
            tp.add_column(col, overflow="fold")
        for r in pinned:
            site = str(r["seller_key"]).partition(":")[0]
            site_note = (
                "可掃" if site in SELLER_PAGE_SOURCE
                else f"⚠️ 掃不到：{UNSUPPORTED_SITE_NOTE.get(site, f'{site} 沒有賣家頁列舉實作')}"
            )
            tp.add_row(
                _seller_cell(r["seller_key"]),
                (r["last_scanned_at"] or "—")[:19],
                (r["last_result"] or "—")[:60],
                (r["reason"] or "")[:60],
                site_note,
            )
        console.print(tp)
    if st:
        console.print(
            f"[dim]上次輪替：第 {st.get('batch')} 批 @ {str(st.get('claimed_at'))[:19]}[/dim]"
        )
    if not rows:
        if not pinned:
            console.print(
                "[dim]名單是空的。`ygo-sniper watch-seller add <key>` 手動加、"
                "`ygo-sniper watch-seller pin <賣家頁URL>` 釘選，"
                "或跑一次 `ygo-sniper sellers --rank --sync-watch` 讓過門檻的自動入選。[/dim]"
            )
        return
    t = Table(title=f"seller_watch（{len(rows)} 列）")
    for col in ("seller_key", "來源", "分數", "批次", "上次掃描", "上次結果", "狀態", "理由"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(
            _seller_cell(r["seller_key"]),
            r["source"],
            # 手動加入的賣家**沒有分數**，這裡印「—」而不是 0：
            # 0 會被讀成「這個賣家很差」，事實是「還沒有證據」。
            "—" if r["score"] is None else f"{r['score']:.1f}",
            str(r["batch"]),
            (r["last_scanned_at"] or "—")[:19],
            (r["last_result"] or "—")[:60],
            "在名單" if r["active"] else "已移除",
            (r["reason"] or "")[:60],
        )
    console.print(t)


# ---------------------------------------------------------------------------
# 指定卡狙擊（card_watch）
# ---------------------------------------------------------------------------
snipe_app = typer.Typer(
    add_completion=False,
    help="指定卡狙擊：登錄特定卡（鑑定機構＋分數＋卡名＋卡號），出現就推播",
)
app.add_typer(snipe_app, name="snipe")

#: 可重複的 `--evidence`。**放模組層而不是簽章預設**：可變型別（`list`）的預設值
#: 會被所有呼叫共用（ruff B008 擋的就是這個）。預設 None，呼叫端 `or []` 收斂。
_EVIDENCE_OPTION = typer.Option(
    None, help="過去出現的商品頁 URL（可重複）；登錄當下抓快照存證"
)


def _tier_tag(tier: object) -> str:
    """`[exact]` 這種方括號在 rich 眼裡是**樣式標籤**，會被整段吃掉（實測輸出
    只剩空白）——tier 是 🎯/👀/near 的唯一依據，弄丟它就是把判定結果安靜地
    丟掉（CLAUDE.md 第五節）。逸出成字面方括號。"""
    return f"\\[{tier}]"


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
                console.print(
                    f"  ⚠️ {_tier_tag(e['status'])} {e['url']}（{e['note']}）"
                )
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
            bids = (f"／{s['bid_count']} 出價"
                    if s.get("bid_count") and s.get("sale_kind") == "auction" else "")
            console.print(
                f"  {mark} {when} {price_s:>12}（{kind}{bids}）"
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
            console.print(f"  {_tier_tag(h['tier'])} {date} {price_s}（{kind}）"
                          f"{str(h['title'])[:56]}")
    notable = [h for h in d.hits if h["tier"] in ("exact", "partial")]
    if notable:
        console.print("\n[bold]狙擊命中帳[/bold]")
        for h in notable:
            sent = "已推播" if h.get("sent_at") else "未推播"
            console.print(
                f"  {_tier_tag(h['tier'])} {str(h['last_seen'])[:16]} "
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
    evidence: list[str] | None = _EVIDENCE_OPTION,
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
                evidence_urls=list(evidence or []), note=note,
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


@app.command()
def watch_scan(
    dry_run: bool = typer.Option(
        False, help="只認領批次並印出「這一輪會掃誰」，不打外網、不落庫"
    ),
    force: bool = typer.Option(False, help="無視輪替節流（人工「我現在就要掃」）"),
):
    """跑一輪**賣家輪替監控**（不跑關鍵字查詢、不跑 canary）。

    正常情況下這一段是跟著 `ygo-sniper daily` 每小時自動跑的；這個指令是
    人工逃生門與實測用。抓回來的標的走**既有的完整管線**（parse_card →
    is_candidate → 估價 → listing_obs → scoring），不是另一條平行路徑。
    """
    from .seller_watch import WatchParams, claim_batch, due_sellers

    cfg = load_config()
    params = WatchParams.from_config(cfg)
    if dry_run:
        store = Store(cfg.db_path)
        batch, why = claim_batch(store, params, force=force)
        console.print(f"[bold]輪替[/bold]：{why}")
        if batch is None:
            return
        due, skipped = due_sellers(store, params, batch)
        t = Table(title=f"第 {batch} 批：{len(due)} 個要掃、{len(skipped)} 個跳過")
        for col in ("seller_key", "來源", "動作"):
            t.add_column(col, overflow="fold")
        for r in due:
            t.add_row(r["seller_key"], r["source"], "掃（1 個請求）")
        for r, reason in skipped:
            t.add_row(r["seller_key"], r["source"], f"[yellow]跳過：{reason}[/yellow]")
        console.print(t)
        console.print(f"[dim]本輪預估對外請求：{len(due)} 個（每賣家 {params.pages} 頁）[/dim]")
        return

    pipe = Pipeline(cfg)
    try:
        result = pipe.scan(watch_only=True, watch_force=force, trigger="watch-scan")
    finally:
        pipe.close()
    w = result.get("seller_watch") or {}
    console.print(
        f"[bold]第 {w.get('batch')} 批[/bold]（{w.get('reason')}）："
        f"掃 {len(w.get('sellers') or [])} 個賣家、{w.get('requests', 0)} 個請求 → "
        f"在架 {w.get('found', 0)} 筆、候選 {w.get('candidates', 0)} 筆"
    )
    for s in w.get("sellers") or []:
        console.print(
            f"  {s['seller_key']}（{s['source']}）：{s['health']}，"
            f"在架 {s['found']}、候選 {s['candidates']}"
            + (f"｜{s['detail']}" if s.get("detail") else "")
        )
    for s in w.get("skipped") or []:
        console.print(f"  [yellow]{s['seller_key']}：跳過——{s['reason']}[/yellow]")
    obs = result.get("listing_obs") or {}
    console.print(
        f"[dim]觀測帳：新增 {obs.get('new', 0)}、更新 {obs.get('updated', 0)}；"
        f"訊號 {result.get('signals', 0)} 筆（新 {result.get('new', 0)}）[/dim]"
    )


@app.command()
def breakeven(
    target: float = typer.Option(None, help="目標到手成本（台幣），預設用鑑定費"),
    bundle: int = typer.Option(None, help="覆寫湊單張數"),
):
    """算出每條運送路徑「最多能出多少錢買卡」。

    這是你調整搜尋條件的依據 —— 直接把 max_item_jpy 當成搜尋的價格上限。
    """
    cfg = load_config()
    fx = FxRates(cfg)
    rows = breakeven_table(cfg, fx, target)

    t = Table(title=f"到手成本上限 NT${target or cfg.grading_fee_twd:,.0f}（匯率來源：{fx.source}）")
    t.add_column("路徑")
    t.add_column("湊單", justify="right")
    t.add_column("固定成本 NT$", justify="right")
    t.add_column("卡價上限 ¥", justify="right", style="bold green")
    for r in rows:
        t.add_row(
            r["label"], str(r["bundle_size"]),
            f"{r['overhead_twd']:,.0f}", f"{r['max_item_jpy']:,.0f}",
        )
    console.print(t)
    console.print(
        "\n[dim]卡價上限 = 0 代表這條路徑光是固定成本就已經超過你的門檻，"
        "只能靠折價 trigger，不可能觸發「白撿」。[/dim]"
    )


@app.command()
def spread(
    limit: int = typer.Option(400, help="拿幾筆庫存標的來實算（不是用係數空算）"),
    state: str = typer.Option("all", help="標的狀態；all = 全庫"),
    assume_jp_presence: bool = typer.Option(
        False, "--assume-jp-presence",
        help="反事實：假設你已經有日本地址＋日本銀行帳戶＋日本手機門號",
    ),
    bundle: int = typer.Option(None, help="覆寫湊單張數（影響買進攤提）"),
):
    """跨平台淨價差盤點：（買進路徑 × 賣出賣場）全組合，哪些走得通、走得通的賺多少。

    這個指令要回答的是**一個問題**：扣掉所有成本之後，還有沒有正的期望淨利？
    答案是「沒有」也要如實說——那是有價值的負面結論，會把資源從「找套利」
    轉去「找別的商業模式」，遠比一個調參數調出來的正數有用。
    """
    import statistics
    from dataclasses import replace as _replace

    from .selling import (
        RoundTrip,
        best_round_trip,
        feasibility_matrix,
        listing_from_signal_row,
        location_label,
        round_trips_for,
        venue_estimator_for_row,
    )

    cfg = load_config()
    if assume_jp_presence:
        cfg = _replace(cfg, resale=_replace(cfg.resale, jp_presence=True))
    fx = FxRates(cfg)
    store = Store(cfg.db_path)

    if not cfg.resale.venues:
        console.print("[red]settings.yaml 沒有 sell_venues: 區塊，沒有任何賣場可以算[/red]")
        raise typer.Exit(1)

    # --- 1. 結構可行性（與任何一筆標的無關）-----------------------------
    console.print(
        f"\n[bold]日本收款身分：{'✅ 假設有（反事實）' if cfg.resale.jp_presence else '❌ 沒有'}[/bold]"
        + ("" if cfg.resale.jp_presence else f"　{cfg.resale.jp_presence_reason}")
    )
    rows = feasibility_matrix(cfg, fx)
    t = Table(title="① 結構可行性：買進路徑 × 賣出賣場")
    for col in ("買進路徑", "貨落在", "賣場", "賣場要求"):
        t.add_column(col)
    t.add_column("可行", justify="center")
    t.add_column("有行情?", justify="center")
    t.add_column("說明")
    for r in rows:
        t.add_row(
            r["route_label"], location_label(r["holding"]), r["venue_label"],
            location_label(r["venue_location"]),
            "[green]✅[/green]" if r["feasible"] else "[red]❌[/red]",
            "[green]有[/green]" if r["has_market"] else "[yellow]無[/yellow]",
            (r["why"][:46] + "…") if len(r["why"]) > 47 else r["why"],
        )
    console.print(t)
    n_ok = sum(r["feasible"] for r in rows)
    n_priceable = sum(1 for r in rows if r["feasible"] and r["has_market"])
    console.print(
        f"[dim]{len(rows)} 個組合 → {n_ok} 個結構上可行 → 其中 {n_priceable} 個有成交樣本可以估價。\n"
        "「可行但無行情」與「不可行」是兩種不同的『不行』：前者要去補 comps，"
        "後者要去改變結構（例如辦日本身分）。[/dim]"
    )

    # --- 2. 用庫裡的實際標的實算 ----------------------------------------
    sig_rows = store.list_signals(state=state, limit=limit)
    if not sig_rows:
        console.print("[yellow]庫裡沒有標的，無法實算。先跑一次 `ygo-sniper daily`。[/yellow]")
        raise typer.Exit(0)

    from .valuation import build_valuator

    valuator = build_valuator(cfg, store, None)

    per_combo: dict[tuple[str, str], list[float]] = {}
    per_combo_roi: dict[tuple[str, str], list[float]] = {}
    labels: dict[tuple[str, str], tuple[str, str]] = {}
    best_per_listing: list[tuple[float, str, RoundTrip]] = []
    n_used = n_skipped = 0

    for row in sig_rows:
        lst = listing_from_signal_row(row)
        if lst is None:
            n_skipped += 1
            continue
        n_used += 1
        trips = round_trips_for(
            lst, cfg, fx,
            estimate_for=venue_estimator_for_row(valuator, row),
            bundle_size=bundle,
        )
        for tr in trips:
            if tr.ok and tr.net_profit_twd is not None:
                k = (tr.buy_route, tr.sell_venue)
                per_combo.setdefault(k, []).append(tr.net_profit_twd)
                if tr.roi is not None:
                    per_combo_roi.setdefault(k, []).append(tr.roi)
                labels[k] = (tr.buy_route_label, tr.sell_venue_label)
        b = best_round_trip(trips)
        if b is not None:
            best_per_listing.append((b.net_profit_twd or 0.0, row.get("title") or "", b))

    console.print(
        f"\n[bold]② 實算（{n_used} 筆庫存標的，跳過 {n_skipped} 筆 payload 殘缺）[/bold]"
    )
    if not per_combo:
        console.print(
            "[red]沒有任何組合算得出數字。[/red]\n"
            "[dim]原因見上表：結構上可行的組合都沒有成交樣本（估不出賣出價，紅線是"
            "不准猜），有成交樣本的組合結構上都到不了。[/dim]"
        )
    else:
        t2 = Table(title="② 可行組合的淨價差（用庫存標的實算，非係數空算）")
        t2.add_column("買進路徑")
        t2.add_column("賣出賣場")
        t2.add_column("n", justify="right")
        t2.add_column("淨利中位數 NT$", justify="right")
        t2.add_column("報酬率中位數", justify="right")
        t2.add_column("正淨利佔比", justify="right")
        for k in sorted(per_combo, key=lambda k: -statistics.median(per_combo[k])):
            vals = per_combo[k]
            med = statistics.median(vals)
            rois = per_combo_roi.get(k) or [0.0]
            pos = sum(1 for v in vals if v > 0) / len(vals)
            style = "bold green" if med > 0 else "red"
            t2.add_row(
                labels[k][0], labels[k][1], str(len(vals)),
                f"[{style}]{med:,.0f}[/{style}]",
                f"{statistics.median(rois):.1%}", f"{pos:.0%}",
            )
        console.print(t2)

    # --- 3. 明確回答：有沒有正淨利路徑 -----------------------------------
    positives = [b for b in best_per_listing if b[0] > 0]
    console.print("\n[bold]③ 有沒有任何一條路徑，扣掉所有成本後仍有正的期望淨利？[/bold]")
    if not per_combo:
        console.print(
            "[bold red]沒有——而且不是『算出來是負的』，是『算不出來』。[/bold red]\n"
            "[dim]這是兩個結構性缺口疊在一起：(a) 三個有行情的日本賣場都需要日本收款身分；"
            "(b) 走得到的台灣／eBay 賣場一筆成交樣本都沒有。[/dim]"
        )
    elif not positives:
        console.print(
            f"[bold red]沒有。{n_used} 筆標的裡，最好的組合淨利中位數仍為負。[/bold red]\n"
            "[dim]不要為了給出正面答案去調參數或忽略成本項。[/dim]"
        )
    else:
        console.print(
            f"[bold green]有：{len(positives)}/{len(best_per_listing)} 筆標的存在正淨利組合"
            f"（{len(positives) / max(1, len(best_per_listing)):.0%}）。[/bold green]"
        )
        t3 = Table(title="淨利最高的 10 筆")
        t3.add_column("標的", overflow="ellipsis", max_width=38)
        t3.add_column("買→賣")
        t3.add_column("到手 NT$", justify="right")
        t3.add_column("實拿 NT$", justify="right")
        t3.add_column("淨利 NT$", justify="right")
        t3.add_column("報酬率", justify="right")
        for profit, title, b in sorted(best_per_listing, key=lambda x: -x[0])[:10]:
            t3.add_row(
                title, f"{b.buy_route_label} → {b.sell_venue_label}",
                f"{b.landed_twd:,.0f}", f"{b.net_proceeds_twd:,.0f}",
                f"[bold green]{profit:,.0f}[/bold green]", f"{(b.roi or 0):.0%}",
            )
        console.print(t3)

    # --- 4. 誠實聲明 ------------------------------------------------------
    console.print("\n[bold]④ 這些數字沒有算進去的東西[/bold]")
    console.print(f"[dim]· {cfg.resale.tax_note}[/dim]")
    console.print(
        "[dim]· **賣得掉率與持有時間完全沒有模型**：所有淨利都假設「賣得掉、而且立刻賣掉」。\n"
        "· 賣出價用估價 80% 區間下緣（保守），但區間本身的實測覆蓋率是 83%（名目 80%）。\n"
        "· 各賣場費率的查證程度不一，逐項如下：[/dim]"
    )
    for name, v in cfg.resale.venues.items():
        console.print(f"[dim]  · {v.label}（{name}）：{v.verified}[/dim]")
        if v.source_url:
            console.print(f"[dim]    來源 {v.source_url}[/dim]")


@app.command()
def probe(url: str):
    """抓取壞掉時用這個。印出某個 URL 解析出什麼。"""
    from .sources import probe as _probe

    cfg = load_config()
    console.print_json(json.dumps(_probe(cfg, url), ensure_ascii=False))


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8321,
):
    """開 dashboard。"""
    import sys

    import uvicorn

    # `web/` 不是安裝進 venv 的套件（pyproject 只打包 src/ygo_sniper），
    # 而 console script 的 sys.path[0] 是 .venv/bin 不是專案根目錄 ——
    # 少了這行，`ygo-sniper serve` 會以 ModuleNotFoundError: No module named 'web' 收場。
    # 用 config 解出來的 root 而不是 cwd：在任何目錄下跑這個指令都要能開起來。
    root = str(load_config().root)
    if root not in sys.path:
        sys.path.insert(0, root)

    uvicorn.run("web.app:app", host=host, port=port, reload=False)


@app.command()
def stats():
    """看目前資料庫狀態。"""
    cfg = load_config()
    console.print_json(json.dumps(Store(cfg.db_path).stats(), ensure_ascii=False, default=str))


@app.command()
def health(
    clear: bool = typer.Option(
        False, "--clear", help="清掉 alerts 表所有告警列（修好病因之後用）"
    ),
    clear_source: str = typer.Option(
        None, "--clear-source", help="只清掉這個來源的告警列（例：yahoo_direct）"
    ),
):
    """看各來源最近一次 scan 的健康狀況，以及目前掛著的告警。

    「今天沒好貨」與「爬蟲瞎了」外顯行為一樣，這個指令是用來分辨的。

    `--clear` / `--clear-source` 是**修好病因之後**的一次性清理。
    為什麼要有這個指令、而不是叫人自己下 SQL：告警列是這套健康判定的記憶
    （occurrences 決定 FETCH_FAILED 的連續門檻、notify_count 決定冷卻），
    手寫 SQL 很容易只刪一半或刪錯表，而且沒有任何人會看到你刪了什麼。
    這裡刪之前先把整列印出來，刪完再印一次現況——留得下痕跡才敢清。

    ⚠️ 清掉的是**觀測紀錄**，不是病因。來源還壞著的話下一輪 scan 會重新記，
    這是刻意的：清理不該變成把警報靜音的手段。
    """
    cfg = load_config()
    store = Store(cfg.db_path)

    if clear or clear_source:
        _clear_alerts(store, only_source=clear_source)
        return
    last = store.stats().get("last_run")
    notes = {}
    if last:
        try:
            notes = json.loads(last.get("notes") or "{}")
        except (ValueError, TypeError):
            notes = {}

    sources = notes.get("sources") or {}
    if not last:
        console.print("[dim]還沒跑過 scan。先執行 `ygo-sniper scan`。[/dim]")
    elif not sources:
        console.print(
            f"[dim]最近一次 scan（{last.get('finished_at', '?')}）沒有留下來源健康資料"
            "（可能是舊版紀錄），重跑一次 scan 就會有。[/dim]"
        )
    else:
        t = Table(title=f"來源健康（最近一次 scan：{last.get('finished_at', '?')}）")
        t.add_column("來源")
        t.add_column("健康")
        t.add_column("筆數", justify="right")
        t.add_column("詳情", max_width=54)
        for name, s in sources.items():
            ok = s.get("health") in ("ok", "empty")
            label = HEALTH_LABEL.get(s.get("health", ""), s.get("health", "?"))
            t.add_row(
                name,
                f"[{'green' if ok else 'red'}]{label}（{s.get('health')}）[/]",
                str(s.get("count", 0)),
                s.get("detail") or "",
            )
        console.print(t)

    rows = store.list_alerts()
    if not rows:
        console.print("[green]告警表：空的（沒有掛著的來源問題）[/green]")
        return
    t = Table(title="告警現況（alerts 表）")
    t.add_column("fingerprint")
    t.add_column("次數", justify="right")
    t.add_column("已通知", justify="right")
    t.add_column("上次通知")
    t.add_column("首見")
    for r in rows:
        t.add_row(
            r["fingerprint"],
            str(r["occurrences"]),
            str(r["notify_count"]),
            (r["last_notified_at"] or "未通知")[:19],
            (r["first_seen"] or "")[:19],
        )
    console.print(t)


def _clear_alerts(store: Store, *, only_source: str | None = None) -> int:
    """刪掉告警列並把刪掉的內容印出來。回傳刪除筆數（0 也照印，讓人知道本來就空）。"""
    rows = store.list_alerts()
    targets = [r for r in rows if not only_source or r["source"] == only_source]
    if not targets:
        scope = f"來源 {only_source} " if only_source else ""
        console.print(f"[dim]{scope}告警表本來就是空的，沒有東西要清。[/dim]")
        return 0

    t = Table(title=f"即將清除 {len(targets)} 列告警")
    for col in ("fingerprint", "次數", "已通知", "首見", "詳情"):
        t.add_column(col, justify="right" if col in ("次數", "已通知") else "left",
                     max_width=60 if col == "詳情" else None)
    for r in targets:
        t.add_row(
            r["fingerprint"], str(r["occurrences"]), str(r["notify_count"]),
            (r["first_seen"] or "")[:19], r["detail"] or "",
        )
    console.print(t)
    for r in targets:
        store.clear_alert(r["fingerprint"])
    left = store.list_alerts()
    # 表格會被窄終端擠掉欄位，所以再補一行純文字清單：**刪了什麼必須留得下痕跡**
    gone = "、".join(f"{r['fingerprint']}（{r['occurrences']} 次）" for r in targets)
    console.print(f"[green]已清除 {len(targets)} 列：{gone}[/green]")
    console.print(
        f"alerts 表剩 {len(left)} 列"
        + (f"（{', '.join(r['fingerprint'] for r in left)}）" if left else "（空）")
    )
    console.print(
        "[dim]清掉的是觀測紀錄不是病因：來源若還壞著，下一輪 scan 會重新記一次。[/dim]"
    )
    return len(targets)


@app.command()
def test_telegram():
    """確認 Telegram 設定正確。"""
    from .notify import TelegramNotifier

    n = TelegramNotifier(load_config())
    if not n.config_enabled:
        console.print(
            "[yellow]Telegram 目前是停用狀態（config/settings.yaml 的 notify.enabled=false）。"
            "\n改回 true 才會送出——.env 裡的 token 沒有被動過。[/yellow]"
        )
        raise typer.Exit(1)
    if not n.configured:
        console.print("[red]未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID[/red]")
        raise typer.Exit(1)
    ok = n.send("🃏 ygo-sniper 連線測試成功。晚安。")
    console.print("[green]已送出[/green]" if ok else "[red]送出失敗[/red]")


# ---------------------------------------------------------------------------
# 卡片主檔與估價模型
# ---------------------------------------------------------------------------
@app.command()
def refresh_cards():
    """重抓 1998-2004 卡片主檔（日文卡名）。

    刻意做成明確指令而不是自動 TTL：「1998-2004 發行過哪些卡」是不會變的
    歷史事實，沒有理由讓每小時跑一次的 scan 去打外網（而且來源之一約 98MB）。
    """
    from .cards import build_master, write_master

    console.print("[dim]下載 YGOPRODeck（年代）＋ yaml-yugi（日文卡名）…[/dim]")
    data = build_master()
    path = write_master(data)
    c = data["counts"]
    console.print(
        f"[green]主檔已更新：{path}[/green]\n"
        f"掃 {c['scanned']} 張 → 1998-2004 [bold]{c['in_era']}[/bold] 張"
        f"｜年代外 {c['out_of_era']} 張｜無日文名 {c['no_japanese_name']} 張"
    )


@app.command()
def match_report(
    misses: int = typer.Option(30, help="列出幾筆未命中的標題（找失敗模式用）"),
    hits: int = typer.Option(0, help="列出幾筆命中結果（人工核對誤配用）"),
    seed: int = typer.Option(0, help="抽樣種子。固定值＝同一份資料每次抽到同一批"),
    as_json: bool = typer.Option(False, "--json", help="輸出原始 JSON（給腳本對照用）"),
    master: str = typer.Option("", help="改用指定路徑的主檔（拿舊主檔跑就是改動前基線）"),
):
    """量卡名比對的命中率：整體 ＋ 分來源，附未命中／命中的抽樣。

    **改比對規則之前先跑一次留基線，改完再跑一次對照。** 命中率是可行動標的
    數量的上游：比不到卡名 → 估價退到 L3（整個稀有度的池）→ `bidding` 的證據
    閘門直接拒絕給出價上限。

    `--hits` 那張表是**誤配稽核**用的，比命中率更重要：未命中會誠實地退到 L3
    並被擋下，誤配會給出一個有信心但錯誤的估值，而你會照著它出價。
    """
    from .cards import CardIndex
    from .cards import match_report as run_report

    cfg = load_config()
    store = Store(cfg.db_path)
    index = CardIndex.load(master or None)
    rows = [
        {"title": r.get("title"), "site": r.get("site"), "table": table}
        for table, source in (
            ("signals", store.list_signals(state="all", limit=100000)),
            ("comps", store.comps_by(limit=100000)),
        )
        for r in source
    ]
    rep = run_report(rows, index, miss_sample=misses, hit_sample=hits, seed=seed)
    if as_json:
        console.print_json(json.dumps(rep, ensure_ascii=False))
        return

    o = rep["overall"]
    console.print(
        f"[dim]主檔 {rep['cards_indexed']} 張卡｜可反查卡號 {rep['codes_indexed']} 個"
        f"｜標題 {o['n']} 筆（signals + comps）[/dim]"
    )
    t = Table(title="卡名比對命中率")
    for col in ("來源", "標題數", "命中", "命中率", "年代內", "年代外", "靠卡名", "靠卡號"):
        t.add_column(col, justify="left" if col == "來源" else "right")

    def _row(label: str, s: dict) -> None:
        rate = s["hit"] / s["n"] if s["n"] else 0.0
        t.add_row(label, str(s["n"]), str(s["hit"]), f"{rate:.1%}", str(s["in_era"]),
                  str(s["out_of_era"]), str(s["via_name"]), str(s["via_code"]))

    for site, s in rep["by_site"].items():
        _row(site, s)
    _row("[bold]總計[/bold]", o)
    console.print(t)
    console.print(
        "[dim]「年代外」＝認得這張卡但它不是 1998-2004。這跟未命中是兩種訊號："
        "前者是明確排除，後者多半是標題沒寫單卡卡名（整包／配件／其他 TCG）。\n"
        "估價只把**年代內**的命中當卡名用，所以能撐起 L1／L2 的是「年代內」那一欄。[/dim]"
    )

    if rep["miss_samples"]:
        mt = Table(title=f"未命中抽樣（全部 {rep['n_misses']} 筆，seed={seed}）")
        mt.add_column("來源")
        mt.add_column("標題", max_width=88)
        for m in rep["miss_samples"]:
            mt.add_row(m["site"], m["title"])
        console.print(mt)
    if rep["hit_samples"]:
        ht = Table(title=f"命中抽樣（誤配人工核對用，全部 {rep['n_hits']} 筆，seed={seed}）")
        ht.add_column("來源")
        ht.add_column("配到的卡名", max_width=24)
        ht.add_column("途徑")
        ht.add_column("年代內")
        ht.add_column("標題", max_width=62)
        for h in rep["hit_samples"]:
            ht.add_row(h["site"], h["name_ja"], h["via"], "是" if h["in_era"] else "否",
                       h["title"])
        console.print(ht)


@app.command()
def backfill_cards():
    """把 comps 既有列的 card_name 欄位填上（物化快取，估價本身不依賴它）。"""
    from .cards import CardIndex

    cfg = load_config()
    store = Store(cfg.db_path)
    index = CardIndex.load()
    if not index.available:
        console.print("[red]找不到卡片主檔，先跑 `ygo-sniper refresh-cards`[/red]")
        raise typer.Exit(1)

    before = store.count_named_comps()
    rows = store.comps_by(limit=100000)
    updates, out_of_era, missed = [], 0, 0
    for r in rows:
        m = index.match(r.get("title") or "")
        if m and m.in_era:
            updates.append((r["id"], m.name_ja))
        else:
            out_of_era += int(bool(m))
            missed += int(not m)
    changed = store.set_card_names(updates)
    after = store.count_named_comps()
    console.print(
        f"[green]回填 {changed} 列[/green]｜card_name 非空：{before} → {after}\n"
        f"[dim]年代外 {out_of_era} 列、比對不到 {missed} 列"
        f"（比對不到多半代表標題沒寫單卡卡名，本身就是有用訊號）[/dim]"
    )


@app.command()
def backfill_images(
    dry_run: bool = typer.Option(False, help="只報數字，不寫 db、不抓網路"),
    include_missing: bool = typer.Option(False, help="連 image_url 是空的那些也重抓"),
    limit: int = typer.Option(200, help="最多處理幾筆（節流下每筆約 2 秒）"),
):
    """把 signals 裡的佔位圖（loading-spinner…）換成真實縮圖，抓不到就清成 NULL。

    為什麼會有這批髒資料：Buyee 搜尋頁的縮圖是 lazyload，真圖在
    `data-bind="lazyload: { imagePath: … }"`，`src` 永遠是佔位動畫。舊版
    parser 只讀 `data-src`／`src`，於是每一筆 Buyee 訊號都存了同一張轉圈圈
    的 gif（2026-08-02 實測 47 筆訊號中 24 筆中招）。parser 已修，但**已經
    落庫的列不會自己更新**——這個指令就是那道回填。

    重跑安全（冪等）：判準是「現在的 parser 認不認這個網址」
    （`BuyeeSource.normalize_image_url`，與抓取端同一把尺），已經是真圖的
    列根本不會進待修清單，所以第二次跑一定是 0 筆需要處理。
    抓不到就寫 NULL 而不是留著佔位圖：**留著等於前端沒有任何辦法分辨真假**，
    寫 NULL 前端至少會畫出「無預覽圖」的佔位框。

    第二批髒資料（2026-08-03）：PayPay 由 Buyee 鏡像改成原站直抓之前落庫的列，
    縮圖指向 Buyee 的代理 CDN（`cdnyauction-pctr.buyee.jp`）而不是原站
    （`auc-pctr.c.yimg.jp`）。這一批走**改寫**而不是重抓（同一張圖只差主機名，
    見 `sources.paypay.canonical_thumbnail_url`），改寫後逐筆連線驗證：
    HTTP 200 ＋ `content-type: image/*` 才寫進去，驗不過就清成 NULL。
    """
    from .sources.base import FetchError
    from .sources.buyee import BuyeeSource, parse_item_image
    from .sources.paypay import canonical_thumbnail_url

    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.all_signal_images()

    # --- 第 0 階段：可改寫的舊主機（不必重抓商品頁）-------------------------
    rewritable = [
        r for r in rows
        if r["image_url"] and canonical_thumbnail_url(r["image_url"]) != r["image_url"]
    ]
    # 待修＝「有存東西，但現在的 parser 判定它不是有效圖」（include_missing 時加上空值）
    broken = [
        r for r in rows
        if (r["image_url"] and BuyeeSource.normalize_image_url(r["image_url"]) is None)
        or (include_missing and not r["image_url"])
    ]
    console.print(
        f"訊號 {len(rows)} 筆｜可改寫主機 [cyan]{len(rewritable)}[/cyan] 筆｜"
        f"待重抓 [yellow]{len(broken)}[/yellow] 筆"
        f"（佔位圖 {sum(1 for r in rows if r['image_url'] and not BuyeeSource.normalize_image_url(r['image_url']))}、"
        f"空值 {sum(1 for r in rows if not r['image_url'])}）"
    )
    if dry_run:
        for r in rewritable[:limit]:
            console.print(f"  [dim]{r['key']}：{r['image_url'][:70]}…[/dim]")
        return

    if rewritable:
        ok, bad = _rewrite_image_hosts(store, rewritable[:limit], cfg)
        console.print(
            f"[green]改寫 {ok} 列主機[/green]（驗證 200＋image/*）｜"
            f"驗不過清成 NULL {bad} 列"
        )
    if not broken:
        return

    waf = None
    updates: list[tuple[str, str | None]] = []
    fixed = nulled = 0
    try:
        for r in broken[:limit]:
            image: str | None = None
            if r["site"] in ("buyee_mercari", "buyee_paypay"):
                if waf is None:
                    from .sources.waf import WafSession

                    waf = WafSession(cfg)
                try:
                    image = parse_item_image(waf.get(r["url"], use_cache=False))
                except FetchError as exc:
                    # 商品下架／被擋都是「這一筆抓不到」，不是整批失敗：
                    # 大聲印出來（工程原則 3），繼續處理下一筆，最後清成 NULL
                    console.print(f"  [dim]{r['key']}：{type(exc).__name__} {exc}[/dim]")
            updates.append((r["key"], image))
            fixed += int(image is not None)
            nulled += int(image is None)
    finally:
        if waf is not None:
            waf.close()

    changed = store.set_signal_images(updates)
    console.print(
        f"[green]回填 {changed} 列[/green]｜取得真圖 {fixed}、抓不到清成 NULL {nulled}\n"
        f"[dim]剩餘待修 {max(0, len(broken) - len(updates))} 筆（--limit 控制批量）[/dim]"
    )


def image_is_live(url: str, *, timeout: float = 20.0, user_agent: str | None = None) -> bool:
    """這個網址現在真的給得出一張圖嗎（HTTP 200 ＋ `content-type: image/*`）。

    **狀態碼與 content-type 兩個都要看**：Yahoo 的圖片 CDN 對不存在的圖回
    `403 image/gif`（一張「沒有圖」的佔位 gif），只看 content-type 會把它當成
    有效圖存回去，那正是這道回填要修掉的病。
    用 HEAD（實測該 CDN 支援）——只是要確認存在，不必把圖抓下來。
    """
    import httpx

    try:
        r = httpx.head(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent} if user_agent else None,
        )
    except Exception as exc:  # noqa: BLE001 - 連不上就是驗不過，不是整批失敗
        console.print(f"  [dim]驗證失敗：{type(exc).__name__} {exc}[/dim]")
        return False
    return r.status_code == 200 and r.headers.get("content-type", "").startswith("image/")


def _rewrite_image_hosts(store: Store, rows: list[dict], cfg) -> tuple[int, int]:
    """把舊 CDN 主機的縮圖改寫成原站主機，逐筆連線驗證後才寫回。

    驗不過就寫 NULL（與本指令的另一半同一條規矩：寧可讓前端畫「無預覽圖」，
    也不要留一個永遠載不出來的網址——前端沒有辦法分辨兩者）。
    對外請求之間沿用 `fetch.delay_seconds`，不另外拍一個節流數字。
    """
    import time

    from .sources.paypay import canonical_thumbnail_url

    delay = float(cfg.fetch.get("delay_seconds", 2.0))
    ua = cfg.fetch.get("user_agent")
    updates: list[tuple[str, str | None]] = []
    ok = bad = 0
    for i, r in enumerate(rows):
        if i:
            time.sleep(delay)
        new = canonical_thumbnail_url(r["image_url"])
        if new and image_is_live(new, user_agent=ua):
            updates.append((r["key"], new))
            ok += 1
        else:
            console.print(f"  [dim]{r['key']}：改寫後仍取不到圖，清成 NULL[/dim]")
            updates.append((r["key"], None))
            bad += 1
    store.set_signal_images(updates)
    return ok, bad


@app.command()
def backfill_era():
    """用**目前**的 watchlist 判準重算 comps 既有列的 era_evidence（冪等）。

    為什麼需要這個指令：`era_evidence` 是**寫入時**算好存進去的，而估價的
    取樣（`load_comps_rows`）就是靠它過濾。所以在 watchlist 加了排除字或改了
    年代標記之後，新判準只對之後抓到的資料生效——庫裡既有的污染（現代卡、
    多卡整包、把「初期絵」當年代證據的那些）會原封不動繼續參與估價。

    判準與入庫**完全同一組**（`parse_card` + `is_candidate`），不是只重跑年代
    關鍵字：`era_evidence` 非空 = 這一列會被估價採用，所以它必須代表「入庫端
    今天也會收這一筆」。實測庫裡就有 8 列有年代證據卻過不了入庫門檻
    （PSA1、ARS6、BGS——含一筆 NT$357,280 的 BGS7），它們一直在參與估價。

    重跑安全：只寫「算出來跟現況不一樣」的列，第二次跑一定是 0 列改動。
    """
    from .parsers import is_candidate, parse_card

    cfg = load_config()
    store = Store(cfg.db_path)
    before = store.count_era_verified_comps()
    rows = store.comps_by(limit=1000000)

    updates: list[tuple[int, str]] = []
    cleared: dict[str, int] = {}
    gained = 0
    for r in rows:
        info = parse_card(r.get("title") or "", cfg.watchlist)
        ok, why = is_candidate(info, cfg.watchlist)
        new = ",".join(info.era_evidence) if ok else ""
        old = r.get("era_evidence") or ""
        if new == old:
            continue
        updates.append((r["id"], new))
        if old and not new:
            cleared[why] = cleared.get(why, 0) + 1
        elif new and not old:
            gained += 1
    changed = store.set_era_evidence(updates)
    after = store.count_era_verified_comps()

    detail = "、".join(f"{k}×{v}" for k, v in sorted(cleared.items(), key=lambda kv: -kv[1]))
    console.print(
        f"[green]重算 {len(rows)} 列，改寫 {changed} 列[/green]｜"
        f"有年代證據：{before} → {after}\n"
        f"[dim]清掉 {sum(cleared.values())} 列（{detail or '無'}）、"
        f"新增 {gained} 列[/dim]"
    )


@app.command()
def recheck_signals(
    apply: bool = typer.Option(False, "--apply", help="真的刪除（預設只報告，不寫 db）"),
):
    """用**目前**的 watchlist 判準重跑 signals，刪掉現在已經不該收的列（冪等）。

    `backfill-era` 對 comps 做的事，這一支對 signals 做——兩者共用**同一組判準**
    （`parse_card` + `is_candidate`），不是各寫一份。加了排除字之後，庫裡既有的
    污染（球員卡、他 TCG）不會自己消失，dashboard 會一直看得到它們。

    紅線：**只刪 `state='new'`**。任何你手動標過的狀態都是人工決策，程式不准刪，
    只會列出來讓你自己處理（`store.purge_signals` 的 `kept_manual`）。
    重跑安全：第二次跑一定是 0 筆（該刪的已經不在庫裡）。
    """
    from .parsers import is_candidate, parse_card

    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.all_signal_titles()

    doomed: list[dict] = []
    for r in rows:
        info = parse_card(r.get("title") or "", cfg.watchlist)
        ok, why = is_candidate(info, cfg.watchlist)
        if not ok:
            doomed.append({**r, "why": why})

    console.print(f"訊號 {len(rows)} 筆｜依現行判準不該收 [yellow]{len(doomed)}[/yellow] 筆")
    if doomed:
        t = Table(title="不該收的列")
        t.add_column("狀態")
        t.add_column("原因", max_width=30)
        t.add_column("標題", max_width=64)
        for d in doomed:
            t.add_row(d["state"], d["why"], d["title"])
        console.print(t)
    if not apply or not doomed:
        console.print("[dim]（未加 --apply，只報告不刪除）[/dim]" if doomed else "")
        return

    rep = store.purge_signals([d["key"] for d in doomed])
    console.print(
        f"[green]刪除 {rep['deleted']} 列訊號[/green]（連帶在架觀測 {rep['obs_deleted']} 列）"
    )
    if rep["kept_manual"]:
        console.print(
            f"[yellow]另有 {rep['kept_manual']} 列你手動標過狀態，未刪除——"
            f"請自己在 dashboard 處理[/yellow]"
        )


@app.command()
def venue_report():
    """印出平台（venue）價格水準係數：模型認為各平台的成交價差幾倍。

    這是 A 的驗收證據，也是日常抽查用的——係數突然變號或倍率跳動，
    代表某個平台的抓取管道壞了或樣本組成整批換了。
    """
    import math

    from .cards import CardIndex
    from .valuation import (
        Params,
        ValuationModel,
        load_comps_rows,
        obs_from_comps,
        venue_label,
    )

    cfg = load_config()
    rows = load_comps_rows(Store(cfg.db_path))
    obs = obs_from_comps(rows, CardIndex.load())
    model = ValuationModel(obs, Params.from_config(cfg))
    if not model.baseline_venue:
        console.print("[red]樣本沒有任何平台資訊（comps.site 全空）[/red]")
        raise typer.Exit(1)

    counts: dict[str, int] = {}
    for o in obs:
        if o.venue:
            counts[o.venue] = counts.get(o.venue, 0) + 1

    t = Table(title=f"平台價格水準（基準＝{venue_label(model.baseline_venue)}）")
    for col in ("平台", "樣本數", "倍率", "log 係數", "係數權重", "來源"):
        t.add_column(col, justify="left" if col == "平台" else "right")
    for venue in sorted(model.venue_delta, key=lambda v: -counts.get(v, 0)):
        w = model.venue_weight.get(venue, 0.0)
        t.add_row(
            venue_label(venue), str(counts.get(venue, 0)),
            f"×{math.exp(model.venue_delta[venue]):.2f}",
            f"{model.venue_delta[venue]:+.3f}", f"{w:.2f}",
            "資料估計" if model.venue_is_estimated(venue) else "先驗為主",
        )
    console.print(t)
    console.print(
        "[dim]倍率是「控制稀有度×機構×分數後，同規格商品在該平台的成交價 vs 基準平台」。"
        "成因是市場結構：Yahoo 是競價拍賣（出清價），Mercari/PayPay 是定價出售"
        "（賣家開的零售價）。這不是資料錯誤，是真實價差。[/dim]"
    )


@app.command()
def venue_study(
    survey: bool = typer.Option(
        True, help="真的去三個平台抓一輪在架價（--no-survey 只用既有資料）"
    ),
    max_requests: int = typer.Option(150, help="對外請求數硬上限（達到就停並回報）"),
    min_per_cell: int = typer.Option(4, help="一個 (平台×分層) 至少幾筆才拿來比"),
    min_strata: int = typer.Option(3, help="至少幾個可比分層才敢給倍率"),
    save: bool = typer.Option(True, help="把結果存進 meta 表供 dashboard 顯示"),
    json_out: str = typer.Option(None, "--json", help="另外把完整報告寫成 JSON 檔"),
):
    """PayPay 到底是不是比較便宜買得到？三個問題一次回答。

    Q1 同規格的卡，哪個平台「現在就買得到的價格」最低？（在架價分層比較）
    Q2 成交價的平台係數是真價差還是選擇偏差？（各平台自己的在架 vs 成交）
    Q3 賣得掉率如何？（在架觀測帳的離場統計）

    ⚠️ 三個平台的在架價語意不同：Yahoo 我們只收**即決価格**（賣家開的溢價），
    Mercari/PayPay 是定價。競標尾盤常以低於即決價成交，所以這份比較
    **系統性有利於 PayPay**——結論會把這個偏差方向印在旁邊，請不要略過。

    樣本不足時一律印「不足以判定」，不外插、不放寬門檻。
    """
    from .fx import FxRates
    from .sources import CachedFetcher, build_sources
    from .valuation import load_comps_rows
    from .venue_study import (
        VENUE_STUDY_META_KEY,
        VENUES,
        build_study,
        rows_from_listing_obs,
        run_listing_survey,
        venue_study_label,
    )

    cfg = load_config()
    store = Store(cfg.db_path)
    fx = FxRates(cfg)

    if not survey:
        # 不打外網時退回**在架觀測帳**（每輪掃描累積的同一種資料），
        # 而不是拿一份空的在架樣本去覆蓋上一次的結論——
        # 「這次沒抓」不可以外顯成「沒有價差」。
        obs = rows_from_listing_obs(store.listing_obs(open_only=True))
        survey_result = {
            "rows": obs, "requests": 0, "max_requests": max_requests,
            "skipped_queries": 0, "keywords": [], "pages": {}, "elapsed_seconds": 0.0,
            "sources": [{"source": "listing_obs（既有觀測，未打外網）", "health": "ok",
                         "detail": "", "parsed": len(obs), "listings": len(obs), "requests": 0}],
            "funnel": {"candidates": len(obs), "rejected": {}},
        }
    else:
        fetcher = CachedFetcher(cfg)
        registry = build_sources(cfg, fetcher)
        try:
            console.print("[dim]在架調查中（每個請求間隔 2 秒，Buyee 冷啟動要開瀏覽器）…[/dim]")
            survey_result = run_listing_survey(
                cfg, registry=registry, fx=fx, max_requests=max_requests
            )
        finally:
            # Buyee 系兩條管道共用同一顆 _LazyWafFetcher（可能已開了瀏覽器），
            # 不關掉會留下一個 Playwright 行程——關它與關 httpx client 是兩件事。
            waf = getattr(registry.get("buyee_mercari"), "fetcher", None)
            if waf is not None and waf is not fetcher:
                waf.close()
            fetcher.close()

    report = build_study(
        survey=survey_result,
        sold_rows=load_comps_rows(store),
        listing_obs_summary=store.listing_obs_summary(),
        min_per_cell=min_per_cell,
        min_strata=min_strata,
    )
    if save:
        store.set_meta(VENUE_STUDY_META_KEY, json.dumps(report, ensure_ascii=False, default=str))
    if json_out:
        from pathlib import Path

        Path(json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        console.print(f"[dim]完整報告已寫入 {json_out}[/dim]")

    _print_venue_study(report, VENUES, venue_study_label)


def _print_venue_study(report: dict, venues, label_fn) -> None:
    """把 build_study 的報告印成三張表。**這裡只排版，不做任何判定**——
    判定全在 venue_study.py，CLI 與 dashboard 才不會各自解讀出不同的結論。"""
    s = report.get("survey") or {}
    if s:
        console.print(
            f"\n[bold]在架調查[/bold]：{s.get('requests', 0)} 個頁面請求"
            f"（上限 {s.get('max_requests')}、間隔 2 秒、耗時 {s.get('elapsed_seconds')}s）"
            f"｜候選 {report.get('listing_n', 0)} 筆｜成交樣本 {report.get('sold_n', 0)} 筆"
        )
        for r in s.get("sources", []):
            style = "green" if r["health"] in ("ok", "empty") else "red"
            console.print(
                f"  [{style}]{r['source']}[/{style}]: {r['health']}"
                f"｜解析 {r['parsed']}｜候選 {r['listings']}｜請求 {r['requests']}"
                + (f"｜{r['detail']}" if r.get("detail") else "")
            )
        if s.get("skipped_queries"):
            console.print(f"  [yellow]請求預算用完，{s['skipped_queries']} 個查詢沒跑[/yellow]")

    q1 = report["q1"]
    t = Table(title="Q1 在架價分層對照（NT$ 中位數／樣本數；★ = 該層樣本足夠）")
    t.add_column("分層（稀有度 × 機構分數）", max_width=34)
    cols = [*venues, "buyee_yahoo_bid"]
    for v in cols:
        t.add_column(label_fn(v), justify="right")
    shown = [row for row in q1["table"] if row["comparable"]][:12]
    for row in shown or q1["table"][:12]:
        cells = []
        for v in cols:
            c = row["cells"].get(v) or {"n": 0}
            cells.append(
                f"{c['median']:,.0f} /{c['n']}{'★' if c.get('enough') else ''}"
                if c.get("n") else "—"
            )
        t.add_row(row["stratum"], *cells)
    console.print(t)
    console.print(
        f"[dim]共 {len(q1['table'])} 個分層，其中 {sum(1 for r in q1['table'] if r['comparable'])}"
        f" 個至少兩個平台各有 ≥{report['params']['min_per_cell']} 筆（上表只列前 12）。"
        f"各平台候選數：{q1['counts']}[/dim]"
    )

    t2 = Table(title="Q1 對齊後的結論（同分層配對比值的中位）")
    for col in ("比較", "倍率", "可比分層", "對方較便宜的分層", "判定"):
        t2.add_column(col, justify="left" if col in ("比較", "判定") else "right")
    for name, res in q1["ratios"].items():
        base = "Mercari" if name.endswith("vs_mercari") else "Yahoo 即決"
        who = label_fn(name.replace("_vs_mercari", ""))
        t2.add_row(
            f"{who} ÷ {base}",
            f"×{res['ratio']:.2f}" if res.get("ratio") else "—",
            str(res["n_strata"]),
            f"{res.get('n_other_cheaper', '—')}/{res['n_strata']}" if res.get("ratio") else "—",
            "可判定" if res["verdict"] == "ok" else "不足以判定",
        )
    bid = q1["bid_reference"]
    t2.add_row(
        "（參考）Yahoo 現在価格 ÷ Yahoo 即決",
        f"×{bid['ratio']:.2f}" if bid.get("ratio") else "—",
        str(bid["n_strata"]), "—",
        "僅供參考・競標中不可直接成交",
    )
    console.print(t2)
    console.print(f"[bold]Q1 結論：{q1['headline']}[/bold]")
    for c in q1["caveats"]:
        console.print(f"[dim]⚠️ {c}[/dim]")

    t3 = Table(title="Q2 選擇偏差檢定（同平台：近期成交 ÷ 目前在架）")
    for col in ("平台", "在架筆數", "成交筆數", "可比分層", "成交/在架", "判讀"):
        t3.add_column(col, justify="left" if col in ("平台", "判讀") else "right",
                      max_width=52 if col == "判讀" else None)
    for venue, res in report["q2"]["by_venue"].items():
        t3.add_row(
            label_fn(venue), str(res["listing_n"]), str(res["sold_n"]), str(res["n_strata"]),
            f"×{res['ratio']:.2f}" if res.get("ratio") else "—", res["reading"],
        )
    console.print(t3)
    for c in report["q2"]["caveats"]:
        console.print(f"[dim]⚠️ {c}[/dim]")

    q3 = report["q3"]
    t4 = Table(title="Q3 賣得掉率（在架觀測帳 listing_obs）")
    for col in ("平台", "觀測列", "仍在架", "判定消失", "擠出觀測窗", "復活(誤判)", "賣得掉率"):
        t4.add_column(col, justify="left" if col == "平台" else "right")
    for r in q3["rows"]:
        t4.add_row(
            label_fn(r["venue"]), str(r["total"]), str(r["still_open"]), str(r["disappeared"]),
            str(r["window_exit"]), str(r["revived"]),
            f"{r['sell_through']:.0%}" if r["sell_through"] is not None
            else f"不足以判定（{r['blocked_by']}）",
        )
    console.print(t4)
    if q3["verdict"] != "ok":
        console.print(f"[yellow]Q3 不足以判定：{q3['detail']}[/yellow]")
    for c in q3["caveats"]:
        console.print(f"[dim]⚠️ {c}[/dim]")


@app.command()
def coverage_time(
    test_fraction: float = typer.Option(0.25, help="最晚的幾成成交當測試集"),
    real_time_only: bool = typer.Option(
        False, help="只用有**真實成交時間**的樣本（排除 sold_at 是入庫時間的）"
    ),
):
    """**時間切分**的區間覆蓋率自檢：早期成交訓練＋校準、晚期成交當測試。

    與 `value --coverage` 的隨機留出不同——隨機留出讓訓練與測試來自同一批
    同一天抓的資料，可交換性是被構造出來的，量到的數字看不見時間漂移與平台
    組成變化。這個指令量的是模型的實際處境：用過去的成交評估現在的標的。

    同時跑「有／無平台校正」兩組作對照。點估計誤差一起看：只看覆蓋率會把
    「區間變寬」誤讀成「模型變準」。

    ⚠️ **這個指令是最容易被 `sold_at` 語意騙到的一個**：Buyee 系的 `sold_at`
    是入庫時間，那批列在時間軸上會全部擠在最近，於是「切早晚」實際上是在
    「切平台」。所以每次都印出兩種資料各佔多少；`--real-time-only` 走乾淨
    但樣本較少的那條路。
    """
    from .cards import CardIndex
    from .valuation import Params, coverage_time_split, load_comps_rows, obs_from_comps

    cfg = load_config()
    rows = load_comps_rows(Store(cfg.db_path))
    n_ingest = sum(1 for r in rows if r.get("sold_at_is_ingest"))
    if real_time_only:
        rows = [r for r in rows if not r.get("sold_at_is_ingest")]
    console.print(
        f"[dim]樣本 {len(rows)} 筆"
        + (
            f"（已排除 {n_ingest} 筆 sold_at 是入庫時間的）"
            if real_time_only
            else f"，其中 {n_ingest} 筆的 sold_at 是**入庫時間不是成交時間**"
            "——這些列在時間軸上全部擠在最近，會讓「切早晚」變成偽裝的切平台。"
            "加 --real-time-only 看乾淨版。"
        )
        + "[/dim]"
    )
    obs = obs_from_comps(rows, CardIndex.load())
    params = Params.from_config(cfg)

    t = Table(title=f"時間切分覆蓋率（名目 {params.confidence:.0%}）")
    for col in ("切分方式", "平台校正", "實測覆蓋率", "測試筆數", "訓練筆數",
                "校準集", "區間中位寬度", "點估計中位誤差"):
        t.add_column(col, justify="left" if "切分" in col or "校正" == col else "right")
    results = {}
    for split_label, stratify in (("平台內切早晚", True), ("全域切早晚", False)):
        for label, aware in (("無", False), ("有", True)):
            res = coverage_time_split(obs, params, test_fraction=test_fraction,
                                      venue_aware=aware, stratify_by_venue=stratify)
            results[(stratify, aware)] = res
            emp = f"{res['empirical']:.1%}" if res["empirical"] is not None else "—"
            width = f"NT${res['median_width_twd']:,.0f}" if res["median_width_twd"] else "—"
            err = f"×{res['median_error_ratio']:.2f}" if res["median_error_ratio"] else "—"
            t.add_row(split_label, label, emp, str(res["n_tested"]), str(res["n_train"]),
                      str(res["calibration_n"]), width, err)
    console.print(t)
    for stratify in (True, False):
        res = results[(stratify, True)]
        name = "平台內切早晚" if stratify else "全域切早晚"
        console.print(
            f"[dim]{name}：訓練 {res['train_venue_mix']} → 測試 {res['test_venue_mix']}[/dim]"
        )
    # 對照組：只用單一平台的樣本建模、只測同平台。這是「完全不混平台」的極限，
    # 也是唯一能把「平台混池的偏誤」與「時間漂移的偏誤」分開看的參照——
    # 少了它，上面那張表的勝負讀不出是哪個原因造成的。
    from .valuation import venue_label

    venues = sorted({o.venue for o in obs if o.venue})
    t2 = Table(title="對照組：單一平台模型（同平台訓練、同平台測試）")
    for col in ("平台", "實測覆蓋率", "測試筆數", "訓練筆數", "點估計中位誤差"):
        t2.add_column(col, justify="left" if col == "平台" else "right")
    for ve in venues:
        sub = [o for o in obs if o.venue == ve]
        res = coverage_time_split(sub, params, test_fraction=test_fraction,
                                  venue_aware=False, stratify_by_venue=True)
        emp = f"{res['empirical']:.1%}" if res["empirical"] is not None else "—"
        err = f"×{res['median_error_ratio']:.2f}" if res["median_error_ratio"] else "—"
        t2.add_row(venue_label(ve), emp, str(res["n_tested"]), str(res["n_train"]), err)
    console.print(t2)

    ref = results[(True, True)]
    console.print(
        f"[dim]訓練集到 {(ref['train_until'] or '')[:10]}｜測試集從 "
        f"{(ref['test_from'] or '')[:10]} 起｜無成交時間而被排除 {ref['n_undated']} 筆。\n"
        "⚠️ 「全域切早晚」那兩列請當成警示而不是結果：Buyee 系的 sold_at 是入庫"
        "時間不是真實成交時間，全域排序會把最晚的一段整片切成 Mercari，"
        "那是偽裝成時間切分的平台切分。\n"
        "覆蓋率明顯低於名目值不是可以調參數調掉的東西——那是在說區間該更寬、"
        "或取樣視窗該更短。[/dim]"
    )
    if ref.get("note"):
        console.print(f"[yellow]{ref['note']}[/yellow]")


@app.command()
def coverage_groups(
    venue_aware: bool = typer.Option(True, help="走競標上限實際用的平台校正路徑"),
    diagnose: bool = typer.Option(
        False, "--diagnose", help="加印每桶的組成與最壞的 (層級×平台) 子切片"
    ),
):
    """**條件**覆蓋率對照：vanilla（全域分位數）vs Mondrian（分群），逐 n 分桶。

    vanilla split conformal 只保證邊際覆蓋率（全部平均 80%），不保證每個子群
    各自 80%——症狀是全庫每一筆估計的區間寬度比例完全一樣。這個指令量的是
    分群到底有沒有把各群拉回名目值，而且量的是**出貨路徑本身**
    （直接呼叫 `Valuator.quantiles_for()`）。

    「下尾違反率」是最重要的一欄：真實成交價低於區間下緣的比例（名目 10%）。
    出價上限完全由下緣反推，**下尾違反＝上限開得比市場實際成交還高**。
    """
    from .cards import CardIndex
    from .valuation import Params, coverage_by_group, load_comps_rows, obs_from_comps

    cfg = load_config()
    obs = obs_from_comps(load_comps_rows(Store(cfg.db_path)), CardIndex.load())
    params = Params.from_config(cfg)
    res = coverage_by_group(obs, params, venue_aware=venue_aware, diagnose=diagnose)
    if res.get("note"):
        console.print(f"[yellow]{res['note']}[/yellow]")
        return

    t = Table(
        title=f"條件覆蓋率（名目 {res['nominal']:.0%}／下尾名目 "
              f"{res['nominal_lower_tail']:.0%}）"
              f"｜平台校正 {'有' if venue_aware else '無'}"
    )
    for col in ("有效n 分桶", "測試筆數", "vanilla 覆蓋", "Mondrian 覆蓋",
                "vanilla 下尾", "Mondrian 下尾"):
        t.add_column(col, justify="left" if "分桶" in col else "right")
    for b in res["buckets"]:
        t.add_row(
            b["bucket"], str(b["n_tested"]),
            f"{b['coverage_vanilla']:.0%}", f"{b['coverage_group']:.0%}",
            f"{b['lower_tail_vanilla']:.0%}", f"{b['lower_tail_group']:.0%}",
        )
    o = res.get("overall") or {}
    if o:
        t.add_row(
            "總計", str(res["n_tested"]),
            f"{o['coverage_vanilla']:.0%}", f"{o['coverage_group']:.0%}",
            f"{o['lower_tail_vanilla']:.0%}", f"{o['lower_tail_group']:.0%}",
            style="bold",
        )
    console.print(t)
    console.print(
        f"[dim]時間切分（平台內切早晚）× test_fraction {res['test_fractions']} 合併計數；"
        f"每群至少 {res['min_group_calibration']} 筆才採用該群分位數。\n"
        "⚠️ 三份測試集互相重疊，合計筆數**不是**獨立樣本數，別拿去算信賴區間。\n"
        "⚠️ 低 n 不等於低證據：n_effective 是「所用層級的池大小」，"
        "n=1 多半是 L1（找到同一張卡），n=325 是 L3（退到整個稀有度的池）。[/dim]"
    )
    if diagnose:
        _print_bucket_diagnosis(res)


def _print_bucket_diagnosis(res: dict) -> None:
    """逐桶印出組成與最壞的 (層級×平台) 子切片。

    印子切片是重點：整桶一個平均值分不出「模型整體太窄」與「某一格整片錯掉」，
    而這兩件事的修法完全不同（調分群 vs 拒絕輸出）。
    """
    from .valuation import venue_label

    for b in res["buckets"]:
        comp = b.get("composition")
        if not comp:
            continue
        console.print(
            f"\n[bold]{b['bucket']}[/bold]（{b['n_tested']} 筆，"
            f"Mondrian 下尾 {b['lower_tail_group']:.0%}）"
        )
        for dim, label in (("level", "層級"), ("venue", "平台"),
                           ("rarity", "稀有度"), ("grade", "分數")):
            parts = "、".join(f"{k} {v}" for k, v in list(comp[dim].items())[:6])
            console.print(f"  {label}：{parts}")
        t = Table(show_header=True, box=None, pad_edge=False)
        for col in ("層級×平台", "筆數", "下尾", "覆蓋", "點估計中位偏誤"):
            t.add_column(col, justify="left" if "×" in col else "right")
        for s in comp["slices"][:6]:
            # 倍率 <1 ＝ 實際成交比模型估的低 ＝ 模型高估 ＝ 上限會開太高
            t.add_row(
                f"{s['level']} × {venue_label(s['venue'])}", str(s["n_tested"]),
                f"{s['lower_tail_group']:.0%}", f"{s['coverage_group']:.0%}",
                f"×{s['median_error_ratio']:.2f}",
            )
        console.print(t)


@app.command()
def rarity_relax_report(
    as_json: bool = typer.Option(False, "--json", help="輸出原始 JSON（給後續統計決定用）"),
):
    """量測「放寬 L2 到跨稀有度換算」的代價（**只量測，不改退化階梯**）。

    拿庫裡「同一張卡有多個稀有度成交」的樣本，模擬「該卡該稀有度 0 筆」的
    處境：用稀有度係數把其他稀有度的成交換算過來（L2X），與現行 L3
    （稀有度池）比誤差。這是「要不要放寬 L2」這個統計決定的證據材料。
    """
    from .cards import CardIndex
    from .valuation import Params, load_comps_rows, obs_from_comps, rarity_relax_study

    cfg = load_config()
    obs = obs_from_comps(load_comps_rows(Store(cfg.db_path)), CardIndex.load())
    rep = rarity_relax_study(obs, Params.from_config(cfg))
    if as_json:
        console.print_json(json.dumps(rep, ensure_ascii=False, default=str))
        return

    console.print(
        f"[dim]樣本 {rep['n_rows']} 筆｜多稀有度成交的卡 {rep['n_cards_multi_rarity']} 張"
        f"｜可量測目標 {rep['n_targets']} 筆"
        f"（無 L3 池可比而跳過 {rep['n_skipped_no_l3']} 筆）[/dim]"
    )
    if not rep["n_targets"]:
        console.print("[yellow]庫裡沒有「同卡多稀有度」的成交，量不出換算誤差。[/yellow]")
        return

    t = Table(title="換算誤差（|log 估計 − log 實際|，報成倍率）")
    for col in ("估計方式", "目標數", "中位誤差", "P90 誤差", "每卡中位"):
        t.add_column(col, justify="left" if col == "估計方式" else "right")
    labels = {
        "L2X_raw": "L2X 原始（跨稀有度換算中位）",
        "L2X_ladder": "L2X 入階梯（放寬後實際輸出）",
        "L3": "L3 現行（稀有度池）",
    }
    for key, label in labels.items():
        s = rep["estimators"][key]
        t.add_row(
            label, str(s["n"]),
            f"×{s['median_ratio']:.2f}" if s["median_ratio"] else "—",
            f"×{s['p90_ratio']:.2f}" if s["p90_ratio"] else "—",
            f"×{s['median_ratio_by_card']:.2f}" if s.get("median_ratio_by_card") else "—",
        )
    console.print(t)
    console.print(
        f"[bold]L2X 入階梯比 L3 準的比例：{rep['win_rate_ladder_vs_l3']:.0%}"
        f"（{rep['n_targets']} 筆配對比較）[/bold]"
    )

    if rep["donor_buckets"]:
        bt = Table(title="按「可換算的他稀有度樣本數」分桶")
        for col in ("樣本數", "目標數", "L2X 入階梯中位誤差", "L3 中位誤差"):
            bt.add_column(col, justify="right")
        for b in rep["donor_buckets"]:
            bt.add_row(
                b["bucket"], str(b["n"]),
                f"×{b['l2x_ladder_median_ratio']:.2f}", f"×{b['l3_median_ratio']:.2f}",
            )
        console.print(bt)

    if rep["pairs"]:
        pt = Table(title="稀有度換算配對（A → B，按樣本數排序，前 10）")
        for col in ("A（來源）", "B（目標）", "n", "中位誤差", "P90 誤差"):
            pt.add_column(col, justify="left" if "（" in col else "right")
        for p in rep["pairs"][:10]:
            pt.add_row(
                p["from"], p["to"], str(p["n"]),
                f"×{p['median_ratio']:.2f}", f"×{p['p90_ratio']:.2f}",
            )
        console.print(pt)
    for c in rep["caveats"]:
        console.print(f"[dim]⚠️ {c}[/dim]")
    console.print(
        "[dim]這份報告只是證據材料：要不要放寬 L2 是之後的統計決定，"
        "本輪不改退化階梯、不改 EvidenceGate。[/dim]"
    )


@app.command()
def recalc_bids(
    apply: bool = typer.Option(False, "--apply", help="真的寫回資料庫（預設只試算）"),
    limit: int = typer.Option(500, help="最多檢查幾筆 signals"),
):
    """用**現在這一版**的估價模型重算既有競標標的的出價上限，並印出前後對照。

    預設是 dry-run：只印表、不碰資料庫。加 `--apply` 才寫回。
    寫回走 `Store.upsert_signal`，人工狀態（已詢問／湊單籃／已買）與筆記不會被洗掉。

    重算走完整的出貨路徑（`scoring.evaluate`），所以上限、旗標、分數與 reason
    會一起更新——只改上限不改旗標會留下「說值得出價、但沒有上限」的自相矛盾列。
    """
    from .bidding import recompute_ceilings
    from .cards import CardIndex
    from .comps import CompsEngine
    from .valuation import build_valuator

    cfg = load_config()
    store = Store(cfg.db_path)
    fx = FxRates(cfg)
    index = CardIndex.load()
    valuator = build_valuator(cfg, store, index)
    changes = recompute_ceilings(
        store.list_signals(state="all", limit=limit),
        cfg, fx,
        comps_engine=CompsEngine(cfg, fx, store),
        valuator=valuator,
        apply_to=store if apply else None,
    )
    if not changes:
        console.print("[dim]資料庫裡沒有競標標的，沒有東西要重算。[/dim]")
        return

    changes.sort(key=lambda c: -c.sort_weight)
    t = Table(title=f"出價上限重算對照（{'已寫回' if apply else 'dry-run，未寫回'}）")
    for col in ("標題", "層級", "有效n", "校準群", "重算前 ¥", "重算後 ¥", "變動"):
        t.add_column(col, max_width=30 if col == "標題" else None,
                     justify="left" if col in ("標題", "層級", "校準群") else "right")
    for c in changes:
        before = f"{c.before_jpy:,.0f}" if c.before_jpy else "—"
        after = f"{c.after_jpy:,.0f}" if c.after_jpy else "[red]撤掉[/red]"
        delta = c.delta_jpy
        move = (
            f"{delta:+,.0f}" if delta is not None
            else ("[red]不再給上限[/red]" if c.before_ok else "—")
        )
        t.add_row(c.title[:30], c.level or "—", str(c.n_effective),
                  c.calibration_group or "—", before, after, move)
    console.print(t)

    pulled = sum(1 for c in changes if c.before_ok and not c.after_ok)
    added = sum(1 for c in changes if not c.before_ok and c.after_ok)
    console.print(
        f"[dim]共 {len(changes)} 筆競標標的：撤掉上限 {pulled} 筆、新增上限 {added} 筆、"
        f"仍有上限 {sum(1 for c in changes if c.after_ok)} 筆。[/dim]"
    )
    if not apply:
        console.print("[yellow]這是 dry-run。要寫回請加 --apply。[/yellow]")
    for c in changes:
        if c.before_ok and not c.after_ok:
            console.print(f"[dim]· {c.title[:40]} → {c.reason}[/dim]")


@app.command()
def resolve_grades(
    apply: bool = typer.Option(False, "--apply", help="真的寫回資料庫（預設只試算）"),
    limit: int = typer.Option(50, help="最多處理幾筆（每筆會開一次商品頁）"),
):
    """對「鑑定分數不明」的訊號開商品頁，從**描述文字**補抓分數。

    為什麼要有這個指令：`bidding.EvidenceGate.require_known_grade` 對
    grade=None 一律不給出價上限，所以標題只寫「鑑定品」的標的整批不可行動。
    但日文賣家常把分數寫在描述裡（「PSA5ですが」），標題只留機構名。

    **圖片 OCR 刻意不做**：成本高、不可靠，而且分數抓錯的方向正好是
    「公允價被高估、上限開太高」。描述也抓不到時，這裡會據實告訴你
    「請自己看照片上的鑑定殼」並附上商品頁連結。

    預設 dry-run。`--apply` 才寫回（走 `Store.upsert_signal`，人工狀態與筆記不動）。
    """
    from .appraise import recover_missing_grades
    from .cards import CardIndex
    from .comps import CompsEngine
    from .sources.base import CachedFetcher
    from .valuation import build_valuator

    cfg = load_config()
    store = Store(cfg.db_path)

    # --- 先量規模：這個缺口有多大、分佈在哪些來源 ---------------------
    coverage = store.grade_coverage()
    t0 = Table(title="鑑定分數缺口（signals 全表）")
    for col in ("來源", "總筆數", "分數不明", "佔比"):
        t0.add_column(col, justify="left" if col == "來源" else "right")
    total = unknown_total = 0
    for row in coverage:
        n, unknown = int(row["n"] or 0), int(row["unknown"] or 0)
        total += n
        unknown_total += unknown
        t0.add_row(row["site"], str(n), str(unknown), f"{unknown / n:.0%}" if n else "—")
    if total:
        t0.add_row("總計", str(total), str(unknown_total),
                   f"{unknown_total / total:.0%}", style="bold")
    console.print(t0)

    rows = store.signals_missing_grade(limit=limit)
    if not rows:
        console.print("[dim]沒有分數不明的訊號，這個缺口目前是空的。[/dim]")
        return

    fx = FxRates(cfg)
    index = CardIndex.load()
    fetcher = CachedFetcher(cfg)
    waf = None
    try:
        # Buyee 系要 WAF token，但**只在真的遇到 Buyee 標的時才開瀏覽器**——
        # 一顆 token TTL 只有約 5 分鐘，先開好再慢慢抓等於開一顆就過期。
        if any(str(r.get("site") or "").startswith("buyee_") and
               "/item/yahoo/" not in (r.get("url") or "") for r in rows):
            from .sources.waf import WafSession

            waf = WafSession(cfg)
        results = recover_missing_grades(
            cfg, rows, fx=fx,
            comps_engine=CompsEngine(cfg, fx, store),
            valuator=build_valuator(cfg, store, index),
            fetcher=fetcher, waf=waf,
            apply_to=store if apply else None,
        )
    finally:
        fetcher.close()
        if waf is not None:
            waf.close()

    t = Table(title=f"描述補抓分數（{'已寫回' if apply else 'dry-run，未寫回'}）")
    for col in ("標題", "來源", "有描述", "補到的分數", "來源標記", "出價上限"):
        t.add_column(col, max_width=34 if col == "標題" else None,
                     justify="left" if col in ("標題", "來源", "來源標記") else "right")
    for r in results:
        grade = f"{r.grader} {r.grade:g}" if r.recovered else (
            "[red]矛盾→無法判定[/red]" if r.conflict else "—"
        )
        bid = (
            f"¥{r.after_bid_jpy:,.0f}" if r.after_bid_jpy
            else ("[dim]仍不給[/dim]" if r.recovered else "—")
        )
        t.add_row(r.title[:34], r.site, "有" if r.has_description else "無",
                  grade, r.grade_source or "—", bid)
    console.print(t)

    hit = sum(1 for r in results if r.recovered)
    with_desc = sum(1 for r in results if r.has_description)
    conflicts = sum(1 for r in results if r.conflict)
    newly = sum(1 for r in results if r.after_bid_ok and not r.before_bid_ok)
    console.print(
        f"[dim]處理 {len(results)} 筆：有描述可讀 {with_desc} 筆、"
        f"補到分數 {hit} 筆（命中率 {hit / len(results):.0%}；"
        f"就有描述的那些算是 {hit / with_desc:.0%}）[/dim]"
        if with_desc
        else f"[dim]處理 {len(results)} 筆：**沒有任何一筆讀得到描述**"
             "（Buyee 代購頁不轉載賣家描述，eBay 沒有抓取路徑）[/dim]"
    )
    console.print(
        f"[dim]矛盾而判定「無法判定」{conflicts} 筆（刻意不猜）；"
        f"**因此多出來的可行動標的：{newly} 筆**。[/dim]"
    )
    for r in results:
        if not r.recovered:
            console.print(f"[dim]· {r.title[:34]} → {r.note}｜{r.url}[/dim]")
    if not apply:
        console.print("[yellow]這是 dry-run。要寫回請加 --apply。[/yellow]")


@app.command()
def value(
    coverage: bool = typer.Option(False, help="額外跑 conformal 區間的留出法覆蓋率自檢"),
    trials: int = typer.Option(200, help="覆蓋率自檢的重複次數"),
):
    """對資料庫裡的訊號跑估價，印出點估計／層級／有效 n／區間／P(值得買)。"""
    from .cards import CardIndex
    from .valuation import Params, build_valuator, coverage_check, estimate_signal_row

    cfg = load_config()
    store = Store(cfg.db_path)
    index = CardIndex.load()
    v = build_valuator(cfg, store, index)
    console.print(
        f"[dim]樣本 {len(v.rows)} 筆｜校準集 {v.calibration_n} 筆"
        f"｜卡片主檔 {len(index)} 張{'（缺檔，估價退到不看卡名）' if not index.available else ''}"
        f"[/dim]"
    )

    rows = store.list_signals(state="all", limit=200)
    t = Table(title="估價")
    # 「平台」欄不是裝飾：公允價已按標的自己的平台校正過，不標出來的話
    # Yahoo 的 763 與 Mercari 的 1,570 看起來會像同一個宣稱（實為同一張卡）。
    for col in ("公允價 NT$", "平台", "層級", "有效n",
                f"{cfg.valuation.get('confidence', 0.8):.0%} 區間",
                "P(值得買)", "到手 NT$", "標題"):
        t.add_column(col, max_width=44 if col == "標題" else None,
                     justify="right" if "NT$" in col or col in ("有效n", "P(值得買)") else "left")
    for row in rows:
        e = estimate_signal_row(v, row)
        rng = f"{e.lo_twd:,.0f}–{e.hi_twd:,.0f}" if e.has_interval else "樣本不足以校準"
        p = f"{e.p_worth_buying:.0%}" if e.p_worth_buying is not None else "—"
        t.add_row(
            f"{e.fair_twd:,.0f}" if e.fair_twd else "—",
            (row.get("site") or "未指定") if e.venue_adjusted else "混合(未校正)",
            f"{e.level} {e.level_label}", str(e.n_effective), rng, p,
            f"{row['landed_twd']:,.0f}", row["title"][:44],
        )
    console.print(t)

    if coverage:
        res = coverage_check(v.rows, Params.from_config(cfg), trials=trials)
        emp = res["empirical"]
        console.print(
            f"\n[bold]覆蓋率自檢[/bold]（留出法 × {trials} 輪）\n"
            f"  名目 {res['nominal']:.0%} → 實測 "
            f"[bold]{emp:.1%}[/bold]（測試樣本 {res['n_tested']} 次）\n"
            f"  平均校準集 {res['mean_calibration_n']:.0f} 筆"
            f"｜區間中位寬度 NT${res['median_width_twd']:,.0f}\n"
            "[dim]覆蓋保證的前提是資料可交換；市場會漂移、成交樣本有選擇偏差"
            "（賣得掉的才有成交價），所以實際覆蓋率預期略低於名目值。[/dim]"
        )


def _print_scan(r: dict) -> None:
    # 「訊號」與「有觸發」必須分開印：keep_all 打開後訊號數含大量「只是符合條件」
    # 的候選，兩者混成一個數字會把「118 筆候選」讀成「118 個撿漏機會」。
    trig = (
        f" ／其中觸發 [bold green]{r['triggered']}[/bold green]"
        if r.get("keep_all") and "triggered" in r else ""
    )
    expired = f"｜過期 {r['expired']}" if r.get("expired") else ""
    # 「還原」是清除功能自己的誤殺自癒——與「過期」方向相反，分開印不合成。
    # 安靜地把標的放回去，使用者就永遠不知道離場判定有多不準（第五節）。
    n_restored = (r.get("restored") or {}).get("restored") or 0
    restored = f"｜還原 {n_restored}" if n_restored else ""
    console.print(
        f"\n掃描 [bold]{r['scanned']}[/bold] 筆 → "
        f"符合年代 [bold]{r['candidates']}[/bold] → "
        f"訊號 [bold]{r['signals']}[/bold]{trig}"
        f"（新 {r.get('new', 0)}）｜行情 +{r['comps_added']}{expired}{restored}"
    )
    # 在架觀測帳的落帳。**「判定消失」與「擠出觀測窗」必須分開印**：
    # 兩者都是「這輪沒看到」，但只有前者能當成交的 proxy（見 store.record_listing_scan）。
    obs = r.get("listing_obs") or {}
    if obs:
        console.print(
            f"[dim]在架觀測帳：新 {obs.get('new', 0)}／更新 {obs.get('updated', 0)}"
            f"｜判定消失 {obs.get('disappeared', 0)}｜擠出觀測窗 {obs.get('window_exit', 0)}"
            f"｜復活(誤判) {obs.get('revived', 0)}"
            + (f"｜來源不可信而跳過 {obs['batches_skipped']} 批" if obs.get("batches_skipped") else "")
            + (f"｜清理 {r['listing_obs_pruned']} 列" if r.get("listing_obs_pruned") else "")
            + "[/dim]"
        )
    # 需求驅動回補的帳（refill）。有跑就印——安靜的回補與壞掉的回補外顯一樣。
    ref = r.get("refill") or {}
    if ref:
        console.print(
            f"[dim]需求回補：選 {len(ref.get('selected') or [])} 張卡"
            f"｜查詢 {ref.get('queries', 0)}（完成觀測 {ref.get('queries_ok', 0)}）"
            f"｜收 {ref.get('kept', 0)} 筆、擋 {ref.get('rejected', 0)} 筆"
            + (f"｜冷卻中跳過 {len(ref['skipped_cooldown'])} 張"
               if ref.get("skipped_cooldown") else "")
            + (f"｜來源失敗 {len(ref['errors'])} 次" if ref.get("errors") else "")
            + "[/dim]"
        )
    # 狙擊比對的觀測帳。**「比對過幾筆」要跟「命中幾筆」一起印**：只印命中數的話
    # 0 有兩種讀法——今天市場上真的沒有那張卡，或掛鉤壞了根本沒在比
    # （CLAUDE.md 第五節）。追蹤 0 張卡時整行不印，沒登錄就不該有這行雜訊。
    sn = r.get("snipe") or {}
    if sn.get("watches"):
        console.print(
            f"[dim]🎯 狙擊：追蹤 {sn['watches']} 張卡"
            f"｜比對 {sn.get('compared', 0):,} 筆標題"
            f"｜命中 {sn.get('hits', 0)} 筆[/dim]"
        )
    # 每個發現管道一行：來源隔離後，哪條管道壞了必須看得見，不能藏在總數裡
    for name, s in (r.get("sources") or {}).items():
        style = "green" if s["health"] in ("ok", "empty") else "red"
        detail = f"｜{s['detail']}" if s.get("detail") else ""
        console.print(f"  [{style}]{name}[/{style}]: {s['health']}  count={s['count']}{detail}")
    # scan 只算不發：這裡預覽「daily 會送出什麼」，不燒冷卻窗口
    alerts = r.get("alerts") or []
    if alerts:
        console.print(f"\n[yellow]告警預覽（daily 會送出 {len(alerts)} 則，scan 不發）[/yellow]")
        for a in alerts:
            console.print("[dim]" + str(a).replace("[", r"\[") + "[/dim]\n")
    if not r["top"]:
        console.print("[dim]今天沒有值得看的標的。[/dim]")
        return

    t = Table(title="Top 訊號")
    t.add_column("分", justify="right")
    t.add_column("到手 NT$", justify="right")
    t.add_column("路徑")
    t.add_column("旗標")
    t.add_column("標題", max_width=48)
    for s in r["top"]:
        t.add_row(
            f"{s['score']:.0f}", f"{s['landed_twd']:,.0f}", s["route"],
            ",".join(s["flags"]), s["title"],
        )
    console.print(t)


@app.command()
def notify_preview(
    show_skipped: bool = typer.Option(True, help="一併列出被排除的標的與原因"),
    show_messages: bool = typer.Option(True, help="印出每則訊息的全文"),
):
    """**只算不送**：印出這一輪三條規則會推播什麼（調門檻時用這個，不要真的發訊息）。

    走的是與 `daily` 完全相同的判定與格式化路徑（`Pipeline.notification_outcome`
    ＋ `TelegramNotifier.render`），差別只在不呼叫 `send()`、不落推播帳。
    看到的訊息就是真的會送出去的那一則——preview 自己排一份版的話，
    調完門檻在手機上看到的會是另一個東西。
    """
    from .notify_rules import (
        RULE_AUCTION_URGENT,
        RULE_CARD_SNIPE,
        RULE_HIGH_P,
        RULE_LABEL,
        RULE_SELLER_NEW,
        RULE_SELLER_UNPRICED,
        SOURCE_MODEL,
        NotifyRules,
    )

    pipe = Pipeline()
    try:
        rules = NotifyRules.from_config(pipe.cfg)
        outcome = pipe.notification_outcome()
        console.print(
            f"[dim]候選 {len(pipe.store.notification_candidates())} 筆"
            f"（state ∈ {list(pipe.store.NOTIFY_CANDIDATE_STATES)}）｜"
            f"門檻：窗口 {rules.actionable_window_hours:.0f}h、"
            f"P>{rules.p_worth_min:.0%}、排除稀有度 {list(rules.exclude_rarities)}、"
            f"出價≥{rules.min_bids} 次且 ≤{rules.price_discovered_within_hours:.0f}h 結標、"
            f"監控賣家同儕折價≥{rules.seller_min_discount:.0%}"
            f"（沒有同儕時改用模型估值，門檻 {rules.seller_model_min_discount:.0%}；"
            f"估不了的每輪上限 {rules.seller_unpriced_max_per_run} 則）[/dim]"
        )
        _print_rule_counts(outcome)

        for rule, matches in (
            (RULE_AUCTION_URGENT, outcome.urgent),
            (RULE_HIGH_P, outcome.high_p),
            (RULE_SELLER_NEW, outcome.seller_new),
            (RULE_SELLER_UNPRICED, outcome.seller_unpriced),
            (RULE_CARD_SNIPE, outcome.card_snipe),
        ):
            t = Table(title=f"{RULE_LABEL[rule]}：命中 {len(matches)} 筆")
            t.add_column("送？", justify="center")
            t.add_column("關鍵數字", max_width=34)
            t.add_column("標題", max_width=46)
            to_send = {(m.key, m.rule) for m in outcome.to_send}
            for m in matches:
                if rule == RULE_AUCTION_URGENT:
                    from .notify import _money_str

                    ceiling_txt = (
                        _money_str(m.max_bid_native, m.native_currency, decimals=2)
                        if m.max_bid_native is not None
                        else _money_str(m.max_bid, m.currency or "JPY")
                    )
                    detail = (
                        f"剩 {m.hours_left:.1f}h｜現價 "
                        f"{_money_str(m.current_bid, m.currency or 'JPY')}"
                        f"→上限 {ceiling_txt}"
                    )
                elif rule == RULE_SELLER_UNPRICED:
                    detail = (
                        f"{m.seller_key}｜估不了：{(m.unpriced_reason or '')[:26]}…"
                    )
                elif rule == RULE_SELLER_NEW and m.judgement_source == SOURCE_MODEL:
                    detail = (
                        f"{m.seller_key}｜🤖 比模型公允價便宜 "
                        f"{m.model_discount_pct:.0f}%（無同儕）"
                    )
                elif rule == RULE_SELLER_NEW:
                    detail = (
                        f"{m.seller_key}｜👤 比同儕便宜 {m.peer_discount_pct:.0f}%"
                        f"（可比 {m.peer_n} 筆）"
                    )
                elif rule == RULE_CARD_SNIPE:
                    # 狙擊的 Match 只帶 key/rule/row，`p_worth` 是 None——掉進下面
                    # 那個 else 會 `TypeError: unsupported format string passed to
                    # NoneType.__format__`。命中 0 筆時這個迴圈根本不執行，所以
                    # 這個分支缺了會一路綠到「真的有一張卡上架」那天才炸。
                    w = m.row.get("watch") or {}
                    price = m.row.get("price_native")
                    price_s = (f"{m.row.get('currency') or ''} {price:,.0f}"
                               if price is not None else "價格不明")
                    mark = "🎯" if m.row.get("tier") == "exact" else "👀"
                    detail = (f"{mark} {w.get('grader', '')}{w.get('grade_label', '')} "
                              f"{w.get('name_ja', '')}｜{price_s}")
                else:
                    detail = f"P={m.p_worth:.0%}｜稀有度 {m.rarity or '未知'}"
                t.add_row("✔" if (m.key, m.rule) in to_send else "—", detail, m.title)
            console.print(t)

        if show_messages:
            for m in outcome.to_send:
                console.print(f"\n[bold cyan]── {RULE_LABEL[m.rule]}／{m.key} ──[/bold cyan]")
                console.print(pipe.notifier.render(m))
            if outcome.overflow:
                from .notify import format_overflow

                console.print("\n[bold cyan]── 併列統計 ──[/bold cyan]")
                console.print(format_overflow(outcome.overflow, pipe.notifier.dashboard_url))

        if show_skipped and outcome.skipped:
            t = Table(title=f"被排除 {len(outcome.skipped)} 筆（為什麼沒收到通知）")
            t.add_column("規則")
            t.add_column("P", justify="right")
            t.add_column("原因", max_width=46)
            t.add_column("標題", max_width=40)
            short = {
                RULE_AUCTION_URGENT: "1", RULE_HIGH_P: "2",
                RULE_SELLER_NEW: "3", RULE_SELLER_UNPRICED: "3b",
                RULE_CARD_SNIPE: "4",
            }
            for s in outcome.skipped:
                t.add_row(
                    short.get(s.rule, s.rule),
                    "–" if s.p_worth is None else f"{s.p_worth:.0%}",
                    s.reason, s.title,
                )
            console.print(t)
        console.print("[dim]（preview 不送任何訊息、不落推播帳）[/dim]")
    finally:
        pipe.close()


@app.command()
def recall_study(
    spec_path: str = typer.Argument(..., help="研究設定檔（YAML/JSON），見 recall.py 頂註"),
    out: str = typer.Option(None, "--out", help="結果落檔路徑（.json）。預設 reports/recall-<時間>.json"),
    price_ceiling: bool = typer.Option(
        True, help="是否套用與實際掃描相同的價格上限（關掉會量到更大的池，但那不是掃描看得到的）"
    ),
    dry_run: bool = typer.Option(False, help="只印會打哪些查詢與請求數，不真的抓"),
):
    """量「這組查詢到底看得到多少貨」。**改 watchlist 之前先跑這個。**

    每組設定各跑一次，輸出：候選數、雜訊率、邊際貢獻（拿掉它聯集少多少）、
    貪婪選法的每步淨增量，以及各分組（例如 old/new）的聯集與請求數。

    為什麼要有這個指令：「沒搜到」是本工具最難察覺的失敗——它與「今天市場
    沒好貨」外顯一模一樣，沒有錯誤訊息、沒有健康告警。憑感覺加查詢會讓每輪
    請求數上升卻不知道換到了什麼；有了這把尺，加法與減法用的是同一個數字。
    """
    from pathlib import Path

    import yaml

    from .recall import load_variants, run_study, save_report

    spec = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8"))
    variants = load_variants(spec)

    if dry_run:
        t = Table(title=f"{len(variants)} 組設定（dry-run，不抓）")
        for col in ("label", "group", "source", "keyword", "category", "pages"):
            t.add_column(col)
        for v in variants:
            t.add_row(v.label, v.group, v.source, v.keyword or "(空)", v.category or "—", str(v.pages))
        console.print(t)
        console.print(f"[dim]預估請求數：{sum(v.pages for v in variants)}[/dim]")
        return

    pipe = Pipeline()
    try:
        def _progress(o) -> None:
            console.print(
                f"[dim]· {o.variant.label:<28} {o.health:<16} "
                f"解析 {o.parsed:>3} → 回傳 {o.listings:>3} → 候選 {o.candidates:>3}[/dim]"
            )

        report = run_study(
            pipe.cfg, pipe.sources, variants,
            max_price_for=(pipe._price_ceiling_jpy if price_ceiling else None),
            progress=_progress,
        )
    finally:
        pipe.close()

    _print_recall(report)
    stamp = report["generated_at"][:19].replace(":", "").replace("-", "")
    path = Path(out) if out else load_config().root / "reports" / f"recall-{stamp}.json"
    console.print(f"\n[green]結果已存檔[/green] {save_report(report, path)}")


def _print_recall(report: dict) -> None:
    """把召回率研究印成三張表：逐組、邊際貢獻、分組聯集。"""
    cov = report["coverage"]
    t = Table(title=f"逐組結果（共 {report['total_requests']} 個請求）")
    for col, just in (
        ("label", "left"), ("group", "left"), ("source", "left"), ("keyword", "left"),
        ("cat", "left"), ("health", "left"), ("解析", "right"), ("回傳", "right"),
        ("候選", "right"), ("雜訊率", "right"),
    ):
        t.add_column(col, justify=just)
    for v in report["variants"]:
        noise = v["noise_rate"]
        t.add_row(
            v["label"], v["group"], v["source"], v["keyword"] or "(空)",
            v["category"] or "—", v["health"],
            str(v["parsed"]), str(v["listings"]), str(v["candidates"]),
            "—" if noise is None else f"{noise:.0%}",
        )
    console.print(t)

    t2 = Table(title=f"邊際貢獻（聯集 {cov['union_candidates']} 筆候選）")
    t2.add_column("label")
    t2.add_column("邊際貢獻", justify="right")
    t2.add_column("順序淨增", justify="right")
    seq = cov["sequential_net_new"]
    for label, gain in cov["marginal_gains"].items():
        t2.add_row(label, str(gain), str(seq.get(label, 0)))
    console.print(t2)

    if cov["greedy_order"]:
        t3 = Table(title="貪婪選法（每步能多帶幾筆新的）")
        t3.add_column("#", justify="right")
        t3.add_column("label")
        t3.add_column("淨增", justify="right")
        t3.add_column("累計", justify="right")
        running = 0
        for i, step in enumerate(cov["greedy_order"], 1):
            running += step["net_new"]
            t3.add_row(str(i), step["label"], str(step["net_new"]), str(running))
        console.print(t3)

    if cov["groups"]:
        t4 = Table(title="分組聯集（前後對照）")
        t4.add_column("group")
        t4.add_column("組數", justify="right")
        t4.add_column("聯集候選", justify="right")
        t4.add_column("請求數", justify="right")
        for g, info in cov["groups"].items():
            t4.add_row(g, str(len(info["labels"])), str(info["union_candidates"]),
                       str(info["requests"]))
        console.print(t4)

    if cov["unobservable"]:
        console.print(
            f"[yellow]⚠️ 不可觀測（被擋／解析壞／連線失敗）："
            f"{', '.join(cov['unobservable'])}——這幾組不參與覆蓋率運算[/yellow]"
        )


DEFAULT_BASELINE = "data/corpus_baseline.json"


def _print_corpus_summary(corpus) -> None:
    """語料本身的體檢。**先印這個再印比對結果**——語料縮水會讓比對假性乾淨。"""
    t = Table(title=f"全語料快照（{corpus.taken_at}）")
    t.add_column("來源")
    t.add_column("筆數", justify="right")
    t.add_row("DB（signals＋comps＋listing_obs，distinct）", f"{corpus.n_db_titles:,}")
    t.add_row(f"快取（{corpus.n_files:,} 個檔案，distinct）", f"{corpus.n_cache_titles:,}")
    t.add_row("標題聯集（實際拿去判定的）", f"{len(corpus.titles):,}", style="bold")
    t.add_row(
        f"卡名主檔（{corpus.master_path.name}，含年代外與套組名）",
        f"{len(corpus.card_names):,}",
    )
    t.add_row("　其中年代內卡名（誤殺的判準）", f"{len(corpus.era_card_names):,}")
    console.print(t)

    kinds = "、".join(f"{k} {v}" for k, v in corpus.kind_counts.items())
    console.print(f"[dim]快取檔分類：{kinds}[/dim]")

    if corpus.failures:
        console.print(
            f"[red]⚠️ {corpus.n_files} 個快取檔中 {corpus.n_failures} 個解不出標題"
            "——這些標題沒有進語料，比對結果會少看這一塊：[/red]"
        )
        for f in corpus.failures:
            console.print(f"   [red]{f.path.name}  kind={f.kind}[/red]")
    else:
        console.print(
            f"[green]{corpus.n_files} 個快取檔全部落進具名分類，0 個解不出標題[/green]"
        )


def _print_changes(title: str, changes, style: str, limit: int | None = None) -> None:
    console.print(f"\n[{style}]{title}（{len(changes)} 筆）[/{style}]")
    shown = changes if limit is None else changes[:limit]
    for c in shown:
        console.print(
            f"  {c.title}\n"
            f"    [dim]before: {c.before.grader}/{c.before.grade} "
            f"candidate={c.before.candidate} {c.before.reason}"
            f"　→　after: {c.after.grader}/{c.after.grade} "
            f"candidate={c.after.candidate} {c.after.reason}[/dim]"
        )
    if limit is not None and len(changes) > limit:
        console.print(
            f"  [dim]…還有 {len(changes) - limit} 筆未列出（--max-listed 調整）[/dim]"
        )


@app.command()
def corpus_diff(
    baseline: str = typer.Option(
        DEFAULT_BASELINE, "--baseline", help="基準快照檔路徑"
    ),
    save_baseline: bool = typer.Option(
        False, "--save-baseline",
        help="把**現行規則**的判定存成基準（改規則之前先跑這個）",
    ),
    self_check: bool = typer.Option(
        False, "--self-check", help="只跑語料統計與現況判定分布，不比對",
    ),
    max_listed: int = typer.Option(
        200, "--max-listed", help="新放行／分數改變最多列幾筆（新擋掉的一律全列）",
    ),
    json_out: str = typer.Option("", "--json", help="把完整比對結果另存成 JSON"),
):
    """全語料雙向比對——`CLAUDE.md` 第一節那條驗收協定的執行體。

    改任何過濾／解析規則的標準流程（三步，順序不能換）：

    \b
      1. 改之前：ygo-sniper corpus-diff --save-baseline
      2. 改規則
      3. 改之後：ygo-sniper corpus-diff

    第 3 步會列出**每一筆新被擋掉的標題**與**每一個新被排除字命中的卡名**，
    由你逐筆判斷有沒有誤殺真卡。工具不會、也不該替你宣稱「誤殺 0」——
    哪些算真卡要人看。工具的職責是把改變壓到可人工審閱的規模並**全部列出來**。

    語料 = `data/sniper.db` 三張表的標題 ＋ `data/cache/` 的抓取快取
    ＋ `data/cards_1998_2004.json` 的卡名主檔。第三桶不可省略：市場上不是每天
    都有那張卡在賣，只掃在架標題會漏掉「排除字命中真實卡名」這一整類誤殺。

    快取有 TTL 會輪替，所以基準快照存的是**當時那份快照的逐筆判定**；
    比對只取兩側都在的標題，語料增減另外列，不會被讀成「判定改變」。
    """
    from .corpus import (
        CorpusError,
        diff_verdicts,
        judge_all,
        load_baseline,
        load_corpus,
        name_hits,
    )
    from .corpus import (
        save_baseline as write_baseline,
    )

    cfg = load_config()
    try:
        corpus = load_corpus(
            db_path=cfg.db_path,
            cache_dir=cfg.cache_dir,
            master_path=cfg.root / "data/cards_1998_2004.json",
        )
    except CorpusError as exc:
        console.print(f"[red]語料載入失敗：{exc}[/red]")
        raise typer.Exit(2) from exc

    _print_corpus_summary(corpus)
    verdicts = judge_all(corpus.titles, cfg.watchlist)
    hits = name_hits(corpus.card_names, cfg.watchlist)
    n_cand = sum(1 for v in verdicts.values() if v.candidate)
    n_hit_era = sum(1 for n, w in hits.items() if w and n in corpus.era_card_names)
    console.print(
        f"\n現行規則判定：候選 [bold]{n_cand:,}[/bold] 筆／"
        f"排除 {len(verdicts) - n_cand:,} 筆"
        f"｜排除字命中的年代內卡名 {n_hit_era} 個"
    )

    if self_check:
        reasons: dict[str, int] = {}
        for v in verdicts.values():
            if not v.candidate:
                reasons[v.reason] = reasons.get(v.reason, 0) + 1
        t = Table(title="排除原因分布（前 15）")
        t.add_column("原因")
        t.add_column("筆數", justify="right")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:15]:
            t.add_row(reason, f"{n:,}")
        console.print(t)
        if n_hit_era:
            console.print(
                f"[yellow]⚠️ 現行排除字命中了 {n_hit_era} 個年代內卡名"
                "（不一定是新的，--save-baseline → 改規則 → corpus-diff 才看得出增減）[/yellow]"
            )
        console.print(
            "[dim]--self-check 只量現況，沒有比對任何東西。"
            "要驗證規則改動請用 --save-baseline → 改規則 → corpus-diff。[/dim]"
        )
        return

    bpath = cfg.root / baseline if not Path(baseline).is_absolute() else Path(baseline)
    if save_baseline:
        write_baseline(bpath, corpus, verdicts, hits)
        console.print(
            f"[green]已存基準快照：{bpath}"
            f"（{len(verdicts):,} 筆標題判定＋{len(hits):,} 個卡名）[/green]\n"
            "[dim]現在去改規則，改完再跑 `ygo-sniper corpus-diff`。[/dim]"
        )
        return

    try:
        base = load_baseline(bpath)
    except CorpusError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    meta = base.meta
    diff = diff_verdicts(
        base, verdicts, current_name_hits=hits, era_names=corpus.era_card_names
    )

    t = Table(title=f"雙向比對（基準 {meta.get('taken_at')}，{bpath.name}）")
    t.add_column("項目")
    t.add_column("筆數", justify="right")
    t.add_row("兩側都在、實際比對的標題", f"{diff.n_compared:,}")
    t.add_row("兩側都在、實際比對的卡名", f"{diff.n_names_compared:,}")
    t.add_row("判定改變合計", f"{diff.n_changed:,}", style="bold")
    t.add_row("其中：新擋掉的標題（可能誤殺）", f"{len(diff.newly_blocked):,}",
              style="bold red" if diff.newly_blocked else "")
    t.add_row("其中：排除字新命中的卡名", f"{len(diff.names_newly_excluded):,}",
              style="bold red" if diff.names_newly_excluded else "")
    t.add_row("　　　└ 其中年代內真卡（＝誤殺）", f"{diff.n_era_names_killed:,}",
              style="bold red" if diff.n_era_names_killed else "")
    t.add_row("其中：新放行的標題（雜訊）", f"{len(diff.newly_allowed):,}")
    t.add_row("其中：機構／分數改變", f"{len(diff.grade_changed):,}")
    t.add_row("其中：卡名解除排除", f"{len(diff.names_no_longer_excluded):,}")
    t.add_row("只在基準（快取已輪替掉）", f"{len(diff.only_in_baseline):,}")
    t.add_row("只在現況（新抓到的）", f"{len(diff.only_in_current):,}")
    console.print(t)

    if diff.names_newly_excluded:
        # 全列，年代內的排在最前面
        console.print(
            f"\n[red]排除字新命中的真實卡名"
            f"（{len(diff.names_newly_excluded)} 個，"
            f"其中年代內 {diff.n_era_names_killed} 個）[/red]"
        )
        for c in diff.names_newly_excluded:
            tag = "[bold red]【年代內真卡＝誤殺】[/bold red]" if c.in_era else "[dim]【年代外】[/dim]"
            console.print(f"  {tag} {c.name}　[dim]← 排除字 {c.after}[/dim]")
    if diff.newly_blocked:
        # 全列，不抽樣：誤殺是靜默的，沒被印出來的那一筆就是看不見的那一筆
        _print_changes("新擋掉的標題（逐筆確認有沒有真卡）", diff.newly_blocked, "red")
    if diff.newly_allowed:
        _print_changes("新放行的標題", diff.newly_allowed, "yellow", limit=max_listed)
    if diff.grade_changed:
        _print_changes("機構／分數改變", diff.grade_changed, "cyan", limit=max_listed)
    if diff.names_no_longer_excluded:
        console.print(
            f"\n[green]不再被排除字命中的卡名（{len(diff.names_no_longer_excluded)} 個）[/green]"
        )
        for c in diff.names_no_longer_excluded[:max_listed]:
            console.print(f"  {c.name}　[dim]← 原本命中 {c.before}[/dim]")

    if json_out:
        outp = Path(json_out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(
            json.dumps(
                {
                    "baseline": str(bpath),
                    "baseline_meta": meta,
                    "n_compared": diff.n_compared,
                    "newly_blocked": [
                        {"title": c.title, "before": c.before.as_row(),
                         "after": c.after.as_row()}
                        for c in diff.newly_blocked
                    ],
                    "newly_allowed": [
                        {"title": c.title, "before": c.before.as_row(),
                         "after": c.after.as_row()}
                        for c in diff.newly_allowed
                    ],
                    "grade_changed": [
                        {"title": c.title, "before": c.before.as_row(),
                         "after": c.after.as_row()}
                        for c in diff.grade_changed
                    ],
                    "names_newly_excluded": [
                        {"name": c.name, "excluded_by": c.after, "in_era": c.in_era}
                        for c in diff.names_newly_excluded
                    ],
                    "names_no_longer_excluded": [
                        {"name": c.name, "was_excluded_by": c.before, "in_era": c.in_era}
                        for c in diff.names_no_longer_excluded
                    ],
                    "only_in_baseline": list(diff.only_in_baseline),
                    "only_in_current": list(diff.only_in_current),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        console.print(f"[dim]完整結果已寫到 {outp}[/dim]")

    if diff.n_changed == 0:
        console.print("\n[green]判定零改變：這次改動對全語料沒有任何行為差異。[/green]")
        return

    if diff.n_era_names_killed:
        console.print(
            f"\n[bold red]✗ 誤殺 {diff.n_era_names_killed} 個年代內真卡卡名[/bold red]"
            "——這一項不需要人工確認：命中真卡名就是誤殺，必須縮小規則"
            "（`CLAUDE.md` 第一節：誤殺數必須是 0）。"
        )
    console.print(
        f"\n[bold yellow]⚠️ 誤殺數需人工確認[/bold yellow]："
        f"上面 {len(diff.newly_blocked)} 筆「新擋掉的標題」"
        f"與 {len(diff.names_newly_excluded) - diff.n_era_names_killed} 個"
        "「年代外卡名」要逐筆看過，只要有一筆是真卡就必須縮小規則。"
        "\n[dim]工具只負責把改變全部攤開，不替你判定哪些是真卡。"
        "確認為誤殺的候選詞，記得寫成紅燈測試（tests/test_exclude_other_tcg.py），"
        "並在 config/watchlist.yaml 的「實測後剔除的候選」註記裡留下反例。[/dim]"
    )


@app.command()
def revive_rate():
    """量「疑似已離場」判定的錯誤率——調 gone_confidence 之前先跑這個。

    復活率 = 曾被判離場、後來又出現的比例。設定檔 `scan.gone_confidence`
    的分級規則是：復活率 < 35% 且 n >= 20 → medium，其餘一律 low。
    """
    cfg = load_config()
    stats = Store(cfg.db_path).revive_rate_by_source()
    if not stats:
        console.print("[dim]還沒有任何離場觀測——爬蟲跑幾輪之後再來量。[/dim]")
        return

    current = (cfg.scan or {}).get("gone_confidence", {})
    t = Table(title="離場判定的復活率（越高越不可信）")
    t.add_column("來源")
    t.add_column("曾判離場", justify="right")
    t.add_column("復活過", justify="right")
    t.add_column("復活率", justify="right")
    t.add_column("目前設定", justify="right")
    t.add_column("建議", justify="right")
    for site, s in sorted(stats.items(), key=lambda kv: -kv[1]["ever_gone"]):
        suggest = "medium" if s["pct"] < 35 and s["ever_gone"] >= 20 else "low"
        now = current.get(site, current.get("_default", "low"))
        t.add_row(
            site, str(int(s["ever_gone"])), str(int(s["revived"])),
            f"{s['pct']}%", now,
            f"[yellow]{suggest}[/yellow]" if suggest != now else suggest,
        )
    console.print(t)
    console.print(
        "[dim]這個比率是下界：目前仍標記離場的列未來還可能復活。[/dim]"
    )


@app.command()
def expiry_stats():
    """清除功能的自我體檢：清掉幾筆、其中幾筆又自己回來了。"""
    cfg = load_config()
    s = Store(cfg.db_path).expiry_stats()

    t = Table(title="清除已離場標的")
    t.add_column("指標")
    t.add_column("值", justify="right")
    t.add_row("目前處於「被清除」狀態", str(s["cleared_now"]))
    t.add_row("累計自動還原（誤殺）", str(s["restored_total"]))
    for state, n in sorted(s["by_cleared_from"].items()):
        t.add_row(f"　來源分頁：{state}", str(n))
    console.print(t)

    if s["restored_total"]:
        total = s["cleared_now"] + s["restored_total"]
        pct = round(100.0 * s["restored_total"] / total, 1) if total else 0.0
        console.print(
            f"[yellow]清掉的東西有 {pct}% 後來又上架了[/yellow]"
            "[dim]——這是本功能自己的誤殺率，偏高就去看 revive-rate 調 gone_confidence。[/dim]"
        )

    t2 = Table(title="各分頁現況")
    t2.add_column("分頁")
    t2.add_column("筆數", justify="right")
    for state, n in sorted(s["by_state"].items()):
        t2.add_row(state, str(n))
    console.print(t2)


@app.command()
def clear_departed(
    state: str = typer.Option(
        "watching", help="要清哪個分頁（watching / asked_seller / offer_sent）"
    ),
    dry_run: bool = typer.Option(
        False, help="只驗證並逐筆列出 verdict，一個 byte 都不寫庫"
    ),
):
    """清除已離場標的——gone 候選逐筆開商品頁驗證，只清有實證的。

    2026-08-07 事故的修法：`disappeared_at` 推論一鍵誤清 43 筆（誤殺率 100%），
    改為**驗證取代推論**。ended（end_time 已過）是事實直接清；gone 候選開頁
    驗證，只清 SOLD／DELISTED；STILL_LIVE 解除離場標記；UNVERIFIABLE
    （被擋／讀不到）一律保留並大聲回報——讀不到 ≠ 已離場。

    非 dry-run 與 dashboard 的清除按鈕走**同一條** store 路徑
    （`clear_expired_signals` + `build_page_verifier`），dry-run 的候選也來自
    同一個入口（`departed_candidates`）——兩邊看到的清單不會漂移。
    """
    from .expiry import gone_confidence_from_config
    from .verify_departed import VerifyResult, build_page_verifier

    cfg = load_config()
    store = Store(cfg.db_path)
    gone_conf = gone_confidence_from_config(cfg)
    try:
        cand = store.departed_candidates(state, gone_confidence=gone_conf)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    gone, ended = cand["gone"], cand["ended"]
    if not gone and not ended:
        console.print(f"[dim]{state} 分頁沒有離場候選，沒有事可做。[/dim]")
        return

    titles = {r["key"]: str(r.get("title") or "") for r in gone}
    results: list[VerifyResult] = []
    verifier = build_page_verifier(cfg)
    try:
        if dry_run:
            for r in gone:
                results.append(verifier(r["key"], str(r.get("url") or "")))
        else:

            def _recording(key: str, url: str) -> VerifyResult:
                res = verifier(key, url)
                results.append(res)
                return res

            outcome = store.clear_expired_signals(
                state, gone_confidence=gone_conf, verifier=_recording
            )
    finally:
        verifier.close()

    if dry_run:
        t = Table(title=f"clear-departed dry-run（{state}；只驗證，未寫庫）")
        t.add_column("標的")
        t.add_column("標題", max_width=30)
        t.add_column("verdict")
        t.add_column("說明", max_width=44)
        for res in results:
            t.add_row(res.key, titles.get(res.key, "")[:30], res.verdict, res.detail)
        console.print(t)
        would = sum(1 for r in results if r.clears)
        console.print(
            f"[dim]另有 ended（end_time 已過）{len(ended)} 筆——那是事實，"
            "不花驗證流量，非 dry-run 會直接清。[/dim]"
        )
        console.print(
            f"[dim]非 dry-run 將清 {would + len(ended)} 筆"
            f"（頁面實證 {would}＋ended {len(ended)}）。[/dim]"
        )
    else:
        bv = outcome["by_verdict"]
        console.print(
            f"已清 {outcome['cleared']} 筆"
            f"（ended {bv['ended']}、售出 {bv['sold']}、下架 {bv['delisted']}）"
            f" · 仍在架 {bv['still_live']} 筆已解除離場標記"
        )

    # 讀不到的要大聲（CLAUDE.md 第五節）：dry-run 与非 dry-run 都印
    unver = [r for r in results if r.verdict == "UNVERIFIABLE"]
    if unver:
        console.print(
            f"[yellow]無法驗證 {len(unver)} 筆——讀不到 ≠ 已離場，全部保留：[/yellow]"
        )
        for r in unver:
            console.print(f"[dim]· {r.key}｜{r.detail}[/dim]")
    else:
        console.print("[dim]無法驗證 0 筆。[/dim]")


if __name__ == "__main__":
    app()
