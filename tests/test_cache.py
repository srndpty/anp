"""`anp.pdf.cache` のテスト。"""

from __future__ import annotations

import logging

import pytest
from PySide6.QtGui import QImage

from anp.pdf.cache import RenderCache, RenderKey


def make_image(width: int, height: int) -> QImage:
    """指定した大きさの画像を作る。"""
    return QImage(width, height, QImage.Format.Format_ARGB32)


def make_key(page_index: int, width: int = 100, height: int = 200, dpr: float = 1.0) -> RenderKey:
    """テスト用のキーを作る。"""
    return RenderKey(page_index=page_index, width_px=width, height_px=height, dpr=dpr)


def test_put_and_get() -> None:
    """入れた画像を取り出せる。"""
    cache = RenderCache()
    key = make_key(0)
    image = make_image(100, 200)

    cache.put(key, image)

    assert cache.get(key) is image
    assert key in cache
    assert len(cache) == 1


def test_get_missing_returns_none() -> None:
    """無いキーは None。"""
    assert RenderCache().get(make_key(0)) is None


def test_keys_differ_by_size_and_dpr() -> None:
    """同じページでもサイズや DPR が違えば別のエントリになる。

    `devicePixelRatio` は `QImage` の論理サイズに影響するため、
    幅だけでキーにすると別物を取り違える。
    """
    cache = RenderCache()
    cache.put(make_key(0, width=100, dpr=1.0), make_image(100, 200))
    cache.put(make_key(0, width=100, dpr=2.0), make_image(100, 200))

    assert len(cache) == 2


def test_replacing_a_key_does_not_double_count_bytes() -> None:
    """同じキーを入れ直しても合計バイト数が二重に増えない。"""
    cache = RenderCache()
    key = make_key(0)

    cache.put(key, make_image(100, 200))
    first_total = cache.total_bytes
    cache.put(key, make_image(100, 200))

    assert cache.total_bytes == first_total
    assert len(cache) == 1


# ------------------------------------------------------------------ 上限
def test_total_bytes_never_exceeds_limit() -> None:
    """合計バイト数が上限を超えない。"""
    one_image_bytes = make_image(100, 100).sizeInBytes()
    cache = RenderCache(max_bytes=one_image_bytes * 3)

    for page in range(10):
        cache.put(make_key(page), make_image(100, 100))
        assert cache.total_bytes <= cache.max_bytes

    assert len(cache) == 3


def test_least_recently_used_is_evicted_first() -> None:
    """追い出されるのは最後に使われたのが最も古いもの。"""
    one_image_bytes = make_image(100, 100).sizeInBytes()
    cache = RenderCache(max_bytes=one_image_bytes * 2)

    cache.put(make_key(0), make_image(100, 100))
    cache.put(make_key(1), make_image(100, 100))
    cache.get(make_key(0))  # 0 を使ったので 1 が最古になる
    cache.put(make_key(2), make_image(100, 100))

    assert make_key(0) in cache
    assert make_key(1) not in cache
    assert make_key(2) in cache


def test_oversized_image_is_not_cached(caplog: pytest.LogCaptureFixture) -> None:
    """上限より大きい画像は入れない（入れると全部追い出したうえで自分も消える）。"""
    cache = RenderCache(max_bytes=1000)

    cache.put(make_key(0), make_image(100, 100))

    with caplog.at_level(logging.WARNING):
        assert len(cache) == 0
    assert cache.total_bytes == 0


def test_clear_empties_everything() -> None:
    """clear ですべて破棄される。"""
    cache = RenderCache()
    cache.put(make_key(0), make_image(100, 200))

    cache.clear()

    assert len(cache) == 0
    assert cache.total_bytes == 0


# ------------------------------------------------------------------ 仮表示用
def test_nearest_picks_closest_width_on_same_page() -> None:
    """同じページで、要求幅にいちばん近い画像を返す。"""
    cache = RenderCache()
    cache.put(make_key(0, width=100, height=200), make_image(100, 200))
    cache.put(make_key(0, width=400, height=800), make_image(400, 800))

    result = cache.nearest(0, width_px=380)

    assert result is not None
    assert result.width() == 400


def test_nearest_ignores_other_pages() -> None:
    """他のページの画像は返さない。"""
    cache = RenderCache()
    cache.put(make_key(1, width=100), make_image(100, 200))

    assert cache.nearest(0, width_px=100) is None


def test_nearest_returns_none_when_empty() -> None:
    """候補が無ければ None。"""
    assert RenderCache().nearest(0, width_px=100) is None
