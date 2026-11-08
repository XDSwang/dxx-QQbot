#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_updater.py — 自动更新服务离线测试：不连 GitHub、不启动真实机器人、不发 HTTP。

覆盖：runN 信号与指纹幂等、配置结构同步（不冲掉密钥）、运行数据改名/删字段/补默认、
      坏文件不覆盖、备份与失败回滚、云端回写、GitHub 私有仓库列目录一次拿全量。
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run
import updater


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote"
        self.data = self.root / "data"
        self.state = self.root / "state"
        for p in (self.remote, self.data, self.state):
            p.mkdir()
        p1 = mock.patch.multiple(
            updater, STATE_DIR=self.state,
            ERROR_LOG=self.state / "updater_error.log",
            FLOW_LOG=self.state / "updater_flow.log",
            PACE_FILE=self.state / "pace.json",
            STATE_FILE=self.state / "state.json",
            BACKUP_ROOT=self.state / "backup",
            LOCK_FILE=self.state / "updater.lock")
        p2 = mock.patch.multiple(
            run, BOT_DATA_DIR=self.data, BOT_CONFIG=self.data / "config.txt",
            CFG_FILE=self.root / "update.cfg", HERE=self.root)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(self.tmp.cleanup)

    def store(self):
        return updater.LocalStore(self.remote)


class TestSignal(_Base):
    def test_run0_is_idle(self):
        (self.remote / "run0").write_text("0", encoding="utf-8")
        sig = self.store().signal()
        self.assertFalse(sig.need_update)
        self.assertEqual(sig.name, "run0")

    def test_run1_and_fingerprint_idempotent(self):
        (self.remote / "run1").write_text("1", encoding="utf-8")
        a = self.store().signal()
        self.assertTrue(a.need_update)
        b = self.store().signal()
        self.assertEqual(a.fingerprint, b.fingerprint, "同一个 run1 不能重复触发")

    def test_new_version_changes_fingerprint(self):
        (self.remote / "run1").write_text("1", encoding="utf-8")
        a = self.store().signal()
        (self.remote / "run1").unlink()
        (self.remote / "run2").write_text("2", encoding="utf-8")
        b = self.store().signal()
        self.assertNotEqual(a.fingerprint, b.fingerprint)
        self.assertTrue(b.need_update)

    def test_no_signal_file_is_idle(self):
        self.assertFalse(self.store().signal().need_update)

    def test_pick_signal_prefers_nonzero(self):
        pairs = [("run0", "f0"), ("run3", "f3")]
        self.assertEqual(updater.pick_signal(pairs).name, "run3")


class TestMergeConfig(_Base):
    LOCAL = ("# 本地注释\nWM_QQ_APPID = REAL_APPID\n"
             "WM_QQ_SECRET = REAL_SECRET\nOLD_FIELD = local-only\n")
    CLOUD = ("# 云端模板注释\nWM_QQ_APPID = \nWM_QQ_SECRET = \n"
             "NEW_FLAG = 1\n")

    def test_structure_keeps_secret_and_syncs_fields(self):
        text, rep = updater.merge_config(self.LOCAL, self.CLOUD)
        self.assertIn("WM_QQ_SECRET = REAL_SECRET", text, "密钥绝不能被云端空值冲掉")
        self.assertIn("NEW_FLAG = 1", text)
        self.assertNotIn("OLD_FIELD", text, "云端删掉的字段不再写入")
        self.assertIn("# 云端模板注释", text)
        self.assertIn("NEW_FLAG", rep["added"])
        self.assertIn("OLD_FIELD", rep["removed"])
        self.assertIn("WM_QQ_SECRET", rep["kept"])

    def test_overwrite_is_literal_copy(self):
        text, _ = updater.merge_config(self.LOCAL, self.CLOUD, "overwrite")
        self.assertEqual(text, self.CLOUD)

    def test_no_cloud_config_is_noop(self):
        text, rep = updater.merge_config(self.LOCAL, None)
        self.assertEqual(text, self.LOCAL)
        self.assertTrue(rep["skipped"])

    def test_load_cfg_merges_defaults(self):
        (self.root / "update.cfg").write_text(
            "# c\nUPDATER_BACKEND = local\nUPDATER_POLL_SECONDS = 7\n", encoding="utf-8")
        cfg = run.load_cfg()
        self.assertEqual(cfg["UPDATER_BACKEND"], "local")
        self.assertEqual(cfg["UPDATER_POLL_SECONDS"], "7")
        self.assertEqual(cfg["UPDATER_CONFIG_MERGE"], "structure")


