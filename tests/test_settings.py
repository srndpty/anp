"""`anp.core.settings` のテスト。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings

from anp.core.settings import Settings


def test_unset_values_return_defaults(settings: Settings) -> None:
    """未保存のキーは None または空文字を返す。"""
    assert settings.window_geometry is None
    assert settings.window_state is None
    assert settings.last_directory == ""


def test_values_round_trip(settings: Settings) -> None:
    """書き込んだ値をそのまま読み戻せる。"""
    settings.window_geometry = QByteArray(b"geometry-blob")
    settings.window_state = QByteArray(b"state-blob")
    settings.last_directory = r"C:\books"

    assert settings.window_geometry == QByteArray(b"geometry-blob")
    assert settings.window_state == QByteArray(b"state-blob")
    assert settings.last_directory == r"C:\books"


def test_values_persist_across_instances(tmp_path: Path) -> None:
    """別インスタンスからも読み出せる（実際に永続化されている）。"""
    ini = str(tmp_path / "settings.ini")

    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.last_directory = r"D:\docs"
    first.sync()

    second = Settings(QSettings(ini, QSettings.Format.IniFormat))
    assert second.last_directory == r"D:\docs"
