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


def test_unset_zoom_returns_defaults(settings: Settings) -> None:
    """倍率の設定が無ければ既定値。"""
    assert settings.zoom_mode == "free"
    assert settings.free_zoom == pytest.approx(1.0)


def test_zoom_values_round_trip(settings: Settings) -> None:
    """倍率モードと手動倍率を読み戻せる。"""
    settings.zoom_mode = "fit_width"
    settings.free_zoom = 1.75

    assert settings.zoom_mode == "fit_width"
    assert settings.free_zoom == pytest.approx(1.75)


def test_zoom_values_persist_as_text(tmp_path: Path) -> None:
    """INI に文字列として書かれても数値として読み戻せる。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.free_zoom = 2.5
    first.zoom_mode = "fit_page"
    first.sync()

    second = Settings(QSettings(ini, QSettings.Format.IniFormat))
    assert second.free_zoom == pytest.approx(2.5)
    assert second.zoom_mode == "fit_page"


@pytest.mark.parametrize("stored", ["", "とても大きく", "0", "-3", "1e9", "nan"])
def test_broken_free_zoom_falls_back(tmp_path: Path, stored: str) -> None:
    """壊れた倍率が保存されていたら既定値へ落とす。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/free_zoom", stored)

    assert Settings(backend).free_zoom == pytest.approx(1.0)


def test_broken_zoom_mode_falls_back(tmp_path: Path) -> None:
    """文字列でない倍率モードは既定値へ落とす。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/zoom_mode", QByteArray(b"\x00\x01"))

    assert Settings(backend).zoom_mode == "free"


def test_unset_page_color_mode_returns_the_default(settings: Settings) -> None:
    """ページの色の設定が無ければ既定値。"""
    assert settings.page_color_mode == "original"


def test_the_page_color_mode_round_trips(tmp_path: Path) -> None:
    """ページの色を読み戻せる。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.page_color_mode = "invert"
    first.sync()

    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).page_color_mode == "invert"


def test_a_broken_page_color_mode_falls_back(tmp_path: Path) -> None:
    """文字列でないページの色は既定値へ落とす。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/page_color_mode", QByteArray(b"\x00\x01"))

    assert Settings(backend).page_color_mode == "original"


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
