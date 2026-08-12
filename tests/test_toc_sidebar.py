"""PDF の目次サイドバー（P5-2）のテスト。

確かめるのは5つ。

- `QPdfBookmarkModel` が **実際の outline つき PDF** から階層を読めること
  （mock だけで済ませない。ロールの実際の意味もここで固定する）
- 表示中の PDF の目次だけが出ること（A → B → C、開くのに失敗した場合、
  学習マークを読めなかった場合）
- 項目を選ぶと PDF の移動先まで移動すること（ページだけでなく
  ページ内の位置も）
- 壊れた移動先でも落ちず、スクロールも倍率も動かないこと
- 移動が倍率・レンダリング・学習マーク・履歴・設定に触らないこと

目次つき PDF はその場で組み立てる（`conftest.write_outline_pdf()`）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, QPointF, QSettings, QSizeF, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtPdf import QPdfBookmarkModel, QPdfDocument
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.pdf.cache import RenderCache
from anp.pdf.color import PageColorMode
from anp.pdf.destination import PdfDestination, clamp_to_page
from anp.pdf.document import DocumentController
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.main_window import MainWindow
from anp.ui.pdf_view import PdfView, ZoomMode
from anp.ui.toc_sidebar import (
    DOCK_OBJECT_NAME,
    NO_DOCUMENT_TEXT,
    NO_OUTLINE_TEXT,
    TocSidebar,
    destination_for,
)
from conftest import SECTION_1_1_TOP_ORIGIN_Y
from helpers import BrokenRepository, RecordingService, put_image

# 目次のドックは既定で非表示なので、ウィジェットの見え方は
# `isVisible()`（祖先の状態を含む）ではなく `isHidden()` で見る。


@pytest.fixture
def sidebar(qtbot: QtBot) -> TocSidebar:
    """ドキュメント未設定のサイドバー。"""
    sidebar = TocSidebar()
    qtbot.addWidget(sidebar)
    return sidebar


@pytest.fixture
def outline_document(outline_pdf: Path) -> Iterator[QPdfDocument]:
    """入れ子の目次を持つ PDF を開いたドキュメント。"""
    document = QPdfDocument()
    assert document.load(str(outline_pdf)) == QPdfDocument.Error.None_
    yield document
    document.close()


def titles(model: QPdfBookmarkModel, parent: QModelIndex | None = None) -> list[str]:
    """その階層の項目名（上から順）。`parent` を省略すると最上位。"""
    parent = QModelIndex() if parent is None else parent
    return [
        model.index(row, 0, parent).data(QPdfBookmarkModel.Role.Title.value)
        for row in range(model.rowCount(parent))
    ]


def row(model: QPdfBookmarkModel, *path: int) -> QModelIndex:
    """行番号をたどって `QModelIndex` を得る（`row(model, 0, 1)` は最初の章の2番目の節）。"""
    index = QModelIndex()
    for step in path:
        index = model.index(step, 0, index)
    return index


def destination_point(view: PdfView, destination: PdfDestination) -> QPointF:
    """移動先に対応するビューポート上の点。

    ビューの変換とは別に組み立てる（`PageLayout` の変換をそのまま呼んで
    比べると、移動の計算が間違っていても一致してしまう）。
    """
    rect = view.page_viewport_rect(destination.page_index)
    assert rect is not None
    return QPointF(
        rect.left() + destination.location.x() * view.zoom,
        rect.top() + destination.location.y() * view.zoom,
    )


# ================================================================ モデルと階層
def test_the_bookmark_model_reads_the_real_outline(
    sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """実際の PDF の outline から、題名・ページ・階層が読める。

    `QPdfBookmarkModel` のロールが実際に何を返すかを固定するテスト。
    ページ番号は **0 始まり**（1 始まりと取り違えると 1 ページずれる）。
    """
    sidebar.set_document(outline_document)
    model = sidebar.model

    assert titles(model) == ["Chapter 1", "Chapter 2"]
    assert titles(model, row(model, 0)) == ["Section 1.1", "Section 1.2"]

    pages = QPdfBookmarkModel.Role.Page.value
    assert row(model, 0).data(pages) == 0
    assert row(model, 0, 0).data(pages) == 1
    assert row(model, 0, 1).data(pages) == 2
    assert row(model, 1).data(pages) == 3

    levels = QPdfBookmarkModel.Role.Level.value
    assert row(model, 0).data(levels) == 0
    assert row(model, 0, 0).data(levels) == 1


def test_the_hierarchy_is_not_flattened(
    sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """節は章の子のまま。平坦な一覧に変換しない。"""
    sidebar.set_document(outline_document)
    model = sidebar.model

    assert model.rowCount(QModelIndex()) == 2
    chapter = row(model, 0)
    assert model.rowCount(chapter) == 2
    assert model.hasChildren(chapter)
    assert model.parent(row(model, 0, 0)) == chapter
    # 「Chapter 2」は子を持たないので、平坦化されていれば節がここに現れる。
    assert model.rowCount(row(model, 1)) == 0


def test_the_tree_shows_the_bookmark_model_itself(
    sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """`QTreeView` はモデルを直接見る。Python 側のツリーへ写し取らない。"""
    sidebar.set_document(outline_document)

    assert sidebar.tree.model() is sidebar.model
    assert sidebar.tree.isHeaderHidden()
    assert not sidebar.tree.isHidden()
    # 初期表示で全展開しない（数千項目の PDF で開いた瞬間に埋まらないため）。
    assert not sidebar.tree.isExpanded(row(sidebar.model, 0))


# ================================================================ 空の表示
def test_without_a_document_the_sidebar_says_so(sidebar: TocSidebar) -> None:
    """PDF を開いていないときは、その旨だけを出す。"""
    assert not sidebar.has_outline
    assert sidebar.empty_text == NO_DOCUMENT_TEXT
    assert sidebar.tree.isHidden()


def test_a_pdf_without_an_outline_is_not_an_error(sidebar: TocSidebar, sample_pdf: Path) -> None:
    """outline を持たない普通の PDF は「目次はありません」。

    本文に目次のページが印刷されていても、outline が無ければ目次なし
    （OCR も本文解析もしない）。エラーではないのでダイアログも出さない。
    """
    document = QPdfDocument()
    assert document.load(str(sample_pdf)) == QPdfDocument.Error.None_
    try:
        sidebar.set_document(document)

        assert not sidebar.has_outline
        assert sidebar.empty_text == NO_OUTLINE_TEXT
        assert sidebar.tree.isHidden()
    finally:
        document.close()


def test_the_outline_follows_the_document_switch(
    sidebar: TocSidebar, outline_pdf: Path, sample_pdf: Path, other_outline_pdf: Path
) -> None:
    """目次あり A → 目次なし B → 目次あり C で、古い行が残らない。"""
    controller = DocumentController()
    try:
        controller.open(outline_pdf)
        sidebar.set_document(controller.document)
        assert titles(sidebar.model) == ["Chapter 1", "Chapter 2"]

        controller.open(sample_pdf)
        sidebar.set_document(controller.document)
        assert titles(sidebar.model) == []
        assert sidebar.empty_text == NO_OUTLINE_TEXT

        controller.open(other_outline_pdf)
        sidebar.set_document(controller.document)
        assert titles(sidebar.model) == ["Section X"]
    finally:
        controller.close()


def test_clearing_the_document_detaches_the_model(
    sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """ドキュメントを手放すと、モデルは何も指さない。

    閉じられる `QPdfDocument` をモデルが指し続けないようにするため。
    """
    sidebar.set_document(outline_document)

    sidebar.clear_document()

    assert sidebar.model.document() is None
    assert not sidebar.has_outline
    assert sidebar.empty_text == NO_DOCUMENT_TEXT


def test_the_selection_is_dropped_when_the_document_changes(
    sidebar: TocSidebar, outline_pdf: Path, other_outline_pdf: Path
) -> None:
    """A で選んだ行を B へ持ち越さない。"""
    controller = DocumentController()
    try:
        controller.open(outline_pdf)
        sidebar.set_document(controller.document)
        sidebar.tree.setCurrentIndex(row(sidebar.model, 1))
        assert sidebar.tree.currentIndex().isValid()

        controller.open(other_outline_pdf)
        sidebar.set_document(controller.document)

        assert not sidebar.tree.currentIndex().isValid()
        assert sidebar.tree.selectionModel().selectedIndexes() == []
    finally:
        controller.close()


def test_setting_a_document_always_drops_the_selection(
    sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """選択と展開を捨てるのは `set_document()` 自身の契約。

    モデルが階層を作り直せば Qt が選択を無効にするが、それに頼らない
    （同じ `QPdfDocument` を渡し直したときも古い選択を持ち越さない）。
    """
    sidebar.set_document(outline_document)
    chapter = row(sidebar.model, 0)
    sidebar.tree.setCurrentIndex(chapter)
    sidebar.tree.expand(chapter)

    sidebar.set_document(outline_document)

    assert not sidebar.tree.currentIndex().isValid()
    assert not sidebar.tree.isExpanded(row(sidebar.model, 0))


# ================================================================ 移動先の取り出し
def test_the_destination_carries_the_page_and_the_location(
    sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """移動先は 0 始まりのページと、ページ左上原点の PDF ポイント。"""
    sidebar.set_document(outline_document)

    destination = destination_for(row(sidebar.model, 0, 0))

    assert destination is not None
    assert destination.page_index == 1
    assert destination.location.y() == pytest.approx(SECTION_1_1_TOP_ORIGIN_Y)
    assert destination.zoom_hint is None


def test_the_zoom_hint_is_read_but_kept_separate(
    sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """PDF が指定した倍率は値としては読める（適用しないのはビューの契約）。"""
    sidebar.set_document(outline_document)

    destination = destination_for(row(sidebar.model, 0, 1))

    assert destination is not None
    assert destination.page_index == 2
    assert destination.zoom_hint == pytest.approx(2.5)


def test_an_invalid_index_has_no_destination() -> None:
    """無効な `QModelIndex` からは移動先を作らない。"""
    assert destination_for(QModelIndex()) is None


def test_a_row_without_a_page_has_no_destination() -> None:
    """ページ番号が整数でない行は移動先にしない（PDF の metadata は壊れうる）。"""
    model = QStandardItemModel()
    item = QStandardItem("壊れた項目")
    item.setData("2", QPdfBookmarkModel.Role.Page.value)
    model.appendRow(item)

    assert destination_for(model.index(0, 0)) is None


def test_a_row_with_broken_extras_falls_back() -> None:
    """位置と倍率が読めなくても、ページが読めれば移動先になる。"""
    model = QStandardItemModel()
    item = QStandardItem("位置の壊れた項目")
    item.setData(1, QPdfBookmarkModel.Role.Page.value)
    item.setData("なにか", QPdfBookmarkModel.Role.Location.value)
    item.setData("なにか", QPdfBookmarkModel.Role.Zoom.value)
    model.appendRow(item)

    destination = destination_for(model.index(0, 0))

    assert destination == PdfDestination(page_index=1, location=QPointF(0.0, 0.0))


def test_clicking_a_row_requests_its_destination(
    qtbot: QtBot, sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """行のクリックは移動の要求だけ。"""
    sidebar.set_document(outline_document)
    index = row(sidebar.model, 0, 1)

    with qtbot.waitSignal(sidebar.destination_requested) as signal:
        sidebar.tree.setCurrentIndex(index)
        sidebar.tree.clicked.emit(index)

    destination = signal.args[0]
    assert isinstance(destination, PdfDestination)
    assert destination.page_index == 2


# ================================================================ 座標の丸め
@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (QPointF(10.0, 20.0), QPointF(10.0, 20.0)),
        (QPointF(float("nan"), 20.0), QPointF(0.0, 20.0)),
        (QPointF(10.0, float("inf")), QPointF(10.0, 0.0)),
        (QPointF(10.0, float("-inf")), QPointF(10.0, 0.0)),
        (QPointF(-5.0, -5.0), QPointF(0.0, 0.0)),
        (QPointF(1e12, 1e12), QPointF(595.0, 842.0)),
        # 浮動小数の誤差でわずかにページの外へ出た座標は縁へ丸める。
        (QPointF(595.0000001, 842.0000001), QPointF(595.0, 842.0)),
    ],
)
def test_the_destination_point_is_kept_inside_the_page(point: QPointF, expected: QPointF) -> None:
    """外部の metadata 由来なので、読めない座標は例外にせず安全な値へ落とす。"""
    assert clamp_to_page(point, QSizeF(595.0, 842.0)) == expected


# ================================================================ ビューの移動
def _destination(
    page: int, x: float = 0.0, y: float = 0.0, zoom_hint: float | None = None
) -> PdfDestination:
    """テストが指定した移動先。ページ内の位置は PDF ポイント（左上原点）。"""
    return PdfDestination(page_index=page, location=QPointF(x, y), zoom_hint=zoom_hint)


def test_navigating_to_a_page_makes_it_visible(loaded_view: PdfView) -> None:
    """ページを指す移動先で、そのページが見えるところまで動く。"""
    assert loaded_view.navigate_to_pdf_destination(_destination(2)) is True

    assert 2 in loaded_view.visible_pages()
    assert loaded_view.current_page == 2


def test_the_top_of_a_page_matches_go_to_page(loaded_view: PdfView) -> None:
    """位置が 0 の移動先は、そのページへのページ移動と同じスクロール量。"""
    loaded_view.go_to_page(1)
    expected = loaded_view.verticalScrollBar().value()

    loaded_view.verticalScrollBar().setValue(0)
    assert loaded_view.navigate_to_pdf_destination(_destination(1)) is True

    assert loaded_view.verticalScrollBar().value() == expected


def test_navigating_uses_the_location_not_just_the_page(loaded_view: PdfView) -> None:
    """ページ内の位置までたどる。ページ先頭までしか動かない実装では落ちる。

    移動先はビューポートの上端付近（余白の分だけ下）へ来る。
    """
    destination = _destination(1, y=400.0)

    assert loaded_view.navigate_to_pdf_destination(destination) is True

    point = destination_point(loaded_view, destination)
    assert point.y() == pytest.approx(16.0, abs=1.5)
    assert loaded_view.viewport().rect().contains(point.toPoint())


def test_navigating_uses_the_horizontal_location(loaded_view: PdfView) -> None:
    """Location.x も反映する（横スクロールできる倍率のとき）。"""
    destination = _destination(1, x=100.0, y=400.0)

    assert loaded_view.navigate_to_pdf_destination(destination) is True

    point = destination_point(loaded_view, destination)
    assert point.x() == pytest.approx(16.0, abs=1.5)
    assert loaded_view.viewport().rect().contains(point.toPoint())


def test_navigating_to_the_last_page_accepts_the_scroll_limit(loaded_view: PdfView) -> None:
    """文書の末端では可動域による丸めを受け入れる。移動先は見えていればよい。"""
    destination = _destination(2, y=800.0)

    assert loaded_view.navigate_to_pdf_destination(destination) is True

    point = destination_point(loaded_view, destination)
    assert loaded_view.viewport().rect().contains(point.toPoint())


@pytest.mark.parametrize("zoom", [0.25, 1.0, 4.0])
def test_navigating_keeps_the_free_zoom(loaded_view: PdfView, zoom: float) -> None:
    """FREE の倍率も倍率モードも変わらない。"""
    loaded_view.set_zoom(zoom)
    destination = _destination(2, y=300.0)

    assert loaded_view.navigate_to_pdf_destination(destination) is True

    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.zoom_mode is ZoomMode.FREE
    point = destination_point(loaded_view, destination)
    assert loaded_view.viewport().rect().contains(point.toPoint())


@pytest.mark.parametrize("mode", [ZoomMode.FIT_WIDTH, ZoomMode.FIT_PAGE])
def test_navigating_keeps_the_fit_mode(loaded_view: PdfView, mode: ZoomMode) -> None:
    """Fit Width / Fit Page のまま飛んでも FREE へ落ちない。

    Fit Page では複数ページが収まることがあるので、固定するのは
    「移動先が見えていること」で、`current_page` は特別扱いしない。
    """
    if mode is ZoomMode.FIT_WIDTH:
        loaded_view.fit_width()
    else:
        loaded_view.fit_page()
    zoom = loaded_view.zoom
    destination = _destination(2, y=300.0)

    assert loaded_view.navigate_to_pdf_destination(destination) is True

    assert loaded_view.zoom_mode is mode
    assert loaded_view.zoom == pytest.approx(zoom)
    point = destination_point(loaded_view, destination)
    assert loaded_view.viewport().rect().contains(point.toPoint())


def test_the_zoom_hint_does_not_change_the_user_zoom(loaded_view: PdfView) -> None:
    """PDF が指定した倍率で利用者の倍率を上書きしない。"""
    loaded_view.fit_width()
    mode, zoom = loaded_view.zoom_mode, loaded_view.zoom

    assert loaded_view.navigate_to_pdf_destination(_destination(1, y=100.0, zoom_hint=4.0)) is True

    assert loaded_view.zoom_mode is mode
    assert loaded_view.zoom == pytest.approx(zoom)


@pytest.mark.parametrize("page", [-1, 3, 99])
def test_an_invalid_page_does_nothing(loaded_view: PdfView, page: int) -> None:
    """壊れた outline のページ番号では、丸めもせず何も動かさない。"""
    loaded_view.go_to_page(1)
    scroll = loaded_view.verticalScrollBar().value()
    zoom = loaded_view.zoom

    assert loaded_view.navigate_to_pdf_destination(_destination(page)) is False

    assert loaded_view.verticalScrollBar().value() == scroll
    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.current_page == 1


@pytest.mark.parametrize("y", [float("nan"), float("inf"), float("-inf"), 1e12, -1e12])
def test_an_invalid_location_falls_back_to_the_page_top(loaded_view: PdfView, y: float) -> None:
    """読めない位置ではそのページの先頭へ。アプリは落とさない。"""
    loaded_view.go_to_page(0)

    assert loaded_view.navigate_to_pdf_destination(_destination(1, y=y)) is True

    # 上端側は「ページ先頭」、下端側はページ末尾（可動域で丸まる）へ収まる。
    assert 1 in loaded_view.visible_pages()


def test_a_huge_location_stays_inside_the_document(loaded_view: PdfView) -> None:
    """巨大な値をスクロールバーへそのまま流さない。"""
    bar = loaded_view.verticalScrollBar()

    assert loaded_view.navigate_to_pdf_destination(_destination(1, y=1e12)) is True

    assert bar.value() <= bar.maximum()


def test_navigating_without_a_document_does_nothing(view: PdfView) -> None:
    """PDF を開いていなければ移動しない。"""
    assert view.navigate_to_pdf_destination(_destination(0)) is False


def test_navigating_does_not_touch_the_rendering_pipeline(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """移動のためにキャッシュや色変換を作り直さない（P4 と同じ観点）。

    スクロールに伴う通常のレンダリング要求は起きてよい。壊してはいけない
    のは **既に持っている画像** の方。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    service.repeat_last_request()

    raw_key = cache.nearest_key(0, width_px=0)
    display_key = service.display_cache.nearest_key(0, 0, PageColorMode.INVERT)
    assert raw_key is not None
    assert display_key is not None

    generation = service.generation

    loaded_view.navigate_to_pdf_destination(_destination(2, y=300.0))

    assert service.generation == generation
    assert raw_key in cache
    assert display_key in service.display_cache
    assert loaded_view.page_color_mode is PageColorMode.INVERT


