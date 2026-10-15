#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_installer.py — installer.py 引擎的隔离测试（uninstall / doctor / 残留体检）

只使用临时目录 + 合成数据：
  * --config / --install-dir 全部指向临时目录；
  * MIMO_USAGE_DATA_DIR 指向临时目录，绝不读真实用户数据目录；
  * 绝不运行真实 /usage，绝不触碰真实 mimocode.jsonc 与真实数据库。

运行：python test/test_installer.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import installer as I  # noqa: E402
import register_command as rc  # noqa: E402

FAILS = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global FAILS
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        FAILS += 1


def run(args: list) -> tuple[int, str]:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = I.main(args)
    finally:
        sys.stdout = old
    return code, buf.getvalue()


def sha(path: str) -> str:
    return I.sha256_file(path) or "<missing>"


def new_env(root: str):
    """造一套隔离环境：config + install-dir + data-dir。"""
    cfg_dir = os.path.join(root, "mimocode")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = os.path.join(cfg_dir, "mimocode.jsonc")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write('{\n  // 用户注释\n  "command": {}\n}\n')
    install_dir = os.path.join(root, "installed")
    data_dir = os.path.join(root, "user-data")
    os.makedirs(data_dir, exist_ok=True)
    os.environ["MIMO_USAGE_DATA_DIR"] = data_dir
    return cfg, install_dir, data_dir


