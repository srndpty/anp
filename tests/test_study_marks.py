"""学習マーク（StudyMark）のドメインモデルとリポジトリのテスト。

Qt を使わないので `QApplication` なしで走る。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from anp.storage import database
from anp.storage.study_mark import StudyMark, document_key
from anp.storage.study_mark_repository import StudyMarkRepository


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """既定のマイグレーションを適用した一時 DB への接続。"""
    with closing(database.connect(tmp_path / "anp.sqlite3")) as conn:
        yield conn


@pytest.fixture
def repository(connection: sqlite3.Connection) -> StudyMarkRepository:
    """テスト対象のリポジトリ。"""
    return StudyMarkRepository(connection)


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    """マークを付ける対象に見立てたファイル。"""
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    return path


# ---------------------------------------------------------------- document_key
def test_document_key_is_absolute(pdf_path: Path) -> None:
    """識別子は絶対パス。"""
    key = document_key(pdf_path)

    assert Path(key).is_absolute()
    assert key.endswith(os.path.normcase(pdf_path.name))


def test_document_key_ignores_relative_notation(
    tmp_path: Path,
    pdf_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.` や `..` を含む表記、カレントディレクトリ相対の表記が同じ鍵になる。"""
    nested = tmp_path / "sub"
    nested.mkdir()
    monkeypatch.chdir(tmp_path)

    expected = document_key(pdf_path)

    assert document_key(pdf_path.name) == expected
    assert document_key(f"./{pdf_path.name}") == expected
    assert document_key(tmp_path / "." / pdf_path.name) == expected
    assert document_key(nested / ".." / pdf_path.name) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のパスは大文字小文字を区別しない")
def test_document_key_normalizes_case_on_windows(pdf_path: Path) -> None:
    """Windows では大文字小文字の違いで別ドキュメントにならない。"""
    assert document_key(str(pdf_path).upper()) == document_key(str(pdf_path).lower())


def test_document_key_rejects_empty_path() -> None:
    """空文字はカレントディレクトリではなく誤りとして扱う。"""
    with pytest.raises(ValueError, match="empty"):
        document_key("")


# ---------------------------------------------------------------- ドメインモデル
def test_study_mark_round_trips_values() -> None:
    """与えた値がそのまま保持される。"""
    mark = StudyMark(
        id=1,
        document_key="doc",
        page_index=0,
        x_norm=0.25,
        y_norm=0.75,
        mistake_count=3,
        note="メモ",
    )

    assert (mark.page_index, mark.x_norm, mark.y_norm) == (0, 0.25, 0.75)
    assert mark.mistake_count == 3
    assert mark.note == "メモ"


def _mark(**overrides: object) -> StudyMark:
    """既定値から StudyMark を組み立てる。"""
    values: dict[str, object] = {
        "id": 1,
        "document_key": "doc",
        "page_index": 0,
        "x_norm": 0.5,
        "y_norm": 0.5,
        "mistake_count": 1,
        "note": None,
    }
    values.update(overrides)
    return StudyMark(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": 0},
        {"id": -1},
        {"document_key": ""},
        {"page_index": -1},
        {"mistake_count": 0},
        {"x_norm": -0.1},
        {"x_norm": 1.1},
        {"y_norm": -0.1},
        {"y_norm": 1.1},
    ],
)
def test_study_mark_rejects_out_of_range_values(overrides: dict[str, object]) -> None:
    """契約から外れた値は作成時点で失敗する。"""
    with pytest.raises(ValueError):
        _mark(**overrides)


