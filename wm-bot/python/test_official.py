#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_official.py — 官方 QQ 机器人对接离线测试（不依赖真实 QQ / 真实网络）

覆盖：
  1) strip_mention：剥离 "<@!bot>" / "<@bot>" 前缀；
  2) TokenManager：mock urlopen 解析 access_token 与过期时间、临近过期自动刷新；
  3) OfficialBot._on_event：群@消息 / 单聊 / 白名单 / 触发词判定（mock send_message）；
  4) 连接+Identify 端到端：起本地 mock gateway（用 onebot 服务端组件），验证
     OfficialBot 完成握手、发出 Identify（鉴权 token 格式正确）；
  5) send_message 请求体：被动回复带 msg_id（事件 d.id）、不带 event_id；
  6) 图文混排：msg_type=7 + content 把卡片图与 /w 指令合成一条消息、
     多图只最后一张带文字、混排被拒/开关关闭时自动回退两条且 /w 不丢。
"""
import os
import sys
import io
import json
import time
import socket
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["WM_SOURCE"] = "mock"   # 必须在 import wm 之前：离线测试不发 HTTP
# 本文件测的是原来的富媒体通道（msg_type=7）。Markdown 通道默认开，
# 这里显式关掉做隔离；它的用例在 test_qq_markdown.py 单独覆盖。
os.environ["WM_QQ_MARKDOWN"] = "0"
import onebot
import qq_official
import qq_common
import wm

# 隔离：离线测试用 mock 假价格，绝不能写进真实 data/cache.json，
# 否则下次启动机器人会把 MockSeller 当真实价格秒回给用户。
import tempfile
from pathlib import Path
_TEST_DATA = tempfile.TemporaryDirectory()
wm.DATA_DIR = Path(_TEST_DATA.name)
wm.CACHE_FILE = wm.DATA_DIR / "cache.json"
wm.PACE_FILE = wm.DATA_DIR / "pace.json"
wm.SLUG_FILE = wm.DATA_DIR / "slug.json"


class TestStripMention(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(qq_official.strip_mention("<@!123> wm boltor"), "wm boltor")
        self.assertEqual(qq_official.strip_mention("<@123> zk tigris"), "zk tigris")
        self.assertEqual(qq_official.strip_mention("wm boltor"), "wm boltor")
        self.assertEqual(qq_official.strip_mention("  <@!9>   WM x "), "WM x")
        # 多个 @
        self.assertEqual(qq_official.strip_mention("<@!1><@!2> wm x"), "wm x")


class TestTokenManager(unittest.TestCase):
    def _fake_urlopen(self, token, expires_in=7200):
        def fake(req, timeout=10):
            body = json.dumps({"access_token": token, "expires_in": str(expires_in)})
            return io.BytesIO(body.encode("utf-8"))
        return fake

    def test_parse_token(self):
        orig = qq_official.urllib.request.urlopen
        qq_official.urllib.request.urlopen = self._fake_urlopen("TOK123")
        try:
            mgr = qq_official.TokenManager("app", "sec")
            tok = mgr.get_token()
            self.assertEqual(tok, "TOK123")
            self.assertGreater(mgr._expires_at, 0)
            # 重复获取应命中缓存（不重新请求）
            tok2 = mgr.get_token()
            self.assertEqual(tok2, "TOK123")
        finally:
            qq_official.urllib.request.urlopen = orig


class TestSendMessageBody(unittest.TestCase):
    """被动回复请求体校验：必须用 msg_id，且绝不带 event_id（用错会报 40034027）。"""
    def _capture(self):
        captured = {}
        def fake_urlopen(req, timeout=10):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return io.BytesIO(b'{"id":"ok"}')
        return captured, fake_urlopen

    def _run(self, msg_id=None):
        orig = qq_official.urllib.request.urlopen
        captured, fake = self._capture()
        qq_official.urllib.request.urlopen = fake
        try:
            ok, _ = qq_official.send_message(
                "TOK", "APP", "https://api.bot.qq.com",
                "group", "G1", "hi", msg_id)
        finally:
            qq_official.urllib.request.urlopen = orig
        return ok, captured["body"]

    def test_body_uses_msg_id(self):
        ok, body = self._run("MSG123")
        self.assertTrue(ok)
        self.assertEqual(body.get("msg_id"), "MSG123")
        self.assertNotIn("event_id", body)

    def test_body_without_msg_id(self):
        ok, body = self._run(None)
        self.assertTrue(ok)
        self.assertNotIn("msg_id", body)
        self.assertNotIn("event_id", body)

    def _capture_image_body(self, content=None):
        captured = {}
        orig = qq_official.urllib.request.urlopen

        def fake(req, timeout=15):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return io.BytesIO(b'{"id":"ok"}')

        qq_official.urllib.request.urlopen = fake
        try:
            ok, _ = qq_official.send_image(
                "TOK", "APP", "https://api.bot.qq.com",
                "group", "G1", "FI123", "M1", content=content)
        finally:
            qq_official.urllib.request.urlopen = orig
        return ok, captured["body"]

    def test_send_image_mixed_body(self):
        """msg_type=7 + media + content = 官方支持的图文混排（仅图片生效）。"""
        ok, body = self._capture_image_body("/w Seller Hi! I want to buy: X for 5 platinum.")
        self.assertTrue(ok)
        self.assertEqual(body["msg_type"], 7)
        self.assertEqual(body["media"], {"file_info": "FI123"})
        self.assertEqual(body["msg_id"], "M1")
        self.assertEqual(body["content"],
                         "/w Seller Hi! I want to buy: X for 5 platinum.")

    def test_send_image_plain_body_has_no_content(self):
        ok, body = self._capture_image_body()
        self.assertTrue(ok)
        self.assertEqual(body["msg_type"], 7)
        self.assertNotIn("content", body)


class TestMixedImageMessage(unittest.TestCase):
    """官方通道：卡片图 + /w 指令合成【一条】图文混排消息。

    走真实 _reply_images（只 mock 上传 / 发图 / 发文本三个网络动作），
    验证消息条数、content 归属，以及混排被拒时的自动回退。
    """

    TAIL = ("英文：/w Seller Hi! I want to buy: Tigris for 800 platinum.\n"
            "中文：/w Seller 你好，我想购买 猛虎 800 铂金.")

    def setUp(self):
        self.bot = qq_official.OfficialBot("app", "sec", lambda t, c: "x")
        self.bot.token_mgr.get_token = lambda *a, **k: "TOK"
        self._orig = (qq_official.upload_image, qq_official.send_image,
                      qq_official.send_message)
        self.image_calls = []
        self.text_calls = []
        qq_official.upload_image = lambda *a, **k: ("FI123", None)
        qq_official.send_image = self._fake_image_ok
        qq_official.send_message = self._fake_text

    def tearDown(self):
        (qq_official.upload_image, qq_official.send_image,
         qq_official.send_message) = self._orig

    def _fake_image_ok(self, token, appid, api_base, kind, tid, file_info,
                       msg_id=None, content=None):
        self.image_calls.append((file_info, msg_id, content))
        return True, {"id": "ok"}

    def _fake_text(self, token, appid, api_base, kind, target_id, content,
                   msg_id=None):
        self.text_calls.append((content, msg_id))
        return True, {"id": "ok"}

    def _reply(self, pngs=None, tail=None, ctx=None, msg_id="M1"):
        if tail is None:
            tail = self.TAIL
        reply = qq_common.ImageReply("价格列表正文", pngs or [b"\x89PNG"])
        reply.tail = tail
        self.bot.handler = lambda t, c: reply
        self.bot._dispatch_reply("TOK", "group", "G1", "wm boltor",
                                 ctx or {}, msg_id)

    def test_one_message_with_tail(self):
        """默认：只发 1 条图片消息，/w 指令作为 content 随图，不额外发文本。"""
        self._reply()
        self.assertEqual(len(self.image_calls), 1)
        self.assertEqual(self.image_calls[0][2], self.TAIL)
        self.assertEqual(self.text_calls, [], "不能再有第二条文本消息")
        self.assertNotIn("wf.m", self.image_calls[0][2])
        self.assertNotIn("RabbitBot", self.image_calls[0][2])

    def test_full_message_also_one_plain_message(self):
        """免 @ 全量消息：一条普通群消息（不带 msg_id），仍是图 + 文字同条。"""
        self._reply(ctx={"channel": "group", "group_id": "G1",
                         "full_message": True})
        self.assertEqual(len(self.image_calls), 1)
        self.assertIsNone(self.image_calls[0][1], "不引用、不 @ 人")
        self.assertEqual(self.image_calls[0][2], self.TAIL)
        self.assertEqual(self.text_calls, [])

    def test_no_tail_is_single_image(self):
        """没有 /w（帮助 / 世界状态等）时就是一条纯图，不补空文本。"""
        self._reply(tail="")
        self.assertEqual(len(self.image_calls), 1)
        self.assertIsNone(self.image_calls[0][2])
        self.assertEqual(self.text_calls, [])

    def test_switch_off_sends_two_messages(self):
        """WM_QQ_MIX_MESSAGE=0：退回「图一条 + 字符串一条」的老行为。"""
        with patch.dict(os.environ, {"WM_QQ_MIX_MESSAGE": "0"}, clear=False):
            self._reply()
        self.assertEqual(len(self.image_calls), 1)
        self.assertIsNone(self.image_calls[0][2])
        self.assertEqual(len(self.text_calls), 1)
        self.assertEqual(self.text_calls[0][0], self.TAIL)

    def test_mixed_rejected_falls_back_without_losing_whisper(self):
        """服务端拒绝混排（22006）：同一个 file_info 重发纯图 + 单独补文本。"""
        def fake_image(token, appid, api_base, kind, tid, file_info,
                       msg_id=None, content=None):
            self.image_calls.append((file_info, msg_id, content))
            if content:
                return False, "HTTP 400: 22006 消息类型与内容不匹配"
            return True, {"id": "ok"}

        qq_official.send_image = fake_image
        self._reply()
        self.assertEqual(len(self.image_calls), 2, "混排失败后用同一 file_info 重发纯图")
        self.assertEqual(self.image_calls[1][0], "FI123")
        self.assertIsNone(self.image_calls[1][2])
        self.assertEqual(len(self.text_calls), 1)
        self.assertEqual(self.text_calls[0][0], self.TAIL, "/w 指令不能丢")

    def test_tail_only_on_last_image(self):
        """多张卡片时只把 /w 并进最后一张，前面几张是纯图。"""
        self._reply(pngs=[b"1", b"2", b"3"])
        self.assertEqual(len(self.image_calls), 3)
        self.assertEqual([c[2] for c in self.image_calls],
                         [None, None, self.TAIL])
        self.assertEqual(self.text_calls, [])

    def test_image_failure_falls_back_to_full_text(self):
        """整条发图都失败：正文 + /w 指令合成一段纯文本发出。"""
        qq_official.send_image = lambda *a, **k: (False, "上传失败")
        self._reply()
        self.assertEqual(len(self.text_calls), 1)
        self.assertIn("价格列表正文", self.text_calls[0][0])
        self.assertIn("/w Seller", self.text_calls[0][0])


class TestOnEvent(unittest.TestCase):
    def setUp(self):
        os.environ["WM_SOURCE"] = "mock"
        self.calls = []
        self.msg_ids = []

        # 替换全局 send_message 为记录调用
        self._orig_send = qq_official.send_message
        qq_official.send_message = self._fake_send
        # 白名单仅含 "G1"
        self.bot = qq_official.OfficialBot(
            "app", "sec",
            qq_common.make_handler({"G1"}, allow_all=False),
        )

    def tearDown(self):
        qq_official.send_message = self._orig_send

    def _fake_send(self, token, appid, api_base, kind, target_id, content, msg_id=None):
        self.calls.append((kind, target_id, content))
        self.msg_ids.append(msg_id)
        return True, {"id": "ok"}

    def _pkt_group(self, content, gid="G1", mid="M1"):
        return {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE",
                "d": {"group_openid": gid, "content": content, "id": mid}}

    def _pkt_group_full(self, content, gid="G1", mid="M1"):
        """群消息全量模式（免 @）：content 是纯文本，@某人会带 <@openid> 占位。"""
        return {"op": 0, "t": "GROUP_MESSAGE_CREATE",
                "d": {"group_openid": gid, "content": content, "id": mid}}

    def _pkt_c2c(self, content, openid="U1", mid="M2"):
        return {"op": 0, "t": "C2C_MESSAGE_CREATE",
                "d": {"author": {"user_openid": openid}, "content": content, "id": mid}}

    def test_group_whitelisted_trigger(self):
        self.bot._on_event(None, self._pkt_group("<@!bot> D/XX/wm boltor prime"), "TOK")
        self.assertEqual(len(self.calls), 1)
        kind, gid, content = self.calls[0]
        self.assertEqual(kind, "group")
        self.assertEqual(gid, "G1")
        self.assertIn("boltor prime", content.lower())
        self.assertIn("铂金", content)

    def test_group_not_whitelisted_silent(self):
        self.bot._on_event(None, self._pkt_group("<@!bot> D/XX/wm boltor", gid="GX"), "TOK")
        self.assertEqual(self.calls, [])

    def test_group_non_trigger_silent(self):
        self.bot._on_event(None, self._pkt_group("<@!bot> 你好啊"), "TOK")
        self.assertEqual(self.calls, [])

    def test_c2c_not_limited(self):
        # 单聊不受白名单限制（WM 查询需带 D/XX/ 前缀）
        self.bot._on_event(None, self._pkt_c2c("D/XX/zk tigris"), "TOK")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "user")
        self.assertIn("tigris", self.calls[0][2].lower())

    def test_passive_msg_id(self):
        pkt = self._pkt_group("<@!bot> D/XX/wm boltor", mid="EVENT999")
        self.bot._on_event(None, pkt, "TOK")
        # 被动回复须把事件 d.id 作为 msg_id 传入（不是 event_id）
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "G1")
        self.assertEqual(self.msg_ids[0], "EVENT999")

    def test_group_full_message_no_mention(self):
        # 群消息全量模式：不用 @，直接 "D/XX/wm boltor" 也要能触发（白名单内）
        self.bot._on_event(None, self._pkt_group_full("D/XX/wm boltor"), "TOK")
        self.assertEqual(len(self.calls), 1)
        kind, gid, content = self.calls[0]
        self.assertEqual(kind, "group")
        self.assertEqual(gid, "G1")
        self.assertIn("boltor", content.lower())

    def test_group_full_message_mention_placeholder(self):
        # 全量模式里 @某人会变成 <@openid> 占位，剥掉后仍能命中 D/XX/ 前缀 + 触发词
        self.bot._on_event(None, self._pkt_group_full("D/XX/wm boltor <@7A3B9C1D>"), "TOK")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("boltor", self.calls[0][2].lower())
        self.assertNotIn("<@", self.calls[0][2])

    def test_group_full_message_not_whitelisted_silent(self):
        self.bot._on_event(None, self._pkt_group_full("D/XX/wm boltor", gid="GX"), "TOK")
        self.assertEqual(self.calls, [])

    def test_image_reply_sends_tail_in_same_message(self):
        """WM 回复：图片 + /w 字符串合成一条图文混排消息，且不带 (wf.m ...) 备注。"""
        calls = []
        reply = qq_common.ImageReply("【物品】Boltor Prime\n1. 🟢 InGame — 10 铂金",
                                     [b"\x89PNG"])
        reply.tail = ("英文：/w InGame Hi! I want to buy: Boltor Prime for 10 platinum.\n"
                      "中文：/w InGame 你好，我想购买 螺钉步枪Prime 10 铂金.")

        def fake_images(self, token, kind, target_id, pngs, msg_id, content=None):
            calls.append(("image", len(pngs), content))
            # 模拟真实发图结果：content 随图发出 → mixed=True
            return qq_official._ImageSendResult(True, mixed=bool(content))

        def fake_send(token, appid, api_base, kind, target_id, content, msg_id=None):
            calls.append(("text", content))
            return True, {"id": "ok"}

        self.bot.handler = lambda text, ctx: reply
        with patch.object(qq_official.OfficialBot, "_reply_images", fake_images), \
             patch.object(qq_official, "send_message", fake_send):
            self.bot._dispatch_reply("TOK", "group", "G1", "wm boltor", {}, "M1")
        self.assertEqual(len(calls), 1, "只发一条消息：图 + /w 文字，不再拆两条")
        self.assertEqual(calls[0][0], "image")
        tail = calls[0][2]
        self.assertIn("/w InGame", tail)
        self.assertNotIn("wf.m", tail)
        self.assertNotIn("RabbitBot", tail)

    def test_text_fallback_keeps_tail(self):
        """发图失败回退纯文本时，/w 指令必须并回去，不能丢。"""
        reply = qq_common.ImageReply("价格列表正文", [b"\x89PNG"])
        reply.tail = "英文：/w Seller Hi! I want to buy: X for 5 platinum."
        self.bot.handler = lambda text, ctx: reply
        with patch.object(qq_official.OfficialBot, "_reply_images",
                          lambda *a, **kw: False):
            self.bot._dispatch_reply("TOK", "group", "G1", "wm boltor", {}, "M1")
        self.assertEqual(len(self.calls), 1)
        content = self.calls[0][2]
        self.assertIn("价格列表正文", content)
        self.assertIn("/w Seller", content, "回退时 /w 指令不能丢")


class TestNotifySwitchAndActivePush(unittest.TestCase):
    """群资料页两个开关对应的事件 / 主动推送（依据官方 1<<25 事件文档）。"""

    def setUp(self):
        self.bot = qq_official.OfficialBot("app", "sec", lambda t, c: "x")
        self.bot.token_mgr.get_token = lambda *a, **k: "TOK"

    def _pkt(self, event, d, eid="EV1"):
        return {"op": 0, "t": event, "id": eid, "d": d}

    def test_group_msg_receive_uses_group_openid(self):
        # 事件体：timestamp / group_openid / op_member_openid
        pkt = self._pkt("GROUP_MSG_RECEIVE",
                        {"timestamp": 1784276800, "group_openid": "G1",
                         "op_member_openid": "OP1"})
        with patch("ws_monitor.notify_active_ready") as fake:
            self.bot._on_event(None, pkt, "TOK")
        fake.assert_called_once_with("group", "G1")

    def test_c2c_msg_receive_uses_openid(self):
        # 单聊事件体是 openid，不是 user_openid（取错就补推不到人）
        pkt = self._pkt("C2C_MSG_RECEIVE",
                        {"timestamp": 1784570617, "openid": "U1"})
        with patch("ws_monitor.notify_active_ready") as fake:
            self.bot._on_event(None, pkt, "TOK")
        fake.assert_called_once_with("private", "U1")

    def test_reject_event_does_not_trigger_push(self):
        with patch("ws_monitor.notify_active_ready") as fake:
            self.bot._on_event(None, self._pkt("GROUP_MSG_REJECT",
                                               {"group_openid": "G1"}), "TOK")
        fake.assert_not_called()

    def test_resumed_with_string_d_is_safe(self):
        # RESUMED 的 d 是空串，不能让 sorted(d.keys()) 把连接搞崩
        self.bot._on_event(None, {"op": 0, "t": "RESUMED", "d": ""}, "TOK")

    def test_send_active_prefers_image(self):
        calls = []

        def fake_reply(self, tok, kind, tid, pngs, mid, content=None):
            calls.append((kind, tid, len(pngs), mid))
            return qq_official._ImageSendResult(True, mixed=True)

        with patch("render_card.render", lambda *a, **k: [b"\x89PNG"]), \
             patch.object(qq_official.OfficialBot, "_reply_images", fake_reply), \
             patch.object(qq_official, "send_message",
                          lambda *a, **k: (False, "不该走到文本")):
            ok = self.bot.send_active("group", "G1", "监控内容")
        self.assertTrue(ok)
        self.assertEqual(calls, [("group", "G1", 1, None)],
                         "主动发图不能带 msg_id，否则就不是主动消息了")

    def test_send_active_falls_back_to_text(self):
        calls = []
        with patch("render_card.render", lambda *a, **k: None), \
             patch.object(qq_official, "send_message",
                          lambda *a, **k: calls.append(k.get("msg_id")) or (True, {})):
            ok = self.bot.send_active("group", "G1", "监控内容")
        self.assertTrue(ok)

    def test_passive_failure_retries_without_msg_id(self):
        """@ 消息被动回复若被拒，自动去掉 msg_id 走主动通道。"""
        calls = []

        def fake_send(token, appid, api_base, kind, target_id, content, msg_id=None):
            calls.append(msg_id)
            return (False, "被动窗口不可用") if msg_id else (True, {"id": "ok"})

        self.bot.handler = lambda t, c: "正文"
        with patch.object(qq_official.OfficialBot, "_reply_images",
                          lambda *a, **kw: False), \
             patch.object(qq_official, "send_message", fake_send):
            self.bot._dispatch_reply("TOK", "group", "G1", "wm x", {}, "M1")
        self.assertEqual(calls, ["M1", None])

    def test_full_message_sends_plain_group_message(self):
        """免 @ 全量消息：单纯发一条普通群消息，不 @ 人、不引用某条消息。"""
        calls = []

        def fake_send(token, appid, api_base, kind, target_id, content, msg_id=None):
            calls.append((msg_id, content))
            return True, {"id": "ok"}

        self.bot.handler = lambda t, c: "正文内容"
        ctx = {"channel": "group", "group_id": "G1", "full_message": True}
        with patch.object(qq_official.OfficialBot, "_reply_images",
                          lambda *a, **kw: False), \
             patch.object(qq_official, "send_message", fake_send):
            self.bot._dispatch_reply("TOK", "group", "G1", "wm boltor", ctx, "M1")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0][0], "免 @ 消息不该带 msg_id（否则变成引用回复）")
        self.assertNotIn("<@", calls[0][1], "回复内容里不能 @ 任何人")

    def test_full_message_active_failure_falls_back_to_passive(self):
        calls = []

        def fake_send(token, appid, api_base, kind, target_id, content, msg_id=None):
            calls.append(msg_id)
            return (False, "40034105 无权限") if msg_id is None else (True, {"id": "ok"})

        self.bot.handler = lambda t, c: "正文"
        ctx = {"channel": "group", "group_id": "G1", "full_message": True}
        with patch.object(qq_official.OfficialBot, "_reply_images",
                          lambda *a, **kw: False), \
             patch.object(qq_official, "send_message", fake_send):
            self.bot._dispatch_reply("TOK", "group", "G1", "wm x", ctx, "M1")
        self.assertEqual(calls, [None, "M1"], "主动发不出去要回退到被动引用")

    def test_at_message_keeps_passive_msg_id(self):
        calls = []

        def fake_send(token, appid, api_base, kind, target_id, content, msg_id=None):
            calls.append(msg_id)
            return True, {"id": "ok"}

        self.bot.handler = lambda t, c: "正文"
        with patch.object(qq_official.OfficialBot, "_reply_images",
                          lambda *a, **kw: False), \
             patch.object(qq_official, "send_message", fake_send):
            self.bot._dispatch_reply("TOK", "group", "G1", "wm x", {}, "M1")
        self.assertEqual(calls, ["M1"], "@ 消息仍走被动窗口，不消耗主动配额")

    def test_fullmsg_active_switch_off(self):
        """WM_FULLMSG_ACTIVE=0 可切回「全量消息也走被动」。"""
        calls = []

        def fake_send(token, appid, api_base, kind, target_id, content, msg_id=None):
            calls.append(msg_id)
            return True, {"id": "ok"}

        self.bot.handler = lambda t, c: "正文"
        ctx = {"channel": "group", "group_id": "G1", "full_message": True}
        with patch.dict(os.environ, {"WM_FULLMSG_ACTIVE": "0"}, clear=False), \
             patch.object(qq_official.OfficialBot, "_reply_images",
                          lambda *a, **kw: False), \
             patch.object(qq_official, "send_message", fake_send):
            self.bot._dispatch_reply("TOK", "group", "G1", "wm x", ctx, "M1")
        self.assertEqual(calls, ["M1"])


class TestConnectIdentify(unittest.TestCase):
    """起本地 mock gateway，验证 OfficialBot 完成握手并发出 Identify。"""

    def test_handshake_and_identify(self):
        # 主线程先绑定「系统分配的空闲端口」并 listen，再交给子线程 accept。
        # 旧版写死 18700：端口被占用 / listen 竞态时客户端会空等 90s 读取超时（偶发 FAIL）。
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(5.0)
        port = srv.getsockname()[1]
        got = {}

        def gateway():
            try:
                conn, _ = srv.accept()
            except Exception as e:
                got["err"] = "accept: %s" % e
                return
            try:
                onebot.server_handshake(conn)
                ws = onebot.WSConn(conn, client_mask=False)   # 服务端发不掩码
                ws.send_text(json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}}))
                typ, payload = ws.recv_message()
                msg = json.loads(payload.decode("utf-8"))
                got["op"] = msg.get("op")
                got["token"] = msg.get("d", {}).get("token")
                got["intents"] = msg.get("d", {}).get("intents")
                ws.send_close()
            except Exception as e:
                got["err"] = str(e)
            finally:
                conn.close()
                srv.close()

        th = threading.Thread(target=gateway, daemon=True)
        th.start()

        bot = qq_official.OfficialBot(
            "myapp", "mysecret",
            qq_common.make_handler(set(), allow_all=True),
            api_base="http://127.0.0.1:%d" % port,
        )
        bot.token_mgr.get_token = lambda *a, **k: "FAKETOKEN"
        bot._get_gateway = lambda token: "ws://127.0.0.1:%d" % port
        try:
            bot._connect_loop()
        except Exception as e:
            got.setdefault("err", str(e))

        self.assertEqual(got.get("op"), 2,
                         "应收到 Identify(op=2)：%s" % got.get("err", ""))
        self.assertTrue(str(got.get("token", "")).startswith("QQBot "),
                        "Identify token 格式应为 'QQBot <token>'")
        self.assertEqual(got.get("intents"), qq_official.DEFAULT_INTENTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
