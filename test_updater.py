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
        # 离线测试用临时 signals/ 目录放 run0，供底层 LocalStore 的旧能力测试使用；
        # 编排层「要不要更新」的判断已不走这些文件。
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
            LOCK_DIR=self.state / "locks",
            LOCK_FILE=self.state / "locks" / "updater.lock",
            TEMPLATE_PATH=self.template_file,
            SCHEMA_PATH=self.schema_file)
        p2 = mock.patch.multiple(
            run, BOT_DATA_DIR=self.data, BOT_CONFIG=self.data / "config.txt",
            CFG_FILE=self.root / "update.cfg", HERE=self.root)
        # 离线测试：默认禁掉真实 git 拷贝（GitSignalStore 走模块级 git_cmd）
        p3 = mock.patch.object(updater, "git_cmd",
                               return_value=(1, "", "offline-test: git disabled"))
        self._git_cmd_patch = p3
        p1.start()
        p2.start()
        p3.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(p3.stop)
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
        with mock.patch.object(run, "_replace_process") as ex, \
             mock.patch("builtins.print"):
            self.assertFalse(run.restart_self(FakeBot()))
        ex.assert_not_called()

    def test_good_code_stops_bot_then_execv(self):
        self._files()
        bot = FakeBot()
        with mock.patch.object(run, "_replace_process") as ex, \
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
                    "UPDATER_GIT_PULL": "0",
                    "UPDATER_SIGNAL_SOURCE": "code"})
        with mock.patch("builtins.print"):
            problems = run.preflight(cfg, self.store())
        text = " ".join(problems)
        self.assertIn("占位", text)
        self.assertNotIn("runN", text, "runN 判断已停用，自检不该再提示信号文件")
        self.assertNotIn("公共仓库 signals/", text)
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
                    "UPDATER_SIGNAL_SOURCE": "data",   # 旧配置已停用，写了也会被忽略
                    "UPDATER_PUSH_RUNTIME": "0"})
        bot_a, bot_b = FakeBot(), FakeBot()

        def fake_signal_store(cfg, service=None, chain=None):
            return updater.LocalStore(
                self.remote / service if service else self.remote)

        p = mock.patch.object(updater, "GitSignalStore",
                              side_effect=fake_signal_store)
        p.start()
        self.addCleanup(p.stop)
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

        cfg = dict(run.DEFAULTS)
        cfg["UPDATER_CODE_REPO"] = "https://example.com/public.git"
        with mock.patch.object(run, "_git", side_effect=fake), \
             mock.patch("builtins.print"):
            ok, info = run.sync_code(cfg)
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
    """判断恒为 GIT 双源拷贝；runN 只是底层旧能力，旧配置盖不回去。"""

    def test_any_legacy_config_still_uses_git_copy(self):
        for source in ("code", "data", "ftp", "git", ""):
            cfg = dict(run.DEFAULTS)
            cfg.update({"UPDATER_BACKEND": "local",
                        "UPDATER_LOCAL_DIR": str(self.remote),
                        "UPDATER_GITHUB_REPO": "me/private",
                        "UPDATER_GITHUB_TOKEN": "tok",
                        "UPDATER_SIGNAL_SOURCE": source})
            self.assertIsInstance(updater.make_signal_store(cfg),
                                  updater.GitSignalStore,
                                  "旧配置 %r 不能把判断盖回 runN" % source)

    def test_signal_store_is_per_service(self):
        cfg = dict(run.DEFAULTS)
        cfg["UPDATER_LOCAL_DIR"] = str(self.remote)
        self.assertEqual(
            updater.make_signal_store(cfg, "wm-bot").state_key, "wm-bot")

    def test_local_run_signal_is_only_a_legacy_capability(self):
        """底层 LocalStore 仍能读 runN，但编排层不再用它判断要不要更新。"""
        (self.signals / "run1").write_text("1", encoding="utf-8")
        sig = updater.LocalStore(self.signals).signal()
        self.assertTrue(sig.need_update)
        self.assertEqual(sig.name, "run1")

    def test_data_and_signal_backends_are_still_separate(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote)})
        data = updater.make_store(cfg, "wm-bot")
        self.assertEqual(data.root, self.remote / "wm-bot",
                         "数据后端仍按服务落到私有目录，不受判断方式影响")

    def test_code_signal_triggers_only_that_service(self):
        """端到端：只触发一个服务（信号源换成按服务隔离的本地目录）。"""
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
                    "UPDATER_SIGNAL_SOURCE": "code",
                    "UPDATER_PUSH_RUNTIME": "0"})
        bot_a, bot_b = FakeBot(), FakeBot()

        def fake_signal_store(cfg, service=None, chain=None):
            return updater.LocalStore(
                self.signals / service if service else self.signals)

        p = mock.patch.object(updater, "GitSignalStore",
                              side_effect=fake_signal_store)
        p.start()
        self.addCleanup(p.stop)
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
    """代码同步：两个接口都用 GIT 指令拷贝，比版本用最新的；全失败报数据拉取异常。"""

    def test_primary_fetch_failure_uses_gitee_copy(self):
        calls = []

        def fake(args, timeout=60):
            calls.append(list(args))
            if args[0] == "fetch":
                if any("gitee" in a for a in args):
                    return 0, "", ""
                return 1, "", "github down"
            if args[0] == "rev-parse":
                return 0, "GSHA111", ""
            if args[0] == "log":
                return 0, ("100" if args[-1] == "HEAD" else "200"), ""
            if args[0] == "merge":
                return 0, "Fast-forward", ""
            return 0, "", ""

        cfg = dict(run.DEFAULTS)
        cfg["UPDATER_CODE_REPO_BACKUP"] = "git@gitee.com:mirror/public.git"
        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(cfg)
        self.assertTrue(ok)
        self.assertIn("备用库(Gitee)", info)
        fetches = [c for c in calls if c[0] == "fetch"]
        self.assertEqual(len(fetches), 2, "主库(origin)无可派生备选协议，备用库首协议即成功，共 2 次 fetch")
        self.assertTrue(any(c[0] == "merge" for c in calls))

    def test_primary_https_fails_falls_back_to_ssh_same_repo(self):
        calls = []
        def fake(args, timeout=60):
            calls.append(list(args))
            if args[0] == "fetch":
                url = args[2] if len(args) > 2 else ""
                # 主库 HTTPS 失败，但 SSH 备选成功；关掉备用库聚焦协议兜底
                if url == "git@github.com:mirror/public.git":
                    return 0, "", ""
                return 1, "", "https down"
            if args[0] == "rev-parse":
                return 0, "SHA111", ""
            if args[0] == "log":
                if args[-1] == "HEAD":
                    return 0, "100", ""      # 本地较旧，需要合并
                return 0, "300", ""           # FETCH_HEAD 时间
            if args[0] == "merge":
                return 0, "Fast-forward", ""
            return 0, "", ""
        cfg = dict(run.DEFAULTS)
        cfg["UPDATER_CODE_REPO"] = "https://github.com/mirror/public.git"
        cfg["UPDATER_FAILOVER"] = "0"
        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(cfg)
        self.assertTrue(ok)
        fetches = [c for c in calls if c[0] == "fetch"]
        self.assertEqual(len(fetches), 2, "主库 HTTPS 失败应改试 SSH 备选，共 2 次 fetch")
        self.assertTrue(any(c[0] == "merge" for c in calls))

    def test_newer_gitee_version_is_the_one_merged(self):
        calls = []

        def fake(args, timeout=60):
            calls.append(list(args))
            if args[0] == "fetch":
                return 0, "", ""
            if args[0] == "rev-parse":
                n = sum(1 for c in calls if c[0] == "rev-parse")
                return 0, ("AAA111" if n == 1 else "BBB222"), ""
            if args[0] == "log":
                if args[-1] == "HEAD":
                    return 0, "100", ""
                n = sum(1 for c in calls if c[0] == "log" and c[-1] != "HEAD")
                return 0, ("200" if n == 1 else "300"), ""
            if args[0] == "merge":
                return 0, "Fast-forward", ""
            return 0, "", ""

        cfg = dict(run.DEFAULTS)
        cfg["UPDATER_CODE_REPO_BACKUP"] = "git@gitee.com:mirror/public.git"
        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(cfg)
        self.assertTrue(ok)
        self.assertIn("备用库(Gitee)", info)
        merges = [c for c in calls if c[0] == "merge"]
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0][-1], "BBB222", "必须合并版本更新的那个接口")

    def test_local_newer_than_remote_does_not_roll_back(self):
        calls = []

        def fake(args, timeout=60):
            calls.append(list(args))
            if args[0] == "fetch":
                return 0, "", ""
            if args[0] == "rev-parse":
                return 0, "AAA111", ""
            if args[0] == "log":
                return 0, ("900" if args[-1] == "HEAD" else "200"), ""
            return 0, "", ""

        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(dict(run.DEFAULTS))
        self.assertTrue(ok)
        self.assertIn("本地版本已是最新", info)
        self.assertFalse(any(c[0] == "merge" for c in calls),
                         "本地比远端新时绝不能回退覆盖")

    def test_both_fetches_fail_report_data_anomaly(self):
        def fake(args, timeout=60):
            return 1, "", "down"

        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(dict(run.DEFAULTS))
        self.assertFalse(ok)
        self.assertIn(updater.DATA_ANOMALY, info)

    def test_failover_disabled_only_copies_primary(self):
        calls = []

        def fake(args, timeout=60):
            calls.append(list(args))
            return 1, "", "down"

        cfg = dict(run.DEFAULTS)
        cfg["UPDATER_FAILOVER"] = "0"
        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(cfg)
        self.assertFalse(ok)
        self.assertEqual(len([c for c in calls if c[0] == "fetch"]), 1)
        self.assertIn(updater.DATA_ANOMALY, info)


