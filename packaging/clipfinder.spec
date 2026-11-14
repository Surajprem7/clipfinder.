# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for the packaged program:  pyinstaller packaging/clipfinder.spec
# Result: dist/ClipFinder/ClipFinder(.exe) with the web page inside. ffmpeg, ffprobe and yt-dlp are NOT inside it:
# they are copied into dist/ClipFinder/tools/ afterwards (see .github/workflows/build-windows.yml).
import os

import sys

root = os.path.abspath(os.path.join(SPECPATH, ".."))

# The Visual C++ runtime (needed by Python and numpy) must travel with the program, so that a computer without it still
# works. PyInstaller usually finds these itself; listing them here makes sure. (Windows only.)
vc_runtime = []
if sys.platform == "win32":
    for dll in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll", "concrt140.dll"):
        for base in (sys.base_prefix, os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")):
            path = os.path.join(base, dll)
            if os.path.isfile(path):
                vc_runtime.append((path, "."))
                break

a = Analysis(
    [os.path.join(root, "clipfinder_web.py")],
    pathex=[root],
    binaries=vc_runtime,
    datas=[(os.path.join(root, "web"), "web")],
    hiddenimports=["clipfinder", "clipfinder_mcp"],
    excludes=["pytest", "IPython", "matplotlib", "scipy", "pandas", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClipFinder",
    console=True,          # the small black window shows the status and stops the program when closed
    upx=False,             # UPX-packed programs are flagged more often by antivirus tools
)
coll = COLLECT(exe, a.binaries, a.datas, name="ClipFinder", upx=False)
