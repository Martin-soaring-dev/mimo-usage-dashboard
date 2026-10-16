#!/usr/bin/env python3
"""Regression coverage for Issue #5; no external dependencies."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usage_cli as U


class ContextLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / ".config" / "mimocode").mkdir(parents=True)
        self.project = self.root / "project"
        self.project.mkdir()
        self.patcher = patch.object(U.os.path, "expanduser", side_effect=lambda p: str(self.home) if p == "~" else os.path.expanduser(p))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def config(self, data, path=None):
        target = path or self.home / ".config" / "mimocode" / "mimocode.jsonc"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return target

    def test_provider_identity_and_jsonc(self):
        self.config('''{
          // same model ID may have different provider limits
          "provider": {
            "opencode-go": {"models": {"deepseek-flash": {"limit": {"context": 1000000, "output": 393216}}}},
            "deepseek": {"models": {"deepseek-flash": {"limit": {"context": 131072}}}}
          },
        }''')
        self.assertEqual(U.model_context_limit("deepseek-flash", "opencode-go")[0], 1000000)
        self.assertEqual(U.model_context_limit("deepseek-flash", "deepseek")[0], 131072)
        self.assertEqual(U.model_context_limit("deepseek-flash", "unknown"), (None, "unknown"))
        self.assertAlmostEqual(40200 / U.model_context_limit("deepseek-flash", "opencode-go")[0], .0402)

    def test_precedence_and_errors(self):
        self.config({"provider": {"opencode-go": {"models": {"deepseek-flash": {"limit": {"context": 131072}}}}}})
        project_file = self.project / "mimocode.jsonc"
        self.config({"provider": {"opencode-go": {"models": {"deepseek-flash": {"limit": {"context": 1000000}}}}}}, project_file)
        self.assertEqual(U.model_context_limit("deepseek-flash", "opencode-go", str(self.project)), (1000000, "config"))
        project_file.write_text("{ invalid ", encoding="utf-8")
        self.assertEqual(U.model_context_limit("deepseek-flash", "opencode-go", str(self.project)), (None, "config-error"))
        project_file.unlink()
        self.assertEqual(U.model_context_limit("deepseek-flash", "opencode-go", str(self.project))[0], 131072)
        self.config({"provider": {"opencode-go": {"models": {"deepseek-flash": {"limit": {"context": -3}}}}}})
        self.assertEqual(U.model_context_limit("deepseek-flash", "opencode-go")[0], None)

    def test_builtin_fallback_is_independent_of_unrelated_config(self):
        """内置兜底不该受「机器上存在一个未声明该模型的配置文件」影响。

        回归用例：早前的实现把内置兜底门控在 ``not found_config`` 上，于是同一份代码
        在**有**配置的机器上（每个真实用户都有）返回 unknown、在**无**配置的机器上
        （CI runner）返回内置值 —— CI 恒绿，本地却失败。
        """
        expected = (1048576, "builtin")
        # 存在配置、但未声明该模型 → 仍应回落到内置表
        self.config({"provider": {"someone-else": {"models": {"other": {"limit": {"context": 999}}}}}})
        self.assertEqual(U.model_context_limit("mimo-v2.6-flash", "xiaomi-token-plan-cn"), expected)
        # 删掉配置后，结果必须**完全相同** —— 证明它与机器上有没有配置文件无关
        (self.home / ".config" / "mimocode" / "mimocode.jsonc").unlink()
        self.assertEqual(U.model_context_limit("mimo-v2.6-flash", "xiaomi-token-plan-cn"), expected)

    def test_broken_home_config_degrades_to_config_error(self):
        """配置读取失败必须降级为 ``config-error``，而不是沿用内置表（unknown 优于 wrong）。"""
        self.config("{ 这不是合法 JSONC ")
        self.assertEqual(U.model_context_limit("mimo-v2.6-flash", "xiaomi-token-plan-cn"),
                         (None, "config-error"))
        # 损坏的配置不得把第三方 provider 也变成「内置已知」
        self.assertEqual(U.model_context_limit("deepseek-flash", "opencode-go"), (None, "config-error"))

    def test_session_model_switch_and_explicit_limit(self):
        self.config({"provider": {
            "old-provider": {"models": {"old-model": {"limit": {"context": 1000}}}},
            "opencode-go": {"models": {"deepseek-flash": {"limit": {"context": 1000000}}}}
        }})
        db = self.root / "fixture.db"
        con = sqlite3.connect(db)
        con.executescript("""
          CREATE TABLE session (id TEXT, title TEXT, directory TEXT, time_created INTEGER,
            time_updated INTEGER, summary_additions INTEGER, summary_deletions INTEGER,
            summary_files INTEGER, context_watermark TEXT, time_compacting INTEGER,
            last_checkpoint_message_id TEXT);
          CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
          CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """)
        con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("s", "switch", str(self.project), 1, 3, 0, 0, 0, None, None, None))
        for mid, ts, pid, model, total in [
            ("m1", 1, "old-provider", "old-model", 600),
            ("m2", 2, "opencode-go", "deepseek-flash", 40200),
        ]:
            payload = {"role": "assistant", "providerID": pid, "modelID": model,
                       "tokens": {"total": total}}
            con.execute("INSERT INTO message VALUES (?,?,?,?)", (mid, "s", ts, json.dumps(payload)))
        con.execute("INSERT INTO part VALUES (?,?,?,?,?)",
                    ("p2", "m2", "s", 3, json.dumps({"type": "step-finish", "tokens": {"total": 40200}})))
        con.commit()
        con.close()
        report = U.session_context(str(db), session_id="s")
        self.assertEqual(report["model"], {"providerID": "opencode-go", "modelID": "deepseek-flash"})
        self.assertEqual(report["context"]["limit"], 1000000)
        self.assertAlmostEqual(report["context"]["ratio"], .0402)
        overridden = U.session_context(str(db), session_id="s", limit=50000)
        self.assertEqual((overridden["context"]["limit"], overridden["context"]["limitSource"]),
                         (50000, "explicit"))


if __name__ == "__main__":
    unittest.main()
