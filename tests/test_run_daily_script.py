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

import json
import os
import re
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


def test_watchdog_ledger_path_agrees_between_shell_writer_and_python_reader():
    """`data/last_run_exit` 有兩處獨立推導：`run_daily.sh` 從 `PROJECT_DIR`
    寫，`pipeline._read_watchdog_ledger` 從 `cfg.db_path.parent` 讀。今天
    兩者算出同一個路徑，純粹是 `config/settings.yaml` 的 `storage.db_path`
    恰好長這樣（`"data/sniper.db"`）——不是因為兩者共用同一份推導。改一行
    `db_path`（例如搬進 `data/db/sniper.db`，稀鬆平常的整理）就會讓 shell
    繼續寫舊路徑、Python 開始找新路徑：讀不到檔案、折不出任何東西、
    watchdog 告警從此靜默消失——沒有錯誤、沒有 log 行、沒有測試會紅燈。
    這正是 CLAUDE.md 第五節的病，也是這次任務裡 `_ALL_SLOTS` 那個洞
    （`test_windows_match_plist`）同一種形狀，只是往外挪了一層：
    「重新推導出來的值」冒充「真正被使用的那個值」。

    路徑刻意不硬編：`PROJECT_DIR=` 與 `LAST_RUN_FILE=` 兩行都從
    `run_daily.sh` 的原始碼解析出來——硬編字串本身就是第三份推導，
    一樣會漂移，等於用另一個「重新推導的值」去驗證「重新推導的值」。

    讀的那一側刻意載入**真正的生產設定**（`load_config()`，不覆寫任何
    欄位）——這裡要驗證的正是「今天的 settings.yaml 到底算出什麼」，
    不是某個測試專用的假設定。
    """
    script_text = RUN_DAILY_SCRIPT.read_text()

    project_dir_m = re.search(r'^PROJECT_DIR="([^"]+)"', script_text, re.MULTILINE)
    assert project_dir_m, "run_daily.sh 找不到 PROJECT_DIR= 那一行，腳本格式是不是變了？"
    project_dir = project_dir_m.group(1).replace("$HOME", str(Path.home()))

    last_run_m = re.search(r'^LAST_RUN_FILE="([^"]+)"', script_text, re.MULTILINE)
    assert last_run_m, "run_daily.sh 找不到 LAST_RUN_FILE= 那一行，腳本格式是不是變了？"
    writer_path = Path(last_run_m.group(1).replace("$PROJECT_DIR", project_dir))

    import ygo_sniper.config as config_mod

    config_mod.load_config.cache_clear()
    try:
        cfg = config_mod.load_config()
        reader_path = cfg.db_path.parent / "last_run_exit"
    finally:
        config_mod.load_config.cache_clear()

    assert writer_path == reader_path, (
        f"帳本路徑兩處推導不一致：run_daily.sh 會寫到 {writer_path}，"
        f"pipeline.py 會去讀 {reader_path}——storage.db_path 動過手腳，"
        "watchdog 告警會從此靜默失聯（見本測試的 docstring）"
    )


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
        self.last_run_file = self.project_dir / "data" / "last_run_exit"
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

    def last_run_ledger(self) -> dict:
        """排程監督帳本（Fix A）：`_read_watchdog_ledger` 讀的就是這個檔案，
        用 json 解析驗證欄位，不用字串比對——欄位名才是介面，不是格式。"""
        assert self.last_run_file.exists(), "data/last_run_exit 沒有被寫出來"
        return json.loads(self.last_run_file.read_text())

    def write_fake_env(self) -> None:
        """只在**特定測試**（驗證失敗通知的送達記錄）刻意打破「沙盒永遠沒有
        .env」的預設——curl 本身仍然是沙盒 stub（見 `set_curl_stub`），
        不會打出真的網路請求，只是要讓腳本裡 `if [ -f .env ]` 那個分支
        走得進去，才能驗證 notify_attempted／notify_http 有沒有被正確填。
        """
        (self.project_dir / ".env").write_text(
            'TELEGRAM_BOT_TOKEN="fake"\nTELEGRAM_CHAT_ID="fake"\n'
        )

    def set_curl_stub(self, body: str) -> None:
        _write_executable(self.venv_bin / "curl", body)


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

    # Fix A：成功也要覆寫排程監督帳本——不覆寫的話，前一輪如果剛好是
    # exit=124，這一輪明明成功了，下一輪卻還會讀到舊的 124 紀錄。
    ledger = sandbox.last_run_ledger()
    assert ledger["exit"] == 0
    assert ledger["notify_attempted"] is False


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

    # Fix A：這是 8.5 小時卡死事故唯一留得住證據的地方——沒有 .env，
    # 失敗通知連嘗試都沒有，帳本要老實記下「沒送達」，讓下一輪的
    # ygo-sniper daily（schedule_watch.watchdog_message）撿得到。
    ledger = sandbox.last_run_ledger()
    assert ledger["exit"] == 124
    assert ledger["notify_attempted"] is False
    assert "失敗通知沒有送出" in log


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

    ledger = sandbox.last_run_ledger()
    assert ledger["exit"] == 7
    assert ledger["notify_attempted"] is False


