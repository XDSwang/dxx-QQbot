#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
onebot.py — OneBot v11 传输层（纯 Python 标准库，零依赖）

支持两种拓扑：
  - client 模式（默认）：本脚本作为 WebSocket 客户端，连接 OneBot 实现
    （NapCat / Lagrange / go-cqhttp）暴露的 WS 地址（如 ws://127.0.0.1:6700）。
  - server 模式（反向 WS）：本脚本起 WebSocket 服务端，等 OneBot 主动连过来
    （手机跑 OneBot 客户端、反向连接本机时常用）。

仅用标准库（socket / struct / base64 / hashlib / ssl / threading），
桌面 / Termux / QPython 通用，无需 pip 安装任何依赖。

用法（见 qq_bot.py）：
    from onebot import OneBotClient
    c = OneBotClient(url="ws://127.0.0.1:6700", token="xxx", mode="client", handler=my_handler)
    c.run()

handler(text, ctx) -> str | None：返回要回复的文本；返回 None 表示不回复。
"""
import os
import sys
import json
import time
import struct
import socket
import base64
import hashlib
import threading
import urllib.parse

import config as bot_config   # 统一 wss 证书处理（手机端缺 CA 时自动兼容）

WSS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _make_frame(opcode, payload, mask):
    """构造一个 WebSocket 数据帧。mask=True 时按规范给负载加 4 字节掩码（客户端→服务端必须）。"""
    b = bytearray()
    b.append(0x80 | opcode)  # FIN=1 + opcode
    length = len(payload)
    mbit = 0x80 if mask else 0x00
    if length < 126:
        b.append(mbit | length)
    elif length < 65536:
        b.append(mbit | 126)
        b += struct.pack(">H", length)
    else:
        b.append(mbit | 127)
        b += struct.pack(">Q", length)
    if mask:
        key = os.urandom(4)
        b += key
        b += bytes(payload[i] ^ key[i % 4] for i in range(length))
    else:
        b += payload
    return bytes(b)


class WSConn:
    """一个已握手的 WebSocket 连接。client_mask=True 表示本端是客户端（发帧需掩码）。"""

    def __init__(self, sock, client_mask, initial=b""):
        self.sock = sock
        self.client_mask = client_mask
        # initial：握手时可能已把「101 响应 + 第一帧」一起读进缓冲区，
        # 那部分剩余字节从这里接上，否则第一帧（如 op10 Hello）会被吞掉。
        self._rbuf = bytes(initial or b"")
        self._lock = threading.Lock()

    def _read_exact(self, n):
        while len(self._rbuf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("socket closed")
            self._rbuf += chunk
        out = self._rbuf[:n]
        self._rbuf = self._rbuf[n:]
        return out

    def read_frame(self):
        """读取并解析一个帧，返回 (fin, opcode, payload)。已自动处理掩码解码。"""
        hdr = self._read_exact(2)
        fin = (hdr[0] & 0x80) != 0
        opcode = hdr[0] & 0x0F
        masked = (hdr[1] & 0x80) != 0
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        mask_key = self._read_exact(4) if masked else None
        payload = self._read_exact(length)
        if masked:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(length))
        return fin, opcode, payload

    def recv_message(self):
        """读取下一个文本消息；遇 ping 自动回 pong；返回 ("text", bytes) 或 ("close", bytes)。"""
        frags = []
        while True:
            fin, opcode, payload = self.read_frame()
            if opcode == 0x8:          # close
                return ("close", payload)
            if opcode == 0x9:          # ping -> 回 pong
                self.send_pong(payload)
                continue
            if opcode == 0xA:          # pong
                continue
            if opcode == 0x0:          # continuation（分片续帧）
                frags.append(payload)
                if fin:
                    return ("text", b"".join(frags))
            elif opcode == 0x1:        # text
                if fin:
                    return ("text", payload)
                frags = [payload]
            else:                      # 其它 opcode（binary 等）忽略
                if fin:
                    return ("data", payload)

    def send_text(self, text):
        with self._lock:
            self.sock.sendall(_make_frame(0x1, text.encode("utf-8"), mask=self.client_mask))

    def send_pong(self, payload=b""):
        with self._lock:
            self.sock.sendall(_make_frame(0xA, payload, mask=self.client_mask))

    def send_close(self):
        with self._lock:
            try:
                self.sock.sendall(_make_frame(0x8, b"", mask=self.client_mask))
            except Exception:
                pass

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def client_handshake(sock, host, port, path, token=None):
    """作为 WebSocket 客户端，向 OneBot WS 服务端发起 HTTP Upgrade 握手。

    返回「紧跟在 101 响应之后、已经被 recv 进来的剩余字节」——服务端常常在
    回完 101 后立刻发第一帧（QQ 网关的 op10 Hello / 有些 OneBot 实现的推送），
    这两段可能落在同一个 TCP 段、被同一次 recv 读走。以前这里把剩余字节直接
    丢掉，若 Hello 正好被吞掉，客户端就会一直等第一帧（90 秒读超时）才能重连，
    表现为「刚连上却迟迟没反应」。调用方必须把返回值交给 WSConn(initial=...)，
    否则会丢帧。
    """
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if token:
        lines.append(f"Authorization: Bearer {token}")
    req = "\r\n".join(lines) + "\r\n\r\n"
    sock.sendall(req.encode("utf-8"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手时连接关闭")
        resp += chunk
    head, _, leftover = resp.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("utf-8", "ignore")
    if "101" not in status_line:
        raise RuntimeError("WebSocket 握手失败: " + status_line)
    return leftover


def server_handshake(conn):
    """作为 WebSocket 服务端，响应 OneBot 客户端的握手请求（含 Sec-WebSocket-Accept）。"""
    req = b""
    while b"\r\n\r\n" not in req:
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError("握手时连接关闭")
        req += chunk
    key = None
    for line in req.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode("ascii")
    if not key:
        raise RuntimeError("缺少 Sec-WebSocket-Key")
    accept = base64.b64encode(
        hashlib.sha1((key + WSS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    conn.sendall(resp.encode("ascii"))


class OneBotClient:
    """OneBot v11 客户端/服务端。handler(text, ctx) -> str|None。"""

    def __init__(self, url=None, token=None, mode="client", handler=None):
        self.url = url or os.environ.get("WM_QQ_URL", "ws://127.0.0.1:6700")
        self.token = token if token is not None else os.environ.get("WM_QQ_TOKEN")
        self.mode = (mode or os.environ.get("WM_QQ_MODE", "client")).lower()
        self.handler = handler
        self._stop = threading.Event()
        self._echo = 0
        self._echo_lock = threading.Lock()
        self._srv = None
        self._conn = None   # 当前活跃连接（供主动发送使用）

    def stop(self):
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass

    def run(self):
        print(f"[onebot] 启动 OneBot {self.mode} 模式，地址={self.url}"
              + ("，token=已设" if self.token else "，token=无"), file=sys.stderr)
        while not self._stop.is_set():
            try:
                if self.mode == "server":
                    self._run_server()
                else:
                    self._run_client()
            except Exception as e:
                print(f"[onebot] 循环出错: {e}", file=sys.stderr)
            if self._stop.wait(3):   # 重连间隔；stop 时立即退出
                break

    def run_forever(self):
        try:
            self.run()
        except KeyboardInterrupt:
            self.stop()

    # ---- client 模式：脚本连 OneBot WS ----
    def _run_client(self):
        u = urllib.parse.urlparse(self.url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        print(f"[onebot] 连接 OneBot WS {host}:{port}{path}", file=sys.stderr)
        sock = socket.create_connection((host, port), timeout=10)
        if u.scheme == "wss":
            import ssl
            ctx = bot_config.ssl_context()
            if os.environ.get("WM_QQ_NO_VERIFY"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        leftover = client_handshake(sock, host, port, path, self.token)
        conn = WSConn(sock, client_mask=True, initial=leftover)
        self._loop(conn)

    # ---- server 模式：脚本起 WS 服务端，OneBot 反向连 ----
    def _run_server(self):
        u = urllib.parse.urlparse(self.url)
        host = u.hostname or "0.0.0.0"
        port = u.port or 6700
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(5)
        self._srv = srv
        print(f"[onebot] 反向 WS 服务端监听 {host}:{port}，等待 OneBot 连接...", file=sys.stderr)
        srv.settimeout(1.0)  # 让 stop() 能及时退出 accept
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                server_handshake(conn)
            except Exception as e:
                print(f"[onebot] 握手失败 {addr}: {e}", file=sys.stderr)
                conn.close()
                continue
            print(f"[onebot] OneBot 已连接：{addr}", file=sys.stderr)
            ws = WSConn(conn, client_mask=False)   # 服务端发帧不掩码
            try:
                self._loop(ws)
            except Exception as e:
                print(f"[onebot] 连接 {addr} 断开: {e}", file=sys.stderr)
            finally:
                ws.close()

    # ---- 收发主循环 ----
    def _loop(self, conn):
        self._conn = conn
        try:
            while not self._stop.is_set():
                typ, payload = conn.recv_message()
                if typ == "close":
                    try:
                        conn.send_close()
                    except Exception:
                        pass
                    break
                if typ != "text":
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                self._on_message(conn, msg)
        finally:
            self._conn = None

    def send_active(self, channel, target_id, text):
        """主动发送（供世界状态监控推送）：直接走 OneBot API（send_group_msg / send_private_msg）。"""
        if self._conn is None or not target_id:
            return False
        if channel == "group":
            action, params = "send_group_msg", {"group_id": target_id, "message": text}
        elif channel == "private":
            action, params = "send_private_msg", {"user_id": target_id, "message": text}
        else:
            return False
        with self._echo_lock:
            self._echo += 1
            e = self._echo
        self._conn.send_text(json.dumps(
            {"action": action, "params": params, "echo": e}, ensure_ascii=False))
        return True

    def _on_message(self, conn, msg):
        post_type = msg.get("post_type")
        if post_type == "meta_event":
            return  # 心跳 / 生命周期：无需回应
        if post_type != "message":
            return
        text = msg.get("message", "")
        if isinstance(text, list):   # OneBot 11 的 message 可能是数组（CQ 段）
            text = "".join(
                s.get("text", "") for s in text
                if isinstance(s, dict) and s.get("type") == "text"
            )
        if self.handler is None:
            return
        try:
            reply = self.handler(text, msg)
        except Exception as e:
            reply = f"⚠️ 处理出错：{e}"
        if reply:
            self._send_reply(conn, msg, reply)

    def _send_reply(self, conn, msg, reply):
        message_type = msg.get("message_type")
        pngs = list(getattr(reply, "pngs", None) or [])
        tail = str(getattr(reply, "tail", "") or "").strip()
        if pngs:
            # OneBot 11：message 传数组，图片用 base64://（免图床、免公网地址）
            segs = []
            for png in pngs[:4]:
                try:
                    segs.append({"type": "image", "data": {
                        "file": "base64://" + base64.b64encode(png).decode("ascii")}})
                except Exception:
                    segs = []
                    break
            if segs and tail:
                # 同一条消息里，图片段后跟可复制的 /w 指令文本段
                segs.append({"type": "text", "data": {"text": tail}})
            params = ({"message": segs} if segs
                      else {"message": str(reply) + (("\n" + tail) if tail else "")})
        else:
            params = {"message": reply}
        if message_type == "group":
            params["group_id"] = msg.get("group_id")
            action = "send_group_msg"
        elif message_type == "private":
            params["user_id"] = msg.get("user_id")
            action = "send_private_msg"
        else:
            return
        with self._echo_lock:
            self._echo += 1
            e = self._echo
        payload = json.dumps(
            {"action": action, "params": params, "echo": e}, ensure_ascii=False
        )
        conn.send_text(payload)


if __name__ == "__main__":
    # 独立运行仅做握手连通性自检（需提供一个真实 OneBot 地址）
    url = sys.argv[1] if len(sys.argv) > 1 else None
    c = OneBotClient(url=url, mode=os.environ.get("WM_QQ_MODE", "client"))
    c.run_forever()
