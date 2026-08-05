"""匯率。

線上抓失敗就退回 settings.yaml 的手動值 —— 這個 script 半夜自己跑，
不能因為匯率 API 掛掉就整批不出訊號。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .config import Config
from .domain import Currency

_RATE_URL = "https://api.exchangerate-api.com/v4/latest/{base}"
_CACHE_TTL = 12 * 3600


class FxRates:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._cache_file: Path = cfg.cache_dir / "fx.json"
        self._rates = {
            "JPY": float(cfg.fx["jpy_twd"]),
            "USD": float(cfg.fx["usd_twd"]),
            "TWD": 1.0,
        }
        self.card_markup = float(cfg.fx.get("card_markup", 0.0))
        self.safety_buffer = float(cfg.fx.get("safety_buffer", 0.0))
        self.source = "config"
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if self._cache_file.exists():
            try:
                blob = json.loads(self._cache_file.read_text())
                if time.time() - blob.get("ts", 0) < _CACHE_TTL:
                    self._rates.update(blob["rates"])
                    self.source = "cache"
                    return
            except (json.JSONDecodeError, KeyError):
                pass
        self.refresh()

    def refresh(self) -> None:
        """更新失敗完全不是錯誤，靜靜用舊值就好。"""
        try:
            with httpx.Client(timeout=10) as client:
                for base in ("JPY", "USD"):
                    r = client.get(_RATE_URL.format(base=base))
                    r.raise_for_status()
                    self._rates[base] = float(r.json()["rates"]["TWD"])
            self._cache_file.write_text(
                json.dumps({"ts": time.time(), "rates": self._rates})
            )
            self.source = "live"
        except Exception:  # noqa: BLE001 - 任何失敗都退回手動值
            self.source = "fallback"

    # ------------------------------------------------------------------
    def to_twd(self, amount: float, currency: Currency | str, *, apply_markup: bool = True) -> float:
        """換算成台幣。

        apply_markup=True 時會加上刷卡海外手續費與安全緩衝 —— 估「成本」時要開，
        估「行情 / comps」時要關，否則你會把行情也估貴，兩邊一起漂移。
        """
        code = currency.value if isinstance(currency, Currency) else str(currency).upper()
        rate = self._rates.get(code)
        if rate is None:
            raise ValueError(f"未知幣別: {code}")
        twd = amount * rate
        if apply_markup:
            twd *= (1 + self.card_markup) * (1 + self.safety_buffer)
        return twd

    def twd_to(self, amount_twd: float, currency: Currency | str) -> float:
        code = currency.value if isinstance(currency, Currency) else str(currency).upper()
        return amount_twd / self._rates[code]

    @property
    def rates(self) -> dict[str, float]:
        return dict(self._rates)
