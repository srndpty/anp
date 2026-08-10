"""学習マークの実行時接続（P3-3A）のテスト。

確かめるのは「表示中の PDF と、表示されている学習マークが常に対応して
いること」。前半は `StudyMarkController` 単体、後半は `MainWindow` を
通した統合、最後にアプリケーションの DB ライフサイクル。

作成 UI はまだ無いので、マークはテストからリポジトリで直接仕込む。
本番コードが `create()` を呼ぶ経路は P3-3B まで存在しない。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QSizeF
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from anp import app as app_module
from anp.core.paths import AppPaths
from anp.core.settings import Settings
from anp.pdf.document import DocumentController
from anp.storage import database
from anp.storage.study_mark import StudyMark
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.main_window import MainWindow
from anp.ui.pdf_view import PdfView
from anp.ui.study_mark_controller import StudyMarkController
from helpers import RecordingService


class RecordingRepository(StudyMarkRepository):
    """問い合わせ先を記録するリポジトリ。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        self.queried: list[str] = []

    def list_for_document(self, document_path: Path | str) -> list[StudyMark]:
        self.queried.append(str(document_path))
        return super().list_for_document(document_path)


class FailingRepository(StudyMarkRepository):
    """読み取りが失敗するリポジトリ。

    DB が壊れている状況を決定的に作るために使う。`sqlite3` の例外を使うのは、
    実際の失敗と同じ型で伝わることを確かめるため。`failing` を指定すると、
    その PDF の読み取りだけが失敗する（「PDF は開けたが DB だけ読めない」
    状況を作るため）。
    """

    def __init__(self, connection: sqlite3.Connection, failing: Path | None = None) -> None:
        super().__init__(connection)
        self._failing = failing

    def list_for_document(self, document_path: Path | str) -> list[StudyMark]:
        if self._failing is None or Path(document_path) == self._failing:
            msg = "no such table: study_marks"
            raise sqlite3.OperationalError(msg)
        return super().list_for_document(document_path)


@pytest.fixture
def doc() -> Iterator[DocumentController]:
    """開き直しに使い回すドキュメントコントローラ。"""
    controller = DocumentController()
    yield controller
    controller.close()


def show(view: PdfView, doc: DocumentController, path: Path) -> None:
    """`MainWindow.open_path()` と同じ順序でビューへ PDF を載せる。"""
    doc.open(path)
    view.set_document(doc.document, doc.page_sizes())


@pytest.fixture
def study_mark_controller(study_marks: StudyMarkRepository, view: PdfView) -> StudyMarkController:
    """テスト対象のコントローラ。ビューは実物を使う。"""
    return StudyMarkController(study_marks, view)


# ---------------------------------------------------------------- コントローラ
def test_no_active_document_at_startup(
    study_mark_controller: StudyMarkController, view: PdfView
) -> None:
    """起動直後は表示対象が無く、マークも空。"""
    assert study_mark_controller.active_document_path is None
    assert view.study_marks == ()


def test_activating_a_document_shows_its_marks(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """保存済みのマークが読み込まれてビューに載る。"""
    mark = study_marks.create(sample_pdf, 0, 0.25, 0.5)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    assert study_mark_controller.active_document_path == sample_pdf
    assert view.study_marks == (mark,)


def test_switching_documents_swaps_the_marks(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """A → B → A で、常に表示中の PDF のマークだけが出る。

    どちらのマークも `page_index = 0` に置く。ページ番号だけでマークを
    引いていると、この検証だけが落ちる。
    """
    a_mark = study_marks.create(sample_pdf, 0, 0.1, 0.1)
    b_mark = study_marks.create(two_page_pdf, 0, 0.9, 0.9)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)
    assert view.study_marks == (a_mark,)

    show(view, doc, two_page_pdf)
    study_mark_controller.activate_document(two_page_pdf)
    assert view.study_marks == (b_mark,)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)
    assert view.study_marks == (a_mark,)


def test_a_document_without_marks_shows_nothing(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
    single_page_pdf: Path,
) -> None:
    """マークが1件も無い PDF へ切り替えたら空になる。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    show(view, doc, single_page_pdf)
    study_mark_controller.activate_document(single_page_pdf)

    assert view.study_marks == ()


def test_clearing_the_document_releases_the_marks(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """表示対象を解除すると、対象もマークも無くなる。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    study_mark_controller.clear_document()

    assert study_mark_controller.active_document_path is None
    assert view.study_marks == ()


