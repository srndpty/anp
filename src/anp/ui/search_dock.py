"""PDF 内テキスト検索のドック。

検索文字列の入力・前後への移動・件数の表示だけを受け持つ。

```
SearchDock
   ↓ query_changed / next_requested / previous_requested
MainWindow（配線だけ）
   ↓
PdfSearchController
```

**ここが持たないもの**: `QPdfDocument`・`QPdfSearchModel`・`PageLayout` の
算術・`QPainter` によるハイライト・レンダリングサービス・学習マーク・
SQLite・設定の読み書き・最近使ったファイル。

検索文字列も現在位置も **永続化しない**。再起動したときドックの表示状態は
ウィンドウ状態（`saveState()`）に従うが、検索語は空から始まる。検索専用の
設定キーは作らない。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from anp.ui.pdf_search_controller import NO_RESULT, SearchState

_TITLE = "検索"

# `saveState()` / `restoreState()` がドックを識別するための名前。
# 変えると保存済みのウィンドウ状態からドックを復元できなくなる。
DOCK_OBJECT_NAME = "searchDock"

_PLACEHOLDER = "PDF 内を検索"

# 一致が無いときの知らせ方。**エラーではない**のでダイアログは出さない。
# スキャンした画像だけの PDF もここに来る（OCR はしないので 0 件が正しい）。
NO_MATCH_TEXT = "一致するテキストはありません"


def result_label(state: SearchState) -> str:
    """件数の表示。利用者には 1 始まりで見せる。

    内部の番号は 0 始まり（`NO_RESULT` は -1）なので、読み替えはここだけで
    行う。結果が無いときは `0 / 0`。
    """
    current = 0 if state.current_index == NO_RESULT else state.current_index + 1
    return f"{current} / {state.count}"


class _SearchLineEdit(QLineEdit):
    """Enter / Shift+Enter を前後の移動として扱う入力欄。

    `returnPressed` は修飾キーを区別しないので、押鍵をそのまま見る。
    """

    next_requested = Signal()
    previous_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt の命名規則)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier:
                self.previous_requested.emit()
            else:
                self.next_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class SearchDock(QDockWidget):
    """検索文字列の入力と、結果の件数・前後への移動。"""

    query_changed = Signal(str)
    """検索文字列が変わった。

    **`strip()` しない**。入力されたままの文字列を渡す。
    """

    next_requested = Signal()
    previous_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(_TITLE, parent)
        # ウィンドウ状態の保存/復元がドックを識別するための名前。
        self.setObjectName(DOCK_OBJECT_NAME)

        self.setWidget(self._build_body())
        self.set_state(SearchState(query="", count=0, current_index=NO_RESULT, has_document=False))

    def _build_body(self) -> QWidget:
        body = QWidget(self)
        row = QHBoxLayout(body)
        row.setContentsMargins(4, 4, 4, 4)

        row.addWidget(QLabel("検索", body))

        self._query_edit = _SearchLineEdit(body)
        self._query_edit.setPlaceholderText(_PLACEHOLDER)
        # 打鍵のたびに検索し直す。`QPdfSearchModel` が非同期に結果を返すので、
        # ここでデバウンスを入れて複雑にしない（実測で問題が出たら考える）。
        self._query_edit.textChanged.connect(self.query_changed)
        self._query_edit.next_requested.connect(self.next_requested)
        self._query_edit.previous_requested.connect(self.previous_requested)
        row.addWidget(self._query_edit, 1)

        self._previous_button = QToolButton(body)
        self._previous_button.setText("↑")
        self._previous_button.setToolTip("前の結果 (Shift+F3)")
        self._previous_button.clicked.connect(self.previous_requested)
        row.addWidget(self._previous_button)

        self._next_button = QToolButton(body)
        self._next_button.setText("↓")
        self._next_button.setToolTip("次の結果 (F3)")
        self._next_button.clicked.connect(self.next_requested)
        row.addWidget(self._next_button)

        self._count_label = QLabel(body)
        row.addWidget(self._count_label)

        self._message_label = QLabel(body)
        row.addWidget(self._message_label)

        return body

    # -------------------------------------------------- 状態
    def set_state(self, state: SearchState) -> None:
        """検索の状態に合わせて表示と操作の可否を揃える。

        情報源は `PdfSearchController` の1つだけ。ここで件数を数え直したり、
        現在位置を覚えたりしない。

        入力欄の文字列はここでは書き換えない。利用者が打っている最中に
        カーソルが飛ぶのを避けるためで、文字列の持ち主は入力欄のまま。
        """
        self._query_edit.setEnabled(state.has_document)
        self._next_button.setEnabled(state.has_results)
        self._previous_button.setEnabled(state.has_results)
        self._count_label.setText(result_label(state))
        # 一致が無いことを伝えるのは、実際に検索したときだけ。空の入力欄に
        # 「一致しません」と出さない。
        self._message_label.setText(
            NO_MATCH_TEXT if state.has_document and state.query and state.count == 0 else ""
        )

    def clear_query(self) -> None:
        """入力欄を空にする。

        ドキュメントを切り替えたときに呼ぶ。`textChanged` 経由で
        `query_changed` が出るので、検索の状態も同じ経路で空になる。
        """
        self._query_edit.clear()

    def focus_query(self) -> None:
        """入力欄へフォーカスを移し、既存の検索語を選択状態にする。

        Ctrl+F の典型的な挙動。続けて打てば置き換わり、そのまま Enter を
        押せば同じ検索語で次へ進める。
        """
        self._query_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._query_edit.selectAll()

    # -------------------------------------------------- 検査用
    @property
    def query_edit(self) -> QLineEdit:
        """検索文字列の入力欄。打鍵を再現するテストから使う。"""
        return self._query_edit

    @property
    def next_button(self) -> QToolButton:
        """次の結果へ進むボタン。"""
        return self._next_button

    @property
    def previous_button(self) -> QToolButton:
        """前の結果へ戻るボタン。"""
        return self._previous_button

    @property
    def count_text(self) -> str:
        """件数の表示（`3 / 17` の形）。"""
        return self._count_label.text()

    @property
    def message_text(self) -> str:
        """一致が無いときの知らせ。無ければ空文字。"""
        return self._message_label.text()
