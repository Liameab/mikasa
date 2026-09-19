<#
  Mikasa 安装程序（自包含：不需要 Inno Setup，也不需要管理员权限）

  为什么自己写：2026-09-11 实测这台机器**下不动 Inno Setup**（winget 走 GitHub，
  443 直接超时），而安装包是发布链路里的一环，不能卡在一个拿不到的工具上。
  系统自带的 PowerShell 就能把同一件事做完：拷文件、建快捷方式、注册卸载入口。

  做到的事：
    - 装进用户目录（默认 %LOCALAPPDATA%\Programs\Mikasa），**全程不需要管理员**
    - 询问是否创建桌面快捷方式 / 开始菜单项（企业级安装包的标准问法）
    - 在"应用和功能"里注册卸载入口（写 HKCU，卸载同样不需要管理员）
    - 覆盖安装前先停掉正在运行的 Mikasa（否则 exe 被占用，拷贝直接失败，
      用户看到的是"装不上"而不是"请先关掉它"）
    - 目标目录不是 Mikasa 安装目录时**要求确认**（手滑输错路径不该被清空）

  用法（用户解压后双击同级的 Install.bat）：
    powershell -ExecutionPolicy Bypass -File install.ps1
  可选参数：
    -InstallDir <路径>   指定安装位置
    -NoDesktopShortcut   不建桌面快捷方式（静默安装用）
    -NoStartMenu         不建开始菜单项
    -Quiet               不询问，全按默认

  ⚠ 本文件必须存成 **UTF-8 with BOM**：Windows PowerShell 5.1 读无 BOM 的
  UTF-8 会按 ANSI(cp936) 解，中文全成乱码。
#>
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Mikasa"),
    [switch]$NoDesktopShortcut,
    [switch]$NoStartMenu,
    [switch]$Quiet,
    # 非 Mikasa 目录的"确认清空"：交互模式可答 y，静默模式必须显式给这个开关
    [switch]$ForceClean
)

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Payload     = Join-Path $ScriptDir "Mikasa"            # 与安装器同级的应用目录
$AppExe      = Join-Path $Payload "Mikasa.exe"
$AppName     = "Mikasa"
$DisplayName = "Mikasa"
$Version     = "0.1.1"
$Publisher   = "Liameab"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Mikasa"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

function Ask-Yes($prompt, [bool]$default = $true) {
    # 只认 y/n，其它输入重新问——避免手滑把路径之类的字符串当成回答
    while ($true) {
        $hint = if ($default) { "[Y/n]" } else { "[y/N]" }
        $answer = Read-Host "$prompt $hint"
        if ([string]::IsNullOrWhiteSpace($answer)) { return $default }
        switch ($answer.Trim().ToLower()) {
            "y"   { return $true }
            "yes" { return $true }
            "n"   { return $false }
            "no"  { return $false }
        }
    }
}

function New-Link($linkPath, $target, $workDir, $description) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($linkPath)
    $lnk.TargetPath = $target
    $lnk.WorkingDirectory = $workDir
    $lnk.IconLocation = "$target,0"   # 图标内嵌在 exe 里（PyInstaller 打包时写入）
    $lnk.Description = $description
    $lnk.Save()
}

# ---------------- 0. 校验载荷 ----------------
if (-not (Test-Path $AppExe)) {
    Say "找不到应用文件：$AppExe" "Red"
    Say "请确认 Install.bat 与 Mikasa 文件夹在同一层。" "Red"
    Say "（解压后只把 Install.bat 单独拿出来是装不了的。）" "Red"
    exit 1
}

Say ""
Say "=== Mikasa 安装程序 ===" "Cyan"
Say ""

if (-not $Quiet) {
    Say "安装位置：$InstallDir"
    $custom = Read-Host "按回车使用该位置，或直接输入其它完整路径"
    if (-not [string]::IsNullOrWhiteSpace($custom)) { $InstallDir = $custom.Trim().Trim('"') }
}

$wantDesktop = -not $NoDesktopShortcut
$wantStart   = -not $NoStartMenu
if (-not $Quiet) {
    Say ""
    $wantDesktop = Ask-Yes "创建桌面快捷方式？" $true
    $wantStart   = Ask-Yes "在开始菜单创建项？" $true
}

