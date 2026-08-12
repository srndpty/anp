"""`anp.ui.main_window` のテスト。

GUI のタイミングに依存しないよう、待ち合わせではなく状態とシグナルを検証する。
ファイル選択ダイアログは出さず、`open_path()` を直接呼ぶ。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QSettings, QSize, Qt
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QSpinBox, QToolBar, QWidget
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.pdf.color import PageColorMode
from anp.pdf.layout import InvalidPageGeometryError
from anp.pdf.render import PageRenderService, PageRequest
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.appearance import CanvasTheme, UiTheme
from anp.ui.main_window import MainWindow
from anp.ui.pdf_view import ZOOM_STEP, ZoomMode


@pytest.fixture
def window(
    qtbot: QtBot, settings: Settings, study_marks: StudyMarkRepository
) -> Iterator[MainWindow]:
    """表示済みのメインウィンドウ。

    ビューポートの大きさは表示されるまで確定しないので、表示してから返す。
    """
    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    yield window
    window.close()


@pytest.fixture
def opened(window: MainWindow, sample_pdf: Path) -> MainWindow:
    """3ページの PDF を開いた状態のウィンドウ。"""
    window.open_path(sample_pdf)
    return window


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`QMessageBox.warning` を捕まえて本文を集める。

    テスト中にモーダルダイアログを出さないため。Phase 1 のエラー通知は
    この1種類だけなので、専用の抽象は作らずここで差し替える。
    """
    messages: list[str] = []
    monkeypatch.setattr(
        "anp.ui.main_window.QMessageBox.warning",
        lambda *args: messages.append(args[2]),
    )
    return messages


def toolbar(window: MainWindow) -> QToolBar:
    """ズームとページ移動のツールバー。

    学習マークのサイドバーにも入力欄やラベルがあるので、ウィンドウ全体
    ではなくツールバーの中だけを探す。
    """
    bars = window.findChildren(QToolBar)
    assert len(bars) == 1
    return bars[0]


def page_input(window: MainWindow) -> QSpinBox:
    """ページ番号の入力欄。ツールバー上の唯一のスピンボックス。"""
    boxes = toolbar(window).findChildren(QSpinBox)
    assert len(boxes) == 1
    return boxes[0]


def zoom_label_text(window: MainWindow) -> str:
    """倍率表示のラベル。ページ数のラベルと区別するため、末尾が `/ n` でない方。"""
    texts = [label.text() for label in toolbar(window).findChildren(QLabel)]
    return next(text for text in texts if not text.startswith("/"))


# ------------------------------------------------------------------ 骨格
def test_window_has_menus_and_status_bar(window: MainWindow) -> None:
    """メニューとステータスバーが用意されている。"""
    titles = [action.text() for action in window.menuBar().actions()]
    assert titles == ["ファイル(&F)", "表示(&V)", "移動(&G)", "設定(&S)", "ヘルプ(&H)"]
    # パスは常設ウィジェットなので、一時メッセージ（`currentMessage()`）
    # とは別に常に出ている。
    assert window.document_status_text == "PDF が開かれていません"


def test_the_pdf_view_is_the_central_widget(window: MainWindow) -> None:
    """PdfView が中央ウィジェット。"""
    assert window.centralWidget() is window.view


def test_default_size_when_no_saved_geometry(
    qapp: QApplication, settings: Settings, study_marks: StudyMarkRepository
) -> None:
    """未保存なら既定サイズで開く。"""
    window = MainWindow(settings, study_marks)
    try:
        assert window.size().width() == 1000
        assert window.size().height() == 800
    finally:
        window.deleteLater()


def test_geometry_is_saved_on_close(
    qapp: QApplication, settings: Settings, study_marks: StudyMarkRepository
) -> None:
    """閉じるときにジオメトリと状態が保存される。"""
    window = MainWindow(settings, study_marks)
    window.resize(640, 480)

    window.close()

    geometry = settings.window_geometry
    assert isinstance(geometry, QByteArray)
    assert not geometry.isEmpty()
    assert settings.window_state is not None
    window.deleteLater()


def test_saved_geometry_is_restored(
    qapp: QApplication, settings: Settings, study_marks: StudyMarkRepository
) -> None:
    """保存されたジオメトリが次回起動時に復元される。

    **論理サイズを直接書かない。** `restoreGeometry()` は保存時と復元時の
    画面 DPI の差を吸収するので、高 DPI スケーリング下では 720×540 が
    そのまま戻るとは限らない。素の `QWidget` に同じデータを復元させ、
    「Qt が返す大きさ」を基準にする。こうすると、確かめたい contract
    （保存したジオメトリを使っている。既定サイズに落ちていない）だけが
    残り、テストが実行環境のスケーリングに左右されなくなる。
    """
    first = MainWindow(settings, study_marks)
    first.resize(720, 540)
    first.close()
    first.deleteLater()

    geometry = settings.window_geometry
    assert geometry is not None
    reference = QWidget()
    try:
        assert reference.restoreGeometry(geometry)
        # 既定サイズと区別できなければ、この検証は何も言っていない。
        assert reference.size() != QSize(1000, 800), "テストの前提が崩れている"

        second = MainWindow(settings, study_marks)
        try:
            assert second.size() == reference.size()
        finally:
            second.deleteLater()
    finally:
        reference.deleteLater()


