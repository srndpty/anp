"""`anp.ui.main_window` のテスト。

GUI のタイミングに依存しないよう、待ち合わせではなく状態とシグナルを検証する。
ファイル選択ダイアログは出さず、`open_path()` を直接呼ぶ。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel, QSpinBox
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.ui.main_window import MainWindow
from anp.ui.pdf_view import ZOOM_STEP, ZoomMode


@pytest.fixture
def window(qtbot: QtBot, settings: Settings) -> Iterator[MainWindow]:
    """表示済みのメインウィンドウ。

    ビューポートの大きさは表示されるまで確定しないので、表示してから返す。
    """
    window = MainWindow(settings)
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


def page_input(window: MainWindow) -> QSpinBox:
    """ページ番号の入力欄。ツールバー上の唯一のスピンボックス。"""
    boxes = window.findChildren(QSpinBox)
    assert len(boxes) == 1
    return boxes[0]


def zoom_label_text(window: MainWindow) -> str:
    """倍率表示のラベル。ページ数のラベルと区別するため、末尾が `/ n` でない方。"""
    texts = [label.text() for label in window.findChildren(QLabel)]
    return next(text for text in texts if not text.startswith("/"))


# ------------------------------------------------------------------ 骨格
def test_window_has_menus_and_status_bar(window: MainWindow) -> None:
    """メニューとステータスバーが用意されている。"""
    titles = [action.text() for action in window.menuBar().actions()]
    assert titles == ["ファイル(&F)", "表示(&V)", "移動(&G)", "ヘルプ(&H)"]
    assert window.statusBar().currentMessage() != ""


def test_the_pdf_view_is_the_central_widget(window: MainWindow) -> None:
    """PdfView が中央ウィジェット。"""
    assert window.centralWidget() is window.view


def test_default_size_when_no_saved_geometry(qapp: QApplication, settings: Settings) -> None:
    """未保存なら既定サイズで開く。"""
    window = MainWindow(settings)
    try:
        assert window.size().width() == 1000
        assert window.size().height() == 800
    finally:
        window.deleteLater()


def test_geometry_is_saved_on_close(qapp: QApplication, settings: Settings) -> None:
    """閉じるときにジオメトリと状態が保存される。"""
    window = MainWindow(settings)
    window.resize(640, 480)

    window.close()

    geometry = settings.window_geometry
    assert isinstance(geometry, QByteArray)
    assert not geometry.isEmpty()
    assert settings.window_state is not None
    window.deleteLater()


def test_saved_geometry_is_restored(qapp: QApplication, settings: Settings) -> None:
    """保存されたジオメトリが次回起動時に復元される。"""
    first = MainWindow(settings)
    first.resize(720, 540)
    first.close()
    first.deleteLater()

    second = MainWindow(settings)
    try:
        assert second.size().width() == 720
        assert second.size().height() == 540
    finally:
        second.deleteLater()


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
    opened: MainWindow, broken_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """開けなかったら、前の PDF の表示だけを残さない。

    `DocumentController.open()` は失敗時に前のドキュメントも閉じる契約なので、
    表示を残すと見えている内容と実体がずれる。
    """
    warnings: list[str] = []
    monkeypatch.setattr(
        "anp.ui.main_window.QMessageBox.warning",
        lambda *args: warnings.append(args[2]),
    )

    opened.open_path(broken_pdf)

    assert not opened.view.has_document
    assert opened.view.page_count == 0
    assert warnings and "PDF として読み取れない" in warnings[0]


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


# ------------------------------------------------------------------ 設定
def test_the_zoom_mode_round_trips(qapp: QApplication, tmp_path: Path) -> None:
    """倍率モードが次回起動時に復元される。"""
    ini = str(tmp_path / "settings.ini")

    first = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)))
    # ドキュメントが無いとフィットのアクションは無効なので、ビューへ直接指示する。
    first.view.fit_page()
    first.close()
    first.deleteLater()

    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)))
    try:
        assert second.view.zoom_mode is ZoomMode.FIT_PAGE
    finally:
        second.deleteLater()


def test_the_free_zoom_round_trips(qapp: QApplication, tmp_path: Path) -> None:
    """手動指定の倍率が次回起動時に復元される。"""
    ini = str(tmp_path / "settings.ini")

    first = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)))
    first.view.set_zoom(2.0)
    first.close()
    first.deleteLater()

    second = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)))
    try:
        assert second.view.zoom == pytest.approx(2.0)
        assert second.view.zoom_mode is ZoomMode.FREE
    finally:
        second.deleteLater()


def test_an_unknown_zoom_mode_falls_back_to_free(qapp: QApplication, tmp_path: Path) -> None:
    """知らない倍率モードが保存されていたら FREE で起動する。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/zoom_mode", "fit_diagonal")

    window = MainWindow(Settings(backend))
    try:
        assert window.view.zoom_mode is ZoomMode.FREE
    finally:
        window.deleteLater()
