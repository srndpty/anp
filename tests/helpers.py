"""テストから色変換ワーカーを決定的に扱うための道具。

`PageRenderService` は色変換をワーカースレッドで行う。完了を待つ書き方に
すると、どのテストもタイミング依存になる。ここでは **投入を捕捉して、
テストが好きな順で完了させる**。sleep も待ち合わせも要らない。

捕捉するのは `PageRenderService._submit_transform()` の1箇所だけ。台帳・
世代の検証・キャッシュへの格納・通知は本番と同じ `_on_transform_finished()`
を通るので、非同期の境界だけを差し替えたことになる。
"""

from __future__ import annotations

from PySide6.QtGui import QImage

from anp.pdf import render as render_module
from anp.pdf.render import PageRenderService, _TransformJob, _TransformResult


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
