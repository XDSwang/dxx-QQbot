#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qq_official.py — 把 WM 查询接入【官方 QQ 机器人】（q.qq.com，无封号风险）

与 OneBot（社区逆向方案）不同，官方机器人走腾讯正规开放平台：
  - 鉴权：POST https://bots.qq.com/app/getAppAccessToken → access_token（7200s，需自刷）
  - 网关：GET https://api.bot.qq.com/gateway → wss://api.bot.qq.com/websocket/
  - 事件（WebSocket，应用层 JSON 帧）：
      GROUP_AT_MESSAGE_CREATE  群 @机器人 消息
      C2C_MESSAGE_CREATE       单聊消息
  - 发消息（REST）：
      POST /v2/groups/{group_openid}/messages   （群）
      POST /v2/users/{openid}/messages          （单聊）
    头：Authorization: QQBot {token}  +  X-Union-Appid: {appid}

要点：
  - group_openid 就是【普通 QQ 群】的标识（字符串形式，不是数字群号）；
    2026 年腾讯官方机器人已支持直接进普通群（群主把机器人拉进群即可，对应 GROUP_ADD_ROBOT 事件），并非只能用频道；
  - 群里默认必须 @机器人 才会收到消息，content 形如 "<@!bot_userid> wm boltor"；
    本脚本会剥离 @前缀，其余逻辑复用 qq_common.make_handler；
  - 发消息走被动回复（带 msg_id = 事件 d.id），5 分钟内有效、且不受主动消息频控限制；
    event_id 仅支持 INTERACTION_CREATE / GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE 等事件，
    普通群@/单聊消息误用会报 40034027「请求参数event_id对应事件不能回复消息」；
  - 全部纯标准库（urllib/ssl/socket/threading），桌面 / Termux / QPython 通用。

配置（统一写在同目录 data/config.txt，每行「名称 = 值」，文件内有中文注释）：
  WM_QQ_APPID            在 q.qq.com 管理端获得的 appid（必填）
  WM_QQ_SECRET           clientSecret（必填，等同账号凭证，勿外泄 / 勿提交）
  WM_QQ_API_BASE         OpenAPI 根地址（默认 https://api.bot.qq.com）
  WM_QQ_GROUPS_FILE      群白名单文件（默认：脚本同目录 data/groups.txt；相对路径也按同目录解析）
  WM_QQ_ALLOW_ALL_GROUPS 0 = 只监控白名单群（正式）；1 = 临时监控所有群（仅调试）
  WM_QQ_INTENTS          事件订阅位掩码（默认 33554432：群@消息 + 单聊）

群白名单：
  - 默认不监控任何群；只有 data/groups.txt 里写进的 group_openid 才回复；
  - 白名单群内，仅消息剥离 @后 以 /wm 或 /zk 开头才回复；
  - 单聊不受白名单限制。

运行（整个文件夹拷到任意目录，配置写进 data/config.txt 后）：
  python qq_bot.py          （Windows / 手机 Termux / QPython 都一样）

