"""`anp.pdf.render` のテスト。

`QPdfPageRenderer` には取り消し API が無いため、「要求を積む前に抑える」
部分が壊れていないことを重点的に確かめる。

デバウンス用のタイマーが実際に発火するのを待つとテストがタイミング依存に
なるので、`flush()` を明示的に呼んで発行タイミングを制御する。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from PySide6.QtPdf import QPdfDocumentRenderOptions
from pytestqt.qtbot import QtBot

from anp.pdf.cache import RenderCache
from anp.pdf.document import DocumentController
from anp.pdf.render import (
    DEFAULT_MAX_RENDER_BYTES,
    PageRenderService,
    PageRequest,
    clamp_render_size,
)

A4_AT_72DPI = QSize(595, 842)


@pytest.fixture
def controller(sample_pdf: Path) -> Iterator[DocumentController]:
    """開いた状態の3ページ PDF。"""
    controller = DocumentController()
    controller.open(sample_pdf)
    yield controller
    controller.close()


@pytest.fixture
def service(controller: DocumentController) -> PageRenderService:
    """ドキュメントを設定済みのレンダリングサービス。"""
    service = PageRenderService(RenderCache(), debounce_ms=50)
    service.set_document(controller.document)
    return service


def requests_for(pages: range, size: QSize = A4_AT_72DPI, dpr: float = 1.0) -> list[PageRequest]:
    """ページ範囲分の要求を作る。"""
    return [PageRequest(page_index=page, size_px=size, dpr=dpr) for page in pages]


# ------------------------------------------------------------------ 要求サイズの上限
def test_small_size_is_unchanged() -> None:
    """上限に収まるサイズはそのまま。"""
    assert clamp_render_size(QSize(600, 800), max_bytes=10 * 1024 * 1024) == QSize(600, 800)


def test_large_size_is_scaled_down() -> None:
    """上限を超えるサイズは縮められる。"""
    max_bytes = 4 * 1024 * 1024

    result = clamp_render_size(QSize(8000, 10000), max_bytes=max_bytes)

    assert result.width() * result.height() * 4 <= max_bytes


def test_aspect_ratio_is_preserved_when_scaling_down() -> None:
    """縮めても縦横比が保たれる。"""
    original = QSize(8000, 10000)

    result = clamp_render_size(original, max_bytes=4 * 1024 * 1024)

    assert result.width() / result.height() == pytest.approx(
        original.width() / original.height(), rel=0.01
    )


def test_extreme_zoom_never_exceeds_the_limit() -> None:
    """極端な高倍率でも上限を超えない。"""
    for zoom in (1, 2, 4, 8, 16):
        size = QSize(595 * zoom * 4, 842 * zoom * 4)

        result = clamp_render_size(size)

        assert result.width() * result.height() * 4 <= DEFAULT_MAX_RENDER_BYTES


def test_size_is_never_zero() -> None:
    """極端に縮めても 1px を下回らない。"""
    result = clamp_render_size(QSize(10000, 10000), max_bytes=4)

    assert result.width() >= 1
    assert result.height() >= 1


# ------------------------------------------------------------------ 要求の抑制
def test_requests_are_limited_to_the_given_pages(service: PageRenderService) -> None:
    """渡された範囲の外には要求を出さない。"""
    service.request_pages(requests_for(range(0, 2)))
    service.flush()

    pages = {key.page_index for key in service.inflight_keys}
    assert pages == {0, 1}


def test_identical_requests_are_not_repeated(service: PageRenderService) -> None:
    """同じ条件の要求は積み増さない。"""
    service.request_pages(requests_for(range(0, 3)))
    service.flush()
    first = len(service.inflight_keys)

    service.request_pages(requests_for(range(0, 3)))
    service.flush()

    assert len(service.inflight_keys) == first == 3


def test_cached_pages_are_not_requested_again(service: PageRenderService, qtbot: QtBot) -> None:
    """キャッシュにあるページは再要求しない。"""
    service.request_pages(requests_for(range(0, 1)))
    with qtbot.waitSignal(service.page_ready, timeout=10_000):
        service.flush()
    assert not service.inflight_keys

    service.request_pages(requests_for(range(0, 1)))
    service.flush()

    assert not service.inflight_keys


def test_scale_change_is_debounced(service: PageRenderService) -> None:
    """倍率が変わっている間は要求を出さない。

    Ctrl+ホイールを連打しても、取り消せない要求が積み上がらないこと。
    """
    service.request_pages(requests_for(range(0, 2), size=QSize(595, 842)))
    service.flush()
    initial = len(service.inflight_keys)

    for zoom in (2, 3, 4, 5, 6):
        service.request_pages(requests_for(range(0, 2), size=QSize(595 * zoom, 842 * zoom)))

    # デバウンス中なので、途中の倍率の要求は1件も増えていない。
    assert len(service.inflight_keys) == initial


def test_only_the_final_scale_is_requested(service: PageRenderService) -> None:
    """倍率が落ち着いたら、最後の倍率の分だけを要求する。

    途中の倍率はデバウンス中に上書きされ、一度も要求されない。
    """
    for zoom in (1, 2, 3, 4):
        service.request_pages(requests_for(range(0, 1), size=QSize(595 * zoom, 842 * zoom)))

    service.flush()

    widths = {key.width_px for key in service.inflight_keys}
    assert widths == {595 * 4}


def test_scrolling_is_not_debounced(service: PageRenderService) -> None:
    """倍率が同じままページが変わる場合（スクロール）は待たされない。"""
    service.request_pages(requests_for(range(0, 2)))
    service.flush()

    service.request_pages(requests_for(range(1, 3)))
    service.flush()

    pages = {key.page_index for key in service.inflight_keys}
    assert 2 in pages


# ------------------------------------------------------------------ 世代
def test_rendered_image_lands_in_the_cache(service: PageRenderService, qtbot: QtBot) -> None:
    """レンダリング結果がキャッシュに入り、通知される。"""
    service.request_pages(requests_for(range(0, 1)))

    with qtbot.waitSignal(service.page_ready, timeout=10_000) as blocker:
        service.flush()

    assert blocker.args == [0]
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None


def test_device_pixel_ratio_is_applied(service: PageRenderService, qtbot: QtBot) -> None:
    """Qt が返す画像の DPR は 1.0 なので、要求した値を設定する。"""
    service.request_pages(requests_for(range(0, 1), dpr=2.0))

    with qtbot.waitSignal(service.page_ready, timeout=10_000):
        service.flush()

    image = service.image_for(0, A4_AT_72DPI, 2.0)
    assert image is not None
    assert image.devicePixelRatio() == pytest.approx(2.0)


def test_reset_clears_everything(service: PageRenderService, qtbot: QtBot) -> None:
    """リセットすると、処理中の要求もキャッシュも消えて世代が進む。"""
    service.request_pages(requests_for(range(0, 1)))
    with qtbot.waitSignal(service.page_ready, timeout=10_000):
        service.flush()
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None
    before = service.generation

    service.reset()

    assert service.inflight_keys == frozenset()
    assert service.generation == before + 1
    assert service.image_for(0, A4_AT_72DPI, 1.0) is None


def test_stale_results_are_discarded(service: PageRenderService) -> None:
    """前のドキュメントのレンダリング結果はキャッシュを汚さない。

    取り消せない要求が処理中のまま別の PDF を開くと、後から古い結果が
    届く。世代が違うものは捨てなければならない。
    """
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    stale_id, stale_meta = next(iter(service._requests.items()))  # noqa: SLF001

    # 別の PDF を開いた後で、前の要求の結果が届く状況を再現する。
    service.reset()
    service._on_page_rendered(  # noqa: SLF001
        stale_meta.key.page_index,
        QSize(stale_meta.key.width_px, stale_meta.key.height_px),
        QImage(stale_meta.key.width_px, stale_meta.key.height_px, QImage.Format.Format_ARGB32),
        QPdfDocumentRenderOptions(),
        stale_id,
    )

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None


def test_generation_mismatch_is_discarded(service: PageRenderService) -> None:
    """要求表に残っていても、世代が違う結果はキャッシュに入れない。

    `reset()` が要求表を消すので通常はここまで届かないが、世代照合は
    取り違えに対する二重の防御として意味を持つ。その防御そのものを検証する。
    """
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    request_id, meta = next(iter(service._requests.items()))  # noqa: SLF001

    # 要求表は消さずに世代だけ進める。
    service._generation += 1  # noqa: SLF001
    service._on_page_rendered(  # noqa: SLF001
        meta.key.page_index,
        QSize(meta.key.width_px, meta.key.height_px),
        QImage(meta.key.width_px, meta.key.height_px, QImage.Format.Format_ARGB32),
        QPdfDocumentRenderOptions(),
        request_id,
    )

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None
    # 処理中の印は外れる（同じ条件を再要求できる状態に戻る）。
    assert meta.key not in service.inflight_keys


def test_unknown_request_ids_are_ignored(service: PageRenderService) -> None:
    """身に覚えのない結果が届いても壊れない。"""
    service._on_page_rendered(  # noqa: SLF001
        0,
        QSize(100, 200),
        QImage(100, 200, QImage.Format.Format_ARGB32),
        QPdfDocumentRenderOptions(),
        99999,
    )

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None


def test_no_requests_without_a_document() -> None:
    """ドキュメントが無ければ要求を出しても壊れない。"""
    service = PageRenderService(RenderCache())

    service.request_pages(requests_for(range(0, 2)))
    service.flush()


# ------------------------------------------------------------------ 仮表示
def test_placeholder_uses_another_resolution(service: PageRenderService, qtbot: QtBot) -> None:
    """目的の解像度が無い間は、同じページの別解像度を返す。"""
    service.request_pages(requests_for(range(0, 1), size=QSize(297, 421)))
    with qtbot.waitSignal(service.page_ready, timeout=10_000):
        service.flush()

    placeholder = service.placeholder_for(0, QSize(1190, 1684))

    assert placeholder is not None
    assert placeholder.width() == 297


def test_placeholder_is_none_for_unrendered_page(service: PageRenderService) -> None:
    """まだ何も無いページには仮表示も無い。"""
    assert service.placeholder_for(2, A4_AT_72DPI) is None
