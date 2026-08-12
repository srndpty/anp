"""読書位置（どこまで読んだか）とスクロール位置の相互変換。

`PdfView` から切り出してある。ここにあるのは `PageLayout` と倍率だけで
決まる算術で、ウィジェットもスクロールバーも `QApplication` も要らない。
往復して同じ値に戻ることを、GUI 抜きで確かめられるようにするのが目的。

**基準はページの上端そのものではなく `page_top_offset()`**（ページ上端 −
余白）。`PdfView.go_to_page()` はページの上に余白を1つ残した位置へ送るので、
ページ上端を 0.0 にすると、その位置が「0 より手前」になって表現できない。
0.0 へ丸めてから復元すると、保存前と復元後で余白ぶんだけ見え方が変わる。

```
go_to_page(n) が作るスクロール位置 = page_top_offset(n) = y_norm 0.0
```

と定義すれば、`go_to_page()` → 保存 → 復元でスクロール位置が一致する。

**完全な逆変換ではない。** 保存できるのは `(page_index, 0.0〜1.0)` だけで、
ページ間の隙間は倍率で伸び縮みしないため、ページの区切り
（`page_top_offset(n)` から `page_top_offset(n+1)` まで）はページ高さより
`page_gap` ぶん長い。はみ出す分は 1.0 へ丸めるので、**ページ末尾の
`page_gap` px だけは、復元するとその分だけ手前に戻る**（既定値で 12 px）。
丸めは常に手前向きで、復元して読んでいない場所へ進むことはない。

最終ページの末尾より下（文書の下余白）はさらに手前へ戻るが、そこが
ビューポートの上端に来るのはビューポートが余白2つ分より小さいときだけ
なので、実際には起こらない。

丸めをページの末尾側へ寄せてあるのは、そこが「利用者が意図して止まる
位置」ではないため。ページ先頭・ページの途中・隙間はすべて誤差なく戻る。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF

from anp.pdf.layout import PageLayout


@dataclass(frozen=True, slots=True)
class ReadingPosition:
    """いま読んでいる位置（ページ番号 + ページ内の縦位置）。

    ビューポート上端に来ているページ内の縦位置を 0.0〜1.0 で持つ。
    スクロールバーのピクセル値も倍率も含まないので、ウィンドウの
    大きさ・DPI・倍率モードが変わっても意味が変わらない。

    `StudyMark` の `PagePosition` とは別の型にする。学習マークは
    「利用者が印を付けた点」で横位置も意味を持つが、こちらは
    「どこまで読んだか」で縦方向しか使わない（P5-1 の契約）。
    同じ型にすると、使わない `x_norm` に意味のない値を入れることになる。
    """

    page_index: int
    y_norm: float


def page_top_offset(layout: PageLayout, index: int, zoom: float) -> float:
    """そのページを開いたときのスクロール位置（コンテンツ座標）。

    ページの上端に余白を1つ残す。`go_to_page()` と `reveal_page_position()`、
    そして読書位置の基準がすべてこの値を使う。ページ配置の算術を
    あちこちに書かない。
    """
    return layout.page_rect(index, zoom).top() - layout.metrics.margin


def reading_position_at(layout: PageLayout, viewport: QRectF, zoom: float) -> ReadingPosition:
    """ビューポート上端が指している読書位置。

    **基準にするのは `current_page` ではない。** `current_page` は
    「ビューポートといちばん大きく重なるページ」なので、ページの継ぎ目
    付近では上端がまだ前のページの途中なのに次のページを指す。それを
    基準にすると比率が負になり、0.0 に丸めた結果「次のページの先頭」として
    保存されて、再起動のたびに数百ピクセルぶん先へ進んでしまう。

    使うのは **`page_top_offset()` から次のページの `page_top_offset()` まで**
    という区切り。ページの上端でも隙間でもなく、この区切りで選ぶと、
    どの位置も 0.0〜1.0 に収まったまま丸めずに表せる（`margin >= page_gap`
    のとき）。1つ手前のページの末尾へ寄せる分は余白1つぶんだけで、
    ページを開いた位置がちょうど 0.0 になる。
    """
    # 区切りは余白1つぶん手前から始まるので、その分だけ下げた矩形で探す。
    shifted = viewport.translated(0.0, layout.metrics.margin)
    visible = layout.visible_pages(shifted, zoom)
    # 可視ページが1つも無い（末尾の余白まで送られた）場合だけ、
    # いちばん近いページを使う。
    page = visible.start if visible else layout.current_page(shifted, zoom)
    if page > 0 and page_top_offset(layout, page, zoom) > viewport.top():
        # 隙間の上にいる。`visible_pages()` は隙間を次のページから数えるが、
        # 区切りとしてはまだ手前のページ（その分だけ 1.0 に近い値になる）。
        page -= 1

    height = layout.page_rect(page, zoom).height()
    top = page_top_offset(layout, page, zoom)
    y_norm = (viewport.top() - top) / height if height > 0 else 0.0
    return ReadingPosition(page_index=page, y_norm=clamp_unit(y_norm))


def scroll_top_for(layout: PageLayout, position: ReadingPosition, zoom: float) -> float | None:
    """読書位置を、ビューポート上端に来るスクロール位置へ戻す。

    `reading_position_at()` の逆。ページ末尾の `page_gap` px ぶんだけは
    1.0 へ丸められているので、そこだけは元の位置より手前に戻る
    （module の docstring を参照）。

    いま開いているドキュメントに無いページなら `None`。**最終ページへ
    丸めない**（差し替えでページ数の減った PDF で、関係のない場所へ
    飛ばないため）。
    """
    if not 0 <= position.page_index < layout.page_count:
        return None
    height = layout.page_rect(position.page_index, zoom).height()
    return page_top_offset(layout, position.page_index, zoom) + position.y_norm * height


def clamp_unit(value: float) -> float:
    """0.0〜1.0 に丸める。"""
    return min(max(value, 0.0), 1.0)
