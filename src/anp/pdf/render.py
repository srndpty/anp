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

レンダリングの結果（raw 画像）と、それに色変換をかけた表示用画像は
別々に持つ。`PageColorMode` はレンダリング要求の条件ではないので、
色を変えても `QPdfPageRenderer` へ再要求しない。

表示用画像は `QThreadPool` のワーカーで作る（`_pump_transforms()`）。
`image_for()` / `placeholder_for()` は `paintEvent` から呼ばれるので、
引き当てだけを行い画素には触らず、ワーカーの完了も待たない。

**PDF のレンダリング要求と色変換の要求は別の台帳で管理する。** 前者は
Qt が取り消せない待ち行列を持つのに対し、後者はこちらが投入量を決められる。
混ぜると、どちらの上限を守っているのか分からなくなる。共通しているのは
「世代が合わない結果は捨てる」という点だけで、そこは同じ `_generation` を見る。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions, QPdfPageRenderer

from anp.pdf.cache import (
    DEFAULT_DISPLAY_MAX_BYTES,
    DisplayCache,
    DisplayKey,
    RenderCache,
    RenderKey,
)
from anp.pdf.color import PageColorMode, transform_page

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

# 同時にワーカーで走らせる色変換の数。CPU のコア数まで増やさない。
# 大きな `QImage` を何枚も同時に変換すると、CPU の奪い合いと一時的な
# メモリのピークがどちらも悪化する。ここはメモリの背圧も兼ねている
# （変換中は「入力の raw」と「出力」がキャッシュ上限の外側に同時に載る）。
DEFAULT_MAX_TRANSFORM_INFLIGHT = 2


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
class _QtRequestKey:
    """Qt が同一の要求と見なす条件。

    `requestPage()` の引数は (ページ, サイズ, オプション) で **DPR を含まない**。
    そのため DPR だけ違う `RenderKey` は、Qt からは同じ要求に見える。
    オプションは現状固定なので、ページとサイズだけで足りる。
    """

    page_index: int
    width_px: int
    height_px: int


@dataclass(slots=True)
class _OutstandingRequest:
    """Qt に出したまま結果が返っていない要求。

    1つの Qt 要求に、DPR だけが違う複数の `RenderKey` が相乗りしうる。
    ただし相乗りできるのは**同じ世代の条件だけ**。世代が違うものを
    ぶら下げると、古いドキュメントの絵を新しい世代の絵として
    受け入れてしまう。
    """

    generation: int
    qt_key: _QtRequestKey
    render_keys: list[RenderKey]


@dataclass(frozen=True, slots=True)
class _TransformJob:
    """ワーカーへ渡す色変換1件。

    ワーカースレッドが触ってよいのはこの中身だけ。キャッシュもウィジェットも
    サービス自身も渡さない。

    `source` は **暗黙共有された `QImage` の参照を job が自分で持つ**。
    変換の最中に入力が raw の LRU から追い出されても、画素は生き続ける。
    """

    generation: int
    display_key: DisplayKey
    source: QImage


@dataclass(frozen=True, slots=True)
class _TransformResult:
    """ワーカーから GUI スレッドへ戻す結果。失敗なら `image` は None。"""

    job: _TransformJob
    image: QImage | None


class _TransformSignals(QObject):
    """ワーカーの完了を GUI スレッドへ渡すためだけの中継。

    **`PageRenderService` を親にしない。** ウィンドウを閉じている最中に
    サービスが先に消えても、ワーカーが破棄済みのオブジェクトへ emit しない
    ようにするため。受け手側の接続は Qt がサービスの破棄時に自動で切るので、
    行き先を失った emit は黙って捨てられる。
    """

    finished = Signal(object)
    """`_TransformResult` を1件運ぶ。"""


