"""レンダリング済みページ画像の上限付きキャッシュ。

`QImage` を無制限に溜めるとスキャン PDF ですぐにメモリを使い切るため、
合計バイト数で上限を設けた LRU にする。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtGui import QImage

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 256 * 1024 * 1024


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


class RenderCache:
    """`RenderKey` から `QImage` への LRU キャッシュ。"""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        self._entries: OrderedDict[RenderKey, QImage] = OrderedDict()
        self._total_bytes = 0

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: RenderKey) -> bool:
        return key in self._entries

    @property
    def total_bytes(self) -> int:
        """保持している画像の合計バイト数。"""
        return self._total_bytes

    @property
    def max_bytes(self) -> int:
        """上限バイト数。"""
        return self._max_bytes

    def get(self, key: RenderKey) -> QImage | None:
        """画像を取り出す。取り出したものは最近使ったものとして扱う。"""
        image = self._entries.get(key)
        if image is None:
            return None
        self._entries.move_to_end(key)
        return image

    def nearest(self, page_index: int, width_px: int) -> QImage | None:
        """同じページの、要求幅にいちばん近い画像を返す。

        目的の解像度がまだ無い間、拡大縮小して仮表示するために使う。
        """
        candidates = [key for key in self._entries if key.page_index == page_index]
        if not candidates:
            return None
        best = min(candidates, key=lambda key: abs(key.width_px - width_px))
        return self.get(best)

    def put(self, key: RenderKey, image: QImage) -> bool:
        """画像を格納し、上限を超えた分を古いものから追い出す。

        格納できたかどうかを返す。取得できない画像を「使えるようになった」と
        通知してしまわないよう、呼び出し側は戻り値を見ること。
        """
        size = image.sizeInBytes()
        if size > self._max_bytes:
            # 入れると自分以外を全部追い出したうえで自分も消える。
            # 呼び出し側が要求サイズを絞る前提なので、通常ここには来ない。
            logger.warning("image too large to cache: %d bytes (page %d)", size, key.page_index)
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
