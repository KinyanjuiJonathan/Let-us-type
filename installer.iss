; Inno Setup script: packages the PyInstaller build into AutoTyperSetup.exe
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B0E1F2C-7C1B-4E0B-9D3A-5A1E6D0C4B21}
AppName=Auto Typer
AppVersion={#AppVersion}
AppPublisher=Let us type
DefaultDirName={autopf}\Auto Typer
DefaultGroupName=Auto Typer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=AutoTyperSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\AutoTyper.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\AutoTyper\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Auto Typer"; Filename: "{app}\AutoTyper.exe"
Name: "{autodesktop}\Auto Typer"; Filename: "{app}\AutoTyper.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AutoTyper.exe"; Description: "Start Auto Typer"; Flags: nowait postinstall skipifsilent
