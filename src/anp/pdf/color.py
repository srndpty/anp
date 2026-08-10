"""ページ画像の色変換。

**レンダリングとは別の概念として分けてある。** `QPdfPageRenderer` が返す
画像（raw image）は色変換に依存しない。ここは「raw image を受け取って
表示用の画像を作る」だけの純粋な処理で、Qt のウィジェットも要求の
ライフサイクルも知らない。

- 入力の `QImage` は絶対に書き換えない。raw image はキャッシュに載って
  いるので、その場で反転すると Original へ戻したときに反転済みの画像が
  残ってしまう
- `devicePixelRatio` と論理サイズを保つ。失うと高 DPI で大きさが変わる
- アルファは保つ
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtGui import QColor, QImage

# 変換の入口で揃える形式。`QPdfPageRenderer` は今のところ ARGB32 を返すが、
# 形式の違いで壊れないようここで明示的に正規化する。乗算済みアルファ
# （Format_ARGB32_Premultiplied）を RGB 反転すると色が壊れるため、
# 「32bit ならそのまま反転してよい」とはしない。
_WORKING_FORMAT = QImage.Format.Format_ARGB32


class PageColorMode(Enum):
    """ページ画像に適用する色変換。

    キャンバス（ページの外側）や UI の配色はここでは扱わない。あくまで
    **PDF のページ画像だけ**への変換。

    値は設定への保存にそのまま使う文字列。
    """

    ORIGINAL = "original"
    """PDF が持っている色のまま。"""

    INVERT = "invert"
    """RGB を単純に反転する。白地の本を黒地にするための最小の手段。"""


# ページ矩形の下地。画像がまだ無い間と、画像が矩形を覆い切らない端で見える色。
# 変換後のページ画像に馴染む色を選ぶ。Invert 中に白で塗ると、読み込み中だけ
# 画面が白く光る。
#
# **キャンバス（ページの外側）の色とは無関係。** キャンバスは
# `anp.ui.appearance.CanvasTheme` が決める。
_PAGE_BACKGROUNDS = {
    PageColorMode.ORIGINAL: QColor(0xFF, 0xFF, 0xFF),
    PageColorMode.INVERT: QColor(0x00, 0x00, 0x00),
}


def page_background_color(mode: PageColorMode) -> QColor:
    """ページ矩形の下地の色。

    モードが増えるたびにビューが `PageColorMode` の中身を知らずに済むよう、
    対応表はここに置く。
    """
    return _PAGE_BACKGROUNDS[mode]


def transform_page(image: QImage, mode: PageColorMode) -> QImage:
    """raw なページ画像から、表示用のページ画像を作る。

    `ORIGINAL` では入力をそのまま返す（複製もしない）。画像は不変の値と
    して扱うので、共有しても差し支えない。
    """
    if mode is PageColorMode.ORIGINAL:
        return image
    return _inverted(image)


def _inverted(image: QImage) -> QImage:
    """RGB を反転した新しい画像。`R' = 255 - R`（アルファはそのまま）。

    白 → 黒、黒 → 白になる。白の検出も輝度も色相も見ない単純な反転で、
    Smart Dark とは別物。

    `convertToFormat()` は形式が同じなら暗黙共有のまま返すが、続く
    `invertPixels()` が detach するので入力は書き換わらない。共有の
    detach に頼っていることが読み取れるよう、ここで明示しておく。
    """
    result = image.convertToFormat(_WORKING_FORMAT)
    result.invertPixels(QImage.InvertMode.InvertRgb)
    # 変換や複製でも保たれるが、表示の大きさが変わる致命的な項目なので明示する。
    result.setDevicePixelRatio(image.devicePixelRatio())
    return result