def test_the_three_docks_restore_their_visibility_together(
    qtbot: QtBot, settings: Settings, study_marks: StudyMarkRepository
) -> None:
    """学習マーク・目次・検索の表示状態が、3つ同時に往復する。

    3つのドックは1つの `saveState()` の blob を共有するので、片方だけを
    見るテストでは「復元しているつもりで、別のドックの状態を潰している」
    実装を通してしまう。**互いを上書きしない**ことまで固定するために、
    3つを別々の状態（表示 / 非表示 / 表示）にしてから往復させる。

    初期状態は3つとも非表示なので、表示にした2つはドック専用の設定キーが
    無くても復元されていることになり、目次は「他の2つに引きずられて
    勝手に開かない」ことになる。
    """
    first = MainWindow(settings, study_marks)
    qtbot.addWidget(first)
    with qtbot.waitExposed(first):
        first.show()
    assert first.study_mark_sidebar.isHidden(), "テストの前提が崩れている（初期状態は非表示）"
    assert first.toc_sidebar.isHidden(), "テストの前提が崩れている（初期状態は非表示）"
    assert first.search_dock.isHidden(), "テストの前提が崩れている（初期状態は非表示）"

    first.study_mark_sidebar.show()
    first.search_dock.show()
    first.close()

    second = MainWindow(settings, study_marks)
    qtbot.addWidget(second)
    with qtbot.waitExposed(second):
        second.show()
    try:
        assert not second.study_mark_sidebar.isHidden()
        assert second.toc_sidebar.isHidden()
        assert not second.search_dock.isHidden()
    finally:
        second.close()


# ------------------------------------------------------------------ PDF を開く
def test_opening_a_pdf_fills_the_view(opened: MainWindow, sample_pdf: Path) -> None:
    """開いた PDF がビューに入る。"""
    assert opened.view.has_document
    assert opened.view.page_count == 3
    assert sample_pdf.name in opened.windowTitle()


def test_opening_a_pdf_updates_the_page_controls(opened: MainWindow) -> None:
    """開いたあとにページ数の表示と操作が有効になる。"""
    spin = page_input(opened)
    assert spin.isEnabled()
    assert spin.maximum() == 3
    assert spin.value() == 1
    assert opened.reader_actions.next_page.isEnabled()
    assert not opened.reader_actions.previous_page.isEnabled()


def test_a_failed_open_clears_the_view(
    opened: MainWindow, broken_pdf: Path, warnings: list[str]
) -> None:
    """開けなかったら、前の PDF の表示だけを残さない。

    `DocumentController.open()` は失敗時に前のドキュメントも閉じる契約なので、
    表示を残すと見えている内容と実体がずれる。
    """
    opened.open_path(broken_pdf)

    assert not opened.view.has_document
    assert opened.view.page_count == 0
    assert warnings and "PDF として読み取れない" in warnings[0]


def test_a_failed_open_resets_the_title_and_status(
    opened: MainWindow, broken_pdf: Path, warnings: list[str]
) -> None:
    """開くのに失敗したら、前の PDF の名前も状態表示も残さない。"""
    opened.open_path(broken_pdf)

    assert opened.windowTitle() == "anp"
    assert opened.document_status_text == "PDF が開かれていません"
    assert warnings


def test_a_failed_open_disables_the_controls(
    opened: MainWindow, broken_pdf: Path, warnings: list[str]
) -> None:
    """失敗後は、開いているつもりで押せる操作を残さない。"""
    opened.open_path(broken_pdf)

    actions = opened.reader_actions
    assert not page_input(opened).isEnabled()
    assert not actions.zoom_in.isEnabled()
    assert not actions.fit_width.isEnabled()
    assert not actions.next_page.isEnabled()
    assert not actions.previous_page.isEnabled()
    assert actions.open.isEnabled()
    assert warnings


@pytest.mark.parametrize(
    "bad", ["broken_pdf", "empty_pdf", "directory_pdf", "encrypted_pdf", "pageless_pdf"]
)
def test_every_kind_of_failure_is_reported_and_clears_the_view(
    opened: MainWindow, bad: str, warnings: list[str], request: pytest.FixtureRequest
) -> None:
    """壊れた・空・開けないパス・パスワード付きのいずれでも同じ後始末をする。"""
    opened.open_path(request.getfixturevalue(bad))

    assert not opened.view.has_document
    assert opened.view.page_count == 0
    assert len(warnings) == 1
    assert warnings[0].strip() != ""


