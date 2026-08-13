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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
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


def _add_document_fingerprint(connection: sqlite3.Connection) -> None:
    """マイグレーション2 `study_marks.document_fingerprint`: 内容の指紋を持たせる。

    マイグレーション1 の識別子はパスだけだった。そのため、同じパスの PDF を
    **別の内容のものへ差し替える**と、古い本の学習マークが新しい本の同じ
    ページ番号のところに、正常なデータとして表示される。位置がそれらしく
    見えるぶん、単に消えるより危ない。

    追加する列は SHA-256 の16進表記（`anp.core.fingerprint`）。
    **既存の行は NULL のまま残す。** 学習の記録は再取得できないので、
    指紋を知らないという理由で消したり、確かめようのない値を埋めたりは
    しない。

    NULL は「内容が分からない ＝ 持ち主を確かめられない」として扱う。
    読み出し側（`StudyMarkRepository`）は普通の一覧には出さず、
    `unverified_count()` で数えて、利用者が承認したときだけ
    `adopt_unverified()` で指紋を書き込む。黙って表示すると、同じパスの
    PDF を差し替えていた場合に前の本のマークが乗ってしまうため。

    インデックスは作り直さない。絞り込みは今までどおり `document_key` で
    効き、指紋の比較は取り出した行に対してかかるだけで、1 PDF あたりの
    行数はもともと数千件に収まる。
    """
    connection.execute(
        "ALTER TABLE study_marks ADD COLUMN document_fingerprint TEXT NULL"
        " CHECK (document_fingerprint IS NULL OR length(document_fingerprint) = 64)"
    )


# スキーマ更新の手順。先頭から順に適用し、適用済み件数を user_version に持つ。
# 一度リリースした要素は書き換えず、末尾に追加していく。
_MIGRATIONS: tuple[Migration, ...] = (_create_study_marks, _add_document_fingerprint)


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


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """明示的なトランザクションで囲む。

    接続は `autocommit=True`（暗黙のトランザクションを作らない）で開くので、
    「読んで、書いて、読み直す」のように1文で終わらない操作は、ここを通して
    まとめる必要がある。

    **`COMMIT` も内側。** これも失敗しうる SQL で、失敗した時点では
    トランザクションが開いたまま残る。接続はアプリの起動から終了まで
    使い回すので、開きっぱなしを残すと以後の更新が意図せずその中に入ったり、
    次の `BEGIN` が失敗したりする。

    巻き戻し自体の失敗で、元の失敗を覆い隠さない（記録だけ残す）。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                logger.exception("failed to roll back the transaction")
        raise


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
    with transaction(connection):
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
    return applied
