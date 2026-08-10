"""ビューアの外観（キャンバスと UI の配色）。

**PDF のページ画像の色変換（`anp.pdf.color.PageColorMode`）とは別の軸。**
ここで扱うのはページの外側と、アプリのウィジェットの見た目だけで、
PDF の画素には一切触らない。

anp の外観は次の3つを独立した軸として扱う。1つの `dark_mode` にまとめない。

- `UiTheme`: メニュー・ツールバーなどウィジェットの配色
- `CanvasTheme`: ページの外側（キャンバス）の色
- `PageColorMode`: ページ画像そのものの色変換（`anp.pdf.color`）

「UI が暗いならページも反転」のような連動はしない。どの組み合わせも
利用者が選んだとおりに使う。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication


class CanvasTheme(Enum):
    """ページの外側（キャンバス）の色。

    値は設定への保存にそのまま使う文字列。
    """

    BLACK = "black"
    """真っ黒。反転したページと合わせると画面全体が暗くなる。"""

    DARK_GRAY = "dark_gray"
    """既定。ページの縁が見分けやすい中間の暗さ。"""

    WHITE = "white"
    """真っ白。明るい環境で紙に近い見え方にするため。"""


class UiTheme(Enum):
    """アプリのウィジェットの配色。

    値は設定への保存にそのまま使う文字列。
    """

    SYSTEM = "system"
    """OS / Qt の標準の配色に従う。"""

    LIGHT = "light"
    """明るい配色を強制する。"""

    DARK = "dark"
    """暗い配色を強制する。"""


_CANVAS_COLORS = {
    CanvasTheme.BLACK: QColor(0x00, 0x00, 0x00),
    CanvasTheme.DARK_GRAY: QColor(0x52, 0x56, 0x59),
    CanvasTheme.WHITE: QColor(0xFF, 0xFF, 0xFF),
}

# SYSTEM に対応する値はここには置かない。SYSTEM は「どれかの配色を選ぶ」のでは
# なく「明示した指定を解除する」ことなので、`apply_ui_theme()` で扱う。
_COLOR_SCHEMES = {
    UiTheme.LIGHT: Qt.ColorScheme.Light,
    UiTheme.DARK: Qt.ColorScheme.Dark,
}


def canvas_color(theme: CanvasTheme) -> QColor:
    """キャンバスを塗る色。"""
    return _CANVAS_COLORS[theme]


def apply_ui_theme(theme: UiTheme) -> None:
    """アプリ全体のウィジェットの配色を切り替える。

    Qt 6.8 以降の `QStyleHints` のカラースキームを使う。スタイルシートも
    パレットの手組みも使わないので、Windows ネイティブスタイルのまま
    明暗が切り替わり、スクロールバーやメッセージボックスにも及ぶ。

    `SYSTEM` は明示した指定を解除して表す。`setColorScheme()` に
    `Qt.ColorScheme.Unknown` を渡しても同じく解除されるが、意図が読み取り
    やすい `unsetColorScheme()` を使う。解除後の OS 設定への追随は Qt が
    行うので、レジストリの監視やポーリングは要らない。

    キャンバスとページ画像はここでは変わらない。PDF の描画は
    `QStyleHints` を見ていない。
    """
    scheme = _COLOR_SCHEMES.get(theme)
    hints = QGuiApplication.styleHints()
    if scheme is None:
        hints.unsetColorScheme()
    else:
        hints.setColorScheme(scheme)