class TestGitSignalStore(_Base):
    """更新判断只看 GIT 拷贝结果：比版本、无变化不更新、单源失败降级、全失败报异常。"""

    def _cfg(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_SIGNAL_SOURCE": "git",
                    "UPDATER_DATA_REPO": "git@github.com:me/private.git",
                    "UPDATER_DATA_REPO_BACKUP": "git@gitee.com:me/private.git"})
        return cfg

    def _fake_git(self, results):
        """results = [(ok, sha, ts)]，按 fetch 顺序给出每个接口的版本。"""
        seq = iter(results)
        cur = {"v": None}

        def fake(args, cwd=None, timeout=None):
            if args[0] == "init":
                return 0, "", ""
            if args[0] == "fetch":
                cur["v"] = next(seq)
                return (0, "", "") if cur["v"][0] else (1, "", "down")
            if args[0] == "rev-parse":
                return (0, cur["v"][1], "") if cur["v"][0] else (1, "", "")
            if args[0] == "log":
                return (0, cur["v"][2], "") if cur["v"][0] else (1, "", "")
            return 0, "", ""

        return fake

    def test_default_source_is_git_not_run_files(self):
        self.assertIsInstance(updater.make_signal_store(dict(run.DEFAULTS)),
                              updater.GitSignalStore)

    def test_newer_gitee_version_wins_and_switches_data_backend(self):
        chain = updater.FailoverStore([object(), object()])
        store = updater.GitSignalStore(self._cfg(), chain=chain)
        fake = self._fake_git([(True, "AAA111", "100"),
                               (True, "BBB222", "300")])
        with mock.patch.object(updater, "git_cmd", side_effect=fake):
            sig = store.signal()
        self.assertTrue(sig.need_update)
        self.assertEqual(sig.fingerprint, "BBB222")
        self.assertEqual(chain.index, 1, "数据读写要跟着版本最新的那个接口")

    def test_copy_without_changes_means_no_update(self):
        updater.save_state({"last_signal": "AAA111"}, "__global__")
        store = updater.GitSignalStore(self._cfg())
        fake = self._fake_git([(True, "AAA111", "100"),
                               (True, "AAA111", "100")])
        with mock.patch.object(updater, "git_cmd", side_effect=fake):
            sig = store.signal()
        self.assertFalse(sig.need_update, "拷贝没有内容更新就是不用更新")
        flow = (self.state / "updater_flow.log").read_text(encoding="utf-8")
        self.assertIn("拷贝没有内容更新", flow)

    def test_one_interface_fails_use_the_successful_copy(self):
        chain = updater.FailoverStore([object(), object()])
        store = updater.GitSignalStore(self._cfg(), chain=chain)
        # 主库两种协议都失败才走备用库；每个不可达源会多发一次「备选协议」fetch
        fake = self._fake_git([(False, "", ""), (False, "", ""),
                               (True, "BBB222", "300")])
        with mock.patch.object(updater, "git_cmd", side_effect=fake):
            sig = store.signal()
        self.assertTrue(sig.need_update)
        self.assertEqual(sig.fingerprint, "BBB222")
        self.assertEqual(chain.index, 1)

    def test_both_fail_reports_data_anomaly(self):
        store = updater.GitSignalStore(self._cfg())
        # 两个源各两种协议都失败：共 4 次 fetch 都要给到结果
        fake = self._fake_git([(False, "", ""), (False, "", ""),
                               (False, "", ""), (False, "", "")])
        with mock.patch("builtins.print"),              mock.patch.object(updater, "git_cmd", side_effect=fake):
            sig = store.signal()
        self.assertFalse(sig.need_update)
        self.assertIn(updater.DATA_ANOMALY,
                      (self.state / "updater_error.log").read_text(encoding="utf-8"))

    def test_owner_repo_only_config_still_has_a_copy_source(self):
        cfg = self._cfg()
        cfg["UPDATER_DATA_REPO"] = ""
        cfg["UPDATER_DATA_REPO_BACKUP"] = ""
        cfg["UPDATER_GITHUB_REPO"] = "me/private"
        store = updater.GitSignalStore(cfg)
        self.assertEqual(len(store.sources()), 1)
        self.assertIn("github.com/me/private.git", store.sources()[0][1])

    def test_primary_protocol_fails_falls_back_to_alternate_same_repo(self):
        chain = updater.FailoverStore([object(), object()])
        store = updater.GitSignalStore(self._cfg(), chain=chain)
        # 主库 SSH 不可达、但 HTTPS 备选可达；备用库两种协议都不可达。
        # 每个不可达源会多发一次「备选协议」fetch，结果要按 fetch 顺序给够。
        fake = self._fake_git([(False, "", ""), (True, "AAA111", "100"),
                               (False, "", ""), (False, "", "")])
        with mock.patch.object(updater, "git_cmd", side_effect=fake):
            sig = store.signal()
        self.assertTrue(sig.need_update)
        self.assertEqual(sig.fingerprint, "AAA111")
        self.assertEqual(chain.index, 0, "主库靠备选协议成功，仍是主库(idx=0)")

    def test_repo_unreachable_on_both_protocols_falls_through_to_next_repo(self):
        chain = updater.FailoverStore([object(), object()])
        store = updater.GitSignalStore(self._cfg(), chain=chain)
        # 主库两种协议都不可达 → 落到备用库（index=1）。
        fake = self._fake_git([(False, "", ""), (False, "", ""),
                               (True, "BBB222", "300")])
        with mock.patch.object(updater, "git_cmd", side_effect=fake):
            sig = store.signal()
        self.assertTrue(sig.need_update)
        self.assertEqual(sig.fingerprint, "BBB222")
        self.assertEqual(chain.index, 1)


