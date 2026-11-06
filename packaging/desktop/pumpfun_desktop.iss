; PumpFun PAPER bot: per-user installer script for Inno Setup 6 (desktop spec 8). Not shipped.
; Prepared only: Inno Setup is not installed on the build PC; compiling is a manual step (stage 5).
;   1) python -I packaging\desktop\build_portable.py --out C:\pfbuild     (builds and verifies the ZIP)
;   2) extract that ZIP into C:\pfbuild  ->  C:\pfbuild\pumpfun_bot_v0.7.103.0_desktop
;   3) ISCC.exe /DStageDir=C:\pfbuild\pumpfun_bot_v0.7.103.0_desktop packaging\desktop\pumpfun_desktop.iss
; Current user only: no elevation, no service, no autostart, no registry writes except the uninstall
; entry Inno keeps under HKCU. Update in place: same folder; app\data, app\.env and results stay.
; Before every install/uninstall the bot of that folder must be stopped (launcher lock, monitor lock, the
; database file, engine host lock, engine_state.json: an orphaned main.py/monitor/dashboard is seen too), and
; the window must be closed (AppMutex: the window holds that mutex while it runs).
; Old app\**\*.py (except app\data) and desktop\**\*.py are removed only when the folder is an existing
; installation of this product (BUILD_MANIFEST.json v0.7.* listing app/release_guard.py, and
; app\release_guard.py present, and no .git: a developer's git working copy has the same files but is never
; an installation), so the release check (EXTRA_MODULE) sees exactly the new version. Any other folder that
; already has app\ or desktop\, and any folder with .git (directory or file), is refused: nothing is deleted there.
; 01_INSTALL.bat and 16_DESKTOP.bat --prepare run after the copy with stdin from NUL: their only interactive
; command is the final "pause", which then returns at once; a non-zero exit code is reported in a message box.
; This file is UTF-8 with BOM (Russian texts).

#define AppName "PumpFun PAPER"
#define AppVersion "0.7.101.6"
#define AppGuid "6F1C2A4E-5B3D-4E8A-9C71-2D0F4B6A8E15"
#ifndef StageDir
  #define StageDir "..\..\dist\pumpfun_bot_v0.7.103.0_desktop"
#endif