def do_install(cfg: str, install_dir: str, extra=()):
    return run(["install", "--config", cfg, "--install-dir", install_dir,
                "--python", sys.executable, *extra])


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="mimo-installer-test-")
    old_data = os.environ.get("MIMO_USAGE_DATA_DIR")
    old_db = os.environ.get("MIMO_USAGE_DB")
    os.environ["MIMO_USAGE_DB"] = os.path.join(tmpdir, "definitely-missing.db")
    try:
        # ================================================================
        print("\n=== 1. doctor：未安装状态 ===")
        cfg, install_dir, data_dir = new_env(os.path.join(tmpdir, "d1"))
        code, sout = run(["doctor", "--config", cfg, "--install-dir", install_dir,
                          "--python", sys.executable])
        check("doctor 退出码 1（有警告：未安装/未注册）", code == 1, f"(code={code})")
        check("doctor 明确说没有 manifest", "没有 install-manifest.json" in sout)
        check("doctor 明确说尚未注册", "尚未注册 /usage" in sout)
        check("doctor 报告残留区块", "残留·报告" in sout and "残留·备份" in sout)

        print("\n=== 2. install 后 doctor 变健康 ===")
        code, sout = do_install(cfg, install_dir)
        check("安装成功", code == 0, f"(code={code})")
        code, sout = run(["doctor", "--config", cfg, "--install-dir", install_dir,
                          "--python", sys.executable])
        check("doctor 退出码 0（健康）", code == 0, f"(code={code})")
        check("doctor 说版本一致", "已安装版本与当前包一致" in sout)
        check("doctor 说两个文件均 owned", "owned" in sout)

        print("\n=== 3. doctor：配置无法解析 → 退出码 2 ===")
        cfg3, inst3, _ = new_env(os.path.join(tmpdir, "d3"))
        with open(cfg3, "w", encoding="utf-8") as fh:
            fh.write("{ not json\n")
        code, sout = run(["doctor", "--config", cfg3, "--install-dir", inst3,
                          "--python", sys.executable])
        check("doctor 退出码 2（有错误）", code == 2, f"(code={code})")
        check("doctor 报出形态问题", "unparseable" in sout or "配置" in sout)

        # ================================================================
        print("\n=== 4. uninstall 默认策略：删可确认归属的，保留用户数据 ===")
        cfg4, inst4, data4 = new_env(os.path.join(tmpdir, "u4"))
        code, _ = do_install(cfg4, inst4)
        check("基线安装成功", code == 0, f"(code={code})")
        # 用户数据 + 未知文件，都必须活下来
        report = os.path.join(data4, "mimo-usage-all-20260101.html")
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("<html>user report</html>")
        unknown = os.path.join(inst4, "MY-NOTES.txt")
        with open(unknown, "w", encoding="utf-8") as fh:
            fh.write("user notes")
        backup_dir = os.path.join(inst4, I.BACKUP_DIRNAME)
        check("安装产生了备份目录", os.path.isdir(backup_dir))
        cfg_text_before = io.open(cfg4, encoding="utf-8").read()
        check("安装后配置里仍有用户注释", "用户注释" in cfg_text_before)

        code, sout = run(["uninstall", "--config", cfg4, "--install-dir", inst4,
                          "--python", sys.executable])
        check("uninstall 退出码 0", code == 0, f"(code={code})")
        check("已注销两个命令", run(["doctor", "--config", cfg4, "--install-dir", inst4,
                                     "--python", sys.executable])[1].count("尚未注册") >= 1)
        check("受管文件已删除", not os.path.isfile(os.path.join(inst4, "usage_cli.py"))
              and not os.path.isfile(os.path.join(inst4, "register_command.py")))
        check("manifest 已删除", not os.path.isfile(os.path.join(inst4, I.MANIFEST_NAME)))
        check("用户报告保留", os.path.isfile(report))
        check("未知文件保留", os.path.isfile(unknown))
        check("备份目录保留", os.path.isdir(backup_dir))
        cfg_text_after = io.open(cfg4, encoding="utf-8").read()
        check("配置里其它内容保留（注释仍在）", "用户注释" in cfg_text_after)
        j4 = json.loads(rc.strip_jsonc(cfg_text_after))
        check("配置里不再有本项目命令", sorted(j4.get("command", {})) == [])
        check("安装目录因非空而保留（未知文件还在）", os.path.isdir(inst4))
        check("明确打印保留了什么", "保留" in sout)

        print("\n=== 5. uninstall --keep-files：只注销，不删文件 ===")
        cfg5, inst5, _ = new_env(os.path.join(tmpdir, "u5"))
        do_install(cfg5, inst5)
        h5 = sha(os.path.join(inst5, "usage_cli.py"))
        code, sout = run(["uninstall", "--config", cfg5, "--install-dir", inst5,
                          "--python", sys.executable, "--keep-files"])
        check("退出码 0", code == 0, f"(code={code})")
        check("--keep-files 时文件仍在且未被改动", sha(os.path.join(inst5, "usage_cli.py")) == h5)
        check("--keep-files 时 manifest 仍在", os.path.isfile(os.path.join(inst5, I.MANIFEST_NAME)))
        check("命令已注销", "尚未注册" in run(["doctor", "--config", cfg5, "--install-dir", inst5,
                                             "--python", sys.executable])[1])

        print("\n=== 6. 被用户修改过的受管文件：默认保留，--force 才删 ===")
        cfg6, inst6, _ = new_env(os.path.join(tmpdir, "u6"))
        do_install(cfg6, inst6)
        cli6 = os.path.join(inst6, "usage_cli.py")
        with open(cli6, "a", encoding="utf-8") as fh:
            fh.write("# 用户自己改过这一行\n")
        code, sout = run(["uninstall", "--config", cfg6, "--install-dir", inst6,
                          "--python", sys.executable])
        check("退出码 0", code == 0, f"(code={code})")
        check("被修改的文件被保留", os.path.isfile(cli6))
        check("明确说明为什么保留", "归属无法确认或已被你修改" in sout)

        cfg6b, inst6b, _ = new_env(os.path.join(tmpdir, "u6b"))
        do_install(cfg6b, inst6b)
        cli6b = os.path.join(inst6b, "usage_cli.py")
        with open(cli6b, "a", encoding="utf-8") as fh:
            fh.write("# 用户改过\n")
        code, _ = run(["uninstall", "--config", cfg6b, "--install-dir", inst6b,
                       "--python", sys.executable, "--force"])
        check("--force 时被修改的文件也会删除", code == 0 and not os.path.isfile(cli6b))

        print("\n=== 7. 无 manifest 且无归属标记：永不删除 ===")
        cfg7, inst7, _ = new_env(os.path.join(tmpdir, "u7"))
        os.makedirs(inst7, exist_ok=True)
        stranger = os.path.join(inst7, "usage_cli.py")
        with open(stranger, "w", encoding="utf-8") as fh:
            fh.write("# 别人的同名文件\n")
        h7 = sha(stranger)
        code, sout = run(["uninstall", "--config", cfg7, "--install-dir", inst7,
                          "--python", sys.executable])
        check("退出码 0（没什么可注销/删除）", code == 0, f"(code={code})")
        check("陌生文件字节未变", sha(stranger) == h7)
        check("说明没有可删除的文件", "没有能证明归属的文件" in sout)

        print("\n=== 8. uninstall --purge-backups：先列出再删 ===")
        cfg8, inst8, _ = new_env(os.path.join(tmpdir, "u8"))
        do_install(cfg8, inst8)
        bdir8 = os.path.join(inst8, I.BACKUP_DIRNAME)
        check("备份目录存在", os.path.isdir(bdir8))
        cfg_baks_before = [n for n in os.listdir(os.path.dirname(cfg8))
                           if n.startswith("mimocode.jsonc.bak-")]
        check("配置备份存在", len(cfg_baks_before) >= 1, f"({cfg_baks_before})")
        code, sout = run(["uninstall", "--config", cfg8, "--install-dir", inst8,
                          "--python", sys.executable, "--purge-backups"])
        check("退出码 0", code == 0, f"(code={code})")
        check("备份目录已清理", not os.path.isdir(bdir8))
        check("配置备份已清理", [n for n in os.listdir(os.path.dirname(cfg8))
                                if n.startswith("mimocode.jsonc.bak-")] == [])
        check("清理前打印了清单", "purge" in sout)

        print("\n=== 9. 配置无法就地修改时：fail closed，不删任何文件 ===")
        cfg9, inst9, _ = new_env(os.path.join(tmpdir, "u9"))
        do_install(cfg9, inst9)
        cli9 = os.path.join(inst9, "usage_cli.py")
        h9 = sha(cli9)
        with open(cfg9, "w", encoding="utf-8") as fh:
            fh.write('{"command": {"usage": {}, "usage": {}}}\n')  # 重复键 → fail closed
        code, sout = run(["uninstall", "--config", cfg9, "--install-dir", inst9,
                          "--python", sys.executable])
        check("退出码非 0", code != 0, f"(code={code})")
        check("明确说 fail closed", "fail closed" in sout)
        check("受管文件未被删除", sha(cli9) == h9)
        check("manifest 未被删除", os.path.isfile(os.path.join(inst9, I.MANIFEST_NAME)))

        # ================================================================
        print("\n=== 10. 残留体检：锁文件与报告/备份只报告不删 ===")
        cfg10, inst10, data10 = new_env(os.path.join(tmpdir, "r10"))
        do_install(cfg10, inst10)
        with open(os.path.join(data10, "mimo-usage-all-20260101.html"), "w",
                  encoding="utf-8") as fh:
            fh.write("<html>r</html>")
        # 失效锁：持有者 PID 不可能存活
        with open(os.path.join(data10, "live-server.lock"), "w", encoding="ascii") as fh:
            fh.write("999999999\n")
        code, sout = run(["install", "--config", cfg10, "--install-dir", inst10,
                          "--python", sys.executable, "--preflight-only"])
        check("preflight-only 退出码 0", code == 0, f"(code={code})")
        check("报告了锁文件状态", "残留·锁" in sout)
        check("识别为失效锁", "已退出" in sout or "失效锁" in sout)
        check("报告了报告份数", "残留·报告" in sout)
        check("残留提示不影响继续（未要求 -Force）", "需 -Force" not in sout or "无法证明归属" not in sout)
        check("锁文件未被删除", os.path.isfile(os.path.join(data10, "live-server.lock")))
        check("报告未被删除", os.path.isfile(os.path.join(data10, "mimo-usage-all-20260101.html")))

        print("\n=== 11. 参数校验：动作不接受不属于它的开关 ===")
        code, sout = run(["doctor", "--config", cfg10, "--install-dir", inst10,
                          "--python", sys.executable, "--keep-files"])
        check("doctor + --keep-files → 参数错误", code == I.EXIT_ARGS, f"(code={code})")
        check("明确说哪个开关不被接受", "--keep-files" in sout)

        print("\n=== 12. 版本必须按数值段比较（字符串比较会反向）===")
        check("0.2.0 < 0.10.0", I.compare_versions("0.2.0", "0.10.0") < 0)
        check("0.10.0 > 0.9.0", I.compare_versions("0.10.0", "0.9.0") > 0)
        check("1.0.0 == 1.0.0", I.compare_versions("1.0.0", "1.0.0") == 0)
        check("0.2.0 == 0.2.0-rc1（忽略预发布后缀）",
              I.compare_versions("0.2.0", "0.2.0-rc1") == 0)
        # 这条断言是这组测试存在的理由：旧实现正是把这两个方向都判反了。
        check("字符串比较确实会把这两个方向判反",
              ("0.2.0" < "0.10.0") is False and ("0.10.0" < "0.9.0") is True)

        print("\n=== 13. 降级护栏：默认拒绝 / 显式开关才允许 ===")
        cfg13, inst13, _ = new_env(os.path.join(tmpdir, "d13"))
        code, _ = do_install(cfg13, inst13)
        check("基线安装成功", code == 0, f"(code={code})")
        pkg_ver = I.product_version()
        # 伪造成"已经装了更新的版本"：文件内容与当前包不同，但 manifest 哈希同步，
        # 所以归属仍判为 owned（走正常 upgrade，不需要 --force），是干净的降级场景。
        cli13 = os.path.join(inst13, "usage_cli.py")
        with open(cli13, "a", encoding="utf-8") as fh:
            fh.write("# NEWER RELEASE FROM THE FUTURE\n")
        mpath13 = os.path.join(inst13, I.MANIFEST_NAME)
        forged = json.load(io.open(mpath13, encoding="utf-8"))
        forged["version"] = "99.0.0"
        forged["files"]["usage_cli.py"]["sha256"] = sha(cli13)
        with open(mpath13, "w", encoding="utf-8") as fh:
            json.dump(forged, fh, ensure_ascii=False, indent=2)
        stale_cli = sha(cli13)
        stale_cfg = sha(cfg13)
        bdir13 = os.path.join(inst13, I.BACKUP_DIRNAME)
        backups_before = sorted(os.listdir(bdir13))

        code, sout = run(["install", "--config", cfg13, "--install-dir", inst13,
                          "--python", sys.executable])
        check("未加开关 → 拒绝降级（退出码 9）", code == I.EXIT_DOWNGRADE_REFUSED, f"(code={code})")
        check("拒绝时说明原因并指向显式开关", "拒绝降级" in sout and "--allow-downgrade" in sout)
        check("拒绝时受管文件字节未变", sha(cli13) == stale_cli)
        check("拒绝时配置字节未变", sha(cfg13) == stale_cfg)
        check("拒绝时未新增备份", sorted(os.listdir(bdir13)) == backups_before)
        check("拒绝时 manifest 版本未变",
              (I.load_manifest(inst13) or {}).get("version") == "99.0.0")

        code, sout = run(["install", "--config", cfg13, "--install-dir", inst13,
                          "--python", sys.executable, "--allow-downgrade"])
        check("加 --allow-downgrade → 安装成功", code == 0, f"(code={code})")
        check("降级后受管文件换成当前版本",
              sha(cli13) == sha(os.path.join(ROOT, "usage_cli.py")))
        check("降级后 manifest 记录当前版本",
              (I.load_manifest(inst13) or {}).get("version") == pkg_ver)
        # 断言"降级前的旧字节仍能从备份取回"，而不是比较备份目录的名字列表：
        # 备份目录名只到秒（ts_stamp()），同一秒内的两次安装会落进同一个目录，
        # 按名字比较会假失败。这里直接验证真正的要求 —— 旧内容可取回。
        def _has_backup_of(dirpath, digest):
            for root, _dirs, names in os.walk(dirpath):
                for fn in names:
                    if fn == "usage_cli.py" and I.sha256_file(os.path.join(root, fn)) == digest:
                        return True
            return False

        check("降级前的旧文件已备份（可取回旧字节）",
              _has_backup_of(bdir13, stale_cli), f"(bdir={sorted(os.listdir(bdir13))})")

        code, sout = run(["doctor", "--config", cfg13, "--install-dir", inst13,
                          "--python", sys.executable, "--allow-downgrade"])
        check("其它动作不接受 --allow-downgrade", code == I.EXIT_ARGS, f"(code={code})")

        # ================================================================
        print("\n=== 14. 暂存目录清理：只读文件要能清掉，清不掉必须如实报告 ===")

        def capture(fn, *a):
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                result = fn(*a)
            finally:
                sys.stdout = old
            return result, buf.getvalue()

        # A) 只读文件：旧的 ignore_errors 写法删不掉，force_rmtree 必须能删掉
        d_ro = os.path.join(tmpdir, "stage-readonly")
        os.makedirs(d_ro, exist_ok=True)
        ro_file = os.path.join(d_ro, "rehearsal.jsonc")
        with open(ro_file, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.chmod(ro_file, 0o444)
        shutil.rmtree(d_ro, ignore_errors=True)
        check("旧写法 ignore_errors 确实删不掉只读文件（这就是原缺陷）", os.path.isdir(d_ro))
        check("force_rmtree 能清掉含只读文件的目录", I.force_rmtree(d_ro))
        check("清理后目录真的不在了", not os.path.exists(d_ro))

        # B) rollback：暂存目录里有只读文件时必须真清掉，且不许谎报
        cfg_r, inst_r, _ = new_env(os.path.join(tmpdir, "stage-rollback"))
        stage_r = os.path.join(inst_r, I.STAGE_PREFIX + "20260101-000000")
        os.makedirs(stage_r, exist_ok=True)
        ro_in_stage = os.path.join(stage_r, "usage_cli.py")
        with open(ro_in_stage, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.chmod(ro_in_stage, 0o444)
        ctx_r = I.Context(cfg_r, inst_r,
                          os.path.join(inst_r, I.BACKUP_DIRNAME, "probe"), stage_r)
        _, out_r = capture(I.rollback, ctx_r)
        check("rollback 清掉了含只读文件的暂存目录", not os.path.exists(stage_r))
        check("rollback 如实报告已清理", "已清理暂存目录" in out_r)
        check("rollback 没有误报失败", "未能清理" not in out_r)

        # C) 真清不掉（文件被占用）时必须如实报告，绝不谎报成功
        cfg_c, inst_c, _ = new_env(os.path.join(tmpdir, "stage-locked"))
        stage_c = os.path.join(inst_c, I.STAGE_PREFIX + "20260101-000001")
        os.makedirs(stage_c, exist_ok=True)
        locked = os.path.join(stage_c, "held.txt")
        with open(locked, "w", encoding="utf-8") as fh:
            fh.write("x")
        ctx_c = I.Context(cfg_c, inst_c,
                          os.path.join(inst_c, I.BACKUP_DIRNAME, "probe"), stage_c)
        holder = open(locked, "rb")
        try:
            _, out_c = capture(I.rollback, ctx_c)
        finally:
            holder.close()
        check("清不掉时目录确实还在", os.path.isdir(stage_c))
        check("清不掉时必须报告失败（不谎报成功）", "未能清理" in out_c)
        check("清不掉时不得出现『已清理暂存目录』", "已清理暂存目录" not in out_c)
        check("清不掉时列入人工处理清单", "手工删除" in out_c or "需人工处理" in out_c)
        I.force_rmtree(stage_c)

        # D) 端到端回归：配置只读导致注册预演失败时，回滚后不得残留暂存目录。
        # 原缺陷路径：预演会把配置副本复制进暂存目录，只读属性一并被复制，
        # 于是 rmtree 静默失败 —— 一次失败留一个目录，且永不回收。
        cfg_e, inst_e, _ = new_env(os.path.join(tmpdir, "stage-e2e"))
        code_e, _ = do_install(cfg_e, inst_e)
        check("端到端：基线安装成功", code_e == 0, f"(code={code_e})")
        e_reg = os.path.join(inst_e, "register_command.py")
        with open(e_reg, "a", encoding="utf-8") as fh:
            fh.write("# 让本次确有事务要做\n")
        man_e = I.load_manifest(inst_e)
        man_e["files"]["register_command.py"]["sha256"] = sha(e_reg)
        with open(os.path.join(inst_e, I.MANIFEST_NAME), "w", encoding="utf-8") as fh:
            json.dump(man_e, fh, ensure_ascii=False, indent=2)
        with open(cfg_e, "w", encoding="utf-8") as fh:
            fh.write('{\n  "command": {}\n}\n')
        os.chmod(cfg_e, 0o444)
        try:
            code_e2, out_e2 = run(["install", "--config", cfg_e, "--install-dir", inst_e,
                                   "--python", sys.executable])
        finally:
            os.chmod(cfg_e, 0o666)
        check("端到端：配置只读时安装失败", code_e2 != 0, f"(code={code_e2})")
        check("端到端：失败后报告了 rollback", "rollback" in out_e2)
        leftovers = [n for n in os.listdir(inst_e) if n.startswith(I.STAGE_PREFIX)]
        check("端到端：回滚后没有残留暂存目录（原缺陷回归）", leftovers == [], f"({leftovers})")

        # ================================================================
        print("\n=== 15. 备份目录同秒碰撞：连续安装必须拿到不同目录 ===")

        # A) 分配器：固定时间戳下连多次分配互不相同，已存在的目录内容不被触碰
        cfg15, inst15, _ = new_env(os.path.join(tmpdir, "b15"))
        os.makedirs(os.path.join(inst15, I.BACKUP_DIRNAME), exist_ok=True)
        orig_ts = I.ts_stamp
        I.ts_stamp = lambda: "20990101-120000"
        try:
            d1 = I.allocate_backup_dir(inst15)
            marker1 = os.path.join(d1, "marker-from-first.txt")
            with open(marker1, "w", encoding="utf-8") as fh:
                fh.write("first")
            d2 = I.allocate_backup_dir(inst15)
            d3 = I.allocate_backup_dir(inst15)
        finally:
            I.ts_stamp = orig_ts
        check("固定时间戳下两次分配得到不同目录", d1 != d2, f"({os.path.basename(d1)} vs {os.path.basename(d2)})")
        check("三次分配三个目录", len({d1, d2, d3}) == 3)
        check("目录名带 -1/-2 序号后缀",
              os.path.basename(d2).endswith("-1") and os.path.basename(d3).endswith("-2"),
              f"({os.path.basename(d2)}, {os.path.basename(d3)})")
        check("先建目录的内容不被后续分配触碰",
              os.path.isfile(marker1)
              and io.open(marker1, encoding="utf-8").read() == "first")

        # B) 端到端：同一固定时间戳下两次真实安装，先前备份逐字节保留
        cfg15b, inst15b, _ = new_env(os.path.join(tmpdir, "b15b"))
        code, _ = do_install(cfg15b, inst15b)
        check("基线安装成功", code == 0, f"(code={code})")
        cli15 = os.path.join(inst15b, "usage_cli.py")
        broot15 = os.path.join(inst15b, I.BACKUP_DIRNAME)

        def _bump_file_and_manifest(path, text):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text)
            man = I.load_manifest(inst15b)
            man["files"]["usage_cli.py"]["sha256"] = sha(path)
            with open(os.path.join(inst15b, I.MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump(man, fh, ensure_ascii=False, indent=2)
            return sha(path)

        I.ts_stamp = lambda: "20990202-120000"
        try:
            h_first = _bump_file_and_manifest(cli15, "# first upgrade content\n")
            code, _ = do_install(cfg15b, inst15b)
            check("同秒前缀下第一次升级成功", code == 0, f"(code={code})")
            dirs_after_1 = sorted(os.listdir(broot15))
            h_second = _bump_file_and_manifest(cli15, "# second upgrade content\n")
            code, _ = do_install(cfg15b, inst15b)
            check("同秒前缀下第二次升级成功", code == 0, f"(code={code})")
            dirs_after_2 = sorted(os.listdir(broot15))
        finally:
            I.ts_stamp = orig_ts
        check("第二次安装使用了新目录而不是复用第一次的",
              "20990202-120000" in dirs_after_1
              and "20990202-120000-1" in dirs_after_2
              and "20990202-120000-1" not in dirs_after_1,
              f"({dirs_after_1} -> {dirs_after_2})")
        bak1 = os.path.join(broot15, "20990202-120000", "usage_cli.py")
        bak2 = os.path.join(broot15, "20990202-120000-1", "usage_cli.py")
        check("第一次的备份内容未被第二次安装覆盖", sha(bak1) == h_first)
        check("第二次的备份记录了升级前的字节", sha(bak2) == h_second)

        # ================================================================
        print("\n=== 16. 纯版本号发布：manifest 版本对齐，文件与配置不动 ===")
        cfg16, inst16, _ = new_env(os.path.join(tmpdir, "m16"))
        code, _ = do_install(cfg16, inst16)
        check("基线安装成功", code == 0, f"(code={code})")
        mpath16 = os.path.join(inst16, I.MANIFEST_NAME)
        cli16 = os.path.join(inst16, "usage_cli.py")
        h16 = sha(cli16)
        cfg_hash16 = sha(cfg16)
        pkg_ver = I.product_version()

        # 伪造"版本号落后、文件哈希仍正确"：模拟纯版本号发布后的差异
        forged = json.load(io.open(mpath16, encoding="utf-8"))
        forged["version"] = "0.0.1"
        with open(mpath16, "w", encoding="utf-8") as fh:
            json.dump(forged, fh, ensure_ascii=False, indent=2)
        bdir16_before = sorted(os.listdir(os.path.join(inst16, I.BACKUP_DIRNAME)))

        code, sout = do_install(cfg16, inst16)
        check("纯版本号差异：安装退出码 0", code == 0, f"(code={code})")
        check("走的是无需改动路径", "无需改动" in sout)
        m16 = I.load_manifest(inst16)
        check("manifest 版本对齐为当前包版本",
              m16 is not None and m16.get("version") == pkg_ver,
              f"({(m16 or {}).get('version')} vs {pkg_ver})")
        check("程序文件字节未变", sha(cli16) == h16)
        check("配置字节未变", sha(cfg16) == cfg_hash16)
        check("未新增备份目录",
              sorted(os.listdir(os.path.join(inst16, I.BACKUP_DIRNAME))) == bdir16_before)
        check("manifest 文件记录仍与磁盘一致",
              m16["files"]["usage_cli.py"]["sha256"] == h16)
        check("输出说明了版本对齐", "版本已对齐" in sout)

        # 版本已一致时重复安装：不得重写 manifest（installedAt 不变）
        inst_at = m16.get("installedAt")
        code, sout2 = do_install(cfg16, inst16)
        check("版本已一致时再次安装退出码 0", code == 0, f"(code={code})")
        m16b = I.load_manifest(inst16)
        check("版本一致时不重写 manifest（installedAt 未变）",
              m16b is not None and m16b.get("installedAt") == inst_at)
        check("版本一致时输出不含版本对齐", "版本已对齐" not in sout2)

        # manifest 被删除但文件仍 owned：重建（恢复归属证明，不重装文件）
        os.unlink(mpath16)
        code, sout3 = do_install(cfg16, inst16)
        check("manifest 缺失时安装退出码 0", code == 0, f"(code={code})")
        m16c = I.load_manifest(inst16)
        check("manifest 缺失时被重建且版本正确",
              m16c is not None and m16c.get("version") == pkg_ver)
        check("重建时文件哈希正确且未重装文件",
              m16c["files"]["usage_cli.py"]["sha256"] == h16 and sha(cli16) == h16)

        # 降级方向：manifest 版本更高时仍受降级护栏保护，--allow-downgrade 后对齐
        forged = json.load(io.open(mpath16, encoding="utf-8"))
        forged["version"] = "99.0.0"
        with open(mpath16, "w", encoding="utf-8") as fh:
            json.dump(forged, fh, ensure_ascii=False, indent=2)
        code, sout4 = run(["install", "--config", cfg16, "--install-dir", inst16,
                           "--python", sys.executable])
        check("高版本 manifest 默认拒绝降级（退出码 9）",
              code == I.EXIT_DOWNGRADE_REFUSED, f"(code={code})")
        code, sout5 = run(["install", "--config", cfg16, "--install-dir", inst16,
                           "--python", sys.executable, "--allow-downgrade"])
        check("--allow-downgrade 后退出码 0", code == 0, f"(code={code})")
        m16d = I.load_manifest(inst16)
        check("降级对齐后 manifest 版本为当前包版本",
              m16d is not None and m16d.get("version") == pkg_ver)

    finally:
        if old_data is None:
            os.environ.pop("MIMO_USAGE_DATA_DIR", None)
        else:
            os.environ["MIMO_USAGE_DATA_DIR"] = old_data
        if old_db is None:
            os.environ.pop("MIMO_USAGE_DB", None)
        else:
            os.environ["MIMO_USAGE_DB"] = old_db
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n{'INSTALLER TESTS OK' if FAILS == 0 else 'INSTALLER TESTS FAILED: ' + str(FAILS)}")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
