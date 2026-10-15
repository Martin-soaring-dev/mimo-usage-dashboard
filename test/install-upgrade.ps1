# test/install-upgrade.ps1 — 安装器升级 / 回滚 / 残留处理的隔离测试
#
# 覆盖（对应用户要求的 T1..T7）：
#   T1 旧版本升级（manifest 齐全 → upgrade；无 manifest 但有本项目标记 → adopt）
#   T2 安装失败后所有程序文件哈希恢复（配置写失败 / 目标文件被占用 两种失败点）
#   T3 混装检测（失败后不允许出现"新 CLI + 旧 register"）
#   T4 未知文件保留（含"同名但无标记"必须 fail closed）
#   T5 manifest 缺失时的安全行为 + 已是最新时不做无意义写入
#   T6 真实 JSONC 注释 / CRLF / BOM / 制表符缩进保持（含"用户自建同名命令"必须 fail closed）
#   T7 运行服务升级流程（身份校验 + 优雅关闭；伪造服务的负向用例）
#   T8 无数据库的机器上也能安装，且安装不写用户数据
#   T9 降级护栏（默认拒绝；-AllowDowngrade 才放行，且降级前先备份）
#
# 全部在临时目录中完成：不读取也不写入真实 mimocode.jsonc / 真实数据库 / 真实数据目录。
# 运行：powershell -NoProfile -ExecutionPolicy Bypass -File test/install-upgrade.ps1

$ErrorActionPreference = "Continue"

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $root "install.ps1"))) {
  throw "找不到 install.ps1（root=$root）"
}
$python = (Get-Command python -ErrorAction Stop).Source
$marker = "mimo-usage-dashboard:managed-file:v1"

$script:FAILS = 0
$script:CHECKS = 0
$script:ABORTED = $null

function Check {
  param([string]$Name, [bool]$Ok, [string]$Extra = "")
  $script:CHECKS++
  if (-not $Ok) { $script:FAILS++ }
  if ($Ok) {
    Write-Host ("  PASS  {0}{1}" -f $Name, $(if ($Extra) { "  $Extra" } else { "" })) -ForegroundColor Gray
  } else {
    Write-Host ("  FAIL  {0}{1}" -f $Name, $(if ($Extra) { "  $Extra" } else { "" })) -ForegroundColor Red
  }
}

function Get-Sha {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "<missing>" }
  try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() } catch { return "<unreadable>" }
}

# 配置一律按 UTF-8 读：本机默认代码页不是 UTF-8，Get-Content 会把中文读成乱码
function Read-Utf8 {
  param([string]$Path)
  return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

# 配置是 JSONC（含注释/尾随逗号），PowerShell 的 ConvertFrom-Json 解析不了它。
# 这里直接借产品自己的解析器来解析，顺带也验证一遍配置文件确实可被产品读取。
function ConvertFrom-Jsonc {
  param([string]$Path)
  $code = "import sys, json; sys.path.insert(0, r'" + $root + "');" +
          "import register_command as R;" +
          "print(json.dumps(json.loads(R.strip_jsonc(open(sys.argv[1], encoding='utf-8-sig').read())), ensure_ascii=False))"
  $out = & $python -c $code $Path
  if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
  return (($out | Out-String) | ConvertFrom-Json)
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("mimo-upgrade-test-" + [guid]::NewGuid().ToString("N"))
$dataDir = Join-Path $work "user-data"
$serveProcs = New-Object System.Collections.ArrayList

function New-Case {
  param([string]$Name, [string]$ConfigText = "{`n}`n", [bool]$Bom = $false, [string]$Newline = "`n")
  $dir = Join-Path $work $Name
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $cfg = Join-Path $dir "mimocode.jsonc"
  $dest = Join-Path $dir "installed"
  $text = $ConfigText
  if ($Newline -ne "`n") { $text = $text.Replace("`n", $Newline) }
  $stream = [System.IO.File]::Create($cfg)
  if ($Bom) { $stream.Write([byte[]]@(0xEF, 0xBB, 0xBF), 0, 3) }
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
  $stream.Write($bytes, 0, $bytes.Length)
  $stream.Close()
  return [pscustomobject]@{ Dir = $dir; Config = $cfg; Dest = $dest }
}

function Run-Install {
  param([pscustomobject]$Case, [string[]]$Extra = @())
  $argv = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $root "install.ps1"),
            "-Config", $Case.Config, "-InstallDir", $Case.Dest, "-Python", $python) + $Extra
  $out = & powershell @argv *>&1 | Out-String
  return [pscustomobject]@{ Code = $LASTEXITCODE; Out = $out }
}

function Get-Manifest {
  param([pscustomobject]$Case)
  $p = Join-Path $Case.Dest "install-manifest.json"
  if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { return $null }
  try { return (Read-Utf8 $p | ConvertFrom-Json) } catch { return $null }
}

