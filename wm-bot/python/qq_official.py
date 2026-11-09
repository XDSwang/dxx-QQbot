#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qq_official.py — 把 WM 查询接入【官方 QQ 机器人】（q.qq.com，无封号风险）

与 OneBot（社区逆向方案）不同，官方机器人走腾讯正规开放平台：
  - 鉴权：POST https://bots.qq.com/app/getAppAccessToken → access_token（7200s，需自刷）
  - 网关：GET https://api.bot.qq.com/gateway → wss://api.bot.qq.com/websocket/
  - 事件（WebSocket，应用层 JSON 帧，全部属于 GROUP_AND_C2C_EVENT = 1<<25）：
      GROUP_AT_MESSAGE_CREATE  群 @机器人 消息
      GROUP_MESSAGE_CREATE     群消息全量模式（免 @，需在群资料页把
                               「机器人可获取的群聊消息范围」设为「获取群内全部消息」）
      C2C_MESSAGE_CREATE       单聊消息
      GROUP_MSG_RECEIVE / C2C_MSG_RECEIVE   群管理员 / 用户开启「主动发言」通知
      GROUP_MSG_REJECT / C2C_MSG_REJECT     关闭该通知
      GROUP_ADD_ROBOT / GROUP_DEL_ROBOT / FRIEND_ADD / FRIEND_DEL   关系变更
  - 发消息（REST）：
      POST /v2/groups/{group_openid}/messages   （群）
      POST /v2/users/{openid}/messages          （单聊）
    头：Authorization: QQBot {token}  +  X-Union-Appid: {appid}

要点：
  - group_openid 就是【普通 QQ 群】的标识（字符串形式，不是数字群号）；
    2026 年腾讯官方机器人已支持直接进普通群（群主把机器人拉进群即可，对应 GROUP_ADD_ROBOT 事件），并非只能用频道；
  - 群资料页的两个开关决定能力，且都【按群 / 按人】单独设置：
      ①「获取群内全部消息」→ 推 GROUP_MESSAGE_CREATE，群友直接发 "wm boltor" 即可，不用 @；
      ②「机器人主动在群聊内发言」→ 开启后 send_active 才能成功（否则 40034105）。
    未开 ① 时只收 @消息（GROUP_AT_MESSAGE_CREATE，content 带 "<@!bot_userid>" 前缀）；
    两种事件都走同一套 handler，strip_mention 统一剥 @占位符。
  - 发消息：@ 消息走被动回复（带 msg_id = 事件 d.id，5 分钟内有效、不占主动配额）；
    全量群消息（免 @）默认【不带 msg_id】单纯发一条普通群消息，不 @ 任何人、不显示引用
    （WM_FULLMSG_ACTIVE=0 可切回被动），这就是「wm 物品名 不用 @ 就回」的效果；
    两条通道失败互相回退，且因为每条群消息都能回，暂存监控可借任意聊天消息带出；
    event_id 仅支持 INTERACTION_CREATE / GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE 等事件，
    普通群@/单聊消息误用会报 40034027「请求参数event_id对应事件不能回复消息」；
  - 主动消息频控：单关系 20 qpm、每群每天 1000 条，因此监控推送做了
    「内容不变不重发 + WS_ACTIVE_MIN_GAP 最小间隔」两道闸，别把 10 秒当发送周期；
  - 交易回复只发【一条消息】。默认走 Markdown（msg_type=2）：一张完整卡片图
    （图里已有标题 / 行情 / 排序 / 订单 / 来源）+ 图片下方英中两条可复制 /w 字符串。
    ★ 文字区绝不重复正文：markdown.content 只有图片标签 + 两行 /w；
    只有「文字 xxx」纯文字回退才输出完整正文 + /w + 来源链接。
    图片【首选】固定公网 URL + 覆盖式上传：网盘 WebDAV / R2 / GitHub / Gitee 里
    永远只有一张 card.png，每次覆盖写，URL 不变、不占免费额度、不攒过期素材，
    客户端按图片标签自动解析；没配置才退回官方【素材管理】换 qqbot-media://。
    不需要模板审核（2026/04/23 起自定义 Markdown 全量开放）。
    WM_QQ_MARKDOWN=0 时改走富媒体 msg_type=7 + content 图文混排：把 /w 指令并进
    最后一张图片消息，图上列表 + 图下可复制文字同属一条；服务端不支持混排
    （如 22006）时自动改发纯图并单独补一条文本，/w 指令绝不丢；
    WM_QQ_MIX_MESSAGE=0 可强制用两条；
  - Markdown 通道（默认开，native）：template 模式用 custom_template_id + params
    八键（后台审核过的模板才需要 ID）；Markdown 文字本身【没有颜色语法】，
    「游戏中红 / 在线蓝 / 离线灰」是卡片图里画出来的，图不出来颜色就没了。
    **图片引用拿不到就回退富媒体发图，绝不发无图纯文字**；发送失败同样回退。
    真机自检：python md_probe.py（dry-run）/ --send --group <openid> --image overwrite。
    详见 qq_markdown.py。
  - 全部纯标准库（urllib/ssl/socket/threading），桌面 / Termux / QPython 通用。

配置（统一写在同目录 data/config.txt，每行「名称 = 值」，文件内有中文注释）：
  WM_QQ_APPID            在 q.qq.com 管理端获得的 appid（必填）
  WM_QQ_SECRET           clientSecret（必填，等同账号凭证，勿外泄 / 勿提交）
  WM_QQ_API_BASE         OpenAPI 根地址（默认 https://api.bot.qq.com）
  WM_QQ_GROUPS_FILE      群白名单文件（默认：脚本同目录 data/groups.txt；相对路径也按同目录解析）
  WM_QQ_ALLOW_ALL_GROUPS 0 = 只监控白名单群（正式）；1 = 临时监控所有群（仅调试）
  WM_QQ_INTENTS          事件订阅位掩码（默认 33554432 = 1<<25，已含上面全部事件，
                         免 @ / 主动发言是群资料页开关，不需要改这里）
  WM_FLOW_LOG_DAYS       流程日志保留天数（单位天，默认 7；可填 3 / 1，0 = 不清理）

群白名单：
  - 默认不监控任何群；只有 data/groups.txt 里写进的 group_openid 才回复；
  - 白名单群内，仅命中 WM / ZK / 世界状态 / 帮助 等指令才回复，闲聊静默不刷屏；
  - 单聊不受白名单限制。

运行（整个文件夹拷到任意目录，配置写进 data/config.txt 后）：
  python qq_bot.py          （Windows / 手机 Termux / QPython 都一样）

