"""`anp.storage.database` のテスト。

マイグレーション機構そのものを検証する。実際のテーブル定義は Phase 2 で
追加されるため、ここではテスト用のマイグレーションを注入する。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from anp.storage import database


def _create_notes(connection: sqlite3.Connection) -> None:
    """テスト用マイグレーション1: テーブルを作る。"""
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")


def _add_notes_column(connection: sqlite3.Connection) -> None:
    """テスト用マイグレーション2: 列を足す。"""
    connection.execute("ALTER TABLE notes ADD COLUMN created_at TEXT")


@contextmanager
def _connect(
    path: Path,
    migrations: Sequence[database.Migration] = (),
) -> Iterator[sqlite3.Connection]:
    """接続を開き、終了時に確実に閉じる。"""
    with closing(database.connect(path, migrations=migrations)) as connection:
        yield connection


def test_connect_creates_file_and_parent_directory(tmp_path: Path) -> None:
    """親ディレクトリごと DB ファイルが作られる。"""
    db_path = tmp_path / "nested" / "anp.sqlite3"

    with _connect(db_path) as connection:
        assert database.schema_version(connection) == 0

    assert db_path.is_file()


def test_no_tables_exist_initially(tmp_path: Path) -> None:
    """Phase 0 ではテーブルを作らない。"""
    with _connect(tmp_path / "anp.sqlite3") as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()

    assert rows == []


def test_migrations_are_applied_in_order(tmp_path: Path) -> None:
    """マイグレーションが順に適用され、スキーマ版が件数と一致する。"""
    with _connect(tmp_path / "anp.sqlite3", [_create_notes, _add_notes_column]) as connection:
        assert database.schema_version(connection) == 2
        columns = {row[1] for row in connection.execute("PRAGMA table_info(notes)")}

    assert columns == {"id", "body", "created_at"}


def test_applied_migrations_are_not_repeated(tmp_path: Path) -> None:
    """再接続しても適用済みのものは再実行されない。"""
    db_path = tmp_path / "anp.sqlite3"

    with _connect(db_path, [_create_notes]) as connection:
        connection.execute("INSERT INTO notes (body) VALUES ('残す')")

    # 同じマイグレーションを再度渡しても CREATE TABLE は走らない。
    with _connect(db_path, [_create_notes]) as connection:
        assert database.schema_version(connection) == 1
        assert connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_pending_migrations_are_applied_on_reconnect(tmp_path: Path) -> None:
    """後から追加されたマイグレーションだけが適用される。"""
    db_path = tmp_path / "anp.sqlite3"

    with _connect(db_path, [_create_notes]) as connection:
        connection.execute("INSERT INTO notes (body) VALUES ('残す')")

    with _connect(db_path, [_create_notes, _add_notes_column]) as connection:
        assert database.schema_version(connection) == 2
        # 既存データは失われない。
        assert connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    """未来のバージョンが作った DB は開かずに失敗する。"""
    db_path = tmp_path / "anp.sqlite3"

    with _connect(db_path, [_create_notes]) as connection:
        assert database.schema_version(connection) == 1

    with pytest.raises(RuntimeError, match="新しい"):
        database.connect(db_path, migrations=[])


def _create_partial_then_fail(connection: sqlite3.Connection) -> None:
    """テスト用マイグレーション: DDL を実行した後で失敗する。"""
    connection.execute("CREATE TABLE partial (id INTEGER PRIMARY KEY)")
    msg = "migration failed on purpose"
    raise RuntimeError(msg)


def test_failed_migration_leaves_database_untouched(tmp_path: Path) -> None:
    """マイグレーションが途中で失敗したら、スキーマも版も元に戻る。

    DDL が中途半端にコミットされると、版が進まないまま次回の実行で
    「table already exists」になり、DB が開けなくなる。
    """
    db_path = tmp_path / "anp.sqlite3"

    with pytest.raises(RuntimeError, match="on purpose"):
        database.connect(db_path, migrations=[_create_notes, _create_partial_then_fail])

    with _connect(db_path) as connection:
        assert database.schema_version(connection) == 0
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
    # 直前に成功した _create_notes ごと巻き戻る（全部か無かのどちらか）。
    assert tables == []


def test_migration_can_be_retried_after_failure(tmp_path: Path) -> None:
    """失敗したマイグレーションを直せば、次回そのまま適用できる。"""
    db_path = tmp_path / "anp.sqlite3"

    with pytest.raises(RuntimeError, match="on purpose"):
        database.connect(db_path, migrations=[_create_partial_then_fail])

    def _create_partial(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial (id INTEGER PRIMARY KEY)")

    with _connect(db_path, [_create_partial]) as connection:
        assert database.schema_version(connection) == 1


def test_existing_data_survives_failed_migration(tmp_path: Path) -> None:
    """後から足したマイグレーションが失敗しても、既存データは残る。"""
    db_path = tmp_path / "anp.sqlite3"

    with _connect(db_path, [_create_notes]) as connection:
        connection.execute("INSERT INTO notes (body) VALUES ('残す')")

    with pytest.raises(RuntimeError, match="on purpose"):
        database.connect(db_path, migrations=[_create_notes, _create_partial_then_fail])

    with _connect(db_path, [_create_notes]) as connection:
        assert database.schema_version(connection) == 1
        assert connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1


def test_foreign_keys_are_enabled(tmp_path: Path) -> None:
    """外部キー制約が有効になっている。"""
    with _connect(tmp_path / "anp.sqlite3") as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
