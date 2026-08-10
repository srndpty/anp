"""テスト共通の設定。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QPageSize, QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication

from anp.core.settings import Settings

# QApplication が作られる前にオフスクリーンを指定し、ローカルと CI で挙動を揃える。
# PySide6 の import 自体はプラットフォームプラグインを読み込まないため、
# import の後に設定しても間に合う。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _write_pdf(path: Path, pages: int) -> Path:
    """A4（72dpi なので 595x842pt）のページを並べた PDF を書き出す。"""
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setResolution(72)

    painter = QPainter(writer)
    try:
        for page in range(pages):
            if page > 0:
                writer.newPage()
            painter.drawText(100, 100, f"page {page + 1}")
    finally:
        painter.end()

    return path


@pytest.fixture
def sample_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """3ページの PDF を生成して返す。

    テスト用の PDF をリポジトリに置かずに済むよう、その場で作る。
    """
    return _write_pdf(tmp_path / "sample.pdf", 3)


@pytest.fixture
def single_page_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """1ページだけの PDF。"""
    return _write_pdf(tmp_path / "single.pdf", 1)


@pytest.fixture
def broken_pdf(tmp_path: Path) -> Path:
    """PDF として読めないファイル。"""
    path = tmp_path / "broken.pdf"
    path.write_text("これは PDF ではありません", encoding="utf-8")
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """一時ファイル上の INI を使う設定オブジェクト。

    実環境のレジストリを汚さないために INI 形式を使う。
    """
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return Settings(backend)
