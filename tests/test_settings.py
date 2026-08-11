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


@pytest.mark.parametrize("mode", ["invert", "smart_dark"])
def test_the_page_color_mode_round_trips(tmp_path: Path, mode: str) -> None:
    """ページの色を読み戻せる。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.page_color_mode = mode
    first.sync()

    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).page_color_mode == mode


def test_a_broken_page_color_mode_falls_back(tmp_path: Path) -> None:
    """文字列でないページの色は既定値へ落とす。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/page_color_mode", QByteArray(b"\x00\x01"))

    assert Settings(backend).page_color_mode == "original"


def test_unset_canvas_theme_returns_the_default(settings: Settings) -> None:
    """キャンバスの設定が無ければダークグレー。"""
    assert settings.canvas_theme == "dark_gray"


def test_the_canvas_theme_round_trips(tmp_path: Path) -> None:
    """キャンバスの色を読み戻せる。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.canvas_theme = "black"
    first.sync()

    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).canvas_theme == "black"


def test_a_broken_canvas_theme_falls_back(tmp_path: Path) -> None:
    """文字列でないキャンバスの色は既定値へ落とす。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("view/canvas_theme", QByteArray(b"\x00\x01"))

    assert Settings(backend).canvas_theme == "dark_gray"


def test_unset_ui_theme_returns_the_default(settings: Settings) -> None:
    """UI テーマの設定が無ければシステム。"""
    assert settings.ui_theme == "system"


def test_the_ui_theme_round_trips(tmp_path: Path) -> None:
    """UI テーマを読み戻せる。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.ui_theme = "dark"
    first.sync()

    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).ui_theme == "dark"


def test_a_broken_ui_theme_falls_back(tmp_path: Path) -> None:
    """文字列でない UI テーマは既定値へ落とす。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("ui/theme", QByteArray(b"\x00\x01"))

    assert Settings(backend).ui_theme == "system"


# ---------------------------------------------------------------- 最近使ったファイル
def test_unset_recent_files_is_empty(settings: Settings) -> None:
    """履歴が未保存なら空。既存利用者の設定にキーが無くても壊れない。"""
    assert settings.recent_files == ()


def test_recent_files_round_trip(tmp_path: Path) -> None:
    """履歴を並びのまま読み戻せる。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.recent_files = [r"C:\books\a.pdf", r"C:\books\b.pdf"]
    first.sync()

    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).recent_files == (
        r"C:\books\a.pdf",
        r"C:\books\b.pdf",
    )


def test_a_single_recent_file_round_trips(tmp_path: Path) -> None:
    """1件だけ書くと INI からは素の文字列で返るが、列として読み戻せる。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.recent_files = [r"C:\books\only.pdf"]
    first.sync()

    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).recent_files == (
        r"C:\books\only.pdf",
    )


def test_an_emptied_recent_list_round_trips(tmp_path: Path) -> None:
    """クリアした履歴は空のまま戻る。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.recent_files = [r"C:\books\a.pdf"]
    first.recent_files = []
    first.sync()

    assert Settings(QSettings(ini, QSettings.Format.IniFormat)).recent_files == ()


@pytest.mark.parametrize("stored", [42, QByteArray(b"\x00\x01"), ["", r"C:\books\a.pdf", 7]])
def test_broken_recent_files_are_dropped(tmp_path: Path, stored: object) -> None:
    """壊れた値が入っていても、読める分だけを返して起動を止めない。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("files/recent", stored)

    recent = Settings(backend).recent_files

    assert all(isinstance(item, str) and item for item in recent)


# ---------------------------------------------------------------- 前回のセッション
def test_unset_session_returns_defaults(settings: Settings) -> None:
    """セッションが未保存なら、復元対象なし・先頭ページ。"""
    assert settings.last_document == ""
    assert settings.last_page_index == 0
    assert settings.last_y_norm == pytest.approx(0.0)


def test_the_session_round_trips(tmp_path: Path) -> None:
    """読んでいた PDF と位置を読み戻せる。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.set_last_session(r"C:\books\a.pdf", 42, 0.65)
    first.sync()

    second = Settings(QSettings(ini, QSettings.Format.IniFormat))
    assert second.last_document == r"C:\books\a.pdf"
    assert second.last_page_index == 42
    assert second.last_y_norm == pytest.approx(0.65)


def test_clearing_the_session_forgets_everything(tmp_path: Path) -> None:
    """セッションを忘れると、復元対象も位置も既定へ戻る。"""
    ini = str(tmp_path / "settings.ini")
    first = Settings(QSettings(ini, QSettings.Format.IniFormat))
    first.set_last_session(r"C:\books\a.pdf", 42, 0.65)
    first.clear_last_session()
    first.sync()

    second = Settings(QSettings(ini, QSettings.Format.IniFormat))
    assert second.last_document == ""
    assert second.last_page_index == 0
    assert second.last_y_norm == pytest.approx(0.0)


def test_clearing_the_session_keeps_the_other_settings(tmp_path: Path) -> None:
    """セッションを忘れても、最後のディレクトリや履歴には触らない。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings = Settings(backend)
    settings.last_directory = r"C:\books"
    settings.recent_files = [r"C:\books\a.pdf"]
    settings.set_last_session(r"C:\books\a.pdf", 3, 0.5)

    settings.clear_last_session()

    assert settings.last_directory == r"C:\books"
    assert settings.recent_files == (r"C:\books\a.pdf",)


def test_a_broken_last_document_falls_back(tmp_path: Path) -> None:
    """文字列でない復元対象は「無し」として扱う。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("session/document", QByteArray(b"\x00\x01"))

    assert Settings(backend).last_document == ""


@pytest.mark.parametrize("stored", ["", "abc", "-10", "3.5abc", "nan", "inf", "1e400"])
def test_a_broken_page_index_falls_back(tmp_path: Path, stored: str) -> None:
    """壊れたページ番号は先頭ページへ落とす。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("session/page_index", stored)

    assert Settings(backend).last_page_index == 0


def test_a_page_index_of_a_wrong_type_falls_back(tmp_path: Path) -> None:
    """int にできない型が入っていても落ちない。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("session/page_index", QByteArray(b"\x00\x01"))

    assert Settings(backend).last_page_index == 0


@pytest.mark.parametrize("stored", ["", "abc", "-0.5", "2", "nan", "inf", "-inf"])
def test_a_broken_y_norm_falls_back(tmp_path: Path, stored: str) -> None:
    """壊れた縦位置はページ先頭へ落とす。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    backend.setValue("session/y_norm", stored)

    assert Settings(backend).last_y_norm == pytest.approx(0.0)


def test_the_session_boundary_values_are_accepted(tmp_path: Path) -> None:
    """0.0 と 1.0 は正しい縦位置なので落とさない。"""
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings = Settings(backend)

    settings.set_last_session(r"C:\a.pdf", 0, 0.0)
    assert settings.last_y_norm == pytest.approx(0.0)

    settings.set_last_session(r"C:\a.pdf", 0, 1.0)
    assert settings.last_y_norm == pytest.approx(1.0)


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
