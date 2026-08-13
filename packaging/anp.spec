# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller のビルド定義。

```
uv run --group build pyinstaller packaging/anp.spec --noconfirm
```

`dist/anp/anp.exe` ができる。**onefile にはしない。** 1つの exe にすると
起動のたびに数百 MB を一時ディレクトリへ展開することになり、PDF を開く
たびに待たされる。Program Files へ置くのだから、ディレクトリのままで困らない。

QtPdf・QtWidgets・NumPy の取り込みは PyInstaller に同梱の PySide6 フックが
行うので、ここで隠し import を並べない。
"""

from pathlib import Path

_ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH は PyInstaller が入れる)

a = Analysis(  # noqa: F821
    [str(_ROOT / "src" / "anp" / "__main__.py")],
    pathex=[str(_ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # QtWebEngine と Qt Quick は使わない。取り込むと 100 MB 単位で太る。
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="anp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # コンソールを出さない GUI アプリとして作る。
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_ROOT / "packaging" / "anp.ico"),
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="anp",
)
