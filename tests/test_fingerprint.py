"""`anp.core.fingerprint` のテスト。

Qt もドメインも使わないので `QApplication` なしで走る。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anp.core.fingerprint import file_fingerprint, validate_fingerprint


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    """指紋を取る対象のファイル。"""
    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    return path


def test_the_fingerprint_depends_only_on_the_content(tmp_path: Path, pdf_path: Path) -> None:
    """指紋は内容だけで決まる。パスが違っても、中身が同じなら同じ。"""
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(pdf_path.read_bytes())

    assert file_fingerprint(copy) == file_fingerprint(pdf_path)


def test_the_fingerprint_changes_with_the_content(pdf_path: Path) -> None:
    """内容が変われば指紋も変わる。"""
    before = file_fingerprint(pdf_path)
    pdf_path.write_bytes(b"%PDF-1.4 another book")

    assert file_fingerprint(pdf_path) != before


def test_the_fingerprint_of_a_missing_file_fails(tmp_path: Path) -> None:
    """読めないファイルの指紋は作れない（黙って既定値にしない）。"""
    with pytest.raises(OSError):
        file_fingerprint(tmp_path / "gone.pdf")


@pytest.mark.parametrize("value", ["", "abc", "x" * 64, "A" * 64, "0" * 63, "0" * 65])
def test_a_value_that_is_not_a_sha256_is_rejected(value: str) -> None:
    """長さだけ合った文字列を指紋として受け付けない。

    長さしか見ないと、`"x" * 64` のような値が「壊れたデータ」ではなく
    「たまたま一致しない指紋」として通り、マークが黙って消えたように見える。
    """
    with pytest.raises(ValueError, match="fingerprint"):
        validate_fingerprint(value)
