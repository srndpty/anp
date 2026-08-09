"""テスト共通の設定。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from anp.core.settings import Settings

# QApplication が作られる前にオフスクリーンを指定し、ローカルと CI で挙動を揃える。
# PySide6 の import 自体はプラットフォームプラグインを読み込まないため、
# import の後に設定しても間に合う。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """一時ファイル上の INI を使う設定オブジェクト。

    実環境のレジストリを汚さないために INI 形式を使う。
    """
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return Settings(backend)
