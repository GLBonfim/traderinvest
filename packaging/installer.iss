; Inno Setup 6 script for TraderInvest FX (per-user install, no admin rights needed).
; Build the app first (packaging/build.py), then: ISCC packaging\installer.iss
; Not compiled in the development environment (Inno Setup is not installed there).
; Sign the resulting setup .exe before distributing it (see docs/commercial-readiness.md).

#define AppName "TraderInvest FX"
#define AppVersion "0.1.0"
#define AppExe "TraderInvestFX.exe"

[Setup]
AppId={{6B0F3B52-3C1E-4C2B-9E55-7A1F0C2D9F11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=TraderInvest
DefaultDirName={localappdata}\Programs\TraderInvestFX
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=TraderInvestFX-{#AppVersion}-setup
SetupIconFile=..\build\packaging\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=LICENSE-COMMERCIAL-DRAFT.md

[Files]
Source: "..\dist\TraderInvestFX\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Criar atalho no ambiente de trabalho"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir {#AppName}"; Flags: nowait postinstall skipifsilent

; User data (%LOCALAPPDATA%\TraderInvestFX: configuration, journal, logs) is kept on uninstall
; on purpose: it holds the audit journal.
