# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for the Canopy tray application.

Build with:
    pyinstaller canopy_tray/build.spec --clean

Output:
    dist/Canopy/Canopy.exe
"""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve().parent.parent


def _existing_path(src: Path, dst: str) -> tuple[str, str] | None:
    if src.exists():
        return (str(src), dst)
    return None


datas = []
datas.extend(copy_metadata("canopy"))
extra_paths = [
    _existing_path(PROJECT_ROOT / "canopy" / "ui" / "templates", "canopy/ui/templates"),
    _existing_path(PROJECT_ROOT / "canopy" / "ui" / "static", "canopy/ui/static"),
    _existing_path(PROJECT_ROOT / "canopy_tray" / "assets", "canopy_tray/assets"),
    _existing_path(PROJECT_ROOT / "logos", "logos"),
]
datas.extend([entry for entry in extra_paths if entry is not None])

hiddenimports = []
for package in (
    "canopy.api",
    "canopy.core",
    "canopy.network",
    "canopy.security",
    "canopy.ui",
    "canopy_tray",
    "pystray",
    "winotify",
):
    hiddenimports.extend(collect_submodules(package))

a = Analysis(
    [str(PROJECT_ROOT / "canopy_tray" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "canopy.mcp",
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "pytest",
        "black",
        "flake8",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Canopy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=str(PROJECT_ROOT / "canopy_tray" / "assets" / "canopy.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Canopy",
)
