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
import urllib.error
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
        # 默认信号源是公共仓库本体 signals/；离线测试里指向临时目录，
        # 放一个 run0 表示「没有待更新」，避免每个用例都被自检刷屏。
        self.signals = self.root / "signals"
        (self.signals / "wm-bot").mkdir(parents=True)
        (self.signals / "run0").write_text("0", encoding="utf-8")
        (self.signals / "wm-bot" / "run0").write_text("0", encoding="utf-8")
        # 公共仓库本体的「模板 + 迁移规则」在测试里指向临时小文件，让 reconcile
        # 行为可预测（不会把真实 346 行模板 / 真实 schema 混进来）。
        self.template_file = self.root / "public_config.txt"
        self.template_file.write_text(
            "# 公共配置模板（测试用）\nWM_QQ_APPID = \nWM_QQ_SECRET = \nNEW_FLAG = 1\n",
            encoding="utf-8")
        self.schema_file = self.root / "public_schema.json"
        self.schema_file.write_text(
            json.dumps({"version": 2, "rename": {"users": "members"}}),
            encoding="utf-8")
        p1 = mock.patch.multiple(
            updater, SIGNAL_DIR=self.signals, STATE_DIR=self.state,
            ERROR_LOG=self.state / "updater_error.log",
            FLOW_LOG=self.state / "updater_flow.log",
            PACE_FILE=self.state / "pace.json",
            STATE_FILE=self.state / "state.json",
            BACKUP_ROOT=self.state / "backup",
            LOCK_FILE=self.state / "updater.lock",
            TEMPLATE_PATH=self.template_file,
            SCHEMA_PATH=self.schema_file)
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


