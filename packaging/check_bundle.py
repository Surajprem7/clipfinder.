"""Checks that the packaged program carries everything it needs, so nothing has to be installed separately.

    python packaging/check_bundle.py <folder> [--system-dir DIR]

<folder> is dist/ClipFinder (the build) or the folder the installer put the program in.
1. Required files: the program, its Python runtime, numpy, the file dialog (Tcl/Tk), the Visual C++ runtime,
   the web page, and ffmpeg / ffprobe / yt-dlp in the tools folder.
2. Every .exe / .dll / .pyd in the folder is opened and the DLLs it imports are listed. Each one must be
   (a) inside the folder, or (b) a Windows system DLL (found in System32 / an "api-ms-win-*" alias).
   The Visual C++ runtime DLLs (vcruntime140, msvcp140, ...) are NOT accepted from the system: they must be inside.
Limit: DLLs that a program loads by name while it runs (LoadLibrary) are not visible to this check.
Exit code 0 = nothing missing, 1 = something missing, 2 = could not check.
"""
from __future__ import annotations

import glob
import os
import re
import sys

REQUIRED = {
    "the program": ["ClipFinder.exe"],
    "licence note": ["THIRD_PARTY.txt"],
    "Python runtime": ["**/python3*.dll"],
    "Visual C++ runtime": ["**/vcruntime140.dll"],
    "numpy": ["**/_multiarray_umath*.pyd"],
    "file dialog (tkinter)": ["**/_tkinter*.pyd", "**/tcl86t.dll", "**/tk86t.dll", "**/init.tcl", "**/tk.tcl"],
    "web page": ["**/web/index.html", "**/web/app.js", "**/web/app.css"],
    "ffmpeg": ["tools/ffmpeg.exe"],
    "ffprobe": ["tools/ffprobe.exe"],
    "yt-dlp": ["tools/yt-dlp.exe"],
}
VC_RUNTIME = re.compile(r"^(vcruntime|msvcp|concrt|vcomp|mfc|vccorlib|msvcr)\d", re.IGNORECASE)
API_SETS = ("api-ms-win-", "ext-ms-win-")


def classify(name: str, bundled: set, in_system) -> str:
    """'bundled', 'system' or 'missing' for one imported DLL name. in_system(name) -> bool."""
    n = name.lower()
    if n in bundled:
        return "bundled"
    if n.startswith(API_SETS):
        return "system"
    if VC_RUNTIME.match(n):
        return "missing"                                     # a redistributable: it has to travel with the program
    return "system" if in_system(n) else "missing"


def missing_files(folder: str) -> list:
    problems = []
    for what, patterns in REQUIRED.items():
        for pattern in patterns:
            if not glob.glob(os.path.join(folder, pattern), recursive=True):
                problems.append(f"{what}: nothing matches {pattern}")
    return problems


def imports_of(path: str) -> set:
    import pefile                                            # only needed for this part
    pe = pefile.PE(path, fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        names = set()
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, attr, []):
                names.add(entry.dll.decode("ascii", "replace").lower())
        return names
    finally:
        pe.close()


def check_imports(folder: str, system_dir: str):
    binaries = []
    for dirpath, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith((".exe", ".dll", ".pyd")):
                binaries.append(os.path.join(dirpath, f))
    bundled = {os.path.basename(p).lower() for p in binaries}
    have_system = os.path.isdir(system_dir)
    in_system = (lambda n: os.path.isfile(os.path.join(system_dir, n))) if have_system else (lambda n: False)
    missing, system_used = {}, set()
    for path in sorted(binaries):
        try:
            names = imports_of(path)
        except Exception as exc:                             # noqa: BLE001 - a file that cannot be read is reported, not hidden
            missing.setdefault("(unreadable file)", set()).add(f"{os.path.relpath(path, folder)}: {type(exc).__name__}")
            continue
        for n in names:
            kind = classify(n, bundled, in_system)
            if kind == "missing":
                missing.setdefault(n, set()).add(os.path.relpath(path, folder))
            elif kind == "system":
                system_used.add(n)
    return len(binaries), missing, system_used, have_system


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    system_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    if "--system-dir" in argv:
        system_dir = argv[argv.index("--system-dir") + 1]
        args = [a for a in args if a != system_dir]
    if len(args) != 1 or not os.path.isdir(args[0]):
        print(__doc__)
        return 2
    folder = os.path.abspath(args[0])
    problems = missing_files(folder)
    try:
        count, missing, system_used, have_system = check_imports(folder, system_dir)
    except ImportError:
        print("pefile is not installed (pip install pefile)")
        return 2
    print(f"{count} program files looked at in {folder}")
    if not have_system:
        print(f"note: system folder {system_dir} not found, so every DLL not inside the folder counts as missing")
    print("Windows system DLLs used (every Windows 10/11 has these):", ", ".join(sorted(system_used)) or "(none)")
    for dll, users in sorted(missing.items()):
        shown = ", ".join(sorted(users)[:4]) + (" ..." if len(users) > 4 else "")
        problems.append(f"needs {dll} which is neither inside the folder nor a Windows system DLL (used by: {shown})")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(" -", p)
        return 1
    print("OK: everything the program needs is inside the folder or comes with Windows.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
