# MiMo Desktop Usage Dashboard — 可靠升级器（Release Hardening）
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Python <exe>] [-Config <jsonc>]
#                                                       [-InstallDir <dir>] [-RemoveLegacy] [-Force]
#                                                       [-PreflightOnly]
#
# 做两件事：
#   1) 把 usage_cli.py / register_command.py 安装到 <InstallDir>（默认 <mimocode 配置目录>\usage-dashboard）
#   2) 往 mimocode.jsonc 的 `command` 字段写入 /usage 与 /usage-open（**最小范围就地修改**）
#
# 本脚本是**事务式**的：Preflight → Backup → Stage → Validate → Apply → Verify。
# 任何阶段失败都会尽量回滚本次改动，并给出明确的人工恢复指引。
#
# 设计原则（与"一次性复制脚本"的关键区别）：
#   * 安装前先检测既有状态；发现旧安装进入 upgrade/adopt 模式，**绝不直接覆盖**。
#   * 用 install-manifest.json（版本 + 每个文件的 sha256）区分"本项目文件"与"用户未知文件"；
#     **没有 manifest 时只依据文件内的归属标记判断，绝不按文件名认领**。
#   * 归属不明 / 已被修改的文件默认拒绝覆盖，必须显式 -Force。
#   * 运行中的本项目服务：先做身份校验（token 合法 + port 合法 + loopback + /api/report
#     返回本项目格式），通过后用本项目协议发 shutdown 并等待退出；
#     **绝不 taskkill、绝不按端口或未知 PID 杀进程**。
#   * 不删除未知文件；不清理用户目录；html 报告 / bak 备份 / 数据库一律保留。
#
# 失败即中止（不会静默降级）：
#   * -Python 显式指定时，路径不存在或版本 < 3.10 直接报错，不会回退到别的解释器；
#   * 自动探测到的解释器也必须版本 >= 3.10；
#   * 配置无法安全就地修改（重复键 / 非对象 / 无法定位区域）时 fail closed，什么都不写；
#   * 注册返回非预期退出码时立即中止并回滚。
# -RemoveLegacy 是唯一会删除旧文件的开关；默认保留旧文件与旧插件引用；
# 且它只删除明确属于本项目的文件；非空目录必须再加 -Force 才会递归删除。

