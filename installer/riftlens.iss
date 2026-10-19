; RiftLens installer (Inno Setup 6). Built by `build_desktop.py --installer`, or:
;   ISCC.exe /DAppVersion=1.0.0 installer\riftlens.iss
; Installs per user (no administrator needed) to %LOCALAPPDATA%\Programs\RiftLens.
; User data stays in %APPDATA%\RiftLens and is kept on uninstall.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{5311168A-EABC-4435-9198-56FFB2750952}
AppName=RiftLens
AppVersion={#AppVersion}
AppVerName=RiftLens {#AppVersion}
AppPublisher=JaveyBae
AppPublisherURL=https://github.com/JaveyBae/riftlens
AppSupportURL=https://github.com/JaveyBae/riftlens/issues
DefaultDirName={autopf}\RiftLens
DefaultGroupName=RiftLens
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\release
OutputBaseFilename=RiftLens-Setup-{#AppVersion}
SetupIconFile=..\assets\riftlens.ico
UninstallDisplayIcon={app}\RiftLens.exe
UninstallDisplayName=RiftLens
LicenseFile=..\LICENSE
WizardStyle=modern
ShowLanguageDialog=auto
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimp.DesktopIcon=创建桌面快捷方式
chinesesimp.RunAsAdmin=总是以管理员身份运行（国服玩家需要勾选：国服客户端以管理员权限运行，否则读不到客户端）
chinesesimp.Launch=启动 RiftLens
chinesesimp.NoWebView2=这台电脑没有检测到 Microsoft Edge WebView2 运行库，RiftLens 的窗口需要它。%n%n安装完成后，请到微软官网下载“WebView2 Runtime（常青版）”并安装：%nhttps://developer.microsoft.com/microsoft-edge/webview2/%n%n（Windows 11 和大多数 Windows 10 已经自带。）
english.DesktopIcon=Create a desktop shortcut
english.RunAsAdmin=Always run as administrator (needed for the Tencent / China client)
english.Launch=Launch RiftLens
english.NoWebView2=Microsoft Edge WebView2 Runtime was not found. RiftLens needs it for its window.%n%nAfter setup, install the Evergreen WebView2 Runtime from:%nhttps://developer.microsoft.com/microsoft-edge/webview2/

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"
Name: "runasadmin"; Description: "{cm:RunAsAdmin}"; Flags: unchecked

[Files]
Source: "..\dist\RiftLens\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\RiftLens"; Filename: "{app}\RiftLens.exe"
Name: "{autodesktop}\RiftLens"; Filename: "{app}\RiftLens.exe"; Tasks: desktopicon

[Registry]
; Windows' own "Run this program as an administrator" compatibility flag, for this user only
Root: HKCU; Subkey: "Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"; ValueType: string; ValueName: "{app}\RiftLens.exe"; ValueData: "~ RUNASADMIN"; Tasks: runasadmin; Flags: uninsdeletevalue

[Run]
Filename: "{app}\RiftLens.exe"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent shellexec

[Code]
function HasWebView2(): Boolean;
var
  V: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0')
    or RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0')
    or RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not HasWebView2() and not WizardSilent() then
    MsgBox(CustomMessage('NoWebView2'), mbInformation, MB_OK);
end;