[Setup]
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher=PumpFun PAPER
DefaultDirName={localappdata}\Programs\PumpfunPaperBot
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsePreviousAppDir=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RestartApplications=no
AppMutex=PumpfunPaperDesktopApp
OutputDir=..\..\dist
OutputBaseFilename=pumpfun_bot_v{#AppVersion}_desktop_setup
SetupIconFile={#StageDir}\desktop\assets\app_idle.ico
UninstallDisplayIcon={app}\desktop\assets\app_idle.ico
UninstallDisplayName={#AppName} v{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык «{#AppName}» на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
; The staged folder is the unpacked portable ZIP: BUILD_MANIFEST.json files + README.md + requirements.txt.
Source: "{#StageDir}\*"; DestDir: "{app}"; Excludes: "\app\data,\app\data\*,\app\.env,\results,\results\*,\.venv,\.venv\*,\.venv-ui,\.venv-ui\*,\packaging,\packaging\*,\dist,\dist\*,__pycache__,*.pyc,.pytest_cache"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\.venv-ui\Scripts\pythonw.exe"; Parameters: "-I ""{app}\desktop\app_main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\desktop\assets\app_idle.ico"; AppUserModelID: "PumpfunPaper.Desktop"; Comment: "PAPER-бот: окно терминала и значок в трее"
Name: "{group}\Папка бота"; Filename: "{app}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\.venv-ui\Scripts\pythonw.exe"; Parameters: "-I ""{app}\desktop\app_main.py"""; WorkingDir: "{app}"; IconFilename: "{app}\desktop\assets\app_idle.ico"; AppUserModelID: "PumpfunPaper.Desktop"; Tasks: desktopicon

[Run]
; .venv (01_INSTALL.bat) and .venv-ui (16_DESKTOP.bat --prepare) are prepared in [Code] CurStepChanged(ssPostInstall),
; which sees their exit codes. The window is offered only when both succeeded.
Filename: "{app}\.venv-ui\Scripts\pythonw.exe"; Parameters: "-I ""{app}\desktop\app_main.py"""; WorkingDir: "{app}"; Description: "Открыть {#AppName}"; Flags: postinstall nowait skipifsilent unchecked; Check: WindowReady

[UninstallDelete]
; Only environments and caches created after install. app\data, app\.env and results are never listed.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\.venv-ui"
Type: filesandordirs; Name: "{app}\.pytest_cache"
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: filesandordirs; Name: "{app}\app\tests\__pycache__"
Type: filesandordirs; Name: "{app}\desktop\__pycache__"
Type: filesandordirs; Name: "{app}\desktop\tests\__pycache__"

[Code]
const
  UNINSTALL_KEY = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\';
  WEBVIEW2_CLIENT = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/';
  PROCESS_QUERY_LIMITED_INFORMATION = $1000;
  STILL_ACTIVE = 259;
  FILE_ATTRIBUTE_REPARSE_POINT = $400;
  FOREIGN_TEXT = 'Папка уже содержит другие файлы. Выберите пустую папку или прежнюю папку установки.';

var
  EnvReady: Boolean;

{ Setup is a 32-bit program: process handles fit in Integer. }
function OpenProcess(dwDesiredAccess: Cardinal; bInheritHandle: Integer; dwProcessId: Cardinal): Integer;
  external 'OpenProcess@kernel32.dll stdcall';
function GetExitCodeProcess(hProcess: Integer; var lpExitCode: Cardinal): Integer;
  external 'GetExitCodeProcess@kernel32.dll stdcall';
function CloseHandle(hObject: Integer): Integer;
  external 'CloseHandle@kernel32.dll stdcall';

function ProcessAlive(Pid: Cardinal): Boolean;
var
  H: Integer;
  Code: Cardinal;
begin
  Result := False;
  if Pid = 0 then Exit;
  H := OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, Pid);
  if H = 0 then Exit;
  try
    Result := (GetExitCodeProcess(H, Code) <> 0) and (Code = STILL_ACTIVE);
  finally
    CloseHandle(H);
  end;
end;

{ True when another process has the file open (the launcher and the engine host keep their lock files
  open while they run; an exclusive open then fails). }
function FileHeld(const FileName: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := False;
  if not FileExists(FileName) then Exit;
  try
    Stream := TFileStream.Create(FileName, fmOpenRead or fmShareExclusive);
    Stream.Free;
  except
    Result := True;
  end;
end;

{ engine_state.json is written by json.dumps: "key": "value" / "key": 123. }
function JsonString(const Text, Key: String): String;
var
  P: Integer;
  Rest: String;
begin
  Result := '';
  P := Pos('"' + Key + '": "', Text);
  if P = 0 then Exit;
  Rest := Copy(Text, P + Length(Key) + 5, 64);
  P := Pos('"', Rest);
  if P > 0 then Result := Copy(Rest, 1, P - 1);
end;

function JsonNumber(const Text, Key: String): Cardinal;
var
  P, I: Integer;
  Digits: String;
begin
  Result := 0;
  P := Pos('"' + Key + '": ', Text);
  if P = 0 then Exit;
  I := P + Length(Key) + 4;
  Digits := '';
  while (I <= Length(Text)) and (Text[I] >= '0') and (Text[I] <= '9') do
  begin
    Digits := Digits + Text[I];
    I := I + 1;
  end;
  Result := StrToIntDef(Digits, 0);
end;

{ Empty when the bot of AppDir is stopped, else what shows that it runs. Files the bot keeps open for a whole run
  (app\launcher.py and app\monitor_service.py names): the launcher lock (launcher.start, 05/08 results), the
  monitor lock (monitor_service.singleton, held by the monitor process even when its launcher is gone) and the
  database (app\database.py keeps one connection per process open: main.py, the dashboard, the monitor). }
function BotRunning(const AppDir: String): String;
var
  Raw: AnsiString;
  Text, Phase: String;
begin
  Result := '';
  if FileHeld(AppDir + '\app\data\clean_0762_launcher.lock') then
  begin
    Result := 'app\data\clean_0762_launcher.lock';
    Exit;
  end;
  if FileHeld(AppDir + '\app\data\clean_0762_monitor.lock') then
  begin
    Result := 'app\data\clean_0762_monitor.lock';
    Exit;
  end;
  if FileHeld(AppDir + '\app\data\clean_0762.db') then
  begin
    Result := 'app\data\clean_0762.db';
    Exit;
  end;
  if FileHeld(AppDir + '\app\data\desktop\engine_host.lock') then
  begin
    Result := 'app\data\desktop\engine_host.lock';
    Exit;
  end;
  if LoadStringFromFile(AppDir + '\app\data\desktop\engine_state.json', Raw) then
  begin
    Text := String(Raw);
    Phase := JsonString(Text, 'phase');
    if ((Phase = 'PREFLIGHT') or (Phase = 'STARTING') or (Phase = 'RUNNING') or (Phase = 'STOP_REQUESTED'))
      and ProcessAlive(JsonNumber(Text, 'pid')) then
      Result := 'app\data\desktop\engine_state.json: ' + Phase;
  end;
end;

function RunningText(const AppDir, Why: String): String;
begin
  Result := 'PAPER-бот в папке ' + AppDir + ' сейчас работает (' + Why + ').' + #13#10#13#10 +
    'Остановите его («Бот → Остановить…» в окне или Ctrl+C в окне 03_START), дождитесь итога, ' +
    'сохраните данные через 08_PRESERVE_DATA и запустите установку снова.';
end;

function PreviousAppDir(): String;
begin
  if not RegQueryStringValue(HKCU, UNINSTALL_KEY + '{' + '{#AppGuid}' + '}_is1', 'Inno Setup: App Path', Result) then
    Result := '';
end;

function PythonFound(): Boolean;
begin
  Result := RegKeyExists(HKCU, 'Software\Python\PythonCore\3.14') or RegKeyExists(HKLM, 'SOFTWARE\Python\PythonCore\3.14');
end;

function WebView2Found(): Boolean;
var
  V: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_CLIENT, 'pv', V) then
    Result := (V <> '') and (V <> '0.0.0.0');
  if not Result and RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_CLIENT, 'pv', V) then
    Result := (V <> '') and (V <> '0.0.0.0');
end;

{ A git working copy (.git directory, or .git file of a worktree/submodule): a developer checkout, never an
  installation, even though it has the same BUILD_MANIFEST.json and app\release_guard.py. }
function GitCheckout(const AppDir: String): Boolean;
begin
  Result := DirExists(AppDir + '\.git') or FileExists(AppDir + '\.git');
end;

{ True only for an existing installation of this product: no .git, BUILD_MANIFEST.json written by
  json.dumps(indent=2) with a v0.7.* version and a files list that names app/release_guard.py and app/bootstrap.py,
  and the app\release_guard.py file itself. Only such a folder may lose its old .py files. }
function OwnInstall(const AppDir: String): Boolean;
var
  Raw: AnsiString;
  Text: String;
begin
  Result := False;
  if GitCheckout(AppDir) then Exit;
  if not FileExists(AppDir + '\app\release_guard.py') then Exit;
  if not LoadStringFromFile(AppDir + '\BUILD_MANIFEST.json', Raw) then Exit;
  Text := String(Raw);
  Result := (Pos('"version": "v0.7.', Text) > 0) and (Pos('"build_revision": "', Text) > 0) and
    (Pos('"files": {', Text) > 0) and (Pos('"app/release_guard.py": "', Text) > 0) and
    (Pos('"app/bootstrap.py": "', Text) > 0);
end;

{ A git working copy, or a folder that already has app\ or desktop\ but is not an installation of this product:
  never install there. }
function ForeignDir(const AppDir: String): Boolean;
begin
  Result := GitCheckout(AppDir) or ((DirExists(AppDir + '\app') or FileExists(AppDir + '\app') or
    DirExists(AppDir + '\desktop') or FileExists(AppDir + '\desktop')) and not OwnInstall(AppDir));
end;

{ EXTRA_MODULE rule: remove every old .py below Dir (Skip and links are not entered). }
procedure DeletePyFiles(const Dir, Skip: String);
var
  FindRec: TFindRec;
  Path: String;
begin
  if not FindFirst(Dir + '\*', FindRec) then Exit;
  try
    repeat
      if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
      begin
        Path := Dir + '\' + FindRec.Name;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        begin
          if (CompareText(Path, Skip) <> 0) and ((FindRec.Attributes and FILE_ATTRIBUTE_REPARSE_POINT) = 0) then
            DeletePyFiles(Path, Skip);
        end
        else if CompareText(ExtractFileExt(FindRec.Name), '.py') = 0 then
          DeleteFile(Path);
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

function InitializeSetup(): Boolean;
var
  Dir, Why: String;
begin
  Result := True;
  Dir := PreviousAppDir();
  if Dir <> '' then
  begin
    Why := BotRunning(Dir);
    if Why <> '' then
    begin
      SuppressibleMsgBox(RunningText(Dir, Why), mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
  end;
  if not PythonFound() then
    if SuppressibleMsgBox('Не найден Python 3.14 (python.org). Без него 01_INSTALL не создаст окружение бота.' + #13#10 +
      'Установите Python 3.14 и запустите 01_INSTALL.bat и 16_DESKTOP.bat в папке бота после установки.' + #13#10#13#10 +
      'Продолжить установку?', mbConfirmation, MB_YESNO, IDYES) = IDNO then
    begin
      Result := False;
      Exit;
    end;
  if not WebView2Found() then
    if SuppressibleMsgBox('Не найден Microsoft Edge WebView2 Runtime: без него окно не откроется.' + #13#10 +
      'Установите Evergreen Runtime: ' + WEBVIEW2_URL + #13#10#13#10 +
      'Продолжить установку?', mbConfirmation, MB_YESNO, IDYES) = IDNO then
      Result := False;
end;

{ Directory page: refuse a foreign folder at once, so another folder can be chosen. }
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and ForeignDir(RemoveBackslashUnlessRoot(WizardDirValue())) then
  begin
    SuppressibleMsgBox(FOREIGN_TEXT, mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir, Why: String;
begin
  Result := '';
  Dir := ExpandConstant('{app}');
  if ForeignDir(Dir) then
  begin
    Result := FOREIGN_TEXT;
    Exit;
  end;
  Why := BotRunning(Dir);
  if Why <> '' then
  begin
    Result := RunningText(Dir, Why);
    Exit;
  end;
  if OwnInstall(Dir) then
  begin
    DeletePyFiles(Dir + '\app', Dir + '\app\data');
    DeletePyFiles(Dir + '\desktop', '');
  end;
end;

{ Runs a .bat of the installation folder through cmd.exe with stdin from NUL (its final "pause" returns at once) in a visible
  console; returns its exit code, -1 when cmd.exe could not be started. }
function RunBat(const Bat, Args, Status: String): Integer;
var
  Code: Integer;
begin
  WizardForm.StatusLabel.Caption := Status;
  if not Exec(ExpandConstant('{cmd}'), '/d /c ""' + ExpandConstant('{app}') + '\' + Bat + '"' + Args + ' <nul"',
    ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, Code) then
    Code := -1;
  Result := Code;
end;

function BatFailedText(const Bat: String; Code: Integer): String;
begin
  Result := 'Файлы бота установлены, но ' + Bat + ' завершился с ошибкой (код ' + IntToStr(Code) + ').' + #13#10#13#10 +
    'Откройте папку бота ' + ExpandConstant('{app}') + ' и запустите вручную 01_INSTALL.bat, затем 16_DESKTOP.bat: ' +
    'окно консоли покажет текст ошибки.';
end;

{ .venv exactly as 01_INSTALL.bat (needs Python 3.14 on PATH), then .venv-ui exactly as 16_DESKTOP.bat. }
procedure InstallEnvironments();
var
  Code: Integer;
begin
  EnvReady := False;
  Code := RunBat('01_INSTALL.bat', '', 'Установка зависимостей бота (.venv, 01_INSTALL)…');
  if Code <> 0 then
  begin
    SuppressibleMsgBox(BatFailedText('01_INSTALL.bat', Code), mbError, MB_OK, IDOK);
    Exit;
  end;
  Code := RunBat('16_DESKTOP.bat', ' --prepare', 'Установка окружения окна (.venv-ui)…');
  if Code <> 0 then
  begin
    SuppressibleMsgBox(BatFailedText('16_DESKTOP.bat', Code), mbError, MB_OK, IDOK);
    Exit;
  end;
  EnvReady := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    InstallEnvironments();
end;

function WindowReady(): Boolean;
begin
  Result := EnvReady;
end;

function InitializeUninstall(): Boolean;
var
  Why: String;
begin
  Result := True;
  Why := BotRunning(ExpandConstant('{app}'));
  if Why <> '' then
  begin
    SuppressibleMsgBox(RunningText(ExpandConstant('{app}'), Why), mbError, MB_OK, IDOK);
    Result := False;
  end;
end;
