"""PDF の目次（outline / bookmarks）のサイドバー。

扱うのは **PDF に埋め込まれている outline / bookmarks だけ**。本文に
「目次」のページが印刷されていても、outline を持たない PDF は目次なしと
して扱う。本文の解析も OCR も行わない。

情報源は `QPdfBookmarkModel` の1つだけで、階層を Python のオブジェクト
ツリーへ写し取らない。

```
QPdfDocument
        ↓
QPdfBookmarkModel（ここが所有する）
        ↓
QTreeView（クリック / Enter）
        ↓ destination_requested
MainWindow（配線だけ）
        ↓
PdfView.navigate_to_pdf_destination()
```

**ここが持たないもの**: `DocumentController`・`PdfView`・スクロールと
ページ配置の算術・`StudyMarkRepository`・SQLite・最近使ったファイル・
設定の読み書き・レンダリングとキャッシュ。

`QPdfBookmarkModel` は渡された `QPdfDocument` の状態変化に自分で追随
する（読み込み・close で階層を作り直す）。それでも `set_document()` /
`clear_document()` を明示的に呼ぶのは、閉じられるドキュメントを
モデルが指し続けないようにするためと、「表示中の PDF の目次だけを出す」
という契約をこのクラスの API として持つため。
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPointF, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtPdf import QPdfBookmarkModel, QPdfDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QLabel,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from anp.pdf.destination import PdfDestination

_TITLE = "目次"

# `saveState()` / `restoreState()` がドックを識別するための名前。
# 変えると保存済みのウィンドウ状態からドックを復元できなくなる。
DOCK_OBJECT_NAME = "tocDock"

# 目次を出せないときの表示。**エラーではない**のでダイアログは出さない。
# 「PDF が無い」と「PDF に目次が無い」は利用者にとって別のことなので、
# 文言を分ける（同じ「目次はありません」だと、開くのに失敗したのか
# 目次が無いだけなのかが区別できない）。
NO_DOCUMENT_TEXT = "PDF が開かれていません"
NO_OUTLINE_TEXT = "目次はありません"


def destination_for(index: QModelIndex) -> PdfDestination | None:
    """モデルの行が指す移動先。移動先として読めない行は None。

    **`QPdfBookmarkModel` のデータが入ってくる唯一の境界**なので、型の
    検査もここだけで行う。PDF の metadata は壊れていることがあるので、
    読めない値は例外にせず None にする（ページ番号が範囲外かどうかは
    ドキュメントを知っている `PdfView` の判断なので、ここでは見ない）。

    Zoom ロールは読むが、載せるだけで適用はしない。
    """
    if not index.isValid():
        return None

    page = index.data(QPdfBookmarkModel.Role.Page.value)
    if isinstance(page, bool) or not isinstance(page, int):
        return None

    location = index.data(QPdfBookmarkModel.Role.Location.value)
    return PdfDestination(
        page_index=page,
        location=location if isinstance(location, QPointF) else QPointF(0.0, 0.0),
        zoom_hint=_zoom_hint(index.data(QPdfBookmarkModel.Role.Zoom.value)),
    )


def _zoom_hint(value: object) -> float | None:
    """PDF が指定した倍率。指定が無ければ None。

    `QPdfBookmarkModel` は指定の無い項目に 0.0 を返す。0 以下は倍率として
    意味を持たないので、指定なしと同じ扱いにする。
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


class _TocTreeView(QTreeView):
    """Enter / Return を、現在行への移動として扱うツリー。

    `activated` シグナルは使わない。プラットフォームによってはダブル
    クリックでも出るので、`clicked` と両方つなぐと1回の操作で移動を
    二重に要求してしまう。ここで見るのは押鍵だけ（検索欄の
    `_SearchLineEdit` と同じ大きさの局所実装）。
    """

    key_activated = Signal(QModelIndex)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt の命名規則)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = self.currentIndex()
            if index.isValid():
                self.key_activated.emit(index)
                event.accept()
                return
        super().keyPressEvent(event)


