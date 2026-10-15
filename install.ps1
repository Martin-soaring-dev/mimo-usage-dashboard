# MiMo Desktop Usage Dashboard — Windows 入口（薄壳）
#
# 事务逻辑（Preflight / Backup / Stage / Validate / Apply / Verify / 回滚 / manifest）
# 全部在 installer.py 里，只有一份实现。本脚本只负责三件事：
#
#   1) 在 Windows 上定位一个可用的 Python 解释器（>= 3.10）；
#   2) 把参数原样转交给 installer.py；
#   3) 把引擎的退出码带回给调用方（非 0 时 throw，保持既有调用方的契约）。
#
# 为什么解释器定位留在这里：必须用 Python 才能启动引擎（先有鸡还是先有蛋）。
# 而且这里的校验必须严格——`-Python` 指到 cmd.exe 这类程序时，绝不能"拿它去启动引擎"。
# 版本与路径的最终校验在引擎里还会再做一次（fail closed，绝不回退到别的解释器）。
#
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1
#          [-Python <exe>] [-Config <jsonc>] [-InstallDir <dir>]
#          [-RemoveLegacy] [-Force] [-PreflightOnly] [-AllowDowngrade]

[CmdletBinding()]
param(
  # 显式指定 python.exe；不传则自动探测
  [string]$Python = "",
  # 实际生效的 MiMo 配置文件；不传则使用引擎默认路径
  [string]$Config = "",
  # 可选的脚本安装目录；不传则放在配置目录下的 usage-dashboard
  [string]$InstallDir = "",
  # 只有明确传入时才清理旧版遗留文件
  [switch]$RemoveLegacy,
  # 允许覆盖"归属不明"或"已被用户修改"的文件（安装前仍会备份）
  [switch]$Force,
  # 只跑安装前检测并打印结论，不安装任何文件（会创建安装目录，并可能用本项目
  # 协议关闭正在运行的本项目服务；身份校验不过则中止，绝不强杀）
  [switch]$PreflightOnly,
  # 已安装版本比本次要装的更新时，允许降级（默认拒绝；覆盖前仍会备份）
  [switch]$AllowDowngrade
)

$ErrorActionPreference = "Stop"

$src = $PSScriptRoot
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
      Write-Host "skip      -> $c ($v < $MinimumPython)"
    } elseif (Test-Path -LiteralPath $c -PathType Leaf) {
      Write-Host "skip      -> $c (无法识别为 Python)"
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

$engine = Join-Path $src "installer.py"
if (-not (Test-Path -LiteralPath $engine -PathType Leaf)) {
  throw "缺少 installer.py：$engine（本脚本只是薄壳，事务逻辑在引擎里）"
}

$engineArgs = @($engine, "--python", $py)
if ($Config) { $engineArgs += @("--config", $Config) }
if ($InstallDir) { $engineArgs += @("--install-dir", $InstallDir) }
if ($Force) { $engineArgs += "--force" }
if ($RemoveLegacy) { $engineArgs += "--remove-legacy" }
if ($PreflightOnly) { $engineArgs += "--preflight-only" }
if ($AllowDowngrade) { $engineArgs += "--allow-downgrade" }

# 引擎只往 stdout 写（诊断信息也走 stdout），所以这里不会被原生命令的 stderr 提升成
# 终止性错误。判定只依赖退出码。
& $py @engineArgs
$engineCode = $LASTEXITCODE

# 保持既有调用方的契约：失败必须抛出（而不是静默返回非 0）。
if ($engineCode -ne 0) {
  throw "安装失败（installer.py 退出码 $engineCode）。详见上方输出。"
}
exit 0