class TestMigrate(_Base):
    def test_rename_nested_drop_and_keep(self):
        local = {"ws_subs.json": {
            "version": 1,
            "sessions": {"g|1": {"users": {"u1": ["alerts"]}, "obsolete": 1}},
            "legacy_top": 9}}
        schema = {"version": 2, "rename": {"users": "members"},
                  "drop": ["obsolete"],
                  "files": {"ws_subs.json": {
                      "keep": ["version", "sessions"],
                      "defaults": {"version": 1}}}}
        out, rep = updater.migrate_runtime(local, schema, {})
        d = out["ws_subs.json"]
        self.assertIn("members", d["sessions"]["g|1"])
        self.assertNotIn("users", d["sessions"]["g|1"])
        self.assertNotIn("obsolete", d["sessions"]["g|1"])
        self.assertNotIn("legacy_top", d)
        self.assertTrue(any("users→members" in x for x in rep["renamed"]))
        self.assertTrue(any("legacy_top" in x for x in rep["dropped"]))

    def test_cloud_runtime_defines_latest_shape(self):
        """没写 keep 时，按私有地址 runtime.json 的字段当最新结构。"""
        local = {"ws_subs.json": {"version": 1, "sessions": {}, "old_field": 123}}
        cloud = {"ws_subs.json": {"version": 1, "sessions": {}}}
        out, _ = updater.migrate_runtime(local, {}, cloud)
        self.assertEqual(set(out["ws_subs.json"]), {"version", "sessions"})

    def test_defaults_added_only_when_declared(self):
        schema = {"files": {"ws_subs.json": {"defaults": {"version": 1, "new_key": None}}}}
        out, rep = updater.migrate_runtime({"ws_subs.json": {}}, schema, {})
        self.assertIsNone(out["ws_subs.json"]["new_key"])
        self.assertTrue(any("new_key" in x for x in rep["added"]))

    def test_nothing_on_either_side_is_not_written(self):
        out, _ = updater.migrate_runtime(
            {}, {"files": {"ghost.json": {"defaults": {"a": 1}}}}, {})
        self.assertNotIn("ghost.json", out, "「没有就不写入」")

    def test_cloud_only_file_is_preserved(self):
        cloud = {"ws_filters.json": {"version": 1, "sessions": {"g|1": ["古纪"]}}}
        out, _ = updater.migrate_runtime({}, {}, cloud)
        self.assertEqual(out["ws_filters.json"], cloud["ws_filters.json"])

    def test_broken_local_file_is_not_overwritten(self):
        (self.data / "ws_subs.json").write_text("{bad json", encoding="utf-8")
        local, bad = updater.collect_runtime(self.data, ["ws_subs.json"])
        self.assertEqual(bad, ["ws_subs.json"])
        self.assertNotIn("ws_subs.json", local)


class TestWriteAndBundle(_Base):
    def test_atomic_write_and_backup(self):
        (self.data / "ws_subs.json").write_text('{"old": 1}', encoding="utf-8")
        ok, detail = updater.write_runtime_files(
            {"ws_subs.json": {"new": 2}}, self.data, backup=True)
        self.assertTrue(ok)
        self.assertEqual(json.loads((self.data / "ws_subs.json").read_text(
            encoding="utf-8")), {"new": 2})
        self.assertTrue((Path(detail) / "ws_subs.json").exists())

    def test_fetch_bundle_and_push_runtime(self):
        (self.remote / "config.txt").write_text("A = 1", encoding="utf-8")
        (self.remote / "schema.json").write_text('{"version": 5}', encoding="utf-8")
        (self.remote / "runtime.json").write_text('{"ws_subs.json": {}}', encoding="utf-8")
        b = updater.fetch_bundle(self.store())
        self.assertEqual(b["config"], "A = 1")
        self.assertEqual(b["schema"]["version"], 5)
        updater.push_runtime(self.store(), {"ws_subs.json": {"x": 1}})
        self.assertEqual(json.loads((self.remote / "runtime.json").read_text(
            encoding="utf-8")), {"ws_subs.json": {"x": 1}})


class FakeBot(object):
    entry = "python/qq_bot.py"

    def __init__(self):
        self.started = 0
        self.stopped = 0

    def alive(self):
        return True

    def stop(self):
        self.stopped += 1
        return True

    def start(self):
        self.started += 1


