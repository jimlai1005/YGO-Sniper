#!/usr/bin/env python3
"""用法: run_with_timeout.py <timeout_seconds> <cmd> [args...]

把 cmd 放進自己的新 session（＝新 process group），超時先 SIGTERM 整組、
30s 內不退再 SIGKILL 整組。比 shell 背景工作可靠的原因：Playwright 的
node driver 與 chromium 是 cmd 的子孫，同組一次殺乾淨，不會留殭屍
佔著 log fd 或記憶體（2026-08-10 事故：launch 逾時後 pid=59906 沒人收）。

exit code：子行程正常結束就沿用；被 watchdog 終止回 124（比照 GNU timeout，
run_daily.sh 據此發「watchdog 終止」而非一般「掃描失敗」通知）。

計時用 subprocess.wait(timeout)（底層 time.monotonic）：macOS 睡眠時停走，
筆電闔蓋不會吃掉預算——watchdog 只抓「醒著卡死」。

純 stdlib、不 import 專案任何東西——它要在任何 venv 狀態下都能跑，
本身也不該是 daily 卡死時唯一會一起卡死的東西。
"""
import os
import signal
import subprocess
import sys

GRACE_SECONDS = 30


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        # 整組已經自己死透了（例如 cmd 剛好在這個瞬間正常結束），
        # 沒有行程可殺不是錯誤。
        pass


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: run_with_timeout.py <timeout_seconds> <cmd> [args...]", file=sys.stderr)
        return 2
    timeout = float(sys.argv[1])
    # start_new_session=True：cmd 變成新 session 的 leader，pgid == pid。
    # Playwright 之後 spawn 的 node driver／chromium 若不自己 setsid，
    # 都會落在同一個 process group 裡，killpg 一次收乾淨。
    proc = subprocess.Popen(sys.argv[2:], start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass

    print(
        f"🚨 [watchdog] 超過 {timeout:.0f}s 未結束，強制終止整個行程樹"
        f"（pgid={proc.pid}）——醒著卡死，可能是 Playwright 殭屍或網路黑洞",
        flush=True,
    )
    _killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _killpg(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # SIGKILL 理論上不會被擋，但行程若卡在不可中斷睡眠
            # （D state，例如壞掉的網路掛載），核心要多久才真的收掉它
            # 是我們管不到的。安全關鍵動作絕不能靜默吞掉——大聲印出來，
            # 但也不能讓 watchdog 自己在這裡無限期卡死等一個永遠不回應的
            # wait()，那樣下一輪連鎖跳過會一路傳染下去。回 124：對
            # run_daily.sh 而言這輪就是「被 watchdog 終止」，日誌裡的
            # 這行警告是唯一的真相來源，需要人去查。
            print(
                f"🚨 [watchdog] SIGKILL 後 {GRACE_SECONDS:.0f}s 仍未回收"
                f"（pgid={proc.pid}）——可能卡在不可中斷睡眠，watchdog 放棄"
                f"等待並回報，不再阻塞",
                flush=True,
            )
    return 124


if __name__ == "__main__":
    sys.exit(main())
