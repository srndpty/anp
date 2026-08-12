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

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

# 内容の指紋を読み取るときの1回分の読み込み量。
_FINGERPRINT_CHUNK_BYTES = 1024 * 1024

# SHA-256 の16進表記の長さ。スキーマの CHECK と揃える。
FINGERPRINT_LENGTH = 64

_HEX_DIGITS = frozenset("0123456789abcdef")


def document_key(path: Path | str) -> str:
    """PDF を識別する文字列を、パスの表記揺れを吸収して組み立てる。

    相対パスを絶対パスへ直し、`.` / `..` を解決し、Windows では大文字小文字を
    正規化する（`os.path.normcase`）。同じファイルを指す表記が別ドキュメント
    として扱われないようにするため。

    **パスだけでは同一性は決まらない。** 同じパスに別の内容の PDF が置かれる
    ことがあるので、学習マークの持ち主の判定にはこれと
    `document_fingerprint()` の2つを使う。

    既知の制限: 識別子はパスなので、**PDF を別の場所へ移動・リネームすると
    以前の学習マークとは自動的に結び付かない**。内容による追跡（移動しても
    見つける）は非目標のままで、ここで防ぐのは逆向きの取り違えだけ。
    """
    if isinstance(path, str) and not path:
        msg = "document path must not be empty"
        raise ValueError(msg)
    return os.path.normcase(str(Path(path).resolve()))


def document_fingerprint(path: Path | str) -> str:
    """PDF の内容から作る指紋（SHA-256 の16進表記）。

    **同じパスに別の PDF が置かれたことを見分けるために使う。** パスだけを
    識別子にすると、`math.pdf` を別の本で上書きしたときに、古い本の学習
    マークが新しい本のページ番号のところへ「正常なデータとして」表示される。
    位置がそれらしく見えるぶん、単に表示が消えるより危ない。

    内容の全体を読む。1 PDF につき、開いたときとマークを作ったときにしか
    計算しないので、数十 MB の本でも体感できる待ちにはならない。先頭数 KB
    だけの簡易な指紋にしないのは、同じテンプレートで作られた PDF が
    区別できなくなるため。

    読めなければ `OSError` がそのまま出る。呼び出し側は「学習マークを
    読み込めなかった」として扱う。
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_FINGERPRINT_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def validate_fingerprint(fingerprint: str) -> None:
    """指紋が SHA-256 の16進表記であることを確かめる。

    長さだけでなく文字種まで見る。長さしか見ないと、`"x" * 64` のような
    値が「壊れたデータ」ではなく「たまたま一致しない指紋」として通り、
    学習マークが黙って消えたように見える。
    """
    if not isinstance(fingerprint, str):
        msg = f"fingerprint must be str, got {type(fingerprint).__name__}"
        raise TypeError(msg)
    if len(fingerprint) != FINGERPRINT_LENGTH or not _HEX_DIGITS.issuperset(fingerprint):
        msg = f"fingerprint must be {FINGERPRINT_LENGTH} lowercase hex digits, got {fingerprint!r}"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class DocumentIdentity:
    """学習マークの持ち主を決める、PDF の同一性。

    パス（表記揺れを吸収した `document_key()`）と内容の指紋の組。

    **PDF を開いた時点で1回だけ作り、以後はこれを持ち回す。** 操作のたびに
    パスから計算し直すと、開いている PDF と保存先の PDF がずれる。例えば
    A を表示している最中に外部で同じパスへ B が置かれると、Ctrl + クリック
    で作ったマークは「A の座標」なのに「B のマーク」として保存される。
    ファイル全体のハッシュを操作のたびに計算しないので、数百 MB の PDF でも
    マークの作成が待たされない、という効果もある。
    """

    key: str
    fingerprint: str

    def __post_init__(self) -> None:
        validate_document_key(self.key)
        validate_fingerprint(self.fingerprint)

    @classmethod
    def of(cls, path: Path | str) -> DocumentIdentity:
        """PDF を1回読んで同一性を作る。読めなければ `OSError`。"""
        return cls(key=document_key(path), fingerprint=document_fingerprint(path))


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