# ---------------- 1. 目标路径安全校验 ----------------
# **这是唯一的数据破坏防线**：下面要对 $InstallDir 做 Remove-Item -Recurse -Force。
# 2026-09-11 审查实测出三种事故，全部由这段拦住：
#   ① `-InstallDir C:\ -Quiet` —— 原来的闸门末尾带着 `-and (-not $Quiet)`，
#      静默模式下**不弹任何确认**就清盘根；
#   ② 交互时手输 `C:\`（或 C:\Users）—— 闸门只问一句，答 y 就清；
#   ③ 把解压出来的发布包目录当安装目录 —— $Payload 先被自己删掉，紧接着从
#      已不存在的源复制 → 原载荷没了、新安装也没成，$ErrorActionPreference=Stop
#      直接中断，用户手上什么都不剩。
function Get-NormalizedPath([string]$p) {
    # 末尾分隔符会让后续判根/前缀比较失准（"C:\" 与 "C:" 要视为同一个）
    return [System.IO.Path]::GetFullPath($p).TrimEnd("\", "/")
}

$target = Get-NormalizedPath $InstallDir
$payloadRoot = Get-NormalizedPath $Payload

if ([string]::IsNullOrWhiteSpace($target)) {
    Say "安装路径为空，已取消。" "Red"; exit 1
}
if ($target -match "^[A-Za-z]:$") {
    Say "拒绝安装到盘根：$target（那会把整个盘清掉）" "Red"; exit 1
}
# 用户目录/桌面/文档/系统目录：装进去没意义，误清空代价极大
$protectedPaths = @(
    [Environment]::GetFolderPath("UserProfile"),
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("MyDocuments"),
    $env:SystemRoot,
    $env:ProgramFiles,
    ${env:ProgramFiles(x86)}
) | Where-Object { $_ } | ForEach-Object { Get-NormalizedPath $_ }
foreach ($guard in $protectedPaths) {
    if ($target -eq $guard) {
        Say "拒绝安装到系统/用户目录：$target" "Red"; exit 1
    }
}
# 与载荷目录重合：删=删掉自己；装进载荷里=自我递归复制
if ($target -eq $payloadRoot) {
    Say "拒绝：安装目录不能就是解压出来的发布包目录。" "Red"
    Say "回车用默认位置，或另指一个空目录。" "Red"
    exit 1
}
if ($payloadRoot.StartsWith($target + "\")) {
    Say "拒绝：$target 是发布包目录的上层，清空它会连带删掉安装文件。" "Red"; exit 1
}
if ($target.StartsWith($payloadRoot + "\")) {
    Say "拒绝：安装目录不能放在发布包目录里面（会自我递归复制）。" "Red"; exit 1
}

$isExistingMikasa = Test-Path (Join-Path $target "Mikasa.exe")
if ((Test-Path $target) -and (-not $isExistingMikasa)) {
    Say ""
    Say "注意：$target 已存在，但里面没有 Mikasa.exe。" "Yellow"
    Say "继续会**清空该目录**。" "Yellow"
    if ($Quiet -and (-not $ForceClean)) {
        # 静默模式绝不能自动清一个来路不明的目录——这条闸不能因为 -Quiet 就消失，
        # 否则无人值守脚本里一个手滑的参数就是一次清盘。要清必须显式 -ForceClean。
        Say "静默模式拒绝清空非 Mikasa 目录；确要如此请显式加 -ForceClean。" "Red"
        exit 1
    }
    if (-not $ForceClean -and -not (Ask-Yes "确定要继续吗？" $false)) {
        Say "已取消，未改动任何文件。"; exit 1
    }
}

# ---------------- 2. 停掉正在运行的实例 ----------------
# 不先停：exe 被占用 → Copy-Item 抛错 → 用户看到"装不上"，而真正原因是"它还开着"
$running = @(Get-Process -Name $AppName -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Say ""
    Say "检测到 Mikasa 正在运行，先关闭它…" "Yellow"
    $running | ForEach-Object { $_.CloseMainWindow() | Out-Null }
    Start-Sleep -Seconds 2
    @(Get-Process -Name $AppName -ErrorAction SilentlyContinue) |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    # 杀不掉就别往下走：删除在复制**之前**，中途失败会留下"删了一半、没装成"的
    # 目录，用户手上连原来那份能用的都没了（审查实测的失败序列）
    if (@(Get-Process -Name $AppName -ErrorAction SilentlyContinue).Count -gt 0) {
        Say "无法关闭正在运行的 Mikasa（可能以管理员身份启动，或文件被杀软占用）。" "Red"
        Say "请手动结束它后重试——现在继续会删掉旧文件却装不上新的。" "Red"
        exit 1
    }
}

# ---------------- 3. 复制文件 ----------------
Say ""
Say "正在复制文件（约 220 MB，视磁盘速度几十秒）…" "Cyan"
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
New-Item -ItemType Directory -Path $target -Force | Out-Null
Copy-Item -Path (Join-Path $Payload "*") -Destination $target -Recurse -Force
Say "文件复制完成。" "Green"

# ---------------- 4. 快捷方式 ----------------
$installedExe = Join-Path $target "Mikasa.exe"
if ($wantDesktop) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    New-Link (Join-Path $desktop "$AppName.lnk") $installedExe $target $DisplayName
    Say "已创建桌面快捷方式。" "Green"
}
if ($wantStart) {
    $startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) $AppName
    New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
    New-Link (Join-Path $startMenu "$AppName.lnk") $installedExe $target $DisplayName
    Say "已创建开始菜单项。" "Green"
}

# ---------------- 5. 卸载入口 ----------------
# 卸载器随安装落地成独立文件（而不是让注册表塞一长串命令）：
# 用户在"应用和功能"里点卸载走它，也能直接去安装目录双击 Uninstall.bat
$uninstaller = Join-Path $ScriptDir "uninstall.ps1"
if (Test-Path $uninstaller) {
    Copy-Item $uninstaller (Join-Path $target "uninstall.ps1") -Force
}
$uninstallBat = Join-Path $ScriptDir "Uninstall.bat"
if (Test-Path $uninstallBat) {
    Copy-Item $uninstallBat (Join-Path $target "Uninstall.bat") -Force
}

$sizeKb = [int]((Get-ChildItem $target -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1KB)
New-Item -Path $UninstallKey -Force | Out-Null
Set-ItemProperty -Path $UninstallKey -Name "DisplayName"     -Value $DisplayName
# 版本从**装好的 exe 的版本资源**读（spec 打包时已从 __init__.py 写入），
# 不再硬编码第二份——否则升版本后 exe 属性页是新的、"应用和功能"里永远是旧的
$appVersion = (Get-Item $installedExe).VersionInfo.ProductVersion
if ([string]::IsNullOrWhiteSpace($appVersion)) { $appVersion = $Version }
Set-ItemProperty -Path $UninstallKey -Name "DisplayVersion"  -Value $appVersion
Set-ItemProperty -Path $UninstallKey -Name "Publisher"       -Value $Publisher
Set-ItemProperty -Path $UninstallKey -Name "InstallLocation" -Value $target
Set-ItemProperty -Path $UninstallKey -Name "DisplayIcon"     -Value $installedExe
Set-ItemProperty -Path $UninstallKey -Name "EstimatedSize"   -Value $sizeKb -Type DWord
Set-ItemProperty -Path $UninstallKey -Name "NoModify"        -Value 1 -Type DWord
Set-ItemProperty -Path $UninstallKey -Name "NoRepair"        -Value 1 -Type DWord
Set-ItemProperty -Path $UninstallKey -Name "UninstallString" `
    -Value "powershell -NoProfile -ExecutionPolicy Bypass -File `"$target\uninstall.ps1`""
Say "已在「应用和功能」里注册卸载入口。" "Green"

# ---------------- 6. 收尾 ----------------
Say ""
Say "安装完成：$installedExe" "Green"
if (-not $Quiet) {
    if (Ask-Yes "现在启动 Mikasa？" $true) { Start-Process $installedExe }
}
