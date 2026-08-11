"""学習マークのサイドバー・絞り込み・移動（P4-1）のテスト。

確かめるのは4つ。

- `StudyMarkController` が表示中のスナップショットの唯一の公開点であること
- オーバーレイと一覧が同じスナップショットから作られ、食い違わないこと
- 絞り込みが表示だけの状態で、DB にもオーバーレイにも影響しないこと
- 行のクリックがマークの位置への移動だけを起こすこと

移動の検証は固定のピクセル値ではなく、既存のレイアウト由来の座標
（`PdfView.viewport_point_for()`）から期待値を組み立てる。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QComboBox, QMenu, QSpinBox, QTreeWidgetItem
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.pdf.cache import RenderCache
from anp.pdf.color import PageColorMode
from anp.pdf.document import DocumentController
from anp.storage.study_mark import StudyMark
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.main_window import MainWindow
from anp.ui.pdf_view import NO_PAGE, PdfView, ZoomMode
from anp.ui.study_mark_controller import StudyMarkController, StudyMarkLoadError
from anp.ui.study_mark_sidebar import (
    DOCK_OBJECT_NAME,
    FilterMode,
    MarkFilter,
    StudyMarkSidebar,
    note_preview,
)
from helpers import BrokenRepository, RecordingRepository, RecordingService, put_image

# 列の位置。文言ではなく並びを固定する。
PAGE_COLUMN, COUNT_COLUMN, NOTE_COLUMN = 0, 1, 2


# ---------------------------------------------------------------- 道具
def page_point(view: PdfView, page: int, x: float, y: float) -> QPointF:
    """ページ内の比率に対応するビューポート上の点（既存レイアウト由来）。"""
    rect = view.page_viewport_rect(page)
    assert rect is not None
    return QPointF(rect.left() + rect.width() * x, rect.top() + rect.height() * y)


def ctrl_click(qtbot: QtBot, view: PdfView, point: QPointF) -> None:
    """Ctrl + 左クリックを実際の Qt イベントとして送る。"""
    qtbot.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        point.toPoint(),
    )


def click_row(sidebar: StudyMarkSidebar, row: int) -> None:
    """一覧の行を利用者がクリックしたときと同じ経路をたどる。

    `itemClicked` は利用者の操作でしか出ないシグナルなので、行の組み立てと
    移動の要求の対応をそのまま検証できる。
    """
    item = sidebar.tree.topLevelItem(row)
    assert item is not None
    sidebar.tree.itemClicked.emit(item, PAGE_COLUMN)


def row_texts(sidebar: StudyMarkSidebar) -> list[tuple[str, str, str]]:
    """一覧の表示内容（上から順）。"""
    return [
        _item_texts(sidebar.tree.topLevelItem(row))
        for row in range(sidebar.tree.topLevelItemCount())
    ]


def _item_texts(item: QTreeWidgetItem | None) -> tuple[str, str, str]:
    assert item is not None
    return (item.text(PAGE_COLUMN), item.text(COUNT_COLUMN), item.text(NOTE_COLUMN))


def counts(sidebar: StudyMarkSidebar) -> list[int]:
    """一覧に出ている間違い回数（上から順）。"""
    return [mark.mistake_count for mark in sidebar.rows]


def anchor_is_visible(view: PdfView, mark: StudyMark) -> bool:
    """マークのアンカーがビューポートの中に入っているか。"""
    point = view.viewport_point_for(mark.page_index, mark.x_norm, mark.y_norm)
    return point is not None and view.viewport().rect().contains(point.toPoint())


@pytest.fixture
def doc() -> Iterator[DocumentController]:
    controller = DocumentController()
    yield controller
    controller.close()


def show(view: PdfView, doc: DocumentController, path: Path) -> None:
    """`MainWindow.open_path()` と同じ順序でビューへ PDF を載せる。"""
    doc.open(path)
    view.set_document(doc.document, doc.page_sizes())


@pytest.fixture
def study_mark_controller(study_marks: StudyMarkRepository, view: PdfView) -> StudyMarkController:
    return StudyMarkController(study_marks, view)


@pytest.fixture
def sidebar(qtbot: QtBot) -> StudyMarkSidebar:
    """単体で使うサイドバー。ウィンドウには載せない。"""
    sidebar = StudyMarkSidebar()
    qtbot.addWidget(sidebar)
    return sidebar


@pytest.fixture
def window(
    qtbot: QtBot, settings: Settings, study_marks: StudyMarkRepository
) -> Iterator[MainWindow]:
    """表示済みのメインウィンドウ。"""
    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    yield window
    window.close()


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """更新の失敗ダイアログを捕まえる（実際には出さない）。"""
    messages: list[str] = []
    monkeypatch.setattr(
        "anp.ui.study_mark_interaction.QMessageBox.warning",
        lambda *args: messages.append(args[2]),
    )
    return messages


# ================================================================ スナップショット
def test_the_snapshot_starts_empty(study_mark_controller: StudyMarkController) -> None:
    """起動直後のスナップショットは空のタプル。"""
    assert study_mark_controller.study_marks == ()


def test_activating_a_document_publishes_its_marks(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """読み込んだマークがスナップショットになり、ビューと一致する。"""
    mark = study_marks.create(sample_pdf, 0, 0.25, 0.5)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    assert study_mark_controller.study_marks == (mark,)
    assert study_mark_controller.study_marks == view.study_marks


def test_the_snapshot_is_an_immutable_tuple(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """呼び出し側へ書き換えられるコレクションを渡さない。

    リポジトリが返したリストをそのまま公開すると、外から継ぎ当てられて
    しまい「DB が source of truth」が崩れる。
    """
    study_marks.create(sample_pdf, 0, 0.25, 0.5)
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    assert isinstance(study_mark_controller.study_marks, tuple)


def test_refresh_publishes_the_updated_snapshot(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """`refresh()` でスナップショットが入れ替わる。"""
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)
    assert list(study_mark_controller.study_marks) == []

    added = study_marks.create(sample_pdf, 1, 0.5, 0.5)
    study_mark_controller.refresh()

    assert study_mark_controller.study_marks == (added,)


def test_clearing_the_document_publishes_an_empty_snapshot(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """表示対象の解除でスナップショットも空になる。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    study_mark_controller.clear_document()

    assert study_mark_controller.study_marks == ()
    assert view.study_marks == ()


