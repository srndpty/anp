"""連続スクロールの PDF ビュー。

`QAbstractScrollArea` の上に、ページを縦に並べたキャンバスを自前で描く。

このウィジェットは **表示と入力だけ** を受け持つ。ページの配置は
`PageLayout`、画像の生成とキャッシュは `PageRenderService` にあり、
ここに写し取らない。`QPdfDocument` を直接レンダリングもしない。

座標系は4つある。

- **ビューポート座標**: いま画面に見えている領域の論理座標。`paintEvent` と
  マウスイベントが使う
- **コンテンツ座標**: `PageLayout` が使う、文書全体を縦に並べた論理座標。
  ビューポート座標との差はスクロール量だけ
- **ページ内の正規化座標**: ページ左上を (0, 0)、右下を (1, 1) とする比率。
  学習マークはこの座標で持つ
- **レンダリング画像の物理ピクセル**: 論理サイズ × `devicePixelRatio`。
  `PageRenderService` への要求サイズだけに使う

**オーバーレイの位置計算に物理ピクセルを混ぜない。** 学習マークの位置は
上の3つの論理座標だけで決まり、DPR には依存しない。

変換は `content_viewport_rect()` と `page_viewport_rect()`、
`page_position_at()` と `viewport_point_for()` に集約する。
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
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from anp.pdf.color import PageColorMode, page_background_color
from anp.pdf.layout import PageLayout
from anp.pdf.render import PageRenderService, PageRequest
from anp.storage.study_mark import StudyMark, validate_position
from anp.ui.appearance import CanvasTheme, canvas_color
from anp.ui.study_marks import (
    PagePosition,
    StudyMarkIndex,
    StudyMarkTarget,
    badge_path,
    draw_badge,
)

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


_PAGE_BORDER_COLOR = QColor(0x9A, 0x9A, 0x9A)

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

# 学習マークを操作する修飾キー。**完全一致で見る**（`Ctrl+Shift` や
# `Ctrl+Alt` では発火しない）。修飾キーの組み合わせを将来の操作へ割り当てる
# 余地を残すため、「Ctrl を含んでいれば」では判定しない。
STUDY_MARK_MODIFIER = Qt.KeyboardModifier.ControlModifier


@dataclass(frozen=True, slots=True)
class ReadingPosition:
    """いま読んでいる位置（ページ番号 + ページ内の縦位置）。

    ビューポート上端に来ているページ内の縦位置を 0.0〜1.0 で持つ。
    スクロールバーのピクセル値も倍率も含まないので、ウィンドウの
    大きさ・DPI・倍率モードが変わっても意味が変わらない。

    `StudyMark` の `PagePosition` とは別の型にする。学習マークは
    「利用者が印を付けた点」で横位置も意味を持つが、こちらは
    「どこまで読んだか」で縦方向しか使わない（P5-1 の契約）。
    同じ型にすると、使わない `x_norm` に意味のない値を入れることになる。
    """

    page_index: int
    y_norm: float


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

    canvas_theme_changed = Signal()
    """キャンバスの色が変わった。メニューの選択表示はこれを見て合わせる。"""

    study_mark_activated = Signal(object)
    """Ctrl + 左クリックで学習マークの操作が要求された（`StudyMarkTarget`）。

    ビューは「どこが押されたか」までしか決めない。作成なのか回数を増やすのかを
    保存へ結び付けるのは受け取った側（`StudyMarkInteraction`）。ページの外を
    押したときは出さない。
    """

    study_mark_menu_requested = Signal(object, object)
    """右クリックされた（`StudyMarkTarget`, グローバル座標の `QPoint`）。

    メニューの中身はビューの関心ではないので、ここでは組み立てない。
    ページの外を押したときは出さない。
    """

    def __init__(self, render_service: PageRenderService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._render = render_service
        self._render.page_ready.connect(self._on_page_ready)

        self._layout: PageLayout | None = None
        self._zoom = 1.0
        self._zoom_mode = ZoomMode.FREE
        self._last_free_zoom = 1.0
        self._current_page = NO_PAGE
        self._canvas_theme = CanvasTheme.DARK_GRAY
        self._study_marks = StudyMarkIndex()

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

        **学習マークは即座に捨てる。** ビューは表示中の PDF と学習マークの
        対応を確かめる手立てを持たない（識別子は呼び出し側が持つ）ので、
        残しておくと別の PDF の同じページ番号へ古いマークを描いてしまう。
        新しいドキュメントの分は呼び出し側が `set_study_marks()` で入れ直す。
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
        self._study_marks = StudyMarkIndex()

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
        """表示を空にする。学習マークも一緒に捨てる。"""
        self._layout = None
        self._study_marks = StudyMarkIndex()
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

    # -------------------------------------------------- キャンバスの色
    @property
    def canvas_theme(self) -> CanvasTheme:
        """ページの外側を塗る色。"""
        return self._canvas_theme

    def set_canvas_theme(self, theme: CanvasTheme) -> None:
        """キャンバスの色を切り替える。

        **`PageColorMode` とは完全に独立。** ここから
        `set_page_color_mode()` を呼ばない。白いキャンバスと反転ページの
        ような組み合わせも、選ばれたとおりに描く。

        必要なのはビューポートの塗り直しだけ。PDF のレンダリングも
        表示用画像の作り直しも起きない。倍率・現在ページ・スクロール位置も
        触らない。
        """
        if theme is self._canvas_theme:
            return
        self._canvas_theme = theme
        self.viewport().update()
        self.canvas_theme_changed.emit()

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
        self.verticalScrollBar().setValue(round(self._page_top_offset(self._layout, index)))

    def reveal_page_position(self, page_index: int, x_norm: float, y_norm: float) -> bool:
        """ページ内の正規化座標が見えるところまで移動する。見せられたか返す。

        サイドバーから学習マークへ飛ぶための入口。**移動の計算はここに置く**。
        呼び出し側にページ配置の算術を書かせない。座標は正規化されたページ
        座標だけで、`devicePixelRatio` も物理ピクセルも使わない。

        いま開いているドキュメントに無いページなら `False` を返して何もしない。
        **最終ページへ丸めたりはしない**（ページ数の減った PDF に差し替えられた
        場合、丸めると関係のない場所へ飛ぶ）。

        見せ方は「そのページの先頭まで移動し、必要ならアンカーが中央に来る
        ところまでさらに送る」。ページの先頭より手前へは戻さないので、
        現在ページは `go_to_page()` と同じく指定したページになる。文書の端や
        可動域の都合で中央に置けない場合はスクロールバーの丸めを受け入れる
        （アンカーがビューポート内に入っていればよい）。

        **倍率と倍率モードは触らない。** Fit Width / Fit Page のまま飛んでも
        FREE へ落ちない。レンダリングもスクロールと同じ経路でしか起こさない
        （キャッシュも世代も色変換も触らない）。
        """
        validate_position(page_index, x_norm, y_norm)
        layout = self._layout
        if layout is None or page_index >= layout.page_count:
            return False

        anchor = layout.from_normalized(page_index, QPointF(x_norm, y_norm), self._zoom)
        size = self.viewport().size()
        top = self._page_top_offset(layout, page_index)

        # 可動域の切り詰めはスクロールバーに任せる。まとめて動かしてから
        # 1回だけ追随する（`_commit_zoom()` と同じ形）。
        with self._quiet_scrollbars():
            self.verticalScrollBar().setValue(round(max(top, anchor.y() - size.height() / 2)))
            self.horizontalScrollBar().setValue(round(anchor.x() - size.width() / 2))

        self.viewport().update()
        self._request_render()
        self._refresh_current_page()
        return True

    # -------------------------------------------------- 読書位置
    def current_reading_position(self) -> ReadingPosition | None:
        """いま読んでいる位置。ドキュメントが無ければ None。

        ビューポートの上端がどのページのどこに当たるかを正規化座標で返す。
        セッションの保存に使う値なので、**スクロールバーの値も倍率も
        含めない**（ウィンドウの大きさや DPI が変わると意味を失うため）。

        **基準にするのは `current_page` ではなく、上端側の最初の可視
        ページ。** `current_page` は「ビューポートといちばん大きく重なる
        ページ」なので、ページの継ぎ目付近では上端がまだ前のページの
        途中なのに次のページを指す。それを基準にすると比率が負になり、
        0.0 に丸めた結果「次のページの先頭」として保存されて、再起動の
        たびに数百ピクセルぶん先へ進んでしまう。

        用途が違うだけで、`current_page` の契約（ページ入力や前/次の
        基準）はそのまま。ここで使い分けるのは読書位置の基準だけ。

        - 上端がページの途中: そのページと正確な比率
        - 上端がページ間の隙間: 次のページの 0.0（`visible_pages()` が
          隙間を次のページから数える）
        - 上端が文書の上余白: 先頭ページの 0.0
        """
        layout = self._layout
        if layout is None or self._current_page < 0:
            return None

        viewport = self.content_viewport_rect()
        visible = layout.visible_pages(viewport, self._zoom)
        # 可視ページが1つも無い（末尾の余白まで送られた）場合だけ、
        # いちばん近いページを使う。
        page = visible.start if visible else layout.current_page(viewport, self._zoom)

        rect = layout.page_rect(page, self._zoom)
        height = rect.height()
        y_norm = (viewport.top() - rect.top()) / height if height > 0 else 0.0
        return ReadingPosition(page_index=page, y_norm=_clamp_unit(y_norm))

    def restore_reading_position(self, position: ReadingPosition) -> bool:
        """読書位置まで戻る。戻れたかを返す。

        `current_reading_position()` の逆変換で、その位置がビューポートの
        上端に来るようスクロールする。**倍率も倍率モードも触らない**ので、
        Fit Width / Fit Page のまま呼んでも FREE に落ちない。呼ぶのは
        倍率が決まった後（順序を逆にすると、フィットの計算がここで決めた
        スクロール位置を動かす）。

        いま開いているドキュメントに無いページなら `False` を返して何も
        しない。**最終ページへ丸めない**（`reveal_page_position()` と同じ
        方針。差し替えでページ数が減った PDF で、関係のない場所へ飛ばない）。

        文書の端で可動域が足りない場合は、行けるところまでで止まる
        （スクロールバーの切り詰めをそのまま受け入れる）。
        """
        validate_position(position.page_index, 0.0, position.y_norm)
        layout = self._layout
        if layout is None or position.page_index >= layout.page_count:
            return False

        top = layout.from_normalized(
            position.page_index, QPointF(0.0, position.y_norm), self._zoom
        ).y()
        with self._quiet_scrollbars():
            self.verticalScrollBar().setValue(round(top))

        self.viewport().update()
        self._request_render()
        self._refresh_current_page()
        return True

    def _page_top_offset(self, layout: PageLayout, index: int) -> float:
        """そのページの上端をビューポート上端へ持ってくるスクロール量。

        `go_to_page()` と `reveal_page_position()` で同じ値を使うために
        分けてある。ページ配置の算術を2箇所に書かない。
        """
        return layout.page_rect(index, self._zoom).top() - layout.metrics.margin

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

    def page_position_at(self, viewport_pos: QPointF) -> PagePosition | None:
        """ビューポート上の点が乗っているページと、そのページ内の正規化座標。

        **ページの上でなければ `None`。** 余白・ページ間の隙間・文書の
        上下いずれも `None` になる。いちばん近いページへ吸着させたり、
        0.0〜1.0 へ丸めたりはしない。ページ外を押したときに「何もしない」
        と決められるのは、ここが丸めないからこそ。

        戻り値はそのまま `StudyMark` の座標として使える契約
        （`page_index >= 0`、`x_norm` と `y_norm` は 0.0〜1.0）。

        **ページ内と判定できた点に限って** 比率を 0.0〜1.0 に収める。
        ページの縁ちょうどでは、内外の判定（`page_at()`）と比率の計算
        （`to_normalized()`）が別々の浮動小数点演算なので、内側と判定された
        点が 1.0000000000000004 のような値になりうる。これをそのまま返すと
        契約が破れ、保存の直前で弾かれる。ページの外を近いページへ吸着させ
        ないという方針とは別の話で、**外は依然として `None`**。
        """
        if self._layout is None:
            return None
        content = viewport_pos + self._scroll_offset()
        page = self._layout.page_at(content, self._zoom)
        if page is None:
            return None
        normalized = self._layout.to_normalized(page, content, self._zoom)
        return PagePosition(
            page_index=page,
            x_norm=_clamp_unit(normalized.x()),
            y_norm=_clamp_unit(normalized.y()),
        )

    def viewport_point_for(self, page_index: int, x_norm: float, y_norm: float) -> QPointF | None:
        """ページ内の正規化座標に対応するビューポート上の点。

        `page_position_at()` の逆変換。ビューポートの外に出る点も、そのまま
        返す（見えているかどうかは呼び出し側の判断）。

        **契約外の値は黙って丸めずに失敗させる。** 判定は P3-1 の
        `validate_position()` をそのまま使い、UI 側に別の基準を作らない。

        いま開いているドキュメントに存在しないページ番号は `None`。
        保存済みのマークがページ数の減った PDF を指していても落ちない
        （最終ページへ丸めたりはしない）。
        """
        validate_position(page_index, x_norm, y_norm)
        if self._layout is None or page_index >= self._layout.page_count:
            return None
        content = self._layout.from_normalized(page_index, QPointF(x_norm, y_norm), self._zoom)
        return content - self._scroll_offset()

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

    # -------------------------------------------------- 学習マーク
    @property
    def study_marks(self) -> tuple[StudyMark, ...]:
        """表示中の学習マーク（`id` 昇順）。

        現在のドキュメントに存在しないページを指すマークもここには含まれる。
        描画とヒットテストの対象から外すだけで、預かったものを捨てはしない。
        """
        return self._study_marks.marks

    def set_study_marks(self, marks: Sequence[StudyMark]) -> None:
        """描画する学習マークを差し替える。

        ビューは **読み取り専用のコレクションだけ** を受け取る。SQLite も
        `StudyMarkRepository` も知らない。どのマークを渡すかは呼び出し側
        （将来のコントローラ）が決める。

        渡されたコレクションは写し取るので、呼び出し側が後から中身を
        変えても表示は動かない。

        起きるのは再描画だけ。PDF のレンダリング要求も色変換も世代の更新も
        起こさない。倍率・スクロール位置・現在ページも触らない。
        """
        self._study_marks = StudyMarkIndex(marks)
        self.viewport().update()

    def study_marks_at(self, viewport_pos: QPointF) -> list[StudyMark]:
        """その点に描かれている学習マーク。上に描かれているものから並べる。

        **バッジの形の中だけがヒット。** 近くにあるだけのマークは返さない。
        判定に使う形は描画とまったく同じ `badge_path()`。

        同じ場所に複数のマークがあってもまとめない。並びは描画順の逆
        （`id` 降順）で決定的。

        対象は描かれているマーク、つまり見えているページの分だけ。
        `page_position_at()` とは別の API のままにしてあるのは、「マークを
        押した」と「空いている場所を押した」が別の操作になるため。
        """
        hits: list[StudyMark] = []
        for index in self.visible_pages():
            for mark in self._study_marks.for_page(index):
                anchor = self.viewport_point_for(index, mark.x_norm, mark.y_norm)
                if anchor is not None and badge_path(anchor, mark.mistake_count).contains(
                    viewport_pos
                ):
                    hits.append(mark)
        hits.reverse()
        return hits

    def study_mark_target_at(self, viewport_pos: QPointF) -> StudyMarkTarget:
        """その点を押したときの学習マークの操作対象。

        **判定の順序が契約**。まず `study_marks_at()`、ヒットが無ければ
        `page_position_at()` を見る。逆にすると、ページの端からはみ出した
        バッジを押したときに、既存のマークではなく新しいマークが増える。

        重なっているマークは **いちばん上の1件だけ** を対象にする
        （`study_marks_at()` の先頭）。まとめて操作したり、選択させたりしない。
        """
        hits = self.study_marks_at(viewport_pos)
        if hits:
            return StudyMarkTarget(mark=hits[0])
        return StudyMarkTarget(position=self.page_position_at(viewport_pos))

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

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """Ctrl + 左クリックで学習マークを操作する。

        **修飾なしの左クリックは学習マークに使わない。** 誤って印が増えるのを
        避けるためと、選択やパンといった後の操作と取り合いにならないため。
        `Shift` や `Alt` を伴う場合も同じで、ここは通り抜ける。
        """
        if not self._handle_study_mark_click(event):
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """2度目の押下も1回の操作として扱う。

        Qt は速い連打の2回目を `MouseButtonPress` ではなく
        `MouseButtonDblClick` として送る。ここで拾わないと、素早く
        Ctrl クリックしたときに1回おきに取りこぼす（1 → 2 → 3 と数えたい
        のに 1 → 2 で止まる）。**押した回数だけ数える** のが契約なので、
        押下の2つの入口を同じ扱いにする。
        """
        if not self._handle_study_mark_click(event):
            super().mouseDoubleClickEvent(event)

    def _handle_study_mark_click(self, event: QMouseEvent) -> bool:
        """学習マークの操作として扱えたか。

        ページの外を押したときは何も起きない（シグナルも出さない）。判定と
        優先順位は `study_mark_target_at()` にある。ここが持つのは
        「どの入力が学習マークの操作か」だけで、保存には触らない。
        """
        if (
            event.button() is not Qt.MouseButton.LeftButton
            or event.modifiers() != STUDY_MARK_MODIFIER
        ):
            return False

        target = self.study_mark_target_at(event.position())
        if target.is_empty:
            return False

        self.study_mark_activated.emit(target)
        event.accept()
        return True

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """右クリックの対象を決めて知らせる。メニューはここでは作らない。

        対象の決め方は Ctrl + 左クリックと同じ `study_mark_target_at()`。
        バッジの上なら既存のマーク、ページの上なら新規作成、ページの外なら
        学習マークのメニューを出さない。
        """
        target = self.study_mark_target_at(QPointF(event.pos()))
        if target.is_empty:
            super().contextMenuEvent(event)
            return

        self.study_mark_menu_requested.emit(target, event.globalPos())
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
        """見えているページだけを描く。

        重ね順は キャンバス → ページ下地 → ページ画像 → 学習マーク。
        学習マークは **ページ画像より後** に描く。ページを1枚ずつ
        「画像 → マーク」と描くのではなくオーバーレイを別の周回にするのは、
        ページの端にあるバッジが次のページの下地で消されないようにするため。

        ここで行うのは軽いベクタ描画だけ。画素変換もレンダリング要求も
        待ち合わせもしない（P2 からの契約）。
        """
        painter = QPainter(self.viewport())
        try:
            painter.fillRect(event.rect(), canvas_color(self._canvas_theme))
            if self._layout is None:
                return

            pages = self.visible_pages()

            # 目的の解像度が揃うまで別解像度の画像を拡大縮小して描くので、
            # 補間を有効にする。paintEvent 内で画像を作り直さないため、
            # QImage.scaled() は使わない。
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setPen(_PAGE_BORDER_COLOR)
            for index in pages:
                self._paint_page(painter, index)

            for index in pages:
                self._paint_study_marks(painter, index)
        finally:
            painter.end()

    def _paint_page(self, painter: QPainter, index: int) -> None:
        rect = self.page_viewport_rect(index)
        if rect is None:
            return

        # ページ矩形の下地は `PageColorMode` 由来。キャンバスの色は
        # ページの内側には入れない。
        painter.fillRect(rect, page_background_color(self._render.color_mode))

        size_px = self._render_size(rect.size())
        image = self._render.image_for(index, size_px, self.devicePixelRatioF())
        if image is None:
            # 目的の解像度がまだ無いので、同じページの別解像度で仮表示する。
            image = self._render.placeholder_for(index, size_px)
        if image is not None:
            painter.drawImage(rect, image)

        painter.drawRect(rect)

    def _paint_study_marks(self, painter: QPainter, index: int) -> None:
        """1ページ分の学習マークを描く。

        引くのはそのページの索引だけなので、マークが何千件あっても
        描画で全件を走査しない。

        バッジはページの色変換（Invert / Smart Dark）を通さない。
        オーバーレイはページ画像の外側にあり、`DisplayCache` にも入らない。
        """
        for mark in self._study_marks.for_page(index):
            anchor = self.viewport_point_for(index, mark.x_norm, mark.y_norm)
            if anchor is None:
                continue
            draw_badge(painter, anchor, mark.mistake_count)

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
