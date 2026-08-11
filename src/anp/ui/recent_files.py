"""最近開いた PDF の一覧（MRU）。

Qt に依存しない純粋な関数だけを置く。並び替え・重複の除去・件数の上限は
ここだけが決めるので、`QApplication` なしで決定的にテストできる。

**「最近開いたファイル」と「前回のセッション」は別の概念。** ここが持つのは
利用者が明示的に開いた履歴で、前回読んでいた PDF とその位置は
`Settings` の session 側にある。片方から他方を導かない。

重複の判定には学習マークと同じ `document_key()` を使う。Windows の
`C:\\Books\\a.pdf` と `c:\\books\\A.pdf` を別の履歴として並べないため。
**学習マークの識別子の契約そのものには触らない**（同じ正規化を借りる
だけで、ここでの結果を DB へ書いたりはしない）。表示と保存に使うのは
利用者が開いたときのパスのままで、正規化した文字列ではない。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from anp.storage.study_mark import document_key

# 履歴に残す件数。これを超えたぶんは古い方から落とす。
MAX_RECENT_FILES = 10

# 同名のファイルを見分けるための区切り。`foo.pdf — C:\Books` の形。
_LABEL_SEPARATOR = " — "


def _key(path: Path) -> str:
    """重複判定に使う正規化済みの文字列。

    パスを解決できない（存在しないドライブなど）場合でも履歴の操作を
    失敗させたくないので、そのときは表記のまま比べる。
    """
    try:
        return document_key(path)
    except (OSError, ValueError):
        return str(path)


def normalize_recent(paths: Sequence[Path]) -> tuple[Path, ...]:
    """保存されていた履歴を契約どおりの形にする。

    並びは保たれたまま、重複を落として上限まで切り詰める。設定を手で
    書き換えられていても「重複なし・最大 `MAX_RECENT_FILES` 件」という
    不変条件が読み込みの境界で成り立つようにする（次に PDF を開くまで
    20件並んだままにしない）。

    重複が並んでいたときに残すのは先に書かれていた方。保存された時点で
    新しい順なので、より新しい表記が残る。
    """
    kept: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = _key(path)
        if key in seen:
            continue
        seen.add(key)
        kept.append(path)
    return tuple(kept[:MAX_RECENT_FILES])


def add_recent(paths: Sequence[Path], path: Path) -> tuple[Path, ...]:
    """`path` を先頭に置いた新しい履歴を返す（MRU）。

    同じファイルを指す古い項目は取り除く。上限を超えたぶんは末尾から落とす。
    渡された列は変更しない。
    """
    key = _key(path)
    kept = [existing for existing in paths if _key(existing) != key]
    return tuple([path, *kept][:MAX_RECENT_FILES])


def remove_recent(paths: Sequence[Path], path: Path) -> tuple[Path, ...]:
    """`path` を履歴から取り除く。無ければそのまま。"""
    key = _key(path)
    return tuple(existing for existing in paths if _key(existing) != key)


def recent_labels(paths: Sequence[Path]) -> tuple[str, ...]:
    """メニューに出す表示文字列（履歴と同じ並び）。

    ふだんはファイル名だけ。同名のファイルが複数あるときに限って、
    どれがどれか分かるようにディレクトリを添える。**表示文字列から
    パスを逆算しない**（メニューの項目はパスそのものを持つ）ので、
    ここは見た目の都合だけで決めてよい。
    """
    names = [path.name for path in paths]
    duplicated = Counter(names)
    return tuple(
        name if duplicated[name] == 1 else f"{name}{_LABEL_SEPARATOR}{path.parent}"
        for name, path in zip(names, paths, strict=True)
    )