def test_a_broken_page_geometry_leaves_no_document(
    opened: MainWindow,
    sample_pdf: Path,
    two_page_pdf: Path,
    warnings: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ページ寸法が壊れていて表示を組み立てられなくても、状態を混ぜない。

    `QPdfDocument.load()` は成功しているので、ここで抜けると「ドキュメントは
    新しい PDF、表示は前の PDF」という組み合わせが残る。
    """

    def broken(*_args: object, **_kwargs: object) -> None:
        raise InvalidPageGeometryError("ページ 1 の高さが不正です（0.0）")

    monkeypatch.setattr(opened.view, "set_document", broken)

    opened.open_path(two_page_pdf)

    assert not opened.view.has_document
    assert opened.view.page_count == 0
    assert opened.windowTitle() == "anp"
    assert opened.document_status_text == "PDF が開かれていません"
    assert len(warnings) == 1
    assert "ページ 1 の高さが不正" in warnings[0]

    # 後始末が済んでいるので、次の PDF は普通に開ける。
    monkeypatch.undo()
    opened.open_path(sample_pdf)
    assert opened.view.page_count == 3


def test_a_broken_page_geometry_does_not_keep_the_old_marks(
    opened: MainWindow,
    two_page_pdf: Path,
    warnings: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表示を組み立てられなかった PDF は、学習マークの表示対象にもしない。"""

    def broken(*_args: object, **_kwargs: object) -> None:
        raise InvalidPageGeometryError("ページ 1 の幅が不正です（nan）")

    monkeypatch.setattr(opened.view, "set_document", broken)

    opened.open_path(two_page_pdf)

    assert opened.study_marks.active_document_path is None
    assert opened.view.study_marks == ()
    assert warnings


def test_a_missing_file_is_reported(
    opened: MainWindow, tmp_path: Path, warnings: list[str]
) -> None:
    """存在しないファイルは理由を添えて知らせる。"""
    opened.open_path(tmp_path / "no_such_file.pdf")

    assert not opened.view.has_document
    assert "ファイルが見つかりません" in warnings[0]


def test_a_password_protected_pdf_is_reported(
    opened: MainWindow, encrypted_pdf: Path, warnings: list[str]
) -> None:
    """パスワード付き PDF は Phase 1 では開かず、理由を伝える。"""
    opened.open_path(encrypted_pdf)

    assert not opened.view.has_document
    assert "パスワード" in warnings[0]


def test_a_pdf_can_be_opened_after_a_failure(
    window: MainWindow, broken_pdf: Path, sample_pdf: Path, warnings: list[str]
) -> None:
    """失敗しても、次に正常な PDF を開ける。"""
    window.open_path(broken_pdf)
    assert warnings

    window.open_path(sample_pdf)

    assert window.view.has_document
    assert window.view.page_count == 3
    assert window.view.current_page == 0
    assert sample_pdf.name in window.windowTitle()
    assert page_input(window).isEnabled()
    assert len(warnings) == 1


def test_a_failed_open_does_not_change_the_last_directory(
    opened: MainWindow, settings: Settings, sample_pdf: Path, tmp_path: Path, warnings: list[str]
) -> None:
    """開けなかったディレクトリは覚えない。"""
    other = tmp_path / "elsewhere"
    other.mkdir()
    broken = other / "broken.pdf"
    broken.write_text("no", encoding="utf-8")

    opened.open_path(broken)

    assert Path(settings.last_directory) == sample_pdf.parent
    assert warnings


def test_the_controls_are_disabled_without_a_document(window: MainWindow) -> None:
    """ドキュメントが無ければページ操作もズームも無効。"""
    actions = window.reader_actions

    assert not page_input(window).isEnabled()
    assert not actions.previous_page.isEnabled()
    assert not actions.next_page.isEnabled()
    assert not actions.zoom_in.isEnabled()
    assert not actions.fit_width.isEnabled()
    assert actions.open.isEnabled()


def test_the_page_count_is_zero_without_a_document(window: MainWindow) -> None:
    """空のときのページ数表示は 0。"""
    texts = [label.text() for label in window.findChildren(QLabel)]
    assert "/ 0" in texts


def test_opening_a_shorter_document_starts_from_the_top(
    opened: MainWindow, two_page_pdf: Path
) -> None:
    """ページ数の少ない PDF を開いても先頭から表示する。"""
    opened.view.go_to_page(2)
    assert page_input(opened).value() == 3

    opened.open_path(two_page_pdf)

    assert opened.view.page_count == 2
    assert opened.view.current_page == 0
    assert page_input(opened).value() == 1
    assert page_input(opened).maximum() == 2


def test_the_last_directory_is_remembered(
    opened: MainWindow, settings: Settings, sample_pdf: Path
) -> None:
    """開いたディレクトリを次回のダイアログのために覚える。"""
    assert opened.view.has_document
    assert Path(settings.last_directory) == sample_pdf.parent


# ------------------------------------------------------------------ ズーム
def test_zoom_actions_change_the_zoom(opened: MainWindow) -> None:
    """拡大・縮小のアクションが倍率を変える。"""
    opened.reader_actions.actual_size.trigger()

    opened.reader_actions.zoom_in.trigger()
    assert opened.view.zoom == pytest.approx(ZOOM_STEP)

    opened.reader_actions.zoom_out.trigger()
    assert opened.view.zoom == pytest.approx(1.0)


def test_actual_size_returns_to_100_percent(opened: MainWindow) -> None:
    """実際の大きさで 100% の FREE に戻る。"""
    opened.reader_actions.fit_page.trigger()

    opened.reader_actions.actual_size.trigger()

    assert opened.view.zoom == pytest.approx(1.0)
    assert opened.view.zoom_mode is ZoomMode.FREE


def test_fit_actions_switch_the_mode(opened: MainWindow) -> None:
    """フィットのアクションでモードが切り替わる。"""
    opened.reader_actions.fit_width.trigger()
    width_mode: ZoomMode = opened.view.zoom_mode
    assert width_mode is ZoomMode.FIT_WIDTH

    opened.reader_actions.fit_page.trigger()
    assert opened.view.zoom_mode is ZoomMode.FIT_PAGE


def test_the_fit_check_state_follows_the_view(opened: MainWindow) -> None:
    """フィットの選択表示が実際のモードと一致する。"""
    actions = opened.reader_actions

    actions.fit_width.trigger()
    assert actions.fit_width.isChecked()
    assert not actions.fit_page.isChecked()

    actions.actual_size.trigger()
    assert not actions.fit_width.isChecked()
    assert not actions.fit_page.isChecked()


def test_triggering_the_same_fit_twice_stays_checked(opened: MainWindow) -> None:
    """同じフィットをもう一度押しても選択表示が外れない。

    チェック可能なアクションは押すたびにチェックが反転するが、ビューの
    状態は変わらないので `zoom_changed` は出ない。表示だけが実態から
    外れるのを防ぐ。
    """
    action = opened.reader_actions.fit_width

    action.trigger()
    action.trigger()

    assert action.isChecked()
    assert opened.view.zoom_mode is ZoomMode.FIT_WIDTH


def test_triggering_the_same_fit_page_twice_stays_checked(opened: MainWindow) -> None:
    """ページ全体でも同じ。"""
    action = opened.reader_actions.fit_page

    action.trigger()
    action.trigger()

    assert action.isChecked()
    assert opened.view.zoom_mode is ZoomMode.FIT_PAGE


def test_the_zoom_label_shows_a_percentage(opened: MainWindow) -> None:
    """FREE ではパーセントを表示する。"""
    opened.reader_actions.actual_size.trigger()
    assert zoom_label_text(opened) == "100%"

    opened.view.set_zoom(2.0)
    assert zoom_label_text(opened) == "200%"


def test_the_zoom_label_shows_the_fit_mode(opened: MainWindow) -> None:
    """フィット中はモードが分かる表示にする。"""
    opened.reader_actions.fit_width.trigger()
    assert zoom_label_text(opened) == "幅に合わせる"

    opened.reader_actions.fit_page.trigger()
    assert zoom_label_text(opened) == "ページ全体"


# ------------------------------------------------------------------ ページ移動
def test_the_page_input_is_one_based(opened: MainWindow) -> None:
    """1 始まりの入力が 0 始まりの内部ページ番号になる。"""
    page_input(opened).setValue(3)

    assert opened.view.current_page == 2


def test_the_page_input_follows_the_current_page(opened: MainWindow) -> None:
    """スクロールでページが変わると入力欄の表示も追随する。"""
    opened.view.go_to_page(1)

    assert page_input(opened).value() == 2


def test_previous_and_next_move_one_page(opened: MainWindow) -> None:
    """前/次は現在ページの ±1。"""
    opened.reader_actions.next_page.trigger()
    assert opened.view.current_page == 1

    opened.reader_actions.next_page.trigger()
    assert opened.view.current_page == 2

    opened.reader_actions.previous_page.trigger()
    assert opened.view.current_page == 1


def test_previous_and_next_are_disabled_at_the_ends(opened: MainWindow) -> None:
    """先頭では前へ、末尾では次へを無効にする。"""
    actions = opened.reader_actions
    assert not actions.previous_page.isEnabled()
    assert actions.next_page.isEnabled()

    opened.view.go_to_page(2)

    assert actions.previous_page.isEnabled()
    assert not actions.next_page.isEnabled()


# ------------------------------------------------------------------ 全画面表示
def test_f11_toggles_full_screen(window: MainWindow, qtbot: QtBot) -> None:
    """F11 で全画面になり、もう一度で戻る。"""
    qtbot.keyClick(window, Qt.Key.Key_F11)
    assert window.isFullScreen()

    qtbot.keyClick(window, Qt.Key.Key_F11)
    assert not window.isFullScreen()


def test_escape_leaves_full_screen(window: MainWindow, qtbot: QtBot) -> None:
    """Esc で全画面を抜ける。"""
    window.reader_actions.full_screen.trigger()
    assert window.isFullScreen()

    qtbot.keyClick(window, Qt.Key.Key_Escape)

    assert not window.isFullScreen()


def test_escape_does_nothing_when_not_full_screen(window: MainWindow, qtbot: QtBot) -> None:
    """通常表示中の Esc には副作用が無い。"""
    state = window.windowState()

    qtbot.keyClick(window, Qt.Key.Key_Escape)

    assert not window.isFullScreen()
    assert window.windowState() == state
    assert window.isVisible()


def test_the_full_screen_check_state_matches_the_window(window: MainWindow) -> None:
    """アクションのチェック状態がウィンドウの状態と一致する。"""
    action = window.reader_actions.full_screen
    assert not action.isChecked()

    action.trigger()
    assert action.isChecked() == window.isFullScreen() is True

    window.reader_actions.full_screen.trigger()
    assert action.isChecked() == window.isFullScreen() is False


def test_leaving_full_screen_restores_a_maximized_window(window: MainWindow) -> None:
    """最大化から全画面へ入って戻ると最大化に戻る。"""
    window.showMaximized()
    assert window.isMaximized()

    window.reader_actions.full_screen.trigger()
    assert window.isFullScreen()
    window.reader_actions.full_screen.trigger()

    assert not window.isFullScreen()
    assert window.isMaximized()


def test_leaving_full_screen_restores_a_normal_window(window: MainWindow) -> None:
    """通常表示から全画面へ入って戻ると通常表示に戻る。"""
    window.reader_actions.full_screen.trigger()
    window.reader_actions.full_screen.trigger()

    assert not window.isFullScreen()
    assert not window.isMaximized()


# ------------------------------------------------------------------ 後始末
def test_closing_releases_the_document(opened: MainWindow) -> None:
    """閉じるときに表示とドキュメントを手放す。

    閉じたウィンドウがレンダリング結果を抱えたままにならないようにする。
    """
    assert opened.view.has_document

    opened.close()

    assert not opened.view.has_document
    assert opened.view.page_count == 0


def test_closing_while_renders_are_in_flight_is_safe(
    window: MainWindow, sample_pdf: Path, qapp: QApplication
) -> None:
    """レンダリングが飛んでいる最中に閉じても例外にならない。

    要求は取り消せないので、閉じた後に結果が返ってくる。受け取り側が
    先に消えていると落ちる。
    """
    window.open_path(sample_pdf)
    window.view.verticalScrollBar().setValue(400)

    window.close()
    for _ in range(8):
        qapp.processEvents()

    assert not window.view.has_document


def test_switching_documents_while_renders_are_in_flight_is_safe(
    window: MainWindow, sample_pdf: Path, two_page_pdf: Path, qapp: QApplication
) -> None:
    """レンダリング中に別の PDF へ切り替えても、古い結果が表示に残らない。"""
    window.open_path(sample_pdf)
    window.view.verticalScrollBar().setValue(400)

    window.open_path(two_page_pdf)
    for _ in range(8):
        qapp.processEvents()

    assert window.view.page_count == 2
    assert window.view.current_page == 0
    assert page_input(window).maximum() == 2


def test_opening_the_same_pdf_again_starts_from_the_top(
    opened: MainWindow, sample_pdf: Path, qapp: QApplication
) -> None:
    """同じ PDF を開き直しても壊れない。"""
    opened.view.go_to_page(2)

    opened.open_path(sample_pdf)
    for _ in range(4):
        qapp.processEvents()

    assert opened.view.page_count == 3
    assert opened.view.current_page == 0


# ------------------------------------------------------------------ 全画面とフィット
def test_full_screen_keeps_the_fit_mode(opened: MainWindow, qapp: QApplication) -> None:
    """全画面へ入っても倍率モードは保たれ、倍率は新しい大きさで取り直される。"""
    opened.reader_actions.fit_width.trigger()
    before = opened.view.zoom

    opened.reader_actions.full_screen.trigger()
    for _ in range(8):
        qapp.processEvents()

    assert opened.isFullScreen()
    assert opened.view.zoom_mode is ZoomMode.FIT_WIDTH
    assert zoom_label_text(opened) == "幅に合わせる"
    rect = opened.view.page_viewport_rect(0)
    assert rect is not None
    assert rect.width() <= opened.view.viewport().width()
    assert opened.view.zoom != pytest.approx(before)


def test_leaving_full_screen_restores_the_fit_zoom(opened: MainWindow, qapp: QApplication) -> None:
    """全画面から戻ると元の大きさのフィット倍率に戻る。"""
    opened.reader_actions.fit_width.trigger()
    for _ in range(8):
        qapp.processEvents()
    before = opened.view.zoom

    opened.reader_actions.full_screen.trigger()
    for _ in range(8):
        qapp.processEvents()
    opened.reader_actions.full_screen.trigger()
    for _ in range(8):
        qapp.processEvents()

    assert not opened.isFullScreen()
    assert opened.view.zoom == pytest.approx(before)


def test_a_single_page_pdf_disables_both_page_actions(
    window: MainWindow, single_page_pdf: Path
) -> None:
    """1ページの PDF では前へも次へも無効。"""
    window.open_path(single_page_pdf)

    assert window.view.page_count == 1
    assert not window.reader_actions.previous_page.isEnabled()
    assert not window.reader_actions.next_page.isEnabled()
    assert page_input(window).maximum() == 1


# ------------------------------------------------------------------ ページの色
def menu_titled(window: MainWindow, title: str) -> QMenu:
    """タイトルでメニューを引く。

    `QAction.menu()` は使わない。取り出した `QAction` の Python 側の参照が
    先に消えると、PySide がメニューの C++ オブジェクトを解放してしまう。
    """
    menus = [menu for menu in window.findChildren(QMenu) if menu.title() == title]
    assert len(menus) == 1, title
    return menus[0]


def test_the_page_color_menu_exists(window: MainWindow) -> None:
    """表示メニューの中に「ページの色」がある。"""
    submenu = menu_titled(window, "ページの色(&C)")

    assert [action.text() for action in submenu.actions()] == [
        "オリジナル(&O)",
        "反転(&I)",
        "スマートダーク(&K)",
    ]
    assert submenu.menuAction() in menu_titled(window, "表示(&V)").actions()


def test_original_is_checked_at_startup(window: MainWindow) -> None:
    """既定ではオリジナルが選択されている。"""
    assert window.reader_actions.page_color_original.isChecked()
    assert not window.reader_actions.page_color_invert.isChecked()
    assert not window.reader_actions.page_color_smart_dark.isChecked()
    assert window.view.page_color_mode is PageColorMode.ORIGINAL


def test_the_menu_switches_to_invert(opened: MainWindow) -> None:
    """メニューから反転へ切り替えられる。"""
    opened.reader_actions.page_color_invert.trigger()

    assert opened.view.page_color_mode is PageColorMode.INVERT


def test_the_menu_switches_to_smart_dark(opened: MainWindow) -> None:
    """メニューからスマートダークへ切り替えられる。"""
    opened.reader_actions.page_color_smart_dark.trigger()

    assert opened.view.page_color_mode is PageColorMode.SMART_DARK


def test_the_menu_switches_back_to_original(opened: MainWindow) -> None:
    """メニューからオリジナルへ戻せる。"""
    opened.reader_actions.page_color_invert.trigger()

    opened.reader_actions.page_color_original.trigger()

    assert opened.view.page_color_mode is PageColorMode.ORIGINAL


def test_the_page_color_choices_are_exclusive(opened: MainWindow) -> None:
    """チェックは常に3つのうち1つだけ。"""
    actions = opened.reader_actions
    choices = {
        PageColorMode.ORIGINAL: actions.page_color_original,
        PageColorMode.INVERT: actions.page_color_invert,
        PageColorMode.SMART_DARK: actions.page_color_smart_dark,
    }
    assert set(choices) == set(PageColorMode), "モードが増えたのにメニューに出ていない"

    for mode, chosen in choices.items():
        chosen.trigger()

        assert opened.view.page_color_mode is mode
        assert [action for action in choices.values() if action.isChecked()] == [chosen]


def test_the_page_color_can_be_chosen_without_a_document(window: MainWindow) -> None:
    """PDF を開いていなくても選べる。"""
    assert window.reader_actions.page_color_invert.isEnabled()

    window.reader_actions.page_color_invert.trigger()

    assert window.view.page_color_mode is PageColorMode.INVERT


def test_the_page_color_survives_opening_another_pdf(
    window: MainWindow, sample_pdf: Path, two_page_pdf: Path
) -> None:
    """PDF を切り替えてもページの色は維持される（PDF ごとの設定ではない）。"""
    window.open_path(sample_pdf)
    window.reader_actions.page_color_invert.trigger()

    window.open_path(two_page_pdf)

    assert window.view.page_color_mode is PageColorMode.INVERT
    assert window.reader_actions.page_color_invert.isChecked()


def test_a_pdf_opened_later_uses_the_chosen_mode(window: MainWindow, sample_pdf: Path) -> None:
    """ドキュメントが無いうちに選んだモードで、次に開く PDF が表示される。"""
    window.reader_actions.page_color_invert.trigger()

    window.open_path(sample_pdf)

    assert window.view.page_color_mode is PageColorMode.INVERT


# ------------------------------------------------------------------ 設定
def test_the_page_color_mode_round_trips(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """ページの色が次回起動時に復元される。"""
    ini = str(tmp_path / "settings.ini")

    first = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    first.reader_actions.page_color_invert.trigger()
    first.close()
    first.deleteLater()

    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    try:
        assert second.view.page_color_mode is PageColorMode.INVERT
        assert second.reader_actions.page_color_invert.isChecked()
    finally:
        second.deleteLater()


def test_smart_dark_round_trips(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """スマートダークも保存され、次回起動時に復元される。"""
    ini = str(tmp_path / "settings.ini")

    first = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    first.reader_actions.page_color_smart_dark.trigger()
    first.close()
    first.deleteLater()

    stored = QSettings(ini, QSettings.Format.IniFormat).value("view/page_color_mode")
    assert stored == "smart_dark"

    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    try:
        assert second.view.page_color_mode is PageColorMode.SMART_DARK
        assert second.reader_actions.page_color_smart_dark.isChecked()
    finally:
        second.deleteLater()


@pytest.mark.parametrize("stored", ["solarized", "unknown", "sepia"])
def test_an_unknown_page_color_mode_falls_back_to_original(
    qapp: QApplication, tmp_path: Path, stored: str, study_marks: StudyMarkRepository
) -> None:
    """知らないページの色が保存されていたらオリジナルで起動する。

    `smart_dark` は P2-3B から正式な値なので、ここでは使わない。
    """
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/page_color_mode", stored)

    window = MainWindow(Settings(backend), study_marks)
    try:
        assert window.view.page_color_mode is PageColorMode.ORIGINAL
        assert window.reader_actions.page_color_original.isChecked()
    finally:
        window.deleteLater()


def test_the_zoom_mode_round_trips(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """倍率モードが次回起動時に復元される。"""
    ini = str(tmp_path / "settings.ini")

    first = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    # ドキュメントが無いとフィットのアクションは無効なので、ビューへ直接指示する。
    first.view.fit_page()
    first.close()
    first.deleteLater()

    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    try:
        assert second.view.zoom_mode is ZoomMode.FIT_PAGE
    finally:
        second.deleteLater()


def test_the_free_zoom_round_trips(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """手動指定の倍率が次回起動時に復元される。"""
    ini = str(tmp_path / "settings.ini")

    first = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    first.view.set_zoom(2.0)
    first.close()
    first.deleteLater()

    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    try:
        assert second.view.zoom == pytest.approx(2.0)
        assert second.view.zoom_mode is ZoomMode.FREE
    finally:
        second.deleteLater()


def test_the_free_zoom_survives_quitting_in_a_fit_mode(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """フィット中に終了しても、最後に手で指定した倍率が残る。

    保存する倍率を「終了時の倍率」にすると、フィット中に終了したときに
    手で選んだ倍率がどこにも残らない。
    """
    ini = str(tmp_path / "settings.ini")

    first = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    first.view.set_zoom(2.0)
    first.view.fit_page()
    first.close()
    first.deleteLater()

    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    try:
        assert second.view.zoom_mode is ZoomMode.FIT_PAGE
        assert second.view.last_free_zoom == pytest.approx(2.0)
    finally:
        second.deleteLater()


def test_an_unknown_zoom_mode_falls_back_to_free(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """知らない倍率モードが保存されていたら FREE で起動する。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/zoom_mode", "fit_diagonal")

    window = MainWindow(Settings(backend), study_marks)
    try:
        assert window.view.zoom_mode is ZoomMode.FREE
    finally:
        window.deleteLater()


# ------------------------------------------------------------------ キャンバスと UI テーマ
def test_the_canvas_menu_exists(window: MainWindow) -> None:
    """表示メニューの中に「キャンバス」がある。"""
    submenu = menu_titled(window, "キャンバス(&N)")

    assert [action.text() for action in submenu.actions()] == [
        "黒(&B)",
        "ダークグレー(&G)",
        "白(&W)",
    ]
    assert submenu.menuAction() in menu_titled(window, "表示(&V)").actions()


def test_the_ui_theme_menu_exists(window: MainWindow) -> None:
    """表示メニューの中に「UI テーマ」がある。"""
    submenu = menu_titled(window, "UI テーマ(&U)")

    assert [action.text() for action in submenu.actions()] == [
        "システム(&S)",
        "ライト(&L)",
        "ダーク(&D)",
    ]
    assert submenu.menuAction() in menu_titled(window, "表示(&V)").actions()


def test_the_appearance_defaults_are_checked_at_startup(window: MainWindow) -> None:
    """既定はダークグレーのキャンバスとシステムの UI テーマ。"""
    actions = window.reader_actions

    assert window.view.canvas_theme is CanvasTheme.DARK_GRAY
    assert actions.canvas_dark_gray.isChecked()
    assert window.ui_theme is UiTheme.SYSTEM
    assert actions.ui_theme_system.isChecked()


def test_the_menu_switches_the_canvas(opened: MainWindow) -> None:
    """メニューからキャンバスの色を選べる。"""
    opened.reader_actions.canvas_black.trigger()

    assert opened.view.canvas_theme is CanvasTheme.BLACK


def test_the_canvas_choices_are_exclusive(opened: MainWindow) -> None:
    """キャンバスのチェックは常に1つだけ。"""
    actions = opened.reader_actions

    actions.canvas_white.trigger()
    assert actions.canvas_white.isChecked()
    assert not actions.canvas_black.isChecked()
    assert not actions.canvas_dark_gray.isChecked()

    actions.canvas_black.trigger()
    assert actions.canvas_black.isChecked()
    assert not actions.canvas_white.isChecked()


def test_the_menu_switches_the_ui_theme(opened: MainWindow) -> None:
    """メニューから UI テーマを選べる。"""
    opened.reader_actions.ui_theme_dark.trigger()

    assert opened.ui_theme is UiTheme.DARK


def test_the_ui_theme_choices_are_exclusive(opened: MainWindow) -> None:
    """UI テーマのチェックは常に1つだけ。"""
    actions = opened.reader_actions

    actions.ui_theme_dark.trigger()
    assert actions.ui_theme_dark.isChecked()
    assert not actions.ui_theme_system.isChecked()
    assert not actions.ui_theme_light.isChecked()

    actions.ui_theme_light.trigger()
    assert actions.ui_theme_light.isChecked()
    assert not actions.ui_theme_dark.isChecked()


def test_the_ui_theme_can_go_back_to_system(opened: MainWindow) -> None:
    """ダークにしてからシステムへ戻せる。"""
    opened.reader_actions.ui_theme_dark.trigger()

    opened.reader_actions.ui_theme_system.trigger()

    assert opened.ui_theme is UiTheme.SYSTEM
    assert opened.reader_actions.ui_theme_system.isChecked()


def test_the_three_appearance_groups_are_separate(opened: MainWindow) -> None:
    """3つの軸は別々のグループ。どれを変えても他の選択は外れない。"""
    actions = opened.reader_actions

    actions.page_color_invert.trigger()
    actions.canvas_black.trigger()
    actions.ui_theme_dark.trigger()

    assert actions.page_color_invert.isChecked()
    assert actions.canvas_black.isChecked()
    assert actions.ui_theme_dark.isChecked()
    assert opened.view.page_color_mode is PageColorMode.INVERT
    assert opened.view.canvas_theme is CanvasTheme.BLACK
    assert opened.ui_theme is UiTheme.DARK


def test_the_ui_theme_does_not_touch_the_pdf_state(opened: MainWindow) -> None:
    """UI テーマを変えても、ページの色・キャンバス・倍率・ページは動かない。"""
    opened.reader_actions.page_color_invert.trigger()
    opened.reader_actions.canvas_white.trigger()
    opened.view.set_zoom(2.0)
    page = opened.view.current_page

    for theme in (UiTheme.DARK, UiTheme.LIGHT, UiTheme.SYSTEM):
        opened.set_ui_theme(theme)

    assert opened.view.page_color_mode is PageColorMode.INVERT
    assert opened.view.canvas_theme is CanvasTheme.WHITE
    assert opened.view.zoom == pytest.approx(2.0)
    assert opened.view.current_page == page


def test_the_appearance_can_be_chosen_without_a_document(window: MainWindow) -> None:
    """PDF を開いていなくても外観を選べる。"""
    assert window.reader_actions.canvas_black.isEnabled()
    assert window.reader_actions.ui_theme_dark.isEnabled()

    window.reader_actions.canvas_black.trigger()
    window.reader_actions.ui_theme_dark.trigger()

    assert window.view.canvas_theme is CanvasTheme.BLACK
    assert window.ui_theme is UiTheme.DARK


def test_the_appearance_survives_opening_another_pdf(
    window: MainWindow, sample_pdf: Path, two_page_pdf: Path
) -> None:
    """PDF を切り替えても外観は維持される（PDF ごとの設定ではない）。"""
    window.open_path(sample_pdf)
    window.reader_actions.canvas_black.trigger()
    window.reader_actions.ui_theme_dark.trigger()
    window.reader_actions.page_color_invert.trigger()

    window.open_path(two_page_pdf)

    assert window.view.canvas_theme is CanvasTheme.BLACK
    assert window.view.page_color_mode is PageColorMode.INVERT
    assert window.ui_theme is UiTheme.DARK
    assert window.reader_actions.canvas_black.isChecked()
    assert window.reader_actions.ui_theme_dark.isChecked()


def test_the_appearance_round_trips(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """外観の3つの軸が次回起動時に復元される。"""
    ini = str(tmp_path / "settings.ini")

    first = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    first.reader_actions.canvas_white.trigger()
    first.reader_actions.ui_theme_dark.trigger()
    first.reader_actions.page_color_invert.trigger()
    first.close()
    first.deleteLater()

    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), study_marks)
    try:
        assert second.view.canvas_theme is CanvasTheme.WHITE
        assert second.view.page_color_mode is PageColorMode.INVERT
        assert second.ui_theme is UiTheme.DARK
        assert second.reader_actions.canvas_white.isChecked()
        assert second.reader_actions.ui_theme_dark.isChecked()
    finally:
        second.deleteLater()


def test_an_unknown_canvas_theme_falls_back_to_dark_gray(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """知らないキャンバスの色が保存されていたらダークグレーで起動する。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/canvas_theme", "sepia")

    window = MainWindow(Settings(backend), study_marks)
    try:
        assert window.view.canvas_theme is CanvasTheme.DARK_GRAY
        assert window.reader_actions.canvas_dark_gray.isChecked()
    finally:
        window.deleteLater()


def test_an_unknown_ui_theme_falls_back_to_system(
    qapp: QApplication, tmp_path: Path, study_marks: StudyMarkRepository
) -> None:
    """知らない UI テーマが保存されていたらシステムで起動する。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("ui/theme", "solarized")

    window = MainWindow(Settings(backend), study_marks)
    try:
        assert window.ui_theme is UiTheme.SYSTEM
        assert window.reader_actions.ui_theme_system.isChecked()
    finally:
        window.deleteLater()


def test_changing_the_appearance_does_not_request_a_render(
    opened: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """キャンバスと UI テーマを変えても、PDF のレンダリング要求は起きない。

    P2-2 の最重要回帰条件をウィンドウ側からも確かめる。
    """
    calls: list[Sequence[PageRequest]] = []
    original = PageRenderService.request_pages

    def recording(service: PageRenderService, requests: Sequence[PageRequest]) -> None:
        calls.append(requests)
        original(service, requests)

    monkeypatch.setattr(PageRenderService, "request_pages", recording)

    opened.reader_actions.canvas_black.trigger()
    opened.reader_actions.ui_theme_dark.trigger()
    opened.reader_actions.ui_theme_system.trigger()

    assert calls == []