# ================================================================ ウィンドウ統合
@pytest.fixture
def ini(tmp_path: Path) -> str:
    """2つのウィンドウが共有する設定ファイル。"""
    return str(tmp_path / "settings.ini")


def make_window(qtbot: QtBot, ini: str, repository: StudyMarkRepository) -> MainWindow:
    """設定を読み込んで表示済みのウィンドウを作る（P5-1 のテストと同じ形）。"""
    window = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), repository)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    return window


@pytest.fixture
def window(qtbot: QtBot, ini: str, study_marks: StudyMarkRepository) -> Iterator[MainWindow]:
    """表示済みのメインウィンドウ。"""
    window = make_window(qtbot, ini, study_marks)
    yield window
    window.close()


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """警告ダイアログを出さずに本文を集める。"""
    messages: list[str] = []
    monkeypatch.setattr(
        "anp.ui.main_window.QMessageBox.warning",
        lambda *args: messages.append(args[2]),
    )
    return messages


def test_the_dock_is_hidden_on_the_left_with_a_stable_object_name(window: MainWindow) -> None:
    """初回は非表示で、左のドック。ウィンドウ状態の保存が識別できる名前を持つ。"""
    dock = window.toc_sidebar

    assert dock.objectName() == DOCK_OBJECT_NAME
    assert dock.windowTitle() == "目次"
    assert dock.isHidden()
    assert window.dockWidgetArea(dock) is Qt.DockWidgetArea.LeftDockWidgetArea
    # 学習マークは右のまま（読む領域を左右から挟む形）。
    marks = window.study_mark_sidebar
    assert window.dockWidgetArea(marks) is Qt.DockWidgetArea.RightDockWidgetArea


