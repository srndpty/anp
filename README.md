# anp

学習向けの PDF リーダー。非破壊アノテーションと強力なダークモードを備える。

技術書・数学書・問題集を読むための個人用デスクトップアプリケーション。PDF ファイル
自体は変更せず、学習メタデータは別途 SQLite に保存する。主対象は Windows 11。

## 状態

**Phase 1（読み取り専用リーダー）まで完了。**

| フェーズ | 内容 | 状態 |
|---|---|---|
| Phase 0 | プロジェクト基盤、ウィンドウ、設定、ログ、永続化の入口 | 完了 |
| Phase 1 | 読み取り専用 PDF リーダー（連続スクロール、ズーム、ページ移動） | 完了 |
| Phase 2 以降 | ページ色変換（Invert / Smart Dark）、学習マーク、目次、検索 | 未着手 |

## できること

- PDF を開いて**連続スクロール**で読む
- 拡大・縮小、実際の大きさ、**幅に合わせる**、**ページ全体**
- Ctrl + ホイールで、カーソル位置を動かさずに拡大・縮小
- ページ番号を指定しての移動、前後のページへの移動
- 全画面表示
- ウィンドウの位置・サイズ、倍率とその決め方を次回起動時に復元

高 DPI（125% / 150%）のスケーリングに合わせた解像度でレンダリングする。

### まだできないこと

パスワード付き PDF を開くこと、ページ色の変換（Invert / Smart Dark）、
学習マーク、注釈、検索、目次、最近使ったファイル、読書位置の記憶。
いずれも Phase 2 以降で扱う。

## キーボードショートカット

| キー | 動作 |
|---|---|
| `Ctrl+O` | PDF を開く |
| `Ctrl+Q` | 終了 |
| `Ctrl++` / `Ctrl+=` | 拡大 |
| `Ctrl+-` | 縮小 |
| `Ctrl+0` | 実際の大きさ（100%） |
| `Ctrl+PgUp` / `Ctrl+PgDown` | 前 / 次のページ |
| `F11` | 全画面表示の切り替え |
| `Esc` | 全画面表示を抜ける |
| `Ctrl` + ホイール | カーソル位置を中心に拡大・縮小 |

「幅に合わせる」「ページ全体」はツールバーと表示メニューから選ぶ。
`PageUp` / `PageDown` は通常のスクロール操作のままにしてある。

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
