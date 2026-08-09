"""ページ画像の非同期レンダリング。

`QPdfPageRenderer` を `MultiThreaded` で使い、レンダリングを GUI スレッドから
外す。Qt がドキュメントのスレッド親和性を管理し、結果は `pageRendered`
シグナルで GUI スレッドに戻ってくるので、こちら側では GUI オブジェクトを
ワーカースレッドから触らない。

**`QPdfPageRenderer` に取り消し API は無い**（PySide6 6.11 で実機確認済み）。
一度 `requestPage()` したものは、結果を無視しても最後まで処理される。そのため
「後で結果を捨てる」ではなく「積む前に抑える」ことが設計の中心になる。

1. 要求できるページを、呼び出し側が渡した範囲（可視ページ ± 1）に限る
2. **同時に処理中の要求数に上限を設ける**。範囲を限っても、スクロールで
   要求先が次々変わればキャンセル不能な要求は溜まり続ける
3. 処理中の要求と同じ条件は再要求しない
4. 表示倍率が変わっている間はデバウンスし、落ち着いてから要求する
5. 1枚あたりの要求サイズに上限を設ける

また **同じパラメータの要求が処理中だと、Qt は同じ request ID を返す**
（実機確認済み）。そのため未処理の要求の台帳は `reset()` でも消さない。
消してしまうと、再利用された ID の古い結果を新しい世代のものとして
受け入れてしまう。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, QSize, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions, QPdfPageRenderer

from anp.pdf.cache import RenderCache, RenderKey

logger = logging.getLogger(__name__)

# ARGB32 の1画素あたりのバイト数。
_BYTES_PER_PIXEL = 4

# 1枚のレンダリング要求で許す最大バイト数。キャッシュ上限より十分小さくする。
# これを超える要求は縦横比を保って縮め、表示時に拡大する。高倍率では多少
# 甘くなるが、キャッシュに入らない巨大画像という例外を作らずに済む。
DEFAULT_MAX_RENDER_BYTES = 32 * 1024 * 1024

# 表示倍率が変わってから要求を出すまでの待ち時間（ミリ秒）。
DEFAULT_DEBOUNCE_MS = 100

# 同時に処理中にできる要求の数。可視ページ ± 1 の2画面分を目安にする。
# 取り消せない以上、これがスクロール中の待ち行列の長さの上限になる。
DEFAULT_MAX_INFLIGHT = 6


def clamp_render_size(size: QSize, max_bytes: int = DEFAULT_MAX_RENDER_BYTES) -> QSize:
    """要求サイズを最大バイト数に収まるよう縦横比を保って縮める。"""
    width = max(size.width(), 1)
    height = max(size.height(), 1)

    estimated = width * height * _BYTES_PER_PIXEL
    if estimated <= max_bytes:
        return QSize(width, height)

    scale = math.sqrt(max_bytes / estimated)
    return QSize(max(int(width * scale), 1), max(int(height * scale), 1))


@dataclass(frozen=True, slots=True)
class PageRequest:
    """1ページ分のレンダリング要求。"""

    page_index: int
    size_px: QSize
    dpr: float


@dataclass(frozen=True, slots=True)
class _RequestMeta:
    """発行済みの要求に紐づく情報。"""

    generation: int
    key: RenderKey


class PageRenderService(QObject):
    """レンダリング要求とキャッシュ充填を受け持つ。

    `page_ready` は、あるページの画像が使えるようになったときに発行される。
    受け取った側はそのページの領域だけ再描画すればよい。
    """

    page_ready = Signal(int)

    def __init__(
        self,
        cache: RenderCache,
        *,
        max_render_bytes: int = DEFAULT_MAX_RENDER_BYTES,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._cache = cache
        # キャッシュに入らない大きさを要求しても捨てるしかないので、上限を揃える。
        self._max_render_bytes = min(max_render_bytes, cache.max_bytes)
        self._max_inflight = max_inflight
        self._debounce_ms = debounce_ms

        self._renderer = QPdfPageRenderer(self)
        self._renderer.setRenderMode(QPdfPageRenderer.RenderMode.MultiThreaded)
        self._renderer.pageRendered.connect(self._on_page_rendered)
        self._options = QPdfDocumentRenderOptions()

        # ドキュメントを開き直すたびに増える。古いドキュメントの結果を捨てるために使う。
        self._generation = 0

        # Qt がまだ結果を返していない要求の台帳。取り消せないので reset() でも消さない。
        # 同じパラメータの要求には同じ ID が返るため、1つの ID に複数の
        # RenderKey（DPR 違いなど）が対応しうる。
        self._outstanding: dict[int, list[_RequestMeta]] = {}
        self._outstanding_keys: set[RenderKey] = set()
        self._desired: dict[int, RenderKey] = {}

        self._dispatch_timer = QTimer(self)
        self._dispatch_timer.setSingleShot(True)
        self._dispatch_timer.timeout.connect(self.flush)

    # -------------------------------------------------- ドキュメント
    def set_document(self, document: QPdfDocument) -> None:
        """レンダリング対象の `QPdfDocument` を設定する。

        `DocumentController` は同じ `QPdfDocument` を使い回すため、通常は
        起動時に一度だけ呼べばよい。中身が入れ替わったときは `reset()`。
        """
        self._renderer.setDocument(document)
        self.reset()

    def reset(self) -> None:
        """キャッシュを破棄し、世代を進める。

        PDF を開き直したり閉じたりしたときに呼ぶ。

        **未処理の要求の台帳は消さない。** Qt の待ち行列は取り消せないので、
        台帳を消すと、同じパラメータで再利用された request ID の古い結果を
        新しい世代のものとして受け入れてしまう。古い結果は届いた時点で
        世代が合わず捨てられ、そこで空いた枠に現在必要な分が入る。
        """
        self._generation += 1
        self._desired.clear()
        self._dispatch_timer.stop()
        self._cache.clear()
        logger.info("render state reset (generation %d)", self._generation)

    # -------------------------------------------------- 取得
    def image_for(self, page_index: int, size_px: QSize, dpr: float) -> QImage | None:
        """要求どおりの画像があれば返す。"""
        return self._cache.get(self._key_for(page_index, size_px, dpr))

    def placeholder_for(self, page_index: int, size_px: QSize) -> QImage | None:
        """目的の解像度が揃うまでの仮表示に使う画像。

        同じページの別解像度があればそれを返す。呼び出し側が目標の矩形へ
        拡大縮小して描く。
        """
        return self._cache.nearest(page_index, clamp_render_size(size_px).width())

    # -------------------------------------------------- 要求
    def request_pages(self, requests: Sequence[PageRequest]) -> None:
        """レンダリングしてほしいページ一式を伝える。

        ここで渡された範囲の外には要求を出さない。呼び出し側は可視ページ
        ± 1 ページに絞ること。

        同じページの要求サイズが変わった場合は表示倍率が動いていると見なし、
        落ち着くまで待ってから要求する。ページの並びだけが変わった場合
        （スクロール）は待たない。
        """
        desired = {
            request.page_index: self._key_for(request.page_index, request.size_px, request.dpr)
            for request in requests
        }
        scale_changed = any(
            page in self._desired and self._desired[page].width_px != key.width_px
            for page, key in desired.items()
        )
        self._desired = desired
        self._schedule(scale_changed=scale_changed)

    def flush(self) -> None:
        """待たずに、いま必要な分の要求を発行する。

        処理中の要求数が上限に達している間は発行しない。結果が返って枠が
        空くたびに続きが発行される。要求は `request_pages()` に渡された順に
        処理するので、呼び出し側は優先度の高いページを先に並べること。
        """
        self._dispatch_timer.stop()

        for page_index, key in self._desired.items():
            if len(self._outstanding) >= self._max_inflight:
                break
            if key in self._cache or key in self._outstanding_keys:
                continue

            request_id = self._renderer.requestPage(
                page_index,
                QSize(key.width_px, key.height_px),
                self._options,
            )
            # 同じ画素を返す要求に Qt が同じ ID を割り当てた場合は相乗りする。
            self._outstanding.setdefault(request_id, []).append(_RequestMeta(self._generation, key))
            self._outstanding_keys.add(key)

    # -------------------------------------------------- 検査用
    @property
    def outstanding_keys(self) -> frozenset[RenderKey]:
        """Qt がまだ結果を返していない要求の条件。"""
        return frozenset(self._outstanding_keys)

    @property
    def outstanding_count(self) -> int:
        """Qt がまだ結果を返していない要求の数。"""
        return len(self._outstanding)

    @property
    def generation(self) -> int:
        """現在の世代。"""
        return self._generation

    # -------------------------------------------------- 内部
    def _key_for(self, page_index: int, size_px: QSize, dpr: float) -> RenderKey:
        clamped = clamp_render_size(size_px, self._max_render_bytes)
        return RenderKey(
            page_index=page_index,
            width_px=clamped.width(),
            height_px=clamped.height(),
            dpr=dpr,
        )

    def _schedule(self, *, scale_changed: bool) -> None:
        if scale_changed:
            # 倍率が動いている間は要求を出さない。動きが止まってから最終倍率で1回だけ。
            self._dispatch_timer.start(self._debounce_ms)
        elif not self._dispatch_timer.isActive():
            self._dispatch_timer.start(0)

    def _on_page_rendered(
        self,
        _page: int,
        _size: QSize,
        image: QImage,
        _options: QPdfDocumentRenderOptions,
        request_id: int,
    ) -> None:
        """レンダリング結果を受け取る（GUI スレッドで呼ばれる）。"""
        metas = self._outstanding.pop(request_id, None)
        if metas is None:
            # 身に覚えのない結果。
            return

        for meta in metas:
            self._outstanding_keys.discard(meta.key)

            if meta.generation != self._generation:
                logger.debug("discarding stale render for page %d", meta.key.page_index)
                continue

            # Qt が返す画像の devicePixelRatio は常に 1.0 なので、ここで設定する。
            # 相乗りした要求と共有しないよう複製してから設定する。
            page_image = image.copy() if len(metas) > 1 else image
            page_image.setDevicePixelRatio(meta.key.dpr)
            if self._cache.put(meta.key, page_image):
                self.page_ready.emit(meta.key.page_index)

        # 枠が空いたので、いま必要な分の続きを発行する。
        self.flush()
