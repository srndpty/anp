"""PDF 内テキスト検索（P5-3）のテスト。

確かめるのは6つ。

- `QPdfSearchModel` が **実際にテキスト層を持つ PDF** から一致を見つけ、
  ページ・位置・矩形が読めること（mock だけで済ませない）
- 検索文字列・件数・現在の結果の契約（空・変更・前後・巡回・件数の増減）
- 表示中の PDF の検索だけが生きていること（A → B、開くのに失敗した場合、
  学習マークを読めなかった場合、自動復元）
- ハイライトが `QPdfLink.rectangles()` の全部を、可視ページの分だけ描くこと
- 結果への移動が、倍率・レンダリング・キャッシュ・学習マーク・目次・
  履歴のどれにも触らないこと
- Ctrl+F / Enter / Shift+Enter / F3 の経路が実際につながっていること

検索できる PDF はその場で組み立てる（`conftest.write_text_pdf()`）。
`QPdfWriter` が書く PDF は ToUnicode を持たず pdfium が文字を取り出せない
ので、検索のテストには使えない。

検索は **非同期に進む**（`setSearchString()` の直後に件数は揃っていない）。
待つときは固定時間ではなく件数が条件を満たすまでにする。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtPdf import QPdfDocument, QPdfLink, QPdfSearchModel
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.pdf.cache import RenderCache
from anp.pdf.color import PageColorMode
from anp.pdf.document import DocumentController
from anp.pdf.render import PageRenderService
from anp.storage.study_mark import StudyMark
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.main_window import MainWindow
from anp.ui.pdf_search_controller import NO_RESULT, PdfSearchController, SearchState
from anp.ui.pdf_view import MIN_ZOOM, PdfView, ZoomMode
from anp.ui.search_dock import DOCK_OBJECT_NAME, SearchDock, result_label
from conftest import (
    SEARCH_QUERY,
    SEARCH_QUERY_HITS,
    SPACED_SEARCH_QUERY,
    SPACED_SEARCH_QUERY_HITS,
    WRAPPING_SEARCH_QUERY,
    write_text_pdf,
)
from helpers import (
    BrokenRepository,
    FakeSearchResult,
    OverlaySpy,
    RecordingService,
    put_image,
    render_view,
)

_SEARCH_TIMEOUT_MS = 5000

# 検索中のドキュメント切り替えを繰り返す回数。寿命の誤りは1回で必ず出る
# とは限らないので、同じプロセスで何度も踏む。
_MID_SEARCH_SWITCHES = 40

# 両方の PDF で必ず一致する検索語。どのページにも一致があるほど走査が
# 長く続き、「まだ走っている最中」に切り替えやすい。
_COMMON_QUERY = "e"


def wait_for_count(qtbot: QtBot, controller: PdfSearchController, expected: int) -> None:
    """検索結果の件数が期待値になるまで待つ。"""
    qtbot.waitUntil(lambda: controller.count == expected, timeout=_SEARCH_TIMEOUT_MS)


def as_link(fake: FakeSearchResult) -> QPdfLink:
    """`PdfView` が読む問い合わせだけを持つ、検索結果の代わりの値。"""
    return cast(QPdfLink, fake)


@pytest.fixture
def searchable_document(searchable_pdf: Path) -> Iterator[QPdfDocument]:
    """テキスト層を持つ PDF を開いたドキュメント。"""
    document = QPdfDocument()
    assert document.load(str(searchable_pdf)) == QPdfDocument.Error.None_
    yield document
    document.close()


@pytest.fixture
def search_model(searchable_document: QPdfDocument) -> Iterator[QPdfSearchModel]:
    """コントローラを挟まない素のモデル。Qt が何を返すかを見るのに使う。

    後始末で必ずドキュメントを外す。`QPdfSearchModel` は検索をタイマーで
    少しずつ進めるので、閉じたドキュメントを指したまま残すと、次に
    イベントループが回ったときに落ちる（本番で `detach_document()` を
    必ず通すのと同じ理由）。
    """
    model = QPdfSearchModel()
    model.setDocument(searchable_document)
    yield model
    model.setDocument(None)  # type: ignore[arg-type]


@pytest.fixture
def search(searchable_document: QPdfDocument) -> Iterator[PdfSearchController]:
    """テキスト層を持つ PDF を対象にしたコントローラ。"""
    controller = PdfSearchController()
    controller.attach_document(searchable_document)
    yield controller
    controller.detach_document()


@pytest.fixture
def search_documents(searchable_pdf: Path) -> Iterator[DocumentController]:
    """ビューに載せる、検索できる PDF。"""
    documents = DocumentController()
    documents.open(searchable_pdf)
    yield documents
    documents.close()


@pytest.fixture
def view_search(search_documents: DocumentController) -> Iterator[PdfSearchController]:
    """ビューに載せた PDF を対象にしたコントローラ。"""
    controller = PdfSearchController()
    controller.attach_document(search_documents.document)
    yield controller
    controller.detach_document()


@pytest.fixture
def search_view(
    view: PdfView, search_documents: DocumentController, view_search: PdfSearchController
) -> Iterator[PdfView]:
    """検索できる PDF を表示し、検索モデルを載せたビュー。"""
    view.set_document(search_documents.document, search_documents.page_sizes())
    view.set_search_model(view_search.model)
    yield view
    view.set_search_model(None)


def expected_rects(view: PdfView, link: QPdfLink) -> list[QRectF]:
    """検索結果の矩形に対応するビューポート上の矩形。

    ビューの変換とは別に組み立てる（`PageLayout` の変換をそのまま呼んで
    比べると、変換が間違っていても一致してしまう）。
    """
    page_rect = view.page_viewport_rect(link.page())
    assert page_rect is not None
    zoom = view.zoom
    return [
        QRectF(
            page_rect.left() + rect.x() * zoom,
            page_rect.top() + rect.y() * zoom,
            rect.width() * zoom,
            rect.height() * zoom,
        )
        for rect in link.rectangles()
    ]


def assert_rects_close(actual: list[QRectF], expected: list[QRectF]) -> None:
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        assert got.left() == pytest.approx(want.left(), abs=0.01)
        assert got.top() == pytest.approx(want.top(), abs=0.01)
        assert got.width() == pytest.approx(want.width(), abs=0.01)
        assert got.height() == pytest.approx(want.height(), abs=0.01)


def viewport_rect(view: PdfView) -> QRectF:
    """ビューポートの矩形（ビューポート座標）。"""
    size = view.viewport().size()
    return QRectF(0.0, 0.0, float(size.width()), float(size.height()))


# ================================================================ 実モデル
def test_the_search_model_finds_text_in_a_real_pdf(
    qtbot: QtBot, search_model: QPdfSearchModel
) -> None:
    """実際のテキスト層から一致が見つかり、ページ・位置・矩形が読める。

    `QPdfSearchModel` が実際に何を返すかを固定するテスト。ページ番号は
    **0 始まり**（1 始まりと取り違えると1ページずれる）。
    """
    search_model.setSearchString(SEARCH_QUERY)
    qtbot.waitUntil(lambda: search_model.count() == SEARCH_QUERY_HITS, timeout=_SEARCH_TIMEOUT_MS)

    links = [search_model.resultAtIndex(index) for index in range(search_model.count())]

    assert all(link.isValid() for link in links)
    # 結果はページ順に並ぶ（0ページ目に2件、1ページ目に2件）。
    assert [link.page() for link in links] == [0, 0, 1, 1]
    for link in links:
        assert link.rectangles(), "検索結果が矩形を持たない"
        # location は矩形の左上と同じ点（ページ左上を原点とする PDF ポイント）。
        assert link.location() == link.rectangles()[0].topLeft()


def test_several_hits_on_one_page_come_from_results_on_page(
    qtbot: QtBot, search_model: QPdfSearchModel
) -> None:
    """同じページの複数の一致は `resultsOnPage()` でまとめて引ける。"""
    search_model.setSearchString(SEARCH_QUERY)
    qtbot.waitUntil(lambda: search_model.count() == SEARCH_QUERY_HITS, timeout=_SEARCH_TIMEOUT_MS)

    assert len(search_model.resultsOnPage(0)) == 2
    assert len(search_model.resultsOnPage(1)) == 2
    assert search_model.resultsOnPage(2) == []


def test_a_result_that_wraps_a_line_has_several_rectangles(
    qtbot: QtBot, search_model: QPdfSearchModel
) -> None:
    """行をまたぐ一致は1件で複数の矩形を持つ。

    ハイライトが先頭の矩形だけを描いていないかを見るための、実 PDF 由来の根拠。
    """
    search_model.setSearchString(WRAPPING_SEARCH_QUERY)
    qtbot.waitUntil(lambda: search_model.count() == 1, timeout=_SEARCH_TIMEOUT_MS)

    rectangles = search_model.resultAtIndex(0).rectangles()

    assert len(rectangles) > 1
    # 別の行なので縦にずれている。
    assert rectangles[0].top() < rectangles[1].top()


def test_an_image_only_pdf_has_no_text_to_search(image_only_pdf: Path) -> None:
    """図形だけの PDF はテキスト層を持たない。OCR へは落ちない。"""
    document = QPdfDocument()
    assert document.load(str(image_only_pdf)) == QPdfDocument.Error.None_
    controller = PdfSearchController()
    controller.attach_document(document)

    controller.set_query(SEARCH_QUERY)

    assert document.getAllText(0).text() == ""
    assert controller.count == 0
    assert controller.current_index == NO_RESULT
    assert controller.current_result() is None
    controller.detach_document()
    document.close()


def test_writing_a_text_pdf_produces_real_text(tmp_path: Path, qapp: QApplication) -> None:
    """フィクスチャが実際にテキスト層を持つ PDF を書けている。

    ここが壊れると検索のテストがまるごと無意味になるので、根拠を1本置く。
    """
    path = write_text_pdf(tmp_path / "probe.pdf", (("hello world",),))
    document = QPdfDocument()
    assert document.load(str(path)) == QPdfDocument.Error.None_

    assert "hello world" in document.getAllText(0).text()
    document.close()


# ================================================================ コントローラ
def test_no_query_means_no_results(search: PdfSearchController) -> None:
    """検索文字列が空なら 0 件・現在の結果なし。"""
    assert search.query == ""
    assert search.count == 0
    assert search.current_index == NO_RESULT
    assert search.current_result() is None
    assert search.has_document


def test_a_query_selects_the_first_result(qtbot: QtBot, search: PdfSearchController) -> None:
    """一致が見つかったら先頭が現在の結果になり、そこへの移動が要求される。

    結果は少しずつ増える（最初の通知の時点で全ページ分は揃っていない）。
    それでも現在の結果は最初の1件に決まる。
    """
    with qtbot.waitSignal(search.result_activated, timeout=_SEARCH_TIMEOUT_MS) as activated:
        search.set_query(SEARCH_QUERY)

    assert search.current_index == 0
    link = search.current_result()
    assert link is not None
    assert link.page() == 0
    assert isinstance(activated.args[0], QPdfLink)

    # 残りの結果が届いても現在の結果は動かない。
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)
    assert search.current_index == 0


def test_the_query_is_not_stripped(qtbot: QtBot, search: PdfSearchController) -> None:
    """前後の空白も検索語の一部として Qt へそのまま渡す。"""
    search.set_query(SPACED_SEARCH_QUERY)

    assert search.query == SPACED_SEARCH_QUERY
    assert search.model.searchString() == SPACED_SEARCH_QUERY
    # 行頭の "target" には空白が無いので一致しない。`strip()` する実装なら
    # 空白なしと同じ件数になる。
    wait_for_count(qtbot, search, SPACED_SEARCH_QUERY_HITS)


def test_clearing_the_query_drops_the_results(qtbot: QtBot, search: PdfSearchController) -> None:
    """検索文字列を空に戻すと 0 件・現在の結果なしへ戻る。"""
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)

    search.set_query("")

    assert search.count == 0
    assert search.current_index == NO_RESULT
    assert search.current_result() is None


def test_a_new_query_discards_the_old_current_result(
    qtbot: QtBot, search: PdfSearchController
) -> None:
    """検索文字列が変われば、前の検索語の現在位置は持ち越さない。"""
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)
    search.next_result()
    search.next_result()
    assert search.current_index == 2

    search.set_query(WRAPPING_SEARCH_QUERY)

    # 件数が揃う前でも、古い番号を指したままにはならない。
    assert search.current_index == NO_RESULT
    wait_for_count(qtbot, search, 1)
    assert search.current_index == 0


def test_a_shorter_result_list_never_leaves_the_index_out_of_range(
    qtbot: QtBot, search: PdfSearchController
) -> None:
    """末尾を見ている状態で件数の少ない検索語へ変えても範囲外にならない。"""
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)
    search.previous_result()
    assert search.current_index == SEARCH_QUERY_HITS - 1

    search.set_query(WRAPPING_SEARCH_QUERY)
    wait_for_count(qtbot, search, 1)

    assert search.current_index == 0
    assert search.current_result() is not None


def test_a_shrinking_result_count_moves_the_current_result_into_range(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, search: PdfSearchController
) -> None:
    """検索中に件数が減っても、範囲外の番号を指したままにしない。

    `QPdfSearchModel` は結果を少しずつ足していくので、通常この向きの変化は
    起きない。それでも範囲外の番号でモデルを引くことがないよう、件数を
    減らして通知したときの振る舞いを固定する。
    """
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)
    search.previous_result()
    assert search.current_index == SEARCH_QUERY_HITS - 1

    monkeypatch.setattr(search.model, "rowCount", lambda _parent: 2)
    search.model.countChanged.emit()

    assert search.current_index == 1
    assert search.current_result() is not None


def test_next_wraps_around(qtbot: QtBot, search: PdfSearchController) -> None:
    """次の結果は末尾から先頭へ回る。"""
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)

    indexes = []
    for _ in range(SEARCH_QUERY_HITS):
        search.next_result()
        indexes.append(search.current_index)

    assert indexes == [*range(1, SEARCH_QUERY_HITS), 0]


def test_previous_wraps_around(qtbot: QtBot, search: PdfSearchController) -> None:
    """前の結果は先頭から末尾へ回る。"""
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)

    indexes = []
    for _ in range(SEARCH_QUERY_HITS):
        search.previous_result()
        indexes.append(search.current_index)

    assert indexes == list(reversed(range(SEARCH_QUERY_HITS)))


def test_moving_without_results_does_nothing(search: PdfSearchController) -> None:
    """結果が無いときの前/次は何も起こさない。"""
    search.next_result()
    search.previous_result()

    assert search.current_index == NO_RESULT


def test_detaching_the_document_clears_the_search(
    qtbot: QtBot, search: PdfSearchController
) -> None:
    """ドキュメントを手放すと検索語も結果も消え、モデルが PDF を指さない。"""
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)

    search.detach_document()

    assert search.query == ""
    assert search.count == 0
    assert search.current_index == NO_RESULT
    assert not search.has_document
    assert search.model.document() is None


def test_attaching_another_document_clears_the_search(
    qtbot: QtBot, search: PdfSearchController, other_searchable_pdf: Path
) -> None:
    """別の PDF を載せると A の検索語も結果も残らない。"""
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)

    other = QPdfDocument()
    assert other.load(str(other_searchable_pdf)) == QPdfDocument.Error.None_
    search.attach_document(other)

    assert search.query == ""
    assert search.count == 0
    assert search.current_index == NO_RESULT
    assert search.model.document() is other
    search.detach_document()
    other.close()


def test_attaching_a_document_replaces_the_search_model(
    qtbot: QtBot, search: PdfSearchController, other_searchable_pdf: Path
) -> None:
    """PDF を載せるたびに検索モデルを作り直し、新しいモデルを知らせる。

    `DocumentController` は `QPdfDocument` を使い回すので、同じ
    `QPdfSearchModel` を開き直しを跨いで生かしておくと、Windows で
    access violation としてプロセスごと落ちる（**検索語を一度も入れて
    いなくても落ちる**）。
    """
    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)
    before = search.model
    announced: list[QPdfSearchModel] = []
    search.model_changed.connect(announced.append)

    other = QPdfDocument()
    assert other.load(str(other_searchable_pdf)) == QPdfDocument.Error.None_
    search.attach_document(other)

    assert search.model is not before
    assert announced == [search.model]
    # 古いモデルはドキュメントを指していない（消えるまでの間も安全）。
    assert before.document() is None
    assert before.searchString() == ""
    search.detach_document()
    other.close()


def test_the_state_snapshot_follows_the_controller(
    qtbot: QtBot, search: PdfSearchController
) -> None:
    """`SearchState` はそのときのコントローラの値をそのまま持つ。"""
    states: list[SearchState] = []
    search.state_changed.connect(states.append)

    search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, search, SEARCH_QUERY_HITS)

    assert states[-1] == SearchState(
        query=SEARCH_QUERY, count=SEARCH_QUERY_HITS, current_index=0, has_document=True
    )
    assert states[-1].has_results


# ================================================================ 検索ドック
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (SearchState("", 0, NO_RESULT, has_document=True), "0 / 0"),
        (SearchState("x", 0, NO_RESULT, has_document=True), "0 / 0"),
        (SearchState("x", 17, 0, has_document=True), "1 / 17"),
        (SearchState("x", 17, 4, has_document=True), "5 / 17"),
    ],
)
def test_the_result_label_is_one_based(state: SearchState, expected: str) -> None:
    """件数の表示は 1 始まり。結果が無ければ `0 / 0`。"""
    assert result_label(state) == expected


@pytest.fixture
def dock(qtbot: QtBot) -> SearchDock:
    """単体の検索ドック。"""
    dock = SearchDock()
    qtbot.addWidget(dock)
    dock.show()
    return dock


def test_the_dock_starts_empty_and_disabled(dock: SearchDock) -> None:
    """PDF が無ければ入力も前後も操作できない。"""
    assert dock.objectName() == DOCK_OBJECT_NAME
    assert not dock.query_edit.isEnabled()
    assert not dock.next_button.isEnabled()
    assert not dock.previous_button.isEnabled()
    assert dock.count_text == "0 / 0"


def test_the_dock_enables_navigation_only_with_results(dock: SearchDock) -> None:
    """前後のボタンは結果が1件以上のときだけ押せる。"""
    dock.set_state(SearchState("x", 0, NO_RESULT, has_document=True))
    assert dock.query_edit.isEnabled()
    assert not dock.next_button.isEnabled()
    assert not dock.previous_button.isEnabled()

    dock.set_state(SearchState("x", 3, 1, has_document=True))
    assert dock.next_button.isEnabled()
    assert dock.previous_button.isEnabled()
    assert dock.count_text == "2 / 3"


def test_zero_hits_are_never_reported_as_a_definitive_no_match(dock: SearchDock) -> None:
    """0 件を「一致しません」と断定しない。

    検索は非同期で、`QPdfSearchModel` には走査の完了を知らせるシグナルが
    無い。「まだ最初の一致が届いていない」と「本当に 0 件」を区別できない
    ので、件数だけを見せる（ダイアログも出さない）。
    """
    dock.set_state(SearchState("x", 0, NO_RESULT, has_document=True))

    assert dock.count_text == "0 / 0"
    labels = [child.text() for child in dock.findChildren(QLabel)]
    assert all("一致" not in label for label in labels)


def test_typing_in_the_field_reports_the_query(qtbot: QtBot, dock: SearchDock) -> None:
    """入力欄の文字列がそのまま流れる（`strip()` しない）。"""
    dock.set_state(SearchState("", 0, NO_RESULT, has_document=True))
    queries: list[str] = []
    dock.query_changed.connect(queries.append)

    qtbot.keyClicks(dock.query_edit, " ab")

    assert queries == [" ", " a", " ab"]


def test_enter_and_shift_enter_move_between_results(qtbot: QtBot, dock: SearchDock) -> None:
    """入力欄の Enter は次へ、Shift+Enter は前へ。"""
    dock.set_state(SearchState("x", 3, 0, has_document=True))
    moves: list[str] = []
    dock.next_requested.connect(lambda: moves.append("next"))
    dock.previous_requested.connect(lambda: moves.append("previous"))

    qtbot.keyClick(dock.query_edit, Qt.Key.Key_Return)
    qtbot.keyClick(dock.query_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)

    assert moves == ["next", "previous"]


def test_focusing_the_field_selects_the_existing_query(dock: SearchDock) -> None:
    """Ctrl+F で入るとき、既存の検索語は選択状態になる。"""
    dock.set_state(SearchState("", 0, NO_RESULT, has_document=True))
    dock.query_edit.setText("target")

    dock.focus_query()

    assert dock.query_edit.selectedText() == "target"


def test_clearing_the_query_empties_the_field(dock: SearchDock) -> None:
    """ドキュメントの切り替えで入力欄を空にできる。"""
    dock.set_state(SearchState("", 0, NO_RESULT, has_document=True))
    dock.query_edit.setText("target")
    queries: list[str] = []
    dock.query_changed.connect(queries.append)

    dock.clear_query()

    assert dock.query_edit.text() == ""
    assert queries == [""]


# ================================================================ ハイライト
def test_all_matches_on_a_visible_page_are_highlighted(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """可視ページ上の一致はすべて薄く塗られる。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    search_view.set_current_search_result(view_search.current_index)

    spy = OverlaySpy(monkeypatch)
    render_view(search_view)

    # 0ページ目だけが見えている状態なので、そのページの2件が対象。
    assert search_view.visible_pages() == range(0, 1)
    assert len(spy.all_matches) == 2
    assert all(draw.rects for draw in spy.all_matches)


