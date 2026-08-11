"""表示中の PDF と、保存済み学習マークの同期。

`PdfView` は表示中の PDF が何であるかを知らない（識別子は持たない）。
`StudyMarkRepository` はどれを表示すべきかを知らない。**どの PDF の
マークをビューへ渡すかを決めるのがここ**で、両者の対応に責任を持つ
唯一の場所になる。

`MainWindow` はこの class を通してのみ学習マークに触る。UI 側に
`document_key()` や SQL を持ち込まないため。追加・更新・削除も同じで、
ウィジェットからリポジトリを直接呼ばない。

Qt の signal/slot を使わないので `QObject` にはしない。呼び出しは
すべて同期で、DB の読み取りも GUI スレッドで行う（学習マークの件数は
1つの PDF あたり多くても数千件で、1回の SELECT で足りる）。

**更新は「DB を変えてから読み直す」の一方向だけ。** 表示中のコレクションを
手で継ぎ当てたり、`StudyMark` を書き換えたりはしない（不変）。1回の更新に
つき SELECT が1回増えるだけなので、差分イベントや楽観的更新の仕組みは持たない。

ログの担当を分けている。読み込み（`activate_document()`）の失敗はここが
`logger.exception` で残す。呼び出し側はダイアログを出さずアプリケーション
境界まで送るため、ここで残さないと記録が残らない。一方 **更新の失敗は
ここでは記録しない**。利用者の操作の失敗はダイアログと一緒に UI の境界
（`StudyMarkInteraction`）が記録するので、両方で書くと同じ stacktrace が
2回出る。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NoReturn

from anp.storage.study_mark import document_key
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.pdf_view import PdfView
from anp.ui.study_marks import PagePosition

logger = logging.getLogger(__name__)


class StudyMarkError(RuntimeError):
    """学習マークの操作を行えなかった。

    種類を細かく分けた例外の階層は作らない。UI から見た扱いは「更新できな
    かったことを知らせて、表示は前のまま保つ」の1通りしかないため。
    リポジトリが送出する `sqlite3` の例外はそのまま通す（握り潰さない）。
    """


class StudyMarkLoadError(RuntimeError):
    """学習マークを読み込めなかった（`activate_document()`）。

    更新の失敗（`StudyMarkError`）とは扱いが違うので別の型にする。
    読み込みの失敗は **想定された失敗経路** で、UI は「PDF なし」まで戻して
    利用者に日本語で知らせる。片や `AttributeError` のような実装の誤りは
    包まずに素通しし、アプリケーション境界の未捕捉例外として扱う。この
    境界を作るために、リポジトリの呼び出しだけをこの型へ包む。

    継承関係は作らない。UI での扱いが「開くのを中止する」と「表示は前の
    まま保つ」で共通点が無く、まとめて捕まえたい場面が無いため。
    """


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

        読み取りの前に表示対象もビューも解除するので、途中で失敗しても
        「新しい PDF ＋古い PDF のマーク」という組み合わせにはならない。
        `set_document()` も同じことをするが、対応の正しさはこの class の
        責任なので、呼び出し順に頼らずここでも空にする。

        **表示対象を確定するのは読み取りに成功した後だけ**（fail-closed）。
        読めなかった PDF を表示対象のまま残すと、以後の追加・更新が
        「中身を読めていない PDF」に対して行われることになる。失敗した
        場合は表示対象なしの状態で `StudyMarkLoadError` を送出する。

        リポジトリの失敗を1つの型へ包むのは、呼び出し側が「想定された
        読み込み失敗」と「実装の誤り」を取り違えないようにするため。
        `sqlite3` の例外も、保存されていた行が契約を満たさなかったときの
        `ValueError` も、UI から見れば同じ「読み込めなかった」になる。
        原因は `__cause__` に残すので調査の情報は失われない。
        """
        self._active_path = None
        self._view.set_study_marks(())

        try:
            marks = self._repository.list_for_document(Path(path))
        except Exception as error:
            logger.exception("failed to load study marks for %s", path)
            msg = f"failed to load study marks for {path}"
            raise StudyMarkLoadError(msg) from error

        self._active_path = Path(path)
        self._view.set_study_marks(marks)

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
        見えてしまう。表示対象は変えない（同じ PDF の読み直しなので、
        別の PDF のマークが残ることはない）。
        """
        if self._active_path is None:
            self._view.set_study_marks(())
            return

        self._view.set_study_marks(self._repository.list_for_document(self._active_path))

    # -------------------------------------------------- 追加・更新・削除
    def create_mark(self, position: PagePosition, *, expected_document: Path | None) -> None:
        """表示中の PDF に学習マークを1件追加する。

        位置は `PdfView.page_position_at()` が返した `PagePosition` だけを
        受け取る。ページ番号と 0.0〜1.0 の座標であることはそこで保証されて
        いるので、ビューポートのピクセル座標がリポジトリまで届くことはない。

        `expected_document` は **その位置を取った時点の表示対象**。
        `PagePosition` は正規化座標なのでどの PDF のものか自分では名乗れず、
        照合しないと「A のページで開いたメニューを B へ切り替えてから実行」
        で B に A 由来の座標のマークができてしまう。既存マークの
        `_require_owned()` と同じ役割を、新規作成に対して果たす。

        既定値は置かない。呼び出し側にどの PDF のつもりかを必ず書かせる。

        間違えた回数は呼び出し側に選ばせない。「マークを作る＝最初に
        間違えた」なので、P3-1 の契約どおり必ず 1 から始まる。
        """
        path = self._require_active_document()
        if expected_document is None or document_key(expected_document) != document_key(path):
            msg = "the active document changed after the position was captured"
            raise StudyMarkError(msg)

        self._repository.create(path, position.page_index, position.x_norm, position.y_norm)
        self._sync_after_mutation()

    def increment_mark(self, mark_id: int) -> None:
        """間違えた回数を1増やす。

        読み出して足して書き戻すのではなく、リポジトリの1文の UPDATE に任せる
        （P3-1 の契約）。連続して押しても取りこぼさない。
        """
        self._require_owned(mark_id)
        if self._repository.increment_mistake_count(mark_id) is None:
            self._fail_missing(mark_id)
        self._sync_after_mutation()

    def update_note(self, mark_id: int, note: str | None) -> None:
        """メモを差し替える。

        受け取った文字列をそのまま渡す。前後の空白を落としたり、空文字を
        `None` へ寄せたりはしない（P3-1 の契約）。
        """
        self._require_owned(mark_id)
        if self._repository.update_note(mark_id, note) is None:
            self._fail_missing(mark_id)
        self._sync_after_mutation()

    def delete_mark(self, mark_id: int) -> None:
        """学習マークを1件消す。確認を取るのは UI の側。"""
        self._require_owned(mark_id)
        if not self._repository.delete(mark_id):
            self._fail_missing(mark_id)
        self._sync_after_mutation()

    def _require_active_document(self) -> Path:
        """表示対象の PDF。無ければ操作させない。

        表示対象が無いときに黙って別の PDF へ保存することだけは避ける。
        UI は表示対象が無ければ操作自体を出さないので、ここを通るのは
        呼び出し側の誤りのときだけ。
        """
        if self._active_path is None:
            msg = "no active document"
            raise StudyMarkError(msg)
        return self._active_path

    def _require_owned(self, mark_id: int) -> None:
        """その ID が **表示中の PDF の** マークであることを確かめる。

        右クリックメニューは対象を捕まえたまま開くので、開いている間に
        PDF が切り替わると、別の PDF のマークを指したまま発火しうる。
        更新の境界で持ち主を確かめておけば、A の記録が B の操作で
        書き換わることはない。

        持ち主の判定には P3-1 の `document_key()` をそのまま使う。パスの
        正規化を UI 側に書き直さない。
        """
        path = self._require_active_document()
        mark = self._repository.get(mark_id)
        if mark is None:
            self._fail_missing(mark_id)
        if mark.document_key != document_key(path):
            msg = f"study mark {mark_id} belongs to another document"
            raise StudyMarkError(msg)

    def _fail_missing(self, mark_id: int) -> NoReturn:
        """操作しようとしたマークが既に無い場合。

        「消えていたのだから成功でよい」とはしない。表示を実体に合わせ直して
        から失敗として伝える。
        """
        self._sync_after_mutation()
        msg = f"study mark {mark_id} no longer exists"
        raise StudyMarkError(msg)

    def _sync_after_mutation(self) -> None:
        """更新後に表示を読み直す。

        DB を source of truth にする。更新が通ったのに読み直せなかった場合、
        **表示は空にする**。古い件数を出したままにすると、画面の数字が保存
        されている値だと誤解させる。表示対象は解除しない（PDF を読むこと
        自体は続けられる。P3-3A の読み込み失敗とはここが違う）。
        """
        try:
            self.refresh()
        except Exception:
            self._view.set_study_marks(())
            raise
