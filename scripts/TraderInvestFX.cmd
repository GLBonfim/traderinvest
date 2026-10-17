@echo off
rem Starts the TraderInvest FX desktop app from source (double-click). No console window stays
rem open: pythonw runs the UI, which starts its own engine process.
cd /d "%~dp0.."
start "" uv run pythonw -m app.desktop
