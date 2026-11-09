#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view_flow_log.py 的离线测试（临时目录，不碰真实 data/）。"""
import contextlib
import datetime
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import view_flow_log as v


class TestViewFlowLog(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        p = patch.object(v, "LOG_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.today = datetime.date(2026, 11, 9)

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_parse_day_strict(self):
        self.assertEqual(v.parse_day("2026-11-09").isoformat(), "2026-11-09")
        for bad in ("2026/11/09", "2026-1-9", "2026-13-01", "abc",
                    "2026-11-09x", "2026-1x-09"):
            with self.assertRaises(ValueError):
                v.parse_day(bad)

    def test_log_path_for_today_and_archive(self):
        path, archived = v.log_path_for(self.today, self.today)
        self.assertFalse(archived)
        self.assertEqual(os.path.basename(path), "bot_flow.log")
        path, archived = v.log_path_for(datetime.date(2026, 11, 8), self.today)
        self.assertTrue(archived)
        self.assertEqual(os.path.basename(path), "bot_flow-2026-11-08.log")

    def test_archived_days_ignores_foreign_files(self):
        self._write("bot_flow-2026-11-08.log", "x")
        self._write("bot_flow-2026-11-07.log", "x")
        for name in ("bot_flow.log", "groups.txt", "bot_flow-bad.log",
                     "bot_flow-2026-13-01.log", "other-2026-11-06.log"):
            self._write(name, "x")
        self.assertEqual(v.archived_days(), ["2026-11-07", "2026-11-08"])
        self.assertEqual(v.available_days(self.today),
                         ["2026-11-07", "2026-11-08", "2026-11-09"])

    def test_read_lines_tail(self):
        self._write("bot_flow.log", "一\n二\n三\n")
        path = os.path.join(self.dir, "bot_flow.log")
        self.assertEqual(v.read_lines(path), ["一", "二", "三"])
        self.assertEqual(v.read_lines(path, 2), ["二", "三"])

    def test_main_prints_today_and_tail(self):
        self._write("bot_flow.log", "上午\n下午\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = v.main([], today=self.today)
        self.assertEqual(code, 0)
        self.assertIn("上午", out.getvalue())
        self.assertIn("下午", out.getvalue())
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            code = v.main(["--tail", "1"], today=self.today)
        self.assertEqual(code, 0)
        self.assertNotIn("上午", out2.getvalue())
        self.assertIn("下午", out2.getvalue())

    def test_main_prints_specified_archive(self):
        self._write("bot_flow-2026-11-08.log", "昨天的记录\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = v.main(["2026-11-08"], today=self.today)
        self.assertEqual(code, 0)
        self.assertIn("昨天的记录", out.getvalue())

    def test_main_missing_day_lists_available(self):
        self._write("bot_flow-2026-11-08.log", "x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = v.main(["2026-11-01"], today=self.today)
        self.assertEqual(code, 1)
        self.assertIn("没有记录", out.getvalue())
        self.assertIn("2026-11-08", out.getvalue())

    def test_main_bad_day(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = v.main(["2026/11/08"], today=self.today)
        self.assertEqual(code, 2)
        self.assertIn("日期错误", err.getvalue())

    def test_main_list(self):
        self._write("bot_flow-2026-11-08.log", "x")
        self._write("bot_flow.log", "x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = v.main(["--list"], today=self.today)
        self.assertEqual(code, 0)
        self.assertIn("2026-11-08", out.getvalue())
        self.assertIn("当天", out.getvalue())


if __name__ == "__main__":
    unittest.main()