def test_the_view_menu_can_toggle_the_dock(window: MainWindow) -> None:
    """「表示」から開閉できる。独自の checked 状態は持たない。"""
    action = window.toc_sidebar.toggleViewAction()
    menus = [menu for menu in window.findChildren(QMenu) if menu.title() == "表示(&V)"]
    assert len(menus) == 1
    assert action in menus[0].actions()

    action.trigger()
    assert window.toc_sidebar.isVisible()

    action.trigger()
    assert not window.toc_sidebar.isVisible()


def test_the_dock_visibility_is_restored_from_the_window_state(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository
) -> None:
    """開いたまま閉じたら、次回も開いた状態で始まる（専用の設定キーは作らない）。"""
    first = make_window(qtbot, ini, study_marks)
    first.toc_sidebar.show()
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        assert not second.toc_sidebar.isHidden()
    finally:
        second.close()


def test_opening_a_pdf_fills_the_toc(window: MainWindow, outline_pdf: Path) -> None:
    """PDF を開くと、その PDF の目次が載る。"""
    window.open_path(outline_pdf)

    sidebar = window.toc_sidebar
    assert sidebar.model.document() is not None
    assert titles(sidebar.model) == ["Chapter 1", "Chapter 2"]
    assert titles(sidebar.model, row(sidebar.model, 0)) == ["Section 1.1", "Section 1.2"]


