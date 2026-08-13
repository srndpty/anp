"""PDF ドキュメントのライフサイクル管理。

`QPdfDocument.load()` は例外ではなく `Error` 列挙を返すので、それを利用者に
見せる日本語メッセージへ写像する。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSizeF
from PySide6.QtPdf import QPdfDocument

from anp.core.fingerprint import file_fingerprint

logger = logging.getLogger(__name__)

_ERROR_MESSAGES: dict[QPdfDocument.Error, str] = {
    QPdfDocument.Error.FileNotFound: "ファイルが見つかりません。",
    QPdfDocument.Error.InvalidFileFormat: "PDF として読み取れないファイルです。",
    QPdfDocument.Error.IncorrectPassword: "パスワードで保護されているため開けません。",
    QPdfDocument.Error.UnsupportedSecurityScheme: "対応していない暗号化方式が使われています。",
    QPdfDocument.Error.DataNotYetAvailable: "ファイルをまだ読み込めていません。",
    QPdfDocument.Error.Unknown: "不明な理由で開けませんでした。",
}

_EMPTY_DOCUMENT_MESSAGE = "ページが1つも含まれていません。"

_UNREADABLE_MESSAGE = "読み込んだ直後にファイルを読めなくなりました。"


class DocumentError(Exception):
    """PDF を開けなかった。

    `message` は利用者にそのまま見せられる日本語であること。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DocumentController:
    """開いている PDF を1つ保持する。

    `QPdfDocument` は使い回す。レンダラが参照を持つため、開き直すたびに
    作り替えると参照の張り替えが必要になるうえ、寿命の管理が難しくなる。
    """

    def __init__(self) -> None:
        self._document = QPdfDocument()
        self._path: Path | None = None
        self._content_fingerprint: str | None = None

    # -------------------------------------------------- 状態
    @property
    def document(self) -> QPdfDocument:
        """レンダラに渡すための `QPdfDocument`。"""
        return self._document

    @property
    def path(self) -> Path | None:
        """開いているファイルのパス。開いていなければ None。"""
        return self._path

    @property
    def content_fingerprint(self) -> str | None:
        """開いた PDF の内容の指紋。開いていなければ None。

        **`load()` の直後に取った値**。学習マークの持ち主
        （`DocumentIdentity`）はこれを使う。後から改めてファイルを読み直して
        計算すると、その間に同じパスが別の内容へ置き換わったときに、
        表示している PDF と持ち主として記録する PDF がずれる。
        """
        return self._content_fingerprint

    @property
    def is_open(self) -> bool:
        """PDF が開かれているか。"""
        return self._path is not None

    @property
    def page_count(self) -> int:
        """ページ数。開いていなければ 0。"""
        return self._document.pageCount() if self.is_open else 0

    def page_sizes(self) -> list[QSizeF]:
        """全ページの寸法（PDF ポイント）。"""
        return [self._document.pagePointSize(i) for i in range(self.page_count)]

    # -------------------------------------------------- 開閉
    def open(self, path: Path) -> None:
        """PDF を開く。失敗した場合は `DocumentError` を送出する。

        内容の指紋も **ここで取る**（`content_fingerprint`）。後から改めて
        読み直すと、その間に同じパスが別の内容へ置き換わったときに、
        表示している PDF と持ち主として記録する PDF がずれる。
        """
        self.close()

        error = self._document.load(str(path))
        if error != QPdfDocument.Error.None_:
            logger.warning("failed to load %s: %s", path, error.name)
            unknown = _ERROR_MESSAGES[QPdfDocument.Error.Unknown]
            raise DocumentError(_ERROR_MESSAGES.get(error, unknown))

        if self._document.pageCount() <= 0:
            logger.warning("document has no pages: %s", path)
            self._document.close()
            raise DocumentError(_EMPTY_DOCUMENT_MESSAGE)

        try:
            fingerprint = file_fingerprint(path)
        except OSError:
            # 読み込みは通ったのに指紋を取れない（読み込みの最中に消された
            # など）。同一性の分からない PDF を開いた状態にはしない。
            logger.warning("failed to fingerprint %s", path, exc_info=True)
            self.close()
            raise DocumentError(_UNREADABLE_MESSAGE) from None
        except BaseException:
            # 想定していない失敗。**包まないが、開きかけは残さない。**
            # ここで抜けると `QPdfDocument` だけ開いた状態になる。
            self.close()
            raise

        self._path = path
        self._content_fingerprint = fingerprint
        logger.info("opened document with %d pages", self._document.pageCount())

    def close(self) -> None:
        """開いている PDF を閉じる。開いていなければ何もしない。

        **`QPdfDocument` は無条件に閉じる。** `_path` を「開いているか」の
        代わりに使って早期に返すと、`open()` の途中（`load()` は成功したが
        `_path` を入れる前）で抜けたときに、閉じられない `QPdfDocument` が
        残ってしまう。
        """
        was_open = self._path is not None
        self._document.close()
        self._path = None
        self._content_fingerprint = None
        if was_open:
            logger.info("closed document")
