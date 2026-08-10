"""`anp.pdf.color` のテスト。

`QApplication` も PDF も要らない、純粋な画素処理として検証する。
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QImage, qAlpha, qBlue, qGreen, qRed

from anp.pdf.color import PageColorMode, transform_page


def solid(argb: int, *, size: int = 4, fmt: QImage.Format = QImage.Format.Format_ARGB32) -> QImage:
    """単色で塗った画像。"""
    image = QImage(size, size, fmt)
    image.fill(argb)
    return image


def rgba(image: QImage, x: int = 0, y: int = 0) -> tuple[int, int, int, int]:
    """指定画素の (R, G, B, A)。"""
    pixel = image.pixel(x, y)
    return qRed(pixel), qGreen(pixel), qBlue(pixel), qAlpha(pixel)


# ------------------------------------------------------------------ 反転
def test_white_becomes_black() -> None:
    """白は黒になる。"""
    assert rgba(transform_page(solid(0xFFFFFFFF), PageColorMode.INVERT)) == (0, 0, 0, 255)


def test_black_becomes_white() -> None:
    """黒は白になる。"""
    assert rgba(transform_page(solid(0xFF000000), PageColorMode.INVERT)) == (255, 255, 255, 255)


@pytest.mark.parametrize("color", [(12, 34, 56), (200, 100, 0), (1, 254, 128)])
def test_each_channel_is_subtracted_from_255(color: tuple[int, int, int]) -> None:
    """任意の RGB が 255 - 値 になる。輝度も色相も見ない単純な反転。"""
    red, green, blue = color
    source = solid(0xFF000000 | (red << 16) | (green << 8) | blue)

    result = transform_page(source, PageColorMode.INVERT)

    assert rgba(result) == (255 - red, 255 - green, 255 - blue, 255)


def test_alpha_is_preserved() -> None:
    """アルファは反転しない。"""
    source = solid(0x80FF0000)

    result = transform_page(source, PageColorMode.INVERT)

    assert rgba(result) == (0, 255, 255, 0x80)


def test_the_device_pixel_ratio_is_preserved() -> None:
    """`devicePixelRatio` が変換の前後で保たれる。

    失うと高 DPI で表示の大きさが変わってしまう。
    """
    source = solid(0xFFFFFFFF)
    source.setDevicePixelRatio(1.5)

    result = transform_page(source, PageColorMode.INVERT)

    assert result.devicePixelRatio() == pytest.approx(1.5)


def test_the_logical_size_is_preserved() -> None:
    """画素数と論理サイズが変わらない。"""
    source = solid(0xFFFFFFFF, size=8)
    source.setDevicePixelRatio(2.0)

    result = transform_page(source, PageColorMode.INVERT)

    assert result.size() == source.size()
    assert result.deviceIndependentSize() == source.deviceIndependentSize()


def test_the_input_image_is_not_modified() -> None:
    """入力の `QImage` を書き換えない。

    raw 画像はキャッシュに載っている。その場で反転すると Original へ
    戻したときに反転済みの絵が残る。
    """
    source = solid(0xFFFFFFFF)

    inverted = transform_page(source, PageColorMode.INVERT)

    assert rgba(source) == (255, 255, 255, 255)
    assert rgba(inverted) == (0, 0, 0, 255)


def test_inverting_twice_restores_the_original_pixels() -> None:
    """2回反転すると元の画素に戻る（破壊的な変換になっていない）。"""
    source = solid(0xFF123456)

    result = transform_page(transform_page(source, PageColorMode.INVERT), PageColorMode.INVERT)

    assert rgba(result) == rgba(source)


# ------------------------------------------------------------------ 入力形式
@pytest.mark.parametrize(
    "fmt",
    [
        QImage.Format.Format_ARGB32,
        QImage.Format.Format_RGB32,
        QImage.Format.Format_RGBA8888,
        QImage.Format.Format_RGB888,
        QImage.Format.Format_Grayscale8,
    ],
)
def test_other_input_formats_still_invert(fmt: QImage.Format) -> None:
    """入力の形式が違っても反転結果は同じ。"""
    result = transform_page(solid(0xFFFFFFFF, fmt=fmt), PageColorMode.INVERT)

    assert rgba(result) == (0, 0, 0, 255)


def test_premultiplied_alpha_is_not_inverted_as_is() -> None:
    """乗算済みアルファでも色が壊れない。

    そのまま RGB を反転すると、乗算済みの成分を非乗算のつもりで
    引くことになり、色が破綻する。変換の入口で形式を揃えている。
    """
    source = solid(0xFFFF0000, fmt=QImage.Format.Format_ARGB32_Premultiplied)

    result = transform_page(source, PageColorMode.INVERT)

    assert rgba(result) == (0, 255, 255, 255)


def test_half_transparent_premultiplied_input_is_unpremultiplied_first() -> None:
    """半透明の乗算済みアルファを、非乗算に直してから反転する。

    ここが形式を正規化する理由そのもの。alpha=128 の赤は乗算済みでは
    (128, 0, 0) として格納されている。そのまま引くと 255-128 = 127 に
    なり、非乗算の赤（255）を反転した 0 と食い違う。
    """
    source = solid(0x80FF0000).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    # 前提の確認。乗算済みでは R が alpha 分だけ減った値で格納されている。
    assert source.constBits()[2] != 0xFF  # BGRA 並びの R

    result = transform_page(source, PageColorMode.INVERT)

    # 丸め誤差の分だけ 1 ずれうるので、破綻していないことを見る。
    red, green, blue, alpha = rgba(result)
    assert alpha == 0x80
    assert red <= 2
    assert green >= 253
    assert blue >= 253


# ------------------------------------------------------------------ Original
def test_original_does_not_alter_the_pixels() -> None:
    """`ORIGINAL` では画素を変えない。"""
    source = solid(0xFF123456)

    result = transform_page(source, PageColorMode.ORIGINAL)

    assert rgba(result) == rgba(source)


def test_original_returns_the_same_image() -> None:
    """`ORIGINAL` は複製すら作らない（同じ絵を二重に持たない）。"""
    source = solid(0xFF123456)

    assert transform_page(source, PageColorMode.ORIGINAL) is source


# ------------------------------------------------------------------ モード
def test_the_mode_values_are_stable_strings() -> None:
    """設定へ保存する文字列は変えない。"""
    assert PageColorMode.ORIGINAL.value == "original"
    assert PageColorMode.INVERT.value == "invert"
