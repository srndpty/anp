"""学習マークの一覧サイドバー。

表示中の PDF の学習マークを一覧で見せ、間違えた回数で絞り込み、行を
クリックしたらその位置へ飛ぶよう要求する。

**ここはスナップショットの表示だけを受け持つ。** `StudyMarkRepository`
にも SQLite にも `document_key` にも触らないし、`PdfView` も持たない。
渡されたスナップショットは `StudyMarkController` が配るもので、
オーバーレイと同じ1つの情報源から来る。

```
StudyMarkController
        ↓ marks_changed
StudyMarkSidebar（絞り込み・行の組み立て）
        ↓ jump_requested
MainWindow（配線だけ）
        ↓
PdfView.reveal_page_position()
```

**行は毎回スナップショットから作り直す。** SQLite の
`INTEGER PRIMARY KEY` は最大 ID の行を消すと ID を再利用しうるので、
ID を選択状態の持ち主として跨って持ち回ると、別のマークが選ばれた
ことになりかねない。作り直しのたびに選択は落ちる。

**絞り込みは表示だけの状態。** DB にも `StudyMarkController` の
スナップショットにも `PdfView` のオーバーレイにも影響しない。
絞り込みを変えても問い合わせは1回も増えない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anp.storage.study_mark import StudyMark

# 「よく間違えている」とみなす下限。ドメインの値は変えないので、これは
# 絞り込みの条件でしかない（一覧に出す数字は常に実際の整数）。
AT_LEAST_THRESHOLD = 3

_TITLE = "学習マーク"

# `saveState()` / `restoreState()` がドックを識別するための名前。
# 変えると保存済みのウィンドウ状態からドックを復元できなくなる。
DOCK_OBJECT_NAME = "studyMarksDock"

_COLUMNS = ("ページ", "回数", "メモ")

_FILTER_LABELS = {
    "all": "すべて",
    "exact": "回数を指定",
    "at_least": f"{AT_LEAST_THRESHOLD}回以上",
}


class FilterMode(Enum):
    """絞り込みの種類。

    「3回以上」を EXACT(3) と同じ扱いにしないために、指定回数ちょうどと
    下限つきを別の種類にする。
    """

    ALL = "all"
    """すべて表示する。"""

    EXACT = "exact"
    """`mistake_count` が指定した回数**ちょうど**のものだけ。"""

    AT_LEAST_3 = "at_least_3"
    """`mistake_count` が 3 以上のもの。"""


@dataclass(frozen=True, slots=True)
class MarkFilter:
    """一覧の絞り込み条件。

    ウィジェットから切り離した純粋な値なので、`QApplication` なしで
    条件そのものを検証できる。
    """

    mode: FilterMode = FilterMode.ALL
    exact_count: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.exact_count, bool) or not isinstance(self.exact_count, int):
            msg = f"exact_count must be int, got {type(self.exact_count).__name__}"
            raise TypeError(msg)
        # 保存される `mistake_count` は 1 以上なので、0 以下の指定は誤り。
        if self.exact_count < 1:
            msg = f"exact_count must be >= 1, got {self.exact_count}"
            raise ValueError(msg)

    def matches(self, mark: StudyMark) -> bool:
        """このマークを一覧に出すか。

        EXACT は **ちょうどその回数だけ**。3 を指定しても「3 回以上」には
        ならない。
        """
        if self.mode is FilterMode.ALL:
            return True
        if self.mode is FilterMode.EXACT:
            return mark.mistake_count == self.exact_count
        return mark.mistake_count >= AT_LEAST_THRESHOLD

    def apply(self, marks: Sequence[StudyMark]) -> tuple[StudyMark, ...]:
        """条件に合うものだけを、渡された並びのまま返す。"""
        return tuple(mark for mark in marks if self.matches(mark))


def note_preview(note: str | None) -> str:
    """一覧の1行に収まる形のメモ。

    改行を空白へ置き換えるだけの **表示上の加工**。保存されている文字列は
    変えないし、加工した値をドメインへ書き戻すこともしない。
    """
    if note is None:
        return ""
    return note.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


class StudyMarkSidebar(QDockWidget):
    """学習マークの一覧・絞り込み・移動要求。"""

    jump_requested = Signal(object)
    """行がクリックされた（対象の `StudyMark`）。

    移動そのものはここでは行わない。ページ配置の算術は `PdfView` にあり、
    サイドバーはビューを持たない。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_TITLE, parent)
        # ウィンドウ状態の保存/復元がドックを識別するための名前。
        self.setObjectName(DOCK_OBJECT_NAME)

        self._marks: tuple[StudyMark, ...] = ()
        self._rows: tuple[StudyMark, ...] = ()
        self._filter = MarkFilter()

        self.setWidget(self._build_body())
        self._rebuild_rows()

    def _build_body(self) -> QWidget:
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)

        layout.addLayout(self._build_filter_row(body))

        self._tree = QTreeWidget(body)
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(list(_COLUMNS))
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        # 並びはスナップショットのまま（ページ順・作成順）。利用者による
        # 並べ替えは P4-1 では持たない。
        self._tree.setSortingEnabled(False)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._tree)

        return body

    def _build_filter_row(self, parent: QWidget) -> QHBoxLayout:
        """絞り込みの操作列。

        内部の契約は ALL / EXACT(n>=1) / AT_LEAST_3 の3つだけ。見た目は
        コンボボックスと回数の入力に割り振っているだけで、条件そのものは
        `MarkFilter` が持つ。
        """
        row = QHBoxLayout()
        row.addWidget(QLabel("絞り込み", parent))

        self._mode_box = QComboBox(parent)
        self._mode_box.addItem(_FILTER_LABELS["all"], FilterMode.ALL)
        self._mode_box.addItem(_FILTER_LABELS["exact"], FilterMode.EXACT)
        self._mode_box.addItem(_FILTER_LABELS["at_least"], FilterMode.AT_LEAST_3)
        self._mode_box.currentIndexChanged.connect(self._on_filter_ui_changed)
        row.addWidget(self._mode_box)

        self._count_box = QSpinBox(parent)
        self._count_box.setMinimum(1)
        self._count_box.setMaximum(9999)
        self._count_box.setKeyboardTracking(False)
        self._count_box.setEnabled(False)
        self._count_box.valueChanged.connect(self._on_filter_ui_changed)
        row.addWidget(self._count_box)

        row.addStretch(1)
        return row

    # -------------------------------------------------- スナップショット
    @property
    def marks(self) -> tuple[StudyMark, ...]:
        """受け取っているスナップショット全体（絞り込み前）。"""
        return self._marks

    @property
    def rows(self) -> tuple[StudyMark, ...]:
        """いま一覧に出ている行に対応するマーク（上から順）。"""
        return self._rows

    def set_marks(self, marks: Sequence[StudyMark]) -> None:
        """表示するスナップショットを差し替える。

        呼ぶのは `StudyMarkController.marks_changed` だけ。ここから
        リポジトリを読みに行くことも、`PdfView` を覗くこともしない。

        行は毎回作り直し、選択は落とす。ID を跨いで選択を復元すると、
        SQLite の ID 再利用で別のマークが選ばれることがある。
        """
        self._marks = tuple(marks)
        self._rebuild_rows()

    # -------------------------------------------------- 絞り込み
    @property
    def mark_filter(self) -> MarkFilter:
        """いまの絞り込み条件。"""
        return self._filter

    def set_filter(self, mark_filter: MarkFilter) -> None:
        """絞り込み条件を変える。

        **これは表示だけの状態**。DB へ問い合わせないし、コントローラの
        スナップショットにもオーバーレイにも触らない。
        """
        self._filter = mark_filter
        self._sync_filter_ui()
        self._rebuild_rows()

    def _sync_filter_ui(self) -> None:
        """絞り込みの操作列を、いまの条件に合わせる。

        表示を合わせるだけなので、変更として跳ね返らないよう黙らせる
        （`MainWindow._sync_page_ui()` と同じ形）。
        """
        blocked = self._mode_box.blockSignals(True)
        try:
            self._mode_box.setCurrentIndex(self._mode_box.findData(self._filter.mode))
        finally:
            self._mode_box.blockSignals(blocked)

        blocked = self._count_box.blockSignals(True)
        try:
            self._count_box.setValue(self._filter.exact_count)
        finally:
            self._count_box.blockSignals(blocked)

        self._count_box.setEnabled(self._filter.mode is FilterMode.EXACT)

    def _on_filter_ui_changed(self) -> None:
        """操作列の変更を条件へ写す。"""
        self.set_filter(
            MarkFilter(mode=self._mode_box.currentData(), exact_count=self._count_box.value())
        )

    # -------------------------------------------------- 行
    def _rebuild_rows(self) -> None:
        """いまのスナップショットと条件から一覧を作り直す。

        差分更新はしない。1つの PDF のマークは多くても数千件で、作り直しは
        一瞬で終わる。差分を追うと、行と実体がずれる余地を作ってしまう。
        """
        self._rows = self._filter.apply(self._marks)

        self._tree.setUpdatesEnabled(False)
        try:
            self._tree.clear()
            self._tree.addTopLevelItems([_row_item(mark) for mark in self._rows])
        finally:
            self._tree.setUpdatesEnabled(True)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """行のクリックは移動の要求だけ。保存には一切触らない。

        追加・回数の増加・メモ・削除は PDF 上の操作（`StudyMarkInteraction`）
        のままで、サイドバーからは行わない。
        """
        mark = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(mark, StudyMark):
            self.jump_requested.emit(mark)

    # -------------------------------------------------- 検査用
    @property
    def tree(self) -> QTreeWidget:
        """一覧のウィジェット。クリックを再現するテストから使う。"""
        return self._tree


def _row_item(mark: StudyMark) -> QTreeWidgetItem:
    """1件分の行。

    ページ番号だけ 1 始まりにして見せる（ドメインは 0 始まりのまま）。
    間違えた回数は丸めずそのままの整数を出す。

    行が指すマークは項目自身に持たせる。移動のときはここから取り出すので、
    絞り込み後の行番号をページ番号と取り違える余地が無い。
    """
    item = QTreeWidgetItem(
        [str(mark.page_index + 1), str(mark.mistake_count), note_preview(mark.note)]
    )
    item.setData(0, Qt.ItemDataRole.UserRole, mark)
    if mark.note:
        item.setToolTip(2, mark.note)
    return item