def test_refresh_without_a_document_leaves_the_view_empty(
    study_mark_controller: StudyMarkController, view: PdfView
) -> None:
    """表示対象が無いときの `refresh()` は空にするだけ。"""
    study_mark_controller.refresh()

    assert view.study_marks == ()


def test_refresh_reloads_the_active_document(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """`refresh()` は表示中の PDF の内容で丸ごと置き換える。

    P3-3B の更新後の反映はこの経路に載せる。
    """
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)
    assert list(view.study_marks) == []

    added = study_marks.create(sample_pdf, 1, 0.5, 0.5)
    study_mark_controller.refresh()

    assert view.study_marks == (added,)


def test_only_the_active_document_is_queried(
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """問い合わせるのは表示中の PDF だけ。"""
    recording = RecordingRepository(study_mark_connection)
    controller = StudyMarkController(recording, view)

    show(view, doc, sample_pdf)
    controller.activate_document(sample_pdf)

    assert recording.queried == [str(sample_pdf)]


def test_marks_are_passed_through_unchanged(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """重複・間違えた回数・メモを、コントローラが加工しない。

    同じ位置のマークをまとめたり、回数を「3回以上」へ丸めたりしない。
    """
    first = study_marks.create(sample_pdf, 0, 0.5, 0.5, note="メモ")
    study_marks.create(sample_pdf, 0, 0.5, 0.5)
    for _ in range(9):
        study_marks.increment_mistake_count(first.id)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(sample_pdf)

    marks = view.study_marks
    assert len(marks) == 2
    assert marks[0].mistake_count == 10
    assert marks[0].note == "メモ"
    assert marks[1].mistake_count == 1
    assert marks[1].note is None


def test_marks_outside_the_page_range_are_still_handed_over(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    single_page_pdf: Path,
) -> None:
    """存在しないページを指すマークも、そのままビューへ渡す。

    描画とヒットテストから外すのは `PdfView` の契約。同じ判断をここでも
    行うと、方針が2箇所に分かれる。
    """
    stale = study_marks.create(single_page_pdf, 7, 0.5, 0.5)

    show(view, doc, single_page_pdf)
    study_mark_controller.activate_document(single_page_pdf)

    assert view.study_marks == (stale,)


def test_a_read_failure_leaves_no_active_document(
    study_marks: StudyMarkRepository,
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """読み取りに失敗したら、その PDF を表示対象として確定しない。

    前の PDF のマークを新しい PDF へ出さないことに加えて、読めなかった
    PDF が表示対象のまま残らないことを固定する（fail-closed）。P3-3B の
    追加・更新は表示対象に対して行うので、「読めていない PDF が表示対象」
    という状態を作らせない。失敗を「マーク0件」として黙って飲み込まない
    ことも同時に確かめる。
    """
    a_mark = study_marks.create(sample_pdf, 0, 0.1, 0.1)
    controller = StudyMarkController(
        FailingRepository(study_mark_connection, failing=two_page_pdf), view
    )

    show(view, doc, sample_pdf)
    controller.activate_document(sample_pdf)
    assert view.study_marks == (a_mark,)

    show(view, doc, two_page_pdf)
    with pytest.raises(sqlite3.OperationalError):
        controller.activate_document(two_page_pdf)

    assert list(view.study_marks) == []
    assert controller.active_document_path is None


def test_a_read_failure_on_a_closed_connection_propagates(
    study_mark_controller: StudyMarkController,
    view: PdfView,
    doc: DocumentController,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
) -> None:
    """接続が閉じられていれば `sqlite3` の例外がそのまま出る。"""
    show(view, doc, sample_pdf)
    study_mark_connection.close()

    with pytest.raises(sqlite3.ProgrammingError):
        study_mark_controller.activate_document(sample_pdf)

    assert view.study_marks == ()
    assert study_mark_controller.active_document_path is None


def test_loading_marks_does_not_touch_the_rendering(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    service: RecordingService,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """マークの読み込みでは、レンダリング要求も倍率も表示位置も動かない。

    P2-2 / P3-2 の分離を統合後も守る。
    """
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    show(view, doc, sample_pdf)
    view.go_to_page(1)

    requests = len(service.requests)
    zoom = view.zoom
    mode = view.zoom_mode
    page = view.current_page
    scroll = view.verticalScrollBar().value()

    study_mark_controller.activate_document(sample_pdf)

    assert len(service.requests) == requests
    assert view.zoom == zoom
    assert view.zoom_mode is mode
    assert view.current_page == page
    assert view.verticalScrollBar().value() == scroll


# ---------------------------------------------------------------- ウィンドウ統合
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
    """`QMessageBox.warning` を捕まえて本文を集める（ダイアログを出さない）。"""
    messages: list[str] = []
    monkeypatch.setattr(
        "anp.ui.main_window.QMessageBox.warning",
        lambda *args: messages.append(args[2]),
    )
    return messages


def test_a_window_without_a_pdf_has_no_marks(window: MainWindow) -> None:
    """PDF を開いていなければ、表示対象もマークも無い。"""
    assert window.study_marks.active_document_path is None
    assert window.view.study_marks == ()


def test_opening_a_pdf_loads_its_marks(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """PDF を開くと、その PDF のマークが表示される。"""
    mark = study_marks.create(sample_pdf, 0, 0.25, 0.75)

    window.open_path(sample_pdf)

    assert window.study_marks.active_document_path == sample_pdf
    assert window.view.study_marks == (mark,)


def test_switching_pdfs_never_shows_the_other_documents_marks(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """A → B → A の切り替えで、常に表示中の PDF のマークだけが出る。"""
    a_mark = study_marks.create(sample_pdf, 0, 0.1, 0.1)
    b_mark = study_marks.create(two_page_pdf, 0, 0.9, 0.9)

    window.open_path(sample_pdf)
    assert window.view.study_marks == (a_mark,)

    window.open_path(two_page_pdf)
    assert window.view.study_marks == (b_mark,)
    assert window.study_marks.active_document_path == two_page_pdf

    window.open_path(sample_pdf)
    assert window.view.study_marks == (a_mark,)


def test_opening_a_pdf_without_marks_clears_the_previous_ones(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    single_page_pdf: Path,
) -> None:
    """マークの無い PDF を開いたら、前の PDF のマークは残らない。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    window.open_path(sample_pdf)

    window.open_path(single_page_pdf)

    assert window.view.study_marks == ()


def test_marks_are_loaded_after_the_new_document_is_in_place(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """新しい PDF のマークを載せるのは、ドキュメントを差し替えた後。

    差し替えた直後の状態が空であること、つまり「新しいページ＋古い PDF の
    マーク」という中間状態が無いことを固定する。
    """
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    study_marks.create(two_page_pdf, 0, 0.9, 0.9)
    window.open_path(sample_pdf)

    snapshots: list[tuple[StudyMark, ...]] = []
    original = window.view.set_document

    def recording(document: QPdfDocument, page_sizes: Sequence[QSizeF]) -> None:
        original(document, page_sizes)
        snapshots.append(window.view.study_marks)

    window.view.set_document = recording  # type: ignore[method-assign]
    window.open_path(two_page_pdf)

    assert snapshots == [()]
    assert len(window.view.study_marks) == 1


def test_a_failed_open_clears_the_active_document(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    broken_pdf: Path,
    warnings: list[str],
) -> None:
    """開けなかった PDF を表示対象にしない。表示とも食い違わせない。

    既存の失敗時の振る舞い（表示を空にする）に合わせ、学習マークだけ別の
    後始末をしない。
    """
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    window.open_path(sample_pdf)

    window.open_path(broken_pdf)

    assert window.study_marks.active_document_path is None
    assert window.view.study_marks == ()
    assert not window.view.has_document
    assert warnings


@pytest.mark.parametrize("bad", ["empty_pdf", "directory_pdf", "encrypted_pdf", "pageless_pdf"])
def test_every_kind_of_failed_open_clears_the_active_document(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    bad: str,
    warnings: list[str],
    request: pytest.FixtureRequest,
) -> None:
    """どの失敗の仕方でも、表示対象は残らない。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    window.open_path(sample_pdf)

    window.open_path(request.getfixturevalue(bad))

    assert window.study_marks.active_document_path is None
    assert window.view.study_marks == ()
    assert len(warnings) == 1


def test_closing_the_window_releases_the_active_document(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ウィンドウを閉じたら、表示対象もマークも手放す。"""
    study_marks.create(sample_pdf, 0, 0.1, 0.1)
    window.open_path(sample_pdf)

    window.close()

    assert window.study_marks.active_document_path is None
    assert window.view.study_marks == ()


def test_a_repository_failure_leaves_no_document_open(
    qtbot: QtBot,
    settings: Settings,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """PDF は開けても学習マークを読めなければ、その PDF も開いた状態にしない。

    「保存されているはずのものが消えたように見える」状態で読み進めさせない。
    後始末は PDF を開くのに失敗したときと同じ「PDF なし」の状態で、
    学習マークだけ別の失敗の仕方をしない。
    """
    repository = FailingRepository(study_mark_connection, failing=two_page_pdf)
    a_mark = repository.create(sample_pdf, 0, 0.1, 0.1)
    window = MainWindow(settings, repository)
    qtbot.addWidget(window)

    window.open_path(sample_pdf)
    assert window.view.study_marks == (a_mark,)

    with pytest.raises(sqlite3.OperationalError):
        window.open_path(two_page_pdf)

    assert window.study_marks.active_document_path is None
    assert list(window.view.study_marks) == []
    assert not window.view.has_document
    assert window.view.page_count == 0
    assert window.windowTitle() == "anp"
    assert window.statusBar().currentMessage() == "PDF が開かれていません"
    assert not window.reader_actions.next_page.isEnabled()
    assert not window.reader_actions.zoom_in.isEnabled()
    window.close()


def test_marks_survive_a_new_session(
    qtbot: QtBot, settings: Settings, tmp_path: Path, sample_pdf: Path
) -> None:
    """前のセッションで保存されたマークが、次のウィンドウで表示される。

    接続もリポジトリも作り直し、残っているのは DB ファイルだけにする。
    """
    db = tmp_path / "session.sqlite3"
    first = database.connect(db)
    try:
        StudyMarkRepository(first).create(sample_pdf, 0, 0.4, 0.6)
    finally:
        first.close()

    second = database.connect(db)
    try:
        window = MainWindow(settings, StudyMarkRepository(second))
        qtbot.addWidget(window)
        window.open_path(sample_pdf)

        assert len(window.view.study_marks) == 1
        assert window.view.study_marks[0].x_norm == pytest.approx(0.4)
        window.close()
    finally:
        second.close()


# ---------------------------------------------------------------- DB ライフサイクル
def test_the_application_opens_and_closes_one_connection(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """アプリケーションがデータ用の DB を1本だけ開き、終了時に閉じる。

    `QApplication`・ログ・例外フック・設定は、実行環境を汚さないよう
    差し替える。確かめたいのは接続の所有と寿命だけ。
    """
    monkeypatch.setattr(app_module, "QApplication", lambda _argv: qapp)
    monkeypatch.setattr(app_module, "setup_logging", lambda _path: None)
    monkeypatch.setattr(app_module, "_install_excepthook", lambda: None)
    monkeypatch.setattr(
        app_module,
        "QSettings",
        lambda: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
    )
    monkeypatch.setattr(
        AppPaths,
        "from_standard_paths",
        classmethod(lambda _cls, _name: AppPaths(data_dir=tmp_path / "data")),
    )
    monkeypatch.setattr(qapp, "exec", lambda: 0)

    opened: list[sqlite3.Connection] = []
    real_connect = database.connect

    def recording_connect(path: Path) -> sqlite3.Connection:
        connection = real_connect(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(app_module.database, "connect", recording_connect)

    assert app_module.main() == 0

    assert len(opened) == 1
    assert (tmp_path / "data" / "anp.sqlite3").is_file()
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_the_database_lives_next_to_the_log(tmp_path: Path) -> None:
    """DB はログと同じアプリケーションデータの場所に置く。"""
    paths = AppPaths(data_dir=tmp_path)

    assert paths.database_file == tmp_path / "anp.sqlite3"
    assert paths.database_file.parent == paths.log_file.parent.parent
