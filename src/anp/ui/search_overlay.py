"""検索結果のオーバーレイ描画。

`PdfView` から使う小さな道具だけを置く。`QPdfSearchModel` も
`QPdfDocument` も `PageLayout` も知らない。受け取るのは **ビューポート
座標に変換済みの矩形** だけで、座標変換はビューの側にある。

`study_marks.draw_badge()` と同じ位置付け。描き方を1箇所に集めるのは、
「全一致」と「現在の一致」で別の見た目にする判断をここだけで持つため。

塗りは半透明で、ページ画像の上に重ねるだけ。`PageColorMode`（オリジナル
/ 反転 / スマートダーク）のどれで表示していても同じ色を重ねる。
`QImage` へ焼き込まないので、色変換のキャッシュには一切影響しない。

色は白いページでも反転したページでも見えるよう、下地の明暗どちらとも
違う青にする。具体的な値は公開契約ではない。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter, QPen

# 全一致の塗り。薄く重ねるだけで、文字が読めなくならない濃さにする。
SEARCH_FILL_COLOR = QColor(0x2E, 0x9B, 0xF0, 0x50)

# 現在の一致の塗りと縁。**別の色ではなく濃さと縁で区別する**。同じ検索の
# 結果であることが分かったうえで、いま見ているのがどれかを判別できる。
CURRENT_SEARCH_FILL_COLOR = QColor(0x2E, 0x9B, 0xF0, 0xA0)
CURRENT_SEARCH_BORDER_COLOR = QColor(0x0B, 0x4F, 0x8A)

_CURRENT_BORDER_WIDTH = 1.5

# 矩形を少しだけ広げる量（論理ピクセル）。文字の外接矩形ぴったりだと
# 前後の文字と見分けが付きにくいため。倍率では伸ばさない（枠の装飾で
# あって PDF の寸法ではない。`LayoutMetrics` の隙間と同じ考え方）。
_PADDING = 1.0


def draw_search_result(
    painter: QPainter, rects: Sequence[QRectF], *, current: bool = False
) -> None:
    """検索結果1件分の矩形をすべて塗る。

    1件の結果が行をまたぐと矩形は複数になる（`QPdfLink.rectangles()`）。
    **渡された矩形は全部描く**。先頭だけで済ませない。

    `paintEvent` から呼ばれるが、行うのは軽いベクタ描画だけ。画素変換も
    画像の作り直しもしない。

    呼び出し側のペン・ブラシを壊さないよう、状態は保存して戻す。
    """
    if not rects:
        return

    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if current:
            painter.setPen(QPen(CURRENT_SEARCH_BORDER_COLOR, _CURRENT_BORDER_WIDTH))
            painter.setBrush(CURRENT_SEARCH_FILL_COLOR)
        else:
            painter.setPen(QPen(SEARCH_FILL_COLOR, 0.0))
            painter.setBrush(SEARCH_FILL_COLOR)
        for rect in rects:
            painter.drawRect(rect.adjusted(-_PADDING, -_PADDING, _PADDING, _PADDING))
    finally:
        painter.restore()
