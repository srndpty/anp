"""ビューのテストで共有する道具。

前半は色変換ワーカーを決定的に扱うための仕掛け、後半は `PdfView` を
使うテスト（`test_pdf_view.py` / `test_study_mark_overlay.py`）が共有する
サービス・描画・スパイ。フィクスチャ本体は `conftest.py` にある。

--- 色変換ワーカー ---

`PageRenderService` は色変換をワーカースレッドで行う。完了を待つ書き方に
すると、どのテストもタイミング依存になる。ここでは **投入を捕捉して、
テストが好きな順で完了させる**。sleep も待ち合わせも要らない。

捕捉するのは `PageRenderService._submit_transform()` の1箇所だけ。台帳・
世代の検証・キャッシュへの格納・通知は本番と同じ `_on_transform_finished()`
を通るので、非同期の境界だけを差し替えたことになる。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage

from anp.pdf import render as render_module
from anp.pdf.cache import RenderCache, RenderKey
from anp.pdf.color import PageColorMode
from anp.pdf.render import PageRenderService, PageRequest, _TransformJob, _TransformResult
from anp.ui.pdf_view import PdfView


class ManualTransforms:
    """変換ワーカーの代わりに、テストが手で完了させる。

    `immediate` が真なら投入と同時に完了させる。色の見た目だけを見る
    テストは、非同期であることを気にせず書ける。
    """

    def __init__(self, service: PageRenderService, *, immediate: bool = False) -> None:
        self.service = service
        self.immediate = immediate
        self.pending: list[_TransformJob] = []
        self.submitted: list[_TransformJob] = []
        service._submit_transform = self._submit  # type: ignore[method-assign]  # noqa: SLF001

    @property
    def submitted_keys(self) -> list[str]:
        """投入された変換の鍵（読みやすい形）。重複投入の検査に使う。"""
        return [
            f"p{job.display_key.render_key.page_index}"
            f"w{job.display_key.render_key.width_px}"
            f"-{job.display_key.color_mode.value}"
            for job in self.submitted
        ]

    def _submit(self, job: _TransformJob) -> None:
        self.submitted.append(job)
        if self.immediate:
            self._finish(job, render_module.transform_page(job.source, job.display_key.color_mode))
        else:
            self.pending.append(job)

    def complete(self, job: _TransformJob) -> None:
        """1件を成功として完了させる。"""
        self._drop(job)
        self._finish(job, render_module.transform_page(job.source, job.display_key.color_mode))

    def fail(self, job: _TransformJob) -> None:
        """1件を失敗として完了させる（ワーカー内で例外が出た状況）。"""
        self._drop(job)
        self._finish(job, None)

    def complete_all(self) -> None:
        """いま溜まっている分を投入順に完了させる。

        完了で枠が空いて新しい分が投入されるので、空になるまで繰り返す。
        """
        while self.pending:
            self.complete(self.pending[0])

    def _drop(self, job: _TransformJob) -> None:
        # `QImage` を持つので等値比較は使わない。同一性で外す。
        self.pending = [pending for pending in self.pending if pending is not job]

    def _finish(self, job: _TransformJob, image: QImage | None) -> None:
        self.service._on_transform_finished(_TransformResult(job=job, image=image))  # noqa: SLF001


# ---------------------------------------------------------------- ビューのテスト
class RecordingService(PageRenderService):
    """要求されたページを記録するレンダリングサービス。

    実際の `requestPage()` は出さない。本物のレンダリングが途中で完了すると
    キャッシュの中身がテストの仕込みと入れ替わり、結果がタイミング依存に
    なるため。要求の中身とキャッシュの読み出しはそのまま検証できる。

    色変換も本物のワーカーには流さない。既定では投入と同時に完了させる
    （`transforms.immediate`）。ビューの関心は「引き当てた画像をどう描くか」
    なので、変換の完了タイミングを見たいテストだけが手動へ切り替える。
    """

    def __init__(self, cache: RenderCache) -> None:
        super().__init__(cache)
        self.requests: list[list[PageRequest]] = []
        self.transforms = ManualTransforms(self, immediate=True)

    def request_pages(self, requests: Sequence[PageRequest]) -> None:
        self.requests.append(list(requests))
        super().request_pages(requests)

    def flush(self) -> None:
        pass

    @property
    def last_pages(self) -> list[int]:
        """直近の要求のページ番号（優先度順）。"""
        return [request.page_index for request in self.requests[-1]]

    def repeat_last_request(self) -> None:
        """直前と同じページ集合を宣言し直す。

        表示用画像はレンダリング完了時・モード変更時・必要なページが
        変わったときに用意される。キャッシュへ直接仕込んだ画像を、
        3番目の経路で反映させるために使う。記録は増やさない。
        """
        PageRenderService.request_pages(self, self.requests[-1])


def put_image(
    cache: RenderCache, view: PdfView, page: int, color: Qt.GlobalColor, *, scale: float = 1.0
) -> None:
    """指定ページの画像をキャッシュに仕込む。

    `scale` を 1.0 以外にすると、表示に必要な解像度とは違う画像になり、
    仮表示（placeholder）としてだけ使える。
    """
    rect = view.page_viewport_rect(page)
    assert rect is not None
    dpr = view.devicePixelRatioF()
    width = max(round(rect.width() * dpr * scale), 1)
    height = max(round(rect.height() * dpr * scale), 1)
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    cache.put(RenderKey(page, width, height, dpr), image)


def render_view(view: PdfView) -> QImage:
    """ビューポートを描画した結果を取り出す。"""
    image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.black)
    view.viewport().render(image)
    return image


class UpdateSpy:
    """`viewport().update()` の呼び出しを記録する。"""

    def __init__(self, view: PdfView) -> None:
        self.rects: list[QRect | None] = []
        self._original = view.viewport().update
        view.viewport().update = self  # type: ignore[assignment]

    def __call__(self, rect: QRect | None = None) -> None:
        self.rects.append(rect)
        if rect is None:
            self._original()
        else:
            self._original(rect)


def count_transforms(monkeypatch: pytest.MonkeyPatch) -> list[PageColorMode]:
    """色変換が呼ばれるたびに記録するリストを返す。"""
    calls: list[PageColorMode] = []
    original = render_module.transform_page

    def counting(image: QImage, mode: PageColorMode) -> QImage:
        calls.append(mode)
        return original(image, mode)

    monkeypatch.setattr(render_module, "transform_page", counting)
    return calls
