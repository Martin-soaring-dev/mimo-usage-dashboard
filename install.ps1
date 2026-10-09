# Install MiMo Desktop Usage Dashboard (dashboard + plugin + skill)
# Usage:  powershell -ExecutionPolicy Bypass -File install.ps1
[CmdletBinding()]
param(
  [int]$Port = 8790
)

$ErrorActionPreference = "Stop"
$src  = $PSScriptRoot
$mimo = Join-Path $env:USERPROFILE ".config\mimocode"

Write-Host "== MiMo Desktop Usage Dashboard - install ==" -ForegroundColor Cyan

# 1) locate Node (needs >= 22 for node:sqlite)
function Resolve-Node {
  $cands = @()
  $cmd = Get-Command node -ErrorAction SilentlyContinue
  if ($cmd) { $cands += $cmd.Source }
  $cands += @(
    "C:\Program Files\nodejs\node.exe",
    (Join-Path $env:LOCALAPPDATA "Programs\nodejs\node.exe"),
    (Join-Path $env:USERPROFILE ".bun\bin\node.exe")
  )
  foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path } }
  return $null
}

$node = Resolve-Node
if (-not $node) {
  Write-Warning "node.exe not found. Install Node.js 22+ or set MIMO_USAGE_NODE to node.exe."
  $node = "node"
} else {
  try {
    $ver = (& $node --version) -replace '^v', ''
    $major = [int]($ver.Split('.')[0])
    Write-Host "Node: $node (v$ver)"
    if ($major -lt 22) { Write-Warning "Node $ver is too old; node:sqlite needs 22+. The dashboard may fail to read the database." }
  } catch { Write-Warning "Could not read node version." }
}

# 2) copy dashboard code
$dash = Join-Path $mimo "usage-dashboard"
New-Item -ItemType Directory -Force -Path $dash | Out-Null
Copy-Item (Join-Path $src "usage-core.mjs") $dash -Force
Copy-Item (Join-Path $src "server.mjs")     $dash -Force
Copy-Item (Join-Path $src "dashboard.html") $dash -Force
Write-Host "dashboard -> $dash"

# 3) write config (no BOM: some readers choke on a leading U+FEFF)
$cfg  = Join-Path $mimo "usage-dashboard.config.json"
$json = [ordered]@{ dashboardDir = $dash; nodePath = $node; port = $Port } | ConvertTo-Json
[System.IO.File]::WriteAllText($cfg, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "config    -> $cfg"

# 4) install plugin + register it in the user config
#    (the engine only globs "{plugin,plugins}/*.{ts,js}" in the plugin dir, so a .mjs
#     must be registered via the config `plugin` array - same as the app's own plugin)
$plugDir = Join-Path $mimo "plugin"
New-Item -ItemType Directory -Force -Path $plugDir | Out-Null
$pluginFile = Join-Path $plugDir "usage-dashboard.mjs"
Copy-Item (Join-Path $src "plugin\usage-dashboard.mjs") $pluginFile -Force
Write-Host "plugin    -> $pluginFile"

$uri     = ([uri]$pluginFile).AbsoluteUri
$cfgUser = Join-Path $mimo "mimocode.jsonc"
if (Test-Path $cfgUser) {
  try {
    $res = & $node (Join-Path $src "register-plugin.mjs") $cfgUser $uri
    Write-Host "registered -> $cfgUser  [$res]"
  } catch { Write-Warning "Registering the plugin failed: $_" }
} else {
  Write-Warning "Config not found: $cfgUser"
  Write-Warning "Add this to its 'plugin' array manually: $uri"
}

# 5) install skill (shows up on the Plugins page)
$skillDst = Join-Path $mimo "skills\mimo-usage-dashboard"
New-Item -ItemType Directory -Force -Path (Join-Path $skillDst "locales") | Out-Null
Copy-Item (Join-Path $src "skill\SKILL.md") $skillDst -Force
Copy-Item (Join-Path $src "skill\locales\*.json") (Join-Path $skillDst "locales") -Force
Write-Host "skill     -> $skillDst"

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Start dashboard : node `"$dash\server.mjs`" --port $Port --open"
Write-Host "  Or double-click : $(Join-Path $src 'start-dashboard.cmd')"
Write-Host "  In MiMo Desktop : open a NEW session and say 'show my usage' / 'open the usage dashboard'."
Write-Host "  URL             : http://127.0.0.1:$Port"
Write-Host ""
Write-Host "Note: plugin/skill take effect in a NEW session." -ForegroundColor Yellow
