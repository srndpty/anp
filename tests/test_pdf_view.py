"""`anp.ui.pdf_view` のテスト。

タイミングに依存させないため、実際のレンダリングは待たない。
`PageRenderService` に要求が渡るところまでを観測し、画像が必要な場合は
キャッシュへ直接入れてから描画結果を調べる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QSizeF, Qt
from PySide6.QtGui import QColor, QWheelEvent
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from anp.pdf.cache import RenderCache
from anp.pdf.color import PageColorMode
from anp.pdf.document import DocumentController
from anp.pdf.layout import LayoutMetrics, PageLayout
from anp.ui.appearance import CanvasTheme
from anp.ui.pdf_view import (
    MAX_ZOOM,
    MIN_ZOOM,
    NO_PAGE,
    ZOOM_STEP,
    PdfView,
    ReadingPosition,
    ZoomMode,
)
from helpers import RecordingService, UpdateSpy, count_transforms, put_image, render_view

# ビューが既定で使う配置寸法。フィット倍率の検算に使う。
MARGIN = LayoutMetrics().margin


# ------------------------------------------------------------------ 補助
def layout_of(controller: DocumentController) -> PageLayout:
    """ビューが内部で作るのと同じレイアウト。"""
    return PageLayout(controller.page_sizes())


def page_center_color(view: PdfView, page: int) -> QColor:
    """ページ中央に描かれた色。"""
    rect = view.page_viewport_rect(page)
    assert rect is not None
    center = rect.intersected(view.viewport().rect().toRectF()).center()
    return QColor(render_view(view).pixelColor(round(center.x()), round(center.y())))


def canvas_color(view: PdfView) -> QColor:
    """ページの外側（キャンバス）に描かれた色。

    上端の余白から取る。ページはビューポート上端から `MARGIN` だけ下に
    始まるので、そこより上は必ずキャンバス。
    """
    point = QPoint(2, 2)
    rect = view.page_viewport_rect(0)
    assert rect is None or not rect.contains(QPointF(point))
    return QColor(render_view(view).pixelColor(point))


# ------------------------------------------------------------------ レイアウトとスクロール
def test_document_sets_the_scrollbar_range(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """ドキュメントを設定するとコンテンツ全体分の可動域になる。"""
    content = layout_of(controller).content_size(1.0)
    viewport = loaded_view.viewport().size()

    assert loaded_view.verticalScrollBar().maximum() == pytest.approx(
        content.height() - viewport.height(), abs=1
    )
    assert loaded_view.horizontalScrollBar().maximum() == max(
        round(content.width() - viewport.width()), 0
    )


def test_clear_document_resets_the_scrollbars(loaded_view: PdfView) -> None:
    """クリアすると可動域が 0 になる。"""
    loaded_view.clear_document()

    assert loaded_view.verticalScrollBar().maximum() == 0
    assert loaded_view.horizontalScrollBar().maximum() == 0
    assert not loaded_view.has_document


def test_scrolling_moves_the_content_viewport(loaded_view: PdfView) -> None:
    """縦スクロールでコンテンツ座標の可視範囲が動く。"""
    before = loaded_view.content_viewport_rect()

    loaded_view.verticalScrollBar().setValue(500)

    after = loaded_view.content_viewport_rect()
    assert after.top() == pytest.approx(before.top() + 500)
    assert after.height() == pytest.approx(before.height())


def test_only_visible_pages_are_listed(loaded_view: PdfView) -> None:
    """先頭では1ページ目しか描画対象にならない。"""
    assert list(loaded_view.visible_pages()) == [0]


def test_scrolling_changes_the_visible_pages(loaded_view: PdfView) -> None:
    """スクロールすると描画対象のページが入れ替わる。"""
    loaded_view.verticalScrollBar().setValue(loaded_view.verticalScrollBar().maximum())

    assert 0 not in loaded_view.visible_pages()
    assert 2 in loaded_view.visible_pages()


def test_scrollbar_range_survives_a_resize(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """リサイズ後も可動域が正しい。"""
    loaded_view.resize(700, 300)

    content = layout_of(controller).content_size(1.0)
    viewport = loaded_view.viewport().size()
    assert loaded_view.verticalScrollBar().maximum() == pytest.approx(
        content.height() - viewport.height(), abs=1
    )


def test_resize_keeps_the_rendered_images(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """リサイズでキャッシュを捨てない。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    generation = service.generation

    loaded_view.resize(500, 400)

    assert len(cache) == 1
    assert service.generation == generation


