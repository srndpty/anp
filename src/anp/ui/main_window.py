"""メインウィンドウ。

PDF を開き、`PdfView` を中央に据えて、ズーム・ページ移動・全画面表示の
操作をつなぐ。

**レンダリングとキャッシュの内部にはここから触らない。** `RenderCache` と
`PageRenderService` は生成して `PdfView` に渡すだけで、`RenderKey`・
レンダリング世代・要求の待ち行列・DPR ごとの要求サイズは PDF/ビュー層に
閉じている。ここが知ってよいのは、開いているパス・倍率とそのモード・
現在ページ・ページ数・全画面かどうかだけ。

**学習マークの永続化にもここから触らない。** SQLite の接続はアプリケーション
層が持ち、ここが受け取るのは `StudyMarkRepository` だけ。どの PDF のマークを
表示するかは `StudyMarkController` が決める。ここの仕事は「PDF が開けた」
「表示が空になった」を伝えることまで。マークの追加・更新・削除の操作は
`StudyMarkInteraction` が受け持ち、一覧・絞り込み・移動の要求は
`StudyMarkSidebar` が受け持つので、ここには接続しかない。

一覧へマークを渡すのも `StudyMarkController` の通知経由だけ。ここから
`PdfView` とサイドバーを別々に更新しない（オーバーレイと一覧が食い違う
状態を作れないようにするため）。

**PDF の目次にもここから触らない。** outline を読むのは `TocSidebar` が
持つ `QPdfBookmarkModel` で、移動先からスクロール位置を決めるのは
`PdfView`。ここの仕事は「この PDF の目次を出せ」と伝えることと、
移動の要求をビューへ渡すことまで。

**テキスト検索にもここから触らない。** 検索するのは
`PdfSearchController` が持つ `QPdfSearchModel`（pdfium）で、ハイライトと
移動は `PdfView`。ここの仕事は配線と、ドキュメントの着脱を伝えることまで。
検索の算術も geometry もここには書かない。

外観のうち **UI テーマだけ**をここが持つ。アプリ全体のウィジェットの
配色はビューの責務ではないため。キャンバスの色は `PdfView`、ページの
色変換は `PageRenderService` が持ち、3つは互いに連動しない。

**PDF を開く経路は `_open_document()` の1本だけ。** 利用者が選んだ場合も、
最近使ったファイルからの場合も、起動時の自動復元の場合も同じ手順を通る。
違うのは「履歴を更新するか」と「失敗をダイアログで知らせるか」の2点
だけなので、そこだけを引数で分ける（復元専用の open を作らない）。
"""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QWidget,
)

from anp import __version__
from anp.core.settings import Settings
from anp.pdf.cache import RenderCache
from anp.pdf.color import PageColorMode
from anp.pdf.destination import PdfDestination
from anp.pdf.document import DocumentController, DocumentError
from anp.pdf.render import PageRenderService
from anp.storage.study_mark import StudyMark
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.actions import ReaderActions, create_actions, populate_menus
from anp.ui.appearance import CanvasTheme, UiTheme, apply_ui_theme
from anp.ui.pdf_search_controller import PdfSearchController, SearchState
from anp.ui.pdf_view import PdfView, ReadingPosition, ZoomMode
from anp.ui.recent_files import add_recent, normalize_recent, recent_labels, remove_recent
from anp.ui.search_dock import SearchDock
from anp.ui.study_mark_controller import StudyMarkController, StudyMarkLoadError
from anp.ui.study_mark_interaction import StudyMarkInteraction
from anp.ui.study_mark_sidebar import StudyMarkSidebar
from anp.ui.toc_sidebar import TocSidebar

logger = logging.getLogger(__name__)

_DEFAULT_SIZE = (1000, 800)

_PDF_FILTER = "PDF ファイル (*.pdf)"

_OPEN_ERROR_TITLE = "PDF を開けません"

# 学習マークを読み込めなかったときの知らせ方。開くのを中止した理由と、
# 記録そのものは消えていないことを書く（利用者がいちばん恐れるのはそこ）。
_MARK_LOAD_ERROR_TITLE = "学習マークを読み込めません"
_MARK_LOAD_ERROR = (
    "この PDF の学習マークを読み込めなかったため、開くのを中止しました。\n"
    "記録は削除されていません。ログを確認してください。"
)

# 一覧から移動できなかったときの知らせ方。読み進めるのを妨げないよう、
# ダイアログではなくステータスバーへ一時的に出す。
_STALE_MARK_MESSAGE = "このマークのページは現在の PDF にありません"
_TRANSIENT_MESSAGE_MS = 5000

# 目次の項目が現在の PDF に無いページを指していたときの知らせ方。壊れた
# outline は PDF 側の事情なので、ダイアログではなくステータスバーへ。
_STALE_DESTINATION_MESSAGE = "この目次の移動先は現在の PDF にありません"

# ステータスバーの常設表示。一時メッセージ（`showMessage()`）が消えた後も
# 開いている PDF が分かるよう、パスは常設ウィジェットに置く。
_NO_DOCUMENT_STATUS = "PDF が開かれていません"

_RECENT_MENU_TITLE = "最近使ったファイル(&R)"
_RECENT_EMPTY_LABEL = "最近使ったファイルはありません"

