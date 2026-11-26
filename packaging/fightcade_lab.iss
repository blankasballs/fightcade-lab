; Fightcade Lab Windows Companion — Inno Setup Installer Script (Zero UAC / Per-User)
; SignPath Foundation OSS Code Signing Ready
; NEVER bundles fcadefbneo_fast.exe, ffmpeg.exe, Grouflon 3rd_training_lua, or ROMs.

#define AppName "Fightcade Lab Companion"
#define AppVersion "0.1.0"
#define AppPublisher "Fightcade Lab Contributors"
#define AppURL "https://fightcadelab.com"
#define AppExeName "FightcadeLabCompanion.exe"

[Setup]
AppId={{7B2E7B1C-4F92-48C6-A211-F8A4D2E3C901}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL=https://github.com/blankasballs/fightcade-lab/issues
DefaultDirName={localappdata}\FightcadeLab
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=FightcadeLabSetup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "startup"; Description: "Start Fightcade Lab Companion quietly with Windows"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\companion\_companion_main.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--open"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--open"; Tasks: desktopicon

[Registry]
; Register fightcadelab:// custom URI scheme under HKCU (zero admin/UAC)
Root: HKCU; Subkey: "Software\Classes\fightcadelab"; ValueType: string; ValueName: ""; ValueData: "URL:Fightcade Lab Protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\fightcadelab"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\fightcadelab\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" --uri ""%1"""
; Optional Start with Windows under HKCU
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "FightcadeLabCompanion"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--open"; Description: "Launch Fightcade Lab Companion now"; Flags: nowait postinstall skipifsilent