class TestDoUpdate(_Base):
    def _prepare(self):
        (self.remote / "run1").write_text("1", encoding="utf-8")
        (self.remote / "config.txt").write_text(
            "# cloud\nWM_QQ_APPID = \nWM_QQ_SECRET = \nNEW_FLAG = 1\n", encoding="utf-8")
        (self.remote / "schema.json").write_text(json.dumps(
            {"version": 2, "rename": {"users": "members"}}), encoding="utf-8")
        (self.remote / "runtime.json").write_text(json.dumps(
            {"ws_subs.json": {"version": 1, "sessions": {}}}), encoding="utf-8")
        (self.data / "config.txt").write_text(
            "WM_QQ_APPID = REAL\nWM_QQ_SECRET = SECRET\nOLD_FIELD = x\n", encoding="utf-8")
        (self.data / "ws_subs.json").write_text(json.dumps({
            "version": 1,
            "sessions": {"g|1": {"users": ["alerts"]}},
            "junk": 1}), encoding="utf-8")
        return self.store(), self.store().signal()

    def test_full_update_keeps_secret_migrates_and_pushes(self):
        store, sig = self._prepare()
        bot = FakeBot()
        with mock.patch.object(run, "git_pull", return_value=(True, "mock")):
            ok = run.do_update({"UPDATER_CONFIG_MERGE": "structure",
                                "UPDATER_PUSH_RUNTIME": "1",
                                "UPDATER_GIT_PULL": "1"}, store, bot, sig)
        self.assertTrue(ok)
        cfg = (self.data / "config.txt").read_text(encoding="utf-8")
        self.assertIn("WM_QQ_SECRET = SECRET", cfg)
        self.assertIn("NEW_FLAG = 1", cfg)
        self.assertNotIn("OLD_FIELD", cfg)
        sub = json.loads((self.data / "ws_subs.json").read_text(encoding="utf-8"))
        self.assertIn("members", sub["sessions"]["g|1"])
        self.assertNotIn("junk", sub, "云端最新结构里没有的字段不再写入")
        pushed = json.loads((self.remote / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(pushed["ws_subs.json"], sub, "私有地址拿到迁移后的最新完整数据")
        self.assertEqual(updater.load_state()["last_signal"], sig.fingerprint)
        self.assertEqual(bot.stopped, 1)
        self.assertEqual(bot.started, 1)
        self.assertEqual(updater.load_state()["failed_signal"], "",
                         "成功一轮要把上一轮的失败标记清掉")

    def test_failure_restores_backup_and_still_starts_bot(self):
        store, sig = self._prepare()
        before = (self.data / "config.txt").read_text(encoding="utf-8")
        bot = FakeBot()
        with mock.patch.object(run, "git_pull", return_value=(True, "mock")), \
             mock.patch.object(updater, "write_runtime_files",
                               side_effect=RuntimeError("disk full")):
            ok = run.do_update({"UPDATER_CONFIG_MERGE": "structure",
                                "UPDATER_PUSH_RUNTIME": "1"}, store, bot, sig)
        self.assertFalse(ok)
        self.assertEqual((self.data / "config.txt").read_text(encoding="utf-8"), before)
        self.assertEqual(bot.started, 1, "更新失败也不能让机器人停摆")
        self.assertEqual(updater.load_state()["failed_signal"], sig.fingerprint,
                         "失败信号必须落盘，否则每 10 秒会重跑同一次失败更新")

    def test_git_pull_failure_aborts_without_touching_data(self):
        store, sig = self._prepare()
        before = (self.data / "ws_subs.json").read_text(encoding="utf-8")
        bot = FakeBot()
        with mock.patch.object(run, "git_pull", return_value=(False, "conflict")):
            ok = run.do_update({"UPDATER_GIT_PULL": "1"}, store, bot, sig)
        self.assertFalse(ok)
        self.assertEqual((self.data / "ws_subs.json").read_text(encoding="utf-8"), before)
        self.assertEqual(bot.started, 1)
        self.assertEqual(updater.load_state()["failed_signal"], sig.fingerprint)

    def test_bad_local_json_is_left_alone(self):
        store, sig = self._prepare()
        (self.data / "ws_subs.json").write_text("{broken", encoding="utf-8")
        bot = FakeBot()
        with mock.patch.object(run, "git_pull", return_value=(True, "mock")):
            ok = run.do_update({"UPDATER_PUSH_RUNTIME": "0"}, store, bot, sig)
        self.assertTrue(ok)
        self.assertEqual((self.data / "ws_subs.json").read_text(encoding="utf-8"),
                         "{broken", "坏文件绝不能被空数据覆盖")


class TestSelfRestart(_Base):
    """更新器改到自己时：指纹能发现、语法坏了不重启、正常时先停机器人再换进程。"""

    def _files(self):
        a = self.root / "run.py"
        b = self.root / "updater.py"
        a.write_text("x = 1\n", encoding="utf-8")
        b.write_text("y = 2\n", encoding="utf-8")
        p = mock.patch.object(run, "SELF_FILES", (a, b))
        p.start()
        self.addCleanup(p.stop)
        return a, b

    def test_fingerprint_tracks_code_change(self):
        a, _ = self._files()
        before = run.self_fingerprint()
        a.write_text("x = 1\nz = 3\n", encoding="utf-8")
        self.assertNotEqual(run.self_fingerprint(), before)

    def test_broken_code_blocks_restart(self):
        a, _ = self._files()
        a.write_text("def (\n", encoding="utf-8")
        self.assertFalse(run.self_syntax_ok())
        with mock.patch.object(run.os, "execv") as ex, mock.patch("builtins.print"):
            self.assertFalse(run.restart_self(FakeBot()))
        ex.assert_not_called()

    def test_good_code_stops_bot_then_execv(self):
        self._files()
        bot = FakeBot()
        with mock.patch.object(run.os, "execv") as ex, \
             mock.patch.object(run, "release_lock") as rel, \
             mock.patch("builtins.print"):
            self.assertTrue(run.restart_self(bot))
        ex.assert_called_once()
        rel.assert_called_once()
        self.assertEqual(bot.stopped, 1)


class TestPreflight(_Base):
    """服务端没人告诉它私有仓库地址时，启动就要把缺什么说清楚。"""

    def test_placeholder_credentials_and_missing_files_are_reported(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "github",
                    "UPDATER_GITHUB_REPO": "你的账号/私有仓库名",
                    "UPDATER_GITHUB_TOKEN": "github_pat_xxxxxxxx",
                    "UPDATER_GITHUB_PATH": "bot-sync",
                    "UPDATER_GIT_PULL": "0"})
        with mock.patch("builtins.print"):
            problems = run.preflight(cfg, self.store())
        text = " ".join(problems)
        self.assertIn("占位", text)
        self.assertIn("bot-sync", text)
        self.assertIn("runN", text)
        self.assertIn("config.txt", text)
        self.assertIn("schema.json", text)

    def test_not_a_git_repo_is_reported(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote),
                    "UPDATER_GIT_PULL": "1"})
        (self.remote / "run0").write_text("0", encoding="utf-8")
        (self.remote / "config.txt").write_text("A = 1\n", encoding="utf-8")
        (self.remote / "schema.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(run.subprocess, "run",
                               return_value=mock.Mock(returncode=1)), \
             mock.patch("builtins.print"):
            problems = run.preflight(cfg, self.store())
        self.assertIn("git", " ".join(problems))


