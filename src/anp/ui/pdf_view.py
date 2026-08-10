"""連続スクロールの PDF ビュー。

`QAbstractScrollArea` の上に、ページを縦に並べたキャンバスを自前で描く。

このウィジェットは **表示と入力だけ** を受け持つ。ページの配置は
`PageLayout`、画像の生成とキャッシュは `PageRenderService` にあり、
ここに写し取らない。`QPdfDocument` を直接レンダリングもしない。

座標系は2つある。

- **コンテンツ座標**: `PageLayout` が使う、文書全体を縦に並べた論理座標
- **ビューポート座標**: いま画面に見えている領域の座標。`paintEvent` が使う

変換は `content_viewport_rect()` と `page_viewport_rect()` に集約する。
スクロール量の足し引きを描画やイベント処理のあちこちに散らさない。

Qt のスクロールバーは整数、レイアウトは浮動小数点なので、境界は
`_update_scrollbars()` と `_scroll_offset()` の2箇所だけで扱う。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, QSizeF, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QResizeEvent
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from anp.pdf.layout import PageLayout
from anp.pdf.render import PageRenderService, PageRequest

logger = logging.getLogger(__name__)

# 表示倍率の範囲。これより外は clamp する。
MIN_ZOOM = 0.25
MAX_ZOOM = 8.0

# ページの下地。Phase 2 でページ色変換を入れるため、キャンバスとは分けて塗る。
_PAGE_COLOR = QColor(0xFF, 0xFF, 0xFF)
_PAGE_BORDER_COLOR = QColor(0x9A, 0x9A, 0x9A)

# キャンバス（ページの外側）。Phase 2 でダークキャンバスに差し替える。
_CANVAS_COLOR = QColor(0x52, 0x56, 0x59)

# スクロールの最小単位（論理ピクセル）。ホイール1ノッチではこれが
# システムの「1度に送る行数」倍だけ動く。
_SCROLL_STEP = 30

# ドキュメントが無いときの現在ページ。
NO_PAGE = -1


class PdfView(QAbstractScrollArea):
    """PDF を縦に連続スクロールして表示するビュー。

    ファイルは自分では開かない。`DocumentController` などが開いた
    `QPdfDocument` とページ寸法を `set_document()` で受け取る。
    """

    current_page_changed = Signal(int)

    def __init__(self, render_service: PageRenderService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._render = render_service
        self._render.page_ready.connect(self._on_page_ready)

        self._layout: PageLayout | None = None
        self._zoom = 1.0
        self._current_page = NO_PAGE

        self.viewport().setAutoFillBackground(False)
        self._update_scrollbars()

    # -------------------------------------------------- ドキュメント
    def set_document(self, document: QPdfDocument, page_sizes: Sequence[QSizeF]) -> None:
        """表示するドキュメントを差し替える。

        `page_sizes` は `DocumentController.page_sizes()` の戻り値。ビューが
        `QPdfDocument` からページ寸法を引き直さないのは、ドキュメントの
        問い合わせ方をビューに持ち込まないため。

        レンダリング状態を先に捨てるので、切り替えた瞬間に前の PDF の
        画像が仮表示として出ることはない。表示倍率とスクロール位置も
        初期状態に戻す。
        """
        # 先にキャッシュと世代を進める。順序を逆にすると、新しいレイアウトで
        # 描いたページに古い PDF の画像が仮表示として乗る。
        self._render.set_document(document)

        self._layout = PageLayout(page_sizes)
        self._zoom = 1.0
        self._update_scrollbars()
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)
        self.viewport().update()
        self._request_render()
        self._refresh_current_page()

    def clear_document(self) -> None:
        """表示を空にする。"""
        self._layout = None
        self._render.reset()
        self._update_scrollbars()
        self.viewport().update()
        self._refresh_current_page()

    @property
    def has_document(self) -> bool:
        """ドキュメントが設定されているか。"""
        return self._layout is not None

    @property
    def page_count(self) -> int:
        """ページ数。ドキュメントが無ければ 0。"""
        return self._layout.page_count if self._layout is not None else 0

    # -------------------------------------------------- 表示倍率
    @property
    def zoom(self) -> float:
        """現在の表示倍率。"""
        return self._zoom

    def set_zoom(self, zoom: float) -> None:
        """表示倍率を変える。範囲外は `MIN_ZOOM`〜`MAX_ZOOM` に収める。

        倍率を変えた瞬間に文書の先頭へ飛ばないよう、ビューポート中央の
        コンテンツ位置を保つ。
        """
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        if zoom == self._zoom:
            return

        anchor = self._center_anchor()
        self._zoom = zoom
        self._update_scrollbars()
        if anchor is not None:
            self._restore_center_anchor(*anchor)
        self.viewport().update()
        self._request_render()
        self._refresh_current_page()

    def _center_anchor(self) -> tuple[int, QPointF] | None:
        """ビューポート中央にあるページと、そのページ内の正規化座標。

        コンテンツ座標そのものではなくページ内の正規化座標で覚える。
        隙間と余白はズームしないため、コンテンツ座標は倍率に比例しない。
        """
        if self._layout is None:
            return None
        viewport = self.content_viewport_rect()
        page = self._layout.current_page(viewport, self._zoom)
        return page, self._layout.to_normalized(page, viewport.center(), self._zoom)

    def _restore_center_anchor(self, page: int, normalized: QPointF) -> None:
        """アンカーの位置がビューポート中央に来るようスクロールする。"""
        if self._layout is None:
            return
        center = self._layout.from_normalized(page, normalized, self._zoom)
        size = self.viewport().size()
        self.horizontalScrollBar().setValue(round(center.x() - size.width() / 2))
        self.verticalScrollBar().setValue(round(center.y() - size.height() / 2))

    # -------------------------------------------------- 現在ページ
    @property
    def current_page(self) -> int:
        """いま読んでいるページ（0 始まり）。無ければ `NO_PAGE`。"""
        return self._current_page

    def _refresh_current_page(self) -> None:
        """現在ページを取り直し、変わっていれば通知する。"""
        page = NO_PAGE
        if self._layout is not None:
            page = self._layout.current_page(self.content_viewport_rect(), self._zoom)
        if page != self._current_page:
            self._current_page = page
            self.current_page_changed.emit(page)

    # -------------------------------------------------- 座標変換
    def content_viewport_rect(self) -> QRectF:
        """いま見えている範囲（コンテンツ座標）。"""
        offset = self._scroll_offset()
        size = self.viewport().size()
        return QRectF(offset.x(), offset.y(), size.width(), size.height())

    def page_viewport_rect(self, index: int) -> QRectF | None:
        """ページの矩形（ビューポート座標）。ドキュメントが無ければ None。"""
        if self._layout is None or not 0 <= index < self._layout.page_count:
            return None
        return self._layout.page_rect(index, self._zoom).translated(-self._scroll_offset())

    def visible_pages(self) -> range:
        """いま見えているページの範囲。描画対象はここに限る。"""
        if self._layout is None:
            return range(0, 0)
        return self._layout.visible_pages(self.content_viewport_rect(), self._zoom)

    def _scroll_offset(self) -> QPointF:
        """ビューポート左上に対応するコンテンツ座標。

        コンテンツがビューポートより狭いときはスクロールできないので、
        横方向は中央に置く。ここが整数のスクロールバーと浮動小数点の
        レイアウトの唯一の境界。
        """
        y = float(self.verticalScrollBar().value())
        if self._layout is None:
            return QPointF(0.0, y)

        content_width = self._layout.content_size(self._zoom).width()
        viewport_width = float(self.viewport().width())
        if content_width < viewport_width:
            return QPointF(-(viewport_width - content_width) / 2, y)
        return QPointF(float(self.horizontalScrollBar().value()), y)

    # -------------------------------------------------- スクロールバー
    def _update_scrollbars(self) -> None:
        """コンテンツとビューポートの大きさから可動域を決める。"""
        viewport_size = self.viewport().size()
        content = (
            self._layout.content_size(self._zoom) if self._layout is not None else QSizeF(0.0, 0.0)
        )

        for bar, content_length, viewport_length in (
            (self.horizontalScrollBar(), content.width(), viewport_size.width()),
            (self.verticalScrollBar(), content.height(), viewport_size.height()),
        ):
            # 端が切れないよう切り上げる。整数への丸めはここだけで行う。
            bar.setRange(0, max(math.ceil(content_length - viewport_length), 0))
            bar.setPageStep(viewport_length)
            bar.setSingleStep(_SCROLL_STEP)

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802 (Qt の命名規則)
        """スクロール位置が変わったときの追随。

        ビューポート全体を描き直す。ここでスクロールバーを触らないので、
        シグナルが再帰することはない。
        """
        super().scrollContentsBy(dx, dy)
        self.viewport().update()
        self._request_render()
        self._refresh_current_page()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """リサイズに追随する。レイアウトもキャッシュも作り直さない。"""
        super().resizeEvent(event)
        self._update_scrollbars()
        self._request_render()
        self._refresh_current_page()

    # -------------------------------------------------- 描画
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """見えているページだけを描く。"""
        painter = QPainter(self.viewport())
        try:
            painter.fillRect(event.rect(), _CANVAS_COLOR)
            if self._layout is None:
                return

            # 目的の解像度が揃うまで別解像度の画像を拡大縮小して描くので、
            # 補間を有効にする。paintEvent 内で画像を作り直さないため、
            # QImage.scaled() は使わない。
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setPen(_PAGE_BORDER_COLOR)
            for index in self.visible_pages():
                self._paint_page(painter, index)
        finally:
            painter.end()

    def _paint_page(self, painter: QPainter, index: int) -> None:
        rect = self.page_viewport_rect(index)
        if rect is None:
            return

        painter.fillRect(rect, _PAGE_COLOR)

        size_px = self._render_size(rect.size())
        image = self._render.image_for(index, size_px, self.devicePixelRatioF())
        if image is None:
            # 目的の解像度がまだ無いので、同じページの別解像度で仮表示する。
            image = self._render.placeholder_for(index, size_px)
        if image is not None:
            painter.drawImage(rect, image)

        painter.drawRect(rect)

    def _on_page_ready(self, page_index: int) -> None:
        """レンダリングが終わったページの領域だけ描き直す。"""
        if page_index not in self.visible_pages():
            return
        rect = self.page_viewport_rect(page_index)
        if rect is None:
            return
        self.viewport().update(self._aligned(rect))

    def _aligned(self, rect: QRectF) -> QRect:
        """浮動小数点の矩形を、確実に覆う整数矩形にする。"""
        return rect.toAlignedRect().intersected(self.viewport().rect())

    # -------------------------------------------------- レンダリング要求
    def _render_size(self, logical_size: QSizeF) -> QSize:
        """論理表示サイズから要求する物理ピクセルサイズを作る。

        論理サイズ × devicePixelRatio。上限への切り詰めと `RenderKey` への
        写像は `PageRenderService` の仕事なので、ここでは二重に補正しない。
        """
        dpr = self.devicePixelRatioF()
        return QSize(
            max(round(logical_size.width() * dpr), 1),
            max(round(logical_size.height() * dpr), 1),
        )

    def _request_render(self) -> None:
        """いま必要なページのレンダリングを要求する。

        対象は `PageLayout.render_window()`（可視ページ ± 1）に限る。
        `PageRenderService` は渡した順を優先度として扱うので、現在ページ →
        他の可視ページ → 先読みの順に並べる。取り消せない要求を先読みに
        使い切ってしまわないため。
        """
        if self._layout is None:
            return

        viewport = self.content_viewport_rect()
        window = self._layout.render_window(viewport, self._zoom)
        visible = self._layout.visible_pages(viewport, self._zoom)
        current = self._layout.current_page(viewport, self._zoom)

        order = [current]
        order += [page for page in visible if page != current]
        order += [page for page in window if page not in visible and page != current]

        dpr = self.devicePixelRatioF()
        self._render.request_pages(
            [
                PageRequest(
                    page_index=page,
                    size_px=self._render_size(self._layout.page_rect(page, self._zoom).size()),
                    dpr=dpr,
                )
                for page in order
            ]
        )
