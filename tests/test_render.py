"""`anp.pdf.render` のテスト。

`QPdfPageRenderer` には取り消し API が無いため、「要求を積む前に抑える」
部分が壊れていないことを重点的に確かめる。

デバウンス用のタイマーが実際に発火するのを待つとテストがタイミング依存に
なるので、`flush()` を明示的に呼んで発行タイミングを制御する。
"""

from __future__ import annotations

import gc
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, qAlpha, qBlue, qGreen, qRed
from PySide6.QtPdf import QPdfDocumentRenderOptions
from pytestqt.qtbot import QtBot

from anp.pdf import render as render_module
from anp.pdf.cache import DisplayKey, RenderCache, RenderKey
from anp.pdf.color import PageColorMode
from anp.pdf.document import DocumentController
from anp.pdf.render import (
    DEFAULT_MAX_RENDER_BYTES,
    DEFAULT_MAX_TRANSFORM_INFLIGHT,
    PageRenderService,
    PageRequest,
    _TransformJob,
    _TransformResult,
    clamp_render_size,
)
from helpers import ManualTransforms

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
def transforms(service: PageRenderService) -> ManualTransforms:
    """色変換のワーカーを捕捉して、テストが手で完了させる。"""
    return ManualTransforms(service)


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
    key = service._outstanding[request_id].render_keys[0]  # noqa: SLF001
    service._on_page_rendered(  # noqa: SLF001
        key.page_index,
        QSize(key.width_px, key.height_px),
        QImage(key.width_px, key.height_px, QImage.Format.Format_ARGB32),
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
    assert service._outstanding[new_id].generation == service.generation  # noqa: SLF001


def test_cross_generation_alias_does_not_accept_stale_image(service: PageRenderService) -> None:
    """世代を跨いで DPR だけ違う要求を、古い要求に相乗りさせない。

    Qt の要求パラメータは (ページ, サイズ, オプション) で **DPR を含まない**。
    そのため DPR だけ違う条件は `RenderKey` としては別物なのに、Qt からは
    同じ要求として同じ ID が返る。古い世代の要求に新しい世代の条件を
    ぶら下げると、前の PDF の絵が新しい PDF のものとして表示される。
    """
    service.request_pages([PageRequest(page_index=0, size_px=A4_AT_72DPI, dpr=1.0)])
    service.flush()
    old_id = next(iter(service._outstanding))  # noqa: SLF001

    # 別の PDF を開き、同じページを同じ画素数・別 DPR で要求する。
    service.reset()
    service.request_pages([PageRequest(page_index=0, size_px=A4_AT_72DPI, dpr=2.0)])
    service.flush()

    # 前の PDF のレンダリング結果が届く。
    deliver(service, old_id)

    assert service.image_for(0, A4_AT_72DPI, 2.0) is None, (
        "前のドキュメントの画像が新しい世代の絵として受け入れられている"
    )


def test_generation_mismatch_is_discarded(service: PageRenderService) -> None:
    """台帳に残っていても、世代が違う結果はキャッシュに入れない。"""
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    request_id = next(iter(service._outstanding))  # noqa: SLF001
    key = service._outstanding[request_id].render_keys[0]  # noqa: SLF001

    service._generation += 1  # noqa: SLF001
    deliver(service, request_id)

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None
    # まだ必要なページなので、新しい世代として要求し直されている。
    assert key in service.outstanding_keys
    for request in service._outstanding.values():  # noqa: SLF001
        assert request.generation == service.generation


def test_same_generation_dpr_alias_shares_one_request(service: PageRenderService) -> None:
    """同じ世代で DPR だけ違う条件は、1つの Qt 要求に相乗りさせる。

    Qt から見れば同じ要求なので、二重に積む意味がない。相乗りした条件は
    結果が届いたときに全部埋まる。片方だけ埋まって片方が永久に待つ、という
    状態を作らない。
    """
    service.request_pages([PageRequest(page_index=0, size_px=A4_AT_72DPI, dpr=1.0)])
    service.flush()
    request_id = next(iter(service._outstanding))  # noqa: SLF001

    # 同じ画素・別 DPR を要求しても、Qt への要求は増えない。
    service.request_pages([PageRequest(page_index=0, size_px=A4_AT_72DPI, dpr=2.0)])
    service.flush()
    assert service.outstanding_count == 1
    assert len(service.outstanding_keys) == 2

    deliver(service, request_id)

    at_1x = service.image_for(0, A4_AT_72DPI, 1.0)
    at_2x = service.image_for(0, A4_AT_72DPI, 2.0)
    assert at_1x is not None
    assert at_2x is not None
    assert at_1x.devicePixelRatio() == pytest.approx(1.0)
    assert at_2x.devicePixelRatio() == pytest.approx(2.0)


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
        request.qt_key.page_index
        for request in service_with_cap._outstanding.values()  # noqa: SLF001
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
    """ドキュメントが無ければ要求を積まない。"""
    service = PageRenderService(RenderCache())

    service.request_pages(requests_for(range(0, 2)))
    service.flush()

    assert service.outstanding_count == 0
    assert not service.outstanding_keys


def test_no_requests_after_the_document_is_closed(
    service: PageRenderService, controller: DocumentController
) -> None:
    """閉じた後は要求を積まない。

    読み込めていないドキュメントに要求しても結果は返らず、台帳に
    幽霊が残るだけになる。
    """
    controller.close()
    service.reset()

    service.request_pages(requests_for(range(0, 2)))
    service.flush()

    assert service.outstanding_count == 0
    assert not service.outstanding_keys


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


# ------------------------------------------------------------------ ページの色
WHITE = 0xFFFFFFFF
BLACK = (0, 0, 0, 255)


def deliver_filled(service: PageRenderService, request_id: int, argb: int = WHITE) -> None:
    """単色で塗った画像がレンダリング結果として届いた状況を再現する。

    `deliver()` の画像は未初期化なので、画素値を見るテストには使えない。
    """
    key = service._outstanding[request_id].render_keys[0]  # noqa: SLF001
    image = QImage(key.width_px, key.height_px, QImage.Format.Format_ARGB32)
    image.fill(argb)
    service._on_page_rendered(  # noqa: SLF001
        key.page_index,
        QSize(key.width_px, key.height_px),
        image,
        QPdfDocumentRenderOptions(),
        request_id,
    )


def render_page(service: PageRenderService, size: QSize = A4_AT_72DPI, argb: int = WHITE) -> None:
    """1ページ目を要求して、単色の結果を届ける。"""
    service.request_pages([PageRequest(page_index=0, size_px=size, dpr=1.0)])
    service.flush()
    deliver_filled(service, next(iter(service._outstanding)), argb)  # noqa: SLF001


def rgba(image: QImage) -> tuple[int, int, int, int]:
    """左上の画素の (R, G, B, A)。"""
    pixel = image.pixel(0, 0)
    return qRed(pixel), qGreen(pixel), qBlue(pixel), qAlpha(pixel)


def count_transforms(monkeypatch: pytest.MonkeyPatch) -> list[PageColorMode]:
    """色変換が呼ばれるたびに記録するリストを返す。"""
    calls: list[PageColorMode] = []
    original = render_module.transform_page

    def counting(image: QImage, mode: PageColorMode) -> QImage:
        calls.append(mode)
        return original(image, mode)

    monkeypatch.setattr(render_module, "transform_page", counting)
    return calls


def test_the_default_mode_is_original(service: PageRenderService) -> None:
    """既定は Original。"""
    assert service.color_mode is PageColorMode.ORIGINAL


def test_original_returns_the_raw_image(service: PageRenderService) -> None:
    """Original では raw 画像をそのまま返す（複製を持たない）。"""
    render_page(service)

    assert service.image_for(0, A4_AT_72DPI, 1.0) is service.raw_image_for(0, A4_AT_72DPI, 1.0)
    assert len(service.display_cache) == 0


def test_invert_transforms_the_page_image(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """Invert では反転した画像を返す。"""
    render_page(service)

    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()

    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == BLACK


def test_the_raw_image_does_not_depend_on_the_color_mode(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """raw 画像は色変換の影響を受けない。

    その場で反転すると、Original へ戻したときに反転済みの絵が残る。
    ワーカーへ渡すのも暗黙共有の参照なので、変換で入力が書き換わっては
    いけない点は同じ。
    """
    render_page(service)

    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None

    raw = service.raw_image_for(0, A4_AT_72DPI, 1.0)
    assert raw is not None
    assert rgba(raw) == (255, 255, 255, 255)


def test_changing_the_mode_keeps_the_raw_cache(service: PageRenderService) -> None:
    """モードを変えても raw 画像は捨てない。"""
    render_page(service)
    before = len(service._cache)  # noqa: SLF001

    service.set_color_mode(PageColorMode.INVERT)
    service.set_color_mode(PageColorMode.ORIGINAL)

    assert len(service._cache) == before  # noqa: SLF001
    assert service.raw_image_for(0, A4_AT_72DPI, 1.0) is not None


def test_changing_the_mode_does_not_request_a_new_render(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """モードを変えても `QPdfPageRenderer` へ要求し直さない。

    往復しても、レンダリング待ちの白紙には戻らない。
    """
    render_page(service)
    generation = service.generation

    for mode in (PageColorMode.INVERT, PageColorMode.ORIGINAL, PageColorMode.INVERT):
        service.set_color_mode(mode)
        transforms.complete_all()
        service.flush()

    assert service.outstanding_count == 0
    assert service.generation == generation
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None


def test_going_back_to_original_restores_the_original_pixels(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """Invert から Original へ戻すと元の画素に戻る。"""
    render_page(service)
    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None

    service.set_color_mode(PageColorMode.ORIGINAL)

    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == (255, 255, 255, 255)


def test_the_display_image_is_not_transformed_on_lookup(
    service: PageRenderService, transforms: ManualTransforms, monkeypatch: pytest.MonkeyPatch
) -> None:
    """引き当てでは変換しない。

    `image_for()` は `paintEvent` から呼ばれる。ここで変換すると、
    描画経路に画素処理が入り込む。
    """
    render_page(service)
    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()
    calls = count_transforms(monkeypatch)

    for _ in range(5):
        assert service.image_for(0, A4_AT_72DPI, 1.0) is not None
        assert service.placeholder_for(0, A4_AT_72DPI) is not None

    assert calls == [], "取得のたびに変換している"


def test_the_transform_is_submitted_when_the_render_completes(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """レンダリングが終わった時点で変換をワーカーへ投入する。"""
    service.set_color_mode(PageColorMode.INVERT)

    render_page(service)

    assert len(transforms.submitted) == 1
    transforms.complete_all()
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == BLACK


def test_no_new_transform_when_the_pages_change_but_the_images_are_ready(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """必要なページの集合が変わっても、用意済みなら変換し直さない。

    倍率を上げて目的の解像度がまだ無い場合は、仮表示に使う別解像度の
    変換済み画像をそのまま使う。
    """
    render_page(service, size=QSize(297, 421))
    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()
    submitted = len(transforms.submitted)

    # 倍率が上がって、まだ無い解像度が必要になった。
    service.request_pages([PageRequest(page_index=0, size_px=QSize(1190, 1684), dpr=1.0)])

    assert len(transforms.submitted) == submitted
    assert service.placeholder_for(0, QSize(1190, 1684)) is not None


def test_returning_to_a_mode_reuses_the_cached_display_image(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """モードを戻したら、残っている変換済み画像をそのまま使う。

    `DisplayKey` がモードを含むので、別モードの画像を残しておいても誤って
    引き当てられることはない。残しておけば Invert ⇄ Original の往復で
    変換をやり直さずに済む。Smart Dark ではここが効いてくる。
    """
    render_page(service)
    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    submitted = len(transforms.submitted)

    service.set_color_mode(PageColorMode.ORIGINAL)
    service.set_color_mode(PageColorMode.INVERT)

    assert len(transforms.submitted) == submitted, "残っている画像があるのに変換し直している"
    assert service.image_for(0, A4_AT_72DPI, 1.0) is image


def test_setting_the_same_mode_keeps_the_display_cache(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """同じモードを選び直しても表示用画像は捨てない（連打で作り直さない）。"""
    render_page(service)
    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()
    image = service.image_for(0, A4_AT_72DPI, 1.0)

    service.set_color_mode(PageColorMode.INVERT)

    assert service.image_for(0, A4_AT_72DPI, 1.0) is image


def test_the_placeholder_is_transformed_too(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """仮表示も現在のモードで変換して返す。

    変換前の絵を先に見せると、切り替えた瞬間に元の色が一瞬見える。
    """
    render_page(service, size=QSize(297, 421))
    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()

    placeholder = service.placeholder_for(0, QSize(1190, 1684))

    assert placeholder is not None
    assert placeholder.width() == 297
    assert rgba(placeholder) == BLACK


def test_the_display_cache_is_bounded() -> None:
    """表示用キャッシュの上限を超えない。

    超える分は **作ってから追い出す** のではなく、最初から作らない。
    残るのは優先度の高い方（先に渡された方）。
    """
    image_bytes = QImage(595, 842, QImage.Format.Format_ARGB32).sizeInBytes()
    service = PageRenderService(RenderCache(), display_max_bytes=image_bytes * 2)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    for page in range(4):
        service._cache.put(  # noqa: SLF001
            RenderKey(page, 595, 842, 1.0), QImage(595, 842, QImage.Format.Format_ARGB32)
        )

    service.request_pages(requests_for(range(0, 4)))
    transforms.complete_all()

    assert service.display_cache.total_bytes <= service.display_cache.max_bytes
    assert len(service.display_cache) == 2
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None
    assert service.image_for(1, A4_AT_72DPI, 1.0) is not None
    assert service.image_for(2, A4_AT_72DPI, 1.0) is None
    assert service.image_for(3, A4_AT_72DPI, 1.0) is None


def test_reset_drops_the_display_images_too(
    service: PageRenderService, transforms: ManualTransforms
) -> None:
    """ドキュメントを入れ替えると、raw も表示用も前の PDF の画像が残らない。"""
    render_page(service)
    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None

    service.reset()

    assert len(service.display_cache) == 0
    assert service.image_for(0, A4_AT_72DPI, 1.0) is None


def test_reset_keeps_the_color_mode(service: PageRenderService) -> None:
    """ページの色はアプリ全体の設定なので、ドキュメントを替えても保つ。"""
    service.set_color_mode(PageColorMode.INVERT)

    service.reset()

    assert service.color_mode is PageColorMode.INVERT


def test_a_stale_result_does_not_reach_the_display_cache(service: PageRenderService) -> None:
    """世代の合わない結果は表示用画像にもならない。"""
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    service.flush()
    old_id = next(iter(service._outstanding))  # noqa: SLF001

    service.reset()
    deliver_filled(service, old_id)

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None
    assert len(service.display_cache) == 0


def test_the_render_size_fits_both_caches() -> None:
    """1枚の要求サイズは、raw と表示用のどちらの上限にも収まる。

    表示用画像は raw と同じ画素数になるので、片方にしか入らない大きさを
    要求すると、そのモードでは永久に表示できない。
    """
    service = PageRenderService(
        RenderCache(max_bytes=8 * 1024 * 1024),
        max_render_bytes=64 * 1024 * 1024,
        display_max_bytes=2 * 1024 * 1024,
    )

    key = service._key_for(0, QSize(4000, 5000), 1.0)  # noqa: SLF001

    assert key.width_px * key.height_px * 4 <= 2 * 1024 * 1024


# ------------------------------------------------------------------ 色変換の非同期実行
# ここから下は「変換を GUI スレッドから追い出した」ことそのものの検証。
# ワーカーの完了は `ManualTransforms` で手動で起こすので、sleep も
# タイミング頼みの待ち合わせも使わない。


def seeded_service(
    pages: range = range(0, 4),
    *,
    max_transform_inflight: int = DEFAULT_MAX_TRANSFORM_INFLIGHT,
    argb: int = WHITE,
) -> PageRenderService:
    """raw 画像を直接仕込んだ、ドキュメント未設定のサービス。

    ドキュメントを持たないので `flush()` は何も要求しない。本物の
    レンダリングが途中で完了して仕込みを上書きすることがなくなる。
    """
    service = PageRenderService(RenderCache(), max_transform_inflight=max_transform_inflight)
    for page in pages:
        image = QImage(595, 842, QImage.Format.Format_ARGB32)
        image.fill(argb)
        service._cache.put(RenderKey(page, 595, 842, 1.0), image)  # noqa: SLF001
    return service


def display_keys(service: PageRenderService) -> set[DisplayKey]:
    """表示用キャッシュに入っている鍵。"""
    return set(service._display_cache._entries)  # noqa: SLF001


# ------------------------------------------------------------------ 非同期の基本
def test_the_transform_does_not_run_while_requesting(monkeypatch: pytest.MonkeyPatch) -> None:
    """要求の呼び出しの中で変換が同期実行されない。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    calls = count_transforms(monkeypatch)

    service.request_pages(requests_for(range(0, 1)))

    assert calls == [], "GUI スレッドで変換している"
    assert len(transforms.submitted) == 1


def test_the_display_image_is_missing_until_the_worker_finishes() -> None:
    """ワーカーが終わるまで表示用画像は引き当てられない。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)

    service.request_pages(requests_for(range(0, 1)))

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None
    assert len(service.display_cache) == 0
    assert transforms.pending


def test_the_display_image_lands_in_the_cache_after_the_worker_finishes() -> None:
    """ワーカーが終わったら表示用キャッシュに入る。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))

    transforms.complete_all()

    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == BLACK


def test_page_ready_is_emitted_when_the_worker_finishes() -> None:
    """変換が終わったら `page_ready` で知らせる。まだの間は黙っている。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    service.request_pages(requests_for(range(0, 1)))
    assert ready == [], "変換が終わる前に通知している"

    transforms.complete_all()

    assert ready == [0]


def test_lookups_do_not_wait_for_the_worker() -> None:
    """引き当ては即座に返る。ワーカーの完了を待たない。

    待つ実装なら、`pending` が残ったままここへ来た時点で戻ってこない。
    """
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None
    assert service.placeholder_for(0, A4_AT_72DPI) is None
    assert transforms.pending, "テストの前提が崩れている（変換が既に終わっている）"


def test_the_real_worker_produces_the_display_image(qtbot: QtBot) -> None:
    """本物のワーカーでも、結果が GUI スレッドへ戻ってキャッシュに入る。

    手動の完了に差し替えたテストだけでは、スレッドをまたぐ経路
    （`QThreadPool` と queued connection）が動くことを確かめられない。
    """
    service = seeded_service(range(0, 1))
    service.set_color_mode(PageColorMode.INVERT)

    with qtbot.waitSignal(service.page_ready, timeout=10_000) as blocker:
        service.request_pages(requests_for(range(0, 1)))

    assert blocker.args == [0]
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == BLACK
    assert service.transform_inflight_count == 0


# ------------------------------------------------------------------ 重複の抑止
def test_the_same_display_key_is_submitted_once() -> None:
    """同じ `DisplayKey` を何度要求してもワーカーは1つだけ。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)

    for _ in range(5):
        service.request_pages(requests_for(range(0, 1)))

    assert len(transforms.submitted) == 1
    assert service.transform_inflight_count == 1


def test_the_same_render_key_in_another_mode_is_another_job() -> None:
    """同じ `RenderKey` でもモードが違えば別の変換。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    transforms.complete_all()

    # ORIGINAL は変換が要らないので、増えるのは Invert の1件だけ。
    service.set_color_mode(PageColorMode.ORIGINAL)
    service.request_pages(requests_for(range(0, 1)))

    keys = {job.display_key for job in transforms.submitted}
    assert keys == {
        DisplayKey(render_key=RenderKey(0, 595, 842, 1.0), color_mode=PageColorMode.INVERT)
    }


def test_a_completed_transform_is_not_submitted_again() -> None:
    """出来上がった分を、要求のたびに作り直さない。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    transforms.complete_all()

    for _ in range(5):
        service.request_pages(requests_for(range(0, 1)))

    assert len(transforms.submitted) == 1


# ------------------------------------------------------------------ 投入量の上限
def test_transforms_in_flight_are_capped() -> None:
    """大量に要求されても、ワーカーで走るのは上限まで。"""
    service = seeded_service(range(0, 4), max_transform_inflight=2)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)

    service.request_pages(requests_for(range(0, 4)))

    assert service.transform_inflight_count == 2
    assert len(transforms.submitted) == 2


def test_completing_one_transform_starts_one_more() -> None:
    """1件終わったら、次の1件だけを投入する。"""
    service = seeded_service(range(0, 4), max_transform_inflight=2)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 4)))

    transforms.complete(transforms.pending[0])

    assert service.transform_inflight_count == 2
    assert len(transforms.submitted) == 3


def test_the_transforms_follow_the_requested_priority() -> None:
    """要求された順（現在ページ → 他の可視ページ → 先読み）に投入する。"""
    service = seeded_service(range(0, 4), max_transform_inflight=1)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)

    service.request_pages(requests_for(range(0, 4)))
    pages = [transforms.submitted[0].display_key.render_key.page_index]
    for _ in range(3):
        transforms.complete(transforms.pending[0])
        pages.append(transforms.submitted[-1].display_key.render_key.page_index)

    assert pages == [0, 1, 2, 3]


def test_fast_scrolling_does_not_queue_every_page() -> None:
    """必要なページが次々変わっても、待ち行列は作らない。

    投入前の古い要求は捨ててよい。ワーカーで走っている分だけが残る。
    """
    service = seeded_service(range(0, 4), max_transform_inflight=2)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)

    for start in range(0, 4):
        service.request_pages(requests_for(range(start, start + 1)))
        assert service.transform_inflight_count <= 2

    # 走っているのは最初に投入した2件だけ。残りは投入されずに消えた。
    assert len(transforms.submitted) == 2
    assert len(transforms.pending) == 2


def budget_service(images: int) -> tuple[PageRenderService, ManualTransforms]:
    """表示用キャッシュが `images` 枚分しかない、Invert のサービス。

    raw は 4 ページ分そろえておく。優先度と予算の関係だけを見たいので、
    レンダリングの待ちは作らない。
    """
    image_bytes = QImage(595, 842, QImage.Format.Format_ARGB32).sizeInBytes()
    service = PageRenderService(RenderCache(), display_max_bytes=image_bytes * images)
    transforms = ManualTransforms(service)
    for page in range(4):
        image = QImage(595, 842, QImage.Format.Format_ARGB32)
        image.fill(WHITE)
        service._cache.put(RenderKey(page, 595, 842, 1.0), image)  # noqa: SLF001
    service.set_color_mode(PageColorMode.INVERT)
    return service, transforms


def test_the_display_budget_does_not_cause_endless_retransforms() -> None:
    """予算に収まらない集合を要求されても、変換を延々と繰り返さない。

    「完了 → 追い出し → 再投入」が回り続けると、CPU を食い続けたうえに
    どのページも安定して表示できない。
    """
    service, transforms = budget_service(images=2)

    service.request_pages(requests_for(range(0, 4)))
    transforms.complete_all()

    assert len(transforms.submitted) == 2, "予算に入らない分まで変換している"
    assert service.transform_inflight_count == 0


def test_the_current_page_survives_display_budget_pressure() -> None:
    """予算が足りなくても、現在ページが先読みに追い出されない。

    LRU 任せにすると、先に作った現在ページが後から作った先読みに押し出され、
    **現在ページだけが下地のまま**という逆転が起きる。Smart Dark では
    「変換が遅い」のか「scheduler が現在ページを捨てた」のか区別が
    つかなくなるので、ここで固定しておく。
    """
    service, transforms = budget_service(images=2)

    # 優先度は現在ページ 0 → 可視 1 → 先読み 2, 3。
    service.request_pages(requests_for(range(0, 4)))
    transforms.complete_all()

    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None, "現在ページが追い出された"


def test_cache_residency_follows_the_priority_order() -> None:
    """キャッシュに残るのは優先度の高い方から順に、予算に入る分だけ。"""
    service, transforms = budget_service(images=3)

    service.request_pages(requests_for(range(0, 4)))
    transforms.complete_all()

    resident = {key.render_key.page_index for key in display_keys(service)}
    assert resident == {0, 1, 2}, "優先度順に残っていない"


def test_the_lowest_priority_image_is_evicted_first() -> None:
    """追い出しが起きるときに消えるのは、いちばん優先度の低いページ。

    予算に入れた分の LRU の並びを優先度と揃えていないと、先に作った
    現在ページから消えていく。
    """
    service, transforms = budget_service(images=3)
    service.request_pages(requests_for(range(0, 3)))
    transforms.complete_all()

    # 別解像度が1枚割り込んでくる（倍率が少しだけ動いた）。
    intruder = QImage(600, 850, QImage.Format.Format_ARGB32)
    intruder.fill(WHITE)
    service._cache.put(RenderKey(0, 600, 850, 1.0), intruder)  # noqa: SLF001
    service.request_pages([PageRequest(page_index=0, size_px=QSize(600, 850), dpr=1.0)])
    transforms.complete_all()

    resident = {
        (key.render_key.page_index, key.render_key.width_px) for key in display_keys(service)
    }
    assert (2, 595) not in resident, "いちばん低い優先度が残っている"
    assert (0, 600) in resident


def test_a_priority_only_reorder_is_treated_as_a_change() -> None:
    """`RenderKey` の集合が同じでも、並びが変われば別の要求として扱う。

    この dict の挿入順は「現在ページ → 可視 → 先読み」という優先度そのもの。
    dict の等値比較は順序を見ないので、順序だけの入れ替わりを取りこぼすと、
    新しく現在ページになったページが古い並びの記憶で抑止されてしまう。
    """
    service, transforms = budget_service(images=4)
    service.request_pages(requests_for(range(0, 2)))
    # ページ 0 の変換が失敗した。作られていないが「試した」記録は残る。
    transforms.fail(transforms.pending[0])
    submitted = len(transforms.submitted)

    # 同じ並びで宣言し直しても作り直さない（安全弁が効いている）。
    service.request_pages(requests_for(range(0, 2)))
    assert len(transforms.submitted) == submitted

    # 並びが変わればもう一度機会を与える。
    service.request_pages(
        [
            PageRequest(page_index=1, size_px=A4_AT_72DPI, dpr=1.0),
            PageRequest(page_index=0, size_px=A4_AT_72DPI, dpr=1.0),
        ]
    )

    assert len(transforms.submitted) > submitted, "並びが変わったのに抑止されたまま"


# ------------------------------------------------------------------ 遅れて届いた結果
def test_a_late_low_priority_result_does_not_evict_the_current_page() -> None:
    """遅れて届いた低優先度の結果で、いま使っている画像を追い出さない。

    倍率が動いた直後は、古い倍率の変換がまだ走っている。それが後から
    届いたときに現在の絵を押し出すと、表示が一段古い倍率へ巻き戻る。
    """
    # どちらか1枚だけが入る予算。2枚は入らない。
    image_bytes = QImage(600, 850, QImage.Format.Format_ARGB32).sizeInBytes()
    service = PageRenderService(RenderCache(), display_max_bytes=image_bytes)
    transforms = ManualTransforms(service)
    for width, height in ((595, 842), (600, 850)):
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(WHITE)
        service._cache.put(RenderKey(0, width, height, 1.0), image)  # noqa: SLF001
    service.set_color_mode(PageColorMode.INVERT)

    service.request_pages([PageRequest(page_index=0, size_px=QSize(595, 842), dpr=1.0)])
    old = transforms.pending[0]
    # 倍率が動いて、いま必要なのは別解像度になった。
    service.request_pages([PageRequest(page_index=0, size_px=QSize(600, 850), dpr=1.0)])
    transforms.complete(transforms.pending[-1])
    assert service.image_for(0, QSize(600, 850), 1.0) is not None

    transforms.complete(old)

    assert service.image_for(0, QSize(600, 850), 1.0) is not None, "古い倍率の結果に追い出された"
    assert service.image_for(0, QSize(595, 842), 1.0) is None


def test_an_old_mode_result_is_dropped_when_there_is_no_room() -> None:
    """旧モードの結果も、予算に余りが無ければキャッシュへ入れない。

    残しておけるのは「余っているから残す」場合だけ。いま表示に使っている
    画像を押しのけてまで、使っていないモードの絵を持たない。
    """
    # どちらか1枚だけが入る予算。2枚は入らない。
    image_bytes = QImage(600, 850, QImage.Format.Format_ARGB32).sizeInBytes()
    service = PageRenderService(RenderCache(), display_max_bytes=image_bytes)
    transforms = ManualTransforms(service)
    for width, height in ((595, 842), (600, 850)):
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(WHITE)
        service._cache.put(RenderKey(0, width, height, 1.0), image)  # noqa: SLF001

    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages([PageRequest(page_index=0, size_px=QSize(595, 842), dpr=1.0)])
    transforms.complete_all()
    assert len(service.display_cache) == 1

    # 別解像度の Invert が走っている間に Original へ戻る。
    service.request_pages([PageRequest(page_index=0, size_px=QSize(600, 850), dpr=1.0)])
    pending = transforms.pending[0]
    service.set_color_mode(PageColorMode.ORIGINAL)
    transforms.complete(pending)

    assert len(service.display_cache) == 1, "予算が無いのに旧モードの絵を足している"
    assert service.display_cache.total_bytes <= service.display_cache.max_bytes


def test_an_old_mode_result_is_kept_when_there_is_room() -> None:
    """余りがあるなら旧モードの結果は残す（戻したときに作り直さない）。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    pending = transforms.pending[0]

    service.set_color_mode(PageColorMode.ORIGINAL)
    transforms.complete(pending)

    assert len(service.display_cache) == 1


# ------------------------------------------------------------------ 世代
def test_a_stale_transform_does_not_reach_the_cache() -> None:
    """前のドキュメントの変換結果は、新しいドキュメントのキャッシュに入らない。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    stale = transforms.pending[0]
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    # 別の PDF を開いた後で、前の変換が終わる。
    service.reset()
    transforms.complete(stale)

    assert len(service.display_cache) == 0
    assert ready == []
    assert service.transform_inflight_count == 0


def test_a_stale_transform_frees_its_slot() -> None:
    """世代の合わない結果でも枠は解放される（台帳が詰まらない）。"""
    service = seeded_service(range(0, 1), max_transform_inflight=1)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    stale = transforms.pending[0]

    service.reset()
    service._cache.put(  # noqa: SLF001
        RenderKey(0, 595, 842, 1.0), QImage(595, 842, QImage.Format.Format_ARGB32)
    )
    service.request_pages(requests_for(range(0, 1)))
    # 同じ鍵の古い変換が走っているので、二重には投入しない。
    assert len(transforms.submitted) == 1

    transforms.complete(stale)

    # 枠が空いたので、新しい世代の分が改めて投入されている。
    assert len(transforms.submitted) == 2
    assert transforms.submitted[-1].generation == service.generation


def test_switching_documents_back_and_forth_keeps_generations_apart() -> None:
    """A → B → A と切り替えても、A の古い結果を新しい A として受け入れない。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    from_first_a = transforms.pending[0]

    service.reset()  # B へ
    service.reset()  # A へ戻る（同じパスでも世代は別）
    service._cache.put(  # noqa: SLF001
        RenderKey(0, 595, 842, 1.0), QImage(595, 842, QImage.Format.Format_ARGB32)
    )
    transforms.complete(from_first_a)

    assert len(service.display_cache) == 0


# ------------------------------------------------------------------ モードの競合
def test_an_old_mode_result_is_not_shown_as_the_current_mode() -> None:
    """モードを戻した後に届いた旧モードの結果で、いまの表示を上書きしない。"""
    service = seeded_service(range(0, 1), argb=WHITE)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    pending = transforms.pending[0]
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    service.set_color_mode(PageColorMode.ORIGINAL)
    transforms.complete(pending)

    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == (255, 255, 255, 255), "Original の表示が Invert の結果で上書きされた"
    assert ready == [], "旧モードの結果で再描画を促している"


def test_an_old_mode_result_is_still_cached() -> None:
    """旧モードの結果もキャッシュには残す。戻したときに作り直さずに済む。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    pending = transforms.pending[0]
    service.set_color_mode(PageColorMode.ORIGINAL)

    transforms.complete(pending)

    invert_key = DisplayKey(render_key=RenderKey(0, 595, 842, 1.0), color_mode=PageColorMode.INVERT)
    assert display_keys(service) == {invert_key}
    # 戻すと、作り直さずにそのまま使える。
    service.set_color_mode(PageColorMode.INVERT)
    assert len(transforms.submitted) == 1
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == BLACK


def test_rapid_mode_switching_does_not_duplicate_jobs() -> None:
    """モードを連打しても変換が増殖しない。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.request_pages(requests_for(range(0, 1)))
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    for mode in (PageColorMode.INVERT, PageColorMode.ORIGINAL) * 4:
        service.set_color_mode(mode)

    assert len(transforms.submitted) == 1, "連打の回数だけワーカーを起こしている"

    # 最後は ORIGINAL。Invert の結果が届いても表示にも通知にも使わない。
    transforms.complete_all()
    assert ready == []
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == (255, 255, 255, 255)


def test_rapid_mode_switching_ending_on_invert_shows_the_inverted_image() -> None:
    """連打の末に Invert で止まれば、届いた結果はそのまま表示に使える。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.request_pages(requests_for(range(0, 1)))
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    for mode in (PageColorMode.INVERT, PageColorMode.ORIGINAL) * 3:
        service.set_color_mode(mode)
    service.set_color_mode(PageColorMode.INVERT)

    transforms.complete_all()

    assert ready == [0]
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == BLACK


# --------------------------------------------------- Smart Dark（非同期の契約）
# ここから下は、変換が要るモードが **2つ** になったことで初めて起きる状況の検証。
# 赤で塗った raw を使うと、どちらのモードの結果が表示されているかを画素で
# 見分けられる（Invert なら水色、Smart Dark なら赤のまま）。
RED = 0xFFFF0000
INVERTED_RED = (0, 255, 255, 255)
SMART_DARK_RED = (255, 0, 0, 255)


def test_smart_dark_does_not_run_while_requesting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Smart Dark も要求の呼び出しの中で同期実行されない。"""
    service = seeded_service(range(0, 1), argb=RED)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.SMART_DARK)
    calls = count_transforms(monkeypatch)

    service.request_pages(requests_for(range(0, 1)))

    assert calls == [], "GUI スレッドで Smart Dark をかけている"
    assert len(transforms.submitted) == 1


def test_the_smart_dark_image_is_missing_until_the_worker_finishes() -> None:
    """ワーカーが終わるまで Smart Dark の画像は引き当てられない。"""
    service = seeded_service(range(0, 1), argb=RED)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.SMART_DARK)

    service.request_pages(requests_for(range(0, 1)))

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None
    assert service.placeholder_for(0, A4_AT_72DPI) is None
    assert len(service.display_cache) == 0
    assert transforms.pending


def test_the_smart_dark_image_lands_in_the_cache_and_notifies() -> None:
    """ワーカーが終わったらキャッシュに入り、`page_ready` で知らせる。"""
    service = seeded_service(range(0, 1), argb=RED)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.SMART_DARK)
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    service.request_pages(requests_for(range(0, 1)))
    assert ready == [], "変換が終わる前に通知している"

    transforms.complete_all()

    assert ready == [0]
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == SMART_DARK_RED, "色相が保たれていない"


def test_the_real_worker_produces_the_smart_dark_image(qtbot: QtBot) -> None:
    """本物のワーカーでも Smart Dark の結果が GUI スレッドへ戻ってくる。"""
    service = seeded_service(range(0, 1), argb=RED)
    service.set_color_mode(PageColorMode.SMART_DARK)

    with qtbot.waitSignal(service.page_ready, timeout=10_000) as blocker:
        service.request_pages(requests_for(range(0, 1)))

    assert blocker.args == [0]
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == SMART_DARK_RED
    assert service.transform_inflight_count == 0


# ------------------------------------------ 変換が要るモードどうしの競合
def test_a_late_invert_result_does_not_pollute_the_smart_dark_display() -> None:
    """Invert の変換中に Smart Dark へ切り替えても、Invert の結果が表示に出ない。

    P2-3A までは「変換が要るモード」対「Original」の競合しかなかった。
    ここが2つの変換モードが同時に存在する状況の要になる。
    """
    service = seeded_service(range(0, 1), argb=RED)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    invert_job = transforms.pending[0]
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    service.set_color_mode(PageColorMode.SMART_DARK)
    transforms.complete(invert_job)

    assert ready == [], "旧モードの結果で再描画を促している"
    assert service.image_for(0, A4_AT_72DPI, 1.0) is None, "Invert の絵が Smart Dark として出た"
    assert service.placeholder_for(0, A4_AT_72DPI) is None, "Invert の絵を仮表示に使っている"


def test_a_late_smart_dark_result_does_not_pollute_the_invert_display() -> None:
    """逆向き。Smart Dark の変換中に Invert へ切り替えた場合も同じ。"""
    service = seeded_service(range(0, 1), argb=RED)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.SMART_DARK)
    service.request_pages(requests_for(range(0, 1)))
    smart_dark_job = transforms.pending[0]
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete(smart_dark_job)

    assert ready == []
    assert service.image_for(0, A4_AT_72DPI, 1.0) is None, "Smart Dark の絵が Invert として出た"


def test_the_smart_dark_job_follows_the_finished_invert_job() -> None:
    """枠が1つしか無くても、旧モードが終わった後に現在モードの変換が進む。

    Invert 実行中 → Smart Dark を選択 → Invert 完了 → Smart Dark 投入 →
    Smart Dark 完了 → Smart Dark 表示、という順序を決定的に固定する。
    """
    service = seeded_service(range(0, 1), max_transform_inflight=1, argb=RED)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    invert_job = transforms.pending[0]
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    service.set_color_mode(PageColorMode.SMART_DARK)
    assert len(transforms.submitted) == 1, "枠が埋まっているのに投入している"

    transforms.complete(invert_job)

    assert len(transforms.submitted) == 2, "枠が空いたのに現在モードの変換が続かない"
    smart_dark_job = transforms.submitted[-1]
    assert smart_dark_job.display_key.color_mode is PageColorMode.SMART_DARK
    assert ready == [], "Invert の完了で通知している"

    transforms.complete(smart_dark_job)

    assert ready == [0]
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == SMART_DARK_RED


def test_switching_between_the_two_transformed_modes_reuses_the_cache() -> None:
    """Smart Dark → Invert → Smart Dark で、残っていれば変換をやり直さない。

    モードが増えたことを理由に、切り替えのたびに表示用キャッシュを
    空にする実装へ戻っていないことを見る。
    """
    service = seeded_service(range(0, 1), argb=RED)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.SMART_DARK)
    service.request_pages(requests_for(range(0, 1)))
    transforms.complete_all()

    service.set_color_mode(PageColorMode.INVERT)
    transforms.complete_all()
    assert len(transforms.submitted) == 2

    service.set_color_mode(PageColorMode.SMART_DARK)

    assert len(transforms.submitted) == 2, "キャッシュにあるのに作り直している"
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == SMART_DARK_RED
    # 両モードの絵が同居している。
    assert {key.color_mode for key in display_keys(service)} == {
        PageColorMode.INVERT,
        PageColorMode.SMART_DARK,
    }


def test_rapid_switching_between_three_modes_does_not_duplicate_jobs() -> None:
    """3モードを行き来しても、変換が増殖せず枠の上限も超えない。"""
    service = seeded_service(range(0, 1), argb=RED)
    transforms = ManualTransforms(service)
    service.request_pages(requests_for(range(0, 1)))
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    for mode in (
        PageColorMode.ORIGINAL,
        PageColorMode.INVERT,
        PageColorMode.SMART_DARK,
        PageColorMode.INVERT,
        PageColorMode.SMART_DARK,
    ):
        service.set_color_mode(mode)
        assert service.transform_inflight_count <= DEFAULT_MAX_TRANSFORM_INFLIGHT

    # 走るのは Invert と Smart Dark の1件ずつ。連打の回数だけは起こさない。
    assert sorted(transforms.submitted_keys) == ["p0w595-invert", "p0w595-smart_dark"]

    transforms.complete_all()

    # 最後は Smart Dark。Invert の完了では通知しない。
    assert ready == [0]
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == SMART_DARK_RED
    assert service.display_cache.total_bytes <= service.display_cache.max_bytes


def test_a_late_invert_result_does_not_evict_the_current_smart_dark_page() -> None:
    """遅れて届いた Invert の結果が、いま表示している Smart Dark を追い出さない。

    P2-3A の late-result admission が、変換モード2つの間でも効いていること。
    """
    # 1枚だけが入る予算。2枚は入らない。
    image_bytes = QImage(595, 842, QImage.Format.Format_ARGB32).sizeInBytes()
    service = PageRenderService(RenderCache(), display_max_bytes=image_bytes)
    transforms = ManualTransforms(service)
    raw = QImage(595, 842, QImage.Format.Format_ARGB32)
    raw.fill(RED)
    service._cache.put(RenderKey(0, 595, 842, 1.0), raw)  # noqa: SLF001

    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    invert_job = transforms.pending[0]

    service.set_color_mode(PageColorMode.SMART_DARK)
    transforms.complete(transforms.pending[-1])
    assert rgba(service.image_for(0, A4_AT_72DPI, 1.0) or QImage()) == SMART_DARK_RED

    transforms.complete(invert_job)

    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None, "Smart Dark が旧モードの結果に追い出された"
    assert rgba(image) == SMART_DARK_RED
    assert display_keys(service) == {
        DisplayKey(render_key=RenderKey(0, 595, 842, 1.0), color_mode=PageColorMode.SMART_DARK)
    }
    assert service.display_cache.total_bytes <= service.display_cache.max_bytes


def test_the_placeholder_does_not_fall_back_to_another_mode() -> None:
    """Smart Dark 待ちのときに、Invert の絵を仮表示に使わない。

    現在のモード以外の絵を出すと、切り替えた瞬間に前のモードの色が見える。
    使えるものが無ければ何も返さず、ビューには黒い下地を描かせる。
    """
    service = seeded_service(range(0, 0), argb=RED)
    transforms = ManualTransforms(service)
    small = QImage(297, 421, QImage.Format.Format_ARGB32)
    small.fill(RED)
    service._cache.put(RenderKey(0, 297, 421, 1.0), small)  # noqa: SLF001
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages([PageRequest(page_index=0, size_px=QSize(297, 421), dpr=1.0)])
    transforms.complete_all()
    assert rgba(service.placeholder_for(0, A4_AT_72DPI) or QImage()) == INVERTED_RED

    service.set_color_mode(PageColorMode.SMART_DARK)

    assert service.placeholder_for(0, A4_AT_72DPI) is None, "Invert の絵を仮表示に使っている"


def test_the_nearest_smart_dark_image_is_used_as_a_placeholder() -> None:
    """Smart Dark でも、同じモードの別解像度があれば仮表示に使う。"""
    service = seeded_service(range(0, 0), argb=RED)
    transforms = ManualTransforms(service)
    small = QImage(297, 421, QImage.Format.Format_ARGB32)
    small.fill(RED)
    service._cache.put(RenderKey(0, 297, 421, 1.0), small)  # noqa: SLF001
    service.set_color_mode(PageColorMode.SMART_DARK)
    service.request_pages([PageRequest(page_index=0, size_px=QSize(297, 421), dpr=1.0)])
    transforms.complete_all()

    placeholder = service.placeholder_for(0, QSize(1190, 1684))

    assert placeholder is not None
    assert placeholder.width() == 297
    assert rgba(placeholder) == SMART_DARK_RED


# ------------------------------------------------------------------ 仮表示
def test_no_placeholder_while_the_transform_is_pending() -> None:
    """現在のモードの画像が1枚も無ければ、仮表示も返さない。

    ここで raw を返すとビューが明るい元の絵を一瞬描いてしまう。返さなければ
    ビューは `PageColorMode` 由来のページ下地（Invert なら黒）を描く。
    """
    service = seeded_service(range(0, 1))
    ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)

    service.request_pages(requests_for(range(0, 1)))

    assert service.image_for(0, A4_AT_72DPI, 1.0) is None
    assert service.placeholder_for(0, QSize(1190, 1684)) is None


def test_the_nearest_current_mode_image_is_used_as_a_placeholder() -> None:
    """現在のモードの別解像度があれば、それを仮表示に使う。"""
    service = seeded_service(range(0, 0))
    transforms = ManualTransforms(service)
    small = QImage(297, 421, QImage.Format.Format_ARGB32)
    small.fill(WHITE)
    service._cache.put(RenderKey(0, 297, 421, 1.0), small)  # noqa: SLF001
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages([PageRequest(page_index=0, size_px=QSize(297, 421), dpr=1.0)])
    transforms.complete_all()

    # 倍率が上がり、目的の解像度はまだ無い。
    placeholder = service.placeholder_for(0, QSize(1190, 1684))

    assert placeholder is not None
    assert placeholder.width() == 297
    assert rgba(placeholder) == BLACK


def test_the_placeholder_ignores_resolutions_that_are_not_transformed_yet() -> None:
    """変換がまだの解像度に引っ張られて仮表示を諦めない。

    raw の中からいちばん近い解像度を選ぶと、そこが未変換だった時点で
    「仮表示なし」になってしまう。変換済みの中から選ぶ。
    """
    service = seeded_service(range(0, 0))
    transforms = ManualTransforms(service)
    for width, height in ((297, 421), (1190, 1684)):
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.fill(WHITE)
        service._cache.put(RenderKey(0, width, height, 1.0), image)  # noqa: SLF001
    service.set_color_mode(PageColorMode.INVERT)

    # 小さい方だけ変換が終わっている状態を作る。
    service.request_pages([PageRequest(page_index=0, size_px=QSize(297, 421), dpr=1.0)])
    transforms.complete_all()

    placeholder = service.placeholder_for(0, QSize(1190, 1684))

    assert placeholder is not None
    assert placeholder.width() == 297
    assert rgba(placeholder) == BLACK


def test_original_uses_the_raw_image_as_a_placeholder() -> None:
    """Original は変換が要らないので、従来どおり raw を仮表示に使う。"""
    service = seeded_service(range(0, 0))
    small = QImage(297, 421, QImage.Format.Format_ARGB32)
    small.fill(WHITE)
    service._cache.put(RenderKey(0, 297, 421, 1.0), small)  # noqa: SLF001

    placeholder = service.placeholder_for(0, QSize(1190, 1684))

    assert placeholder is not None
    assert placeholder.width() == 297


# ------------------------------------------------------------------ 失敗
def test_a_failed_transform_is_not_cached() -> None:
    """変換に失敗した画像はキャッシュにも通知にも出さない。"""
    service = seeded_service(range(0, 1))
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 1)))
    ready: list[int] = []
    service.page_ready.connect(ready.append)

    transforms.fail(transforms.pending[0])

    assert len(service.display_cache) == 0
    assert ready == []


