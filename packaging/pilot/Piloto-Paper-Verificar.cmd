@echo off
rem Verifica o registo de candidatos incluido (sem ligacao ao IBKR, nenhuma ordem).
start "" /wait "%~dp0TraderInvestFX.exe" --paper-pilot --self-check