class TestReconcileConfig(_Base):
    TEMPLATE = ("# 模板注释\nWM_QQ_APPID = \nWM_QQ_SECRET = \n"
                "NEW_FLAG = 1\n")
    VALUES = ("# 本地注释\nWM_QQ_APPID = REAL_APPID\n"
              "WM_QQ_SECRET = REAL_SECRET\nOLD_FIELD = local-only\n")

    def test_template_is_base_and_secrets_kept(self):
        text, rep = updater.reconcile_config(self.TEMPLATE, self.VALUES)
        self.assertIn("WM_QQ_SECRET = REAL_SECRET", text, "密钥绝能被模板空值冲掉")
        self.assertIn("NEW_FLAG = 1", text, "模板里的新字段要出现")
        self.assertIn("WM_QQ_APPID = REAL_APPID", text)
        self.assertIn("NEW_FLAG", rep["added"])
        self.assertIn("WM_QQ_SECRET", rep["kept"])
        self.assertEqual(rep["removed"], [], "方案 A 不再删除任何字段")

    def test_unknown_field_is_kept_not_dropped(self):
        text, rep = updater.reconcile_config(self.TEMPLATE, self.VALUES)
        self.assertIn("OLD_FIELD = local-only", text, "私有有、模板没有的字段必须保留")
        self.assertIn("OLD_FIELD", rep["extra_kept"])

    def test_new_fields_reported_for_push(self):
        # 模板有、私有没有 → 这些是新字段，需要回写私有库把空位补上
        text, rep = updater.reconcile_config(self.TEMPLATE, "WM_QQ_APPID = X\n")
        self.assertEqual(rep["new_fields"], ["NEW_FLAG", "WM_QQ_SECRET"])
        self.assertIn("NEW_FLAG = 1", text)

    def test_overwrite_returns_values_verbatim(self):
        text, _ = updater.reconcile_config(self.TEMPLATE, self.VALUES, "overwrite")
        self.assertEqual(text, self.VALUES, "overwrite 模式原样返回私有值")

    def test_no_values_uses_template(self):
        text, rep = updater.reconcile_config(self.TEMPLATE, None)
        self.assertEqual(text, self.TEMPLATE, "私有没有值时给模板（全空）等本人填")
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

    def test_fetch_bundle_reads_local_schema_and_private_values(self):
        (self.remote / "config.txt").write_text("A = 1", encoding="utf-8")
        (self.remote / "runtime.json").write_text('{"ws_subs.json": {}}', encoding="utf-8")
        schema_file = self.root / "schema.json"
        schema_file.write_text('{"version": 5}', encoding="utf-8")
        old_path = updater.SCHEMA_PATH
        updater.SCHEMA_PATH = schema_file
        try:
            b = updater.fetch_bundle(self.store())
            self.assertEqual(b["config"], "A = 1")
            self.assertEqual(b["schema"]["version"], 5, "规则改从公共仓库本地读")
        finally:
            updater.SCHEMA_PATH = old_path
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
        # 方案 A：真实值（密钥）存在私有库（remote）的 config.txt，模板在公共仓库；
        # 本地 data/config.txt 只是上一轮合并结果，会被覆盖，不提供值。
        (self.remote / "config.txt").write_text(
            "# 私有真实值\nWM_QQ_APPID = REAL\nWM_QQ_SECRET = SECRET\nNEW_FLAG = 1\n",
            encoding="utf-8")
        (self.remote / "runtime.json").write_text(json.dumps(
            {"ws_subs.json": {"version": 1, "sessions": {}}}), encoding="utf-8")
        (self.data / "config.txt").write_text("", encoding="utf-8")
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
        self.assertIn("公共仓库 signals/", text,
                      "缺信号时该指向公共库发布目录，而不是私有数据后端")
        self.assertIn("runN", text)
        self.assertIn("config.txt", text)

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
        """统一 RUN：装配数据后端与服务清单、拉起服务、Ctrl+C 时收干净。"""
        (self.root / "update.cfg").write_text("UPDATER_BACKEND = local\n",
                                              encoding="utf-8")
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

    def test_only_signaled_service_is_updated(self):
        """两个服务共用统一 RUN：只停 / 迁 / 启被点名的那个，指纹不串。"""
        (self.root / "update.cfg").write_text("UPDATER_BACKEND = local\n",
                                              encoding="utf-8")
        manifest = self.root / "services.json"
        manifest.write_text(json.dumps({"services": [
            {"name": "a", "entry": "a/run.py", "cwd": "a", "data_dir": "a/data",
             "config_file": "config.txt", "runtime_files": ["state.json"],
             "remote_path": "a"},
            {"name": "b", "entry": "b/run.py", "cwd": "b", "data_dir": "b/data",
             "config_file": "config.txt", "runtime_files": ["state.json"],
             "remote_path": "b"}]}), encoding="utf-8")
        for name, signal in (("a", "run1"), ("b", "run0")):
            d = self.remote / name
            d.mkdir(parents=True, exist_ok=True)
            (d / signal).write_text("1" if signal == "run1" else "0",
                                    encoding="utf-8")
            (d / "config.txt").write_text("K = \n", encoding="utf-8")
            (d / "schema.json").write_text("{}", encoding="utf-8")
            (d / "runtime.json").write_text("{}", encoding="utf-8")
            (self.root / name / "data").mkdir(parents=True, exist_ok=True)
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote),
                    "UPDATER_GIT_PULL": "0", "UPDATER_SYNC_ON_START": "0",
                    "UPDATER_SIGNAL_SOURCE": "data",   # 兼容旧布局：信号也在数据后端
                    "UPDATER_PUSH_RUNTIME": "0"})
        bot_a, bot_b = FakeBot(), FakeBot()
        with mock.patch.object(run, "load_cfg", return_value=cfg), \
             mock.patch.object(run, "SERVICES_FILE", manifest), \
             mock.patch.object(run, "BotProcess", side_effect=[bot_a, bot_b]), \
             mock.patch.object(run, "git_pull", return_value=(True, "mock")), \
             mock.patch.object(run.time, "sleep",
                               side_effect=[None, KeyboardInterrupt]), \
             mock.patch("builtins.print"):
            rc = run.main()
        self.assertEqual(rc, 0)
        self.assertEqual(bot_a.started, 2, "a 被更新：停后重新拉起")
        self.assertEqual(bot_b.started, 1, "b 没被触发：只有启动时那一次")
        self.assertIn("last_signal", updater.load_state("a"))
        self.assertEqual(updater.load_state("b"), {}, "b 的指纹不能被 a 串写")


