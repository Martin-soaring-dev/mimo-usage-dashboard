#!/usr/bin/env node
// MiMo Desktop Usage Dashboard — npm 入口（零第三方依赖）
//
// 这一层只做三件事，事务逻辑一律在 installer.py（唯一事实来源）里：
//   1) 找到"能启动引擎"的 Python 解释器；
//   2) 把动词映射成引擎子命令并转发参数；
//   3) 把引擎的退出码原样带回（脚本可依赖）。
//
// 用法：
//   mimo-usage-dashboard install   [--config P] [--install-dir P] [--python EXE]
//                                  [--force] [--remove-legacy] [--preflight-only]
//   mimo-usage-dashboard upgrade   [同上] [--allow-downgrade]
//   mimo-usage-dashboard uninstall [--config P] [--install-dir P] [--python EXE]
//                                  [--keep-files] [--purge-backups] [--force]
//   mimo-usage-dashboard doctor    [--config P] [--install-dir P] [--python EXE]
//   mimo-usage-dashboard where     [同上]
//   mimo-usage-dashboard version
//   mimo-usage-dashboard open      [--config P] [--install-dir P] [--python EXE] [其它参数转发]
//   mimo-usage-dashboard report    [同上]
//
// 退出码：0 成功/健康 · 1 失败 · 2 参数错误 · 3 配置无法解析 · 4 路径缺失
//         5 写入失败 · 6 配置无法安全就地修改 · 7 需人工确认 · 9 拒绝降级
//         （全部来自引擎；9 表示拒绝降级。本 CLI 只做更快的提示，
//           判断权在 installer.py，它对任何入口都做同样的检查。）
//
// 交互约定：**任何命令都不会阻塞等待输入**，可以直接在脚本/CI/AI 里调用。
// 破坏性操作一律需要显式开关（--force / --purge-backups / --allow-downgrade）。

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PKG_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ENGINE = path.join(PKG_DIR, 'installer.py');
const CLI_VERSION = readPackageVersion();

const EXIT = { OK: 0, FAIL: 1, ARGS: 2, DOWNGRADE_REFUSED: 9 };
const MIN_PYTHON = [3, 10];

function readPackageVersion() {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(PKG_DIR, 'package.json'), 'utf8'));
    return String(pkg.version || '0.0.0');
  } catch {
    return '0.0.0';
  }
}

// 版本必须按数值段比较，不能按字符串比：字符串下 "0.2.0" > "0.10.0"，
// 会把合法升级误判成降级、又把真正的降级放行。预发布后缀（-beta.1）按 semver
// 只在同号时区分，这里直接忽略：本项目的发布版本不带后缀。
function compareVersions(a, b) {
  const parts = (v) => String(v ?? '').split('-')[0].split('.');
  const x = parts(a);
  const y = parts(b);
  for (let i = 0; i < 3; i += 1) {
    const nx = parseInt(x[i], 10) || 0;
    const ny = parseInt(y[i], 10) || 0;
    if (nx !== ny) return nx < ny ? -1 : 1;
  }
  return 0;
}

function say(line = '') {
  process.stdout.write(line + '\n');
}

function usage() {
  say('MiMo Desktop Usage Dashboard');
  say('');
  say('  mimo-usage-dashboard install      把看板安装进 MiMo（首次安装/重复安装都会走同一条安全路径）');
  say('  mimo-usage-dashboard upgrade      升级到当前 CLI 自带的版本（拒绝静默降级）');
  say('  mimo-usage-dashboard uninstall    注销命令并删除本项目文件（保留你的报告/数据库/备份）');
  say('  mimo-usage-dashboard doctor       只读体检：Python、配置、注册、安装状态、运行实例、残留');
  say('  mimo-usage-dashboard where        打印所有关键路径');
  say('  mimo-usage-dashboard version      打印 CLI 版本与已安装版本');
  say('  mimo-usage-dashboard open         打开实时看板（等价 usage_cli.py --open）');
  say('  mimo-usage-dashboard report       生成静态快照并打印路径');
  say('');
  say('注意：`npm install -g mimo-usage-dashboard` 只是把本 CLI 装到 PATH；');
  say('      真正把看板装进 MiMo 需要再运行 `mimo-usage-dashboard install`。');
  say('');
  say('公共开关：--config <mimocode.jsonc>  --install-dir <dir>  --python <python.exe>');
  say('升级开关：upgrade --allow-downgrade  仅在显式指定时才允许装回旧版本（会先备份）');
}

// ---------------------------------------------------------------------------
// 解释器定位
// ---------------------------------------------------------------------------
//
// 引擎必须由 Python 启动（先有鸡还是先有蛋），所以这里有一份"候选解释器"清单。
// 这是**有意的、有界的重复**：install.ps1 只覆盖 Python 3.13/3.12 的固定安装路径，
// 本清单则多覆盖 3.11/3.10（npm CLI 的用户环境更杂，网撒得更宽；这是清单覆盖
// 范围的差异，不是版本策略的差异——版本与路径的最终裁定权在 installer.py，
// 它对指定的解释器 fail closed）。清单漏掉的后果只是"本 CLI 找不到 Python"，
// 会给出明确提示，不会造成危险行为。

