; Mikasa 安装向导（Inno Setup）——单文件 Setup.exe，双击即装。
;
; 与 packaging/install.ps1 的分工：
;   - 本脚本产出**单文件 exe 安装向导**：双击、下一步、勾"创建桌面快捷方式"，
;     装完自动出现在开始菜单与"应用和功能"里，卸载走同一个向导。这是"企业级"
;     的那条路（用户不用解压、不用看到 bat）。
;   - install.ps1 保留：zip 绿色包里那份，给"解压即用 / 不想安装"的人和
;     没有 Inno Setup 的构建环境兜底。两者装出来的东西一致（同一份
;     dist\Mikasa 载荷、同样的快捷方式与卸载入口）。
;
; 编译（版本号从命令行传入，保持单一来源，见 tools/build_installer.py）：
;   ISCC.exe packaging\Mikasa.iss /DAppVersion=0.1.4
;
; 设计取舍：
;   - PrivilegesRequired=lowest → 装进 %LOCALAPPDATA%\Programs\Mikasa，
;     **全程不弹 UAC**。装到 Program Files 需要管理员，而这是个个人工具，
;     让用户每台机器都过一遍 UAC 不值当。
;   - 桌面快捷方式做成 [Tasks] 勾选项（默认勾上）——安装程序该问的事。
;   - 卸载**不动用户数据**（%LOCALAPPDATA%\Mikasa 里的库/上传件/日志）。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Mikasa"
#define AppDisplayName "Mikasa"
#define AppPublisher "Liameab"
#define AppExe "Mikasa.exe"

[Setup]
; AppId 标识"同一个应用"，升级时据此找到旧版本——**换它等于换一个应用**，
; 老用户会看到两份并存。保持不变。
AppId={{7C4B1F52-9E3A-4D18-8F2B-6A5D0C7E91A4}
AppName={#AppDisplayName}
AppVersion={#AppVersion}
AppVerName={#AppDisplayName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppDisplayName} 安装程序
VersionInfoCompany={#AppPublisher}

; 装进用户目录：不需要管理员
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Mikasa
DisableDirPage=no
DefaultGroupName=Mikasa
AllowNoIcons=yes

; 关掉"许可协议"页：MIT 的全文已随包放进安装目录，没必要卡一步让用户翻
LicenseFile=

OutputDir=..\dist
OutputBaseFilename=Mikasa-Setup-{#AppVersion}-win64
SetupIconFile=Mikasa.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppDisplayName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
; 向导语言**只改安装界面自己**（"下一步 / 取消"这些按钮的字），改不了应用语言——
; 应用目前是一套中文界面。原先还挂着 english（Inno 内置 Default.isl），结果用户
; 选了英文、装完打开还是中文，看起来像 bug（2026-09-11 反馈）。删掉它，
; 顺带省一步：Inno 只在语言数 ≥2 时才弹"选择安装语言"那个对话框。
; 哪天真做了应用多语言，再把它加回来。
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
; 只有桌面快捷方式做成勾选项（用户明确要的"安装时问一句"）。
; **开始菜单项无条件创建**：它本来就是标准配置，做成可选项只会多一个失败面——
; 实测把它挂在 Task 上时，静默安装（Inno 沿用上一轮的 task 记忆）根本没建出来。
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："

[Files]
; 载荷 = PyInstaller 的 onedir 产物（Mikasa.exe + _internal）
Source: "..\dist\Mikasa\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 法律文件随包分发：附第三方声明正是那些许可证的要求
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppDisplayName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\卸载 Mikasa"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppDisplayName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Description: "立即启动 {#AppDisplayName}"; Filename: "{app}\{#AppExe}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; **不删用户数据**。库里是用户攒的文档、向量与日志，装在别处；
; 卸载是"把这个程序拿掉"，不是"把用户的资料清掉"。要彻底清请手动删。
; 这里只清理安装时可能产生的 PyInstaller 运行残留。
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
// 装之前先关掉正在运行的 Mikasa：exe 被占用时复制会失败，
// 用户看到的会是"装不上"，而真正原因是"它还开着"。
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if Exec('taskkill.exe', '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    // 返回码 128 = 没有这个进程在跑，属正常；其余不管，交给后续步骤报错
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DirExists(ExpandConstant('{localappdata}\Mikasa')) then
      MsgBox('已卸载 Mikasa。' + #13#10 + #13#10 +
             '你的资料**没有被删除**，仍在：' + #13#10 +
             ExpandConstant('{localappdata}\Mikasa') + #13#10 + #13#10 +
             '（数据库 / 上传件 / 日志都在那里，重装即可继续用。要彻底清掉请手动删该目录。）',
             mbInformation, MB_OK);
  end;
end;
