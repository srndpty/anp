"""テスト共通の設定。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from hashlib import md5
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QGuiApplication, QPageSize, QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.pdf.cache import RenderCache
from anp.pdf.document import DocumentController
from anp.storage import database
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.pdf_view import PdfView
from helpers import RecordingService

# QApplication が作られる前にオフスクリーンを指定し、ローカルと CI で挙動を揃える。
# PySide6 の import 自体はプラットフォームプラグインを読み込まないため、
# import の後に設定しても間に合う。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def reset_color_scheme() -> Iterator[None]:
    """テストごとに UI テーマの指定を外す。

    UI テーマは `QStyleHints`、つまり `QApplication` 全体の状態を変える。
    戻さないと、Dark にしたテストの影響が後続のテストへ漏れる。

    `QGuiApplication.instance()` を確かめるのは、`QApplication` を作らない
    純粋ロジックのテストでも autouse で走るため。
    """
    yield
    if QGuiApplication.instance() is not None:
        QGuiApplication.styleHints().unsetColorScheme()


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
def two_page_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """2ページの PDF。`sample_pdf` より短い PDF が要るときに使う。"""
    return _write_pdf(tmp_path / "two.pdf", 2)


@pytest.fixture
def broken_pdf(tmp_path: Path) -> Path:
    """PDF として読めないファイル。"""
    path = tmp_path / "broken.pdf"
    path.write_text("これは PDF ではありません", encoding="utf-8")
    return path


@pytest.fixture
def empty_pdf(tmp_path: Path) -> Path:
    """中身が0バイトのファイル。"""
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    return path


@pytest.fixture
def directory_pdf(tmp_path: Path) -> Path:
    """PDF のような名前のディレクトリ。開けないパスの代表として使う。"""
    path = tmp_path / "directory.pdf"
    path.mkdir()
    return path


def _write_pdf_objects(path: Path, objects: list[bytes], trailer_extra: str = "") -> Path:
    """番号付きオブジェクトと xref を並べた PDF を書き出す。

    `QPdfWriter` では作れない PDF（ページが無い、暗号化されている）を
    テストのために組み立てる。
    """
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{trailer_extra} >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()

    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def pageless_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """PDF としては読めるが、ページが1つも無いファイル。"""
    return _write_pdf_objects(
        tmp_path / "pageless.pdf",
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [] /Count 0 >>",
        ],
    )


# ---------------------------------------------------------------- 暗号化 PDF
# パスワードが要る PDF を作るライブラリを増やしたくないので、最小の
# RC4 40bit（/V 1 /R 2）暗号化 PDF をここで組み立てる。PDF 1.4 の
# Algorithm 2〜5 をそのまま実装したもので、pdfium は空パスワードでの
# 認証に失敗し `IncorrectPassword` を返す。
_PASSWORD_PAD = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)


def _write_encrypted_pdf(path: Path, user_password: str, owner_password: str) -> Path:
    """RC4 40bit で暗号化した1ページの PDF を書き出す。"""
    file_id = bytes(range(16))
    permissions = -1

    owner_key = md5((owner_password.encode() + _PASSWORD_PAD)[:32]).digest()[:5]
    owner_value = _rc4(owner_key, (user_password.encode() + _PASSWORD_PAD)[:32])

    digest = md5((user_password.encode() + _PASSWORD_PAD)[:32])
    digest.update(owner_value)
    digest.update(permissions.to_bytes(4, "little", signed=True))
    digest.update(file_id)
    user_value = _rc4(digest.digest()[:5], _PASSWORD_PAD)

    def literal(data: bytes) -> bytes:
        escaped = data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        return b"(" + escaped + b")"

    # 内容のないページなので、暗号化が要るストリームは無い。
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>",
        b"<< /Filter /Standard /V 1 /R 2 /O "
        + literal(owner_value)
        + b" /U "
        + literal(user_value)
        + b" /P "
        + str(permissions).encode()
        + b" >>",
    ]

    return _write_pdf_objects(
        path,
        objects,
        f" /Encrypt {len(objects)} 0 R /ID [<{file_id.hex()}> <{file_id.hex()}>]",
    )


@pytest.fixture
def encrypted_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """開くのにユーザパスワードが要る PDF。"""
    return _write_encrypted_pdf(tmp_path / "encrypted.pdf", "secret", "owner")


# ---------------------------------------------------------------- PdfView
# `PdfView` を使うテストが共有するフィクスチャ。実体（記録用サービスや
# 描画の取り出し）は `helpers.py` にある。
VIEWPORT = (400, 600)


@pytest.fixture
def controller(sample_pdf: Path) -> Iterator[DocumentController]:
    """開いた状態の3ページ PDF（A4 / 595x842pt）。"""
    controller = DocumentController()
    controller.open(sample_pdf)
    yield controller
    controller.close()


@pytest.fixture
def cache() -> RenderCache:
    """ビューが参照するキャッシュ。テストから画像を仕込むために取っておく。"""
    return RenderCache()


@pytest.fixture
def service(cache: RenderCache) -> RecordingService:
    """要求を記録するレンダリングサービス。"""
    return RecordingService(cache)


@pytest.fixture
def view(qtbot: QtBot, service: RecordingService) -> PdfView:
    """ドキュメント未設定のビュー。

    ビューポートの大きさは表示されるまで確定しないので、表示してから返す。
    """
    view = PdfView(service)
    qtbot.addWidget(view)
    view.resize(*VIEWPORT)
    with qtbot.waitExposed(view):
        view.show()
    return view


@pytest.fixture
def loaded_view(view: PdfView, controller: DocumentController) -> PdfView:
    """3ページ PDF を設定済みのビュー。"""
    view.set_document(controller.document, controller.page_sizes())
    return view


# ---------------------------------------------------------------- 学習マーク
@pytest.fixture
def study_mark_connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """一時ディレクトリ上の DB への接続。

    **実行環境の `%LOCALAPPDATA%` には触らない。** 本番のパスを使うのは
    `AppPaths` の組み立てを見るテストだけ。
    """
    with closing(database.connect(tmp_path / "anp.sqlite3")) as connection:
        yield connection


@pytest.fixture
def study_marks(study_mark_connection: sqlite3.Connection) -> StudyMarkRepository:
    """一時 DB を使う学習マークのリポジトリ。"""
    return StudyMarkRepository(study_mark_connection)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """一時ファイル上の INI を使う設定オブジェクト。

    実環境のレジストリを汚さないために INI 形式を使う。
    """
    backend = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return Settings(backend)
