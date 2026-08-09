"""`anp.core.settings` のテスト。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
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


def test_sync_failure_is_logged_but_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """書き出しに失敗しても例外にせず、警告を残す。"""
    # QSettings は無い親ディレクトリを作ってしまうので、ファイルを親に見立てて塞ぐ。
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    settings = Settings(QSettings(str(blocker / "settings.ini"), QSettings.Format.IniFormat))
    settings.last_directory = r"C:\books"

    with caplog.at_level(logging.WARNING):
        settings.sync()

    assert "failed to persist settings" in caplog.text


def test_values_persist_across_instances(tmp_path: Path) -> None:
    """別インスタンスからも読み出せる（実際に永続化されている）。"""
    ini = str(tmp_path / "settings.ini")

    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.last_directory = r"D:\docs"
    first.sync()

    second = Settings(QSettings(ini, QSettings.Format.IniFormat))
    assert second.last_directory == r"D:\docs"
