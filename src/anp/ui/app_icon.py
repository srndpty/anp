"""アプリのアイコン（ウィンドウ・タスクバー・実行ファイル）を描く。

ツールバーのアイコン（`anp.ui.icons`）とは性格が違うので分ける。あちらは
UI テーマに合わせて色が変わる線画だが、こちらは **アプリの見分けが付く
ことだけが仕事** で、どの背景でも同じ絵になる。

絵柄は「角を折った紙 ＋ 学習マークのバッジ」。非破壊アノテーションという
anp の中身がそのまま出るようにした。

色は `anp.ui.study_marks` を参照せず、ここに写しを持つ。実行ファイルへ
埋め込む `.ico` は `tools/make_app_icon.py` で焼き直すまで変わらないので、
参照にすると「バッジの色を変えたのにアイコンだけ古い」というずれが黙って
生まれる。**写しであることを承知で複製する。**
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

# `.ico` に入れる大きさ。Windows のエクスプローラは 16/32/48、
# 大アイコン表示とタスクバーのジャンプリストが 256 を使う。
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

_PAPER_COLOR = QColor(0xFA, 0xFA, 0xFA)
_INK_COLOR = QColor(0x1A, 0x1A, 0x1A)
_TEXT_LINE_COLOR = QColor(0x6E, 0x6E, 0x6E)
# 学習マークのバッジと同じ黄色（`anp.ui.study_marks` の写し）。
_BADGE_COLOR = QColor(0xFF, 0xC1, 0x07)


def app_icon() -> QIcon:
    """全サイズを載せたアプリアイコン。"""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(app_icon_pixmap(size))
    return icon


def app_icon_pixmap(size: int) -> QPixmap:
    """1枚描く。位置と太さは一辺に対する割合だけで決める。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _draw_page(painter, float(size))
        _draw_badge(painter, float(size))
    finally:
        painter.end()
    return pixmap


def _draw_page(painter: QPainter, size: float) -> None:
    """角を折った紙と、本文に見立てた3本の線。"""
    stroke = size * 0.055
    left, right = size * 0.14, size * 0.78
    top, bottom = size * 0.08, size * 0.92
    fold = size * 0.20  # 折り返しの一辺

    outline = QPainterPath()
    outline.moveTo(left, top)
    outline.lineTo(right - fold, top)
    outline.lineTo(right, top + fold)
    outline.lineTo(right, bottom)
    outline.lineTo(left, bottom)
    outline.closeSubpath()

    painter.setPen(QPen(_INK_COLOR, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.setBrush(_PAPER_COLOR)
    painter.drawPath(outline)

    # 折り返し。紙の縁と同じ線で、折り目だけを引く。
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolyline(
        [
            QPointF(right - fold, top),
            QPointF(right - fold, top + fold),
            QPointF(right, top + fold),
        ]
    )

    # 本文。最後の1本は短くして、バッジが重なる右下を空ける。
    painter.setPen(
        QPen(_TEXT_LINE_COLOR, stroke * 0.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    )
    text_left = left + size * 0.10
    text_right = right - size * 0.08
    for row, line_end in enumerate((text_right, text_right, text_right - size * 0.14)):
        y = top + size * (0.36 + 0.14 * row)
        painter.drawLine(QPointF(text_left, y), QPointF(line_end, y))


def _draw_badge(painter: QPainter, size: float) -> None:
    """学習マークのバッジ。紙の右下角に重ねる。

    **中に数字は入れない。** 16px では潰れて滲むだけで、バッジの丸と色
    そのものが既に anp の目印になっている。
    """
    painter.setPen(QPen(_INK_COLOR, size * 0.055))
    painter.setBrush(_BADGE_COLOR)
    painter.drawEllipse(QPointF(size * 0.72, size * 0.74), size * 0.24, size * 0.24)
