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
from PySide6.QtCore import QLockFile, QSettings, QSizeF
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication, QMessageBox
from pytestqt.qtbot import QtBot

from anp import app as app_module
from anp.core.fingerprint import file_fingerprint
from anp.core.paths import AppPaths
from anp.core.settings import Settings
from anp.pdf import document as document_module
from anp.pdf.document import DocumentController
from anp.storage import database
from anp.storage import study_mark as study_mark_module
from anp.storage.study_mark import DocumentIdentity, StudyMark, document_key
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.fatal import EXIT_INTERNAL_ERROR
from anp.ui.main_window import MainWindow
from anp.ui.pdf_view import PdfView
from anp.ui.study_mark_controller import StudyMarkController, StudyMarkLoadError
from anp.ui.study_marks import PagePosition
from conftest import FatalCalls
from helpers import RecordingRepository, RecordingService


class FailingRepository(StudyMarkRepository):
    """読み取りが失敗するリポジトリ。

    DB が壊れている状況を決定的に作るために使う。`sqlite3` の例外を使うのは、
    実際の失敗と同じ型で伝わることを確かめるため。`failing` を指定すると、
    その PDF の読み取りだけが失敗する（「PDF は開けたが DB だけ読めない」
    状況を作るため）。
    """

    def __init__(self, connection: sqlite3.Connection, failing: Path | None = None) -> None:
        super().__init__(connection)
        self._failing = None if failing is None else DocumentIdentity.of(failing)

    def list_for_document(self, document: DocumentIdentity) -> list[StudyMark]:
        if self._failing is None or document == self._failing:
            msg = "no such table: study_marks"
            raise sqlite3.OperationalError(msg)
        return super().list_for_document(document)


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
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.25, 0.5)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))

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
    a_mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    b_mark = study_marks.create(DocumentIdentity.of(two_page_pdf), 0, 0.9, 0.9)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))
    assert view.study_marks == (a_mark,)

    show(view, doc, two_page_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(two_page_pdf))
    assert view.study_marks == (b_mark,)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))
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
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))

    show(view, doc, single_page_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(single_page_pdf))

    assert view.study_marks == ()


def test_clearing_the_document_releases_the_marks(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
) -> None:
    """表示対象を解除すると、対象もマークも無くなる。"""
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))

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
    study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))
    assert list(view.study_marks) == []

    added = study_marks.create(DocumentIdentity.of(sample_pdf), 1, 0.5, 0.5)
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
    controller.activate_document(DocumentIdentity.of(sample_pdf))

    assert recording.queried == [document_key(sample_pdf)]


def test_the_identity_is_fixed_when_the_document_is_opened(
    study_mark_connection: sqlite3.Connection,
    view: PdfView,
    doc: DocumentController,
    tmp_path: Path,
    sample_pdf: Path,
    two_page_pdf: Path,
) -> None:
    """開いた後に同じパスの中身が入れ替わっても、保存先は開いた PDF のまま。

    操作のたびにパスから指紋を計算し直すと、表示しているのは A なのに、
    ディスク上の B のマークとして保存される（画面に見えない場所へ、
    A の座標が記録される）。
    """
    path = tmp_path / "swapped.pdf"
    path.write_bytes(sample_pdf.read_bytes())
    original = DocumentIdentity.of(path)
    repository = StudyMarkRepository(study_mark_connection)
    controller = StudyMarkController(repository, view)
    show(view, doc, path)
    controller.activate_document(DocumentIdentity.of(path))

    # 表示したまま、同じパスの中身が別の PDF に置き換わった。
    path.write_bytes(two_page_pdf.read_bytes())
    controller.create_mark(
        PagePosition(page_index=0, x_norm=0.5, y_norm=0.5),
        expected_document=controller.active_document,
    )

    assert len(repository.list_for_document(original)) == 1
    assert repository.list_for_document(DocumentIdentity.of(path)) == []
    assert len(controller.study_marks) == 1


def test_the_controller_records_the_fingerprint_of_what_it_loaded(
    doc: DocumentController, sample_pdf: Path
) -> None:
    """`DocumentController` は、読み込んだ内容の指紋を持って返る。"""
    assert doc.content_fingerprint is None

    doc.open(sample_pdf)

    assert doc.content_fingerprint == file_fingerprint(sample_pdf)

    doc.close()
    assert doc.content_fingerprint is None


