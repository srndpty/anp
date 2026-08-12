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
from anp.storage.study_mark import DocumentIdentity, StudyMark, document_key
from anp.storage.study_mark_repository import StoredStudyMarkError, StudyMarkRepository


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


def test_a_stored_fingerprint_that_is_not_a_sha256_is_a_data_error(
    repository: StudyMarkRepository, connection: sqlite3.Connection, pdf_path: Path
) -> None:
    """保存されていた指紋が壊れていたら、保存データの不整合として気づける。

    スキーマの CHECK は長さしか見ないので、ここをすり抜ける値は入りうる。
    黙って「一致しないマーク」として消すと、記録が失われたように見える。
    """
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    connection.execute(
        "UPDATE study_marks SET document_fingerprint = ? WHERE id = ?", ("x" * 64, mark.id)
    )

    with pytest.raises(StoredStudyMarkError):
        repository.get(mark.id)


def test_a_broken_stored_fingerprint_is_not_silently_skipped(
    repository: StudyMarkRepository, connection: sqlite3.Connection, pdf_path: Path
) -> None:
    """壊れた指紋の行は、PDF を開く経路（一覧）でも見逃さない。

    SQL で `document_fingerprint = ?` と絞ると、壊れた値は「一致しない
    だけの行」として素通りし、検証にかからない。利用者からは記録が黙って
    消えたようにしか見えない。
    """
    repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    connection.execute("UPDATE study_marks SET document_fingerprint = ?", ("x" * 64,))

    with pytest.raises(StoredStudyMarkError):
        repository.list_for_document(DocumentIdentity.of(pdf_path))


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
    mark = repository.create(DocumentIdentity.of(pdf_path), 2, 0.25, 0.75)

    assert mark.mistake_count == 1
    assert mark.id > 0


def test_create_round_trips_through_database(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """作った内容が DB から取り出しても一致する。"""
    created = repository.create(
        DocumentIdentity.of(pdf_path), 2, 0.25, 0.75, note="式変形を間違えた"
    )

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
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)

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
        repository.create(DocumentIdentity.of(pdf_path), page_index, x_norm, y_norm)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_create_rejects_non_finite_coordinates(
    repository: StudyMarkRepository,
    connection: sqlite3.Connection,
    pdf_path: Path,
    value: float,
) -> None:
    """NaN / 無限大が黙って SQLite へ入らない。"""
    with pytest.raises(ValueError):
        repository.create(DocumentIdentity.of(pdf_path), 0, value, 0.5)

    assert connection.execute("SELECT COUNT(*) FROM study_marks").fetchone()[0] == 0