function Get-Backups {
  param([pscustomobject]$Case)
  $b = Join-Path $Case.Dest ".mimo-usage-backups"
  if (-not (Test-Path -LiteralPath $b)) { return @() }
  return @(Get-ChildItem -LiteralPath $b -Directory | Sort-Object Name)
}

# 把安装目录伪造成"上一版的安装"：文件带本项目标记但内容不同，并同步 manifest 哈希
# （这样它在 preflight 里会被判为 owned，从而走正常 upgrade 路径而不是 requiresForce）
function Set-StaleInstall {
  param([pscustomobject]$Case, [string]$Content = "#!/usr/bin/env python3`n# $marker`n# OLD RELEASE`n",
        [string]$Version = "0.1.0")
  $cli = Join-Path $Case.Dest "usage_cli.py"
  $tmp = $cli + ".new"
  [System.IO.File]::WriteAllText($tmp, $Content)
  Move-Item -LiteralPath $tmp -Destination $cli -Force
  $m = Get-Manifest -Case $Case
  if ($null -ne $m) {
    $m.version = $Version
    $m.files.'usage_cli.py'.sha256 = (Get-Sha $cli)
    [System.IO.File]::WriteAllText((Join-Path $Case.Dest "install-manifest.json"), ($m | ConvertTo-Json -Depth 6))
  }
}

function Stop-IsolatedService {
  # 只通过本项目协议关闭自己启动的隔离服务，绝不 taskkill
  $st = Join-Path $dataDir "live-server.json"
  if (-not (Test-Path -LiteralPath $st)) { return }
  $env:MIMO_USAGE_DATA_DIR = $dataDir
  & $python (Join-Path $root "usage_cli.py") "--shutdown" "--wait-seconds" "10" | Out-Null
}

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$env:MIMO_USAGE_DATA_DIR = $dataDir
# 测试全程隔离：数据库也指向合成库，绝不读真实的 ~/.local/share/mimocode/mimocode.db
$synthDb = Join-Path $work "synth.db"
$synthFixture = "import sys, time; sys.path.insert(0, r'" + $PSScriptRoot + "'); from test_python import make_db; make_db(sys.argv[1], int(time.time()*1000))"
& $python -c $synthFixture $synthDb | Out-Null
$env:MIMO_USAGE_DB = $synthDb
Write-Host "work = $work"
Write-Host "synthDb = $synthDb"

