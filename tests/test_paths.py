"""`anp.core.paths` のテスト。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication

from anp.core.paths import AppPaths


def test_derived_paths_are_under_data_dir(tmp_path: Path) -> None:
    """派生パスはすべてデータディレクトリ配下になる。"""
    paths = AppPaths(data_dir=tmp_path)

    assert paths.log_dir == tmp_path / "logs"
    assert paths.log_file == tmp_path / "logs" / "anp.log"
    assert paths.database_file == tmp_path / "anp.sqlite3"


def test_ensure_directories_creates_them(tmp_path: Path) -> None:
    """必要なディレクトリが作成される。"""
    paths = AppPaths(data_dir=tmp_path / "anp")

    paths.ensure_directories()

    assert paths.data_dir.is_dir()
    assert paths.log_dir.is_dir()


def test_from_standard_paths_uses_generic_data_location(qapp: QApplication) -> None:
    """ローミングしない位置に、アプリ名が一度だけ付いた場所を使う。

    Windows では `GenericDataLocation` が `AppData\\Local`、`AppDataLocation` が
    ローミング側。さらに後者は organizationName と applicationName の両方を足す
    ため `anp\\anp` と二重になる。どちらの間違いも検出する。
    """
    expected = (
        Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation))
        / "anp"
    )

    paths = AppPaths.from_standard_paths("anp")

    assert paths.data_dir == expected
    assert paths.data_dir.parent.name != "anp"


def test_ensure_directories_is_idempotent(tmp_path: Path) -> None:
    """繰り返し呼んでも失敗しない。"""
    paths = AppPaths(data_dir=tmp_path / "anp")

    paths.ensure_directories()
    paths.ensure_directories()

    assert paths.log_dir.is_dir()
