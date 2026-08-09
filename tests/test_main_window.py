"""`anp.ui.main_window` のテスト。

GUI のタイミングに依存しないよう、シグナルや描画ではなく状態のみを検証する。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QApplication

from anp.core.settings import Settings
from anp.ui.main_window import MainWindow


def test_window_has_menus_and_status_bar(qapp: QApplication, settings: Settings) -> None:
    """メニューとステータスバーが用意されている。"""
    window = MainWindow(settings)
    try:
        titles = [action.text() for action in window.menuBar().actions()]
        assert titles == ["ファイル(&F)", "ヘルプ(&H)"]
        assert window.statusBar().currentMessage() != ""
    finally:
        window.deleteLater()


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