[CmdletBinding()]
param(
  # 显式指定 python.exe；不传则自动探测
  [string]$Python = "",
  # 实际生效的 MiMo 配置文件；不传则使用默认路径
  [string]$Config = "",
  # 可选的脚本安装目录；不传则放在配置目录下的 usage-dashboard
  [string]$InstallDir = "",
  # 只有明确传入时才清理旧版遗留文件
  [switch]$RemoveLegacy,
  # 允许覆盖"归属不明"或"已被用户修改"的文件（安装前仍会备份）
  [switch]$Force,
  # 只跑安装前检测并打印结论，不写任何文件
  [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"

$src = $PSScriptRoot
$Product = "mimo-usage-dashboard"
$ManagedFiles = @("usage_cli.py", "register_command.py")
$ManifestName = "install-manifest.json"
$ProductMarker = "mimo-usage-dashboard:managed-file:v1"

$cfg = if ($Config) { [System.IO.Path]::GetFullPath($Config) } else { Join-Path $env:USERPROFILE ".config\mimocode\mimocode.jsonc" }
$mimo = Split-Path -Parent $cfg
$dest = if ($InstallDir) { [System.IO.Path]::GetFullPath($InstallDir) } else { Join-Path $mimo "usage-dashboard" }
$manifestPath = Join-Path $dest $ManifestName

Write-Host "== MiMo Desktop Usage Dashboard - install ==" -ForegroundColor Cyan
Write-Host "config    : $cfg"
Write-Host "installDir: $dest"

# ---------------------------------------------------------------------------
# 帮助函数
# ---------------------------------------------------------------------------

function Get-Sha256 {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# 把 PowerShell 对 native stderr 的渲染文本收敛成可读的诊断文字。
# PowerShell 5.1 会把子进程 stderr 包装成 ErrorRecord（含 "At line ..."、"CategoryInfo" 等
# 框架行），并且在本脚本的 ErrorActionPreference=Stop 下**直接把脚本中断**。
# 因此这里：调用期间临时降为 Continue，只认退出码 + stdout，stderr 仅作展示用。
function Get-DiagText {
  param([string]$Raw, [string]$ExeName)
  if (-not $Raw) { return "" }
  $lines = @($Raw -split "`r?`n" | Where-Object {
    $_ -match "\S" -and
    $_ -notmatch "^\s*\+" -and
    $_ -notmatch "^At " -and
    $_ -notmatch "CategoryInfo" -and
    $_ -notmatch "FullyQualifiedErrorId" -and
    $_ -notmatch "^\s*$"
  })
  $text = ($lines -join " | ").Trim()
  if ($ExeName) {
    $text = $text -replace ("^" + [regex]::Escape($ExeName) + "\s*:\s*"), ""
  }
  return $text
}

# 执行 python 并取回 stdout / 退出码 / 诊断文本。
# 决策只依赖**退出码与 stdout**（另有 --journal / --preflight 的结构化输出），
# stderr 只用于把原因展示给用户，绝不参与判定。
function Invoke-Python {
  param([string]$Exe, [string[]]$Arguments)
  $errFile = Join-Path ([System.IO.Path]::GetTempPath()) ("mimo-py-err-" + [guid]::NewGuid().ToString("N") + ".txt")
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $stdout = & $Exe @Arguments 2>$errFile
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $saved
  }
  $rawErr = ""
  if (Test-Path -LiteralPath $errFile) {
    try { $rawErr = (Get-Content -LiteralPath $errFile -Raw) } catch { $rawErr = "" }
    Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
  }
  $exeName = Split-Path -Leaf $Exe
  return [pscustomobject]@{
    Code = $code
    Out  = (($stdout | Out-String).TrimEnd())
    Err  = (Get-DiagText -Raw $rawErr -ExeName $exeName)
  }
}

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

function Test-DirWritable {
  param([string]$Dir)
  try {
    if (-not (Test-Path -LiteralPath $Dir)) {
      New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    }
    $probe = Join-Path $Dir (".mimo-write-probe-" + [guid]::NewGuid().ToString("N"))
    [System.IO.File]::WriteAllText($probe, "probe")
    Remove-Item -LiteralPath $probe -Force
    return $true
  } catch {
    return $false
  }
}

function Get-ProductVersion {
  $pkg = Join-Path $src "package.json"
  if (Test-Path -LiteralPath $pkg) {
    try {
      $v = (Get-Content -LiteralPath $pkg -Raw | ConvertFrom-Json).version
      if ($v) { return [string]$v }
    } catch { }
  }
  return "0.0.0"
}

function Write-AtomicText {
  param([string]$Path, [string]$Text)
  $dir = Split-Path -Parent $Path
  $tmp = Join-Path $dir (".mimo-atomic-" + [guid]::NewGuid().ToString("N") + ".tmp")
  try {
    [System.IO.File]::WriteAllText($tmp, $Text, (New-Object System.Text.UTF8Encoding($true)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
  } catch {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    throw
  }
}

function Write-Manifest {
  param([string]$Path, [string]$Version, [string]$CfgPath, [string]$PyExe, [hashtable]$Hashes)
  $files = [ordered]@{}
  foreach ($name in ($Hashes.Keys | Sort-Object)) {
    $p = Join-Path $dest $name
    $size = if (Test-Path -LiteralPath $p) { (Get-Item -LiteralPath $p).Length } else { 0 }
    $files[$name] = [ordered]@{ sha256 = $Hashes[$name]; bytes = $size }
  }
  $payload = [ordered]@{
    product     = $Product
    version     = $Version
    installedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    python      = $PyExe
    config      = $CfgPath
    files       = $files
  }
  Write-AtomicText -Path $Path -Text (($payload | ConvertTo-Json -Depth 6) + "`n")
}

# ---------------------------------------------------------------------------
# 回滚：只还原"本次安装动过的东西"，且只在当前内容仍是本次写入的内容时才动
# ---------------------------------------------------------------------------

function Invoke-Rollback {
  param([pscustomobject]$Ctx)
  Write-Host ""
  Write-Host "  [rollback] 只回滚本次安装动过的文件；若内容已被改动则不覆盖（保护你的新修改）" -ForegroundColor DarkYellow
  $manual = @()

  # 1) 配置文件
  if ($Ctx.ConfigJournal -and (Test-Path -LiteralPath $Ctx.ConfigJournal -PathType Leaf)) {
    $cj = $null
    try { $cj = (Get-Content -LiteralPath $Ctx.ConfigJournal -Raw | ConvertFrom-Json) } catch { $cj = $null }
    if ($null -ne $cj -and $cj.changed) {
      $cur = Get-Sha256 -Path $Ctx.Config
      if ($cur -eq $cj.postSha256) {
        if ($cj.backupPath -and (Test-Path -LiteralPath $cj.backupPath -PathType Leaf)) {
          try {
            Copy-Item -LiteralPath $cj.backupPath -Destination $Ctx.Config -Force
            Write-Host "  [rollback] 配置已逐字节还原：$($Ctx.Config)"
          } catch {
            $manual += "配置还原失败，请手工复制：$($cj.backupPath) -> $($Ctx.Config)"
          }
        } else {
          $manual += "找不到配置备份，请人工检查：$($Ctx.Config)"
        }
      } else {
        Write-Host "  [rollback] 配置在本次安装后又被改动过，未自动还原（避免覆盖你的新修改）" -ForegroundColor Yellow
        $manual += "如需恢复安装前的配置：$($cj.backupPath) -> $($Ctx.Config)"
      }
    }
  }

  # 2) 程序文件
  foreach ($name in $ManagedFiles) {
    $target = Join-Path $Ctx.InstallDir $name
    $backup = Join-Path $Ctx.BackupDir $name
    $expected = $Ctx.Applied[$name]
    $exists = Test-Path -LiteralPath $target -PathType Leaf
    if ($Ctx.Overwritten -contains $name) {
      if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) {
        $manual += "$name 缺少备份，请人工恢复"
        continue
      }
      if ($exists -and $expected -and (Get-Sha256 -Path $target) -eq $expected) {
        try {
          Move-Item -LiteralPath $backup -Destination $target -Force
          Write-Host "  [rollback] $name 已还原为安装前版本"
        } catch {
          $manual += "$name 还原失败，备份在：$backup"
        }
      } else {
        Write-Host "  [rollback] $name 在安装后又被改动过，未自动还原" -ForegroundColor Yellow
        $manual += "如需恢复：$backup -> $target"
      }
    } elseif ($Ctx.Created -contains $name) {
      if ($exists) {
        if ($expected -and (Get-Sha256 -Path $target) -eq $expected) {
          Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
          Write-Host "  [rollback] $name 是本次新建的，已移除"
        } else {
          Write-Host "  [rollback] $name 是本次新建但随后被改动，保留不删" -ForegroundColor Yellow
          $manual += "请人工确认是否删除：$target"
        }
      }
    }
  }

  # 3) manifest
  $mb = Join-Path $Ctx.BackupDir $ManifestName
  if ($Ctx.ManifestExisted) {
    if (Test-Path -LiteralPath $mb -PathType Leaf) {
      try {
        Copy-Item -LiteralPath $mb -Destination $Ctx.ManifestPath -Force
        Write-Host "  [rollback] manifest 已还原为安装前版本"
      } catch {
        $manual += "manifest 还原失败，备份在：$mb"
      }
    }
  } elseif (Test-Path -LiteralPath $Ctx.ManifestPath -PathType Leaf) {
    Remove-Item -LiteralPath $Ctx.ManifestPath -Force -ErrorAction SilentlyContinue
    Write-Host "  [rollback] manifest 是本次新建的，已移除"
  }

  # 4) 暂存目录（备份目录一律保留）
  if (Test-Path -LiteralPath $Ctx.StageDir) {
    Remove-Item -LiteralPath $Ctx.StageDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  [rollback] 已清理暂存目录"
  }

  if ($manual.Count -gt 0) {
    Write-Host "  [rollback] 以下项目无法自动恢复，需人工处理：" -ForegroundColor Red
    foreach ($m in $manual) { Write-Host "              - $m" -ForegroundColor Red }
  } else {
    Write-Host "  [rollback] 本次改动已全部还原。" -ForegroundColor DarkYellow
  }
  Write-Host "  [rollback] 安装前的文件副本保留在：$($Ctx.BackupDir)" -ForegroundColor DarkYellow
}


# ---------------------------------------------------------------------------
# 1) 定位 Python（>= 3.10；只用标准库）
# ---------------------------------------------------------------------------

$MinimumPython = [version]"3.10"

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
$version = Get-ProductVersion

# ---------------------------------------------------------------------------
# 2) Preflight（只读检测；只允许"创建目标目录 + 写权限探测文件"这两种无害写入）
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "-- Preflight --" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) {
  throw "MiMo 配置文件不存在：$cfg（请先创建配置，或用 -Config <path> 指定）"
}

$rcSrc = Join-Path $src "register_command.py"
if (-not (Test-Path -LiteralPath $rcSrc -PathType Leaf)) {
  throw "缺少 register_command.py：$rcSrc"
}

# 2a. 配置形态 / 既有安装归属
$pfArgs = @($rcSrc, "--preflight", "--config", $cfg, "--install-dir", $dest,
            "--python", $py, "--script", (Join-Path $dest "usage_cli.py"))
if ($Force) { $pfArgs += "--force" }
$pfRun = Invoke-Python -Exe $py -Arguments $pfArgs
$report = $null
try { $report = ($pfRun.Out | ConvertFrom-Json) } catch { $report = $null }

if ($pfRun.Code -eq 4) { throw "MiMo 配置文件不存在：$cfg" }
if ($pfRun.Code -eq 3) { throw "配置文件无法解析（JSONC 结构不可安全修改）：$($pfRun.Err)" }
if ($pfRun.Code -eq 6) { throw "配置文件无法安全就地修改，已 fail closed（未写入任何内容）：$($pfRun.Err)" }
if ($pfRun.Code -eq 7) {
  Write-Host "  检测到需要人工确认的既有状态：" -ForegroundColor Yellow
  foreach ($r in $report.forceReasons) { Write-Host "    - $r" -ForegroundColor Yellow }
  throw "安装已中止。请先确认这些文件/命令确实属于本项目，再显式加 -Force 重跑。"
}
if ($pfRun.Code -ne 0 -or $null -eq $report) {
  throw "preflight 失败（退出码 $($pfRun.Code)）：$($pfRun.Err)"
}

$modeText = switch ($report.mode) {
  "adopt"   { "adopt（无 manifest 但有本项目标记：备份后接管）" }
  "upgrade" { "upgrade（检测到既有安装目录，将先备份再替换）" }
  default   { "fresh（全新安装）" }
}
Write-Host "  mode        : $modeText"
Write-Host "  归属已证明  : $($report.ownershipProven)$(if (-not $report.ownershipProven -and $report.mode -ne 'fresh') { '  ← 既有文件无法证明归属，需 -Force' })"
Write-Host "  配置形态    : $($report.configShape)"
Write-Host "  既有命令    : usage=$($report.hasCommandUsage) usage-open=$($report.hasCommandUsageOpen)"
Write-Host "  管理文件    : $(( $report.managedFiles.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" } ) -join ', ')"
if ($report.manifestPresent) {
  Write-Host "  已安装版本  : $($report.manifest.version)"
} else {
  Write-Host "  manifest    : 不存在（旧文件只能检测，不会被删除；归属不明者需 -Force）" -ForegroundColor DarkGray
}