def test_switching_pdfs_swaps_the_toc(
    window: MainWindow, outline_pdf: Path, sample_pdf: Path, other_outline_pdf: Path
) -> None:
    """目次あり A → 目次なし B → 目次あり C。B に A の目次を出さない。"""
    window.open_path(outline_pdf)
    window.open_path(sample_pdf)

    assert not window.toc_sidebar.has_outline
    assert window.toc_sidebar.empty_text == NO_OUTLINE_TEXT

    window.open_path(other_outline_pdf)

    assert titles(window.toc_sidebar.model) == ["Section X"]


def test_a_failed_open_leaves_no_outline(
    window: MainWindow, outline_pdf: Path, broken_pdf: Path, warnings: list[str]
) -> None:
    """開くのに失敗したら、前の PDF の目次も残らない。"""
    window.open_path(outline_pdf)

    window.open_path(broken_pdf)

    assert not window.view.has_document
    assert not window.toc_sidebar.has_outline
    assert window.toc_sidebar.empty_text == NO_DOCUMENT_TEXT
    assert warnings


def test_a_study_mark_failure_leaves_no_outline(
    qtbot: QtBot,
    ini: str,
    study_mark_connection: sqlite3.Connection,
    outline_pdf: Path,
    other_outline_pdf: Path,
    warnings: list[str],
) -> None:
    """PDF は開けても学習マークを読めなければ、目次も出さない（fail-closed）。"""
    repository = BrokenRepository(study_mark_connection)
    window = make_window(qtbot, ini, repository)
    try:
        window.open_path(outline_pdf)
        assert window.toc_sidebar.has_outline

        repository.failing = "list"
        window.open_path(other_outline_pdf)

        assert not window.view.has_document
        assert not window.toc_sidebar.has_outline
        assert warnings
    finally:
        window.close()


