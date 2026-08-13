"""ファイルの内容から作る指紋。

**ドメインの知識は持たない。** ここにあるのは「このファイルの中身は
さっきと同じか」を判定するための SHA-256 だけで、それを何の同一性に
使うかは呼び出し側（`anp.storage.study_mark.DocumentIdentity`）が決める。

`pdf` と `storage` の両方から使うので `core` に置く。開いた PDF の指紋を
`DocumentController` が取り、学習マークの持ち主として `storage` が保存する、
という流れで同じ値を共有する。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# 1回分の読み込み量。
_CHUNK_BYTES = 1024 * 1024

# SHA-256 の16進表記の長さ。スキーマの CHECK と揃える。
FINGERPRINT_LENGTH = 64

_HEX_DIGITS = frozenset("0123456789abcdef")


def file_fingerprint(path: Path | str) -> str:
    """ファイルの内容から作る指紋（SHA-256 の16進表記）。

    内容の全体を読む。先頭数 KB だけの簡易な指紋にしないのは、同じ
    テンプレートで作られた PDF が区別できなくなるため。

    **同期で読む。** PDF を開くたびに1回だけ呼ばれる。実測（ローカル SSD）で
    50 MB → 約 45 ms、200 MB → 約 150 ms、800 MB → 約 580 ms なので、
    技術書サイズでは体感できない。スキャン済みの巨大な PDF やネットワーク
    ドライブで開くのが遅いと感じたら、ここをワーカーへ移すことを考える
    （その場合も、指紋が確定するまで学習マークの操作を出さないこと）。

    読めなければ `OSError` がそのまま出る。呼び出し側は「開けなかった」
    「読み込めなかった」として扱う。
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def validate_fingerprint(fingerprint: str) -> None:
    """指紋が SHA-256 の16進表記であることを確かめる。

    長さだけでなく文字種まで見る。長さしか見ないと、`"x" * 64` のような
    値が「壊れたデータ」ではなく「たまたま一致しない指紋」として通り、
    それを持ち主の判定に使っている側では、記録が黙って消えたように見える。
    """
    if not isinstance(fingerprint, str):
        msg = f"fingerprint must be str, got {type(fingerprint).__name__}"
        raise TypeError(msg)
    if len(fingerprint) != FINGERPRINT_LENGTH or not _HEX_DIGITS.issuperset(fingerprint):
        msg = f"fingerprint must be {FINGERPRINT_LENGTH} lowercase hex digits, got {fingerprint!r}"
        raise ValueError(msg)
