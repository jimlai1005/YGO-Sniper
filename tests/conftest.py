import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ygo_sniper.config import load_config  # noqa: E402
from ygo_sniper.domain import Currency, Listing, Site  # noqa: E402


@pytest.fixture(autouse=True)
def _mute_telegram(monkeypatch):
    """全套件靜音 Telegram：測試絕不碰真實世界。

    工作樹裡有真的 .env（config._load_dotenv 會把 TELEGRAM_BOT_TOKEN 灌進 os.environ，
    而且是 setdefault——清掉環境變數後，只要有人再 load_config 就又回來了），
    所以承重的防線是**從送出點斷**：TelegramNotifier.send 是唯一真的打 httpx 的地方，
    這裡把它換成會爆的 stub，誤送會立刻紅燈而不是靜靜送出去。
    清環境變數只是順手多一層。
    """
    from ygo_sniper import notify as notify_mod

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(
        notify_mod.TelegramNotifier,
        "send",
        lambda self, text, photo=None: (_ for _ in ()).throw(
            AssertionError("測試不得送出真實 Telegram 訊息（請自行 mock send/send_alert）")
        ),
    )


@pytest.fixture(autouse=True)
def _block_ebay_oauth(monkeypatch):
    """全套件封住 eBay 的真實 OAuth。測試絕不碰真實世界。

    為什麼需要這道（2026-08-03 實裝）：工作樹裡的真 `.env` 帶著
    EBAY_CLIENT_ID／SECRET，`config._load_dotenv` 會把它灌進環境變數，於是
    `cfg.ebay_enabled` 在測試裡是 True。鑑價支援 eBay 網址之後，任何一個
    不小心拿 eBay 網址當「不支援的網址」的測試都會**真的打 eBay API**——
    這正是那次抓到的事故（test_appraise 的補抓測試安靜地變成在打網路）。

    快取的 token 仍然放行（`_token`／`_token_exp` 自己設好的測試不受影響），
    真的要出門取 token 才會爆——爆掉的訊息會直接說怎麼修。
    """
    from ygo_sniper.sources import ebay as ebay_mod

    def _guarded(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        raise AssertionError(
            "測試不得向 eBay 取真實 token："
            "請注入假的 EbaySource（appraise(..., ebay=FakeEbay())）"
            "或先設定 src._token / src._token_exp"
        )

    monkeypatch.setattr(ebay_mod.EbaySource, "_get_token", _guarded)


@pytest.fixture(autouse=True)
def _no_us_query_sleep(monkeypatch):
    """鑑價的美國地址重查在兩次對外請求間睡 2 秒（對外禮貌）。
    測試裡的 eBay 都是假的（FakeEbay），睡真的 2 秒只是拖慢整個 suite。"""
    from ygo_sniper import appraise as appraise_mod

    monkeypatch.setattr(appraise_mod, "_us_query_sleep", lambda s: None)


class FakeFx:
    """固定匯率，測試不該依賴網路或當日行情。"""

    def __init__(self, jpy=0.21, usd=31.5, markup=0.015, buffer=0.02):
        self._r = {"JPY": jpy, "USD": usd, "TWD": 1.0}
        self.card_markup = markup
        self.safety_buffer = buffer
        self.source = "test"

    def to_twd(self, amount, currency, *, apply_markup=True):
        code = currency.value if hasattr(currency, "value") else str(currency)
        twd = amount * self._r[code]
        if apply_markup:
            twd *= (1 + self.card_markup) * (1 + self.safety_buffer)
        return twd

    def twd_to(self, amount_twd, currency):
        """`to_twd(..., apply_markup=False)` 的反函數（與 `fx.FxRates.twd_to` 同義）。

        測試替身少一個方法，被測程式碼就會 AttributeError 而不是算錯——
        但那代表這個替身根本沒在替 FxRates 的完整介面，所以要補齊。
        """
        code = currency.value if hasattr(currency, "value") else str(currency)
        return amount_twd / self._r[code]

    @property
    def rates(self):
        return dict(self._r)


@pytest.fixture
def fx():
    return FakeFx()


@pytest.fixture
def cfg():
    load_config.cache_clear()
    return load_config()


@pytest.fixture
def watchlist(cfg):
    return cfg.watchlist


def make_listing(price=1000, site=Site.BUYEE_MERCARI, title="test", **kw) -> Listing:
    return Listing(
        site=site,
        external_id=kw.pop("external_id", "m123"),
        title=title,
        url="https://example.test/1",
        price=price,
        currency=kw.pop("currency", Currency.JPY),
        **kw,
    )
