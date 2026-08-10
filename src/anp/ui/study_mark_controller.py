"""表示中の PDF と、保存済み学習マークの同期。

`PdfView` は表示中の PDF が何であるかを知らない（識別子は持たない）。
`StudyMarkRepository` はどれを表示すべきかを知らない。**どの PDF の
マークをビューへ渡すかを決めるのがここ**で、両者の対応に責任を持つ
唯一の場所になる。

`MainWindow` はこの class を通してのみ学習マークに触る。UI 側に
`document_key()` や SQL を持ち込まないため。

Qt の signal/slot を使わないので `QObject` にはしない。呼び出しは
すべて同期で、DB の読み取りも GUI スレッドで行う（学習マークの件数は
1つの PDF あたり多くても数千件で、1回の SELECT で足りる）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.pdf_view import PdfView

logger = logging.getLogger(__name__)


class StudyMarkController:
    """表示中のドキュメントの学習マークを `PdfView` へ載せる。"""

    def __init__(self, repository: StudyMarkRepository, view: PdfView) -> None:
        self._repository = repository
        self._view = view
        self._active_path: Path | None = None

    @property
    def active_document_path(self) -> Path | None:
        """いま学習マークを表示している PDF のパス。無ければ None。"""
        return self._active_path

    def activate_document(self, path: Path) -> None:
        """この PDF の学習マークを読み込んで表示する。

        **呼ぶのは `PdfView.set_document()` が成功した後**。ビューは
        ドキュメントの差し替えで学習マークを捨てるので、先に呼ぶと
        読み込んだマークがその場で消える。

        読み取りの前にビューを空にするので、途中で失敗しても
        「新しい PDF ＋古い PDF のマーク」という組み合わせにはならない。
        `set_document()` も同じことをするが、対応の正しさはこの class の
        責任なので、呼び出し順に頼らずここでも空にする。
        """
        self._active_path = Path(path)
        self._view.set_study_marks(())
        self.refresh()

    def clear_document(self) -> None:
        """表示対象を解除し、ビューの学習マークを空にする。"""
        self._active_path = None
        self._view.set_study_marks(())

    def refresh(self) -> None:
        """表示中のマークを、保存されている内容で丸ごと置き換える。

        表示対象が無ければ空にする。リポジトリが返したドメイン
        オブジェクトは加工せずそのまま渡す。件数の丸め・重複の除去・
        ページ番号による絞り込みは行わない（存在しないページを指す
        マークの扱いは `PdfView` の契約）。

        読み取りに失敗した場合は例外をそのまま送出する。「マークが0件」
        として黙って続けると、保存されているはずのものが消えたように
        見えてしまう。
        """
        if self._active_path is None:
            self._view.set_study_marks(())
            return

        try:
            marks = self._repository.list_for_document(self._active_path)
        except Exception:
            logger.exception("failed to load study marks for %s", self._active_path)
            raise

        self._view.set_study_marks(marks)