def test_missing_venv_writes_ledger_and_attempts_notify(sandbox):
    """`.venv/bin/activate` 不見了（venv 壞掉或整包沒裝）是最早的一個
    early exit，過去只寫 log 就 `exit 1`，完全不進失敗通知、也不留任何
    排程監督看得到的痕跡——這種情況下本輪根本沒執行到 `ygo-sniper daily`，
    連 schedule_health 那條路都摸不到，所以帳本是唯一的線索。"""
    sandbox.venv_bin.joinpath("activate").unlink()

    result = sandbox.run()

    assert result.returncode == 1, result.stdout + result.stderr
    assert not sandbox.marker_file.exists(), "venv 都不見了，不該執行到 ygo-sniper stub"
    log = sandbox.log_text()
    assert "找不到 .venv" in log
    sandbox.assert_no_network_call()  # 沒有 .env，不該真的打 curl

    ledger = sandbox.last_run_ledger()
    assert ledger["exit"] == 1
    assert ledger["notify_attempted"] is False


def test_notify_delivery_recorded_when_curl_succeeds(sandbox):
    """失敗通知的 curl 真的送達（http=200）——帳本要記下「已送達」，
    這樣下一輪的 watchdog_message 才不會對著已經通知過的失敗重複講一次。

    刻意打破沙盒平常「永遠沒有 .env」的預設（見 `write_fake_env` 的
    docstring）：curl 本身仍然是沙盒 stub，不是真的網路，只是要讓
    `if [ -f .env ]` 那個分支走得進去。
    """
    sandbox.set_ygo_sniper_stub(_STUB_EXIT_7)
    sandbox.write_fake_env()
    sandbox.set_curl_stub(
        f'#!/bin/bash\ntouch "{sandbox.curl_marker_file}"\nprintf "200"\nexit 0\n'
    )

    result = sandbox.run()

    assert result.returncode == 7, result.stderr
    assert sandbox.curl_marker_file.exists(), "curl stub 應該被叫到"
    log = sandbox.log_text()
    assert "失敗通知已送達（http=200）" in log

    ledger = sandbox.last_run_ledger()
    assert ledger["exit"] == 7
    assert ledger["notify_attempted"] is True
    assert ledger["notify_http"] == "200"
    assert ledger["notify_curl_exit"] == 0


def test_notify_delivery_recorded_when_curl_itself_fails(sandbox):
    """失敗通知的 curl 自己也失敗（模擬筆電剛醒、Wi-Fi 還沒穩）——這正是
    Fix A 要解的事故形狀：連失敗通知都送不出去，帳本必須留下痕跡，
    不能像過去那樣 `> /dev/null` 把結果直接丟掉。
    """
    sandbox.set_ygo_sniper_stub(_STUB_EXIT_7)
    sandbox.write_fake_env()
    sandbox.set_curl_stub(
        f'#!/bin/bash\ntouch "{sandbox.curl_marker_file}"\nprintf "000"\nexit 6\n'
    )

    result = sandbox.run()

    assert result.returncode == 7, result.stderr
    log = sandbox.log_text()
    assert "失敗通知本身也沒送成功" in log
    assert "curl exit=6" in log

    ledger = sandbox.last_run_ledger()
    assert ledger["exit"] == 7
    assert ledger["notify_attempted"] is True
    assert ledger["notify_http"] == "000"
    assert ledger["notify_curl_exit"] == 6


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
