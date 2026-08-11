"""排程真正入口（scripts/run_daily.sh）的端到端測試。

CLAUDE.md 第六節「測試路徑必須等於生產路徑」記錄過兩次事故：測試繞過真正的
使用者指令去測元件，元件測試全綠，指令本身卻壞掉。這份測試直接執行**真的**
`scripts/run_daily.sh`（不是複製、不是改寫），只用環境變數與 PATH 把它會碰到
的東西（.venv、ygo-sniper、ping、curl）換成沙盒裡的假貨——腳本本身一行都不動。

⚠️ 安全：絕對不可以讓這裡執行到真正的專案目錄或真的 .env。做法是整段偽造一個
$HOME，腳本自己的 `PROJECT_DIR="$HOME/projects/ygo-sniper"` 就會落在沙盒裡而不是
`/Users/jim/projects/ygo-sniper`。沙盒裡從頭到尾不放 `.env`，所以失敗通知的
curl 分支在原始碼層級就進不去；另外還疊了一層 curl stub 當保險——就算未來有人
改掉那個 `.env` 判斷式，這裡也會在 curl 真的被呼叫的當下就被抓到，而不是要等到
真的送出 Telegram 訊息才發現。

`ping` 一樣全程 stub 成立刻成功：真的 `/sbin/ping` 在這裡沒有意義（沙盒沒有網路
需求），而且腳本失敗時會重試 6 次、每次補 10 秒，stub 掉才能讓測試維持在秒等級。
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_DAILY_SCRIPT = REPO_ROOT / "scripts" / "run_daily.sh"
RUN_WITH_TIMEOUT_SCRIPT = REPO_ROOT / "scripts" / "run_with_timeout.py"

# 給子行程的安全上限：正常路徑應該在幾秒內結束。設寬一點只是為了在忙碌 CI 機器上
# 不要假紅；如果 ping stub 失效、腳本真的跑進 6 次 x 10 秒的重試迴圈，這個 timeout
# 會讓測試明確失敗（而不是安靜地變慢、被誤以為是「稍微久一點」）。
_SUBPROCESS_TIMEOUT = 20


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


class Sandbox:
    """一份假的 `$HOME/projects/ygo-sniper`，讓真正的 run_daily.sh 誤以為它就是
    生產目錄，但其實整棵樹都在 tmp_path 底下。"""

    def __init__(self, tmp_path: Path):
        self.home_dir = tmp_path / "home"
        self.project_dir = self.home_dir / "projects" / "ygo-sniper"
        self.venv_bin = self.project_dir / ".venv" / "bin"
        self.log_dir = self.project_dir / "data" / "logs"
        self.lock_dir = self.project_dir / "data" / "run_daily.lock"
        self.marker_file = tmp_path / "ygo_sniper_ran.marker"
        self.curl_marker_file = tmp_path / "curl_called.marker"

        self.venv_bin.mkdir(parents=True)
        (self.project_dir / "scripts").mkdir(parents=True)

        # 真的 run_with_timeout.py（symlink，不複製一份——複製出來的是另一個檔案，
        # 改動真檔不會反映在測試裡，等於又繞回「測元件、不測指令」的老問題）。
        os.symlink(RUN_WITH_TIMEOUT_SCRIPT, self.project_dir / "scripts" / "run_with_timeout.py")

        # 假 activate：唯一必要行為是把這個 bin 目錄推到 PATH 最前面，
        # 這樣後面的 `source .venv/bin/activate` 之後，PATH 上的 ygo-sniper／
        # ping／curl 才會是沙盒版本而不是系統版本。
        _write_executable(
            self.venv_bin / "activate",
            '#!/bin/bash\n'
            'export PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd):$PATH"\n',
        )

        # .venv/bin/python：run_daily.sh 現在用「.venv/bin/python」這個相對路徑
        # 直接呼叫（Finding 1 的修復），不透過 PATH 搜尋，所以這裡必須是真的存在
        # 於這個路徑的可執行檔，不能只靠 PATH 上有 python。
        os.symlink(sys.executable, self.venv_bin / "python")

        # ping：立刻成功，略過腳本裡的網路等待重試迴圈。
        _write_executable(self.venv_bin / "ping", "#!/bin/bash\nexit 0\n")

        # curl：只在「失敗通知」分支、且沙盒有 .env 時才可能被呼叫——這裡永遠不放
        # .env，所以理論上不會執行到，但還是留一份會被偵測到的 stub 當第二道防線。
        _write_executable(
            self.venv_bin / "curl",
            f'#!/bin/bash\ntouch "{self.curl_marker_file}"\nexit 0\n',
        )

    def set_ygo_sniper_stub(self, body: str) -> None:
        _write_executable(self.venv_bin / "ygo-sniper", body)

    def env(self, **extra: str) -> dict:
        base = {
            "HOME": str(self.home_dir),
            # 刻意窄：模擬 launchd 幾乎是空的 PATH（腳本自己的註解就是這樣描述的），
            # venv 那段要靠腳本自己 source activate 補上，不是靠這裡先塞好。
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "YGO_TEST_MARKER": str(self.marker_file),
        }
        base.update(extra)
        return base

    def run(self, **env_extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(RUN_DAILY_SCRIPT)],
            env=self.env(**env_extra),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )

    def log_text(self) -> str:
        logs = list(self.log_dir.glob("daily-*.log"))
        assert len(logs) == 1, f"預期剛好一份 log，實際：{logs}"
        return logs[0].read_text()

    def assert_no_network_call(self) -> None:
        assert not self.curl_marker_file.exists(), (
            "curl 被呼叫了——這代表失敗通知路徑在沙盒裡意外送出了真的網路請求"
        )


@pytest.fixture
def sandbox(tmp_path) -> Sandbox:
    return Sandbox(tmp_path)


_STUB_EXIT_0 = '#!/bin/bash\ntouch "$YGO_TEST_MARKER"\nexit 0\n'
_STUB_EXIT_7 = '#!/bin/bash\ntouch "$YGO_TEST_MARKER"\nexit 7\n'
# `exec sleep N`：把行程直接換成 sleep，收到 SIGTERM 時用預設行為立刻死掉
# （不是自己 trap 忽略），watchdog 的 killpg 才能在毫秒等級把它收掉，
# 不用等到 run_with_timeout.py 的 30 秒 SIGKILL 寬限期，測試才跑得快。
_STUB_SLEEP_PAST_TIMEOUT = '#!/bin/bash\ntouch "$YGO_TEST_MARKER"\nexec sleep 30\n'


def test_happy_path_exit_0_logs_start_and_end(sandbox):
    sandbox.set_ygo_sniper_stub(_STUB_EXIT_0)

    result = sandbox.run()

    assert result.returncode == 0, result.stderr
    assert sandbox.marker_file.exists(), "ygo-sniper stub 沒有被真的執行到"
    log = sandbox.log_text()
    assert "開始" in log
    assert "結束" in log
    assert "exit=0" in log
    assert not sandbox.lock_dir.exists(), "成功結束後鎖應該被 trap 釋放掉"
    sandbox.assert_no_network_call()


def test_watchdog_timeout_exits_124_and_logs_watchdog_line(sandbox):
    """全套測試裡最有價值的一條：證明「supervisor 卡死 → 124 → 專屬告警文案」
    這條新接起來的線，從真正的 shell 入口一路到真正的 run_with_timeout.py，
    真的是通的——不是兩邊各自的單元測試看起來像會兜起來而已。"""
    sandbox.set_ygo_sniper_stub(_STUB_SLEEP_PAST_TIMEOUT)

    result = sandbox.run(YGO_CYCLE_TIMEOUT="2")

    assert result.returncode == 124, result.stdout + result.stderr
    assert sandbox.marker_file.exists(), "ygo-sniper stub 沒有被真的執行到"
    log = sandbox.log_text()
    # run_with_timeout.py 自己印的終止訊息（stdout，被 run_daily.sh 的
    # `>> "$LOG_FILE" 2>&1` 接住）：這是唯一在 log 裡看得到「為什麼是 124」
    # 的地方——run_daily.sh 組出的 124 專屬文案（MSG 變數）只會送進 curl 的
    # payload、不會回寫 log，沙盒沒有 .env 所以那段完全不會執行，本來就不該
    # 出現在 log 裡。
    assert "🚨" in log and "watchdog" in log, "watchdog 的終止訊息沒有進到 log"
    assert "exit=124" in log
    assert not sandbox.lock_dir.exists(), "被 watchdog 終止後鎖也應該被 trap 釋放掉"
    sandbox.assert_no_network_call()


def test_ordinary_failure_exit_7_records_exit_code(sandbox):
    sandbox.set_ygo_sniper_stub(_STUB_EXIT_7)

    result = sandbox.run()

    assert result.returncode == 7, result.stderr
    assert sandbox.marker_file.exists(), "ygo-sniper stub 沒有被真的執行到"
    log = sandbox.log_text()
    assert "exit=7" in log
    # 一般失敗要用泛用文案，不能被誤標成 watchdog 終止。
    assert "watchdog 強制終止" not in log
    assert not sandbox.lock_dir.exists()
    sandbox.assert_no_network_call()


def test_reentrancy_skips_when_lock_owner_is_alive(sandbox):
    """鎖目錄存在、pid 檔指向一個真的還活著的行程 → 本輪必須跳過，
    完全不去執行 ygo-sniper stub（不是執行了又提早退出，是根本沒開始）。"""
    sandbox.set_ygo_sniper_stub(_STUB_EXIT_0)
    sandbox.lock_dir.mkdir(parents=True)
    holder = subprocess.Popen(["sleep", "60"])
    try:
        (sandbox.lock_dir / "pid").write_text(str(holder.pid))

        result = sandbox.run()

        assert result.returncode == 0, result.stderr
        assert not sandbox.marker_file.exists(), (
            "本輪應該在拿鎖失敗時就跳過，不該執行到 ygo-sniper stub"
        )
        log = sandbox.log_text()
        assert "本輪跳過" in log
        # 被跳過的這一輪不是鎖的主人，不該把還活著的持有者的鎖清掉。
        assert sandbox.lock_dir.exists(), "跳過的一輪不該動到別人持有中的鎖"
        sandbox.assert_no_network_call()
    finally:
        holder.terminate()
        holder.wait(timeout=5)
