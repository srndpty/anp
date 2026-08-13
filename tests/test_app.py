"""`anp.app` のテスト。

`main()` は `QApplication` とロックと DB を作るので、ここで動かすのは
コマンドライン引数の読み取りだけ。純粋な関数なので Qt は要らない。
"""

from __future__ import annotations

from pathlib import Path

from anp.app import initial_document


def test_no_argument_means_no_initial_document() -> None:
    """引数が無ければ None（前回のセッションを復元する経路になる）。"""
    assert initial_document(["anp"]) is None


def test_the_first_argument_is_the_document() -> None:
    """関連付けから渡されるパスを受け取る。"""
    assert initial_document(["anp", r"C:\book.pdf"]) == Path(r"C:\book.pdf")


def test_extra_arguments_are_ignored() -> None:
    """2つ目以降は見ない。複数ファイルを1つのウィンドウでは開かない。"""
    assert initial_document(["anp", "a.pdf", "b.pdf"]) == Path("a.pdf")
