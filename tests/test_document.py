"""`anp.pdf.document` のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtPdf import QPdfDocument

from anp.pdf import document as document_module
from anp.pdf.document import _ERROR_MESSAGES, DocumentController, DocumentError


def test_starts_closed() -> None:
    """開く前は閉じた状態。"""
    controller = DocumentController()

    assert not controller.is_open
    assert controller.path is None
    assert controller.page_count == 0
    assert controller.page_sizes() == []


def test_opens_a_valid_pdf(sample_pdf: Path) -> None:
    """正常な PDF を開ける。"""
    controller = DocumentController()
    try:
        controller.open(sample_pdf)

        assert controller.is_open
        assert controller.path == sample_pdf
        assert controller.page_count == 3
    finally:
        controller.close()


def test_page_sizes_are_reported_in_points(sample_pdf: Path) -> None:
    """全ページの寸法を PDF ポイントで取得できる。"""
    controller = DocumentController()
    try:
        controller.open(sample_pdf)
        sizes = controller.page_sizes()

        assert len(sizes) == 3
        # A4 は 595 x 842 ポイント。
        assert sizes[0].width() == pytest.approx(595.0, abs=1.0)
        assert sizes[0].height() == pytest.approx(842.0, abs=1.0)
    finally:
        controller.close()


def test_close_releases_the_document(sample_pdf: Path) -> None:
    """閉じると状態が戻る。"""
    controller = DocumentController()
    controller.open(sample_pdf)

    controller.close()

    assert not controller.is_open
    assert controller.path is None
    assert controller.page_count == 0


def test_close_without_open_is_harmless() -> None:
    """開いていないときに閉じても何も起きない。"""
    DocumentController().close()


def test_reopening_replaces_the_previous_document(sample_pdf: Path, tmp_path: Path) -> None:
    """別の PDF を開くと前のものは閉じられる。"""
    other = tmp_path / "copy.pdf"
    other.write_bytes(sample_pdf.read_bytes())

    controller = DocumentController()
    try:
        controller.open(sample_pdf)
        controller.open(other)

        assert controller.path == other
        assert controller.page_count == 3
    finally:
        controller.close()


# ------------------------------------------------------------------ 異常系
def test_missing_file_raises_with_japanese_message(tmp_path: Path) -> None:
    """存在しないファイルはクラッシュせず日本語のエラーになる。"""
    controller = DocumentController()

    with pytest.raises(DocumentError) as excinfo:
        controller.open(tmp_path / "no_such_file.pdf")

    assert excinfo.value.message == "ファイルが見つかりません。"
    assert not controller.is_open


def test_broken_file_raises_with_japanese_message(broken_pdf: Path) -> None:
    """壊れたファイルはクラッシュせず日本語のエラーになる。"""
    controller = DocumentController()

    with pytest.raises(DocumentError) as excinfo:
        controller.open(broken_pdf)

    assert excinfo.value.message == "PDF として読み取れないファイルです。"
    assert not controller.is_open


def test_failed_open_closes_the_previous_document(sample_pdf: Path, broken_pdf: Path) -> None:
    """開くのに失敗したら、前に開いていた PDF も閉じられている。

    失敗後に古いページが表示され続けるのを防ぐ。
    """
    controller = DocumentController()
    controller.open(sample_pdf)

    with pytest.raises(DocumentError):
        controller.open(broken_pdf)

    assert not controller.is_open
    assert controller.page_count == 0


def test_empty_file_raises_with_japanese_message(empty_pdf: Path) -> None:
    """0バイトのファイルも PDF として読めないものとして扱う。"""
    controller = DocumentController()

    with pytest.raises(DocumentError) as excinfo:
        controller.open(empty_pdf)

    assert excinfo.value.message == "PDF として読み取れないファイルです。"
    assert not controller.is_open


def test_a_directory_raises_with_japanese_message(directory_pdf: Path) -> None:
    """開けないパス（ディレクトリ）でもクラッシュしない。"""
    controller = DocumentController()

    with pytest.raises(DocumentError) as excinfo:
        controller.open(directory_pdf)

    assert excinfo.value.message == "ファイルが見つかりません。"
    assert not controller.is_open


def test_password_protected_pdf_is_not_opened(encrypted_pdf: Path) -> None:
    """パスワードが要る PDF は Phase 1 では開かず、理由を伝える。"""
    controller = DocumentController()

    with pytest.raises(DocumentError) as excinfo:
        controller.open(encrypted_pdf)

    assert excinfo.value.message == "パスワードで保護されているため開けません。"
    assert not controller.is_open
    assert controller.page_count == 0


def test_a_pdf_without_pages_is_not_opened(pageless_pdf: Path) -> None:
    """読めてもページが無い PDF は開いたことにしない。

    レイアウトはページ0枚を扱えないので、ここで止める。
    """
    controller = DocumentController()

    with pytest.raises(DocumentError) as excinfo:
        controller.open(pageless_pdf)

    assert excinfo.value.message == "ページが1つも含まれていません。"
    assert not controller.is_open


@pytest.mark.parametrize(
    "bad", ["broken_pdf", "empty_pdf", "encrypted_pdf", "pageless_pdf", "missing"]
)
def test_a_valid_pdf_opens_after_a_failure(
    bad: str, sample_pdf: Path, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """開くのに失敗しても、次に正常な PDF を開ける。

    失敗した `QPdfDocument` を使い回すので、エラー状態が残っていると
    以後どの PDF も開けなくなる。
    """
    path = tmp_path / "missing.pdf" if bad == "missing" else request.getfixturevalue(bad)

    controller = DocumentController()
    try:
        with pytest.raises(DocumentError):
            controller.open(path)

        controller.open(sample_pdf)

        assert controller.is_open
        assert controller.page_count == 3
    finally:
        controller.close()


def test_every_error_value_has_a_message() -> None:
    """`QPdfDocument.Error` のすべての値に日本語メッセージがある。

    Qt 側に値が増えたときに気付けるようにする。None_ は成功なので除く。
    """
    expected = {error for error in QPdfDocument.Error if error != QPdfDocument.Error.None_}

    assert set(_ERROR_MESSAGES) == expected


def test_password_protected_message_is_defined() -> None:
    """パスワード保護は Phase 1 では開かず、理由を伝える。"""
    assert _ERROR_MESSAGES[QPdfDocument.Error.IncorrectPassword] == (
        "パスワードで保護されているため開けません。"
    )


def test_an_unexpected_fingerprint_failure_leaves_nothing_open(
    sample_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指紋の計算が想定外の失敗をしても、開きかけの PDF を残さない。

    `load()` は成功しているので、ここで抜けると `QPdfDocument` だけが開いた
    状態になる。`close()` はその状態も確実に閉じられなければならない。
    """

    def buggy(_path: Path | str) -> str:
        raise AttributeError("bug")

    monkeypatch.setattr(document_module, "file_fingerprint", buggy)
    controller = DocumentController()
    try:
        with pytest.raises(AttributeError):
            controller.open(sample_pdf)

        assert not controller.is_open
        assert controller.path is None
        assert controller.content_fingerprint is None
        assert controller.document.status() != QPdfDocument.Status.Ready
    finally:
        controller.close()


def test_a_fingerprint_that_cannot_be_read_is_an_open_failure(
    sample_pdf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """読み込みの直後にファイルを読めなくなった場合は、開けなかったとして扱う。"""

    def unreadable(_path: Path | str) -> str:
        raise OSError(2, "gone")

    monkeypatch.setattr(document_module, "file_fingerprint", unreadable)
    controller = DocumentController()
    try:
        with pytest.raises(DocumentError):
            controller.open(sample_pdf)

        assert not controller.is_open
        assert controller.document.status() != QPdfDocument.Status.Ready
    finally:
        controller.close()