def test_clicking_an_item_moves_the_view(window: MainWindow, outline_pdf: Path) -> None:
    """ツリーの選択 → サイドバーのシグナル → ウィンドウ → ビューの経路を通る。

    `QTest.mouseClick` は使わない（ドックは既定で非表示なので行の座標が
    決まらない）。実物の `QTreeView` と実物のモデルの `QModelIndex` で
    `clicked` を出す。
    """
    window.open_path(outline_pdf)
    sidebar = window.toc_sidebar
    index = row(sidebar.model, 0, 1)

    sidebar.tree.setCurrentIndex(index)
    sidebar.tree.clicked.emit(index)

    assert 2 in window.view.visible_pages()


def test_clicking_a_nested_item_reaches_the_location(window: MainWindow, outline_pdf: Path) -> None:
    """節の移動先（ページ途中）まで動く。ページ先頭だけでは落ちる。"""
    window.open_path(outline_pdf)
    sidebar = window.toc_sidebar
    index = row(sidebar.model, 0, 0)
    destination = destination_for(index)
    assert destination is not None

    sidebar.tree.clicked.emit(index)

    point = destination_point(window.view, destination)
    assert window.view.viewport().rect().contains(point.toPoint())
    assert point.y() == pytest.approx(16.0, abs=1.5)


