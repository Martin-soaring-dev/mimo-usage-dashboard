$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("mimo-install-smoke-" + [guid]::NewGuid().ToString("N"))
$configDir = Join-Path $temp "mimocode"
$config = Join-Path $configDir "mimocode.jsonc"
$installDir = Join-Path $temp "installed-dashboard"
$python = (Get-Command python -ErrorAction Stop).Source

try {
  New-Item -ItemType Directory -Force -Path $configDir | Out-Null
  @'
{
  // Preserve unrelated user settings.
  "command": {
    "existing-command": {
      "description": "must remain",
      "template": "unchanged"
    }
  },
  "plugin": ["example-user-plugin.mjs"]
}
'@ | Set-Content -LiteralPath $config -Encoding UTF8

  $legacy = Join-Path $configDir "skills\usage"
  New-Item -ItemType Directory -Force -Path $legacy | Out-Null
  "keep" | Set-Content -LiteralPath (Join-Path $legacy "keep.txt") -Encoding UTF8

  & (Join-Path $root "install.ps1") -Config $config -InstallDir $installDir -Python $python
  if ($LASTEXITCODE -ne 0) {
    throw "install.ps1 returned exit code $LASTEXITCODE"
  }

  foreach ($file in @("usage_cli.py", "register_command.py")) {
    if (-not (Test-Path -LiteralPath (Join-Path $installDir $file) -PathType Leaf)) {
      throw "Installed file missing: $file"
    }
  }

  $json = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
  if (-not $json.command."existing-command") {
    throw "Unrelated command was removed."
  }
  if ($json.plugin -notcontains "example-user-plugin.mjs") {
    throw "Unrelated plugin was removed."
  }
  if (-not $json.command.usage -or -not $json.command."usage-open") {
    throw "Expected usage commands were not registered."
  }
  foreach ($name in @("usage", "usage-open")) {
    $template = $json.command.$name.template
    if ($template.Contains('$ARGUMENTS') -or $template.Contains('--arg-string')) {
      throw "$name template unexpectedly forwards user arguments into shell."
    }
  }
  if (-not (Test-Path -LiteralPath (Join-Path $legacy "keep.txt"))) {
    throw "Legacy files were unexpectedly deleted by default."
  }
  $backups = @(Get-ChildItem -LiteralPath $configDir -Filter "mimocode.jsonc.bak-mimo-usage-dashboard-*")
  if ($backups.Count -lt 1) {
    throw "Expected timestamped configuration backup was not created."
  }

  Write-Host "WINDOWS INSTALL SMOKE TEST OK" -ForegroundColor Green
} finally {
  if (Test-Path -LiteralPath $temp) {
    Remove-Item -LiteralPath $temp -Recurse -Force
  }
}
