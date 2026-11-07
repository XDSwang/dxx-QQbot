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
        onebot.client_handshake(sock, "127.0.0.1", port, "/", None)
        ws = onebot.WSConn(sock, client_mask=True)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
