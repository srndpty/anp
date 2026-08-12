"""PDF のテキスト検索の状態。

検索そのものは行わない。**検索エンジンは `QPdfSearchModel`（pdfium）**で、
ここが持つのは「いまの検索文字列・結果数・何件目を見ているか」だけ。

```
QPdfDocument
        ↓
QPdfSearchModel（ここが所有する）
        ↓
PdfSearchController
   ↙              ↘
SearchDock       PdfView
（入力と件数表示）  （ハイライト）
```

見る側が2つ（検索 UI とビュー）に増えたので、現在位置を1箇所に置く。
それ以上の役目は持たない。

**ページのテキストを Python 側へ写し取らない。** 結果の実体は
`QPdfSearchModel` の行で、ここは番号だけを持つ。`resultAtIndex()` は
呼ばれたときに引く。

**検索モデルはドキュメント1つにつき1つ。** `DocumentController` は
`QPdfDocument` を使い回すので、同じ `QPdfSearchModel` を開き直しを跨いで
生かしておくと Windows でプロセスごと落ちる。開くたびに作り直し、古い
モデルは `deleteLater()` で消す（`model_changed` で見る側に知らせる）。

**独自の検索意味論を作らない。** 大文字小文字の扱い・Unicode 正規化・
合字・ハイフネーション・語境界は `QPdfSearchModel` が返したとおり。
検索文字列も `strip()` しない（前後の空白も検索語の一部）。空文字だけを
「検索なし」として扱う。

対象は PDF のテキストレイヤだけ。スキャン画像だけの PDF は 0 件になる
（OCR はしない。AGENTS.md の非目標）。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QModelIndex, QObject, Signal
from PySide6.QtPdf import QPdfDocument, QPdfLink, QPdfSearchModel

# 現在の検索結果が無いことを表す番号。結果があるときは 0〜count-1。
NO_RESULT = -1


@dataclass(frozen=True, slots=True)
class SearchState:
    """検索 UI に見せる状態のスナップショット。

    番号は 0 始まりのまま持つ。1 始まりへの読み替えは表示の都合なので、
    `SearchDock` の側で行う。
    """

    query: str
    count: int
    current_index: int
    has_document: bool

    @property
    def has_results(self) -> bool:
        """結果が1件以上あるか。前/次を動かせるかと同じ条件。"""
        return self.count > 0


class PdfSearchController(QObject):
    """検索文字列と現在の結果番号を持ち、`QPdfSearchModel` に追随する。"""

    state_changed = Signal(object)
    """検索文字列・結果数・現在の結果番号のどれかが変わった（`SearchState`）。

    表示の更新は受け取った側で行う。**移動はここでは起こさない**。
    """

    result_activated = Signal(object)
    """現在の結果が別の結果になった（その `QPdfLink`）。

    移動そのものはここでは行わない。ページ配置の算術は `PdfView` にあり、
    このコントローラはビューを持たない。
    """

    model_changed = Signal(object)
    """検索モデルを作り直した（新しい `QPdfSearchModel`）。

    ハイライトを描く側は、これを受けて参照を張り替える。**古いモデルを
    指したままにしない**（`deleteLater()` で消える）。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # モデルはこのコントローラが所有する（Qt の親子関係で寿命を持つ）。
        self._model = self._new_model()
        self._query = ""
        self._current = NO_RESULT

        # 最後に知らせた状態。同じ状態を繰り返し流さないためだけに持つ。
        self._published = self.state

    def _new_model(self) -> QPdfSearchModel:
        """検索モデルを1つ作り、件数の通知だけつなぐ。

        検索は非同期に進む（`setSearchString()` の直後に全件が揃っている
        とは限らない）。結果数が後から増減しても現在位置がずれないよう、
        モデルの通知から取り直す。固定時間の待ちは入れない。
        """
        model = QPdfSearchModel(self)
        model.countChanged.connect(self._refresh)
        return model

    # -------------------------------------------------- ドキュメント
    def attach_document(self, document: QPdfDocument) -> None:
        """検索対象のドキュメントを差し替える。

        呼ぶのは **開く操作が最後まで成功した後**（目次と同じ扱い）。

        検索文字列と現在位置は必ず捨てる。A の検索語のまま B を開くと、
        B に対する検索が始まっていないのに件数だけ残る、という状態が
        作れてしまう。文字列を先に空にしてからドキュメントを差し替える
        ので、A の結果が B の結果として一瞬でも見えることはない。

        **モデルは毎回作り直す。** 理由は `_replace_model()` を参照。
        """
        self._replace_model()
        self._model.setDocument(document)
        self._refresh()

    def _replace_model(self) -> None:
        """検索モデルを新品に取り替え、古いモデルは Qt に消させる。

        `DocumentController` は `QPdfDocument` を使い回すので、同じ
        `QPdfSearchModel` を「外す → 開き直したドキュメントへ付け直す」と、
        Windows で access violation としてプロセスごと落ちる。**検索語を
        一度も入れていなくても落ちる**ので、走査中のキャンセルの問題では
        なく、モデルがドキュメントの入れ替わりを跨いで生き続けること自体が
        危ない。

        `setDocument(None)` してから `deleteLater()` する。**即座に削除
        しない**のは、モデルの中で進んでいる遅延処理がこのイベントの中から
        呼ばれている可能性があるため。
        """
        self._reset_query()
        old = self._model
        # C++ の `setDocument(QPdfDocument *)` は nullptr を受けるが、
        # PySide6 の型定義は None を含んでいない（目次と同じ）。
        old.setDocument(None)  # type: ignore[arg-type]
        old.countChanged.disconnect(self._refresh)
        old.deleteLater()

        self._model = self._new_model()
        self.model_changed.emit(self._model)

    def detach_document(self) -> None:
        """ドキュメントを手放し、検索を空の状態へ戻す。

        閉じられる `QPdfDocument` をモデルが指し続けないよう、必ず外す。
        PDF を開くのに失敗したときも、学習マークを読み込めなかったときも、
        終了時も、後始末はここ1つ。
        """
        self._reset_query()
        # C++ の `setDocument(QPdfDocument *)` は nullptr を受けるが、
        # PySide6 の型定義は None を含んでいない（目次と同じ）。
        self._model.setDocument(None)  # type: ignore[arg-type]
        self._refresh()

    def _reset_query(self) -> None:
        """検索文字列と現在位置を捨てる。通知はしない（呼び出し側でまとめる）。"""
        self._query = ""
        self._current = NO_RESULT
        self._model.setSearchString("")

    # -------------------------------------------------- 検索文字列
    @property
    def query(self) -> str:
        """いまの検索文字列。空文字なら検索していない。"""
        return self._query

    def set_query(self, query: str) -> None:
        """検索文字列を差し替える。

        **前の検索語の現在位置は必ず捨てる。** 「3件目を見ていた」状態を
        別の検索語へ持ち越さない。結果が1件以上になった時点で先頭が現在の
        結果になり、`result_activated` でそこへ移動する（Ctrl+F の典型的な
        挙動）。

        `strip()` はしない。空白も検索語の一部として `QPdfSearchModel` へ
        そのまま渡す。
        """
        if query == self._query:
            return
        self._query = query
        self._current = NO_RESULT
        # モデルは検索文字列を変えた時点で古い結果を捨てるので、ここから
        # 先に届く通知はすべて新しい検索語のもの。世代番号や専用スレッドは
        # 要らない。
        self._model.setSearchString(query)
        # 件数が 0 のまま変わらなかった場合は通知が出ないので、ここでも
        # 取り直す（`_refresh()` は状態が同じなら何も出さない）。
        self._refresh()

    # -------------------------------------------------- 結果
    @property
    def count(self) -> int:
        """いまの検索結果の件数。情報源は `QPdfSearchModel` の行数だけ。"""
        return self._model.rowCount(QModelIndex())

    @property
    def current_index(self) -> int:
        """現在の結果の番号（0 始まり）。無ければ `NO_RESULT`。"""
        return self._current

    @property
    def has_document(self) -> bool:
        """検索対象のドキュメントがあるか。"""
        return self._model.document() is not None

    @property
    def state(self) -> SearchState:
        """いまの状態のスナップショット。"""
        return SearchState(
            query=self._query,
            count=self.count,
            current_index=self._current,
            has_document=self.has_document,
        )

    @property
    def model(self) -> QPdfSearchModel:
        """いまの検索モデル。ハイライトを描く `PdfView` へ渡す。

        **結果を Python のリストへ写し取らない**ので、ビューもこのモデルを
        直接引く。

        **ドキュメントを開くたびに別のインスタンスになる**（`model_changed`）。
        参照を持ち続ける側は、その通知で張り替えること。
        """
        return self._model

    def current_result(self) -> QPdfLink | None:
        """現在の結果。無ければ None。

        モデルは範囲外の番号に対して無効な `QPdfLink` を返すので、件数が
        減った直後に呼ばれても落ちない。
        """
        if self._current == NO_RESULT:
            return None
        link = self._model.resultAtIndex(self._current)
        return link if link.isValid() else None

    # -------------------------------------------------- 移動
    def next_result(self) -> None:
        """次の結果へ。末尾なら先頭へ回る。"""
        self._step(1)

    def previous_result(self) -> None:
        """前の結果へ。先頭なら末尾へ回る。"""
        self._step(-1)

    def _step(self, delta: int) -> None:
        count = self.count
        if count == 0:
            return
        if self._current == NO_RESULT:
            # 結果があるのに現在位置が無い状態は通常起きないが、進む向きに
            # 応じた端から始める（次なら先頭、前なら末尾）。
            self._current = 0 if delta > 0 else count - 1
        else:
            self._current = (self._current + delta) % count
        self._publish(activate=True)

    # -------------------------------------------------- モデルへの追随
    def _refresh(self) -> None:
        """モデルの件数に現在位置を合わせ、変わっていれば知らせる。

        検索中に件数が増減しても、範囲外の番号を指したままにしない。

        - 0 件になった: 現在位置なし
        - 結果が現れた（現在位置なし）: 先頭を現在の結果にする
        - 件数が減って範囲外になった: 末尾へ寄せる
        """
        count = self.count
        previous = self._current
        if count == 0:
            self._current = NO_RESULT
        elif self._current == NO_RESULT:
            self._current = 0
        elif self._current >= count:
            self._current = count - 1
        self._publish(activate=self._current != NO_RESULT and self._current != previous)

    def _publish(self, *, activate: bool) -> None:
        """状態を知らせる。移動が要るときだけ現在の結果も知らせる。

        順序は「状態 → 移動」。ハイライトの対象を先に合わせてから移動する
        ので、移動先が描かれていない一瞬が生まれない。

        状態が前回と同じなら流さない。検索文字列を変えた直後はモデルの
        通知とこちらの取り直しが続けて走るので、同じ値が二重に届く。
        """
        state = self.state
        if state != self._published:
            self._published = state
            self.state_changed.emit(state)
        if not activate:
            return
        link = self.current_result()
        if link is not None:
            self.result_activated.emit(link)
