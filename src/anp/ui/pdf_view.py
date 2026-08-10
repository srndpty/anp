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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, QSizeF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QResizeEvent, QWheelEvent
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from anp.pdf.color import PageColorMode
from anp.pdf.layout import PageLayout
from anp.pdf.render import PageRenderService, PageRequest

logger = logging.getLogger(__name__)

# 表示倍率の範囲。これより外は clamp する。
MIN_ZOOM = 0.25
MAX_ZOOM = 8.0

# ズームイン/アウト1回の倍率比。加算ではなく比にするのは、低倍率でも高倍率でも
# 体感の変化量を揃えるため。2回で2倍になる。
ZOOM_STEP = math.sqrt(2.0)

# ホイール1ノッチの角度（1/8度単位）。Qt の慣習値。
_WHEEL_NOTCH = 120.0


class ZoomMode(Enum):
    """表示倍率の決め方。

    「幅に合わせる」「ページ全体」を bool の組み合わせで持つと、両方 True の
    ような表現できない状態が作れてしまうので、排他の列挙で持つ。

    値は設定への保存にそのまま使う文字列。
    """

    FREE = "free"
    """手動で指定した倍率。"""

    FIT_WIDTH = "fit_width"
    """現在ページの幅がビューポートに収まる倍率。"""

    FIT_PAGE = "fit_page"
    """現在ページ全体がビューポートに収まる倍率。"""


# ページの下地。画像がまだ無い間と、画像が矩形を覆い切らない端で見える色。
# ページ色変換に合わせて選ぶ。Invert 中に白で塗ると、読み込み中だけ
# 画面が白く光る。キャンバスとは独立に塗る。
_PAGE_COLORS = {
    PageColorMode.ORIGINAL: QColor(0xFF, 0xFF, 0xFF),
    PageColorMode.INVERT: QColor(0x00, 0x00, 0x00),
}
_PAGE_BORDER_COLOR = QColor(0x9A, 0x9A, 0x9A)

# キャンバス（ページの外側）。**ページ色変換の影響を受けない。**
# 反転するのはページの中身だけで、ページ外の領域は常にこの色。
_CANVAS_COLOR = QColor(0x52, 0x56, 0x59)

# スクロールの最小単位（論理ピクセル）。ホイール1ノッチではこれが
# システムの「1度に送る行数」倍だけ動く。
_SCROLL_STEP = 30

# 可動域を切り上げるときに無視する、はみ出しの端数（論理ピクセル）。
# 幅フィットではページ幅をビューポート幅ちょうどに合わせるが、
# 倍率を掛け直す過程で 1e-13 程度の誤差が残る。切り上げると、この誤差が
# 「1px 分スクロールできる」に化けて横スクロールバーが出てしまう。
_SCROLL_EPSILON = 1e-6

# ドキュメントが無いときの現在ページ。
NO_PAGE = -1


@dataclass(frozen=True, slots=True)
class _ZoomAnchor:
    """倍率を変えても動かさない点。

    ページ内の正規化座標で覚える。隙間と余白はズームしないので、コンテンツ
    座標は倍率に比例しないため。`viewport_pos` はその点を留めるビューポート
    座標で、中央アンカーならビューポート中央、ポインタアンカーならカーソル位置。
    """

    page: int
    normalized: QPointF
    viewport_pos: QPointF


