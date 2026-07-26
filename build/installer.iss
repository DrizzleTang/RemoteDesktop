; ============================================================
;  远程桌面助手 - Inno Setup 安装脚本
; ============================================================
;  编译方法(需要先安装 Inno Setup 6, https://jrsoftware.org/isinfo.php):
;      ISCC.exe build\installer.iss
;
;  依赖: 编译前必须已经存在下列文件(先运行 build\build_windows.bat
;        或对应的 CI 步骤生成):
;      dist\RemoteDesktop-Host.exe
;      dist\RemoteDesktop-Client.exe
;
;  编译产物: Output\RemoteDesktopSetup.exe (相对于本 .iss 文件所在目录,
;            即最终生成于 build\Output\RemoteDesktopSetup.exe)
;
;  关于中文界面:
;      标准 Inno Setup 官方安装包默认只内置英文语言文件
;      (compiler:Default.isl),简体中文语言文件 ChineseSimplified.isl
;      并不是官方内置的,需要额外从
;      https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation
;      下载后放入 Inno Setup 安装目录的 Languages 文件夹才能使用。
;      为了保证在 CI(GitHub Actions, choco install innosetup)以及大多数
;      开发者本机上都能直接编译成功,这里默认只启用英文向导语言
;      (安装向导的 Next/Back/Cancel 等系统按钮会是英文),但所有
;      自定义的应用名称、快捷方式名称、任务描述等文字均使用中文。
;      如果你本机已经安装了简体中文语言包,可以取消下面 [Languages]
;      节中被注释掉的那一行,即可让安装向导整体显示为中文。
; ============================================================

#define MyAppName "远程桌面助手"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "RemoteDesktop Project"
#define MyAppExeHost "RemoteDesktop-Host.exe"
#define MyAppExeClient "RemoteDesktop-Client.exe"
#define MyDistDir "..\dist"

[Setup]
AppId={{7C6E9B2A-4C3D-4E6F-9B2A-1D3C5E7F9A0B}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\RemoteDesktop
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeClient}
OutputDir=Output
OutputBaseFilename=RemoteDesktopSetup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
; 注: {autopf}(系统 Program Files 目录)需要管理员权限才能写入,
;     因此这里使用默认的 PrivilegesRequired=admin,不要改成 lowest,
;     否则安装到 {autopf}\RemoteDesktop 可能因权限不足而失败。
PrivilegesRequired=admin
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; 如果本机 Inno Setup 已安装简体中文语言包(需从
; https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation
; 下载 ChineseSimplified.isl 并放入 Inno Setup 安装目录的 Languages 文件夹),
; 可取消下面这一行的注释以启用完整中文向导界面:
; Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked

[Files]
Source: "{#MyDistDir}\{#MyAppExeHost}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyDistDir}\{#MyAppExeClient}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyDistDir}\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\远程桌面-被控端"; Filename: "{app}\{#MyAppExeHost}"; Comment: "在被控制的电脑上运行"
Name: "{group}\远程桌面-主控端"; Filename: "{app}\{#MyAppExeClient}"; Comment: "在用来控制对方的电脑上运行"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\远程桌面-被控端"; Filename: "{app}\{#MyAppExeHost}"; Tasks: desktopicon
Name: "{autodesktop}\远程桌面-主控端"; Filename: "{app}\{#MyAppExeClient}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeClient}"; Description: "安装完成后立即启动 远程桌面-主控端"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
