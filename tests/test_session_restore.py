"""セッションの復元と最近使ったファイル（P5-1）のテスト。

確かめるのは4つ。

- 最近使ったファイルが MRU で、正常に開けたときだけ増えること
- 履歴からの起動が、通常の「開く」とまったく同じ手順を通ること
- 前回読んでいた PDF とその位置が、次回の起動で戻ること
- 壊れた設定・消えたファイル・読めない学習マークで起動が止まらないこと

読書位置は正規化座標なので、固定のスクロール量では比べない。ウィンドウを
作り直す検証は、実際に同じ INI を使う2つの `MainWindow` で行う。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.pdf.document import DocumentController
from anp.storage.study_mark import DocumentIdentity
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.main_window import MainWindow
from anp.ui.pdf_view import ZoomMode
from anp.ui.recent_files import MAX_RECENT_FILES
from helpers import BrokenRepository


@pytest.fixture
def ini(tmp_path: Path) -> str:
    """2つのウィンドウが共有する設定ファイル。"""
    return str(tmp_path / "settings.ini")


@pytest.fixture
def backend(ini: str) -> QSettings:
    """テストから設定を直接仕込む/覗くための入口。"""
    return QSettings(ini, QSettings.Format.IniFormat)


def set_session(
    backend: QSettings, document: object, *, page: object = 0, y_norm: object = 0.0
) -> None:
    """前回のセッションを直接仕込む（壊れた値も含めてそのまま書く）。

    3つの値は1つの鍵に JSON でまとまっているので、テストからも同じ形で
    書き込む。書いたら `sync()` するのは呼び出し側。
    """
    backend.setValue(
        "session/last",
        json.dumps({"document": document, "page_index": page, "y_norm": y_norm}),
    )


def make_window(
    qtbot: QtBot, ini: str, repository: StudyMarkRepository, *, size: tuple[int, int] | None = None
) -> MainWindow:
    """設定を読み込んで表示済みのウィンドウを作る。

    表示するのは、セッションの復元がビューポートの大きさの決まった
    `showEvent()` で走るため。待ち合わせは `waitExposed` だけで、
    固定時間の sleep は使わない。
    """
    window = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), repository)
    qtbot.addWidget(window)
    if size is not None:
        window.resize(*size)
    with qtbot.waitExposed(window):
        window.show()
    return window


@pytest.fixture
def window(qtbot: QtBot, ini: str, study_marks: StudyMarkRepository) -> Iterator[MainWindow]:
    """表示済みのウィンドウ1つ。"""
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


@pytest.fixture
def other_pdf(sample_pdf: Path, tmp_path: Path) -> Path:
    """`sample_pdf` と同じ内容の別ファイル。履歴の並びを見るために使う。"""
    return Path(shutil.copy(sample_pdf, tmp_path / "other.pdf"))


def recent_actions(window: MainWindow) -> list[QAction]:
    """履歴のメニュー項目（区切りと「クリア」を除く）。"""
    return [
        action
        for action in window.recent_menu.actions()
        if not action.isSeparator() and action is not window.reader_actions.clear_recent
    ]


def recent_texts(window: MainWindow) -> list[str]:
    return [action.text() for action in recent_actions(window)]


# ---------------------------------------------------------------- 履歴の更新
def test_a_successful_open_is_recorded(window: MainWindow, sample_pdf: Path) -> None:
    """正常に開けた PDF が履歴に載る。"""
    window.open_path(sample_pdf)

    assert window.recent_files == (sample_pdf,)


def test_the_history_is_most_recently_used(
    window: MainWindow, sample_pdf: Path, other_pdf: Path
) -> None:
    """A → B の順に開いたら [B, A]。"""
    window.open_path(sample_pdf)
    window.open_path(other_pdf)

    assert window.recent_files == (other_pdf, sample_pdf)


def test_reopening_moves_an_entry_to_the_front(
    window: MainWindow, sample_pdf: Path, other_pdf: Path
) -> None:
    """A → B → A の順なら [A, B]。重複はできない。"""
    window.open_path(sample_pdf)
    window.open_path(other_pdf)
    window.open_path(sample_pdf)

    assert window.recent_files == (sample_pdf, other_pdf)


def test_the_history_is_bounded(qtbot: QtBot, window: MainWindow, sample_pdf: Path) -> None:
    """上限を超えて開くと、いちばん古い項目が落ちる。"""
    copies = [
        Path(shutil.copy(sample_pdf, sample_pdf.parent / f"book{index}.pdf"))
        for index in range(MAX_RECENT_FILES + 1)
    ]
    for path in copies:
        window.open_path(path)

    assert len(window.recent_files) == MAX_RECENT_FILES
    assert window.recent_files[0] == copies[-1]
    assert copies[0] not in window.recent_files
    assert copies[1] in window.recent_files


@pytest.mark.parametrize(
    "bad", ["broken_pdf", "empty_pdf", "directory_pdf", "encrypted_pdf", "pageless_pdf"]
)
def test_a_failed_open_is_not_recorded(
    window: MainWindow,
    sample_pdf: Path,
    bad: str,
    warnings: list[str],
    request: pytest.FixtureRequest,
) -> None:
    """開けなかった PDF は履歴に載らない。開けていた分はそのまま残る。"""
    window.open_path(sample_pdf)

    window.open_path(request.getfixturevalue(bad))

    assert window.recent_files == (sample_pdf,)
    assert len(warnings) == 1


def test_a_missing_file_is_not_recorded(
    window: MainWindow, tmp_path: Path, warnings: list[str]
) -> None:
    """存在しないファイルも履歴に載らない。"""
    window.open_path(tmp_path / "no_such_file.pdf")

    assert window.recent_files == ()
    assert warnings


def test_a_study_mark_failure_is_not_recorded(
    qtbot: QtBot,
    ini: str,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    warnings: list[str],
) -> None:
    """PDF は開けても学習マークを読めなければ履歴に載らない。

    開く操作全体が成功したときだけ履歴を更新する契約（fail-closed の
    後始末で PDF なしへ戻るので、履歴にだけ残るのはおかしい）。
    """
    window = make_window(qtbot, ini, BrokenRepository(study_mark_connection, failing="list"))

    window.open_path(sample_pdf)

    assert not window.view.has_document
    assert window.recent_files == ()
    assert len(warnings) == 1
    window.close()


# ---------------------------------------------------------------- 履歴のメニュー
def test_the_recent_menu_is_in_the_file_menu(window: MainWindow) -> None:
    """「ファイル」メニューに履歴のサブメニューがある。

    `QAction.menu()` は使わない。取り出した `QAction` の Python 側の参照が
    先に消えると、PySide がメニューの C++ オブジェクトを解放してしまう。
    """
    menus = [menu for menu in window.findChildren(QMenu) if menu.title() == "ファイル(&F)"]
    assert len(menus) == 1

    texts = [action.text() for action in menus[0].actions()]

    assert texts == ["開く(&O)...", "最近使ったファイル(&R)", "", "終了(&X)"]
    assert window.recent_menu.menuAction() in menus[0].actions()


def test_the_empty_recent_menu_says_so(window: MainWindow) -> None:
    """履歴が空なら、押せない案内を1つだけ出す。"""
    actions = window.recent_menu.actions()

    assert len(actions) == 1
    assert actions[0].text() == "最近使ったファイルはありません"
    assert not actions[0].isEnabled()


def test_the_recent_menu_follows_the_history(
    window: MainWindow, sample_pdf: Path, other_pdf: Path
) -> None:
    """履歴が変わるとメニューも作り直される。古い項目は残らない。"""
    window.open_path(sample_pdf)
    window.open_path(other_pdf)

    assert recent_texts(window) == [other_pdf.name, sample_pdf.name]
    assert window.reader_actions.clear_recent in window.recent_menu.actions()


def test_a_recent_item_carries_the_path_not_the_label(window: MainWindow, sample_pdf: Path) -> None:
    """項目はフルパスを持つ（表示文字列から逆算しない）。"""
    window.open_path(sample_pdf)

    action = recent_actions(window)[0]

    assert action.text() == sample_pdf.name
    assert action.statusTip() == str(sample_pdf)
    assert action.toolTip() == str(sample_pdf)


def test_clicking_a_recent_item_opens_it(
    window: MainWindow, sample_pdf: Path, other_pdf: Path, study_marks: StudyMarkRepository
) -> None:
    """履歴からの起動も通常の「開く」と同じ手順を通る。

    ドキュメント・学習マーク・一覧のどれも、専用の経路ではなく
    `open_path()` から載る。
    """
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 1, 0.5, 0.5)
    window.open_path(sample_pdf)
    window.open_path(other_pdf)

    recent_actions(window)[1].trigger()

    assert window.view.has_document
    assert window.document_status_text == str(sample_pdf)
    assert window.study_marks.active_document_path == sample_pdf
    assert window.view.study_marks == (mark,)
    assert window.study_mark_sidebar.rows == (mark,)
    # 履歴からの起動でも MRU の先頭へ来る。
    assert window.recent_files == (sample_pdf, other_pdf)


def test_a_recent_item_is_connected_only_once(
    window: MainWindow, sample_pdf: Path, other_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """メニューを作り直しても接続が二重にならない。

    二重に繋がっていれば、1回のクリックで2回開くことになる。
    """
    window.open_path(sample_pdf)
    window.open_path(other_pdf)
    window.open_path(sample_pdf)

    opened: list[Path] = []
    original = DocumentController.open

    def recording(controller: DocumentController, path: Path) -> None:
        opened.append(path)
        original(controller, path)

    monkeypatch.setattr(DocumentController, "open", recording)

    recent_actions(window)[0].trigger()

    assert opened == [sample_pdf]


def test_a_missing_recent_entry_is_removed_after_a_failure(
    qtbot: QtBot,
    ini: str,
    backend: QSettings,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    warnings: list[str],
) -> None:
    """消えたファイルは、いつもの失敗を知らせたうえで履歴から外す。

    他の項目は残す。
    """
    missing = sample_pdf.parent / "gone.pdf"
    backend.setValue("files/recent", [str(missing), str(sample_pdf)])
    backend.sync()
    window = make_window(qtbot, ini, study_marks)

    try:
        recent_actions(window)[0].trigger()

        assert warnings and "ファイルが見つかりません" in warnings[0]
        assert not window.view.has_document
        assert window.recent_files == (sample_pdf,)
        assert recent_texts(window) == [sample_pdf.name]
    finally:
        window.close()


def test_a_broken_recent_entry_is_kept(
    qtbot: QtBot,
    ini: str,
    backend: QSettings,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    broken_pdf: Path,
    warnings: list[str],
) -> None:
    """開けないだけの項目は履歴に残す（差し替えれば次は開ける）。

    「存在しない」と「開けない」を区別する。
    """
    backend.setValue("files/recent", [str(broken_pdf), str(sample_pdf)])
    backend.sync()
    window = make_window(qtbot, ini, study_marks)

    try:
        recent_actions(window)[0].trigger()

        assert warnings and "PDF として読み取れない" in warnings[0]
        assert not window.view.has_document
        assert window.recent_files == (broken_pdf, sample_pdf)
    finally:
        window.close()


def test_clearing_the_recent_files_keeps_everything_else(
    window: MainWindow, sample_pdf: Path, backend: QSettings, study_marks: StudyMarkRepository
) -> None:
    """クリアは履歴だけを空にする。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 0, 0.5, 0.5)
    window.open_path(sample_pdf)
    last_directory = window._settings.last_directory  # noqa: SLF001

    window.reader_actions.clear_recent.trigger()

    assert window.recent_files == ()
    assert recent_texts(window) == ["最近使ったファイルはありません"]
    # 開いている PDF・学習マーク・最後のディレクトリは触らない。
    assert window.view.has_document
    assert window.view.study_marks == (mark,)
    assert window._settings.last_directory == last_directory  # noqa: SLF001
    assert study_marks.get(mark.id) == mark


