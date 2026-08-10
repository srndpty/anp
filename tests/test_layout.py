"""`anp.pdf.layout` のテスト。

`QApplication` を使わずに動く決定的なテスト。
"""

from __future__ import annotations

from collections.abc import Sequence

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


def test_viewport_starting_exactly_at_a_page_bottom_excludes_it() -> None:
    """下端とちょうど一致するページは、重なりが 0 なので含めない。"""
    layout = make_layout(3)

    # 1ページ目の下端はちょうど 220。
    assert layout.page_rect(0, 1.0).bottom() == pytest.approx(220.0)
    assert layout.visible_pages(QRectF(0, 220.0, 140, 100), 1.0) == range(1, 2)


def test_viewport_ending_exactly_at_a_page_top_excludes_it() -> None:
    """上端とちょうど一致するページも含めない（境界の扱いを揃える）。"""
    layout = make_layout(3)

    # 2ページ目の上端はちょうど 230。
    assert layout.page_rect(1, 1.0).top() == pytest.approx(230.0)
    assert layout.visible_pages(QRectF(0, 100.0, 140, 130.0), 1.0) == range(0, 1)


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


class CountingLayout(PageLayout):
    """内部の作業量を数えられるレイアウト。

    `PageLayout` はページ寸法を自前のリストに複製するため、渡した
    シーケンスへのアクセスを数えても計測にならない。内部メソッドの
    呼び出し回数を数える。
    """

    def __init__(self, page_sizes: Sequence[QSizeF], metrics: LayoutMetrics) -> None:
        self.tops_rebuilds = 0
        self.page_bottom_calls = 0
        super().__init__(page_sizes, metrics)

    def _tops(self, zoom: float) -> list[float]:
        rebuilt = self._cached_zoom != zoom
        result = super()._tops(zoom)
        if rebuilt:
            self.tops_rebuilds += 1
        return result

    def _page_bottom(self, index: int, zoom: float) -> float:
        self.page_bottom_calls += 1
        return super()._page_bottom(index, zoom)


def test_visible_pages_does_not_scan_all_pages() -> None:
    """ページ数が多くても、1回の呼び出しの作業量は可視ページ数に比例する。

    `paintEvent` から毎回呼ばれるため、全ページ走査に退行していないことを
    内部メソッドの呼び出し回数で確かめる。
    """
    count = 2000
    layout = CountingLayout([QSizeF(PAGE) for _ in range(count)], METRICS)

    # 上端座標の組み立ては倍率ごとに一度だけ。ここでは計測対象外。
    layout.visible_pages(QRectF(0, 100_000, 140, 300), 1.0)
    layout.tops_rebuilds = 0
    layout.page_bottom_calls = 0

    visible = layout.visible_pages(QRectF(0, 100_000, 140, 300), 1.0)

    assert len(visible) <= 3
    assert layout.tops_rebuilds == 0, "同じ倍率なのに上端座標を作り直している"
    assert layout.page_bottom_calls <= 2, (
        f"ページ数 {count} に対して {layout.page_bottom_calls} 回のページ走査が発生している"
    )


def test_tops_are_rebuilt_once_per_zoom() -> None:
    """上端座標の組み立ては倍率が変わったときだけ行う。"""
    layout = CountingLayout([QSizeF(PAGE) for _ in range(100)], METRICS)

    for _ in range(5):
        layout.visible_pages(QRectF(0, 0, 140, 300), 1.0)
    assert layout.tops_rebuilds == 1

    layout.visible_pages(QRectF(0, 0, 140, 300), 2.0)
    assert layout.tops_rebuilds == 2


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


# ------------------------------------------------------------------ フィット倍率
def test_fit_width_zoom_accounts_for_the_margin() -> None:
    """幅フィットは余白を差し引いた幅にページを合わせる。"""
    layout = make_layout(3)

    # 使える幅 = 340 - 余白 20*2 = 300。ページ幅 100 なので 3.0 倍。
    zoom = layout.fit_width_zoom(0, 340.0)

    assert zoom == pytest.approx(3.0)
    assert layout.content_size(zoom).width() == pytest.approx(340.0)


def test_fit_page_zoom_uses_the_stricter_side() -> None:
    """ページフィットは縦横のうち厳しい方に合わせる。"""
    layout = make_layout(3)

    # 幅なら 3.0 倍だが、高さ 240 - 余白 40 = 200 に高さ 200 を収めるので 1.0 倍。
    zoom = layout.fit_page_zoom(0, QSizeF(340.0, 240.0))

    assert zoom == pytest.approx(1.0)
    assert zoom == pytest.approx(min(layout.fit_width_zoom(0, 340.0), 1.0))


def test_fit_page_zoom_fits_the_whole_page() -> None:
    """ページフィットした倍率ならページ全体がビューポートに収まる。"""
    layout = make_layout(3)
    viewport = QSizeF(500.0, 300.0)

    rect = layout.page_rect(0, layout.fit_page_zoom(0, viewport))

    assert rect.width() <= viewport.width()
    assert rect.height() <= viewport.height()


def test_fit_zoom_uses_the_requested_page() -> None:
    """基準ページごとに倍率が変わる。"""
    layout = PageLayout([QSizeF(100.0, 200.0), QSizeF(200.0, 200.0)], METRICS)

    assert layout.fit_width_zoom(0, 340.0) == pytest.approx(3.0)
    assert layout.fit_width_zoom(1, 340.0) == pytest.approx(1.5)


@pytest.mark.parametrize("viewport_width", [0.0, 40.0, -100.0])
def test_fit_width_zoom_is_zero_when_it_cannot_fit(viewport_width: float) -> None:
    """余白すら入らない幅では 0.0 を返し、丸めは呼び出し側に任せる。"""
    assert make_layout(1).fit_width_zoom(0, viewport_width) == 0.0


def test_fit_page_zoom_is_zero_when_it_cannot_fit() -> None:
    """高さが足りなければ 0.0。"""
    assert make_layout(1).fit_page_zoom(0, QSizeF(340.0, 10.0)) == 0.0


# ------------------------------------------------------------------ 点の属するページ
def test_page_at_finds_the_page_under_a_point() -> None:
    """ページ上の点からページ番号を引ける。"""
    layout = make_layout(3)
    rect = layout.page_rect(1, 1.0)

    assert layout.page_at(rect.center(), 1.0) == 1


def test_page_at_returns_none_in_the_gap() -> None:
    """ページ間の隙間や左右の余白では None。"""
    layout = make_layout(3)
    rect = layout.page_rect(0, 1.0)

    assert layout.page_at(QPointF(rect.center().x(), rect.bottom() + 5.0), 1.0) is None
    assert layout.page_at(QPointF(rect.left() - 5.0, rect.center().y()), 1.0) is None
    assert layout.page_at(QPointF(rect.center().x(), rect.top() - 5.0), 1.0) is None
