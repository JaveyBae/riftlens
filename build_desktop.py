"""Build the Windows desktop app with PyInstaller.

    .venv\Scripts\python build_desktop.py            # -> dist\RiftLens\RiftLens.exe
    .venv\Scripts\python build_desktop.py --admin    # ask for administrator on launch (needed for the 国服 client)
    .venv\Scripts\python build_desktop.py --installer  # also build release\RiftLens-Setup-<version>.exe (needs Inno Setup 6)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller.__main__

import make_icon

VERSION = "1.0.0"

args = [
    "desktop.py", "--name", "RiftLens", "--noconfirm", "--windowed", "--clean",
    "--add-data", "web/static;web/static",
    "--collect-submodules", "uvicorn", "--collect-submodules", "mcp.server", "--collect-submodules", "mcp.shared",
    "--collect-data", "mcp", "--exclude-module", "mcp.cli",
    "--collect-submodules", "webview", "--hidden-import", "server",
    "--icon", str(make_icon.main()),
]
if "--admin" in sys.argv:
    args.append("--uac-admin")
if "--dist" in sys.argv:  # build elsewhere, e.g. while dist\RiftLens is open and its files are locked
    args += ["--distpath", sys.argv[sys.argv.index("--dist") + 1]]
PyInstaller.__main__.run(args)

if "--installer" in sys.argv:
    iscc = shutil.which("ISCC") or next((str(p) for p in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Inno Setup 6/ISCC.exe") if p.exists()), None)
    if not iscc:
        sys.exit("没有找到 Inno Setup 6（ISCC.exe）。安装：winget install -e --id JRSoftware.InnoSetup")
    subprocess.run([iscc, f"/DAppVersion={VERSION}", "installer/riftlens.iss"], check=True)
    print(f"安装包：release/RiftLens-Setup-{VERSION}.exe")
