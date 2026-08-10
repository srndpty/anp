"""`anp.ui.pdf_view` のテスト。

タイミングに依存させないため、実際のレンダリングは待たない。
`PageRenderService` に要求が渡るところまでを観測し、画像が必要な場合は
キャッシュへ直接入れてから描画結果を調べる。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, QSizeF, Qt
from PySide6.QtGui import QColor, QImage
from pytestqt.qtbot import QtBot

from anp.pdf.cache import RenderCache, RenderKey
from anp.pdf.document import DocumentController
from anp.pdf.layout import PageLayout
from anp.pdf.render import PageRenderService, PageRequest
from anp.ui.pdf_view import MAX_ZOOM, MIN_ZOOM, NO_PAGE, PdfView

VIEWPORT = (400, 600)


class RecordingService(PageRenderService):
    """要求されたページを記録するレンダリングサービス。

    実際の `requestPage()` は出さない。本物のレンダリングが途中で完了すると
    キャッシュの中身がテストの仕込みと入れ替わり、結果がタイミング依存に
    なるため。要求の中身とキャッシュの読み出しはそのまま検証できる。
    """

    def __init__(self, cache: RenderCache) -> None:
        super().__init__(cache)
        self.requests: list[list[PageRequest]] = []

    def request_pages(self, requests: Sequence[PageRequest]) -> None:
        self.requests.append(list(requests))
        super().request_pages(requests)

    def flush(self) -> None:
        pass

    @property
    def last_pages(self) -> list[int]:
        """直近の要求のページ番号（優先度順）。"""
        return [request.page_index for request in self.requests[-1]]


# ------------------------------------------------------------------ フィクスチャ
@pytest.fixture
def controller(sample_pdf: Path) -> Iterator[DocumentController]:
    """開いた状態の3ページ PDF（A4 / 595x842pt）。"""
    controller = DocumentController()
    controller.open(sample_pdf)
    yield controller
    controller.close()


@pytest.fixture
def cache() -> RenderCache:
    """ビューが参照するキャッシュ。テストから画像を仕込むために取っておく。"""
    return RenderCache()


@pytest.fixture
def service(cache: RenderCache) -> RecordingService:
    """要求を記録するレンダリングサービス。"""
    return RecordingService(cache)


@pytest.fixture
def view(qtbot: QtBot, service: RecordingService) -> PdfView:
    """ドキュメント未設定のビュー。

    ビューポートの大きさは表示されるまで確定しないので、表示してから返す。
    """
    view = PdfView(service)
    qtbot.addWidget(view)
    view.resize(*VIEWPORT)
    with qtbot.waitExposed(view):
        view.show()
    return view


@pytest.fixture
def loaded_view(view: PdfView, controller: DocumentController) -> PdfView:
    """3ページ PDF を設定済みのビュー。"""
    view.set_document(controller.document, controller.page_sizes())
    return view


# ------------------------------------------------------------------ 補助
def layout_of(controller: DocumentController) -> PageLayout:
    """ビューが内部で作るのと同じレイアウト。"""
    return PageLayout(controller.page_sizes())


def put_image(
    cache: RenderCache, view: PdfView, page: int, color: Qt.GlobalColor, *, scale: float = 1.0
) -> None:
    """指定ページの画像をキャッシュに仕込む。

    `scale` を 1.0 以外にすると、表示に必要な解像度とは違う画像になり、
    仮表示（placeholder）としてだけ使える。
    """
    rect = view.page_viewport_rect(page)
    assert rect is not None
    dpr = view.devicePixelRatioF()
    width = max(round(rect.width() * dpr * scale), 1)
    height = max(round(rect.height() * dpr * scale), 1)
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    cache.put(RenderKey(page, width, height, dpr), image)


def render_view(view: PdfView) -> QImage:
    """ビューポートを描画した結果を取り出す。"""
    image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.black)
    view.viewport().render(image)
    return image


def page_center_color(view: PdfView, page: int) -> QColor:
    """ページ中央に描かれた色。"""
    rect = view.page_viewport_rect(page)
    assert rect is not None
    center = rect.intersected(view.viewport().rect().toRectF()).center()
    return QColor(render_view(view).pixelColor(round(center.x()), round(center.y())))


class UpdateSpy:
    """`viewport().update()` の呼び出しを記録する。"""

    def __init__(self, view: PdfView) -> None:
        self.rects: list[QRect | None] = []
        self._original = view.viewport().update
        view.viewport().update = self  # type: ignore[assignment]

    def __call__(self, rect: QRect | None = None) -> None:
        self.rects.append(rect)
        if rect is None:
            self._original()
        else:
            self._original(rect)


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
    assert spy.rects == [expected.toAlignedRect().intersected(loaded_view.viewport().rect())]


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
    view.set_document(controller.document, [QSizeF(100.0, 200.0), QSizeF(100.0, 200.0)])

    assert view.page_count == 2
    rect = view.page_viewport_rect(0)
    assert rect is not None
    assert rect.width() == pytest.approx(100.0)
