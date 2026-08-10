"""`anp.pdf.color` のテスト。

`QApplication` も PDF も要らない、純粋な画素処理として検証する。
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QImage, qAlpha, qBlue, qGreen, qRed

from anp.pdf.color import PageColorMode, page_background_color, transform_page


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


# ------------------------------------------------------------------ Smart Dark
# 期待値はテスト側に直書きする。実装と同じ式を呼んで期待値を作ると、式が
# 変わってもテストが一緒に動いてしまい、回帰を検出できない。
def smart_dark(
    argb: int, *, fmt: QImage.Format = QImage.Format.Format_ARGB32
) -> tuple[int, int, int, int]:
    """単色を Smart Dark にかけた結果の (R, G, B, A)。"""
    return rgba(transform_page(solid(argb, fmt=fmt), PageColorMode.SMART_DARK))


def test_smart_dark_turns_white_into_black() -> None:
    """白は黒になる。白地の紙面が黒地になるのが目的。"""
    assert smart_dark(0xFFFFFFFF) == (0, 0, 0, 255)


def test_smart_dark_turns_black_into_white() -> None:
    """黒は白になる。"""
    assert smart_dark(0xFF000000) == (255, 255, 255, 255)


@pytest.mark.parametrize(
    ("gray", "expected"),
    [(0, 255), (64, 191), (128, 127), (200, 55), (255, 0)],
)
def test_smart_dark_maps_gray_to_255_minus_gray(gray: int, expected: int) -> None:
    """無彩色は `255 - 値` になる。8bit なので 128 は 127（127.5 ではない）。"""
    source = 0xFF000000 | (gray << 16) | (gray << 8) | gray

    assert smart_dark(source) == (expected, expected, expected, 255)


@pytest.mark.parametrize("gray", [0, 1, 17, 64, 128, 129, 200, 254, 255])
def test_smart_dark_matches_invert_on_grayscale(gray: int) -> None:
    """無彩色では Invert と完全に一致する。

    白黒の技術書・数式・スキャン PDF の読みやすさが Invert のままである
    ことが、Smart Dark を既定の候補にできる条件になる。
    """
    source = solid(0xFF000000 | (gray << 16) | (gray << 8) | gray)

    assert rgba(transform_page(source, PageColorMode.SMART_DARK)) == rgba(
        transform_page(source, PageColorMode.INVERT)
    )


@pytest.mark.parametrize(
    "color",
    [
        (255, 0, 0),  # 赤
        (0, 255, 0),  # 緑
        (0, 0, 255),  # 青
        (0, 255, 255),  # シアン
        (255, 0, 255),  # マゼンタ
        (255, 255, 0),  # 黄
    ],
)
def test_smart_dark_keeps_pure_colors(color: tuple[int, int, int]) -> None:
    """純色は動かない。hi=255, lo=0 なので delta が 0 になる。"""
    red, green, blue = color

    assert smart_dark(0xFF000000 | (red << 16) | (green << 8) | blue) == (*color, 255)


def test_smart_dark_differs_from_invert_on_chromatic_input() -> None:
    """有彩色では Invert と違う。赤は Invert ではシアン、Smart Dark では赤。

    ここが Smart Dark の存在理由。単純な Invert へ取り違えて実装すると
    このテストが落ちる。
    """
    source = solid(0xFFFF0000)

    assert rgba(transform_page(source, PageColorMode.INVERT)) == (0, 255, 255, 255)
    assert rgba(transform_page(source, PageColorMode.SMART_DARK)) == (255, 0, 0, 255)


@pytest.mark.parametrize(
    ("color", "expected"),
    [
        # hi=128, lo=0, delta=127
        ((128, 0, 0), (255, 127, 127)),
        # hi=200, lo=40, delta=15
        ((200, 100, 40), (215, 115, 55)),
        # hi=90, lo=10, delta=155
        ((10, 90, 60), (165, 245, 215)),
        # hi=255, lo=200, delta=-200。delta が負になる側。
        ((255, 250, 200), (55, 50, 0)),
    ],
)
def test_smart_dark_follows_the_integer_formula(
    color: tuple[int, int, int], expected: tuple[int, int, int]
) -> None:
    """`delta = 255 - max - min` を各チャンネルへ足す、という式そのもの。"""
    red, green, blue = color

    assert smart_dark(0xFF000000 | (red << 16) | (green << 8) | blue) == (*expected, 255)


@pytest.mark.parametrize("argb", [0xFF123456, 0xFF800000, 0x80ABCDEF, 0xFF000102, 0x2AFF00FF])
def test_smart_dark_twice_restores_the_original_pixels(argb: int) -> None:
    """2回かけると元に戻る（アルファも含めて画素単位で一致する）。

    `hi' = 255 - lo`、`lo' = 255 - hi` なので2回目の delta は符号が
    反転する。整数でも丸めが入らないので、この往復は厳密に成立する。
    """
    source = solid(argb)

    result = transform_page(
        transform_page(source, PageColorMode.SMART_DARK), PageColorMode.SMART_DARK
    )

    assert rgba(result) == rgba(source)


def test_smart_dark_matches_a_naive_reference_on_a_mixed_image() -> None:
    """1枚の画像の全画素が、素直に書き下した式と一致する。

    実装は 8bit の桁溢れを避けるために `255 - ((hi - X) + lo)` へ括り
    直している。ここでは仕様どおりの `X + (255 - hi - lo)` を Python の
    整数で素朴に計算して突き合わせ、その括り直しが等価であることを見る。
    """
    width, height = 23, 19
    source = QImage(width, height, QImage.Format.Format_ARGB32)
    expected: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for y in range(height):
        for x in range(width):
            # 端も中間も暗い色も明るい色も混ぜる、決定的な散らし方。
            red = (x * 37 + y * 11) % 256
            green = (x * 91 + y * 53) % 256
            blue = (x * 7 + y * 199) % 256
            alpha = (x * 13 + y * 29) % 256
            source.setPixel(x, y, (alpha << 24) | (red << 16) | (green << 8) | blue)
            delta = 255 - max(red, green, blue) - min(red, green, blue)
            expected[x, y] = (red + delta, green + delta, blue + delta, alpha)

    result = transform_page(source, PageColorMode.SMART_DARK)

    for (x, y), pixel in expected.items():
        assert rgba(result, x, y) == pixel, f"({x}, {y})"


def test_smart_dark_preserves_alpha() -> None:
    """アルファは変えない。"""
    assert smart_dark(0x80FF0000) == (255, 0, 0, 0x80)


def test_smart_dark_does_not_modify_the_input_image() -> None:
    """入力の `QImage` を書き換えない。raw はキャッシュに載っている。"""
    source = solid(0xFF800000)

    result = transform_page(source, PageColorMode.SMART_DARK)

    assert rgba(source) == (128, 0, 0, 255)
    assert rgba(result) == (255, 127, 127, 255)


def test_smart_dark_preserves_the_device_pixel_ratio_and_size() -> None:
    """`devicePixelRatio` と画素数・論理サイズが保たれる。"""
    source = solid(0xFFFFFFFF, size=8)
    source.setDevicePixelRatio(1.5)

    result = transform_page(source, PageColorMode.SMART_DARK)

    assert result.devicePixelRatio() == pytest.approx(1.5)
    assert result.size() == source.size()
    assert result.deviceIndependentSize() == source.deviceIndependentSize()


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
def test_smart_dark_handles_other_input_formats(fmt: QImage.Format) -> None:
    """入力の形式が違っても結果は同じ。入口で ARGB32 へ揃えている。"""
    assert smart_dark(0xFFFFFFFF, fmt=fmt) == (0, 0, 0, 255)


def test_smart_dark_unpremultiplies_before_transforming() -> None:
    """半透明の乗算済みアルファを、非乗算に直してから変換する。

    alpha=128 の赤は乗算済みでは (128, 0, 0) として格納されている。
    そのまま max/min を取ると delta=127 の暗い赤（(255,127,127) 相当）に
    なってしまい、非乗算の赤を変換した「赤のまま」と食い違う。
    """
    source = solid(0x80FF0000).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    # 前提の確認。BGRA 並びの R が alpha 分だけ減っている。この添字だけは
    # リトルエンディアン前提（`_smart_dark()` のコメントを参照）。検証本体は
    # `pixel()` 経由なのでバイト順に依存しない。
    assert source.constBits()[2] != 0xFF

    red, green, blue, alpha = rgba(transform_page(source, PageColorMode.SMART_DARK))

    # 乗算・非乗算の往復で 1 ずれうるので、破綻していないことを見る。
    assert alpha == 0x80
    assert red >= 253
    assert green <= 2
    assert blue <= 2


@pytest.mark.parametrize("width", [1, 3, 17, 64])
def test_smart_dark_handles_widths_that_are_not_multiples_of_four(width: int) -> None:
    """幅が 4 の倍数でなくても正しく変換する。

    走査線の長さ（`bytesPerLine()`）が `width * 4` と一致することを
    暗黙に前提にしていると、パディングのある形式で端が壊れる。
    """
    source = QImage(width, 3, QImage.Format.Format_ARGB32)
    source.fill(0xFF800000)

    result = transform_page(source, PageColorMode.SMART_DARK)

    assert result.size() == source.size()
    for x in range(width):
        for y in range(3):
            assert rgba(result, x, y) == (255, 127, 127, 255)


def test_smart_dark_is_continuous_across_stripe_boundaries() -> None:
    """行を刻んで処理しても継ぎ目ができない。

    一時配列を抑えるために数百行ずつ処理するので、刻み目のある高さで
    全画素を確かめる。
    """
    height = 600  # 刻み幅（128 行）を複数回跨ぎ、端数も出る高さ
    source = QImage(3, height, QImage.Format.Format_ARGB32)
    for y in range(height):
        source.setPixel(0, y, 0xFF000000 | ((y % 256) << 16))
        source.setPixel(1, y, 0xFF000000 | (y % 256))
        source.setPixel(2, y, 0xFFFFFFFF)

    result = transform_page(source, PageColorMode.SMART_DARK)

    for y in range(height):
        level = y % 256
        # hi=level, lo=0 なので delta = 255 - level
        assert rgba(result, 0, y) == (255, 255 - level, 255 - level, 255)
        assert rgba(result, 1, y) == (255 - level, 255 - level, 255, 255)
        assert rgba(result, 2, y) == (0, 0, 0, 255)


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
    assert PageColorMode.SMART_DARK.value == "smart_dark"


# ------------------------------------------------------------------ ページの下地
def test_the_page_background_matches_the_mode() -> None:
    """下地はモードに合わせる。反転中に白で塗ると読み込み中だけ画面が光る。"""
    assert page_background_color(PageColorMode.ORIGINAL) == QColor(0xFF, 0xFF, 0xFF)
    assert page_background_color(PageColorMode.INVERT) == QColor(0x00, 0x00, 0x00)
    assert page_background_color(PageColorMode.SMART_DARK) == QColor(0x00, 0x00, 0x00)


def test_every_mode_has_a_page_background() -> None:
    """モードの追加時に定義漏れがないことを確かめる。"""
    for mode in PageColorMode:
        assert page_background_color(mode).isValid()
