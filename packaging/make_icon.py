"""`anp.ico` を書き出す。

実行ファイルとショートカットに埋め込むアイコンを作るための開発用スクリプト。
絵は `anp.ui.app_icon` が描くので、ここが受け持つのは **ICO の容器を組む
ところだけ**。アプリの実行時には使わない。

```
uv run python packaging/make_icon.py
```

Qt の ICO 書き出しは1枚しか収められず、大きさごとの絵を持てない
（`QImageWriter` に何枚書いても最後の1枚だけが残る）。小さい表示のために
16px の絵を別に描いてある以上ここは譲れないので、ICO の容器だけ自前で
組む。中身の各コマは PNG で入れる（Windows Vista 以降が対応）。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODeviceBase
from PySide6.QtWidgets import QApplication

from anp.ui.app_icon import ICON_SIZES, app_icon_pixmap

_OUTPUT = Path(__file__).parent / "anp.ico"

# ICONDIR: 予約(2) + 種別(2, 1=アイコン) + コマ数(2)
_HEADER = "<HHH"
# ICONDIRENTRY: 幅(1) 高さ(1) 色数(1) 予約(1) プレーン(2) ビット深度(2)
#               データ長(4) データ位置(4)
_ENTRY = "<BBBBHHII"
_ENTRY_SIZE = struct.calcsize(_ENTRY)


def _png_bytes(size: int) -> bytes:
    """1コマ分の PNG。"""
    # 引数なしで作る。`QBuffer(QByteArray())` は寿命の切れた一時オブジェクト
    # を指してしまう。
    buffer = QBuffer()
    buffer.open(QIODeviceBase.OpenModeFlag.WriteOnly)
    if not app_icon_pixmap(size).save(buffer, "PNG"):
        msg = f"failed to encode the {size}px icon as PNG"
        raise RuntimeError(msg)
    return bytes(buffer.data())


def build_ico(sizes: tuple[int, ...]) -> bytes:
    """大きさごとの絵を1つの ICO にまとめる。"""
    frames = [_png_bytes(size) for size in sizes]

    offset = struct.calcsize(_HEADER) + _ENTRY_SIZE * len(frames)
    directory = bytearray()
    for size, frame in zip(sizes, frames, strict=True):
        # 256px は幅・高さの欄に 0 と書く（1バイトに収まらないため）。
        side = 0 if size >= 256 else size
        directory += struct.pack(_ENTRY, side, side, 0, 0, 1, 32, len(frame), offset)
        offset += len(frame)

    return struct.pack(_HEADER, 0, 1, len(frames)) + bytes(directory) + b"".join(frames)


def main() -> int:
    # `QPixmap` は QGuiApplication を必要とするので、描く前に用意する。
    QApplication(sys.argv)
    _OUTPUT.write_bytes(build_ico(ICON_SIZES))
    sys.stdout.write(f"{_OUTPUT} ({_OUTPUT.stat().st_size} bytes)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
