"""学習マークのマウス操作とダイアログ。

`PdfView` が出した「どこが押されたか」を、`StudyMarkController` の
追加・更新・削除へつなぐ。間に入るのはこの class だけで、

```
PdfView（ヒットテストと座標変換）
  → StudyMarkInteraction（メニュー・ダイアログ・失敗の知らせ方）
    → StudyMarkController（表示対象の確認と保存）
      → StudyMarkRepository（SQL）
```

という一方向になる。ビューはリポジトリを知らず、ここは SQL を知らない。

`MainWindow` から分けてあるのは、ウィンドウが持つ関心（PDF を開く・
ズーム・ページ移動）と混ざると読めなくなるため。Qt の signal を受けるが、
自分では出さないので `QObject` にはしない。

**利用者の操作の失敗はここで止める。** アプリケーション全体の未捕捉例外へ
送ると PDF を読んでいる最中に致命的なダイアログが出てしまうが、間違い回数を
増やせなかっただけなら読み続けられる。ここで捕まえて記録し、小さな警告を
出して、表示は前のまま（`StudyMarkController` が保った状態）にしておく。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QWidget

from anp.storage.study_mark import DocumentIdentity, StudyMark
from anp.storage.study_mark_repository import StoredStudyMarkError
from anp.ui.pdf_view import PdfView
from anp.ui.study_mark_controller import StudyMarkController, StudyMarkError
from anp.ui.study_marks import PagePosition, StudyMarkTarget

logger = logging.getLogger(__name__)

_ERROR_TITLE = "学習マーク"
_ERROR_TEXT = "学習マークを更新できませんでした。"

_NOTE_TITLE = "学習マークのメモ"
_NOTE_LABEL = "メモ"

_DELETE_TITLE = "学習マークを削除"
_DELETE_TEXT = "この学習マークを削除しますか？"

# 指紋を持たない古い学習マーク（マイグレーション2 より前の分）の尋ね方。
# どの PDF に付けたものか確かめようがないので、表示する前に本人へ聞く。
_UNVERIFIED_TITLE = "古い形式の学習マーク"
_UNVERIFIED_TEXT = (
    "このファイルには、どの内容の PDF に付けられたか記録されていない\n"
    "学習マークが {count} 件あります。\n\n"
    "いま開いている PDF のものとして紐付けますか？\n"
    "「いいえ」を選んでもマークは削除されません（次に開いたときにまた尋ねます）。"
)

# 更新の操作で起こりうる**想定された失敗**。表示対象の食い違い
# （`StudyMarkError`）、DB の障害、保存データの不整合、PDF そのものを
# 読めないこと（内容の指紋）。SQL はここに書かないが、リポジトリが
# `sqlite3` の例外を上げてくるという事実はこの境界が引き受ける。
_OPERATION_ERRORS = (StudyMarkError, sqlite3.Error, StoredStudyMarkError, OSError)


class StudyMarkInteraction:
    """Ctrl + 左クリックと右クリックメニューを、保存の操作へつなぐ。"""

    def __init__(self, view: PdfView, controller: StudyMarkController, parent: QWidget) -> None:
        self._controller = controller
        self._parent = parent

        view.study_mark_activated.connect(self._on_activated)
        view.study_mark_menu_requested.connect(self._on_menu_requested)

    # -------------------------------------------------- Ctrl + 左クリック
    def _on_activated(self, target: StudyMarkTarget) -> None:
        """バッジの上なら回数を増やし、ページの上なら追加する。

        どちらも確認を挟まない。間違えた問題をその場で記録するための操作
        なので、1クリックで終わらせる（消したいときは右クリックから）。
        """
        if target.mark is not None:
            self._increment(target.mark)
        elif target.position is not None:
            # クリックと保存の間に PDF は切り替わりようがないが、対象の
            # PDF を名乗るのは呼び出し側の責任なので、ここでも明示する。
            self._create(target.position, self._controller.active_document)

    # -------------------------------------------------- 右クリックメニュー
    def _on_menu_requested(self, target: StudyMarkTarget, global_pos: QPoint) -> None:
        menu = self.build_menu(target)
        if menu is None:
            return
        try:
            menu.exec(global_pos)
        finally:
            menu.deleteLater()

    def build_menu(self, target: StudyMarkTarget) -> QMenu | None:
        """対象に応じたメニューを組み立てる。対象が無ければ `None`。

        **アクションはここで受け取った対象を捕まえたまま持つ。** 発火した
        時点でカーソルの下を見直したりはしない。メニューを開いてから
        マウスを動かしても、操作されるのは押した場所のマークのまま。

        意味のない項目を並べて無効にするのではなく、対象に応じて必要な
        ものだけを作る。
        """
        if target.mark is not None:
            return self._mark_menu(target.mark)
        if target.position is not None:
            return self._page_menu(target.position)
        return None

    def _mark_menu(self, mark: StudyMark) -> QMenu:
        menu = QMenu(self._parent)
        menu.addAction("間違い回数を増やす").triggered.connect(lambda: self._increment(mark))
        menu.addAction("メモを編集…").triggered.connect(lambda: self._edit_note(mark))
        menu.addSeparator()
        menu.addAction("学習マークを削除").triggered.connect(lambda: self._delete(mark))
        return menu

    def _page_menu(self, position: PagePosition) -> QMenu:
        """ページ上で開いたときのメニュー。

        位置と一緒に **メニューを開いた時点の表示対象** も捕まえる。
        `PagePosition` は正規化座標なのでどの PDF のものか名乗れず、
        これが無いと PDF を切り替えた後の発火で別の PDF にマークができる。

        捕まえるのはパスではなく `DocumentIdentity`。同じパスの PDF を別の
        内容へ差し替えて開き直された場合も、別の PDF として弾ける。中身の
        比較はコントローラ側（判定基準を UI に持ち込まない）。
        """
        expected = self._controller.active_document
        menu = QMenu(self._parent)
        menu.addAction("学習マークを追加").triggered.connect(
            lambda: self._create(position, expected)
        )
        return menu

    # -------------------------------------------------- 操作
    def _create(self, position: PagePosition, expected_document: DocumentIdentity | None) -> None:
        self._run(
            lambda: self._controller.create_mark(position, expected_document=expected_document)
        )

    def _increment(self, mark: StudyMark) -> None:
        self._run(lambda: self._controller.increment_mark(mark.id))

    def _edit_note(self, mark: StudyMark) -> None:
        """メモを複数行で編集する。

        初期値は保存されている文字列そのまま（未設定の `None` は空欄）。
        取り消したときは DB に触らない。空のまま OK した場合は、`None` へ
        戻すのではなく空文字として保存する（P3-1 では別物）。
        """
        text, accepted = QInputDialog.getMultiLineText(
            self._parent, _NOTE_TITLE, _NOTE_LABEL, mark.note if mark.note is not None else ""
        )
        if not accepted:
            return
        self._run(lambda: self._controller.update_note(mark.id, text))

    def _delete(self, mark: StudyMark) -> None:
        """確認してから消す。学習の記録は取り戻せないため。

        **既定のボタンは「いいえ」**。ボタンを指定しないと Enter で削除が
        通ってしまう。取り消せない操作なので、何もしない側を既定にする。
        """
        answer = QMessageBox.question(
            self._parent,
            _DELETE_TITLE,
            _DELETE_TEXT,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._run(lambda: self._controller.delete_mark(mark.id))

    # -------------------------------------------------- 古い形式のマーク
    def prompt_adopt_unverified(self) -> None:
        """指紋を持たない古い学習マークを、この PDF に紐付けるか尋ねる。

        **PDF を開いた直後に呼ぶ**（呼ぶかどうかは `MainWindow` が決める）。
        マイグレーション2 より前に作られたマークは、どの内容の PDF に
        付けられたのかが分からない。黙って表示すると、同じパスの PDF を
        別の本へ差し替えていた場合に前の本のマークが正常なデータとして
        並ぶ。かといって黙って紐付けるのは取り消せない。

        **どちらの側にも倒さず、その PDF を実際に見ている利用者に尋ねる。**
        「いいえ」を選んでもマークは消えない（次に開いたときにまた尋ねる）。
        既定のボタンも「いいえ」にしておく。

        ここに置くのは、学習マークの操作の失敗を止める境界（`_run()`）が
        ここにあるため。`MainWindow` から直に呼ぶと、DB の障害がそのまま
        起動時の未捕捉例外になる。
        """
        count = self._controller.unverified_count
        if count == 0:
            return

        answer = QMessageBox.question(
            self._parent,
            _UNVERIFIED_TITLE,
            _UNVERIFIED_TEXT.format(count=count),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._run(self._controller.adopt_unverified)

    def _run(self, operation: Callable[[], object]) -> None:
        """操作を実行し、失敗したら記録して知らせる。

        捕まえるのは **想定された失敗だけ**（`_OPERATION_ERRORS`）。どれも
        利用者から見れば「更新できなかった」の1通りなので、種類では分けない。
        表示を成功したように見せないのは `StudyMarkController` の責任で、
        ここは「読み続けられる」状態を壊さないことだけを守る。

        `AttributeError` のような実装の誤りはここで止めない。小さな警告に
        化けさせるとバグが残り続けるので、アプリケーション境界の未捕捉例外
        として扱う。
        """
        try:
            operation()
        except _OPERATION_ERRORS as error:
            logger.exception("study mark operation failed")
            QMessageBox.warning(self._parent, _ERROR_TITLE, f"{_ERROR_TEXT}\n\n{error}")