# 2b. 目标目录写权限
if (-not (Test-DirWritable -Dir $dest)) {
  throw "安装目录不可写：$dest（请检查权限或以合适的身份重跑）"
}
Write-Host "  目录可写    : $dest"

# 2b-1. 清理**本项目自己的、已失效的**暂存目录（安装中断留下的残留）。
#        规则刻意收得很紧：只认本项目命名前缀 .mimo-usage-stage-<yyyyMMdd-HHmmss>，
#        且时间戳早于 1 小时（保证不会碰到并发安装正在使用的目录）。
#        绝不按文件名删除任何其它东西。
$staleCutoff = (Get-Date).AddHours(-1)
$removedStale = 0
foreach ($dir in @(Get-ChildItem -LiteralPath $dest -Directory -Force -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -like ".mimo-usage-stage-*" })) {
  $stampText = $dir.Name.Substring(".mimo-usage-stage-".Length)
  $parsed = [datetime]::MinValue
  $ok = [datetime]::TryParseExact($stampText, "yyyyMMdd-HHmmss", $null,
                                  [System.Globalization.DateTimeStyles]::None, [ref]$parsed)
  if (-not $ok) {
    Write-Host "  cleanup     : 保留 $($dir.Name)（名称不符合本项目暂存目录格式，归属不明）" -ForegroundColor DarkGray
    continue
  }
  if ($parsed -lt $staleCutoff) {
    Remove-Item -LiteralPath $dir.FullName -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $dir.FullName)) {
      $removedStale++
      Write-Host "  cleanup     : 已清理失效暂存目录 $($dir.Name)"
    }
  } else {
    Write-Host "  cleanup     : 保留 $($dir.Name)（时间很新，可能是并发安装在使用）" -ForegroundColor DarkGray
  }
}
# 2b-2. 是否已经就是本次要安装的内容（文件哈希 + 命令模板都一致）
#        一致时没有任何可写的东西，于是不建备份、不做事务，避免备份目录无界增长。
$filesCurrent = $true
foreach ($name in $ManagedFiles) {
  if ((Get-Sha256 -Path (Join-Path $dest $name)) -ne (Get-Sha256 -Path (Join-Path $src $name))) {
    $filesCurrent = $false
  }
}
$alreadyCurrent = ($filesCurrent -and $report.upToDate)
Write-Host "  文件已是最新: $filesCurrent ; 命令已是最新: $($report.upToDate)"

