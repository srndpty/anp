"""学習マーク（StudyMark）のドメインモデル。

StudyMark は「この問題を間違えた」位置を記録する非破壊メタデータで、PDF
ファイル自体は変更せず SQLite に保存する。マークが存在する時点で最低1回は
間違えているため、`mistake_count` は 1 から始まる整数で、同じ問題を再び
間違えたら 1 ずつ増やす。「1回」「2回」「3回以上」のような列挙にはしない。

座標系は **正規化ページ座標**。Qt / PdfView と同じ向きで、

```
(0, 0) = ページ左上   (1, 0) = ページ右上
(0, 1) = ページ左下   (1, 1) = ページ右下
```

ズーム・ウィンドウサイズ・DPI・Fit Width / Fit Page から独立した値だけを
持つ。ビューポートのピクセル座標は保存しない。

この module は SQLite にも Qt にも依存しない。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


def document_key(path: Path | str) -> str:
    """PDF を識別する文字列を、パスの表記揺れを吸収して組み立てる。

    相対パスを絶対パスへ直し、`.` / `..` を解決し、Windows では大文字小文字を
    正規化する（`os.path.normcase`）。同じファイルを指す表記が別ドキュメント
    として扱われないようにするため。

    既知の制限: 識別子はパスなので、**PDF を別の場所へ移動・リネームすると
    以前の学習マークとは自動的に結び付かない**。内容ハッシュやファイル ID に
    よる追跡は Phase 3-1 の非目標で、必要になったら後続のマイグレーションで
    識別子を拡張する。
    """
    if isinstance(path, str) and not path:
        msg = "document path must not be empty"
        raise ValueError(msg)
    return os.path.normcase(str(Path(path).resolve()))


def validate_document_key(key: str) -> None:
    """ドキュメント識別子が空でない文字列であることを確かめる。"""
    if not isinstance(key, str):
        msg = f"document_key must be str, got {type(key).__name__}"
        raise TypeError(msg)
    if not key:
        msg = "document_key must not be empty"
        raise ValueError(msg)


def validate_position(page_index: int, x_norm: float, y_norm: float) -> None:
    """ページ番号と正規化座標が契約を満たすことを確かめる。

    範囲外の値を黙って 0.0〜1.0 へ丸めない。呼び出し側の誤りとして失敗させる。
    """
    _validate_index(page_index, "page_index")
    _validate_norm(x_norm, "x_norm")
    _validate_norm(y_norm, "y_norm")


def validate_note(note: str | None) -> None:
    """メモが文字列か None であることを確かめる。

    strip や最大長のような方針は持たない。空文字と None も相互に変換しない。
    """
    if note is not None and not isinstance(note, str):
        msg = f"note must be str or None, got {type(note).__name__}"
        raise TypeError(msg)


def _validate_index(value: int, name: str) -> None:
    """0 以上の整数であることを確かめる。

    bool は int の派生なので、`page_index=True` のような値を 1 として
    受け取らないように型そのものを見る。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be int, got {type(value).__name__}"
        raise TypeError(msg)
    if value < 0:
        msg = f"{name} must be >= 0, got {value}"
        raise ValueError(msg)


def _validate_norm(value: float, name: str) -> None:
    """0.0〜1.0 の有限な数であることを確かめる。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{name} must be a real number, got {type(value).__name__}"
        raise TypeError(msg)
    if not math.isfinite(value):
        msg = f"{name} must be finite, got {value!r}"
        raise ValueError(msg)
    if not 0.0 <= value <= 1.0:
        msg = f"{name} must be within 0.0..1.0, got {value!r}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class StudyMark:
    """保存済みの学習マーク1件。

    同一性は `id` だけで決まる。座標は識別子ではないので、同じページの同じ
    位置に複数のマークが並んでいてもよい（近接した別の問題がありうる）。
    """

    id: int
    document_key: str
    page_index: int
    x_norm: float
    y_norm: float
    mistake_count: int
    note: str | None

    def __post_init__(self) -> None:
        _validate_index(self.id, "id")
        if self.id <= 0:
            msg = f"id must be > 0, got {self.id}"
            raise ValueError(msg)
        validate_document_key(self.document_key)
        validate_position(self.page_index, self.x_norm, self.y_norm)
        _validate_index(self.mistake_count, "mistake_count")
        if self.mistake_count < 1:
            msg = f"mistake_count must be >= 1, got {self.mistake_count}"
            raise ValueError(msg)
        validate_note(self.note)