def test_a_failed_transform_frees_the_slot() -> None:
    """変換に失敗しても台帳が詰まらず、次の分が進む。"""
    service = seeded_service(range(0, 2), max_transform_inflight=1)
    transforms = ManualTransforms(service)
    service.set_color_mode(PageColorMode.INVERT)
    service.request_pages(requests_for(range(0, 2)))
    assert len(transforms.submitted) == 1

    transforms.fail(transforms.pending[0])

    assert service.transform_inflight_count == 1
    assert len(transforms.submitted) == 2
    assert transforms.submitted[-1].display_key.render_key.page_index == 1


def test_an_exception_in_the_worker_does_not_escape(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ワーカーの中で例外が出ても、アプリを巻き込まず枠が解放される。"""

    def exploding(image: QImage, mode: PageColorMode) -> QImage:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(render_module, "transform_page", exploding)
    service = seeded_service(range(0, 1))
    service.set_color_mode(PageColorMode.INVERT)

    service.request_pages(requests_for(range(0, 1)))
    qtbot.waitUntil(lambda: service.transform_inflight_count == 0, timeout=10_000)

    assert len(service.display_cache) == 0


# ------------------------------------------------------------------ 終了時
def test_the_worker_can_finish_after_the_service_is_gone() -> None:
    """サービスが先に消えても、ワーカーの完了通知で落ちない。

    中継役をサービスの子にしていると、閉じている最中にワーカーが
    破棄済みのオブジェクトへ emit してプロセスごと落ちる。親を持たせず、
    受け手側の接続は Qt に切らせる。
    """
    service = seeded_service(range(0, 1))
    signals = service._transform_signals  # noqa: SLF001
    assert signals.parent() is None
    job = _TransformJob(
        generation=service.generation,
        display_key=DisplayKey(
            render_key=RenderKey(0, 595, 842, 1.0), color_mode=PageColorMode.INVERT
        ),
        source=QImage(595, 842, QImage.Format.Format_ARGB32),
    )

    reference = weakref.ref(service)
    del service
    gc.collect()
    assert reference() is None, "サービスが生き残っていて、テストの前提が崩れている"

    # 行き先を失った通知。黙って捨てられる。
    signals.finished.emit(_TransformResult(job=job, image=None))


# ------------------------------------------------------------------ 設定値
def test_a_transform_cap_below_one_is_rejected() -> None:
    """同時変換数を 0 以下にすると、何も投入されないまま止まる。設定させない。"""
    with pytest.raises(ValueError, match="max_transform_inflight"):
        PageRenderService(RenderCache(), max_transform_inflight=0)


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: PageRenderService(RenderCache(), max_inflight=0), "max_inflight"),
        (lambda: PageRenderService(RenderCache(), max_render_bytes=0), "max_render_bytes"),
        (lambda: PageRenderService(RenderCache(), display_max_bytes=0), "display_max_bytes"),
        (lambda: PageRenderService(RenderCache(), debounce_ms=-1), "debounce_ms"),
    ],
)
def test_a_broken_setting_is_rejected_at_construction(
    build: Callable[[], PageRenderService], message: str
) -> None:
    """壊れた設定値でサービスを作れてしまわない。

    どれも症状は「1ページも描かれない」という遠くの静かな停止になる。
    例えば `max_inflight=0` では、`flush()` の枠の判定が最初から成立して
    要求が1件も発行されない。
    """
    with pytest.raises(ValueError, match=message):
        build()


def test_a_cache_that_cannot_hold_a_pixel_is_rejected() -> None:
    """1画素も入らない上限のキャッシュは作らせない。"""
    with pytest.raises(ValueError, match="max_bytes"):
        RenderCache(max_bytes=0)


def test_clamping_to_less_than_a_pixel_is_rejected() -> None:
    """縮めようのない上限を渡されたら、`sqrt()` の定義域へ行く前に弾く。"""
    with pytest.raises(ValueError, match="max_bytes"):
        clamp_render_size(QSize(100, 100), max_bytes=0)