def test_opening_does_not_reread_the_file_for_the_identity(
    qtbot: QtBot,
    settings: Settings,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持ち主の指紋は、読み込みの直後に取った値をそのまま使う。

    開いた後でファイルを読み直して指紋を取ると、その隙に同じパスが別の
    内容へ置き換わったときに「表示は A、持ち主は B」になる。読み直しの
    経路（`DocumentIdentity.of()`）を通ったら失敗するようにして固定する。
    """

    def forbidden(_path: Path | str) -> str:
        msg = "the identity must come from the load, not from a second read"
        raise AssertionError(msg)

    monkeypatch.setattr(study_mark_module, "file_fingerprint", forbidden)

    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)
    try:
        window.open_path(sample_pdf)

        assert window.view.has_document
        assert window.study_marks.active_document_path == sample_pdf
    finally:
        window.close()


def test_the_fingerprint_is_read_once_per_open(
    study_mark_controller: StudyMarkController,
    study_marks: StudyMarkRepository,
    view: PdfView,
    doc: DocumentController,
    sample_pdf: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内容の指紋を計算するのは、開いたときの1回だけ。

    マークを作るたびにファイル全体を読み直すと、数百 MB の PDF では
    Ctrl + クリックのたびに画面が固まる。
    """
    reads: list[Path] = []
    original = document_module.file_fingerprint

    def counting(path: Path | str) -> str:
        reads.append(Path(path))
        return original(path)

    # 指紋を取りうる2箇所（PDF を開いたとき・同一性を作るとき）を両方数える。
    monkeypatch.setattr(document_module, "file_fingerprint", counting)
    monkeypatch.setattr(study_mark_module, "file_fingerprint", counting)

    show(view, doc, sample_pdf)
    fingerprint = doc.content_fingerprint
    assert fingerprint is not None
    study_mark_controller.activate_document(DocumentIdentity.for_content(sample_pdf, fingerprint))
    assert len(reads) == 1, "開いたときの1回だけのはず"

    for index in range(3):
        study_mark_controller.create_mark(
            PagePosition(page_index=0, x_norm=0.1 * (index + 1), y_norm=0.5),
            expected_document=study_mark_controller.active_document,
        )

    assert len(reads) == 1, "マークを作るたびに読み直している"
    assert len(study_mark_controller.study_marks) == 3


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
    first = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5, note="メモ")
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    for _ in range(9):
        study_marks.increment_mistake_count(first.id)

    show(view, doc, sample_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))

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
    stale = study_marks.create(DocumentIdentity.of(single_page_pdf), 7, 0.5, 0.5)

    show(view, doc, single_page_pdf)
    study_mark_controller.activate_document(DocumentIdentity.of(single_page_pdf))

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
    a_mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    controller = StudyMarkController(
        FailingRepository(study_mark_connection, failing=two_page_pdf), view
    )

    show(view, doc, sample_pdf)
    controller.activate_document(DocumentIdentity.of(sample_pdf))
    assert view.study_marks == (a_mark,)

    show(view, doc, two_page_pdf)
    with pytest.raises(StudyMarkLoadError):
        controller.activate_document(DocumentIdentity.of(two_page_pdf))

    assert list(view.study_marks) == []
    assert controller.active_document_path is None


def test_a_read_failure_on_a_closed_connection_propagates(
    study_mark_controller: StudyMarkController,
    view: PdfView,
    doc: DocumentController,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
) -> None:
    """接続が閉じられていれば、読み込み失敗として伝わる。"""
    show(view, doc, sample_pdf)
    study_mark_connection.close()

    with pytest.raises(StudyMarkLoadError):
        study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))

    assert view.study_marks == ()
    assert study_mark_controller.active_document_path is None


def test_a_read_failure_keeps_the_original_error_as_the_cause(
    study_mark_controller: StudyMarkController,
    view: PdfView,
    doc: DocumentController,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
) -> None:
    """包んでも原因は失われない。

    利用者に見せるのは日本語の一言だけだが、調査に必要なのは元の例外なので
    `__cause__` に残す（ログにも stacktrace が出る）。
    """
    show(view, doc, sample_pdf)
    study_mark_connection.close()

    with pytest.raises(StudyMarkLoadError) as error:
        study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))

    assert isinstance(error.value.__cause__, sqlite3.ProgrammingError)