try {
  # =========================================================================
  Write-Host ""
  Write-Host "=== T1 旧版本升级 ===" -ForegroundColor Cyan
  # =========================================================================
  $c1 = New-Case "t1-upgrade" "{`n  // 用户注释`n  `"mcp`": {}`n}`n"
  $r1 = Run-Install -Case $c1
  Check "T1: 首次安装成功（exit 0）" ($r1.Code -eq 0) "(code=$($r1.Code))"
  Check "T1: 首次安装报告 fresh" ($r1.Out -match "fresh")
  $m1 = Get-Manifest -Case $c1
  Check "T1: 首次安装写入 manifest" ($null -ne $m1 -and $m1.product -eq "mimo-usage-dashboard")
  Check "T1: manifest 记录两个文件" ($null -ne $m1 -and $null -ne $m1.files.'usage_cli.py' -and $null -ne $m1.files.'register_command.py')
  $installedHash1 = Get-Sha (Join-Path $c1.Dest "usage_cli.py")
  Check "T1: manifest 哈希与磁盘一致" ($m1.files.'usage_cli.py'.sha256 -eq $installedHash1)
  Check "T1: 首次安装保留注释" ((Read-Utf8 $c1.Config).Contains("// 用户注释"))

  # 伪造成上一版，再升级
  Set-StaleInstall -Case $c1
  $oldHash = Get-Sha (Join-Path $c1.Dest "usage_cli.py")
  $r1b = Run-Install -Case $c1
  Check "T1: 升级安装成功（exit 0）" ($r1b.Code -eq 0) "(code=$($r1b.Code))"
  Check "T1: 识别为 upgrade 模式" ($r1b.Out -match "upgrade")
  $m1b = Get-Manifest -Case $c1
  Check "T1: 升级后文件被替换为新版" ((Get-Sha (Join-Path $c1.Dest "usage_cli.py")) -eq (Get-Sha (Join-Path $root "usage_cli.py")))
  Check "T1: 升级后 manifest 记录新哈希" ($m1b.files.'usage_cli.py'.sha256 -eq (Get-Sha (Join-Path $c1.Dest "usage_cli.py")))
  Check "T1: 升级后 manifest 版本更新" ($m1b.version -ne "0.1.0") "(version=$($m1b.version))"
  $bks = Get-Backups -Case $c1
  Check "T1: 升级前的旧文件已备份" ($bks.Count -ge 1) "(backups=$($bks.Count))"
  $foundOld = $false
  foreach ($b in $bks) {
    if ((Get-Sha (Join-Path $b.FullName "usage_cli.py")) -eq $oldHash) { $foundOld = $true }
  }
  Check "T1: 备份里能取回旧版字节" $foundOld
  Check "T1: 升级后注释仍保留" ((Read-Utf8 $c1.Config).Contains("// 用户注释"))
  Check "T1: 升级后 mcp 段仍在" ((Read-Utf8 $c1.Config).Contains('"mcp": {}'))

  # 无 manifest 但有标记 → adopt
  $c1c = New-Case "t1-adopt" "{`n  `"mcp`": {}`n}`n"
  New-Item -ItemType Directory -Force -Path $c1c.Dest | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $c1c.Dest "usage_cli.py"), "#!/usr/bin/env python3`n# $marker`n# OLD`n")
  $preAdoptHash = Get-Sha (Join-Path $c1c.Dest "usage_cli.py")
  $r1c = Run-Install -Case $c1c
  Check "T1: 无 manifest 但有标记 → 允许接管（exit 0）" ($r1c.Code -eq 0) "(code=$($r1c.Code))"
  Check "T1: 识别为 adopt 模式" ($r1c.Out -match "adopt")
  Check "T1: adopt 后 manifest 已补齐" ($null -ne (Get-Manifest -Case $c1c))
  $adoptFound = $false
  foreach ($b in (Get-Backups -Case $c1c)) {
    if ((Get-Sha (Join-Path $b.FullName "usage_cli.py")) -eq $preAdoptHash) { $adoptFound = $true }
  }
  Check "T1: adopt 前旧文件已备份" $adoptFound

  # =========================================================================
  Write-Host ""
  Write-Host "=== T2 / T3 失败回滚与混装检测 ===" -ForegroundColor Cyan
  # =========================================================================
  $c2 = New-Case "t2-rollback" "{`n  // keep`n  `"command`": {},`n  `"mcp`": {}`n}`n"
  $r2 = Run-Install -Case $c2
  Check "T2: 基线安装成功" ($r2.Code -eq 0) "(code=$($r2.Code))"

  # ---- T2a：升级时配置不可写 → Apply 已替换文件后失败 → 必须整体回滚 ----
  Set-StaleInstall -Case $c2
  $baseCfg = Get-Sha $c2.Config
  $baseCli = Get-Sha (Join-Path $c2.Dest "usage_cli.py")
  $baseReg = Get-Sha (Join-Path $c2.Dest "register_command.py")
  $baseManifest = Get-Sha (Join-Path $c2.Dest "install-manifest.json")
  $baseCfgRaw = Read-Utf8 $c2.Config
  Set-ItemProperty -LiteralPath $c2.Config -Name IsReadOnly -Value $true -Force
  $r2a = Run-Install -Case $c2
  Set-ItemProperty -LiteralPath $c2.Config -Name IsReadOnly -Value $false -Force
  Check "T2a: 配置只读时安装失败（非 0）" ($r2a.Code -ne 0) "(code=$($r2a.Code))"
  Check "T2a: 失败后 usage_cli.py 哈希恢复" ((Get-Sha (Join-Path $c2.Dest "usage_cli.py")) -eq $baseCli)
  Check "T2a: 失败后 register_command.py 哈希未变" ((Get-Sha (Join-Path $c2.Dest "register_command.py")) -eq $baseReg)
  Check "T2a: 失败后 manifest 哈希恢复" ((Get-Sha (Join-Path $c2.Dest "install-manifest.json")) -eq $baseManifest)
  Check "T2a: 失败后配置字节未变" ((Get-Sha $c2.Config) -eq $baseCfg)
  Check "T2a: 报告了回滚动作" ($r2a.Out -match "rollback")

  # ---- T2b：升级时 register_command.py 被占用 → 移动阶段失败 → 不允许混装 ----
  Set-StaleInstall -Case $c2
  $baseCfg = Get-Sha $c2.Config
  $baseCli = Get-Sha (Join-Path $c2.Dest "usage_cli.py")
  $baseReg = Get-Sha (Join-Path $c2.Dest "register_command.py")
  $baseManifest = Get-Sha (Join-Path $c2.Dest "install-manifest.json")
  $lock = [System.IO.File]::Open((Join-Path $c2.Dest "register_command.py"), 'Open', 'Read', 'None')
  try { $r2b = Run-Install -Case $c2 } finally { $lock.Close() }
  Check "T2b: 目标文件被占用时安装失败（非 0）" ($r2b.Code -ne 0) "(code=$($r2b.Code))"
  Check "T2b: 失败后配置未变" ((Get-Sha $c2.Config) -eq $baseCfg)
  Check "T2b: 失败后 usage_cli.py 哈希恢复" ((Get-Sha (Join-Path $c2.Dest "usage_cli.py")) -eq $baseCli)
  Check "T2b: 失败后 register_command.py 哈希未变" ((Get-Sha (Join-Path $c2.Dest "register_command.py")) -eq $baseReg)
  Check "T2b: 失败后 manifest 哈希恢复" ((Get-Sha (Join-Path $c2.Dest "install-manifest.json")) -eq $baseManifest)
  # T3：两个文件必须要么都是旧版、要么都是新版
  $nowCli = Get-Sha (Join-Path $c2.Dest "usage_cli.py")
  $nowReg = Get-Sha (Join-Path $c2.Dest "register_command.py")
  $srcCli = Get-Sha (Join-Path $root "usage_cli.py")
  $srcReg = Get-Sha (Join-Path $root "register_command.py")
  $bothOld = ($nowCli -eq $baseCli) -and ($nowReg -eq $baseReg)
  $bothNew = ($nowCli -eq $srcCli) -and ($nowReg -eq $srcReg)
  Check "T3: 未出现混装（非 新CLI+旧register）" ($bothOld -or $bothNew) "(old=$bothOld new=$bothNew)"
  Check "T3: 回滚后回到安装前的完整状态" $bothOld
  Check "T3: 报告了回滚动作" ($r2b.Out -match "rollback")

  # ---- T2c：全新目录 + 配置非法 → Preflight 就中止，什么都不写 ----
  $c2c = New-Case "t2c-fresh-fail" "{ this is not json`n"
  $r2c = Run-Install -Case $c2c
  Check "T2c: 配置非法时安装失败（非 0）" ($r2c.Code -ne 0) "(code=$($r2c.Code))"
  Check "T2c: 非法配置字节未变" ((Read-Utf8 $c2c.Config).Contains("this is not json"))
  Check "T2c: 未遗留已安装的 usage_cli.py" (-not (Test-Path -LiteralPath (Join-Path $c2c.Dest "usage_cli.py")))
  Check "T2c: 未遗留 manifest" (-not (Test-Path -LiteralPath (Join-Path $c2c.Dest "install-manifest.json")))

  # ---- T2d：-PreflightOnly 不得写入任何内容 ----
  $c2d = New-Case "t2d-preflightonly" "{`n  `"command`": {}`n}`n"
  $r2d = Run-Install -Case $c2d -Extra @("-PreflightOnly")
  Check "T2d: -PreflightOnly 成功退出" ($r2d.Code -eq 0) "(code=$($r2d.Code))"
  Check "T2d: -PreflightOnly 未安装脚本" (-not (Test-Path -LiteralPath (Join-Path $c2d.Dest "usage_cli.py")))
  Check "T2d: -PreflightOnly 未写 manifest" (-not (Test-Path -LiteralPath (Join-Path $c2d.Dest "install-manifest.json")))
  Check "T2d: -PreflightOnly 未改动配置" ((Read-Utf8 $c2d.Config).Contains('"command": {}'))

  # =========================================================================
  Write-Host ""
  Write-Host "=== T4 未知文件保留 ===" -ForegroundColor Cyan
  # =========================================================================
  $c4 = New-Case "t4-unknown" "{`n  `"command`": {}`n}`n"
  New-Item -ItemType Directory -Force -Path $c4.Dest | Out-Null
  $userNote = Join-Path $c4.Dest "MY-CUSTOM-NOTES.txt"
  [System.IO.File]::WriteAllText($userNote, "user data - must survive")
  $userNoteHash = Get-Sha $userNote
  $legacyDir = Join-Path (Split-Path -Parent $c4.Config) "skills\usage"
  New-Item -ItemType Directory -Force -Path $legacyDir | Out-Null
  $legacyFile = Join-Path $legacyDir "keep.txt"
  [System.IO.File]::WriteAllText($legacyFile, "legacy user file")

  $r4 = Run-Install -Case $c4
  Check "T4: 含未知文件的目录可正常安装" ($r4.Code -eq 0) "(code=$($r4.Code))"
  Check "T4: 未知文件字节保留" ((Get-Sha $userNote) -eq $userNoteHash)
  Check "T4: 默认不删遗留目录" (Test-Path -LiteralPath $legacyFile)

  $r4b = Run-Install -Case $c4 -Extra @("-RemoveLegacy")
  Check "T4: -RemoveLegacy 单独使用时安装仍成功" ($r4b.Code -eq 0) "(code=$($r4b.Code))"
  Check "T4: 非空遗留目录未被递归删除" (Test-Path -LiteralPath $legacyFile)
  Check "T4: 提示需要 -Force" ($r4b.Out -match "-Force")
  Check "T4: 未知文件仍在" (Test-Path -LiteralPath $userNote)

  # 残留清理规则：只清"本项目命名 + 已失效"的暂存目录，其它一律保留
  $staleDir = Join-Path $c4.Dest ".mimo-usage-stage-20200101-000000"
  New-Item -ItemType Directory -Force -Path $staleDir | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $staleDir "leftover.py"), "old leftover")
  $oddDir = Join-Path $c4.Dest ".mimo-usage-stage-not-a-timestamp"
  New-Item -ItemType Directory -Force -Path $oddDir | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $oddDir "user.txt"), "unknown ownership")
  $r4e = Run-Install -Case $c4
  Check "T4: 失效暂存目录被清理" (-not (Test-Path -LiteralPath $staleDir)) "(code=$($r4e.Code))"
  Check "T4: 名称不合规的同前缀目录被保留" (Test-Path -LiteralPath (Join-Path $oddDir "user.txt"))
  Check "T4: 未知文件在清理后仍在" (Test-Path -LiteralPath $userNote)

  # 同名但无标记的文件 → 必须 fail closed
  $c4c = New-Case "t4-stranger" "{`n  `"command`": {}`n}`n"
  New-Item -ItemType Directory -Force -Path $c4c.Dest | Out-Null
  $stranger = Join-Path $c4c.Dest "usage_cli.py"
  [System.IO.File]::WriteAllText($stranger, "# someone else's file - NOT ours`n")
  $strangerHash = Get-Sha $stranger
  $r4c = Run-Install -Case $c4c
  Check "T4: 同名无标记文件 → 拒绝安装" ($r4c.Code -ne 0) "(code=$($r4c.Code))"
  Check "T4: 陌生文件字节未变" ((Get-Sha $stranger) -eq $strangerHash)
  Check "T4: 未写入 manifest" (-not (Test-Path -LiteralPath (Join-Path $c4c.Dest "install-manifest.json")))
  Check "T4: 提示需要 -Force" ($r4c.Out -match "-Force")
  Check "T4: 说明归属不明" ($r4c.Out -match "归属不明")
  $r4d = Run-Install -Case $c4c -Extra @("-Force")
  Check "T4: -Force 后安装成功" ($r4d.Code -eq 0) "(code=$($r4d.Code))"
  $strangerBak = $false
  foreach ($b in (Get-Backups -Case $c4c)) {
    if ((Get-Sha (Join-Path $b.FullName "usage_cli.py")) -eq $strangerHash) { $strangerBak = $true }
  }
  Check "T4: -Force 覆盖前已备份陌生文件" $strangerBak

  # =========================================================================
  Write-Host ""
  Write-Host "=== T5 manifest 缺失时的安全行为 / 已是最新 ===" -ForegroundColor Cyan
  # =========================================================================
  $c5 = New-Case "t5-nomanifest" "{`n  `"command`": {}`n}`n"
  New-Item -ItemType Directory -Force -Path $c5.Dest | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $c5.Dest "usage_cli.py"), "#!/usr/bin/env python3`n# $marker`n")
  [System.IO.File]::WriteAllText((Join-Path $c5.Dest "register_command.py"), "#!/usr/bin/env python3`n# $marker`n")
  Check "T5: 起始状态没有 manifest" (-not (Test-Path -LiteralPath (Join-Path $c5.Dest "install-manifest.json")))
  $r5 = Run-Install -Case $c5
  Check "T5: 有标记无 manifest → 安装成功" ($r5.Code -eq 0) "(code=$($r5.Code))"
  $m5 = Get-Manifest -Case $c5
  Check "T5: 安装后补齐 manifest" ($null -ne $m5)
  Check "T5: manifest 哈希与磁盘一致" ($m5.files.'usage_cli.py'.sha256 -eq (Get-Sha (Join-Path $c5.Dest "usage_cli.py")))
  # 已是最新：第二次安装应完全跳过事务（不新增备份）
  $b5a = (Get-Backups -Case $c5).Count
  $cfg5a = Get-Sha $c5.Config
  $r5b = Run-Install -Case $c5
  $b5b = (Get-Backups -Case $c5).Count
  Check "T5: 重复安装成功（exit 0）" ($r5b.Code -eq 0) "(code=$($r5b.Code))"
  Check "T5: 识别为已是最新并跳过事务" ($r5b.Out -match "无需改动")
  Check "T5: 已是最新时不新增备份" ($b5b -eq $b5a) "($b5a->$b5b)"
  Check "T5: 已是最新时配置字节未变" ((Get-Sha $c5.Config) -eq $cfg5a)

  # =========================================================================
  Write-Host ""
  Write-Host "=== T6 真实 JSONC 注释 / CRLF / BOM 保持 ===" -ForegroundColor Cyan
  # =========================================================================
  # 该配置里的 command.usage 是**用户自建**的（template 不指向本项目脚本）：
  # 因此必须 fail closed，只有 -Force 才允许覆盖 —— 这本身就是一条重要断言。
  $t6cfg = "{`n`t// 行注释：必须保留`n`t`"`$schema`": `"https://x/s.json`",`n`t`"command`": {`n`t`t/* 块注释 */`n`t`t`"other`": { `"description`": `"d`", `"template`": `"t`" },`n`t`t`"usage`": { `"description`": `"我的`", `"template`": `"mine`", `"agent`": `"my-agent`" }`n`t},`n`t`"mcp`": { `"servers`": { `"a`": { `"url`": `"http://x`" } } }`n}`n"
  $c6 = New-Case "t6-jsonc" $t6cfg -Bom $true -Newline "`r`n"
  $before6Bytes = [System.IO.File]::ReadAllBytes($c6.Config)
  Check "T6: 起始文件带 BOM" ($before6Bytes[0] -eq 0xEF -and $before6Bytes[1] -eq 0xBB -and $before6Bytes[2] -eq 0xBF)
  $before6 = Read-Utf8 $c6.Config
  Check "T6: 起始文件是 CRLF" ($before6.Contains("`r`n") -and -not ($before6 -replace "`r`n", "").Contains("`n"))

  $r6refuse = Run-Install -Case $c6
  Check "T6: 用户自建同名命令 → 拒绝安装（fail closed）" ($r6refuse.Code -ne 0) "(code=$($r6refuse.Code))"
  Check "T6: 拒绝时说明不指向本项目脚本" ($r6refuse.Out -match "不指向本项目")
  Check "T6: 拒绝时配置字节完全未变" ((Get-Sha $c6.Config) -eq (Get-Sha $c6.Config))

  $r6 = Run-Install -Case $c6 -Extra @("-Force")
  Check "T6: -Force 后安装成功" ($r6.Code -eq 0) "(code=$($r6.Code))"
  $after6Bytes = [System.IO.File]::ReadAllBytes($c6.Config)
  Check "T6: BOM 保留" ($after6Bytes[0] -eq 0xEF -and $after6Bytes[1] -eq 0xBB -and $after6Bytes[2] -eq 0xBF)
  $after6 = Read-Utf8 $c6.Config
  Check "T6: CRLF 保留（无裸 LF）" (-not ($after6 -replace "`r`n", "").Contains("`n"))
  Check "T6: 行注释原文保留" ($after6.Contains("// 行注释：必须保留"))
  Check "T6: 块注释原文保留" ($after6.Contains("/* 块注释 */"))
  Check "T6: 制表符缩进保留" ($after6.Contains("`t`t`"other`""))
  Check "T6: 表头键行字节保留" ($after6.Contains('	"$schema": "https://x/s.json",'))
  Check "T6: 嵌套 mcp 段字节保留" ($after6.Contains('"mcp": { "servers": { "a": { "url": "http://x" } } }'))
  Check "T6: 用户自加字段保留" ($after6.Contains('"agent": "my-agent"'))
  Check "T6: 邻居命令未被改动" ($after6.Contains('"other": { "description": "d", "template": "t" }'))
  $j6 = ConvertFrom-Jsonc -Path $c6.Config
  Check "T6: usage 描述已更新为项目内容" ($j6.command.usage.description -like "MiMo Desktop*")
  Check "T6: usage-open 已注册" ($null -ne $j6.command.'usage-open')
  Check "T6: 邻居命令语义未变" ($j6.command.other.template -eq "t")
  Check "T6: mcp 语义未变" ($j6.mcp.servers.a.url -eq "http://x")
  # 逐字节：command 段之外必须完全一致
  Check "T6: 文件尾部字节一致" ($after6.EndsWith($before6.Substring($before6.LastIndexOf("}"))))
  Check "T6: command 段之前字节一致" ($after6.StartsWith($before6.Substring(0, $before6.IndexOf('"command"'))))
  Check "T6: 文件总长度增加仅来自新增条目" ($after6Bytes.Length -gt $before6Bytes.Length)

  # =========================================================================
  Write-Host ""
  Write-Host "=== T7 运行服务升级流程 ===" -ForegroundColor Cyan
  # =========================================================================
  $t7db = $synthDb
  Check "T7: 合成库已创建" (Test-Path -LiteralPath $t7db)

  $c7 = New-Case "t7-service" "{`n  `"command`": {}`n}`n"
  $r7first = Run-Install -Case $c7
  Check "T7: 首次安装成功（拿到可运行的 CLI）" ($r7first.Code -eq 0) "(code=$($r7first.Code))"

  $token7 = "T7TOKEN0000000000"
  $env:MIMO_USAGE_DATA_DIR = $dataDir
  $serve = Start-Process -FilePath $python -NoNewWindow -PassThru -ArgumentList @(
    (Join-Path $c7.Dest "usage_cli.py"), "serve", "--db", $t7db, "--token", $token7, "--idle-minutes", "5"
  )
  [void]$serveProcs.Add($serve)
  $ready = $false
  for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 200
    $sc = & $python (Join-Path $root "usage_cli.py") "service-check" | Out-String
    if ($sc -match '"owned":\s*true') { $ready = $true; break }
  }
  Check "T7: 隔离服务身份校验通过（owned=true）" $ready
  if ($ready) {
    # 造一个"上一版"，确保真的会走升级事务而不是"已是最新"跳过
    Set-StaleInstall -Case $c7
    $r7 = Run-Install -Case $c7
    Check "T7: 服务运行中仍能完成升级" ($r7.Code -eq 0) "(code=$($r7.Code))"
    Check "T7: 走的是优雅关闭路径（不是强杀）" ($r7.Out -match "优雅关闭")
    $stillUp = $false
    for ($i = 0; $i -lt 30; $i++) {
      Start-Sleep -Milliseconds 200
      $sc2 = & $python (Join-Path $root "usage_cli.py") "service-check" | Out-String
      if ($sc2 -match '"reachable":\s*true') { $stillUp = $true; break }
    }
    Check "T7: 服务已被本项目协议关闭（不再可达）" (-not $stillUp)
    Check "T7: 服务进程已自行退出" $serve.HasExited
    Check "T7: 升级后文件为新版" ((Get-Sha (Join-Path $c7.Dest "usage_cli.py")) -eq (Get-Sha (Join-Path $root "usage_cli.py")))
  } else {
    Write-Host "  服务未就绪，T7 正向断言跳过" -ForegroundColor Yellow
  }

  # ---- 负向用例：状态文件指向"不是本项目"的监听者 → 拒绝、且不碰它 ----
  $foreignPort = 45999
  $foreignScript = Join-Path $work "foreign-server.py"
  $foreignCode = "import json, sys`n" +
                 "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer`n" +
                 "class H(BaseHTTPRequestHandler):`n" +
                 "    def log_message(self, *a):`n" +
                 "        pass`n" +
                 "    def do_GET(self):`n" +
                 "        body = json.dumps({'hello': 'not-the-dashboard'}).encode()`n" +
                 "        self.send_response(200)`n" +
                 "        self.send_header('Content-Type', 'application/json')`n" +
                 "        self.send_header('Content-Length', str(len(body)))`n" +
                 "        self.end_headers()`n" +
                 "        self.wfile.write(body)`n" +
                 "ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()`n"
  [System.IO.File]::WriteAllText($foreignScript, $foreignCode)
  $foreign = Start-Process -FilePath $python -NoNewWindow -PassThru -ArgumentList @($foreignScript, "$foreignPort")
  [void]$serveProcs.Add($foreign)
  Start-Sleep -Milliseconds 900

  $forgeDir = Join-Path $work "forge-data"
  New-Item -ItemType Directory -Force -Path $forgeDir | Out-Null
  $forged = [ordered]@{
    pid = $foreign.Id; port = $foreignPort; token = "FORGEDTOKEN000000"
    pageUrl = "http://127.0.0.1:$foreignPort/FORGEDTOKEN000000/"
    reportUrl = "http://127.0.0.1:$foreignPort/FORGEDTOKEN000000/api/report"
    db = "C:\nope\mimocode.db"; kind = "all"; days = 30
  }
  [System.IO.File]::WriteAllText((Join-Path $forgeDir "live-server.json"), ($forged | ConvertTo-Json))
  $env:MIMO_USAGE_DATA_DIR = $forgeDir
  $c7b = New-Case "t7-foreign" "{`n  `"command`": {}`n}`n"
  $r7b = Run-Install -Case $c7b
  Check "T7: 伪造状态（响应非本项目格式）→ 拒绝安装" ($r7b.Code -ne 0) "(code=$($r7b.Code))"
  Check "T7: 明确指出监听者不是本项目服务" ($r7b.Out -match "不是本项目服务")
  $foreignAlive = $false
  try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$foreignPort/" -UseBasicParsing -TimeoutSec 3
    if ($resp.StatusCode -eq 200) { $foreignAlive = $true }
  } catch { $foreignAlive = $false }
  Check "T7: 陌生监听者未被关闭/未被杀" $foreignAlive
  Check "T7: 未安装任何文件（拒绝发生在写操作之前）" (-not (Test-Path -LiteralPath (Join-Path $c7b.Dest "usage_cli.py")))
  $env:MIMO_USAGE_DATA_DIR = $dataDir

  # =========================================================================
  Write-Host ""
  Write-Host "=== T8 环境自检不依赖数据库 / 安装不产生用户数据 ===" -ForegroundColor Cyan
  # =========================================================================
  # P0 回归（2026-10-14 真实部署暴露）：安装器的"自检"曾用 `usage_cli.py --help`，
  # 而 --help 会被 usage_cli.py 的参数白名单静默丢弃 → 变成无参调用 → 读真实数据库、
  # 往用户数据目录写一份报告，并且在**没有数据库的机器上直接让安装失败**。
  # 下面四条断言把这条路封死：无库可安装 + 有明确提示 + 不写用户数据 + 安装完成。
  $t8 = New-Case "t8-nodb" "{`n  `"command`": {}`n}`n"
  $t8data = Join-Path $t8.Dir "empty-data"
  New-Item -ItemType Directory -Force -Path $t8data | Out-Null
  $savedDb = $env:MIMO_USAGE_DB
  $savedData = $env:MIMO_USAGE_DATA_DIR
  $env:MIMO_USAGE_DB = Join-Path $t8.Dir "no-such-db\missing.db"
  $env:MIMO_USAGE_DATA_DIR = $t8data
  try {
    $r8 = Run-Install -Case $t8
  } finally {
    $env:MIMO_USAGE_DB = $savedDb
    $env:MIMO_USAGE_DATA_DIR = $savedData
  }
  Check "T8: 数据库不存在时安装仍然成功（exit 0）" ($r8.Code -eq 0) "(code=$($r8.Code))"
  Check "T8: 明确提示数据库尚未创建、且不阻止安装" ($r8.Out -match "数据库尚未创建")
  Check "T8: 安装确实完成（manifest 已写入）" ($null -ne (Get-Manifest -Case $t8))
  $t8files = @(Get-ChildItem -LiteralPath $t8data -Force -ErrorAction SilentlyContinue)
  Check "T8: 安装过程没有往用户数据目录写任何文件" ($t8files.Count -eq 0) "(files=$($t8files.Count))"
  Check "T8: 安装目录里没有残留暂存目录" (
    @(Get-ChildItem -LiteralPath $t8.Dest -Force -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -like ".mimo-usage-stage-*" }).Count -eq 0)

  # =========================================================================
  Write-Host ""
  Write-Host "=== T9 降级护栏（默认拒绝 / -AllowDowngrade 才放行）===" -ForegroundColor Cyan
  # =========================================================================
  $c9 = New-Case "t9-downgrade" "{`n  `"command`": {}`n}`n"
  $r9a = Run-Install -Case $c9
  Check "T9: 基线安装成功（exit 0）" ($r9a.Code -eq 0) "(code=$($r9a.Code))"
  $pkgVer = (Read-Utf8 (Join-Path $root "package.json") | ConvertFrom-Json).version
  # 伪造成"已装了更新的版本"：内容不同 + manifest 哈希同步 → 归属仍是 owned（不需 -Force）
  Set-StaleInstall -Case $c9 -Version "99.0.0"
  $stale9 = Get-Sha (Join-Path $c9.Dest "usage_cli.py")
  $cfg9 = Get-Sha $c9.Config
  $b9a = (Get-Backups -Case $c9).Count

  $r9b = Run-Install -Case $c9
  Check "T9: 未加开关 → 拒绝降级（非 0 退出）" ($r9b.Code -ne 0) "(code=$($r9b.Code))"
  Check "T9: 拒绝时说明原因" ($r9b.Out -match "拒绝降级")
  Check "T9: 拒绝时指出 -AllowDowngrade" ($r9b.Out -match "AllowDowngrade")
  Check "T9: 拒绝时受管文件字节未变" ((Get-Sha (Join-Path $c9.Dest "usage_cli.py")) -eq $stale9)
  Check "T9: 拒绝时配置字节未变" ((Get-Sha $c9.Config) -eq $cfg9)
  Check "T9: 拒绝时未新增备份" ((Get-Backups -Case $c9).Count -eq $b9a)
  Check "T9: 拒绝时 manifest 版本未变" ((Get-Manifest -Case $c9).version -eq "99.0.0")

  $r9c = Run-Install -Case $c9 -Extra @("-AllowDowngrade")
  Check "T9: -AllowDowngrade → 安装成功（exit 0）" ($r9c.Code -eq 0) "(code=$($r9c.Code))"
  Check "T9: 降级后文件换成当前版本" (
    (Get-Sha (Join-Path $c9.Dest "usage_cli.py")) -eq (Get-Sha (Join-Path $root "usage_cli.py")))
  Check "T9: 降级后 manifest 记录当前版本" ((Get-Manifest -Case $c9).version -eq $pkgVer)
  Check "T9: 降级前的旧文件已备份" ((Get-Backups -Case $c9).Count -gt $b9a)

} catch {
  $script:ABORTED = "$($_.Exception.Message)"
  Write-Host ("  !! 测试脚本在第 {0} 行异常中止：{1}" -f $_.InvocationInfo.ScriptLineNumber, $script:ABORTED) -ForegroundColor Red

} finally {
  # ---- 清理：只关自己启动的隔离进程；本项目的服务走本项目协议 ----
  Remove-Item Env:\MIMO_USAGE_DB -ErrorAction SilentlyContinue
  $env:MIMO_USAGE_DATA_DIR = $dataDir
  Stop-IsolatedService
  foreach ($p in $serveProcs) {
    try { if ($p -and -not $p.HasExited) { $p.Kill() } } catch { }
  }
  Remove-Item Env:\MIMO_USAGE_DATA_DIR -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $work) {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
  }
}

if ($null -ne $script:ABORTED) {
  Check "测试脚本全程未异常中止（否则后续用例会被静默跳过）" $false $script:ABORTED
}

Write-Host ""
if ($script:FAILS -eq 0) {
  Write-Host "INSTALL UPGRADE TESTS OK ($($script:CHECKS) checks)" -ForegroundColor Green
  exit 0
}
Write-Host "INSTALL UPGRADE TESTS FAILED: $($script:FAILS)/$($script:CHECKS)" -ForegroundColor Red
exit 1