class TestMultiService(_Base):
    """公共仓库本体 + 根目录优先配置 + 数据后端按服务隔离。"""

    def _svc(self, name="wm-bot", root=None):
        return run.Service({
            "name": name,
            "entry": "wm-bot/python/qq_bot.py",
            "cwd": "wm-bot",
            "data_dir": "wm-bot/python/data",
            "config_file": "config.txt",
            "runtime_files": ["ws_subs.json"],
            "remote_path": name,
        }, root or self.root)

    def test_services_json_is_the_public_repo_manifest(self):
        svcs = run.load_services()
        self.assertEqual([s.name for s in svcs], ["wm-bot"])
        self.assertTrue(svcs[0].entry.endswith("qq_bot.py"))
        self.assertEqual(svcs[0].remote_path, "wm-bot")
        self.assertIn("config.txt", str(svcs[0].config_file))

    def test_enabled_whitelist_and_unknown_name(self):
        svcs = [self._svc("a"), self._svc("b")]
        got = run.enabled_services(svcs, {"SERVICES_ENABLED": "b"})
        self.assertEqual([s.name for s in got], ["b"])
        self.assertEqual(len(run.enabled_services(svcs, {})), 2)
        with self.assertRaises(RuntimeError):
            run.enabled_services(svcs, {"SERVICES_ENABLED": "nope"})

    def test_duplicate_service_name_rejected(self):
        p = self.root / "services.json"
        p.write_text(json.dumps({"services": [
            {"name": "x", "entry": "a", "data_dir": "d"},
            {"name": "x", "entry": "b", "data_dir": "d"}]}), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            run.load_services(p)

    def test_data_backend_points_into_service_subdir(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "github", "UPDATER_GITHUB_REPO": "me/private",
                    "UPDATER_GITHUB_TOKEN": "tok", "UPDATER_GITHUB_PATH": "services"})
        self.assertIn("/contents/services?", updater.make_store(cfg)._url())
        self.assertIn("/contents/services/wm-bot?",
                      updater.make_store(cfg, "wm-bot")._url())
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote)})
        self.assertEqual(updater.make_store(cfg, "wm-bot").root,
                         self.remote / "wm-bot")

    def test_database_backend_is_explicitly_unimplemented(self):
        with self.assertRaises(NotImplementedError) as ctx:
            updater.make_store({"UPDATER_BACKEND": "database",
                                "UPDATER_DATA_URL": "postgres://x"})
        self.assertIn("Store", str(ctx.exception))

    def test_state_and_backup_are_isolated_per_service(self):
        updater.save_state({"mark": "A"}, "wm-bot")
        updater.save_state({"mark": "B"}, "other")
        self.assertEqual(updater.load_state("wm-bot")["mark"], "A")
        self.assertEqual(updater.load_state("other")["mark"], "B")
        self.assertEqual(updater.load_state(), {})
        self.assertIn("services", str(updater.backup_root("wm-bot")))

    def test_sync_code_bootstraps_without_checkout(self):
        calls = []

        def fake(args, timeout=60):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return 1, "", "not a git repo"
            return 0, "", ""

        with mock.patch.object(run, "_git", side_effect=fake), \
             mock.patch("builtins.print"):
            ok, info = run.sync_code(dict(run.DEFAULTS))
        self.assertTrue(ok)
        self.assertIn("bootstrap", info)
        flat = [" ".join(c) for c in calls]
        self.assertTrue(any("init" in c for c in flat))
        self.assertTrue(any("fetch" in c for c in flat))
        self.assertFalse(any("checkout" in c for c in flat),
                         "绝不能把本地较新的代码覆盖成远端旧版")

    def _remote_service(self, name="wm-bot"):
        remote = self.remote / name
        remote.mkdir(parents=True, exist_ok=True)
        for n, t in (("config.txt", "WM_QQ_APPID = REAL\n"),
                     ("runtime.json", "{}"), ("run1", "1")):
            (remote / n).write_text(t, encoding="utf-8")
        return updater.LocalStore(remote)

    def test_do_update_uses_service_paths_and_state(self):
        svc = self._svc("wm-bot", self.root)
        svc.data_dir.mkdir(parents=True, exist_ok=True)
        (svc.data_dir / "config.txt").write_text("WM_QQ_APPID = REAL\n",
                                                 encoding="utf-8")
        (svc.data_dir / "ws_subs.json").write_text(
            json.dumps({"version": 1, "sessions": {}}), encoding="utf-8")
        store = self._remote_service()
        sig = store.signal()
        with mock.patch.object(run, "git_pull", return_value=(True, "mock")):
            ok = run.do_update({"UPDATER_CONFIG_MERGE": "structure",
                                "UPDATER_PUSH_RUNTIME": "0"},
                               store, FakeBot(), sig, svc=svc)
        self.assertTrue(ok)
        self.assertIn("REAL", (svc.data_dir / "config.txt").read_text(encoding="utf-8"))
        self.assertEqual(updater.load_state("wm-bot")["last_signal"], sig.fingerprint)
        self.assertEqual(updater.load_state(), {}, "指纹不能再写全局 state")

    def test_pull_false_skips_git_pull_for_global_signal(self):
        svc = self._svc("wm-bot", self.root)
        svc.data_dir.mkdir(parents=True, exist_ok=True)
        store = self._remote_service()
        sig = store.signal()
        with mock.patch.object(run, "git_pull") as gp:
            ok = run.do_update({"UPDATER_PUSH_RUNTIME": "0"}, store, FakeBot(),
                               sig, svc=svc, pull=False)
        self.assertTrue(ok)
        gp.assert_not_called()


