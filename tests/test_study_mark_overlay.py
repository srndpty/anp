"""学習マークの座標変換・オーバーレイ描画・ヒットテストのテスト。

観測するのは公開 API と実際に描かれた画素だけにする。バッジの内部形状に
テストから直接触ると、見た目を調整するたびにテストが壊れてしまう。

固定のピクセル座標は書かない。期待値は必ず `page_viewport_rect()`
（既存のレイアウト計算）から組み立てる。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage

from anp.pdf.cache import RenderCache
from anp.pdf.color import PageColorMode
from anp.pdf.document import DocumentController
from anp.storage.study_mark import StudyMark
from anp.ui.appearance import CanvasTheme
from anp.ui.pdf_view import MAX_ZOOM, MIN_ZOOM, PdfView, ZoomMode
from anp.ui.study_marks import BADGE_FILL_COLOR, BADGE_HEIGHT, PagePosition, StudyMarkIndex
from helpers import RecordingService, UpdateSpy, count_transforms, put_image, render_view

# 位置の一致を見るときの許容誤差（論理ピクセル）。描画は反エイリアスされるので、
# 画素から復元した中心は 1px 程度ずれる。
POSITION_TOLERANCE = 1.5


def make_mark(
    *,
    id: int = 1,
    page: int = 0,
    x: float = 0.5,
    y: float = 0.5,
    count: int = 1,
) -> StudyMark:
    """テスト用の学習マーク。"""
    return StudyMark(
        id=id,
        document_key="C:\\book.pdf",
        page_index=page,
        x_norm=x,
        y_norm=y,
        mistake_count=count,
        note=None,
    )


def page_point(view: PdfView, page: int, x: float, y: float) -> QPointF:
    """ページ内の比率に対応するビューポート上の点（既存レイアウト由来）。"""
    rect = view.page_viewport_rect(page)
    assert rect is not None
    return QPointF(rect.left() + rect.width() * x, rect.top() + rect.height() * y)


def fill_pixels(view: PdfView) -> np.ndarray:
    """描画結果のうち、バッジの塗り色と一致する画素の位置（行, 列）。

    画素を1つずつ Python で読むと遅いので、`QImage` のバッファをまとめて見る。
    """
    image = render_view(view).convertToFormat(QImage.Format.Format_ARGB32)
    raw = np.frombuffer(image.constBits(), dtype=np.uint8)
    pixels = raw.reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4].reshape(
        image.height(), image.width(), 4
    )
    # Format_ARGB32 のバイト順（リトルエンディアン）は B, G, R, A。
    match = (
        (pixels[:, :, 0] == BADGE_FILL_COLOR.blue())
        & (pixels[:, :, 1] == BADGE_FILL_COLOR.green())
        & (pixels[:, :, 2] == BADGE_FILL_COLOR.red())
    )
    return np.argwhere(match)


def badge_count_pixels(view: PdfView) -> int:
    """バッジの塗り色で描かれた画素の数。0 なら描かれていない。"""
    return len(fill_pixels(view))


def painted_badge_center(view: PdfView) -> QPointF:
    """実際に描かれたバッジ（塗りの部分）の中心。

    バッジが1つだけ、かつ全体が見えているときに使う。
    """
    found = fill_pixels(view)
    assert len(found) > 0, "バッジが描かれていない"
    rows, columns = found[:, 0], found[:, 1]
    return QPointF(
        (float(columns.min()) + float(columns.max())) / 2,
        (float(rows.min()) + float(rows.max())) / 2,
    )


def assert_close(actual: QPointF, expected: QPointF, tolerance: float = POSITION_TOLERANCE) -> None:
    assert actual.x() == pytest.approx(expected.x(), abs=tolerance)
    assert actual.y() == pytest.approx(expected.y(), abs=tolerance)


@pytest.fixture
def fitted_view(loaded_view: PdfView) -> PdfView:
    """1ページ全体が見えているビュー。描かれたバッジを画素から確かめるのに使う。"""
    loaded_view.fit_page()
    return loaded_view


# ------------------------------------------------------------------ ビューポート → 正規化座標
def test_the_page_center_is_the_middle_of_the_page(loaded_view: PdfView) -> None:
    """ページ中央は (0.5, 0.5)。"""
    rect = loaded_view.page_viewport_rect(0)
    assert rect is not None

    position = loaded_view.page_position_at(rect.center())

    assert isinstance(position, PagePosition)
    assert position.page_index == 0
    assert position.x_norm == pytest.approx(0.5)
    assert position.y_norm == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("corner", "expected"),
    [
        ("topLeft", (0.0, 0.0)),
        ("topRight", (1.0, 0.0)),
        ("bottomLeft", (0.0, 1.0)),
        ("bottomRight", (1.0, 1.0)),
    ],
)
def test_the_page_corners_are_the_unit_square(
    loaded_view: PdfView, corner: str, expected: tuple[float, float]
) -> None:
    """4隅がそのまま 0.0 と 1.0 になる。"""
    rect = loaded_view.page_viewport_rect(0)
    assert rect is not None

    position = loaded_view.page_position_at(getattr(rect, corner)())

    assert position is not None
    assert position.page_index == 0
    assert (position.x_norm, position.y_norm) == pytest.approx(expected)


def test_a_point_on_the_second_page_reports_that_page(loaded_view: PdfView) -> None:
    """ページ番号は点が乗っているページのもの。"""
    loaded_view.go_to_page(1)

    position = loaded_view.page_position_at(page_point(loaded_view, 1, 0.5, 0.5))

    assert position is not None
    assert position.page_index == 1


def test_the_canvas_margin_is_not_on_a_page(loaded_view: PdfView) -> None:
    """余白の上は None。いちばん近いページへ吸着させない。"""
    assert loaded_view.page_position_at(QPointF(2.0, 2.0)) is None


def test_the_gap_between_pages_is_not_on_a_page(loaded_view: PdfView) -> None:
    """ページ間の隙間も None。"""
    loaded_view.set_zoom(MIN_ZOOM)
    first = loaded_view.page_viewport_rect(0)
    second = loaded_view.page_viewport_rect(1)
    assert first is not None
    assert second is not None
    gap = QPointF(first.center().x(), (first.bottom() + second.top()) / 2)

    assert loaded_view.page_position_at(gap) is None


def test_above_and_below_the_document_is_not_on_a_page(loaded_view: PdfView) -> None:
    """文書の上端より上・下端より下も None。"""
    last = loaded_view.page_viewport_rect(2)
    assert last is not None

    assert loaded_view.page_position_at(QPointF(200.0, -100.0)) is None
    assert loaded_view.page_position_at(QPointF(last.center().x(), last.bottom() + 5.0)) is None


def test_a_point_beside_the_page_is_not_on_a_page(loaded_view: PdfView) -> None:
    """ページの左右のキャンバスも None（横方向にも吸着しない）。"""
    rect = loaded_view.page_viewport_rect(0)
    assert rect is not None

    assert loaded_view.page_position_at(QPointF(rect.left() - 5.0, rect.center().y())) is None
    assert loaded_view.page_position_at(QPointF(rect.right() + 5.0, rect.center().y())) is None


def test_positions_are_never_clamped_into_range(loaded_view: PdfView) -> None:
    """ページ外の点を 0.0〜1.0 へ丸めて返したりしない。"""
    rect = loaded_view.page_viewport_rect(0)
    assert rect is not None

    for offset in (-30.0, 30.0):
        outside = QPointF(
            rect.left() + offset if offset < 0 else rect.right() + offset, rect.center().y()
        )
        assert loaded_view.page_position_at(outside) is None


def test_there_is_no_position_without_a_document(view: PdfView) -> None:
    """ドキュメントが無ければ None。"""
    assert view.page_position_at(QPointF(10.0, 10.0)) is None


# ------------------------------------------------------------------ スクロールと倍率からの独立
def test_scrolling_does_not_change_the_normalized_position(loaded_view: PdfView) -> None:
    """スクロールしてもページ上の同じ位置の正規化座標は変わらない。"""
    before = loaded_view.page_position_at(page_point(loaded_view, 0, 0.25, 0.3))
    assert before is not None

    loaded_view.verticalScrollBar().setValue(200)

    after = loaded_view.page_position_at(page_point(loaded_view, 0, 0.25, 0.3))
    assert after is not None
    assert after.page_index == before.page_index
    assert after.x_norm == pytest.approx(before.x_norm)
    assert after.y_norm == pytest.approx(before.y_norm)


def test_scrolling_moves_the_viewport_point(loaded_view: PdfView) -> None:
    """一方、同じ正規化座標に対応するビューポート上の点はスクロール分だけ動く。"""
    before = loaded_view.viewport_point_for(0, 0.25, 0.3)
    assert before is not None

    loaded_view.verticalScrollBar().setValue(200)

    after = loaded_view.viewport_point_for(0, 0.25, 0.3)
    assert after is not None
    assert after.x() == pytest.approx(before.x())
    assert after.y() == pytest.approx(before.y() - 200)


@pytest.mark.parametrize("zoom", [0.25, 1.0, 4.0])
def test_the_normalized_position_is_the_same_at_any_zoom(loaded_view: PdfView, zoom: float) -> None:
    """25% / 100% / 400% のどれでも同じ正規化座標になる。"""
    loaded_view.set_zoom(zoom)

    position = loaded_view.page_position_at(page_point(loaded_view, 0, 0.25, 0.75))

    assert position is not None
    assert position.page_index == 0
    assert position.x_norm == pytest.approx(0.25)
    assert position.y_norm == pytest.approx(0.75)


# ------------------------------------------------------------------ 正規化座標 → ビューポート
@pytest.mark.parametrize(("x", "y"), [(0.0, 0.0), (0.25, 0.75), (0.5, 0.5), (1.0, 1.0)])
@pytest.mark.parametrize("zoom", [0.25, 1.0, 4.0])
def test_the_round_trip_keeps_the_normalized_position(
    loaded_view: PdfView, x: float, y: float, zoom: float
) -> None:
    """正規化 → ビューポート → 正規化 で元の値に戻る（画素へ量子化しない）。"""
    loaded_view.set_zoom(zoom)

    point = loaded_view.viewport_point_for(0, x, y)
    assert point is not None
    position = loaded_view.page_position_at(point)

    assert position is not None
    assert position.page_index == 0
    assert position.x_norm == pytest.approx(x)
    assert position.y_norm == pytest.approx(y)


def test_the_round_trip_from_a_viewport_point(loaded_view: PdfView) -> None:
    """ビューポート → 正規化 → ビューポート でも元の点に戻る。"""
    loaded_view.verticalScrollBar().setValue(150)
    point = page_point(loaded_view, 0, 0.4, 0.6)

    position = loaded_view.page_position_at(point)
    assert position is not None
    restored = loaded_view.viewport_point_for(position.page_index, position.x_norm, position.y_norm)

    assert restored is not None
    assert_close(restored, point, tolerance=1e-6)


@pytest.mark.parametrize("apply_fit", ["fit_width", "fit_page"])
def test_the_round_trip_works_in_the_fit_modes(loaded_view: PdfView, apply_fit: str) -> None:
    """Fit Width / Fit Page でも変換は成り立つ（フィット用の分岐を持たない）。"""
    getattr(loaded_view, apply_fit)()

    point = loaded_view.viewport_point_for(0, 0.25, 0.75)
    assert point is not None
    position = loaded_view.page_position_at(point)

    assert position is not None
    assert position.x_norm == pytest.approx(0.25)
    assert position.y_norm == pytest.approx(0.75)


def test_the_viewport_point_follows_the_page_geometry(loaded_view: PdfView) -> None:
    """ビューポート上の点は、そのときのページ矩形から決まる。"""
    for zoom in (MIN_ZOOM, 1.0, 3.0, MAX_ZOOM):
        loaded_view.set_zoom(zoom)

        point = loaded_view.viewport_point_for(0, 0.25, 0.75)

        assert point is not None
        assert_close(point, page_point(loaded_view, 0, 0.25, 0.75), tolerance=1e-6)


def test_a_missing_page_has_no_viewport_point(loaded_view: PdfView) -> None:
    """存在しないページ番号は安全に None。"""
    assert loaded_view.viewport_point_for(3, 0.5, 0.5) is None
    assert loaded_view.viewport_point_for(99, 0.5, 0.5) is None


def test_there_is_no_viewport_point_without_a_document(view: PdfView) -> None:
    """ドキュメントが無ければ逆変換も None。"""
    assert view.viewport_point_for(0, 0.5, 0.5) is None


@pytest.mark.parametrize(("x", "y"), [(-0.1, 0.5), (1.5, 0.5), (0.5, -0.01), (0.5, 1.01)])
def test_out_of_contract_values_are_rejected(loaded_view: PdfView, x: float, y: float) -> None:
    """契約外の正規化座標は黙って丸めず失敗させる。"""
    with pytest.raises(ValueError, match=r"within 0\.0\.\.1\.0"):
        loaded_view.viewport_point_for(0, x, y)


def test_the_conversions_do_not_depend_on_the_device_pixel_ratio(
    loaded_view: PdfView, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DPR が変わっても論理座標の変換結果は変わらない。

    位置を物理ピクセルから計算していると、ここで 1.5 倍ずれる。
    """
    point = loaded_view.viewport_point_for(0, 0.25, 0.75)
    position = loaded_view.page_position_at(page_point(loaded_view, 0, 0.25, 0.75))
    assert point is not None
    assert position is not None

    monkeypatch.setattr(loaded_view, "devicePixelRatioF", lambda: 1.5)

    scaled_point = loaded_view.viewport_point_for(0, 0.25, 0.75)
    scaled_position = loaded_view.page_position_at(page_point(loaded_view, 0, 0.25, 0.75))
    assert scaled_point is not None
    assert scaled_position is not None
    assert_close(scaled_point, point, tolerance=1e-9)
    assert scaled_position == position


