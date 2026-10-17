"""Entry point of the desktop application (and of the frozen executable).

`TraderInvestFX.exe` starts the UI; `TraderInvestFX.exe --engine ...` runs the engine process
(the UI launches itself that way), so one executable ships both.
`TraderInvestFX.exe --smoke-test [screenshot.png]` checks a build without user interaction.
"""

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--engine"]:
        from app.engine.__main__ import main as engine_main

        return engine_main(args[1:])

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from app.desktop import APP_ID, APP_NAME, APP_VERSION, ORG_NAME
    from app.desktop.dialogs import disclaimer
    from app.desktop.icon import app_icon
    from app.desktop.main_window import MainWindow
    from app.desktop.theme import stylesheet

    if sys.platform == "win32":  # own taskbar identity (icon grouping) instead of python.exe
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{ORG_NAME}.{APP_ID}")

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet())
    if args[:1] == ["--smoke-test"]:
        return smoke_test(app, Path(args[1]) if len(args) > 1 else None)
    settings = QSettings(ORG_NAME, APP_ID)
    if settings.value("disclaimer_accepted") != APP_VERSION:
        if not disclaimer(None):
            return 0
        settings.setValue("disclaimer_accepted", APP_VERSION)
    win = MainWindow()
    win.show()
    return app.exec()


def smoke_test(app: "QApplication", screenshot: Path | None, seconds: int = 15) -> int:
    """Diagnostics for builds: opens the window (no disclaimer, nothing persisted in the
    settings), waits for engine data, optionally saves a screenshot, stops the engine and
    exits 0 when the UI received snapshots, 2 otherwise."""
    from PySide6.QtCore import QTimer

    from app.desktop.main_window import MainWindow

    win = MainWindow(remember_geometry=False)
    win.show()
    result = {"code": 2}

    def finish() -> None:
        if screenshot is not None:
            win.grab().save(str(screenshot))
        result["code"] = 0 if win.snap and win.client.online else 2
        win._quitting = True
        win.close()
        app.quit()

    QTimer.singleShot(seconds * 1000, finish)
    app.exec()
    return result["code"]


if __name__ == "__main__":
    raise SystemExit(main())
