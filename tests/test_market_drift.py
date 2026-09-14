"""市場漂移監視器：同卡重複成交倍率、外部指數解析、告警帳本。"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ygo_sniper.config import load_config
from ygo_sniper.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


class TestConfig:
    def test_market_drift_section_is_loaded(self):
        cfg = load_config()
        md = cfg.market_drift
        assert md["recent_days"] == 28
        assert md["base_days"] == 56
        assert md["alert_ratio"] == 0.90
        assert md["min_pairs"] == 8
        assert md["external_url"].startswith("https://yugioh-relief.com/")
        assert md["external_enabled"] is True
