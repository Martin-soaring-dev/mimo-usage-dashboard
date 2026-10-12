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

  # --- 失败必须中止：不能静默回退、也不能谎报成功 ---
  function Test-InstallFails {
    param([hashtable]$InstallArgs)
    try {
      & (Join-Path $root "install.ps1") @InstallArgs
      return $false
    } catch {
      return $true
    }
  }

  # 1) -Python 指向不存在的文件：不得回退到其它解释器
  $missingPython = Join-Path $temp "no-such-python.exe"
  if (-not (Test-InstallFails @{ Config = $config; InstallDir = $installDir; Python = $missingPython })) {
    throw "install.ps1 did not fail on a non-existent -Python path."
  }

  # 2) -Python 指向非 Python 可执行文件：版本校验必须拒绝
  $notPython = (Get-Command cmd.exe -ErrorAction Stop).Source
  if (-not (Test-InstallFails @{ Config = $config; InstallDir = $installDir; Python = $notPython })) {
    throw "install.ps1 did not fail when -Python is not a Python interpreter."
  }

  # 3) 配置文件不存在：必须失败
  $missingConfig = Join-Path $temp "no-such-dir\mimocode.jsonc"
  if (-not (Test-InstallFails @{ Config = $missingConfig; InstallDir = $installDir; Python = $python })) {
    throw "install.ps1 did not fail on a missing configuration file."
  }

  # 4) 注册失败（配置不是合法 JSON）：必须中止，不得输出成功
  $badDir = Join-Path $temp "bad-config"
  New-Item -ItemType Directory -Force -Path $badDir | Out-Null
  $badConfig = Join-Path $badDir "mimocode.jsonc"
  '{ this is not json' | Set-Content -LiteralPath $badConfig -Encoding UTF8
  $badBefore = Get-Content -LiteralPath $badConfig -Raw
  if (-not (Test-InstallFails @{ Config = $badConfig; InstallDir = (Join-Path $temp "bad-install"); Python = $python })) {
    throw "install.ps1 did not abort when command registration failed."
  }
  if ((Get-Content -LiteralPath $badConfig -Raw) -ne $badBefore) {
    throw "A failing registration modified the configuration file."
  }

  # 5) 同秒重复安装：备份文件名必须唯一，不能覆盖
  $uniqDir = Join-Path $temp "unique-backup"
  New-Item -ItemType Directory -Force -Path $uniqDir | Out-Null
  $uniqConfig = Join-Path $uniqDir "mimocode.jsonc"
  '{ "command": {} }' | Set-Content -LiteralPath $uniqConfig -Encoding UTF8
  & (Join-Path $root "install.ps1") -Config $uniqConfig -InstallDir (Join-Path $temp "uniq-install") -Python $python
  # 复位成“需要改动”的状态，强制第二次真正写盘（尽量落在同一秒内）
  '{ "command": {} }' | Set-Content -LiteralPath $uniqConfig -Encoding UTF8
  & (Join-Path $root "install.ps1") -Config $uniqConfig -InstallDir (Join-Path $temp "uniq-install") -Python $python
  $uniqBackups = @(Get-ChildItem -LiteralPath $uniqDir -Filter "mimocode.jsonc.bak-mimo-usage-dashboard-*")
  if ($uniqBackups.Count -ne 2) {
    throw "Expected 2 unique backups after two installs, found $($uniqBackups.Count)."
  }
  $uniqNames = @($uniqBackups | Select-Object -ExpandProperty Name)
  if (($uniqNames | Sort-Object -Unique).Count -ne 2) {
    throw "Backup file names collided: $($uniqNames -join ', ')"
  }

  Write-Host "WINDOWS INSTALL SMOKE TEST OK" -ForegroundColor Green
} finally {
  if (Test-Path -LiteralPath $temp) {
    Remove-Item -LiteralPath $temp -Recurse -Force
  }
}
