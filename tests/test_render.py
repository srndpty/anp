"""`anp.pdf.render` のテスト。

`QPdfPageRenderer` には取り消し API が無いため、「要求を積む前に抑える」
部分が壊れていないことを重点的に確かめる。

デバウンス用のタイマーが実際に発火するのを待つとテストがタイミング依存に
なるので、`flush()` を明示的に呼んで発行タイミングを制御する。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
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
    _RequestMeta,
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


@pytest.fixture
def service_with_cap(controller: DocumentController) -> PageRenderService:
    """同時要求数を2件に絞ったレンダリングサービス。"""
    service = PageRenderService(RenderCache(), max_inflight=2, debounce_ms=50)
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

    pages = {key.page_index for key in service.outstanding_keys}
    assert pages == {0, 1}


def test_identical_requests_are_not_repeated(service: PageRenderService) -> None:
    """同じ条件の要求は積み増さない。"""
    service.request_pages(requests_for(range(0, 3)))
    service.flush()
    first = len(service.outstanding_keys)

    service.request_pages(requests_for(range(0, 3)))
    service.flush()

    assert len(service.outstanding_keys) == first == 3


def test_cached_pages_are_not_requested_again(service: PageRenderService, qtbot: QtBot) -> None:
    """キャッシュにあるページは再要求しない。"""
    service.request_pages(requests_for(range(0, 1)))
    with qtbot.waitSignal(service.page_ready, timeout=10_000):
        service.flush()
    assert not service.outstanding_keys

    service.request_pages(requests_for(range(0, 1)))
    service.flush()

    assert not service.outstanding_keys


def test_scale_change_is_debounced(service: PageRenderService) -> None:
    """倍率が変わっている間は要求を出さない。

    Ctrl+ホイールを連打しても、取り消せない要求が積み上がらないこと。
    """
    service.request_pages(requests_for(range(0, 2), size=QSize(595, 842)))
    service.flush()
    initial = len(service.outstanding_keys)

    for zoom in (2, 3, 4, 5, 6):
        service.request_pages(requests_for(range(0, 2), size=QSize(595 * zoom, 842 * zoom)))

    # デバウンス中なので、途中の倍率の要求は1件も増えていない。
    assert len(service.outstanding_keys) == initial


def test_only_the_final_scale_is_requested(service: PageRenderService) -> None:
    """倍率が落ち着いたら、最後の倍率の分だけを要求する。

    途中の倍率はデバウンス中に上書きされ、一度も要求されない。
    """
    for zoom in (1, 2, 3, 4):
        service.request_pages(requests_for(range(0, 1), size=QSize(595 * zoom, 842 * zoom)))

    service.flush()

    widths = {key.width_px for key in service.outstanding_keys}
    assert widths == {595 * 4}


def test_scrolling_is_not_debounced(service: PageRenderService) -> None:
    """倍率が同じままページが変わる場合（スクロール）は待たされない。"""
    service.request_pages(requests_for(range(0, 2)))
    service.flush()

    service.request_pages(requests_for(range(1, 3)))
    service.flush()

    pages = {key.page_index for key in service.outstanding_keys}
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


def deliver(service: PageRenderService, request_id: int) -> None:
    """指定した request ID のレンダリング結果が届いた状況を再現する。"""
    meta = service._outstanding[request_id][0]  # noqa: SLF001
    service._on_page_rendered(  # noqa: SLF001
        meta.key.page_index,
        QSize(meta.key.width_px, meta.key.height_px),
        QImage(meta.key.width_px, meta.key.height_px, QImage.Format.Format_ARGB32),
        QPdfDocumentRenderOptions(),
        request_id,
    )


def test_reset_clears_the_cache_and_advances_the_generation(
    service: PageRenderService, qtbot: QtBot
) -> None:
    """リセットするとキャッシュが消えて世代が進む。"""
    service.request_pages(requests_for(range(0, 1)))
    with qtbot.waitSignal(service.page_ready, timeout=10_000):
        service.flush()
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None
    before = service.generation

    service.reset()

    assert service.generation == before + 1
    assert service.image_for(0, A4_AT_72DPI, 1.0) is None


def test_reset_keeps_the_outstanding_ledger(service: PageRenderService) -> None:
    """リセットしても未処理の要求の台帳は残す。

    Qt の待ち行列は取り消せないので、台帳を消すと、再利用された
    request ID の古い結果を新しい世代のものとして受け入れてしまう。
    """
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    assert service.outstanding_count == 1

    service.reset()

    assert service.outstanding_count == 1


def test_stale_results_are_discarded(service: PageRenderService) -> None:
    """前のドキュメントのレンダリング結果はキャッシュを汚さない。"""
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    stale_id = next(iter(service._outstanding))  # noqa: SLF001

    # 別の PDF を開いた後で、前の要求の結果が届く状況を再現する。
    service.reset()
    deliver(service, stale_id)

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None


def test_reused_request_id_does_not_leak_across_generations(
    service: PageRenderService,
) -> None:
    """再利用された request ID の古い結果を、新しい世代として受け入れない。

    Qt は同じパラメータの要求が処理中だと同じ ID を返す（実機確認済み）。
    世代をまたいで同じページを同じ倍率で要求したとき、古い結果が新しい
    ドキュメントの絵として表示されてはならない。
    """
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    old_id = next(iter(service._outstanding))  # noqa: SLF001

    # 別の PDF を開き、同じページを同じ条件で要求する。
    service.reset()
    service.request_pages(requests_for(range(0, 1)))
    service.flush()

    # 古い要求が処理中なので、同じ条件を二重に積まない。
    assert service.outstanding_count == 1

    # 古い結果が届く。捨てられ、キャッシュは汚れない。
    deliver(service, old_id)
    assert service.image_for(0, A4_AT_72DPI, 1.0) is None

    # 枠が空いたので、新しい世代の分が改めて要求される。
    assert service.outstanding_count == 1
    new_id = next(iter(service._outstanding))  # noqa: SLF001
    assert service._outstanding[new_id][0].generation == service.generation  # noqa: SLF001


def test_generation_mismatch_is_discarded(service: PageRenderService) -> None:
    """台帳に残っていても、世代が違う結果はキャッシュに入れない。"""
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    request_id = next(iter(service._outstanding))  # noqa: SLF001
    key = service._outstanding[request_id][0].key  # noqa: SLF001

    service._generation += 1  # noqa: SLF001
    deliver(service, request_id)

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None
    # まだ必要なページなので、新しい世代として要求し直されている。
    assert key in service.outstanding_keys
    for metas in service._outstanding.values():  # noqa: SLF001
        assert all(meta.generation == service.generation for meta in metas)


def test_shared_request_id_fills_every_key(service: PageRenderService) -> None:
    """1つの request ID に複数の条件が相乗りしていても、全部に配る。

    Qt が同じ画素を返す要求をまとめると、DPR だけが違う条件が同じ ID を
    共有しうる。片方だけキャッシュされて片方が永久に埋まらない、という
    状態を作らない。
    """
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    request_id = next(iter(service._outstanding))  # noqa: SLF001
    original = service._outstanding[request_id][0]  # noqa: SLF001

    # 同じ画素・別 DPR の条件が相乗りしている状態を作る。
    alias_key = replace(original.key, dpr=2.0)
    service._outstanding[request_id].append(  # noqa: SLF001
        _RequestMeta(generation=service.generation, key=alias_key)
    )
    service._outstanding_keys.add(alias_key)  # noqa: SLF001

    deliver(service, request_id)

    both = [
        service.image_for(0, A4_AT_72DPI, 1.0),
        service.image_for(0, A4_AT_72DPI, 2.0),
    ]
    assert all(image is not None for image in both)
    assert both[0].devicePixelRatio() == pytest.approx(1.0)  # type: ignore[union-attr]
    assert both[1].devicePixelRatio() == pytest.approx(2.0)  # type: ignore[union-attr]


def test_render_size_never_exceeds_the_cache_limit() -> None:
    """キャッシュに入らない大きさは要求しない。

    要求できてもキャッシュに入らなければ、いつまでも表示できない。
    """
    cache = RenderCache(max_bytes=1 * 1024 * 1024)
    service = PageRenderService(cache, max_render_bytes=64 * 1024 * 1024)

    key = service._key_for(0, QSize(4000, 5000), 1.0)  # noqa: SLF001

    assert key.width_px * key.height_px * 4 <= cache.max_bytes


# ------------------------------------------------------------------ 同時要求数の上限
def test_outstanding_requests_are_capped(service_with_cap: PageRenderService) -> None:
    """同時に処理中の要求数が上限を超えない。"""
    service_with_cap.request_pages(requests_for(range(0, 3)))
    service_with_cap.flush()

    assert service_with_cap.outstanding_count == 2


def test_fast_scrolling_does_not_accumulate_requests(
    service_with_cap: PageRenderService,
) -> None:
    """高速スクロールでも取り消せない要求が溜まり続けない。

    要求先が次々変わっても、結果が返るまで新しい要求は積まれない。
    """
    for start in range(0, 3):
        service_with_cap.request_pages(requests_for(range(start, start + 1)))
        service_with_cap.flush()

    assert service_with_cap.outstanding_count <= 2


def test_completing_a_request_frees_a_slot(service_with_cap: PageRenderService) -> None:
    """結果が返ると枠が空き、いま必要な分の続きが要求される。"""
    service_with_cap.request_pages(requests_for(range(0, 3)))
    service_with_cap.flush()
    requested_first = {
        meta[0].key.page_index
        for meta in service_with_cap._outstanding.values()  # noqa: SLF001
    }
    assert requested_first == {0, 1}

    deliver(service_with_cap, next(iter(service_with_cap._outstanding)))  # noqa: SLF001

    pages = {key.page_index for key in service_with_cap.outstanding_keys}
    assert 2 in pages, "枠が空いても続きが要求されていない"


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