function versionOf(exe) {
  let r;
  try {
    r = spawnSync(exe, ['--version'], { encoding: 'utf8' });
  } catch {
    return null;
  }
  const text = `${r.stdout || ''}${r.stderr || ''}`;
  const m = /Python\s+(\d+)\.(\d+)/.exec(text);
  if (!m) return null;
  return [Number(m[1]), Number(m[2])];
}

function isSupported(v) {
  if (!v) return false;
  return v[0] > MIN_PYTHON[0] || (v[0] === MIN_PYTHON[0] && v[1] >= MIN_PYTHON[1]);
}

function candidates(explicit) {
  const list = [];
  if (explicit) list.push(explicit);
  if (process.env.MIMO_USAGE_PY) list.push(process.env.MIMO_USAGE_PY);
  list.push('python', 'python3');
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA;
    if (local) {
      for (const minor of [13, 12, 11, 10]) {
        list.push(path.join(local, 'Programs', 'Python', `Python3${minor}`, 'python.exe'));
      }
    }
    for (const minor of [13, 12, 11, 10]) list.push(`C:\\Python3${minor}\\python.exe`);
  }
  return list;
}

function resolvePython(explicit) {
  const tried = [];
  for (const cand of candidates(explicit)) {
    if (!cand) continue;
    if (path.isAbsolute(cand) && !fs.existsSync(cand)) {
      tried.push(`${cand}（不存在）`);
      if (explicit && cand === explicit) {
        say(`指定的 --python 不存在：${explicit}（不会自动回退到其它解释器）`);
        return null;
      }
      continue;
    }
    const ver = versionOf(cand);
    if (isSupported(ver)) return cand;
    if (ver) {
      tried.push(`${cand}（Python ${ver[0]}.${ver[1]} < ${MIN_PYTHON[0]}.${MIN_PYTHON[1]}）`);
    } else {
      tried.push(`${cand}（无法识别为 Python）`);
    }
    if (explicit && cand === explicit) {
      say(`指定的 --python 不受支持：${explicit}（${tried[tried.length - 1].split('（')[1] || '无法识别'}）`);
      return null;
    }
  }
  say(`未找到满足 >= ${MIN_PYTHON[0]}.${MIN_PYTHON[1]} 的 Python 解释器。`);
  say('请安装 Python 3.10+，或用 --python <path> / 环境变量 MIMO_USAGE_PY 显式指定。');
  if (tried.length) {
    say('');
    say('已尝试：');
    for (const t of tried) say(`  - ${t}`);
  }
  return null;
}

// ---------------------------------------------------------------------------
// 参数解析（只解析本 CLI 自己要用的三个路径开关，其余原样转发给引擎）
// ---------------------------------------------------------------------------

const PATH_FLAGS = new Set(['--config', '--install-dir', '--python']);

function splitArgs(argv) {
  const own = {};
  const rest = [];
  for (let i = 0; i < argv.length; i += 1) {
    const tok = argv[i];
    if (PATH_FLAGS.has(tok)) {
      const val = argv[i + 1];
      if (val === undefined) {
        say(`参数错误：${tok} 缺少取值`);
        return null;
      }
      own[tok] = val;
      i += 1;
      continue;
    }
    // --config=... 形式
    const eq = /^(--config|--install-dir|--python)=(.*)$/.exec(tok);
    if (eq) {
      own[eq[1]] = eq[2];
      continue;
    }
    rest.push(tok);
  }
  return { own, rest };
}

function installDirOf(own) {
  if (own['--install-dir']) return path.resolve(own['--install-dir']);
  const cfgDir = own['--config']
    ? path.dirname(path.resolve(own['--config']))
    : path.join(process.env.USERPROFILE || process.env.HOME || '.', '.config', 'mimocode');
  return path.join(cfgDir, 'usage-dashboard');
}

