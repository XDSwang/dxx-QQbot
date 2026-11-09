#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_official.broadcast_to_groups 的单元测试（mock token + 发消息通道，不联网）。"""
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
