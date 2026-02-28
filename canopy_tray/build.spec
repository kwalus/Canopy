# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Canopy system tray application.

Build with:
    cd "D:\Dropbox\Python Toolbox\Canopy"
    pyinstaller canopy_tray/build.spec

Output: dist/Canopy.exe
"""

import os
import sys
from pathlib import Path

block_cipher = None

# Project root (where this spec file's parent directory lives)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPECPATH)))

# Collect all data files
datas = [
    # Flask templates and static files
    (os.path.join(PROJECT_ROOT, 'canopy', 'ui', 'templates'), 'canopy/ui/templates'),
    (os.path.join(PROJECT_ROOT, 'canopy', 'ui', 'static'), 'canopy/ui/static'),
    # Tray assets (icon)
    (os.path.join(PROJECT_ROOT, 'canopy_tray', 'assets'), 'canopy_tray/assets'),
    # Logos
    (os.path.join(PROJECT_ROOT, 'logos'), 'logos'),
]

# Filter out non-existent paths
datas = [(src, dst) for src, dst in datas if os.path.exists(src)]

# Hidden imports that PyInstaller might miss
hiddenimports = [
    # Flask and related
    'flask',
    'flask.json',
    'werkzeug',
    'werkzeug.serving',
    'jinja2',
    'markupsafe',
    # Canopy core
    'canopy',
    'canopy.core',
    'canopy.core.app',
    'canopy.core.config',
    'canopy.core.database',
    'canopy.core.device',
    'canopy.core.channels',
    'canopy.core.profile',
    'canopy.api',
    'canopy.api.routes',
    'canopy.ui',
    'canopy.ui.routes',
    'canopy.network',
    'canopy.network.manager',
    'canopy.network.connection',
    'canopy.network.discovery',
    'canopy.network.identity',
    'canopy.network.routing',
    'canopy.network.invite',
    'canopy.security',
    'canopy.security.encryption',
    # P2P networking
    'websockets',
    'websockets.server',
    'websockets.client',
    'zeroconf',
    'msgpack',
    'base58',
    # Crypto
    'cryptography',
    'bcrypt',
    # Tray
    'pystray',
    'pystray._win32',
    'winotify',
    # Image processing
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    # Utilities
    'dateutil',
    'dateutil.parser',
    'sqlite3',
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'canopy_tray', '__main__.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'pytest',
        'black',
        'flake8',
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Canopy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window (windowed mode)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(PROJECT_ROOT, 'canopy_tray', 'assets', 'canopy.ico'),
)
