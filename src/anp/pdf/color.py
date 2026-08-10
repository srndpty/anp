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

import numpy as np
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

    SMART_DARK = "smart_dark"
    """色相と彩度を保ったまま明度だけを反転する。

    白地・黒文字は Invert と同じように反転しつつ、色の付いた図・数式・
    シンタックスハイライトを補色へ変えない。詳細は `_smart_dark()`。
    """


# ページ矩形の下地。画像がまだ無い間と、画像が矩形を覆い切らない端で見える色。
# 変換後のページ画像に馴染む色を選ぶ。Invert 中に白で塗ると、読み込み中だけ
# 画面が白く光る。
#
# **キャンバス（ページの外側）の色とは無関係。** キャンバスは
# `anp.ui.appearance.CanvasTheme` が決める。
_PAGE_BACKGROUNDS = {
    PageColorMode.ORIGINAL: QColor(0xFF, 0xFF, 0xFF),
    PageColorMode.INVERT: QColor(0x00, 0x00, 0x00),
    # Smart Dark も白地を黒へ移すので、待っている間に白い紙面を見せない。
    PageColorMode.SMART_DARK: QColor(0x00, 0x00, 0x00),
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
    match mode:
        case PageColorMode.ORIGINAL:
            return image
        case PageColorMode.INVERT:
            return _inverted(image)
        case PageColorMode.SMART_DARK:
            return _smart_dark(image)


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


# 一度に計算する行数。一時配列の大きさを画像の大きさから切り離すために刻む。
# 一時配列は3枚（hi, lo, scratch）で 1画素あたり 3 バイトなので、
# 4096px 幅・128行なら 1.5 MiB ほど。刻まずに 32 MiB の画像をまとめて計算すると、
# 入力・出力に加えて 24 MiB の一時配列がワーカーごとに乗る。
#
# 実測（2435 × 3444 の ARGB32、Windows 11 / Python 3.13）では 128 行が最も速く、
# 512 行や刻まない場合より 1〜2 割速い。キャッシュに収まる大きさで回すため。
_STRIPE_ROWS = 128


def _smart_dark(image: QImage) -> QImage:
    """色相と彩度を保ったまま明度を反転した新しい画像。

    HSL で言えば `H' = H`、`S' = S`、`L' = 1 - L`。8bit の RGB では
    HSL への往復をせずに、次の整数演算で同じ結果が得られる。

        hi = max(R, G, B)
        lo = min(R, G, B)
        delta = 255 - hi - lo
        R' = R + delta   （G, B も同じ delta を足す）

    3チャンネルに同じ値を足すので、チャンネル間の差 = 色相と彩度は変わらない。
    明るさの指標である `(hi + lo) / 2` は `255 - (hi + lo) / 2` へ移る。

    - 灰色（R = G = B）では `delta = 255 - 2R` となり、結果は `255 - R`。
      つまり **無彩色では Invert と完全に一致する**。白黒の技術書や
      スキャン PDF での読みやすさは Invert のまま
    - 純色は動かない。赤 (255, 0, 0) は hi=255, lo=0, delta=0 で赤のまま。
      Invert なら水色になってしまうところ
    - 暗い有彩色は明るい側へ移る。(128, 0, 0) → (255, 127, 127)
    - 2回かけると元に戻る（`hi' = 255 - lo`, `lo' = 255 - hi` より
      `delta' = -delta`）

    結果の各チャンネルは必ず 0〜255 に収まる。最小は `R = lo` のときの
    `255 - hi`、最大は `R = hi` のときの `255 - lo`。

    **`delta` は負になりうる**ので、`R + delta` を uint8 のまま計算しては
    いけない。ただし式を次のように括り直すと、途中の値も 0〜255 を出ない。

        R' = 255 - ((hi - R) + lo)

    `hi - R >= 0`（hi は最大値）、`(hi - R) + lo <= hi <= 255`（R >= lo）
    なので、引き算の借りも足し算の桁上がりも起きない。この形なら 8bit の
    まま計算でき、int16 へ広げる版より実測で約3倍速い（32 MiB の画像で
    230 ms → 80 ms）。桁溢れを避けるために型を広げるのではなく、
    **溢れない式を選ぶ**。

    **写真は検出しない。** 写真領域も色相を保ったまま明暗が反転する。
    これは Smart Dark v1 の既知の仕様。
    """
    # 乗算済みアルファは Invert と同じ理由でここで非乗算へ直す。乗算済みの
    # まま max/min を取ると、アルファの掛かった値どうしを比べることになる。
    source = image.convertToFormat(_WORKING_FORMAT)
    width = source.width()
    height = source.height()

    result = QImage(source.size(), _WORKING_FORMAT)
    # 表示の大きさが変わる致命的な項目なので明示する。新しく作った画像には
    # 入力の devicePixelRatio が引き継がれない。
    result.setDevicePixelRatio(image.devicePixelRatio())
    if width == 0 or height == 0:
        return result

    # **`bytesPerLine() == width * 4` を前提にしない。** 32bit 形式では
    # 実際には一致するが（Qt は 32bit 境界へ揃えるので 4 の倍数の幅に
    # パディングは要らない）、行の先頭を `bytesPerLine()` で取り、行内の
    # 有効な範囲だけを見るようにしておけば形式が変わっても壊れない。
    #
    # `constBits()` は読み取り専用なので、入力と暗黙共有されたままでも
    # 書き換えの心配がない。書き込むのは新しく確保した `result` だけ。
    # 最後の軸を (幅, 4) へ割るのは、行内が連続なのでコピーを伴わない。
    # Format_ARGB32 はリトルエンディアンでは B, G, R, A の並びになるが、
    # 色の3チャンネルは対称に扱うので並び順は結果に影響しない。効くのは
    # 「アルファが4バイト目」という点だけ。
    src_bytes = np.frombuffer(source.constBits(), dtype=np.uint8)
    dst_bytes = np.asarray(result.bits(), dtype=np.uint8)
    src_pixels = src_bytes.reshape(height, source.bytesPerLine())[:, : width * 4].reshape(
        height, width, 4
    )
    dst_pixels = dst_bytes.reshape(height, result.bytesPerLine())[:, : width * 4].reshape(
        height, width, 4
    )

    for top in range(0, height, _STRIPE_ROWS):
        bottom = min(top + _STRIPE_ROWS, height)
        stripe = src_pixels[top:bottom]
        out = dst_pixels[top:bottom]

        # 持つ一時配列は uint8 の3枚（hi, lo, 計算用）だけ。float も
        # HSL の3成分も作らない。`max(axis=2)` は長さ3の縮約を画素ごとに
        # 回すので遅い。チャンネルごとの `maximum` を2回重ねる方が速い。
        blue, green, red = stripe[:, :, 0], stripe[:, :, 1], stripe[:, :, 2]
        high = np.maximum(np.maximum(blue, green), red)
        low = np.minimum(np.minimum(blue, green), red)
        scratch = np.empty_like(high)

        for channel in range(3):
            # 上の docstring のとおり、どの中間結果も 0〜255 を出ない。
            np.subtract(high, stripe[:, :, channel], out=scratch)
            np.add(scratch, low, out=scratch)
            # uint8 の `invert` は 255 - x。ビット反転として書いているのでは
            # なく、8bit の補数がそのまま欲しい値になる。
            np.invert(scratch, out=scratch)
            out[:, :, channel] = scratch
        out[:, :, 3] = stripe[:, :, 3]

    return result