class TestGitRepoStore(_Base):
    """纯 GIT 数据后端：读用 git show、回写用 commit + push，全程不碰 API / token。"""

    def _cfg(self):
        return {"UPDATER_BACKEND": "git", "UPDATER_GIT_PATH": "services",
                "UPDATER_DATA_REPO": "https://github.com/me/private.git",
                "UPDATER_DATA_REPO_BACKUP": "git@gitee.com:me/private.git",
                "UPDATER_DATA_BRANCH": "main", "UPDATER_FAILOVER": "1"}

    def _prepare(self, store):
        (store.mirror / ".git").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _fake(calls, push_ok=True):
        def fake(args, cwd=None, timeout=60, env=None, strip=True):
            a = list(args)
            calls.append((a, env, strip))
            if "fetch" in a:
                return 0, "", ""
            if "rev-parse" in a:
                return 0, "BASE111", ""
            if "show" in a:
                return 0, "WM_QQ_APPID = REAL\n", ""
            if "hash-object" in a:
                return 0, "BLOB111", ""
            if "write-tree" in a:
                return 0, "TREE111", ""
            if "commit-tree" in a:
                return 0, "COMMIT111", ""
            if "push" in a:
                return (0, "", "") if push_ok else (1, "", "non-fast-forward")
            return 0, "", ""
        return fake

    def test_make_store_git_uses_service_subpath(self):
        store = updater.make_store(self._cfg(), "wm-bot")
        self.assertIsInstance(store, updater.GitRepoStore)
        self.assertEqual(store._rel("config.txt"), "services/wm-bot/config.txt")
        self.assertEqual(store.base_ref, "refs/updater/wm-bot")

    def test_chain_has_both_git_sources_without_token(self):
        chain = updater.make_store_chain(self._cfg(), "wm-bot")
        self.assertEqual(len(chain.stores), 2)
        self.assertEqual(chain.stores[1].url, "git@gitee.com:me/private.git")
        self.assertFalse(hasattr(chain.current, "token"))

    def test_refresh_uses_private_ref_not_fetch_head(self):
        store = updater.make_store(self._cfg(), "wm-bot")
        self._prepare(store)
        calls = []
        with mock.patch.object(updater, "git_cmd", side_effect=self._fake(calls)), \
             mock.patch.object(updater, "pace_gate"):
            sha = store.refresh()
        self.assertEqual(sha, "BASE111")
        fetch = [c[0] for c in calls if "fetch" in c[0]][0]
        self.assertIn("main:refs/updater/wm-bot", fetch)
        self.assertNotIn("FETCH_HEAD", " ".join(fetch))

    def test_read_uses_git_show_and_keeps_trailing_newline(self):
        store = updater.make_store(self._cfg(), "wm-bot")
        self._prepare(store)
        calls = []
        with mock.patch.object(updater, "git_cmd", side_effect=self._fake(calls)), \
             mock.patch.object(updater, "pace_gate"):
            text = store.read("config.txt")
        self.assertEqual(text, "WM_QQ_APPID = REAL\n")
        show = [c for c in calls if "show" in c[0]][0]
        self.assertIn("refs/updater/wm-bot:services/wm-bot/config.txt", show[0])
        self.assertFalse(show[2], "文件内容不能被 strip 掉末尾换行")

    def test_write_is_commit_and_push(self):
        store = updater.make_store(self._cfg(), "wm-bot")
        self._prepare(store)
        calls = []
        with mock.patch.object(updater, "git_cmd", side_effect=self._fake(calls)), \
             mock.patch.object(updater, "pace_gate"):
            store.write("runtime.json", '{"a": 1}\n')
        flat = [" ".join(c[0]) for c in calls]
        self.assertTrue(any("hash-object" in x for x in flat))
        self.assertTrue(any("commit-tree" in x for x in flat))
        pushes = [c[0] for c in calls if "push" in c[0]]
        self.assertEqual(len(pushes), 1)
        self.assertIn("COMMIT111:refs/heads/main", pushes[0])
        self.assertIn("https://github.com/me/private.git", pushes[0])

    def test_write_push_conflict_raises_for_retry(self):
        store = updater.make_store(self._cfg(), "wm-bot")
        self._prepare(store)
        with mock.patch.object(updater, "git_cmd",
                               side_effect=self._fake([], push_ok=False)), \
             mock.patch.object(updater, "pace_gate"):
            with self.assertRaises(RuntimeError):
                store.write("runtime.json", "{}")



