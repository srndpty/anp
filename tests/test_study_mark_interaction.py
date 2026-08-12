"""学習マークの追加・更新・削除（P3-3B）のテスト。

確かめるのは「読みながら記録できること」と、「失敗したときに成功したように
見えないこと」。前半は `StudyMarkController` の更新 API 単体、後半は実際の
`PdfView` へマウスイベントを送る統合。

ダイアログは monkeypatch で決定的にする（実際には出さない）。DB の操作は
同期なので、待ち合わせもポーリングも要らない。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.pdf.document import DocumentController
from anp.storage import database
from anp.storage.study_mark import DocumentIdentity
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.fatal import EXIT_INTERNAL_ERROR
from anp.ui.main_window import MainWindow
from anp.ui.pdf_view import PdfView
from anp.ui.study_mark_controller import StudyMarkController, StudyMarkError
from anp.ui.study_marks import PagePosition, StudyMarkTarget
from conftest import FatalCalls
from helpers import BrokenRepository, RecordingRepository, RecordingService


# ---------------------------------------------------------------- 道具
def page_point(view: PdfView, page: int, x: float, y: float) -> QPointF:
    """ページ内の比率に対応するビューポート上の点（既存レイアウト由来）。"""
    rect = view.page_viewport_rect(page)
    assert rect is not None
    return QPointF(rect.left() + rect.width() * x, rect.top() + rect.height() * y)


def menu_actions(menu: QMenu) -> list[QAction]:
    """区切り線を除いたアクション。"""
    return [action for action in menu.actions() if not action.isSeparator()]


# バッジのメニューの並び。文言そのものではなく位置と振る舞いを固定する。
INCREMENT, EDIT_NOTE, DELETE = 0, 1, 2


def ctrl_click(qtbot: QtBot, view: PdfView, point: QPointF) -> None:
    """Ctrl + 左クリックを実際の Qt イベントとして送る。"""
    qtbot.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        point.toPoint(),
    )


def right_click(view: PdfView, point: QPointF) -> None:
    """右クリック（コンテキストメニュー要求）を送る。"""
    position = point.toPoint()
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, position, view.viewport().mapToGlobal(position)
    )
    QApplication.sendEvent(view.viewport(), event)


@pytest.fixture
def doc() -> Iterator[DocumentController]:
    controller = DocumentController()
    yield controller
    controller.close()


@pytest.fixture
def study_mark_controller(
    study_marks: StudyMarkRepository, view: PdfView, doc: DocumentController, sample_pdf: Path
) -> StudyMarkController:
    """3ページ PDF を表示対象にしたコントローラ。"""
    doc.open(sample_pdf)
    view.set_document(doc.document, doc.page_sizes())
    controller = StudyMarkController(study_marks, view)
    controller.activate_document(DocumentIdentity.of(sample_pdf))
    return controller


# ================================================================ コントローラ
def test_creating_a_mark_starts_at_one(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    sample_pdf: Path,
) -> None:
    """作成した直後の間違い回数は 1。呼び出し側に初期値を選ばせない。"""
    study_mark_controller.create_mark(
        PagePosition(page_index=1, x_norm=0.25, y_norm=0.75),
        expected_document=DocumentIdentity.of(sample_pdf),
    )

    (stored,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))
    assert stored.page_index == 1
    assert stored.x_norm == pytest.approx(0.25)
    assert stored.y_norm == pytest.approx(0.75)
    assert stored.mistake_count == 1
    assert stored.note is None
    assert view.study_marks == (stored,)


def test_creating_without_an_active_document_is_refused(
    study_marks: StudyMarkRepository, view: PdfView, sample_pdf: Path
) -> None:
    """表示対象が無ければ作成できない。別の PDF へ黙って保存しない。"""
    controller = StudyMarkController(study_marks, view)

    with pytest.raises(StudyMarkError):
        controller.create_mark(
            PagePosition(page_index=0, x_norm=0.5, y_norm=0.5),
            expected_document=DocumentIdentity.of(sample_pdf),
        )

    assert study_marks.list_for_document(DocumentIdentity.of(sample_pdf)) == []


@pytest.mark.parametrize("operation", ["increment_mark", "delete_mark"])
def test_mutating_without_an_active_document_is_refused(
    study_marks: StudyMarkRepository, view: PdfView, sample_pdf: Path, operation: str
) -> None:
    """既存マークの更新も、表示対象が無ければ行わない。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    controller = StudyMarkController(study_marks, view)

    with pytest.raises(StudyMarkError):
        getattr(controller, operation)(mark.id)

    assert study_marks.get(mark.id) == mark


