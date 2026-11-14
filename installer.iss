; AetherNode 安装程序（Inno Setup 6）
;
; 用法：先跑 build.ps1 生成 dist\AetherNode\，再用 Inno Setup 编译本文件：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;
; 本机没有安装 Inno Setup，所以这个脚本**未经编译验证**。语法按 Inno Setup 6 的
; 标准写法，但真要用需要你在装了 ISCC 的机器上跑一次。

#define AppName "AetherNode"
#define AppVersion "0.2.0"
#define AppPublisher "AetherNode"
#define AppExe "AetherNode.exe"

[Setup]
AppId={{8F3C1A2E-5B7D-4E9A-9C21-7D4E6F8A1B3C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist\installer
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
WizardStyle=modern
; 未签名的安装包会被 SmartScreen 警告，这是分发层问题，不影响功能
UninstallDisplayName={#AppName} {#AppVersion}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; 整个 onedir 产物一起装：少了任何一个 DLL 都起不来
Source: "dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 保留 data 目录（模型、账本、身份），只清理程序文件——
; 卸载时删掉 identity.json 等于让用户永久失去节点身份
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#AppExe}"