def test_a_mutation_publishes_the_updated_snapshot(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """更新が通ったらスナップショットも読み直した内容になる。"""
    mark = study_marks.create(sample_pdf, 0, 0.5, 0.5)
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    study_mark_controller.increment_mark(mark.id)

    assert study_mark_controller.study_marks[0].mistake_count == 2
    assert study_mark_controller.study_marks == view.study_marks


def test_a_refresh_failure_publishes_an_empty_snapshot(
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """更新は通ったが読み直せなかった場合、スナップショットは空になる。

    古い件数を残すと、画面の数字が保存されている値だと誤解させる。
    """
    repository = BrokenRepository(study_mark_connection)
    mark = repository.create(sample_pdf, 0, 0.5, 0.5)
    controller = StudyMarkController(repository, view)
    show(view, doc, sample_pdf)
    controller.activate_document(sample_pdf)

    repository.failing = "list"
    with pytest.raises(sqlite3.OperationalError):
        controller.increment_mark(mark.id)

    assert controller.study_marks == ()
    assert view.study_marks == ()
    assert controller.active_document_path == sample_pdf


def test_every_publish_notifies_once(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """スナップショットの差し替えは必ず1本の通知として出る。

    通知の中身はビューへ渡したものと同じでなければならない（表示先が
    2つに増えても、内容が分かれない）。
    """
    published: list[tuple[StudyMark, ...]] = []
    study_mark_controller.marks_changed.connect(published.append)

    mark = study_marks.create(sample_pdf, 0, 0.5, 0.5)
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    # 読み取りの前の空と、読み取れた分の2回。
    assert published == [(), (mark,)]
    assert published[-1] == view.study_marks

    study_mark_controller.clear_document()
    assert published[-1] == ()


def test_a_load_failure_publishes_an_empty_snapshot(
    study_mark_connection: sqlite3.Connection,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """読み込みに失敗したら、前の PDF のスナップショットも残らない。"""
    repository = BrokenRepository(study_mark_connection)
    repository.create(sample_pdf, 0, 0.1, 0.1)
    controller = StudyMarkController(repository, view)

    show(view, doc, sample_pdf)
    controller.activate_document(sample_pdf)
    assert len(controller.study_marks) == 1

    repository.failing = "list"
    show(view, doc, two_page_pdf)
    with pytest.raises(StudyMarkLoadError):
        controller.activate_document(two_page_pdf)

    assert list(controller.study_marks) == []
    assert list(view.study_marks) == []


# ================================================================ 絞り込み（純粋）
@pytest.mark.parametrize("count", [1, 2, 3, 4, 10])
def test_exact_matches_only_that_count(count: int) -> None:
    """EXACT(n) は n ちょうどだけ。EXACT(3) を「3回以上」にしない。"""
    mark_filter = MarkFilter(mode=FilterMode.EXACT, exact_count=count)

    for other in (1, 2, 3, 4, 10):
        assert mark_filter.matches(_mark(id=other, mistake_count=other)) is (other == count)


def test_at_least_three_matches_three_and_above() -> None:
    """3+ は 3, 4, 10 を含み、1 と 2 を含まない。"""
    mark_filter = MarkFilter(mode=FilterMode.AT_LEAST_3)

    assert [
        count for count in (1, 2, 3, 4, 10) if mark_filter.matches(_mark(id=1, mistake_count=count))
    ] == [3, 4, 10]


def test_all_matches_everything() -> None:
    """ALL は絞り込まない。"""
    mark_filter = MarkFilter()

    assert all(mark_filter.matches(_mark(id=1, mistake_count=count)) for count in (1, 3, 99))


@pytest.mark.parametrize("count", [0, -1])
def test_an_exact_count_below_one_is_refused(count: int) -> None:
    """保存される回数は 1 以上なので、0 以下の指定は誤り。"""
    with pytest.raises(ValueError, match="exact_count"):
        MarkFilter(mode=FilterMode.EXACT, exact_count=count)


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        (None, ""),
        ("", ""),
        ("abc", "abc"),
        ("1行目\n2行目", "1行目 2行目"),
        ("1行目\r\n2行目", "1行目 2行目"),
        ("√2 ≠ π", "√2 ≠ π"),
    ],
)
def test_the_note_preview_only_flattens_newlines(note: str | None, expected: str) -> None:
    """一覧のメモは改行を空白へ直すだけ。前後の空白も内容も削らない。"""
    assert note_preview(note) == expected


def _mark(
    *,
    id: int,
    mistake_count: int,
    page_index: int = 0,
    note: str | None = None,
    x: float = 0.5,
    y: float = 0.5,
) -> StudyMark:
    """テスト用の学習マーク。DB を使わずに作る。"""
    return StudyMark(
        id=id,
        document_key="key",
        page_index=page_index,
        x_norm=x,
        y_norm=y,
        mistake_count=mistake_count,
        note=note,
    )


# ================================================================ サイドバー
def test_the_list_shows_page_count_and_note(sidebar: StudyMarkSidebar) -> None:
    """ページは 1 始まり、回数はそのままの整数、メモは1行の見出し。"""
    sidebar.set_marks(
        [
            _mark(id=1, page_index=0, mistake_count=1, note="微分の符号"),
            _mark(id=2, page_index=2, mistake_count=4, note="場合分けを\n忘れた"),
            _mark(id=3, page_index=7, mistake_count=12),
        ]
    )

    assert row_texts(sidebar) == [
        ("1", "1", "微分の符号"),
        ("3", "4", "場合分けを 忘れた"),
        ("8", "12", ""),
    ]


def test_the_rows_keep_the_snapshot_order(sidebar: StudyMarkSidebar) -> None:
    """並びはスナップショットのまま（ページ順・作成順）。並べ替えない。"""
    marks = [
        _mark(id=5, page_index=0, mistake_count=9),
        _mark(id=2, page_index=0, mistake_count=1),
        _mark(id=9, page_index=1, mistake_count=3),
    ]

    sidebar.set_marks(marks)

    assert sidebar.rows == tuple(marks)


def test_marks_at_the_same_position_stay_separate_rows(sidebar: StudyMarkSidebar) -> None:
    """同じページの同じ位置のマークもまとめない。"""
    sidebar.set_marks([_mark(id=1, mistake_count=1), _mark(id=2, mistake_count=1)])

    assert len(sidebar.rows) == 2


def test_a_stale_page_still_gets_a_row(sidebar: StudyMarkSidebar) -> None:
    """現在の PDF に無いページのマークも、記録として一覧に残す。"""
    sidebar.set_marks([_mark(id=1, page_index=99, mistake_count=2)])

    assert row_texts(sidebar) == [("100", "2", "")]


@pytest.mark.parametrize("note", [None, "", "abc", "1行目\n2行目", "√2 ≠ π"])
def test_any_note_renders_without_changing_the_domain_value(
    sidebar: StudyMarkSidebar, note: str | None
) -> None:
    """どんなメモでも一覧が壊れず、保存された値も変わらない。"""
    mark = _mark(id=1, mistake_count=1, note=note)

    sidebar.set_marks([mark])

    assert len(sidebar.rows) == 1
    assert sidebar.rows[0].note == note


def test_a_large_collection_builds_and_filters(sidebar: StudyMarkSidebar) -> None:
    """数千件でも一覧と絞り込みが正しく組み上がる。"""
    marks = [
        _mark(id=index + 1, page_index=index // 10, mistake_count=index % 5 + 1)
        for index in range(2000)
    ]

    sidebar.set_marks(marks)
    assert len(sidebar.rows) == 2000

    sidebar.set_filter(MarkFilter(mode=FilterMode.EXACT, exact_count=5))
    assert len(sidebar.rows) == 400
    assert set(counts(sidebar)) == {5}


@pytest.mark.parametrize("count", [1, 2, 3, 4, 10])
def test_the_list_can_be_filtered_to_an_exact_count(sidebar: StudyMarkSidebar, count: int) -> None:
    """EXACT(n) の一覧に出るのは n ちょうどのマークだけ。"""
    sidebar.set_marks([_mark(id=value, mistake_count=value) for value in (1, 2, 3, 4, 10)])

    sidebar.set_filter(MarkFilter(mode=FilterMode.EXACT, exact_count=count))

    assert counts(sidebar) == [count]
    assert row_texts(sidebar) == [("1", str(count), "")]


def test_the_list_can_be_filtered_to_three_or_more(sidebar: StudyMarkSidebar) -> None:
    """3+ には 3, 4, 10 が出て、1 と 2 は出ない。"""
    sidebar.set_marks([_mark(id=value, mistake_count=value) for value in (1, 2, 3, 4, 10)])

    sidebar.set_filter(MarkFilter(mode=FilterMode.AT_LEAST_3))

    assert counts(sidebar) == [3, 4, 10]


def test_clearing_the_filter_shows_everything_again(sidebar: StudyMarkSidebar) -> None:
    """ALL へ戻せば全件に戻る。絞り込みは元のスナップショットを削らない。"""
    sidebar.set_marks([_mark(id=value, mistake_count=value) for value in (1, 2, 3, 4, 10)])
    sidebar.set_filter(MarkFilter(mode=FilterMode.EXACT, exact_count=2))

    sidebar.set_filter(MarkFilter())

    assert counts(sidebar) == [1, 2, 3, 4, 10]
    assert len(sidebar.marks) == 5


def test_a_new_snapshot_keeps_the_filter(sidebar: StudyMarkSidebar) -> None:
    """スナップショットが変わっても絞り込みは持続する（表示だけの状態）。"""
    sidebar.set_filter(MarkFilter(mode=FilterMode.AT_LEAST_3))

    sidebar.set_marks([_mark(id=value, mistake_count=value) for value in (1, 5)])

    assert sidebar.mark_filter.mode is FilterMode.AT_LEAST_3
    assert counts(sidebar) == [5]


def test_the_filter_controls_drive_the_contract(sidebar: StudyMarkSidebar) -> None:
    """コンボボックスと回数の入力が、内部の条件へそのまま写る。

    UI の見せ方は変わっても、内部の契約は ALL / EXACT(n) / AT_LEAST_3 の
    3つだけであることを固定する。
    """
    sidebar.set_marks([_mark(id=value, mistake_count=value) for value in (1, 2, 3, 4, 10)])
    mode_box = sidebar.findChild(QComboBox)
    count_box = sidebar.findChild(QSpinBox)
    assert mode_box is not None
    assert count_box is not None

    # 既定は「すべて」。回数の入力は指定回数のときだけ効く。
    assert sidebar.mark_filter == MarkFilter()
    assert not count_box.isEnabled()

    mode_box.setCurrentIndex(mode_box.findData(FilterMode.EXACT))
    count_box.setValue(4)

    assert count_box.isEnabled()
    assert sidebar.mark_filter == MarkFilter(mode=FilterMode.EXACT, exact_count=4)
    assert counts(sidebar) == [4]

    mode_box.setCurrentIndex(mode_box.findData(FilterMode.AT_LEAST_3))

    assert sidebar.mark_filter.mode is FilterMode.AT_LEAST_3
    assert counts(sidebar) == [3, 4, 10]


def test_the_rows_are_rebuilt_and_the_selection_is_dropped(sidebar: StudyMarkSidebar) -> None:
    """スナップショットが変わったら行を作り直し、選択は残さない。

    SQLite の `INTEGER PRIMARY KEY` は最大 ID の行を消すと ID を再利用する。
    選択を ID で跨いで復元すると、別のマークが選ばれたことになる。
    """
    sidebar.set_marks([_mark(id=1, mistake_count=1), _mark(id=2, page_index=1, mistake_count=1)])
    last = sidebar.tree.topLevelItem(1)
    assert last is not None
    last.setSelected(True)
    assert sidebar.tree.selectedItems()

    # id 2 が消え、別の場所の新しいマークが同じ id を再利用した。
    sidebar.set_marks([_mark(id=1, mistake_count=1), _mark(id=2, page_index=5, mistake_count=7)])

    assert sidebar.tree.selectedItems() == []
    assert row_texts(sidebar) == [("1", "1", ""), ("6", "7", "")]


def test_an_empty_sidebar_survives_filtering_and_clicks(sidebar: StudyMarkSidebar) -> None:
    """マークが1件も無くても、絞り込みで落ちない。"""
    sidebar.set_filter(MarkFilter(mode=FilterMode.EXACT, exact_count=3))
    sidebar.set_filter(MarkFilter(mode=FilterMode.AT_LEAST_3))
    sidebar.set_filter(MarkFilter())

    assert sidebar.rows == ()
    assert sidebar.tree.topLevelItemCount() == 0


def test_a_row_click_requests_a_jump_for_that_mark(sidebar: StudyMarkSidebar) -> None:
    """行のクリックで、その行のマークがそのまま渡る。"""
    marks = [_mark(id=1, page_index=0, mistake_count=1), _mark(id=2, page_index=4, mistake_count=3)]
    sidebar.set_marks(marks)
    requested: list[StudyMark] = []
    sidebar.jump_requested.connect(requested.append)

    click_row(sidebar, 1)

    assert requested == [marks[1]]


def test_a_click_on_a_filtered_row_uses_the_original_mark(sidebar: StudyMarkSidebar) -> None:
    """絞り込み後の行番号をページ番号と取り違えない。"""
    sidebar.set_marks(
        [
            _mark(id=1, page_index=0, mistake_count=1),
            _mark(id=2, page_index=6, mistake_count=4),
        ]
    )
    sidebar.set_filter(MarkFilter(mode=FilterMode.AT_LEAST_3))
    requested: list[StudyMark] = []
    sidebar.jump_requested.connect(requested.append)

    click_row(sidebar, 0)

    assert len(requested) == 1
    assert requested[0].page_index == 6
    assert requested[0].mistake_count == 4


# ================================================================ 移動（PdfView）
def test_revealing_a_position_moves_to_that_page(loaded_view: PdfView) -> None:
    """指定した位置のページが現在ページになる。"""
    assert loaded_view.reveal_page_position(2, 0.5, 0.8) is True

    assert loaded_view.current_page == 2


def test_revealing_a_position_brings_the_anchor_into_view(loaded_view: PdfView) -> None:
    """アンカーがビューポートの中に入る。"""
    loaded_view.reveal_page_position(2, 0.5, 0.9)

    point = loaded_view.viewport_point_for(2, 0.5, 0.9)
    assert point is not None
    assert loaded_view.viewport().rect().contains(point.toPoint())


def test_revealing_centres_the_anchor_when_there_is_room(loaded_view: PdfView) -> None:
    """可動域に余裕があればアンカーはビューポート中央へ来る。

    固定のピクセル値ではなく、ビューポートの大きさから期待値を作る。
    """
    loaded_view.reveal_page_position(1, 0.5, 0.5)

    point = loaded_view.viewport_point_for(1, 0.5, 0.5)
    assert point is not None
    assert point.y() == pytest.approx(loaded_view.viewport().height() / 2, abs=1.0)


def test_revealing_a_missing_page_does_not_clamp(
    view: PdfView, doc: DocumentController, single_page_pdf: Path
) -> None:
    """存在しないページのマークでは移動しない。最終ページへ丸めない。"""
    show(view, doc, single_page_pdf)
    scroll = view.verticalScrollBar().value()

    assert view.reveal_page_position(7, 0.5, 0.5) is False

    assert view.current_page == 0
    assert view.verticalScrollBar().value() == scroll


def test_revealing_without_a_document_is_refused(view: PdfView) -> None:
    """PDF が無ければ何もしない。"""
    assert view.reveal_page_position(0, 0.5, 0.5) is False
    assert view.current_page == NO_PAGE


@pytest.mark.parametrize("zoom", [0.25, 1.0, 4.0])
def test_revealing_keeps_the_free_zoom(loaded_view: PdfView, zoom: float) -> None:
    """倍率も倍率モードも変わらない。アンカーは見える位置に入る。

    現在ページまでは固定しない。縮小して文書全体がビューポートに収まる
    状態では、`go_to_page()` でも現在ページは動かない（重なりが最大の
    ページという既存の規則がそのまま効く）。移動の契約として守るべきなのは
    「アンカーが見えること」の方。
    """
    loaded_view.set_zoom(zoom)

    assert loaded_view.reveal_page_position(2, 0.9, 0.9) is True

    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.zoom_mode is ZoomMode.FREE
    assert anchor_is_visible(loaded_view, _mark(id=1, page_index=2, mistake_count=1, x=0.9, y=0.9))


@pytest.mark.parametrize("mode", [ZoomMode.FIT_WIDTH, ZoomMode.FIT_PAGE])
def test_revealing_keeps_the_fit_mode(loaded_view: PdfView, mode: ZoomMode) -> None:
    """Fit Width / Fit Page のまま飛んでも FREE へ落ちない。"""
    if mode is ZoomMode.FIT_WIDTH:
        loaded_view.fit_width()
    else:
        loaded_view.fit_page()
    zoom = loaded_view.zoom

    assert loaded_view.reveal_page_position(2, 0.5, 0.8) is True

    assert loaded_view.zoom_mode is mode
    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.current_page == 2


@pytest.mark.parametrize("y_norm", [0.0, 0.5, 0.95])
def test_revealing_reaches_anywhere_on_the_last_page(loaded_view: PdfView, y_norm: float) -> None:
    """遠いページの上端でも下端でも、アンカーが見える位置まで動く。"""
    assert loaded_view.reveal_page_position(2, 0.5, y_norm) is True

    point = loaded_view.viewport_point_for(2, 0.5, y_norm)
    assert point is not None
    assert loaded_view.viewport().rect().contains(point.toPoint())
    assert loaded_view.current_page == 2


def test_revealing_does_not_touch_the_rendering_pipeline(
    loaded_view: PdfView, cache: RenderCache, service: RecordingService
) -> None:
    """移動のためにキャッシュや色変換を作り直さない。

    スクロールに伴う通常のレンダリング要求は起きてよい。壊してはいけないのは
    **既に持っている画像**の方で、世代を進めたりキャッシュを捨てたりすると、
    一覧から問題を見て回るたびに開いている PDF を全ページ描き直すことになる。

    「要求が増えていないこと」では検証にならない（世代を進めても要求は増える
    方向にしか動かない）。世代そのものと、仕込んだ raw / 表示用の画像が生き
    残っていることを見る。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    service.repeat_last_request()

    # 仕込んだ画像は1ページ分だけなので、幅を問わず同じ鍵が返る。
    raw_key = cache.nearest_key(0, width_px=0)
    display_key = service.display_cache.nearest_key(0, 0, PageColorMode.INVERT)
    assert raw_key is not None
    assert display_key is not None

    generation = service.generation
    color_mode = loaded_view.page_color_mode

    loaded_view.reveal_page_position(2, 0.5, 0.5)

    assert service.generation == generation
    assert raw_key in cache
    assert display_key in service.display_cache
    assert loaded_view.page_color_mode is color_mode


def test_revealing_refuses_coordinates_outside_the_contract(loaded_view: PdfView) -> None:
    """契約外の座標は黙って丸めずに失敗させる（P3-1 と同じ判定）。"""
    with pytest.raises(ValueError, match="y_norm"):
        loaded_view.reveal_page_position(0, 0.5, 1.5)


# ================================================================ ウィンドウ統合
def test_the_dock_is_hidden_with_a_stable_object_name(window: MainWindow) -> None:
    """初回は非表示。ウィンドウ状態の保存が識別できる名前を持つ。"""
    dock = window.study_mark_sidebar

    assert dock.objectName() == DOCK_OBJECT_NAME
    assert dock.isHidden()
    assert window.dockWidgetArea(dock) is Qt.DockWidgetArea.RightDockWidgetArea


def test_the_view_menu_can_toggle_the_dock(window: MainWindow) -> None:
    """「表示」から開閉できる。専用の設定キーは作らない。

    `QAction.menu()` は使わない（取り出した `QAction` の Python 側の参照が
    残らず、メニューごと破棄されることがある）。
    """
    action = window.study_mark_sidebar.toggleViewAction()
    menus = [menu for menu in window.findChildren(QMenu) if menu.title() == "表示(&V)"]
    assert len(menus) == 1
    assert action in menus[0].actions()

    action.trigger()
    assert window.study_mark_sidebar.isVisible()

    action.trigger()
    assert not window.study_mark_sidebar.isVisible()


def test_the_dock_visibility_is_restored_from_the_window_state(
    qtbot: QtBot, settings: Settings, study_marks: StudyMarkRepository
) -> None:
    """開いたまま閉じたら、次回も開いた状態で始まる。

    既存の `saveState()` / `restoreState()` に相乗りする。
    """
    first = MainWindow(settings, study_marks)
    qtbot.addWidget(first)
    first.study_mark_sidebar.show()
    first.close()

    second = MainWindow(settings, study_marks)
    qtbot.addWidget(second)

    assert not second.study_mark_sidebar.isHidden()
    second.close()


def test_opening_a_pdf_fills_the_sidebar(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """PDF を開くと一覧にその PDF のマークが並ぶ。"""
    first = study_marks.create(sample_pdf, 0, 0.2, 0.3, note="メモ")
    second = study_marks.create(sample_pdf, 2, 0.4, 0.5)

    window.open_path(sample_pdf)

    assert window.study_mark_sidebar.marks == (first, second)
    assert row_texts(window.study_mark_sidebar) == [("1", "1", "メモ"), ("3", "1", "")]


def test_switching_pdfs_swaps_the_sidebar_rows(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """A → B → A で、一覧に出るのは常に表示中の PDF のマークだけ。

    どちらも `page_index = 0` を含めておく。ページ番号だけで引いていると
    この検証が落ちる。
    """
    a1 = study_marks.create(sample_pdf, 0, 0.1, 0.1)
    a2 = study_marks.create(sample_pdf, 1, 0.2, 0.2)
    b1 = study_marks.create(two_page_pdf, 0, 0.9, 0.9)

    window.open_path(sample_pdf)
    assert list(window.study_mark_sidebar.rows) == [a1, a2]

    window.open_path(two_page_pdf)
    assert list(window.study_mark_sidebar.rows) == [b1]

    window.open_path(sample_pdf)
    assert list(window.study_mark_sidebar.rows) == [a1, a2]


def test_a_pdf_without_marks_empties_the_sidebar(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    single_page_pdf: Path,
) -> None:
    """マークの無い PDF へ切り替えたら、前の PDF の行は残らない。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    window.open_path(sample_pdf)

    window.open_path(single_page_pdf)

    assert window.study_mark_sidebar.rows == ()
    assert window.study_mark_sidebar.tree.topLevelItemCount() == 0


def test_a_window_without_a_pdf_has_an_empty_sidebar(window: MainWindow) -> None:
    """PDF を開いていなければ一覧も空。絞り込みを触っても落ちない。"""
    assert window.study_mark_sidebar.rows == ()

    window.study_mark_sidebar.set_filter(MarkFilter(mode=FilterMode.AT_LEAST_3))

    assert window.study_mark_sidebar.rows == ()


def test_a_failed_mark_load_empties_the_sidebar(
    qtbot: QtBot,
    settings: Settings,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    two_page_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """学習マークを読めずに開くのを中止したら、一覧も空になる。"""
    monkeypatch.setattr("anp.ui.main_window.QMessageBox.warning", lambda *_args: None)
    repository = BrokenRepository(study_mark_connection)
    repository.create(sample_pdf, 0, 0.1, 0.1)
    window = MainWindow(settings, repository)
    qtbot.addWidget(window)

    window.open_path(sample_pdf)
    assert len(window.study_mark_sidebar.rows) == 1

    repository.failing = "list"
    window.open_path(two_page_pdf)

    assert list(window.study_mark_sidebar.rows) == []
    assert list(window.view.study_marks) == []
    window.close()


def test_the_sidebar_and_the_overlay_always_match(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """一覧とオーバーレイは同じスナップショットから作られる。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    study_marks.create(sample_pdf, 2, 0.5, 0.5)

    window.open_path(sample_pdf)

    assert window.study_marks.study_marks == window.view.study_marks
    assert window.study_mark_sidebar.marks == window.view.study_marks


# ---------------------------------------------------------------- 絞り込みの独立性
def test_filtering_leaves_the_overlay_complete(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """絞り込みは一覧だけ。オーバーレイは全件を出し続ける。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    marked = study_marks.create(sample_pdf, 1, 0.5, 0.5)
    for _ in range(3):
        study_marks.increment_mistake_count(marked.id)
    window.open_path(sample_pdf)

    window.study_mark_sidebar.set_filter(MarkFilter(mode=FilterMode.AT_LEAST_3))

    assert len(window.study_mark_sidebar.rows) == 1
    assert len(window.view.study_marks) == 2
    assert window.study_marks.study_marks == window.view.study_marks


def test_filtering_does_not_query_the_database(
    qtbot: QtBot,
    settings: Settings,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
) -> None:
    """絞り込みで問い合わせは1回も増えない。"""
    repository = RecordingRepository(study_mark_connection)
    repository.create(sample_pdf, 0, 0.1, 0.1)
    window = MainWindow(settings, repository)
    qtbot.addWidget(window)
    window.open_path(sample_pdf)
    queries = len(repository.queried)

    for mark_filter in (
        MarkFilter(mode=FilterMode.EXACT, exact_count=1),
        MarkFilter(mode=FilterMode.EXACT, exact_count=4),
        MarkFilter(mode=FilterMode.AT_LEAST_3),
        MarkFilter(),
    ):
        window.study_mark_sidebar.set_filter(mark_filter)

    assert len(repository.queried) == queries
    window.close()


def test_filtering_does_not_move_the_view(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """絞り込みは表示位置にも倍率にも触らない。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    window.open_path(sample_pdf)
    window.view.go_to_page(1)
    scroll = window.view.verticalScrollBar().value()
    zoom = window.view.zoom

    window.study_mark_sidebar.set_filter(MarkFilter(mode=FilterMode.AT_LEAST_3))

    assert window.view.verticalScrollBar().value() == scroll
    assert window.view.zoom == pytest.approx(zoom)


# ---------------------------------------------------------------- 更新の反映
def test_creating_a_mark_adds_a_row(
    qtbot: QtBot, window: MainWindow, sample_pdf: Path, warnings: list[str]
) -> None:
    """Ctrl + クリックで作ったマークが、そのまま一覧に現れる。

    `MainWindow` からもサイドバーへ直接渡していないことの検証でもある。
    """
    window.open_path(sample_pdf)
    assert window.study_mark_sidebar.rows == ()

    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert warnings == []
    assert row_texts(window.study_mark_sidebar) == [("1", "1", "")]


def test_incrementing_updates_the_row_count(
    qtbot: QtBot,
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    warnings: list[str],
) -> None:
    """回数を増やすと、一覧の数字も 1 → 2 になる。"""
    study_marks.create(sample_pdf, 0, 0.5, 0.5)
    window.open_path(sample_pdf)
    assert counts(window.study_mark_sidebar) == [1]

    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert warnings == []
    assert counts(window.study_mark_sidebar) == [2]


def test_a_failed_mutation_keeps_the_old_rows(
    qtbot: QtBot,
    settings: Settings,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    warnings: list[str],
) -> None:
    """更新自体が失敗したら、一覧もオーバーレイも前のまま。"""
    repository = BrokenRepository(study_mark_connection)
    repository.create(sample_pdf, 0, 0.5, 0.5)
    window = MainWindow(settings, repository)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    window.open_path(sample_pdf)

    repository.failing = "increment"
    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert len(warnings) == 1
    assert counts(window.study_mark_sidebar) == [1]
    assert window.view.study_marks[0].mistake_count == 1
    window.close()


def test_a_failed_refresh_empties_both_views(
    qtbot: QtBot,
    settings: Settings,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    warnings: list[str],
) -> None:
    """更新は通ったが読み直せなかった場合、一覧もオーバーレイも空になる。

    オーバーレイだけ空で、一覧に古い数字が残る状態を作らない。PDF を
    読むこと自体は続けられる。
    """
    repository = BrokenRepository(study_mark_connection)
    repository.create(sample_pdf, 0, 0.5, 0.5)
    window = MainWindow(settings, repository)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    window.open_path(sample_pdf)

    repository.failing = "list"
    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert len(warnings) == 1
    assert window.study_mark_sidebar.rows == ()
    assert window.view.study_marks == ()
    assert window.study_marks.active_document_path == sample_pdf
    assert window.view.has_document
    window.close()


# ---------------------------------------------------------------- 移動の配線
def test_clicking_a_row_jumps_to_the_mark(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """行のクリックで、そのマークの位置まで移動する。"""
    study_marks.create(sample_pdf, 0, 0.5, 0.1)
    far = study_marks.create(sample_pdf, 2, 0.5, 0.8)
    window.open_path(sample_pdf)

    click_row(window.study_mark_sidebar, 1)

    assert window.view.current_page == far.page_index
    assert anchor_is_visible(window.view, far)


def test_clicking_a_row_does_not_change_the_records(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """行のクリックは移動だけ。回数もメモも増減しない。"""
    mark = study_marks.create(sample_pdf, 1, 0.5, 0.5, note="メモ")
    window.open_path(sample_pdf)

    click_row(window.study_mark_sidebar, 0)
    click_row(window.study_mark_sidebar, 0)

    assert study_marks.get(mark.id) == mark
    assert window.study_mark_sidebar.rows == (mark,)


def test_clicking_a_stale_row_is_safe(
    window: MainWindow, study_marks: StudyMarkRepository, single_page_pdf: Path
) -> None:
    """現在の PDF に無いページの行を押しても、落ちず・丸めず・記録も変えない。"""
    stale = study_marks.create(single_page_pdf, 7, 0.5, 0.5)
    window.open_path(single_page_pdf)
    scroll = window.view.verticalScrollBar().value()

    click_row(window.study_mark_sidebar, 0)

    assert window.view.current_page == 0
    assert window.view.verticalScrollBar().value() == scroll
    assert study_marks.get(stale.id) == stale
    assert "現在の PDF にありません" in window.statusBar().currentMessage()


def test_clicking_a_row_does_not_discard_the_rendering_state(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """一覧から移動しても、レンダリングの世代は進まない。

    `PdfView` 単体だけでなく配線の側でも固定する。移動の前後で
    `MainWindow` がドキュメントを載せ直せば、ビューが正しくても
    キャッシュは丸ごと捨てられる。
    """
    study_marks.create(sample_pdf, 2, 0.5, 0.8)
    window.open_path(sample_pdf)
    render = window.view._render  # noqa: SLF001
    generation = render.generation

    click_row(window.study_mark_sidebar, 0)

    assert render.generation == generation


@pytest.mark.parametrize("mode", ["free_25", "free_100", "free_400", "fit_width", "fit_page"])
def test_jumping_from_any_zoom_state(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path, mode: str
) -> None:
    """どの倍率状態から飛んでも、アンカーが見えて倍率モードも変わらない。

    現在ページは固定しない。25% のように文書全体が収まる状態では
    `go_to_page()` でも現在ページは動かないため（重なりが最大のページ、
    という既存の規則がそのまま効く）。
    """
    mark = study_marks.create(sample_pdf, 2, 0.5, 0.9)
    window.open_path(sample_pdf)

    if mode == "fit_width":
        window.view.fit_width()
    elif mode == "fit_page":
        window.view.fit_page()
    else:
        window.view.set_zoom({"free_25": 0.25, "free_100": 1.0, "free_400": 4.0}[mode])
    expected_mode = window.view.zoom_mode

    click_row(window.study_mark_sidebar, 0)

    assert window.view.zoom_mode is expected_mode
    assert anchor_is_visible(window.view, mark)