def test_updating_a_note_without_an_active_document_is_refused(
    study_marks: StudyMarkRepository, view: PdfView, sample_pdf: Path
) -> None:
    """メモの更新も同じ。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    controller = StudyMarkController(study_marks, view)

    with pytest.raises(StudyMarkError):
        controller.update_note(mark.id, "メモ")

    assert study_marks.get(mark.id) == mark


def test_the_count_stays_an_exact_integer(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    sample_pdf: Path,
) -> None:
    """1 → 2 → … → 10 と整数のまま増える。「3+」へ丸めない。"""
    study_mark_controller.create_mark(
        PagePosition(page_index=0, x_norm=0.5, y_norm=0.5),
        expected_document=DocumentIdentity.of(sample_pdf),
    )
    (mark,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))

    for expected in range(2, 11):
        study_mark_controller.increment_mark(mark.id)

        assert study_marks.get(mark.id) is not None
        assert view.study_marks[0].mistake_count == expected


def test_the_note_is_stored_verbatim(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    sample_pdf: Path,
) -> None:
    """改行も前後の空白も Unicode もそのまま保存する。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    study_mark_controller.refresh()

    study_mark_controller.update_note(mark.id, "  1行目\n2行目 √2  ")

    assert study_marks.get(mark.id) is not None
    assert view.study_marks[0].note == "  1行目\n2行目 √2  "


def test_an_empty_note_is_not_turned_into_none(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
) -> None:
    """空文字は空文字のまま。`None` へ寄せない（P3-1 では別物）。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5, note="前のメモ")
    study_mark_controller.refresh()

    study_mark_controller.update_note(mark.id, "")

    stored = study_marks.get(mark.id)
    assert stored is not None
    assert stored.note == ""


def test_deleting_removes_the_row_and_the_overlay(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    sample_pdf: Path,
) -> None:
    """削除すると DB からも表示からも消える。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    study_mark_controller.refresh()

    study_mark_controller.delete_mark(mark.id)

    assert study_marks.get(mark.id) is None
    assert view.study_marks == ()


