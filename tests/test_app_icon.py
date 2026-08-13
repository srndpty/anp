"""`anp.ui.app_icon` と、そこから焼いた `packaging/anp.ico` のテスト。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication

from anp.ui.app_icon import ICON_SIZES, app_icon, app_icon_pixmap

_ICO = Path(__file__).parents[1] / "packaging" / "anp.ico"


def test_every_size_is_drawn(qapp: QApplication) -> None:
    """どの大きさでも中身のある絵になる。"""
    for size in ICON_SIZES:
        pixmap = app_icon_pixmap(size)
        assert pixmap.size() == QSize(size, size)
        image = pixmap.toImage()
        assert any(
            image.pixelColor(x, y).alpha() > 0
            for y in range(image.height())
            for x in range(image.width())
        )


def test_the_icon_carries_all_sizes(qapp: QApplication) -> None:
    """`QIcon` に全サイズが載っている。"""
    assert app_icon().availableSizes() == [QSize(size, size) for size in ICON_SIZES]


def test_the_committed_ico_matches_the_sizes(qapp: QApplication) -> None:
    """焼いてある `.ico` が、いまの大きさ一覧をそのまま持っている。

    実行ファイルへ埋め込むのはこのファイルなので、大きさを増減したのに
    `packaging/make_icon.py` を流し忘れた、を気付けるようにする。
    """
    reader = QImageReader(str(_ICO))
    assert reader.imageCount() == len(ICON_SIZES)

    frames = []
    for index in range(reader.imageCount()):
        reader.jumpToImage(index)
        frames.append(reader.size().width())
    assert frames == list(ICON_SIZES)
