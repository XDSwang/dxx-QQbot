#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_official.broadcast_to_groups 的单元测试（mock token + 发消息通道，不联网）。"""
import os
import sys
import time
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import qq_official


class _GoodToken:
    def get_token(self, force=False):
        return "FAKE_TOKEN"


class _BadToken:
    def get_token(self, force=False):
        raise RuntimeError("no token")


def _patch(token_obj, send_results):
    """替换 TokenManager 与 send_message；send_results 是逐群 (ok, info) 列表。"""
    qq_official.TokenManager = lambda appid, secret, api_base: token_obj
    calls = []

    def fake_send(token, appid, api_base, target_kind, target_id, content, msg_id=None):
        calls.append((target_kind, target_id, content))
        if send_results:
            return send_results.pop(0)
        return True, None

    qq_official.send_message = fake_send
    return calls


class TestBroadcast(unittest.TestCase):
    def test_empty_groups_returns_zero(self):
        calls = _patch(_GoodToken(), [])
        sent, failed = qq_official.broadcast_to_groups(
            "a", "s", "https://x", [], "hi")
        self.assertEqual((sent, failed), (0, 0))
        self.assertEqual(calls, [])

    def test_all_ok_counts_and_content(self):
        calls = _patch(_GoodToken(), [(True, None), (True, None), (True, None)])
        sent, failed = qq_official.broadcast_to_groups(
            "a", "s", "https://x", ["g1", "g2", "g3"], "即将更新，暂停服务")
        self.assertEqual((sent, failed), (3, 0))
        self.assertEqual([c[1] for c in calls], ["g1", "g2", "g3"])
        self.assertEqual(calls[0][2], "即将更新，暂停服务")
        self.assertEqual(calls[0][0], "group")

    def test_partial_failure_counted(self):
        calls = _patch(_GoodToken(),
                       [(True, None), (False, "40034105"), (True, None)])
        sent, failed = qq_official.broadcast_to_groups(
            "a", "s", "https://x", ["g1", "g2", "g3"], "更新完成，开始服务")
        self.assertEqual((sent, failed), (2, 1))
        self.assertEqual(len(calls), 3)

    def test_token_failure_returns_all_failed(self):
        calls = _patch(_BadToken(), [])
        sent, failed = qq_official.broadcast_to_groups(
            "a", "s", "https://x", ["g1", "g2"], "hi")
        self.assertEqual((sent, failed), (0, 2))
        self.assertEqual(calls, [])


class TestReplyAtTag(unittest.TestCase):
    """回复 @ 回去：只在「群里 + 这条消息 @ 了机器人」时给标签。"""

    def test_no_tag_when_not_at_me(self):
        self.assertEqual(qq_official.reply_at_tag(
            {"channel": "group", "member_openid": "M1"}, "group"), "",
            "没 @ 机器人就不该 @ 回去")

    def test_no_tag_in_private(self):
        self.assertEqual(qq_official.reply_at_tag(
            {"at_me": True, "member_openid": "M1"}, "user"), "",
            "私聊不加 @ 标签")

    def test_tag_uses_member_openid(self):
        self.assertEqual(qq_official.reply_at_tag(
            {"at_me": True, "member_openid": "M1", "user_id": "U1"}, "group"),
            '<qqbot-at-user id="M1" />')

    def test_fallback_to_user_id(self):
        # 真实事件里 member_openid 偶发为空：回退 user_id，否则「@ 了机器人却不 @ 我」
        self.assertEqual(qq_official.reply_at_tag(
            {"at_me": True, "member_openid": "", "user_id": "U1"}, "group"),
            '<qqbot-at-user id="U1" />')

    def test_empty_when_no_ids(self):
        self.assertEqual(qq_official.reply_at_tag({"at_me": True}, "group"), "")


class TestMentionPlaceholder(unittest.TestCase):
    """@ 占位符四种形态：能剥干净，且只有真正 @机器人 的才点亮 at_me。"""

    def test_strip_all_forms(self):
        for raw in ("<@openid> 平原", "<@all> 平原", "<@!botid> 平原",
                    "<@//9700b570215f656ce30231ee174ed3c1/> 平原"):
            self.assertEqual(qq_official.strip_mention(raw), "平原",
                             "%r 没被剥干净" % raw)

    def test_bot_mention_forms(self):
        for raw in ("<@!botid> 平原",
                    "<@//9700b570215f656ce30231ee174ed3c1/> 平原"):
            self.assertTrue(qq_official._BOT_MENTION_RE.search(raw),
                            "%r 应判为 @机器人" % raw)

    def test_member_mention_not_bot(self):
        for raw in ("<@memberid> 平原", "<@all> 平原"):
            self.assertFalse(qq_official._BOT_MENTION_RE.search(raw),
                             "%r 不该判成 @机器人" % raw)


