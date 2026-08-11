"""`anp.ui.recent_files` のテスト。

並び・重複の除去・件数の上限は Qt に依存しないので、`QApplication`
なしで決定的に確かめる。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from anp.ui.recent_files import (
    MAX_RECENT_FILES,
    add_recent,
    normalize_recent,
    recent_labels,
    remove_recent,
)


def _paths(tmp_path: Path, *names: str) -> tuple[Path, ...]:
    return tuple(tmp_path / name for name in names)


# ---------------------------------------------------------------- 並び（MRU）
def test_adding_to_an_empty_history(tmp_path: Path) -> None:
    """最初の1件がそのまま先頭になる。"""
    (a,) = _paths(tmp_path, "a.pdf")

    assert add_recent((), a) == (a,)


def test_the_newest_comes_first(tmp_path: Path) -> None:
    """A → B の順に開いたら [B, A]。"""
    a, b = _paths(tmp_path, "a.pdf", "b.pdf")

    assert add_recent(add_recent((), a), b) == (b, a)


def test_reopening_moves_to_the_front(tmp_path: Path) -> None:
    """A → B → A の順に開いたら [A, B]。件数は増えない。"""
    a, b = _paths(tmp_path, "a.pdf", "b.pdf")

    recent = add_recent(add_recent(add_recent((), a), b), a)

    assert recent == (a, b)


def test_the_same_path_never_appears_twice(tmp_path: Path) -> None:
    """同じファイルを何度開いても1件だけ。"""
    a, b = _paths(tmp_path, "a.pdf", "b.pdf")

    recent: tuple[Path, ...] = (a, b)
    for _ in range(5):
        recent = add_recent(recent, a)

    assert recent == (a, b)


def test_the_source_sequence_is_not_modified(tmp_path: Path) -> None:
    """渡した列は書き換えない（呼び出し側の状態を勝手に変えない）。"""
    a, b = _paths(tmp_path, "a.pdf", "b.pdf")
    original = [a]

    add_recent(original, b)

    assert original == [a]


# ---------------------------------------------------------------- 件数の上限
def test_the_history_is_bounded(tmp_path: Path) -> None:
    """上限を超えたら古い方から落とす。"""
    recent: tuple[Path, ...] = ()
    for index in range(MAX_RECENT_FILES + 1):
        recent = add_recent(recent, tmp_path / f"{index}.pdf")

    assert len(recent) == MAX_RECENT_FILES
    assert recent[0] == tmp_path / f"{MAX_RECENT_FILES}.pdf"
    # いちばん古い1件だけが落ちる。
    assert tmp_path / "0.pdf" not in recent
    assert tmp_path / "1.pdf" in recent


def test_exactly_the_limit_keeps_everything(tmp_path: Path) -> None:
    """ちょうど上限までは1件も落とさない（境界）。"""
    recent: tuple[Path, ...] = ()
    for index in range(MAX_RECENT_FILES):
        recent = add_recent(recent, tmp_path / f"{index}.pdf")

    assert len(recent) == MAX_RECENT_FILES


def test_reopening_the_oldest_entry_does_not_drop_it(tmp_path: Path) -> None:
    """満杯のときに古い項目を開き直しても、落ちずに先頭へ来る。"""
    recent: tuple[Path, ...] = ()
    for index in range(MAX_RECENT_FILES):
        recent = add_recent(recent, tmp_path / f"{index}.pdf")
    oldest = recent[-1]

    recent = add_recent(recent, oldest)

    assert recent[0] == oldest
    assert len(recent) == MAX_RECENT_FILES


# ---------------------------------------------------------------- 読み込みの正規化
def test_normalizing_keeps_a_valid_history_as_is(tmp_path: Path) -> None:
    """契約どおりの履歴は並びも件数も変えない。"""
    a, b = _paths(tmp_path, "a.pdf", "b.pdf")

    assert normalize_recent((a, b)) == (a, b)


def test_normalizing_drops_duplicates(tmp_path: Path) -> None:
    """保存されていた重複は、先に書かれていた（より新しい）方を残す。"""
    a, b = _paths(tmp_path, "a.pdf", "b.pdf")
    detoured = tmp_path / "sub" / ".." / "a.pdf"

    assert normalize_recent((a, b, detoured)) == (a, b)


def test_normalizing_enforces_the_limit(tmp_path: Path) -> None:
    """上限を超えて保存されていたら、読み込んだ時点で切り詰める。"""
    stored = tuple(tmp_path / f"{index}.pdf" for index in range(MAX_RECENT_FILES + 10))

    normalized = normalize_recent(stored)

    assert len(normalized) == MAX_RECENT_FILES
    assert normalized == stored[:MAX_RECENT_FILES]


def test_normalizing_an_empty_history(tmp_path: Path) -> None:
    """空なら空のまま。"""
    assert normalize_recent(()) == ()


# ---------------------------------------------------------------- 重複の判定
@pytest.mark.skipif(os.name != "nt", reason="大文字小文字を無視するのは Windows のみ")
def test_windows_paths_differing_in_case_are_the_same_entry(tmp_path: Path) -> None:
    """`C:\\Books\\a.pdf` と `c:\\books\\A.pdf` を別の履歴として並べない。"""
    lower = tmp_path / "books" / "a.pdf"
    upper = Path(str(tmp_path).upper()) / "BOOKS" / "A.PDF"

    recent = add_recent(add_recent((), lower), upper)

    assert recent == (upper,)


def test_a_relative_path_matches_its_absolute_form(tmp_path: Path) -> None:
    """`.` や `..` の入った表記でも同じファイルとして扱う。"""
    absolute = tmp_path / "a.pdf"
    detoured = tmp_path / "sub" / ".." / "a.pdf"

    recent = add_recent(add_recent((), absolute), detoured)

    assert recent == (detoured,)


def test_the_stored_path_is_the_one_that_was_opened(tmp_path: Path) -> None:
    """保存するのは正規化した文字列ではなく、開いたときのパスそのもの。"""
    path = tmp_path / "sub" / ".." / "a.pdf"

    assert add_recent((), path)[0] == path


# ---------------------------------------------------------------- 削除
def test_removing_an_entry_keeps_the_others(tmp_path: Path) -> None:
    """1件だけ外し、並びは変えない。"""
    a, b, c = _paths(tmp_path, "a.pdf", "b.pdf", "c.pdf")

    assert remove_recent((a, b, c), b) == (a, c)


def test_removing_an_absent_entry_changes_nothing(tmp_path: Path) -> None:
    """入っていない項目を外しても何も起きない。"""
    a, b = _paths(tmp_path, "a.pdf", "b.pdf")

    assert remove_recent((a,), b) == (a,)


def test_removing_uses_the_same_duplicate_rule(tmp_path: Path) -> None:
    """表記が違っても同じファイルなら外れる。"""
    a = tmp_path / "a.pdf"
    detoured = tmp_path / "sub" / ".." / "a.pdf"

    assert remove_recent((a,), detoured) == ()


# ---------------------------------------------------------------- 表示文字列
def test_labels_are_file_names(tmp_path: Path) -> None:
    """ふだんはファイル名だけを見せる。"""
    a, b = _paths(tmp_path, "a.pdf", "b.pdf")

    assert recent_labels((a, b)) == ("a.pdf", "b.pdf")


def test_labels_disambiguate_identical_names(tmp_path: Path) -> None:
    """同名のファイルが並ぶときだけディレクトリを添える。"""
    first = tmp_path / "math" / "text.pdf"
    second = tmp_path / "physics" / "text.pdf"
    other = tmp_path / "other.pdf"

    labels = recent_labels((first, second, other))

    assert labels[0] == f"text.pdf — {first.parent}"
    assert labels[1] == f"text.pdf — {second.parent}"
    assert labels[2] == "other.pdf"


def test_labels_are_empty_for_an_empty_history() -> None:
    """履歴が空なら表示文字列も無い。"""
    assert recent_labels(()) == ()
