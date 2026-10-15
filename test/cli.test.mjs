// test/cli.test.mjs — npm 真实安装流程测试（pack → 解包校验 → npm install → 全生命周期）
//
// 覆盖用户要求的：
//   * npm pack 生成 tgz，且**包内文件与开发环境逐字节一致**；
//   * 发布包内容契约：含 bin/ 与 installer.py，不含 test/、不含 skill/；
//   * 用 npm install 把 tgz 装到一个**临时 prefix**（不动全局环境），并通过 npm 生成的
//     shim 调用 CLI；
//   * 用装好的 CLI 跑完 install → report → upgrade → doctor → uninstall；
//   * 降级护栏：默认拒绝（退出码 9）且不写任何文件，加 --allow-downgrade 才放行；
//   * 全程 MIMO_USAGE_CONFIG/--config、--install-dir、MIMO_USAGE_DATA_DIR、MIMO_USAGE_DB
//     都指向临时目录；测试结束前后校验**真实 mimocode.jsonc 与真实安装目录逐字节未变**。
//
// 运行：node test/cli.test.mjs
// 退出码：0 = PASS / 1 = FAIL

import { execFileSync, spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..');
const IS_WIN = process.platform === 'win32';
const PY = process.env.MIMO_USAGE_PY || (IS_WIN ? 'python' : 'python3');

let failures = 0;
function check(name, ok, extra = '') {
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  ' + extra : ''}`);
  if (!ok) failures += 1;
}

const q = (s) => (IS_WIN && /\s/.test(s) ? `"${s}"` : s);
function run(cmd, args, opts = {}) {
  const needsShell = IS_WIN && /\.(cmd|bat)$/i.test(cmd);
  const r = spawnSync(cmd, needsShell || opts.shell ? args.map(q) : args, {
    encoding: 'utf8',
    shell: Boolean(needsShell || opts.shell),
    cwd: opts.cwd || REPO,
    env: opts.env || process.env,
    maxBuffer: 64 * 1024 * 1024,
  });
  return {
    code: r.status === null ? -1 : r.status,
    out: `${r.stdout || ''}`,
    err: `${r.stderr || ''}`,
    error: r.error ? r.error.message : '',
  };
}
// npm 调用走 npm 自己的 CLI JS：仍是同一条 npm 代码路径（同样的 pack / install /
// shim 生成），但避免 Windows 上 .cmd + shell:true 带来的引号拼接与弃用告警。
function npmCli() {
  const cands = [
    path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js'),
    path.join(path.dirname(process.execPath), '..', 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js'),
    '/usr/lib/node_modules/npm/bin/npm-cli.js',
    '/usr/local/lib/node_modules/npm/bin/npm-cli.js',
  ];
  for (const c of cands) if (fs.existsSync(c)) return c;
  return null;
}
const NPM_CLI = npmCli();
const npm = (...args) => (NPM_CLI
  ? run('node', [NPM_CLI, ...args])
  : run('npm', args, { shell: IS_WIN }));

const isDir = (p) => { try { return fs.statSync(p).isDirectory(); } catch { return false; } };
const sha = (p) => (fs.existsSync(p)
  ? crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex') : '<missing>');

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'mimo-npm-test-'));
const prefix = path.join(work, 'prefix');
const extract = path.join(work, 'extract');
const cfgDir = path.join(work, 'mimocode');
const cfg = path.join(cfgDir, 'mimocode.jsonc');
const installDir = path.join(work, 'installed');
const dataDir = path.join(work, 'user-data');
const synthDb = path.join(work, 'synth.db');

// 真实环境的"不该被碰"基线（在 CI 上没有这些文件时自动跳过该断言）
const REAL_CFG = path.join(os.homedir(), '.config', 'mimocode', 'mimocode.jsonc');
const REAL_INSTALL = path.join(os.homedir(), '.config', 'mimocode', 'usage-dashboard');
const realBefore = { cfg: sha(REAL_CFG), cli: sha(path.join(REAL_INSTALL, 'usage_cli.py')) };

const SHIPPED = ['usage_cli.py', 'register_command.py', 'installer.py', 'install.ps1',
  'bin/mimo-usage.js', 'usage.cmd'];

try {
  fs.mkdirSync(cfgDir, { recursive: true });
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(cfg, '{\n  // 用户注释\n  "command": {}\n}\n', 'utf8');
  execFileSync(PY, ['-c',
    'import sys, time; sys.path.insert(0, sys.argv[1]); from test_python import make_db;'
    + 'make_db(sys.argv[2], int(time.time()*1000))', HERE, synthDb], { stdio: 'pipe' });

  const env = { ...process.env, MIMO_USAGE_DATA_DIR: dataDir, MIMO_USAGE_DB: synthDb };

  // ------------------------------------------------------------------ pack
  console.log('\n=== 1. npm pack 与包内容契约 ===');
  const packed = npm('pack', '--pack-destination', work);
  check('npm pack 成功', packed.code === 0, `(exit=${packed.code} ${packed.err.trim().slice(0, 120)})`);
  const tgzName = packed.out.trim().split('\n').filter((l) => l.endsWith('.tgz')).pop();
  const tgz = path.join(work, tgzName || 'missing.tgz');
  check('生成 tgz', fs.existsSync(tgz), `(${tgzName})`);

  fs.mkdirSync(extract, { recursive: true });
  const untar = run('tar', ['-xzf', tgz, '-C', extract], { shell: IS_WIN });
  check('tgz 可解包', untar.code === 0, `(exit=${untar.code})`);
  const pkgRoot = path.join(extract, 'package');
  check('解包出 package/ 目录', isDir(pkgRoot));

  for (const rel of SHIPPED) {
    const inPkg = path.join(pkgRoot, rel);
    check(`包内存在 ${rel}`, fs.existsSync(inPkg));
    check(`包内 ${rel} 与开发环境逐字节一致`, sha(inPkg) === sha(path.join(REPO, rel)),
      `(pkg=${sha(inPkg).slice(0, 12)} dev=${sha(path.join(REPO, rel)).slice(0, 12)})`);
  }
  check('发布包不含 test/', !fs.existsSync(path.join(pkgRoot, 'test')));
  check('发布包不含 skill/', !fs.existsSync(path.join(pkgRoot, 'skill')));
  check('发布包声明了 bin', (() => {
    try {
      const pj = JSON.parse(fs.readFileSync(path.join(pkgRoot, 'package.json'), 'utf8'));
      return pj.bin && pj.bin['mimo-usage-dashboard'] === 'bin/mimo-usage.js';
    } catch { return false; }
  })());

  // ------------------------------------------------------------------ npm install
  console.log('\n=== 2. 用 npm install 装到临时 prefix（不动全局）===');
  const inst = npm('install', '--prefix', prefix, tgz, '--no-audit', '--no-fund', '--loglevel=error');
  check('npm install <tgz> 成功', inst.code === 0,
    `(exit=${inst.code} ${inst.err.trim().slice(0, 160)})`);
  const installedPkgDir = path.join(prefix, 'node_modules', 'mimo-usage-dashboard');
  check('包被装到 prefix/node_modules', isDir(installedPkgDir));
  const shim = path.join(prefix, 'node_modules', '.bin',
    IS_WIN ? 'mimo-usage-dashboard.cmd' : 'mimo-usage-dashboard');
  check('npm 生成了可执行 shim', fs.existsSync(shim), `(${path.basename(shim)})`);

  const cliJs = path.join(installedPkgDir, 'bin', 'mimo-usage.js');
  const cli = (...args) => run('node', [cliJs, ...args], { env });
  const cliViaShim = (...args) => run(shim, args, { env, shell: IS_WIN });

  // ------------------------------------------------------------------ version
  console.log('\n=== 3. shim 可用 + version / where ===');
  const v = cliViaShim('version', '--install-dir', installDir);
  check('通过 npm shim 调用 CLI 成功', v.code === 0, `(exit=${v.code} ${v.err.trim().slice(0, 120)})`);
  check('shim 输出含 cli 版本', /cli\s*:\s*\d+\.\d+\.\d+/.test(v.out), `(${v.out.split('\n')[0]})`);
  check('未安装时 version 显示 (未安装)', v.out.includes('(未安装)'));

  const w = cli('where', '--install-dir', installDir, '--config', cfg);
  check('where 退出码 0', w.code === 0, `(exit=${w.code})`);
  check('where 打印 dataDir 与 installDir', w.out.includes(dataDir) && w.out.includes(installDir));

  // ------------------------------------------------------------------ doctor（未安装）
  console.log('\n=== 4. doctor（未安装 → 退出码 1）===');
  const d0 = cli('doctor', '--install-dir', installDir, '--config', cfg);
  check('doctor 退出码 1（有警告）', d0.code === 1, `(exit=${d0.code})`);
  check('doctor 指出未安装/未注册',
    d0.out.includes('没有 install-manifest.json') && d0.out.includes('尚未注册'));

  // ------------------------------------------------------------------ install
  console.log('\n=== 5. install（真实注册到临时配置）===');
  const i1 = cli('install', '--install-dir', installDir, '--config', cfg);
  check('install 退出码 0', i1.code === 0, `(exit=${i1.code})`);
  check('两个脚本已就位',
    fs.existsSync(path.join(installDir, 'usage_cli.py'))
    && fs.existsSync(path.join(installDir, 'register_command.py')));
  check('manifest 已写入', fs.existsSync(path.join(installDir, 'install-manifest.json')));
  const cfgText = fs.readFileSync(cfg, 'utf8');
  check('配置注释保留', cfgText.includes('// 用户注释'));
  const cfgJson = JSON.parse(cfgText.replace(/^\s*\/\/.*$/gm, ''));
  check('command.usage 与 command.usage-open 已注册',
    Boolean(cfgJson.command?.usage && cfgJson.command?.['usage-open']));
  check('模板指向临时安装目录',
    String(cfgJson.command.usage.template).includes(path.join(installDir, 'usage_cli.py').replace(/\\/g, '\\')));

  const d1 = cli('doctor', '--install-dir', installDir, '--config', cfg);
  check('安装后 doctor 退出码 0', d1.code === 0, `(exit=${d1.code})`);
  check('doctor 报告版本一致', d1.out.includes('已安装版本与当前包一致'));

  // ------------------------------------------------------------------ report（= usage）
  console.log('\n=== 6. report（等价 /usage，只读数据库）===');
  const outHtml = path.join(work, 'snapshot.html');
  const rep = cli('report', '--install-dir', installDir, '--config', cfg, '--out', outHtml);
  check('report 退出码 0', rep.code === 0, `(exit=${rep.code})`);
  check('生成了 HTML 快照', fs.existsSync(outHtml) && fs.statSync(outHtml).size > 20000,
    `(${fs.existsSync(outHtml) ? fs.statSync(outHtml).size : 0} 字节)`);
  const html = fs.existsSync(outHtml) ? fs.readFileSync(outHtml, 'utf8') : '';
  check('快照含三视图内联数据',
    html.includes('var DATA') && html.includes('"token"') && html.includes('"session"')
    && html.includes('"context"'));

  // ------------------------------------------------------------------ open（只验证解析，不拉浏览器）
  console.log('\n=== 7. open 的路径解析（不启动浏览器）===');
  const openMissing = cli('open', '--install-dir', path.join(work, 'no-such-install'), '--config', cfg);
  check('未安装时 open 给出可执行指引', openMissing.code !== 0 && openMissing.out.includes('请先运行'),
    `(exit=${openMissing.code})`);

  // ------------------------------------------------------------------ upgrade（同版本 → 无写入）
  console.log('\n=== 8. upgrade（同版本 → 不写入）===');
  const cfgHashBefore = sha(cfg);
  const u1 = cli('upgrade', '--install-dir', installDir, '--config', cfg);
  check('upgrade 退出码 0', u1.code === 0, `(exit=${u1.code})`);
  check('upgrade 报告 Already installed', /Already installed/i.test(u1.out));
  check('upgrade 未改动配置', sha(cfg) === cfgHashBefore);

  // ------------------------------------------------------------------ 降级护栏
  console.log('\n=== 8b. 降级：默认拒绝，--allow-downgrade 才放行 ===');
  const pkgVersion = JSON.parse(fs.readFileSync(path.join(REPO, 'package.json'), 'utf8')).version;
  const manifestPath = path.join(installDir, 'install-manifest.json');
  const installedCli = path.join(installDir, 'usage_cli.py');
  const backupDir = path.join(installDir, '.mimo-usage-backups');
  const currentHash = sha(installedCli);
  // 伪造成"已经装了更新的版本"：文件内容与当前包不同，但 manifest 哈希同步，
  // 所以归属仍判为 owned —— 是一个干净的降级场景，不是伪造的混装状态。
  fs.appendFileSync(installedCli, '# NEWER RELEASE FROM THE FUTURE\n', 'utf8');
  const forged = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  forged.version = '99.0.0';
  forged.files['usage_cli.py'].sha256 = sha(installedCli);
  fs.writeFileSync(manifestPath, JSON.stringify(forged, null, 2) + '\n', 'utf8');
  const staleHash = sha(installedCli);
  const cfgHashPre = sha(cfg);
  const backupsPre = fs.readdirSync(backupDir).length;

  const v2 = cliViaShim('version', '--install-dir', installDir);
  check('version 认出已装版本更高并提示需开关',
    v2.out.includes('99.0.0') && v2.out.includes('--allow-downgrade'));

  const u2 = cli('upgrade', '--install-dir', installDir, '--config', cfg);
  check('upgrade 拒绝降级（退出码 9）', u2.code === 9, `(exit=${u2.code})`);
  check('拒绝时说明原因', u2.out.includes('拒绝降级'));
  check('拒绝时未改动受管文件', sha(installedCli) === staleHash);
  check('拒绝时未改动配置', sha(cfg) === cfgHashPre);
  check('拒绝时未新增备份', fs.readdirSync(backupDir).length === backupsPre);

  const u3 = cli('upgrade', '--install-dir', installDir, '--config', cfg, '--allow-downgrade');
  check('--allow-downgrade 后退出码 0', u3.code === 0, `(exit=${u3.code})`);
  check('降级后受管文件换成当前版本', sha(installedCli) === currentHash);
  check('降级后 manifest 记录当前版本',
    JSON.parse(fs.readFileSync(manifestPath, 'utf8')).version === pkgVersion);
  check('降级前的旧文件已备份（可回退）', fs.readdirSync(backupDir).length > backupsPre);

  // ------------------------------------------------------------------ uninstall
  console.log('\n=== 9. uninstall（保留用户数据）===');
  const report2 = path.join(dataDir, 'mimo-usage-all-20260101.html');
  fs.writeFileSync(report2, '<html>user report</html>', 'utf8');
  const unknown = path.join(installDir, 'MY-NOTES.txt');
  fs.writeFileSync(unknown, 'user notes', 'utf8');

  const un = cli('uninstall', '--install-dir', installDir, '--config', cfg);
  check('uninstall 退出码 0', un.code === 0, `(exit=${un.code})`);
  check('受管文件已删除',
    !fs.existsSync(path.join(installDir, 'usage_cli.py'))
    && !fs.existsSync(path.join(installDir, 'register_command.py')));
  check('manifest 已删除', !fs.existsSync(path.join(installDir, 'install-manifest.json')));
  check('用户报告保留', fs.existsSync(report2));
  check('未知文件保留', fs.existsSync(unknown));
  check('备份目录保留', isDir(path.join(installDir, '.mimo-usage-backups')));
  const cfgAfterUn = fs.readFileSync(cfg, 'utf8');
  check('卸载后注释仍在', cfgAfterUn.includes('// 用户注释'));
  check('卸载后不再有本项目命令',
    Object.keys(JSON.parse(cfgAfterUn.replace(/^\s*\/\/.*$/gm, '')).command || {}).length === 0);
  const d2 = cli('doctor', '--install-dir', installDir, '--config', cfg);
  check('卸载后 doctor 回到退出码 1', d2.code === 1, `(exit=${d2.code})`);

  // ------------------------------------------------------------------ 隔离性
  console.log('\n=== 10. 隔离性：真实环境逐字节未变 ===');
  if (fs.existsSync(REAL_CFG) || realBefore.cfg !== '<missing>') {
    check('真实 mimocode.jsonc 未被触碰', sha(REAL_CFG) === realBefore.cfg,
      `(before=${realBefore.cfg.slice(0, 12)} after=${sha(REAL_CFG).slice(0, 12)})`);
    check('真实安装目录的 usage_cli.py 未被触碰',
      sha(path.join(REAL_INSTALL, 'usage_cli.py')) === realBefore.cli);
  } else {
    console.log('  SKIP  本机没有真实 MiMo 环境（CI 场景），跳过该断言');
  }
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}

console.log('');
if (failures === 0) {
  console.log('CLI / NPM TESTS OK');
  process.exit(0);
}
console.log(`CLI / NPM TESTS FAILED: ${failures}`);
process.exit(1);
