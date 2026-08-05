"""縮圖回填：舊 CDN 主機的改寫規則與冪等性。

背景（2026-08-03）：PayPay 由 Buyee 鏡像（`buyee_paypay`）改成原站直抓
（`paypay_direct`）之前落庫的列，縮圖指向 Buyee 的代理 CDN
`cdnyauction-pctr.buyee.jp`；直抓之後存的是原站 CDN `auc-pctr.c.yimg.jp`。
兩個主機的路徑逐字相同，實測 4 筆舊資料換主機後回來的**位元組完全一樣**。

修法是改寫而不是重抓（省 4 個商品頁請求，而且商品可能已下架），但改寫後
**必須逐筆連線驗證**才寫回：Yahoo 的圖片 CDN 對不存在的圖回 `403 image/gif`
（一張「沒有圖」的佔位 gif），只看 content-type 會把佔位圖當成真圖存回去——
那正是這道回填最初要修掉的病。

冪等：改寫過的網址主機已經是原站，第二次跑一定是 0 筆待處理。
"""

from __future__ import annotations

import httpx
import pytest

from ygo_sniper.cli import image_is_live
from ygo_sniper.sources.paypay import canonical_thumbnail_url
from ygo_sniper.store import Store

BUYEE = (
    "https://cdnyauction-pctr.buyee.jp/i/auctions.c.yimg.jp/images.auctions.yahoo.co.jp"
    "/image/dr000/auc0207/users/b801ba/i-img1200x1200-1784453428652b44x46.jpg?pri=l&w=300"
)
YAHOO = BUYEE.replace("cdnyauction-pctr.buyee.jp", "auc-pctr.c.yimg.jp")


# ---------------------------------------------------------------------------
# 1. 改寫規則本身
# ---------------------------------------------------------------------------
def test_rewrites_only_the_host():
    assert canonical_thumbnail_url(BUYEE) == YAHOO
    # query string 與路徑逐字保留——它們是同一張圖的同一組裁切參數
    assert canonical_thumbnail_url(BUYEE).endswith("?pri=l&w=300")


def test_rewrite_is_idempotent():
    once = canonical_thumbnail_url(BUYEE)
    assert canonical_thumbnail_url(once) == once
    assert canonical_thumbnail_url(canonical_thumbnail_url(once)) == once


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://static.mercdn.net/item/detail/orig/photos/m123_1.jpg",
        "https://assets.mercari-shops-static.com/-/small/plain/abc.jpg@jpg",
        "https://i.ebayimg.com/images/g/abc/s-l500.jpg",
        YAHOO,
    ],
)
def test_other_hosts_pass_through_untouched(url):
    """只認那**一個**舊主機。其他來源的縮圖（Mercari／eBay／已是原站的）不准動。"""
    assert canonical_thumbnail_url(url) == url


# ---------------------------------------------------------------------------
# 2. 驗證：狀態碼與 content-type 兩個都要看
# ---------------------------------------------------------------------------
def _stub_head(monkeypatch, status, content_type):
    def fake_head(url, timeout=None, follow_redirects=None, headers=None):
        return httpx.Response(
            status,
            headers={"content-type": content_type},
            request=httpx.Request("HEAD", url),
        )

    monkeypatch.setattr(httpx, "head", fake_head)


def test_live_image_passes(monkeypatch):
    _stub_head(monkeypatch, 200, "image/jpeg")
    assert image_is_live(YAHOO) is True


def test_403_placeholder_gif_is_not_a_live_image(monkeypatch):
    """Yahoo 對不存在的圖回 403 + image/gif。只看 content-type 會把它當真圖。"""
    _stub_head(monkeypatch, 403, "image/gif")
    assert image_is_live(YAHOO) is False


def test_200_but_not_an_image_fails(monkeypatch):
    _stub_head(monkeypatch, 200, "text/html; charset=utf-8")
    assert image_is_live(YAHOO) is False


def test_network_error_is_not_a_live_image(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "head", boom)
    assert image_is_live(YAHOO) is False


# ---------------------------------------------------------------------------
# 3. 端到端（store 層）：改寫 → 待處理清單變空 → 再跑一次是 0 筆
# ---------------------------------------------------------------------------
def _seed(tmp_path):
    store = Store(tmp_path / "t.db")
    with store._conn() as c:
        for key, image in (
            ("buyee_paypay:z1", BUYEE),
            ("buyee_paypay:z2", YAHOO),
            ("buyee_mercari:m1", "https://static.mercdn.net/item/detail/orig/photos/m1.jpg"),
        ):
            c.execute(
                "INSERT INTO signals (key, site, external_id, title, url, image_url, "
                "state, score) VALUES (?,?,?,?,?,?,'new',1)",
                (key, key.split(":")[0], key.split(":")[1], "t", "https://x/1", image),
            )
    return store


def _pending(store):
    return [
        r for r in store.all_signal_images()
        if r["image_url"] and canonical_thumbnail_url(r["image_url"]) != r["image_url"]
    ]


def test_backfill_rewrites_then_becomes_a_noop(tmp_path):
    store = _seed(tmp_path)
    pending = _pending(store)
    assert [r["key"] for r in pending] == ["buyee_paypay:z1"]

    changed = store.set_signal_images(
        [(r["key"], canonical_thumbnail_url(r["image_url"])) for r in pending]
    )
    assert changed == 1
    assert _pending(store) == [], "第二次跑必須是 0 筆待處理（冪等）"

    by_key = {r["key"]: r["image_url"] for r in store.all_signal_images()}
    assert by_key["buyee_paypay:z1"] == YAHOO
    assert by_key["buyee_paypay:z2"] == YAHOO, "已經是原站的不准被動到"
    assert by_key["buyee_mercari:m1"].startswith("https://static.mercdn.net/")