def _clamp_unit(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _anchor_ratio(
    viewport_start: float, viewport_end: float, page_start: float, page_end: float
) -> float:
    """ページ内のどこをビューポート中央に据えるか（0.0〜1.0）。

    ページがビューポートに収まっているならページの中心。はみ出しているなら
    ビューポート中央にあたる位置。

    ビューポート中央がページの外（余白や隙間）にある場合はページの端に
    丸める。`PageLayout.to_normalized()` はページ外の点も 1.0 を超える比率に
    そのまま変換するため、丸めずに使うと倍率変更でページ末尾へ大きく飛ぶ。
    """
    length = page_end - page_start
    if length <= 0 or (page_start >= viewport_start and page_end <= viewport_end):
        return 0.5
    center = (viewport_start + viewport_end) / 2
    return min(max((center - page_start) / length, 0.0), 1.0)


def _fit_zoom_of(layout: PageLayout, mode: ZoomMode, page: int, size: QSizeF) -> float:
    """与えたビューポートの大きさに対するフィット倍率。

    上下限への丸めは行わない（`PageLayout` と同じく範囲を知らない）。
    """
    if mode is ZoomMode.FIT_WIDTH:
        return layout.fit_width_zoom(page, size.width())
    return layout.fit_page_zoom(page, size)


class PdfView(QAbstractScrollArea):
    """PDF を縦に連続スクロールして表示するビュー。

    ファイルは自分では開かない。`DocumentController` などが開いた
    `QPdfDocument` とページ寸法を `set_document()` で受け取る。
    """

    current_page_changed = Signal(int)
    zoom_changed = Signal()
    """表示倍率か倍率モードが変わった。表示の更新は受け取った側で行う。"""

    page_color_mode_changed = Signal()
    """ページ色変換が変わった。メニューの選択表示はこれを見て合わせる。"""

    def __init__(self, render_service: PageRenderService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._render = render_service
        self._render.page_ready.connect(self._on_page_ready)

        self._layout: PageLayout | None = None
        self._zoom = 1.0
        self._zoom_mode = ZoomMode.FREE
        self._last_free_zoom = 1.0
        self._current_page = NO_PAGE

        # 可動域と位置をまとめて更新している間だけ立てる。`_quiet_scrollbars()` を参照。
        self._scroll_updates_suppressed = False

        self.viewport().setAutoFillBackground(False)
        self._update_scrollbars()

    # -------------------------------------------------- ドキュメント
    def set_document(self, document: QPdfDocument, page_sizes: Sequence[QSizeF]) -> None:
        """表示するドキュメントを差し替える。

        `page_sizes` は `DocumentController.page_sizes()` の戻り値。ビューが
        `QPdfDocument` からページ寸法を引き直さないのは、ドキュメントの
        問い合わせ方をビューに持ち込まないため。

        レンダリング状態を先に捨てるので、切り替えた瞬間に前の PDF の
        画像が仮表示として出ることはない。スクロール位置は先頭に戻す。

        表示倍率は現在の `ZoomMode` を新しいドキュメントに適用し直す。
        フィット系なら1ページ目に対して計算し直し、FREE なら手動で
        指定された倍率をそのまま引き継ぐ。
        """
        if len(page_sizes) != document.pageCount():
            msg = (
                f"page size count {len(page_sizes)} does not match "
                f"document page count {document.pageCount()}"
            )
            raise ValueError(msg)

        # レイアウトを先に組み立てる。捨ててから作ると、構築に失敗したときに
        # 古いレイアウトと新しいレンダリング状態が混ざる。
        layout = PageLayout(page_sizes)

        # キャッシュと世代はレイアウトを差し替える前に進める。順序を逆にすると、
        # 新しいレイアウトで描いたページに古い PDF の画像が仮表示として乗る。
        self._render.set_document(document)
        self._layout = layout

        before = self._zoom
        self._zoom = self._resolved_zoom(0)

        with self._quiet_scrollbars():
            self._update_scrollbars()
            self.horizontalScrollBar().setValue(0)
            self.verticalScrollBar().setValue(0)

        self.viewport().update()
        self._request_render()
        self._refresh_current_page()
        if self._zoom != before:
            self.zoom_changed.emit()

    def clear_document(self) -> None:
        """表示を空にする。"""
        self._layout = None
        self._render.reset()
        with self._quiet_scrollbars():
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

    # -------------------------------------------------- ページの色
    @property
    def page_color_mode(self) -> PageColorMode:
        """ページ画像に適用している色変換。"""
        return self._render.color_mode

    def set_page_color_mode(self, mode: PageColorMode) -> None:
        """ページ色変換を切り替える。

        アプリ全体の表示設定なので、ドキュメントを開き直しても保たれる。

        **PDF のレンダリングはやり直さない。** 表示用画像を捨てて描き直す
        だけで、raw 画像はそのまま残る。倍率・倍率モード・現在ページ・
        スクロール位置のどれも触らない。

        raw がまだ無いページについても、ここで要求を積み直さない。必要な
        ページの要求は `PageRenderService` の台帳に残っており、処理中の
        枠が空くたびに続きが発行されるため、モードを変えたことで足りなく
        なる要求は無い。積み直すと、モードの連打で要求だけが増える。
        """
        if mode is self.page_color_mode:
            return
        self._render.set_color_mode(mode)
        self.viewport().update()
        self.page_color_mode_changed.emit()

    # -------------------------------------------------- 表示倍率
    @property
    def zoom(self) -> float:
        """現在の表示倍率。"""
        return self._zoom

    @property
    def zoom_mode(self) -> ZoomMode:
        """表示倍率の決め方。"""
        return self._zoom_mode

    @property
    def last_free_zoom(self) -> float:
        """最後に手動で指定された倍率。

        フィット中の倍率はビューポートの大きさ次第なので保存しても意味が
        ない。一方「最後に手で選んだ倍率」は、フィットへ切り替えたあとも
        覚えておきたい値なので、`zoom` とは別に持つ。
        """
        return self._last_free_zoom

    def set_zoom(self, zoom: float) -> None:
        """表示倍率を手動で指定する。`ZoomMode.FREE` へ移行する。

        範囲外は `MIN_ZOOM`〜`MAX_ZOOM` に収める。倍率を変えた瞬間に
        文書の先頭へ飛ばないよう、ビューポート中央のコンテンツ位置を保つ。
        """
        self._set_zoom_state(zoom, ZoomMode.FREE, self._center_anchor())

    def zoom_by(self, factor: float) -> None:
        """現在の倍率に比を掛ける。`ZoomMode.FREE` へ移行する。"""
        self.set_zoom(self._zoom * factor)

    def zoom_in(self) -> None:
        """1段階拡大する。加算ではなく `ZOOM_STEP` 倍。"""
        self.zoom_by(ZOOM_STEP)

    def zoom_out(self) -> None:
        """1段階縮小する。"""
        self.zoom_by(1.0 / ZOOM_STEP)

    def fit_width(self) -> None:
        """現在ページの幅をビューポートに合わせる。"""
        self._apply_fit(ZoomMode.FIT_WIDTH)

    def fit_page(self) -> None:
        """現在ページ全体をビューポートに収める。"""
        self._apply_fit(ZoomMode.FIT_PAGE)

    def _apply_fit(self, mode: ZoomMode) -> None:
        """フィットモードへ移行し、そのときの現在ページを基準に倍率を決める。

        基準にするのは **この瞬間の** 現在ページだけ。以後スクロールして
        ページを跨いでも倍率は変えない（`current_page_changed` から
        ここを呼ばないこと）。ページを跨ぐたびに倍率が動くと読めない。
        """
        self._set_zoom_state(self._fit_zoom(mode, self._anchor_page()), mode, self._center_anchor())

    def _fit_zoom(self, mode: ZoomMode, page: int) -> float:
        """フィット倍率。ドキュメントが無いか FREE なら現在の倍率のまま。

        `PageLayout` は表示倍率の許容範囲を知らないので 0.0 を返しうる。
        `_set_zoom_state()` が下限へ丸める。
        """
        layout = self._layout
        if layout is None or mode is ZoomMode.FREE:
            return self._zoom
        return _fit_zoom_of(layout, mode, page, self._fit_viewport_size(layout, mode, page))

    def _fit_viewport_size(self, layout: PageLayout, mode: ZoomMode, page: int) -> QSizeF:
        """フィット倍率の基準にするビューポートの大きさ。

        **いま縦スクロールバーが出ているかどうかで決めてはならない。**
        バーの出入りはビューポートを狭め、`resizeEvent` を通じてフィット
        倍率を変え、それがまたバーの出入りを変える。境界付近の文書では
        「バーが出る ⇄ 引っ込む」が止まらなくなる。

        そこでバーが無いときの大きさ（`maximumViewportSize()`）だけを
        入力にして、「その倍率で縦スクロールが要るならバーの幅を引く」と
        一意に決める。現在の表示状態が入らないので、同じウィンドウサイズ
        なら常に同じ倍率になり、1〜2回の追随で必ず止まる。

        境界付近では、結果的にバーが引っ込んでも幅を引いたままになる
        （数ピクセル小さく表示される）。デバウンスで振動を隠すのではなく、
        決定的に収束する側を選んだ結果。
        """
        full = QSizeF(self.maximumViewportSize())
        zoom = min(max(_fit_zoom_of(layout, mode, page, full), MIN_ZOOM), MAX_ZOOM)
        if layout.content_size(zoom).height() <= full.height():
            return full
        extent = self.verticalScrollBar().sizeHint().width()
        return QSizeF(max(full.width() - extent, 0.0), full.height())

    def _resolved_zoom(self, page: int) -> float:
        """いまのモードで page を基準にしたときの倍率（範囲内に丸め済み）。"""
        return min(max(self._fit_zoom(self._zoom_mode, page), MIN_ZOOM), MAX_ZOOM)

    def _anchor_page(self) -> int:
        """フィットの基準にするページ。ドキュメントが無ければ先頭。"""
        return max(self._current_page, 0)

    def _set_zoom_state(self, zoom: float, mode: ZoomMode, anchor: _ZoomAnchor | None) -> None:
        """倍率とモードを一度に変える。ズームの唯一の入口。

        中央アンカー・ポインタアンカー・フィットのすべてがここを通るので、
        1回の倍率変更につきスクロールバー更新・再描画・レンダリング要求は
        それぞれ1回で済む。
        """
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        changed = zoom != self._zoom or mode is not self._zoom_mode

        self._zoom_mode = mode
        if mode is ZoomMode.FREE:
            self._last_free_zoom = zoom
        if zoom != self._zoom:
            self._commit_zoom(zoom, anchor)
        if changed:
            self.zoom_changed.emit()

    def _commit_zoom(self, zoom: float, anchor: _ZoomAnchor | None) -> None:
        """新しい倍率を反映し、追随をまとめて1回だけ行う。"""
        self._zoom = zoom
        with self._quiet_scrollbars():
            self._update_scrollbars()
            if anchor is not None:
                self._restore_anchor(anchor)

        self.viewport().update()
        self._request_render()
        self._refresh_current_page()

    # -------------------------------------------------- アンカー
    def _center_anchor(self) -> _ZoomAnchor | None:
        """ビューポート中央に据えるページと、そのページ内の正規化座標。

        比率は `_anchor_ratio()` でページの内側に収める。ページ全体が
        見えている場合はページ中心を中央に据える。
        """
        if self._layout is None:
            return None
        viewport = self.content_viewport_rect()
        page = self._layout.current_page(viewport, self._zoom)
        rect = self._layout.page_rect(page, self._zoom)
        size = self.viewport().size()
        return _ZoomAnchor(
            page=page,
            normalized=QPointF(
                _anchor_ratio(viewport.left(), viewport.right(), rect.left(), rect.right()),
                _anchor_ratio(viewport.top(), viewport.bottom(), rect.top(), rect.bottom()),
            ),
            viewport_pos=QPointF(size.width() / 2, size.height() / 2),
        )

    def _pointer_anchor(self, position: QPointF) -> _ZoomAnchor | None:
        """カーソル直下の点を留めるアンカー。

        カーソルがページではなく余白や隙間の上にある場合は、現在ページの
        いちばん近い縁へ丸める。ページの外を基準にすると、倍率を変えた
        瞬間に遠くへ飛んでしまう。
        """
        if self._layout is None:
            return None
        content = position + self._scroll_offset()
        page = self._layout.page_at(content, self._zoom)
        if page is None:
            page = self._layout.current_page(self.content_viewport_rect(), self._zoom)
        normalized = self._layout.to_normalized(page, content, self._zoom)
        return _ZoomAnchor(
            page=page,
            normalized=QPointF(_clamp_unit(normalized.x()), _clamp_unit(normalized.y())),
            viewport_pos=position,
        )

    def _restore_anchor(self, anchor: _ZoomAnchor) -> None:
        """アンカーの点が元のビューポート位置に来るようスクロールする。"""
        if self._layout is None:
            return
        content = self._layout.from_normalized(anchor.page, anchor.normalized, self._zoom)
        self.horizontalScrollBar().setValue(round(content.x() - anchor.viewport_pos.x()))
        self.verticalScrollBar().setValue(round(content.y() - anchor.viewport_pos.y()))

    # -------------------------------------------------- 現在ページ
    @property
    def current_page(self) -> int:
        """いま読んでいるページ（0 始まり）。無ければ `NO_PAGE`。"""
        return self._current_page

    def go_to_page(self, page_index: int) -> None:
        """指定ページの上端をビューポート上端へ持ってくる（0 始まり）。

        範囲外は先頭/末尾に丸める。最終ページなど可動域の都合で上端まで
        寄せきれない場合は、行けるところまでで止まる。

        スクロールバー経由で動かすので、現在ページ・レンダリング要求・
        再描画は通常のスクロールと同じ経路で更新される。
        """
        if self._layout is None:
            return
        index = min(max(page_index, 0), self._layout.page_count - 1)
        top = self._layout.page_rect(index, self._zoom).top() - self._layout.metrics.margin
        self.verticalScrollBar().setValue(round(top))

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
            overflow = content_length - viewport_length - _SCROLL_EPSILON
            bar.setRange(0, max(math.ceil(overflow), 0))
            bar.setPageStep(viewport_length)
            bar.setSingleStep(_SCROLL_STEP)

    @contextmanager
    def _quiet_scrollbars(self) -> Iterator[None]:
        """可動域と位置をまとめて更新する間、`scrollContentsBy()` を止める。

        更新の途中でスクロールバーが値を切り詰めるたびに追随すると、中間状態
        での再描画・レンダリング要求・`current_page_changed` が出てしまう。
        呼び出し側は抜けたあとに一度だけまとめて行う。

        **`blockSignals()` は使わない。** 可動域の変化は
        `QAbstractScrollArea` がバーを出し入れする合図でもあるので、
        黙らせると PDF を開いてもスクロールバーが出ないままになる。
        止めたいのは自分の追随だけなので、フラグで自分だけを止める。
        """
        previous = self._scroll_updates_suppressed
        self._scroll_updates_suppressed = True
        try:
            yield
        finally:
            self._scroll_updates_suppressed = previous

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802 (Qt の命名規則)
        """スクロール位置が変わったときの追随。

        ビューポート全体を描き直す。ここでスクロールバーを触らないので、
        シグナルが再帰することはない。
        """
        super().scrollContentsBy(dx, dy)
        if self._scroll_updates_suppressed:
            return
        self.viewport().update()
        self._request_render()
        self._refresh_current_page()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """リサイズに追随する。レイアウトもキャッシュも作り直さない。

        フィットモードなら倍率を計算し直す。FREE なら倍率は動かさない。
        どちらの場合も `_commit_zoom()` に通し、リサイズ由来の追随と
        倍率変更由来の追随でレンダリング要求が二重に出ないようにする。
        """
        super().resizeEvent(event)
        zoom = self._resolved_zoom(self._anchor_page())
        changed = zoom != self._zoom
        self._commit_zoom(zoom, self._center_anchor() if changed else None)
        if changed:
            self.zoom_changed.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """Ctrl + ホイールでズーム。修飾なしのホイールは通常のスクロール。

        カーソル直下の PDF 上の点が動かないようにする。倍率の更新経路は
        メニューからのズームと同じ `_set_zoom_state()`。
        """
        delta = event.angleDelta().y()
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier) or delta == 0:
            super().wheelEvent(event)
            return

        self._set_zoom_state(
            self._zoom * ZOOM_STEP ** (delta / _WHEEL_NOTCH),
            ZoomMode.FREE,
            self._pointer_anchor(event.position()),
        )
        event.accept()

    def event(self, event: QEvent) -> bool:
        """画面の `devicePixelRatio` が変わったらレンダリングし直す。

        高 DPI モニタへ移動しただけではスクロールもリサイズもズームも
        起きないので、ここで拾わないと古い DPR の画像を拡大したまま
        表示し続けてしまう。ビューの DPR はビュー自身が知っているので、
        MainWindow ではなくここで扱う。
        """
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._request_render()
            self.viewport().update()
        return super().event(event)

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

        painter.fillRect(rect, _PAGE_COLORS[self._render.color_mode])

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
        """浮動小数点の矩形を、確実に覆う整数矩形にする。

        枠線のペンが矩形の境界の外側にはみ出す分を 1px 見込む。
        """
        return rect.toAlignedRect().adjusted(-1, -1, 1, 1).intersected(self.viewport().rect())

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