凭证安全：appid/secret 从 data/config.txt 读取；本脚本不写日志落盘、不打印 secret。
"""
import os
import re
import sys
import json
import time
import ssl
import socket
import base64
import queue
import threading
import urllib.parse
import urllib.request
import urllib.error

import config as bot_config

# 复用 onebot 的 WebSocket 帧编解码 / 客户端握手（纯标准库）
from onebot import WSConn, client_handshake

import qq_common
try:                       # 可选通道：手机漏拷这个文件也不能让整个机器人起不来
    import qq_markdown
except ImportError:        # pragma: no cover - 取决于部署文件是否齐全
    qq_markdown = None
import drops
import wm

# ---------------- 按天保留的运行流程日志 ----------------
# 当天仍写固定 data/bot_flow.log，但改成追加（重启不再清空，下午的证据不丢）；
# 跨天把旧文件归档成 data/bot_flow-YYYY-MM-DD.log，超出保留天数的归档自动清理。
# 参数 WM_FLOW_LOG_DAYS：单位「天」，可填 7 / 3 / 1；0 = 永不清理；缺省 7。
# ⭐异常规范代码 | 用户：dxx | 日期：2026-11-09 | 违反：通用规范 1.3（流程日志按日期滚动生成多份） |
# 原因：用户明确要求按天保留、可设保留天数；非临时实现，同按 1.4 锁进本组函数 |
# 隔离：仅本组函数内，归档命名 / 日期判定 / 清理都由统一入口提供，不扩散到业务代码。
import datetime
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_LOG_PATH = os.path.join(LOG_DIR, "bot_flow.log")
os.makedirs(LOG_DIR, exist_ok=True)   # 全新拷贝时 data/ 可能还不存在
FLOW_LOG_PREFIX = "bot_flow-"
FLOW_LOG_SUFFIX = ".log"
FLOW_LOG_KEEP_DAYS = 7
FLOW_LOG_KEEP_KEY = "WM_FLOW_LOG_DAYS"
_flow_log_lock = threading.Lock()
_flow_log_day = None


def flow_log_day(timestamp):
    """时间戳归属的本地日期串 YYYY-MM-DD。"""
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def flow_log_archive_name(day, prefix=FLOW_LOG_PREFIX):
    """某天的归档文件名：统一入口，别处不许自己拼；prefix 区分不同日志。"""
    return "%s%s%s" % (prefix, day, FLOW_LOG_SUFFIX)


def flow_log_archived_day(name, prefix=FLOW_LOG_PREFIX):
    """从归档名取日期串；不是本程序生成的归档返回空串，避免误删别的文件。"""
    if not (name.startswith(prefix) and name.endswith(FLOW_LOG_SUFFIX)):
        return ""
    day = name[len(prefix):-len(FLOW_LOG_SUFFIX)]
    if len(day) != 10:
        return ""
    try:
        datetime.date(int(day[0:4]), int(day[5:7]), int(day[8:10]))
    except ValueError:
        return ""
    return day


def flow_log_keep_days():
    """保留天数（单位天）：非法值 / 负数回退缺省 7，0 = 不清理。"""
    try:
        days = int(str(bot_config.get(FLOW_LOG_KEEP_KEY, FLOW_LOG_KEEP_DAYS)).strip())
    except (TypeError, ValueError):
        return FLOW_LOG_KEEP_DAYS
    return days if days >= 0 else FLOW_LOG_KEEP_DAYS


def flow_log_cutoff(today, keep_days):
    """保留窗口的起始日期串（含当天），早于它的归档算超期。"""
    d = datetime.date(int(today[0:4]), int(today[5:7]), int(today[8:10]))
    return (d - datetime.timedelta(days=int(keep_days) - 1)).isoformat()


def flow_log_prune(directory, today, keep_days, prefix=FLOW_LOG_PREFIX):
    """删除超期归档，返回删掉的文件名列表；只认本组生成的对应前缀归档名。"""
    if int(keep_days) <= 0 or not os.path.isdir(directory):
        return []
    cutoff = flow_log_cutoff(today, keep_days)
    removed = []
    for name in sorted(os.listdir(directory)):
        day = flow_log_archived_day(name, prefix)
        if day and day < cutoff:
            try:
                os.remove(os.path.join(directory, name))
                removed.append(name)
            except OSError:
                pass
    return removed


def _flow_log_archive(path, archive):
    """把未归档日志并入归档文件；同名归档已存在时追加而不覆盖。"""
    try:
        if os.path.exists(archive):
            with open(path, "rb") as src, open(archive, "ab") as dst:
                dst.write(b"\n")
                dst.write(src.read())
            os.remove(path)
        else:
            os.replace(path, archive)
    except OSError:
        pass


def _flow_log_rotate(now):
    """跨天归档 + 超期清理；每次 flog 调用，同一天只做一次目录扫描。"""
    global _flow_log_day
    today = flow_log_day(now)
    with _flow_log_lock:
        if _flow_log_day == today:
            return today
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.isfile(_LOG_PATH):
            old_day = flow_log_day(os.path.getmtime(_LOG_PATH))
            if old_day != today:
                _flow_log_archive(
                    _LOG_PATH, os.path.join(LOG_DIR, flow_log_archive_name(old_day)))
        flow_log_prune(LOG_DIR, today, flow_log_keep_days())
        _flow_log_day = today
    return today


def flog(msg, now=None):
    """写流程日志：当天追加、跨天归档；每条写完立即 flush，程序被冻结也不丢。"""
    try:
        now = time.time() if now is None else now
        _flow_log_rotate(now)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S", time.localtime(now)), msg))
            f.flush()
    except Exception:
        pass


# ---------------- 消息链路记录（主链三层 + 网关辅助层） ----------------
# 独立于 bot_flow.log 的链路记录：手机熄屏冻结 / 进程被结束时，要能看出
# 「到底卡在哪一层」。故每条单独开写、立刻 flush + fsync，不进 logging 缓冲。
# 主链三层（收到/组装/已发送）顺序固定；「连接」「静默」是辅助诊断层：
# 网关假死时主链一条都没有，靠连接层才能区分「进程没起来」和「网关不推事件」。
# 同样按天归档成 selfcheck_trace-YYYY-MM-DD.log，与 bot_flow 共用 WM_FLOW_LOG_DAYS。
# ⭐异常规范代码 | 用户：dxx | 日期：2026-11-09 | 违反：通用规范 1.3（按日期滚动生成多份日志） |
# 原因：用户明确要求链路记录单独成文件、逐条落盘且不能无限增长；同按 1.4 锁进本组函数 |
# 隔离：仅本组函数内，归档命名 / 日期判定 / 清理都由统一入口提供。
TRACE_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "selfcheck_trace.log")
TRACE_STAGES = ("收到", "组装", "已发送", "连接", "静默")
TRACE_LOG_PREFIX = "selfcheck_trace-"
_trace_log_day = None


def _trace_log_rotate(now):
    """链路日志跨天归档 + 超期清理：与 bot_flow 同参数、同命名规则，只换前缀。"""
    global _trace_log_day
    today = flow_log_day(now)
    with _flow_log_lock:
        if _trace_log_day == today:
            return today
        directory = os.path.dirname(TRACE_LOG_PATH)
        os.makedirs(directory, exist_ok=True)
        if os.path.isfile(TRACE_LOG_PATH):
            old_day = flow_log_day(os.path.getmtime(TRACE_LOG_PATH))
            if old_day != today:
                _flow_log_archive(TRACE_LOG_PATH, os.path.join(
                    directory, flow_log_archive_name(old_day, TRACE_LOG_PREFIX)))
        flow_log_prune(directory, today, flow_log_keep_days(), TRACE_LOG_PREFIX)
        _trace_log_day = today
    return today

# 网关会话健康（2026-11-09 手机实测：进程活着、主动广播正常，但一下午收不到
# 任何群消息；时好时坏）。根因：读循环只有 sock.settimeout(90) 兜底，而心跳
# Timer 每约 45s 发 op1、服务端回 op11 ACK —— 只要 ACK 还在，读超时永远不触发；
# 服务端「还回 ACK 但不再推事件」时连接永久假死，run.py 的 alive() 也看不出来。
# 两道自愈：① 连续 N 个心跳周期无任何帧/ACK → 主动断开重连；
#          ② 会话超过上限秒数主动软重连，兜底「只回 ACK 不推事件」。
GATEWAY_STALE_FACTOR = 2.0         # 允许连续几个心跳周期收不到 ACK / 任意帧
GATEWAY_SESSION_MAX_SECONDS = 600  # 会话最长存活秒数（软重连），WM_QQ_SESSION_MAX 可覆盖，0=关
GATEWAY_PROOF_SECONDS = 300        # 网关存活存证写入间隔（证明进程活着、网关有帧）


def gateway_is_stale(last_frame_at, last_ack_at, now, interval, factor):
    """参数进返回值出：连续 factor 个心跳周期没收到任何帧 / ACK 即判假死。

    还没收到过任何帧（时间戳为 0）时不判死，避免刚连上就被误杀。
    """
    last = max(float(last_frame_at or 0.0), float(last_ack_at or 0.0))
    return last > 0 and (now - last) > float(interval) * float(factor)


def session_expired(started_at, now, max_seconds):
    """会话是否到期需要软重连；max_seconds<=0 表示该机制已关闭。"""
    return float(max_seconds) > 0 and float(started_at) > 0 \
        and (now - float(started_at)) > float(max_seconds)


def trace_chain(stage, detail, raw=None):
    """链路记录，逐条追加、立刻刷盘；异常静默返回 False，绝不卡住消息主流程。

    raw 是「代码层原样、未被客户端渲染处理」的完整内容（图片链接原样、不截断、
    不做展示层替换），单独成块写出，方便整段复制出来验证链接与拼接。
    """
    if stage not in TRACE_STAGES:
        return False
    head = "%s | %s | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), stage, detail)
    if raw is not None:
        head += "---- 完整内容（代码层原样，未渲染）----\n" \
                + str(raw) + "\n---- 完整内容结束 ----\n"
    try:
        _trace_log_rotate(time.time())
    except Exception:
        pass                       # 归档失败也要照常写当天记录，绝不丢链路证据
    try:
        with open(TRACE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(head)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:
        return False


def _mk_raw(md):
    """取 markdown 对象的代码层原文：native 直接取 content；template 取整份 JSON。"""
    if isinstance(md, dict) and "content" in md:
        return md["content"]
    return json.dumps(md, ensure_ascii=False, sort_keys=True)


DEFAULT_API_BASE = "https://api.bot.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
# 1 << 25 = GROUP_AND_C2C_EVENT：群@、群全量消息、单聊、通知开关、进退群、好友变更
# 全都在这一位里，免 @ / 主动发言是群资料页开关，不需要改 intents。
DEFAULT_INTENTS = 33554432

# 主动推送失败日志的节流时间戳（只影响日志，不影响逻辑）
_active_fail_at = [0.0]


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
                with bot_config.urlopen(req, timeout=10) as resp:
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
        with bot_config.urlopen(req, timeout=10) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"
    except Exception as e:
        return False, str(e)


def broadcast_to_groups(appid, secret, api_base, groups, text, kind="group"):
    """给一组授权群逐个主动推送一条纯文字系统通知（不依赖 bot_config/config 模块）。

    用于「服务端更新前/后」给所有授权群广播暂停/恢复服务。逐群发送，
    单群失败（如该群没开「机器人主动在群聊内发言」开关）只记日志、
    不中断其余群；全部失败返回 (sent, failed)。发送走与查询回复同一套
    send_message（被动窗口之外，故不带 msg_id，纯主动推送）。
    """
    if not groups:
        return 0, 0
    try:
        mgr = TokenManager(appid, secret, api_base)
        token = mgr.get_token()
    except Exception as e:
        flog(f"广播取 token 失败: {e}")
        return 0, len(groups)
    sent = 0
    failed = 0
    for gid in groups:
        ok, info = send_message(token, appid, api_base, kind, gid, text, msg_id=None)
        if ok:
            sent += 1
            flog(f"广播成功: {kind} {gid}")
        else:
            failed += 1
            flog(f"广播失败: {kind} {gid} {info}")
    return sent, failed


# ---------------- 发图片（富媒体：先上传拿 file_info，再 msg_type=7） ----------------
def _api_get(token, appid, api_base, path, timeout=10):
    """统一 GET，返回 (ok, resp_dict_or_err)。失败不抛异常（与 _api_post 对称）。"""
    url = api_base.rstrip("/") + path
    req = urllib.request.Request(
        url, method="GET",
        headers={
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": str(appid),
        },
    )
    try:
        with bot_config.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(raw)
            except ValueError:
                return True, {"raw": raw}
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"
    except Exception as e:
        return False, str(e)


def _api_post(token, appid, api_base, path, body, timeout=15):
    """统一 POST，返回 (ok, resp_dict_or_err)。失败不抛异常。"""
    url = api_base.rstrip("/") + path
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
        with bot_config.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(raw)
            except ValueError:
                return True, {"raw": raw}
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"
    except Exception as e:
        return False, str(e)


def upload_image(token, appid, api_base, target_kind, target_id, png,
                 file_name="card.png"):
    """上传图片，返回 (file_info, err)。

    走「二进制直传」：官方 /files 接口的 file_data 字段，≤5MB 一次搞定，
    比分片上传（prepare → PUT → part_finish → 合并，4 次请求）省 3 次往返。
    单聊和群聊的上传接口隔离，不能混用。
    """
    path = (f"/v2/groups/{target_id}/files" if target_kind == "group"
            else f"/v2/users/{target_id}/files")
    body = {
        "file_type": 1,                       # 1=图片（png/jpg）
        "file_name": file_name,
        "file_data": base64.b64encode(png).decode("ascii"),
    }
    ok, info = _api_post(token, appid, api_base, path, body, timeout=25)
    if not ok:
        return None, info
    if isinstance(info, dict) and info.get("file_info"):
        return info["file_info"], None
    return None, str(info)[:300]


def send_image(token, appid, api_base, target_kind, target_id, file_info,
               msg_id=None, content=None):
    """用上一步的 file_info 发图片消息（msg_type=7）。

    content 非空 = 图文混排：官方 OpenAPI 允许 msg_type=7 的【图片】消息同时带
    content，客户端把文字显示在图片同属的一条消息里（其它文件类型会忽略 content）。
    这样「行情卡片 + /w 指令」就只占一条消息，而不是两条。
    """
    path = (f"/v2/groups/{target_id}/messages" if target_kind == "group"
            else f"/v2/users/{target_id}/messages")
    body = {"msg_type": 7, "media": {"file_info": file_info}}
    if msg_id:
        body["msg_id"] = msg_id
    if content:
        body["content"] = content
    return _api_post(token, appid, api_base, path, body, timeout=15)


def mixed_message_enabled():
    """WM_QQ_MIX_MESSAGE：默认开。关掉就退回「图一条 + 文本一条」的老行为。"""
    return (os.environ.get("WM_QQ_MIX_MESSAGE") or "1").strip().lower() \
        not in ("0", "false", "no", "off", "none")


class _ImageSendResult:
    """发图结果：可当 bool 用（兼容旧的 True/False 判断），另带 mixed 标记。

    mixed=True 表示 tail 已随图片一起发出，上层不用再单独补发文本。
    """

    def __init__(self, ok, mixed=False):
        self.ok = bool(ok)
        self.mixed = bool(mixed)

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"_ImageSendResult(ok={self.ok}, mixed={self.mixed})"


# ---------------- 剥离 @ 占位符 ----------------
# 群聊 @ 占位符共四种形态（均在真实事件里出现过）：
#   <@openid>    @普通成员
#   <@all>       @全体成员
#   <@!bot_id>   @机器人（GROUP_AT_MESSAGE_CREATE）
#   <@//id/>     @机器人（GROUP_MESSAGE_CREATE 全量消息里的新形态，斜杠包裹）
# 注意 \w 匹配不到 "/"，所以 `<@//id/>` 必须单独列一支，否则既剥不掉也认不出。
_MENTION_RE = re.compile(r"<@//[^>]*/>|<@!?\w+>")
# 「这条消息 @ 的是不是机器人」：带叹号的，或全量消息里的 <@//id/> 形态。
# 后者按实测样本（@机器人 必带此形态）判定；若将来发现它也用于 @成员，
# 再改成用机器人自身 openid 精确比对（at_me 只影响免前缀与是否 @ 回去，
# 不影响白名单，误判不会让白名单外的群被回复）。
_BOT_MENTION_RE = re.compile(r"<@//[^>]*/>|<@!\w+>")


def strip_mention(text):
    """去掉消息里的 @占位符 "<@!xxx>" / "<@xxx>" / "<@all>"。

    群@消息（GROUP_AT_MESSAGE_CREATE）content 以 "<@!机器人id>" 开头；
    群消息全量模式（GROUP_MESSAGE_CREATE）content 是纯文本，但 @某成员时会替换成
    "<@成员openid>"、@全体成员替换成 "<@all>"。统一剥掉这些占位符，剩下的才是
    真正要判定的文字。
    """
    t = _MENTION_RE.sub("", text or "")
    return t.strip()


def _text_mentions_bot(raw, bot_name):
    """开头是不是明文的「@机器人昵称」。

    官方占位符（`<@!openid>`）是标准形态，但有的客户端 / 全量消息模式下，
    「@机器人」会以纯文字「@昵称」发过来 —— 这时只能拿自己的昵称去对。
    只看开头（与 qq_common.strip_mentions 的口径一致），句中 / 句尾不算。
    """
    if not bot_name:
        return False
    t = (raw or "").lstrip()
    return t.startswith("@" + bot_name) or t.startswith("＠" + bot_name)


def _sender_ids(author):
    """发送者身份，返回 (user_openid, member_openid, 昵称)。

    群聊事件里 member_openid 才有值（user_openid 是单聊场景用的），而官方
    @ 用户标签的 id 只认 member_openid —— 取错字段就会 @ 不出去。
    昵称（username）只用来打日志，拿不到就是空串。
    """
    a = author or {}
    return (str(a.get("user_openid") or "").strip(),
            str(a.get("member_openid") or "").strip(),
            str(a.get("username") or "").strip())


def reply_at_tag(ctx, kind):
    """回复要不要 @ 回去：只在「这条消息 @ 了机器人」时给标签，其余一律不 @。

    官方 @ 用户标签形如 `<qqbot-at-user id="member_openid" />`，文档明确它
    只支持 markdown 消息（文本 / 富媒体消息塞进去会原样显示成文字），所以
    标签只在 Markdown 通道用；id 必须是群成员 member_openid。
    群聊 @ 消息（GROUP_AT_MESSAGE_CREATE）天然算「@了我」；全量消息
    （GROUP_MESSAGE_CREATE）由入站时判定 at_me 决定 —— 两者共用同一条判定线。
    """
    if kind != "group" or not (ctx or {}).get("at_me"):
        return ""
    c = ctx or {}
    # 官方 @ 标签只认 member_openid；但个别事件里它可能为空（只剩 user_openid），
    # 此时回退到入站时记下的 user_id，否则就会出现「@ 了机器人，回复却不 @ 我」。
    mid = str(c.get("member_openid") or c.get("user_id") or "").strip()
    return ('<qqbot-at-user id="%s" />' % mid) if mid else ""


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
        # 网关会话健康状态（读循环单线程写、心跳线程只读；浮点赋值本身原子）
        self._hb_generation = 0       # 心跳链代次：重连后旧链按代次自行退出
        self._last_frame_at = 0.0     # 最后一次收到服务端任意帧
        self._last_ack_at = 0.0       # 最后一次收到心跳 ACK
        self._last_event_at = 0.0     # 最后一次收到 op0 dispatch 消息事件
        self._session_started_at = 0.0
        self._last_proof_at = 0.0     # 上一次写存活存证
        try:
            self._session_max = float(os.environ.get(
                "WM_QQ_SESSION_MAX", GATEWAY_SESSION_MAX_SECONDS))
        except (TypeError, ValueError):
            self._session_max = float(GATEWAY_SESSION_MAX_SECONDS)
        # 用户查询的异步处理：每个会话一条串行队列 + 一个工作线程。
        # 只有真的跑起网关主循环（_loop）时才开启；测试里直接调 _dispatch_reply
        # 仍是同步执行，断言不受影响。
        self._async_replies = False
        self._reply_queues = {}
        self._reply_threads = {}
        self._reply_qlock = threading.Lock()
        # 机器人自己的名字（GET /users/@me 自动取）；有的客户端把「@机器人」发成
        # 明文「@昵称」而不是占位符，没有它就没法判断「@ 的是不是我」。
        self.bot_name = ""
        self._self_tries = 0

    def stop(self):
        self._stop.set()
        self._cancel_heartbeat()

    def _cancel_heartbeat(self):
        """取消当前心跳 Timer，并让旧链在下次触发时按代次自行退出。

        旧链若不收干净，会一直持有上一次连接的 conn，每 45s 往死 socket 发帧
        （异常被吞）并无限续期；重连次数越多僵尸线程越多。
        """
        self._hb_generation += 1
        t = self._hb_timer
        self._hb_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def send_active(self, kind, target_id, text):
        """主动推送（不带 msg_id）：用于世界状态定时监控。

        前提：群管理员 / 用户在机器人资料页打开「机器人主动在群聊内发言」开关
        （对应 GROUP_MSG_RECEIVE / C2C_MSG_RECEIVE 事件），否则固定 40034105。
        结构：与 WM 交易卡片同一套单条 Markdown 通道，但把【文字标签放在图片上方】
        —— 交易卡是「图在上、字在下」，监控反过来「字在上、图在下」，一条消息搞定。
        做法：原生 Markdown（msg_type=2）内容里先写标签文字、再写图片标签即可，
        QQ 自定义 Markdown 允许文字排在图片前面（不像富媒体 caption 永远在图下），
        因此「单条消息 + 字在上」两者可同时成立，原图完全不变。
        优先单条 Markdown；Markdown 不可用 / 没拿到图片引用 / 发送失败时回退：
          两条消息（文字标签在上 + 卡片图在下，保持「字在上」意图）→ 纯文本。
        两者都失败才返回 False，由 ws_monitor 暂存、等本会话有人发消息时随回复带出。
        失败日志按 5 分钟节流。
        """
        if not target_id:
            return False
        try:
            from ws_monitor import monitor_label
            label = monitor_label(text)
        except Exception:
            label = ""
        try:
            token = self.token_mgr.get_token()
        except Exception as e:
            flog(f"主动推送取 token 失败: {e}")
            return False
        mode = (os.environ.get("WM_WS_CARD") or "always").strip().lower()
        if mode not in ("off", "0", "false", "no", "none"):
            try:
                import render_card
                pngs = render_card.render(text, title="星际战甲 · 监控推送")
            except Exception:
                pngs = None
            if pngs:
                # 单条 Markdown：文字标签在上、卡片图在下（与交易卡反过来，占一条消息）
                if self._send_active_markdown(token, kind, target_id, pngs, label):
                    _active_fail_at[0] = 0.0
                    flog(f"主动推送(单条Markdown·字在上)成功: {kind} {target_id} "
                         f"{label or '【监控信息如下】'} 张数={len(pngs)}")
                    print(f"[official] 主动推送 单条Markdown(字在上) {kind} {target_id} "
                          f"{label or '【监控信息如下】'}（{len(pngs)} 张）", file=sys.stderr)
                    return True
                # Markdown 没成（没图引用 / 被拒 / 关了）：回退两条消息（字在上+图在下），
                # 保证「字在上」的意图在兜底时也不丢；多页卡片图也能全部发出
                label_sent = False
                if label:
                    ok_l, _ = send_message(token, self.appid, self.api_base,
                                          kind, target_id, label, msg_id=None)
                    label_sent = ok_l
                res = self._reply_images(token, kind, target_id, pngs, None,
                                         content=None)
                if res and res.ok:
                    _active_fail_at[0] = 0.0
                    flog(f"主动推送(两条·字在上)成功: {kind} {target_id} "
                         f"{label or '【监控信息如下】'} 张数={len(pngs)}")
                    print(f"[official] 主动推送 两条(字在上) {kind} {target_id} "
                          f"{label or '【监控信息如下】'}（{len(pngs)} 张）", file=sys.stderr)
                    return True
                # 图片也没发成：补发纯文本；标签若已发出则省略，避免重复
                fb = text if label_sent else ((label + "\n\n" + text) if label else text)
                ok2, info2 = send_message(token, self.appid, self.api_base,
                                          kind, target_id, fb, msg_id=None)
                if ok2:
                    _active_fail_at[0] = 0.0
                    return True
                flog("主动推送图片与文本均失败")
        # 回退纯文本（卡片模式关 / 渲染失败 / 上面都没成功）
        body = (label + "\n\n" + text) if label else text
        ok, info = send_message(token, self.appid, self.api_base,
                                kind, target_id, body, msg_id=None)
        if not ok:
            now = time.time()
            if now - _active_fail_at[0] >= 300:
                _active_fail_at[0] = now
                flog(f"主动推送失败（转为随消息附带）: {info}")
        else:
            _active_fail_at[0] = 0.0
            flog(f"主动推送文本成功: {kind} {target_id}")
        return ok

    def _send_active_markdown(self, token, kind, target_id, pngs, label):
        """单条 Markdown 主动推送：文字标签在上、卡片图在下（与交易卡反过来）。

        复用 qq_markdown 的原生 Markdown 通道（msg_type=2）：内容里先写标签文字、
        再逐个写图片标签，即可实现「字在上、图在下」且只占一条消息，原图不变。
        多页卡片逐张上传并依次排在标签下方。无图引用 / Markdown 关闭 / 发送失败
        时返回 False，由 send_active 回退两条消息或纯文本。
        """
        if qq_markdown is None or not qq_markdown.enabled():
            return False
        try:
            tags = []
            for png in pngs[:4]:
                url = qq_markdown.resolve_image_url(
                    [png], token, self.appid, self.api_base)
                if not url:
                    flog("主动推送 Markdown 未取得图片引用，回退两条消息")
                    return False
                tags.append(f"![img {qq_markdown.image_markup_size([png])}]({url})")
        except Exception as e:
            flog(f"主动推送 Markdown 取图失败: {e}")
            return False
        # 文字在上、图在下：原生 Markdown 先写标签，再写图片标签
        md = ((label.strip() + "\n\n") if label and label.strip() else "") \
            + "\n".join(tags)
        try:
            ok, info = qq_markdown.send(token, self.appid, self.api_base, kind,
                                        target_id, {"content": md}, msg_id=None)
        except Exception as e:
            flog(f"主动推送 Markdown 发送异常: {e}")
            return False
        if not ok:
            flog(f"主动推送 Markdown 发送失败({info})，回退两条消息")
        return bool(ok)

    def run_forever(self):
        # 启动即落一条追加式存证：日志里连这行都没有 = 进程没起来 / 被冻结，
        # 有这行但一直没有「收到」= 网关没推事件，两者一眼分开。
        trace_chain("连接", "进程已启动，进入 run_forever（准备连接网关）")
        try:
            while not self._stop.is_set():
                try:
                    self._connect_loop()
                except Exception as e:
                    print(f"[official] 连接/处理出错: {e}", file=sys.stderr)
                    flog(f"连接/处理出错，准备重连: {e}")
                    trace_chain("连接", "连接异常，准备重连: %s" % str(e)[:200])
                if self._stop.wait(5):
                    break
        except KeyboardInterrupt:
            self.stop()

    # ---- 连接并维持网关会话 ----
    def _connect_loop(self):
        token = self.token_mgr.get_token()
        self._fetch_self(token)
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
            ctx = bot_config.ssl_context()   # 与 REST 共用同一套 CA / 降级策略
            sock = ctx.wrap_socket(sock, server_hostname=host)
        # 握手返回值是「101 响应之后已被读进来的剩余字节」：QQ 网关常在握手后
        # 立刻发 op10 Hello，两者可能落在同一个 TCP 段里。丢掉它会让客户端干等
        # 90 秒读超时才重连（现象：日志显示已连网关，但一直没有任何事件）。
        leftover = client_handshake(sock, host, port, path, token=None)
        conn = WSConn(sock, client_mask=True, initial=leftover)   # 客户端→服务端 发帧需掩码
        print(f"[official] 已连网关 {host}{path}", file=sys.stderr)
        flog(f"已连网关 {host}{path}")
        now = time.time()
        self._session_started_at = now
        self._last_frame_at = now
        self._last_ack_at = now
        trace_chain("连接", "已连网关 %s%s，会话开始" % (host, path))
        try:
            self._loop(conn, token)
        finally:
            # 每次会话结束都收干净：旧心跳链、旧 socket，否则重连会越堆越僵尸。
            self._cancel_heartbeat()
            try:
                conn.close()
            except Exception:
                pass
            trace_chain("连接", "会话结束，准备重连")

    def _fetch_self(self, token):
        """拉一次机器人自己的资料（GET /users/@me），username 就是群里显示的昵称。

        只在启动后试（成功后不再试；失败最多 3 次，符合静默重试上限）；
        接口不可用（老应用 / 权限不足）也不报错，继续用 `<@!openid>` 占位符与
        WM_QQ_BOT_NAME 判定，不影响正常收发。
        """
        if self.bot_name or self._self_tries >= 3:
            return
        self._self_tries += 1
        ok, info = _api_get(token, self.appid, self.api_base, "/users/@me")
        if ok and isinstance(info, dict) and info.get("username"):
            self.bot_name = str(info["username"]).strip()
            bot_config.echo("[official] 自身信息：昵称=%r" % self.bot_name)
            flog(f"自身信息 昵称={self.bot_name!r}")
        elif not ok:
            flog(f"取自身资料失败（改用占位符/配置判定）: {info}")

    def _get_gateway(self, token):
        url = self.api_base + "/gateway"
        req = urllib.request.Request(url, headers={"Authorization": f"QQBot {token}"})
        with bot_config.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))["url"]

    # ---- 网关收发主循环 ----
    def _loop(self, conn, token):
        # 用户查询改走「每会话一条串行队列」，网关读取循环立刻回来读下一帧：
        # 这样某一问要跑网络/重试时，不会把后面的消息全堵在 socket 里
        # （那种堵法看起来就像「回复排了长队、回的是上一句」）。
        # 监控推送本来就在自己的 ws-push 线程里串行发，两边互不占线。
        self._async_replies = True
        while not self._stop.is_set():
            try:
                typ, payload = conn.recv_message()
            except Exception as e:
                raise RuntimeError(f"网关读取失败: {e}")
            now = time.time()
            self._last_frame_at = now      # 任意帧都算「网关还活着」的证据
            if typ == "close":
                flog("收到 close 帧，关闭连接")
                trace_chain("连接", "收到 close 帧，会话结束")
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
                self._last_ack_at = now
                flog("心跳 ACK（op11）已确认")
                # 会话到期软重连：服务端「只回 ACK、不推事件」时读超时永远不触发，
                # 只有主动重建会话才能恢复收消息（否则就是一下午全静默）。
                # 特意放在 ACK 帧上判断，不拦在消息事件前面，避免正好到点时丢一条。
                if session_expired(self._session_started_at, now, self._session_max):
                    flog(f"网关会话已存活 {now - self._session_started_at:.0f}s，主动软重连")
                    trace_chain("连接", "会话到期（上限 %ds），主动断开重建会话"
                                % int(self._session_max))
                    return
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
                self._last_event_at = now
                if pkt.get("t") in ("READY", "RESUMED"):
                    trace_chain("连接", "网关 %s：会话已就绪，开始接收群消息"
                                % pkt.get("t"))
                self._on_event(conn, pkt, token)

    def _start_heartbeat(self, conn, interval):
        """一条心跳链只服务本次连接：重连后旧链按代次自行退出，不再往死 socket 发帧。"""
        self._cancel_heartbeat()
        generation = self._hb_generation
        self._last_ack_at = time.time()

        def hb():
            if self._stop.is_set() or generation != self._hb_generation:
                return
            now = time.time()
            # ① 看门狗：连续 N 个心跳周期没收到任何帧 / ACK，主动断开让外层重连。
            # socket 的 90s 读超时只在「一个字节都没有」时触发，且没有明确证据；
            # 这里额外留下一条可查的假死记录。
            if gateway_is_stale(self._last_frame_at, self._last_ack_at, now,
                                interval, GATEWAY_STALE_FACTOR):
                flog(f"⚠️ 网关看门狗：{now - max(self._last_frame_at, self._last_ack_at):.0f}s "
                     f"没收到任何帧/ACK，主动断开重连")
                trace_chain("连接", "看门狗触发：连续无帧/ACK，主动断开重连")
                try:
                    conn.close()
                except Exception:
                    pass
                return
            # ② 会话到期也由心跳线程兜底：读循环万一卡在 recv 里执行不到检查，
            #    这条独立线程仍能强制断连，让外层重建会话恢复收消息。
            if session_expired(self._session_started_at, now, self._session_max):
                flog(f"心跳线程：网关会话已存活 {now - self._session_started_at:.0f}s，"
                     f"主动软重连")
                trace_chain("连接", "心跳线程检测到会话到期，主动断开重建会话")
                try:
                    conn.close()
                except Exception:
                    pass
                return
            try:
                conn.send_text(json.dumps({"op": 1, "d": self._seq}))
                flog(f"发送心跳(op1) seq={self._seq}")
            except Exception as e:
                flog(f"发送心跳失败: {e}")
            # ③ 存活存证：群里安静时日志也不空，能区分「进程被冻结」和
            #    「网关还回 ACK 但就是不推消息事件」。
            if now - self._last_proof_at >= GATEWAY_PROOF_SECONDS:
                self._last_proof_at = now
                trace_chain("连接", "网关存活存证：心跳正常，最后帧=%.0fs前，"
                            "最后消息事件=%.0fs前" % (
                                now - self._last_frame_at,
                                (now - self._last_event_at) if self._last_event_at else -1))
            if generation != self._hb_generation:
                return
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
        if not isinstance(d, dict):
            # READY / RESUMED 等事件的 d 可能是空串，别让 sorted(d.keys()) 把连接搞崩
            flog(f"非字典事件 t={t} d={str(d)[:80]!r}（已忽略）")
            return
        # 被动回复用消息 ID（事件 d.id）。注意：event_id 仅支持特定事件类型，
        # 普通群@/全量群消息/单聊消息事件必须用 msg_id。
        msg_id = d.get("id")
        flog(f"事件分发 t={t} msg_id={msg_id} d_keys={sorted(d.keys())}")

        # ---- 1<<25 自带的关系 / 主动消息开关事件（无需额外 intents） ----
        if t in ("GROUP_MSG_RECEIVE", "C2C_MSG_RECEIVE"):
            # 官方文档事件体：GROUP 用 group_openid（另有 op_member_openid 操作人）；
            # C2C 用 openid（不是 user_openid！取错会拿不到会话、补推不触发）。
            ch = "group" if t.startswith("GROUP") else "private"
            tid = (d.get("group_openid") if ch == "group" else
                   (d.get("openid") or d.get("user_openid")
                    or (d.get("author") or {}).get("user_openid")))
            flog(f"✅ 主动消息通知已开启 t={t} {ch}={tid} "
                 f"op={d.get('op_member_openid')} event_id={pkt.get('id')}")
            print(f"[official] {t}：{ch} {tid} 已开启主动发言，立刻补推一轮",
                  file=sys.stderr)
            if tid:
                try:
                    import ws_monitor
                    ws_monitor.notify_active_ready(ch, tid)
                except Exception as e:
                    flog(f"通知开关触发补推失败: {e}")
            return
        if t in ("GROUP_MSG_REJECT", "C2C_MSG_REJECT"):
            ch = "group" if t.startswith("GROUP") else "private"
            tid = (d.get("group_openid") if ch == "group" else
                   (d.get("openid") or d.get("user_openid")))
            flog(f"⚠️ 主动消息通知被关闭 t={t} {ch}={tid}（本会话改走随消息附带）")
            print(f"[official] {t}：{ch} {tid} 关闭了主动发言", file=sys.stderr)
            return
        if t in ("GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "FRIEND_ADD", "FRIEND_DEL"):
            flog(f"关系变更 t={t} group_openid={d.get('group_openid')} "
                 f"user_openid={d.get('user_openid')} d_keys={sorted(d.keys())}")
            print(f"[official] 收到 {t}（机器人进退群 / 好友变更）", file=sys.stderr)
            return

        if t == "GROUP_AT_MESSAGE_CREATE":
            raw = d.get("content", "")
            text = strip_mention(raw)
            group_openid = d.get("group_openid")
            uo, mo, name = _sender_ids(d.get("author"))
            flog(f"群@消息 group_openid={group_openid} 原始内容={raw!r} 剥离后={text!r}")
            bot_config.echo("[recv] 群@ %s 用户=%s 昵称=%s 原文=%r 剥离=%r"
                            % (group_openid, mo or uo, name or "-", raw, text))
            ctx = {"channel": "group", "group_id": group_openid,
                   # 群 @ 事件：明确对着机器人说的，允许不带 dxx  前缀；
                   # member_openid 记下来，回复时用它 @ 回去、之后的监控推送也 @ 他
                   "at_me": True,
                   "user_id": mo or uo,
                   "member_openid": mo,
                   # 链路日志「收到」层要用的原始内容 / 对方昵称 / 事件类型
                   "raw": raw, "nickname": name, "event": t}
            self._dispatch_reply(token, "group", group_openid, text, ctx, msg_id)
        elif t == "GROUP_MESSAGE_CREATE":
            # 群消息「全量模式」：机器人在 q.qq.com 开了「接收所有消息」后，
            # 群里每条消息（不限于 @机器人）都会推此事件。content 是纯文本，
            # @某人会替换成 "<@openid>" 占位（strip_mention 统一剥掉）。
            raw = d.get("content", "")
            text = strip_mention(raw)
            group_openid = d.get("group_openid")
            uo, mo, name = _sender_ids(d.get("author"))
            flog(f"群消息(全量) group_openid={group_openid} 原始内容={raw!r} 剥离后={text!r}")
            bot_config.echo("[recv] 群 %s 用户=%s 昵称=%s 原文=%r 剥离=%r"
                            % (group_openid, mo or uo, name or "-", raw, text))
            # full_message：这条消息不是专门 @ 机器人的，回复也不要「对着某人引用」，
            # 而是单纯往群里发一条普通消息（不 @ 任何人、不带 msg_id 引用）。
            ctx = {"channel": "group", "group_id": group_openid,
                   "full_message": True,
                   "user_id": mo or uo,
                   "member_openid": mo,
                   "raw": raw, "nickname": name, "event": t}
            # 但「@机器人 平原」在全量模式下也走这个事件：机器人占位符带叹号
            # （<@!bot_openid>），普通成员是 <@openid>、全体是 <@all>。据此点亮 at_me，
            # 否则免前缀的 @ 指令会被当成闲聊静默 —— 用户看到的就是「@ 了没反应」。
            # at_me 一旦点亮，回复就和群@事件一样走「@ 回对方」。
            if _BOT_MENTION_RE.search(raw):
                ctx["at_me"] = True
            elif _text_mentions_bot(raw, self.bot_name):
                # 有的客户端 / 全量模式下「@机器人」是明文「@昵称」，没有叹号占位符。
                # 认出来才算 @ 我；认出来就按 @ 消息处理（可以引用回复，不用普通群消息）。
                ctx["at_me"] = True
                ctx.pop("full_message", None)
            self._dispatch_reply(token, "group", group_openid, text, ctx, msg_id)
        elif t == "C2C_MESSAGE_CREATE":
            raw = d.get("content", "")
            text = strip_mention(raw)
            openid, _mo, name = _sender_ids(d.get("author"))
            flog(f"单聊消息 user_openid={openid} 原始内容={raw!r} 剥离后={text!r}")
            bot_config.echo("[recv] 单聊 %s 昵称=%s 原文=%r 剥离=%r"
                            % (openid, name or "-", raw, text))
            ctx = {"channel": "private", "group_id": openid,
                   "raw": raw, "nickname": name, "event": t}
            self._dispatch_reply(token, "user", openid, text, ctx, msg_id)
        else:
            flog(f"未处理的事件类型 t={t}（已忽略）")

    def _dispatch_reply(self, token, kind, target_id, text, ctx, msg_id):
        """回复入口：网关循环只负责「登记」，真正的查询/发图交给工作线程。

        每会话单独一条队列 + 一个线程：同一会话仍按顺序一问一答（不会串台、
        不会乱序），不同会话之间并行，谁都别被别人的慢查询或监控推送拖住。
        """
        if not target_id:
            return
        c = ctx or {}
        # 自检只是「一条真实样本」：打标只用于日志区分，不产生新消息，
        # 所以不会递归触发自己、也不会刷屏。
        tag = ""
        try:
            q = qq_common.strip_query_prefix(text) or text
            if qq_common.is_diagnostic(q):
                tag = "[自检] "
        except Exception:
            tag = ""
        nick = str(c.get("nickname") or "").strip() or "-"
        trace_chain("收到", "%s事件=%s 目标=%s %s msg_id=%s 昵称=%s 剥离后=%r"
                    % (tag, c.get("event") or "-", kind, target_id,
                       msg_id or "-", nick, text),
                    raw=str(c.get("raw", text)))
        # 自己的昵称交给 handler：明文「@昵称」靠它判定（取不到就退回配置/占位符）
        if self.bot_name:
            ctx.setdefault("bot_name", self.bot_name)
        if not self._async_replies:
            self._do_reply(token, kind, target_id, text, ctx, msg_id)
            return
        key = "%s:%s" % (kind, target_id)
        flog(f"用户查询入队 key={key} msg_id={msg_id} 文本={text!r}")

        def job():
            flog(f"用户查询开始处理 key={key} msg_id={msg_id} 文本={text!r}")
            try:
                tok = self.token_mgr.get_token()     # 排队久了 token 可能过期，重新取
            except Exception:
                tok = token
            mon = None
            try:
                # 查询期间给推送线程打「让行」标记：推送排队，查询不排队
                import ws_monitor as _mon
                mon = _mon
                mon.query_begin()
            except Exception:
                mon = None
            try:
                self._do_reply(tok, kind, target_id, text, ctx, msg_id)
            finally:
                if mon is not None:
                    try:
                        mon.query_end()
                    except Exception:
                        pass

        self._submit_reply(key, job)

    def _submit_reply(self, key, job):
        with self._reply_qlock:
            q = self._reply_queues.get(key)
            th = self._reply_threads.get(key)
            # 兜底：工作线程如果异常退出（不是正常空闲注销），队列还在但没有消费者，
            # 之后的任务会永远躺在队列里、用户一条回复都收不到。这里发现线程已死
            # 就补一个新消费者接着消费同一个队列（队列里排着的任务不会丢）。
            if q is None or (th is not None and not th.is_alive()):
                if q is None:
                    q = queue.Queue()
                    self._reply_queues[key] = q
                else:
                    flog(f"⚠️ 会话 {key} 的回复线程已退出，重建消费者（队列中 {q.qsize()} 条待处理）")
                th = threading.Thread(target=self._reply_worker, args=(key, q),
                                      daemon=True, name="reply-%s" % key)
                self._reply_threads[key] = th
                th.start()
        q.put(job)

    def _reply_worker(self, key, q):
        """单会话串行执行；空闲 5 分钟自动退出并注销，避免线程越积越多。

        等待用 1 秒一轮的 get，好处是 bot.stop() 时能立刻退出（不然要等满超时）。
        """
        idle = 0.0
        while not self._stop.is_set():
            try:
                job = q.get(timeout=1.0)
            except queue.Empty:
                idle += 1.0
                if idle >= 300:
                    with self._reply_qlock:
                        if q.empty() and self._reply_queues.get(key) is q:
                            self._reply_queues.pop(key, None)
                            self._reply_threads.pop(key, None)
                            return
                    idle = 0.0
                continue
            idle = 0.0
            try:
                job()
            except Exception as e:
                flog(f"用户查询处理异常 key={key}: {e}")
        with self._reply_qlock:
            if self._reply_queues.get(key) is q:
                self._reply_queues.pop(key, None)
            if self._reply_threads.get(key) is threading.current_thread():
                self._reply_threads.pop(key, None)

    def _do_reply(self, token, kind, target_id, text, ctx, msg_id):
        if not target_id:
            return
        trace_tag = ""
        try:
            q = qq_common.strip_query_prefix(text) or text
            if qq_common.is_diagnostic(q):
                trace_tag = "[自检] "
        except Exception:
            trace_tag = ""
        flog(f"进入回复分发 kind={kind} target_id={target_id} msg_id={msg_id} 文本={text!r}")
        try:
            reply = self.handler(text, ctx)
        except Exception as e:
            reply = f"⚠️ 处理出错：{e}"
            flog(f"handler 抛出异常: {e}")
        if not reply:
            flog("handler 返回空（闲聊未带 dxx  前缀 / 白名单外群静默），不回复")
            bot_config.echo("[silent] %s %s 文本=%r 没命中指令或群不在白名单，不回复"
                            % (kind, target_id, text))
            # 有「收到」没「组装」时，这条能证明事件确实到了，只是被判定静默。
            trace_chain("静默", "事件已收到但 handler 返回空：%s %s 文本=%r "
                        "（白名单外 / 未命中指令 / 判定闲聊）"
                        % (kind, target_id, text))
            return
        # 全量群消息（没 @ 机器人）：优先【不带 msg_id】发一条普通群消息，
        # 不 @ 任何人、也不显示「引用某条消息」；@ 消息才走被动引用回复。
        # 两条通道互为兜底：主动没权限 / 被动窗口被拒时自动换另一条再试。
        # 想让全量消息也走被动，把 data/config.txt 的 WM_FULLMSG_ACTIVE 设为 0。
        active_first = (bool((ctx or {}).get("full_message"))
                        and (os.environ.get("WM_FULLMSG_ACTIVE") or "1").strip().lower()
                        not in ("0", "false", "no", "off", "none"))
        skip = object()          # 哨兵：区分「不重试」和「回退到不带 msg_id 的主动通道」
        if msg_id and active_first:
            mid_try, mid_fallback = None, msg_id     # 主动优先，失败回退被动
        elif msg_id:
            mid_try, mid_fallback = msg_id, None     # 被动优先，失败回退主动
        else:
            mid_try, mid_fallback = None, skip       # 没有 msg_id 就只发一次

        def _can_retry():
            return mid_fallback is not skip and mid_fallback != mid_try

        def _retry_note(info):
            way = "主动" if mid_try is None else "被动"
            back = "被动" if mid_fallback else "主动"
            flog(f"{way}发送失败({info})，改走{back}通道重试")

        def _send_text(content):
            ok, info = send_message(token, self.appid, self.api_base,
                                    kind, target_id, content, mid_try)
            if not ok and _can_retry():
                _retry_note(info)
                ok, info = send_message(token, self.appid, self.api_base,
                                        kind, target_id, content, mid_fallback)
            return ok, info

        def _send_images(png_list, content=None):
            res = self._reply_images(token, kind, target_id, png_list, mid_try,
                                     content=content)
            if not bool(res) and _can_retry():
                flog("发图失败，换另一条消息通道重试")
                res = self._reply_images(token, kind, target_id, png_list,
                                         mid_fallback, content=content)
            return res

        # 卡片 PNG + 可复制的 /w 指令字符串
        pngs = list(getattr(reply, "pngs", None) or [])
        tail = str(getattr(reply, "tail", "") or "").strip()
        # 这条消息 @ 了机器人 → 回复也 @ 回去（与 dxx  指令同一套处理，只多这一行）
        at_tag = reply_at_tag(ctx, kind)
        if (ctx or {}).get("at_me") and not at_tag:
            # 排查用：@ 了机器人却没生成 @ 回标签，说明事件里 member_openid/user_id 都是空。
            flog(f"⚠️ @ 了机器人但拿不到 @ 回标签（member_openid/user_id 均空）: "
                 f"{kind} {target_id}")
        nick = str((ctx or {}).get("nickname") or "").strip() or "-"
        composed = ("@%s %s" % (nick, str(reply))) if at_tag else str(reply)
        trace_chain("组装", "%s准备发送 昵称=%s at_tag=%r 回复类型=%s"
                    % (trace_tag, nick, at_tag, type(reply).__name__),
                    raw=composed)

        # ① 默认 Markdown 消息（msg_type=2）：图片 + 图下两条 /w（图里已有正文，不重复）
        #    config.txt: WM_QQ_MARKDOWN = native；设为 0 可关。
        #    发送失败自动回退下面的富媒体图文混排 / 纯文本。
        if qq_markdown is not None and qq_markdown.enabled():
            # 捕获组装完成、尚未交给客户端渲染的 markdown 原文，供「已发送」层落盘
            built = {}

            def _capture_mk(mode, md):
                built["mode"] = mode
                built["md"] = md

            ok_md, info_md, md_mode = qq_markdown.send_reply(
                token, self.appid, self.api_base, kind, target_id, reply,
                msg_id=mid_try, pngs=pngs, tail=tail, post=_api_post,
                at_tag=at_tag, on_built=_capture_mk)
            # 图片没上传成功时换 msg_id 重试毫无意义（还是没图），直接回退发图通道
            no_image = str(info_md).startswith(qq_markdown.NO_IMAGE)
            if not ok_md and _can_retry() and not no_image:
                flog(f"markdown 首选通道失败({info_md})，换另一条消息通道重试")
                ok_md, info_md, md_mode = qq_markdown.send_reply(
                    token, self.appid, self.api_base, kind, target_id, reply,
                    msg_id=mid_fallback, pngs=pngs, tail=tail, post=_api_post,
                    at_tag=at_tag, on_built=_capture_mk)
            if ok_md:
                print(f"[official] 已回复 Markdown（{md_mode}）{kind} {target_id}",
                      file=sys.stderr)
                flog(f"发送 Markdown 成功: {kind} {target_id} 模式={md_mode}")
                trace_chain("已发送", "%s方式=Markdown(msg_type=2,%s) 目标=%s %s 模式=%s"
                            % (trace_tag, built.get("mode", md_mode), kind,
                               target_id, md_mode),
                            raw=_mk_raw(built.get("md", {})))
                return
            flog(f"Markdown 发送失败({info_md})，回退富媒体/文本通道")
            # 没图属于「通道拿不到素材」，不是业务发送失败：即便调试时关了
            # WM_QQ_MD_FALLBACK，也要让富媒体把卡片图发出去。
            if not qq_markdown.fallback_enabled() and not no_image:
                flog(f"⚠️ 回复未送达（Markdown 失败且已关闭回退）: {kind} {target_id} msg_id={msg_id}")
                return

        # ② 默认并进【同一条】图文混排消息（msg_type=7 + content）
        mix_tail = tail if (pngs and tail and mixed_message_enabled()) else None
        img_result = _send_images(pngs, content=mix_tail) if pngs else None
        sent_img = bool(img_result)
        mixed = bool(getattr(img_result, "mixed", False))
        if sent_img:
            way = "单条图文混排（含 /w 指令）" if mixed else "纯图"
            print(f"[official] 已回复图片 {kind} {target_id}"
                  f"（{len(pngs)} 张，{way}）", file=sys.stderr)
            flog(f"发送图片成功: {kind} {target_id} 张数={len(pngs)} {way}")
            if tail and not mixed:
                # 混排被关掉 / 不被服务端支持：单独补一条文本，/w 指令不能丢
                ok2, info2 = _send_text(tail)
                if ok2:
                    flog(f"图片后 /w 字符串补发成功: {kind} {target_id}")
                else:
                    print(f"[official] 图片后字符串发送失败: {info2}",
                          file=sys.stderr)
                    flog(f"图片后字符串发送失败: {info2}")
            trace_chain("已发送", "%s方式=富媒体(msg_type=7) 目标=%s %s 张数=%d %s"
                        % (trace_tag, kind, target_id, len(pngs), way),
                        raw=mix_tail if mix_tail is not None else "(纯图，无文字)")
            return
        # 图片没发成：退回完整文本；tail（/w 指令）并回正文一起发，不能丢
        text_out = str(reply)
        if tail:
            text_out = (text_out + "\n\n" + tail) if text_out.strip() else tail
        flog(f"handler 返回回复（前80字）: {text_out[:80]!r}")
        ok, info = _send_text(text_out)
        if not ok:
            print(f"[official] 发送失败: {info}", file=sys.stderr)
            # 明确标记「用户没收到」：QQ 侧频控/权限/网络全失败时用户看不到任何东西，
            # 只能靠这行日志定位（含 msg_id，可对着用户反馈的时间点排查）。
            flog(f"⚠️ 回复未送达（文字通道也失败）: {kind} {target_id} msg_id={msg_id} 原因={info}")
        else:
            print(f"[official] 已回复 {kind} {target_id}", file=sys.stderr)
            flog(f"发送成功: {kind} {target_id}")
            trace_chain("已发送", "%s方式=文本(msg_type=0) 目标=%s %s"
                        % (trace_tag, kind, target_id), raw=text_out)

    def _reply_images(self, token, kind, target_id, pngs, msg_id, content=None):
        """逐张上传 + 发送；content 非空时并进【最后一张】图片消息做图文混排。

        官方富媒体 msg_type=7 + content 只对图片生效，客户端把图与文字放在同一条
        消息里，因此「行情卡片 + /w 指令」只占一条。返回 _ImageSendResult；
        混排请求若被服务端拒绝（如 22006），用同一个 file_info 重发纯图，并让
        上层单独补发 tail 文本，/w 指令绝不丢。
        """
        try:
            token = self.token_mgr.get_token()
        except Exception as e:
            flog(f"发图取 token 失败: {e}")
            return _ImageSendResult(False)
        items = list(pngs[:4])
        if not items:
            return _ImageSendResult(False)
        use_mixed = bool(content)
        last = len(items) - 1
        for i, png in enumerate(items):
            info, err = upload_image(token, self.appid, self.api_base,
                                     kind, target_id, png,
                                     file_name=f"card{i + 1}.png")
            if err:
                print(f"[official] 图片上传失败: {err}", file=sys.stderr)
                flog(f"图片上传失败: {err}")
                return _ImageSendResult(False)
            tail_here = content if (use_mixed and i == last) else None
            ok, resp = send_image(token, self.appid, self.api_base,
                                  kind, target_id, info, msg_id,
                                  content=tail_here)
            if not ok and tail_here:
                # 服务端不支持 / 拒绝图文混排：同一 file_info 改发纯图，
                # tail 交给上层用文本消息补发
                print(f"[official] 图文混排不支持，回退两条消息: {resp}",
                      file=sys.stderr)
                flog(f"图文混排被拒({resp})，改发纯图 + 单独文本")
                use_mixed = False
                ok, resp = send_image(token, self.appid, self.api_base,
                                      kind, target_id, info, msg_id)
            if not ok:
                print(f"[official] 图片发送失败: {resp}", file=sys.stderr)
                flog(f"图片发送失败: {resp}")
                return _ImageSendResult(False)
        return _ImageSendResult(True, mixed=use_mixed)


def main():
    bot_config.apply()    # 读同目录 data/config.txt；直接运行本文件时也生效
    ca = bot_config.ensure_ssl_ca()
    if ca:
        print(f"[ssl] 已启用 CA 根证书：{ca}", file=sys.stderr)
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
    base_handler = qq_common.make_handler(allowed, allow_all=allow_all,
                                         groups_file=groups_file)
    # 全量群消息下任意一条消息都能作为「监控内容被动捎带」的窗口，
    # 不必非等有人发查询指令（白名单外依然严格静默）。
    handler = qq_common.make_monitor_aware_handler(base_handler)

    import ws_monitor
    ws_monitor.set_sender(lambda kind, tid, text: bot.send_active(kind, tid, text))
    # 恢复上次的去重记录：重启后只要内容没变，就不再重发首轮监控（防刷屏 / 重复）
    ws_monitor.restore_last_sent()
    # 先恢复聊天指令保存的开关（「活动=开启」等），再按配置补自动注册；
    # 两者都只合并栏目，不覆盖对方 / 用户手动开的监控范围。
    saved_n = ws_monitor.load_switches()
    if saved_n:
        print(f"[official] 已恢复 {saved_n} 个会话保存的主推开关"
              "（发「主推状态」可查看）", file=sys.stderr)
    # 恢复裂缝类栏目的过滤条件（「监控 裂缝后纪中断」等），与开关各自独立存盘
    saved_f = ws_monitor.load_filters()
    if saved_f:
        print(f"[official] 已恢复 {saved_f} 个会话的裂缝过滤条件", file=sys.stderr)
    # 恢复「监控是谁开的」：官方主动消息不能 @ 人，这里只保留记录（换到 OneBot 时立刻生效）
    saved_a = ws_monitor.load_at_users()
    if saved_a:
        print(f"[official] 已恢复 {saved_a} 个会话的监控发起人", file=sys.stderr)
    # 恢复成员级订阅：群里「停止监控」只撤自己，「停止全部监控」才清全群
    saved_s = ws_monitor.load_subs()
    if saved_s:
        print(f"[official] 已恢复 {saved_s} 个会话的成员订阅", file=sys.stderr)
    # 可选：对白名单群自动注册「警报 / 活动」主推（默认关，见 WS_AUTO_PRIORITY）
    auto_n = ws_monitor.auto_start_priority(allowed if not allow_all else ())
    if auto_n:
        print(f"[official] WS_AUTO_PRIORITY 已开启：{auto_n} 个白名单群自动主推 "
              f"{ws_monitor.priority_types()}（新条目即时推送）", file=sys.stderr)
    # 遗物 / 掉落索引：后台预抓一次（首次约 30 秒），之后查询只读本地索引秒回
    drops.ensure_async()

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
    print("[official] 群里直接发「WM <物品名>」/「ZK <武器名>」即可查询"
          "（开了全量消息可免 @，否则请 @机器人）；「帮助」看全部指令。", file=sys.stderr)
    print("[official] 两个开关都在【群资料页 → 机器人设置】按群单独开："
          "「机器人可获取的群聊消息范围 = 获取群内全部消息」(免@) / "
          "「机器人主动在群聊内发言」(定时监控推送)。intents=1<<25 无需改动。",
          file=sys.stderr)
    flog("=" * 40)
    flog(f"bot 启动 appid={appid} intents={intents} allow_all={allow_all}")
    flog(f"白名单文件={groups_file} 生效群数={len(allowed)} groups={sorted(allowed)}")
    flog("事件支持: GROUP_AT_MESSAGE_CREATE / GROUP_MESSAGE_CREATE(全量) / "
         "C2C_MESSAGE_CREATE / *_MSG_RECEIVE|REJECT / 进退群与好友事件")
    bot.run_forever()


if __name__ == "__main__":
    main()
