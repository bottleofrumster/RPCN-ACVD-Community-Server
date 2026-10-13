"""Application entry point and deterministic offscreen visual smoke test."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .core import RESOURCES
from .style import STYLE
from .window import Console


def main():
    parser = argparse.ArgumentParser(description="Native ACVD server control center")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--smoke-image", type=Path, help="Save an offscreen visual QA screenshot")
    parser.add_argument("--smoke-page", type=int, default=0)
    args = parser.parse_args()
    cursor_library = RESOURCES / "native/libxcb-cursor.so.0"
    if cursor_library.is_file():
        ctypes.CDLL(str(cursor_library), mode=ctypes.RTLD_GLOBAL)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ACVD Control Center")
    app.setOrganizationName("ACVD Community")
    settings = QSettings()
    for font in RESOURCES.glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("DejaVu Sans", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setWindowIcon(QIcon(str(RESOURCES / "console.svg")))
    root = args.root or Path(os.environ.get("ACVD_SERVER_ROOT", settings.value("server_root", "")))
    if not (root / "src/acvd_server/server.py").is_file():
        if args.smoke_image:
            raise SystemExit("A valid --root is required for visual QA")
        selected = QFileDialog.getExistingDirectory(None, "Choose ACVD server repository")
        if not selected:
            return 1
        root = Path(selected)
    root = root.resolve()
    if not (root / "src/acvd_server/server.py").is_file():
        QMessageBox.critical(
            None,
            "Not an ACVD server root",
            "Choose the folder containing src/acvd_server/server.py.",
        )
        return 2
    if not args.smoke_image:
        settings.setValue("server_root", str(root))
    window = Console(root)
    window.show()
    if args.smoke_image:

        def capture():
            window.navigation.setCurrentRow(max(0, min(6, args.smoke_page)))
            args.smoke_image.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.smoke_image)):
                raise RuntimeError("Screenshot save failed")
            window.close()

        QTimer.singleShot(3500, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
