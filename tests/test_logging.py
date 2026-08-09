"""`anp.core.logging` のテスト。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from anp.core.logging import setup_logging, shutdown_logging


@pytest.fixture(autouse=True)
def restore_root_level() -> Iterator[None]:
    """テストがルートロガーの状態を残さないようにする。

    ハンドラは `shutdown_logging()` が anp のものだけを外すので、
    ここではレベルだけ戻せばよい。
    """
    root = logging.getLogger()
    original_level = root.level
    yield
    shutdown_logging()
    root.setLevel(original_level)


def test_creates_log_file_and_writes(tmp_path: Path) -> None:
    """ログディレクトリごと作られ、メッセージが書き込まれる。"""
    log_file = tmp_path / "logs" / "anp.log"

    setup_logging(log_file)
    logging.getLogger("anp.test").info("hello from test")
    shutdown_logging()

    assert log_file.is_file()
    assert "hello from test" in log_file.read_text(encoding="utf-8")


def test_repeated_setup_does_not_duplicate_handlers(tmp_path: Path) -> None:
    """複数回呼んでもハンドラが増えない。"""
    log_file = tmp_path / "anp.log"

    setup_logging(log_file)
    first = len(logging.getLogger().handlers)
    setup_logging(log_file)

    assert len(logging.getLogger().handlers) == first


def test_foreign_handlers_are_left_alone(tmp_path: Path) -> None:
    """anp 以外が付けたハンドラは取り外されず、閉じられもしない。"""
    root = logging.getLogger()
    foreign = logging.StreamHandler()
    root.addHandler(foreign)
    try:
        setup_logging(tmp_path / "anp.log")
        assert foreign in root.handlers

        shutdown_logging()
        assert foreign in root.handlers
        # close() されていれば stream が閉じるため、まだ使えることを確認する。
        foreign.handle(logging.LogRecord("t", logging.INFO, __file__, 1, "生存確認", None, None))
    finally:
        root.removeHandler(foreign)
        foreign.close()


def test_shutdown_removes_only_anp_handlers(tmp_path: Path) -> None:
    """`shutdown_logging()` は anp のハンドラだけを取り外す。"""
    root = logging.getLogger()
    before = list(root.handlers)

    setup_logging(tmp_path / "anp.log")
    shutdown_logging()

    assert root.handlers == before


def test_level_is_applied(tmp_path: Path) -> None:
    """指定したレベルより下のメッセージは出力されない。"""
    log_file = tmp_path / "anp.log"

    setup_logging(log_file, level=logging.WARNING)
    logging.getLogger("anp.test").info("見えないはず")
    logging.getLogger("anp.test").warning("見えるはず")
    shutdown_logging()

    content = log_file.read_text(encoding="utf-8")
    assert "見えないはず" not in content
    assert "見えるはず" in content
