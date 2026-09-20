; ============================================================
;   setup.iss - Dr.COM 校园网自动登录 Inno Setup 6 脚本
;   版本: v2.1
;   编码: UTF-8 + BOM（ISCC 推荐 UTF-8 BOM）
;   目标: 生成 DrcomAutoLogin-Setup-v2.1.exe
; ============================================================

#define MyAppName "Dr.COM 校园网自动登录"
; 允许 CI 用 ISCC /DMyAppVersion=x.y 覆盖；本地直接编译时用下面的默认值
#ifndef MyAppVersion
  #define MyAppVersion "2.1"
#endif
#define MyAppPublisher "Dr.COM AutoLogin"
#define MyAppExeName "联网_service.py"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\DrcomAutoLogin
DisableProgramGroupPage=yes
PrivilegesRequired=admin
AppMutex=DrcomAutoLogin-mutex-v2
AppId={{A8F2E3D1-7C4B-4F89-9D5E-1A2B3C4D5E6F}
OutputBaseFilename=DrcomAutoLogin-Setup-v{#MyAppVersion}
OutputDir=output
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
;SetupIconFile=installer.ico

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "startservice"; Description: "安装完成后立即启动服务"; GroupDescription: "附加任务:"

[Files]
Source: "..\联网_service.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "config.json.template"; DestDir: "{app}"; DestName: "config.json"; Flags: ignoreversion onlyifdoesntexist
Source: "password.txt.template"; DestDir: "{app}"; DestName: "password.txt"; Flags: ignoreversion onlyifdoesntexist
Source: "..\tools\nssm.exe"; DestDir: "{app}\tools"; Flags: ignoreversion

[Dirs]
Name: "{app}\logs"

[Icons]
Name: "{group}\Dr.COM 校园网自动登录"; Filename: "{app}\启动UI.bat"; IconFilename: "{sys}\shell32.dll"; IconIndex: 13
Name: "{group}\查看日志"; Filename: "{app}\logs"
Name: "{group}\卸载 Dr.COM 校园网自动登录"; Filename: "{uninstallexe}"
Name: "{commondesktop}\Dr.COM 校园网自动登录"; Filename: "{app}\启动UI.bat"; Tasks: desktopicon; IconFilename: "{sys}\shell32.dll"; IconIndex: 13

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\tools"

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c ""{app}\tools\nssm.exe"" stop DrcomAutoLogin"; Flags: runhidden; RunOnceId: "StopDrcomAutoLogin"
Filename: "{cmd}"; Parameters: "/c ""{app}\tools\nssm.exe"" remove DrcomAutoLogin confirm"; Flags: runhidden; RunOnceId: "RemoveDrcomAutoLogin"

[Code]

// ============================================================
//   GetPythonPath - 探测 Python 3.14 安装位置
//   返回完整路径（含文件名），失败返回空字符串
// ============================================================
function GetPythonPath(): string;
var
  RegValue: string;
begin
  // 1. 注册表 HKLM
  if RegQueryStringValue(HKEY_LOCAL_MACHINE, 'SOFTWARE\Python\PythonCore\3.14\InstallPath', '', RegValue) then
  begin
    Result := AddBackslash(RegValue) + 'python.exe';
    if FileExists(Result) then Exit;
  end;
  // 2. 注册表 HKCU
  if RegQueryStringValue(HKEY_CURRENT_USER, 'SOFTWARE\Python\PythonCore\3.14\InstallPath', '', RegValue) then
  begin
    Result := AddBackslash(RegValue) + 'python.exe';
    if FileExists(Result) then Exit;
  end;
  // 3. 常见路径 fallback
  if FileExists('C:\Python314\python.exe') then begin Result := 'C:\Python314\python.exe'; Exit; end;
  if FileExists('C:\Program Files\Python314\python.exe') then begin Result := 'C:\Program Files\Python314\python.exe'; Exit; end;
  if FileExists('C:\Program Files (x86)\Python314\python.exe') then begin Result := 'C:\Program Files (x86)\Python314\python.exe'; Exit; end;
  // 4. 找不到返回空
  Result := '';
end;

// ============================================================
//   InitializeSetup - 安装前 Python 探测
// ============================================================
function InitializeSetup(): Boolean;
var
  PythonPath: string;
begin
  PythonPath := GetPythonPath();
  if PythonPath = '' then
  begin
    MsgBox('未检测到 Python 3.14。' + #13#10 + #13#10 +
           '请先安装 Python 3.14，或将 python.exe 所在目录加入 PATH。' + #13#10 +
           'Python 官网: https://www.python.org/downloads/' + #13#10 + #13#10 +
           '安装路径可以是:' + #13#10 +
           '  C:\Python314\python.exe' + #13#10 +
           '  C:\Program Files\Python314\python.exe',
           mbError, MB_OK);
    Result := False;
  end
  else
  begin
    Result := True;
  end;
end;

// ============================================================
//   CreateLauncherBat - 生成启动 Web UI 的 bat 文件
// ============================================================
procedure CreateLauncherBat();
var
  LauncherPath: string;
  Content: string;
begin
  LauncherPath := ExpandConstant('{app}\启动UI.bat');
  Content :=
    '@echo off' + #13#10 +
    'start "" "http://127.0.0.1:8848"' + #13#10 +
    'exit';
  SaveStringToFile(LauncherPath, Content, False);
end;

// ============================================================
//   CreateURLFile - 写 .url Internet 快捷方式
//   内容纯 ASCII，无需考虑编码
// ============================================================
procedure CreateURLFile(const FilePath, URL: string);
var
  Content: AnsiString;
begin
  Content := '[InternetShortcut]' + #13#10 + 'URL=' + URL;
  SaveStringToFile(FilePath, Content, False);
end;

// ============================================================
//   CreateStartMenuShortcuts - 创建桌面/开始菜单 .url 快捷方式
// ============================================================
procedure CreateStartMenuShortcuts();
var
  DesktopPath: string;
  StartMenuPath: string;
begin
  DesktopPath := ExpandConstant('{userdesktop}');
  StartMenuPath := ExpandConstant('{userstartmenu}') + '\Programs';
  if IsTaskSelected('desktopicon') then
  begin
    CreateURLFile(DesktopPath + '\Dr.COM 校园网自动登录.url', 'http://127.0.0.1:8848');
  end;
  CreateURLFile(StartMenuPath + '\Dr.COM 校园网自动登录.url', 'http://127.0.0.1:8848');
end;

// ============================================================
//   RegisterService - 通过 NSSM 注册 Windows 服务
// ============================================================
procedure RegisterService();
var
  PythonPath: string;
  AppDir: string;
  ScriptPath: string;
  NSSM: string;
  ResultCode: Integer;
begin
  PythonPath := GetPythonPath();
  AppDir := ExpandConstant('{app}');
  ScriptPath := AppDir + '\联网_service.py';
  NSSM := AppDir + '\tools\nssm.exe';

  // 幂等：先停再删
  Exec(NSSM, 'stop DrcomAutoLogin', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'remove DrcomAutoLogin confirm', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // 注册
  Exec(NSSM, 'install DrcomAutoLogin "' + PythonPath + '" "' + ScriptPath + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin AppDirectory "' + AppDir + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin DisplayName "Dr.COM 校园网自动登录"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin Description "Dr.COM 校园网认证 - Web UI 配置版 v2.1"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin Start SERVICE_AUTO_START', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin AppStdout "' + AppDir + '\logs\service_stdout.log"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin AppStderr "' + AppDir + '\logs\service_stderr.log"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin AppRotateFiles 1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin AppRotateBytes 1048576', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin AppExit Default Restart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(NSSM, 'set DrcomAutoLogin AppExit 0 Restart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  // 启动（仅当用户在 wizard 勾选）
  if IsTaskSelected('startservice') then
  begin
    Exec(NSSM, 'start DrcomAutoLogin', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

// ============================================================
//   CurStepChanged - 安装完成后调用
// ============================================================
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    CreateLauncherBat();
    RegisterService();
    CreateStartMenuShortcuts();
  end;
end;