def test_create_allows_duplicate_coordinates(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """同じ位置に複数のマークを置ける。同一性は id だけ。"""
    first = repository.create(DocumentIdentity.of(pdf_path), 1, 0.5, 0.5)
    second = repository.create(DocumentIdentity.of(pdf_path), 1, 0.5, 0.5)

    assert first.id != second.id
    assert len(repository.list_for_document(DocumentIdentity.of(pdf_path))) == 2


def test_create_handles_special_characters(
    repository: StudyMarkRepository,
    tmp_path: Path,
) -> None:
    """引用符や日本語を含むパス・メモでも壊れない（プレースホルダ束縛）。"""
    path = tmp_path / "O'Brien" / "数学; DROP TABLE study_marks;--.pdf"
    path.parent.mkdir()
    path.write_bytes(b"%PDF-1.4\n")

    mark = repository.create(
        DocumentIdentity.of(path), 0, 0.5, 0.5, note="'; DROP TABLE study_marks;--"
    )

    assert repository.get(mark.id) == mark
    assert repository.list_for_document(DocumentIdentity.of(path)) == [mark]


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

    a1 = repository.create(DocumentIdentity.of(a), 0, 0.1, 0.1)
    a2 = repository.create(DocumentIdentity.of(a), 1, 0.2, 0.2)
    b1 = repository.create(DocumentIdentity.of(b), 0, 0.3, 0.3)

    assert repository.list_for_document(DocumentIdentity.of(a)) == [a1, a2]
    assert repository.list_for_document(DocumentIdentity.of(b)) == [b1]


def test_list_is_ordered_by_page_then_id(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """並び順はページ順、同じページ内は作成順で決定的。"""
    third = repository.create(DocumentIdentity.of(pdf_path), 5, 0.1, 0.1)
    first = repository.create(DocumentIdentity.of(pdf_path), 0, 0.9, 0.9)
    second = repository.create(DocumentIdentity.of(pdf_path), 0, 0.1, 0.1)

    assert repository.list_for_document(DocumentIdentity.of(pdf_path)) == [first, second, third]


def test_list_matches_equivalent_paths(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """表記の違うパスでも同じドキュメントとして引ける。"""
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)

    equivalent = DocumentIdentity.of(pdf_path.parent / "." / pdf_path.name)
    assert repository.list_for_document(equivalent) == [mark]


def test_list_of_unknown_document_is_empty(
    repository: StudyMarkRepository,
    tmp_path: Path,
) -> None:
    """マークの無い PDF では空リスト。"""
    unknown = tmp_path / "unknown.pdf"
    unknown.write_bytes(b"%PDF-unknown")

    assert repository.list_for_document(DocumentIdentity.of(unknown)) == []


def test_the_identity_of_a_missing_file_fails(tmp_path: Path) -> None:
    """読めないファイルの同一性は作れない。

    内容の指紋を計算できない以上、そのパスのどのマークが持ち主なのかを
    決められない。黙って空のマーク一覧を返すと、記録が消えたように見える。
    """
    with pytest.raises(OSError):
        DocumentIdentity.of(tmp_path / "gone.pdf")


# ---------------------------------------------------------------- 内容の同一性
def test_marks_do_not_follow_a_replaced_file(
    repository: StudyMarkRepository, pdf_path: Path
) -> None:
    """同じパスの PDF を別の内容へ差し替えたら、古いマークは出てこない。

    パスだけを識別子にすると、差し替えた本の同じページ番号のところに、
    前の本のマークが正常なデータとして表示される。位置がそれらしく
    見えるぶん、単に消えるより危ない。
    """
    original = pdf_path.read_bytes()
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    pdf_path.write_bytes(b"%PDF-1.4 another book")

    assert repository.list_for_document(DocumentIdentity.of(pdf_path)) == []

    # 記録は消していない。元の PDF を戻せばまた出てくる。
    pdf_path.write_bytes(original)
    assert repository.list_for_document(DocumentIdentity.of(pdf_path)) == [mark]


def test_marks_survive_an_unchanged_reopen(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """内容が同じなら、開き直しても持ち主のままでいる。"""
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    pdf_path.write_bytes(pdf_path.read_bytes())

    assert repository.list_for_document(DocumentIdentity.of(pdf_path)) == [mark]


def test_marks_without_a_fingerprint_are_not_listed(
    repository: StudyMarkRepository, connection: sqlite3.Connection, pdf_path: Path
) -> None:
    """マイグレーション2 より前に作られた行は、確かめずには表示しない。

    どの内容の PDF に付けられたのか分からないので、普通のマークと同じ顔で
    並べると、差し替えた PDF に前の本のマークが乗る取り違えがそのまま残る。
    消しはせず、数えられる状態にしておく。
    """
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    connection.execute(
        "UPDATE study_marks SET document_fingerprint = NULL WHERE id = ?", (mark.id,)
    )

    identity = DocumentIdentity.of(pdf_path)
    assert repository.list_for_document(identity) == []
    assert repository.unverified_count(identity) == 1
    assert connection.execute("SELECT COUNT(*) FROM study_marks").fetchone()[0] == 1


def test_adopting_makes_the_old_marks_belong_to_this_pdf(
    repository: StudyMarkRepository, connection: sqlite3.Connection, pdf_path: Path
) -> None:
    """引き取れば、以後は普通のマークとして扱われる。"""
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    connection.execute("UPDATE study_marks SET document_fingerprint = NULL")
    identity = DocumentIdentity.of(pdf_path)

    assert repository.adopt_unverified(identity) == [mark]

    assert repository.list_for_document(identity) == [mark]
    assert repository.unverified_count(identity) == 0


def test_adopting_only_touches_this_path(
    repository: StudyMarkRepository, connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """引き取るのは同じパスの分だけ。別の PDF の古いマークには触らない。"""
    mine = tmp_path / "mine.pdf"
    mine.write_bytes(b"%PDF-mine")
    other = tmp_path / "other.pdf"
    other.write_bytes(b"%PDF-other")
    repository.create(DocumentIdentity.of(mine), 0, 0.5, 0.5)
    repository.create(DocumentIdentity.of(other), 0, 0.5, 0.5)
    connection.execute("UPDATE study_marks SET document_fingerprint = NULL")

    assert len(repository.adopt_unverified(DocumentIdentity.of(mine))) == 1

    assert repository.unverified_count(DocumentIdentity.of(other)) == 1


def test_adopting_a_broken_old_row_changes_nothing(
    repository: StudyMarkRepository, connection: sqlite3.Connection, pdf_path: Path
) -> None:
    """引き取ろうとした行が壊れていたら、1件も引き取らない。

    更新だけ確定して読み直しが失敗すると、壊れた行に指紋が焼き込まれた
    まま残り、以後その PDF を開くたびに読み込み失敗になる。更新と読み直しを
    1つのトランザクションにしてあるので、巻き戻る。
    """
    repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    connection.execute("UPDATE study_marks SET document_fingerprint = NULL, note = x'414243'")
    identity = DocumentIdentity.of(pdf_path)

    with pytest.raises(StoredStudyMarkError):
        repository.adopt_unverified(identity)

    assert repository.unverified_count(identity) == 1
    assert repository.list_for_document(identity) == []


# ---------------------------------------------------------------- increment
def test_increment_counts_exact_integers(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """1 → 2 → 3 → 4 と整数で増える（3 以上をまとめない）。"""
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
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
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    for _ in range(9):
        repository.increment_mistake_count(mark.id)

    persisted = repository.get(mark.id)

    assert persisted is not None
    assert persisted.mistake_count == 10


def test_increment_is_a_single_statement(
    repository: StudyMarkRepository,
    connection: sqlite3.Connection,
    pdf_path: Path,
) -> None:
    """回数を増やすのは1文の UPDATE で、読んでから書き戻さない。

    「1 増やす」を SQL の外で計算すると、読み取りと書き込みの間に別の更新が
    入れば取りこぼす。接続は `autocommit=True` でトランザクションを持たない
    ので、アトミック性の拠り所は「1文で完結していること」だけになる。
    同期のテストでは退行しても結果が同じになってしまうため、発行された文を
    直接見る。

    SQL の全文とは比べない（文言を変えるたびに壊れるため）。固定するのは
    「文が1つ」と「読み取りが混ざっていない」の2点。
    """
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        repository.increment_mistake_count(mark.id)
    finally:
        connection.set_trace_callback(None)

    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("UPDATE")
    assert "SELECT" not in statements[0].upper()


def test_increment_touches_only_target_mark(
    repository: StudyMarkRepository,
    pdf_path: Path,
) -> None:
    """他のマークの回数は変わらない。"""
    target = repository.create(DocumentIdentity.of(pdf_path), 0, 0.1, 0.1)
    other = repository.create(DocumentIdentity.of(pdf_path), 0, 0.2, 0.2)

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
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5, note=note)

    fetched = repository.get(mark.id)

    assert fetched is not None
    assert fetched.note == note
    assert (fetched.note is None) == (note is None)


def test_update_note_transitions(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """None → 文字列 → 空文字 → None と更新できる。"""
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)
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
    mark = repository.create(DocumentIdentity.of(pdf_path), 3, 0.25, 0.75)
    repository.increment_mistake_count(mark.id)

    updated = repository.update_note(mark.id, "メモ")

    assert updated is not None
    assert (updated.page_index, updated.x_norm, updated.y_norm) == (3, 0.25, 0.75)
    assert updated.mistake_count == 2


def test_update_note_rejects_non_string(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """メモに文字列以外は渡せない。"""
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)

    with pytest.raises(TypeError):
        repository.update_note(mark.id, 123)  # type: ignore[arg-type]


# ---------------------------------------------------------------- delete
def test_delete_removes_mark(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """削除するとどこからも見えなくなる。"""
    mark = repository.create(DocumentIdentity.of(pdf_path), 0, 0.5, 0.5)

    assert repository.delete(mark.id) is True
    assert repository.get(mark.id) is None
    assert repository.list_for_document(DocumentIdentity.of(pdf_path)) == []
    # 2回目は消す行が無い。
    assert repository.delete(mark.id) is False


def test_delete_keeps_other_marks(repository: StudyMarkRepository, pdf_path: Path) -> None:
    """削除は指定した1件だけ。"""
    first = repository.create(DocumentIdentity.of(pdf_path), 0, 0.1, 0.1)
    second = repository.create(DocumentIdentity.of(pdf_path), 0, 0.2, 0.2)

    repository.delete(first.id)

    assert repository.list_for_document(DocumentIdentity.of(pdf_path)) == [second]


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


@pytest.mark.parametrize(("column", "value"), [("page_index", 0.5), ("mistake_count", 1.5)])
def test_database_rejects_fractional_integers(
    connection: sqlite3.Connection,
    column: str,
    value: float,
) -> None:
    """SQLite の INTEGER 宣言は型を強制しないので、typeof の CHECK で弾く。

    小数が入ると、後でリポジトリから読むときに StudyMark の生成で失敗する。
    """
    values = {"page_index": 0, "mistake_count": 1, column: value}

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO study_marks"
            " (document_key, page_index, x_norm, y_norm, mistake_count)"
            " VALUES ('doc', ?, 0.5, 0.5, ?)",
            (values["page_index"], values["mistake_count"]),
        )


@pytest.mark.parametrize("mark_id", [0, -1, 1.5])
def test_database_rejects_invalid_id(connection: sqlite3.Connection, mark_id: float) -> None:
    """id を明示した INSERT でも 0 / 負数 / 小数は入らない。"""
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO study_marks (id, document_key, page_index, x_norm, y_norm)"
            " VALUES (?, 'doc', 0, 0.5, 0.5)",
            (mark_id,),
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
        mark = repository.create(DocumentIdentity.of(pdf_path), 1, 0.25, 0.75, note="メモ")
        repository.increment_mistake_count(mark.id)

    with closing(database.connect(db_path)) as conn:
        reopened = StudyMarkRepository(conn).list_for_document(DocumentIdentity.of(pdf_path))

    assert len(reopened) == 1
    assert reopened[0].mistake_count == 2
    assert reopened[0].note == "メモ"