def test_a_broken_stored_row_is_a_load_failure(
    study_mark_controller: StudyMarkController,
    view: PdfView,
    doc: DocumentController,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
) -> None:
    """保存されていた行がドメインの契約を満たさなくても、読み込み失敗として扱う。

    `sqlite3` の例外だけを包むのでは足りない。マイグレーションの取りこぼしや
    外部からの書き込みで壊れた行があった場合も、利用者から見れば「この PDF の
    マークを読めなかった」で、扱いは同じ。
    """
    show(view, doc, sample_pdf)
    # CHECK 制約を通る値だけで、ドメインが受け付けない行を作る。TEXT 列でも
    # BLOB はそのまま入るので、note が文字列でない行になる。
    identity = DocumentIdentity.of(sample_pdf)
    study_mark_connection.execute(
        "INSERT INTO study_marks"
        " (document_key, document_fingerprint, page_index, x_norm, y_norm, note)"
        " VALUES (?, ?, 0, 0.5, 0.5, x'414243')",
        (identity.key, identity.fingerprint),
    )

    with pytest.raises(StudyMarkLoadError):
        study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))

    assert study_mark_controller.active_document_path is None


def test_a_programming_error_is_not_reported_as_a_load_failure(
    view: PdfView,
    doc: DocumentController,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
) -> None:
    """実装の誤りは「学習マークを読み込めません」に化けさせない。

    広く捕まえて包むと、`AttributeError` のようなバグが想定内の失敗の
    見た目になり、fail-fast が効かなくなる。
    """

    class BuggyRepository(StudyMarkRepository):
        def list_for_document(self, document: DocumentIdentity) -> list[StudyMark]:
            raise AttributeError(document.key)

    controller = StudyMarkController(BuggyRepository(study_mark_connection), view)
    show(view, doc, sample_pdf)

    with pytest.raises(AttributeError):
        controller.activate_document(DocumentIdentity.of(sample_pdf))


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
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    show(view, doc, sample_pdf)
    view.go_to_page(1)

    requests = len(service.requests)
    zoom = view.zoom
    mode = view.zoom_mode
    page = view.current_page
    scroll = view.verticalScrollBar().value()

    study_mark_controller.activate_document(DocumentIdentity.of(sample_pdf))

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


class AdoptPrompt:
    """古い形式のマークの問いかけを捕まえて、答えをテストから決める。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.asked: list[str] = []
        self.accept = False
        monkeypatch.setattr("anp.ui.study_mark_interaction.QMessageBox.question", self._question)

    def _question(self, *args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        self.asked.append(str(args[2]))
        return QMessageBox.StandardButton.Yes if self.accept else QMessageBox.StandardButton.No


@pytest.fixture
def adopt_prompt(monkeypatch: pytest.MonkeyPatch) -> AdoptPrompt:
    """古い形式のマークの問いかけ。既定は「いいえ」で答える。"""
    return AdoptPrompt(monkeypatch)


def make_unverified(connection: sqlite3.Connection) -> None:
    """保存済みのマークを、マイグレーション2 より前の形（指紋なし）へ戻す。"""
    connection.execute("UPDATE study_marks SET document_fingerprint = NULL")


def test_old_marks_without_a_fingerprint_are_not_shown_silently(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    adopt_prompt: AdoptPrompt,
) -> None:
    """指紋を持たない古いマークは、尋ねて断られたら表示しない。

    どの内容の PDF に付けられたか分からないものを普通のマークと同じ顔で
    並べると、差し替えた PDF に前の本のマークが乗る。
    """
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.25, 0.75)
    make_unverified(study_mark_connection)

    window.open_path(sample_pdf)

    assert len(adopt_prompt.asked) == 1
    assert "1 件" in adopt_prompt.asked[0]
    assert window.view.study_marks == ()
    assert window.study_marks.unverified_count == 1
    # 断っても消さない。次に開けばまた尋ねる。
    assert study_mark_connection.execute("SELECT COUNT(*) FROM study_marks").fetchone()[0] == 1


def test_accepting_adopts_the_old_marks(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    adopt_prompt: AdoptPrompt,
) -> None:
    """「はい」と答えれば、この PDF のマークとして表示される。"""
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.25, 0.75)
    make_unverified(study_mark_connection)
    adopt_prompt.accept = True

    window.open_path(sample_pdf)

    assert len(window.view.study_marks) == 1
    assert window.study_marks.unverified_count == 0

    # 引き取ったあとは、もう尋ねない。
    window.open_path(sample_pdf)
    assert len(adopt_prompt.asked) == 1


def test_nothing_is_asked_without_old_marks(
    window: MainWindow,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    adopt_prompt: AdoptPrompt,
) -> None:
    """指紋のあるマークしか無ければ、問いかけは出ない。"""
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.25, 0.75)

    window.open_path(sample_pdf)

    assert adopt_prompt.asked == []
    assert len(window.view.study_marks) == 1


def test_a_window_without_a_pdf_has_no_marks(window: MainWindow) -> None:
    """PDF を開いていなければ、表示対象もマークも無い。"""
    assert window.study_marks.active_document_path is None
    assert window.view.study_marks == ()


def test_opening_a_pdf_loads_its_marks(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """PDF を開くと、その PDF のマークが表示される。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.25, 0.75)

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
    a_mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    b_mark = study_marks.create(DocumentIdentity.of(two_page_pdf), 0, 0.9, 0.9)

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
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
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
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    study_marks.create(DocumentIdentity.of(two_page_pdf), 0, 0.9, 0.9)
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
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
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
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    window.open_path(sample_pdf)

    window.open_path(request.getfixturevalue(bad))

    assert window.study_marks.active_document_path is None
    assert window.view.study_marks == ()
    assert len(warnings) == 1


