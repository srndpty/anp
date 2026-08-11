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
    clear_recent: QAction
    """最近使ったファイルの一覧だけを空にする。他の設定には触らない。"""

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
    page_color_smart_dark: QAction
    page_color_group: QActionGroup
    """ページの色は排他選択。いずれか1つだけがチェックされる。"""

    canvas_black: QAction
    canvas_dark_gray: QAction
    canvas_white: QAction
    canvas_group: QActionGroup
    """キャンバスの色は排他選択。ページの色とは別のグループにする。"""

    ui_theme_system: QAction
    ui_theme_light: QAction
    ui_theme_dark: QAction
    ui_theme_group: QActionGroup
    """UI テーマは排他選択。外観の3つの軸は互いに混ぜない。"""

    previous_page: QAction
    next_page: QAction

    find: QAction
    """検索ドックを出して入力欄へフォーカスする（Ctrl+F）。"""

    find_next: QAction
    find_previous: QAction
    """次/前の検索結果へ移動する（F3 / Shift+F3）。

    P5-4 でショートカットを設定可能にするまでは固定のキーで持つ。

    ドキュメントの有無では切り替えない（`set_document_dependent_enabled()`
    に入れない）。結果が無ければコントローラ側で何も起きないので、
    「押せるのに何も起きない」以上の害はなく、有効/無効の情報源を
    検索結果の件数とドキュメントの2つに分けずに済む。
    """

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


def _exclusive_group(parent: QWidget, actions: list[QAction]) -> QActionGroup:
    """排他選択のグループを作る。

    グループも親にぶら下げるので、`ReaderActions` が凍結されていても
    寿命は Qt 側で持つ。
    """
    group = QActionGroup(parent)
    group.setExclusive(True)
    for action in actions:
        group.addAction(action)
    return group


def create_actions(parent: QWidget) -> ReaderActions:
    """アクションを作る。親を渡すのは Qt の所有権をウィンドウに持たせるため。

    外観の3つの軸（ページの色・キャンバス・UI テーマ）はそれぞれ独立した
    排他グループにする。1つにまとめると、軸をまたいで選択が外れてしまう。
    """
    page_color_original = _action(parent, "オリジナル(&O)", checkable=True)
    page_color_invert = _action(parent, "反転(&I)", checkable=True)
    page_color_smart_dark = _action(parent, "スマートダーク(&K)", checkable=True)

    canvas_black = _action(parent, "黒(&B)", checkable=True)
    canvas_dark_gray = _action(parent, "ダークグレー(&G)", checkable=True)
    canvas_white = _action(parent, "白(&W)", checkable=True)

    ui_theme_system = _action(parent, "システム(&S)", checkable=True)
    ui_theme_light = _action(parent, "ライト(&L)", checkable=True)
    ui_theme_dark = _action(parent, "ダーク(&D)", checkable=True)

    return ReaderActions(
        open=_action(parent, "開く(&O)...", shortcuts=["Ctrl+O"]),
        clear_recent=_action(parent, "最近使ったファイルをクリア(&C)"),
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
        page_color_smart_dark=page_color_smart_dark,
        page_color_group=_exclusive_group(
            parent, [page_color_original, page_color_invert, page_color_smart_dark]
        ),
        canvas_black=canvas_black,
        canvas_dark_gray=canvas_dark_gray,
        canvas_white=canvas_white,
        canvas_group=_exclusive_group(parent, [canvas_black, canvas_dark_gray, canvas_white]),
        ui_theme_system=ui_theme_system,
        ui_theme_light=ui_theme_light,
        ui_theme_dark=ui_theme_dark,
        ui_theme_group=_exclusive_group(parent, [ui_theme_system, ui_theme_light, ui_theme_dark]),
        # 通常の PageUp/PageDown はスクロール操作として空けておく。
        previous_page=_action(parent, "前のページ(&P)", shortcuts=["Ctrl+PgUp"]),
        next_page=_action(parent, "次のページ(&N)", shortcuts=["Ctrl+PgDown"]),
        find=_action(parent, "検索(&F)...", shortcuts=["Ctrl+F"]),
        find_next=_action(parent, "次を検索(&N)", shortcuts=["F3"]),
        find_previous=_action(parent, "前を検索(&V)", shortcuts=["Shift+F3"]),
    )


def populate_menus(
    menu_bar: QMenuBar,
    actions: ReaderActions,
    study_marks_toggle: QAction,
    toc_toggle: QAction,
    search_toggle: QAction,
    recent_menu: QMenu,
) -> None:
    """メニューバーを組み立てる。

    `study_marks_toggle`・`toc_toggle`・`search_toggle` はそれぞれのドックの
    `toggleViewAction()`。表示/非表示の状態はドック自身が持つので、
    `ReaderActions` には入れず受け取ったものをそのまま並べる。

    `recent_menu` も同じ扱い。中身は履歴が変わるたびに作り直されるので、
    ここでは場所を決めるだけで項目には触らない。
    """
    file_menu = menu_bar.addMenu("ファイル(&F)")
    file_menu.addAction(actions.open)
    file_menu.addMenu(recent_menu)
    file_menu.addSeparator()
    file_menu.addAction(actions.quit)

    view_menu = menu_bar.addMenu("表示(&V)")
    view_menu.addAction(study_marks_toggle)
    view_menu.addAction(toc_toggle)
    view_menu.addAction(search_toggle)
    view_menu.addSeparator()
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
    page_color_menu.addAction(actions.page_color_smart_dark)
    view_menu.addMenu(page_color_menu)

    canvas_menu = QMenu("キャンバス(&N)", view_menu)
    canvas_menu.addAction(actions.canvas_black)
    canvas_menu.addAction(actions.canvas_dark_gray)
    canvas_menu.addAction(actions.canvas_white)
    view_menu.addMenu(canvas_menu)

    ui_theme_menu = QMenu("UI テーマ(&U)", view_menu)
    ui_theme_menu.addAction(actions.ui_theme_system)
    ui_theme_menu.addAction(actions.ui_theme_light)
    ui_theme_menu.addAction(actions.ui_theme_dark)
    view_menu.addMenu(ui_theme_menu)
    view_menu.addSeparator()
    view_menu.addAction(actions.full_screen)

    go_menu = menu_bar.addMenu("移動(&G)")
    go_menu.addAction(actions.previous_page)
    go_menu.addAction(actions.next_page)
    go_menu.addSeparator()
    go_menu.addAction(actions.find)
    go_menu.addAction(actions.find_next)
    go_menu.addAction(actions.find_previous)

    help_menu = menu_bar.addMenu("ヘルプ(&H)")
    help_menu.addAction(actions.about)