class TestMainLoop(_Base):
    def test_main_starts_bot_and_exits_cleanly(self):
        """run.py 唯一入口：能装配后端、拉起机器人、Ctrl+C 时收干净。"""
        (self.remote / "run0").write_text("0", encoding="utf-8")
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote),
                    "UPDATER_GIT_PULL": "0", "UPDATER_PUSH_RUNTIME": "0"})
        bot = FakeBot()
        with mock.patch.object(run, "load_cfg", return_value=cfg), \
             mock.patch.object(run, "BotProcess", return_value=bot), \
             mock.patch.object(run.time, "sleep",
                               side_effect=[None, KeyboardInterrupt]):
            rc = run.main()
        self.assertEqual(rc, 0)
        self.assertEqual(bot.started, 1)
        self.assertEqual(bot.stopped, 1)


class TestGitHubStore(unittest.TestCase):
    """用假 _api 验证私有仓库后端：不联网、不发真实请求。"""

    def test_listing_signal_read_and_write(self):
        items = {
            "run1": {"name": "run1", "sha": "S1", "content": _b64("1")},
            "config.txt": {"name": "config.txt", "sha": "S2", "content": _b64("A = 1")},
            "schema.json": {"name": "schema.json", "sha": "S3",
                            "content": _b64('{"version": 1}')},
        }
        calls = []

        def fake(method, url, body=None):
            calls.append((method, url, body))
            if method == "GET" and "contents/bot-sync?" in url:
                return [dict(v) for v in items.values()]
            if method == "PUT":
                return {"ok": True}
            return None

        s = updater.GitHubStore("me/private-repo", "tok", "bot-sync")
        s._api = fake
        self.assertTrue(s.signal().need_update)
        self.assertEqual(s.read("config.txt"), "A = 1")
        self.assertEqual(len([c for c in calls if c[0] == "GET"]), 1,
                         "一次列目录就该拿到全部小文件内容")
        s.write("runtime.json", '{"x": 1}')
        puts = [c for c in calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(base64.b64decode(puts[0][2]["content"]).decode("utf-8"),
                         '{"x": 1}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