class TestSignalSeparation(_Base):
    """runN 是公共库的发布指令，不该跟着私有库 PAT 一起走。"""

    def test_default_signal_source_is_public_repo(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote),
                    "UPDATER_GITHUB_REPO": "me/private",
                    "UPDATER_GITHUB_TOKEN": "tok"})
        signals = updater.make_signal_store(cfg)
        data = updater.make_store(cfg, "wm-bot")
        self.assertIsInstance(signals, updater.LocalStore)
        self.assertEqual(signals.root, self.signals)
        self.assertEqual(data.root, self.remote / "wm-bot",
                         "信号后端与数据后端必须是两个不同目录")
        self.assertFalse(signals.signal().need_update)

    def test_data_source_is_backward_compatible(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote),
                    "UPDATER_SIGNAL_SOURCE": "data"})
        self.assertEqual(updater.make_signal_store(cfg, "wm-bot").root,
                         self.remote / "wm-bot")

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            updater.make_signal_store({"UPDATER_SIGNAL_SOURCE": "ftp"})

    def test_no_pat_needed_to_see_a_release(self):
        """私有目录里故意什么都不放：信号照样能判出要更新。"""
        (self.signals / "run1").write_text("1", encoding="utf-8")
        sig = updater.make_signal_store(dict(run.DEFAULTS)).signal()
        self.assertTrue(sig.need_update)
        self.assertEqual(sig.name, "run1")

    def test_code_signal_triggers_only_that_service(self):
        """新布局端到端：公共库 signals/a/run1 只更新 a，b 不受影响。"""
        (self.root / "update.cfg").write_text("UPDATER_BACKEND = local\n",
                                              encoding="utf-8")
        manifest = self.root / "services.json"
        manifest.write_text(json.dumps({"services": [
            {"name": "a", "entry": "a/run.py", "cwd": "a", "data_dir": "a/data",
             "config_file": "config.txt", "runtime_files": ["state.json"],
             "remote_path": "a"},
            {"name": "b", "entry": "b/run.py", "cwd": "b", "data_dir": "b/data",
             "config_file": "config.txt", "runtime_files": ["state.json"],
             "remote_path": "b"}]}), encoding="utf-8")
        for name, signal in (("a", "run1"), ("b", "run0")):
            sd = self.signals / name
            sd.mkdir(parents=True, exist_ok=True)
            (sd / signal).write_text("1", encoding="utf-8")
            rd = self.remote / name
            rd.mkdir(parents=True, exist_ok=True)
            for n, t in (("config.txt", "K = \n"), ("schema.json", "{}"),
                         ("runtime.json", "{}")):
                (rd / n).write_text(t, encoding="utf-8")
            (self.root / name / "data").mkdir(parents=True, exist_ok=True)
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote),
                    "UPDATER_GIT_PULL": "0", "UPDATER_SYNC_ON_START": "0",
                    "UPDATER_PUSH_RUNTIME": "0"})
        bot_a, bot_b = FakeBot(), FakeBot()
        with mock.patch.object(run, "load_cfg", return_value=cfg), \
             mock.patch.object(run, "SERVICES_FILE", manifest), \
             mock.patch.object(run, "BotProcess", side_effect=[bot_a, bot_b]), \
             mock.patch.object(run, "git_pull", return_value=(True, "mock")), \
             mock.patch.object(run.time, "sleep",
                               side_effect=[None, KeyboardInterrupt]), \
             mock.patch("builtins.print"):
            rc = run.main()
        self.assertEqual(rc, 0)
        self.assertEqual(bot_a.started, 2, "a 被更新：停后重新拉起")
        self.assertEqual(bot_b.started, 1, "b 没被触发：只有启动那一次")
        self.assertIn("last_signal", updater.load_state("a"))
        self.assertEqual(updater.load_state("b"), {}, "b 的指纹不能被 a 串写")


