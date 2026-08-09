"""`anp.core.logging` のテスト。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from anp.core.logging import setup_logging


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """テストがルートロガーを汚さないよう元に戻す。"""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)


def test_creates_log_file_and_writes(tmp_path: Path) -> None:
    """ログディレクトリごと作られ、メッセージが書き込まれる。"""
    log_file = tmp_path / "logs" / "anp.log"

    setup_logging(log_file)
    logging.getLogger("anp.test").info("hello from test")
    logging.shutdown()

    assert log_file.is_file()
    assert "hello from test" in log_file.read_text(encoding="utf-8")


def test_repeated_setup_does_not_duplicate_handlers(tmp_path: Path) -> None:
    """複数回呼んでもハンドラが増えない。"""
    log_file = tmp_path / "anp.log"

    setup_logging(log_file)
    first = len(logging.getLogger().handlers)
    setup_logging(log_file)

    assert len(logging.getLogger().handlers) == first


def test_level_is_applied(tmp_path: Path) -> None:
    """指定したレベルより下のメッセージは出力されない。"""
    log_file = tmp_path / "anp.log"

    setup_logging(log_file, level=logging.WARNING)
    logging.getLogger("anp.test").info("見えないはず")
    logging.getLogger("anp.test").warning("見えるはず")
    logging.shutdown()

    content = log_file.read_text(encoding="utf-8")
    assert "見えないはず" not in content
    assert "見えるはず" in content