class TestTraceChain(unittest.TestCase):
    """链路三层记录：单独文件、逐条落盘、完整内容原样不截断。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.tmp.name, "selfcheck_trace.log")
        self.patcher = patch.object(qq_official, "TRACE_LOG_PATH", self.log_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_three_stages_in_order_and_raw_untouched(self):
        url = "https://cdn.example.com/card.png?v=123"
        mk = "![img #618px #900px](%s)\n\n🔧 自检三层：①收到 ②组装 ③已发送" % url
        self.assertTrue(qq_official.trace_chain(
            "收到", "昵称=小明", raw="<@//9700b570/> 平原"))
        self.assertTrue(qq_official.trace_chain(
            "组装", "准备发送 @小明", raw="@小明 平原"))
        self.assertTrue(qq_official.trace_chain(
            "已发送", "方式=Markdown(msg_type=2)", raw=mk))
        with open(self.log_path, encoding="utf-8") as f:
            text = f.read()
        pos = [text.index("| %s |" % s) for s in ("收到", "组装", "已发送")]
        self.assertEqual(pos, sorted(pos), "三层顺序必须是收到→组装→已发送")
        self.assertIn("![img #618px #900px](%s)" % url, text)
        self.assertNotIn("\\n", text, "完整内容不能被转义/处理过")
        self.assertIn("---- 完整内容（代码层原样，未渲染）----", text)

    def test_unknown_stage_does_not_write(self):
        self.assertFalse(qq_official.trace_chain("未知", "x"))
        self.assertFalse(os.path.exists(self.log_path))

    def test_mk_raw_keeps_native_content_and_template_json(self):
        self.assertEqual(qq_official._mk_raw({"content": "![img](u)"}), "![img](u)")
        md = {"custom_template_id": "T1",
              "params": [{"key": "image", "values": ["https://x/y.png"]}]}
        self.assertIn("https://x/y.png", qq_official._mk_raw(md))


class TestReplyChain(unittest.TestCase):
    """真实回复链路：收到 → 组装 → 已发送，第 3 层是完整 mk 原文。"""

    def test_markdown_reply_writes_all_three_layers(self):
        url = "https://cdn.example.com/card.png?v=123"
        mk = {"content": "![img #618px #900px](%s)\n\n🔧 自检报告" % url}
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "selfcheck_trace.log")
            with patch.object(qq_official, "TRACE_LOG_PATH", log_path), \
                    patch.object(qq_official.qq_markdown, "enabled", return_value=True), \
                    patch.object(qq_official.qq_markdown, "send_reply") as sr:
                def fake_send_reply(*a, **kw):
                    cb = kw.get("on_built")
                    if cb:
                        cb("native", mk)
                    return True, {}, "native"

                sr.side_effect = fake_send_reply
                bot = qq_official.OfficialBot(
                    "APP", "SEC", lambda t, c: "🔧 机器人自检")
                ctx = {"channel": "group", "group_id": "G1", "raw": "dxx 自检",
                       "nickname": "小明", "event": "GROUP_AT_MESSAGE_CREATE",
                       "at_me": True, "member_openid": "M1"}
                bot._dispatch_reply("TOK", "group", "G1", "自检", ctx, "MSG1")
            with open(log_path, encoding="utf-8") as f:
                text = f.read()
            for stage in ("收到", "组装", "已发送"):
                self.assertIn("| %s |" % stage, text)
            self.assertIn("[自检] ", text, "自检消息要打标")
            self.assertIn("昵称=小明", text)
            self.assertIn("方式=Markdown(msg_type=2,native)", text)
            self.assertIn(mk["content"], text, "第 3 层必须是完整 mk 原文")


class TestGatewayHealth(unittest.TestCase):
    """网关会话健康：连续无帧/ACK 判假死、会话到期软重连、重连清掉旧心跳链。

    现场现象：进程活着、主动广播正常，但一下午收不到任何群消息。心跳 ACK
    会不断刷新 sock 读超时，服务端「只回 ACK、不推事件」时连接永久假死。
    """

    def test_stale_after_two_heartbeat_intervals(self):
        # interval=45s、factor=2 → 90s 内没有任何帧/ACK 才判假死
        self.assertFalse(qq_official.gateway_is_stale(100.0, 100.0, 189.0, 45.0, 2))
        self.assertTrue(qq_official.gateway_is_stale(100.0, 100.0, 191.0, 45.0, 2))

    def test_recent_frame_clears_stale(self):
        # 服务端还在发任意帧（含 op11 ACK）就不该误判断线
        self.assertFalse(qq_official.gateway_is_stale(200.0, 100.0, 205.0, 45.0, 2))

    def test_no_timestamp_yet_is_not_stale(self):
        self.assertFalse(qq_official.gateway_is_stale(0.0, 0.0, 999.0, 45.0, 2))

    def test_session_expired_and_switchable(self):
        self.assertFalse(qq_official.session_expired(100.0, 699.0, 600))
        self.assertTrue(qq_official.session_expired(100.0, 701.0, 600))
        self.assertFalse(qq_official.session_expired(100.0, 99999.0, 0), "0=关闭软重连")
        self.assertFalse(qq_official.session_expired(0.0, 99999.0, 600), "未开始不算")

    def test_cancel_heartbeat_stops_old_chain(self):
        bot = qq_official.OfficialBot("APP", "SEC", lambda t, c: "")

        class _Timer:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        t = _Timer()
        bot._hb_timer = t
        gen = bot._hb_generation
        bot._cancel_heartbeat()
        self.assertTrue(t.cancelled, "旧 Timer 要被 cancel")
        self.assertEqual(bot._hb_generation, gen + 1, "代次递增，旧链下次触发自行退出")
        self.assertIsNone(bot._hb_timer)

    def test_gateway_and_silent_stages_writable(self):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "selfcheck_trace.log")
            with patch.object(qq_official, "TRACE_LOG_PATH", log_path):
                self.assertTrue(qq_official.trace_chain("连接", "进程已启动"))
                self.assertTrue(qq_official.trace_chain("静默", "白名单外"))
            with open(log_path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("| 连接 | 进程已启动", text)
            self.assertIn("| 静默 | 白名单外", text)

    def test_stale_watchdog_closes_conn_and_stops_chain(self):
        bot = qq_official.OfficialBot("APP", "SEC", lambda t, c: "")

        class _Conn:
            def __init__(self):
                self.closed = False

            def send_text(self, text):
                pass

            def close(self):
                self.closed = True

        conn = _Conn()
        with patch.object(qq_official.threading, "Timer") as timer:
            bot._start_heartbeat(conn, 45.0)
            hb = timer.call_args[0][1]
            old = time.time() - 200
            bot._last_frame_at = old
            bot._last_ack_at = old
            with patch.object(qq_official, "flog"), \
                    patch.object(qq_official, "trace_chain", return_value=True):
                hb()
        self.assertTrue(conn.closed, "判假死后必须主动关掉 conn 触发重连")

    def test_heartbeat_thread_ends_expired_session_even_if_loop_stuck(self):
        bot = qq_official.OfficialBot("APP", "SEC", lambda t, c: "")

        class _Conn:
            def __init__(self):
                self.closed = False
                self.sent = []

            def send_text(self, text):
                self.sent.append(text)

            def close(self):
                self.closed = True

        conn = _Conn()
        with patch.object(qq_official.threading, "Timer") as timer:
            bot._start_heartbeat(conn, 45.0)
            hb = timer.call_args[0][1]
            now = time.time()
            bot._session_max = 600.0
            bot._session_started_at = now - 700.0
            # ACK/帧都新鲜：看门狗不触发，只有「会话到期」该触发
            bot._last_frame_at = now
            bot._last_ack_at = now
            with patch.object(qq_official, "flog"), \
                    patch.object(qq_official, "trace_chain", return_value=True):
                hb()
        self.assertTrue(conn.closed, "读循环卡住时心跳线程也要能强制重建会话")
        self.assertEqual(conn.sent, [], "到期直接断连，不再往旧连接发心跳")

    def test_session_expiry_ends_loop_on_ack(self):
        bot = qq_official.OfficialBot("APP", "SEC", lambda t, c: "")
        bot._session_max = 600.0
        bot._session_started_at = time.time() - 700.0

        class _Conn:
            def __init__(self):
                self.frames = [b'{"op": 11}']

            def recv_message(self):
                if not self.frames:
                    raise AssertionError("会话到期后不该继续读下一帧")
                return ("text", self.frames.pop(0))

            def send_close(self):
                pass

        # 服务端「只回 ACK、不推事件」：靠 ACK 触发软重连，_loop 正常返回
        with patch.object(qq_official, "flog"), \
                patch.object(qq_official, "trace_chain", return_value=True):
            bot._loop(_Conn(), "TOK")


class TestFlowLogRotation(unittest.TestCase):
    """按天归档 + 保留天数自动清理（全在临时目录，不碰真实 data/）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "bot_flow.log")
        for name, value in (("LOG_DIR", self.dir), ("_LOG_PATH", self.path),
                            ("_flow_log_day", None)):
            p = patch.object(qq_official, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.today = time.strftime("%Y-%m-%d")
        self.noon = time.mktime(
            time.strptime(self.today + " 12:00:00", "%Y-%m-%d %H:%M:%S"))

    def _write(self, name, text=""):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self, name="bot_flow.log"):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def test_archive_name_roundtrip_and_foreign_files(self):
        name = qq_official.flow_log_archive_name("2026-11-09")
        self.assertEqual(name, "bot_flow-2026-11-09.log")
        self.assertEqual(qq_official.flow_log_archived_day(name), "2026-11-09")
        for other in ("bot_flow.log", "groups.txt", "bot_flow-bad.log",
                      "bot_flow-2026-13-01.log", "bot_flow-2026-1-09.log",
                      "other-2026-11-09.log", "bot_flow-2026-11-09.txt"):
            self.assertEqual(qq_official.flow_log_archived_day(other), "")

    def test_keep_days_values(self):
        for raw, expected in (("7", 7), ("3", 3), ("1", 1), ("0", 0),
                              ("abc", 7), ("-1", 7)):
            with patch.dict(os.environ, {"WM_FLOW_LOG_DAYS": raw}):
                self.assertEqual(qq_official.flow_log_keep_days(), expected, raw)

    def test_cutoff_and_prune_only_expired_archives(self):
        self.assertEqual(qq_official.flow_log_cutoff("2026-11-09", 7), "2026-11-03")
        self.assertEqual(qq_official.flow_log_cutoff("2026-11-09", 1), "2026-11-09")
        days = ("2026-11-01", "2026-11-02", "2026-11-06",
                "2026-11-07", "2026-11-08")
        for day in days:
            self._write(qq_official.flow_log_archive_name(day))
        for name in ("bot_flow.log", "groups.txt", "bot_flow-bad.log"):
            self._write(name)
        removed = qq_official.flow_log_prune(self.dir, "2026-11-09", 3)
        self.assertEqual(removed, [
            "bot_flow-2026-11-01.log", "bot_flow-2026-11-02.log",
            "bot_flow-2026-11-06.log"])
        for name in ("bot_flow.log", "groups.txt", "bot_flow-bad.log",
                     "bot_flow-2026-11-07.log", "bot_flow-2026-11-08.log"):
            self.assertTrue(os.path.exists(os.path.join(self.dir, name)), name)
        self.assertEqual(qq_official.flow_log_prune(self.dir, "2026-11-09", 0), [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "bot_flow.log")))

    def test_same_day_restart_appends_instead_of_truncating(self):
        with patch.dict(os.environ, {"WM_FLOW_LOG_DAYS": "7"}):
            qq_official.flog("上午的记录", now=self.noon)
            qq_official._flow_log_day = None       # 模拟当天重启
            qq_official.flog("下午的记录", now=self.noon + 60)
        text = self._read()
        self.assertIn("上午的记录", text)
        self.assertIn("下午的记录", text)
        self.assertEqual(len(text.strip().splitlines()), 2)

    def test_cross_day_archives_and_prunes(self):
        yesterday = time.strftime(
            "%Y-%m-%d", time.localtime(self.noon - 86400))
        self._write("bot_flow.log", "昨天的记录\n")
        os.utime(self.path, (self.noon - 86400, self.noon - 86400))
        self._write(qq_official.flow_log_archive_name(
            time.strftime("%Y-%m-%d", time.localtime(self.noon - 10 * 86400))))
        with patch.dict(os.environ, {"WM_FLOW_LOG_DAYS": "3"}):
            qq_official.flog("今天的记录", now=self.noon)
        archive = qq_official.flow_log_archive_name(yesterday)
        self.assertIn("昨天的记录", self._read(archive))
        current = self._read()
        self.assertIn("今天的记录", current)
        self.assertNotIn("昨天的记录", current)
        self.assertFalse(os.path.exists(os.path.join(
            self.dir, qq_official.flow_log_archive_name(
                time.strftime("%Y-%m-%d", time.localtime(self.noon - 10 * 86400))))))

    def test_existing_archive_is_appended_not_overwritten(self):
        self._write("bot_flow.log", "新的一段\n")
        archive = os.path.join(self.dir,
                               qq_official.flow_log_archive_name("2026-11-08"))
        self._write(os.path.basename(archive), "旧的一段\n")
        qq_official._flow_log_archive(self.path, archive)
        text = self._read(os.path.basename(archive))
        self.assertIn("旧的一段", text)
        self.assertIn("新的一段", text)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
