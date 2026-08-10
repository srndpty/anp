"""メインウィンドウのアクション定義。

`QAction` の生成とメニューへの割り付けだけを受け持つ。ここには
アクションの**実装**を置かない。何をするかは `MainWindow` が
`triggered` に接続して決める。

アクションの数が増えて `MainWindow` の構築コードが読みにくくなったので
分けただけで、コマンド機構やアクションフレームワークではない。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import QMenu, QMenuBar, QWidget


@dataclass(frozen=True, slots=True)
class ReaderActions:
    """リーダーのアクション一式。"""

    open: QAction
    quit: QAction
    about: QAction

    zoom_in: QAction
    zoom_out: QAction
    actual_size: QAction
    fit_width: QAction
    fit_page: QAction
    full_screen: QAction

    page_color_original: QAction
    page_color_invert: QAction
    page_color_group: QActionGroup
    """ページの色は排他選択。どちらか一方だけがチェックされる。"""

    previous_page: QAction
    next_page: QAction

    def set_document_dependent_enabled(self, *, enabled: bool) -> None:
        """ドキュメントが無いと意味のないアクションをまとめて切り替える。

        前/次ページは先頭・末尾でも変わるので、ここでは扱わない。
        """
        for action in (
            self.zoom_in,
            self.zoom_out,
            self.actual_size,
            self.fit_width,
            self.fit_page,
        ):
            action.setEnabled(enabled)


def _action(
    parent: QWidget,
    text: str,
    *,
    shortcuts: list[str] | None = None,
    checkable: bool = False,
) -> QAction:
    action = QAction(text, parent)
    if shortcuts:
        action.setShortcuts([QKeySequence(shortcut) for shortcut in shortcuts])
    action.setCheckable(checkable)
    return action


def create_actions(parent: QWidget) -> ReaderActions:
    """アクションを作る。親を渡すのは Qt の所有権をウィンドウに持たせるため。

    ページの色は排他なので `QActionGroup` にまとめる。グループも親に
    ぶら下げるので、`ReaderActions` が凍結されていても寿命は Qt 側で持つ。
    """
    page_color_original = _action(parent, "オリジナル(&O)", checkable=True)
    page_color_invert = _action(parent, "反転(&I)", checkable=True)
    page_color_group = QActionGroup(parent)
    page_color_group.setExclusive(True)
    page_color_group.addAction(page_color_original)
    page_color_group.addAction(page_color_invert)

    return ReaderActions(
        open=_action(parent, "開く(&O)...", shortcuts=["Ctrl+O"]),
        quit=_action(parent, "終了(&X)", shortcuts=["Ctrl+Q"]),
        about=_action(parent, "anp について(&A)"),
        # Ctrl++ は Shift が要るキーボードが多いので Ctrl+= も受ける。
        zoom_in=_action(parent, "拡大(&I)", shortcuts=["Ctrl++", "Ctrl+="]),
        zoom_out=_action(parent, "縮小(&O)", shortcuts=["Ctrl+-"]),
        actual_size=_action(parent, "実際の大きさ(&A)", shortcuts=["Ctrl+0"]),
        fit_width=_action(parent, "幅に合わせる(&W)", checkable=True),
        fit_page=_action(parent, "ページ全体(&P)", checkable=True),
        full_screen=_action(parent, "全画面表示(&F)", shortcuts=["F11"], checkable=True),
        page_color_original=page_color_original,
        page_color_invert=page_color_invert,
        page_color_group=page_color_group,
        # 通常の PageUp/PageDown はスクロール操作として空けておく。
        previous_page=_action(parent, "前のページ(&P)", shortcuts=["Ctrl+PgUp"]),
        next_page=_action(parent, "次のページ(&N)", shortcuts=["Ctrl+PgDown"]),
    )


def populate_menus(menu_bar: QMenuBar, actions: ReaderActions) -> None:
    """メニューバーを組み立てる。"""
    file_menu = menu_bar.addMenu("ファイル(&F)")
    file_menu.addAction(actions.open)
    file_menu.addSeparator()
    file_menu.addAction(actions.quit)

    view_menu = menu_bar.addMenu("表示(&V)")
    view_menu.addAction(actions.zoom_in)
    view_menu.addAction(actions.zoom_out)
    view_menu.addAction(actions.actual_size)
    view_menu.addSeparator()
    view_menu.addAction(actions.fit_width)
    view_menu.addAction(actions.fit_page)
    view_menu.addSeparator()
    # 親を明示して作る。`addMenu(title)` の戻り値だけを持つと、Python 側で
    # 参照が残らずサブメニューが破棄されてしまう。
    page_color_menu = QMenu("ページの色(&C)", view_menu)
    page_color_menu.addAction(actions.page_color_original)
    page_color_menu.addAction(actions.page_color_invert)
    view_menu.addMenu(page_color_menu)
    view_menu.addSeparator()
    view_menu.addAction(actions.full_screen)

    go_menu = menu_bar.addMenu("移動(&G)")
    go_menu.addAction(actions.previous_page)
    go_menu.addAction(actions.next_page)

    help_menu = menu_bar.addMenu("ヘルプ(&H)")
    help_menu.addAction(actions.about)