凭证安全：appid/secret 从 data/config.txt 读取；本脚本不写日志落盘、不打印 secret。
"""
import os
import sys
import json
import time
import ssl
import socket
import threading
import urllib.parse
import urllib.request
import urllib.error

import config as bot_config

# 复用 onebot 的 WebSocket 帧编解码 / 客户端握手（纯标准库）
from onebot import WSConn, client_handshake

import qq_common
import wm

# ---------------- 覆盖式运行流程日志 ----------------
# 每次启动清空重写（FileHandler mode="w"），便于排查"连上但没回复"等问题。
# 日志路径：脚本同目录 data/bot_flow.log（基于 __file__，手机任意路径可用）
import logging
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_flow.log")
os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)   # 全新拷贝时 data/ 可能还不存在
_flow_logger = logging.getLogger("wm_qq_flow")
_flow_logger.setLevel(logging.DEBUG)
_flow_logger.propagate = False
if not _flow_logger.handlers:
    _fh = logging.FileHandler(_LOG_PATH, mode="w", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    _flow_logger.addHandler(_fh)


def flog(msg):
    """写流程日志（覆盖式落盘到 data/bot_flow.log）。"""
    try:
        _flow_logger.info(msg)
    except Exception:
        pass


DEFAULT_API_BASE = "https://api.bot.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
# 1 << 25：GROUP_AT_MESSAGE_CREATE（群@消息）+ C2C_MESSAGE_CREATE（单聊）
DEFAULT_INTENTS = 33554432


# ---------------- 鉴权：access_token 获取与刷新 ----------------
class TokenManager:
    """获取并缓存 app_access_token（7200s 有效期，临近过期 60s 自动刷新）。"""

    def __init__(self, appid, secret, api_base=DEFAULT_API_BASE):
        self.appid = appid
        self.secret = secret
        self.api_base = api_base.rstrip("/")
        self._token = None
        self._expires_at = 0
        self._lock = threading.Lock()

    def get_token(self, force=False):
        with self._lock:
            now = time.time()
            if not force and self._token and now < self._expires_at - 60:
                return self._token
            data = json.dumps(
                {"appId": self.appid, "clientSecret": self.secret}
            ).encode("utf-8")
            req = urllib.request.Request(
                TOKEN_URL, data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")
                raise RuntimeError(f"获取 access_token 失败 HTTP {e.code}: {detail}")
            except Exception as e:
                raise RuntimeError(f"获取 access_token 失败: {e}")
            token = body.get("access_token")
            if not token:
                raise RuntimeError(f"access_token 为空: {body}")
            try:
                exp = int(body.get("expires_in", 7200))
            except (TypeError, ValueError):
                exp = 7200
            self._token = token
            self._expires_at = time.time() + exp
            return token


# ---------------- 发消息（REST） ----------------
def send_message(token, appid, api_base, target_kind, target_id, content, msg_id=None):
    """
    发送被动/主动消息。target_kind: "group"（群）| "user"（单聊）。
    被动回复带 msg_id（来自事件的 d.id）可走 5 分钟有效窗口、避开主动频控。
    注意：GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE 这类消息事件必须用 msg_id；
    event_id 仅支持 INTERACTION_CREATE / GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE 等特定事件。
    返回 (ok, resp_json_or_error_str)。
    """
    if target_kind == "group":
        path = f"/v2/groups/{target_id}/messages"
    else:
        path = f"/v2/users/{target_id}/messages"
    url = api_base.rstrip("/") + path
    body = {"content": content, "msg_type": 0}
    if msg_id:
        body["msg_id"] = msg_id
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": str(appid),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"
    except Exception as e:
        return False, str(e)


# ---------------- 剥离 @机器人 前缀 ----------------
def strip_mention(text):
    """去掉形如 "<@!123456>" / "<@123456>" 的 @前缀（官方群里消息必带）。"""
    t = (text or "").strip()
    while t.startswith("<@"):
        end = t.find(">")
        if end == -1:
            break
        t = t[end + 1:].strip()
    return t


# ---------------- 官方机器人主类 ----------------
class OfficialBot:
    def __init__(self, appid, secret, handler, api_base=DEFAULT_API_BASE,
                 intents=DEFAULT_INTENTS):
        self.appid = appid
        self.secret = secret
        self.api_base = api_base.rstrip("/")
        self.intents = intents
        self.handler = handler
        self.token_mgr = TokenManager(appid, secret, api_base)
        self._stop = threading.Event()
        self._seq = None
        self._hb_timer = None

    def stop(self):
        self._stop.set()
        if self._hb_timer:
            try:
                self._hb_timer.cancel()
            except Exception:
                pass

    def run_forever(self):
        try:
            while not self._stop.is_set():
                try:
                    self._connect_loop()
                except Exception as e:
                    print(f"[official] 连接/处理出错: {e}", file=sys.stderr)
                if self._stop.wait(5):
                    break
        except KeyboardInterrupt:
            self.stop()

    # ---- 连接并维持网关会话 ----
    def _connect_loop(self):
        token = self.token_mgr.get_token()
        gateway = self._get_gateway(token)
        u = urllib.parse.urlparse(gateway)
        host = u.hostname or "api.bot.qq.com"
        port = u.port or 443
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        sock = socket.create_connection((host, port), timeout=15)
        # 长连接空闲读取超时：必须 > 网关心跳间隔(约45s)，否则空闲期被误判断线→重连死循环
        sock.settimeout(90)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        client_handshake(sock, host, port, path, token=None)
        conn = WSConn(sock, client_mask=True)   # 客户端→服务端 发帧需掩码
        print(f"[official] 已连网关 {host}{path}", file=sys.stderr)
        flog(f"已连网关 {host}{path}")
        self._loop(conn, token)

    def _get_gateway(self, token):
        url = self.api_base + "/gateway"
        req = urllib.request.Request(url, headers={"Authorization": f"QQBot {token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))["url"]

    # ---- 网关收发主循环 ----
    def _loop(self, conn, token):
        while not self._stop.is_set():
            try:
                typ, payload = conn.recv_message()
            except Exception as e:
                raise RuntimeError(f"网关读取失败: {e}")
            if typ == "close":
                flog("收到 close 帧，关闭连接")
                try:
                    conn.send_close()
                except Exception:
                    pass
                return
            if typ != "text":
                continue
            try:
                pkt = json.loads(payload.decode("utf-8"))
            except Exception:
                flog("收到无法解析的帧（非 JSON），已跳过")
                continue
            op = pkt.get("op")
            flog(f"收到帧 op={op} t={pkt.get('t')}")
            if op == 10:    # Hello：启动心跳 + 发送 Identify
                interval = pkt.get("d", {}).get("heartbeat_interval", 45000) / 1000.0
                flog(f"Hello：心跳间隔 {interval}s，开始心跳并 Identify")
                self._start_heartbeat(conn, interval)
                self._identify(conn, token)
            elif op == 11:  # Heartbeat ACK
                flog("心跳 ACK（op11）已确认")
            elif op == 1:   # 服务器心跳探测：必须立即回一个心跳，否则服务器可能停止推事件
                try:
                    conn.send_text(json.dumps({"op": 1, "d": self._seq}))
                    flog("收到服务器心跳探测(op1)，已立即回心跳")
                except Exception as e:
                    flog(f"回心跳失败: {e}")
            elif op == 7:   # Reconnect：断开由外层重连
                print("[official] 收到 op7 重连指令", file=sys.stderr)
                flog("收到 op7 重连指令，断开等待外层重连")
                return
            elif op == 9:   # Invalid Session
                print("[official] op9 invalid session", file=sys.stderr)
                flog("收到 op9 invalid session")
                return
            elif op == 0:   # Dispatch 事件
                self._seq = pkt.get("s")
                self._on_event(conn, pkt, token)

    def _start_heartbeat(self, conn, interval):
        def hb():
            if self._stop.is_set():
                return
            try:
                conn.send_text(json.dumps({"op": 1, "d": self._seq}))
                flog(f"发送心跳(op1) seq={self._seq}")
            except Exception as e:
                flog(f"发送心跳失败: {e}")
            self._hb_timer = threading.Timer(interval, hb)
            self._hb_timer.daemon = True
            self._hb_timer.start()

        self._hb_timer = threading.Timer(interval, hb)
        self._hb_timer.daemon = True
        self._hb_timer.start()

    def _identify(self, conn, token):
        payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": self.intents,
                "shard": [0, 1],
                "properties": {
                    "$os": "linux", "$browser": "wm-bot", "$device": "wm-bot",
                },
            },
        }
        conn.send_text(json.dumps(payload, ensure_ascii=False))
        print("[official] 已发送 Identify，等待事件...", file=sys.stderr)
        flog("已发送 Identify，等待事件...")

    # ---- 事件分发 ----
    def _on_event(self, conn, pkt, token):
        t = pkt.get("t")
        d = pkt.get("d", {})
        # 被动回复用消息 ID（事件 d.id）。注意：event_id 仅支持特定事件类型，
        # 普通群@/单聊消息事件(GROUP_AT_MESSAGE_CREATE/C2C_MESSAGE_CREATE)必须用 msg_id。
        msg_id = d.get("id")
        flog(f"事件分发 t={t} msg_id={msg_id} d_keys={sorted(d.keys())}")
        if t == "GROUP_AT_MESSAGE_CREATE":
            raw = d.get("content", "")
            text = strip_mention(raw)
            group_openid = d.get("group_openid")
            flog(f"群@消息 group_openid={group_openid} 原始内容={raw!r} 剥离后={text!r}")
            ctx = {"channel": "group", "group_id": group_openid}
            self._dispatch_reply(token, "group", group_openid, text, ctx, msg_id)
        elif t == "C2C_MESSAGE_CREATE":
            raw = d.get("content", "")
            text = strip_mention(raw)
            openid = (d.get("author") or {}).get("user_openid")
            flog(f"单聊消息 user_openid={openid} 原始内容={raw!r} 剥离后={text!r}")
            ctx = {"channel": "private", "group_id": openid}
            self._dispatch_reply(token, "user", openid, text, ctx, msg_id)
        else:
            flog(f"未处理的事件类型 t={t}（已忽略）")

    def _dispatch_reply(self, token, kind, target_id, text, ctx, msg_id):
        if not target_id:
            return
        flog(f"进入回复分发 kind={kind} target_id={target_id} msg_id={msg_id} 文本={text!r}")
        try:
            reply = self.handler(text, ctx)
        except Exception as e:
            reply = f"⚠️ 处理出错：{e}"
            flog(f"handler 抛出异常: {e}")
        if not reply:
            flog("handler 返回空（未触发 WM/ZK 指令 或 白名单外群静默），不回复")
            return
        flog(f"handler 返回回复（前80字）: {reply[:80]!r}")
        ok, info = send_message(token, self.appid, self.api_base,
                                kind, target_id, reply, msg_id)
        if not ok:
            print(f"[official] 发送失败: {info}", file=sys.stderr)
            flog(f"发送失败: {info}")
        else:
            print(f"[official] 已回复 {kind} {target_id}", file=sys.stderr)
            flog(f"发送成功: {kind} {target_id}")


def main():
    bot_config.apply()    # 读同目录 data/config.txt；直接运行本文件时也生效
    wm.LOG_HOOK = flog    # 把缓存判定 / 刷新耗时 / 失败原因记进 data/bot_flow.log
    wm.reset_all_locks()   # 清掉上次可能残留的后台刷新锁
    appid = os.environ.get("WM_QQ_APPID")
    secret = os.environ.get("WM_QQ_SECRET")
    if not appid or not secret:
        print("[official] 缺少 WM_QQ_APPID / WM_QQ_SECRET。"
              "请把同目录 config.txt 模板复制到 data/config.txt 并填入凭证"
              "（到 https://q.qq.com 申请）。",
              file=sys.stderr)
        sys.exit(2)
    api_base = os.environ.get("WM_QQ_API_BASE", DEFAULT_API_BASE)
    try:
        intents = int(os.environ.get("WM_QQ_INTENTS", DEFAULT_INTENTS))
    except ValueError:
        intents = DEFAULT_INTENTS
    groups_file = os.environ.get("WM_QQ_GROUPS_FILE",
                                 str(qq_common.DEFAULT_GROUPS_FILE))
    allowed = qq_common.load_allowed_groups(groups_file)
    allow_all = bot_config.flag("WM_QQ_ALLOW_ALL_GROUPS")
    handler = qq_common.make_handler(allowed, allow_all=allow_all)

    bot = OfficialBot(appid, secret, handler, api_base=api_base, intents=intents)
    print(f"[official] 官方 QQ 机器人启动，appid={appid}，intents={intents}",
          file=sys.stderr)
    if allow_all:
        print("[official] ⚠️ WM_QQ_ALLOW_ALL_GROUPS 已设：监控【所有群】（调试用，"
              "正式运行请勿开启）", file=sys.stderr)
    elif allowed:
        print(f"[official] 群白名单已加载 {len(allowed)} 个：{sorted(allowed)}",
              file=sys.stderr)
    else:
        print("[official] 群白名单为空（未配置）：默认不监控任何群。"
              f"请在 {groups_file} 每行写入一个 group_openid。", file=sys.stderr)
    print("[official] 群内 @机器人 后发送「WM <物品名>」或「ZK <武器名>」即可查询；"
          "「/报错 WM <名称>」强制刷新。", file=sys.stderr)
    flog("=" * 40)
    flog(f"bot 启动 appid={appid} intents={intents} allow_all={allow_all}")
    flog(f"白名单文件={groups_file} 生效群数={len(allowed)} groups={sorted(allowed)}")
    bot.run_forever()


if __name__ == "__main__":
    main()
