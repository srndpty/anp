"""前回のセッション（どの PDF を、どこまで読んでいたか）の保存と復元。

`MainWindow` から切り出してある。ここが持つのは **設定とビューの間の
やり取りだけ**で、PDF を開く手順（`DocumentOpenCoordinator`）も、履歴も、
失敗の知らせ方も持たない。「開く」と「位置を戻す」の順序が入れ替わると
フィット倍率の計算が復元した位置を動かしてしまうので、その順序は
呼び出し側（`MainWindow.showEvent()`）に残してある。

設定の読み書きは `Settings`、位置の座標変換は
`anp.pdf.reading_position` にあり、ここには算術も鍵の名前も無い。
"""

from __future__ import annotations

import logging
from pathlib import Path

from anp.core.settings import Settings
from anp.pdf.reading_position import ReadingPosition
from anp.ui.pdf_view import PdfView

logger = logging.getLogger(__name__)


class ReadingSession:
    """前回読んでいた PDF と位置を、設定へ出し入れする。"""

    def __init__(self, settings: Settings, view: PdfView) -> None:
        self._settings = settings
        self._view = view

    @property
    def stored_document(self) -> Path | None:
        """前回終了時に開いていた PDF のパス。無ければ None。

        **最近開いたファイルの先頭とは別物。** 実際に開くかどうか、開けな
        かったときに履歴をどうするかは呼び出し側が決める。
        """
        stored = self._settings.last_document
        return Path(stored) if stored else None

    def restore_position(self) -> None:
        """保存された読書位置まで戻る。戻れなければ先頭のまま。

        **呼ぶのは PDF を開いた後、倍率が決まった後。** 順序を逆にすると、
        フィットの計算がここで決めたスクロール位置を動かす。

        差し替えでページ数が減っていた場合は、最終ページへ丸めずに文書の
        先頭で開く。セッションの位置は過去の読書状態のヒントでしかないので、
        存在しない位置を無理に解釈しない（学習マークの移動とは扱いが違う）。
        """
        position = ReadingPosition(
            page_index=self._settings.last_page_index,
            y_norm=self._settings.last_y_norm,
        )
        if not self._view.restore_reading_position(position):
            logger.info("saved reading position is outside the document: %s", position)

    def save(self, path: Path | None) -> None:
        """いま読んでいる PDF と位置を、次回の起動のために覚える。

        **呼ぶのは表示を捨てる前。** 後では現在ページもスクロール位置も
        失われている。

        PDF を開いていない状態で終了したときは忘れる。閉じたはずの PDF が
        次回また勝手に開くのを避けるため。倍率は既存の設定
        （`view/zoom_mode` と `view/free_zoom`）をそのまま使うので、
        セッション用の倍率キーは作らない。
        """
        position = self._view.current_reading_position()
        if path is None or position is None:
            self.forget()
            return
        self._settings.set_last_session(str(path), position.page_index, position.y_norm)

    def forget(self) -> None:
        """前回のセッションを忘れる。

        復元に失敗したときにも呼ぶ。残しておくと、起動のたびに同じ失敗を
        繰り返すことになる。
        """
        self._settings.clear_last_session()