@pytest.mark.parametrize("field", ["x_norm", "y_norm"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_study_mark_rejects_non_finite_coordinates(field: str, value: float) -> None:
    """NaN と無限大は座標として受け取らない。"""
    with pytest.raises(ValueError, match="finite"):
        _mark(**{field: value})


@pytest.mark.parametrize("field", ["id", "page_index", "mistake_count"])
def test_study_mark_rejects_bool_as_integer(field: str) -> None:
    """bool は int の派生だが、整数として受け取らない。"""
    with pytest.raises(TypeError):
        _mark(**{field: True})


def test_study_mark_rejects_non_string_note() -> None:
    """メモは str か None のみ。"""
    with pytest.raises(TypeError):
        _mark(note=123)


@pytest.mark.parametrize("corner", [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)])
def test_study_mark_accepts_page_corners(corner: tuple[float, float]) -> None:
    """ページの四隅は有効な座標。"""
    mark = _mark(x_norm=corner[0], y_norm=corner[1])

    assert (mark.x_norm, mark.y_norm) == corner


def test_study_mark_is_immutable() -> None:
    """作成後に書き換えられない。"""
    mark = _mark()

    with pytest.raises(AttributeError):
        mark.mistake_count = 5  # type: ignore[misc]


# ---------------------------------------------------------------- create
def test_create_starts_at_one_mistake(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """マークを作った時点で「1回間違えた」。"""
    mark = repository.create(pdf_path, 2, 0.25, 0.75)

    assert mark.mistake_count == 1
    assert mark.id > 0


def test_create_round_trips_through_database(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """作った内容が DB から取り出しても一致する。"""
    created = repository.create(pdf_path, 2, 0.25, 0.75, note="式変形を間違えた")

    fetched = repository.get(created.id)

    assert fetched == created
    assert fetched is not None
    assert fetched.document_key == document_key(pdf_path)
    assert fetched.page_index == 2
    assert (fetched.x_norm, fetched.y_norm) == (0.25, 0.75)
    assert fetched.mistake_count == 1
    assert fetched.note == "式変形を間違えた"


def test_create_accepts_page_index_zero(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """ページ番号は 0 始まり（QPdfDocument と同じ）。"""
    mark = repository.create(pdf_path, 0, 0.5, 0.5)

    assert mark.page_index == 0


@pytest.mark.parametrize(
    ("page_index", "x_norm", "y_norm"),
    [
        (-1, 0.5, 0.5),
        (0, -0.000001, 0.5),
        (0, 1.000001, 0.5),
        (0, 0.5, -0.000001),
        (0, 0.5, 1.000001),
    ],
)
def test_create_rejects_invalid_position(
    repository: StudyMarkRepository,
    pdf_path: Path,
    page_index: int,
    x_norm: float,
    y_norm: float,
) -> None:
    """範囲外の値は丸めずに失敗させる。"""
    with pytest.raises(ValueError):
        repository.create(pdf_path, page_index, x_norm, y_norm)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_create_rejects_non_finite_coordinates(
    repository: StudyMarkRepository,
    connection: sqlite3.Connection,
    pdf_path: Path,
    value: float,
) -> None:
    """NaN / 無限大が黙って SQLite へ入らない。"""
    with pytest.raises(ValueError):
        repository.create(pdf_path, 0, value, 0.5)

    assert connection.execute("SELECT COUNT(*) FROM study_marks").fetchone()[0] == 0


def test_create_allows_duplicate_coordinates(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """同じ位置に複数のマークを置ける。同一性は id だけ。"""
    first = repository.create(pdf_path, 1, 0.5, 0.5)
    second = repository.create(pdf_path, 1, 0.5, 0.5)

    assert first.id != second.id
    assert len(repository.list_for_document(pdf_path)) == 2


def test_create_handles_special_characters(
    repository: StudyMarkRepository,
    tmp_path: Path,
) -> None:
    """引用符や日本語を含むパス・メモでも壊れない（プレースホルダ束縛）。"""
    path = tmp_path / "O'Brien" / "数学; DROP TABLE study_marks;--.pdf"
    path.parent.mkdir()
    path.write_bytes(b"%PDF-1.4\n")

    mark = repository.create(path, 0, 0.5, 0.5, note="'; DROP TABLE study_marks;--")

    assert repository.get(mark.id) == mark
    assert repository.list_for_document(path) == [mark]


# ---------------------------------------------------------------- list
def test_list_is_isolated_per_document(
    repository: StudyMarkRepository,
    tmp_path: Path,
) -> None:
    """別の PDF のマークは絶対に混ざらない。"""
    a = tmp_path / "A.pdf"
    b = tmp_path / "B.pdf"
    a.write_bytes(b"%PDF-1.4\n")
    b.write_bytes(b"%PDF-1.4\n")

    a1 = repository.create(a, 0, 0.1, 0.1)
    a2 = repository.create(a, 1, 0.2, 0.2)
    b1 = repository.create(b, 0, 0.3, 0.3)

    assert repository.list_for_document(a) == [a1, a2]
    assert repository.list_for_document(b) == [b1]


def test_list_is_ordered_by_page_then_id(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """並び順はページ順、同じページ内は作成順で決定的。"""
    third = repository.create(pdf_path, 5, 0.1, 0.1)
    first = repository.create(pdf_path, 0, 0.9, 0.9)
    second = repository.create(pdf_path, 0, 0.1, 0.1)

    assert repository.list_for_document(pdf_path) == [first, second, third]


def test_list_matches_equivalent_paths(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """表記の違うパスでも同じドキュメントとして引ける。"""
    mark = repository.create(pdf_path, 0, 0.5, 0.5)

    assert repository.list_for_document(pdf_path.parent / "." / pdf_path.name) == [mark]


def test_list_of_unknown_document_is_empty(
    repository: StudyMarkRepository,
    tmp_path: Path,
) -> None:
    """マークの無い PDF では空リスト。"""
    assert repository.list_for_document(tmp_path / "unknown.pdf") == []


# ---------------------------------------------------------------- increment
def test_increment_counts_exact_integers(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """1 → 2 → 3 → 4 と整数で増える（3 以上をまとめない）。"""
    mark = repository.create(pdf_path, 0, 0.5, 0.5)
    assert mark.mistake_count == 1

    counts = [repository.increment_mistake_count(mark.id) for _ in range(3)]

    assert [m.mistake_count for m in counts if m is not None] == [2, 3, 4]
    persisted = repository.get(mark.id)
    assert persisted is not None
    assert persisted.mistake_count == 4


def test_increment_reaches_double_digits(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """10 回でも 10 のまま保存される。"""
    mark = repository.create(pdf_path, 0, 0.5, 0.5)
    for _ in range(9):
        repository.increment_mistake_count(mark.id)

    persisted = repository.get(mark.id)

    assert persisted is not None
    assert persisted.mistake_count == 10


def test_increment_touches_only_target_mark(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """他のマークの回数は変わらない。"""
    target = repository.create(pdf_path, 0, 0.1, 0.1)
    other = repository.create(pdf_path, 0, 0.2, 0.2)

    repository.increment_mistake_count(target.id)

    unchanged = repository.get(other.id)
    assert unchanged is not None
    assert unchanged.mistake_count == 1


# ---------------------------------------------------------------- note
@pytest.mark.parametrize("note", [None, "", "復習する", "式変形を間違えた\n符号に注意", "x²+y²=1"])
def test_note_round_trips(
    repository: StudyMarkRepository,
    pdf_path: Path,
    note: str | None,
) -> None:
    """メモはそのまま往復する。空文字を None に変えない。"""
    mark = repository.create(pdf_path, 0, 0.5, 0.5, note=note)

    fetched = repository.get(mark.id)

    assert fetched is not None
    assert fetched.note == note
    assert (fetched.note is None) == (note is None)


def test_update_note_transitions(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """None → 文字列 → 空文字 → None と更新できる。"""
    mark = repository.create(pdf_path, 0, 0.5, 0.5)
    assert mark.note is None

    assert _note_after(repository, mark.id, "覚え直す") == "覚え直す"
    assert _note_after(repository, mark.id, "") == ""
    assert _note_after(repository, mark.id, None) is None


def _note_after(repository: StudyMarkRepository, mark_id: int, note: str | None) -> str | None:
    """メモを更新し、保存された値を読み直して返す。"""
    updated = repository.update_note(mark_id, note)
    assert updated is not None
    assert updated.note == note

    persisted = repository.get(mark_id)
    assert persisted is not None
    return persisted.note


def test_update_note_keeps_other_fields(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """メモの更新で位置や回数は変わらない。"""
    mark = repository.create(pdf_path, 3, 0.25, 0.75)
    repository.increment_mistake_count(mark.id)

    updated = repository.update_note(mark.id, "メモ")

    assert updated is not None
    assert (updated.page_index, updated.x_norm, updated.y_norm) == (3, 0.25, 0.75)
    assert updated.mistake_count == 2


def test_update_note_rejects_non_string(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """メモに文字列以外は渡せない。"""
    mark = repository.create(pdf_path, 0, 0.5, 0.5)

    with pytest.raises(TypeError):
        repository.update_note(mark.id, 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------- delete
def test_delete_removes_mark(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """削除するとどこからも見えなくなる。"""
    mark = repository.create(pdf_path, 0, 0.5, 0.5)

    assert repository.delete(mark.id) is True
    assert repository.get(mark.id) is None
    assert repository.list_for_document(pdf_path) == []
    # 2回目は消す行が無い。
    assert repository.delete(mark.id) is False


def test_delete_keeps_other_marks(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """削除は指定した1件だけ。"""
    first = repository.create(pdf_path, 0, 0.1, 0.1)
    second = repository.create(pdf_path, 0, 0.2, 0.2)

    repository.delete(first.id)

    assert repository.list_for_document(pdf_path) == [second]


# ---------------------------------------------------------------- 存在しない ID
def test_missing_id_contract(repository: StudyMarkRepository) -> None:
    """存在しない ID では例外にせず、None / False を返す。"""
    assert repository.get(999) is None
    assert repository.increment_mistake_count(999) is None
    assert repository.update_note(999, "メモ") is None
    assert repository.delete(999) is False


# ---------------------------------------------------------------- DB の CHECK 制約
@pytest.mark.parametrize(
    ("page_index", "x_norm", "y_norm", "mistake_count"),
    [
        (-1, 0.5, 0.5, 1),
        (0, -0.1, 0.5, 1),
        (0, 1.1, 0.5, 1),
        (0, 0.5, -0.1, 1),
        (0, 0.5, 1.1, 1),
        (0, 0.5, 0.5, 0),
    ],
)
def test_database_rejects_invalid_rows(
    connection: sqlite3.Connection,
    page_index: int,
    x_norm: float,
    y_norm: float,
    mistake_count: int,
) -> None:
    """リポジトリを通さない INSERT も CHECK 制約で弾かれる。"""
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO study_marks"
            " (document_key, page_index, x_norm, y_norm, mistake_count)"
            " VALUES (?, ?, ?, ?, ?)",
            ("doc", page_index, x_norm, y_norm, mistake_count),
        )


def test_database_rejects_empty_document_key(connection: sqlite3.Connection) -> None:
    """空のドキュメント識別子も CHECK 制約で弾かれる。"""
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO study_marks (document_key, page_index, x_norm, y_norm)"
            " VALUES ('', 0, 0.5, 0.5)"
        )


def test_database_defaults_mistake_count_to_one(connection: sqlite3.Connection) -> None:
    """mistake_count を省略した行は 1 になる。"""
    connection.execute(
        "INSERT INTO study_marks (document_key, page_index, x_norm, y_norm)"
        " VALUES ('doc', 0, 0.5, 0.5)"
    )

    assert connection.execute("SELECT mistake_count FROM study_marks").fetchone()[0] == 1


def test_marks_survive_reconnect(tmp_path: Path, pdf_path: Path) -> None:
    """接続を閉じてもマークは残る（更新が確定している）。"""
    db_path = tmp_path / "anp.sqlite3"

    with closing(database.connect(db_path)) as conn:
        repository = StudyMarkRepository(conn)
        mark = repository.create(pdf_path, 1, 0.25, 0.75, note="メモ")
        repository.increment_mistake_count(mark.id)

    with closing(database.connect(db_path)) as conn:
        reopened = StudyMarkRepository(conn).list_for_document(pdf_path)

    assert len(reopened) == 1
    assert reopened[0].mistake_count == 2
    assert reopened[0].note == "メモ"
