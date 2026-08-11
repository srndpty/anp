"""連続スクロール表示のページ配置計算。

Qt ウィジェットに依存しないため、`QApplication` なしで単体テストできる。

座標系は2つある。

- **PDF ポイント**: ページの寸法。`QPdfDocument.pagePointSize()` が返す単位
- **コンテンツ座標**: スクロール領域全体の論理ピクセル。原点は左上

ページの寸法だけがズーム倍率の影響を受ける。**ページ間の隙間と余白は
ズームしない**（UI の装飾であって PDF の寸法ではないため）。両方を一緒に
拡大すると、12px の隙間が 800% 表示で 96px に膨れてしまう。
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import accumulate

from PySide6.QtCore import QPointF, QRectF, QSizeF


@dataclass(frozen=True, slots=True)
class LayoutMetrics:
    """ズームの影響を受けない配置の寸法（論理ピクセル）。"""

    page_gap: float = 12.0
    margin: float = 16.0


class PageLayout:
    """ページを縦に並べたときの位置とサイズを計算する。

    ページ高さの累積和だけを保持し、ズーム倍率ごとの上端座標は必要に応じて
    組み立て直す。`paintEvent` から呼ばれる `visible_pages()` を二分探索の
    O(log n) に保つのが目的で、全ページ走査は行わない。
    """

    def __init__(
        self,
        page_sizes: Sequence[QSizeF],
        metrics: LayoutMetrics | None = None,
    ) -> None:
        if not page_sizes:
            msg = "ページのない PDF はレイアウトできません"
            raise ValueError(msg)

        self._page_sizes = list(page_sizes)
        self._metrics = metrics or LayoutMetrics()

        # _cum_heights[i] は i ページ目より前のページ高さの合計（PDF ポイント）。
        self._cum_heights: list[float] = [0.0, *accumulate(s.height() for s in self._page_sizes)]
        self._max_width = max(s.width() for s in self._page_sizes)

        # ズーム倍率ごとの上端座標のキャッシュ。倍率が変わったときだけ作り直す。
        self._cached_zoom: float | None = None
        self._cached_tops: list[float] = []

    # -------------------------------------------------- 基本情報
    @property
    def page_count(self) -> int:
        """ページ数。"""
        return len(self._page_sizes)

    @property
    def metrics(self) -> LayoutMetrics:
        """配置の寸法。"""
        return self._metrics

    def page_size(self, index: int) -> QSizeF:
        """ページの寸法（PDF ポイント）。"""
        return self._page_sizes[index]

    # -------------------------------------------------- 配置
    def content_size(self, zoom: float) -> QSizeF:
        """スクロール領域全体の大きさ。"""
        gaps = self._metrics.page_gap * (self.page_count - 1)
        total_height = self._cum_heights[-1] * zoom
        return QSizeF(
            self._max_width * zoom + self._metrics.margin * 2,
            total_height + gaps + self._metrics.margin * 2,
        )

    def page_rect(self, index: int, zoom: float) -> QRectF:
        """ページの矩形（コンテンツ座標）。横方向は中央に寄せる。"""
        size = self._page_sizes[index]
        width = size.width() * zoom
        content_width = self.content_size(zoom).width()
        return QRectF(
            (content_width - width) / 2,
            self._tops(zoom)[index],
            width,
            size.height() * zoom,
        )

    # -------------------------------------------------- フィット倍率
    def fit_width_zoom(self, index: int, viewport_width: float) -> float:
        """ページの幅がビューポート幅に収まる倍率。

        余白はズームしないので、使える幅から先に差し引く。収まりようが
        ない大きさのときは 0.0 を返し、下限への丸めは呼び出し側に任せる
        （このクラスは表示倍率の許容範囲を知らない）。
        """
        available = viewport_width - self._metrics.margin * 2
        width = self._page_sizes[index].width()
        if available <= 0 or width <= 0:
            return 0.0
        return available / width

    def fit_page_zoom(self, index: int, viewport_size: QSizeF) -> float:
        """ページ全体がビューポートに収まる倍率。

        縦横のうち厳しい方に合わせる。縦の余白もページの上下に1つずつ
        入るため、幅と同じように差し引く。
        """
        available = viewport_size.height() - self._metrics.margin * 2
        height = self._page_sizes[index].height()
        if available <= 0 or height <= 0:
            return 0.0
        return min(self.fit_width_zoom(index, viewport_size.width()), available / height)

    # -------------------------------------------------- 可視範囲
    def visible_pages(self, viewport: QRectF, zoom: float) -> range:
        """viewport と縦方向に重なるページの範囲。

        二分探索で開始ページを求めるため、ページ数に対して O(log n + 可視数)。
        ページ数分の走査は行わない。
        """
        tops = self._tops(zoom)
        top, bottom = viewport.top(), viewport.bottom()

        start = max(bisect_right(tops, top) - 1, 0)
        # 開始位置がページ間の隙間だった場合は次のページから。
        # 下端と viewport 上端がちょうど一致する場合は重なりが 0 なので含めない。
        if start < self.page_count and self._page_bottom(start, zoom) <= top:
            start += 1
        if start >= self.page_count:
            return range(0, 0)

        end = start
        while end < self.page_count and tops[end] < bottom:
            end += 1
        return range(start, end)

    def render_window(self, viewport: QRectF, zoom: float, *, extra: int = 1) -> range:
        """レンダリングを要求してよいページの範囲（可視ページ ± extra）。

        レンダリング要求はここで得た範囲に限定する。取り消せない要求を
        積み上げないための上限であり、先読みの量はここだけで決まる。
        """
        visible = self.visible_pages(viewport, zoom)
        if not visible:
            nearest = self.current_page(viewport, zoom)
            visible = range(nearest, nearest + 1)
        return range(
            max(visible.start - extra, 0),
            min(visible.stop + extra, self.page_count),
        )

    def current_page(self, viewport: QRectF, zoom: float) -> int:
        """いま読んでいるページ。

        viewport と縦方向に最も大きく重なるページとする。末尾が数ピクセル
        見えているだけで前のページが「現在」であり続けるのを避けるため、
        「最初の可視ページ」ではなく重なりの最大で決める。
        """
        visible = self.visible_pages(viewport, zoom)
        if not visible:
            # 隙間や範囲外にいる場合は、位置的にいちばん近いページを返す。
            tops = self._tops(zoom)
            return min(max(bisect_right(tops, viewport.top()) - 1, 0), self.page_count - 1)

        return max(visible, key=lambda index: self._overlap(index, viewport, zoom))

    # -------------------------------------------------- 座標変換
    def page_at(self, point: QPointF, zoom: float) -> int | None:
        """コンテンツ座標の点が乗っているページ。余白や隙間の上なら None。"""
        tops = self._tops(zoom)
        index = bisect_right(tops, point.y()) - 1
        if index < 0 or point.y() > self._page_bottom(index, zoom):
            return None
        rect = self.page_rect(index, zoom)
        if not rect.left() <= point.x() <= rect.right():
            return None
        return index

    def to_normalized(self, index: int, point: QPointF, zoom: float) -> QPointF:
        """コンテンツ座標をページ内の正規化座標（0.0〜1.0）に変換する。

        アノテーションはこの正規化座標で保存する。ビューポートのピクセル座標を
        保存するとズームやリサイズで位置が壊れるため。
        """
        rect = self.page_rect(index, zoom)
        return QPointF(
            (point.x() - rect.left()) / rect.width(),
            (point.y() - rect.top()) / rect.height(),
        )

    def from_normalized(self, index: int, normalized: QPointF, zoom: float) -> QPointF:
        """ページ内の正規化座標をコンテンツ座標に変換する。"""
        rect = self.page_rect(index, zoom)
        return QPointF(
            rect.left() + normalized.x() * rect.width(),
            rect.top() + normalized.y() * rect.height(),
        )

    def from_page_points(self, index: int, point: QPointF, zoom: float) -> QPointF:
        """ページ内の PDF ポイント座標をコンテンツ座標に変換する。

        原点はページの左上。PDF の outline destination はこの単位で来る
        （`PdfDestination`）。正規化座標を経由しないのは、ページ寸法で
        割ってから同じ寸法を掛け直す往復を作らないため。倍率が掛かるのは
        ページの寸法だけで、余白と隙間には掛からない。
        """
        rect = self.page_rect(index, zoom)
        return QPointF(rect.left() + point.x() * zoom, rect.top() + point.y() * zoom)

    # -------------------------------------------------- 内部
    def _tops(self, zoom: float) -> list[float]:
        """各ページの上端座標。倍率が変わったときだけ組み立て直す。"""
        if self._cached_zoom != zoom:
            gap, margin = self._metrics.page_gap, self._metrics.margin
            self._cached_tops = [
                margin + self._cum_heights[i] * zoom + gap * i for i in range(self.page_count)
            ]
            self._cached_zoom = zoom
        return self._cached_tops

    def _page_bottom(self, index: int, zoom: float) -> float:
        return self._tops(zoom)[index] + self._page_sizes[index].height() * zoom

    def _overlap(self, index: int, viewport: QRectF, zoom: float) -> float:
        top = max(self._tops(zoom)[index], viewport.top())
        bottom = min(self._page_bottom(index, zoom), viewport.bottom())
        return max(bottom - top, 0.0)
