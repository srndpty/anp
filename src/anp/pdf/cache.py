"""ページ画像の上限付きキャッシュ。

`QImage` を無制限に溜めるとスキャン PDF ですぐにメモリを使い切るため、
合計バイト数で上限を設けた LRU にする。

キャッシュは2段ある。**raw と display を混同しないこと。**

| キャッシュ | 鍵 | 中身 |
|---|---|---|
| `RenderCache` | `RenderKey` | `QPdfPageRenderer` が返した未変換の画像 |
| `DisplayCache` | `DisplayKey` | 現在の `PageColorMode` を適用した表示用の画像 |

`PageColorMode` は **PDF のレンダリング条件ではない**ので `RenderKey` には
入れない。色を変えても同じ raw 画像を使い回せる、というのがこの分割の目的。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtGui import QImage

from anp.pdf.color import PageColorMode

logger = logging.getLogger(__name__)

# raw 画像と表示用画像の**定常保持上限**。合計は Phase 1 の 256 MiB のまま。
# 表示用画像は変換が要るモード（Invert など）でしか作らず、必要なのは
# 描画対象のページだけなので、raw 側に多く配分する。
#
# 「定常」と断るのは、LRU が「入れてから溢れた分を追い出す」構造だから。
# 32 MiB の画像を1枚入れる瞬間は上限を超え、追い出しはその直後に起きる。
# レンダリング中の画像や Qt 内部の待ち行列もこの予算の外側にある。
#
# 配分は固定。ORIGINAL で使っている間は表示用の 96 MiB が空いていても raw
# は借りられないので、Phase 1 より raw のヒット率は下がりうる。共有予算に
# するかどうかは、実 PDF での追い出し状況を測ってから決める。
DEFAULT_MAX_BYTES = 160 * 1024 * 1024
DEFAULT_DISPLAY_MAX_BYTES = 96 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RenderKey:
    """レンダリング結果を一意に決める条件。

    幅だけでは足りない。`devicePixelRatio` は `QImage` の論理サイズ
    （`size() / dpr`）に影響するため、同じピクセル幅でも別物になりうる。
    """

    page_index: int
    width_px: int
    height_px: int
    dpr: float


@dataclass(frozen=True, slots=True)
class DisplayKey:
    """表示用画像を一意に決める条件。

    「どの raw 画像に、どの色変換をかけたか」。`RenderKey` を包むのは、
    色変換が PDF のレンダリング条件ではなく、その結果への変換だから。
    """

    render_key: RenderKey
    color_mode: PageColorMode


class ImageCache[KeyT]:
    """鍵から `QImage` への、合計バイト数で上限を設けた LRU キャッシュ。"""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._entries: OrderedDict[KeyT, QImage] = OrderedDict()
        self._total_bytes = 0

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: KeyT) -> bool:
        return key in self._entries

    @property
    def total_bytes(self) -> int:
        """保持している画像の合計バイト数。"""
        return self._total_bytes

    @property
    def max_bytes(self) -> int:
        """上限バイト数。"""
        return self._max_bytes

    def get(self, key: KeyT) -> QImage | None:
        """画像を取り出す。取り出したものは最近使ったものとして扱う。"""
        image = self._entries.get(key)
        if image is None:
            return None
        self._entries.move_to_end(key)
        return image

    def put(self, key: KeyT, image: QImage) -> bool:
        """画像を格納し、上限を超えた分を古いものから追い出す。

        格納できたかどうかを返す。取得できない画像を「使えるようになった」と
        通知してしまわないよう、呼び出し側は戻り値を見ること。
        """
        size = image.sizeInBytes()
        if size > self._max_bytes:
            # 入れると自分以外を全部追い出したうえで自分も消える。
            # 呼び出し側が要求サイズを絞る前提なので、通常ここには来ない。
            logger.warning("image too large to cache: %d bytes (%r)", size, key)
            return False

        if key in self._entries:
            self._total_bytes -= self._entries[key].sizeInBytes()

        self._entries[key] = image
        self._entries.move_to_end(key)
        self._total_bytes += size
        self._evict()
        return True

    def clear(self) -> None:
        """すべて破棄する。"""
        self._entries.clear()
        self._total_bytes = 0

    def _evict(self) -> None:
        while self._total_bytes > self._max_bytes and self._entries:
            _, evicted = self._entries.popitem(last=False)
            self._total_bytes -= evicted.sizeInBytes()


class RenderCache(ImageCache[RenderKey]):
    """`QPdfPageRenderer` が返した未変換の画像のキャッシュ。"""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        super().__init__(max_bytes)

    def nearest_key(self, page_index: int, width_px: int) -> RenderKey | None:
        """同じページの、要求幅にいちばん近い画像の鍵を返す。

        目的の解像度がまだ無い間、拡大縮小して仮表示するために使う。
        画像ではなく鍵を返すのは、呼び出し側がその鍵から表示用画像を
        引き当てられるようにするため。
        """
        candidates = [key for key in self._entries if key.page_index == page_index]
        if not candidates:
            return None
        return min(candidates, key=lambda key: abs(key.width_px - width_px))


class DisplayCache(ImageCache[DisplayKey]):
    """色変換をかけたあとの、表示用画像のキャッシュ。

    `ORIGINAL` の画像はここに入れない。raw をそのまま表示できるので、
    同じ絵を二重に持つ意味がない。
    """

    def __init__(self, max_bytes: int = DEFAULT_DISPLAY_MAX_BYTES) -> None:
        super().__init__(max_bytes)
