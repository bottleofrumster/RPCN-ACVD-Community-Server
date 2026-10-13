"""Bundle the native console only; never touch client/ or the client ZIP."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from analysis.protocol_catalog import load_catalog  # noqa: E402 -- build-time repository tool


def main():
    resources = ROOT / "desktop/acvd_console/resources"
    (resources / "protocol-catalog.json").write_text(json.dumps(load_catalog(), indent=2))
    fonts = Path("/usr/share/fonts/truetype/dejavu")
    for name in ("DejaVuSans.ttf", "DejaVuSansMono.ttf"):
        if (fonts / name).is_file():
            shutil.copy2(fonts / name, resources / name)
    license_path = Path("/usr/share/doc/fonts-dejavu-core/copyright")
    if license_path.is_file():
        shutil.copy2(license_path, resources / "FONT-LICENSE")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        "ACVD-Control-Center",
        "--onefile",
        "--paths",
        str(ROOT / "desktop"),
        "--paths",
        str(ROOT / "src"),
        "--add-data",
        f"{resources}:acvd_console/resources",
        "--hidden-import",
        "PySide6.QtSvg",
        "--distpath",
        str(ROOT / "releases/desktop"),
        "--workpath",
        str(ROOT / "build/desktop"),
        "--specpath",
        str(ROOT / "build/desktop"),
        str(ROOT / "desktop/run.py"),
    ]
    native_library = resources / "native/libxcb-cursor.so.0"
    if native_library.is_file():
        command[3:3] = ["--add-binary", f"{native_library}:."]
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
