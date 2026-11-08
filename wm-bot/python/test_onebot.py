#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_onebot.py — onebot / qq_bot 离线测试（不依赖真实 QQ）

覆盖：
  1) WebSocket 帧编解码往返（掩码 / 非掩码 / ping->pong）；
  2) 消息处理 handler：触发词识别、mock 源查询、非指令不回复；
  3) 端到端：起 onebot server（脚本为 WS 服务端），用标准库写的 mock OneBot 客户端连过来
     发 group message 事件，验证脚本回 send_group_msg 且内容正确。
"""
import os
import sys
import json
import time
import socket
import threading
import unittest
import contextlib
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["WM_SOURCE"] = "mock"   # 必须在 import wm 之前：离线测试不发 HTTP
import onebot
import qq_common as qq_bot   # 测试共享逻辑（handler / 触发词 / 白名单）已迁至 qq_common
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


class TestFrames(unittest.TestCase):
    def test_frame_roundtrip_masked(self):
        a, b = socket.socketpair()
        ca = onebot.WSConn(a, client_mask=True)    # 客户端发掩码
        cb = onebot.WSConn(b, client_mask=False)   # 服务端收（不解掩码）
        ca.send_text("hello 世界")
        typ, payload = cb.recv_message()
        self.assertEqual(typ, "text")
        self.assertEqual(payload.decode("utf-8"), "hello 世界")

    def test_frame_roundtrip_unmasked(self):
        a, b = socket.socketpair()
        ca = onebot.WSConn(a, client_mask=False)   # 服务端发不掩码
        cb = onebot.WSConn(b, client_mask=True)    # 客户端收
        ca.send_text("server data 123")
        typ, payload = cb.recv_message()
        self.assertEqual(payload.decode("utf-8"), "server data 123")

    def test_handshake_keeps_trailing_frame(self):
        """101 响应和第一帧落在同一个 TCP 段时，第一帧不能被握手丢掉。

        这是真实踩过的坑：QQ 网关常在回完 101 后立刻发 op10 Hello，两段被同一次
        recv 读走；旧实现把 \r\n\r\n 之后的字节直接丢弃，客户端就会一直等第一帧
        （90 秒读超时）才重连 —— 现象是「日志显示已连网关，但完全没有事件」。
        """
        c, s = socket.socketpair()

        def server():
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += s.recv(4096)
            s.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                      b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                      b"\x81\x03abc")          # 文本帧 "abc"，故意和响应同一次发送

        th = threading.Thread(target=server, daemon=True)
        th.start()
        try:
            leftover = onebot.client_handshake(c, "127.0.0.1", 1, "/", None)
            self.assertEqual(leftover, b"\x81\x03abc", "101 之后的剩余字节不能丢")
            conn = onebot.WSConn(c, client_mask=True, initial=leftover)
            typ, payload = conn.recv_message()
            self.assertEqual((typ, payload), ("text", b"abc"))
        finally:
            c.close()
            s.close()

    def test_ping_auto_pong(self):
        a, b = socket.socketpair()
        ca = onebot.WSConn(a, client_mask=False)   # a 端会消费 ping 并自动回 pong
        cb = onebot.WSConn(b, client_mask=False)
        stop = threading.Event()

        def drain():
            try:
                while not stop.is_set():
                    ca.recv_message()   # 读到 ping 会自动回 pong 并继续循环
            except Exception:
                pass

        th = threading.Thread(target=drain, daemon=True)
        th.start()
        b.sendall(onebot._make_frame(0x9, b"", mask=False))   # b 端发 ping
        fin, opcode, _ = cb.read_frame()                       # b 端读 a 回的 pong
        self.assertEqual(opcode, 0xA)
        stop.set()


class TestHandler(unittest.TestCase):
    def test_is_wm_command(self):
        self.assertTrue(qq_bot.is_wm_command("WM Boltor Prime"))
        self.assertTrue(qq_bot.is_wm_command("zk Tigris"))
        self.assertTrue(qq_bot.is_wm_command("/ZK Tigris"))
        self.assertTrue(qq_bot.is_wm_command("/报错 WM x"))
        self.assertTrue(qq_bot.is_wm_command("紫卡 Tigris"))
        self.assertFalse(qq_bot.is_wm_command("今天天气不错"))
        self.assertFalse(qq_bot.is_wm_command("bolt or prime"))
        self.assertFalse(qq_bot.is_wm_command("/help"))
        self.assertFalse(qq_bot.is_wm_command("/wmz other"))   # 不是 wm/zk 首词

    def test_handler_mock(self):
        os.environ["WM_SOURCE"] = "mock"
        h = qq_bot.make_handler()   # 无参=宽松（所有群都回），便于本地直跑
        # WM 查询现在必须带 D/XX/ 前缀，否则静默（防群聊误触发）
        self.assertIsNone(h("WM Boltor Prime", {}), "裸 WM 查询应静默")
        reply = h("D/XX/WM Boltor Prime", {})
        self.assertIn("boltor prime", reply.lower())
        self.assertIn("铂金", reply)
        # 非指令不回复
        self.assertIsNone(h("你好啊群友", {}))
        # 报错 强制刷新也能走通（带 D/XX/ 前缀；报错是斜杠触发词，前缀前加 /）
        reply2 = h("/D/XX/报错 WM Boltor Prime", {})
        self.assertIn("boltor prime", reply2.lower())

    def test_group_whitelist(self):
        os.environ["WM_SOURCE"] = "mock"
        # 严格模式：仅白名单群 123456 才回复
        h = qq_bot.make_handler({"123456"}, allow_all=False)
        # 白名单群内 WM 触发（带 D/XX/ 前缀） -> 回复
        self.assertIsNotNone(h("D/XX/WM Boltor Prime",
                               {"message_type": "group", "group_id": 123456}))
        # 非白名单群（即使带前缀）-> 静默
        self.assertIsNone(h("D/XX/WM Boltor Prime",
                            {"message_type": "group", "group_id": 999999}))
        # 非白名单群内非指令 -> 静默
        self.assertIsNone(h("闲聊一下",
                            {"message_type": "group", "group_id": 123456}))
        # 私聊不受白名单限制
        self.assertIsNotNone(h("D/XX/ZK Tigris",
                               {"message_type": "private", "user_id": 1}))
        # 空白名单 -> 所有群都静默（符合"没写就不监控"）
        h2 = qq_bot.make_handler(set(), allow_all=False)
        self.assertIsNone(h2("D/XX/WM x", {"message_type": "group", "group_id": 123456}))

    def test_query_prefix_gating(self):
        """D/XX/ 前缀门槛：WM / 世界状态查询 / 监控开关 / 自检 都必须带 D/XX/ 前缀；
        唯一例外是「帮助」类词（发两个字的「帮助」就出菜单，用户明确要求）；
        只有机器人主动推送（send_active）不在用户指令范畴，不受影响。"""
        os.environ["WM_SOURCE"] = "mock"
        h = qq_bot.make_handler()   # 宽松
        # WM 查询：裸 -> 静默；带 D/XX/ -> 回复
        self.assertIsNone(h("wm Boltor", {"channel": "group", "group_id": "g"}))
        self.assertIsNone(h("zk Tigris", {"channel": "group", "group_id": "g"}))
        self.assertIsNotNone(h("D/XX/wm Boltor",
                               {"channel": "group", "group_id": "g"}))
        self.assertIsNotNone(h("D/XX/zk Tigris",
                               {"channel": "group", "group_id": "g"}))
        # 世界状态查询：也必须带 D/XX/ 前缀（防群聊误触发），裸 -> 静默
        self.assertIsNone(h("平原", {"channel": "group", "group_id": "g"}))
        self.assertIsNotNone(h("D/XX/平原", {"channel": "group", "group_id": "g"}))
        # 监控控制：裸「监控」「监控全部」都静默；带 D/XX/ 才生效
        self.assertIsNone(h("监控", {"channel": "group", "group_id": "g"}))
        self.assertIsNone(h("监控全部", {"channel": "group", "group_id": "g"}))
        r = h("D/XX/监控", {"channel": "group", "group_id": "g"})
        self.assertIsNotNone(r)
        self.assertIn("监控全部", r)   # 安全提示，不全开
        self.assertIsNotNone(h("D/XX/监控全部", {"channel": "group", "group_id": "g"}))
        # 帮助【例外】：发「帮助」两个字就出菜单，不要求前缀（用户明确要求）；
        # 自检仍需前缀
        r_bare_help = h("帮助", {"channel": "group", "group_id": "g"})
        self.assertIsNotNone(r_bare_help)
        self.assertIn("全部指令一览", r_bare_help)
        self.assertIsNotNone(h("D/XX/帮助", {"channel": "group", "group_id": "g"}))
        self.assertIsNone(h("?", {"channel": "group", "group_id": "g"}))
        self.assertIsNone(h("自检", {"channel": "group", "group_id": "g"}))
        self.assertIsNotNone(h("D/XX/自检", {"channel": "group", "group_id": "g"}))


class TestAtBot(unittest.TestCase):
    """@ 机器人：允许不带 D/XX/ 前缀；回复与监控推送都 @ 发起人。"""

    @classmethod
    def setUpClass(cls):
        global ws_monitor
        import ws_monitor
        d = tempfile.mkdtemp(prefix="at_bot_")
        os.environ["WS_AT_FILE"] = os.path.join(d, "ws_at.json")
        os.environ["WS_SWITCH_FILE"] = os.path.join(d, "ws_sw.json")
        os.environ["WS_FILTER_FILE"] = os.path.join(d, "ws_fl.json")
        os.environ["WS_SUB_FILE"] = os.path.join(d, "ws_subs.json")
        ws_monitor.LAST_SENT_FILE = Path(d) / "ws_last_sent.json"
        cls._dir = d

    def setUp(self):
        os.environ["WM_SOURCE"] = "mock"
        ws_monitor._at_users.clear()

    def test_parse_at_segments(self):
        """数组段 / 老协议 CQ 码都能认出「@ 了机器人」，@ 别人不算。"""
        segs = [{"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": " 监控 警报"}}]
        self.assertEqual(onebot._parse_message(segs, "999"), ("监控 警报", True))
        other = [{"type": "at", "data": {"qq": "111"}},
                 {"type": "text", "data": {"text": " hello"}}]
        self.assertEqual(onebot._parse_message(other, "999"), ("hello", False))
        self.assertEqual(onebot._parse_message("[CQ:at,qq=999] 平原", "999"),
                         ("平原", True))
        # 图片 / 表情段不能混进指令文本
        mixed = [{"type": "at", "data": {"qq": "999"}},
                 {"type": "image", "data": {"file": "x"}},
                 {"type": "text", "data": {"text": "wm Boltor"}}]
        self.assertEqual(onebot._parse_message(mixed, "999"), ("wm Boltor", True))

    def test_at_allows_prefixless(self):
        """@ 了机器人 = 明确对着我说的：不带 D/XX/ 也能用；没 @ 仍然静默。"""
        h = qq_bot.make_handler()
        at = {"channel": "group", "group_id": "G1", "user_id": "12345", "at_me": True}
        self.assertIsNotNone(h("wm Boltor", at))
        self.assertIsNotNone(h("平原", at))
        self.assertIsNotNone(h("帮助", at))
        no_at = {"channel": "group", "group_id": "G1", "user_id": "12345"}
        self.assertIsNone(h("wm Boltor", no_at), "没 @ 就该维持静默")
        self.assertIsNone(h("平原", no_at))
        # @ 了但说的不是指令：仍然不猜、不接话
        self.assertIsNone(h("今天吃什么", at))

    def test_on_message_at_segment_prefixless(self):
        """整条链路（数组 at 段 → handler）：@机器人 + 免前缀要有回复，且回复 @ 提问人；
        @ 的是别人的号则维持静默。"""
        sent = []

        class FakeConn:
            def send_text(self, s):
                sent.append(json.loads(s))

        c = onebot.OneBotClient(mode="client")
        c.handler = qq_bot.make_handler()
        at_me_msg = {"post_type": "message", "message_type": "group",
                     "group_id": 77, "user_id": 5, "self_id": 9,
                     "message": [{"type": "at", "data": {"qq": "9"}},
                                 {"type": "text", "data": {"text": " 自检"}}]}
        c._on_message(FakeConn(), at_me_msg)
        self.assertEqual(len(sent), 1)
        self.assertIn("[CQ:at,qq=5]", sent[0]["params"]["message"])
        sent.clear()
        at_other = dict(at_me_msg, message=[
            {"type": "at", "data": {"qq": "111"}},
            {"type": "text", "data": {"text": " 自检"}}])
        c._on_message(FakeConn(), at_other)
        self.assertEqual(sent, [], "@ 别人不该回")

    def test_login_info_fills_self_and_nick(self):
        """连上自动问 get_login_info：自身号与昵称不用再手填。"""
        sent = []

        class FakeConn:
            def send_text(self, s):
                sent.append(json.loads(s))

        c = onebot.OneBotClient(mode="client")
        c._ask_login_info(FakeConn())
        self.assertEqual(sent[0]["action"], "get_login_info")
        c._on_api_resp({"status": "ok", "retcode": 0, "echo": c._login_echo,
                        "data": {"user_id": 9, "nickname": "自动化服务查询_D/XX/"}})
        self.assertEqual(c.self_id, "9")
        self.assertEqual(c.nickname, "自动化服务查询_D/XX/")
        # 别的 action 的回执不能把自身信息改坏
        c._on_api_resp({"status": "ok", "retcode": 0, "echo": 987654,
                        "data": {"user_id": 111, "nickname": "别人"}})
        self.assertEqual(c.self_id, "9")
        self.assertEqual(c.nickname, "自动化服务查询_D/XX/")

    def test_auto_nickname_tight_at(self):
        """自动取到昵称后，明文「@昵称紧贴指令」且事件无 self_id 也能回。"""
        sent = []

        class FakeConn:
            def send_text(self, s):
                sent.append(json.loads(s))

        c = onebot.OneBotClient(mode="client")
        c.handler = qq_bot.make_handler()
        c.self_id = "9"
        c.nickname = "自动化服务查询_D/XX/"
        msg = {"post_type": "message", "message_type": "group", "group_id": 77,
               "user_id": 5, "message": "@自动化服务查询_D/XX/自检"}
        c._on_message(FakeConn(), msg)
        self.assertEqual(len(sent), 1, "自动昵称应能切开紧贴的指令")
        self.assertIn("[CQ:at,qq=5]", sent[0]["params"]["message"])

    def test_monitor_records_at_user(self):
        """@机器人 开监控要记住是谁开的，推送才 @ 得到人；停止监控要连带清掉。"""
        from unittest import mock
        with mock.patch.object(ws_monitor, "_ensure_push_thread", lambda: None), \
             mock.patch.object(ws_monitor.ws, "ensure_active", lambda: None):
            h = qq_bot.make_handler()
            self.assertIsNotNone(h("监控 警报", {
                "channel": "group", "group_id": "G9",
                "user_id": "12345", "at_me": True}))
        self.assertEqual(ws_monitor.at_user_of("group", "G9"), "12345")
        ws_monitor._at_users.clear()
        self.assertGreaterEqual(ws_monitor.load_at_users(), 1, "重启后要能恢复")
        self.assertEqual(ws_monitor.at_user_of("group", "G9"), "12345")
        with mock.patch.object(ws_monitor, "_save_last_sent", lambda: None):
            self.assertTrue(ws_monitor.stop_monitor("group", "G9"))
        self.assertIsNone(ws_monitor.at_user_of("group", "G9"))

    def test_push_passes_at_user(self):
        """推送把发起人交给后端；只有三参的后端（官方不能 @）自动退回，不崩。"""
        got = []
        ws_monitor.set_sender(lambda ch, tid, text, at_user=None:
                              got.append((ch, tid, at_user)) or True)
        self.assertTrue(ws_monitor._send_one("group", "G1", "x", "12345"))
        self.assertEqual(got, [("group", "G1", "12345")])
        got.clear()
        ws_monitor.set_sender(lambda ch, tid, text: got.append((ch, tid, None)) or True)
        self.assertTrue(ws_monitor._send_one("group", "G1", "x", "12345"))
        self.assertEqual(got, [("group", "G1", None)])
        ws_monitor.set_sender(None)

    def test_reply_mentions_sender(self):
        """群回复带 @ 发送者（文本用 CQ 码前缀，老客户端也认）；私聊不加。"""
        sent = []

        class FakeConn:
            def send_text(self, s):
                sent.append(json.loads(s))

        c = onebot.OneBotClient(mode="client")
        c._send_reply(FakeConn(), {"message_type": "group", "group_id": 777,
                                   "user_id": 12345}, "你好")
        self.assertEqual(sent[-1]["params"]["message"], "[CQ:at,qq=12345] 你好")
        sent.clear()
        c._send_reply(FakeConn(), {"message_type": "private", "user_id": 1}, "你好")
        self.assertEqual(sent[-1]["params"]["message"], "你好")

    def test_send_active_mentions(self):
        """监控推送：给了 at_user 就在最前面 @ 他，没给就还是原样子符串。"""
        sent = []

        class FakeConn:
            def send_text(self, s):
                sent.append(json.loads(s))

        c = onebot.OneBotClient(mode="client")
        c._conn = FakeConn()
        c.send_active("group", 777, "警报：新", at_user=12345)
        segs = sent[-1]["params"]["message"]
        self.assertEqual(segs[0], {"type": "at", "data": {"qq": "12345"}})
        self.assertEqual(segs[1]["data"]["text"], "警报：新")
        sent.clear()
        # 群里多人开了监控：逐个 @，文本段仍只有一条
        c.send_active("group", 777, "警报：新", at_user=["111", "222"])
        segs = sent[-1]["params"]["message"]
        self.assertEqual([s["data"]["qq"] for s in segs if s["type"] == "at"],
                         ["111", "222"])
        self.assertEqual(segs[-1], {"type": "text", "data": {"text": "警报：新"}})
        sent.clear()
        c.send_active("group", 777, "警报：新", at_user="111,222")
        segs = sent[-1]["params"]["message"]
        self.assertEqual([s["data"]["qq"] for s in segs if s["type"] == "at"],
                         ["111", "222"])
        sent.clear()
        c.send_active("group", 777, "警报：新")
        self.assertEqual(sent[-1]["params"]["message"], "警报：新")


class TestIntegrationServer(unittest.TestCase):
    """起 onebot server，用 mock OneBot 客户端连过来，验证端到端收发。"""

    def test_server_echo(self):
        os.environ["WM_SOURCE"] = "mock"
        port = 16700
        client = onebot.OneBotClient(
            url=f"ws://127.0.0.1:{port}", mode="server", handler=qq_bot.make_handler()
        )
        t = threading.Thread(target=client.run, daemon=True)
        t.start()
        time.sleep(0.6)   # 等监听起来

        # mock OneBot 客户端：连 server、做握手、发 message 事件、收 send_group_msg
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        leftover = onebot.client_handshake(sock, "127.0.0.1", port, "/", None)
        ws = onebot.WSConn(sock, client_mask=True, initial=leftover)

        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123456,
            "user_id": 999,
            "message": "D/XX/WM Boltor Prime",
            "raw_message": "D/XX/WM Boltor Prime",
        }
        ws.send_text(json.dumps(event, ensure_ascii=False))

        got = None
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                typ, payload = ws.recv_message()
            except Exception:
                break
            if typ == "close":
                break
            if typ == "text":
                msg = json.loads(payload.decode("utf-8"))
                if msg.get("action") == "send_group_msg":
                    got = msg
                    break

        client.stop()
        try:
            sock.close()
        except Exception:
            pass

        self.assertIsNotNone(got, "未收到 send_group_msg 回复")
        self.assertEqual(got["params"]["group_id"], 123456)
        # 现在默认「一律出图」，message 会是段数组（含 base64 图片段）；
        # 无字体/Pillow 时退回纯文本。两种形态都要能发出去。
        sent = got["params"]["message"]
        if isinstance(sent, list):
            kinds = {s.get("type") for s in sent if isinstance(s, dict)}
            self.assertTrue(kinds & {"image", "text"}, "段数组里既没有图片也没有文本")
            texts = "".join(
                (s.get("data") or {}).get("text", "") for s in sent
                if isinstance(s, dict) and s.get("type") == "text")
        else:
            texts = str(sent)
        if texts:
            self.assertIn("boltor prime", texts.lower())

    def test_send_reply_text_fallback(self):
        """强制纯文本（如「文字 WM …」）时仍发单个字符串，老客户端兼容。"""
        class _Sink:
            sent = None

            def send_text(self, s):
                self.sent = s

        class _Reply(str):
            pngs = []

        conn = _Sink()
        client = onebot.OneBotClient()
        client._send_reply(conn, {"message_type": "group", "group_id": 42},
                           _Reply("hello 文本"))
        payload = json.loads(conn.sent)
        self.assertEqual(payload["params"]["message"], "hello 文本")


class TestEcho(unittest.TestCase):
    """收发都打一行：手机上才能分清「没收到」「收到了但静默」「回了但发失败」。"""

    def setUp(self):
        os.environ["WM_QQ_ECHO"] = "1"

    def tearDown(self):
        os.environ.pop("WM_QQ_ECHO", None)

    def _feed(self, handler, msg):
        sent = []

        class FakeConn:
            def send_text(self, s):
                sent.append(json.loads(s))

        c = onebot.OneBotClient(mode="client")
        c.handler = handler
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            c._on_message(FakeConn(), msg)
        return buf.getvalue(), sent

    def test_recv_then_silent(self):
        """收到先说一句（含 @ 状态与原文），静默再补一行原因。"""
        at_msg = {"post_type": "message", "message_type": "group", "group_id": 77,
                  "user_id": 5, "self_id": 9,
                  "message": [{"type": "at", "data": {"qq": "9"}},
                              {"type": "text", "data": {"text": " 今天吃什么"}}]}
        out, sent = self._feed(lambda t, c: None, at_msg)
        self.assertIn("[recv] 群 77 用户=5 @我=是 at=9 self=9 文本='今天吃什么'", out)
        self.assertIn("[silent]", out)
        self.assertEqual(sent, [])

    def test_recv_then_reply(self):
        """命中指令：先打 [reply] 行，再真的发出去。"""
        msg = {"post_type": "message", "message_type": "private", "user_id": 5,
               "self_id": 9, "message": "D/XX/自检"}
        out, sent = self._feed(lambda t, c: "好的", msg)
        self.assertIn("[recv] 私聊 5 用户=5 @我=否 at=- self=9 文本='D/XX/自检'", out)
        self.assertIn("[reply] 私聊 5", out)
        self.assertEqual(len(sent), 1)

    def test_echo_muted(self):
        """WM_QQ_ECHO = 0 时一行都不打（群全量模式嫌吵时用）。"""
        os.environ["WM_QQ_ECHO"] = "0"
        msg = {"post_type": "message", "message_type": "group", "group_id": 77,
               "user_id": 5, "self_id": 9, "message": "闲聊"}
        out, _ = self._feed(lambda t, c: None, msg)
        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
