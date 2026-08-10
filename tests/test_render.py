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
from PySide6.QtGui import QImage, qAlpha, qBlue, qGreen, qRed
from PySide6.QtPdf import QPdfDocumentRenderOptions
from pytestqt.qtbot import QtBot

from anp.pdf import render as render_module
from anp.pdf.cache import RenderCache, RenderKey
from anp.pdf.color import PageColorMode
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


def test_invert_transforms_the_page_image(service: PageRenderService) -> None:
    """Invert では反転した画像を返す。"""
    render_page(service)

    service.set_color_mode(PageColorMode.INVERT)

    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == BLACK


def test_the_raw_image_does_not_depend_on_the_color_mode(service: PageRenderService) -> None:
    """raw 画像は色変換の影響を受けない。

    その場で反転すると、Original へ戻したときに反転済みの絵が残る。
    """
    render_page(service)

    service.set_color_mode(PageColorMode.INVERT)
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


def test_changing_the_mode_does_not_request_a_new_render(service: PageRenderService) -> None:
    """モードを変えても `QPdfPageRenderer` へ要求し直さない。

    往復しても、レンダリング待ちの白紙には戻らない。
    """
    render_page(service)
    generation = service.generation

    for mode in (PageColorMode.INVERT, PageColorMode.ORIGINAL, PageColorMode.INVERT):
        service.set_color_mode(mode)
        service.flush()

    assert service.outstanding_count == 0
    assert service.generation == generation
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None


def test_going_back_to_original_restores_the_original_pixels(service: PageRenderService) -> None:
    """Invert から Original へ戻すと元の画素に戻る。"""
    render_page(service)
    service.set_color_mode(PageColorMode.INVERT)
    assert service.image_for(0, A4_AT_72DPI, 1.0) is not None

    service.set_color_mode(PageColorMode.ORIGINAL)

    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == (255, 255, 255, 255)


def test_the_display_image_is_prepared_before_it_is_asked_for(
    service: PageRenderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """変換はモードを変えた時点で済ませ、取得時には行わない。

    `image_for()` は `paintEvent` から呼ばれる。ここで変換すると、
    描画経路に画素処理が入り込む。
    """
    render_page(service)
    calls = count_transforms(monkeypatch)

    service.set_color_mode(PageColorMode.INVERT)
    assert calls == [PageColorMode.INVERT], "モード変更の時点で用意されていない"

    for _ in range(5):
        assert service.image_for(0, A4_AT_72DPI, 1.0) is not None

    assert calls == [PageColorMode.INVERT], "取得のたびに変換している"


def test_the_display_image_is_prepared_when_the_render_completes(
    service: PageRenderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """レンダリングが終わった時点で表示用画像も用意しておく。"""
    service.set_color_mode(PageColorMode.INVERT)
    calls = count_transforms(monkeypatch)

    render_page(service)

    assert calls == [PageColorMode.INVERT]
    image = service.image_for(0, A4_AT_72DPI, 1.0)
    assert image is not None
    assert rgba(image) == BLACK


def test_the_display_image_is_prepared_when_the_pages_change(
    service: PageRenderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """必要なページの集合が変わったときにも用意し直す。

    仮表示に使う別解像度の分も含める。倍率を変えた直後、目的の解像度が
    届くまでのあいだも変換済みの絵を描けるようにするため。
    """
    render_page(service, size=QSize(297, 421))
    service.set_color_mode(PageColorMode.INVERT)
    calls = count_transforms(monkeypatch)

    # 倍率が上がって、まだ無い解像度が必要になった。
    service.request_pages([PageRequest(page_index=0, size_px=QSize(1190, 1684), dpr=1.0)])

    assert calls == []  # 既に用意済みの仮表示を使い回すので、変換は起きない
    assert service.placeholder_for(0, QSize(1190, 1684)) is not None


def test_returning_to_a_mode_transforms_again(
    service: PageRenderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """モードを戻すと、表示用画像は作り直す（raw は作り直さない）。

    使えない画像でメモリを占めないよう、切り替え時に表示用は捨てる。
    """
    render_page(service)
    calls = count_transforms(monkeypatch)

    service.set_color_mode(PageColorMode.INVERT)
    service.set_color_mode(PageColorMode.ORIGINAL)
    service.set_color_mode(PageColorMode.INVERT)

    assert calls == [PageColorMode.INVERT, PageColorMode.INVERT]
    assert len(service._cache) == 1  # noqa: SLF001


def test_setting_the_same_mode_keeps_the_display_cache(service: PageRenderService) -> None:
    """同じモードを選び直しても表示用画像は捨てない（連打で作り直さない）。"""
    render_page(service)
    service.set_color_mode(PageColorMode.INVERT)
    image = service.image_for(0, A4_AT_72DPI, 1.0)

    service.set_color_mode(PageColorMode.INVERT)

    assert service.image_for(0, A4_AT_72DPI, 1.0) is image


def test_the_placeholder_is_transformed_too(service: PageRenderService) -> None:
    """仮表示も現在のモードで変換して返す。

    変換前の絵を先に見せると、切り替えた瞬間に元の色が一瞬見える。
    """
    render_page(service, size=QSize(297, 421))
    service.set_color_mode(PageColorMode.INVERT)

    placeholder = service.placeholder_for(0, QSize(1190, 1684))

    assert placeholder is not None
    assert placeholder.width() == 297
    assert rgba(placeholder) == BLACK


def test_the_display_cache_is_bounded() -> None:
    """表示用キャッシュにも上限があり、超えた分は追い出される。"""
    image_bytes = QImage(595, 842, QImage.Format.Format_ARGB32).sizeInBytes()
    service = PageRenderService(RenderCache(), display_max_bytes=image_bytes * 2)
    service.set_color_mode(PageColorMode.INVERT)
    for page in range(4):
        service._cache.put(  # noqa: SLF001
            RenderKey(page, 595, 842, 1.0), QImage(595, 842, QImage.Format.Format_ARGB32)
        )

    service.request_pages(requests_for(range(0, 4)))

    assert service.display_cache.total_bytes <= service.display_cache.max_bytes
    assert len(service.display_cache) == 2
    # 残るのは最後に用意した2ページ分。
    assert service.image_for(3, A4_AT_72DPI, 1.0) is not None
    assert service.image_for(0, A4_AT_72DPI, 1.0) is None


def test_reset_drops_the_display_images_too(service: PageRenderService) -> None:
    """ドキュメントを入れ替えると、raw も表示用も前の PDF の画像が残らない。"""
    render_page(service)
    service.set_color_mode(PageColorMode.INVERT)
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