# 2c. 运行中的本项目服务：身份校验 + 优雅关闭（绝不强杀）
$svcRun = Invoke-Python -Exe $py -Arguments @((Join-Path $src "usage_cli.py"), "service-check")
if ($svcRun.Code -eq 0) {
  $svc = $null
  try { $svc = ($svcRun.Out | ConvertFrom-Json) } catch { $svc = $null }
  if ($null -eq $svc) {
    Write-Host "  服务检测    : 无法解析 service-check 输出，按'无服务'处理" -ForegroundColor DarkYellow
  } elseif (-not $svc.statePresent) {
    Write-Host "  服务检测    : 无运行中的本项目服务"
  } elseif ($svc.stateValid -and $svc.reachable -and $svc.owned) {
    Write-Host "  服务检测    : 检测到身份确认的本项目服务（port=$($svc.port)，token=$($svc.tokenPrefix)…），正在优雅关闭并等待退出…"
    $sdRun = Invoke-Python -Exe $py -Arguments @((Join-Path $src "usage_cli.py"), "--shutdown", "--wait-seconds", "20")
    if ($sdRun.Code -ne 0) {
      throw "无法优雅关闭正在运行的本项目服务（退出码 $($sdRun.Code)）：$($sdRun.Err)`n" +
            "本脚本不会按端口或 PID 强制结束进程。请手动关闭后重跑，或稍候重试。"
    }
    Write-Host "  服务检测    : 已确认退出（$($sdRun.Out))"
  } elseif ($svc.stateValid -and $svc.reachable -and -not $svc.owned) {
    throw "live-server.json 指向的监听者（port=$($svc.port)）不是本项目服务（原因：$($svc.reason)）。" +
          "已拒绝继续，也不会强制结束任何进程。请人工确认后处理。"
  } else {
    Write-Host "  服务检测    : 状态文件存在但服务不可达/无效（$($svc.reason)），视为陈旧状态，继续安装" -ForegroundColor DarkYellow
    Write-Host "                该状态文件不会被删除；下次 --open 会覆盖它。"
  }
} else {
  Write-Host "  服务检测    : service-check 不可用（退出码 $($svcRun.Code)），跳过" -ForegroundColor DarkYellow
}

