"""表示中の PDF と、保存済み学習マークの同期。

`PdfView` は表示中の PDF が何であるかを知らない（識別子は持たない）。
`StudyMarkRepository` はどれを表示すべきかを知らない。**どの PDF の
マークをビューへ渡すかを決めるのがここ**で、両者の対応に責任を持つ
唯一の場所になる。

`MainWindow` はこの class を通してのみ学習マークに触る。UI 側に
`document_key()` や SQL を持ち込まないため。追加・更新・削除も同じで、
ウィジェットからリポジトリを直接呼ばない。

**表示中のスナップショットの唯一の公開点がここ**（P4-1）。表示先が
`PdfView` と `StudyMarkSidebar` の2つになったので、どちらへも
`_publish_marks()` の1経路からしか届かないようにする。呼び出し側が
ビューとサイドバーを別々に更新できてしまうと、オーバーレイの内容と
一覧の内容が食い違う状態を作れてしまう。

通知が要るようになったので `QObject` にして `marks_changed` を1本だけ
持つ。汎用のイベントバスや observable の仕組みは作らない。呼び出しは
すべて同期で、DB の読み取りも GUI スレッドで行う（学習マークの件数は
1つの PDF あたり多くても数千件で、1回の SELECT で足りる）。

**更新は「DB を変えてから、返ってきた行で表示を差し替える」。** リポジトリの
更新は `RETURNING` で更新後の1件を返すので、それを使ってスナップショットを
組み直す（`StudyMark` は不変なので、既存の要素を書き換えることはしない）。

以前は更新のたびに全件を SELECT し直していたが、**それだと「DB では成功、
表示では失敗」という状態が作れてしまう**。接続は `autocommit=True` なので
更新は 1 文で確定済みで、その後の SELECT が失敗しても取り消されない。
利用者には「更新できませんでした」と出るのに実際は増えているので、もう一度
押すと 1 回のつもりが 2 回加算される。更新の成功と、その後の全件同期の失敗を
別の状態として扱わないかぎり避けられないので、そもそも読み直さないことにした。

書き手はこのプロセスだけなので、返ってきた1件を反映すれば表示は DB と一致
する。全件の読み直しは `activate_document()` と `refresh()`（外から明示的に
呼ぶ）に限る。楽観的更新はしない（DB が成功を返す前に表示は変えない）。

ログの担当を分けている。読み込み（`activate_document()`）の失敗はここが
`logger.exception` で残す。呼び出し側はダイアログを出さずアプリケーション
境界まで送るため、ここで残さないと記録が残らない。一方 **更新の失敗は
ここでは記録しない**。利用者の操作の失敗はダイアログと一緒に UI の境界
（`StudyMarkInteraction`）が記録するので、両方で書くと同じ stacktrace が
2回出る。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from PySide6.QtCore import QObject, Signal

from anp.storage.study_mark import DocumentIdentity, StudyMark, document_key
from anp.storage.study_mark_repository import StoredStudyMarkError, StudyMarkRepository
from anp.ui.pdf_view import PdfView
from anp.ui.study_marks import PagePosition

logger = logging.getLogger(__name__)

# リポジトリが送出しうる**想定された失敗**。DB の障害、保存データの不整合、
# PDF そのものを読めないこと（内容の指紋）の3つ。これ以外（`AttributeError`
# など）は実装の誤りなので包まずに素通しする。
_LOAD_ERRORS = (sqlite3.Error, StoredStudyMarkError, OSError)


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


class StudyMarkController(QObject):
    """表示中のドキュメントの学習マークを、表示側へ配る。"""

    marks_changed = Signal(object)
    """表示中の学習マークが差し替わった（`tuple[StudyMark, ...]`）。

    通知はこの1本だけ。「1件増えた」「1件消えた」のような差分は出さない。
    更新は常に「DB を変えてから読み直す」の一方向で、受け取った側は
    渡されたスナップショットから作り直すだけでよい。
    """

    def __init__(
        self,
        repository: StudyMarkRepository,
        view: PdfView,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._view = view
        self._active_path: Path | None = None
        # 表示中の PDF の同一性。**開いたときに1回だけ作る。** 操作のたびに
        # パスから作り直すと、表示している PDF と保存先の PDF がずれる
        # （表示中に同じパスが別の内容へ置き換わった場合）。
        self._identity: DocumentIdentity | None = None
        self._marks: tuple[StudyMark, ...] = ()
        self._unverified_count = 0

    @property
    def active_document_path(self) -> Path | None:
        """いま学習マークを表示している PDF のパス。無ければ None。"""
        return self._active_path

    @property
    def unverified_count(self) -> int:
        """表示中の PDF のパスに残っている、指紋を持たない学習マークの数。

        マイグレーション2 より前に作られた分。**表示はしていない**（持ち主を
        確かめられないため）。引き取るかどうかは利用者に尋ねる。
        """
        return self._unverified_count

    @property
    def study_marks(self) -> tuple[StudyMark, ...]:
        """いま表示している学習マーク（`page_index`・`id` の昇順）。

        タプルなので呼び出し側から書き換えられない。表示に使うコレクションを
        外から継ぎ当てられると、DB を source of truth にする契約が崩れる。
        """
        return self._marks

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
        `StoredStudyMarkError` も、内容の指紋を読めなかったときの `OSError`
        も、UI から見れば同じ「読み込めなかった」になる。原因は `__cause__`
        に残すので調査の情報は失われない。

        **包むのは想定された失敗だけ。** `AttributeError` のような実装の
        誤りは素通しする。ここで `except Exception` にすると、プログラムの
        誤りが「学習マークを読み込めませんでした」という日常的な失敗の
        見た目に化けて、fail-fast が効かなくなる。

        **PDF の同一性を作るのはここだけ。** 内容の指紋はファイル全体を
        読むので、開いたときに1回で済ませる。以後の追加・更新はこの値を
        使うので、表示中に同じパスが別の内容へ置き換わっても、いま見えて
        いる PDF のマークとして扱われる。
        """
        self._active_path = None
        self._identity = None
        self._unverified_count = 0
        self._publish_marks(())

        try:
            identity = DocumentIdentity.of(path)
            marks = self._repository.list_for_document(identity)
            unverified = self._repository.unverified_count(identity)
        except _LOAD_ERRORS as error:
            logger.exception("failed to load study marks for %s", path)
            msg = f"failed to load study marks for {path}"
            raise StudyMarkLoadError(msg) from error

        self._active_path = Path(path)
        self._identity = identity
        self._unverified_count = unverified
        self._publish_marks(marks)

    def clear_document(self) -> None:
        """表示対象を解除し、表示中の学習マークを空にする。"""
        self._active_path = None
        self._identity = None
        self._unverified_count = 0
        self._publish_marks(())

    def refresh(self) -> None:
        """表示中のマークを、保存されている内容で丸ごと**読み直す**。

        更新のたびには呼ばない（module の docstring を参照）。外から別の
        経路で DB が変わったときに、表示を実体へ合わせ直すための入口。

        表示対象が無ければ空にする。リポジトリが返したドメイン
        オブジェクトは加工せずそのまま渡す。件数の丸め・重複の除去・
        ページ番号による絞り込みは行わない（存在しないページを指す
        マークの扱いは `PdfView` の契約）。

        読み取りに失敗した場合は例外をそのまま送出する。「マークが0件」
        として黙って続けると、保存されているはずのものが消えたように
        見えてしまう。表示対象は変えない（同じ PDF の読み直しなので、
        別の PDF のマークが残ることはない）。
        """
        if self._identity is None:
            self._publish_marks(())
            return

        self._publish_marks(self._repository.list_for_document(self._identity))

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

        # 持ち主は **開いたときに決めた同一性**。ここでパスから作り直すと、
        # 表示中に同じパスが別の内容へ置き換わっていた場合に、いま見えて
        # いる PDF の座標を別の PDF のマークとして保存してしまう。
        created = self._repository.create(
            self._require_identity(), position.page_index, position.x_norm, position.y_norm
        )
        self._apply_stored(created)

    def increment_mark(self, mark_id: int) -> None:
        """間違えた回数を1増やす。

        読み出して足して書き戻すのではなく、リポジトリの1文の UPDATE に任せる
        （P3-1 の契約）。連続して押しても取りこぼさない。
        """
        self._require_owned(mark_id)
        updated = self._repository.increment_mistake_count(mark_id)
        if updated is None:
            self._fail_missing(mark_id)
        self._apply_stored(updated)

    def update_note(self, mark_id: int, note: str | None) -> None:
        """メモを差し替える。

        受け取った文字列をそのまま渡す。前後の空白を落としたり、空文字を
        `None` へ寄せたりはしない（P3-1 の契約）。
        """
        self._require_owned(mark_id)
        updated = self._repository.update_note(mark_id, note)
        if updated is None:
            self._fail_missing(mark_id)
        self._apply_stored(updated)

    def adopt_unverified(self) -> int:
        """指紋を持たない学習マークを、表示中の PDF のものとして引き取る。

        引き取った件数を返す。**呼ぶのは利用者が承認したときだけ**（尋ねるのは
        UI の側）。取り消せない操作なので、黙って結び付けはしない。

        引き取った分は表示に加わるので、ここだけは全件を読み直す。件数は
        マイグレーション2 より前のマークに限られ、この操作は1つの PDF に
        つき一度きり。
        """
        identity = self._require_identity()
        adopted = self._repository.adopt_unverified(identity)
        self._unverified_count = self._repository.unverified_count(identity)
        self.refresh()
        return adopted

    def delete_mark(self, mark_id: int) -> None:
        """学習マークを1件消す。確認を取るのは UI の側。"""
        self._require_owned(mark_id)
        if not self._repository.delete(mark_id):
            self._fail_missing(mark_id)
        self._publish_marks([mark for mark in self._marks if mark.id != mark_id])

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

    def _require_identity(self) -> DocumentIdentity:
        """表示対象の PDF の同一性。無ければ操作させない。

        `_active_path` と必ず対で入るので、ここを通るのは呼び出し側の
        誤りのときだけ。
        """
        if self._identity is None:
            msg = "no active document"
            raise StudyMarkError(msg)
        return self._identity

    def _require_owned(self, mark_id: int) -> None:
        """その ID が **表示中の PDF の** マークであることを確かめる。

        右クリックメニューは対象を捕まえたまま開くので、開いている間に
        PDF が切り替わると、別の PDF のマークを指したまま発火しうる。
        更新の境界で持ち主を確かめておけば、A の記録が B の操作で
        書き換わることはない。

        判定に使うのは **表示中のスナップショット**。そこに載っているのは
        `list_for_document()` が「このパス・この内容の PDF のもの」として
        返した行だけなので、パスの正規化も内容の指紋もリポジトリ側の1箇所に
        寄せたままになる。UI へ持ち主の判定基準を書き直さない。

        スナップショットに無い ID は、DB を1回だけ引いて「消えた」のか
        「別の PDF のもの」なのかを分ける。呼び出し側から見た扱いが違う
        （前者は表示の作り直し、後者は操作の拒否）ため。
        """
        self._require_active_document()
        if any(mark.id == mark_id for mark in self._marks):
            return
        if self._repository.get(mark_id) is None:
            self._fail_missing(mark_id)
        msg = f"study mark {mark_id} belongs to another document"
        raise StudyMarkError(msg)

    def _fail_missing(self, mark_id: int) -> NoReturn:
        """操作しようとしたマークが既に無い場合。

        「消えていたのだから成功でよい」とはしない。表示からその1件を外して
        から失敗として伝える。**ここで全件を読み直さない。** 読み直しが
        失敗すると、伝えたい「もう無い」が DB の障害にすり替わってしまう。
        """
        self._publish_marks([mark for mark in self._marks if mark.id != mark_id])
        msg = f"study mark {mark_id} no longer exists"
        raise StudyMarkError(msg)

    def _apply_stored(self, mark: StudyMark) -> None:
        """更新後の1件を、表示中のスナップショットへ反映する。

        `id` が一致するものがあれば差し替え、無ければ挿し込む。並びは
        リポジトリの契約（`page_index`・`id` の昇順）に合わせるので、
        追加したマークの位置も読み直したときと同じになる。
        """
        remaining = [existing for existing in self._marks if existing.id != mark.id]
        order = (mark.page_index, mark.id)
        index = next(
            (
                position
                for position, existing in enumerate(remaining)
                if (existing.page_index, existing.id) > order
            ),
            len(remaining),
        )
        remaining.insert(index, mark)
        self._publish_marks(remaining)

    def _publish_marks(self, marks: Sequence[StudyMark]) -> None:
        """表示中のスナップショットを差し替え、表示側へ配る唯一の経路。

        **ビューとサイドバーを別々に更新しない。** ここを通さない更新を
        許すと、オーバーレイと一覧が違うものを見せる状態が作れてしまう。

        受け取った並びは変えない（`page_index`・`id` の昇順というリポジトリの
        契約をそのまま引き継ぐ）。件数の丸めも重複の除去も行わない。
        """
        snapshot = tuple(marks)
        self._marks = snapshot
        self._view.set_study_marks(snapshot)
        self.marks_changed.emit(snapshot)