def test_clicking_an_item_does_not_touch_marks_or_history(
    window: MainWindow, study_marks: StudyMarkRepository, outline_pdf: Path
) -> None:
    """目次の移動は学習マークにも履歴にも設定にも触らない。"""
    mark = study_marks.create(outline_pdf, 1, 0.5, 0.5)
    window.open_path(outline_pdf)
    recent = window.recent_files
    marks_filter = window.study_mark_sidebar.mark_filter

    window.toc_sidebar.tree.clicked.emit(row(window.toc_sidebar.model, 1))

    assert window.recent_files == recent
    assert window.view.study_marks == (mark,)
    assert window.study_mark_sidebar.rows == (mark,)
    assert window.study_mark_sidebar.mark_filter == marks_filter
    assert study_marks.list_for_document(outline_pdf) == [mark]


def test_an_out_of_range_destination_is_reported_in_the_status_bar(
    window: MainWindow, outline_pdf: Path
) -> None:
    """現在の PDF に無いページを指す移動先は、ダイアログではなく一時表示で知らせる。"""
    window.open_path(outline_pdf)
    scroll = window.view.verticalScrollBar().value()

    window.toc_sidebar.destination_requested.emit(_destination(99))

    assert window.statusBar().currentMessage() == "この目次の移動先は現在の PDF にありません"
    assert window.view.verticalScrollBar().value() == scroll


