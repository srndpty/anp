"""`anp.pdf.layout` のテスト。

`QApplication` を使わずに動く決定的なテスト。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPointF, QRectF, QSizeF

from anp.pdf.layout import LayoutMetrics, PageLayout

# 検算しやすいよう、幅 100 / 高さ 200 のページを使う。
PAGE = QSizeF(100.0, 200.0)
METRICS = LayoutMetrics(page_gap=10.0, margin=20.0)


def make_layout(count: int = 3, metrics: LayoutMetrics = METRICS) -> PageLayout:
    """同じ大きさのページを並べたレイアウトを作る。"""
    return PageLayout([QSizeF(PAGE) for _ in range(count)], metrics)


def test_empty_document_is_rejected() -> None:
    """ページが無いレイアウトは作れない。"""
    with pytest.raises(ValueError, match="ページのない"):
        PageLayout([])


def test_content_size_at_unit_zoom() -> None:
    """等倍のときの全体サイズ。"""
    layout = make_layout(3)

    size = layout.content_size(1.0)

    # 幅 = 100 + 余白 20*2、高さ = 200*3 + 隙間 10*2 + 余白 20*2
    assert size.width() == pytest.approx(140.0)
    assert size.height() == pytest.approx(660.0)


def test_page_positions_are_stacked_with_gaps() -> None:
    """ページが隙間を挟んで縦に並ぶ。"""
    layout = make_layout(3)

    assert layout.page_rect(0, 1.0).top() == pytest.approx(20.0)
    assert layout.page_rect(1, 1.0).top() == pytest.approx(230.0)
    assert layout.page_rect(2, 1.0).top() == pytest.approx(440.0)


def test_pages_are_horizontally_centered() -> None:
    """幅の異なるページはそれぞれ中央に置かれる。"""
    layout = PageLayout([QSizeF(100.0, 200.0), QSizeF(50.0, 200.0)], METRICS)

    wide = layout.page_rect(0, 1.0)
    narrow = layout.page_rect(1, 1.0)

    assert wide.center().x() == pytest.approx(narrow.center().x())
    assert narrow.left() == pytest.approx(45.0)


# ------------------------------------------------------------------ ズーム
def test_page_size_scales_with_zoom() -> None:
    """ページの寸法はズーム倍率に比例する。"""
    layout = make_layout(2)

    rect = layout.page_rect(0, 2.0)

    assert rect.width() == pytest.approx(200.0)
    assert rect.height() == pytest.approx(400.0)


@pytest.mark.parametrize("zoom", [0.25, 1.0, 2.0, 8.0])
def test_gap_and_margin_do_not_scale(zoom: float) -> None:
    """隙間と余白はズームしても一定のピクセル数を保つ。

    ページの寸法と一緒に拡大すると、高倍率で隙間が異常に開いてしまう。
    """
    layout = make_layout(3)

    gap = layout.page_rect(1, zoom).top() - layout.page_rect(0, zoom).bottom()
    assert gap == pytest.approx(METRICS.page_gap)

    assert layout.page_rect(0, zoom).top() == pytest.approx(METRICS.margin)


def test_content_height_matches_last_page_bottom() -> None:
    """全体の高さが最後のページの下端＋余白と一致する。"""
    layout = make_layout(4)

    for zoom in (0.5, 1.0, 3.0):
        expected = layout.page_rect(3, zoom).bottom() + METRICS.margin
        assert layout.content_size(zoom).height() == pytest.approx(expected)


# ------------------------------------------------------------------ 可視範囲
def test_visible_pages_within_single_page() -> None:
    """1ページに収まる viewport ではそのページだけが返る。"""
    layout = make_layout(3)

    visible = layout.visible_pages(QRectF(0, 50, 140, 100), 1.0)

    assert visible == range(0, 1)


def test_visible_pages_spanning_boundary() -> None:
    """ページ境界をまたぐと両方が返る。"""
    layout = make_layout(3)

    # 1ページ目の下端は 220、2ページ目の上端は 230。
    visible = layout.visible_pages(QRectF(0, 200, 140, 60), 1.0)

    assert visible == range(0, 2)


def test_visible_pages_inside_gap_is_empty() -> None:
    """隙間だけが見えている場合はどのページとも交差しない。

    1ページ目の下端は 220、2ページ目の上端は 230。その間に収まる viewport は
    背景しか映していないので、可視ページは無い。
    """
    layout = make_layout(3)

    assert layout.visible_pages(QRectF(0, 222.0, 140, 5.0), 1.0) == range(1, 1)


def test_visible_pages_above_content_is_empty() -> None:
    """コンテンツより上の領域には可視ページが無い。"""
    layout = make_layout(3)

    assert layout.visible_pages(QRectF(0, 0, 140, 10), 1.0) == range(0, 0)


def test_visible_pages_below_content_is_empty() -> None:
    """コンテンツより下の領域には可視ページが無い。"""
    layout = make_layout(3)

    assert layout.visible_pages(QRectF(0, 700, 140, 100), 1.0) == range(0, 0)


def test_visible_pages_covers_everything_when_zoomed_out() -> None:
    """全体が見えるほど縮小すれば全ページが返る。"""
    layout = make_layout(5)

    visible = layout.visible_pages(QRectF(0, 0, 200, 10_000), 1.0)

    assert visible == range(0, 5)


def test_visible_pages_does_not_scan_all_pages() -> None:
    """ページ数が多くても走査量は可視ページ数に比例する。

    `paintEvent` から呼ばれるため、全ページ走査になっていないことを
    アクセス回数で確かめる。
    """
    count = 2000
    sizes = [QSizeF(PAGE) for _ in range(count)]
    accesses = 0

    class CountingList(list[QSizeF]):
        """要素アクセスの回数を数えるリスト。"""

        def __getitem__(self, index: Any) -> Any:
            nonlocal accesses
            accesses += 1
            return super().__getitem__(index)

    layout = PageLayout(CountingList(sizes), METRICS)
    layout.page_rect(0, 1.0)  # ここまでの分は数えない
    accesses = 0

    visible = layout.visible_pages(QRectF(0, 100_000, 140, 300), 1.0)

    assert len(visible) <= 3
    assert accesses < 20, f"ページ数 {count} に対して {accesses} 回の走査が発生している"


# ------------------------------------------------------------------ 現在ページ
def test_current_page_is_the_most_overlapping_one() -> None:
    """重なりが最大のページが現在ページになる。"""
    layout = make_layout(3)

    # 1ページ目は 200〜220 の 20px、2ページ目は 230〜300 の 70px 見えている。
    assert layout.current_page(QRectF(0, 200, 140, 100), 1.0) == 1


def test_current_page_prefers_larger_overlap_not_first_visible() -> None:
    """わずかに見えている先頭ページではなく、大きく見えている方を選ぶ。"""
    layout = make_layout(3)

    # 1ページ目は下端 1px だけ、2ページ目は大きく見えている。
    assert layout.current_page(QRectF(0, 219, 140, 100), 1.0) == 1


def test_current_page_in_gap_returns_previous_page() -> None:
    """隙間にいるときは直前のページを返す。

    可視ページが無くても現在ページは決まらなければならない。
    """
    layout = make_layout(3)

    assert layout.visible_pages(QRectF(0, 222, 140, 5), 1.0) == range(1, 1)
    assert layout.current_page(QRectF(0, 222, 140, 5), 1.0) == 0


def test_current_page_is_clamped_to_document() -> None:
    """コンテンツの外にいても範囲内のページを返す。"""
    layout = make_layout(3)

    assert layout.current_page(QRectF(0, -500, 140, 10), 1.0) == 0
    assert layout.current_page(QRectF(0, 10_000, 140, 10), 1.0) == 2


# ------------------------------------------------------------------ 要求範囲
def test_render_window_extends_by_one_page() -> None:
    """レンダリング範囲は可視ページの前後1ページ。"""
    layout = make_layout(10)

    window = layout.render_window(QRectF(0, 660, 140, 100), 1.0)

    assert layout.visible_pages(QRectF(0, 660, 140, 100), 1.0) == range(3, 4)
    assert window == range(2, 5)


def test_render_window_is_clamped_at_both_ends() -> None:
    """文書の端では範囲がはみ出さない。"""
    layout = make_layout(3)

    assert layout.render_window(QRectF(0, 0, 140, 100), 1.0).start == 0
    assert layout.render_window(QRectF(0, 500, 140, 200), 1.0).stop == 3


def test_render_window_is_not_empty_in_gap() -> None:
    """隙間にいてもレンダリング対象が無くならない。"""
    layout = make_layout(5)

    window = layout.render_window(QRectF(0, 222, 140, 5), 1.0)

    assert len(window) > 0


# ------------------------------------------------------------------ 座標変換
@pytest.mark.parametrize("zoom", [0.5, 1.0, 4.0])
def test_normalized_coordinates_are_zoom_independent(zoom: float) -> None:
    """同じページ上の点は、ズーム倍率が変わっても同じ正規化座標になる。

    アノテーションの位置がズームで壊れないことの土台。
    """
    layout = make_layout(3)
    rect = layout.page_rect(1, zoom)
    point = QPointF(rect.left() + rect.width() * 0.25, rect.top() + rect.height() * 0.75)

    normalized = layout.to_normalized(1, point, zoom)

    assert normalized.x() == pytest.approx(0.25)
    assert normalized.y() == pytest.approx(0.75)


@pytest.mark.parametrize("zoom", [0.5, 1.0, 4.0])
def test_normalized_round_trip(zoom: float) -> None:
    """正規化座標とコンテンツ座標を往復しても元に戻る。"""
    layout = make_layout(3)
    original = QPointF(0.3, 0.6)

    content = layout.from_normalized(2, original, zoom)
    result = layout.to_normalized(2, content, zoom)

    assert result.x() == pytest.approx(original.x())
    assert result.y() == pytest.approx(original.y())
