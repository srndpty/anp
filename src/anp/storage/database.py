"""SQLite への接続とスキーマ更新の入口。

Phase 3 で最初の実テーブル `study_marks`（学習マーク）を追加した。
スキーマ版は `PRAGMA user_version` に持つ適用済みマイグレーションの
件数で、`_MIGRATIONS` の並び順がそのまま版番号になる。

**リリース済みのマイグレーションは書き換えない。** 変更が必要になったら
末尾に forward migration を足す（AGENTS.md「フォールバックと互換性は
最小限」を参照）。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

Migration = Callable[[sqlite3.Connection], None]


def _create_study_marks(connection: sqlite3.Connection) -> None:
    """マイグレーション1 `study_marks`: 学習マークの表を作る。

    座標は正規化ページ座標（左上原点の 0.0〜1.0）なので REAL で持つ。
    ズームやウィンドウサイズに依存する値は保存しない。

    Python 側の検証とは別に CHECK 制約も置く。リポジトリを通さない
    将来のマイグレーションやスクリプトから不正な行が入るのを防ぎ、
    不変条件をスキーマ自身に記録するため、二重化は意図的。

    整数の列には `typeof()` の CHECK も付ける。SQLite の型は列の宣言では
    決まらず値ごとに持つため、`INTEGER` と宣言しただけでは
    `page_index = 0.5` のような値をそのまま保存できてしまう。
    """
    connection.execute("""
        CREATE TABLE study_marks (
            id            INTEGER PRIMARY KEY,
            document_key  TEXT    NOT NULL,
            page_index    INTEGER NOT NULL,
            x_norm        REAL    NOT NULL,
            y_norm        REAL    NOT NULL,
            mistake_count INTEGER NOT NULL DEFAULT 1,
            note          TEXT    NULL,

            CHECK (typeof(id) = 'integer' AND id > 0),
            CHECK (document_key <> ''),
            CHECK (typeof(page_index) = 'integer' AND page_index >= 0),
            CHECK (x_norm >= 0.0 AND x_norm <= 1.0),
            CHECK (y_norm >= 0.0 AND y_norm <= 1.0),
            CHECK (typeof(mistake_count) = 'integer' AND mistake_count >= 1)
        )
    """)
    # 現在の唯一の検索は「ある PDF の学習マークを取り出す」。document_key での
    # 絞り込みと ORDER BY page_index, id をこのインデックスだけで賄える
    # （x_norm 等も取り出すので covering index ではない）。
    connection.execute(
        "CREATE INDEX study_marks_by_document ON study_marks (document_key, page_index, id)"
    )


# スキーマ更新の手順。先頭から順に適用し、適用済み件数を user_version に持つ。
# 一度リリースした要素は書き換えず、末尾に追加していく。
_MIGRATIONS: tuple[Migration, ...] = (_create_study_marks,)


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
