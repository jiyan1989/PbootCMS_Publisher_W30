# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from app_meta import EXE_BASENAME


build_metadata = Path('.build_runtime') / 'build_metadata.json'
if not build_metadata.is_file():
    raise RuntimeError(
        'Run: python build_support.py metadata --output '
        '.build_runtime\\build_metadata.json')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('webui', 'webui'), (str(build_metadata), '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The bundled runtime does not import setuptools.  Excluding it avoids
    # PyInstaller's optional SetuptoolsInfo probe hanging on this Windows
    # build environment after pip's metadata upgrade.
    excludes=['setuptools', 'pkg_resources'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_BASENAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
