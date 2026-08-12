"""Qt から呼ばれる境界で、想定外の失敗を fail-stop させる。

**Qt の signal/slot 機構が呼び出した slot から例外を外へ出すのは undefined
behavior**（Qt のドキュメントが明記している）。一方で anp のエラー処理方針は
「想定していない失敗＝実装の誤りは、日常的な失敗の見た目に化けさせない」
（AGENTS.md）。この2つを両立させる場所がここになる。

```
QAction / Signal / Qt の仮想関数
  → ここ（`guard_qt_callback`）
      想定内の失敗はもっと内側で処理済み
      想定外の失敗は記録して知らせ、イベントループを終わらせる
  → 内側のメソッド（想定外の失敗はそのまま送出してよい）
```

**続行させない**のが要点。学習マークの更新に失敗しただけなら読み続けられる
が、実装の誤りが起きた後の状態は誰にも分からない。Qt も、例外の後は
イベントループを終えて後始末し、アプリケーションを終了する方向を案内して
いる。ダイアログを閉じたら普通に使えてしまう、という状態にはしない。

`app.py` の `sys.excepthook` は最後の砦として残す（Qt を経由しない経路）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QMessageBox

logger = logging.getLogger(__name__)

_TITLE = "予期しないエラー"
_TEXT = (
    "予期しないエラーが発生したため、anp を終了します。\n\n{detail}\n\n"
    "学習マークの記録は削除されていません。ログを確認してください。"
)

# 想定外の失敗で終了したときの終了コード。二重起動（1）やロックの失敗（2）
# とは別にして、ログを見なくても種類が分かるようにする。
EXIT_INTERNAL_ERROR = 3


def report_fatal(error: BaseException) -> None:
    """想定外の失敗を記録し、知らせて、イベントループを終わらせる。

    ダイアログの親は指定しない。壊れているのがウィジェットの側かもしれず、
    その上にモーダルを乗せると出せないことがある。

    `QCoreApplication.exit()` は「いま走っているイベントループを終える」
    要求で、その場で止まるわけではない。呼んだ callback は普通に戻り、
    後始末（`closeEvent` → DB の接続を閉じる）は `main()` の経路で行われる。
    """
    logger.critical("unexpected failure at a Qt callback boundary", exc_info=error)
    QMessageBox.critical(None, _TITLE, _TEXT.format(detail=f"{type(error).__name__}: {error}"))
    QCoreApplication.exit(EXIT_INTERNAL_ERROR)


def guard_qt_callback[**P](method: Callable[P, None]) -> Callable[P, None]:
    """Qt から呼ばれるメソッドを、fail-stop の境界にする。

    **付けるのは Qt が直接呼ぶ最も外側だけ。** 内側のメソッドに広く付けると、
    どこで止まったのかが分からなくなるうえ、想定内の失敗の扱い（もっと内側で
    ダイアログにする）と混ざる。
    """

    @wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            method(*args, **kwargs)
        except Exception as error:
            # Qt 境界。ここで外へ出すと undefined behavior なので、必ず
            # 受け止めて記録し、イベントループを終わらせる。
            report_fatal(error)

    return wrapper