def test_closing_the_window_releases_the_active_document(
    window: MainWindow, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ウィンドウを閉じたら、表示対象もマークも手放す。"""
    study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
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
    warnings: list[str],
) -> None:
    """PDF は開けても学習マークを読めなければ、その PDF も開いた状態にしない。

    「保存されているはずのものが消えたように見える」状態で読み進めさせない。
    後始末は PDF を開くのに失敗したときと同じ「PDF なし」の状態で、
    学習マークだけ別の失敗の仕方をしない。
    """
    repository = FailingRepository(study_mark_connection, failing=two_page_pdf)
    a_mark = repository.create(DocumentIdentity.of(sample_pdf), 0, 0.1, 0.1)
    window = MainWindow(settings, repository)
    qtbot.addWidget(window)

    window.open_path(sample_pdf)
    assert window.view.study_marks == (a_mark,)

    window.open_path(two_page_pdf)

    assert window.study_marks.active_document_path is None
    assert list(window.view.study_marks) == []
    assert not window.view.has_document
    assert window.view.page_count == 0
    assert window.windowTitle() == "anp"
    assert window.document_status_text == "PDF が開かれていません"
    assert not window.reader_actions.next_page.isEnabled()
    assert not window.reader_actions.zoom_in.isEnabled()
    assert len(warnings) == 1
    window.close()


def test_a_repository_failure_is_reported_as_a_study_mark_failure(
    qtbot: QtBot,
    settings: Settings,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    warnings: list[str],
) -> None:
    """学習マークを読めなかったことが、利用者に日本語で伝わる。

    これは **想定された失敗経路** なので、アプリケーション境界の
    「予期しないエラー」ダイアログには送らない。記録そのものは消えて
    いないことも伝える（利用者がいちばん気にするのはそこ）。
    """
    window = MainWindow(settings, FailingRepository(study_mark_connection))
    qtbot.addWidget(window)

    window.open_path(sample_pdf)

    assert len(warnings) == 1
    assert "学習マーク" in warnings[0]
    assert sample_pdf.name in warnings[0]
    assert "削除されていません" in warnings[0]
    window.close()


def test_an_unexpected_failure_while_opening_stops_the_application(
    qtbot: QtBot,
    settings: Settings,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    warnings: list[str],
    fatal: FatalCalls,
) -> None:
    """実装の誤りは警告にすり替えず、その場で終了させる。

    読み込み失敗を1つの型に包むのは、この境界を作るため。`open_path()` は
    Qt から直に呼ばれるので、例外を外へ出さずに fail-stop する。後始末だけは
    読み込み失敗と同じで、開きかけの PDF を残さない。
    """
    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)

    def boom(_document: object) -> None:
        msg = "programming error"
        raise AttributeError(msg)

    window.study_marks.activate_document = boom  # type: ignore[assignment]

    window.open_path(sample_pdf)

    assert not window.view.has_document
    assert window.study_marks.active_document_path is None
    assert warnings == [], "実装の誤りが通常の警告に化けている"
    assert fatal.exit_codes == [EXIT_INTERNAL_ERROR]
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
        StudyMarkRepository(first).create(DocumentIdentity.of(sample_pdf), 0, 0.4, 0.6)
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


def test_a_second_instance_does_not_open_the_database(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既に起動していたら、2つ目は DB もウィンドウも開かずに終わる。

    `StudyMarkController` は「表示中のスナップショットと DB が一致する」を
    前提に、更新のたびの読み直しを省いている。書き手が2つになるとその
    前提が崩れ、片方の画面が古い件数を出したままになる。
    """
    data = tmp_path / "data"
    data.mkdir(parents=True)
    monkeypatch.setattr(app_module, "QApplication", lambda _argv: qapp)
    monkeypatch.setattr(app_module, "setup_logging", lambda _path: None)
    monkeypatch.setattr(app_module, "_install_excepthook", lambda: None)
    monkeypatch.setattr(
        AppPaths,
        "from_standard_paths",
        classmethod(lambda _cls, _name: AppPaths(data_dir=data)),
    )
    informed: list[str] = []
    monkeypatch.setattr(
        app_module.QMessageBox,
        "information",
        lambda *args: informed.append(str(args[2])),
    )

    # 先に起動しているプロセスの代わりに、同じロックを取っておく。
    held = QLockFile(str(AppPaths(data_dir=data).lock_file))
    assert held.tryLock(0), "テストの前提が崩れている（ロックを取れない）"
    try:
        connections: list[object] = []
        monkeypatch.setattr(app_module.database, "connect", lambda path: connections.append(path))

        assert app_module.main() == app_module.EXIT_ALREADY_RUNNING

        assert connections == []
        assert len(informed) == 1
        assert "既に起動" in informed[0]
    finally:
        held.unlock()


def test_a_lock_that_cannot_be_created_is_not_reported_as_a_second_instance(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ロックを作れないのと、既に起動しているのを混同しない。

    まとめて「既に起動しています」と伝えると、書き込めないディレクトリが
    原因のときに、利用者が居もしない別のウィンドウを探すことになる。
    """
    data = tmp_path / "data"
    monkeypatch.setattr(app_module, "QApplication", lambda _argv: qapp)
    monkeypatch.setattr(app_module, "setup_logging", lambda _path: None)
    monkeypatch.setattr(app_module, "_install_excepthook", lambda: None)
    monkeypatch.setattr(
        AppPaths,
        "from_standard_paths",
        classmethod(lambda _cls, _name: AppPaths(data_dir=data)),
    )
    monkeypatch.setattr(QLockFile, "tryLock", lambda _self, _timeout: False)
    monkeypatch.setattr(QLockFile, "error", lambda _self: QLockFile.LockError.PermissionError)

    informed: list[str] = []
    critical: list[str] = []
    monkeypatch.setattr(
        app_module.QMessageBox, "information", lambda *args: informed.append(str(args[2]))
    )
    monkeypatch.setattr(
        app_module.QMessageBox, "critical", lambda *args: critical.append(str(args[2]))
    )

    assert app_module.main() == app_module.EXIT_LOCK_FAILED

    assert informed == []
    assert len(critical) == 1
    assert "ロックファイル" in critical[0]


def test_the_lock_is_not_treated_as_stale_over_time(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ロックは経過時間では無効にならない（起動中ずっと持ち続けるため）。

    Qt の既定（30 秒）のままだと、長く開いているだけのウィンドウが
    「古いロック」と判断され、2つ目が起動できてしまう。
    """
    data = tmp_path / "data"
    data.mkdir(parents=True)
    monkeypatch.setattr(app_module, "QApplication", lambda _argv: qapp)
    monkeypatch.setattr(app_module, "setup_logging", lambda _path: None)
    monkeypatch.setattr(app_module, "_install_excepthook", lambda: None)
    monkeypatch.setattr(
        AppPaths,
        "from_standard_paths",
        classmethod(lambda _cls, _name: AppPaths(data_dir=data)),
    )

    stale: list[int] = []
    original = QLockFile.setStaleLockTime

    def record(self: QLockFile, value: int) -> None:
        stale.append(value)
        original(self, value)

    monkeypatch.setattr(QLockFile, "setStaleLockTime", record)
    monkeypatch.setattr(qapp, "exec", lambda: 0)
    monkeypatch.setattr(
        app_module,
        "QSettings",
        lambda: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
    )

    assert app_module.main() == 0

    assert stale == [0]


def test_the_lock_is_released_when_the_application_exits(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """終了したら、次の起動はロックを取れる。"""
    data = tmp_path / "data"
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
        classmethod(lambda _cls, _name: AppPaths(data_dir=data)),
    )
    monkeypatch.setattr(qapp, "exec", lambda: 0)

    assert app_module.main() == 0

    after = QLockFile(str(AppPaths(data_dir=data).lock_file))
    try:
        assert after.tryLock(0)
    finally:
        after.unlock()


def test_the_database_lives_next_to_the_log(tmp_path: Path) -> None:
    """DB はログと同じアプリケーションデータの場所に置く。"""
    paths = AppPaths(data_dir=tmp_path)

    assert paths.database_file == tmp_path / "anp.sqlite3"
    assert paths.database_file.parent == paths.log_file.parent.parent