class TocSidebar(QDockWidget):
    """PDF の目次を階層のまま見せ、選ばれた項目の移動先を要求する。"""

    destination_requested = Signal(object)
    """項目が選ばれた（移動先の `PdfDestination`）。

    移動そのものはここでは行わない。ページ配置の算術は `PdfView` にあり、
    サイドバーはビューを持たない。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_TITLE, parent)
        # ウィンドウ状態の保存/復元がドックを識別するための名前。
        self.setObjectName(DOCK_OBJECT_NAME)

        # モデルはこのドックが所有する（Qt の親子関係で寿命を持つ）。
        self._model = QPdfBookmarkModel(self)
        self._has_document = False

        self.setWidget(self._build_body())

        # モデルはドキュメントの状態変化に自分で追随して階層を作り直すので、
        # 空表示の切り替えと選択の破棄はモデルの通知から行う。`set_document()`
        # を通らない経路（PDF を閉じた・読み込み直された）でも表示がずれない。
        self._model.modelReset.connect(self._on_model_reset)
        self._sync_empty_state()

    def _build_body(self) -> QWidget:
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)

        self._empty_label = QLabel(NO_DOCUMENT_TEXT, body)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        layout.addWidget(self._empty_label)

        self._tree = _TocTreeView(body)
        self._tree.setModel(self._model)
        # 目次は1列（`QPdfBookmarkModel.columnCount()` は 1）なので見出しは
        # 出さない。編集も並べ替えもしない。
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # **`expandAll()` はしない。** 数千項目の PDF で開いた瞬間に全展開
        # すると読めなくなる。展開/折り畳みは Qt の通常の操作に任せる。
        # クリックと Enter は同じ移動要求へ落とす。キーボードのためだけの
        # 別の移動経路は作らない。
        self._tree.clicked.connect(self._request_destination)
        self._tree.key_activated.connect(self._request_destination)
        layout.addWidget(self._tree)

        return body

    # -------------------------------------------------- ドキュメント
    def set_document(self, document: QPdfDocument) -> None:
        """目次を表示するドキュメントを差し替える。

        outline を Python 側へ写し取らない。`QPdfBookmarkModel` が
        `QPdfDocument` から作る階層をそのまま `QTreeView` へ載せるので、
        ドキュメントを開き直すたびに全項目を組み立て直すことはない。
        """
        self._has_document = True
        self._model.setDocument(document)
        self._reset_view_state()
        self._sync_empty_state()

    def clear_document(self) -> None:
        """ドキュメントを手放し、空表示へ戻す。

        閉じられる `QPdfDocument` をモデルが指し続けないよう、必ず外す。
        PDF を開くのに失敗したときも、学習マークを読み込めなかったときも、
        後始末はここ1つ。
        """
        self._has_document = False
        # C++ の `setDocument(QPdfDocument *)` は nullptr を受けるが、
        # PySide6 の型定義は None を含んでいない。
        self._model.setDocument(None)  # type: ignore[arg-type]
        self._reset_view_state()
        self._sync_empty_state()

    @property
    def has_outline(self) -> bool:
        """いま目次を出しているか（項目が1件以上あるか）。"""
        return self._model.rowCount(QModelIndex()) > 0

    @property
    def empty_text(self) -> str:
        """目次を出せないときに見せている文言。"""
        return self._empty_label.text()

    # -------------------------------------------------- 表示の同期
    def _on_model_reset(self) -> None:
        """モデルが階層を作り直したら、選択を捨てて空表示を取り直す。"""
        self._reset_view_state()
        self._sync_empty_state()

    def _reset_view_state(self) -> None:
        """選択と展開状態を捨てる。

        A の `QModelIndex` を B で使わないため。ドキュメントが変われば
        同じ行番号が別の項目を指すので、選択は跨いで持たない。
        """
        self._tree.selectionModel().clear()
        self._tree.setCurrentIndex(QModelIndex())
        self._tree.collapseAll()

    def _sync_empty_state(self) -> None:
        """目次の有無に合わせて、ツリーと空表示を入れ替える。"""
        has_outline = self.has_outline
        self._tree.setVisible(has_outline)
        self._empty_label.setVisible(not has_outline)
        self._empty_label.setText(NO_OUTLINE_TEXT if self._has_document else NO_DOCUMENT_TEXT)

    def _request_destination(self, index: QModelIndex) -> None:
        """項目が選ばれたら移動を要求するだけ。

        クリックでも Enter でもここを通る。倍率にもレンダリングにも
        学習マークにも履歴にも触らない。移動先として読めない行
        （壊れた outline）では何も要求しない。
        """
        destination = destination_for(index)
        if destination is not None:
            self.destination_requested.emit(destination)

    # -------------------------------------------------- 検査用
    @property
    def tree(self) -> QTreeView:
        """目次のツリー。クリックを再現するテストから使う。"""
        return self._tree

    @property
    def model(self) -> QPdfBookmarkModel:
        """目次のモデル。階層をそのまま検証するテストから使う。"""
        return self._model
