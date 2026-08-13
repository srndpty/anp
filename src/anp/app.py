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
from pathlib import Path
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

_LOCK_FAILED_TITLE = "anp を起動できません"
_LOCK_PERMISSION_TEXT = (
    "起動に必要なロックファイルを作成できませんでした。\n\n{path}\n\n"
    "この場所へ書き込めるかを確認してください。"
)
_LOCK_UNKNOWN_TEXT = (
    "起動に必要なロックファイルを作成できませんでした。\n\nログを確認してください。"
)

# ロックを保持するのは **アプリが動いている間ずっと**。この使い方では
# 経過時間で無効と判断させてはいけないので 0 にする（Qt の既定の 30 秒は、
# 短時間で終わる処理向け）。0 でも、ロックを持っていたプロセスが居なく
# なっていれば Qt が PID を見て無効と判断するので、強制終了で残った
# ロックは次の起動で回復する。
_STALE_LOCK_DISABLED = 0

# 起動できなかったときの終了コード。
EXIT_ALREADY_RUNNING = 1
EXIT_LOCK_FAILED = 2


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


def _report_lock_failure(lock: QLockFile, path: Path) -> int:
    """ロックを取れなかった理由を知らせ、終了コードを返す。

    **取れない ＝ 二重起動、ではない。** `QLockFile.error()` は「他が持って
    いる（`LockFailedError`）」のほかに、ロックファイルを作れない
    （`PermissionError`）や原因不明（`UnknownError`）も返す。まとめて
    「既に起動しています」と伝えると、書き込めないディレクトリが原因の
    ときに利用者が延々と別のウィンドウを探すことになる。
    """
    error = lock.error()
    if error == QLockFile.LockError.LockFailedError:
        logger.warning("another instance is already running")
        QMessageBox.information(None, _ALREADY_RUNNING_TITLE, _ALREADY_RUNNING_TEXT)
        return EXIT_ALREADY_RUNNING

    logger.error("failed to acquire the lock file %s: %s", path, error.name)
    text = (
        _LOCK_PERMISSION_TEXT.format(path=path)
        if error == QLockFile.LockError.PermissionError
        else _LOCK_UNKNOWN_TEXT
    )
    QMessageBox.critical(None, _LOCK_FAILED_TITLE, text)
    return EXIT_LOCK_FAILED


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
    lock.setStaleLockTime(_STALE_LOCK_DISABLED)
    if not lock.tryLock(0):
        return _report_lock_failure(lock, paths.lock_file)

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
