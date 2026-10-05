; Inno Setup script for "LTC Player Setup <version>.exe".
;
; Built by .github/workflows/windows-app.yml after PyInstaller has made
; dist\LTC Player\ (packaging/windows/ltcplay.spec):
;   iscc /DAppVersion=2026.10.10.1 /DVersionInfo=2026.10.10.1 packaging\windows\installer.iss
;
; What it does that matters for a show PC:
;   - Refuses to install (or uninstall) while the engine says a show is
;     running or starting.
;   - Stops flamesafe the proper way before replacing anything: it asks LTC
;     Player.exe (the supervisor) to stop, and the supervisor sends each
;     program Ctrl-Break, so flamesafe sends its safe zeros. Nothing is ever
;     force-closed. Windows' "close applications" feature is switched off for
;     the same reason: it would end flamesafe without its zeros.
;   - Keeps a copy of itself in {app}\Installers, so the Start menu's "Roll
;     back to the previous version" can run the one before.
;   - Settings and show files are never touched: they live in
;     %LOCALAPPDATA%\ltcplay and wherever the show folder is.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef VersionInfo
  #define VersionInfo "0.0.0.0"
#endif
#define AppDist "..\..\dist\LTC Player"

[Setup]
AppId={{8C3F2B6E-5D1A-4E7B-9F0C-2A6D4B8E1C35}
AppName=LTC Player
AppVersion={#AppVersion}
AppVerName=LTC Player {#AppVersion}
AppPublisher=Jeff Holmes Presents
AppPublisherURL=https://github.com/jholmes09/LTCPlay
DefaultDirName={autopf}\LTC Player
DefaultGroupName=LTC Player
DisableProgramGroupPage=yes
DisableDirPage=yes
UsePreviousAppDir=yes
OutputDir=..\..\dist
OutputBaseFilename=LTC Player Setup {#AppVersion}
SetupIconFile=build\ltcplay.ico
UninstallDisplayIcon={app}\LTC Player.exe
UninstallDisplayName=LTC Player
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=no
RestartApplications=no
WizardStyle=modern
SetupLogging=yes
MinVersion=10.0
VersionInfoVersion={#VersionInfo}
VersionInfoProductVersion={#VersionInfo}
VersionInfoProductTextVersion={#AppVersion}
; ===================== SIGNING HOOK (not used yet) =====================
; The installer and uninstaller are unsigned for now. To sign them, define
; SignToolCmd and pass the signing command to iscc as a sign tool named
; "ltcsign", for example:
;   iscc /DSignToolCmd "/Sltcsign=signtool.exe sign /fd sha256 /tr http://timestamp.acs.microsoft.com /td sha256 /dlib ... $f" installer.iss
; The workflow has a matching, switched-off "Sign" step. The four .exe files
; inside the app would be signed by that step before this script runs.
#ifdef SignToolCmd
SignTool=ltcsign
SignedUninstaller=yes
#endif
; =======================================================================

[Tasks]
Name: "autostart"; Description: "Start LTC Player automatically when this Windows account signs in (recommended on the show PC)"
Name: "desktopicon"; Description: "Put an LTC Player icon on the desktop"; Flags: unchecked

[InstallDelete]
; The old program files go first, so nothing from an earlier version is
; left mixed in with the new one.
Type: filesandordirs; Name: "{app}\_internal"

[Dirs]
; Where "Stop LTC Player" and this installer leave the stop request. The
; show account (not an administrator) must be able to write it too.
Name: "{commonappdata}\LTC Player"; Permissions: users-modify

[Files]
Source: "{#AppDist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "SHOW PC CHECKLIST.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\flamesafe\flamesafe.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{srcexe}"; DestDir: "{app}\Installers"; DestName: "LTC Player Setup {#AppVersion}.exe"; Flags: external ignoreversion; Check: NotRunFromInstallers

[Icons]
Name: "{group}\LTC Player"; Filename: "{app}\LTC Player.exe"; Parameters: "--start"; Comment: "Start LTC Player if it is not running, and open the show page"
Name: "{group}\Stop LTC Player"; Filename: "{app}\LTC Player.exe"; Parameters: "--stop"; Comment: "Stop everything the safe way (refused while a show is running)"
Name: "{group}\Roll back to the previous version"; Filename: "{app}\LTC Player.exe"; Parameters: "--rollback"
Name: "{group}\LTC Player bench soak test"; Filename: "{app}\ltcplay-soak.exe"; Comment: "PC-only stress test: a 7 min 24 s show every 15 minutes for 1, 8 or 24 hours, then a report on the Desktop"
Name: "{group}\LTC Player bench soak A-B (priority on, then off)"; Filename: "{app}\ltcplay-soak.exe"; Parameters: "--ab 20"; Comment: "Two 20 minute soaks, each with one full 7 min 24 s show, scheduling protection on then off, MadMapper maximized and restored every 2 minutes; one report on the Desktop"
Name: "{group}\LTC Player bench soak A-B (containment on, then off)"; Filename: "{app}\ltcplay-soak.exe"; Parameters: "--ab-contain 20"; Comment: "Two 20 minute soaks, scheduling protection on in both, MadMapper started fresh before each; the first with MadMapper and BEYOND at Below normal and MadMapper kept off two CPUs; one report on the Desktop"
Name: "{group}\LTC Player bench soak, MadMapper settings (3 runs)"; Filename: "{app}\ltcplay-soak.exe"; Parameters: "--mm-settings 20"; Comment: "Three 20 minute soaks, scheduling protection on, containment off; before each it says what to set in MadMapper and waits for MadMapper started fresh; one report on the Desktop"
Name: "{group}\Show PC checklist"; Filename: "{app}\SHOW PC CHECKLIST.txt"
Name: "{group}\Settings and logs"; Filename: "{app}\LTC Player.exe"; Parameters: "--open-settings"
Name: "{group}\Uninstall LTC Player"; Filename: "{uninstallexe}"
Name: "{autodesktop}\LTC Player"; Filename: "{app}\LTC Player.exe"; Parameters: "--start"; Tasks: desktopicon

[Run]
Filename: "{app}\LTC Player.exe"; Parameters: "--prune-installers --quiet"; Flags: waituntilterminated
Filename: "{app}\LTC Player.exe"; Parameters: "--install-task --quiet"; Tasks: autostart; Flags: runasoriginaluser waituntilterminated
Filename: "{app}\LTC Player.exe"; Parameters: "--remove-task --quiet"; Tasks: not autostart; Flags: waituntilterminated
Filename: "{app}\LTC Player.exe"; Parameters: "--start --quiet"; Flags: runasoriginaluser nowait; StatusMsg: "Starting LTC Player..."

[UninstallRun]
Filename: "{app}\LTC Player.exe"; Parameters: "--remove-task --quiet"; Flags: waituntilterminated; RunOnceId: "RemoveTask"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\Installers"
Type: filesandordirs; Name: "{app}\_internal"

[Code]
const
  MutexName = 'LTCPlayerSupervisor';
  StateUrl = 'http://127.0.0.1:7878/api/state';

function ControlDir: String;
begin
  Result := ExpandConstant('{commonappdata}\LTC Player');
end;

function NotRunFromInstallers: Boolean;
begin
  Result := CompareText(ExtractFileDir(ExpandConstant('{srcexe}')),
                        ExpandConstant('{app}\Installers')) <> 0;
end;

{ True while the engine says a show is running or starting. An engine that
  does not answer has no show running. }
function ShowRunning: Boolean;
var
  Http: Variant;
  Body: String;
begin
  Result := False;
  try
    Http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Http.SetTimeouts(2000, 2000, 3000, 3000);
    Http.Open('GET', StateUrl, False);
    Http.Send('');
    if Http.Status = 200 then
    begin
      Body := Http.ResponseText;
      Result := (Pos('"running": true', Body) > 0) or
                (Pos('"starting": true', Body) > 0);
    end;
  except
    Result := False;
  end;
end;

{ The names of any LTC Player programs running, in any session. }
function OurPrograms: String;
var
  Locator, Service, Procs, P: Variant;
  I: Integer;
begin
  Result := '';
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Procs := Service.ExecQuery('SELECT Name FROM Win32_Process WHERE ' +
      'Name=''flamesafe.exe'' OR Name=''ltcplay.exe'' OR ' +
      'Name=''ltcplay-deck.exe'' OR Name=''LTC Player.exe'' OR ' +
      'Name=''ltcplay-soak.exe''');
    for I := 0 to Procs.Count - 1 do
    begin
      P := Procs.ItemIndex(I);
      if Result <> '' then Result := Result + ', ';
      Result := Result + P.Name;
    end;
  except
    Result := '';
  end;
end;

{ Ask LTC Player to stop, the safe way, and wait for it. }
function StopLTCPlayer(var Why: String): Boolean;
var
  Token, Running: String;
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := False;
  Why := '';
  if ShowRunning then
  begin
    Why := 'A show is running. Press Stop on the show page first, then run this again. Nothing was changed.';
    Exit;
  end;
  Running := OurPrograms;
  if Running = '' then
  begin
    Result := True;
    Exit;
  end;
  if not CheckForMutexes(MutexName) then
  begin
    Why := 'These LTC Player programs are running, but not under LTC Player itself (or under another Windows account): ' + Running + '. Stop them with "Stop LTC Player" in that account, or with Ctrl+C in their windows. Never use End task: it stops flamesafe without its safe zeros. Nothing was changed.';
    Exit;
  end;
  ForceDirectories(ControlDir);
  Token := 'setup ' + GetDateTimeString('yyyymmddhhnnss', #0, #0) + ' ' + IntToStr(Random(1000000));
  if not SaveStringToFile(ControlDir + '\stop', Token + #13#10, False) then
  begin
    Why := 'Could not ask LTC Player to stop (' + ControlDir + '\stop could not be written). Use "Stop LTC Player" from the Start menu, then run this again.';
    Exit;
  end;
  Log('Asked LTC Player to stop: ' + Token);
  for I := 1 to 180 do
  begin
    Sleep(500);
    if LoadStringsFromFile(ControlDir + '\stop-refused', Lines) then
      if (GetArrayLength(Lines) > 1) and (Trim(Lines[0]) = Token) then
      begin
        Why := 'LTC Player did not stop: ' + Lines[1] + ' Nothing was changed.';
        Exit;
      end;
    if not CheckForMutexes(MutexName) then
      Break;
  end;
  if CheckForMutexes(MutexName) then
  begin
    Why := 'LTC Player did not finish stopping within 90 seconds. Nothing was changed. Its logs are in the "Settings and logs" folder.';
    Exit;
  end;
  for I := 1 to 20 do
  begin
    Running := OurPrograms;
    if Running = '' then
      Break;
    Sleep(500);
  end;
  if Running <> '' then
  begin
    Why := 'These programs are still running after LTC Player stopped: ' + Running + '. They were not forced to quit. Nothing was changed.';
    Exit;
  end;
  Log('LTC Player stopped cleanly');
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Why: String;
begin
  if StopLTCPlayer(Why) then
    Result := ''
  else
  begin
    Log('Refused: ' + Why);
    Result := Why;
  end;
end;

function InitializeUninstall: Boolean;
var
  Why: String;
begin
  Result := StopLTCPlayer(Why);
  if not Result then
  begin
    Log('Uninstall refused: ' + Why);
    SuppressibleMsgBox(Why, mbCriticalError, MB_OK, IDOK);
  end;
end;
