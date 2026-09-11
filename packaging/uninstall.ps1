<#
  Mikasa 卸载程序（随安装落地在安装目录里，与 install.ps1 对称）

  做四件事，顺序不能换：
    1. 先停掉正在运行的 Mikasa —— exe 还开着就删不掉目录
    2. 删快捷方式（桌面 / 开始菜单）—— 两个位置都试，用户装时可能只选了其一
    3. 删"应用和功能"里的注册项（HKCU，不需要管理员）
    4. 最后删安装目录**自己**——脚本正跑在里面，直接删会失败，
       所以交给一个延迟启动的 cmd：等本进程退出后再 rmdir

  **不动用户数据**：数据库/上传件/日志在 %LOCALAPPDATA%\Mikasa，与本目录无关，
  卸载不该顺手删掉用户攒的资料（重装即恢复）。要彻底清干净，安装后提示里给了路径。

  ⚠ 与 install.ps1 一样，本文件必须存成 UTF-8 with BOM。
#>
param([switch]$Quiet)

$ErrorActionPreference = "Stop"
$InstallDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppName     = "Mikasa"
$DataDir     = Join-Path $env:LOCALAPPDATA "Mikasa"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Mikasa"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

Say ""
Say "=== 卸载 Mikasa ===" "Cyan"
Say "安装位置：$InstallDir"

# 安全闸：**只在"这里确实是 Mikasa 安装目录"时才允许整树删除**。
# .ps1 的常规启动方式就是右键"使用 PowerShell 运行"，用户很容易把它拷到别处
# 再双击（发布包根目录里就躺着一份）。没有这道闸的话，拷到桌面运行 = 清空桌面
# ——2026-09-11 审查明确指出（install.ps1 对自定义路径有 Mikasa.exe 闸门，
# 卸载侧当时完全没有对应物）。
if (-not (Test-Path (Join-Path $InstallDir "Mikasa.exe"))) {
    Say "这个目录里没有 Mikasa.exe：$InstallDir" "Red"
    Say "为避免误删，卸载已中止。" "Red"
    Say "请从安装目录里运行 Uninstall.bat，或走「设置 → 应用和功能」。" "Red"
    exit 1
}

if (-not $Quiet) {
    $answer = Read-Host "确认卸载？[y/N]"
    if ($answer.Trim().ToLower() -notin @("y", "yes")) { Say "已取消。"; exit 0 }
}

# 1) 停掉正在运行的实例
$running = @(Get-Process -Name $AppName -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Say "先关闭正在运行的 Mikasa…" "Yellow"
    $running | ForEach-Object { $_.CloseMainWindow() | Out-Null }
    Start-Sleep -Seconds 2
    @(Get-Process -Name $AppName -ErrorAction SilentlyContinue) |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# 2) 快捷方式：两个位置都清（只建了其中一个也要清干净）
$links = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "$AppName\$AppName.lnk")
)
foreach ($link in $links) {
    if (Test-Path $link) { Remove-Item $link -Force; Say "已删除 $link" }
}
$startMenuDir = Join-Path ([Environment]::GetFolderPath("Programs")) $AppName
if (Test-Path $startMenuDir) { Remove-Item $startMenuDir -Recurse -Force }

# 3) 注册项
if (Test-Path $UninstallKey) { Remove-Item $UninstallKey -Recurse -Force; Say "已移除卸载注册项" }

# 4) 删自己：交给延迟 cmd（本进程还占着目录里的脚本文件）
#
# ⚠ 必须先离开这个目录，父子两个进程都要离开：
# Windows **不允许删除任何进程的当前工作目录**。而 Uninstall.bat 会先
# `cd /d "%~dp0"`（进安装目录）再 `pause` 挂住，父 PowerShell 与子 cmd 的 CWD
# 都在安装目录里——结果 rmdir 只删掉目录内的文件、保留空壳并报"另一个程序
# 正在使用此文件"，脚本却照样打印"已卸载"（2026-09-11 审查在本机实测）。
Set-Location $env:TEMP
Say "正在清理安装目录…" "Cyan"
Start-Process cmd -ArgumentList "/c timeout /t 2 /nobreak >nul & rmdir /s /q `"$InstallDir`"" `
    -WindowStyle Hidden -WorkingDirectory $env:TEMP

Say ""
Say "已卸载。" "Green"
Say "你的资料**没有被删除**，仍在：$DataDir" "Yellow"
Say "（数据库 / 上传件 / 日志都在那里，重装即可继续用；要彻底清掉请手动删该目录。）"
