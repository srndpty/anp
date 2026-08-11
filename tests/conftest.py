"""テスト共通の設定。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
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


# ---------------------------------------------------------------- 目次つき PDF
# `QPdfWriter` は outline / bookmarks を書けないので、`_write_pdf_objects()`
# を使って最小の outline ツリーを組み立てる（PDF を1つリポジトリへ置くより、
# 期待値がテストのすぐ隣にある方を選んだ）。PDF ライブラリは増やさない。
#
# 位置は PDF の座標（ページ左下が原点）で書く。`QPdfBookmarkModel` の
# Location ロールは、これをページ左上原点へ直した値を返す。
@dataclass(frozen=True)
class OutlineItem:
    """テスト用 PDF に書き込む outline の1項目。"""

    title: str
    page: int
    y: float | None = None
    """PDF 座標（下からの高さ）での移動先。None なら位置を持たない移動先。"""

    zoom: float | None = None
    children: tuple[OutlineItem, ...] = ()


@dataclass(frozen=True)
class _OutlineEntry:
    """番号を割り当てた outline 項目（PDF オブジェクトの参照を張るため）。"""

    number: int
    item: OutlineItem
    parent: int
    previous: int | None
    next: int | None
    first_child: int | None
    last_child: int | None


def _assign_numbers(
    items: Sequence[OutlineItem], parent: int, next_number: int, out: list[_OutlineEntry]
) -> int:
    """兄弟を連番で並べ、その後ろへ子孫を続ける。次の空き番号を返す。

    兄弟が連続するので、親の /First と /Last は子を書き出す前に決まる。
    """
    numbers = list(range(next_number, next_number + len(items)))
    cursor = next_number + len(items)
    for position, number in enumerate(numbers):
        item = items[position]
        first_child = last_child = None
        if item.children:
            first_child = cursor
            last_child = cursor + len(item.children) - 1
            cursor = _assign_numbers(item.children, number, cursor, out)
        out.append(
            _OutlineEntry(
                number=number,
                item=item,
                parent=parent,
                previous=numbers[position - 1] if position > 0 else None,
                next=numbers[position + 1] if position + 1 < len(numbers) else None,
                first_child=first_child,
                last_child=last_child,
            )
        )
    return cursor


def _outline_body(entry: _OutlineEntry, first_page_number: int) -> bytes:
    """outline 項目1件の PDF オブジェクト本体。"""
    item = entry.item
    page_reference = f"{first_page_number + item.page} 0 R"
    if item.y is None:
        destination = f"[{page_reference} /Fit]"
    else:
        destination = f"[{page_reference} /XYZ 0 {item.y} {item.zoom or 0}]"

    title = item.title.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    parts = [f"/Title ({title})", f"/Parent {entry.parent} 0 R", f"/Dest {destination}"]
    if entry.previous is not None:
        parts.append(f"/Prev {entry.previous} 0 R")
    if entry.next is not None:
        parts.append(f"/Next {entry.next} 0 R")
    if entry.first_child is not None and entry.last_child is not None:
        parts.append(f"/First {entry.first_child} 0 R")
        parts.append(f"/Last {entry.last_child} 0 R")
        parts.append(f"/Count {len(item.children)}")
    return ("<< " + " ".join(parts) + " >>").encode()


def write_outline_pdf(path: Path, pages: int, items: Sequence[OutlineItem]) -> Path:
    """outline を持つ A4（595x842pt）の PDF を書き出す。"""
    first_page_number = 3
    outline_root = first_page_number + pages
    kids = " ".join(f"{first_page_number + index} 0 R" for index in range(pages))

    entries: list[_OutlineEntry] = []
    _assign_numbers(items, outline_root, outline_root + 1, entries)
    entries.sort(key=lambda entry: entry.number)

    objects = [
        f"<< /Type /Catalog /Pages 2 0 R /Outlines {outline_root} 0 R >>".encode(),
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
        *[b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>"] * pages,
        (
            f"<< /Type /Outlines /First {outline_root + 1} 0 R "
            f"/Last {outline_root + len(items)} 0 R /Count {len(items)} >>"
        ).encode(),
        *[_outline_body(entry, first_page_number) for entry in entries],
    ]
    return _write_pdf_objects(path, objects)


# 章と節を持つ目次。階層が保たれていることと、ページ番号が 0 始まりで
# 読めていることを見るための最小の形。
OUTLINE_ITEMS = (
    OutlineItem(
        "Chapter 1",
        0,
        y=842.0,
        children=(
            OutlineItem("Section 1.1", 1, y=600.0),
            OutlineItem("Section 1.2", 2, y=200.0, zoom=2.5),
        ),
    ),
    OutlineItem("Chapter 2", 3, y=842.0),
)

# `Section 1.1` の移動先を、ページ左上を原点とする PDF ポイントで表した値。
SECTION_1_1_TOP_ORIGIN_Y = 842.0 - 600.0


@pytest.fixture
def outline_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """入れ子の目次を持つ4ページの PDF。"""
    return write_outline_pdf(tmp_path / "outline.pdf", 4, OUTLINE_ITEMS)


@pytest.fixture
def other_outline_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """別の目次を持つ2ページの PDF。目次の入れ替わりを見るために使う。"""
    return write_outline_pdf(
        tmp_path / "other_outline.pdf", 2, (OutlineItem("Section X", 1, y=400.0),)
    )


# ------------------------------------------------------------ テキスト層つき PDF
# `QPdfWriter` が書く PDF はフォントを埋め込むだけで ToUnicode を持たないため、
# pdfium がテキストを取り出せない（`getAllText()` が空になり、検索は必ず 0 件）。
# 検索のテストには **実際に検索できる PDF** が要るので、`_write_pdf_objects()`
# を使って base-14 フォント（Helvetica）の content stream を組み立てる。
# PDF ライブラリは増やさない。
#
# 座標は PDF の座標（ページ左下が原点）。`QPdfLink.rectangles()` は、これを
# ページ左上原点へ直した値を返す。
_TEXT_FIRST_LINE_Y = 750.0
_TEXT_LINE_GAP = 50.0
_TEXT_LEFT_X = 72.0
_TEXT_FONT_SIZE = 14


def write_text_pdf(path: Path, pages: Sequence[Sequence[str]]) -> Path:
    """1ページに数行ずつテキストを置いた A4（595x842pt）の PDF を書き出す。"""
    page_count = len(pages)
    # オブジェクト番号は 1: カタログ、2: ページツリー、以降ページと content が
    # 交互、最後にフォント。
    font_number = 3 + page_count * 2
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(page_count))

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode(),
    ]
    for index, lines in enumerate(pages):
        content_number = 3 + index * 2 + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {font_number} 0 R >> >> "
                f"/Contents {content_number} 0 R >>"
            ).encode()
        )
        body = f"BT /F1 {_TEXT_FONT_SIZE} Tf\n"
        for line, text in enumerate(lines):
            y = _TEXT_FIRST_LINE_Y - _TEXT_LINE_GAP * line
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            body += f"1 0 0 1 {_TEXT_LEFT_X} {y} Tm ({escaped}) Tj\n"
        stream = (body + "ET").encode()
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    return _write_pdf_objects(path, objects)


# 検索のテストが期待値の根拠にする文面。
#
# - "target" は **同じページに複数件**（0ページ目・1ページ目）で合計4件
# - "gamma target" は0ページ目の行をまたぐので、1件の結果が矩形を2つ持つ
# - 2ページ目は一致しないので、`resultsOnPage()` が空になるページがある
# - 1ページ目の4件目は **ページのかなり下** にある。ページの先頭まで移動
#   するだけの実装では見えない位置を作るため
_FILLER_LINES = ("filler",) * 11

SEARCHABLE_PDF_PAGES = (
    ("alpha beta target gamma", "target again"),
    ("something target something", *_FILLER_LINES, "target near the bottom"),
    ("no match here",),
)

SEARCH_QUERY = "target"
SEARCH_QUERY_HITS = 4
"""`SEARCH_QUERY` の一致件数。0ページ目に2件、1ページ目に2件。"""

SPACED_SEARCH_QUERY = " target"
SPACED_SEARCH_QUERY_HITS = 2
"""前後の空白を落とさないことを見る検索語。行頭の "target" には一致しない。"""

WRAPPING_SEARCH_QUERY = "gamma target"
"""0ページ目の行をまたぐ検索語。1件の結果が複数の矩形を持つ。"""


@pytest.fixture
def searchable_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """テキスト層を実際に持つ3ページの PDF。"""
    return write_text_pdf(tmp_path / "searchable.pdf", SEARCHABLE_PDF_PAGES)


@pytest.fixture
def other_searchable_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """別の文面を持つ2ページの PDF。検索状態が PDF を跨がないことを見る。

    `SEARCH_QUERY` は **1件も含まない**。A の結果が B へ残っていれば、
    件数がそのまま食い違う。
    """
    return write_text_pdf(
        tmp_path / "other_searchable.pdf", (("completely different words",), ("nothing here",))
    )


@pytest.fixture
def image_only_pdf(qapp: QApplication, tmp_path: Path) -> Path:
    """図形だけでテキストを持たない PDF（スキャンした本の代わり）。

    OCR はしないので、何を検索しても 0 件になる境界を固定する。
    """
    stream = b"0 0 1 rg 100 100 300 400 re f"
    return _write_pdf_objects(
        tmp_path / "image_only.pdf",
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        ],
    )


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
