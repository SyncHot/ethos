; EthOS Drive — Inno Setup Installer Script
; Produces a single EthOSDriveSetup.exe Windows installer

#define AppName "EthOS Drive"
#define AppVersion "1.0.0"
#define AppPublisher "EthOS"
#define AppExeName "EthOS Drive.exe"

[Setup]
AppId={{B3F2A1D4-5E6F-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\..\..\dist
OutputBaseFilename=EthOSDriveSetup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Installer appearance
;SetupIconFile=..\icons\ethos-drive.ico
;WizardImageFile=..\icons\wizard-banner.bmp
;WizardSmallImageFile=..\icons\wizard-small.bmp

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Start EthOS Drive when Windows starts"; GroupDescription: "Startup:"; Flags: checkedonce

[Files]
; Copy PyInstaller output
Source: "..\..\..\dist\EthOS Drive\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; Auto-start on login
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "EthOS Drive"; ValueData: """{app}\{#AppExeName}"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch EthOS Drive"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Clean shutdown before uninstall
Filename: "{cmd}"; Parameters: "/c taskkill /f /im ""{#AppExeName}"" >nul 2>&1"; Flags: runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\EthOS\EthOS Drive"

[Code]
// Check if already running before install
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if Exec('cmd.exe', '/c tasklist /fi "imagename eq EthOS Drive.exe" | find /i "EthOS Drive.exe" >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
    begin
      if MsgBox('EthOS Drive is currently running. It will be closed before installation. Continue?', mbConfirmation, MB_YESNO) = IDNO then
        Result := False
      else
        Exec('cmd.exe', '/c taskkill /f /im "EthOS Drive.exe" >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;
