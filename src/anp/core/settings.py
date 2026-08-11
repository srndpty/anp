"""永続設定への型付きアクセス。

`QSettings`（Windows ではレジストリ）を直接あちこちで触らず、この薄い
ラッパーに集約する。`QSettings` は呼び出し側から渡すため、テストでは
一時ディレクトリ上の INI 形式を使える。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

from PySide6.QtCore import QByteArray, QSettings

logger = logging.getLogger(__name__)

_KEY_GEOMETRY = "window/geometry"
_KEY_WINDOW_STATE = "window/state"
_KEY_LAST_DIRECTORY = "files/last_directory"
_KEY_RECENT_FILES = "files/recent"
_KEY_ZOOM_MODE = "view/zoom_mode"
_KEY_FREE_ZOOM = "view/free_zoom"
_KEY_PAGE_COLOR_MODE = "view/page_color_mode"
_KEY_CANVAS_THEME = "view/canvas_theme"
_KEY_UI_THEME = "ui/theme"
_KEY_SESSION_DOCUMENT = "session/document"
_KEY_SESSION_PAGE_INDEX = "session/page_index"
_KEY_SESSION_Y_NORM = "session/y_norm"

DEFAULT_ZOOM_MODE = "free"
DEFAULT_FREE_ZOOM = 1.0
DEFAULT_PAGE_COLOR_MODE = "original"
DEFAULT_CANVAS_THEME = "dark_gray"
DEFAULT_UI_THEME = "system"

# 前回の読書位置が読めなかったときに戻る場所。文書の先頭。
DEFAULT_SESSION_PAGE_INDEX = 0
DEFAULT_SESSION_Y_NORM = 0.0

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

    @property
    def recent_files(self) -> tuple[str, ...]:
        """最近開いた PDF のパス（新しい順）。未保存なら空。

        並び順と重複の除去は呼び出し側（`anp.ui.recent_files`）の契約で、
        ここは文字列の列として読み書きするだけ。INI へ1件だけ書くと
        素の文字列で返ってくるので、そこだけ吸収する。空文字や文字列で
        ない要素は落とす（**壊れた1件で一覧全体を捨てはしない**）。
        """
        value = self._backend.value(_KEY_RECENT_FILES)
        if isinstance(value, str):
            items: list[object] = [value]
        elif isinstance(value, list):
            items = list(value)
        else:
            return ()
        return tuple(item for item in items if isinstance(item, str) and item)

    @recent_files.setter
    def recent_files(self, value: Sequence[str]) -> None:
        self._backend.setValue(_KEY_RECENT_FILES, list(value))

    # -------------------------------------------------- 前回のセッション
    @property
    def last_document(self) -> str:
        """前回終了時に開いていた PDF のパス。無ければ空文字。

        **最近開いたファイルの先頭とは別物。** 「最後に読んでいた PDF」と
        「利用者が明示的に開いた履歴」は意味が違うので、同じ値から導かない。
        """
        value = self._backend.value(_KEY_SESSION_DOCUMENT, "")
        return value if isinstance(value, str) else ""

    @property
    def last_page_index(self) -> int:
        """前回終了時に読んでいたページ（0 始まり）。読めなければ先頭。"""
        value = self._backend.value(_KEY_SESSION_PAGE_INDEX, DEFAULT_SESSION_PAGE_INDEX)
        try:
            page = int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError, OverflowError):
            logger.warning("ignoring invalid session page index: %r", value)
            return DEFAULT_SESSION_PAGE_INDEX
        if page < 0:
            logger.warning("ignoring out-of-range session page index: %r", page)
            return DEFAULT_SESSION_PAGE_INDEX
        return page

    @property
    def last_y_norm(self) -> float:
        """前回終了時のページ内の縦位置（0.0〜1.0）。読めなければページ先頭。

        ビューポートのピクセル座標は保存しない。ウィンドウの大きさ・DPI・
        倍率が変わっても意味が変わらない値だけを持つ。
        """
        value = self._backend.value(_KEY_SESSION_Y_NORM, DEFAULT_SESSION_Y_NORM)
        try:
            y_norm = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning("ignoring invalid session position: %r", value)
            return DEFAULT_SESSION_Y_NORM
        if not math.isfinite(y_norm) or not 0.0 <= y_norm <= 1.0:
            logger.warning("ignoring out-of-range session position: %r", y_norm)
            return DEFAULT_SESSION_Y_NORM
        return y_norm

    def set_last_session(self, document: str, page_index: int, y_norm: float) -> None:
        """前回のセッションを丸ごと保存する。

        3つの値を別々の setter にしないのは、パスだけ新しくてページが古い
        ような中途半端な組み合わせを作れないようにするため。
        """
        self._backend.setValue(_KEY_SESSION_DOCUMENT, document)
        self._backend.setValue(_KEY_SESSION_PAGE_INDEX, page_index)
        self._backend.setValue(_KEY_SESSION_Y_NORM, y_norm)

    def clear_last_session(self) -> None:
        """前回のセッションを忘れる。

        PDF を開いていない状態で終了したときと、復元に失敗したときに呼ぶ。
        後者で消しておかないと、起動のたびに同じ失敗を繰り返す。
        """
        for key in (_KEY_SESSION_DOCUMENT, _KEY_SESSION_PAGE_INDEX, _KEY_SESSION_Y_NORM):
            self._backend.remove(key)

    # -------------------------------------------------- 表示
    @property
    def zoom_mode(self) -> str:
        """保存された倍率モードの名前。未保存や不正な型なら既定値。

        名前の妥当性（どのモードが存在するか）は UI 層が判断する。`core` は
        表示の都合を知らないので、ここでは文字列であることだけを保証する。
        """
        return self._name(_KEY_ZOOM_MODE, DEFAULT_ZOOM_MODE)

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

    @property
    def page_color_mode(self) -> str:
        """保存されたページ色変換の名前。未保存や不正な型なら既定値。

        `zoom_mode` と同じく、どのモードが存在するかは UI 層が判断する。
        `core` は文字列であることだけを保証する。
        """
        return self._name(_KEY_PAGE_COLOR_MODE, DEFAULT_PAGE_COLOR_MODE)

    @page_color_mode.setter
    def page_color_mode(self, value: str) -> None:
        self._backend.setValue(_KEY_PAGE_COLOR_MODE, value)

    # -------------------------------------------------- 外観
    @property
    def canvas_theme(self) -> str:
        """保存されたキャンバスの色の名前。未保存や不正な型なら既定値。"""
        return self._name(_KEY_CANVAS_THEME, DEFAULT_CANVAS_THEME)

    @canvas_theme.setter
    def canvas_theme(self, value: str) -> None:
        self._backend.setValue(_KEY_CANVAS_THEME, value)

    @property
    def ui_theme(self) -> str:
        """保存された UI テーマの名前。未保存や不正な型なら既定値。"""
        return self._name(_KEY_UI_THEME, DEFAULT_UI_THEME)

    @ui_theme.setter
    def ui_theme(self, value: str) -> None:
        self._backend.setValue(_KEY_UI_THEME, value)

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

    def _name(self, key: str, default: str) -> str:
        """列挙の名前として保存されている文字列。

        値の妥当性（その名前の選択肢が存在するか）は UI 層が判断する。
        ここでは空でない文字列であることだけを保証する。
        """
        value = self._backend.value(key, default)
        return value if isinstance(value, str) and value else default

    def _byte_array(self, key: str) -> QByteArray | None:
        value = self._backend.value(key)
        return value if isinstance(value, QByteArray) else None
