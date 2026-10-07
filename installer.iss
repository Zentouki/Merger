; Optional: makes MergerSetup.exe from the folder built by Build-Desktop.bat.
; Install Inno Setup (jrsoftware.org), open this file and press Compile.
[Setup]
AppName=Merger
AppVersion=1.0
DefaultDirName={autopf}\Merger
DefaultGroupName=Merger
OutputDir=installer
OutputBaseFilename=MergerSetup
SetupIconFile=favicon.ico
UninstallDisplayIcon={app}\Merger.exe
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"

[Files]
Source: "dist\Merger\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Merger"; Filename: "{app}\Merger.exe"
Name: "{autodesktop}\Merger"; Filename: "{app}\Merger.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Merger.exe"; Description: "Start Merger"; Flags: nowait postinstall skipifsilent
