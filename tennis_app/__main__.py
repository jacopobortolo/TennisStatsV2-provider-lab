"""
Entry point for the Tennis Analytics V2 desktop application.

Run with:
    python -m tennis_app                # sync cloud, then open UI
    python -m tennis_app --no-sync      # skip cloud sync (offline mode)
"""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def _configure_webengine():
    """Set WebEngine options before QApplication is created."""
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    os.environ.setdefault(
        "QT_LOGGING_RULES",
        "qt.webenginecontext.debug=false;qt.webenginecontext.warning=false",
    )
    chromium_flags = [
        "--disable-logging",
        "--log-level=3",
        "--disable-gpu",
        "--disable-gpu-compositing",
        "--disable-gpu-rasterization",
        "--disable-webgl",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if existing_flags:
        chromium_flags.insert(0, existing_flags)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(chromium_flags)
    try:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.setDesktopSettingsAware(False)
    except Exception as exc:
        logger.debug("Could not set WebEngine application attributes: %s", exc)


def _ensure_db_initialized():
    """Create local DB schema if it doesn't exist yet (first run on a new PC)."""
    try:
        from .core.database import TennisDatabase
        db = TennisDatabase()
        db.conn.close()
        logger.debug("Local DB schema ready: %s", db.db_path)
    except Exception as exc:
        logger.warning("Could not pre-initialize local DB: %s", exc)


def _run_cloud_sync(splash, app):
    """Try to merge cloud data into local DB. Never raises."""
    try:
        from cloud.sync import sync_cloud_to_local
    except Exception as exc:
        logger.info("Cloud sync module unavailable: %s", exc)
        return

    def _on_progress(table, n):
        if splash is not None:
            splash.showMessage(
                f"Sincronizzazione cloud...\n{table}: {n} righe",
                alignment=0x0004 | 0x0080,  # Qt.AlignBottom | Qt.AlignHCenter
                color=0xFFFFFF,
            )
            app.processEvents()

    try:
        if splash is not None:
            splash.showMessage(
                "Connessione a Turso...",
                alignment=0x0004 | 0x0080,
                color=0xFFFFFF,
            )
            app.processEvents()
        result = sync_cloud_to_local(progress_callback=_on_progress)
        elapsed = result.pop("_elapsed_seconds", 0.0)
        total = sum(v for v in result.values() if isinstance(v, int))
        logger.info("Cloud sync OK: %d total rows in %.1fs", total, elapsed)
    except Exception:
        logger.exception("Cloud sync failed; opening app with stale data")


def _warm_up_webengine(app):
    """Initialize QWebEngine and Plotly before the first chart is shown."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from .ui.charts import get_chart_base_url, spider_chart

        view = QWebEngineView()
        view.setAttribute(Qt.WA_DontShowOnScreen, True)
        view.resize(1, 1)
        html = spider_chart(["A", "B", "C"], [10, 20, 30], height=120)
        view.setHtml(html, get_chart_base_url())
        app.processEvents()
        logger.debug("QWebEngine warm-up started")
        return view
    except Exception as exc:
        logger.debug("Could not warm up QWebEngine: %s", exc)
        return None


def main():
    _configure_webengine()

    from PySide6.QtWidgets import QApplication, QSplashScreen
    from PySide6.QtGui import QPixmap, QColor
    from .ui.main_window import MainWindow
    from .ui.theme import build_stylesheet, enable_dark_title_bar, load_fonts

    no_sync = "--no-sync" in sys.argv
    if no_sync:
        sys.argv.remove("--no-sync")

    app = QApplication(sys.argv)
    app.setApplicationName("Tennis Analytics")
    app.setStyle("Fusion")
    load_fonts()
    app.setStyleSheet(build_stylesheet())
    app._webengine_warmup = _warm_up_webengine(app)

    splash = None
    if not no_sync:
        _ensure_db_initialized()
        pix = QPixmap(520, 220)
        pix.fill(QColor("#101218"))
        splash = QSplashScreen(pix)
        splash.show()
        app.processEvents()
        _run_cloud_sync(splash, app)

    window = MainWindow()
    window.show()
    if splash is not None:
        splash.finish(window)

    if sys.platform == "win32":
        enable_dark_title_bar(window)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
