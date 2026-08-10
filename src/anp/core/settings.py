"""永続設定への型付きアクセス。

`QSettings`（Windows ではレジストリ）を直接あちこちで触らず、この薄い
ラッパーに集約する。`QSettings` は呼び出し側から渡すため、テストでは
一時ディレクトリ上の INI 形式を使える。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, QSettings

logger = logging.getLogger(__name__)

_KEY_GEOMETRY = "window/geometry"
_KEY_WINDOW_STATE = "window/state"
_KEY_LAST_DIRECTORY = "files/last_directory"
_KEY_ZOOM_MODE = "view/zoom_mode"
_KEY_FREE_ZOOM = "view/free_zoom"

DEFAULT_ZOOM_MODE = "free"
DEFAULT_FREE_ZOOM = 1.0

# 保存された倍率として受け付ける範囲。UI 側の上下限とは独立に、壊れた値を
# ここで弾く。`core` は表示の都合を知らないので、緩めの健全性チェックに留める。
_MIN_FREE_ZOOM = 0.01
_MAX_FREE_ZOOM = 100.0


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

    # -------------------------------------------------- 表示
    @property
    def zoom_mode(self) -> str:
        """保存された倍率モードの名前。未保存や不正な型なら既定値。

        名前の妥当性（どのモードが存在するか）は UI 層が判断する。`core` は
        表示の都合を知らないので、ここでは文字列であることだけを保証する。
        """
        value = self._backend.value(_KEY_ZOOM_MODE, DEFAULT_ZOOM_MODE)
        return value if isinstance(value, str) and value else DEFAULT_ZOOM_MODE

    @zoom_mode.setter
    def zoom_mode(self, value: str) -> None:
        self._backend.setValue(_KEY_ZOOM_MODE, value)

    @property
    def free_zoom(self) -> float:
        """保存された手動指定の倍率。未保存や壊れていれば 1.0。

        INI から読むと数値も文字列で返るので、数に変換できるかを確かめる。
        """
        value = self._backend.value(_KEY_FREE_ZOOM, DEFAULT_FREE_ZOOM)
        try:
            zoom = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning("ignoring invalid free zoom setting: %r", value)
            return DEFAULT_FREE_ZOOM
        if not _MIN_FREE_ZOOM <= zoom <= _MAX_FREE_ZOOM:
            logger.warning("ignoring out-of-range free zoom setting: %r", zoom)
            return DEFAULT_FREE_ZOOM
        return zoom

    @free_zoom.setter
    def free_zoom(self, value: float) -> None:
        self._backend.setValue(_KEY_FREE_ZOOM, value)

    # -------------------------------------------------- 内部
    def sync(self) -> None:
        """変更を書き出す。

        書き出しに失敗しても例外にはしない。ウィンドウ位置を保存できない
        程度のことでアプリを終了できなくする必要はないため、警告を残して
        処理を続ける。ただし黙って失敗させはしない。
        """
        self._backend.sync()
        status = self._backend.status()
        if status != QSettings.Status.NoError:
            logger.warning("failed to persist settings: %s", status.name)

    def _byte_array(self, key: str) -> QByteArray | None:
        value = self._backend.value(key)
        return value if isinstance(value, QByteArray) else None