if ($PreflightOnly) {
  Write-Host ""
  Write-Host "PreflightOnly：检测完成，未写入任何内容。" -ForegroundColor Green
  exit 0
}

# ---------------------------------------------------------------------------
# 事务上下文（供回滚使用）
# ---------------------------------------------------------------------------

if ($alreadyCurrent) {
  Write-Host ""
  Write-Host "-- 无需改动 --" -ForegroundColor Green
  Write-Host "  已是最新版本：程序文件与命令注册都与本次安装内容一致。"
  Write-Host "  跳过 Backup / Stage / Apply / Verify（本次没有任何写入），也不会新增备份。"
} else {

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDir = Join-Path $dest (Join-Path ".mimo-usage-backups" $stamp)
$stageDir = Join-Path $dest (".mimo-usage-stage-$stamp")

$ctx = [pscustomobject]@{
  Config          = $cfg
  InstallDir      = $dest
  BackupDir       = $backupDir
  StageDir        = $stageDir
  ManifestPath    = $manifestPath
  Overwritten     = @()      # 安装前已存在、本次将被覆盖的文件名
  Created         = @()      # 安装前不存在、本次将新建的文件名
  Applied         = @{}      # 本次写入的期望哈希
  ManifestExisted = $false
  ConfigJournal   = $null
  AppliedConfig   = $false
}

# ---------------------------------------------------------------------------
# 3) Backup：在任何写操作之前，把将被覆盖的文件与配置都备份下来
# ---------------------------------------------------------------------------

# ===== 事务开始（Backup -> Stage -> Validate -> Apply -> Verify）=====
# 这一段内任何失败都由末尾的 catch 统一回滚一次（只回滚本次动过的文件）。

Write-Host ""
Write-Host "-- Backup --" -ForegroundColor Cyan

try {

New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
  $ctx.ManifestExisted = $true
  Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $backupDir $ManifestName) -Force
}

