# PyInstaller spec — TraderInvest FX (Windows, one-folder build). Run via packaging/build.py.
# ruff: noqa
import os
from pathlib import Path

ROOT = Path(SPECPATH).parent
BUILD = ROOT / "build" / "packaging"

EXCLUDES = [
    # research / web stack not used by the desktop product (sqlalchemy stays: app.core.config)
    "streamlit", "plotly", "sklearn", "scipy", "fastapi", "uvicorn", "starlette",
    "alembic", "psycopg", "yfinance", "exchange_calendars", "matplotlib", "IPython", "pytest",
    "tkinter", "pyarrow", "altair", "pydeck", "tornado", "watchdog", "httpx",
    # Qt modules the app does not use
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtSvg", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtXml", "PySide6.QtDBus", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtConcurrent", "PySide6.QtHelp", "PySide6.QtDesigner",
    "PySide6.QtUiTools", "PySide6.QtPrintSupport",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "packaging" / "THIRD_PARTY_NOTICES.md"), "."),
           (str(ROOT / "packaging" / "LICENSE-COMMERCIAL-DRAFT.md"), ".")],
    hiddenimports=["app.engine.__main__", "app.engine.service", "app.engine.protocol",
                   "zoneinfo", "tzdata"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TraderInvestFX",
    console=False,
    icon=str(BUILD / "icon.ico"),
    version=str(BUILD / "version_info.txt"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="TraderInvestFX", upx=False)
