# -*- mode: python ; coding: utf-8 -*-
# Build with: pyinstaller packaging/chatgrab.spec --noconfirm --clean
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(os.path.abspath(SPECPATH))

datas = [
    (os.path.join(ROOT, "resources", "icon.png"), "resources"),
    (os.path.join(ROOT, "resources", "icons"), os.path.join("resources", "icons")),
    (os.path.join(ROOT, "presets"), "presets"),
]
binaries = []
hiddenimports = ["telethon.tl.alltlobjects"]

for pkg in ("telethon",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

ICON = os.path.join(ROOT, "resources", "icon.ico")

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ChatGrab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
