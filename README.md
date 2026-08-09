# anp

学習向けの PDF リーダー。非破壊アノテーションと強力なダークモードを備える。

技術書・数学書・問題集を読むための個人用デスクトップアプリケーション。PDF ファイル
自体は変更せず、学習メタデータは別途 SQLite に保存する。主対象は Windows 11。

## 状態

**Phase 0（基盤）まで完了。** PDF の表示は Phase 1 で実装する。

| フェーズ | 内容 | 状態 |
|---|---|---|
| Phase 0 | プロジェクト基盤、ウィンドウ、設定、ログ、永続化の入口 | 完了 |
| Phase 1 | 読み取り専用 PDF リーダー（連続スクロール、ズーム、ページ移動） | 未着手 |
| Phase 2 以降 | ページ色変換（Invert / Smart Dark）、学習マーク、目次、検索 | 未着手 |

## 必要なもの

- Python 3.12 以上
- [uv](https://docs.astral.sh/uv/)

## セットアップ

```bash
uv sync --all-groups
uv run pre-commit install
```

## 実行

```bash
uv run anp
```

## 開発

```bash
uv run pytest --cov=anp   # テスト
uv run ruff check .       # lint
uv run ruff format .      # 整形
uv run mypy               # 型チェック
```

開発規約は [AGENTS.md](AGENTS.md) を参照。

## データの保存場所

| 種類 | 場所 |
|---|---|
| ログ | `%LOCALAPPDATA%\anp\logs\anp.log` |
| データベース | `%LOCALAPPDATA%\anp\anp.sqlite3` |
| 設定 | レジストリ `HKEY_CURRENT_USER\Software\anp\anp` |
