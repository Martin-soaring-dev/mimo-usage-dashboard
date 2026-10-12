# Install MiMo Desktop Usage Dashboard (Python CLI + /usage command)
# Usage:  powershell -ExecutionPolicy Bypass -File install.ps1 [-Python <exe>] [-Config <jsonc>] [-InstallDir <dir>] [-RemoveLegacy]
#
# 本脚本做两件事：
#   1) 把 usage_cli.py / register_command.py 拷到 ~/.config/mimocode/usage-dashboard/
#   2) 往 mimocode.jsonc 的 `command` 字段写入 /usage 与 /usage-open
#
# 不安装插件、不启动服务、不监听端口。
#
# 失败即中止（不会静默降级）：
#   * -Python 显式指定时，路径不存在或版本 < 3.10 直接报错，不会回退到别的解释器；
#   * 自动探测到的解释器也必须版本 >= 3.10；
#   * 注册命令返回非零退出码时，脚本立即中止，不打印成功信息。
# -RemoveLegacy 是唯一会删除旧文件的开关；默认保留旧文件与旧插件引用。
[CmdletBinding()]
param(
  # 显式指定 python.exe；不传则自动探测
  [string]$Python = "",
  # 实际生效的 MiMo 配置文件；不传则使用默认路径
  [string]$Config = "",
  # 可选的脚本安装目录；不传则放在配置目录下的 usage-dashboard
  [string]$InstallDir = "",
  # 只有明确传入时才清理旧版遗留文件
  [switch]$RemoveLegacy
)

$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$cfg = if ($Config) { [System.IO.Path]::GetFullPath($Config) } else { Join-Path $env:USERPROFILE ".config\mimocode\mimocode.jsonc" }
if ($Config -and -not (Test-Path -LiteralPath $cfg -PathType Leaf)) {
  throw "指定的 MiMo 配置文件不存在：$cfg"
}
$mimo = Split-Path -Parent $cfg
$dest = if ($InstallDir) { [System.IO.Path]::GetFullPath($InstallDir) } else { Join-Path $mimo "usage-dashboard" }

Write-Host "== MiMo Desktop Usage Dashboard - install ==" -ForegroundColor Cyan

# 配置文件必须存在：否则注册一定失败，不能先复制文件再谎报成功。
if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) {
  throw "MiMo 配置文件不存在：$cfg（请先创建配置，或用 -Config <path> 指定）"
}

# 1) locate Python (>= 3.10; 只用标准库)
$MinimumPython = [version]"3.10"

function Get-PythonVersion {
  param([string]$Path)
  if (-not $Path) { return $null }
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
  try {
    $raw = (& $Path --version 2>&1 | Out-String)
  } catch {
    return $null
  }
  if ($raw -match 'Python\s+(\d+)\.(\d+)') {
    return [version]("$($Matches[1]).$($Matches[2])")
  }
  return $null
}

function Test-SupportedPython {
  param([string]$Path)
  $v = Get-PythonVersion -Path $Path
  if ($null -eq $v) { return $false }
  return ($v -ge $MinimumPython)
}

# 自动探测：每个候选都必须通过版本校验，不达标就继续找下一个。
function Resolve-SupportedPython {
  $cands = @()
  foreach ($name in @("python", "python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { $cands += $cmd.Source }
  }
  $cands += @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    "C:\Python313\python.exe",
    "C:\Python312\python.exe"
  )
  foreach ($c in $cands) {
    if (-not $c) { continue }
    if (Test-SupportedPython -Path $c) { return (Resolve-Path -LiteralPath $c).Path }
    $v = Get-PythonVersion -Path $c
    if ($v) {
      Write-Host "skip      -> $c ($v < $MinimumPython)" -ForegroundColor DarkGray
    } elseif (Test-Path -LiteralPath $c -PathType Leaf) {
      Write-Host "skip      -> $c (无法识别为 Python)" -ForegroundColor DarkGray
    }
  }
  return $null
}

if ($Python) {
  # 显式指定就绝不回退：路径不存在或版本不达标一律失败。
  if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "指定的 -Python 不存在：$Python（不会自动回退到其它解释器）"
  }
  if (-not (Test-SupportedPython -Path $Python)) {
    $v = Get-PythonVersion -Path $Python
    throw "指定的 -Python 不受支持：$Python（探测到 '$v'，需要 >= $MinimumPython）"
  }
  $py = (Resolve-Path -LiteralPath $Python).Path
} else {
  $py = Resolve-SupportedPython
  if (-not $py) {
    throw "未找到满足 >= $MinimumPython 的 python.exe。请安装 Python 3.10+，或用 -Python <path> 显式指定。"
  }
}
Write-Host "Python    : $py ($(Get-PythonVersion -Path $py))"

# 2) copy the CLI
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $src "usage_cli.py")        $dest -Force
Copy-Item (Join-Path $src "register_command.py") $dest -Force
Write-Host "cli       -> $dest"

# 3) register the /usage command into mimocode.jsonc (JSONC-aware)
$regArgs = @(
  "--config", $cfg,
  "--python", $py,
  "--script", (Join-Path $dest "usage_cli.py")
)
if ($RemoveLegacy) { $regArgs += "--purge-legacy-plugin" }
$out = & $py (Join-Path $dest "register_command.py") @regArgs
$regCode = $LASTEXITCODE
# 0 = 本次写入成功；1 = 已是最新（无变化）；其余退出码一律中止安装。
if ($regCode -ne 0 -and $regCode -ne 1) {
  throw "注册 /usage 命令失败（register_command.py 退出码 $regCode）：$out"
}
if ($regCode -eq 1) {
  Write-Host "command   -> $cfg  [already-registered]"
} else {
  Write-Host "command   -> $cfg  [$out]"
}

# 4) Legacy cleanup is opt-in; preserve user files by default.
if ($RemoveLegacy) {
  foreach ($legacy in @("skills\usage", "skills\mimo-usage-dashboard", "usage-dashboard.config.json",
                        "plugin\usage-dashboard.mjs")) {
    $p = Join-Path $mimo $legacy
    if (Test-Path $p) {
      Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue
      Write-Host "removed legacy -> $p" -ForegroundColor DarkYellow
    }
  }
} else {
  Write-Host "Legacy files preserved (pass -RemoveLegacy only after reviewing them)." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Try it now   : & `"$py`" `"$dest\usage_cli.py`" --open"
Write-Host "  In MiMo      : open a NEW session and type /usage   (or /usage-open)"
Write-Host "  With args    : the registered command never forwards arguments; run the CLI directly,"
Write-Host "                 e.g. & `"$py`" `"$dest\usage_cli.py`" token --days 7"
Write-Host "  Python       : $py"
Write-Host "  Config       : $cfg"
Write-Host "  Uninstall    : & `"$py`" `"$dest\register_command.py`" --config `"$cfg`" --uninstall"
Write-Host ""
Write-Host "Note: /usage takes effect in a NEW session (the engine builds the command table at session start)." -ForegroundColor Yellow
