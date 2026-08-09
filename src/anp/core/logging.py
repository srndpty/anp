"""ログの初期化。

ファイル（ローテーションあり）と標準エラー出力の両方に出す。ログメッセージ
自体は英語で書く（AGENTS.md の言語規約に従う）。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_BYTES = 1024 * 1024
_BACKUP_COUNT = 3


def setup_logging(log_file: Path, *, level: int = logging.INFO) -> None:
    """ルートロガーにハンドラを設定する。

    複数回呼ばれてもハンドラが重複しないよう、既存のハンドラは取り除く。
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    root.setLevel(level)