def test_the_current_match_is_drawn_by_a_separate_path(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """現在の1件だけが強調の描画を受ける。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    search_view.set_current_search_result(0)

    spy = OverlaySpy(monkeypatch)
    render_view(search_view)

    assert len(spy.current_matches) == 1
    current = view_search.current_result()
    assert current is not None
    assert_rects_close(list(spy.current_matches[0].rects), expected_rects(search_view, current))


def test_no_current_match_means_no_strong_highlight(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """現在の結果が無ければ強調は描かれない（薄い塗りは残る）。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    search_view.set_current_search_result(NO_RESULT)

    spy = OverlaySpy(monkeypatch)
    render_view(search_view)

    assert spy.current_matches == []
    assert spy.all_matches


def test_a_current_match_on_another_page_is_not_drawn(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """見えていないページの現在の結果は描かない。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    # 3件目は1ページ目にある。ビューは0ページ目を出したまま。
    search_view.set_current_search_result(2)

    spy = OverlaySpy(monkeypatch)
    render_view(search_view)

    assert search_view.visible_pages() == range(0, 1)
    assert spy.current_matches == []


def test_a_multi_rectangle_result_draws_every_rectangle(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """行をまたぐ一致は、矩形を全部塗る。先頭だけで済ませない。"""
    view_search.set_query(WRAPPING_SEARCH_QUERY)
    wait_for_count(qtbot, view_search, 1)
    search_view.set_current_search_result(0)

    spy = OverlaySpy(monkeypatch)
    render_view(search_view)

    assert len(spy.all_matches[0].rects) > 1
    assert len(spy.current_matches[0].rects) == len(spy.all_matches[0].rects)


def test_a_synthetic_multi_rectangle_result_is_converted_whole(search_view: PdfView) -> None:
    """複数矩形の変換は、矩形の数だけそのまま返る。"""
    link = as_link(
        FakeSearchResult(
            page=0,
            location=QPointF(10.0, 20.0),
            rectangles=[QRectF(10.0, 20.0, 30.0, 12.0), QRectF(50.0, 60.0, 40.0, 12.0)],
        )
    )

    rects = search_view.search_result_viewport_rects(link)

    assert_rects_close(rects, expected_rects(search_view, link))


def test_only_visible_pages_are_scanned_for_results(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """描画のたびに文書全体の結果を走査しない。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)

    scanned: list[int] = []
    original = view_search.model.resultsOnPage

    def recording(page: int) -> list[QPdfLink]:
        scanned.append(page)
        return original(page)

    monkeypatch.setattr(view_search.model, "resultsOnPage", recording)
    render_view(search_view)

    assert scanned == list(search_view.visible_pages())


def test_highlight_geometry_matches_the_page_rectangles(
    qtbot: QtBot, search_view: PdfView, view_search: PdfSearchController
) -> None:
    """ハイライトの矩形は、ページ上の一致位置（PDF ポイント）に対応する。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    link = view_search.current_result()
    assert link is not None

    assert_rects_close(
        search_view.search_result_viewport_rects(link), expected_rects(search_view, link)
    )


@pytest.mark.parametrize("zoom", [MIN_ZOOM, 1.0, 2.0])
def test_highlight_geometry_follows_the_zoom(
    qtbot: QtBot, search_view: PdfView, view_search: PdfSearchController, zoom: float
) -> None:
    """倍率を変えてもハイライトはページ上の一致位置に追随する。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    search_view.set_zoom(zoom)
    link = view_search.current_result()
    assert link is not None

    assert_rects_close(
        search_view.search_result_viewport_rects(link), expected_rects(search_view, link)
    )


def test_highlight_geometry_ignores_the_device_pixel_ratio(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """ハイライトの位置は `devicePixelRatio` に依存しない。

    物理ピクセルを混ぜた実装なら、DPR 1.5 で 1.5 倍ずれる。
    """
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    link = view_search.current_result()
    assert link is not None
    before = search_view.search_result_viewport_rects(link)

    monkeypatch.setattr(search_view, "devicePixelRatioF", lambda: 1.5)
    QApplication.sendEvent(search_view, QEvent(QEvent.Type.DevicePixelRatioChange))

    assert_rects_close(search_view.search_result_viewport_rects(link), before)


def test_highlight_geometry_follows_a_resize(
    qtbot: QtBot, search_view: PdfView, view_search: PdfSearchController
) -> None:
    """ウィンドウの大きさが変わってもハイライトはページに追随する。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    link = view_search.current_result()
    assert link is not None

    search_view.resize(700, 500)

    assert_rects_close(
        search_view.search_result_viewport_rects(link), expected_rects(search_view, link)
    )


def test_highlights_are_drawn_over_the_page_image(
    qtbot: QtBot, search_view: PdfView, view_search: PdfSearchController, cache: RenderCache
) -> None:
    """ハイライトはページ画像の上に重なる。

    重ね順が逆（ページ画像が後）なら、同じ位置の画素が変わらない。
    """
    put_image(cache, search_view, 0, Qt.GlobalColor.white)
    before = render_view(search_view)

    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    search_view.set_current_search_result(0)
    link = view_search.current_result()
    assert link is not None
    center = search_view.search_result_viewport_rects(link)[0].center().toPoint()

    after = render_view(search_view)

    assert after.pixelColor(center) != before.pixelColor(center)


def test_the_study_mark_badges_stay_above_the_highlights(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """重ね順は ページ → 検索 → 学習マーク。バッジは検索の上に残る。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    search_view.set_current_search_result(0)
    search_view.set_study_marks(
        [
            StudyMark(
                id=1,
                document_key="C:\\book.pdf",
                page_index=0,
                x_norm=0.5,
                y_norm=0.5,
                mistake_count=1,
                note=None,
            )
        ]
    )

    spy = OverlaySpy(monkeypatch)
    render_view(search_view)

    assert "badge" in spy.order
    last_search = max(index for index, name in enumerate(spy.order) if name == "search")
    assert spy.order.index("badge") > last_search


@pytest.mark.parametrize(
    "mode", [PageColorMode.ORIGINAL, PageColorMode.INVERT, PageColorMode.SMART_DARK]
)
def test_highlights_exist_in_every_page_color_mode(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
    cache: RenderCache,
    mode: PageColorMode,
) -> None:
    """ページの色変換のどれでもハイライトは重ねて描かれる。

    オーバーレイなので、変換の結果を取っておく表示用キャッシュには入らない。
    """
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    put_image(cache, search_view, 0, Qt.GlobalColor.white)
    search_view.set_page_color_mode(mode)
    search_view.set_current_search_result(0)

    spy = OverlaySpy(monkeypatch)
    render_view(search_view)

    assert spy.all_matches
    assert spy.current_matches


def test_dropping_the_search_model_disconnects_it(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    search_view: PdfView,
    view_search: PdfSearchController,
) -> None:
    """モデルを外したらハイライトも消え、古いモデルは描画に使われない。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)

    search_view.set_search_model(None)

    spy = OverlaySpy(monkeypatch)
    render_view(search_view)
    assert spy.searches == []
    assert search_view.current_search_index == NO_RESULT


def test_a_new_document_drops_the_current_search_result(
    qtbot: QtBot, search_view: PdfView, view_search: PdfSearchController, two_page_pdf: Path
) -> None:
    """PDF を差し替えたらビューの現在の検索結果も捨てる。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    search_view.set_current_search_result(0)

    other = DocumentController()
    other.open(two_page_pdf)
    search_view.set_document(other.document, other.page_sizes())

    assert search_view.current_search_index == NO_RESULT
    other.close()


# ================================================================ 結果への移動
def test_revealing_a_result_brings_it_into_the_viewport(
    qtbot: QtBot, search_view: PdfView, view_search: PdfSearchController
) -> None:
    """結果への移動で、その領域がビューポートの中に入る。

    ページ先頭までしか動かない実装なら、ページの下の方にある一致は
    見えないままになる。
    """
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    # 3件目は1ページ目の中ほどにある。
    view_search.next_result()
    view_search.next_result()
    link = view_search.current_result()
    assert link is not None

    assert search_view.reveal_pdf_search_result(link)

    rects = search_view.search_result_viewport_rects(link)
    assert rects
    for rect in rects:
        assert viewport_rect(search_view).contains(rect), f"{rect} がビューポートの外にある"


def test_revealing_a_result_below_the_fold_scrolls_past_the_page_top(
    qtbot: QtBot, search_view: PdfView, view_search: PdfSearchController
) -> None:
    """一致がページ上端から遠くても、その領域まで送る。

    最後の一致は1ページ目のかなり下にあるので、「そのページの先頭まで
    移動する」だけの実装では見えないままになる。
    """
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    view_search.previous_result()
    link = view_search.current_result()
    assert link is not None
    assert view_search.current_index == SEARCH_QUERY_HITS - 1

    # ページの先頭では一致が見えない位置にあることを先に確かめる。
    search_view.go_to_page(link.page())
    assert not viewport_rect(search_view).intersects(
        search_view.search_result_viewport_rects(link)[0]
    )

    assert search_view.reveal_pdf_search_result(link)

    for rect in search_view.search_result_viewport_rects(link):
        assert viewport_rect(search_view).contains(rect), f"{rect} がビューポートの外にある"


def test_revealing_an_invalid_result_does_nothing(search_view: PdfView) -> None:
    """無効な結果は安全に無視する。最終ページへ丸めたりしない。"""
    before = search_view.verticalScrollBar().value()

    assert not search_view.reveal_pdf_search_result(QPdfLink())
    assert not search_view.reveal_pdf_search_result(as_link(FakeSearchResult(valid=False)))
    assert search_view.verticalScrollBar().value() == before


@pytest.mark.parametrize("page", [-1, 3, 99])
def test_revealing_a_result_outside_the_document_does_nothing(
    search_view: PdfView, page: int
) -> None:
    """いまの PDF に無いページを指す結果は無視する。"""
    before = search_view.verticalScrollBar().value()
    link = as_link(FakeSearchResult(page=page, rectangles=[QRectF(10.0, 400.0, 30.0, 12.0)]))

    assert not search_view.reveal_pdf_search_result(link)
    assert search_view.search_result_viewport_rects(link) == []
    assert search_view.verticalScrollBar().value() == before


def test_a_result_without_rectangles_falls_back_to_the_location(search_view: PdfView) -> None:
    """矩形を持たない結果は location へ落ちる（そこへ移動できる）。"""
    link = as_link(FakeSearchResult(page=0, location=QPointF(100.0, 400.0)))

    rects = search_view.search_result_viewport_rects(link)

    assert len(rects) == 1
    page_rect = search_view.page_viewport_rect(0)
    assert page_rect is not None
    assert rects[0].left() == pytest.approx(page_rect.left() + 100.0 * search_view.zoom)
    assert rects[0].top() == pytest.approx(page_rect.top() + 400.0 * search_view.zoom)
    assert search_view.reveal_pdf_search_result(link)


def test_a_result_with_broken_geometry_is_clamped(search_view: PdfView) -> None:
    """NaN や範囲外の矩形でも落ちず、ページの内側へ収める。

    値の出どころは外部の PDF なので、例外にせず安全な値へ落とす
    （目次の移動先と同じ方針）。
    """
    broken = QRectF(float("nan"), -10_000.0, 1e9, 1e9)
    link = as_link(FakeSearchResult(page=0, rectangles=[broken]))

    rects = search_view.search_result_viewport_rects(link)

    page_rect = search_view.page_viewport_rect(0)
    assert page_rect is not None
    assert len(rects) == 1
    assert rects[0].left() >= page_rect.left() - 0.01
    assert rects[0].bottom() <= page_rect.bottom() + 0.01
    assert search_view.reveal_pdf_search_result(link)


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda view: view.set_zoom(MIN_ZOOM), id="25%"),
        pytest.param(lambda view: view.set_zoom(1.0), id="100%"),
        pytest.param(lambda view: view.set_zoom(4.0), id="400%"),
        pytest.param(lambda view: view.fit_width(), id="fit_width"),
        pytest.param(lambda view: view.fit_page(), id="fit_page"),
    ],
)
def test_moving_between_results_keeps_the_zoom(
    qtbot: QtBot,
    search_view: PdfView,
    view_search: PdfSearchController,
    prepare: Callable[[PdfView], None],
) -> None:
    """次の結果へ飛んでも倍率と倍率モードは変わらない。"""
    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    prepare(search_view)
    zoom, mode = search_view.zoom, search_view.zoom_mode

    view_search.next_result()
    link = view_search.current_result()
    assert link is not None
    assert search_view.reveal_pdf_search_result(link)

    assert search_view.zoom == pytest.approx(zoom)
    assert search_view.zoom_mode is mode