foreach ($name in $ManagedFiles) {
  $p = Join-Path $dest $name
  if (Test-Path -LiteralPath $p -PathType Leaf) {
    $target = Join-Path $backupDir $name
    Copy-Item -LiteralPath $p -Destination $target -Force
    if ((Get-Sha256 -Path $p) -ne (Get-Sha256 -Path $target)) {
      throw "备份校验失败：$name（复制结果与原文件哈希不一致）。已中止，未改动任何文件。"
    }
    $ctx.Overwritten += $name
    Write-Host "  backup      : $name"
  } else {
    $ctx.Created += $name
  }
}
Write-Host "  backupDir   : $backupDir"

# ---------------------------------------------------------------------------
# 4) Stage：把新文件准备在真实位置之外，并做自检 + 注册预演
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "-- Stage / Validate --" -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
foreach ($name in $ManagedFiles) {
  Copy-Item -LiteralPath (Join-Path $src $name) -Destination (Join-Path $stageDir $name) -Force
  $ctx.Applied[$name] = Get-Sha256 -Path (Join-Path $stageDir $name)
  Write-Host "  staged      : $name  $($ctx.Applied[$name].Substring(0, 12))…"
}

# 4a. 暂存文件可运行。
#     usage_cli.py 必须用 selfcheck：它不读数据库、不写任何文件。
#     （用 --help 是有害的：usage_cli.py 的参数白名单会丢弃 --help，于是变成无参调用，
#      既读真实数据库、又往用户数据目录写报告，还会在没有数据库的机器上直接失败。）
$selfCheck = Invoke-Python -Exe $py -Arguments @((Join-Path $stageDir "usage_cli.py"), "selfcheck")
if ($selfCheck.Code -ne 0) {
  throw "暂存文件自检失败：usage_cli.py selfcheck（退出码 $($selfCheck.Code)）$($selfCheck.Err)"
}
$rcHelp = Invoke-Python -Exe $py -Arguments @((Join-Path $stageDir "register_command.py"), "--help")
if ($rcHelp.Code -ne 0) {
  throw "暂存文件自检失败：register_command.py --help（退出码 $($rcHelp.Code)）$($rcHelp.Err)"
}
Write-Host "  自检        : usage_cli.py selfcheck + register_command.py --help 均通过（未读写任何数据）"
$selfInfo = $null
try { $selfInfo = ($selfCheck.Out | ConvertFrom-Json) } catch { $selfInfo = $null }
if ($null -ne $selfInfo -and -not $selfInfo.dbExists) {
  Write-Host "  note        : MiMo 数据库尚未创建（$($selfInfo.dbPath)）——不影响安装；" -ForegroundColor DarkYellow
  Write-Host "                MiMo Desktop 至少成功用过一次之后，/usage 才会有数据。" -ForegroundColor DarkYellow
}

# 4b. 注册预演：在**配置副本**上跑一遍真实注册流程。
#     这一步能在改动真实配置之前发现"重复键 / 非对象 / 无法定位区域"等形态问题，
#     也能验证最小范围修改后可解析、命令齐全。
$rehearsalDir = Join-Path $stageDir "rehearsal"
New-Item -ItemType Directory -Force -Path $rehearsalDir | Out-Null
$rehearsalCfg = Join-Path $rehearsalDir "mimocode.jsonc"
Copy-Item -LiteralPath $cfg -Destination $rehearsalCfg -Force
# 预演只需要验证"配置形态可被安全就地修改"，用暂存路径即可（此刻最终路径还不存在）；
# 真正落盘的模板路径在 Apply 阶段使用 $dest\usage_cli.py。
$rehearsalScript = Join-Path $stageDir "usage_cli.py"

$rehArgs = @((Join-Path $stageDir "register_command.py"),
             "--config", $rehearsalCfg,
             "--python", $py,
             "--script", $rehearsalScript,
             "--journal", (Join-Path $rehearsalDir "journal.json"))
if ($RemoveLegacy) { $rehArgs += "--purge-legacy-plugin" }
$rehRun = Invoke-Python -Exe $py -Arguments $rehArgs
if ($rehRun.Code -ne 0 -and $rehRun.Code -ne 1) {
  throw "注册预演失败（退出码 $($rehRun.Code)）：$($rehRun.Err)`n真实配置未被改动。"
}
Write-Host "  注册预演    : OK（退出码 $($rehRun.Code)）"

