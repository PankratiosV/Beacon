; Inno Setup script for the Beacon installer (built from the v2 code).
; Built by installer\build_installer.bat (needs the app bundled into dist\ first).
; Bump MyAppVersion with APP_VERSION in laser_status_app.py for each release.
; v2 has its own name, AppId and taskbar ID, so it installs alongside the
; original Laser Status instead of replacing it.
;
; How an Inno Setup script reads: #define lines are constants used below as
; {#Name}; [Setup] sets up the installer itself; [Files] is what gets copied;
; [Icons] the shortcuts; [Run] what starts after installing; [UninstallRun]
; and [Code] (Pascal script) extra steps when uninstalling. {app} is the
; install folder, {autoprograms} the Start menu, {userappdata} %APPDATA%.

#define MyAppName      "Beacon"
#define MyAppVersion   "2.1.0"
#define MyAppPublisher "Beacon"
#define MyAppExeName   "Beacon.exe"
; Shortcut name, matching the window title (RELEASE_LABEL in laser_status_app.py)
#define MyShortcutName MyAppName + " " + MyAppVersion + " BETA"
; same as APP_ID in the app, so the taskbar groups the window with its shortcut
#define MyAppId        "LaserStatusV2.App"

[Setup]
; Keep this AppId the same for every v2 release, so updates replace the old install
; (the original Laser Status uses {6F1C2D8A-4B7E-4C1F-9A3D-2E5B8C7F1A40})
AppId={{D84EE59C-41B6-4670-BFF9-1EA3A6D6DED7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Add AppPublisherURL= / AppSupportURL= here once there's a website
; Per-user install: no admin prompt, goes to %LOCALAPPDATA%\Programs\Laser Status
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=license.txt
OutputDir=Output
OutputBaseFilename=Beacon-Setup-{#MyAppVersion}
SetupIconFile=..\laser_status.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Close a running copy before updating or uninstalling
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Beacon\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\laser_stack_light\laser_stack_light.ino"; DestDir: "{app}\Arduino\laser_stack_light"; Flags: ignoreversion
Source: "..\esp32_stack_light\esp32_stack_light.ino"; DestDir: "{app}\Arduino\esp32_stack_light"; Flags: ignoreversion
Source: "..\docs\hardware-kit.md"; DestDir: "{app}\Docs"; Flags: ignoreversion

[InstallDelete]
; Shortcuts from earlier versions ("Beacon", "Beacon 2.0.0 BETA", ...): replaced below
Type: files; Name: "{autoprograms}\{#MyAppName}.lnk"
Type: files; Name: "{autoprograms}\{#MyAppName} * BETA.lnk"
Type: files; Name: "{autodesktop}\{#MyAppName}.lnk"
Type: files; Name: "{autodesktop}\{#MyAppName} * BETA.lnk"

[Icons]
Name: "{autoprograms}\{#MyShortcutName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppId}"
Name: "{autodesktop}\{#MyShortcutName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppId}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
; After an automatic update (silent install) start the new version again
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: WizardSilent

[UninstallRun]
; The phone-dashboard firewall rule (only removable when uninstalling as admin)
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Beacon phone dashboard"""; Flags: runhidden; RunOnceId: "DelFirewallRule"

[Code]
// Settings (Telegram token, relay buttons), statistics and logs live in
// %APPDATA%\LaserStatusV2. Uninstalling asks whether to delete them too;
// keeping them means a reinstall picks up where you left off.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // "Start with Windows" entry, if it was switched on
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyAppName}');
    Data := ExpandConstant('{userappdata}\LaserStatusV2');
    if DirExists(Data) and not UninstallSilent then
      if MsgBox('Also delete your {#MyAppName} settings, statistics and logs?' #13#10#13#10 +
                'This removes saved notification logins (such as your Telegram bot), relay ' +
                'buttons and job history from this PC. Choose No to keep them for a reinstall.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
