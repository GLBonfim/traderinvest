@echo off
rem TraderInvest FX - PILOTO EXPERIMENTAL na conta PAPER do IBKR, em DRY-RUN (nenhuma ordem).
rem Requer o IB Gateway em modo Paper (porta 4002). Ver PILOTO-PAPER-LEIA-ME.txt.
start "" /wait "%~dp0TraderInvestFX.exe" --paper-pilot %*
