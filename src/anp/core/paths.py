"""アプリケーションが使うディレクトリとファイルの場所。

`QStandardPaths` に依存する箇所をここに閉じ込め、テストでは任意のベース
ディレクトリから `AppPaths` を組み立てられるようにする。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths


@dataclass(frozen=True, slots=True)
class AppPaths:
    """アプリケーションが読み書きする場所の一覧。"""

    data_dir: Path

    @classmethod
    def from_standard_paths(cls, app_name: str) -> AppPaths:
        """OS標準の位置から組み立てる（Windows では `%LOCALAPPDATA%\\<app_name>`）。

        ログとデータベースは端末固有なので、ローミングしない場所に置く。
        `AppLocalDataLocation` は organizationName と applicationName の両方を
        末尾に足して `anp\\anp` のように二重になるため、汎用の位置に自分で
        アプリ名を足す。
        """
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
        if not base:
            msg = "GenericDataLocation を取得できませんでした"
            raise RuntimeError(msg)
        return cls(data_dir=Path(base) / app_name)

    @property
    def log_dir(self) -> Path:
        """ログファイルを置くディレクトリ。"""
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        """アプリケーションログのパス。"""
        return self.log_dir / "anp.log"

    @property
    def database_file(self) -> Path:
        """学習メタデータを保存する SQLite ファイルのパス。"""
        return self.data_dir / "anp.sqlite3"

    @property
    def lock_file(self) -> Path:
        """二重起動を防ぐためのロックファイルのパス。

        DB と同じ場所に置く。守りたいのは「この DB を触るのは1プロセス
        だけ」なので、DB ごとにロックが1つある形にする。
        """
        return self.data_dir / "anp.lock"

    def ensure_directories(self) -> None:
        """必要なディレクトリを作成する。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
