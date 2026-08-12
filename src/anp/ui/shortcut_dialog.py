"""キーボードショートカットの設定ダイアログ。

```
現在有効な割り当て
        ↓ 複製
ダイアログの下書き（draft）
        ↓ 編集 / 解除 / 既定値に戻す
      OK → 検証 → 呼び出し側が保存と反映を行う
```

**ここは下書きしか触らない。** 編集のたびに `QAction.setShortcut()` や
`QSettings.setValue()` を呼ばないので、キャンセルすれば何も残らない。
「既定値に戻す」も下書きを戻すだけで、その後キャンセルすれば元のまま。

保存も `QAction` への反映も行わないので、`Settings` も `ShortcutManager`
も受け取らない。受け取るのは割り当ての写しだけ。

**Apply ボタンは置かない。** OK / キャンセル / 既定値に戻す の3つで足りる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anp.ui.shortcuts import (
    SHORTCUT_SPECS,
    default_assignments,
    default_sequences,
    describe_conflicts,
    display_text,
    find_conflicts,
)

_TITLE = "キーボードショートカット"

_HEADERS = ("コマンド", "ショートカット", "既定")

_CONFLICT_TITLE = "ショートカットが重複しています"
_CONFLICT_BODY = "次のショートカットが重なっているため、適用できません。\n\n"

_HINT = "変更するコマンドを選び、割り当てたいキーを押してください。"

_EDIT_LABEL = "ショートカット"
_CLEAR_TEXT = "解除(&U)"
_RESTORE_TEXT = "既定値に戻す(&D)"

# 既定の大きさ。11 行が縦に収まり、3 列が省略されずに読める程度。
# 利用者が広げたければ広げられる（固定サイズにはしない）。
_DEFAULT_SIZE = (560, 520)

# 列の番号。表の組み立てと更新の両方から使うので名前で持つ。
_COLUMN_LABEL = 0
_COLUMN_SHORTCUT = 1
_COLUMN_DEFAULT = 2


class ShortcutDialog(QDialog):
    """ショートカットの割り当てを編集する。"""

    def __init__(
        self,
        assignments: Mapping[str, Sequence[QKeySequence]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_TITLE)

        # 渡された割り当ての写し。ここから先は外の状態に触らない。
        self._draft: dict[str, tuple[QKeySequence, ...]] = {
            spec.command_id: tuple(assignments.get(spec.command_id, ())) for spec in SHORTCUT_SPECS
        }

        # 表の選択に合わせて編集欄を書き戻すとき、その書き込みを下書きへ
        # 跳ね返さないための目印。
        self._loading = False

        self._build_body()
        self._table.selectRow(0)
        self.resize(*_DEFAULT_SIZE)

    # -------------------------------------------------- 組み立て
    def _build_body(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(_HINT, self)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._table = QTableWidget(len(SHORTCUT_SPECS), len(_HEADERS), self)
        self._table.setHorizontalHeaderLabels(list(_HEADERS))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for row, spec in enumerate(SHORTCUT_SPECS):
            self._table.setItem(row, _COLUMN_LABEL, QTableWidgetItem(spec.label))
            self._table.setItem(row, _COLUMN_SHORTCUT, QTableWidgetItem(""))
            self._table.setItem(
                row, _COLUMN_DEFAULT, QTableWidgetItem(display_text(default_sequences(spec)))
            )
            self._refresh_row(row)
        self._table.resizeColumnsToContents()
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self._table)

        layout.addLayout(self._build_editor())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._restore_button = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        self._restore_button.setText(_RESTORE_TEXT)
        self._restore_button.clicked.connect(self.restore_defaults)
        layout.addWidget(buttons)

    def _build_editor(self) -> QHBoxLayout:
        """選んだコマンドのショートカットを打鍵で入力する欄。

        `"Ctrl+Shift+X"` を手で打たせるのではなく、実際に押したキーを
        受け取る（`QKeySequenceEdit`）。
        """
        row = QHBoxLayout()

        form = QFormLayout()
        self._editor = QKeySequenceEdit(self)
        self._editor.keySequenceChanged.connect(self._on_sequence_changed)
        form.addRow(_EDIT_LABEL, self._editor)
        row.addLayout(form, 1)

        self._clear_button = QPushButton(_CLEAR_TEXT, self)
        self._clear_button.clicked.connect(self.clear_selected)
        row.addWidget(self._clear_button)

        return row

    # -------------------------------------------------- 下書きの編集
    @property
    def assignments(self) -> dict[str, tuple[QKeySequence, ...]]:
        """編集中の割り当て。OK の後に呼び出し側が読む。"""
        return dict(self._draft)

    @property
    def selected_command_id(self) -> str:
        """いま選ばれているコマンドの ID。

        行は構築時に選ぶので、選択が外れている状態は作らない（外れていれば
        先頭に戻す）。
        """
        row = self._table.currentRow()
        return SHORTCUT_SPECS[max(row, 0)].command_id

    def restore_defaults(self) -> None:
        """**下書きだけ**を既定値へ戻す。設定にも `QAction` にも書かない。"""
        self._draft = default_assignments()
        self._refresh_all()

    def clear_selected(self) -> None:
        """選ばれているコマンドの割り当てを外す（キーで起動しなくなる）。

        コマンド自体を無効にはしない。メニューからはこれまでどおり使える。
        """
        self._set_selected(())

    def _on_sequence_changed(self, sequence: QKeySequence) -> None:
        if self._loading:
            return
        # 打鍵での編集は1つの割り当てに置き換える。既定で複数持つコマンド
        # （拡大）の代替は、「既定値に戻す」で戻せる。
        self._set_selected(() if sequence.isEmpty() else (sequence,))

    def _set_selected(self, sequences: tuple[QKeySequence, ...]) -> None:
        self._draft[self.selected_command_id] = sequences
        self._refresh_row(self._table.currentRow())
        self._load_editor()

    # -------------------------------------------------- 表示の同期
    def _on_selection_changed(self) -> None:
        self._load_editor()

    def _load_editor(self) -> None:
        """編集欄を、選ばれているコマンドの下書きに合わせる。"""
        sequences = self._draft[self.selected_command_id]
        self._loading = True
        try:
            self._editor.setKeySequence(sequences[0] if sequences else QKeySequence())
        finally:
            self._loading = False

    def _refresh_all(self) -> None:
        for row in range(len(SHORTCUT_SPECS)):
            self._refresh_row(row)
        self._load_editor()

    def _refresh_row(self, row: int) -> None:
        command_id = SHORTCUT_SPECS[row].command_id
        self._cell(row, _COLUMN_SHORTCUT).setText(display_text(self._draft[command_id]))

    def _cell(self, row: int, column: int) -> QTableWidgetItem:
        """表のマス。全ての行と列は構築時に埋めてある。"""
        item = self._table.item(row, column)
        assert item is not None
        return item

    # -------------------------------------------------- 確定
    def accept(self) -> None:
        """下書き全体を検証してから閉じる。

        衝突があればダイアログを開いたままにする。**一部だけを適用する
        経路は作らない。**
        """
        conflicts = find_conflicts(self._draft)
        if conflicts:
            QMessageBox.warning(
                self, _CONFLICT_TITLE, _CONFLICT_BODY + describe_conflicts(conflicts)
            )
            return
        super().accept()

    # -------------------------------------------------- 検査用
    @property
    def table(self) -> QTableWidget:
        """コマンドの一覧。行の選択と表示を見るテストから使う。"""
        return self._table

    @property
    def editor(self) -> QKeySequenceEdit:
        """打鍵の入力欄。"""
        return self._editor

    @property
    def clear_button(self) -> QPushButton:
        """解除ボタン。"""
        return self._clear_button

    def shortcut_text(self, command_id: str) -> str:
        """表に出ているショートカットの文字列。"""
        return self._cell(_row_for(command_id), _COLUMN_SHORTCUT).text()

    def default_text(self, command_id: str) -> str:
        """表に出ている既定値の文字列。"""
        return self._cell(_row_for(command_id), _COLUMN_DEFAULT).text()

    def select(self, command_id: str) -> None:
        """コマンドを選ぶ。マウス操作の代わりにテストから使う。"""
        self._table.selectRow(_row_for(command_id))


def _row_for(command_id: str) -> int:
    """コマンド ID の行番号。表の行はレジストリと同じ並び。"""
    return next(index for index, spec in enumerate(SHORTCUT_SPECS) if spec.command_id == command_id)