# ------------------------------------------------------------------ レンダリング要求
def test_requests_are_limited_to_the_render_window(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """可視ページ ± 1 ページの外は要求しない。"""
    assert sorted(service.last_pages) == [0, 1]


def test_the_current_page_is_requested_first(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """現在ページが先読みより先に要求される。"""
    loaded_view.verticalScrollBar().setValue(loaded_view.verticalScrollBar().maximum())

    pages = service.last_pages
    assert pages[0] == loaded_view.current_page == 2
    assert pages.index(2) < pages.index(1)


def test_a_dpr_change_requests_the_new_resolution(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """DPR が変わったら要求し直す。

    高 DPI モニタへ移すだけではスクロールもリサイズもズームも起きないため、
    ここで拾わないと古い DPR の画像を拡大したまま表示し続けてしまう。
    """
    count = len(service.requests)

    QApplication.sendEvent(loaded_view, QEvent(QEvent.Type.DevicePixelRatioChange))

    assert len(service.requests) == count + 1


def test_a_dpr_change_keeps_the_zoom_and_the_mode(loaded_view: PdfView) -> None:
    """DPR が変わっても倍率とモードは動かさない。

    要求する解像度だけが変わる。倍率まで動くと、モニタ間の移動で表示の
    大きさが変わってしまう。
    """
    loaded_view.fit_width()
    zoom = loaded_view.zoom

    QApplication.sendEvent(loaded_view, QEvent(QEvent.Type.DevicePixelRatioChange))

    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.zoom_mode is ZoomMode.FIT_WIDTH


def test_a_dpr_change_keeps_the_scroll_position(loaded_view: PdfView) -> None:
    """DPR が変わってもスクロール位置と現在ページは動かさない。"""
    loaded_view.verticalScrollBar().setValue(1000)
    position = loaded_view.verticalScrollBar().value()
    page = loaded_view.current_page

    QApplication.sendEvent(loaded_view, QEvent(QEvent.Type.DevicePixelRatioChange))

    assert loaded_view.verticalScrollBar().value() == position
    assert loaded_view.current_page == page


def test_zoom_requests_only_once(loaded_view: PdfView, service: RecordingService) -> None:
    """ズームの途中経過でレンダリング要求を出さない。

    可動域と位置を段階的に変える途中でスクロールバーに追随すると、中間状態の
    要求とページ通知が出てしまう。
    """
    loaded_view.verticalScrollBar().setValue(1000)
    count = len(service.requests)

    loaded_view.set_zoom(2.0)

    assert len(service.requests) == count + 1


def test_setting_a_document_requests_only_once(
    loaded_view: PdfView, controller: DocumentController, service: RecordingService
) -> None:
    """ドキュメント切り替えの途中経過でレンダリング要求を出さない。"""
    loaded_view.verticalScrollBar().setValue(1000)
    count = len(service.requests)

    loaded_view.set_document(controller.document, controller.page_sizes())

    assert len(service.requests) == count + 1


def test_scrolling_updates_the_requested_pages(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """スクロールで要求対象が更新される。"""
    assert 2 not in service.last_pages

    loaded_view.verticalScrollBar().setValue(loaded_view.verticalScrollBar().maximum())

    assert 2 in service.last_pages
    assert 0 not in service.last_pages


def test_the_exact_image_is_painted(loaded_view: PdfView, cache: RenderCache) -> None:
    """目的の解像度の画像があればそれを描く。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.red)


def test_another_resolution_is_used_as_a_placeholder(
    loaded_view: PdfView, cache: RenderCache
) -> None:
    """目的の解像度が無ければ同じページの別解像度で仮表示する。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red, scale=0.5)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.red)


def test_unrendered_pages_are_painted_blank(loaded_view: PdfView) -> None:
    """まだ画像が無いページは白いまま描かれる。"""
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.white)


def test_page_ready_updates_only_that_page(loaded_view: PdfView, service: RecordingService) -> None:
    """`page_ready` では該当ページの領域だけを更新する。"""
    spy = UpdateSpy(loaded_view)

    service.page_ready.emit(0)

    expected = loaded_view.page_viewport_rect(0)
    assert expected is not None
    # 枠線のはみ出しを見込んで 1px 広げた領域。
    assert spy.rects == [
        expected.toAlignedRect().adjusted(-1, -1, 1, 1).intersected(loaded_view.viewport().rect())
    ]


def test_page_ready_outside_the_viewport_is_ignored(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """見えていないページの `page_ready` では再描画しない。"""
    spy = UpdateSpy(loaded_view)

    service.page_ready.emit(2)

    assert spy.rects == []


# ------------------------------------------------------------------ ズーム
def test_zoom_changes_the_page_geometry(loaded_view: PdfView) -> None:
    """ズームするとページの大きさが変わる。"""
    before = loaded_view.page_viewport_rect(0)
    assert before is not None

    loaded_view.set_zoom(2.0)

    after = loaded_view.page_viewport_rect(0)
    assert after is not None
    assert after.width() == pytest.approx(before.width() * 2)
    assert after.height() == pytest.approx(before.height() * 2)


def test_zoom_does_not_change_the_gap_and_margin(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """隙間と余白はズームしない。"""
    layout = layout_of(controller)

    for zoom in (1.0, 2.0, 4.0):
        gap = layout.page_rect(1, zoom).top() - layout.page_rect(0, zoom).bottom()
        assert gap == pytest.approx(layout.metrics.page_gap)
        assert layout.page_rect(0, zoom).top() == pytest.approx(layout.metrics.margin)


def test_zoom_updates_the_content_size_and_scrollbars(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """ズームで可動域が更新される。"""
    before = loaded_view.verticalScrollBar().maximum()

    loaded_view.set_zoom(2.0)

    content = layout_of(controller).content_size(2.0)
    viewport = loaded_view.viewport().size()
    assert loaded_view.verticalScrollBar().maximum() > before
    assert loaded_view.verticalScrollBar().maximum() == pytest.approx(
        content.height() - viewport.height(), abs=1
    )


def test_zoom_keeps_the_center_position(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """ズームしても中央付近の位置が保たれる（先頭に飛ばない）。"""
    layout = layout_of(controller)
    loaded_view.verticalScrollBar().setValue(1000)
    page = loaded_view.current_page
    before = layout.to_normalized(page, loaded_view.content_viewport_rect().center(), 1.0)

    loaded_view.set_zoom(2.0)

    assert loaded_view.current_page == page
    after = layout.to_normalized(page, loaded_view.content_viewport_rect().center(), 2.0)
    assert after.y() == pytest.approx(before.y(), abs=0.02)


def test_zoom_keeps_a_fully_visible_page_centered(view: PdfView, single_page_pdf: Path) -> None:
    """ページ全体が見えている状態から拡大しても末尾へ飛ばない。

    ビューポート中央がページの外（下の余白）にある状態。中央の位置を
    そのままページ内の比率として扱うと 1.0 を超え、拡大した瞬間に
    ページ末尾までスクロールしてしまう。
    """
    single = DocumentController()
    single.open(single_page_pdf)
    try:
        view.set_document(single.document, single.page_sizes())
        view.set_zoom(MIN_ZOOM)
        # ページ全体がビューポートに収まっていて、中央はページより下にある。
        page = view.page_viewport_rect(0)
        assert page is not None
        assert page.bottom() < view.viewport().height() / 2

        view.set_zoom(1.0)

        layout = PageLayout(single.page_sizes())
        center = layout.to_normalized(0, view.content_viewport_rect().center(), 1.0)
        assert center.y() == pytest.approx(0.5, abs=0.05)
        assert view.verticalScrollBar().value() < view.verticalScrollBar().maximum()
    finally:
        single.close()


def test_zoom_is_clamped(loaded_view: PdfView) -> None:
    """範囲外のズームは丸められる。"""
    loaded_view.set_zoom(100.0)
    assert loaded_view.zoom == MAX_ZOOM

    loaded_view.set_zoom(0.001)
    assert loaded_view.zoom == MIN_ZOOM


def test_zoom_requests_the_new_resolution(loaded_view: PdfView, service: RecordingService) -> None:
    """ズーム後は新しい解像度で要求し直す。"""
    before = service.requests[-1][0].size_px

    loaded_view.set_zoom(2.0)

    assert service.requests[-1][0].size_px.width() == pytest.approx(before.width() * 2, abs=2)


# ------------------------------------------------------------------ 現在ページ
def test_current_page_is_reported(loaded_view: PdfView) -> None:
    """現在ページを取得できる。"""
    assert loaded_view.current_page == 0

    loaded_view.verticalScrollBar().setValue(loaded_view.verticalScrollBar().maximum())

    assert loaded_view.current_page == 2


def test_current_page_changed_is_not_emitted_twice(loaded_view: PdfView) -> None:
    """同じページのままスクロールしても重複して通知しない。"""
    emitted: list[int] = []
    loaded_view.current_page_changed.connect(emitted.append)

    bar = loaded_view.verticalScrollBar()
    bar.setValue(10)
    bar.setValue(20)
    bar.setValue(30)

    assert emitted == []


def test_current_page_changed_is_emitted_on_change(loaded_view: PdfView) -> None:
    """ページが変わったときだけ通知する。"""
    emitted: list[int] = []
    loaded_view.current_page_changed.connect(emitted.append)

    loaded_view.verticalScrollBar().setValue(loaded_view.verticalScrollBar().maximum())

    assert emitted[-1] == 2


# ------------------------------------------------------------------ ドキュメントの入れ替え
def test_switching_documents_drops_the_old_images(
    loaded_view: PdfView, controller: DocumentController, cache: RenderCache
) -> None:
    """ドキュメントを切り替えると前の PDF の画像が残らない。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.red)

    loaded_view.set_document(controller.document, controller.page_sizes())

    assert len(cache) == 0
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.white)


def test_switching_documents_returns_to_the_top(loaded_view: PdfView, sample_pdf: Path) -> None:
    """切り替えたら先頭から表示する。"""
    loaded_view.verticalScrollBar().setValue(1000)

    other = DocumentController()
    other.open(sample_pdf)
    try:
        loaded_view.set_document(other.document, other.page_sizes())
    finally:
        other.close()

    assert loaded_view.verticalScrollBar().value() == 0
    assert loaded_view.current_page == 0


def test_no_requests_after_clear(loaded_view: PdfView, service: RecordingService) -> None:
    """クリア後はレンダリング要求を積まない。"""
    loaded_view.clear_document()
    count = len(service.requests)

    loaded_view.verticalScrollBar().setValue(0)
    loaded_view.resize(500, 500)
    loaded_view.set_zoom(2.0)

    assert len(service.requests) == count


# ------------------------------------------------------------------ 空の状態
def test_empty_view_has_no_scroll_range(view: PdfView) -> None:
    """ドキュメントが無ければ可動域は 0。"""
    assert view.verticalScrollBar().maximum() == 0
    assert view.horizontalScrollBar().maximum() == 0
    assert view.current_page == NO_PAGE
    assert view.page_count == 0


def test_empty_view_paints_without_a_document(view: PdfView) -> None:
    """ドキュメントが無くても描画で落ちない。"""
    image = render_view(view)

    assert image.pixelColor(10, 10) != QColor(Qt.GlobalColor.black)
    assert not view.visible_pages()


def test_empty_view_requests_nothing(view: PdfView, service: RecordingService) -> None:
    """ドキュメントが無ければ要求を出さない。"""
    view.resize(500, 500)
    view.set_zoom(2.0)

    assert service.requests == []
    assert view.page_viewport_rect(0) is None


def test_a_document_can_be_set_after_clearing(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """クリア後に再度ドキュメントを設定できる。"""
    loaded_view.clear_document()

    loaded_view.set_document(controller.document, controller.page_sizes())

    assert loaded_view.page_count == 3
    assert list(loaded_view.visible_pages()) == [0]


def test_page_sizes_come_from_the_caller(view: PdfView, controller: DocumentController) -> None:
    """ページ寸法はドキュメントからではなく呼び出し側から与える。"""
    sizes = [QSizeF(100.0, 200.0), *controller.page_sizes()[1:]]

    view.set_document(controller.document, sizes)

    assert view.page_count == 3
    first = view.page_viewport_rect(0)
    assert first is not None
    assert first.width() == pytest.approx(100.0)
    assert first.height() == pytest.approx(200.0)


def test_page_size_count_must_match_the_document(
    view: PdfView, controller: DocumentController
) -> None:
    """ページ数の合わない寸法は受け付けない。

    黙って受け入れると、ドキュメントは3ページなのにレイアウトは2ページ、
    という壊れた状態のまま表示してしまう。
    """
    with pytest.raises(ValueError, match="page size count"):
        view.set_document(controller.document, controller.page_sizes()[:2])


def test_a_rejected_document_does_not_disturb_the_current_one(
    loaded_view: PdfView, controller: DocumentController, cache: RenderCache
) -> None:
    """受け付けられなかった設定は、いま表示中の状態を壊さない。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)

    with pytest.raises(ValueError, match="page size count"):
        loaded_view.set_document(controller.document, [])

    assert loaded_view.page_count == 3
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.red)


# ------------------------------------------------------------------ 倍率モード
def test_manual_zoom_switches_to_free(loaded_view: PdfView) -> None:
    """手動でズームすると FREE になる。"""
    loaded_view.fit_width()
    fitted: ZoomMode = loaded_view.zoom_mode
    assert fitted is ZoomMode.FIT_WIDTH

    loaded_view.set_zoom(1.0)

    assert loaded_view.zoom_mode is ZoomMode.FREE
    assert loaded_view.zoom == pytest.approx(1.0)


def test_zoom_in_and_out_use_a_ratio(loaded_view: PdfView) -> None:
    """ズームは加算ではなく倍率比で変わる。"""
    loaded_view.set_zoom(1.0)

    loaded_view.zoom_in()
    assert loaded_view.zoom == pytest.approx(ZOOM_STEP)

    loaded_view.zoom_in()
    assert loaded_view.zoom == pytest.approx(2.0)

    loaded_view.zoom_out()
    assert loaded_view.zoom == pytest.approx(ZOOM_STEP)


def test_the_zoom_ratio_is_the_same_at_any_zoom(loaded_view: PdfView) -> None:
    """どの倍率からでも同じ比率で変わる（一定量の加算ではない）。"""
    loaded_view.set_zoom(2.0)

    loaded_view.zoom_in()

    assert loaded_view.zoom == pytest.approx(2.0 * ZOOM_STEP)


def test_zoom_in_switches_to_free(loaded_view: PdfView) -> None:
    """ズームイン/アウトでも FREE へ移る。"""
    loaded_view.fit_page()

    loaded_view.zoom_in()

    assert loaded_view.zoom_mode is ZoomMode.FREE


def test_fit_width_matches_the_viewport(loaded_view: PdfView) -> None:
    """幅フィットでページ幅が余白を除いたビューポート幅に一致する。"""
    loaded_view.fit_width()

    rect = loaded_view.page_viewport_rect(0)
    assert rect is not None
    assert rect.width() == pytest.approx(loaded_view.viewport().width() - MARGIN * 2)
    assert loaded_view.zoom_mode is ZoomMode.FIT_WIDTH


def test_fit_page_fits_the_whole_page(loaded_view: PdfView) -> None:
    """ページフィットでページ全体がビューポートに収まる。"""
    loaded_view.fit_page()

    rect = loaded_view.page_viewport_rect(0)
    assert rect is not None
    assert rect.width() <= loaded_view.viewport().width()
    assert rect.height() <= loaded_view.viewport().height()
    assert loaded_view.zoom_mode is ZoomMode.FIT_PAGE


def test_fit_page_is_not_larger_than_fit_width(loaded_view: PdfView) -> None:
    """ページフィットは幅フィット以下の倍率になる（縦横の厳しい方）。"""
    loaded_view.fit_width()
    width_zoom = loaded_view.zoom

    loaded_view.fit_page()

    assert loaded_view.zoom <= width_zoom


def test_fit_zoom_stays_within_the_limits(view: PdfView, controller: DocumentController) -> None:
    """収まりようがない大きさでもフィット倍率は上下限に収まる。"""
    view.set_document(controller.document, controller.page_sizes())

    view.resize(60, 60)
    view.fit_page()
    assert view.zoom == MIN_ZOOM

    view.resize(20000, 20000)
    view.fit_width()
    assert view.zoom == MAX_ZOOM


def test_fit_uses_the_page_current_at_that_moment(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """フィットの基準はその瞬間の現在ページ。"""
    sizes = [QSizeF(100.0, 200.0), QSizeF(400.0, 200.0), QSizeF(100.0, 200.0)]
    loaded_view.set_document(controller.document, sizes)
    loaded_view.fit_width()
    first = loaded_view.zoom

    loaded_view.go_to_page(1)
    assert loaded_view.current_page == 1

    loaded_view.fit_width()

    # 2ページ目は4倍幅なので、同じ幅に収めるには 1/4 の倍率になる。
    assert loaded_view.zoom == pytest.approx(first / 4.0)


def test_scrolling_does_not_change_the_fit_zoom(loaded_view: PdfView) -> None:
    """スクロールしただけでは倍率を計算し直さない。"""
    loaded_view.fit_page()
    zoom = loaded_view.zoom

    loaded_view.verticalScrollBar().setValue(loaded_view.verticalScrollBar().maximum())

    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.zoom_mode is ZoomMode.FIT_PAGE


def test_crossing_a_page_does_not_change_the_fit_zoom(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """幅の違うページへ移っても、跨いだだけでは倍率が動かない。"""
    sizes = [QSizeF(595.0, 842.0), QSizeF(200.0, 842.0), QSizeF(595.0, 842.0)]
    loaded_view.set_document(controller.document, sizes)
    loaded_view.fit_width()
    zoom = loaded_view.zoom

    loaded_view.go_to_page(1)

    assert loaded_view.current_page == 1
    assert loaded_view.zoom == pytest.approx(zoom)


def test_resize_keeps_a_free_zoom(loaded_view: PdfView) -> None:
    """FREE ならリサイズで倍率は変わらない。"""
    loaded_view.set_zoom(1.0)

    loaded_view.resize(700, 500)

    assert loaded_view.zoom == pytest.approx(1.0)


def test_resize_recomputes_a_fit_zoom(loaded_view: PdfView, controller: DocumentController) -> None:
    """フィットモードならリサイズで倍率を計算し直す。"""
    loaded_view.fit_width()
    before = loaded_view.zoom

    loaded_view.resize(700, 500)

    expected = layout_of(controller).fit_width_zoom(0, loaded_view.viewport().width())
    assert loaded_view.zoom == pytest.approx(expected)
    assert loaded_view.zoom != pytest.approx(before)
    assert loaded_view.zoom_mode is ZoomMode.FIT_WIDTH


def test_a_fit_resize_does_not_add_render_requests(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """フィット中のリサイズでも、倍率変更とリサイズで要求が二重に出ない。

    Qt はスクロールバーの出入りで1回のリサイズに複数の `resizeEvent` を
    送るので、絶対数ではなく FREE のときとの差で見る。
    """
    loaded_view.set_zoom(1.0)
    count = len(service.requests)
    loaded_view.resize(700, 500)
    free_cost = len(service.requests) - count

    loaded_view.fit_width()
    count = len(service.requests)
    loaded_view.resize(500, 400)
    fit_cost = len(service.requests) - count

    assert fit_cost == free_cost


def test_zoom_changed_reports_a_mode_switch(loaded_view: PdfView) -> None:
    """倍率の値が同じでもモードが変われば通知する。"""
    emitted: list[None] = []
    loaded_view.zoom_changed.connect(lambda: emitted.append(None))

    loaded_view.fit_width()
    assert len(emitted) == 1

    loaded_view.set_zoom(loaded_view.zoom)

    assert loaded_view.zoom_mode is ZoomMode.FREE
    assert len(emitted) == 2


def test_a_new_document_reapplies_the_fit_mode(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """ドキュメントを差し替えてもモードは保たれ、倍率は計算し直される。"""
    loaded_view.fit_width()

    loaded_view.set_document(controller.document, [QSizeF(200.0, 400.0)] * 3)

    assert loaded_view.zoom_mode is ZoomMode.FIT_WIDTH
    rect = loaded_view.page_viewport_rect(0)
    assert rect is not None
    assert rect.width() == pytest.approx(loaded_view.viewport().width() - MARGIN * 2)


def test_a_new_document_keeps_the_free_zoom(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """FREE なら手動で指定した倍率をそのまま引き継ぐ。"""
    loaded_view.set_zoom(2.0)

    loaded_view.set_document(controller.document, controller.page_sizes())

    assert loaded_view.zoom == pytest.approx(2.0)


def test_the_last_free_zoom_survives_a_fit(loaded_view: PdfView) -> None:
    """フィットへ切り替えても、最後に手で指定した倍率は覚えている。"""
    loaded_view.set_zoom(2.0)

    loaded_view.fit_page()

    assert loaded_view.zoom != pytest.approx(2.0)
    assert loaded_view.last_free_zoom == pytest.approx(2.0)


def test_the_last_free_zoom_follows_manual_zoom(loaded_view: PdfView) -> None:
    """手動の倍率変更のたびに更新される。"""
    loaded_view.set_zoom(2.0)
    assert loaded_view.last_free_zoom == pytest.approx(2.0)

    loaded_view.zoom_in()

    assert loaded_view.last_free_zoom == pytest.approx(loaded_view.zoom)


def test_fit_without_a_document_only_records_the_mode(view: PdfView) -> None:
    """ドキュメントが無くてもモードだけは覚える。"""
    view.fit_width()

    assert view.zoom_mode is ZoomMode.FIT_WIDTH
    assert view.zoom == pytest.approx(1.0)


# ------------------------------------------------------------------ フィットとスクロールバー
def settle(view: PdfView, *, rounds: int = 8) -> list[float]:
    """保留中のレイアウトを流し切りながら、各回の倍率を記録する。

    スクロールバーの出し入れは `QAbstractScrollArea` が遅延して行うので、
    イベントを流さないと最終状態にならない。待ち時間ではなくイベントの
    処理回数で回すので、実行環境の速さに依存しない。
    """
    zooms: list[float] = []
    for _ in range(rounds):
        QApplication.processEvents()
        zooms.append(view.zoom)
    return zooms


def single_page_view(view: PdfView, path: Path, size: QSizeF | None = None) -> PdfView:
    """1ページ PDF を設定したビュー。`size` を渡すと寸法を差し替える。"""
    controller = DocumentController()
    controller.open(path)
    try:
        view.set_document(controller.document, [size] if size else controller.page_sizes())
    finally:
        controller.close()
    return view


def test_the_vertical_scrollbar_appears_when_a_document_is_set(loaded_view: PdfView) -> None:
    """PDF を設定した時点で縦スクロールバーが出る。

    可動域の変化はバーを出し入れする合図でもあるので、更新中に
    `blockSignals()` で黙らせると、リサイズするまでバーが出ないままになる。
    """
    settle(loaded_view)

    assert loaded_view.verticalScrollBar().isVisible()
    assert loaded_view.verticalScrollBar().maximum() > 0


def test_the_scrollbars_disappear_when_the_document_is_cleared(loaded_view: PdfView) -> None:
    """クリアするとバーも消える。"""
    settle(loaded_view)

    loaded_view.clear_document()
    settle(loaded_view)

    assert not loaded_view.verticalScrollBar().isVisible()
    assert not loaded_view.horizontalScrollBar().isVisible()


@pytest.mark.parametrize("height", range(540, 620, 4))
def test_fit_width_settles_when_the_scrollbar_toggles(
    view: PdfView, single_page_pdf: Path, height: int
) -> None:
    """幅フィットが縦スクロールバーの出入りで振動しない。

    バーが出るとビューポートが狭まり、幅フィットの倍率が下がり、ページが
    縦に縮んでバーが要らなくなる…という往復が起こりうる大きさがある
    （1ページの A4 で実際に再現した）。倍率をバーの有無から決めないので、
    同じウィンドウサイズなら必ず同じ倍率に落ち着く。

    リサイズイベントの回数ではなく、最終状態が動かなくなることを見る。
    """
    single_page_view(view, single_page_pdf)
    view.fit_width()

    view.resize(410, height)
    zooms = settle(view, rounds=10)

    assert zooms[-4:] == pytest.approx([zooms[-1]] * 4)
    assert view.zoom_mode is ZoomMode.FIT_WIDTH


@pytest.mark.parametrize("height", range(540, 620, 4))
def test_fit_width_leaves_no_horizontal_scrollbar(
    view: PdfView, single_page_pdf: Path, height: int
) -> None:
    """収束後、ページ幅がビューポートに収まっていて横バーが出ない。

    振動を「バーを出したまま」で止めても、ページが横にはみ出していては
    収束したとは言えない。
    """
    single_page_view(view, single_page_pdf)
    view.fit_width()

    view.resize(410, height)
    settle(view, rounds=10)

    rect = view.page_viewport_rect(0)
    assert rect is not None
    assert rect.width() <= view.viewport().width()
    assert view.horizontalScrollBar().maximum() == 0


@pytest.mark.parametrize("width", range(360, 720, 6))
def test_fit_width_never_needs_horizontal_scrolling(
    view: PdfView, single_page_pdf: Path, width: int
) -> None:
    """どの幅でも、幅フィットの後に横スクロールが残らない。

    ページ幅はビューポート幅ちょうどに合わせるので、可動域を素直に
    切り上げると浮動小数点の誤差が「1px 分の横スクロール」に化ける。
    """
    single_page_view(view, single_page_pdf)
    view.fit_width()

    view.resize(width, 600)
    settle(view, rounds=10)

    assert view.horizontalScrollBar().maximum() == 0
    assert not view.horizontalScrollBar().isVisible()


def test_fit_page_settles_when_the_scrollbar_toggles(view: PdfView, single_page_pdf: Path) -> None:
    """ページフィットでも同じ大きさなら同じ倍率に落ち着く。"""
    single_page_view(view, single_page_pdf)
    view.fit_page()

    for height in range(540, 620, 4):
        view.resize(410, height)
        zooms = settle(view, rounds=10)
        assert zooms[-4:] == pytest.approx([zooms[-1]] * 4), f"height={height}"


def test_the_same_size_gives_the_same_fit_zoom(view: PdfView, single_page_pdf: Path) -> None:
    """行き来しても、同じウィンドウサイズなら同じ倍率に戻る。

    倍率が「いまバーが出ているか」に依存すると、同じ大きさでも直前の
    経路によって違う倍率になる。
    """
    single_page_view(view, single_page_pdf)
    view.fit_width()

    view.resize(410, 560)
    settle(view)
    first = view.zoom

    view.resize(800, 300)
    settle(view)
    view.resize(410, 560)
    settle(view)

    assert view.zoom == pytest.approx(first)


# ------------------------------------------------------------------ 極端なページ
@pytest.mark.parametrize(
    ("width", "height"),
    [(2000.0, 100.0), (100.0, 5000.0), (10.0, 10.0), (5000.0, 5000.0)],
)
def test_extreme_aspect_ratios_fit_within_the_limits(
    view: PdfView, single_page_pdf: Path, width: float, height: float
) -> None:
    """極端な縦横比でも倍率は上下限に収まり、状態が落ち着く。"""
    single_page_view(view, single_page_pdf, QSizeF(width, height))

    for apply_fit in (view.fit_width, view.fit_page):
        apply_fit()
        zooms = settle(view, rounds=10)

        assert MIN_ZOOM <= view.zoom <= MAX_ZOOM
        assert zooms[-4:] == pytest.approx([zooms[-1]] * 4)


def test_a_wide_page_fits_the_width(view: PdfView, single_page_pdf: Path) -> None:
    """横長のページでも、下限に達しない範囲なら幅フィットは幅に収まる。"""
    single_page_view(view, single_page_pdf, QSizeF(1000.0, 100.0))

    view.fit_width()
    settle(view)

    rect = view.page_viewport_rect(0)
    assert rect is not None
    assert rect.width() <= view.viewport().width()


def test_a_tall_page_fits_the_page(view: PdfView, single_page_pdf: Path) -> None:
    """縦長のページでも、下限に達しない範囲ならページフィットは収まる。"""
    single_page_view(view, single_page_pdf, QSizeF(300.0, 1000.0))

    view.fit_page()
    settle(view)

    rect = view.page_viewport_rect(0)
    assert rect is not None
    assert rect.height() <= view.viewport().height()
    assert rect.width() <= view.viewport().width()


def test_a_page_too_large_to_fit_stops_at_the_minimum_zoom(
    view: PdfView, single_page_pdf: Path
) -> None:
    """下限より小さくしないと収まらないページは、下限で止めてスクロールに任せる。

    フィットのために `MIN_ZOOM` を割ると、読めない大きさまで縮んでしまう。
    """
    single_page_view(view, single_page_pdf, QSizeF(2000.0, 100.0))

    view.fit_width()
    settle(view)

    assert view.zoom == MIN_ZOOM
    rect = view.page_viewport_rect(0)
    assert rect is not None
    assert rect.width() > view.viewport().width()
    # 収まらない分は横スクロールで届く。
    assert view.horizontalScrollBar().maximum() > 0


def test_a_single_page_document_has_no_next_page(view: PdfView, single_page_pdf: Path) -> None:
    """1ページの PDF では移動しても現在ページは 0 のまま。"""
    single_page_view(view, single_page_pdf)

    view.go_to_page(1)

    assert view.page_count == 1
    assert view.current_page == 0
    assert list(view.visible_pages()) == [0]


def test_min_and_max_zoom_still_paint(view: PdfView, single_page_pdf: Path) -> None:
    """上下限の倍率でも描画とレンダリング要求が成立する。"""
    single_page_view(view, single_page_pdf)

    for zoom in (MIN_ZOOM, MAX_ZOOM):
        view.set_zoom(zoom)
        settle(view)

        assert view.zoom == pytest.approx(zoom)
        assert page_center_color(view, 0) == QColor(Qt.GlobalColor.white)


# ------------------------------------------------------------------ Ctrl+ホイール
def send_wheel(
    view: PdfView, position: QPointF, delta: int, modifiers: Qt.KeyboardModifier
) -> None:
    """ビューポートへホイールイベントを送る。"""
    event = QWheelEvent(
        position,
        QPointF(view.viewport().mapToGlobal(position.toPoint())),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(view.viewport(), event)


def test_ctrl_wheel_zooms_in(loaded_view: PdfView) -> None:
    """Ctrl+ホイールで拡大する。"""
    loaded_view.set_zoom(1.0)

    send_wheel(loaded_view, QPointF(200.0, 300.0), 120, Qt.KeyboardModifier.ControlModifier)

    assert loaded_view.zoom == pytest.approx(ZOOM_STEP)
    assert loaded_view.zoom_mode is ZoomMode.FREE


def test_ctrl_wheel_zooms_out(loaded_view: PdfView) -> None:
    """逆回転では縮小する。"""
    loaded_view.set_zoom(1.0)

    send_wheel(loaded_view, QPointF(200.0, 300.0), -120, Qt.KeyboardModifier.ControlModifier)

    assert loaded_view.zoom == pytest.approx(1.0 / ZOOM_STEP)


def test_plain_wheel_scrolls_without_zooming(loaded_view: PdfView) -> None:
    """修飾なしのホイールはスクロールだけ。"""
    loaded_view.set_zoom(1.0)
    before = loaded_view.verticalScrollBar().value()

    send_wheel(loaded_view, QPointF(200.0, 300.0), -120, Qt.KeyboardModifier.NoModifier)

    assert loaded_view.zoom == pytest.approx(1.0)
    assert loaded_view.verticalScrollBar().value() > before


def test_ctrl_wheel_keeps_the_point_under_the_cursor(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """カーソル直下の PDF 上の点が、ズームの前後でほぼ動かない。"""
    layout = layout_of(controller)
    loaded_view.verticalScrollBar().setValue(300)
    position = QPointF(200.0, 250.0)

    def under_cursor(zoom: float) -> QPointF:
        content = position + loaded_view.content_viewport_rect().topLeft()
        return layout.to_normalized(loaded_view.current_page, content, zoom)

    before = under_cursor(loaded_view.zoom)

    send_wheel(loaded_view, position, 120, Qt.KeyboardModifier.ControlModifier)

    after = under_cursor(loaded_view.zoom)
    assert after.x() == pytest.approx(before.x(), abs=0.01)
    assert after.y() == pytest.approx(before.y(), abs=0.01)


def test_ctrl_wheel_over_the_canvas_clamps_to_the_page(loaded_view: PdfView) -> None:
    """カーソルがページの外なら、いちばん近いページの縁を留める。

    ページ外の比率をそのまま使うと、1.0 を超える正規化座標になり、
    拡大した瞬間に遠くへ飛んでしまう。
    """
    loaded_view.set_zoom(MIN_ZOOM)
    rect = loaded_view.page_viewport_rect(0)
    assert rect is not None
    # 1ページ目の左下、ページの外のキャンバス上。
    position = QPointF(5.0, rect.bottom() + 5.0)

    send_wheel(loaded_view, position, 120, Qt.KeyboardModifier.ControlModifier)

    after = loaded_view.page_viewport_rect(0)
    assert after is not None
    assert after.bottom() == pytest.approx(position.y(), abs=2)
    assert 0 in loaded_view.visible_pages()


def test_ctrl_wheel_requests_render_only_once(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """ホイール1回につきレンダリング要求は1回。"""
    count = len(service.requests)

    send_wheel(loaded_view, QPointF(200.0, 300.0), 120, Qt.KeyboardModifier.ControlModifier)

    assert len(service.requests) == count + 1


# ------------------------------------------------------------------ ページの色
def test_the_default_page_color_mode_is_original(view: PdfView) -> None:
    """既定はオリジナル。"""
    assert view.page_color_mode is PageColorMode.ORIGINAL


def test_original_paints_the_rendered_pixels(loaded_view: PdfView, cache: RenderCache) -> None:
    """オリジナルでは元の画素を描く。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.white)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.white)


def test_invert_paints_the_inverted_pixels(loaded_view: PdfView, cache: RenderCache) -> None:
    """反転ではページ画像だけが反転して描かれる。

    白ではなく赤を使うのは、反転の結果（cyan）と、画像が無いときの
    ページ下地（黒）を取り違えないため。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)

    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)


def test_the_canvas_is_not_inverted(loaded_view: PdfView, cache: RenderCache) -> None:
    """キャンバス（ページの外側）はモードを変えても色が変わらない。

    これが Windows 全体の色反転ではなく、ページだけを反転する目的そのもの。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    before = canvas_color(loaded_view)

    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert canvas_color(loaded_view) == before


def test_the_loading_background_is_dark_in_invert(loaded_view: PdfView) -> None:
    """画像がまだ無い間のページ下地も、反転に合わせて暗くする。

    白いまま塗ると、読み込み中だけ画面が白く光る。
    """
    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.black)
    # 暗いのはページの下地だけで、キャンバスは巻き添えにしない。
    assert canvas_color(loaded_view) != QColor(Qt.GlobalColor.black)


def test_the_placeholder_is_inverted_too(loaded_view: PdfView, cache: RenderCache) -> None:
    """目的の解像度が無くても、仮表示は現在のモードで描く。

    一瞬だけ元の色が見えてから反転へ切り替わる、という状態を避ける。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red, scale=0.5)

    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)


def test_the_old_mode_image_is_not_painted_after_a_switch(
    loaded_view: PdfView, cache: RenderCache
) -> None:
    """切り替えた直後に旧モードの画像を描かない。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)

    loaded_view.set_page_color_mode(PageColorMode.ORIGINAL)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.red)


def test_a_mode_switch_repaints_the_viewport(loaded_view: PdfView) -> None:
    """モードを変えたらビューポートを描き直す。"""
    spy = UpdateSpy(loaded_view)

    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert spy.rects == [None]


def test_a_mode_switch_does_not_rerender(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """モードを変えても PDF のレンダリングをやり直さない。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.white)
    count = len(service.requests)
    generation = service.generation

    for mode in (PageColorMode.INVERT, PageColorMode.ORIGINAL, PageColorMode.INVERT):
        loaded_view.set_page_color_mode(mode)

    assert len(service.requests) == count
    assert service.generation == generation
    assert len(cache) == 1


def test_a_mode_switch_keeps_the_zoom_and_the_position(loaded_view: PdfView) -> None:
    """モードを変えても倍率・倍率モード・現在ページ・スクロール位置は動かない。"""
    loaded_view.fit_width()
    loaded_view.verticalScrollBar().setValue(500)
    zoom = loaded_view.zoom
    position = loaded_view.verticalScrollBar().value()
    page = loaded_view.current_page

    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.zoom_mode is ZoomMode.FIT_WIDTH
    assert loaded_view.verticalScrollBar().value() == position
    assert loaded_view.current_page == page


def test_the_mode_survives_a_document_switch(
    loaded_view: PdfView,
    controller: DocumentController,
    cache: RenderCache,
    service: RecordingService,
) -> None:
    """ドキュメントを替えてもモードは維持され、新しい PDF も反転で描かれる。"""
    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    loaded_view.set_document(controller.document, controller.page_sizes())

    assert loaded_view.page_color_mode is PageColorMode.INVERT
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    service.repeat_last_request()
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)


def test_the_mode_can_be_set_without_a_document(view: PdfView) -> None:
    """ドキュメントが無くてもモードを選べる。"""
    view.set_page_color_mode(PageColorMode.INVERT)

    assert view.page_color_mode is PageColorMode.INVERT


def test_painting_never_transforms(
    loaded_view: PdfView, cache: RenderCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """描画では色変換を一切行わない。

    `paintEvent` は「引き当てて `drawImage()`」だけ。変換が描画経路に
    入ると、スクロール中ずっと CPU を使い、Smart Dark でさらに悪化する。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    calls = count_transforms(monkeypatch)

    for _ in range(5):
        render_view(loaded_view)
    loaded_view.verticalScrollBar().setValue(200)
    render_view(loaded_view)

    assert calls == []


def test_the_transform_happens_once_when_the_mode_changes(
    loaded_view: PdfView, cache: RenderCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """変換はモードを変えた時点で1回だけ済ませる。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    calls = count_transforms(monkeypatch)

    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert calls == [PageColorMode.INVERT]
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)


# ------------------------------------------------------------------ 変換の完了待ち
# 変換はワーカーで走るので、モードを切り替えた直後は現在のモードの画像が
# まだ無いことがある。その間に何を描くか。


def test_the_page_is_dark_while_the_invert_transform_is_pending(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """変換の完了を待っている間は、ページ下地（黒）を描く。

    **元の明るい画像を一瞬でも描いてはいけない。** 切り替えた瞬間に画面が
    白く光るのを避けるのが Invert の目的そのもの。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    service.transforms.immediate = False

    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert service.transforms.pending, "テストの前提が崩れている（変換が既に終わっている）"
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.black)


def test_the_page_becomes_inverted_when_the_transform_completes(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """変換が終わったら、そのページを描き直して反転画像を出す。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    service.transforms.immediate = False
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    spy = UpdateSpy(loaded_view)

    service.transforms.complete_all()

    assert spy.rects != [], "変換が終わっても描き直していない"
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)


def test_the_nearest_inverted_image_is_shown_while_waiting(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """目的の解像度の変換待ちでも、現在のモードの別解像度があればそれを描く。

    ここで raw を出すと赤が見えてしまう。出すのは変換済みの絵だけ。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red, scale=0.5)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)

    # 目的の解像度の raw が届いたが、その変換はまだ終わっていない。
    service.transforms.immediate = False
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    service.repeat_last_request()

    assert service.transforms.pending
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)


def test_smart_dark_paints_the_hue_preserving_pixels(
    loaded_view: PdfView, cache: RenderCache
) -> None:
    """スマートダークでは赤が赤のまま描かれる。

    反転なら水色になるところ。図表の色を補色へ変えないのが目的。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)

    loaded_view.set_page_color_mode(PageColorMode.SMART_DARK)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.red)


def test_smart_dark_darkens_a_white_page(loaded_view: PdfView, cache: RenderCache) -> None:
    """白い紙面は黒くなる（無彩色では反転と同じ）。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.white)

    loaded_view.set_page_color_mode(PageColorMode.SMART_DARK)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.black)


def test_the_loading_background_is_dark_in_smart_dark(loaded_view: PdfView) -> None:
    """スマートダークでも、画像がまだ無い間のページ下地は黒。

    変換待ちの間に明るい紙面を見せない。
    """
    loaded_view.set_page_color_mode(PageColorMode.SMART_DARK)

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.black)
    assert canvas_color(loaded_view) != QColor(Qt.GlobalColor.black)


def test_the_page_is_dark_while_the_smart_dark_transform_is_pending(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """スマートダークの変換待ちでも、元の明るい画像を一瞬でも描かない。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.white)
    service.transforms.immediate = False

    loaded_view.set_page_color_mode(PageColorMode.SMART_DARK)

    assert service.transforms.pending, "テストの前提が崩れている（変換が既に終わっている）"
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.black)


def test_the_invert_image_is_not_painted_while_smart_dark_is_pending(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """反転からスマートダークへ切り替えた直後に、反転の絵を描かない。

    他モードの絵を仮表示に使うと、切り替えた瞬間に前の色相が見えてしまう。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)
    service.transforms.immediate = False

    loaded_view.set_page_color_mode(PageColorMode.SMART_DARK)

    assert service.transforms.pending
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.black)

    service.transforms.complete_all()

    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.red)


def test_painting_never_transforms_in_smart_dark(
    loaded_view: PdfView, cache: RenderCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """スマートダークでも `paintEvent` で画素処理をしない。

    Invert より重い変換なので、描画経路に入るとスクロールが目に見えて重くなる。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.SMART_DARK)
    calls = count_transforms(monkeypatch)

    for _ in range(5):
        render_view(loaded_view)
    loaded_view.verticalScrollBar().setValue(200)
    render_view(loaded_view)

    assert calls == []


def test_switching_between_transformed_modes_does_not_rerender(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """反転 ⇄ スマートダークでも PDF のレンダリングはやり直さない。

    `PageColorMode` は `RenderKey` に入らないので、色を変えても raw は同じ。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    count = len(service.requests)
    generation = service.generation

    for mode in (PageColorMode.INVERT, PageColorMode.SMART_DARK, PageColorMode.INVERT):
        loaded_view.set_page_color_mode(mode)

    assert len(service.requests) == count
    assert service.generation == generation
    assert len(cache) == 1


def test_a_mode_switch_returns_without_waiting_for_the_worker(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """モードの切り替えは変換の完了を待たずに戻る。

    待つ実装だと、Smart Dark で切り替えのたびに UI が固まる。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    service.transforms.immediate = False

    for mode in (PageColorMode.INVERT, PageColorMode.ORIGINAL, PageColorMode.INVERT):
        loaded_view.set_page_color_mode(mode)

    # 連打しても走っているのは1件だけ。どの呼び出しも待たずに戻っている。
    assert len(service.transforms.submitted) == 1
    assert service.transform_inflight_count == 1


# ------------------------------------------------------------------ キャンバスの色
def test_the_default_canvas_theme_is_dark_gray(view: PdfView) -> None:
    """既定はダークグレー。Phase 1 までと同じ見た目。"""
    assert view.canvas_theme is CanvasTheme.DARK_GRAY
    assert canvas_color(view) == QColor("#525659")


@pytest.mark.parametrize(
    ("theme", "expected"),
    [
        (CanvasTheme.BLACK, "#000000"),
        (CanvasTheme.DARK_GRAY, "#525659"),
        (CanvasTheme.WHITE, "#ffffff"),
    ],
)
def test_each_canvas_theme_paints_its_color(
    loaded_view: PdfView, theme: CanvasTheme, expected: str
) -> None:
    """選んだ色でページの外側が塗られる。"""
    loaded_view.set_canvas_theme(theme)

    assert loaded_view.canvas_theme is theme
    assert canvas_color(loaded_view) == QColor(expected)


def test_the_canvas_theme_does_not_change_the_page_pixels(
    loaded_view: PdfView, cache: RenderCache
) -> None:
    """キャンバスを変えてもページの画素は変わらない。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    before = page_center_color(loaded_view, 0)

    loaded_view.set_canvas_theme(CanvasTheme.WHITE)

    assert page_center_color(loaded_view, 0) == before


def test_the_canvas_theme_does_not_change_the_page_color_mode(loaded_view: PdfView) -> None:
    """キャンバスを変えてもページの色変換は変わらない（自動連動しない）。"""
    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    for theme in (CanvasTheme.WHITE, CanvasTheme.BLACK, CanvasTheme.DARK_GRAY):
        loaded_view.set_canvas_theme(theme)
        assert loaded_view.page_color_mode is PageColorMode.INVERT


def test_the_page_color_mode_does_not_change_the_canvas_theme(loaded_view: PdfView) -> None:
    """逆向きにも連動しない。"""
    loaded_view.set_canvas_theme(CanvasTheme.BLACK)

    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert loaded_view.canvas_theme is CanvasTheme.BLACK


def test_a_white_canvas_with_invert_is_allowed(loaded_view: PdfView, cache: RenderCache) -> None:
    """白いキャンバスと反転ページの組み合わせも、そのまま描く。

    見た目として好ましくなくても自動補正はしない。3つの軸は独立。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_canvas_theme(CanvasTheme.WHITE)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert canvas_color(loaded_view) == QColor(Qt.GlobalColor.white)
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.cyan)


def test_a_black_canvas_with_original_is_allowed(loaded_view: PdfView, cache: RenderCache) -> None:
    """黒いキャンバスとオリジナルページの組み合わせも、そのまま描く。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.white)
    loaded_view.set_canvas_theme(CanvasTheme.BLACK)

    assert canvas_color(loaded_view) == QColor(Qt.GlobalColor.black)
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.white)


def test_the_canvas_color_stays_out_of_the_page_rect(loaded_view: PdfView) -> None:
    """ページ矩形の下地はページの色変換由来で、キャンバスの色は入り込まない。

    白いキャンバス + 反転でも、読み込み中のページ下地は黒のまま。
    """
    loaded_view.set_canvas_theme(CanvasTheme.WHITE)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)

    assert canvas_color(loaded_view) == QColor(Qt.GlobalColor.white)
    assert page_center_color(loaded_view, 0) == QColor(Qt.GlobalColor.black)


def test_a_canvas_change_repaints_the_viewport(loaded_view: PdfView) -> None:
    """キャンバスを変えたらビューポートを描き直す。"""
    spy = UpdateSpy(loaded_view)

    loaded_view.set_canvas_theme(CanvasTheme.BLACK)

    assert spy.rects == [None]


def test_setting_the_same_canvas_theme_does_nothing(loaded_view: PdfView) -> None:
    """同じ色を選び直しても、再描画も通知も起こさない。"""
    loaded_view.set_canvas_theme(CanvasTheme.BLACK)
    emitted: list[None] = []
    loaded_view.canvas_theme_changed.connect(lambda: emitted.append(None))
    spy = UpdateSpy(loaded_view)

    loaded_view.set_canvas_theme(CanvasTheme.BLACK)

    assert spy.rects == []
    assert emitted == []


def test_a_canvas_change_emits_the_signal(loaded_view: PdfView) -> None:
    """色が変わったら通知する。"""
    emitted: list[None] = []
    loaded_view.canvas_theme_changed.connect(lambda: emitted.append(None))

    loaded_view.set_canvas_theme(CanvasTheme.WHITE)

    assert emitted == [None]


def test_a_canvas_change_does_not_rerender(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """キャンバスを変えてもレンダリングもキャッシュも動かさない。

    P2-2 の最重要回帰条件。ページの色を変えていないのだから、表示用画像を
    作り直す理由も無い。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    service.repeat_last_request()

    count = len(service.requests)
    generation = service.generation
    outstanding = service.outstanding_keys
    raw_size = len(cache)
    display_size = len(service.display_cache)

    for theme in (CanvasTheme.BLACK, CanvasTheme.WHITE, CanvasTheme.DARK_GRAY):
        loaded_view.set_canvas_theme(theme)

    assert len(service.requests) == count
    assert service.generation == generation
    assert service.outstanding_keys == outstanding
    assert len(cache) == raw_size
    assert len(service.display_cache) == display_size


def test_a_canvas_change_does_not_transform(
    loaded_view: PdfView, cache: RenderCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """キャンバスを変えても色変換は走らない。描画もこれまでどおり転送だけ。"""
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    calls = count_transforms(monkeypatch)

    loaded_view.set_canvas_theme(CanvasTheme.BLACK)
    render_view(loaded_view)

    assert calls == []


def test_a_canvas_change_keeps_the_zoom_and_the_position(loaded_view: PdfView) -> None:
    """キャンバスを変えても倍率・倍率モード・現在ページ・スクロール位置は動かない。"""
    loaded_view.fit_width()
    loaded_view.verticalScrollBar().setValue(500)
    zoom = loaded_view.zoom
    position = loaded_view.verticalScrollBar().value()
    page = loaded_view.current_page

    loaded_view.set_canvas_theme(CanvasTheme.BLACK)

    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.zoom_mode is ZoomMode.FIT_WIDTH
    assert loaded_view.verticalScrollBar().value() == position
    assert loaded_view.current_page == page


def test_the_canvas_theme_survives_a_document_switch(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """ドキュメントを替えてもキャンバスの色は維持される。"""
    loaded_view.set_canvas_theme(CanvasTheme.BLACK)

    loaded_view.set_document(controller.document, controller.page_sizes())

    assert loaded_view.canvas_theme is CanvasTheme.BLACK
    assert canvas_color(loaded_view) == QColor(Qt.GlobalColor.black)


def test_the_canvas_theme_can_be_set_without_a_document(view: PdfView) -> None:
    """ドキュメントが無くても選べる。"""
    view.set_canvas_theme(CanvasTheme.WHITE)

    assert view.canvas_theme is CanvasTheme.WHITE
    assert canvas_color(view) == QColor(Qt.GlobalColor.white)


# ------------------------------------------------------------------ ページ移動
def test_go_to_page_brings_the_page_to_the_top(loaded_view: PdfView) -> None:
    """指定ページの上端付近がビューポート上端に来る。"""
    loaded_view.go_to_page(1)

    rect = loaded_view.page_viewport_rect(1)
    assert rect is not None
    assert rect.top() == pytest.approx(MARGIN, abs=1)
    assert loaded_view.current_page == 1


def test_go_to_page_updates_current_page_and_requests(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """移動後に現在ページ・通知・レンダリング要求が追随する。"""
    emitted: list[int] = []
    loaded_view.current_page_changed.connect(emitted.append)

    loaded_view.go_to_page(2)

    assert loaded_view.current_page == 2
    assert emitted[-1] == 2
    assert service.last_pages[0] == 2


def test_go_to_page_clamps_out_of_range(loaded_view: PdfView) -> None:
    """範囲外は先頭/末尾へ丸める。"""
    loaded_view.go_to_page(99)
    assert loaded_view.current_page == 2

    loaded_view.go_to_page(-5)
    assert loaded_view.current_page == 0


def test_go_to_page_without_a_document_is_harmless(view: PdfView) -> None:
    """ドキュメントが無ければ何もしない。"""
    view.go_to_page(3)

    assert view.current_page == NO_PAGE


# ------------------------------------------------------------------ 読書位置
def test_the_reading_position_starts_at_the_top(loaded_view: PdfView) -> None:
    """開いた直後は1ページ目の先頭。"""
    position = loaded_view.current_reading_position()

    assert position is not None
    assert position.page_index == 0
    assert position.y_norm == pytest.approx(0.0, abs=0.05)


def test_the_reading_position_follows_the_scroll(loaded_view: PdfView) -> None:
    """ページの途中までスクロールすると、その割合が縦位置になる。"""
    rect = loaded_view.page_viewport_rect(1)
    assert rect is not None
    bar = loaded_view.verticalScrollBar()
    # 2ページ目の 40% がビューポート上端に来るところまで送る。
    bar.setValue(round(bar.value() + rect.top() + rect.height() * 0.4))

    position = loaded_view.current_reading_position()

    assert position is not None
    assert position.page_index == 1
    assert position.y_norm == pytest.approx(0.4, abs=0.01)


def test_the_reading_position_is_none_without_a_document(view: PdfView) -> None:
    """ドキュメントが無ければ読書位置も無い。"""
    assert view.current_reading_position() is None


def test_the_reading_position_round_trips(loaded_view: PdfView) -> None:
    """保存した位置へ戻すと、同じ読書位置になる。"""
    bar = loaded_view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    saved = loaded_view.current_reading_position()
    assert saved is not None

    loaded_view.go_to_page(0)
    assert loaded_view.restore_reading_position(saved)

    restored = loaded_view.current_reading_position()
    assert restored is not None
    assert restored.page_index == saved.page_index
    assert restored.y_norm == pytest.approx(saved.y_norm, abs=0.01)


def test_restoring_survives_a_viewport_resize(loaded_view: PdfView, qtbot: QtBot) -> None:
    """保存したときと違う大きさのウィンドウでも、同じページの同じ割合へ戻る。

    正規化座標なので、固定のピクセル値では比べない。
    """
    bar = loaded_view.verticalScrollBar()
    bar.setValue(bar.maximum() // 3)
    saved = loaded_view.current_reading_position()
    assert saved is not None

    loaded_view.resize(560, 420)
    qtbot.waitUntil(lambda: loaded_view.viewport().width() > 0)
    loaded_view.go_to_page(0)

    assert loaded_view.restore_reading_position(saved)
    restored = loaded_view.current_reading_position()
    assert restored is not None
    assert restored.page_index == saved.page_index
    assert restored.y_norm == pytest.approx(saved.y_norm, abs=0.02)


def test_restoring_does_not_touch_the_zoom(loaded_view: PdfView) -> None:
    """読書位置を戻しても倍率も倍率モードも動かない（Fit のまま）。"""
    loaded_view.fit_width()
    zoom, mode = loaded_view.zoom, loaded_view.zoom_mode

    assert loaded_view.restore_reading_position(ReadingPosition(page_index=1, y_norm=0.5))

    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.zoom_mode is mode


def test_restoring_a_missing_page_does_nothing(loaded_view: PdfView) -> None:
    """いまの PDF に無いページは、最終ページへ丸めずに何もしない。"""
    before = loaded_view.verticalScrollBar().value()

    assert not loaded_view.restore_reading_position(ReadingPosition(page_index=9, y_norm=0.5))

    assert loaded_view.verticalScrollBar().value() == before
    assert loaded_view.current_page == 0


def test_restoring_without_a_document_does_nothing(view: PdfView) -> None:
    """ドキュメントが無ければ戻せない。"""
    assert not view.restore_reading_position(ReadingPosition(page_index=0, y_norm=0.5))


@pytest.mark.parametrize(
    "position",
    [
        ReadingPosition(page_index=-1, y_norm=0.5),
        ReadingPosition(page_index=0, y_norm=-0.1),
        ReadingPosition(page_index=0, y_norm=1.5),
        ReadingPosition(page_index=0, y_norm=float("nan")),
    ],
)
def test_restoring_an_invalid_position_is_refused(
    loaded_view: PdfView, position: ReadingPosition
) -> None:
    """契約外の値は黙って丸めずに失敗させる（呼び出し側の誤り）。

    設定から読んだ値は `Settings` の側で既定へ落とすので、ここまで
    壊れた値が来るのは実装の誤りだけ。
    """
    with pytest.raises((ValueError, TypeError)):
        loaded_view.restore_reading_position(position)


def test_restoring_does_not_discard_the_rendering_state(
    loaded_view: PdfView, service: RecordingService
) -> None:
    """読書位置を戻しても世代は進まない（キャッシュを捨てない）。"""
    generation = service.generation

    loaded_view.restore_reading_position(ReadingPosition(page_index=2, y_norm=0.3))

    assert service.generation == generation
    assert service.last_pages[0] == 2
