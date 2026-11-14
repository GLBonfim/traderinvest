; Inno Setup 6 script for TraderInvest FX (per-user install, no admin rights needed).
; Build the app first (packaging/build.py), then: ISCC packaging\installer.iss
; Optional compilation is available through packaging/build.py --installer.
; Sign the resulting setup .exe before distributing it (see docs/commercial-readiness.md).

#define AppName "TraderInvest FX"
#define VersionSource FileOpen(AddBackslash(SourcePath) + "..\app\desktop\__init__.py")
#define VersionLine
#sub ReadVersionLine
  #expr VersionLine = FileRead(VersionSource)
  #if Pos("APP_VERSION = ", VersionLine) == 1
    #define public AppVersion Trim(StringChange(Copy(VersionLine, Pos("=", VersionLine) + 1, Len(VersionLine)), '"', ""))
  #endif
#endsub
#for {0; VersionSource && !FileEof(VersionSource); 0} ReadVersionLine
#if VersionSource
  #expr FileClose(VersionSource)
#endif
#ifndef AppVersion
  #error APP_VERSION was not found in app/desktop/__init__.py
#endif
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
InfoBeforeFile=RISK-NOTICE-PT.txt

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
