"""PDF が指し示す移動先（destination）。

PDF の outline / bookmarks が持つ「どのページのどこを開くか」を、Qt の
モデルからもビューからも切り離した値として表す。

座標は **ページ内の PDF ポイント**で、原点はページの左上。
`QPdfBookmarkModel` の Location ロールがこの向きで返す（PDF の内部表現は
左下原点だが、Qt がページ高さから引いた値にして渡す）。ページ寸法で
割った正規化座標にはしない。ページの寸法を知っているのは
`PageLayout` なので、割る場所をここへ増やさない。

**値の出どころは PDF の metadata であって利用者の操作ではない。**
壊れた PDF は珍しくないので、非有限な値や範囲外の値をプログラムの
誤りとして例外にせず、`clamp_to_page()` で安全な値へ落とす。
学習マークの座標（`validate_position()` が契約違反を例外にする）とは
目的が違う。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QSizeF


@dataclass(frozen=True, slots=True)
class PdfDestination:
    """outline の項目が指す移動先。

    `zoom_hint` は PDF の作者が指定した倍率。**読み取るだけで適用しない**
    （P5-2 では navigation が倍率の方針を変えない）。指定が無い場合は None。
    """

    page_index: int
    location: QPointF
    zoom_hint: float | None = None


def clamp_to_page(point: QPointF, page_size: QSizeF) -> QPointF:
    """ページの内側に収めた、ページ内の PDF ポイント座標。

    NaN・inf はページの左上（0, 0）へ落とす。移動先の座標が読めない
    ことは「そのページの先頭を開く」であって、失敗ではない。

    ページ外の値は縁へ丸める。浮動小数の誤差でわずかに外へ出た座標を
    そのまま使えるようにするためと、巨大な値をスクロールバーへ
    流さないため。
    """
    return QPointF(
        _clamped(point.x(), page_size.width()),
        _clamped(point.y(), page_size.height()),
    )


def _clamped(value: float, limit: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(max(value, 0.0), limit)
