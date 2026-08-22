.PHONY: setup test lint daily scan comps serve schedule unschedule schedule-high unschedule-high schedule-dashboard unschedule-dashboard logs clean

PY := .venv/bin/python
SNIPER := .venv/bin/ygo-sniper

# 這台機器的系統 python 是 3.9，專案需要 3.11+。
# 優先用 uv（會自己下載對應版本的 python，不動系統），沒有 uv 才退回系統 python3.11/3.12。
setup:
	@if command -v uv >/dev/null 2>&1; then \
		uv venv --python 3.12 --seed .venv && uv pip install -e ".[dev]"; \
	elif command -v python3.12 >/dev/null 2>&1; then \
		python3.12 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e ".[dev]"; \
	elif command -v python3.11 >/dev/null 2>&1; then \
		python3.11 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e ".[dev]"; \
	else \
		echo "❌ 找不到 uv 也找不到 python3.11+。裝一個：curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; \
	fi
	@test -f .env || cp .env.example .env
	@echo "\n✅ 裝好了。下一步：make test"

test:
	.venv/bin/pytest -v

lint:
	.venv/bin/ruff check src tests web

# --- 日常 ---
daily:
	$(SNIPER) daily

scan:
	$(SNIPER) scan

comps:
	$(SNIPER) comps

serve:
	$(SNIPER) serve

breakeven:
	$(SNIPER) breakeven

# --- 排程 ---
schedule:
	chmod +x scripts/run_daily.sh
	cp scripts/com.jim.ygosniper.plist ~/Library/LaunchAgents/
	launchctl unload ~/Library/LaunchAgents/com.jim.ygosniper.plist 2>/dev/null || true
	launchctl load ~/Library/LaunchAgents/com.jim.ygosniper.plist
	@echo "✅ 已排程每天 09:30。用 launchctl list | grep ygosniper 確認"

unschedule:
	launchctl unload ~/Library/LaunchAgents/com.jim.ygosniper.plist
	rm -f ~/Library/LaunchAgents/com.jim.ygosniper.plist

# 高價帶（¥8,624～50,000）獨立排程，跟上面的低價帶 daily 排程分開裝卸、
# 允許並行（各自的鎖／log／排程監督帳本都是獨立檔名，見 scripts/run_high.sh）
schedule-high:
	chmod +x scripts/run_high.sh
	cp scripts/com.jim.ygosniper.high.plist ~/Library/LaunchAgents/
	launchctl unload ~/Library/LaunchAgents/com.jim.ygosniper.high.plist 2>/dev/null || true
	launchctl load ~/Library/LaunchAgents/com.jim.ygosniper.high.plist
	@echo "✅ 高價帶已排程（白天偶數整點＋晚間 :15）。用 launchctl list | grep ygosniper.high 確認"

unschedule-high:
	launchctl unload ~/Library/LaunchAgents/com.jim.ygosniper.high.plist
	rm -f ~/Library/LaunchAgents/com.jim.ygosniper.high.plist

# dashboard 常駐（開機/登入自動起，掛了自動重啟），跟上面的 daily 排程分開裝卸
schedule-dashboard:
	chmod +x scripts/run_dashboard.sh
	cp scripts/com.jim.ygosniper.dashboard.plist ~/Library/LaunchAgents/
	launchctl unload ~/Library/LaunchAgents/com.jim.ygosniper.dashboard.plist 2>/dev/null || true
	launchctl load ~/Library/LaunchAgents/com.jim.ygosniper.dashboard.plist
	@echo "✅ dashboard 已常駐 → http://127.0.0.1:8321 。用 launchctl list | grep ygosniper.dashboard 確認"

unschedule-dashboard:
	launchctl unload ~/Library/LaunchAgents/com.jim.ygosniper.dashboard.plist
	rm -f ~/Library/LaunchAgents/com.jim.ygosniper.dashboard.plist

logs:
	@tail -n 60 data/logs/daily-$$(date +%Y%m%d).log 2>/dev/null || echo "今天還沒有 log"

# 清掉 HTTP 快取，強制重抓（調 parser 時會用到）
clean-cache:
	rm -rf data/cache/*.html
	@echo "✅ 快取已清"

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ *.egg-info
