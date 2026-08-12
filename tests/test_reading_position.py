"""`anp.pdf.reading_position` のテスト。

`QApplication` もウィジェットも使わずに動く決定的なテスト。読書位置の
往復（保存 → 復元）がずれないことを、GUI のタイミングから切り離して
確かめる。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRectF, QSizeF

from anp.pdf.layout import LayoutMetrics, PageLayout
from anp.pdf.reading_position import (
    ReadingPosition,
    page_top_offset,
    reading_position_at,
    scroll_top_for,
)

PAGE = QSizeF(100.0, 200.0)
METRICS = LayoutMetrics(page_gap=10.0, margin=20.0)
VIEWPORT_HEIGHT = 150.0


def make_layout(count: int = 3) -> PageLayout:
    """同じ大きさのページを並べたレイアウト。"""
    return PageLayout([QSizeF(PAGE) for _ in range(count)], METRICS)


def viewport_at(top: float) -> QRectF:
    """上端が `top` にあるビューポート（コンテンツ座標）。"""
    return QRectF(0.0, top, 100.0, VIEWPORT_HEIGHT)


@pytest.mark.parametrize("page", [0, 1, 2])
def test_a_page_top_is_exactly_zero(page: int) -> None:
    """ページを開いた位置は、そのページの 0.0 として表せる。

    ここがページ上端（余白を含まない位置）だと、`go_to_page()` の残す
    余白1つぶんが負の比率になり、0.0 へ丸められる。復元すると余白の
    ぶんだけ先へ進む。
    """
    layout = make_layout()

    position = reading_position_at(layout, viewport_at(page_top_offset(layout, page, 1.0)), 1.0)

    assert position.page_index == page
    assert position.y_norm == pytest.approx(0.0)


@pytest.mark.parametrize(
    "top",
    [0.0, 5.0, 60.0, 199.0, 210.0, 215.0, 219.0, 220.0, 300.0, 430.0, 500.0],
)
def test_the_scroll_position_round_trips(top: float) -> None:
    """ページ末尾の隙間を除けば、保存して復元すれば同じ位置に戻る。

    区切りをページ上端に置くと、ページの先頭付近と隙間で 0.0 / 1.0 へ
    丸められ、その分だけずれる。
    """
    layout = make_layout()

    position = reading_position_at(layout, viewport_at(top), 1.0)
    restored = scroll_top_for(layout, position, 1.0)

    assert restored == pytest.approx(top)


@pytest.mark.parametrize("top", [200.5, 205.0, 209.5, 410.5, 415.0, 419.5])
def test_the_page_tail_is_rounded_back_by_at_most_a_page_gap(top: float) -> None:
    """ページ末尾の隙間ぶんだけは、復元すると手前へ丸められる。

    保存できるのは `(page_index, 0.0〜1.0)` だけで、ページの区切りは
    ページ高さより `page_gap` ぶん長い。はみ出す分は 1.0 になるので、
    **ページ末尾の `page_gap` px は表現できない**。これは仕様。
    """
    layout = make_layout()

    position = reading_position_at(layout, viewport_at(top), 1.0)
    restored = scroll_top_for(layout, position, 1.0)

    assert position.y_norm == pytest.approx(1.0)
    assert restored is not None
    assert 0.0 < top - restored <= METRICS.page_gap


def test_the_round_trip_never_moves_forward() -> None:
    """スクロールできるどの位置でも、復元して先へ進むことはない。

    先へ進む向きに丸めると、再起動のたびに読んでいない場所へ進んでしまう。
    戻る側は最大でもページ末尾の隙間（`page_gap`）まで。

    走査するのは実際にスクロールできる範囲だけ（ビューポートの上端は
    「全体の高さ − ビューポートの高さ」より下へは行けない）。
    """
    layout = make_layout()
    limit = METRICS.page_gap
    reachable = int(layout.content_size(1.0).height() - VIEWPORT_HEIGHT)

    for step in range(reachable + 1):
        top = float(step)
        position = reading_position_at(layout, viewport_at(top), 1.0)
        restored = scroll_top_for(layout, position, 1.0)
        assert restored is not None
        # 浮動小数点の丸め（1e-15 程度）は「進んだ」に数えない。
        assert -1e-9 <= top - restored <= limit, f"top={top}"


def test_the_position_stays_within_the_unit_range() -> None:
    """比率は 0.0〜1.0 に収まる（保存の契約）。"""
    layout = make_layout()

    for step in range(0, 700, 7):
        position = reading_position_at(layout, viewport_at(float(step)), 1.0)
        assert 0.0 <= position.y_norm <= 1.0


def test_the_position_is_independent_of_the_zoom() -> None:
    """倍率が変わっても、同じ読書位置は同じ場所を指す。"""
    layout = make_layout()
    saved = reading_position_at(layout, viewport_at(page_top_offset(layout, 1, 1.0) + 50.0), 1.0)

    restored = scroll_top_for(layout, saved, 2.0)

    assert restored == pytest.approx(page_top_offset(layout, 1, 2.0) + 100.0)


def test_a_page_beyond_the_document_has_no_scroll_position() -> None:
    """いまの PDF に無いページは、最終ページへ丸めずに None。"""
    layout = make_layout(2)

    assert scroll_top_for(layout, ReadingPosition(page_index=5, y_norm=0.5), 1.0) is None
