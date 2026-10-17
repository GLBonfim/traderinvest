"""PyInstaller entry point: one executable for the desktop UI and (with --engine) the engine."""

from app.desktop.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