# 倍率モードの表示名。FREE はパーセント表示なのでここには無い。
_ZOOM_MODE_LABELS = {
    ZoomMode.FIT_WIDTH: "幅に合わせる",
    ZoomMode.FIT_PAGE: "ページ全体",
}


class MainWindow(QMainWindow):
    """アプリケーションのメインウィンドウ。"""

    def __init__(
        self,
        settings: Settings,
        study_marks: StudyMarkRepository,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings

        # 全画面から戻るときに復元する状態。全画面へ入る直前に記録する。
        self._maximized_before_full_screen = False

        # 最近使ったファイル。並び・重複・件数の契約は `recent_files` に
        # あるので、ここが持つのは「いまの一覧」だけ。読み込んだ時点で
        # 契約の形に揃えるので、設定を手で書き換えられていても、次に
        # PDF を開くまで重複や上限超えが残ることはない。
        self._recent: tuple[Path, ...] = normalize_recent(
            [Path(stored) for stored in self._settings.recent_files]
        )

        # 前回のセッションを復元したか。最初に表示されたときの1回だけ行う。
        self._session_restored = False

        # UI テーマはアプリ全体の外観なので、ビューではなくここが持つ。
        # `QStyleHints` は「指定を外した」状態を読み出せないので、選ばれた
        # テーマの情報源はこの属性になる。
        #
        # 適用先が `QApplication` 全体である一方、選択はウィンドウが持つ。
        # ウィンドウが1つしかないうちは一致するが、複数ウィンドウにするなら
        # この state をアプリケーション層へ上げる必要がある。
        self._ui_theme = UiTheme.SYSTEM

        # ウィンドウ状態の変化を `changeEvent()` で拾うので、ウィジェットを
        # 作る前にアクションを用意しておく。
        self._actions = create_actions(self)

        # PDF 層。所有権は Qt の親子関係（サービス）と Python の参照（残り）で持つ。
        self._controller = DocumentController()
        self._cache = RenderCache()
        self._render = PageRenderService(self._cache, parent=self)

        self._view = PdfView(self._render, self)
        self.setCentralWidget(self._view)

        # 学習マークはこのコントローラ越しにだけ触る。ここに
        # `document_key()` や SQL を持ち込まない。DB 接続はアプリケーション
        # 層が持ち、ここはリポジトリを受け取るだけ。
        self._study_marks = StudyMarkController(study_marks, self._view)

        # マウス操作・メニュー・ダイアログは別に分ける。ウィンドウの関心
        # （PDF を開く・ズーム・ページ移動）と混ぜない。
        self._study_mark_interaction = StudyMarkInteraction(self._view, self._study_marks, self)

        self._create_study_mark_sidebar()
        self._create_toc_sidebar()
        self._create_search_dock()

        self.setWindowTitle("anp")
        # 履歴のサブメニューはここが所有する。`addMenu(title)` の戻り値だけを
        # 持つと Python 側の参照が消えてメニューが破棄されるので、親を明示する。
        self._recent_menu = QMenu(_RECENT_MENU_TITLE, self)
        populate_menus(
            self.menuBar(),
            self._actions,
            self._marks_sidebar.toggleViewAction(),
            self._toc_sidebar.toggleViewAction(),
            self._search_dock.toggleViewAction(),
            self._recent_menu,
        )
        self._create_toolbar()
        self._create_status_bar()
        self._connect_actions()
        self._rebuild_recent_menu()

        self._view.current_page_changed.connect(self._on_current_page_changed)
        self._view.zoom_changed.connect(self._sync_zoom_ui)
        self._view.page_color_mode_changed.connect(self._sync_page_color_ui)
        self._view.canvas_theme_changed.connect(self._sync_canvas_ui)

        self._install_escape_shortcut()
        self._restore_window_state()
        self._restore_zoom()
        # 外観の3つの軸は互いに独立なので、復元の順序に依存しない。
        self._restore_ui_theme()
        self._restore_canvas_theme()
        self._restore_page_color_mode()
        self._sync_document_ui()

    # -------------------------------------------------- 構築
    def _create_study_mark_sidebar(self) -> None:
        """学習マークの一覧を右のドックへ入れて配線する。

        ここが書くのは配線だけ。絞り込みも行の組み立ても移動の計算も
        持たない（それぞれ `StudyMarkSidebar` と `PdfView` にある）。

        **初期状態は非表示。** PDF を読む領域を今までどおり広く保ち、
        必要な人が「表示 → 学習マーク」から開ける形にする。保存済みの
        ウィンドウ状態があれば `restoreState()` が表示/非表示ごと復元する
        ので、サイドバー専用の設定キーは作らない。

        一覧へマークを渡すのはコントローラの通知だけ。ここから
        `set_marks()` を直接呼ぶのは、通知の届かない起動時の1回に限る。
        """
        self._marks_sidebar = StudyMarkSidebar(self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._marks_sidebar)
        self._marks_sidebar.hide()

        self._study_marks.marks_changed.connect(self._marks_sidebar.set_marks)
        self._marks_sidebar.jump_requested.connect(self._jump_to_mark)
        self._marks_sidebar.set_marks(self._study_marks.study_marks)

    def _jump_to_mark(self, mark: StudyMark) -> None:
        """一覧で選ばれたマークの位置まで移動する。

        現在の PDF に無いページを指すマーク（ページ数の減った PDF に
        差し替えられた場合）では、`reveal_page_position()` が `False` を
        返す。行は消さず、警告ダイアログも出さず、ステータスバーで
        知らせるだけにする。移動できなかっただけで、記録は残っている。
        """
        if not self._view.reveal_page_position(mark.page_index, mark.x_norm, mark.y_norm):
            self.statusBar().showMessage(_STALE_MARK_MESSAGE, _TRANSIENT_MESSAGE_MS)

    def _create_toc_sidebar(self) -> None:
        """PDF の目次を左のドックへ入れて配線する。

        ここが書くのは配線だけ。outline の階層も行の組み立ても移動の計算も
        持たない（それぞれ `TocSidebar` の `QPdfBookmarkModel` と
        `PdfView` にある）。

        学習マークが右なので、目次は左に置く（Left: 目次 / Center: PDF /
        Right: 学習マーク）。**初期状態は非表示**で、保存済みのウィンドウ
        状態があれば `restoreState()` の値が勝つ。学習マークのドックと
        同じ扱いなので、目次専用の設定キーは作らない。
        """
        self._toc_sidebar = TocSidebar(self)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._toc_sidebar)
        self._toc_sidebar.hide()

        self._toc_sidebar.destination_requested.connect(self._navigate_to_destination)

    def _navigate_to_destination(self, destination: PdfDestination) -> None:
        """目次で選ばれた移動先まで移動する。

        ここに `location.y` とページ高さの計算やスクロールバーの算術を
        書かない。ページ配置の変換は `PdfView` に閉じている。

        壊れた outline が現在の PDF に無いページを指していた場合は
        `False` が返る。最終ページへ丸めず、警告ダイアログも出さず、
        ステータスバーで知らせるだけにする（学習マークの移動と同じ形）。
        """
        if not self._view.navigate_to_pdf_destination(destination):
            self.statusBar().showMessage(_STALE_DESTINATION_MESSAGE, _TRANSIENT_MESSAGE_MS)

    def _create_search_dock(self) -> None:
        """テキスト検索のコントローラと UI を作って配線する。

        ここが書くのは配線だけ。検索そのものは `QPdfSearchModel`、
        ハイライトと移動は `PdfView` にある。

        ドックは下に置く（Left: 目次 / Center: PDF / Right: 学習マーク /
        Bottom: 検索）。**初期状態は非表示**で、保存済みのウィンドウ状態が
        あれば `restoreState()` の値が勝つ。他の2つのドックと同じ扱いなので、
        検索専用の設定キーは作らない。

        検索モデルは **PDF を開くたびにコントローラが作り直す**（同じ
        `QPdfSearchModel` を、使い回しの `QPdfDocument` へ付け直すと落ちる）。
        ビューの参照は `model_changed` で張り替える。ここで渡すのは最初の
        1つだけ。
        """
        self._search = PdfSearchController(self)
        self._search_dock = SearchDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._search_dock)
        self._search_dock.hide()

        self._view.set_search_model(self._search.model)
        self._search.model_changed.connect(self._view.set_search_model)

        self._search_dock.query_changed.connect(self._search.set_query)
        self._search_dock.next_requested.connect(self._search.next_result)
        self._search_dock.previous_requested.connect(self._search.previous_result)

        self._search.state_changed.connect(self._on_search_state_changed)
        self._search.result_activated.connect(self._view.reveal_pdf_search_result)

    def _on_search_state_changed(self, state: SearchState) -> None:
        """検索の状態を、件数表示とハイライトの両方へ配る。

        情報源は `PdfSearchController` の1つだけ。ここから件数を数え直したり、
        UI とビューを別々に更新したりしない（表示と強調がずれる状態を
        作れないようにするため）。
        """
        self._search_dock.set_state(state)
        self._view.set_current_search_result(state.current_index)

    def _show_search(self) -> None:
        """検索ドックを出して入力欄へフォーカスする（Ctrl+F）。

        既に開いていても呼べる。入力欄の既存の検索語は選択状態になるので、
        続けて打てば置き換わり、そのまま Enter を押せば次の結果へ進む。
        """
        self._search_dock.show()
        self._search_dock.raise_()
        self._search_dock.focus_query()

    def _create_toolbar(self) -> None:
        """ズームとページ移動のツールバー。見た目には凝らない。"""
        toolbar = QToolBar("ツールバー", self)
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        toolbar.addAction(self._actions.open)
        toolbar.addSeparator()
        toolbar.addAction(self._actions.zoom_out)
        self._zoom_label = QLabel("100%", self)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_label.setMinimumWidth(96)
        toolbar.addWidget(self._zoom_label)
        toolbar.addAction(self._actions.zoom_in)
        toolbar.addAction(self._actions.actual_size)
        toolbar.addAction(self._actions.fit_width)
        toolbar.addAction(self._actions.fit_page)

        toolbar.addSeparator()
        toolbar.addAction(self._actions.previous_page)

        # ページ番号は 1 始まりで見せる。内部の 0 始まりとの変換はこの
        # ウィジェットの周りだけで行う。
        self._page_input = QSpinBox(self)
        # 打鍵のたびに飛ばないよう、確定（Enter・フォーカス移動）まで待つ。
        self._page_input.setKeyboardTracking(False)
        self._page_input.setMinimum(1)
        self._page_input.valueChanged.connect(self._on_page_input_changed)
        toolbar.addWidget(self._page_input)

        self._page_count_label = QLabel("/ 0", self)
        toolbar.addWidget(self._page_count_label)
        toolbar.addAction(self._actions.next_page)

    def _create_status_bar(self) -> None:
        """開いている PDF のパスを常設ウィジェットとして置く。

        `showMessage()` は普通のウィジェットを隠すが、常設ウィジェットは
        隠さない。学習マークの一時メッセージが5秒で消えた後も、パスの
        表示が戻る（消えたままにならない）のはこのため。
        """
        self._path_label = QLabel(_NO_DOCUMENT_STATUS, self)
        self.statusBar().addPermanentWidget(self._path_label)

    # -------------------------------------------------- 最近使ったファイル
    def _rebuild_recent_menu(self) -> None:
        """履歴のサブメニューを、いまの一覧から作り直す。

        差分更新はしない。`QMenu.clear()` が前回の項目（メニューが親）を
        破棄するので、古い `QAction` も接続も残らない。「クリア」だけは
        ウィンドウが持つアクションなので、外れるだけで生き残る。

        項目が持つのは **パスそのもの**。表示文字列からパスを逆算しない。
        """
        self._recent_menu.clear()

        if not self._recent:
            empty = self._recent_menu.addAction(_RECENT_EMPTY_LABEL)
            empty.setEnabled(False)
            return

        for path, label in zip(self._recent, recent_labels(self._recent), strict=True):
            # メニューは `&` をニーモニックとして食べるので、ファイル名に
            # 含まれる分は二重にして見せる。
            action = self._recent_menu.addAction(label.replace("&", "&&"))
            action.setStatusTip(str(path))
            action.setToolTip(str(path))
            action.triggered.connect(partial(self.open_path, path))

        self._recent_menu.addSeparator()
        self._recent_menu.addAction(self._actions.clear_recent)

    def _set_recent(self, paths: tuple[Path, ...]) -> None:
        """履歴を差し替え、保存とメニューを揃える唯一の経路。"""
        self._recent = paths
        self._settings.recent_files = [str(path) for path in paths]
        self._rebuild_recent_menu()

    def _clear_recent(self) -> None:
        """履歴だけを空にする。

        前回のセッション・学習マーク・最後のディレクトリ・ウィンドウ状態・
        外観のいずれにも触らない。
        """
        self._set_recent(())

    def _connect_actions(self) -> None:
        self._actions.open.triggered.connect(self._prompt_open)
        self._actions.clear_recent.triggered.connect(self._clear_recent)
        self._actions.quit.triggered.connect(self.close)
        self._actions.about.triggered.connect(self._show_about)

        self._actions.zoom_in.triggered.connect(self._view.zoom_in)
        self._actions.zoom_out.triggered.connect(self._view.zoom_out)
        self._actions.actual_size.triggered.connect(lambda: self._view.set_zoom(1.0))
        self._actions.fit_width.triggered.connect(lambda: self._apply_fit(ZoomMode.FIT_WIDTH))
        self._actions.fit_page.triggered.connect(lambda: self._apply_fit(ZoomMode.FIT_PAGE))
        self._actions.full_screen.triggered.connect(self._set_full_screen)

        # 選択表示はビューの `page_color_mode_changed` から取り直すので、
        # ここでは切り替えを伝えるだけ。ビュー側の API を直接使う経路でも
        # メニューの表示がずれない。
        self._actions.page_color_original.triggered.connect(
            lambda: self._view.set_page_color_mode(PageColorMode.ORIGINAL)
        )
        self._actions.page_color_invert.triggered.connect(
            lambda: self._view.set_page_color_mode(PageColorMode.INVERT)
        )
        self._actions.page_color_smart_dark.triggered.connect(
            lambda: self._view.set_page_color_mode(PageColorMode.SMART_DARK)
        )

        # キャンバスと UI テーマも同様に、状態を変えてから選択表示を取り直す。
        # ページの色に触らないので、3つの軸は独立したまま。
        self._actions.canvas_black.triggered.connect(
            lambda: self._view.set_canvas_theme(CanvasTheme.BLACK)
        )
        self._actions.canvas_dark_gray.triggered.connect(
            lambda: self._view.set_canvas_theme(CanvasTheme.DARK_GRAY)
        )
        self._actions.canvas_white.triggered.connect(
            lambda: self._view.set_canvas_theme(CanvasTheme.WHITE)
        )

        self._actions.ui_theme_system.triggered.connect(lambda: self.set_ui_theme(UiTheme.SYSTEM))
        self._actions.ui_theme_light.triggered.connect(lambda: self.set_ui_theme(UiTheme.LIGHT))
        self._actions.ui_theme_dark.triggered.connect(lambda: self.set_ui_theme(UiTheme.DARK))

        self._actions.previous_page.triggered.connect(
            lambda: self._view.go_to_page(self._view.current_page - 1)
        )
        self._actions.next_page.triggered.connect(
            lambda: self._view.go_to_page(self._view.current_page + 1)
        )

        self._actions.find.triggered.connect(self._show_search)
        self._actions.find_next.triggered.connect(self._search.next_result)
        self._actions.find_previous.triggered.connect(self._search.previous_result)

    def _apply_fit(self, mode: ZoomMode) -> None:
        """フィットを適用し、選択表示を取り直す。

        チェック可能なアクションは押されるたびにチェックが反転する。同じ
        モードのまま押された場合はビューの状態が変わらず `zoom_changed` が
        出ないので、勝手に外れたチェックをここで戻す。
        """
        if mode is ZoomMode.FIT_WIDTH:
            self._view.fit_width()
        else:
            self._view.fit_page()
        self._sync_zoom_ui()

    # -------------------------------------------------- UI テーマ
    @property
    def ui_theme(self) -> UiTheme:
        """アプリのウィジェットに適用している配色。"""
        return self._ui_theme

    def set_ui_theme(self, theme: UiTheme) -> None:
        """UI テーマを切り替える。

        **PDF の表示には一切影響しない。** キャンバスもページの色変換も
        ここからは触らない。「UI が暗いからページを反転する」といった
        連動は行わない。

        同じテーマでも適用し直すのは、`QStyleHints` の状態が外部から
        変えられていても、選ばれたテーマに揃え直すため。
        """
        self._ui_theme = theme
        apply_ui_theme(theme)
        self._sync_ui_theme_ui()

    def _install_escape_shortcut(self) -> None:
        """Esc で全画面を抜ける。通常表示中は何もしない。"""
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(self._exit_full_screen)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "anp について",
            f"anp {__version__}\n\n学習向けPDFリーダー",
        )

    # -------------------------------------------------- PDF を開く
    def _prompt_open(self) -> None:
        """ファイル選択ダイアログを出して PDF を開く。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "PDF を開く", self._settings.last_directory, _PDF_FILTER
        )
        if path:
            self.open_path(Path(path))

    def open_path(self, path: Path) -> None:
        """利用者の操作で PDF を開く。

        「開く...」からでも履歴からでも、通る手順はここで同じ。履歴専用の
        読み込み経路は作らない。

        履歴を更新するのは **PDF と学習マークの両方が読めて、開く操作が
        最後まで成功したとき** だけ。開けなかった PDF が履歴の先頭に
        並ぶことはない。

        失敗したときは、**ファイルが無くなっていた場合に限り** その項目を
        履歴から外す。何度押しても同じように失敗する項目を残さないため。
        「開けなかった」だけの場合は残す（一時的な事情かもしれないし、
        差し替えれば次は開ける）。
        """
        if self._open_document(path, notify=True):
            self._set_recent(add_recent(self._recent, path))
        elif not path.exists():
            self._set_recent(remove_recent(self._recent, path))

    def _open_document(self, path: Path, *, notify: bool) -> bool:
        """PDF を開く。開けたかを返す。失敗したら表示を空にする。

        `notify` は失敗を利用者に知らせるかどうか。利用者が明示的に開いた
        ときは知らせるが、起動時の自動復元では出さない（起動した瞬間に
        ダイアログが積み上がるのを避ける。ログには必ず残る）。

        `DocumentController.open()` は失敗時に前のドキュメントも閉じるので、
        表示だけ古い PDF のまま残すと、見えている内容と実体がずれる。
        学習マークも同じ理由で、失敗時は表示対象ごと解除する。

        学習マークを読み込むのは **表示が新しい PDF に確定した後**。
        `set_document()` より前に読み込むと、ドキュメントの差し替えで
        そのまま捨てられる。

        学習マークを読み込めなかった場合は、その PDF を開いた状態にも
        しない（fail-closed）。読み込めていないまま読み進められると、
        利用者からは「マークが消えた」ようにしか見えず、そのうえ
        P3-3B 以降の追加・更新が実体の分からない PDF に対して行われる。
        後始末は開くのに失敗したときと同じ「PDF なし」の状態まで戻す。

        知らせ方も PDF を開けなかったときと揃える。**これは想定された失敗
        経路** なので、アプリケーション境界の「予期しないエラー」に送らず、
        ここで日本語の警告にする。包まれていない例外（実装の誤り）は
        後始末だけして送出し、未捕捉例外として扱う。

        **検索は読み込みを始める前に止める。** `DocumentController` は
        `QPdfDocument` を使い回すので、検索モデルを付けたまま
        `load()` を呼ぶと、`QPdfSearchModel` がページを走査している最中に
        対象のページが消える（走査はタイマーで少しずつ進む）。
        `_clear_document()` の後始末では遅い。
        """
        self._search_dock.clear_query()
        self._search.detach_document()
        try:
            self._controller.open(path)
        except DocumentError as error:
            self._clear_document()
            self._report_open_failure(
                notify=notify, title=_OPEN_ERROR_TITLE, path=path, body=error.message
            )
            return False

        self._settings.last_directory = str(path.parent)
        self._view.set_document(self._controller.document, self._controller.page_sizes())
        try:
            self._study_marks.activate_document(path)
        except StudyMarkLoadError:
            self._abort_open()
            self._report_open_failure(
                notify=notify, title=_MARK_LOAD_ERROR_TITLE, path=path, body=_MARK_LOAD_ERROR
            )
            return False
        except Exception:
            self._abort_open()
            raise

        # 目次を載せるのは **開く操作が最後まで成功した後**。途中で中止する
        # 経路（PDF が読めない・学習マークが読めない）はすべて
        # `_clear_document()` を通るので、古い PDF の目次が残ることはない。
        self._toc_sidebar.set_document(self._controller.document)
        # 検索も目次と同じ扱い。**開く操作が最後まで成功した後**に載せる。
        # 検索語と入力欄は読み込みの前に空にしてあるので、A の検索語のまま
        # B の件数が出ることはない。
        self._search.attach_document(self._controller.document)

        self.setWindowTitle(f"{path.name} - anp")
        self._sync_document_ui()
        return True

    def _report_open_failure(self, *, notify: bool, title: str, path: Path, body: str) -> None:
        """開けなかったことを知らせる。

        知らせ方は1つだけ。PDF の失敗と学習マークの失敗で、まとめて2枚の
        ダイアログを出すような経路は作らない（失敗した時点で戻る）。
        """
        if notify:
            QMessageBox.warning(self, title, f"{path.name}\n\n{body}")
        else:
            logger.warning("automatic restore failed for %s: %s", path, title)

    def _abort_open(self) -> None:
        """開きかけた PDF を手放し、「PDF なし」の状態へ戻す。

        `_clear_document()` との違いはドキュメント自体も閉じること。
        順序は解放時と同じ「表示 → ドキュメント」で、閉じた後の
        ドキュメントに対するレンダリング要求を残さない。
        """
        self._clear_document()
        self._controller.close()

    def _clear_document(self) -> None:
        """表示を「PDF なし」の状態へ戻す。

        開くのに失敗したときと、学習マークを読み込めなかったときの後始末は
        同じ。学習マークだけ別の後始末を作らない。

        目次と検索も一緒に手放す。「PDF なし」なのに前の PDF の目次や
        検索結果が残っている状態を作らないため。
        """
        self._view.clear_document()
        self._study_marks.clear_document()
        self._toc_sidebar.clear_document()
        self._search_dock.clear_query()
        self._search.detach_document()
        self._sync_document_ui()

    # -------------------------------------------------- 表示の同期
    def _sync_document_ui(self) -> None:
        """ドキュメントの有無に合わせて操作の可否と表示を揃える。"""
        has_document = self._view.has_document
        page_count = self._view.page_count

        self._actions.set_document_dependent_enabled(enabled=has_document)

        self._page_input.setEnabled(has_document)
        self._page_input.setMaximum(max(page_count, 1))
        self._page_count_label.setText(f"/ {page_count}")

        # 常設ウィジェットなので、一時メッセージが消えた後もここが残る。
        # 開いているパスの情報源は `DocumentController` の1つだけ。
        path = self._controller.path
        self._path_label.setText(
            str(path) if has_document and path is not None else _NO_DOCUMENT_STATUS
        )

        if not has_document:
            self.setWindowTitle("anp")

        self._sync_page_ui()
        self._sync_zoom_ui()
        self._sync_page_color_ui()

    def _sync_page_ui(self) -> None:
        """現在ページに合わせてページ入力と前/次を揃える。

        現在ページは `PdfView` が唯一の情報源。ここで計算し直さない。
        """
        page = self._view.current_page
        has_page = self._view.has_document and page >= 0

        self._actions.previous_page.setEnabled(has_page and page > 0)
        self._actions.next_page.setEnabled(has_page and page < self._view.page_count - 1)

        # 表示を戻すだけなので、ページ移動として跳ね返さないよう黙らせる。
        blocked = self._page_input.blockSignals(True)
        try:
            self._page_input.setValue(page + 1 if has_page else 1)
        finally:
            self._page_input.blockSignals(blocked)

    def _sync_zoom_ui(self) -> None:
        """倍率の表示とフィットの選択状態を、ビューの状態に合わせる。"""
        mode = self._view.zoom_mode
        self._zoom_label.setText(_ZOOM_MODE_LABELS.get(mode, f"{round(self._view.zoom * 100)}%"))
        self._actions.fit_width.setChecked(mode is ZoomMode.FIT_WIDTH)
        self._actions.fit_page.setChecked(mode is ZoomMode.FIT_PAGE)

    def _sync_page_color_ui(self) -> None:
        """ページの色の選択表示を、ビューの状態に合わせる。"""
        mode = self._view.page_color_mode
        self._actions.page_color_original.setChecked(mode is PageColorMode.ORIGINAL)
        self._actions.page_color_invert.setChecked(mode is PageColorMode.INVERT)
        self._actions.page_color_smart_dark.setChecked(mode is PageColorMode.SMART_DARK)

    def _sync_canvas_ui(self) -> None:
        """キャンバスの選択表示を、ビューの状態に合わせる。"""
        theme = self._view.canvas_theme
        self._actions.canvas_black.setChecked(theme is CanvasTheme.BLACK)
        self._actions.canvas_dark_gray.setChecked(theme is CanvasTheme.DARK_GRAY)
        self._actions.canvas_white.setChecked(theme is CanvasTheme.WHITE)

    def _sync_ui_theme_ui(self) -> None:
        """UI テーマの選択表示を、いまの状態に合わせる。"""
        self._actions.ui_theme_system.setChecked(self._ui_theme is UiTheme.SYSTEM)
        self._actions.ui_theme_light.setChecked(self._ui_theme is UiTheme.LIGHT)
        self._actions.ui_theme_dark.setChecked(self._ui_theme is UiTheme.DARK)

    def _on_current_page_changed(self, _page: int) -> None:
        """ビューの現在ページが変わったときだけ UI を更新する。

        ここから倍率を触らないこと。ページを跨いだだけでフィット倍率を
        計算し直すと、スクロールのたびに表示の大きさが変わってしまう。
        """
        self._sync_page_ui()

    def _on_page_input_changed(self, value: int) -> None:
        """ページ番号の入力（1 始まり）を内部のページ番号（0 始まり）へ。"""
        self._view.go_to_page(value - 1)

    # -------------------------------------------------- 全画面表示
    def _set_full_screen(self, enabled: bool) -> None:
        """全画面表示を切り替える。ジオメトリを自前で組み立てない。"""
        if enabled == self.isFullScreen():
            return
        if enabled:
            self._maximized_before_full_screen = self.isMaximized()
            self.showFullScreen()
        elif self._maximized_before_full_screen:
            self.showMaximized()
        else:
            self.showNormal()

    def _exit_full_screen(self) -> None:
        """全画面中なら解除する。通常表示中は何もしない。"""
        if self.isFullScreen():
            self._set_full_screen(enabled=False)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """ウィンドウ状態が変わったらアクションのチェック状態を合わせる。

        F11・Esc・ウィンドウマネージャのどれで変わっても、ここを通るので
        表示と実際の状態がずれない。
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._actions.full_screen.setChecked(self.isFullScreen())

    # -------------------------------------------------- 状態の保存/復元
    def _restore_window_state(self) -> None:
        geometry = self._settings.window_geometry
        if geometry is None:
            self.resize(*_DEFAULT_SIZE)
        else:
            self.restoreGeometry(geometry)

        state = self._settings.window_state
        if state is not None:
            self.restoreState(state)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """最初に表示されたときだけ、前回のセッションを復元する。

        構築中ではなくここで行うのは、**ビューポートの大きさが確定して
        から読書位置を決める**ため。先に決めると、その後のレイアウトで
        フィット倍率が変わり、復元した位置がずれる。

        タイマーは使わない。ジオメトリの復元は構築時に済んでおり、
        この時点でビューポートの大きさは決まっている。
        """
        super().showEvent(event)
        if self._session_restored:
            return
        self._session_restored = True
        self._restore_last_document()

    def _restore_last_document(self) -> None:
        """前回終了時に読んでいた PDF を、その位置ごと開き直す。

        **どんな失敗でも起動は続ける。** ファイルが無い・壊れている・
        学習マークを読み込めない、のいずれでも「PDF なし」の状態で
        立ち上がる（学習マークの fail-closed は Phase 3 のまま。ただし
        起動時なのでアプリを終了させはしない）。

        失敗したら復元対象を忘れる。残しておくと、起動のたびに同じ失敗を
        繰り返すことになる。ファイルが無くなっていた場合は履歴からも外す。

        **履歴の並びは変えない。** 履歴は「利用者が明示的に開いたもの」
        なので、自動で開いただけの PDF が先頭へ上がったり、意図せず
        履歴に載ったりはしない。

        順序は「倍率（構築時に復元済み）→ ドキュメント → 読書位置」。
        逆にすると、フィットの計算が読書位置を動かしてしまう。
        """
        stored = self._settings.last_document
        if not stored:
            return

        path = Path(stored)
        if not self._open_document(path, notify=False):
            self._settings.clear_last_session()
            if not path.exists():
                self._set_recent(remove_recent(self._recent, path))
            return

        self._restore_reading_position()

    def _restore_reading_position(self) -> None:
        """保存された読書位置まで戻る。戻れなければ先頭のまま。

        差し替えでページ数が減っていた場合は、最終ページへ丸めずに文書の
        先頭で開く。セッションの位置は過去の読書状態のヒントでしかないので、
        存在しない位置を無理に解釈しない（学習マークの移動とは扱いが違う）。
        """
        position = ReadingPosition(
            page_index=self._settings.last_page_index,
            y_norm=self._settings.last_y_norm,
        )
        if not self._view.restore_reading_position(position):
            logger.info("saved reading position is outside the document: %s", position)

    def _save_last_session(self) -> None:
        """いま読んでいる PDF と位置を、次回の起動のために覚える。

        PDF を開いていない状態で終了したときは忘れる。閉じたはずの PDF が
        次回また勝手に開くのを避けるため。倍率は既存の設定
        （`view/zoom_mode` と `view/free_zoom`）をそのまま使うので、
        セッション用の倍率キーは作らない。
        """
        path = self._controller.path
        position = self._view.current_reading_position()
        if path is None or position is None:
            self._settings.clear_last_session()
            return
        self._settings.set_last_session(str(path), position.page_index, position.y_norm)

    def _restore_zoom(self) -> None:
        """保存された倍率モードと手動倍率を復元する。

        知らない名前が入っていたら FREE に落とす。設定は消えても再設定
        すれば済むので、読めない値で起動を止めない。
        """
        self._view.set_zoom(self._settings.free_zoom)

        name = self._settings.zoom_mode
        try:
            mode = ZoomMode(name)
        except ValueError:
            logger.warning("unknown zoom mode in settings: %r", name)
            return

        if mode is ZoomMode.FIT_WIDTH:
            self._view.fit_width()
        elif mode is ZoomMode.FIT_PAGE:
            self._view.fit_page()

    def _restore_page_color_mode(self) -> None:
        """保存されたページの色を復元する。知らない名前なら Original。"""
        name = self._settings.page_color_mode
        try:
            mode = PageColorMode(name)
        except ValueError:
            logger.warning("unknown page color mode in settings: %r", name)
            return
        self._view.set_page_color_mode(mode)

    def _restore_canvas_theme(self) -> None:
        """保存されたキャンバスの色を復元する。知らない名前ならダークグレー。

        レンダリング要求は起きない。塗る色が変わるだけで、必要なページも
        解像度も変わらないため。
        """
        name = self._settings.canvas_theme
        try:
            theme = CanvasTheme(name)
        except ValueError:
            logger.warning("unknown canvas theme in settings: %r", name)
            theme = CanvasTheme.DARK_GRAY
        self._view.set_canvas_theme(theme)
        # 既定値のままだとビューの状態が変わらず信号が出ないので、
        # ここで選択表示を揃える。
        self._sync_canvas_ui()

    def _restore_ui_theme(self) -> None:
        """保存された UI テーマを復元する。知らない名前ならシステム。"""
        name = self._settings.ui_theme
        try:
            theme = UiTheme(name)
        except ValueError:
            logger.warning("unknown ui theme in settings: %r", name)
            theme = UiTheme.SYSTEM
        self.set_ui_theme(theme)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt の命名規則)
        """終了時にウィンドウの位置と状態、倍率、セッションを保存し、PDF を解放する。

        解放の順序は「表示 → ドキュメント」。`clear_document()` が世代を
        進めてキャッシュと要求を捨ててから、ドキュメントを閉じる。逆に
        すると、閉じた後のドキュメントに対する要求が残る。

        ここで閉じるのは、閉じたウィンドウがレンダリング結果とキャッシュを
        抱えたままにならないようにするため。なお `QPdfDocument.close()` は
        ファイルハンドルまでは手放さない（それは破棄時）。
        """
        # 表示を捨てる前に読書位置を取る。`clear_document()` の後では
        # 現在ページもスクロール位置も失われている。
        self._save_last_session()

        self._settings.window_geometry = self.saveGeometry()
        self._settings.window_state = self.saveState()
        self._settings.zoom_mode = self._view.zoom_mode.value
        # 終了時のモードに関わらず、最後に手で指定した倍率を保存する。
        # 現在の倍率を使うと、フィット中に終了したときにビューポート依存の
        # 値が焼き付いてしまい、手で選んだ倍率も失われる。
        self._settings.free_zoom = self._view.last_free_zoom
        self._settings.page_color_mode = self._view.page_color_mode.value
        self._settings.canvas_theme = self._view.canvas_theme.value
        self._settings.ui_theme = self._ui_theme.value
        self._settings.sync()

        self._view.clear_document()
        self._study_marks.clear_document()
        # 目次と検索のモデルは `QPdfDocument` を指しているので、閉じる前に外す。
        self._toc_sidebar.clear_document()
        self._search.detach_document()
        self._controller.close()
        logger.info("main window closed")
        super().closeEvent(event)

    # -------------------------------------------------- 検査用
    @property
    def reader_actions(self) -> ReaderActions:
        """アクション一式。`QWidget.actions()` とは別物なので名前を分ける。"""
        return self._actions

    @property
    def view(self) -> PdfView:
        """中央の PDF ビュー。"""
        return self._view

    @property
    def study_marks(self) -> StudyMarkController:
        """学習マークのコントローラ。"""
        return self._study_marks

    @property
    def study_mark_interaction(self) -> StudyMarkInteraction:
        """学習マークのマウス操作とメニュー。"""
        return self._study_mark_interaction

    @property
    def study_mark_sidebar(self) -> StudyMarkSidebar:
        """学習マークの一覧ドック。"""
        return self._marks_sidebar

    @property
    def toc_sidebar(self) -> TocSidebar:
        """PDF の目次のドック。"""
        return self._toc_sidebar

    @property
    def search(self) -> PdfSearchController:
        """テキスト検索のコントローラ。"""
        return self._search

    @property
    def search_dock(self) -> SearchDock:
        """テキスト検索のドック。"""
        return self._search_dock

    @property
    def recent_files(self) -> tuple[Path, ...]:
        """最近使ったファイルの一覧（新しい順）。"""
        return self._recent

    @property
    def recent_menu(self) -> QMenu:
        """最近使ったファイルのサブメニュー。"""
        return self._recent_menu

    @property
    def document_status_text(self) -> str:
        """ステータスバーに常設しているパスの表示。"""
        return self._path_label.text()
