"""学習マーク（StudyMark）のオーバーレイ描画とヒットテストの部品。

`PdfView` から使う小さな道具だけを置く。**SQLite にも
`StudyMarkRepository` にも依存しない。** 参照するのは
`anp.storage.study_mark.StudyMark` というドメインの値だけ。

ここが受け持つのは次の3つ。

- `PagePosition`: 「どのページのどこか」を表す最小の値
- `StudyMarkIndex`: 呼び出し側のコレクションを写し取った不変のスナップショットと、
  ページ番号ごとの索引
- バッジの形（`badge_path()`）と描画（`draw_badge()`）

**バッジの形は `badge_path()` だけが決める。** 描画とヒットテストで別々の
数値を書くと、見えている位置と当たり判定が静かにずれる。

バッジは **Qt の論理座標** で組み立てる。`devicePixelRatio` を自分で掛けない
（掛けると高 DPI でバッジだけ2倍になる）。ページの拡大率も掛けない。
バッジは画面上でおおむね一定の大きさを保ち、**位置（アンカー）だけ**が
ページの配置に追随する。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from anp.storage.study_mark import StudyMark


@dataclass(frozen=True, slots=True)
class PagePosition:
    """ページ上の位置（ページ番号 + 正規化座標）。

    正規化座標の向きは `StudyMark` と同じで、(0, 0) がページ左上、
    (1, 1) がページ右下。ビューポートのピクセル座標は持たない。
    """

    page_index: int
    x_norm: float
    y_norm: float


# バッジの高さ（論理ピクセル）。丸みの半径もここから決まる。
BADGE_HEIGHT = 20.0

# 数字の左右に確保する余白（論理ピクセル）。桁が増えると横に伸びる。
_BADGE_PADDING = 6.0

_BADGE_BORDER_WIDTH = 2.0
_BADGE_FONT_POINT_SIZE = 9.0

# 白いページでも黒いページでも見えるよう、下地の明暗どちらとも違う色を
# 塗り、さらに濃い縁を付ける。具体的な色は公開契約ではない。
BADGE_FILL_COLOR = QColor(0xFF, 0xC1, 0x07)
BADGE_BORDER_COLOR = QColor(0x1A, 0x1A, 0x1A)
_BADGE_TEXT_COLOR = QColor(0x1A, 0x1A, 0x1A)


def _badge_font() -> QFont:
    """バッジの数字に使うフォント。

    `QFont` は `QGuiApplication` が必要なので、モジュールの読み込み時では
    なく呼ばれた時に作る。
    """
    font = QFont()
    font.setPointSizeF(_BADGE_FONT_POINT_SIZE)
    font.setBold(True)
    return font


def badge_path(anchor: QPointF, mistake_count: int) -> QPainterPath:
    """バッジの形（ビューポート座標）。

    `anchor` は `StudyMark` の正規化座標に対応するビューポート上の点で、
    バッジの **中心** になる。ページの端にアンカーがあるとバッジは
    ページからはみ出すが、アンカーを丸めてページ内へ押し込めたりはしない。

    間違えた回数はそのままの整数を出すので、桁数に応じて横に伸びる。
    高さが直径の丸から、幅の広い角丸（pill）へ連続的に変わるだけ。
    """
    text = str(mistake_count)
    width = max(
        BADGE_HEIGHT,
        QFontMetricsF(_badge_font()).horizontalAdvance(text) + _BADGE_PADDING * 2,
    )
    rect = QRectF(
        anchor.x() - width / 2,
        anchor.y() - BADGE_HEIGHT / 2,
        width,
        BADGE_HEIGHT,
    )
    path = QPainterPath()
    path.addRoundedRect(rect, BADGE_HEIGHT / 2, BADGE_HEIGHT / 2)
    return path


def draw_badge(painter: QPainter, anchor: QPointF, mistake_count: int) -> None:
    """バッジを1つ描く。

    `paintEvent` から呼ばれるが、行うのは軽いベクタ描画だけ。画素変換も
    画像の作り直しもしない。

    呼び出し側のペン・ブラシ・フォントを壊さないよう、状態は保存して戻す。
    """
    path = badge_path(anchor, mistake_count)
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(BADGE_BORDER_COLOR, _BADGE_BORDER_WIDTH))
        painter.setBrush(BADGE_FILL_COLOR)
        painter.drawPath(path)

        painter.setPen(_BADGE_TEXT_COLOR)
        painter.setFont(_badge_font())
        # 「3+」や星の数へ丸めない。P3-1 のドメイン契約どおり整数のまま出す。
        painter.drawText(
            path.boundingRect(),
            Qt.AlignmentFlag.AlignCenter,
            str(mistake_count),
        )
    finally:
        painter.restore()


class StudyMarkIndex:
    """表示中の学習マークのスナップショットと、ページ番号ごとの索引。

    **呼び出し側のリストを持ち続けない。** 受け取った時点で写し取り、
    以後は不変のタプルとして扱う。呼び出し側が後からリストへ足しても、
    描画中の内容が黙って変わることはない。

    並びは `id` の昇順に固定する。描画順・ヒットテスト順を呼び出し側の
    渡し方に左右させないため。

    ページ番号ごとの索引を持つので、描画は見えているページの分だけを
    引けばよい。マークが数千件あっても毎回の描画で全件を走査しない。
    """

    def __init__(self, marks: Iterable[StudyMark] = ()) -> None:
        self._marks: tuple[StudyMark, ...] = tuple(sorted(marks, key=lambda mark: mark.id))

        by_page: dict[int, list[StudyMark]] = {}
        for mark in self._marks:
            by_page.setdefault(mark.page_index, []).append(mark)
        self._by_page: dict[int, tuple[StudyMark, ...]] = {
            page: tuple(page_marks) for page, page_marks in by_page.items()
        }

    @property
    def marks(self) -> tuple[StudyMark, ...]:
        """保持している全マーク（`id` 昇順）。

        現在のドキュメントに存在しないページのマークもここには残る。
        描画とヒットテストの対象から外すのは `PdfView` の役目。
        """
        return self._marks

    def for_page(self, page_index: int) -> tuple[StudyMark, ...]:
        """そのページのマーク（描画順 = `id` 昇順）。"""
        return self._by_page.get(page_index, ())

    def __len__(self) -> int:
        return len(self._marks)