def test_a_result_zoom_hint_is_never_applied(search_view: PdfView) -> None:
    """検索結果に倍率が入っていても適用しない。利用者の方針を優先する。"""
    search_view.set_zoom(1.0)
    link = as_link(FakeSearchResult(page=0, rectangles=[QRectF(50.0, 50.0, 20.0, 10.0)], zoom=4.0))

    assert search_view.reveal_pdf_search_result(link)
    assert search_view.zoom == pytest.approx(1.0)
    assert search_view.zoom_mode is ZoomMode.FREE


def test_searching_never_resets_the_render_state(
    qtbot: QtBot,
    search_view: PdfView,
    view_search: PdfSearchController,
    cache: RenderCache,
    service: RecordingService,
) -> None:
    """検索・ハイライト・前後への移動でレンダリングをやり直さない。

    スクロールに伴う通常のレンダリング要求は起きてよい。壊してはいけない
    のは **既に持っている画像** の方（P4 / P5-2 と同じ観点）。
    """
    put_image(cache, search_view, 0, Qt.GlobalColor.red)
    search_view.set_page_color_mode(PageColorMode.INVERT)
    service.repeat_last_request()

    raw_key = cache.nearest_key(0, width_px=0)
    display_key = service.display_cache.nearest_key(0, 0, PageColorMode.INVERT)
    assert raw_key is not None
    assert display_key is not None
    generation = service.generation

    view_search.set_query(SEARCH_QUERY)
    wait_for_count(qtbot, view_search, SEARCH_QUERY_HITS)
    search_view.set_current_search_result(view_search.current_index)
    view_search.next_result()
    view_search.previous_result()
    link = view_search.current_result()
    assert link is not None
    search_view.reveal_pdf_search_result(link)

    assert service.generation == generation
    assert raw_key in cache
    assert display_key in service.display_cache
    assert search_view.page_color_mode is PageColorMode.INVERT


