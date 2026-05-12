"""
Cloud-mode entry point.

Same UI as ``python -m tennis_app`` but the database is a Turso embedded
replica instead of a plain local SQLite file.

Run with:
    python -m cloud.run_app
"""

from __future__ import annotations

import logging
import sys


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from PySide6.QtWidgets import QApplication
    from tennis_app.ui.main_window import MainWindow
    from tennis_app.ui.theme import build_stylesheet, enable_dark_title_bar, load_fonts
    from .db import SnapshotTennisDatabase

    app = QApplication(sys.argv)
    app.setApplicationName("Tennis Analytics — Cloud")
    app.setStyle("Fusion")
    load_fonts()
    app.setStyleSheet(build_stylesheet())

    # Patch MainWindow so it uses our snapshot DB instead of TennisDatabase().
    import tennis_app.ui.main_window as mw
    mw.TennisDatabase = lambda: SnapshotTennisDatabase(refresh_on_open=True)

    window = MainWindow()
    window.setWindowTitle(window.windowTitle() + " [Cloud]")
    window.show()

    if sys.platform == "win32":
        enable_dark_title_bar(window)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