function readManifest(installDir) {
  const p = path.join(installDir, 'install-manifest.json');
  try {
    const data = JSON.parse(fs.readFileSync(p, 'utf8'));
    return data && data.product === 'mimo-usage-dashboard' ? data : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// 各动词
// ---------------------------------------------------------------------------

function runEngine(python, engineArgs) {
  const r = spawnSync(python, [ENGINE, ...engineArgs], { stdio: 'inherit' });
  if (r.error) {
    say(`无法启动引擎（${python} installer.py）：${r.error.message}`);
    return EXIT.FAIL;
  }
  return r.status === null ? EXIT.FAIL : r.status;
}

function cmdDelegate(python, own, verb, extra = []) {
  const args = [verb];
  if (own['--config']) args.push('--config', own['--config']);
  if (own['--install-dir']) args.push('--install-dir', own['--install-dir']);
  args.push('--python', python);
  return runEngine(python, [...args, ...extra]);
}

function cmdVersion(python, own) {
  const installDir = installDirOf(own);
  const manifest = readManifest(installDir);
  say(`cli        : ${CLI_VERSION}`);
  say(`installed  : ${manifest ? manifest.version : '(未安装)'}`);
  say(`installDir : ${installDir}`);
  say(`engine     : ${ENGINE}`);
  if (manifest && manifest.version !== CLI_VERSION) {
    say('');
    if (compareVersions(manifest.version, CLI_VERSION) < 0) {
      say(`提示：已安装 ${manifest.version}，当前 CLI 是 ${CLI_VERSION} → 可运行 upgrade。`);
    } else {
      say(`提示：已安装 ${manifest.version} 比当前 CLI(${CLI_VERSION}) 更新 → 升级会降级，需显式 --allow-downgrade。`);
    }
  }
  return EXIT.OK;
}

function cmdUpgrade(python, own, rest) {
  const allowDowngrade = rest.includes('--allow-downgrade');
  // --allow-downgrade 必须继续传给引擎：引擎才是降级判断的唯一事实来源，
  // 本 CLI 的这一层只是为了少起一次子进程、给出更快的提示。
  const forward = rest.filter((a) => a !== '--allow-downgrade');
  if (allowDowngrade) forward.push('--allow-downgrade');
  const installDir = installDirOf(own);
  const manifest = readManifest(installDir);

  if (!manifest) {
    say('尚未安装（没有 install-manifest.json）→ 按首次安装处理。');
    return cmdDelegate(python, own, 'install', forward);
  }
  const installed = String(manifest.version);
  if (compareVersions(installed, CLI_VERSION) === 0) {
    say(`Already installed (version ${installed}) — 无需改动，也不会有任何写入。`);
    return EXIT.OK;
  }
  if (compareVersions(installed, CLI_VERSION) > 0 && !allowDowngrade) {
    say(`拒绝降级：已安装 ${installed}，当前 CLI 是 ${CLI_VERSION}。`);
    say('如确实要降级，请显式加 --allow-downgrade（会先备份，且备份会保留）。');
    return EXIT.DOWNGRADE_REFUSED;
  }
  say(`升级：${installed} → ${CLI_VERSION}`);
  return cmdDelegate(python, own, 'install', forward);
}

function cmdRunCli(python, own, verb, rest) {
  const installDir = installDirOf(own);
  const script = path.join(installDir, 'usage_cli.py');
  if (!fs.existsSync(script)) {
    say(`找不到已安装的看板脚本：${script}`);
    say('请先运行：mimo-usage-dashboard install');
    return EXIT.FAIL;
  }
  const extra = verb === 'open' ? ['--open'] : [];
  const r = spawnSync(python, [script, ...rest, ...extra], { stdio: 'inherit' });
  if (r.error) {
    say(`无法启动 ${script}：${r.error.message}`);
    return EXIT.FAIL;
  }
  return r.status === null ? EXIT.FAIL : r.status;
}

// ---------------------------------------------------------------------------

function main() {
  const argv = process.argv.slice(2);
  if (argv.length === 0 || argv[0] === 'help' || argv[0] === '--help' || argv[0] === '-h') {
    usage();
    return EXIT.OK;
  }
  const verb = argv[0];
  const KNOWN = ['install', 'upgrade', 'uninstall', 'doctor', 'where', 'version', 'open', 'report'];
  if (!KNOWN.includes(verb)) {
    say(`未知命令：${verb}`);
    say('');
    usage();
    return EXIT.ARGS;
  }

  const parsed = splitArgs(argv.slice(1));
  if (parsed === null) return EXIT.ARGS;
  const { own, rest } = parsed;

  if (verb === 'version') return cmdVersion(null, own);

  if (!fs.existsSync(ENGINE)) {
    say(`安装包不完整：缺少 ${ENGINE}`);
    say('请重新安装：npm install -g mimo-usage-dashboard');
    return EXIT.FAIL;
  }

  const python = resolvePython(own['--python']);
  if (!python) return EXIT.FAIL;

  switch (verb) {
    case 'install':
      return cmdDelegate(python, own, 'install', rest);
    case 'upgrade':
      return cmdUpgrade(python, own, rest);
    case 'uninstall':
      return cmdDelegate(python, own, 'uninstall', rest);
    case 'doctor':
      return cmdDelegate(python, own, 'doctor', rest);
    case 'where':
      return cmdDelegate(python, own, 'where', rest);
    case 'open':
      return cmdRunCli(python, own, 'open', rest);
    case 'report':
      return cmdRunCli(python, own, 'report', rest);
    default:
      usage();
      return EXIT.ARGS;
  }
}

process.exit(main());
