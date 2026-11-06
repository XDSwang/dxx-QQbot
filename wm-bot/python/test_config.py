#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/config.txt 配置读取与模板安全回归（离线、零联网）。

验证：注释 / 空值 / 引号 / BOM 解析、开关 0 与 1、apply 不覆盖已有环境变量、
生效配置固定在脚本同目录的 data/ 下、**会上传的模板不含任何真实凭证**、
data/ 已被 .gitignore 排除、业务参数确实被 wm.py 采用、启动脚本已彻底移除。

运行：python test_config.py
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["WM_SOURCE"] = "mock"   # 必须在 import wm 之前：离线测试不发 HTTP

import config
import wm
import qq_common


class TestParse(unittest.TestCase):
    def test_comments_empty_and_quotes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.txt"
            p.write_text(
                "# 全行注释\n"
                "AAA = 1                       # 行内注释\n"
                "bbb = 'hello world'           # 引号包住的值\n"
                "EMPTY =                       # 留空 -> 跳过（用程序默认值）\n"
                "URL = https://a.b/c?x=1&y=2  # 值里可以再带 =\n"
                "NO_EQUAL_SIGN\n",
                encoding="utf-8")
            got = config.read(p)
            self.assertEqual(got["AAA"], "1")
            self.assertEqual(got["BBB"], "hello world")
            self.assertEqual(got["URL"], "https://a.b/c?x=1&y=2")
            self.assertNotIn("EMPTY", got)
            self.assertEqual(len(got), 3)

    def test_bom_utf8(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.txt"
            p.write_text("\ufeff# Windows 记事本 BOM\nK = V\n", encoding="utf-8")
            self.assertEqual(config.read(p)["K"], "V")

    def test_missing_file_returns_empty(self):
        p = Path(tempfile.gettempdir()) / "wm_bot_no_such_config_9527.txt"
        self.assertEqual(config.read(p), {})


class TestApplyAndFlag(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("WM_TEST_KEY", "WM_QQ_ALLOW_ALL_GROUPS")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_apply_fills_but_never_overrides_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.txt"
            p.write_text("WM_TEST_KEY = from_file\n", encoding="utf-8")
            os.environ["WM_TEST_KEY"] = "from_env"
            config.apply(p)
            self.assertEqual(os.environ["WM_TEST_KEY"], "from_env")
            os.environ.pop("WM_TEST_KEY", None)
            config.apply(p)
            self.assertEqual(os.environ["WM_TEST_KEY"], "from_file")

    def test_flag_zero_is_off_one_is_on(self):
        for v, want in [("0", False), ("", False), ("false", False), ("no", False),
                       ("1", True), ("true", True), ("yes", True)]:
            if v == "":
                os.environ.pop("WM_QQ_ALLOW_ALL_GROUPS", None)
            else:
                os.environ["WM_QQ_ALLOW_ALL_GROUPS"] = v
            self.assertEqual(config.flag("WM_QQ_ALLOW_ALL_GROUPS"), want, v)


class TestWiring(unittest.TestCase):
    def test_config_file_next_to_script(self):
        # 生效文件在 data/ 下（含密钥、不上传）；同目录 config.txt 只是模板
        self.assertEqual(config.CONFIG_FILE, HERE / "data" / "config.txt")
        self.assertTrue(config.CONFIG_FILE.exists())
        self.assertEqual(config.TEMPLATE_FILE, HERE / "config.txt")

    def test_template_has_no_secrets(self):
        """会上传 GitHub 的模板里，凭证两项必须留空，防止手滑把密钥提交上去。"""
        tpl = HERE / "config.txt"
        self.assertTrue(tpl.exists())
        text = tpl.read_text(encoding="utf-8")
        for key in ("WM_QQ_APPID", "WM_QQ_SECRET"):
            m = re.search(r"^%s[ \t]*=[ \t]*(.*)$" % key, text, re.M)
            self.assertTrue(m, key)
            self.assertEqual(m.group(1).strip(), "", key + " 在模板里必须留空")
        self.assertNotIn("WM_QQ_SECRET", config.read(tpl))

    def test_data_dir_is_gitignored(self):
        """data/ 必须被排除，否则 data/config.txt 里的密钥会随仓库上传。"""
        gi = HERE.parent / ".gitignore"
        self.assertTrue(gi.exists())
        lines = [l.strip() for l in gi.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        self.assertIn("data/", lines)

    def test_business_values_take_effect(self):
        cfg = config.read()
        self.assertEqual(cfg["WM_QQ_BACKEND"], "official")
        self.assertEqual(os.environ.get("WM_QQ_BACKEND"), "official")
        self.assertEqual(cfg["WM_QQ_ALLOW_ALL_GROUPS"], "0")
        self.assertFalse(config.flag("WM_QQ_ALLOW_ALL_GROUPS"))
        self.assertIn("WM_CACHE_MINUTES", cfg)
        self.assertEqual(wm.CACHE_MINUTES,
                         int(os.environ.get("WM_CACHE_MINUTES", cfg["WM_CACHE_MINUTES"])))
        self.assertEqual(wm.STALE_MS, wm.CACHE_MINUTES * 60 * 1000)

    def test_no_launch_scripts_left(self):
        for name in ("启动.bat", "获取openid.bat", "start.sh"):
            self.assertFalse((HERE / name).exists(), name)

    def test_relative_path_resolved_from_script_dir(self):
        self.assertEqual(qq_common.resolve_path("data/groups.txt"),
                         HERE / "data" / "groups.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
