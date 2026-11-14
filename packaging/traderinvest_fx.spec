# PyInstaller spec — TraderInvest FX (Windows, one-folder build). Run via packaging/build.py.
# ruff: noqa
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
BUILD = ROOT / "build" / "packaging"
# training run whose selection the experimental paper pilot ships (set by build.py)
PILOT_RUN = Path(os.environ.get("TRADERINVEST_PILOT_RUN", ROOT / "data/fx/training/run-20261018"))
PILOT_FILES = [(str(PILOT_RUN / name), "pilot") for name in ("selected.json", "report.md",
               "protocol.json", "source-evidence.json") if (PILOT_RUN / name).is_file()]
if not (PILOT_RUN / "selected.json").is_file():
    raise SystemExit(f"pilot selection missing: {PILOT_RUN / 'selected.json'}")

EXCLUDES = [
    # research / web stack not used by the desktop product (sqlalchemy stays: app.core.config)
    "streamlit", "plotly", "sklearn", "scipy", "fastapi", "uvicorn", "starlette",
    "alembic", "psycopg", "yfinance", "exchange_calendars", "matplotlib", "IPython", "pytest",
    "tkinter", "pyarrow", "altair", "pydeck", "tornado", "watchdog", "httpx",
    # dev/optional modules pulled in by hooks but never used at runtime
    "mypy", "setuptools", "pkg_resources", "lxml", "yaml", "markupsafe", "jinja2",
    "charset_normalizer", "PIL", "pytz", "openpyxl", "xlrd", "tables", "numexpr",
    "bottleneck", "fsspec", "s3fs",
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
           (str(ROOT / "packaging" / "LICENSE-COMMERCIAL-DRAFT.md"), "."),
           (str(ROOT / "packaging" / "RISK-NOTICE-PT.txt"), "."),
           (str(ROOT / "docs" / "install.md"), "docs"),
           (str(ROOT / "docs" / "fx-platform.md"), "docs"),
           (str(ROOT / "docs" / "fx-agent-validation.md"), "docs"),
           (str(ROOT / "docs" / "fx-pattern-research.md"), "docs"),
           (str(ROOT / "docs" / "candlestick-engine.md"), "docs"),
           (str(ROOT / "packaging" / "pilot" / "PILOTO-PAPER-LEIA-ME.txt"), "."),
           *PILOT_FILES],
    hiddenimports=["app.engine.__main__", "app.engine.service", "app.engine.protocol",
                   "zoneinfo", "tzdata",
                   # experimental paper pilot (inference is numpy/pandas + JSON numbers)
                   "app.fx.ibkr.pilot", "app.fx.ibkr.execution", "app.fx.agents.trained",
                   "eventkit", "nest_asyncio", *collect_submodules("ib_async")],
    excludes=EXCLUDES,
    noarchive=False,
)
# This Windows Qt build imports the OS ICU ABI. An unrelated Poppler directory on
# PATH can make analysis collect ICU 78 instead, whose versioned exports cannot
# satisfy QtCore. Keep the Windows system dependency out of the portable bundle.
if os.name == "nt":
    a.binaries = [entry for entry in a.binaries
                  if Path(entry[0]).name.lower() != "icuuc.dll"]
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
