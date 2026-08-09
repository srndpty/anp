"""永続設定への型付きアクセス。

`QSettings`（Windows ではレジストリ）を直接あちこちで触らず、この薄い
ラッパーに集約する。`QSettings` は呼び出し側から渡すため、テストでは
一時ディレクトリ上の INI 形式を使える。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings

_KEY_GEOMETRY = "window/geometry"
_KEY_WINDOW_STATE = "window/state"
_KEY_LAST_DIRECTORY = "files/last_directory"


class Settings:
    """アプリケーション設定の読み書き。"""

    def __init__(self, backend: QSettings) -> None:
        self._backend = backend

    # -------------------------------------------------- ウィンドウ
    @property
    def window_geometry(self) -> QByteArray | None:
        """保存されたウィンドウジオメトリ。未保存なら None。"""
        return self._byte_array(_KEY_GEOMETRY)

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._backend.setValue(_KEY_GEOMETRY, value)

    @property
    def window_state(self) -> QByteArray | None:
        """保存されたウィンドウ状態（ツールバー配置など）。未保存なら None。"""
        return self._byte_array(_KEY_WINDOW_STATE)

    @window_state.setter
    def window_state(self, value: QByteArray) -> None:
        self._backend.setValue(_KEY_WINDOW_STATE, value)

    # -------------------------------------------------- ファイル
    @property
    def last_directory(self) -> str:
        """最後にファイルを開いたディレクトリ。未保存なら空文字。"""
        value = self._backend.value(_KEY_LAST_DIRECTORY, "")
        return value if isinstance(value, str) else ""

    @last_directory.setter
    def last_directory(self, value: str) -> None:
        self._backend.setValue(_KEY_LAST_DIRECTORY, value)

    # -------------------------------------------------- 内部
    def sync(self) -> None:
        """変更をディスクに書き出す。"""
        self._backend.sync()

    def _byte_array(self, key: str) -> QByteArray | None:
        value = self._backend.value(key)
        return value if isinstance(value, QByteArray) else None
