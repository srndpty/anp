"""ログの初期化。

ファイル（ローテーションあり）と標準エラー出力の両方に出す。ログメッセージ
自体は英語で書く（AGENTS.md の言語規約に従う）。

anp が追加したハンドラには目印を付け、取り外すときはそれだけを対象にする。
ルートロガーのハンドラを無条件に消すと、テストランナーや将来のクラッシュ
レポータなど、他が付けたハンドラまで巻き込むため。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_BYTES = 1024 * 1024
_BACKUP_COUNT = 3

# anp が追加したハンドラに付ける目印。状態はハンドラ自身が持つので、
# モジュール側にグローバルな可変状態を置かずに済む。
_OWNED_FLAG = "_anp_owned"


def _owned_handlers(root: logging.Logger) -> list[logging.Handler]:
    """anp が追加したハンドラだけを返す。"""
    return [handler for handler in root.handlers if getattr(handler, _OWNED_FLAG, False)]


def _adopt(handler: logging.Handler, formatter: logging.Formatter) -> logging.Handler:
    """ハンドラに書式と目印を付ける。"""
    handler.setFormatter(formatter)
    setattr(handler, _OWNED_FLAG, True)
    return handler


def setup_logging(log_file: Path, *, level: int = logging.INFO) -> None:
    """ルートロガーに anp のハンドラを設定する。

    複数回呼ばれてもハンドラが重複しないよう、anp が前に付けたハンドラは
    取り外してから追加する。他が付けたハンドラには触れない。
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    shutdown_logging()

    root = logging.getLogger()
    formatter = logging.Formatter(_FORMAT)

    root.addHandler(
        _adopt(
            RotatingFileHandler(
                log_file,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            ),
            formatter,
        )
    )
    root.addHandler(_adopt(logging.StreamHandler(sys.stderr), formatter))
    root.setLevel(level)


def shutdown_logging() -> None:
    """anp が追加したハンドラを取り外して閉じる。"""
    root = logging.getLogger()
    for handler in _owned_handlers(root):
        root.removeHandler(handler)
        handler.close()