def test_creating_for_a_different_document_is_refused(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """位置を取ったときの PDF と表示中の PDF が違えば作成しない。

    `PagePosition` は正規化座標なのでどの PDF のものか名乗れない。照合が
    無いと、B を開いた後に A のメニューを発火させて B へマークができる。
    """
    with pytest.raises(StudyMarkError):
        study_mark_controller.create_mark(
            PagePosition(page_index=0, x_norm=0.5, y_norm=0.5),
            expected_document=DocumentIdentity.of(two_page_pdf),
        )

    assert study_marks.list_for_document(DocumentIdentity.of(sample_pdf)) == []
    assert study_marks.list_for_document(DocumentIdentity.of(two_page_pdf)) == []


@pytest.mark.parametrize("operation", ["increment_mark", "delete_mark"])
def test_a_mark_of_another_document_is_not_touched(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    two_page_pdf: Path,
    operation: str,
) -> None:
    """表示中でない PDF のマークは更新させない。"""
    other = study_marks.create(DocumentIdentity.of(two_page_pdf), 0, 0.5, 0.5)

    with pytest.raises(StudyMarkError):
        getattr(study_mark_controller, operation)(other.id)

    assert study_marks.get(other.id) == other


def test_a_note_update_on_another_document_is_refused(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    two_page_pdf: Path,
) -> None:
    """メモでも持ち主を確かめる。"""
    other = study_marks.create(DocumentIdentity.of(two_page_pdf), 0, 0.5, 0.5, note="B のメモ")

    with pytest.raises(StudyMarkError):
        study_mark_controller.update_note(other.id, "書き換え")

    assert study_marks.get(other.id) == other


@pytest.mark.parametrize("operation", ["increment_mark", "delete_mark"])
def test_a_vanished_mark_is_reported_and_the_view_catches_up(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    sample_pdf: Path,
    operation: str,
) -> None:
    """消えていたマークへの操作は、成功として飲み込まず表示を合わせ直す。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    study_mark_controller.refresh()
    assert len(view.study_marks) == 1
    study_marks.delete(mark.id)

    with pytest.raises(StudyMarkError):
        getattr(study_mark_controller, operation)(mark.id)

    assert list(view.study_marks) == []


@pytest.mark.parametrize(
    ("failing", "operation"),
    [("increment", "increment_mark"), ("delete", "delete_mark")],
)
def test_a_failed_mutation_leaves_the_previous_snapshot(
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
    failing: str,
    operation: str,
) -> None:
    """更新に失敗したら、表示は前の内容のまま。成功したように見せない。"""
    repository = BrokenRepository(study_mark_connection)
    mark = repository.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    repository.increment_mistake_count(mark.id)
    doc.open(sample_pdf)
    view.set_document(doc.document, doc.page_sizes())
    controller = StudyMarkController(repository, view)
    controller.activate_document(DocumentIdentity.of(sample_pdf))
    repository.failing = failing

    with pytest.raises(sqlite3.OperationalError):
        getattr(controller, operation)(mark.id)

    assert [(m.id, m.mistake_count) for m in view.study_marks] == [(mark.id, 2)]
    assert controller.active_document_path == sample_pdf


def test_a_failed_create_adds_nothing(
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """作成に失敗したらバッジも増えない。表示対象はそのまま。"""
    repository = BrokenRepository(study_mark_connection)
    doc.open(sample_pdf)
    view.set_document(doc.document, doc.page_sizes())
    controller = StudyMarkController(repository, view)
    controller.activate_document(DocumentIdentity.of(sample_pdf))
    repository.failing = "create"

    with pytest.raises(sqlite3.OperationalError):
        controller.create_mark(
            PagePosition(page_index=0, x_norm=0.5, y_norm=0.5),
            expected_document=DocumentIdentity.of(sample_pdf),
        )

    assert view.study_marks == ()
    assert controller.active_document_path == sample_pdf


def test_a_failed_note_update_keeps_the_old_note(
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """メモの更新に失敗したら、前のメモが残る。"""
    repository = BrokenRepository(study_mark_connection)
    mark = repository.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5, note="元のメモ")
    doc.open(sample_pdf)
    view.set_document(doc.document, doc.page_sizes())
    controller = StudyMarkController(repository, view)
    controller.activate_document(DocumentIdentity.of(sample_pdf))
    repository.failing = "update_note"

    with pytest.raises(sqlite3.OperationalError):
        controller.update_note(mark.id, "新しいメモ")

    stored = repository.get(mark.id)
    assert stored is not None
    assert stored.note == "元のメモ"
    assert view.study_marks[0].note == "元のメモ"


def test_a_successful_mutation_does_not_depend_on_a_reread(
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """更新が通ったら、全件を読み直せなくても成功として扱う。

    接続は `autocommit=True` なので UPDATE の1文で確定している。その後の
    SELECT が失敗したことを理由に「更新できませんでした」と伝えると、
    利用者はもう一度押し、1回のつもりが2回加算される。更新後の1件は
    `RETURNING` で返ってきているので、そもそも読み直さない。
    """
    repository = BrokenRepository(study_mark_connection)
    mark = repository.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    doc.open(sample_pdf)
    view.set_document(doc.document, doc.page_sizes())
    controller = StudyMarkController(repository, view)
    controller.activate_document(DocumentIdentity.of(sample_pdf))
    repository.failing = "list"

    controller.increment_mark(mark.id)

    assert [(shown.id, shown.mistake_count) for shown in view.study_marks] == [(mark.id, 2)]
    assert controller.active_document_path == sample_pdf
    repository.failing = ""
    stored = repository.get(mark.id)
    assert stored is not None
    assert stored.mistake_count == 2


def test_a_mutation_does_not_reread_the_whole_document(
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """更新のたびに全件を SELECT し直さない。"""
    repository = RecordingRepository(study_mark_connection)
    mark = repository.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    doc.open(sample_pdf)
    view.set_document(doc.document, doc.page_sizes())
    controller = StudyMarkController(repository, view)
    controller.activate_document(DocumentIdentity.of(sample_pdf))
    queries = len(repository.queried)

    controller.increment_mark(mark.id)
    controller.update_note(mark.id, "メモ")
    controller.create_mark(
        PagePosition(page_index=0, x_norm=0.2, y_norm=0.2),
        expected_document=DocumentIdentity.of(sample_pdf),
    )
    controller.delete_mark(mark.id)

    assert len(repository.queried) == queries


def test_a_mutation_does_not_touch_the_rendering(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    service: RecordingService,
    sample_pdf: Path,
) -> None:
    """更新でレンダリング要求も倍率もスクロール位置も動かない（P3-2 の分離）。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    study_mark_controller.refresh()
    requests = len(service.requests)
    generation = service.generation
    zoom = view.zoom
    page = view.current_page
    scroll = view.verticalScrollBar().value()

    study_mark_controller.increment_mark(mark.id)
    study_mark_controller.update_note(mark.id, "メモ")
    study_mark_controller.create_mark(
        PagePosition(page_index=0, x_norm=0.2, y_norm=0.2),
        expected_document=DocumentIdentity.of(sample_pdf),
    )
    study_mark_controller.delete_mark(mark.id)

    assert len(service.requests) == requests
    assert service.generation == generation
    assert view.zoom == zoom
    assert view.current_page == page
    assert view.verticalScrollBar().value() == scroll


# ================================================================ ビューの判定
def test_a_badge_wins_over_the_page_underneath(
    study_mark_controller: StudyMarkController, view: PdfView, sample_pdf: Path
) -> None:
    """バッジの上を指したら、新規作成ではなく既存マークが対象になる。"""
    view.fit_page()
    study_mark_controller.create_mark(
        PagePosition(page_index=0, x_norm=0.5, y_norm=0.5),
        expected_document=DocumentIdentity.of(sample_pdf),
    )
    point = page_point(view, 0, 0.5, 0.5)

    target = view.study_mark_target_at(point)

    assert target.mark == view.study_marks[0]
    assert target.position is None


def test_a_point_on_the_page_is_a_create_target(loaded_view: PdfView) -> None:
    """バッジが無ければページ上の位置が対象。"""
    loaded_view.fit_page()

    target = loaded_view.study_mark_target_at(page_point(loaded_view, 0, 0.25, 0.75))

    assert target.mark is None
    assert target.position is not None
    assert target.position.page_index == 0


def test_a_point_outside_the_page_has_no_target(view: PdfView) -> None:
    """ページの外は対象なし。"""
    assert view.study_mark_target_at(QPointF(2.0, 2.0)) == StudyMarkTarget()
    assert view.study_mark_target_at(QPointF(2.0, 2.0)).is_empty


def test_only_the_topmost_of_overlapping_badges_is_the_target(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    sample_pdf: Path,
) -> None:
    """同じ位置に2件あっても、対象は上に描かれた1件だけ。"""
    view.fit_page()
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    top = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    study_mark_controller.refresh()

    target = view.study_mark_target_at(page_point(view, 0, 0.5, 0.5))

    assert target.mark is not None
    assert target.mark.id == top.id


# ================================================================ ウィンドウ統合
@pytest.fixture
def window(
    qtbot: QtBot, settings: Settings, study_marks: StudyMarkRepository, sample_pdf: Path
) -> Iterator[MainWindow]:
    """3ページ PDF を開き、1ページ目全体が見えているウィンドウ。"""
    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    window.open_path(sample_pdf)
    window.view.fit_page()
    yield window
    window.close()


@pytest.fixture(autouse=True)
def errors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """更新に失敗したときの警告ダイアログを捕まえる（実際には出さない）。

    どのテストでもモーダルダイアログを開かせないため autouse にする。
    出ないはずのテストでは、空のままであること自体が検証になる。
    """
    messages: list[str] = []
    monkeypatch.setattr(
        "anp.ui.study_mark_interaction.QMessageBox.warning",
        lambda *args: messages.append(args[2]),
    )
    return messages


@pytest.fixture
def confirm_delete(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """削除の確認を「はい」で答える。呼ばれた回数を記録する。"""
    asked: list[bool] = []

    def answer(*_args: object) -> QMessageBox.StandardButton:
        asked.append(True)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr("anp.ui.study_mark_interaction.QMessageBox.question", answer)
    return asked


@pytest.fixture
def cancel_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """削除の確認を「いいえ」で答える。"""
    monkeypatch.setattr(
        "anp.ui.study_mark_interaction.QMessageBox.question",
        lambda *_args: QMessageBox.StandardButton.No,
    )


def stub_note_dialog(
    monkeypatch: pytest.MonkeyPatch, text: str, *, accepted: bool = True
) -> list[str]:
    """メモの編集ダイアログを差し替え、初期値を記録する。"""
    initial: list[str] = []

    def dialog(*args: object) -> tuple[str, bool]:
        initial.append(str(args[3]))
        return text, accepted

    monkeypatch.setattr("anp.ui.study_mark_interaction.QInputDialog.getMultiLineText", dialog)
    return initial


# ---------------------------------------------------------------- Ctrl + 左クリック
def test_ctrl_clicking_the_page_creates_a_mark(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ページ上の Ctrl + 左クリックで、押した位置にマークが1件できる。"""
    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    (stored,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))
    assert stored.page_index == 0
    assert stored.x_norm == pytest.approx(0.5, abs=0.01)
    assert stored.y_norm == pytest.approx(0.5, abs=0.01)
    assert stored.mistake_count == 1
    assert window.view.study_marks == (stored,)


def test_ctrl_clicking_the_second_page_records_that_page(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ページ番号は押したページのもの。"""
    window.view.go_to_page(1)

    ctrl_click(qtbot, window.view, page_point(window.view, 1, 0.5, 0.5))

    (stored,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))
    assert stored.page_index == 1


def test_ctrl_clicking_a_badge_increments_it(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """バッジの上を Ctrl + 左クリックすると回数が増える（新しく作らない）。"""
    point = page_point(window.view, 0, 0.5, 0.5)
    ctrl_click(qtbot, window.view, point)

    ctrl_click(qtbot, window.view, point)

    (stored,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))
    assert stored.mistake_count == 2
    assert window.view.study_marks[0].mistake_count == 2


def test_repeated_ctrl_clicks_do_not_drop_any(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """連続して押しても 1 → 2 → 3 → 4 と取りこぼさない。"""
    point = page_point(window.view, 0, 0.5, 0.5)

    for expected in range(1, 5):
        ctrl_click(qtbot, window.view, point)

        (stored,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))
        assert stored.mistake_count == expected
        assert window.view.study_marks[0].mistake_count == expected


def test_a_ctrl_double_click_counts_both_presses(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """速い連打でも押した回数だけ増える。

    Qt は速い2回目の押下を `MouseButtonPress` ではなく
    `MouseButtonDblClick` として送る。押下の入口を1つしか見ていないと、
    連打したときに1回おきに取りこぼす（ここでは 1 のまま止まる）。
    """
    point = page_point(window.view, 0, 0.5, 0.5)
    ctrl_click(qtbot, window.view, point)

    qtbot.mouseDClick(
        window.view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        point.toPoint(),
    )

    (stored,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))
    assert stored.mistake_count == 2


def test_ctrl_clicking_a_duplicate_increments_only_the_topmost(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """同じ位置に2件あっても、増えるのは上のバッジだけ。"""
    lower = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    upper = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()

    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert study_marks.get(lower.id) == lower
    top = study_marks.get(upper.id)
    assert top is not None
    assert top.mistake_count == 2


def test_ctrl_clicking_outside_the_page_does_nothing(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """キャンバスの余白では何も起きない（例外もダイアログも無し）。"""
    ctrl_click(qtbot, window.view, QPointF(2.0, 2.0))

    assert study_marks.list_for_document(DocumentIdentity.of(sample_pdf)) == []


def test_ctrl_clicking_the_gap_between_pages_does_nothing(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ページ間の隙間でも作られない。"""
    window.view.set_zoom(0.25)
    first = window.view.page_viewport_rect(0)
    second = window.view.page_viewport_rect(1)
    assert first is not None
    assert second is not None

    ctrl_click(qtbot, window.view, QPointF(first.center().x(), (first.bottom() + second.top()) / 2))

    assert study_marks.list_for_document(DocumentIdentity.of(sample_pdf)) == []


def test_a_plain_click_never_touches_the_marks(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """修飾なしの左クリックでは、ページの上でもバッジの上でも何も変わらない。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()
    point = page_point(window.view, 0, 0.5, 0.5)

    qtbot.mouseClick(window.view.viewport(), Qt.MouseButton.LeftButton, pos=point.toPoint())
    qtbot.mouseClick(
        window.view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=page_point(window.view, 0, 0.2, 0.2).toPoint(),
    )

    assert study_marks.list_for_document(DocumentIdentity.of(sample_pdf)) == [mark]


@pytest.mark.parametrize(
    "modifier",
    [
        Qt.KeyboardModifier.ShiftModifier,
        Qt.KeyboardModifier.AltModifier,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    ],
)
def test_other_modifiers_do_not_trigger_study_marks(
    qtbot: QtBot,
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    modifier: Qt.KeyboardModifier,
) -> None:
    """Ctrl だけを学習マークの操作とする（完全一致）。"""
    qtbot.mouseClick(
        window.view.viewport(),
        Qt.MouseButton.LeftButton,
        modifier,
        page_point(window.view, 0, 0.5, 0.5).toPoint(),
    )

    assert study_marks.list_for_document(DocumentIdentity.of(sample_pdf)) == []


def test_ctrl_clicking_without_a_pdf_does_nothing(
    qtbot: QtBot, settings: Settings, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """PDF を開いていないときに押しても落ちないし、何も保存されない。"""
    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)

    ctrl_click(qtbot, window.view, QPointF(50.0, 50.0))

    assert list(window.view.study_marks) == []
    assert study_marks.list_for_document(DocumentIdentity.of(sample_pdf)) == []
    window.close()


def test_right_clicking_without_a_pdf_asks_for_nothing(qtbot: QtBot, view: PdfView) -> None:
    """PDF を開いていないときの右クリックでも学習マークのメニューは出ない。

    ウィンドウではなく単体のビューへ送る。`QMainWindow` は自分の
    `contextMenuEvent` でツールバーの表示切り替えメニューを開く（P3-3B より
    前からの既定の振る舞い）ので、素通しした先でモーダルになってしまう。
    ここで見たいのは「学習マークのメニューを要求しないこと」だけ。
    """
    with qtbot.assertNotEmitted(view.study_mark_menu_requested):
        right_click(view, QPointF(50.0, 50.0))


def test_ctrl_clicking_still_works_after_scrolling_and_zooming(
    qtbot: QtBot, window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """スクロールやズームの後でも、同じバッジを押せば同じマークが増える。"""
    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.4, 0.4))
    (mark,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))

    window.view.set_zoom(2.0)
    window.view.verticalScrollBar().setValue(window.view.verticalScrollBar().value() + 30)
    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.4, 0.4))

    stored = study_marks.get(mark.id)
    assert stored is not None
    assert stored.mistake_count == 2
    assert len(study_marks.list_for_document(DocumentIdentity.of(sample_pdf))) == 1


# ---------------------------------------------------------------- 右クリックメニュー
def test_right_clicking_a_badge_asks_for_a_menu(
    qtbot: QtBot,
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    sample_pdf: Path,
) -> None:
    """バッジの上の右クリックは、そのマークを対象としたメニューを要求する。

    ここでは単体のビューへ送る。`MainWindow` に載せると、受け取った側が
    その場でメニューを開いて（`QMenu.exec()`）入れ子のイベントループへ入って
    しまうため。メニューの中身と振る舞いは `build_menu()` 経由で確かめる。
    """
    view.fit_page()
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    study_mark_controller.refresh()

    with qtbot.waitSignal(view.study_mark_menu_requested) as blocker:
        right_click(view, page_point(view, 0, 0.5, 0.5))

    target = blocker.args[0]
    assert isinstance(target, StudyMarkTarget)
    assert target.mark == mark
    assert isinstance(blocker.args[1], QPoint)


def test_right_clicking_the_page_asks_for_a_create_menu(qtbot: QtBot, loaded_view: PdfView) -> None:
    """ページ上の右クリックは、その位置を対象としたメニューを要求する。"""
    loaded_view.fit_page()

    with qtbot.waitSignal(loaded_view.study_mark_menu_requested) as blocker:
        right_click(loaded_view, page_point(loaded_view, 0, 0.3, 0.3))

    target = blocker.args[0]
    assert target.mark is None
    assert target.position is not None
    assert target.position.x_norm == pytest.approx(0.3, abs=0.01)


def test_right_clicking_outside_the_page_asks_for_nothing(
    qtbot: QtBot, loaded_view: PdfView
) -> None:
    """ページの外では学習マークのメニューを出さない。

    ウィンドウではなく単体のビューへ送る理由は
    `test_right_clicking_without_a_pdf_asks_for_nothing` と同じ。
    """
    with qtbot.assertNotEmitted(loaded_view.study_mark_menu_requested):
        right_click(loaded_view, QPointF(2.0, 2.0))


def test_the_menu_differs_between_a_badge_and_the_page(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ページの上とバッジの上で別のメニューになる。

    項目数だけを見る（文言そのものは固定しない）。ページ上は追加の1項目、
    バッジ上は回数・メモ・削除の3項目。
    """
    page_menu = window.study_mark_interaction.build_menu(
        window.view.study_mark_target_at(page_point(window.view, 0, 0.3, 0.3))
    )
    assert page_menu is not None
    assert len(menu_actions(page_menu)) == 1

    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()

    assert len(menu_actions(badge_menu(window))) == 3


def test_the_page_menu_creates_a_mark(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """「学習マークを追加」で、確認なしに回数 1 のマークができる。"""
    point = page_point(window.view, 0, 0.3, 0.7)
    target = window.view.study_mark_target_at(point)
    menu = window.study_mark_interaction.build_menu(target)
    assert menu is not None

    menu_actions(menu)[0].trigger()

    (stored,) = study_marks.list_for_document(DocumentIdentity.of(sample_pdf))
    assert stored.mistake_count == 1
    assert stored.x_norm == pytest.approx(0.3, abs=0.01)


def test_the_badge_menu_increments(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """「間違い回数を増やす」で回数が増える。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()
    menu = window.study_mark_interaction.build_menu(
        window.view.study_mark_target_at(page_point(window.view, 0, 0.5, 0.5))
    )
    assert menu is not None

    menu_actions(menu)[INCREMENT].trigger()

    stored = study_marks.get(mark.id)
    assert stored is not None
    assert stored.mistake_count == 2
    assert window.view.study_marks[0].mistake_count == 2


def test_there_is_no_menu_outside_the_page(window: MainWindow) -> None:
    """対象が無ければメニューを作らない。"""
    assert window.study_mark_interaction.build_menu(StudyMarkTarget()) is None


# ---------------------------------------------------------------- メモ
def badge_menu(window: MainWindow, x: float = 0.5, y: float = 0.5) -> QMenu:
    """バッジの上で開いたときのメニュー。"""
    menu = window.study_mark_interaction.build_menu(
        window.view.study_mark_target_at(page_point(window.view, 0, x, y))
    )
    assert menu is not None
    return menu


@pytest.mark.parametrize(("note", "expected"), [(None, ""), ("", ""), ("abc", "abc")])
def test_the_note_editor_starts_with_the_stored_text(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
    note: str | None,
    expected: str,
) -> None:
    """未設定も空文字も空欄から。保存されている文字列はそのまま初期値。"""
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5, note=note)
    window.study_marks.refresh()
    initial = stub_note_dialog(monkeypatch, "変更後")

    menu_actions(badge_menu(window))[EDIT_NOTE].trigger()

    assert initial == [expected]


def test_cancelling_the_note_editor_changes_nothing(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取り消したら DB を変えない。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5, note="元のメモ")
    window.study_marks.refresh()
    stub_note_dialog(monkeypatch, "捨てられる文字列", accepted=False)

    menu_actions(badge_menu(window))[EDIT_NOTE].trigger()

    assert study_marks.get(mark.id) == mark


def test_a_multiline_note_is_saved_exactly(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """複数行のメモが1文字も変わらずに保存される。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()
    stub_note_dialog(monkeypatch, "abc\nxyz")

    menu_actions(badge_menu(window))[EDIT_NOTE].trigger()

    stored = study_marks.get(mark.id)
    assert stored is not None
    assert stored.note == "abc\nxyz"
    assert window.view.study_marks[0].note == "abc\nxyz"


def test_an_empty_note_is_saved_as_an_empty_string(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空のまま OK したら空文字。`None` へは戻さない。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5, note="元のメモ")
    window.study_marks.refresh()
    stub_note_dialog(monkeypatch, "")

    menu_actions(badge_menu(window))[EDIT_NOTE].trigger()

    stored = study_marks.get(mark.id)
    assert stored is not None
    assert stored.note == ""


# ---------------------------------------------------------------- 削除
def test_deleting_asks_for_confirmation_first(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    confirm_delete: list[bool],
) -> None:
    """「はい」で消える。確認を必ず通る。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()

    menu_actions(badge_menu(window))[DELETE].trigger()

    assert confirm_delete == [True]
    assert study_marks.get(mark.id) is None
    assert window.view.study_marks == ()


@pytest.mark.usefixtures("cancel_delete")
def test_cancelling_the_delete_keeps_the_mark(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """「いいえ」なら消さない。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()

    menu_actions(badge_menu(window))[DELETE].trigger()

    assert study_marks.get(mark.id) == mark
    assert window.view.study_marks == (mark,)


# ---------------------------------------------------------------- 失敗の見せ方
@pytest.fixture
def broken_window(
    qtbot: QtBot,
    settings: Settings,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
) -> Iterator[tuple[MainWindow, BrokenRepository]]:
    """更新を失敗させられるリポジトリを使うウィンドウ。"""
    repository = BrokenRepository(study_mark_connection)
    window = MainWindow(settings, repository)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    window.open_path(sample_pdf)
    window.view.fit_page()
    yield window, repository
    window.close()


def test_a_failed_create_is_reported_and_keeps_the_pdf_open(
    qtbot: QtBot,
    broken_window: tuple[MainWindow, BrokenRepository],
    sample_pdf: Path,
    errors: list[str],
) -> None:
    """作成に失敗しても PDF は開いたまま。バッジも増えない。"""
    window, repository = broken_window
    repository.failing = "create"

    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert len(errors) == 1
    assert window.view.study_marks == ()
    assert window.view.has_document
    assert window.study_marks.active_document_path == sample_pdf


def test_a_failed_increment_keeps_the_previous_count(
    qtbot: QtBot,
    broken_window: tuple[MainWindow, BrokenRepository],
    sample_pdf: Path,
    errors: list[str],
) -> None:
    """増やせなかったら表示も DB も前のまま（2 のまま）。"""
    window, repository = broken_window
    mark = repository.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    repository.increment_mistake_count(mark.id)
    window.study_marks.refresh()
    repository.failing = "increment"

    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert len(errors) == 1
    assert window.view.study_marks[0].mistake_count == 2
    repository.failing = ""
    stored = repository.get(mark.id)
    assert stored is not None
    assert stored.mistake_count == 2
    assert window.view.has_document


def test_a_failed_delete_keeps_the_badge(
    broken_window: tuple[MainWindow, BrokenRepository],
    sample_pdf: Path,
    errors: list[str],
    confirm_delete: list[bool],
) -> None:
    """削除に失敗したらバッジは残る。"""
    window, repository = broken_window
    mark = repository.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()
    repository.failing = "delete"

    menu_actions(badge_menu(window))[DELETE].trigger()

    assert len(errors) == 1
    assert window.view.study_marks == (mark,)
    assert window.view.has_document


def test_a_failed_note_update_is_reported(
    broken_window: tuple[MainWindow, BrokenRepository],
    sample_pdf: Path,
    errors: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """メモを保存できなかったら、前のメモが残ったまま知らせる。"""
    window, repository = broken_window
    repository.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5, note="元のメモ")
    window.study_marks.refresh()
    repository.failing = "update_note"
    stub_note_dialog(monkeypatch, "新しいメモ")

    menu_actions(badge_menu(window))[EDIT_NOTE].trigger()

    assert len(errors) == 1
    assert window.view.study_marks[0].note == "元のメモ"
    assert window.view.has_document


def test_a_programming_error_stops_the_application(
    qtbot: QtBot,
    window: MainWindow,
    sample_pdf: Path,
    errors: list[str],
    fatal: FatalCalls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実装の誤りは小さな警告に化けさせず、その場で終了させる。

    ここで広く捕まえると、バグが「学習マークを更新できませんでした」に
    見えたまま残り続ける。かといって Qt の slot から例外を外へ出すのは
    undefined behavior なので、境界で受け止めて fail-stop する。

    **本番と同じ経路（Ctrl + クリック）で確かめる。** 直接メソッドを呼ぶと、
    Qt の境界を通っていないので、この契約を検査したことにならない。
    """

    def buggy(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("bug")

    monkeypatch.setattr(window.study_marks, "create_mark", buggy)

    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert errors == [], "実装の誤りが通常の警告に化けている"
    assert fatal.exit_codes == [EXIT_INTERNAL_ERROR]
    assert "AttributeError" in fatal.dialogs[0]


def test_a_broken_reread_does_not_break_a_click(
    qtbot: QtBot,
    broken_window: tuple[MainWindow, BrokenRepository],
    sample_pdf: Path,
    errors: list[str],
) -> None:
    """全件を読み直せない状態でも、Ctrl + クリックの加算はそのまま通る。

    DB では成功しているのに警告が出る、という食い違いを作らない
    （利用者がもう一度押して二重に加算されるのを避ける）。
    """
    window, repository = broken_window
    mark = repository.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()
    repository.failing = "list"

    ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.5, 0.5))

    assert errors == []
    assert [shown.mistake_count for shown in window.view.study_marks] == [2]
    assert window.view.has_document
    assert window.study_marks.active_document_path == sample_pdf
    repository.failing = ""
    stored = repository.get(mark.id)
    assert stored is not None
    assert stored.mistake_count == 2


# ---------------------------------------------------------------- ドキュメントの切り替え
def test_a_stale_menu_action_does_not_touch_the_other_document(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    two_page_pdf: Path,
    errors: list[str],
) -> None:
    """A のマークを対象にしたアクションが、B を開いた後に発火しても何も壊さない。

    同期 UI なので通常は起こらないが、更新の境界が最後の防御になっている
    ことを固定する。
    """
    a_mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    b_mark = study_marks.create(DocumentIdentity.of(two_page_pdf), 0, 0.5, 0.5)
    window.study_marks.refresh()
    stale = menu_actions(badge_menu(window))[INCREMENT]

    window.open_path(two_page_pdf)
    stale.trigger()

    assert study_marks.get(a_mark.id) == a_mark
    assert study_marks.get(b_mark.id) == b_mark
    assert len(errors) == 1
    assert window.view.has_document


def test_a_stale_create_action_does_not_add_to_the_other_document(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    two_page_pdf: Path,
    errors: list[str],
) -> None:
    """A のページで開いた「追加」が、B を開いた後に発火しても B を汚さない。

    `PagePosition` だけを持ち回すと、B に A 由来の座標のマークができる。
    メニューを開いた時点の表示対象を一緒に捕まえておく理由。
    """
    b_mark = study_marks.create(DocumentIdentity.of(two_page_pdf), 0, 0.1, 0.1)
    page_menu = window.study_mark_interaction.build_menu(
        window.view.study_mark_target_at(page_point(window.view, 0, 0.3, 0.7))
    )
    assert page_menu is not None
    stale = menu_actions(page_menu)[0]

    window.open_path(two_page_pdf)
    stale.trigger()

    assert study_marks.list_for_document(DocumentIdentity.of(sample_pdf)) == []
    assert study_marks.list_for_document(DocumentIdentity.of(two_page_pdf)) == [b_mark]
    assert len(errors) == 1
    assert window.view.has_document
    assert window.study_marks.active_document_path == two_page_pdf
    assert window.view.study_marks == (b_mark,)


def test_a_stale_create_action_does_not_add_to_a_replaced_pdf(
    qtbot: QtBot,
    settings: Settings,
    study_marks: StudyMarkRepository,
    tmp_path: Path,
    sample_pdf: Path,
    two_page_pdf: Path,
    errors: list[str],
) -> None:
    """同じパスの PDF を差し替えて開き直しても、前の内容の座標は入らない。

    捕まえるのがパスだけだと、パスは一致したままなので照合を素通りする。
    差し替え後の PDF に、見ていないページの座標が記録されてしまう。
    """
    path = tmp_path / "book.pdf"
    path.write_bytes(sample_pdf.read_bytes())
    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    window.open_path(path)
    window.view.fit_page()

    page_menu = window.study_mark_interaction.build_menu(
        window.view.study_mark_target_at(page_point(window.view, 0, 0.3, 0.7))
    )
    assert page_menu is not None
    stale = menu_actions(page_menu)[0]

    # 同じパスのまま、中身が別の PDF に差し替わって開き直された。
    path.write_bytes(two_page_pdf.read_bytes())
    window.open_path(path)
    replaced = DocumentIdentity.of(path)

    stale.trigger()

    try:
        assert study_marks.list_for_document(replaced) == []
        assert window.view.study_marks == ()
        assert len(errors) == 1
        assert window.view.has_document
    finally:
        window.close()


def test_marks_created_in_one_session_come_back_in_the_next(
    qtbot: QtBot, settings: Settings, tmp_path: Path, sample_pdf: Path
) -> None:
    """UI で作ったマークが、開き直したときに戻ってくる。"""
    db = tmp_path / "session.sqlite3"

    first = database.connect(db)
    try:
        window = MainWindow(settings, StudyMarkRepository(first))
        qtbot.addWidget(window)
        with qtbot.waitExposed(window):
            window.show()
        window.open_path(sample_pdf)
        window.view.fit_page()
        ctrl_click(qtbot, window.view, page_point(window.view, 0, 0.25, 0.75))
        assert len(window.view.study_marks) == 1
        window.close()
    finally:
        first.close()

    second = database.connect(db)
    try:
        window = MainWindow(settings, StudyMarkRepository(second))
        qtbot.addWidget(window)
        window.open_path(sample_pdf)

        assert len(window.view.study_marks) == 1
        assert window.view.study_marks[0].x_norm == pytest.approx(0.25, abs=0.01)
        assert window.view.study_marks[0].mistake_count == 1
        window.close()
    finally:
        second.close()
