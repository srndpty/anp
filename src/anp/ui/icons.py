"""ツールバーのアイコンを描く。

**画像ファイルもアイコンフォントも持ち込まない。** 必要なのは拡大・縮小の
虫眼鏡とページ送りの山形だけなので、`QPainter` でその場で描く。Material
Symbols のようなアイコンフォントを1組入れると、フォントファイルの同梱・
ライセンス表記・グリフ名の管理が付いてくるうえ、`QIcon.fromTheme()` は
Windows ではテーマが無く空のアイコンを返す。

色は呼び出し側から受け取る。UI テーマ（明暗）でパレットが変わるので、
ここで色を決め打ちにすると暗いツールバーに黒いアイコンが沈む。

形は大きさに対する **割合** だけで決める。特定のピクセル数に合わせた
微調整（ヒンティング）はしない。線の太さと描き方（丸い端・アンチ
エイリアス）は全アイコンで揃えるので、`_pixmap()` の1箇所で決める。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

# 用意しておく大きさ（ピクセル）。Qt は要求より大きいものがあればそれを
# 縮小して使うので、100%〜200% の DPI スケーリングで拡大されずに済む。
_ICON_SIZES = (16, 24, 32, 48)

# 1つのアイコンを描く手続き。引数は (画家, 一辺の長さ, 線の太さ)。
# 大きさは呼ばれるたびに変わるので、描く側は割合で位置を決める。
_Draw = Callable[[QPainter, float, float], None]


def zoom_in_icon(color: QColor) -> QIcon:
    """虫眼鏡に + を入れたアイコン。"""
    return _icon(partial(_draw_magnifier, plus=True), color)


def zoom_out_icon(color: QColor) -> QIcon:
    """虫眼鏡に - を入れたアイコン。"""
    return _icon(partial(_draw_magnifier, plus=False), color)


def previous_page_icon(color: QColor) -> QIcon:
    """左向きの山形。"""
    return _icon(partial(_draw_chevron, forward=False), color)


def next_page_icon(color: QColor) -> QIcon:
    """右向きの山形。

    ページ送りを上下ではなく左右の山形にするのは、← → のページ移動と
    向きを揃えるため（`PdfView.keyPressEvent()`）。
    """
    return _icon(partial(_draw_chevron, forward=True), color)


def _icon(draw: _Draw, color: QColor) -> QIcon:
    icon = QIcon()
    for size in _ICON_SIZES:
        icon.addPixmap(_pixmap(draw, color, size))
    return icon


def _pixmap(draw: _Draw, color: QColor, size: int) -> QPixmap:
    """1枚描く。下地・線の太さ・画家の設定はここだけで決める。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    stroke = max(size * 0.09, 1.0)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(color, stroke)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        draw(painter, float(size), stroke)
    finally:
        painter.end()
    return pixmap


def _draw_magnifier(painter: QPainter, size: float, stroke: float, *, plus: bool) -> None:
    """レンズを左上に寄せ、柄を右下へ出す。

    半径は線の太さの半分だけ内側に取るので、どの大きさでも端が切れない。
    """
    center = QPointF(size * 0.42, size * 0.42)
    radius = size * 0.30 - stroke / 2
    # レンズの縁から 45 度方向へ柄を伸ばす。円の内側には入り込ませない。
    edge = radius * 0.707
    handle_end = size - stroke / 2
    arm = radius * 0.5

    painter.drawEllipse(center, radius, radius)
    painter.drawLine(center + QPointF(edge, edge), QPointF(handle_end, handle_end))
    painter.drawLine(center - QPointF(arm, 0.0), center + QPointF(arm, 0.0))
    if plus:
        painter.drawLine(center - QPointF(0.0, arm), center + QPointF(0.0, arm))


def _draw_chevron(painter: QPainter, size: float, stroke: float, *, forward: bool) -> None:
    """進む向きへ尖った山形。"""
    tip = size * 0.70 if forward else size * 0.30
    back = size * 0.30 if forward else size * 0.70
    top = size * 0.18 + stroke / 2
    bottom = size * 0.82 - stroke / 2

    painter.drawPolyline(
        QPolygonF(
            [
                QPointF(back, top),
                QPointF(tip, size * 0.5),
                QPointF(back, bottom),
            ]
        )
    )
