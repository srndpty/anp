"""ショートカットの設定と `QAction` の橋渡し。

```
Settings（shortcuts/<command-id>）
        ↓ 読み込み・検証
有効な割り当て
        ↓
QAction.setShortcuts()
```

**ここが持たないもの**: ウィジェットの組み立て・ダイアログ・メニューの
配線・生の `QSettings`・既定値の表（`anp.ui.shortcuts` にある）。

## 設定のキー

```
shortcuts/<command-id>
```

例: `shortcuts/search.find`。値は PortableText。

## キーが無いことと、空であることは違う

| 設定の状態 | 意味 |
|---|---|
| キーが無い | 既定値を使う |
| キーがあり、値が空 | 割り当て無し（利用者が明示的に解除した） |

この区別を失うと、「解除したのに再起動で戻る」か「既定を消したつもりが
無い」のどちらかになる。保存も対称にする。既定と同じならキーを消し、
違えば書く。将来 anp 側で既定を変えたとき、既定のままの利用者と自分で
選んだ利用者を区別できる。

## 壊れた設定

INI を手で書き換えられている可能性があるので、次の2つを分けて扱う。

- **読めない値**: そのコマンドだけ既定へ戻す。他のコマンドは設定を活かす
- **曖昧な組み合わせ**: その起動では**カスタム設定を丸ごと使わない**。
  一部だけ既定へ戻すと、戻した既定が別のカスタムとぶつかる連鎖が起きる

どちらも警告を残すだけで、設定ファイルを勝手に書き換えない。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from PySide6.QtGui import QAction, QKeySequence

from anp.core.settings import Settings
from anp.ui.shortcuts import (
    SHORTCUT_SPECS,
    apply_assignments,
    default_assignments,
    default_sequences,
    describe_conflicts,
    find_conflicts,
    format_assignment,
    parse_assignment,
)

logger = logging.getLogger(__name__)

Assignments = dict[str, tuple[QKeySequence, ...]]


class ShortcutManager:
    """設定を読んでショートカットを決め、`QAction` へ載せる。"""

    def __init__(self, settings: Settings, actions: Mapping[str, QAction]) -> None:
        """`actions` はコマンド ID から `QAction` への明示的な対応。

        表示名から `QAction` を探したり、`findChildren()` で総当たりしたり
        しない。レジストリにあるコマンドが揃っていなければ実装の誤りなので、
        設定の問題（黙って読み飛ばす）とは分けて即座に落とす。
        """
        missing = [spec.command_id for spec in SHORTCUT_SPECS if spec.command_id not in actions]
        if missing:
            message = f"no QAction for shortcut commands: {', '.join(missing)}"
            raise ValueError(message)

        self._settings = settings
        self._actions = dict(actions)

    # -------------------------------------------------- 読み出し
    def stored_assignments(self) -> Assignments:
        """設定から決まる有効な割り当て。`QAction` には触らない。"""
        resolved: Assignments = {}
        for spec in SHORTCUT_SPECS:
            stored = self._settings.shortcut_override(spec.command_id)
            if stored is None:
                resolved[spec.command_id] = default_sequences(spec)
                continue

            parsed = parse_assignment(stored)
            if parsed is None:
                logger.warning(
                    "ignoring unreadable shortcut setting for %s: %r", spec.command_id, stored
                )
                resolved[spec.command_id] = default_sequences(spec)
                continue

            resolved[spec.command_id] = parsed

        conflicts = find_conflicts(resolved)
        if conflicts:
            # 曖昧なショートカットを `QAction` へ渡さない。設定ファイルは
            # 残すので、利用者が直せば次の起動から効く。
            logger.warning(
                "ignoring all custom shortcut settings, they are ambiguous:\n%s",
                describe_conflicts(conflicts),
            )
            return default_assignments()
        return resolved

    def current_assignments(self) -> Assignments:
        """いま `QAction` に載っている割り当て。

        ダイアログの初期値はここから取る。設定の生の値ではなく、実際に
        効いている割り当てを見せるため。
        """
        return {
            spec.command_id: tuple(self._actions[spec.command_id].shortcuts())
            for spec in SHORTCUT_SPECS
        }

    # -------------------------------------------------- 反映
    def apply_stored(self) -> None:
        """設定を読んで `QAction` へ反映する。

        `QAction` が使えるようになった直後に1回だけ呼ぶ。メニューを開く
        まで反映されない、といった遅延を作らない。
        """
        apply_assignments(self._actions, self.stored_assignments())

    def apply(self, assignments: Mapping[str, Sequence[QKeySequence]]) -> None:
        """検証済みの割り当てを保存し、`QAction` へ反映する。

        保存と反映は必ずこの順で丸ごと行う。「衝突している割り当ての一部
        だけが効いている」状態を作れないよう、曖昧な組み合わせはここで
        拒む（呼び出し側が先に検証して利用者へ知らせる契約なので、ここへ
        届いた時点では実装の誤り）。
        """
        conflicts = find_conflicts(assignments)
        if conflicts:
            message = f"refusing to apply ambiguous shortcuts:\n{describe_conflicts(conflicts)}"
            raise ValueError(message)

        self._store(assignments)
        apply_assignments(self._actions, assignments)

    def _store(self, assignments: Mapping[str, Sequence[QKeySequence]]) -> None:
        """既定と違うものだけを設定へ書く。"""
        for spec in SHORTCUT_SPECS:
            sequences = assignments.get(spec.command_id)
            if sequences is None:
                continue
            text = format_assignment(sequences)
            if text == format_assignment(default_sequences(spec)):
                self._settings.remove_shortcut_override(spec.command_id)
            else:
                self._settings.set_shortcut_override(spec.command_id, text)
        # 次の起動で読めるように書き出す。失敗しても警告だけで続くのは
        # 他の設定と同じ（`Settings.sync()` の契約）。
        self._settings.sync()
