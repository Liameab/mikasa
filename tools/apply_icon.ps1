#Requires -Version 5.1
<#
  把 dist\Mikasa 这套刚打好的程序装到"桌面快捷方式指向的那个目录"，并刷新图标。

  **为什么需要它**：打包产物落在仓库的 dist\，而桌面图标指向的是安装目录
  （本机 = D:\项目\Mikasa）——两套文件。换图标/改代码后不覆盖安装目录，
  桌面看到的还是旧的。安装目录在本仓库之外，所以这一步交给用户自己双击。

  用法（双击 tools\apply_icon.bat 等价）：
      pwsh -File tools\apply_icon.ps1
      pwsh -File tools\apply_icon.ps1 -DryRun     # 只报告要做什么，不动任何文件
      pwsh -File tools\apply_icon.ps1 -IconOnly   # 只把 .ico 拷过去并换快捷方式图标

  安全性：只碰"快捷方式指向的那个 Mikasa 目录"和快捷方式本身；用户的数据库 /
  uploads / 索引都在 %LOCALAPPDATA%\Mikasa，全程不碰。
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$IconOnly
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$payload = Join-Path $root 'dist\Mikasa'
$payloadExe = Join-Path $payload 'Mikasa.exe'
$ico = Join-Path $root 'packaging\Mikasa.ico'

function Say([string]$msg, [string]$color = 'Gray') { Write-Host $msg -ForegroundColor $color }

if (-not (Test-Path -LiteralPath $ico)) {
    Say "找不到 packaging\Mikasa.ico —— 先跑：.venv\Scripts\python.exe tools\make_icon.py" Red
    exit 1
}
if (-not $IconOnly -and -not (Test-Path -LiteralPath $payloadExe)) {
    Say "找不到 dist\Mikasa\Mikasa.exe —— 先跑：.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\Mikasa.spec" Red
    exit 1
}

# ---- 找出所有指向 Mikasa.exe 的快捷方式（桌面可能是 OneDrive 重定向过的，
#      不能只问 [Environment]::GetFolderPath('Desktop')——本机它就不对） ----
function Get-CandidateDirs {
    $dirs = [System.Collections.Generic.List[string]]::new()
    foreach ($name in 'Desktop', 'CommonDesktopDirectory', 'Programs', 'CommonPrograms', 'StartMenu', 'CommonStartMenu') {
        $p = [Environment]::GetFolderPath($name)
        if ($p) { $dirs.Add($p) }
    }
    $dirs.Add($root)  # 仓库根那个 Mikasa.lnk
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue).Root) {
        foreach ($sub in 'Desktop', '桌面') { $dirs.Add((Join-Path $drive $sub)) }
        Get-ChildItem -LiteralPath $drive -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $top = $_.FullName
            foreach ($sub in 'Desktop', '桌面') { $dirs.Add((Join-Path $top $sub)) }
            $od = Join-Path $top 'OneDrive'
            if (Test-Path -LiteralPath $od) {
                foreach ($sub in 'Desktop', '桌面') { $dirs.Add((Join-Path $od $sub)) }
            }
        }
    }
    $dirs | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique
}

$shell = New-Object -ComObject WScript.Shell
$shortcuts = [System.Collections.Generic.List[object]]::new()
foreach ($dir in (Get-CandidateDirs)) {
    $depth = if ($dir -like '*Programs*') { 3 } else { 1 }
    Get-ChildItem -LiteralPath $dir -Filter *.lnk -Recurse -Depth $depth -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            try { $sc = $shell.CreateShortcut($_.FullName) } catch { return }
            if ($sc.TargetPath -and ($sc.TargetPath -split '[\\/]')[-1] -ieq 'Mikasa.exe') {
                $shortcuts.Add([pscustomobject]@{ Path = $_.FullName; Target = $sc.TargetPath })
            }
        }
}

if ($shortcuts.Count -eq 0) {
    Say "没找到任何指向 Mikasa.exe 的快捷方式。先跑一次安装程序建好快捷方式再试。" Yellow
    exit 1
}

# 用 @() 兜住空集合，并且不写 -ExpandProperty -Unique 那对组合：
# Windows PowerShell 5.1 下 "Select-Object -ExpandProperty X -Unique" 在集合为空时
# 返回 $null，后面 `$null | ForEach-Object {...}` 会照跑一遍并炸在 Split-Path 上。
$targets = @($shortcuts | ForEach-Object { $_.Target } | Select-Object -Unique)
Say "找到 $($shortcuts.Count) 个快捷方式，指向 $($targets.Count) 个目录：" Cyan
foreach ($t in $targets) { Say "    $t" }

$running = Get-Process -Name 'Mikasa' -ErrorAction SilentlyContinue
if ($running) {
    Say "Mikasa 正在运行，先关掉它（任务栏里退出 / 关掉它的窗口）再跑本脚本。" Red
    exit 1
}

foreach ($dir in @($targets | ForEach-Object { Split-Path -Parent $_ } | Select-Object -Unique)) {
    Say ""
    Say "==> $dir" Cyan
    if ($dir -eq $payload.TrimEnd('\')) {
        Say "    就是打包产物本身，跳过复制" DarkGray
    }
    elseif ($IconOnly) {
        Say "    只放图标：Mikasa.ico"
        if (-not $DryRun) { Copy-Item -LiteralPath $ico -Destination (Join-Path $dir 'Mikasa.ico') -Force }
    }
    else {
        Say "    覆盖程序文件（dist\Mikasa\* → 这里）"
        if (-not $DryRun) {
            try {
                Copy-Item -Path (Join-Path $payload '*') -Destination $dir -Recurse -Force
            }
            catch {
                Say "    复制失败：$($_.Exception.Message)" Red
                Say "    常见原因：Mikasa 还在运行（文件被占用），或这个目录需要管理员权限。" Yellow
                exit 1
            }
        }
    }
}

Say ""
Say "==> 刷新快捷方式图标" Cyan
foreach ($sc in $shortcuts) {
    $dir = Split-Path -Parent $sc.Target
    # 正常模式：新 exe 自带新图标，指向 exe 即可（和安装程序建出来的快捷方式一致）。
    # -IconOnly：exe 没换，只能指向旁边那份 .ico。
    $iconPath = if ($IconOnly) { "$(Join-Path $dir 'Mikasa.ico'),0" } else { "$($sc.Target),0" }
    Say "    $($sc.Path)"
    Say "        → $iconPath" DarkGray
    if (-not $DryRun) {
        $s = $shell.CreateShortcut($sc.Path)
        $s.IconLocation = $iconPath
        $s.Save()
    }
}

if (-not $DryRun) {
    # 不通知 shell 的话，资源管理器会用缓存的旧图标继续画
    Start-Process -FilePath "$env:SystemRoot\System32\ie4uinit.exe" -ArgumentList '-show' -WindowStyle Hidden -Wait
    Say ""
    Say "完成。桌面图标可能要按 F5 刷一下才变。" Green
}
else {
    Say ""
    Say "（-DryRun：什么都没有改动）" Yellow
}