def test_the_automatic_restore_shows_the_outline(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, outline_pdf: Path
) -> None:
    """起動時の自動復元で開いた PDF でも目次が載る（復元専用の経路は作らない）。"""
    first = make_window(qtbot, ini, study_marks)
    first.open_path(outline_pdf)
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        assert second.view.has_document
        assert titles(second.toc_sidebar.model) == ["Chapter 1", "Chapter 2"]
    finally:
        second.close()


def test_the_session_keeps_the_position_after_a_toc_jump(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, outline_pdf: Path
) -> None:
    """目次で移動した位置は、P5-1 の通常のセッション保存で次回も戻る。

    目次専用のセッション状態は作らない。
    """
    first = make_window(qtbot, ini, study_marks)
    first.open_path(outline_pdf)
    first.toc_sidebar.tree.clicked.emit(row(first.toc_sidebar.model, 0, 1))
    position = first.view.current_reading_position()
    assert position is not None
    assert position.page_index == 2
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        restored = second.view.current_reading_position()
        assert restored is not None
        assert restored.page_index == position.page_index
        assert restored.y_norm == pytest.approx(position.y_norm, abs=0.01)
    finally:
        second.close()


# ================================================================ キーボード操作
# P5-2 では移動をマウスのクリックだけで実装し、Enter / Return は P5-4 へ
# 送っていた。ここでその積み残しを閉じる。**マウスの挙動は変えない。**
def test_enter_requests_the_current_destination(
    qtbot: QtBot, sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """現在行で Enter を押すと、クリックと同じ移動先を要求する。"""
    sidebar.set_document(outline_document)
    index = row(sidebar.model, 0, 1)
    sidebar.tree.setCurrentIndex(index)

    with qtbot.waitSignal(sidebar.destination_requested) as signal:
        qtbot.keyClick(sidebar.tree, Qt.Key.Key_Return)

    assert signal.args[0] == destination_for(index)


def test_return_and_enter_both_activate(
    qtbot: QtBot, sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """テンキーの Enter でも同じ。"""
    sidebar.set_document(outline_document)
    sidebar.tree.setCurrentIndex(row(sidebar.model, 1))

    with qtbot.waitSignal(sidebar.destination_requested):
        qtbot.keyClick(sidebar.tree, Qt.Key.Key_Enter)


def test_enter_requests_the_destination_only_once(
    qtbot: QtBot, sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """1回の押鍵で移動の要求は1回だけ。

    `clicked` と `activated` を両方つなぐと、環境によっては二重に出る。
    """
    sidebar.set_document(outline_document)
    sidebar.tree.setCurrentIndex(row(sidebar.model, 1))
    requests: list[PdfDestination] = []
    sidebar.destination_requested.connect(requests.append)

    qtbot.keyClick(sidebar.tree, Qt.Key.Key_Return)

    assert len(requests) == 1


def test_enter_without_a_current_row_does_nothing(
    qtbot: QtBot, sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """選択が無ければ何も要求しない（P5-2 の stale の扱いと同じ）。"""
    sidebar.set_document(outline_document)
    sidebar.tree.setCurrentIndex(QModelIndex())
    requests: list[PdfDestination] = []
    sidebar.destination_requested.connect(requests.append)

    qtbot.keyClick(sidebar.tree, Qt.Key.Key_Return)

    assert requests == []


def test_other_keys_still_move_the_current_row(
    qtbot: QtBot, sidebar: TocSidebar, outline_document: QPdfDocument
) -> None:
    """Enter 以外の押鍵はツリーの通常の操作のまま。"""
    sidebar.set_document(outline_document)
    sidebar.tree.setCurrentIndex(row(sidebar.model, 0))

    qtbot.keyClick(sidebar.tree, Qt.Key.Key_Down)

    assert sidebar.tree.currentIndex() != row(sidebar.model, 0)


def test_enter_moves_the_view(qtbot: QtBot, window: MainWindow, outline_pdf: Path) -> None:
    """ツリーの Enter → サイドバー → ウィンドウ → ビューの経路を通る。"""
    window.open_path(outline_pdf)
    sidebar = window.toc_sidebar
    sidebar.tree.setCurrentIndex(row(sidebar.model, 0, 1))

    qtbot.keyClick(sidebar.tree, Qt.Key.Key_Return)

    assert 2 in window.view.visible_pages()


def test_enter_on_a_nested_item_reaches_the_location(
    qtbot: QtBot, window: MainWindow, outline_pdf: Path
) -> None:
    """節の移動先（ページ途中）までキーボードでも動く。"""
    window.open_path(outline_pdf)
    sidebar = window.toc_sidebar
    index = row(sidebar.model, 0, 0)
    destination = destination_for(index)
    assert destination is not None
    sidebar.tree.setCurrentIndex(index)

    qtbot.keyClick(sidebar.tree, Qt.Key.Key_Return)

    point = destination_point(window.view, destination)
    assert window.view.viewport().rect().contains(point.toPoint())
    assert point.y() == pytest.approx(16.0, abs=1.5)


@pytest.mark.parametrize("mode", [ZoomMode.FIT_WIDTH, ZoomMode.FIT_PAGE])
def test_enter_keeps_the_zoom_mode(
    qtbot: QtBot, window: MainWindow, outline_pdf: Path, mode: ZoomMode
) -> None:
    """キーボードでの移動でも倍率モードを維持する（マウスと同じ経路）。"""
    window.open_path(outline_pdf)
    if mode is ZoomMode.FIT_WIDTH:
        window.view.fit_width()
    else:
        window.view.fit_page()
    zoom = window.view.zoom

    sidebar = window.toc_sidebar
    sidebar.tree.setCurrentIndex(row(sidebar.model, 1))
    qtbot.keyClick(sidebar.tree, Qt.Key.Key_Return)

    assert window.view.zoom_mode is mode
    assert window.view.zoom == pytest.approx(zoom)


def test_clicking_still_works_after_the_keyboard_support(
    window: MainWindow, outline_pdf: Path
) -> None:
    """シングルクリックでの移動は従来どおり（キーボード対応で壊さない）。"""
    window.open_path(outline_pdf)
    sidebar = window.toc_sidebar
    index = row(sidebar.model, 0, 1)

    sidebar.tree.clicked.emit(index)

    assert 2 in window.view.visible_pages()
