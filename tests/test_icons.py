"""`anp.ui.icons` のテスト。

見た目の良し悪しは測れないので、確かめるのは「描けていること」「渡した色で
描かれること」「拡大と縮小が別の絵になること」だけ。
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication

from anp.ui.icons import (
    next_page_icon,
    previous_page_icon,
    zoom_in_icon,
    zoom_out_icon,
)

_SIZE = QSize(32, 32)


def ink(icon: QIcon) -> int:
    """塗られている量（不透明度の合計）。"""
    image = icon.pixmap(_SIZE).toImage()
    return sum(
        image.pixelColor(x, y).alpha() for y in range(image.height()) for x in range(image.width())
    )


def color_count(icon: QIcon, color: QColor) -> int:
    """その色で塗られた画素の数。"""
    image = icon.pixmap(_SIZE).toImage()
    return sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y) == color
    )


def tip_x(icon: QIcon) -> float:
    """高さの中央で塗られている画素の x の平均。

    山形は先端だけが中央の高さを通る（付け根の2本は上下の端にある）ので、
    この値がどちら向きかを表す。
    """
    image = icon.pixmap(_SIZE).toImage()
    middle = image.height() // 2
    columns = [x for x in range(image.width()) if image.pixelColor(x, middle).alpha() > 0]
    assert columns
    return sum(columns) / len(columns)


def test_the_icons_are_drawn(qapp: QApplication) -> None:
    """4種類とも、要求した大きさで中身のある絵になる。"""
    color = QColor(0, 0, 0)
    for icon in (
        zoom_in_icon(color),
        zoom_out_icon(color),
        previous_page_icon(color),
        next_page_icon(color),
    ):
        assert not icon.isNull()
        assert icon.pixmap(_SIZE).size() == _SIZE
        assert ink(icon) > 0


def test_the_icon_uses_the_given_color(qapp: QApplication) -> None:
    """色は引数で決まる。UI テーマに合わせて描き直せる、という契約。"""
    red = QColor(255, 0, 0)
    blue = QColor(0, 0, 255)

    assert color_count(zoom_in_icon(red), red) > 0
    assert color_count(zoom_in_icon(red), blue) == 0
    assert color_count(zoom_in_icon(blue), blue) > 0


def test_plus_uses_more_ink_than_minus(qapp: QApplication) -> None:
    """+ の縦棒の分だけ拡大の方が塗られる。2つが同じ絵ではないことの確認。"""
    color = QColor(0, 0, 0)

    assert ink(zoom_in_icon(color)) > ink(zoom_out_icon(color))


def test_the_page_chevrons_point_in_opposite_directions(qapp: QApplication) -> None:
    """次のページは右、前のページは左を向く。"""
    color = QColor(0, 0, 0)
    middle = _SIZE.width() / 2

    assert tip_x(next_page_icon(color)) > middle
    assert tip_x(previous_page_icon(color)) < middle
