"""メインウィンドウ。

Phase 0 の時点では PDF を扱わず、ウィンドウ・メニュー・ステータスバーの
骨格と、ジオメトリの保存/復元だけを担う。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QWidget

from anp import __version__
from anp.core.settings import Settings

logger = logging.getLogger(__name__)

_DEFAULT_SIZE = (1000, 800)


class MainWindow(QMainWindow):
    """アプリケーションのメインウィンドウ。"""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self.setWindowTitle("anp")
        self.setCentralWidget(self._create_placeholder())
        self._create_menus()
        self.statusBar().showMessage("準備完了")

        self._restore_window_state()

    # -------------------------------------------------- 構築
    def _create_placeholder(self) -> QWidget:
        """PDF を開くまでの間に表示するプレースホルダ。"""
        label = QLabel("PDF が開かれていません")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("ファイル(&F)")
        quit_action = QAction("終了(&X)", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("ヘルプ(&H)")
        about_action = QAction("anp について(&A)", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "anp について",
            f"anp {__version__}\n\n学習向けPDFリーダー",
        )

    # -------------------------------------------------- 状態の保存/復元
    def _restore_window_state(self) -> None:
        geometry = self._settings.window_geometry
        if geometry is None:
            self.resize(*_DEFAULT_SIZE)
        else:
            self.restoreGeometry(geometry)

        state = self._settings.window_state
        if state is not None:
            self.restoreState(state)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """終了時にウィンドウの位置と状態を保存する。"""
        self._settings.window_geometry = self.saveGeometry()
        self._settings.window_state = self.saveState()
        self._settings.sync()
        logger.info("main window closed")
        super().closeEvent(event)