class TestUnattended(_Base):
    """无人值守回归：只 push 代码也更新、失败能自愈、GIT 全过闸、崩溃/残留锁不致死。"""

    def test_code_signal_store_only_watches_public_repos(self):
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_CODE_REPO": "https://example.com/owner/public.git",
                    "UPDATER_CODE_REPO_BACKUP": "git@example.com:owner/public.git",
                    "UPDATER_CODE_BRANCH": "dev",
                    "UPDATER_DATA_REPO": "https://example.com/owner/private.git"})
        store = updater.make_code_signal_store(cfg)
        self.assertIsInstance(store, updater.CodeSignalStore)
        self.assertEqual(store.state_key, "__code__", "代码指纹不能污染私有库指纹")
        self.assertEqual(store.branch, "dev")
        urls = [u for _, u in store.sources()]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("public.git" in u for u in urls))
        self.assertIsNone(updater.make_code_signal_store(dict(run.DEFAULTS)),
                          "没配公共库地址就保持旧行为")

    def test_code_only_push_triggers_update_without_private_change(self):
        """V1：私有库完全没动，只 push 公共库代码，手机也必须自动同步并迁移。"""
        (self.root / "update.cfg").write_text("UPDATER_BACKEND = local\n",
                                              encoding="utf-8")
        manifest = self.root / "services.json"
        manifest.write_text(json.dumps({"services": [
            {"name": "wm-bot", "entry": "wm-bot/python/qq_bot.py",
             "cwd": "wm-bot", "data_dir": "wm-bot/python/data",
             "config_file": "config.txt", "runtime_files": ["state.json"],
             "remote_path": "wm-bot"}]}), encoding="utf-8")
        rd = self.remote / "wm-bot"
        rd.mkdir(parents=True, exist_ok=True)
        for n, t in (("config.txt", "K = \n"), ("schema.json", "{}"),
                     ("runtime.json", "{}")):
            (rd / n).write_text(t, encoding="utf-8")
        (self.root / "wm-bot" / "python" / "data").mkdir(parents=True,
                                                         exist_ok=True)
        cfg = dict(run.DEFAULTS)
        cfg.update({"UPDATER_BACKEND": "local",
                    "UPDATER_LOCAL_DIR": str(self.remote),
                    "UPDATER_GIT_PULL": "0", "UPDATER_SYNC_ON_START": "0",
                    "UPDATER_PUSH_RUNTIME": "0",
                    "UPDATER_CODE_REPO": "https://example.com/owner/public.git"})
        bot = FakeBot()
        fake_code = mock.Mock()
        fake_code.signal.return_value = updater.Signal("code:主库", "NEWCODE", True)
        with mock.patch.object(run, "load_cfg", return_value=cfg), \
             mock.patch.object(run, "SERVICES_FILE", manifest), \
             mock.patch.object(run, "BotProcess", return_value=bot), \
             mock.patch.object(run, "git_pull", return_value=(True, "mock")) as gp, \
             mock.patch.object(updater, "make_code_signal_store",
                               return_value=fake_code), \
             mock.patch.object(run.time, "sleep",
                               side_effect=[None, KeyboardInterrupt]), \
             mock.patch("builtins.print"):
            rc = run.main()
        self.assertEqual(rc, 0)
        gp.assert_called_once_with(cfg)
        self.assertEqual(bot.started, 2, "代码更新必须停服务、迁移、再拉起")
        self.assertEqual(updater.load_state("__code__")["last_signal"], "NEWCODE")

    def test_failed_signal_window_allows_retry_then_backs_off(self):
        """V2：失败后网络恢复但远端没新提交，也要在窗口过后自动重试。"""
        sig = updater.Signal("git", "BAD", True)
        with mock.patch.object(run.time, "time", return_value=1000.0):
            run.mark_failed(sig, "wm-bot")
        st = updater.load_state("wm-bot")
        self.assertEqual(st["failed_rounds"], 1)
        self.assertTrue(run._blocked_by_failure(st, "BAD", now=1001.0))
        self.assertFalse(run._blocked_by_failure(
            st, "BAD", now=1000.0 + run.FAILED_RETRY_SECONDS + 1),
            "窗口过后必须重新给机会，不能永久卡死")
        st["failed_rounds"] = run.FAILED_MAX_ROUNDS
        self.assertTrue(run._blocked_by_failure(
            st, "BAD", now=1000.0 + run.FAILED_RETRY_SECONDS + 1),
            "3 轮用尽后进入长退避，不无限静默轰炸")
        self.assertFalse(run._blocked_by_failure(
            st, "BAD", now=1000.0 + run.FAILED_LONG_PAUSE_SECONDS + 1))
        self.assertFalse(run._blocked_by_failure(st, "OTHER", now=1001.0))

    def test_normalize_host_merges_subdomains_and_exempts_local(self):
        """V3：同一域名的各种写法归到同一个闸门 key。"""
        self.assertEqual(
            updater.normalize_host("https://api.github.com/repos/x"),
            updater.normalize_host("https://github.com/me/repo.git"))
        self.assertEqual(
            updater.normalize_host("git@gitee.com:me/repo.git"),
            updater.normalize_host("https://gitee.com/api/v5/x"))
        self.assertEqual(updater.normalize_host(""), "")
        self.assertEqual(updater.normalize_host(r"C://tmp//remote.git"), "",
                         "本地文件系统路径不是地址访问")
        self.assertEqual(updater.normalize_host("/tmp/remote.git"), "")
        self.assertIn("localhost", updater.PACE_EXEMPT_HOSTS)

    def test_git_cmd_auto_paces_only_network_commands(self):
        """V3：fetch / push 自动过闸，本地 show 不排队。"""
        # 本类默认把 git_cmd 整个 mock 掉（离线测试禁联网）；这里临时恢复真实函数，
        # 只把底层 subprocess.run 换成假对象，才能验证闸门这一层。
        self._git_cmd_patch.stop()
        try:
            with mock.patch.object(updater, "pace_gate") as gate, \
                 mock.patch.object(updater.subprocess, "run") as sp:
                sp.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                updater.git_cmd(["fetch", "--no-tags",
                                 "https://github.com/me/p.git", "main"])
                updater.git_cmd(["push", "git@gitee.com:me/p.git",
                                 "C:refs/heads/main"])
                updater.git_cmd(["show", "refs/updater/x:services/config.txt"])
        finally:
            self._git_cmd_patch.start()
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(updater.normalize_host(gate.call_args_list[0][0][0]),
                         "github.com")
        self.assertEqual(updater.normalize_host(gate.call_args_list[1][0][0]),
                         "gitee.com")

    def test_git_pull_fetches_configured_url_for_pacing(self):
        """V3：代码同步主库用真实地址，不再写死 origin 让闸门没法归一。"""
        calls = []

        def fake(args, timeout=60):
            calls.append(list(args))
            if args[0] == "fetch":
                return 0, "", ""
            if args[0] == "rev-parse":
                return 0, "AAA111", ""
            if args[0] == "log":
                return 0, ("900" if args[-1] == "HEAD" else "100"), ""
            return 0, "", ""

        cfg = dict(run.DEFAULTS)
        cfg["UPDATER_CODE_REPO"] = "https://example.com/owner/public.git"
        with mock.patch.object(run, "_git", side_effect=fake):
            ok, info = run.git_pull(cfg)
        self.assertTrue(ok)
        fetches = [c for c in calls if c[0] == "fetch"]
        self.assertIn("https://example.com/owner/public.git", fetches[0])

    def test_derive_signal_reuses_one_copy_for_all_services(self):
        """V6：同一轮拷贝结果派生给所有服务，不再每个服务各 fetch 一遍。"""
        store = updater.GitSignalStore(
            {"UPDATER_DATA_REPO": "https://example.com/me/p.git",
             "UPDATER_DATA_BRANCH": "main"})
        base = {"label": "主库", "sha": "AAA111", "ts": 100, "index": 0}
        with mock.patch.object(store, "sync",
                               side_effect=AssertionError("不该再 fetch")):
            sig = updater.derive_signal(store, base, "wm-bot")
        self.assertEqual(sig.fingerprint, "AAA111")
        self.assertTrue(sig.need_update)
        (self.remote / "run1").write_text("1", encoding="utf-8")
        self.assertTrue(
            updater.derive_signal(updater.LocalStore(self.remote),
                                  None, "wm-bot").need_update,
            "旧 Store 仍走自己的 signal()")

    def test_crash_budget_resets_on_window_and_code_update(self):
        """V4：窗口过期或代码更新后重新给 3 次机会，不是永久不再拉起。"""
        ok, count, _ = run.next_crash_budget(0, 100.0, 101.0)
        self.assertTrue(ok)
        self.assertEqual(count, 1)
        ok, _, _ = run.next_crash_budget(3, 100.0, 102.0)
        self.assertFalse(ok, "窗口内第 4 次必须被拦下")
        ok, count, _ = run.next_crash_budget(
            3, 100.0, 100.0 + run.CRASH_WINDOW_SECONDS + 1)
        self.assertTrue(ok)
        self.assertEqual(count, 1)
        ok, count, _ = run.next_crash_budget(
            9, 100.0, 101.0, code_updated=True)
        self.assertTrue(ok)
        self.assertEqual(count, 1, "修复推上来后崩溃预算立刻清零")

    def test_second_instance_is_rejected_until_lock_is_released(self):
        """V5：OS 文件锁判单实例，残留锁不会因 pid 复用误拒启动。"""
        self.addCleanup(run.release_lock)
        self.assertTrue(run.acquire_lock())
        self.assertFalse(run.acquire_lock(), "第二个实例必须被拦下")
        run.release_lock()
        self.assertTrue(run.acquire_lock(), "锁释放后可以重新启动")
        run.release_lock()

    def test_bootstrap_pace_shares_state_with_updater(self):
        """V3：启动器 clone / pull 与长期运行共用同一份落盘节流状态。"""
        import importlib.util
        bs_path = (Path(__file__).resolve().parent.parent
                   / "dxx_private_data" / "bootstrap.py")
        spec = importlib.util.spec_from_file_location("bs_under_test", str(bs_path))
        bs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bs)
        self.assertEqual(bs.normalize_host("git@gitee.com:me/p.git"),
                         updater.normalize_host("git@gitee.com:me/p.git"))
        dest = self.root / "public"
        with mock.patch.object(bs, "LOCK_DIR", self.root / "locks"), \
             mock.patch.object(bs.time, "sleep") as slp:
            bs._pace("https://github.com/me/a.git", dest)
            bs._pace("https://api.github.com/me/b.git", dest)
        pf = dest / "data" / "updater_pace.json"
        self.assertTrue(pf.exists())
        state = json.loads(pf.read_text(encoding="utf-8"))
        self.assertEqual(list(state), ["github.com"],
                         "github.com 与 api.github.com 归到同一个 key")
        self.assertEqual(slp.call_count, 1, "第二次同域名访问被补齐间隔")

    def test_deploy_dir_defaults_to_sibling_of_private_repo(self):
        """手机/电脑公共库默认放私有库同级，不嵌进私有库 git 内部。"""
        import importlib.util
        bs_path = (Path(__file__).resolve().parent.parent
                   / "dxx_private_data" / "bootstrap.py")
        spec = importlib.util.spec_from_file_location("bs_deploy", str(bs_path))
        bs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bs)
        fake_root = Path("/data/user/0/com.termux/files/home/dxx_private_data")
        with mock.patch.object(bs, "HERE", fake_root):
            self.assertEqual(bs.deploy_dir({}),
                             fake_root.parent / "dxx-QQbot",
                             "默认部署目录是私有库根的同级 dxx-QQbot")
            self.assertEqual(bs.deploy_dir({"DEPLOY_DIR": "/abs/path/x"}),
                             Path("/abs/path/x"))
            self.assertEqual(bs.deploy_dir({"DEPLOY_DIR": "rel/code"}),
                             fake_root / "rel/code")

