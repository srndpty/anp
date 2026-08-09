"""SQLite への接続とスキーマ更新の入口。

Phase 0 の時点ではテーブルを1つも作らない（`_MIGRATIONS` が空）。学習
メタデータのテーブルは Phase 2 でマイグレーションとして追加する。ここで
用意しているのは「接続の作り方」と「スキーマ版の進め方」だけ。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

Migration = Callable[[sqlite3.Connection], None]

# スキーマ更新の手順。先頭から順に適用し、適用済み件数を user_version に持つ。
# 一度リリースした要素は書き換えず、末尾に追加していく。
_MIGRATIONS: tuple[Migration, ...] = ()


def connect(path: Path, *, migrations: Sequence[Migration] = _MIGRATIONS) -> sqlite3.Connection:
    """データベースに接続し、必要なマイグレーションを適用して返す。

    親ディレクトリが無ければ作成する。返された接続は呼び出し側が閉じる。

    接続は `autocommit=True`（暗黙のトランザクションを作らない）で開く。
    暗黙のトランザクション管理では DDL の前にトランザクションが始まらず、
    マイグレーションの途中で失敗したときにスキーマだけが進んでしまうため、
    トランザクションの範囲は呼び出し側が `BEGIN` で明示する。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, autocommit=True)
    try:
        connection.row_factory = sqlite3.Row
        # PRAGMA foreign_keys はトランザクション内では無視されるため、先に設定する。
        connection.execute("PRAGMA foreign_keys = ON")
        apply_migrations(connection, migrations)
    except BaseException:
        # 呼び出し側は接続を受け取れないので、ここで確実に閉じる。
        connection.close()
        raise
    return connection


def schema_version(connection: sqlite3.Connection) -> int:
    """適用済みマイグレーションの件数を返す。"""
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration] = _MIGRATIONS,
) -> int:
    """未適用のマイグレーションを順に適用し、適用後のスキーマ版を返す。

    版の確認・各マイグレーション・`user_version` の更新をすべて1つの
    トランザクションに入れる。途中で失敗した場合はスキーマも版も適用前に
    戻るため、次回の起動で同じマイグレーションを再実行できる。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = schema_version(connection)
        if current > len(migrations):
            msg = f"データベースのスキーマ版 {current} はこのバージョンの anp より新しいです"
            raise RuntimeError(msg)

        for index in range(current, len(migrations)):
            logger.info("applying migration %d", index + 1)
            migrations[index](connection)
            # PRAGMA はプレースホルダを使えないため、整数であることを確かめて埋め込む。
            connection.execute(f"PRAGMA user_version = {index + 1:d}")

        applied = schema_version(connection)
    except BaseException:
        connection.execute("ROLLBACK")
        raise

    connection.execute("COMMIT")
    return applied
