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
  5) send_message 请求体：被动回复带 msg_id（事件 d.id）、不带 event_id。
"""
import os
import sys
import io
import json
import time
import socket
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["WM_SOURCE"] = "mock"   # 必须在 import wm 之前：离线测试不发 HTTP
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

    def _pkt_c2c(self, content, openid="U1", mid="M2"):
        return {"op": 0, "t": "C2C_MESSAGE_CREATE",
                "d": {"author": {"user_openid": openid}, "content": content, "id": mid}}

    def test_group_whitelisted_trigger(self):
        self.bot._on_event(None, self._pkt_group("<@!bot> wm boltor prime"), "TOK")
        self.assertEqual(len(self.calls), 1)
        kind, gid, content = self.calls[0]
        self.assertEqual(kind, "group")
        self.assertEqual(gid, "G1")
        self.assertIn("boltor prime", content.lower())
        self.assertIn("铂金", content)

    def test_group_not_whitelisted_silent(self):
        self.bot._on_event(None, self._pkt_group("<@!bot> wm boltor", gid="GX"), "TOK")
        self.assertEqual(self.calls, [])

    def test_group_non_trigger_silent(self):
        self.bot._on_event(None, self._pkt_group("<@!bot> 你好啊"), "TOK")
        self.assertEqual(self.calls, [])

    def test_c2c_not_limited(self):
        # 单聊不受白名单限制
        self.bot._on_event(None, self._pkt_c2c("zk tigris"), "TOK")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "user")
        self.assertIn("tigris", self.calls[0][2].lower())

    def test_passive_msg_id(self):
        pkt = self._pkt_group("<@!bot> wm boltor", mid="EVENT999")
        self.bot._on_event(None, pkt, "TOK")
        # 被动回复须把事件 d.id 作为 msg_id 传入（不是 event_id）
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "G1")
        self.assertEqual(self.msg_ids[0], "EVENT999")


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
