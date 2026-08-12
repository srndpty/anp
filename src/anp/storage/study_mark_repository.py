"""学習マークの永続化。

SQL をここに閉じ込め、UI からは `StudyMarkRepository` 越しに扱う。

接続は `anp.storage.database.connect()` が返したものを受け取るだけで、
この class は所有しない。接続を閉じたり PRAGMA やジャーナルモードを
操作したりはしない。接続は `autocommit=True`（暗黙のトランザクションを
作らない）で開かれているため、更新系はいずれも 1 文で完結させて
アトミック性を得ている（`RETURNING` を使い、読み取ってから書き戻す
2 段構えを避ける）。

ドキュメントの同一性は **`DocumentIdentity`（パスと内容の指紋の組）** で
決まる。この class はファイルを読まない。同一性を作るのは PDF を開いた
1回だけで（`DocumentIdentity.of()`）、以後の操作はその値を持ち回す。
操作のたびにパスから計算し直すと、表示中の PDF と保存先の PDF がずれる。
"""

from __future__ import annotations

import sqlite3

from anp.storage.study_mark import (
    DocumentIdentity,
    StudyMark,
    validate_note,
    validate_position,
)

_COLUMNS = "id, document_key, page_index, x_norm, y_norm, mistake_count, note"


class StoredStudyMarkError(RuntimeError):
    """保存されていた行がドメインの契約を満たしていなかった。

    **想定された失敗経路**として `sqlite3.Error` と同じ扱いにするために
    型を分ける。これが無いと、呼び出し側は行の不整合を捕まえるために
    `ValueError` / `TypeError` を広く捕まえることになり、実装の誤りまで
    「読み込めなかった」に化ける。原因は `__cause__` に残す。
    """


def _to_study_mark(row: sqlite3.Row) -> StudyMark:
    """`study_marks` の1行をドメインオブジェクトへ写す。

    CHECK 制約をすり抜けた行（手で書き換えられた DB など）は
    `StudyMark` の検証で弾かれる。呼び出し側が実装の誤りと区別できるよう、
    ここで保存データの不整合として包み直す。
    """
    try:
        return StudyMark(
            id=row["id"],
            document_key=row["document_key"],
            page_index=row["page_index"],
            x_norm=row["x_norm"],
            y_norm=row["y_norm"],
            mistake_count=row["mistake_count"],
            note=row["note"],
        )
    except (TypeError, ValueError) as error:
        msg = f"stored study mark {row['id']!r} does not satisfy the contract"
        raise StoredStudyMarkError(msg) from error


def _validate_mark_id(mark_id: int) -> None:
    """マーク ID が整数であることを確かめる（bool は受け取らない）。"""
    if isinstance(mark_id, bool) or not isinstance(mark_id, int):
        msg = f"mark_id must be int, got {type(mark_id).__name__}"
        raise TypeError(msg)


class StudyMarkRepository:
    """`study_marks` 表に対する操作。

    存在しない ID を指定したときは例外にせず、`get` / `increment_mistake_count` /
    `update_note` は None、`delete` は False を返す。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(
        self,
        document: DocumentIdentity,
        page_index: int,
        x_norm: float,
        y_norm: float,
        note: str | None = None,
    ) -> StudyMark:
        """学習マークを1件作る。

        「マークを作る＝最初に間違えた」なので `mistake_count` は必ず 1 から
        始まる。呼び出し側に初期値を選ばせない。

        **持ち主は呼び出し側が渡した `DocumentIdentity`。** ここでパスから
        指紋を計算し直さない。表示しているのは開いた時点の PDF なので、
        その間に同じパスが別の内容へ置き換わっていても、いま画面に見えて
        いるものの持ち主として保存する。
        """
        validate_position(page_index, x_norm, y_norm)
        validate_note(note)

        row = self._connection.execute(
            "INSERT INTO study_marks"
            " (document_key, document_fingerprint, page_index, x_norm, y_norm,"
            " mistake_count, note)"
            f" VALUES (?, ?, ?, ?, ?, 1, ?) RETURNING {_COLUMNS}",
            (
                document.key,
                document.fingerprint,
                page_index,
                float(x_norm),
                float(y_norm),
                note,
            ),
        ).fetchone()
        return _to_study_mark(row)

    def get(self, mark_id: int) -> StudyMark | None:
        """ID で1件取り出す。無ければ None。"""
        _validate_mark_id(mark_id)
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM study_marks WHERE id = ?",
            (mark_id,),
        ).fetchone()
        return None if row is None else _to_study_mark(row)

    def list_for_document(self, document: DocumentIdentity) -> list[StudyMark]:
        """指定した PDF の学習マークをすべて返す。

        並び順はページ順、同じページ内は作成順（id）。読み順（y 座標順など）は
        段組みのある PDF で一意に決まらないので契約にしない。

        **パスが同じでも、内容が違えば別のドキュメント。** 指紋が食い違う行は
        返さない。消しはしないので、元の PDF を戻せばまた出てくる。

        指紋が NULL の行（マイグレーション2 より前に作られた分）はそのまま
        返す。内容が分からないものを、確かめずに切り離さないため。
        """
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM study_marks WHERE document_key = ?"
            " AND (document_fingerprint IS NULL OR document_fingerprint = ?)"
            " ORDER BY page_index, id",
            (document.key, document.fingerprint),
        ).fetchall()
        return [_to_study_mark(row) for row in rows]

    def increment_mistake_count(self, mark_id: int) -> StudyMark | None:
        """同じ問題をまた間違えたときに、間違えた回数を1増やす。

        1 → 2 → 3 → 4 と整数のまま増える。3 以上をまとめたりはしない。
        """
        _validate_mark_id(mark_id)
        row = self._connection.execute(
            "UPDATE study_marks SET mistake_count = mistake_count + 1"
            f" WHERE id = ? RETURNING {_COLUMNS}",
            (mark_id,),
        ).fetchone()
        return None if row is None else _to_study_mark(row)

    def update_note(self, mark_id: int, note: str | None) -> StudyMark | None:
        """メモを差し替える。空文字と None は区別したまま保存する。"""
        _validate_mark_id(mark_id)
        validate_note(note)
        row = self._connection.execute(
            f"UPDATE study_marks SET note = ? WHERE id = ? RETURNING {_COLUMNS}",
            (note, mark_id),
        ).fetchone()
        return None if row is None else _to_study_mark(row)

    def delete(self, mark_id: int) -> bool:
        """1件削除する。消す行があったかどうかを返す。"""
        _validate_mark_id(mark_id)
        cursor = self._connection.execute("DELETE FROM study_marks WHERE id = ?", (mark_id,))
        return cursor.rowcount > 0