class TestOwner(_Base):
    """两个不同授权误指同一数据目录时，绝不互相覆盖。"""

    def _remote_with(self, name="wm-bot"):
        remote = self.remote / name
        remote.mkdir(parents=True, exist_ok=True)
        # 方案 A：真实值进私有库 config.txt；schema 已改从公共仓库读，私有库不再放
        for n, t in (("config.txt", "WM_QQ_APPID = REAL\n"),
                     ("runtime.json", "{}"), ("run1", "1")):
            (remote / n).write_text(t, encoding="utf-8")
        return updater.LocalStore(remote)

    def _owner_instance(self):
        return json.loads((self.remote / "wm-bot" / updater.OWNER_NAME).read_text(
            encoding="utf-8"))["instance"]

    def test_first_claim_then_same_instance_ok(self):
        store = self._remote_with()
        self.assertTrue(updater.claim_owner(store, "prod", "wm-bot")[0])
        self.assertEqual(self._owner_instance(), "prod")
        self.assertTrue(updater.claim_owner(store, "prod", "wm-bot")[0])

    def test_foreign_instance_is_refused_without_overwriting(self):
        store = self._remote_with()
        updater.claim_owner(store, "prod", "wm-bot")
        ok, owner = updater.claim_owner(store, "test", "wm-bot")
        self.assertFalse(ok)
        self.assertEqual(owner, "prod")
        self.assertEqual(self._owner_instance(), "prod")

    def test_foreign_owner_skips_push_but_local_migration_still_runs(self):
        store = self._remote_with()
        (self.remote / "wm-bot" / updater.OWNER_NAME).write_text(
            json.dumps({"instance": "prod"}), encoding="utf-8")
        original = (self.remote / "wm-bot" / "runtime.json").read_text(encoding="utf-8")
        (self.data / "config.txt").write_text("WM_QQ_APPID = REAL\n", encoding="utf-8")
        sig = store.signal()
        with mock.patch.object(run, "git_pull", return_value=(True, "mock")):
            ok = run.do_update({"UPDATER_PUSH_RUNTIME": "1",
                                "UPDATER_INSTANCE_ID": "test"},
                               store, FakeBot(), sig)
        self.assertTrue(ok, "认领冲突不该让本地迁移失败")
        self.assertEqual((self.remote / "wm-bot" / "runtime.json").read_text(
            encoding="utf-8"), original, "别人认领的目录绝不能被本实例覆盖")
        self.assertIn("REAL", (self.data / "config.txt").read_text(encoding="utf-8"))


class TestNamespace(_Base):
    def test_namespace_isolates_data_path(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote),
                    "UPDATER_DATA_NAMESPACE": "prod"})
        self.assertEqual(updater.make_store(cfg, "wm-bot").root,
                         self.remote / "prod" / "wm-bot")
        cfg.update({"UPDATER_BACKEND": "github",
                    "UPDATER_GITHUB_REPO": "me/private",
                    "UPDATER_GITHUB_TOKEN": "tok",
                    "UPDATER_GITHUB_PATH": "services"})
        self.assertIn("/contents/services/prod/wm-bot?",
                      updater.make_store(cfg, "wm-bot")._url())


