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

    def restore_position(self, fingerprint: str | None) -> None:
        """保存された読書位置まで戻る。戻れなければ先頭のまま。

        **呼ぶのは PDF を開いた後、倍率が決まった後。** 順序を逆にすると、
        フィットの計算がここで決めたスクロール位置を動かす。

        `fingerprint` は **いま開いている PDF の内容の指紋**
        （`DocumentController.content_fingerprint`）。保存された指紋と
        一致しなければ位置は戻さない。同じパスでも中身が違えば「120 ページ目の
        途中」に意味は無く、読んでいない場所へ飛ばすことになる。

        指紋を持たないセッション（1つ前の保存形式）も戻さない。同じ内容だと
        確かめられない以上、先頭から読み直せる方を選ぶ（fail-closed）。

        差し替えでページ数が減っていた場合は、最終ページへ丸めずに文書の
        先頭で開く。セッションの位置は過去の読書状態のヒントでしかないので、
        存在しない位置を無理に解釈しない（学習マークの移動とは扱いが違う）。
        """
        stored = self._settings.last_fingerprint
        if stored is None:
            logger.info("the last session has no fingerprint; starting at the top")
            return
        if stored != fingerprint:
            logger.info("the document changed since the last session; starting at the top")
            return

        position = ReadingPosition(
            page_index=self._settings.last_page_index,
            y_norm=self._settings.last_y_norm,
        )
        if not self._view.restore_reading_position(position):
            logger.info("saved reading position is outside the document: %s", position)

    def save(self, path: Path | None, fingerprint: str | None) -> None:
        """いま読んでいる PDF と位置を、次回の起動のために覚える。

        **呼ぶのは表示を捨てる前。** 後では現在ページもスクロール位置も
        失われている。

        内容の指紋も一緒に残す（`DocumentController.content_fingerprint`）。
        パスだけだと、次の起動までに同じパスの中身が入れ替わっていた場合に、
        別の本の同じページ番号へ飛んでしまう。

        PDF を開いていない状態で終了したときは忘れる。閉じたはずの PDF が
        次回また勝手に開くのを避けるため。倍率は既存の設定
        （`view/zoom_mode` と `view/free_zoom`）をそのまま使うので、
        セッション用の倍率キーは作らない。
        """
        position = self._view.current_reading_position()
        if path is None or position is None:
            self.forget()
            return
        self._settings.set_last_session(
            str(path), fingerprint, position.page_index, position.y_norm
        )

    def forget(self) -> None:
        """前回のセッションを忘れる。

        復元に失敗したときにも呼ぶ。残しておくと、起動のたびに同じ失敗を
        繰り返すことになる。
        """
        self._settings.clear_last_session()