# 4c. 预演结果必须能被解析，且两个命令都在
$rehCheck = [System.IO.File]::ReadAllText($rehearsalCfg)
Remove-Item -LiteralPath $rehearsalDir -Recurse -Force

# ---------------------------------------------------------------------------
# 5) Apply：配置 → 程序文件 → manifest（manifest 最后写）
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "-- Apply --" -ForegroundColor Cyan

# 执行顺序刻意是 程序文件 -> manifest -> 配置：
  #   * 配置里的模板要指向 $dest\usage_cli.py，所以脚本必须先就位（否则注册会拒绝）;
  #   * manifest 紧跟文件写入，中断时"文件+manifest"是一致的，下次 preflight 仍判为 owned;
  #   * 配置最后写。即使中断，配置里的固定命令仍然能用（路径没变），下次可平滑重跑。
  # 5a. 程序文件：从 stage 逐个 Move-Item（同卷 rename，窗口最小）
  foreach ($name in $ManagedFiles) {
    Move-Item -LiteralPath (Join-Path $stageDir $name) -Destination (Join-Path $dest $name) -Force
    Write-Host "  installed   : $name"
  }

  # 5b. manifest
  Write-Manifest -Path $manifestPath -Version $version -CfgPath $cfg -PyExe $py -Hashes $ctx.Applied
  Write-Host "  manifest    : $manifestPath (v$version)"

  # 5c. 配置：就地修改（用**已安装的** register_command.py，脚本自带备份 + 原子写 + 最小范围校验）
  $cfgJournal = Join-Path $backupDir "config-journal.json"
  $appArgs = @((Join-Path $dest "register_command.py"),
               "--config", $cfg,
               "--python", $py,
               "--script", (Join-Path $dest "usage_cli.py"),
               "--journal", $cfgJournal)
  if ($RemoveLegacy) { $appArgs += "--purge-legacy-plugin" }
  $appRun = Invoke-Python -Exe $py -Arguments $appArgs
  if ($appRun.Code -ne 0 -and $appRun.Code -ne 1) {
    throw "注册 /usage 命令失败（register_command.py 退出码 $($appRun.Code)）：$($appRun.Err)"
  }
  $ctx.ConfigJournal = $cfgJournal
  if ($appRun.Code -eq 1) {
    Write-Host "  command     -> $cfg  [already-registered]"
  } else {
    Write-Host "  command     -> $cfg  [$($appRun.Out)]"
  }

# ---------------------------------------------------------------------------
# 6) Verify：核对真实副作用
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "-- Verify --" -ForegroundColor Cyan

$verifyErrors = @()

foreach ($name in $ManagedFiles) {
  $now = Get-Sha256 -Path (Join-Path $dest $name)
  if ($now -ne $ctx.Applied[$name]) {
    $verifyErrors += "$name 的哈希与暂存内容不一致（$now != $($ctx.Applied[$name])）"
  }
}
if ($verifyErrors.Count -eq 0) { Write-Host "  哈希校验    : 两个脚本与暂存内容一致" }

$manifestNow = $null
try { $manifestNow = (Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json) } catch { }
if ($null -eq $manifestNow -or $manifestNow.product -ne $Product) {
  $verifyErrors += "install-manifest.json 缺失或内容异常"
} else {
  foreach ($name in $ManagedFiles) {
    $rec = $manifestNow.files.$name
    if ($null -eq $rec -or $rec.sha256 -ne $ctx.Applied[$name]) {
      $verifyErrors += "manifest 中的 $name 记录与磁盘不一致"
    }
  }
  Write-Host "  manifest    : 已写入并自洽（v$($manifestNow.version)）"
}

$vfRun = Invoke-Python -Exe $py -Arguments @((Join-Path $dest "register_command.py"),
                                             "--preflight", "--config", $cfg, "--install-dir", $dest)
$vf = $null
try { $vf = ($vfRun.Out | ConvertFrom-Json) } catch { }
if ($vfRun.Code -ne 0 -or $null -eq $vf) {
  $verifyErrors += "安装后 preflight 复查失败（退出码 $($vfRun.Code)）：$($vfRun.Err)"
} else {
  if (-not ($vf.hasCommandUsage -and $vf.hasCommandUsageOpen)) {
    $verifyErrors += "配置中缺少 command.usage / command.usage-open"
  }
  if (-not ($vf.usageOwnedByUs -and $vf.usageOpenOwnedByUs)) {
    $verifyErrors += "命令模板未指向本次安装的脚本路径"
  }
  $badFiles = @($vf.managedFiles.PSObject.Properties | Where-Object { $_.Value -ne "owned" })
  if ($badFiles.Count -gt 0) {
    $verifyErrors += "manifest 归属校验未通过：$(($badFiles | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', ')"
  }
  if ($verifyErrors.Count -eq 0) {
    Write-Host "  配置复查    : 两个命令已注册且指向 $dest\usage_cli.py"
    Write-Host "  归属校验    : manifest 与磁盘一致（owned）"
  }
}