class TestPreflightMultiInstance(_Base):
    def test_shared_data_dir_is_reported(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_GIT_PULL": "0", "UPDATER_SIGNAL_SOURCE": "data"})
        def svc(name):
            return run.Service({"name": name, "entry": name, "cwd": name,
                                "data_dir": "same", "config_file": "config.txt",
                                "runtime_files": []}, self.root)
        services = [svc("a"), svc("b")]
        with mock.patch("builtins.print"):
            problems = run.preflight(cfg, self.store(), services,
                                     {"a": self.store(), "b": self.store()})
        self.assertIn("共用 data_dir", " ".join(problems))

    def test_missing_git_binary_is_reported(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_GIT_PULL": "1", "UPDATER_SIGNAL_SOURCE": "data"})
        (self.remote / "run0").write_text("0", encoding="utf-8")
        with mock.patch.object(run.subprocess, "run",
                               return_value=mock.Mock(returncode=1)), \
             mock.patch("builtins.print"):
            problems = run.preflight(cfg, self.store())
        self.assertIn("没有 git 命令", " ".join(problems))


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

    def test_fetch_bundle_refreshes_private_listing(self):
        """私有库列目录缓存必须由 fetch_bundle 主动失效，否则读不到刚改的 config。"""
        gets = []

        def fake(method, url, body=None):
            if method == "GET" and "contents/wm-bot?" in url:
                gets.append(url)
                v = len(gets)
                return [{"name": "config.txt", "sha": "C%d" % v,
                         "content": _b64("K = %d" % v)}]
            if method == "GET" and "config.txt" in url:
                return {"name": "config.txt", "sha": "C%d" % len(gets),
                        "content": _b64("K = %d" % len(gets))}
            return None

        s = updater.GitHubStore("me/private-repo", "tok", "wm-bot")
        s._api = fake
        self.assertEqual(updater.fetch_bundle(s)["config"], "K = 1")
        self.assertEqual(updater.fetch_bundle(s)["config"], "K = 2",
                         "第二轮必须重新列目录，不能拿上一轮缓存的旧配置")
        self.assertEqual(len(gets), 2)

    def test_write_puts_with_current_sha(self):
        """正常回写：带当前 sha 一次 PUT 成功，不自行重试。"""
        calls = []
        def fake(method, url, body=None):
            calls.append((method, url, body))
            if method == "GET":
                return [{"name": "runtime.json", "sha": "S1",
                         "content": _b64("{}")}]
            return {"ok": True}
        s = updater.GitHubStore("me/private-repo", "tok", "wm-bot")
        s._api = fake
        s.write("runtime.json", '{"x": 1}')
        puts = [c for c in calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][2]["sha"], "S1")

    def test_write_raises_on_409_no_internal_retry(self):
        """并发改写（409）立即抛出：不自行重试同内容，交由调用方重拉-重合并。"""
        calls = []
        def fake(method, url, body=None):
            calls.append((method, url, body))
            if method == "GET":
                return [{"name": "runtime.json", "sha": "S1",
                         "content": _b64("{}")}]
            if method == "PUT":
                raise urllib.error.HTTPError(url, 409, "conflict", {}, None)
            return None
        s = updater.GitHubStore("me/private-repo", "tok", "wm-bot")
        s._api = fake
        with self.assertRaises(urllib.error.HTTPError):
            s.write("runtime.json", '{"x": 1}')
        puts = [c for c in calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1, "409 立即抛出，不浪费同内容重试")


class TestPushRuntimeRebase(_Base):
    """回写冲突 / 失败时的「重拉-重合并」兜底（防并发改写与写回卡死丢数据）。"""

    def test_rebases_on_conflict_then_succeeds(self):
        # 第一轮回写被并发改写（异常）→ 重新拉最新云端 runtime，以本地迁移结果为基准
        # 再合云端独有文件后重试，第二轮回写成功，不丢别人的数据。
        class Store(object):
            def __init__(self):
                self.writes = 0
                self.last = None
                self.cloud = {"ws_filters.json": {"version": 1, "sessions": {"g|1": ["古纪"]}}}
            def refresh(self):
                pass
            def read(self, name):
                return json.dumps(self.cloud)
            def write(self, name, text):
                self.writes += 1
                if self.writes == 1:
                    raise RuntimeError("409 conflict")
                self.last = text
        store = Store()
        local = {"ws_subs.json": {"version": 1, "sessions": {"g|1": {"users": ["alerts"]}}}}
        ok = updater.push_runtime(store, local, schema={}, service="wm-bot")
        self.assertTrue(ok)
        pushed = json.loads(store.last)
        # 本地字段保留
        self.assertEqual(pushed["ws_subs.json"]["sessions"]["g|1"]["users"], ["alerts"])
        # 云端独有文件也保留（别人的数据没被冲掉）
        self.assertIn("ws_filters.json", pushed)
        self.assertEqual(pushed["ws_filters.json"]["sessions"]["g|1"], ["古纪"])

    def test_reports_anomaly_after_max_attempts(self):
        class Store(object):
            def refresh(self):
                pass
            def read(self, name):
                return json.dumps({"ws_subs.json": {"version": 1}})
            def write(self, name, text):
                raise RuntimeError("persistent 409")
        store = Store()
        ok = updater.push_runtime(store, {"ws_subs.json": {"version": 1}}, service="wm-bot")
        self.assertFalse(ok, "持续冲突 / 失败应返回 False 并报「私有库异常」")


class TestGiteeStore(unittest.TestCase):
    """Gitee 备用后端：把与 GitHub 的三处 API 差异都验证到，不联网。"""

    def test_new_file_uses_post_existing_uses_put_with_sha(self):
        calls = []
        listing = []

        def fake(method, url, body=None):
            calls.append((method, url, body))
            if method == "GET":
                return list(listing)
            return {"ok": True}

        s = updater.GiteeStore("me/private", "gtok", "wm-bot")
        s._api = fake
        s.write("runtime.json", '{"x": 1}')      # 目录里没有 → POST 新建
        self.assertEqual(calls[-1][0], "POST")
        self.assertNotIn("sha", calls[-1][2])
        listing.append({"name": "runtime.json", "sha": "GSHA", "content": _b64("{}")})
        s.refresh()
        s.write("runtime.json", '{"x": 2}')      # 已存在 → PUT + 当前 sha
        self.assertEqual(calls[-1][0], "PUT")
        self.assertEqual(calls[-1][2]["sha"], "GSHA")
        self.assertEqual(base64.b64decode(calls[-1][2]["content"]).decode("utf-8"),
                         '{"x": 2}')

    def test_access_token_in_query_and_gitee_host_pacing(self):
        seen = {}

        def fake_pace(host):
            seen["host"] = host

        class R(object):
            def read(self):
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = dict(req.header_items())
            return R()

        s = updater.GiteeStore("me/private", "tok with space", "wm-bot")
        with mock.patch.object(updater, "pace_gate", side_effect=fake_pace), \
             mock.patch.object(updater.urllib.request, "urlopen",
                               side_effect=fake_urlopen):
            s._dir()
        self.assertEqual(seen["host"], "gitee.com", "节流必须按 gitee.com 域名")
        self.assertIn("access_token=tok%20with%20space", seen["url"])
        self.assertNotIn("Authorization", seen["headers"],
                         "Gitee 不认 Bearer，认证只能走 access_token")

    def test_listing_without_content_falls_back_to_single_file_get(self):
        gets = []

        def fake(method, url, body=None):
            gets.append(url)
            if "contents/wm-bot?" in url:
                return [{"name": "config.txt", "sha": "S1"}]   # Gitee 目录列表不带 content
            return {"name": "config.txt", "sha": "S1", "content": _b64("K = 9")}

        s = updater.GiteeStore("me/private", "gtok", "wm-bot")
        s._api = fake
        self.assertEqual(s.read("config.txt"), "K = 9")
        self.assertEqual(len(gets), 2, "列目录没带 content 时要再 GET 一次单文件")


class TestFailover(_Base):
    """主库 3 轮失败 → 自动切 Gitee → 全失败报「数据拉取异常」。"""

    def test_backup_store_created_only_when_token_is_set(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "github", "UPDATER_GITHUB_REPO": "me/private",
                    "UPDATER_GITHUB_TOKEN": "tok"})
        self.assertEqual(len(updater.make_store_chain(cfg, "wm-bot").stores), 1,
                         "没填 Gitee token 就只有主库，行为与旧版一致")
        cfg.update({"UPDATER_GITEE_REPO": "me/gitee-private",
                    "UPDATER_GITEE_TOKEN": "gtok"})
        chain = updater.make_store_chain(cfg, "wm-bot")
        self.assertEqual(len(chain.stores), 2)
        self.assertIsInstance(chain.stores[1], updater.GiteeStore)
        self.assertIn("/contents/services/wm-bot?", chain.stores[1]._url())
        cfg["UPDATER_FAILOVER"] = "0"
        self.assertEqual(len(updater.make_store_chain(cfg, "wm-bot").stores), 1,
                         "关掉故障转移就不装配备用库")

    def test_fetch_switches_to_gitee_after_three_primary_failures(self):
        class Primary(object):
            def __init__(self):
                self.reads = 0

            def refresh(self):
                pass

            def read(self, name):
                self.reads += 1
                raise RuntimeError("github down")

        class Backup(object):
            def __init__(self):
                self.reads = 0

            def refresh(self):
                pass

            def read(self, name):
                self.reads += 1
                return "{}" if name == updater.RUNTIME_NAME else "K = 1"

        primary, backup = Primary(), Backup()
        fs = updater.FailoverStore([primary, backup])
        bundle = updater.fetch_bundle_failover(fs, service="wm-bot")
        self.assertEqual(bundle["config"], "K = 1")
        self.assertEqual(primary.reads, 3, "主库必须试满 3 轮才允许切换")
        self.assertEqual(backup.reads, 2)
        self.assertEqual(fs.label(), "备用库(Gitee)")

    def test_fetch_all_fail_reports_data_anomaly(self):
        class Dead(object):
            def __init__(self):
                self.reads = 0

            def refresh(self):
                pass

            def read(self, name):
                self.reads += 1
                raise RuntimeError("down")

        a, b = Dead(), Dead()
        fs = updater.FailoverStore([a, b])
        with self.assertRaises(RuntimeError) as ctx, \
             mock.patch("builtins.print"):
            updater.fetch_bundle_failover(fs, service="wm-bot")
        self.assertEqual(str(ctx.exception), updater.DATA_ANOMALY)
        self.assertEqual(a.reads, 3)
        self.assertEqual(b.reads, 3)
        self.assertIn(updater.DATA_ANOMALY,
                      (self.state / "updater_error.log").read_text(encoding="utf-8"))

    def test_push_switches_to_gitee_after_three_primary_failures(self):
        class Store(object):
            def __init__(self, fail_times=0):
                self.fail_times = fail_times
                self.writes = 0
                self.last = None

            def refresh(self):
                pass

            def read(self, name):
                return json.dumps({"ws_subs.json": {"version": 1}})

            def write(self, name, text):
                self.writes += 1
                if self.writes <= self.fail_times:
                    raise RuntimeError("github down")
                self.last = text

        primary, backup = Store(fail_times=99), Store()
        fs = updater.FailoverStore([primary, backup])
        ok = updater.push_runtime_failover(fs, {"ws_subs.json": {"version": 2}},
                                           schema={}, service="wm-bot")
        self.assertTrue(ok)
        self.assertEqual(primary.writes, 3, "主库 3 轮回写都失败")
        self.assertIsNotNone(backup.last, "切到 Gitee 后回写成功")
        self.assertEqual(fs.index, 1)

    def test_push_all_fail_reports_data_anomaly(self):
        class Dead(object):
            def __init__(self):
                self.writes = 0

            def refresh(self):
                pass

            def read(self, name):
                return "{}"

            def write(self, name, text):
                self.writes += 1
                raise RuntimeError("down")

        a, b = Dead(), Dead()
        fs = updater.FailoverStore([a, b])
        with mock.patch("builtins.print"):
            ok = updater.push_runtime_failover(fs, {"a.json": {}},
                                               schema={}, service="wm-bot")
        self.assertFalse(ok)
        self.assertEqual(a.writes, 3)
        self.assertEqual(b.writes, 3)
        self.assertIn(updater.DATA_ANOMALY,
                      (self.state / "updater_error.log").read_text(encoding="utf-8"))

    def test_slow_write_is_logged_as_window_overrun(self):
        class Slow(object):
            def __init__(self):
                self.writes = 0

            def refresh(self):
                pass

            def read(self, name):
                return "{}"

            def write(self, name, text):
                self.writes += 1
                raise RuntimeError("slow")

        import time as _time
        seq = iter([0, 6, 10, 16, 20, 26])
        fake_time = mock.Mock()
        fake_time.time = mock.Mock(side_effect=lambda: next(seq))
        fake_time.strftime = _time.strftime
        store = Slow()
        with mock.patch.object(updater, "time", fake_time):
            ok = updater.push_runtime(store, {"a.json": {}}, service="wm-bot")
        self.assertFalse(ok)
        self.assertEqual(store.writes, 3)
        flow = (self.state / "updater_flow.log").read_text(encoding="utf-8")
        self.assertIn("窗口过长", flow, "单轮超 5 秒必须记「窗口过长」并回到第一步重拉")


