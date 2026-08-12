"""キーボードショートカットの定義・直列化・衝突の判定。

設定から実際のキー操作までの流れはこの1本だけ。

```
ShortcutSpec レジストリ（既定値の唯一の情報源）
        ↓
有効な割り当て（dict[command_id, tuple[QKeySequence, ...]]）
        ↓
QAction.setShortcuts()
        ↓
メニュー表示と実際のキー操作
```

**コマンド機構ではない。** ここにあるのは既定値の表と、
`QKeySequence` を設定へ書ける形にする関数と、曖昧なショートカットを
見つける関数だけ。設定の読み書きと `QAction` への反映は
`ShortcutManager`、編集の UI は `ShortcutDialog` が持つ。

## コマンド ID

設定のキーになるので、**表示文言とは分けて固定する**。表示名・
メニューの並び・配列の添字を設定のキーにしない（文言を直した瞬間に
利用者の設定が失われる）。

## 代替ショートカット

拡大は `Ctrl++` と `Ctrl+=` の2つを既定で持つ（`Ctrl++` は Shift が要る
キーボードが多い）。1コマンド1ショートカットに狭めると、この既定の
挙動が変わってしまうので、割り当ては常に列として扱う。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence

logger = logging.getLogger(__name__)

# 設定へ書く形式。ロケールやプラットフォームで変わる表示文字列
# （NativeText）を設定の契約にしない。
PORTABLE = QKeySequence.SequenceFormat.PortableText

# 表示に使う形式。利用者に見せるのはこちら。
NATIVE = QKeySequence.SequenceFormat.NativeText

# 1コマンドが複数のショートカットを持つときの区切り。`QKeySequence` の
# 一覧を1つの文字列にする Qt の既定と同じ（多打鍵の `Ctrl+K, Ctrl+C` は
# カンマ区切りなので、代替の区切りには使えない）。
_ALTERNATE_SEPARATOR = "; "

# ショートカットとして読めなかった打鍵。`QKeySequence.fromString()` は
# 解釈できない文字列に対して例外を投げず、この値を1打鍵として返す。
_UNKNOWN_KEY = Qt.Key.Key_unknown


@dataclass(frozen=True, slots=True)
class ShortcutSpec:
    """設定できるコマンド1つ分の定義。

    `defaults` は PortableText の文字列で持つ。`QKeySequence` を
    モジュール変数にすると、`QGuiApplication` より先に作られる順序の
    問題を抱えるうえ、既定値をそのまま設定の形式で読めなくなる。
    """

    command_id: str
    label: str
    defaults: tuple[str, ...]


# ---------------------------------------------------------------- レジストリ
# **既定のショートカットの唯一の情報源。** `actions.py`・ダイアログ・
# 設定・テストのどこにも複製しない。
#
# 対象は P5-4 開始時点で既に明示的なショートカットを持っていたコマンドだけ。
# 「一般にはこちらが標準的」を理由に既定値を変えない。持っていなかった
# アクション（幅に合わせる・ページ全体・anp について・履歴のクリア）へ、
# ここで新しい既定値を足しもしない。
#
# 全画面を抜ける Esc はここに入れない。`QAction` ではなくウィンドウの
# `QShortcut` で、メニューに出るコマンドではないため（検索欄の
# Enter / Shift+Enter と同じく、ウィジェット局所の操作として据え置く）。
SHORTCUT_SPECS: tuple[ShortcutSpec, ...] = (
    ShortcutSpec("file.open", "PDF を開く", ("Ctrl+O",)),
    ShortcutSpec("file.quit", "終了", ("Ctrl+Q",)),
    ShortcutSpec("view.zoom_in", "拡大", ("Ctrl++", "Ctrl+=")),
    ShortcutSpec("view.zoom_out", "縮小", ("Ctrl+-",)),
    ShortcutSpec("view.actual_size", "実際の大きさ", ("Ctrl+0",)),
    ShortcutSpec("view.full_screen", "全画面表示", ("F11",)),
    ShortcutSpec("navigation.previous_page", "前のページ", ("Ctrl+PgUp",)),
    ShortcutSpec("navigation.next_page", "次のページ", ("Ctrl+PgDown",)),
    ShortcutSpec("search.find", "検索", ("Ctrl+F",)),
    ShortcutSpec("search.find_next", "次を検索", ("F3",)),
    ShortcutSpec("search.find_previous", "前を検索", ("Shift+F3",)),
)

_LABELS = {spec.command_id: spec.label for spec in SHORTCUT_SPECS}


def label_for(command_id: str) -> str:
    """コマンドの表示名。知らない ID はそのまま返す（診断のため）。"""
    return _LABELS.get(command_id, command_id)


def default_sequences(spec: ShortcutSpec) -> tuple[QKeySequence, ...]:
    """1コマンドの既定の割り当て。"""
    return tuple(QKeySequence.fromString(text, PORTABLE) for text in spec.defaults)


def default_assignments() -> dict[str, tuple[QKeySequence, ...]]:
    """全コマンドの既定の割り当て（レジストリ順）。"""
    return {spec.command_id: default_sequences(spec) for spec in SHORTCUT_SPECS}


# ---------------------------------------------------------------- 直列化
def format_assignment(sequences: Sequence[QKeySequence]) -> str:
    """割り当てを設定へ書ける文字列にする。空の割り当ては空文字。

    空文字は「割り当て無し」を意味する。設定にキーが**無い**こととは
    別物なので、混ぜてはいけない（`ShortcutManager` を参照）。
    """
    return QKeySequence.listToString(list(sequences), PORTABLE)


def parse_assignment(text: str) -> tuple[QKeySequence, ...] | None:
    """設定の文字列を割り当てへ戻す。読めなければ None。

    空文字は「割り当て無し」なので、空のタプル（None ではない）を返す。

    `QKeySequence.fromString()` は解釈できない文字列でも例外を投げず、
    `Key_unknown` を1打鍵として返す。そのまま `QAction` へ渡すと押しても
    反応しないショートカットが載るので、ここで読めなかったことにする。
    """
    sequences: list[QKeySequence] = []
    for sequence in QKeySequence.listFromString(text, PORTABLE):
        if sequence.isEmpty():
            continue
        if _has_unknown_key(sequence):
            return None
        sequences.append(sequence)
    return tuple(sequences)


def _has_unknown_key(sequence: QKeySequence) -> bool:
    """打鍵のどれかが「解釈できないキー」か。

    `QKeySequence` の添字アクセスは PySide6 の型定義に無いが、実際には
    打鍵ごとの `QKeyCombination` を返す。
    """
    return any(
        sequence[stroke].key() == _UNKNOWN_KEY  # type: ignore[index]
        for stroke in range(sequence.count())
    )


def display_text(sequences: Sequence[QKeySequence]) -> str:
    """利用者に見せる文字列。割り当てが無ければ空文字。

    表示だけに使う。この文字列を設定へ書かない。
    """
    return _ALTERNATE_SEPARATOR.join(sequence.toString(NATIVE) for sequence in sequences)


# ---------------------------------------------------------------- 衝突
@dataclass(frozen=True, slots=True)
class ShortcutConflict:
    """2つのコマンドの間で曖昧になっているショートカット。"""

    command_id: str
    other_command_id: str
    sequence: str
    """衝突したショートカット（PortableText）。"""

    other_sequence: str
    """相手側のショートカット。多打鍵の前方一致では別の文字列になる。"""


def find_conflicts(
    assignments: Mapping[str, Sequence[QKeySequence]],
) -> list[ShortcutConflict]:
    """曖昧になっている組を全て挙げる。無ければ空。

    見るのは**別のコマンド同士**だけ。1つのコマンドが持つ代替
    ショートカットは、同じコマンドを起動するので衝突ではない。

    完全一致（`Ctrl+O` と `Ctrl+O`）だけでなく、多打鍵の前方一致
    （`Ctrl+K` と `Ctrl+K, Ctrl+C`）も拾う。前者を押した時点で後者が
    確定できず、Qt には曖昧なショートカットとして届くため。
    `QKeySequence.matches()` は片方向の判定なので、両向きで見る。

    割り当ての無いコマンド（空）は無視する。
    """
    entries = [
        (command_id, sequence)
        for command_id, sequences in assignments.items()
        for sequence in sequences
        if not sequence.isEmpty()
    ]

    conflicts: list[ShortcutConflict] = []
    for position, (command_id, sequence) in enumerate(entries):
        for other_id, other in entries[position + 1 :]:
            if other_id == command_id:
                continue
            if not _is_ambiguous(sequence, other):
                continue
            conflicts.append(
                ShortcutConflict(
                    command_id=command_id,
                    other_command_id=other_id,
                    sequence=sequence.toString(PORTABLE),
                    other_sequence=other.toString(PORTABLE),
                )
            )
    return conflicts


def _is_ambiguous(first: QKeySequence, second: QKeySequence) -> bool:
    """2つのショートカットが同じ打鍵から始まるか。"""
    no_match = QKeySequence.SequenceMatch.NoMatch
    return first.matches(second) != no_match or second.matches(first) != no_match


def describe_conflicts(conflicts: Sequence[ShortcutConflict]) -> str:
    """衝突を利用者に見せる文面へ。どのコマンドのどのキーかが分かる形。"""
    return "\n".join(
        f"・{label_for(conflict.command_id)} ({conflict.sequence})"
        f" と {label_for(conflict.other_command_id)} ({conflict.other_sequence})"
        for conflict in conflicts
    )


# ---------------------------------------------------------------- 適用
def apply_assignments(
    actions: Mapping[str, QAction],
    assignments: Mapping[str, Sequence[QKeySequence]],
) -> None:
    """割り当てを `QAction` へ載せる。

    触るのはショートカットだけ。`triggered` の接続も、有効/無効も、
    `shortcutContext()` も作り直さない（`QAction` 自体も作り直さない）。

    知らないコマンド ID は黙って読み飛ばす。古い/新しいバージョンの設定に
    今は無いコマンドが残っていても、起動できなくならないようにするため。
    """
    for command_id, sequences in assignments.items():
        action = actions.get(command_id)
        if action is None:
            logger.warning("ignoring shortcut for unknown command: %s", command_id)
            continue
        action.setShortcuts(list(sequences))