def test_clearing_does_not_forget_the_session(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """クリアしても、次回の自動復元は生きている。"""
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    first.reader_actions.clear_recent.trigger()
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        assert second.view.has_document
        assert second.recent_files == ()
    finally:
        second.close()


def test_a_persisted_history_is_normalized_on_startup(
    qtbot: QtBot, ini: str, backend: QSettings, study_marks: StudyMarkRepository, tmp_path: Path
) -> None:
    """設定に重複や上限超えが入っていても、読み込んだ時点で契約の形にする。

    次に PDF を開くまで20件並んだままにしない。
    """
    stored = [str(tmp_path / f"{index}.pdf") for index in range(MAX_RECENT_FILES + 5)]
    stored.insert(1, stored[0])
    backend.setValue("files/recent", stored)
    backend.sync()

    window = make_window(qtbot, ini, study_marks)
    try:
        assert len(window.recent_files) == MAX_RECENT_FILES
        assert len(set(window.recent_files)) == MAX_RECENT_FILES
        assert len(recent_actions(window)) == MAX_RECENT_FILES
        assert window.recent_files[0] == tmp_path / "0.pdf"
    finally:
        window.close()


def test_the_history_persists_across_windows(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """履歴が次回の起動でも残る。"""
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        assert second.recent_files == (sample_pdf,)
        assert recent_texts(second) == [sample_pdf.name]
    finally:
        second.close()


# ---------------------------------------------------------------- セッションの復元
def test_the_last_document_is_reopened(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """前回読んでいた PDF が次回の起動で開く。"""
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        assert second.view.has_document
        assert second.view.page_count == 3
        assert sample_pdf.name in second.windowTitle()
        assert second.document_status_text == str(sample_pdf)
    finally:
        second.close()


def test_the_reading_position_is_restored(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ページだけでなく、そのページのどこまで読んだかも戻る。"""
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    first.view.go_to_page(1)
    bar = first.view.verticalScrollBar()
    bar.setValue(bar.value() + 200)
    saved = first.view.current_reading_position()
    assert saved is not None
    assert saved.page_index == 1
    assert saved.y_norm > 0.05, "テストの前提が崩れている（ページの途中まで進んでいない）"
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        restored = second.view.current_reading_position()
        assert restored is not None
        assert restored.page_index == saved.page_index
        assert restored.y_norm == pytest.approx(saved.y_norm, abs=0.02)
    finally:
        second.close()


def test_the_reading_position_does_not_creep_across_a_page_boundary(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ページの継ぎ目付近で終了しても、再起動で先へ進まない。

    現在ページ（重なりの最大）と、ビューポート上端のページが食い違う
    状態を作る。読書位置の基準を取り違えていると、再起動のたびに
    次のページの先頭まで送られてしまう。
    """
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    rect = first.view.page_viewport_rect(0)
    assert rect is not None
    bar = first.view.verticalScrollBar()
    bar.setValue(round(bar.value() + rect.bottom() - first.view.viewport().height() * 0.4))
    assert first.view.current_page == 1, "テストの前提が崩れている（継ぎ目付近にいない）"
    saved = first.view.current_reading_position()
    assert saved is not None
    assert saved.page_index == 0
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        restored = second.view.current_reading_position()
        assert restored is not None
        assert restored.page_index == 0
        assert restored.y_norm == pytest.approx(saved.y_norm, abs=0.02)
    finally:
        second.close()


def test_the_reading_position_survives_a_different_window_size(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """保存時と違う大きさで起動しても、同じページの同じ割合へ戻る。

    正規化座標なので、ピクセル一致は求めない。
    """
    first = make_window(qtbot, ini, study_marks, size=(900, 700))
    first.open_path(sample_pdf)
    first.view.go_to_page(1)
    bar = first.view.verticalScrollBar()
    bar.setValue(bar.value() + 150)
    saved = first.view.current_reading_position()
    assert saved is not None
    first.close()

    second = make_window(qtbot, ini, study_marks, size=(640, 520))
    try:
        restored = second.view.current_reading_position()
        assert restored is not None
        assert restored.page_index == saved.page_index
        assert restored.y_norm == pytest.approx(saved.y_norm, abs=0.05)
    finally:
        second.close()


@pytest.mark.parametrize("mode", [ZoomMode.FREE, ZoomMode.FIT_WIDTH, ZoomMode.FIT_PAGE])
def test_the_zoom_mode_is_restored_with_the_document(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path, mode: ZoomMode
) -> None:
    """FREE / 幅に合わせる / ページ全体のどれで終了しても、そのまま戻る。

    倍率を決めてから読書位置を決める順序でないと、フィットの計算が
    スクロール位置を動かしてしまう。
    """
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    if mode is ZoomMode.FREE:
        first.view.set_zoom(1.5)
    elif mode is ZoomMode.FIT_WIDTH:
        first.view.fit_width()
    else:
        first.view.fit_page()
    first.view.go_to_page(1)
    saved = first.view.current_reading_position()
    assert saved is not None
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        assert second.view.zoom_mode is mode
        if mode is ZoomMode.FREE:
            assert second.view.zoom == pytest.approx(1.5)
        restored = second.view.current_reading_position()
        assert restored is not None
        assert restored.page_index == saved.page_index
        assert restored.y_norm == pytest.approx(saved.y_norm, abs=0.05)
    finally:
        second.close()


def test_the_study_marks_are_restored_with_the_document(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """自動復元でも学習マークがオーバーレイと一覧に載る。"""
    mark = study_marks.create(DocumentIdentity.of(sample_pdf), 1, 0.5, 0.5)
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        assert second.study_marks.active_document_path == sample_pdf
        assert second.view.study_marks == (mark,)
        assert second.study_mark_sidebar.rows == (mark,)
    finally:
        second.close()


def test_closing_without_a_document_forgets_the_session(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path, warnings: list[str]
) -> None:
    """読んでいた PDF を閉じてから終了したら、次回は勝手に開かない。

    保存済みのセッションを **上書きして消す** ところまで見る。開いていない
    まま終了したのに前回の値が残っていると、閉じたはずの PDF が次回また
    開いてしまう。
    """
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    first.close()
    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).last_document != ""

    # 「表示を空にしてから終了する」を、開くのに失敗する経路で作る。
    second = make_window(qtbot, ini, study_marks)
    assert second.view.has_document, "テストの前提が崩れている（自動復元が働いていない）"
    second.open_path(sample_pdf.parent / "no_such_file.pdf")
    assert not second.view.has_document
    second.close()

    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).last_document == ""
    third = make_window(qtbot, ini, study_marks)
    try:
        assert not third.view.has_document
        assert third.document_status_text == "PDF が開かれていません"
    finally:
        third.close()
    assert warnings


def test_the_automatic_restore_does_not_reorder_the_history(
    qtbot: QtBot, ini: str, backend: QSettings, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """自動で開いただけの PDF は、履歴の並びを変えない。

    履歴は「利用者が明示的に開いたもの」なので、起動しただけで先頭が
    入れ替わると、直前に何を開いたのかが分からなくなる。
    """
    other = sample_pdf.parent / "other.pdf"
    shutil.copy(sample_pdf, other)
    backend.setValue("files/recent", [str(other), str(sample_pdf)])
    set_session(backend, str(sample_pdf))
    backend.sync()

    window = make_window(qtbot, ini, study_marks)
    try:
        assert window.view.has_document
        assert window.recent_files == (other, sample_pdf)
    finally:
        window.close()


def test_the_automatic_restore_does_not_add_to_the_history(
    qtbot: QtBot, ini: str, backend: QSettings, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """自動復元だけでは履歴に載らない。"""
    set_session(backend, str(sample_pdf))
    backend.sync()

    window = make_window(qtbot, ini, study_marks)
    try:
        assert window.view.has_document
        assert window.recent_files == ()
    finally:
        window.close()


# ---------------------------------------------------------------- 復元の失敗
def test_a_missing_last_document_does_not_break_startup(
    qtbot: QtBot,
    ini: str,
    backend: QSettings,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    warnings: list[str],
) -> None:
    """前回の PDF が消えていても起動する。復元対象は忘れ、履歴からも外す。"""
    missing = Path(shutil.copy(sample_pdf, sample_pdf.parent / "gone.pdf"))
    backend.setValue("files/recent", [str(missing), str(sample_pdf)])
    set_session(backend, str(missing))
    backend.sync()
    missing.unlink()

    first = make_window(qtbot, ini, study_marks)
    assert not first.view.has_document
    assert first.isVisible()
    assert first.recent_files == (sample_pdf,)
    # 起動直後にダイアログを積み上げない。
    assert warnings == []
    first.close()

    # 同じ失敗を毎回繰り返さない。
    second = make_window(qtbot, ini, study_marks)
    try:
        assert Settings(QSettings(ini, QSettings.Format.IniFormat)).last_document == ""
        assert not second.view.has_document
    finally:
        second.close()


def test_a_broken_last_document_does_not_break_startup(
    qtbot: QtBot,
    ini: str,
    backend: QSettings,
    study_marks: StudyMarkRepository,
    broken_pdf: Path,
    warnings: list[str],
) -> None:
    """壊れた PDF でも起動する。履歴からは外さない。"""
    backend.setValue("files/recent", [str(broken_pdf)])
    set_session(backend, str(broken_pdf))
    backend.sync()

    window = make_window(qtbot, ini, study_marks)
    try:
        assert not window.view.has_document
        assert window.isVisible()
        assert window.recent_files == (broken_pdf,)
        assert warnings == []
        assert Settings(QSettings(ini, QSettings.Format.IniFormat)).last_document == ""
    finally:
        window.close()


def test_a_study_mark_failure_during_restore_does_not_break_startup(
    qtbot: QtBot,
    ini: str,
    backend: QSettings,
    study_mark_connection: sqlite3.Connection,
    sample_pdf: Path,
    warnings: list[str],
) -> None:
    """学習マークを読めなければ、PDF なしで起動を続ける（fail-closed のまま）。

    アプリを終了させはしない。復元対象は忘れる。
    """
    set_session(backend, str(sample_pdf))
    backend.sync()

    window = make_window(qtbot, ini, BrokenRepository(study_mark_connection, failing="list"))
    try:
        assert window.isVisible()
        assert not window.view.has_document
        assert window.study_marks.active_document_path is None
        assert warnings == []
        assert Settings(QSettings(ini, QSettings.Format.IniFormat)).last_document == ""
    finally:
        window.close()


@pytest.mark.parametrize(
    ("page", "y_norm", "expected_page", "expected_y"),
    [
        ("-10", "0.5", 0, 0.5),
        ("abc", "0.5", 0, 0.5),
        ("3.5", "0.5", 0, 0.5),
        ("1", "nan", 1, 0.0),
        ("1", "2", 1, 0.0),
        ("1", "-1", 1, 0.0),
        ("", "", 0, 0.0),
    ],
)
def test_broken_session_values_fall_back_safely(
    qtbot: QtBot,
    ini: str,
    backend: QSettings,
    study_marks: StudyMarkRepository,
    sample_pdf: Path,
    page: str,
    y_norm: str,
    expected_page: int,
    expected_y: float,
) -> None:
    """壊れた位置が保存されていても起動し、安全な既定へ落ちる。

    ページと縦位置は別々に検査する。片方だけ壊れていても、読める方は
    そのまま使う（読めない方だけを既定へ落とす）。
    """
    set_session(backend, str(sample_pdf), page=page, y_norm=y_norm)
    backend.sync()

    window = make_window(qtbot, ini, study_marks)
    try:
        assert window.view.has_document
        position = window.view.current_reading_position()
        assert position is not None
        assert position.page_index == expected_page
        assert position.y_norm == pytest.approx(expected_y, abs=0.05)
    finally:
        window.close()


def test_a_page_beyond_the_document_falls_back_to_the_top(
    qtbot: QtBot, ini: str, backend: QSettings, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """ページ数の減った PDF に差し替えられていたら、丸めずに先頭で開く。

    セッションの位置は過去の読書状態のヒントでしかないので、存在しない
    位置を最終ページとして解釈しない（学習マークの移動とは扱いが違う）。
    """
    set_session(backend, str(sample_pdf), page=99, y_norm=0.5)
    backend.sync()

    window = make_window(qtbot, ini, study_marks)
    try:
        assert window.view.has_document
        assert window.view.current_page == 0
        assert window.view.verticalScrollBar().value() == 0
    finally:
        window.close()


def test_an_invalid_last_document_type_is_ignored(
    qtbot: QtBot, ini: str, backend: QSettings, study_marks: StudyMarkRepository
) -> None:
    """復元対象が文字列でなくても起動する。"""
    set_session(backend, 42)
    backend.sync()

    window = make_window(qtbot, ini, study_marks)
    try:
        assert window.isVisible()
        assert not window.view.has_document
    finally:
        window.close()


def test_settings_without_the_new_keys_start_cleanly(
    qtbot: QtBot, ini: str, backend: QSettings, study_marks: StudyMarkRepository
) -> None:
    """新しいキーを持たない既存の設定でも、そのまま起動する。"""
    backend.setValue("view/canvas_theme", "black")
    backend.sync()

    window = make_window(qtbot, ini, study_marks)
    try:
        assert window.isVisible()
        assert not window.view.has_document
        assert window.recent_files == ()
        assert window.view.canvas_theme.value == "black"
    finally:
        window.close()


# ---------------------------------------------------------------- 副作用の無さ
def test_saving_the_session_does_not_touch_the_render_state(
    window: MainWindow, sample_pdf: Path
) -> None:
    """セッションの保存でキャッシュを捨てたり世代を進めたりしない。"""
    window.open_path(sample_pdf)
    render = window.view._render  # noqa: SLF001
    generation = render.generation

    window._save_last_session()  # noqa: SLF001

    assert render.generation == generation
    assert window.view.has_document


def test_restoring_does_not_open_the_document_twice(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """自動復元も通常の open ライフサイクルを1度だけ通る。"""
    first = make_window(qtbot, ini, study_marks)
    first.open_path(sample_pdf)
    first.close()

    opened: list[Path] = []
    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    qtbot.addWidget(second)
    original = MainWindow._open_document  # noqa: SLF001

    def recording(self: MainWindow, path: Path, *, notify: bool) -> bool:
        opened.append(path)
        return original(self, path, notify=notify)

    second._open_document = recording.__get__(second)  # type: ignore[method-assign]  # noqa: SLF001
    with qtbot.waitExposed(second):
        second.show()
    try:
        assert opened == [sample_pdf]
        # 2度目の表示では復元し直さない。
        second.hide()
        second.show()
        assert opened == [sample_pdf]
    finally:
        second.close()