class TestPrivateUpdateCfg(_Base):
    """update.cfg 属于私有库数据：真实值进私有库、启动器只拷、改了自动生效。"""

    def _bootstrap(self, root=None):
        import importlib.util
        bs_path = (Path(__file__).resolve().parent.parent
                   / "dxx_private_data" / "bootstrap.py")
        spec = importlib.util.spec_from_file_location("bs_cfg_test", str(bs_path))
        bs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bs)
        if root is not None:
            bs.DATA_CFG_FILE = root / "update.cfg"
            bs.CONFIG_FILE = root / "bootstrap.cfg"
        return bs

    def test_private_repo_has_real_update_cfg_tracked(self):
        """私有库根目录的 update.cfg 是真实数据，四个地址一处权威、无密钥。"""
        import re
        root = Path(__file__).resolve().parent.parent / "dxx_private_data"
        f = root / "update.cfg"
        self.assertTrue(f.exists(), "私有库根目录必须有真实 update.cfg")
        text = f.read_text(encoding="utf-8")
        for key in ("UPDATER_CODE_REPO", "UPDATER_CODE_REPO_BACKUP",
                    "UPDATER_DATA_REPO", "UPDATER_DATA_REPO_BACKUP"):
            self.assertIn(key + " = ", text)
        self.assertIn("UPDATER_BACKEND = git", text)
        self.assertFalse(re.search(r"TOKEN|SECRET|github_pat_|ghp_", text, re.I),
                         "配置里不能有任何凭证")
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertFalse(
            any(line.strip() == "update.cfg" for line in ignored.splitlines()),
            "真实配置必须允许进私有库 git，不能被忽略")

    def test_bootstrap_cfg_only_has_local_items(self):
        """bootstrap.cfg 不再重复仓库地址，只留本机部署项。"""
        root = Path(__file__).resolve().parent.parent / "dxx_private_data"
        text = (root / "bootstrap.cfg.example").read_text(encoding="utf-8")
        for key in ("PUBLIC_REPO", "DATA_REPO", "GITEE_REPO"):
            self.assertNotIn(key + " =", text, "地址只能有一处权威")
        self.assertIn("DEPLOY_DIR", text)

    def test_load_cfg_reads_addresses_from_update_cfg_without_bootstrap_cfg(self):
        """地址只认 update.cfg；bootstrap.cfg 整个不存在也能启动（手机零手填）。"""
        bs = self._bootstrap(self.root)
        (self.root / "update.cfg").write_text(
            "UPDATER_CODE_REPO = https://example.com/o/pub.git\n"
            "UPDATER_CODE_REPO_BACKUP = git@example.com:o/pub.git\n"
            "UPDATER_CODE_BRANCH = dev\n"
            "UPDATER_DATA_REPO = https://example.com/o/priv.git\n"
            "UPDATER_DATA_REPO_BACKUP = git@example.com:o/priv.git\n"
            "UPDATER_GIT_PATH = services\n", encoding="utf-8")
        with mock.patch.object(bs, "log"):
            cfg = bs.load_cfg()
        self.assertIsInstance(cfg, dict)
        self.assertEqual(cfg["PUBLIC_REPO"], "https://example.com/o/pub.git")
        self.assertEqual(cfg["PUBLIC_BRANCH"], "dev")
        self.assertEqual(cfg["DATA_REPO"], "https://example.com/o/priv.git")
        self.assertEqual(cfg["DEPLOY_DIR"], "", "本机项缺省为空")

    def test_load_cfg_ignores_addresses_written_in_bootstrap_cfg(self):
        """就算 bootstrap.cfg 里还残留旧地址，也不得覆盖唯一权威。"""
        bs = self._bootstrap(self.root)
        (self.root / "update.cfg").write_text(
            "UPDATER_CODE_REPO = https://example.com/o/pub.git\n",
            encoding="utf-8")
        (self.root / "bootstrap.cfg").write_text(
            "PUBLIC_REPO = https://evil.example.com/wrong.git\n"
            "DEPLOY_DIR = ../my-public\n", encoding="utf-8")
        with mock.patch.object(bs, "log"):
            cfg = bs.load_cfg()
        self.assertEqual(cfg["PUBLIC_REPO"], "https://example.com/o/pub.git")
        self.assertEqual(cfg["DEPLOY_DIR"], "../my-public")

    def test_load_cfg_missing_update_cfg_fails_loudly(self):
        bs = self._bootstrap(self.root)
        with mock.patch.object(bs, "log"):
            rc = bs.load_cfg()
        self.assertEqual(rc, 1, "缺真实配置必须拦下，不能继续 execv")

    def test_copy_update_cfg_is_byte_identical_not_generated(self):
        """启动器只原样拷贝，不再拼字符串生成。"""
        bs = self._bootstrap(self.root)
        src = self.root / "update.cfg"
        src.write_bytes(b"UPDATER_BACKEND = git\r\nKEEP = \xe4\xb8\xad\xe6\x96\x87\r\n")
        dest = self.root / "public"
        dest.mkdir()
        with mock.patch.object(bs, "log"):
            rc = bs.copy_update_cfg(dest)
        self.assertIs(rc, True)
        self.assertEqual((dest / "update.cfg").read_bytes(), src.read_bytes(),
                         "必须逐字节一致，不能改写 / 补字段")
        self.assertFalse(hasattr(bs, "write_update_cfg"),
                         "生成函数应已删除")

    def test_read_root_file_uses_copied_sha_without_extra_fetch(self):
        """读根目录配置复用本轮拷贝结果，只发 git show，不额外 fetch。"""
        calls = []

        def fake(args, cwd=None, timeout=60, env=None, strip=True):
            calls.append(list(args))
            return (0, "UPDATER_BACKEND = git\n", "")

        self._git_cmd_patch.stop()
        try:
            with mock.patch.object(updater, "git_cmd", side_effect=fake):
                raw = updater.read_root_file(
                    {"sha": "AAA111"}, updater.UPDATE_CFG_NAME)
        finally:
            self._git_cmd_patch.start()
        self.assertEqual(raw, "UPDATER_BACKEND = git\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "show")
        self.assertIn("AAA111:update.cfg", calls[0])
        self.assertNotIn("fetch", [c[0] for c in calls])

    def test_sync_update_cfg_reloads_only_on_real_change(self):
        """改了私有库 update.cfg 才重启；首次观测 / 无变化 / 读不到都不误重启。"""
        cfg_file = self.root / "update.cfg"
        cfg_file.write_text("UPDATER_BACKEND = git\n", encoding="utf-8")
        base = {"sha": "AAA111"}
        with mock.patch.object(updater, "read_root_file",
                               return_value="UPDATER_BACKEND = git\r\n"), \
             mock.patch.object(run, "restart_self") as rs:
            h = run.sync_update_cfg(base, None, {})
            self.assertEqual(rs.call_count, 0, "首次观测不能空重启")
            self.assertEqual(run.sync_update_cfg(base, h, {}), h,
                             "内容没变不能重复重启")
        with mock.patch.object(updater, "read_root_file",
                               return_value="UPDATER_BACKEND = local\n"), \
             mock.patch.object(run, "restart_self") as rs:
            h2 = run.sync_update_cfg(base, h, {})
            self.assertEqual(rs.call_count, 1, "真变更必须重启重新装配")
            self.assertNotEqual(h2, h)
        self.assertEqual(cfg_file.read_text(encoding="utf-8"),
                         "UPDATER_BACKEND = local\n",
                         "新版配置必须落到本机")
        with mock.patch.object(updater, "read_root_file", return_value=None), \
             mock.patch.object(run, "restart_self") as rs:
            self.assertEqual(run.sync_update_cfg(base, h2, {}), h2)
            self.assertEqual(rs.call_count, 0, "读不到就保持现状")


class TestPlatformCompat(_Base):
    """手机 FUSE 不支持 flock、Windows 控制台 Ctrl+C 与 GBK 解码的回归。"""

    def _load_bootstrap(self, root=None):
        import importlib.util
        bs_path = (Path(__file__).resolve().parent.parent
                   / "dxx_private_data" / "bootstrap.py")
        spec = importlib.util.spec_from_file_location("bs_platform_test", str(bs_path))
        bs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bs)
        if root is not None:
            bs.DATA_CFG_FILE = root / "update.cfg"
            bs.CONFIG_FILE = root / "bootstrap.cfg"
        return bs

    @staticmethod
    def _fake_fcntl(errno=38):
        import types
        m = types.ModuleType("fcntl")
        m.LOCK_EX = 2
        m.LOCK_NB = 4
        m.flock = mock.Mock(side_effect=OSError(errno, "Function not implemented"))
        return m

    def test_pace_lock_unsupported_degrades(self):
        """手机共享存储 flock 抛 ENOSYS：闸门锁降级为无锁，不崩且记异常日志。"""
        import tempfile as _tf
        with _tf.TemporaryFile() as f, \
             mock.patch.dict(sys.modules, {"fcntl": self._fake_fcntl()}), \
             mock.patch.object(updater.os, "name", "posix"):
            self.assertFalse(updater._hold_pace_lock(f))
        self.assertIn("文件系统不支持文件锁",
                      (self.state / "updater_error.log").read_text(encoding="utf-8"))

    def test_acquire_lock_unsupported_degrades(self):
        """单实例锁同样降级：手机不能因为文件锁不支持就拒绝启动。"""
        run.release_lock()
        with mock.patch.dict(sys.modules, {"fcntl": self._fake_fcntl()}), \
             mock.patch.object(updater.os, "name", "posix"):
            self.assertTrue(run.acquire_lock())
        self.assertIn("单实例锁降级为无锁",
                      (self.state / "updater_error.log").read_text(encoding="utf-8"))
        self.assertIsNone(run._LOCK_HANDLE, "降级时不能假装持有锁")

    def test_lock_dir_shared_by_bootstrap_and_updater(self):
        """启动器与长期运行用同一处锁目录，跨进程互斥才成立。"""
        root = Path(__file__).resolve().parent.parent / "dxx_private_data"
        bs_text = (root / "bootstrap.py").read_text(encoding="utf-8")
        up_text = (Path(__file__).resolve().parent / "updater.py").read_text(
            encoding="utf-8")
        needle = 'LOCK_DIR = Path.home() / ".dxx-updater"'
        self.assertIn(needle, bs_text, "启动器必须与公共库写死同一处锁目录")
        self.assertIn(needle, up_text)
        self.assertEqual(updater.LOCK_FILE.parent, updater.LOCK_DIR)
        self.assertEqual(updater._pace_lock_path().parent, updater.LOCK_DIR)

    def test_git_cmd_forces_utf8_decoding(self):
        """git 输出显式按 UTF-8 解码并容错，不再吃 Windows 默认 GBK。"""
        import types
        self._git_cmd_patch.stop()
        try:
            fake = types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
            with mock.patch.object(updater.subprocess, "run",
                                   return_value=fake) as sr:
                rc, out, _ = updater.git_cmd(["status"])
            self.assertEqual((rc, out), (0, "ok"))
            kw = sr.call_args[1]
            self.assertEqual(kw.get("encoding"), "utf-8")
            self.assertEqual(kw.get("errors"), "replace")
        finally:
            self._git_cmd_patch.start()

    def test_bootstrap_git_net_forces_utf8_decoding(self):
        """启动器的 git 出口同样显式 UTF-8，避免首跑就抛解码异常。"""
        import types
        bs = self._load_bootstrap(self.root)
        fake = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(bs, "LOCK_DIR", self.root / "locks"), \
             mock.patch.object(bs.subprocess, "run", return_value=fake) as sr, \
             mock.patch.object(bs, "log"):
            bs.git_net(["status"], "https://example.com/o/pub.git", self.root)
        kw = sr.call_args[1]
        self.assertEqual(kw.get("encoding"), "utf-8")
        self.assertEqual(kw.get("errors"), "replace")

    def test_bootstrap_windows_starts_run_in_foreground(self):
        """Windows 不再用 execv 另起脱离控制台的进程，Ctrl+C 才传得过去。"""
        root = Path(__file__).resolve().parent.parent / "dxx_private_data"
        text = (root / "bootstrap.py").read_text(encoding="utf-8")
        self.assertIn('if os.name == "nt":', text)
        self.assertIn("return subprocess.call(argv)", text)

    def test_replace_process_windows_spawns_new_run(self):
        """run.py 自重启在 Windows 也走前台拉起，不留在 execv 的旧行为。"""
        with mock.patch.object(run.os, "name", "nt"), \
             mock.patch.object(run.subprocess, "call", return_value=0) as cl, \
             mock.patch.object(run.os, "_exit") as ex:
            run._replace_process(["python", "run.py"])
        cl.assert_called_once_with(["python", "run.py"])
        ex.assert_called_once_with(0)

    def test_derive_alternate_url_both_directions(self):
        self.assertEqual(updater.derive_alternate_url("https://github.com/me/p.git"),
                         "git@github.com:me/p.git")
        self.assertEqual(updater.derive_alternate_url("git@gitee.com:me/p.git"),
                         "https://gitee.com/me/p.git")
        self.assertEqual(
            updater.derive_alternate_url("ssh://git@github.com/me/p.git"),
            "https://github.com/me/p.git")
        self.assertEqual(
            updater.derive_alternate_url("https://gitee.com/a/b/c.git"),
            "git@gitee.com:a/b/c.git")
        self.assertEqual(updater.derive_alternate_url("/local/path.git"), "")
        self.assertEqual(updater.derive_alternate_url("me/p.git"), "")
        # 往返：HTTPS→SSH→HTTPS 还原
        https = "https://github.com/me/p.git"
        self.assertEqual(updater.derive_alternate_url(
            updater.derive_alternate_url(https)), https)

    def test_bootstrap_derive_alternate_url_both_directions(self):
        bs = self._load_bootstrap(self.root)
        self.assertEqual(bs.derive_alternate_url("https://github.com/me/p.git"),
                         "git@github.com:me/p.git")
        self.assertEqual(bs.derive_alternate_url("git@gitee.com:me/p.git"),
                         "https://gitee.com/me/p.git")
        self.assertEqual(bs.derive_alternate_url("ssh://git@github.com/me/p.git"),
                         "https://github.com/me/p.git")
        self.assertEqual(bs.derive_alternate_url("/local/path.git"), "")
        self.assertEqual(bs.derive_alternate_url("me/p.git"), "")

    def test_bootstrap_clone_tries_alternate_protocol_when_primary_fails(self):
        import types
        bs = self._load_bootstrap(self.root)
        dest = self.root / "public"
        cfg = dict(bs.DEFAULTS)
        cfg["PUBLIC_REPO"] = "https://github.com/mirror/public.git"
        cfg["PUBLIC_REPO_BACKUP"] = "git@gitee.com:mirror/public.git"
        cfg["PUBLIC_BRANCH"] = "main"
        tried = []
        def fake(args, url, dest_dir, cwd=None, timeout=180):
            tried.append(url)
            if url == "git@github.com:mirror/public.git":
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="https down")
        with mock.patch.object(bs, "git_net", side_effect=fake), \
             mock.patch.object(bs, "log"):
            rc = bs.clone_or_pull(cfg, dest)
        self.assertTrue(rc)
        self.assertIn("git@github.com:mirror/public.git", tried,
                      "主库 HTTPS 克隆失败应改试 SSH 备选")


if __name__ == "__main__":
    unittest.main(verbosity=2)
