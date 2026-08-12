"""PDF を開く・閉じるときの副作用を、1つの手順にまとめる。

`MainWindow` から切り出してある。1つの PDF を開くには、ドキュメント・
ビュー・学習マーク・目次・検索・最後に開いたディレクトリを **決まった順で**
差し替える必要があり、途中で失敗したときの後始末もその順に依存する。
ウィンドウの他の関心（メニュー・ツールバー・履歴・ダイアログ・タイトル）と
混ざっていると、この順序が正しいかどうかを読み取れない。

ここが持たないもの: ダイアログ、ウィンドウタイトル、ステータスバー、
最近使ったファイル、前回のセッション。失敗をどう知らせるかは呼び出し側が
決めるので、この class は `OpenFailure` を **返す**（自分では出さない）。

手順は1本だけ。利用者が選んだ場合も、履歴からの場合も、起動時の自動復元の
場合も `open()` を通る。復元専用の経路は作らない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from anp.core.settings import Settings
from anp.pdf.document import DocumentController, DocumentError
from anp.pdf.layout import InvalidPageGeometryError
from anp.storage.study_mark import DocumentIdentity
from anp.ui.pdf_search_controller import PdfSearchController
from anp.ui.pdf_view import PdfView
from anp.ui.search_dock import SearchDock
from anp.ui.study_mark_controller import StudyMarkController, StudyMarkLoadError
from anp.ui.toc_sidebar import TocSidebar

logger = logging.getLogger(__name__)

_OPEN_ERROR_TITLE = "PDF を開けません"

# 学習マークを読み込めなかったときの知らせ方。開くのを中止した理由と、
# 記録そのものは消えていないことを書く（利用者がいちばん恐れるのはそこ）。
_MARK_LOAD_ERROR_TITLE = "学習マークを読み込めません"
_MARK_LOAD_ERROR = (
    "この PDF の学習マークを読み込めなかったため、開くのを中止しました。\n"
    "記録は削除されていません。ログを確認してください。"
)


@dataclass(frozen=True, slots=True)
class OpenFailure:
    """開けなかった理由。**これは想定された失敗経路**。

    知らせ方（ダイアログを出すか、ログだけにするか）は呼び出し側が決める。
    包まれていない例外（実装の誤り）はここには来ない。素通しして、
    アプリケーション境界の未捕捉例外として扱う。
    """

    title: str
    body: str


class DocumentOpenCoordinator:
    """PDF の差し替えと、その後始末を受け持つ。"""

    def __init__(
        self,
        *,
        document: DocumentController,
        view: PdfView,
        study_marks: StudyMarkController,
        toc: TocSidebar,
        search: PdfSearchController,
        search_dock: SearchDock,
        settings: Settings,
    ) -> None:
        self._document = document
        self._view = view
        self._study_marks = study_marks
        self._toc = toc
        self._search = search
        self._search_dock = search_dock
        self._settings = settings

    @property
    def path(self) -> Path | None:
        """いま開いている PDF のパス。無ければ None。"""
        return self._document.path

    def open(self, path: Path) -> OpenFailure | None:
        """PDF を開く。開けたら None、開けなかったら理由を返す。

        **途中のどこで失敗しても「PDF なし」まで戻す。** 差し替えは
        ドキュメント・ビュー・学習マーク・目次・検索の5つに跨るので、
        どれか1つで抜けると「ドキュメントは B、表示は A」のように
        混ざった状態が残る。

        `DocumentController.open()` は失敗時に前のドキュメントも閉じるので、
        表示だけ古い PDF のまま残すと、見えている内容と実体がずれる。
        学習マークも同じ理由で、失敗時は表示対象ごと解除する。

        学習マークを読み込むのは **表示が新しい PDF に確定した後**。
        `set_document()` より前に読み込むと、ドキュメントの差し替えで
        そのまま捨てられる。

        学習マークを読み込めなかった場合は、その PDF を開いた状態にも
        しない（fail-closed）。読み込めていないまま読み進められると、
        利用者からは「マークが消えた」ようにしか見えず、そのうえ
        追加・更新が実体の分からない PDF に対して行われる。後始末は
        開くのに失敗したときと同じ「PDF なし」の状態まで戻す。

        **検索は読み込みを始める前に止める。** `DocumentController` は
        `QPdfDocument` を使い回すので、検索モデルを付けたまま `load()` を
        呼ぶと、`QPdfSearchModel` がページを走査している最中に対象のページが
        消える（走査はタイマーで少しずつ進む）。後始末では遅い。
        """
        self._search_dock.clear_query()
        self._search.detach_document()
        try:
            self._document.open(path)
        except DocumentError as error:
            self.clear()
            return OpenFailure(title=_OPEN_ERROR_TITLE, body=error.message)

        # **`open()` が成功した時点から後始末の内側。** ここから先で失敗すると
        # `DocumentController` の `QPdfDocument` は新しい PDF に切り替わって
        # いるので、途中で抜けると「ドキュメントは B、表示は A」という
        # 混ざった状態が残る。例えばページ寸法が壊れた PDF では
        # `set_document()` がレイアウトの構築で失敗する。不変条件の確認
        # （指紋があること）も、抜ける経路である以上この内側に置く。
        try:
            # 学習マークの持ち主は、**いま読み込んだ内容**で決める。ここで
            # パスから指紋を取り直すと、その隙に同じパスが別の内容へ
            # 置き換わっていた場合に、表示している PDF と持ち主がずれる。
            fingerprint = self._document.content_fingerprint
            if fingerprint is None:
                # `open()` が成功していれば必ず入っている。ここへ来るのは実装の誤り。
                msg = "the document was opened without a content fingerprint"
                raise RuntimeError(msg)
            identity = DocumentIdentity.for_content(path, fingerprint)

            self._view.set_document(self._document.document, self._document.page_sizes())
            self._study_marks.activate_document(identity)
            # 目次と検索を載せるのは **他がすべて成功した後**。中止する経路は
            # すべて `close()` を通るので、古い PDF の目次や検索結果が残る
            # ことはない。検索語と入力欄は読み込みの前に空にしてあるので、
            # A の検索語のまま B の件数が出ることもない。
            self._toc.set_document(self._document.document)
            self._search.attach_document(self._document.document)
        except InvalidPageGeometryError as error:
            self.close()
            return OpenFailure(title=_OPEN_ERROR_TITLE, body=error.message)
        except StudyMarkLoadError:
            self.close()
            return OpenFailure(title=_MARK_LOAD_ERROR_TITLE, body=_MARK_LOAD_ERROR)
        except BaseException:
            # 想定していない失敗（実装の誤り）。**包まない。** 後始末だけ
            # 済ませて送出し、アプリケーション境界の未捕捉例外として扱う。
            self.close()
            raise

        # 「最後にファイルを開いたディレクトリ」も開けたときだけ覚える。
        self._settings.last_directory = str(path.parent)
        return None

    def clear(self) -> None:
        """表示を「PDF なし」の状態へ戻す。ドキュメントは閉じない。

        開くのに失敗したときと、学習マークを読み込めなかったときの後始末は
        同じ。学習マークだけ別の後始末を作らない。

        目次と検索も一緒に手放す。「PDF なし」なのに前の PDF の目次や
        検索結果が残っている状態を作らないため。
        """
        self._view.clear_document()
        self._study_marks.clear_document()
        self._toc.clear_document()
        self._search_dock.clear_query()
        self._search.detach_document()

    def close(self) -> None:
        """表示を捨て、ドキュメントも閉じる。

        順序は「表示 → ドキュメント」。`clear()` が世代を進めてキャッシュと
        要求を捨ててから閉じる。逆にすると、閉じた後のドキュメントに対する
        レンダリング要求が残る。
        """
        self.clear()
        self._document.close()