class _TransformRunnable(QRunnable):
    """1件の色変換をワーカースレッドで実行する。"""

    def __init__(self, job: _TransformJob, signals: _TransformSignals) -> None:
        super().__init__()
        self._job = job
        self._signals = signals

    def run(self) -> None:
        """ワーカースレッドで呼ばれる。GUI オブジェクトには触らない。"""
        try:
            image = transform_page(self._job.source, self._job.display_key.color_mode)
        except Exception:
            # スレッド境界。ここで漏らすと Qt のワーカースレッドごと死ぬ。
            # 失敗しても台帳が詰まらないよう、必ず結果を返して枠を解放する。
            logger.exception("page transform failed for %r", self._job.display_key)
            image = None
        self._signals.finished.emit(_TransformResult(job=self._job, image=image))


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
        display_max_bytes: int = DEFAULT_DISPLAY_MAX_BYTES,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        max_transform_inflight: int = DEFAULT_MAX_TRANSFORM_INFLIGHT,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if max_transform_inflight < 1:
            msg = f"max_transform_inflight must be at least 1, got {max_transform_inflight}"
            raise ValueError(msg)

        self._cache = cache
        self._display_cache = DisplayCache(display_max_bytes)
        self._color_mode = PageColorMode.ORIGINAL
        # どちらのキャッシュにも入らない大きさを要求しても捨てるしかないので、
        # 上限を揃える。表示用画像は raw と同じ画素数になる。
        self._max_render_bytes = min(max_render_bytes, cache.max_bytes, display_max_bytes)
        self._max_inflight = max_inflight
        self._debounce_ms = debounce_ms

        self._renderer = QPdfPageRenderer(self)
        self._renderer.setRenderMode(QPdfPageRenderer.RenderMode.MultiThreaded)
        self._renderer.pageRendered.connect(self._on_page_rendered)
        self._options = QPdfDocumentRenderOptions()

        # ドキュメントを開き直すたびに増える。古いドキュメントの結果を捨てるために使う。
        self._generation = 0

        # Qt がまだ結果を返していない要求の台帳。取り消せないので reset() でも消さない。
        self._outstanding: dict[int, _OutstandingRequest] = {}
        # Qt から見た要求条件 → request ID。同じ条件を二重に出さないために持つ。
        self._outstanding_by_qt: dict[_QtRequestKey, int] = {}
        self._outstanding_keys: set[RenderKey] = set()
        self._desired: dict[int, RenderKey] = {}

        # 色変換の台帳。PDF レンダリングの台帳とは共用しない。世代は job 自身が
        # 持つので、ここは「いまワーカーで走っている `DisplayKey`」だけでよい。
        self._max_transform_inflight = max_transform_inflight
        self._transform_inflight: set[DisplayKey] = set()
        # いまの優先度の並びに対して、表示用キャッシュの予算に入れた鍵。
        # 出来上がった画像をキャッシュへ入れてよいかの判断に使う。
        self._transform_admitted: set[DisplayKey] = set()
        # いまの優先度の並びに対して既にワーカーへ出した鍵。予算の勘定が
        # ずれても「完了 → 追い出し → 再投入」を繰り返さないための安全弁。
        # 必要なページの並び・モード・世代が変わった時点で忘れる。
        self._transform_attempted: set[DisplayKey] = set()
        # 専用のスレッドプール。`QPdfPageRenderer` が使うグローバルプールとは
        # 分ける。投入は台帳側で `max_transform_inflight` に抑えるので、
        # プールの中に待ち行列は溜まらない。
        #
        # `QThreadPool` のデストラクタは走っている分の完了を待つ（GIL は
        # 解放されるので固まらないことを実機で確認済み）。終了時に待たされる
        # のは走っている1件分だけで、最も重い条件（上限いっぱいの 32 MiB を
        # Smart Dark）でも実測 50 ms 弱。体感できないので、取り消しの仕組みは
        # 足していない。1件がこれより明らかに重くなったら考え直す。
        self._transform_pool = QThreadPool()
        self._transform_pool.setMaxThreadCount(max_transform_inflight)
        self._transform_signals = _TransformSignals()
        self._transform_signals.finished.connect(
            self._on_transform_finished, Qt.ConnectionType.QueuedConnection
        )

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

        色変換の台帳も同じ理由で消さない。走っているワーカーは止められない
        ので、消すと枠を二重に使ってしまう。古い世代の結果は完了時に捨てられ、
        そこで空いた枠に現在必要な分が入る。
        """
        self._generation += 1
        self._desired.clear()
        self._transform_attempted.clear()
        self._transform_admitted.clear()
        self._dispatch_timer.stop()
        self._cache.clear()
        self._display_cache.clear()
        logger.info("render state reset (generation %d)", self._generation)

    # -------------------------------------------------- ページの色
    @property
    def color_mode(self) -> PageColorMode:
        """ページ画像に適用している色変換。"""
        return self._color_mode

    def set_color_mode(self, mode: PageColorMode) -> None:
        """色変換を切り替える。**raw も表示用画像も捨てない。**

        `QPdfPageRenderer` への再要求は起きないので、Original ⇄ Invert を
        往復してもレンダリング待ちの白紙には戻らない。

        `DisplayKey` はモードを含むので、古いモードの画像が引き当てられる
        ことは鍵の上で起こらない。だから捨てずに残しておき、モードを戻した
        ときに変換をやり直さずに済ませる。残った分は LRU の予算内で
        古いものから追い出される。

        **即座に戻る。** 変換はワーカーで走るので、切り替えた直後は現在の
        モードの画像がまだ無いことがある。その間ビューは `PageColorMode`
        由来のページ下地（Invert なら黒）を描く。
        """
        if mode is self._color_mode:
            return
        self._color_mode = mode
        self._transform_attempted.clear()
        self._pump_transforms()

    # -------------------------------------------------- 取得（描画経路）
    # ここは `paintEvent` から呼ばれる。**引き当てだけを行い、変換はしない。**
    # 表示用画像はワーカーが先に作っておく（`_pump_transforms()`）。
    # まだ無ければ None を返す。**ワーカーの完了をここで待たない。**
    def image_for(self, page_index: int, size_px: QSize, dpr: float) -> QImage | None:
        """要求どおりの解像度の表示用画像があれば返す。"""
        return self._display_lookup(self._key_for(page_index, size_px, dpr))

    def placeholder_for(self, page_index: int, size_px: QSize) -> QImage | None:
        """目的の解像度が揃うまでの仮表示に使う画像。

        同じページの別解像度があればそれを返す。呼び出し側が目標の矩形へ
        拡大縮小して描く。仮表示も **現在のモードのもの** を返す。
        変換前の絵を先に見せると、切り替えた瞬間に元の色が一瞬見える。

        そのため Invert では **変換済みの画像の中から** いちばん近い解像度を
        探す。raw の中から探して変換済みかどうかを後で見ると、raw はあるが
        変換がまだ終わっていない解像度に当たった時点で仮表示を諦めてしまう。
        """
        width = clamp_render_size(size_px, self._max_render_bytes).width()
        if self._color_mode is PageColorMode.ORIGINAL:
            raw_key = self._cache.nearest_key(page_index, width)
            return self._cache.get(raw_key) if raw_key is not None else None

        display_key = self._display_cache.nearest_key(page_index, width, self._color_mode)
        return self._display_cache.get(display_key) if display_key is not None else None

    def raw_image_for(self, page_index: int, size_px: QSize, dpr: float) -> QImage | None:
        """色変換をかける前の画像。検査用。"""
        return self._cache.get(self._key_for(page_index, size_px, dpr))

    def _display_lookup(self, key: RenderKey) -> QImage | None:
        """描画に使う画像を引き当てる。ここでは画素に触らない。"""
        if self._color_mode is PageColorMode.ORIGINAL:
            # ORIGINAL は raw をそのまま表示する。同じ絵を二重に持たない。
            return self._cache.get(key)
        return self._display_cache.get(DisplayKey(render_key=key, color_mode=self._color_mode))

    # -------------------------------------------------- 表示用画像の用意
    def _pump_transforms(self) -> None:
        """いま必要な表示用画像の変換を、空いている枠の分だけワーカーへ投入する。

        呼ばれるのは次の4か所だけ。どれも描画の外側にある。

        1. raw のレンダリングが終わったとき
        2. 色変換のモードが変わったとき
        3. 必要なページの集合が変わったとき（`request_pages()`）
        4. 変換が1件終わって枠が空いたとき

        **待ち行列は持たない。** 必要なものは毎回 `self._desired` から数え
        直し、枠が空いている分だけ投入する。高速なスクロールやズームで
        必要なページが次々変わっても、投入前の古い要求はそのまま消える。
        `self._desired` は優先度順（現在ページ → 他の可視ページ → 先読み）
        なので、枠の奪い合いもその順に決まる。

        **表示用キャッシュの予算に収まる優先度の前半だけを作る。** 予算を
        超える分まで作ると、LRU が「先に作った高優先度」から追い出すので、
        現在ページが下地のままで先読みだけが残る、という逆転が起きる。
        高コストな変換を捨てるために回すくらいなら、最初から作らない。
        """
        if self._color_mode is PageColorMode.ORIGINAL:
            # 変換が不要なので作るものが無い。raw の LRU は描画時の
            # 引き当てで更新される。
            self._transform_admitted.clear()
            return

        budget_used = 0
        admitted: list[DisplayKey] = []
        for page_index, key in self._desired.items():
            # 目的の解像度がまだ無いページには、仮表示に使う別解像度を用意する。
            raw_key = (
                key if key in self._cache else self._cache.nearest_key(page_index, key.width_px)
            )
            # raw 側の LRU もここで更新する。描画時の引き当ては表示用しか見ないので、
            # ここで触らないと、表示用が残っているのに元の raw が追い出されてしまう。
            # 予算や枠が尽きても最後まで回すのはこのため。
            raw = self._cache.get(raw_key) if raw_key is not None else None
            if raw_key is None or raw is None:
                # そのページには使える画像がまだ1枚も無い。
                continue

            # 変換後は必ず ARGB32 になるので、大きさは raw の画素数から決まる。
            budget_used += raw_key.width_px * raw_key.height_px * _BYTES_PER_PIXEL
            if budget_used > self._display_cache.max_bytes:
                continue

            display_key = DisplayKey(render_key=raw_key, color_mode=self._color_mode)
            admitted.append(display_key)
            if display_key in self._display_cache or display_key in self._transform_inflight:
                # 同じ `DisplayKey` に複数のワーカーを起こさない。世代違いの
                # 変換が走っている場合も、それが終わって枠が空いてから出し直す。
                continue
            if display_key in self._transform_attempted:
                # この優先度の並びに対しては一度作った。予算の勘定が合わずに
                # 追い出されたとしても作り直さない（作り直しても同じことの
                # 繰り返しになる）。予算での足切りが本筋で、こちらは安全弁。
                continue
            if len(self._transform_inflight) >= self._max_transform_inflight:
                continue

            self._transform_inflight.add(display_key)
            self._transform_attempted.add(display_key)
            self._submit_transform(
                _TransformJob(
                    generation=self._generation,
                    display_key=display_key,
                    # 暗黙共有の参照を job 側で持つ。変換の最中に raw の LRU
                    # から追い出されても画素は生き続ける。
                    source=QImage(raw),
                )
            )

        self._transform_admitted = set(admitted)
        # 予算に入れた分を **優先度の低い方から** 触り、LRU の並びを優先度と
        # 揃える。追い出しが起きるときに真っ先に消えるのが最も低い優先度に
        # なり、現在ページが最後まで残る。
        for display_key in reversed(admitted):
            self._display_cache.get(display_key)

    def _submit_transform(self, job: _TransformJob) -> None:
        """変換1件をワーカーへ投入する。

        テストはここを差し替えてワーカーを捕捉し、`_on_transform_finished()`
        を直接呼んで完了させる。
        """
        self._transform_pool.start(_TransformRunnable(job, self._transform_signals))

    def _on_transform_finished(self, result: _TransformResult) -> None:
        """変換の結果を受け取る（GUI スレッドで呼ばれる）。

        キャッシュの書き換えと通知はここだけで行う。ワーカーは `QImage` を
        作って返すだけで、キャッシュにもウィジェットにも触らない。
        """
        job = result.job
        self._transform_inflight.discard(job.display_key)

        if result.image is None or result.image.isNull():
            # 失敗した画像はキャッシュに入れない。枠は上で解放済みなので、
            # 台帳が詰まることはない。
            logger.warning("dropping failed transform for %r", job.display_key)
        elif job.generation != self._generation:
            # 前のドキュメントの変換。**絶対にキャッシュへ入れない。**
            logger.debug("discarding stale transform for %r", job.display_key)
        elif not self._admits(result.image, job.display_key):
            # いま必要な分ではない結果が、いま必要な分を追い出しかけている。
            # 遅れて届いた古い倍率や古いモードの絵がこれに当たる。
            logger.debug("dropping unneeded transform for %r", job.display_key)
        # 現在のモードでなければ通知しない。旧モードの結果で今の表示を
        # 上書きしないため。キャッシュには残すので、戻したときに使える。
        elif (
            self._display_cache.put(job.display_key, result.image)
            and job.display_key.color_mode is self._color_mode
        ):
            self.page_ready.emit(job.display_key.render_key.page_index)

        # 枠が空いたので、いま必要な分の続きを投入する。
        self._pump_transforms()

    def _admits(self, image: QImage, display_key: DisplayKey) -> bool:
        """出来上がった表示用画像をキャッシュへ入れてよいか。

        入れてよいのは次のどちらか。

        1. いま必要な分として予算に入れた鍵（`_transform_admitted`）
        2. 余っている予算にそのまま収まる場合

        2 があるので、モードを戻したときに使い回せる画像は残る。一方、
        遅れて届いた古い倍率・古いモードの結果が、いま表示に使っている
        画像を LRU から押し出すことはない。
        """
        if display_key in self._transform_admitted:
            return True
        return self._display_cache.total_bytes + image.sizeInBytes() <= (
            self._display_cache.max_bytes
        )

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
        # **並び順まで含めて比べる。** この dict の挿入順は実装の都合ではなく
        # 「現在ページ → 他の可視ページ → 先読み」という優先度そのもので、
        # 順序が入れ替われば予算に入る前半も変わる。dict の等値比較は順序を
        # 見ないので、`items()` を並びごと比べる。
        if tuple(desired.items()) != tuple(self._desired.items()):
            # 優先度の並びが変わったので、安全弁の記憶は作り直す。同じ並びを
            # 繰り返し宣言されるだけ（スクロールしない再描画）のときに忘れると、
            # 追い出された分をまた作り始めてしまう。
            self._transform_attempted.clear()
        self._desired = desired
        self._pump_transforms()
        self._schedule(scale_changed=scale_changed)

    def flush(self) -> None:
        """待たずに、いま必要な分の要求を発行する。

        処理中の要求数が上限に達している間は発行しない。結果が返って枠が
        空くたびに続きが発行される。要求は `request_pages()` に渡された順に
        処理するので、呼び出し側は優先度の高いページを先に並べること。
        """
        self._dispatch_timer.stop()

        document = self._renderer.document()
        if document is None or document.status() != QPdfDocument.Status.Ready:
            return

        for page_index, key in self._desired.items():
            if len(self._outstanding) >= self._max_inflight:
                break
            if key in self._cache or key in self._outstanding_keys:
                continue

            qt_key = _QtRequestKey(page_index, key.width_px, key.height_px)
            existing_id = self._outstanding_by_qt.get(qt_key)
            if existing_id is not None:
                existing = self._outstanding[existing_id]
                if existing.generation != self._generation:
                    # 古い世代の同じ要求が処理中。結果が返って捨てられるまで待つ。
                    # ここで相乗りさせると古いドキュメントの絵を受け入れてしまう。
                    continue
                existing.render_keys.append(key)
                self._outstanding_keys.add(key)
                continue

            request_id = self._renderer.requestPage(
                page_index,
                QSize(key.width_px, key.height_px),
                self._options,
            )
            self._outstanding[request_id] = _OutstandingRequest(
                generation=self._generation,
                qt_key=qt_key,
                render_keys=[key],
            )
            self._outstanding_by_qt[qt_key] = request_id
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

    @property
    def display_cache(self) -> DisplayCache:
        """表示用画像のキャッシュ。中身と上限を確かめるために公開している。"""
        return self._display_cache

    @property
    def transform_inflight_count(self) -> int:
        """ワーカーで走っている色変換の数。"""
        return len(self._transform_inflight)

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
        request = self._outstanding.pop(request_id, None)
        if request is None:
            # 身に覚えのない結果。
            return

        self._outstanding_by_qt.pop(request.qt_key, None)
        for key in request.render_keys:
            self._outstanding_keys.discard(key)

        if request.generation != self._generation:
            logger.debug("discarding stale render for page %d", request.qt_key.page_index)
        else:
            ready: list[int] = []
            for key in request.render_keys:
                # Qt が返す画像の devicePixelRatio は常に 1.0 なので、ここで設定する。
                # 相乗りした条件と共有しないよう複製してから設定する。
                page_image = image.copy() if len(request.render_keys) > 1 else image
                page_image.setDevicePixelRatio(key.dpr)
                if self._cache.put(key, page_image):
                    ready.append(key.page_index)

            # raw が増えたので、必要な変換を投入し直す。
            self._pump_transforms()

            # 通知するのは ORIGINAL のときだけ。変換が要るモードでは、raw が
            # 届いただけでは描けるものが増えていない。ここで通知すると、
            # 変換の完了時と合わせて同じページを二度描き直すことになる。
            if self._color_mode is PageColorMode.ORIGINAL:
                for page_index in ready:
                    self.page_ready.emit(page_index)

        # 枠が空いたので、いま必要な分の続きを発行する。
        self.flush()