$runCheck = Invoke-Python -Exe $py -Arguments @((Join-Path $dest "usage_cli.py"), "selfcheck")
if ($runCheck.Code -ne 0) {
  $verifyErrors += "安装后的 usage_cli.py selfcheck 失败（退出码 $($runCheck.Code)）$($runCheck.Err)"
} else {
  Write-Host "  可运行性    : $dest\usage_cli.py selfcheck 通过（未读写任何数据）"
}

if ($verifyErrors.Count -gt 0) {
  Write-Host ""
  Write-Host "Verify 阶段未通过：" -ForegroundColor Red
  foreach ($e in $verifyErrors) { Write-Host "    - $e" -ForegroundColor Red }
  throw "安装验证未通过：$(($verifyErrors -join ' / '))"
}

Remove-Item -LiteralPath $stageDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  stage 清理  : 已移除 $stageDir"

} catch {
  Write-Host ""
  Write-Host "安装失败：$($_.Exception.Message)" -ForegroundColor Red
  Invoke-Rollback -Ctx $ctx
  Write-Host ""
  Write-Host "本次安装未完成。请按上面的 [rollback] 提示处理后再重试。" -ForegroundColor Red
  exit 1
}
# ===== 事务结束 =====
}   # if ($alreadyCurrent)


# ---------------------------------------------------------------------------
# 7) Legacy 清理（仅在显式 -RemoveLegacy 时；非空目录还需 -Force）
# ---------------------------------------------------------------------------

Write-Host ""
if ($RemoveLegacy) {
  Write-Host "-- Legacy 清理（显式 -RemoveLegacy）--" -ForegroundColor DarkYellow
  foreach ($legacy in @("skills\usage", "skills\mimo-usage-dashboard",
                        "usage-dashboard.config.json", "plugin\usage-dashboard.mjs")) {
    $p = Join-Path $mimo $legacy
    if (-not (Test-Path -LiteralPath $p)) { continue }
    $item = Get-Item -LiteralPath $p -Force
    if ($item.PSIsContainer) {
      $children = @(Get-ChildItem -LiteralPath $p -Force -Recurse -ErrorAction SilentlyContinue)
      if ($children.Count -gt 0 -and -not $Force) {
        Write-Host "  保留        : $p（非空目录，含 $($children.Count) 个条目）" -ForegroundColor DarkYellow
        Write-Host "                未确认这些文件是否属于本项目，拒绝递归删除。内容如下：" -ForegroundColor DarkGray
        $children | Select-Object -First 20 | ForEach-Object { Write-Host "                  $($_.FullName)" -ForegroundColor DarkGray }
        Write-Host "                确认要删请同时加 -Force。" -ForegroundColor DarkGray
        continue
      }
      Remove-Item -LiteralPath $p -Recurse -Force
      Write-Host "  removed     : $p（目录）" -ForegroundColor DarkYellow
    } else {
      Remove-Item -LiteralPath $p -Force
      Write-Host "  removed     : $p（文件）" -ForegroundColor DarkYellow
    }
  }
} else {
  Write-Host "Legacy files preserved (pass -RemoveLegacy only after reviewing them)." -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  Version      : $version ($modeText)"
Write-Host "  Try it now   : & `"$py`" `"$dest\usage_cli.py`" --open"
Write-Host "  In MiMo      : open a NEW session and type /usage   (or /usage-open)"
Write-Host "  With args    : the registered command never forwards arguments; run the CLI directly,"
Write-Host "                 e.g. & `"$py`" `"$dest\usage_cli.py`" token --days 7"
Write-Host "  Python       : $py"
Write-Host "  Config       : $cfg"
Write-Host "  Manifest     : $manifestPath"
Write-Host "  Backup       : $backupDir  (本次安装前的文件副本；不含用户数据)"
Write-Host "  Uninstall    : & `"$py`" `"$dest\register_command.py`" --config `"$cfg`" --uninstall"
Write-Host ""
Write-Host "Note: /usage takes effect in a NEW session (the engine builds the command table at session start)." -ForegroundColor Yellow
exit 0
