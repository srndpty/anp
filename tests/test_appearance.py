"""`anp.ui.appearance` のテスト。

実際に描かれる色は環境（OS の配色設定）に依存するので、`apply_ui_theme()`
については **Qt へ何を要求したか** を検証する。オフスクリーンのプラット
フォームプラグインはカラースキームを実装していないため、`colorScheme()` を
読み返しても常に `Unknown` が返り、要求が届いたかを区別できない。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from anp.ui.appearance import CanvasTheme, UiTheme, apply_ui_theme, canvas_color

# 「指定を外す」要求を、カラースキームの要求と同じ列で記録するための印。
UNSET = "unset"


@pytest.fixture
def scheme_requests(qapp: QApplication) -> Iterator[list[object]]:
    """`QStyleHints` へのカラースキーム要求を記録する。

    `styleHints()` は同じオブジェクトを返すので、差し替えたメソッドは
    製品コードからの呼び出しにも効く。後始末で元へ戻す。
    """
    hints = qapp.styleHints()
    requests: list[object] = []

    original_set = hints.setColorScheme
    original_unset = hints.unsetColorScheme

    hints.setColorScheme = requests.append  # type: ignore[method-assign]
    hints.unsetColorScheme = lambda: requests.append(UNSET)  # type: ignore[method-assign]
    try:
        yield requests
    finally:
        hints.setColorScheme = original_set  # type: ignore[method-assign]
        hints.unsetColorScheme = original_unset  # type: ignore[method-assign]


# ------------------------------------------------------------------ キャンバス
@pytest.mark.parametrize(
    ("theme", "expected"),
    [
        (CanvasTheme.BLACK, "#000000"),
        (CanvasTheme.DARK_GRAY, "#525659"),
        (CanvasTheme.WHITE, "#ffffff"),
    ],
)
def test_each_canvas_theme_has_its_color(theme: CanvasTheme, expected: str) -> None:
    """キャンバスの色は3種類。既定のダークグレーは従来と同じ #525659。"""
    assert canvas_color(theme) == QColor(expected)


def test_every_canvas_theme_has_a_color() -> None:
    """色の定義漏れがない。"""
    for theme in CanvasTheme:
        assert canvas_color(theme).isValid()


# ------------------------------------------------------------------ UI テーマ
def test_dark_requests_the_dark_color_scheme(scheme_requests: list[object]) -> None:
    """ダークは Qt の暗いカラースキームを要求する。"""
    apply_ui_theme(UiTheme.DARK)

    assert scheme_requests == [Qt.ColorScheme.Dark]


def test_light_requests_the_light_color_scheme(scheme_requests: list[object]) -> None:
    """ライトは Qt の明るいカラースキームを要求する。"""
    apply_ui_theme(UiTheme.LIGHT)

    assert scheme_requests == [Qt.ColorScheme.Light]


def test_system_unsets_the_color_scheme(scheme_requests: list[object]) -> None:
    """システムは明示した指定を解除する。

    `setColorScheme(Qt.ColorScheme.Unknown)` でも解除できるが、意図が
    読み取りやすい `unsetColorScheme()` を使う方に固定しておく。
    """
    apply_ui_theme(UiTheme.SYSTEM)

    assert scheme_requests == [UNSET]


def test_system_restores_the_scheme_after_dark(scheme_requests: list[object]) -> None:
    """ダークにしてからシステムへ戻すと、指定を外す要求が出る。"""
    apply_ui_theme(UiTheme.DARK)
    apply_ui_theme(UiTheme.SYSTEM)

    assert scheme_requests == [Qt.ColorScheme.Dark, UNSET]


def test_the_system_palette_comes_back(qapp: QApplication) -> None:
    """SYSTEM → DARK → SYSTEM で元の配色に戻る。

    実際のパレットで確かめる。オフスクリーンでは明暗が動かないので、
    「戻ったこと」だけを見る（動いた先の色は環境依存）。
    """
    apply_ui_theme(UiTheme.SYSTEM)
    before = qapp.palette()

    apply_ui_theme(UiTheme.DARK)
    apply_ui_theme(UiTheme.SYSTEM)

    assert qapp.palette() == before