# ================================================================ ウィンドウ統合
@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`QMessageBox.warning` を捕まえて本文を集める（ダイアログを出さない）。"""
    messages: list[str] = []
    monkeypatch.setattr(
        "anp.ui.main_window.QMessageBox.warning",
        lambda *args: messages.append(args[2]),
    )
    return messages


@pytest.fixture
def window(
    qtbot: QtBot, settings: Settings, study_marks: StudyMarkRepository
) -> Iterator[MainWindow]:
    """表示済みのメインウィンドウ。"""
    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    yield window
    window.close()


@pytest.fixture
def searching(window: MainWindow, searchable_pdf: Path) -> MainWindow:
    """検索できる PDF を開いたウィンドウ。"""
    window.open_path(searchable_pdf)
    return window


def type_query(qtbot: QtBot, window: MainWindow, query: str) -> None:
    """検索欄へ実際に打鍵し、件数が揃うまで待つ。"""
    qtbot.keyClicks(window.search_dock.query_edit, query)
    qtbot.waitUntil(lambda: window.search.query == query, timeout=_SEARCH_TIMEOUT_MS)


def wait_for_hits(qtbot: QtBot, window: MainWindow, expected: int) -> None:
    qtbot.waitUntil(lambda: window.search.count == expected, timeout=_SEARCH_TIMEOUT_MS)


def send_key(
    widget: QWidget,
    key: Qt.Key,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    """フォーカスの状態に依らず、そのウィジェットへ押鍵を送る。"""
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


def test_the_search_dock_is_hidden_until_asked_for(window: MainWindow) -> None:
    """検索ドックは既定で非表示。下に置く（左が目次、右が学習マーク）。"""
    assert window.search_dock.isHidden()
    assert window.dockWidgetArea(window.search_dock) == Qt.DockWidgetArea.BottomDockWidgetArea


def test_ctrl_f_shows_the_dock_and_focuses_the_field(searching: MainWindow) -> None:
    """Ctrl+F で検索ドックが出て、入力欄にフォーカスが移る。"""
    assert searching.search_dock.isHidden()

    searching.reader_actions.find.trigger()

    assert not searching.search_dock.isHidden()
    assert searching.search_dock.query_edit.hasFocus()


def test_ctrl_f_selects_the_existing_query(qtbot: QtBot, searching: MainWindow) -> None:
    """既に検索語があれば、Ctrl+F で選択状態にして打ち直せるようにする。"""
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)

    searching.reader_actions.find.trigger()

    assert searching.search_dock.query_edit.selectedText() == SEARCH_QUERY


def test_typing_a_query_searches_and_shows_the_first_result(
    qtbot: QtBot, searching: MainWindow
) -> None:
    """打鍵 → 検索モデル → 件数表示 → 先頭の結果まで移動、が実際につながる。"""
    searching.reader_actions.find.trigger()

    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)

    assert searching.search.model.searchString() == SEARCH_QUERY
    assert searching.search_dock.count_text == f"1 / {SEARCH_QUERY_HITS}"
    assert searching.view.current_search_index == 0

    link = searching.search.current_result()
    assert link is not None
    rects = searching.view.search_result_viewport_rects(link)
    assert rects
    assert viewport_rect(searching.view).contains(rects[0])


def test_enter_moves_between_results(qtbot: QtBot, searching: MainWindow) -> None:
    """検索欄の Enter で次の結果へ、Shift+Enter で前の結果へ。"""
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)

    send_key(searching.search_dock.query_edit, Qt.Key.Key_Return)
    assert searching.search.current_index == 1
    assert searching.view.current_search_index == 1

    send_key(searching.search_dock.query_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert searching.search.current_index == 0


def test_f3_moves_between_results(qtbot: QtBot, searching: MainWindow) -> None:
    """F3 / Shift+F3 でも前後へ動く。"""
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)

    searching.reader_actions.find_next.trigger()
    assert searching.search.current_index == 1

    searching.reader_actions.find_previous.trigger()
    assert searching.search.current_index == 0


def test_the_next_button_wraps_at_the_end(qtbot: QtBot, searching: MainWindow) -> None:
    """末尾で次へ進むと先頭へ回る。件数の表示も戻る。"""
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)

    for _ in range(SEARCH_QUERY_HITS):
        searching.search_dock.next_button.click()

    assert searching.search.current_index == 0
    assert searching.search_dock.count_text == f"1 / {SEARCH_QUERY_HITS}"


def test_hiding_the_dock_keeps_the_query(qtbot: QtBot, searching: MainWindow) -> None:
    """同じ PDF を読んでいる間は、ドックを閉じても検索語は残る。"""
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)

    searching.search_dock.hide()
    searching.reader_actions.find.trigger()

    assert searching.search.query == SEARCH_QUERY
    assert searching.search_dock.query_edit.text() == SEARCH_QUERY


def test_opening_another_document_resets_the_search(
    qtbot: QtBot, searching: MainWindow, other_searchable_pdf: Path
) -> None:
    """A で検索したまま B を開くと、検索語も結果もハイライトも残らない。"""
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)

    searching.open_path(other_searchable_pdf)

    assert searching.search.query == ""
    assert searching.search.count == 0
    assert searching.search.current_index == NO_RESULT
    assert searching.search_dock.query_edit.text() == ""
    assert searching.search_dock.count_text == "0 / 0"
    assert searching.view.current_search_index == NO_RESULT
    # B の検索は始められる状態になっている。
    assert searching.search.has_document


def test_opening_another_document_repoints_the_view_to_the_new_model(
    qtbot: QtBot, searching: MainWindow, other_searchable_pdf: Path
) -> None:
    """モデルを作り直したら、ハイライトを描く側の参照も張り替わる。

    古いモデルは `deleteLater()` で消えるので、ビューが指したままだと
    描画のたびに解放済みのオブジェクトを引くことになる。
    """
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)
    before = searching.view.search_model

    searching.open_path(other_searchable_pdf)

    assert searching.view.search_model is searching.search.model
    assert searching.view.search_model is not before


def test_the_search_is_detached_before_the_document_is_loaded(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    searching: MainWindow,
    other_searchable_pdf: Path,
) -> None:
    """B の読み込みに入る時点で、検索は完全に外れている。

    `DocumentController` は `QPdfDocument` を使い回すので、検索モデルを
    付けたまま `load()` を呼ぶと、`QPdfSearchModel` が走査している最中に
    対象のページが消える。最終状態（検索語も件数も空）だけでは
    **順序そのもの**を固定できないので、`open()` に入った瞬間を直接見る。
    """
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)

    observed: list[tuple[str, str, QPdfDocument | None, int]] = []
    original_open = DocumentController.open

    def checked_open(controller: DocumentController, path: Path) -> None:
        search = searching.search
        observed.append(
            (search.query, search.model.searchString(), search.model.document(), search.count)
        )
        original_open(controller, path)

    monkeypatch.setattr(DocumentController, "open", checked_open)

    searching.open_path(other_searchable_pdf)

    # 検索語・モデルの検索文字列・モデルのドキュメント・件数のすべてが、
    # 読み込みを始める前に空へ戻っている。
    assert observed == [("", "", None, 0)]


def test_switching_documents_while_a_search_is_running(
    qtbot: QtBot, window: MainWindow, searchable_pdf: Path, other_searchable_pdf: Path
) -> None:
    """検索が **走っている最中** の切り替えを繰り返しても壊れない。

    `QPdfSearchModel` の走査はイベントループ上で少しずつ進む。検索が
    終わってから切り替えるのでは危ない瞬間を踏めないので、最初の一致が
    届いた直後（＝まだ後ろのページを走査している）に次の PDF を開く。

    `QPdfDocument` を使い回す設計そのものへの回帰テストなので、同じ
    プロセスで何度も繰り返す（native の Qt プラットフォームで動かすと、
    寿命の誤りは access violation として出る）。
    """
    window.reader_actions.find.trigger()
    paths = (searchable_pdf, other_searchable_pdf)

    for index in range(_MID_SEARCH_SWITCHES):
        window.open_path(paths[index % len(paths)])
        # 打鍵と同じ経路（`textChanged` → `query_changed`）で検索を始める。
        window.search_dock.query_edit.setText(_COMMON_QUERY)
        qtbot.waitUntil(lambda: window.search.count > 0, timeout=_SEARCH_TIMEOUT_MS)

    # 最後まで生きていて、通常の検索がそのまま続けられる。
    window.open_path(searchable_pdf)
    window.search_dock.query_edit.setText(SEARCH_QUERY)
    wait_for_hits(qtbot, window, SEARCH_QUERY_HITS)
    assert window.search.current_index == 0
    assert window.view.has_document


def test_a_failed_open_detaches_the_search(
    qtbot: QtBot, searching: MainWindow, broken_pdf: Path, warnings: list[str]
) -> None:
    """開くのに失敗したら、検索も「PDF なし」の状態まで戻る。"""
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)

    searching.open_path(broken_pdf)

    assert warnings
    assert not searching.search.has_document
    assert searching.search.model.document() is None
    assert searching.search.query == ""
    assert searching.search.count == 0
    assert searching.view.current_search_index == NO_RESULT


def test_a_study_mark_failure_detaches_the_search(
    qtbot: QtBot,
    settings: Settings,
    study_mark_connection: sqlite3.Connection,
    searchable_pdf: Path,
    other_searchable_pdf: Path,
    warnings: list[str],
) -> None:
    """PDF は読めたが学習マークを読めなかった場合も、検索は載せない。"""
    repository = BrokenRepository(study_mark_connection)
    window = MainWindow(settings, repository)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    window.open_path(searchable_pdf)
    window.reader_actions.find.trigger()
    type_query(qtbot, window, SEARCH_QUERY)

    repository.failing = "list"
    window.open_path(other_searchable_pdf)

    assert warnings
    assert not window.search.has_document
    assert window.search.model.document() is None
    assert window.search.count == 0
    assert window.search.query == ""
    window.close()


def test_an_automatic_restore_attaches_the_search_model(
    qtbot: QtBot, tmp_path: Path, study_marks: StudyMarkRepository, searchable_pdf: Path
) -> None:
    """自動復元で開いた PDF にも検索モデルが載る。検索語は復元しない。"""
    settings = Settings(QSettings(str(tmp_path / "restore.ini"), QSettings.Format.IniFormat))
    settings.set_last_session(str(searchable_pdf), 0, 0.0)

    window = MainWindow(settings, study_marks)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()

    assert window.view.has_document
    assert window.search.has_document
    document = window.search.model.document()
    assert document is not None
    assert document.pageCount() == window.view.page_count
    assert window.search.query == ""
    assert window.search.count == 0
    assert window.search.current_index == NO_RESULT
    assert window.search_dock.query_edit.text() == ""
    window.close()


def test_the_query_is_not_persisted(
    qtbot: QtBot, tmp_path: Path, study_marks: StudyMarkRepository, searchable_pdf: Path
) -> None:
    """終了して開き直しても検索語は空から始まる。設定キーも増やさない。"""
    path = str(tmp_path / "persist.ini")
    first = MainWindow(Settings(QSettings(path, QSettings.Format.IniFormat)), study_marks)
    qtbot.addWidget(first)
    with qtbot.waitExposed(first):
        first.show()
    first.open_path(searchable_pdf)
    first.reader_actions.find.trigger()
    type_query(qtbot, first, SEARCH_QUERY)
    first.close()

    stored = QSettings(path, QSettings.Format.IniFormat)
    assert [key for key in stored.allKeys() if "search" in key.lower()] == []

    second = MainWindow(Settings(stored), study_marks)
    qtbot.addWidget(second)
    with qtbot.waitExposed(second):
        second.show()

    assert second.search.query == ""
    assert second.search.count == 0
    assert second.search_dock.query_edit.text() == ""
    second.close()


def test_an_image_only_document_finds_nothing_without_a_dialog(
    qtbot: QtBot, window: MainWindow, image_only_pdf: Path, warnings: list[str]
) -> None:
    """スキャン画像だけの PDF は 0 件。`0 / 0` だけで、断定も警告もしない。"""
    window.open_path(image_only_pdf)
    window.reader_actions.find.trigger()

    type_query(qtbot, window, SEARCH_QUERY)

    assert window.search.count == 0
    assert window.search_dock.count_text == "0 / 0"
    labels = [child.text() for child in window.search_dock.findChildren(QLabel)]
    assert all("一致" not in label for label in labels)
    assert warnings == []


def test_searching_leaves_the_other_features_alone(qtbot: QtBot, searching: MainWindow) -> None:
    """検索は学習マーク・目次・履歴・設定のどれにも触らない。"""
    marks_before = searching.study_marks.study_marks
    recent_before = searching.recent_files
    toc_selection_before = searching.toc_sidebar.tree.currentIndex()

    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)
    searching.search.next_result()

    assert searching.study_marks.study_marks == marks_before
    assert searching.study_mark_sidebar.marks == marks_before
    assert searching.recent_files == recent_before
    assert searching.toc_sidebar.tree.currentIndex() == toc_selection_before


def test_a_pdf_without_a_text_layer_still_opens(window: MainWindow, sample_pdf: Path) -> None:
    """テキストを取り出せない PDF でも開ける（検索が 0 件になるだけ）。"""
    window.open_path(sample_pdf)

    assert window.view.has_document
    assert window.search.has_document


def test_closing_the_window_detaches_the_search_model(qtbot: QtBot, searching: MainWindow) -> None:
    """終了時、`QPdfDocument` を閉じる前に検索モデルを外す。

    閉じたドキュメントをモデルが指し続けないようにするため（目次と同じ扱い）。
    """
    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)

    searching.close()

    assert searching.search.model.document() is None
    assert searching.search.count == 0


def test_the_view_gets_the_controller_model(window: MainWindow) -> None:
    """ハイライトの情報源はコントローラが持つ検索モデル1つだけ。"""
    assert window.view.search_model is window.search.model
    assert isinstance(window.view.search_model, QPdfSearchModel)


def test_the_render_service_is_untouched_by_the_search(qtbot: QtBot, searching: MainWindow) -> None:
    """検索操作で世代を進めない（本物のレンダリングサービス経由でも）。"""
    service = searching.findChild(PageRenderService)
    assert isinstance(service, PageRenderService)
    generation = service.generation

    searching.reader_actions.find.trigger()
    type_query(qtbot, searching, SEARCH_QUERY)
    wait_for_hits(qtbot, searching, SEARCH_QUERY_HITS)
    searching.reader_actions.find_next.trigger()

    assert service.generation == generation
