; LimitSwitcher's Windows installer (Inno Setup 6), built by scripts\build_windows.ps1:
;   iscc /DAppVersion=1.0.0 /DSourceDir=<build\LimitSwitcher> scripts\LimitSwitcher.iss
; Per user, no admin rights. It asks where to install, has boxes for a Start menu entry (on)
; and a desktop shortcut (off); starting at sign-in is the app's own setting (on at first run). The app's in-app updater runs it with
; /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=<this folder>: it closes the app, replaces the
; files and starts the app again. Saved accounts and settings live in %LOCALAPPDATA%\LimitSwitcher
; and are kept, also by the uninstaller.

#define AppName "LimitSwitcher"
#define AppExe "LimitSwitcher.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\LimitSwitcher"
#endif
; How the files are packed (the Release workflow's test builds try others: some antivirus engines
; judge an installer by how it's packed)
#ifndef Compress
  #define Compress "lzma2/max"
#endif
#ifndef Solid
  #define Solid "yes"
#endif

[Setup]
AppId={{6B1E0C54-3F7A-4D2B-9E8C-5A1F2D7B4C90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=doge006
AppPublisherURL=https://github.com/doge006/LimitSwitcher
AppSupportURL=https://github.com/doge006/LimitSwitcher/issues
AppUpdatesURL=https://github.com/doge006/LimitSwitcher/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=no
DisableProgramGroupPage=yes
DisableWelcomePage=yes
PrivilegesRequired=lowest
UsePreviousAppDir=yes
UsePreviousTasks=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\build
OutputBaseFilename=LimitSwitcher-Setup
SetupIconFile=..\account_switcher\static\assets\switcher.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression={#Compress}
SolidCompression={#Solid}
; Who made it and what it is, in the installer's own file properties
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
VersionInfoCompany=doge006
VersionInfoCopyright=Copyright (c) doge006, MIT License
; The app is closed with --quit first (it puts Codex's and Claude Code's settings back); this
; only catches a copy that did not answer.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "startmenu"; Description: "Add to the Start menu"
Name: "desktopicon"; Description: "Add a desktop shortcut"; Flags: unchecked

[InstallDelete]
; An update replaces the app and its Python whole, so no file of an older version is left behind.
Type: filesandordirs; Name: "{app}\account_switcher"
Type: filesandordirs; Name: "{app}\runtime"
; From before the rename to LimitSwitcher (1.0.0 test builds were LimitSwitch).
Type: files; Name: "{app}\LimitSwitch.exe"
Type: files; Name: "{app}\LimitSwitch.pyw"
Type: files; Name: "{userprograms}\LimitSwitch.lnk"
Type: files; Name: "{userdesktop}\LimitSwitch.lnk"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Claude Code and Codex usage limits and account switching"; \
    AppUserModelID: "LimitSwitcher.App"; Tasks: startmenu
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--show"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\LimitSwitcher.pyw"" --quit"; WorkingDir: "{app}"; \
    Flags: runhidden waituntilterminated; RunOnceId: "QuitApp"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.runtime"
Type: filesandordirs; Name: "{app}\account_switcher"
Type: filesandordirs; Name: "{app}\runtime"

[Code]
procedure QuitRunningCopy();
var
  Python, Script: String;
  Code: Integer;
begin
  Python := ExpandConstant('{app}\runtime\pythonw.exe');
  Script := ExpandConstant('{app}\LimitSwitcher.pyw');
  if not FileExists(Script) then
    Script := ExpandConstant('{app}\LimitSwitch.pyw'); { a copy from before the rename }
  if FileExists(Python) and FileExists(Script) then
  begin
    Exec(Python, AddQuotes(Script) + ' --quit', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(500); { its process ends just after it says it has quit }
  end;
end;

{ Claude Code runs the app's status line script and Auto resume hook with this copy's own
  runtime\python.exe; a hook waiting for a limit to reset can run for hours and holds the
  runtime's files. End those: only processes started from this copy's runtime folder. }
procedure StopScriptsFromThisCopy();
var
  Runtime, Command: String;
  Code: Integer;
begin
  Runtime := ExpandConstant('{app}\runtime\');
  StringChangeEx(Runtime, '''', '''''', True); { a quote inside PowerShell's '...' }
  Command := '-NoProfile -NonInteractive -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -and $_.Path.StartsWith(''' + Runtime + ''', [StringComparison]::OrdinalIgnoreCase) } | ' +
    'Stop-Process -Force -ErrorAction SilentlyContinue"';
  Exec('powershell.exe', Command, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  QuitRunningCopy();
  StopScriptsFromThisCopy();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopScriptsFromThisCopy();
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'LimitSwitch');
  end;
end;
