"""アプリケーションの起動処理。

ここがアプリケーション境界なので、未捕捉例外のログ出力とダイアログ表示も
この層で行う。

**anp は同時に1つしか動かさない**（`QLockFile`）。`StudyMarkController` は
「表示中のスナップショットと DB の内容は一致する」を不変条件にしていて、
更新のたびに全件を読み直さずに済ませている。これは書き手がこのプロセス
だけであることが前提なので、二重起動を許すと片方の画面が古い件数を
出したままになる。個人用のリーダーで複数ウィンドウの必要もないため、
「後から起動した方は開かない」を契約にしてここで守る。
"""

from __future__ import annotations

import logging
import sys
from types import TracebackType

from PySide6.QtCore import QCoreApplication, QLockFile, QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from anp.core.logging import setup_logging
from anp.core.paths import AppPaths
from anp.core.settings import Settings
from anp.storage import database
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

ORGANIZATION_NAME = "anp"
APPLICATION_NAME = "anp"

# 二重起動を知らせるダイアログ。既に開いているウィンドウを前面に出す仕組みは
# 持たないので、そこまでは書かない（誘導できないことを約束しない）。
_ALREADY_RUNNING_TITLE = "anp は既に起動しています"
_ALREADY_RUNNING_TEXT = (
    "anp は既に起動しています。\n\n学習マークの記録が食い違わないよう、同時に1つだけ起動します。"
)

# ロックの持ち主が生きているかを Qt に確かめさせるまでの時間（ミリ秒）。
# これを過ぎたロックは、プロセスの生死を見たうえで無効と判断される。
# 強制終了やブルースクリーンでロックが残っても、次の起動で回復する。
_STALE_LOCK_MS = 30_000

# 起動できなかったときの終了コード。
EXIT_ALREADY_RUNNING = 1


def _install_excepthook() -> None:
    """未捕捉例外をログに残し、利用者にも知らせる。"""

    def hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
        QMessageBox.critical(
            None,
            "予期しないエラー",
            f"予期しないエラーが発生しました。\n\n{exc_type.__name__}: {exc_value}",
        )

    sys.excepthook = hook


def main() -> int:
    """アプリケーションを起動し、終了コードを返す。"""
    QCoreApplication.setOrganizationName(ORGANIZATION_NAME)
    QCoreApplication.setApplicationName(APPLICATION_NAME)

    app = QApplication(sys.argv)

    paths = AppPaths.from_standard_paths(APPLICATION_NAME)
    paths.ensure_directories()
    setup_logging(paths.log_file)
    logger.info("anp starting up")

    _install_excepthook()

    # **DB を開く前にロックを取る。** 取れなければウィンドウも接続も作らない。
    lock = QLockFile(str(paths.lock_file))
    lock.setStaleLockTime(_STALE_LOCK_MS)
    if not lock.tryLock(0):
        logger.warning("another instance is already running")
        QMessageBox.information(None, _ALREADY_RUNNING_TITLE, _ALREADY_RUNNING_TEXT)
        return EXIT_ALREADY_RUNNING

    # SQLite の接続はアプリケーションが所有する。ウィンドウやリポジトリより
    # 寿命が長く、閉じるのはここだけ（`__del__` には頼らない）。学習マークの
    # 操作ごとに開き直さないよう、接続は1本を起動から終了まで使い回す。
    #
    # 接続やマイグレーションに失敗したらここで落ちる。学習マークが保存
    # されない状態でリーダーだけ動かすと、記録できているつもりの学習が
    # 失われるため、握り潰さずに未捕捉例外の経路（ログ＋ダイアログ）へ渡す。
    try:
        connection = database.connect(paths.database_file)
        try:
            settings = Settings(QSettings())
            window = MainWindow(settings, StudyMarkRepository(connection))
            window.show()

            exit_code = app.exec()
        finally:
            connection.close()
    finally:
        # ロックはプロセスの終了でも外れるが、次の起動を待たせないよう明示的に外す。
        lock.unlock()

    logger.info("anp exited with code %d", exit_code)
    return exit_code
