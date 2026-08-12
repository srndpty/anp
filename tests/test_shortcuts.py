"""キーボードショートカットの設定（P5-4）のテスト。

確かめるのは6つ。

- レジストリが設定の契約になっていること（ID が一意・既定値が読める・
  既定同士が衝突しない・P5-4 導入前と同じキーが載る）
- 設定の意味（キーが無い＝既定 / 値が空＝割り当て無し）が保たれること
- 壊れた設定・曖昧な設定・知らないコマンド ID で落ちず、曖昧な
  ショートカットを `QAction` へ渡さないこと
- ダイアログが下書きしか触らないこと（キャンセル・既定値に戻す）
- OK が全体を検証してから丸ごと適用すること（部分適用しない）
- 実際のキー操作で切り替わり、再起動を跨いで残ること

キーの押下は `qtbot.keyClick()` で実際に流す。`sleep` も固定待ちも使わない。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QDialog, QWidget
from pytestqt.qtbot import QtBot

from anp.core.settings import Settings
from anp.pdf.color import PageColorMode
from anp.storage.study_mark import DocumentIdentity
from anp.storage.study_mark_repository import StudyMarkRepository
from anp.ui.actions import ReaderActions, create_actions
from anp.ui.main_window import MainWindow
from anp.ui.shortcut_dialog import ShortcutDialog
from anp.ui.shortcut_manager import ShortcutManager
from anp.ui.shortcuts import (
    PORTABLE,
    SHORTCUT_SPECS,
    default_assignments,
    default_sequences,
    display_text,
    find_conflicts,
    format_assignment,
    label_for,
    parse_assignment,
)
from anp.ui.study_marks import PagePosition
from conftest import SEARCH_QUERY
from helpers import ManualTransforms, put_image

# P5-4 導入前から使われていたキー。**設定の互換性の契約**なので、
# レジストリを書き換えたらここが落ちる。「一般にはこちらが標準的」を
# 理由に既定値を変えないための固定。
LEGACY_DEFAULTS = {
    "file.open": ("Ctrl+O",),
    "file.quit": ("Ctrl+Q",),
    "view.zoom_in": ("Ctrl++", "Ctrl+="),
    "view.zoom_out": ("Ctrl+-",),
    "view.actual_size": ("Ctrl+0",),
    "view.full_screen": ("F11",),
    "navigation.previous_page": ("Ctrl+PgUp",),
    "navigation.next_page": ("Ctrl+PgDown",),
    "search.find": ("Ctrl+F",),
    "search.find_next": ("F3",),
    "search.find_previous": ("Shift+F3",),
}


def seq(*texts: str) -> tuple[QKeySequence, ...]:
    """PortableText からショートカットの並びを作る。"""
    return tuple(QKeySequence.fromString(text, PORTABLE) for text in texts)


def texts(sequences: tuple[QKeySequence, ...]) -> tuple[str, ...]:
    """ショートカットの並びを PortableText の並びへ。比較を読みやすくする。"""
    return tuple(sequence.toString(PORTABLE) for sequence in sequences)


def action_texts(actions: ReaderActions, command_id: str) -> tuple[str, ...]:
    """`QAction` に実際に載っているショートカット。"""
    return tuple(
        sequence.toString(PORTABLE)
        for sequence in actions.command_actions()[command_id].shortcuts()
    )


@pytest.fixture
def ini(tmp_path: Path) -> str:
    """ウィンドウと設定オブジェクトが共有する設定ファイル。"""
    return str(tmp_path / "settings.ini")


@pytest.fixture
def store(ini: str) -> Settings:
    """一時ファイル上の INI を使う設定。"""
    return Settings(QSettings(ini, QSettings.Format.IniFormat))


@pytest.fixture
def action_parent(qtbot: QtBot) -> QWidget:
    """アクションの所有者。

    フィクスチャの戻り値として持っておかないと、Qt の親子関係ごと
    `QAction` が破棄される。
    """
    parent = QWidget()
    qtbot.addWidget(parent)
    return parent


@pytest.fixture
def actions(action_parent: QWidget) -> ReaderActions:
    """ウィンドウを作らずに用意したアクション一式。"""
    return create_actions(action_parent)


@pytest.fixture
def manager(store: Settings, actions: ReaderActions) -> ShortcutManager:
    """アクション一式に結び付いた管理オブジェクト。"""
    return ShortcutManager(store, actions.command_actions())


def rebind(manager: ShortcutManager, command_id: str, *sequences: str) -> None:
    """1コマンドだけを変えて保存・反映する（ダイアログの OK と同じ経路）。"""
    assignments = manager.current_assignments()
    assignments[command_id] = seq(*sequences)
    manager.apply(assignments)


# ================================================================ レジストリ
def test_command_ids_are_unique_and_usable() -> None:
    """ID は一意で空でなく、表示名を持ち、既定値が読める。"""
    command_ids = [spec.command_id for spec in SHORTCUT_SPECS]

    assert len(command_ids) == len(set(command_ids))
    assert all(spec.command_id for spec in SHORTCUT_SPECS)
    assert all(spec.label for spec in SHORTCUT_SPECS)
    for spec in SHORTCUT_SPECS:
        # 既定値は設定へ書くのと同じ書式で読めなければならない。
        assert parse_assignment(format_assignment(default_sequences(spec))) is not None


def test_command_ids_are_not_display_names() -> None:
    """設定のキーに表示文言を使っていない。

    表示名を直しただけで利用者の設定が消える設計になっていないこと。
    """
    labels = {spec.label for spec in SHORTCUT_SPECS}
    assert not labels & {spec.command_id for spec in SHORTCUT_SPECS}


def test_default_registry_has_no_conflicts() -> None:
    """既定同士が衝突しない。ここが落ちたら実装の誤り。"""
    assert find_conflicts(default_assignments()) == []


def test_defaults_match_the_shortcuts_used_before_p5_4() -> None:
    """P5-4 の前と同じキーが既定であること（設定の互換性の契約）。"""
    assert {
        command_id: texts(sequences) for command_id, sequences in default_assignments().items()
    } == LEGACY_DEFAULTS


def test_fresh_actions_carry_the_registry_defaults(actions: ReaderActions) -> None:
    """設定を読む前の `QAction` に、レジストリの既定が載っている。"""
    for command_id, sequences in default_assignments().items():
        assert action_texts(actions, command_id) == texts(sequences)


def test_every_registered_command_has_an_action(actions: ReaderActions) -> None:
    """レジストリと `QAction` の対応に漏れが無い。"""
    assert set(actions.command_actions()) == {spec.command_id for spec in SHORTCUT_SPECS}


def test_a_missing_action_is_a_programmer_error(store: Settings, actions: ReaderActions) -> None:
    """対応の漏れは設定の問題と分けて即座に落とす。"""
    incomplete = actions.command_actions()
    del incomplete["search.find"]

    with pytest.raises(ValueError, match=r"search\.find"):
        ShortcutManager(store, incomplete)


# ================================================================ 直列化
def test_portable_text_round_trip() -> None:
    """PortableText で書いて読むと同じ割り当てに戻る。"""
    original = seq("Ctrl+J")
    assert format_assignment(original) == "Ctrl+J"
    assert texts(parse_assignment("Ctrl+J") or ()) == ("Ctrl+J",)


def test_alternate_shortcuts_survive_the_round_trip() -> None:
    """複数のショートカットを持つコマンドを1つへ狭めない。"""
    stored = format_assignment(seq("Ctrl++", "Ctrl+="))

    assert texts(parse_assignment(stored) or ()) == ("Ctrl++", "Ctrl+=")


def test_multi_stroke_shortcuts_survive_the_round_trip() -> None:
    """多打鍵（カンマ区切り）も書式を壊さない。"""
    stored = format_assignment(seq("Ctrl+K, Ctrl+C"))

    assert texts(parse_assignment(stored) or ()) == ("Ctrl+K, Ctrl+C",)


def test_an_empty_value_means_unbound() -> None:
    """空文字は「割り当て無し」。読めなかった値ではない。"""
    assert parse_assignment("") == ()


@pytest.mark.parametrize("value", ["garbage", "Ctrl+Nope", "Ctrl+", "Nonsense, Ctrl+C"])
def test_unreadable_values_are_rejected(value: str) -> None:
    """読めない文字列を割り当てとして受け付けない。

    `QKeySequence.fromString()` は例外を投げずに「不明なキー」を返すので、
    そのまま通すと押しても反応しないショートカットが載る。
    """
    assert parse_assignment(value) is None


def test_display_text_is_not_the_stored_format() -> None:
    """表示は利用者向けの書式で、設定の契約とは別物。"""
    assert display_text(seq("Ctrl+O")) != ""
    assert display_text(()) == ""


def test_label_for_falls_back_to_the_id() -> None:
    """知らない ID でも診断できる文字列を返す。"""
    assert label_for("some.removed.command") == "some.removed.command"


# ================================================================ 衝突
def test_an_exact_duplicate_is_a_conflict() -> None:
    """同じキーを2つのコマンドへ割り当てられない。"""
    conflicts = find_conflicts({"a": seq("Ctrl+X"), "b": seq("Ctrl+X")})

    assert len(conflicts) == 1
    assert {conflicts[0].command_id, conflicts[0].other_command_id} == {"a", "b"}


def test_a_multi_stroke_prefix_is_a_conflict() -> None:
    """`Ctrl+K` と `Ctrl+K, Ctrl+C` は前方一致で曖昧になる。"""
    assert find_conflicts({"a": seq("Ctrl+K"), "b": seq("Ctrl+K, Ctrl+C")}) != []


def test_a_multi_stroke_prefix_is_a_conflict_in_either_order() -> None:
    """並び順を入れ替えても検出する（`matches()` は片方向の判定）。"""
    assert find_conflicts({"a": seq("Ctrl+K, Ctrl+C"), "b": seq("Ctrl+K")}) != []


def test_unbound_commands_never_conflict() -> None:
    """割り当ての無いコマンド同士はぶつからない。"""
    assert find_conflicts({"a": (), "b": seq("Ctrl+X"), "c": ()}) == []


def test_a_command_does_not_conflict_with_itself() -> None:
    """同じコマンドが持つ代替ショートカットは衝突ではない。"""
    assert find_conflicts({"a": seq("Ctrl+X", "Ctrl+X")}) == []
    assert find_conflicts({"a": seq("Ctrl+K", "Ctrl+K, Ctrl+C")}) == []


def test_different_shortcuts_do_not_conflict() -> None:
    """関係のないキー同士は衝突にしない。"""
    assert find_conflicts({"a": seq("Ctrl+X"), "b": seq("Ctrl+Y")}) == []


def test_conflicts_name_the_commands_and_keys() -> None:
    """利用者に「どのコマンドのどのキーか」が分かる。"""
    from anp.ui.shortcuts import describe_conflicts

    conflicts = find_conflicts({"search.find": seq("Ctrl+X"), "file.open": seq("Ctrl+X")})
    message = describe_conflicts(conflicts)

    assert "検索" in message
    assert "PDF を開く" in message
    assert "Ctrl+X" in message


# ================================================================ 設定の意味
def test_a_missing_key_means_the_default(store: Settings) -> None:
    """設定にキーが無ければ既定値。"""
    assert store.shortcut_override("search.find") is None


def test_a_stored_empty_value_is_not_missing(store: Settings) -> None:
    """空文字を保存すると「キーが無い」とは区別される。"""
    store.set_shortcut_override("search.find", "")

    assert store.shortcut_override("search.find") == ""


def test_missing_settings_give_the_defaults(manager: ShortcutManager) -> None:
    """設定が空なら既定の割り当て。"""
    assert manager.stored_assignments() == default_assignments()


def test_an_explicit_empty_setting_unbinds(manager: ShortcutManager, store: Settings) -> None:
    """空文字は「割り当て無し」。既定へは戻らない。"""
    store.set_shortcut_override("search.find", "")

    assert manager.stored_assignments()["search.find"] == ()


def test_a_custom_setting_is_used(manager: ShortcutManager, store: Settings) -> None:
    """保存された割り当てが既定より優先される。"""
    store.set_shortcut_override("navigation.next_page", "Ctrl+J")

    assert texts(manager.stored_assignments()["navigation.next_page"]) == ("Ctrl+J",)


def test_a_custom_setting_round_trips_through_a_new_settings_object(
    manager: ShortcutManager, ini: str
) -> None:
    """保存した割り当ては、別の `Settings` からも同じ書式で読める。"""
    rebind(manager, "navigation.next_page", "Ctrl+J")

    reopened = Settings(QSettings(ini, QSettings.Format.IniFormat))
    assert reopened.shortcut_override("navigation.next_page") == "Ctrl+J"


def test_going_back_to_the_default_removes_the_override(
    manager: ShortcutManager, store: Settings
) -> None:
    """既定と同じ割り当てなら、設定のキーを残さない。"""
    rebind(manager, "navigation.next_page", "Ctrl+J")
    assert store.shortcut_override("navigation.next_page") is not None

    rebind(manager, "navigation.next_page", "Ctrl+PgDown")

    assert store.shortcut_override("navigation.next_page") is None


def test_an_explicit_unbind_is_stored_as_empty(manager: ShortcutManager, store: Settings) -> None:
    """解除は空文字として残る（既定へ戻したことにしない）。"""
    rebind(manager, "search.find")

    assert store.shortcut_override("search.find") == ""


def test_an_unreadable_setting_falls_back_to_the_default(
    manager: ShortcutManager, store: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """読めない値でも落ちず、そのコマンドだけ既定へ戻る。"""
    store.set_shortcut_override("search.find", "garbage")

    with caplog.at_level("WARNING"):
        assignments = manager.stored_assignments()

    assert texts(assignments["search.find"]) == ("Ctrl+F",)
    # 他のコマンドの設定は生きたまま。
    assert texts(assignments["navigation.next_page"]) == ("Ctrl+PgDown",)
    assert "search.find" in caplog.text


def test_an_unreadable_setting_does_not_unbind(manager: ShortcutManager, store: Settings) -> None:
    """読めない値を「割り当て無し」と取り違えない。"""
    store.set_shortcut_override("search.find", "garbage")

    assert manager.stored_assignments()["search.find"] != ()


def test_an_unknown_command_id_is_ignored(manager: ShortcutManager, store: Settings) -> None:
    """今は無いコマンドの設定が残っていても影響しない。"""
    store.set_shortcut_override("some.removed.command", "Ctrl+Y")

    assignments = manager.stored_assignments()

    assert "some.removed.command" not in assignments
    assert assignments == default_assignments()


def test_an_unknown_command_id_survives_a_save(manager: ShortcutManager, store: Settings) -> None:
    """起動しただけで知らない設定を消しに行かない。"""
    store.set_shortcut_override("some.removed.command", "Ctrl+Y")

    rebind(manager, "navigation.next_page", "Ctrl+J")

    assert store.shortcut_override("some.removed.command") == "Ctrl+Y"


def test_a_conflicting_setting_set_is_dropped_as_a_whole(
    manager: ShortcutManager, store: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """手で編集された設定が曖昧なら、その起動ではカスタムを丸ごと使わない。

    一部だけ既定へ戻すと、戻した既定が別のカスタムとぶつかる連鎖が起きる。
    """
    store.set_shortcut_override("file.open", "Ctrl+X")
    store.set_shortcut_override("search.find", "Ctrl+X")

    with caplog.at_level("WARNING"):
        assignments = manager.stored_assignments()

    assert assignments == default_assignments()
    assert "ambiguous" in caplog.text


def test_a_conflicting_setting_set_is_not_deleted(
    manager: ShortcutManager, store: Settings
) -> None:
    """曖昧でも設定ファイルは勝手に消さない。"""
    store.set_shortcut_override("file.open", "Ctrl+X")
    store.set_shortcut_override("search.find", "Ctrl+X")

    manager.stored_assignments()

    assert store.shortcut_override("file.open") == "Ctrl+X"


def test_a_conflicting_setting_set_does_not_reach_the_actions(
    manager: ShortcutManager, actions: ReaderActions, store: Settings
) -> None:
    """曖昧なショートカットを `QAction` へ渡さない。"""
    store.set_shortcut_override("file.open", "Ctrl+X")
    store.set_shortcut_override("search.find", "Ctrl+X")

    manager.apply_stored()

    assert action_texts(actions, "file.open") == ("Ctrl+O",)
    assert action_texts(actions, "search.find") == ("Ctrl+F",)


def test_applying_a_conflicting_set_is_refused(manager: ShortcutManager) -> None:
    """検証を通っていない割り当ては受け付けない。"""
    assignments = manager.current_assignments()
    assignments["search.find"] = seq("Ctrl+O")

    with pytest.raises(ValueError, match="ambiguous"):
        manager.apply(assignments)


def test_a_non_string_setting_falls_back_to_the_default(
    manager: ShortcutManager, store: Settings
) -> None:
    """文字列として読めない値でも落ちない。"""
    store.set_shortcut_override("search.find", "")
    # レジストリではなく INI でも起こり得る型（数値）を直接ねじ込む。
    store._backend.setValue("shortcuts/search.find", 3)  # noqa: SLF001

    assert texts(manager.stored_assignments()["search.find"]) == ("Ctrl+F",)


def test_a_hand_written_multi_stroke_value_is_read(
    manager: ShortcutManager, store: Settings
) -> None:
    """INI へ引用符なしで書かれた多打鍵（カンマ区切り）も読める。"""
    store._backend.setValue("shortcuts/search.find", ["Ctrl+K", "Ctrl+C"])  # noqa: SLF001

    assert texts(manager.stored_assignments()["search.find"]) == ("Ctrl+K, Ctrl+C",)


def test_applying_only_touches_the_shortcuts(
    manager: ShortcutManager, actions: ReaderActions
) -> None:
    """有効/無効もショートカットの文脈も `QAction` の同一性も変えない。"""
    find = actions.find
    find.setEnabled(False)
    context = find.shortcutContext()

    rebind(manager, "search.find", "Ctrl+G")

    assert actions.find is find
    assert find.isEnabled() is False
    assert find.shortcutContext() == context


# ================================================================ ダイアログ
@pytest.fixture
def dialog(qtbot: QtBot, manager: ShortcutManager) -> ShortcutDialog:
    """現在の割り当てを写したダイアログ。"""
    dialog = ShortcutDialog(manager.current_assignments())
    qtbot.addWidget(dialog)
    return dialog


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """衝突の警告ダイアログを捕まえて本文を集める。"""
    messages: list[str] = []
    monkeypatch.setattr(
        "anp.ui.shortcut_dialog.QMessageBox.warning",
        lambda *args: messages.append(args[2]),
    )
    return messages


def test_the_dialog_shows_the_effective_shortcuts(
    qtbot: QtBot, manager: ShortcutManager, store: Settings
) -> None:
    """設定の生の値ではなく、いま効いている割り当てを見せる。

    ここでは設定に曖昧な組み合わせを仕込む。実際に効いているのは既定なので、
    ダイアログにも既定が出なければならない。
    """
    store.set_shortcut_override("file.open", "Ctrl+X")
    store.set_shortcut_override("search.find", "Ctrl+X")
    manager.apply_stored()

    dialog = ShortcutDialog(manager.current_assignments())
    qtbot.addWidget(dialog)

    assert dialog.shortcut_text("search.find") == display_text(seq("Ctrl+F"))


def test_the_dialog_shows_the_defaults_column(dialog: ShortcutDialog) -> None:
    """既定値の列がレジストリのとおりに出る。"""
    assert dialog.default_text("view.zoom_in") == display_text(seq("Ctrl++", "Ctrl+="))


def test_editing_only_changes_the_draft(
    dialog: ShortcutDialog, actions: ReaderActions, store: Settings
) -> None:
    """編集しても `QAction` にも設定にも書かない。"""
    dialog.select("search.find")
    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+G", PORTABLE))

    assert texts(dialog.assignments["search.find"]) == ("Ctrl+G",)
    assert action_texts(actions, "search.find") == ("Ctrl+F",)
    assert store.shortcut_override("search.find") is None


def test_clearing_unbinds_in_the_draft(dialog: ShortcutDialog) -> None:
    """解除ボタンで割り当てを空にできる。"""
    dialog.select("search.find")
    dialog.clear_button.click()

    assert dialog.assignments["search.find"] == ()
    assert dialog.shortcut_text("search.find") == ""


def test_restore_defaults_only_changes_the_draft(
    dialog: ShortcutDialog, actions: ReaderActions, store: Settings
) -> None:
    """「既定値に戻す」も下書きだけ。設定にも `QAction` にも書かない。"""
    dialog.select("navigation.next_page")
    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+J", PORTABLE))

    dialog.restore_defaults()

    assert texts(dialog.assignments["navigation.next_page"]) == ("Ctrl+PgDown",)
    assert action_texts(actions, "navigation.next_page") == ("Ctrl+PgDown",)
    assert store.shortcut_override("navigation.next_page") is None


def test_restore_defaults_brings_back_a_cleared_assignment(dialog: ShortcutDialog) -> None:
    """解除しても、既定へ戻せば代替ごと戻る。"""
    dialog.select("view.zoom_in")
    dialog.clear_button.click()
    assert dialog.assignments["view.zoom_in"] == ()

    dialog.restore_defaults()

    assert texts(dialog.assignments["view.zoom_in"]) == ("Ctrl++", "Ctrl+=")


# ---------------------------------------------------------------- 代替の保持
# `QKeySequenceEdit` が扱えるのは1つだけなので、打鍵で編集できるのは先頭の
# ショートカットだけ。**そこで代替まで巻き添えに消さない。**
def test_editing_keeps_the_alternate_shortcut(dialog: ShortcutDialog) -> None:
    """拡大の先頭を変えても `Ctrl+=` は残る。"""
    dialog.select("view.zoom_in")

    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+Up", PORTABLE))

    assert texts(dialog.assignments["view.zoom_in"]) == ("Ctrl+Up", "Ctrl+=")


def test_editing_keeps_a_custom_alternate_shortcut(qtbot: QtBot) -> None:
    """既定に限らず、いま持っている代替を残す。"""
    dialog = ShortcutDialog({"view.zoom_in": seq("Ctrl+K", "Ctrl+L")})
    qtbot.addWidget(dialog)
    dialog.select("view.zoom_in")

    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+J", PORTABLE))

    assert texts(dialog.assignments["view.zoom_in"]) == ("Ctrl+J", "Ctrl+L")


def test_editing_after_clearing_leaves_a_single_shortcut(dialog: ShortcutDialog) -> None:
    """代替ごと捨てたいときは「解除」してから打鍵する。"""
    dialog.select("view.zoom_in")
    dialog.clear_button.click()

    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+J", PORTABLE))

    assert texts(dialog.assignments["view.zoom_in"]) == ("Ctrl+J",)


def test_editing_to_the_alternate_key_does_not_duplicate(dialog: ShortcutDialog) -> None:
    """先頭を代替と同じキーにしても、同じ割り当てを2つ持たない。"""
    dialog.select("view.zoom_in")

    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+=", PORTABLE))

    assert texts(dialog.assignments["view.zoom_in"]) == ("Ctrl+=",)


def test_editing_a_single_shortcut_command_stays_single(dialog: ShortcutDialog) -> None:
    """代替を持たないコマンドは、編集しても1つのまま。"""
    dialog.select("search.find")

    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+G", PORTABLE))

    assert texts(dialog.assignments["search.find"]) == ("Ctrl+G",)


def test_the_alternate_shortcut_survives_the_round_trip_to_the_actions(
    dialog: ShortcutDialog, manager: ShortcutManager, actions: ReaderActions, store: Settings
) -> None:
    """OK まで通しても代替が残り、設定にも両方が書かれる。"""
    dialog.select("view.zoom_in")
    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+Up", PORTABLE))
    dialog.accept()

    manager.apply(dialog.assignments)

    assert action_texts(actions, "view.zoom_in") == ("Ctrl+Up", "Ctrl+=")
    assert store.shortcut_override("view.zoom_in") == "Ctrl+Up; Ctrl+="


def test_a_conflicting_draft_keeps_the_dialog_open(
    dialog: ShortcutDialog, warnings: list[str], actions: ReaderActions, store: Settings
) -> None:
    """衝突したまま OK を押しても、閉じず・何も適用しない。"""
    dialog.select("search.find")
    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+O", PORTABLE))

    dialog.accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert len(warnings) == 1
    assert "検索" in warnings[0]
    assert "PDF を開く" in warnings[0]
    # 部分適用も禁止。片方だけ載っていないこと。
    assert action_texts(actions, "search.find") == ("Ctrl+F",)
    assert action_texts(actions, "file.open") == ("Ctrl+O",)
    assert store.shortcut_override("search.find") is None


def test_a_clean_draft_accepts(dialog: ShortcutDialog, warnings: list[str]) -> None:
    """衝突が無ければ閉じる。"""
    dialog.select("search.find")
    dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+G", PORTABLE))

    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert warnings == []


# ================================================================ ウィンドウ統合
def make_window(qtbot: QtBot, ini: str, repository: StudyMarkRepository) -> MainWindow:
    """設定を読み込んで表示済みのウィンドウを作る。"""
    window = MainWindow(Settings(QSettings(ini, QSettings.Format.IniFormat)), repository)
    qtbot.addWidget(window)
    with qtbot.waitExposed(window):
        window.show()
    return window


@pytest.fixture
def window(qtbot: QtBot, ini: str, study_marks: StudyMarkRepository) -> Iterator[MainWindow]:
    """表示済みのメインウィンドウ。"""
    window = make_window(qtbot, ini, study_marks)
    yield window
    window.close()


@pytest.fixture
def opened(window: MainWindow, sample_pdf: Path) -> MainWindow:
    """3ページの PDF を開いた状態のウィンドウ。"""
    window.open_path(sample_pdf)
    return window


def test_the_window_applies_the_defaults(window: MainWindow) -> None:
    """設定が無ければ P5-4 前と同じキーで動く。"""
    for command_id, sequences in default_assignments().items():
        assert action_texts(window.reader_actions, command_id) == texts(sequences)


def test_the_window_applies_stored_overrides_at_startup(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository
) -> None:
    """保存済みの割り当ては、起動した時点で載っている。

    メニューを一度開くまで反映されない、といった遅延を作らない。
    """
    Settings(QSettings(ini, QSettings.Format.IniFormat)).set_shortcut_override(
        "navigation.next_page", "Ctrl+J"
    )

    window = make_window(qtbot, ini, study_marks)
    try:
        assert action_texts(window.reader_actions, "navigation.next_page") == ("Ctrl+J",)
    finally:
        window.close()


def test_rebinding_find_moves_the_key(opened: MainWindow, qtbot: QtBot) -> None:
    """Ctrl+F を Ctrl+G へ移すと、新しいキーだけが検索を開く。"""
    rebind(opened.shortcuts, "search.find", "Ctrl+G")

    qtbot.keyClick(opened, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
    assert opened.search_dock.isHidden() is False

    opened.search_dock.hide()
    qtbot.keyClick(opened, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert opened.search_dock.isHidden() is True


def test_unbinding_find_keeps_the_menu_command(opened: MainWindow, qtbot: QtBot) -> None:
    """解除してもコマンド自体は無効にしない。メニューからは使える。"""
    rebind(opened.shortcuts, "search.find")

    qtbot.keyClick(opened, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert opened.search_dock.isHidden() is True
    assert opened.reader_actions.find.isEnabled() is True

    opened.reader_actions.find.trigger()
    assert opened.search_dock.isHidden() is False


def test_rebinding_next_page_moves_the_key(opened: MainWindow, qtbot: QtBot) -> None:
    """実際の PDF で、新しいキーだけがページを進める。"""
    assert opened.view.current_page == 0

    rebind(opened.shortcuts, "navigation.next_page", "Ctrl+J")

    qtbot.keyClick(opened, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
    assert opened.view.current_page == 1

    qtbot.keyClick(opened, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier)
    assert opened.view.current_page == 1


def test_custom_shortcuts_survive_a_restart(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, sample_pdf: Path
) -> None:
    """同じ INI から作り直したウィンドウでも、変更したキーが効く。"""
    first = make_window(qtbot, ini, study_marks)
    rebind(first.shortcuts, "navigation.next_page", "Ctrl+J")
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        second.open_path(sample_pdf)
        qtbot.keyClick(second, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
        assert second.view.current_page == 1

        qtbot.keyClick(second, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier)
        assert second.view.current_page == 1
    finally:
        second.close()


def test_restoring_defaults_survives_a_restart(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository
) -> None:
    """既定へ戻したら、次の起動でも既定のまま。"""
    first = make_window(qtbot, ini, study_marks)
    rebind(first.shortcuts, "navigation.next_page", "Ctrl+J")
    first.shortcuts.apply(default_assignments())
    first.close()

    second = make_window(qtbot, ini, study_marks)
    try:
        assert action_texts(second.reader_actions, "navigation.next_page") == ("Ctrl+PgDown",)
        assert (
            Settings(QSettings(ini, QSettings.Format.IniFormat)).shortcut_override(
                "navigation.next_page"
            )
            is None
        )
    finally:
        second.close()


def test_the_dialog_opens_from_the_settings_menu(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """メニューからダイアログを開き、OK なら割り当てが反映される。"""

    def accept(dialog: ShortcutDialog) -> int:
        dialog.select("navigation.next_page")
        dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+J", PORTABLE))
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr("anp.ui.shortcut_dialog.ShortcutDialog.exec", accept)

    window.reader_actions.shortcut_settings.trigger()

    assert action_texts(window.reader_actions, "navigation.next_page") == ("Ctrl+J",)


def test_cancelling_the_dialog_changes_nothing(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, ini: str
) -> None:
    """キャンセルなら `QAction` も設定も変わらない。"""

    def reject(dialog: ShortcutDialog) -> int:
        dialog.select("navigation.next_page")
        dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+J", PORTABLE))
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr("anp.ui.shortcut_dialog.ShortcutDialog.exec", reject)

    window.reader_actions.shortcut_settings.trigger()

    assert action_texts(window.reader_actions, "navigation.next_page") == ("Ctrl+PgDown",)
    stored = Settings(QSettings(ini, QSettings.Format.IniFormat))
    assert stored.shortcut_override("navigation.next_page") is None


# ---------------------------------------------------------------- 他の状態への影響
def test_document_dependent_state_is_unchanged(window: MainWindow, sample_pdf: Path) -> None:
    """ショートカットの変更で、有効/無効の契約を壊さない。"""
    assert window.reader_actions.zoom_in.isEnabled() is False

    rebind(window.shortcuts, "view.zoom_in", "Ctrl+Up")
    assert window.reader_actions.zoom_in.isEnabled() is False

    window.open_path(sample_pdf)
    assert window.reader_actions.zoom_in.isEnabled() is True

    rebind(window.shortcuts, "view.zoom_in", "Ctrl+Down")
    assert window.reader_actions.zoom_in.isEnabled() is True


def test_page_state_is_unchanged(opened: MainWindow) -> None:
    """前/次ページの有効状態は現在ページだけで決まる。"""
    assert opened.reader_actions.previous_page.isEnabled() is False
    assert opened.reader_actions.next_page.isEnabled() is True

    rebind(opened.shortcuts, "navigation.next_page", "Ctrl+J")

    assert opened.reader_actions.previous_page.isEnabled() is False
    assert opened.reader_actions.next_page.isEnabled() is True


def test_search_dock_enter_still_moves_to_the_next_result(
    qtbot: QtBot, window: MainWindow, searchable_pdf: Path
) -> None:
    """次を検索のキーを変えても、検索欄の Enter は従来どおり。"""
    window.open_path(searchable_pdf)
    window.search_dock.query_edit.setText(SEARCH_QUERY)

    def has_results() -> None:
        assert window.search.state.count > 0

    qtbot.waitUntil(has_results)
    first = window.search.state.current_index

    rebind(window.shortcuts, "search.find_next", "Ctrl+Alt+N")

    qtbot.keyClick(window.search_dock.query_edit, Qt.Key.Key_Return)

    assert window.search.state.current_index != first


def test_search_state_is_untouched(qtbot: QtBot, window: MainWindow, searchable_pdf: Path) -> None:
    """ショートカットの変更で検索語も結果も動かない。"""
    window.open_path(searchable_pdf)
    window.search_dock.query_edit.setText(SEARCH_QUERY)

    def has_results() -> None:
        assert window.search.state.count > 0

    qtbot.waitUntil(has_results)
    before = window.search.state

    rebind(window.shortcuts, "search.find", "Ctrl+G")

    assert window.search.state == before
    assert window.search_dock.query_edit.text() == SEARCH_QUERY


def test_study_marks_are_untouched(opened: MainWindow, sample_pdf: Path) -> None:
    """ショートカットの変更で学習マークに触らない。"""
    opened.study_marks.create_mark(
        PagePosition(page_index=0, x_norm=0.5, y_norm=0.5),
        expected_document=DocumentIdentity.of(sample_pdf),
    )
    before = opened.study_marks.study_marks
    assert len(before) == 1

    rebind(opened.shortcuts, "search.find", "Ctrl+G")

    assert opened.study_marks.study_marks == before


def test_toc_and_recent_files_are_untouched(
    qtbot: QtBot, ini: str, study_marks: StudyMarkRepository, outline_pdf: Path
) -> None:
    """ショートカットの変更で目次も履歴も最後のディレクトリも動かない。"""
    window = make_window(qtbot, ini, study_marks)
    try:
        window.open_path(outline_pdf)
        recent = window.recent_files
        outline = window.toc_sidebar.has_outline
        directory = Settings(QSettings(ini, QSettings.Format.IniFormat)).last_directory

        rebind(window.shortcuts, "search.find", "Ctrl+G")

        assert window.recent_files == recent
        assert window.toc_sidebar.has_outline == outline
        assert Settings(QSettings(ini, QSettings.Format.IniFormat)).last_directory == directory
    finally:
        window.close()


def test_editing_shortcuts_does_not_touch_the_rendering_pipeline(
    opened: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ショートカットの設定はレンダリングにもキャッシュにも触らない。

    ダイアログを開いて編集し、OK まで通しても、世代も持っている画像
    （生・表示用の両方）もページの色も変わらない。

    表示用キャッシュまで見るのは、生の画像だけ残して表示用を捨てる実装でも
    通ってしまわないようにするため（P4 で一度空いていた穴）。色変換は
    本物のワーカーへ流さず、投入と同時に完了させて決定的にする。
    """
    render = opened.view._render  # noqa: SLF001
    cache = opened._cache  # noqa: SLF001
    ManualTransforms(render, immediate=True)

    put_image(cache, opened.view, 0, Qt.GlobalColor.red)
    raw_key = cache.nearest_key(0, width_px=0)
    assert raw_key is not None

    opened.view.set_page_color_mode(PageColorMode.INVERT)
    display_key = render.display_cache.nearest_key(0, 0, PageColorMode.INVERT)
    assert display_key is not None

    generation = render.generation
    color_mode = opened.view.page_color_mode

    def accept(dialog: ShortcutDialog) -> int:
        dialog.select("navigation.next_page")
        dialog.editor.setKeySequence(QKeySequence.fromString("Ctrl+J", PORTABLE))
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr("anp.ui.shortcut_dialog.ShortcutDialog.exec", accept)
    opened.reader_actions.shortcut_settings.trigger()

    assert render.generation == generation
    assert raw_key in cache
    assert display_key in render.display_cache
    assert opened.view.page_color_mode is color_mode
    assert color_mode is PageColorMode.INVERT