# ------------------------------------------------------------------ コレクションの受け渡し
def test_the_marks_are_kept_as_a_snapshot(loaded_view: PdfView) -> None:
    """呼び出し側のリストを持ち続けない。"""
    first = make_mark(id=1)
    marks = [first]

    loaded_view.set_study_marks(marks)
    marks.append(make_mark(id=2, x=0.1, y=0.1))

    assert loaded_view.study_marks == (first,)


def test_the_marks_are_ordered_by_id(loaded_view: PdfView) -> None:
    """保持する並びは呼び出し順ではなく `id` 昇順。"""
    loaded_view.set_study_marks([make_mark(id=3), make_mark(id=1), make_mark(id=2)])

    assert [mark.id for mark in loaded_view.study_marks] == [1, 2, 3]


def test_setting_marks_repaints_the_viewport(loaded_view: PdfView) -> None:
    """設定したらビューポートを描き直す。"""
    spy = UpdateSpy(loaded_view)

    loaded_view.set_study_marks([make_mark()])

    assert spy.rects == [None]


def test_setting_marks_does_no_render_work(
    loaded_view: PdfView,
    cache: RenderCache,
    service: RecordingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """マークの設定と描画で、レンダリングも色変換もキャッシュも動かない。

    Phase 2 との境界。オーバーレイはページ画像の外側にあり、
    `RenderCache` にも `DisplayCache` にも入らない。
    """
    put_image(cache, loaded_view, 0, Qt.GlobalColor.red)
    loaded_view.set_page_color_mode(PageColorMode.INVERT)
    service.repeat_last_request()
    calls = count_transforms(monkeypatch)
    requests = len(service.requests)
    generation = service.generation
    outstanding = service.outstanding_keys
    raw_size = len(cache)
    display_size = len(service.display_cache)

    loaded_view.set_study_marks([make_mark(x=0.5, y=0.5), make_mark(id=2, x=0.1, y=0.9)])
    render_view(loaded_view)

    assert calls == []
    assert len(service.requests) == requests
    assert service.generation == generation
    assert service.outstanding_keys == outstanding
    assert len(cache) == raw_size
    assert len(service.display_cache) == display_size


def test_setting_marks_does_not_move_the_view(loaded_view: PdfView) -> None:
    """倍率・倍率モード・現在ページ・スクロール位置のどれも動かさない。"""
    loaded_view.fit_width()
    loaded_view.verticalScrollBar().setValue(500)
    zoom = loaded_view.zoom
    position = loaded_view.verticalScrollBar().value()
    page = loaded_view.current_page

    loaded_view.set_study_marks([make_mark(x=0.5, y=0.5)])

    assert loaded_view.zoom == pytest.approx(zoom)
    assert loaded_view.zoom_mode is ZoomMode.FIT_WIDTH
    assert loaded_view.verticalScrollBar().value() == position
    assert loaded_view.current_page == page


def test_setting_the_same_marks_again_is_harmless(loaded_view: PdfView) -> None:
    """同じ内容を設定し直しても、描き直す以上のことは起きない。"""
    marks = [make_mark(id=1), make_mark(id=2, x=0.2, y=0.2)]
    loaded_view.set_study_marks(marks)

    loaded_view.set_study_marks(marks)

    assert [mark.id for mark in loaded_view.study_marks] == [1, 2]


# ------------------------------------------------------------------ オーバーレイの描画
def test_the_badge_is_centered_on_the_normalized_position(fitted_view: PdfView) -> None:
    """バッジの中心が正規化座標の指す点に来る。"""
    fitted_view.set_study_marks([make_mark(x=0.25, y=0.75)])

    assert_close(painted_badge_center(fitted_view), page_point(fitted_view, 0, 0.25, 0.75))


def test_the_badge_follows_the_zoom(fitted_view: PdfView) -> None:
    """ズームするとバッジはページの新しい位置へ移る。"""
    fitted_view.set_study_marks([make_mark(x=0.25, y=0.25)])
    assert_close(painted_badge_center(fitted_view), page_point(fitted_view, 0, 0.25, 0.25))

    fitted_view.set_zoom(fitted_view.zoom * 1.5)

    assert_close(painted_badge_center(fitted_view), page_point(fitted_view, 0, 0.25, 0.25))


def test_the_badge_keeps_its_size_across_zooms(fitted_view: PdfView) -> None:
    """バッジ自体の大きさは画面上でほぼ一定（ページと一緒には拡大しない）。"""
    fitted_view.set_study_marks([make_mark(x=0.5, y=0.5)])

    sizes: list[float] = []
    for zoom in (MIN_ZOOM, 1.0, 4.0):
        fitted_view.set_zoom(zoom)
        found = fill_pixels(fitted_view)
        sizes.append(float(found[:, 0].max() - found[:, 0].min()))

    assert sizes == pytest.approx([sizes[0]] * len(sizes), abs=2)
    assert sizes[0] == pytest.approx(BADGE_HEIGHT, abs=4)


def test_the_badge_follows_the_scroll(fitted_view: PdfView) -> None:
    """スクロールするとバッジのビューポート位置は動くが、ドメインの値は変わらない。"""
    mark = make_mark(x=0.5, y=0.4)
    fitted_view.set_study_marks([mark])
    before = painted_badge_center(fitted_view)

    fitted_view.verticalScrollBar().setValue(fitted_view.verticalScrollBar().value() + 40)

    after = painted_badge_center(fitted_view)
    assert after.y() == pytest.approx(before.y() - 40, abs=POSITION_TOLERANCE)
    assert fitted_view.study_marks == (mark,)


def test_only_the_marks_of_the_visible_pages_are_painted(fitted_view: PdfView) -> None:
    """見えていないページのマークは描かない。"""
    fitted_view.set_study_marks([make_mark(page=2, x=0.5, y=0.5)])

    assert 2 not in fitted_view.visible_pages()
    assert badge_count_pixels(fitted_view) == 0


def test_the_exact_mistake_count_is_shown(fitted_view: PdfView) -> None:
    """間違えた回数は整数のまま出す。「3+」のような丸めをしない。

    バッジの幅は数字の文字幅から決まるので、桁が増えると横に広がる。
    10 以上でも形が壊れない（描画が成立する）ことも一緒に見る。

    グリフそのものが塗り潰されるかは環境のフォント次第（オフスクリーンの
    テスト環境にはフォントが無い）ので、ここでは見ない。実機での確認は
    ネイティブプラグインで行う。
    """
    widths: list[float] = []
    for count in (1, 4, 10):
        fitted_view.set_study_marks([make_mark(x=0.5, y=0.5, count=count)])
        found = fill_pixels(fitted_view)
        assert len(found) > 0
        widths.append(float(found[:, 1].max() - found[:, 1].min()))

    assert widths[2] > widths[0], "桁が増えてもバッジが広がらない（数字が入りきらない）"


def test_the_badge_is_painted_over_the_page_image(fitted_view: PdfView, cache: RenderCache) -> None:
    """バッジはページ画像より後に描く。"""
    put_image(cache, fitted_view, 0, Qt.GlobalColor.red)

    assert badge_count_pixels(fitted_view) == 0
    fitted_view.set_study_marks([make_mark(x=0.5, y=0.5)])
    assert badge_count_pixels(fitted_view) > 0


# ------------------------------------------------------------------ ヒットテスト
def test_a_point_on_the_badge_hits(loaded_view: PdfView) -> None:
    """バッジの上ならヒットする。"""
    mark = make_mark(x=0.25, y=0.75)
    loaded_view.set_study_marks([mark])

    assert loaded_view.study_marks_at(page_point(loaded_view, 0, 0.25, 0.75)) == [mark]


def test_a_point_outside_the_badge_does_not_hit(loaded_view: PdfView) -> None:
    """バッジの外なら空。いちばん近いマークを返したりしない。"""
    loaded_view.set_study_marks([make_mark(x=0.5, y=0.5)])
    anchor = page_point(loaded_view, 0, 0.5, 0.5)

    assert loaded_view.study_marks_at(anchor + QPointF(60.0, 0.0)) == []
    assert loaded_view.study_marks_at(anchor + QPointF(0.0, 60.0)) == []


def test_the_hit_area_does_not_grow_with_the_zoom(loaded_view: PdfView) -> None:
    """当たり判定の大きさも画面上でほぼ一定。"""
    loaded_view.set_study_marks([make_mark(x=0.5, y=0.5)])

    for zoom in (MIN_ZOOM, 1.0, 4.0):
        loaded_view.set_zoom(zoom)
        anchor = page_point(loaded_view, 0, 0.5, 0.5)

        assert loaded_view.study_marks_at(anchor + QPointF(0.0, BADGE_HEIGHT / 2 - 2)) != []
        assert loaded_view.study_marks_at(anchor + QPointF(0.0, BADGE_HEIGHT / 2 + 4)) == []


def test_duplicate_marks_are_all_returned_in_a_stable_order(loaded_view: PdfView) -> None:
    """同じ位置の複数マークはまとめず、決定的な順（上に描かれたものから）で返す。"""
    first = make_mark(id=1, x=0.3, y=0.3, count=1)
    second = make_mark(id=2, x=0.3, y=0.3, count=2)
    loaded_view.set_study_marks([first, second])

    hits = loaded_view.study_marks_at(page_point(loaded_view, 0, 0.3, 0.3))

    assert hits == [second, first]
    assert loaded_view.study_marks == (first, second)


def test_marks_on_other_pages_are_not_hit(loaded_view: PdfView) -> None:
    """同じ比率でも別ページのマークはヒットしない。"""
    loaded_view.set_study_marks([make_mark(page=1, x=0.5, y=0.5)])

    assert loaded_view.study_marks_at(page_point(loaded_view, 0, 0.5, 0.5)) == []


def test_hit_testing_without_marks_is_empty(loaded_view: PdfView) -> None:
    """マークが無ければどこを押しても空。"""
    assert loaded_view.study_marks_at(page_point(loaded_view, 0, 0.5, 0.5)) == []


def test_hit_testing_without_a_document_is_empty(view: PdfView) -> None:
    """ドキュメントが無くても落ちない。"""
    assert view.study_marks_at(QPointF(10.0, 10.0)) == []


# ------------------------------------------------------------------ ドキュメントの切り替え
def test_switching_documents_drops_the_marks(
    loaded_view: PdfView, controller: DocumentController
) -> None:
    """PDF を切り替えたら前の PDF のマークは即座に消える。"""
    loaded_view.fit_page()
    loaded_view.set_study_marks([make_mark(x=0.5, y=0.5)])
    assert badge_count_pixels(loaded_view) > 0

    loaded_view.set_document(controller.document, controller.page_sizes())
    loaded_view.fit_page()

    assert loaded_view.study_marks == ()
    assert badge_count_pixels(loaded_view) == 0
    assert loaded_view.study_marks_at(page_point(loaded_view, 0, 0.5, 0.5)) == []


def test_going_back_to_the_first_document_does_not_restore_the_marks(
    loaded_view: PdfView, controller: DocumentController, two_page_pdf: Path
) -> None:
    """A → B → A でも、前に設定したマークをビューが勝手に戻さない。"""
    loaded_view.set_study_marks([make_mark(x=0.5, y=0.5)])

    other = DocumentController()
    other.open(two_page_pdf)
    try:
        loaded_view.set_document(other.document, other.page_sizes())
    finally:
        other.close()
    loaded_view.set_document(controller.document, controller.page_sizes())

    assert loaded_view.study_marks == ()


def test_clearing_the_document_drops_the_marks(loaded_view: PdfView) -> None:
    """クリアでもマークを捨てる。"""
    loaded_view.set_study_marks([make_mark()])

    loaded_view.clear_document()

    assert loaded_view.study_marks == ()


# ------------------------------------------------------------------ 保存済みの古いページ番号
def test_a_mark_beyond_the_page_count_is_ignored(view: PdfView, single_page_pdf: Path) -> None:
    """ページ数を超えるマークでも落ちず、描かれず、ヒットもしない。

    同じパスの PDF が差し替えられた場合に起こりうる。最終ページへ丸めない。
    """
    single = DocumentController()
    single.open(single_page_pdf)
    try:
        view.set_document(single.document, single.page_sizes())
    finally:
        single.close()
    view.fit_page()

    view.set_study_marks([make_mark(page=5, x=0.5, y=0.5)])

    assert badge_count_pixels(view) == 0
    assert view.study_marks_at(page_point(view, 0, 0.5, 0.5)) == []
    assert view.viewport_point_for(5, 0.5, 0.5) is None
    # 預かったものは捨てない。表示から外すだけ。
    assert len(view.study_marks) == 1


# ------------------------------------------------------------------ 外観からの独立
@pytest.mark.parametrize(
    "mode", [PageColorMode.ORIGINAL, PageColorMode.INVERT, PageColorMode.SMART_DARK]
)
def test_the_marks_do_not_depend_on_the_page_color_mode(
    fitted_view: PdfView, mode: PageColorMode
) -> None:
    """ページの色変換を変えても、マークの位置も当たり判定も変わらない。

    バッジはページ画像の色変換を通らないので、塗り色もそのまま。
    """
    mark = make_mark(x=0.25, y=0.4)
    fitted_view.set_study_marks([mark])
    expected = page_point(fitted_view, 0, 0.25, 0.4)

    fitted_view.set_page_color_mode(mode)

    assert fitted_view.study_marks == (mark,)
    assert fitted_view.study_marks_at(expected) == [mark]
    assert_close(painted_badge_center(fitted_view), expected)


@pytest.mark.parametrize("theme", [CanvasTheme.BLACK, CanvasTheme.WHITE, CanvasTheme.DARK_GRAY])
def test_the_marks_do_not_depend_on_the_canvas_theme(
    fitted_view: PdfView, theme: CanvasTheme
) -> None:
    """キャンバスの色を変えてもマークは変わらない。"""
    mark = make_mark(x=0.5, y=0.5, count=3)
    fitted_view.set_study_marks([mark])

    fitted_view.set_canvas_theme(theme)

    assert fitted_view.study_marks == (mark,)
    assert fitted_view.study_marks_at(page_point(fitted_view, 0, 0.5, 0.5)) == [mark]


def test_the_badge_is_visible_on_a_white_and_on_a_black_page(
    fitted_view: PdfView, cache: RenderCache
) -> None:
    """白いページでも黒いページでもバッジが見える。"""
    put_image(cache, fitted_view, 0, Qt.GlobalColor.white)
    fitted_view.set_study_marks([make_mark(x=0.5, y=0.5)])

    for mode in (PageColorMode.ORIGINAL, PageColorMode.INVERT, PageColorMode.SMART_DARK):
        fitted_view.set_page_color_mode(mode)
        assert badge_count_pixels(fitted_view) > 0, f"{mode} でバッジが見えない"


def test_a_page_color_change_keeps_the_marks(fitted_view: PdfView) -> None:
    """`PageColorMode` を変えてもコレクションを捨てない。"""
    loaded = [make_mark(id=1), make_mark(id=2, x=0.1, y=0.1)]
    fitted_view.set_study_marks(loaded)

    fitted_view.set_page_color_mode(PageColorMode.SMART_DARK)

    assert list(fitted_view.study_marks) == loaded


# ------------------------------------------------------------------ 件数が増えたとき
def test_a_large_collection_is_indexed_by_page(loaded_view: PdfView) -> None:
    """件数が増えても、描画とヒットテストは対象ページの分だけを見る。"""
    marks = [
        make_mark(id=index + 1, page=index % 3, x=0.5, y=0.5, count=index % 7 + 1)
        for index in range(1500)
    ]

    loaded_view.set_study_marks(marks)

    assert len(loaded_view.study_marks) == 1500
    # 先頭ページに乗っているのは3件に1件。ヒットもその分だけ。
    hits = loaded_view.study_marks_at(page_point(loaded_view, 0, 0.5, 0.5))
    assert len(hits) == 500
    assert [mark.id for mark in hits] == sorted((mark.id for mark in hits), reverse=True)


# ------------------------------------------------------------------ 索引（純粋なロジック）
def test_the_index_groups_marks_by_page() -> None:
    """ページ番号ごとに引ける。"""
    first = make_mark(id=1, page=0)
    second = make_mark(id=2, page=2)
    index = StudyMarkIndex([second, first])

    assert index.for_page(0) == (first,)
    assert index.for_page(2) == (second,)
    assert index.for_page(1) == ()
    assert len(index) == 2


def test_an_empty_index_has_no_marks() -> None:
    """空の索引。"""
    index = StudyMarkIndex()

    assert index.marks == ()
    assert index.for_page(0) == ()


def test_the_index_sorts_by_id() -> None:
    """並びは `id` 昇順に正規化される。"""
    index = StudyMarkIndex([make_mark(id=5), make_mark(id=2), make_mark(id=9)])

    assert [mark.id for mark in index.marks] == [2, 5, 9]