class TestGitPullFailover(_Base):
    """代码同步：git pull 失败自动改从 Gitee 公共库拉，全失败报数据拉取异常。"""

    def test_primary_pull_failure_falls_back_to_gitee(self):
        calls = []

        def fake(args, timeout=60):
            calls.append(list(args))
            if len(args) == 2:                      # 主库 git pull --ff-only
                return 1, "", "github down"
            return 0, "Already up to date.", ""

        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(dict(run.DEFAULTS))
        self.assertTrue(ok)
        self.assertIn("备用库(Gitee)", info)
        self.assertEqual(len(calls), 2)
        self.assertIn("https://gitee.com/xdswang/dxx-QQbot.git", calls[1])
        self.assertIn("main", calls[1])

    def test_both_pulls_fail_report_data_anomaly(self):
        def fake(args, timeout=60):
            return 1, "", "down"

        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(dict(run.DEFAULTS))
        self.assertFalse(ok)
        self.assertIn(updater.DATA_ANOMALY, info)

    def test_failover_disabled_does_not_try_gitee(self):
        calls = []

        def fake(args, timeout=60):
            calls.append(list(args))
            return 1, "", "down"

        cfg = dict(run.DEFAULTS)
        cfg["UPDATER_FAILOVER"] = "0"
        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(cfg)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1, "关掉故障转移就不该打第二次 git pull")
        self.assertIn(updater.DATA_ANOMALY, info)


if __name__ == "__main__":
    unittest.main(verbosity=2)
