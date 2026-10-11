# Install MiMo Desktop Usage Dashboard (Python CLI + /usage command)
# Usage:  powershell -ExecutionPolicy Bypass -File install.ps1
#
# 本脚本做两件事：
#   1) 把 usage_cli.py / register_command.py 拷到 ~/.config/mimocode/usage-dashboard/
#   2) 往 mimocode.jsonc 的 `command` 字段写入 /usage 与 /usage-open
#
# 不安装插件、不启动服务、不监听端口。
[CmdletBinding()]
param(
  # 显式指定 python.exe；不传则自动探测
  [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$src  = $PSScriptRoot
$mimo = Join-Path $env:USERPROFILE ".config\mimocode"
$dest = Join-Path $mimo "usage-dashboard"

Write-Host "== MiMo Desktop Usage Dashboard - install ==" -ForegroundColor Cyan

# 1) locate Python (>= 3.10; 只用标准库)
function Resolve-Python {
  param([string]$Hint)
  $cands = @()
  if ($Hint) { $cands += $Hint }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { $cands += $cmd.Source }
  $cmd3 = Get-Command python3 -ErrorAction SilentlyContinue
  if ($cmd3) { $cands += $cmd3.Source }
  $cands += @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    "C:\Python313\python.exe",
    "C:\Python312\python.exe"
  )
  foreach ($c in $cands) {
    if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path }
  }
  return $null
}

$py = Resolve-Python -Hint $Python
if (-not $py) {
  Write-Warning "python.exe not found. Install Python 3.10+ and re-run, or pass -Python <path>."
  $py = "python"
} else {
  try {
    $ver = (& $py --version) 2>&1
    Write-Host "Python: $py ($ver)"
  } catch { Write-Warning "Could not read python version." }
}

# 2) copy the CLI
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $src "usage_cli.py")        $dest -Force
Copy-Item (Join-Path $src "register_command.py") $dest -Force
Write-Host "cli       -> $dest"

# 3) register the /usage command into mimocode.jsonc (JSONC-aware)
$cfg = Join-Path $mimo "mimocode.jsonc"
if (Test-Path $cfg) {
  try {
    $out = & $py (Join-Path $dest "register_command.py") --config $cfg `
              --python $py --script (Join-Path $dest "usage_cli.py")
    Write-Host "command   -> $cfg  [$out]"
  } catch {
    Write-Warning "Registering the command failed: $_"
  }
} else {
  Write-Warning "Config not found: $cfg"
  Write-Warning "Create it, then re-run this script."
}

# 4) remove any legacy skill left over from the old (AI-routed) design
foreach ($legacy in @("skills\usage", "skills\mimo-usage-dashboard", "usage-dashboard.config.json",
                      "plugin\usage-dashboard.mjs")) {
  $p = Join-Path $mimo $legacy
  if (Test-Path $p) {
    Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "removed legacy -> $p" -ForegroundColor DarkYellow
  }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Try it now   : & `"$py`" `"$dest\usage_cli.py`" --open"
Write-Host "  In MiMo      : open a NEW session and type /usage   (or /usage token, /usage session, /usage context)"
Write-Host "  Uninstall    : & `"$py`" `"$dest\register_command.py`" --uninstall"
Write-Host ""
Write-Host "Note: /usage takes effect in a NEW session (the engine builds the command table at session start)." -ForegroundColor Yellow
