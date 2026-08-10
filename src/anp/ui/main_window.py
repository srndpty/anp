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
「表示が空になった」を伝えることまで。

外観のうち **UI テーマだけ**をここが持つ。アプリ全体のウィジェットの
配色はビューの責務ではないため。キャンバスの色は `PdfView`、ページの
色変換は `PageRenderService` が持ち、3つは互いに連動しない。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QWidget,
)

from anp import __version__
from anp.core.settings import Settings
from anp.pdf.cache import RenderCache
from anp.pdf.color import PageColorMode
from anp.pdf.document import DocumentController, DocumentError
from anp.pdf.render import PageRenderService
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.actions import ReaderActions, create_actions, populate_menus
from anp.ui.appearance import CanvasTheme, UiTheme, apply_ui_theme
from anp.ui.pdf_view import PdfView, ZoomMode
from anp.ui.study_mark_controller import StudyMarkController

logger = logging.getLogger(__name__)

_DEFAULT_SIZE = (1000, 800)

_PDF_FILTER = "PDF ファイル (*.pdf)"

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

        self.setWindowTitle("anp")
        populate_menus(self.menuBar(), self._actions)
        self._create_toolbar()
        self._connect_actions()

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

    def _connect_actions(self) -> None:
        self._actions.open.triggered.connect(self._prompt_open)
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
        """PDF を開く。失敗したら表示を空にしてから知らせる。

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
        後始末は開くのに失敗したときと同じ「PDF なし」の状態まで戻し、
        原因は握り潰さずに送出する。
        """
        try:
            self._controller.open(path)
        except DocumentError as error:
            self._clear_document()
            QMessageBox.warning(self, "PDF を開けません", f"{path.name}\n\n{error.message}")
            return

        self._settings.last_directory = str(path.parent)
        self._view.set_document(self._controller.document, self._controller.page_sizes())
        try:
            self._study_marks.activate_document(path)
        except Exception:
            self._clear_document()
            self._controller.close()
            raise

        self.setWindowTitle(f"{path.name} - anp")
        self._sync_document_ui()
        self.statusBar().showMessage(str(path))

    def _clear_document(self) -> None:
        """表示を「PDF なし」の状態へ戻す。

        開くのに失敗したときと、学習マークを読み込めなかったときの後始末は
        同じ。学習マークだけ別の後始末を作らない。
        """
        self._view.clear_document()
        self._study_marks.clear_document()
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

        if not has_document:
            self.setWindowTitle("anp")
            self.statusBar().showMessage("PDF が開かれていません")

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
        """終了時にウィンドウの位置と状態、倍率を保存し、PDF を解放する。

        解放の順序は「表示 → ドキュメント」。`clear_document()` が世代を
        進めてキャッシュと要求を捨ててから、ドキュメントを閉じる。逆に
        すると、閉じた後のドキュメントに対する要求が残る。

        ここで閉じるのは、閉じたウィンドウがレンダリング結果とキャッシュを
        抱えたままにならないようにするため。なお `QPdfDocument.close()` は
        ファイルハンドルまでは手放さない（それは破棄時）。
        """
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
