#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ws_monitor.py — 世界状态监控推送（事件主推 + 周期兜底 + 被动捎带）

职责：
  - 维护一份「会话 -> 监控类型集合」的注册表（内存态，重启即清空）；
    但聊天指令「活动=开启 / 警报=关闭」设的开关会存盘，启动时 load_switches
    自动恢复并重新注册，所以设置一次就一直有效，不必重启后重发；
  - 【主推】警报 / 活动等时效栏目出现【新条目】时立刻单独推一条精简消息，
    不受 WS_ACTIVE_MIN_GAP 的 5 分钟限频约束（只有自己的 60 秒防抖）；
  - 【周期】其余栏目按内容变化推送，签名会剔除倒计时，倒计时跳动不再刷屏；
  - 【会话开关】群里直接发「活动=开启」「警报=关闭」即可改本会话的主推范围，
    不用改配置、不用重启；设置存到 data/ws_switches.json，重启自动恢复。
  - 后台线程每 worldstate.WS_PUSH_SECONDS 秒检查一次，先走【主动发送】；
  - 主动发送失败（官方机器人未开通主动消息配额时必然失败：40034105 无权限），
    就把这条内容存进 pending；等这个会话里【有人发消息】时，由 qq_common
    随回复一起带出去（被动通道有 5 分钟窗口，一定能发）。

  → 结果：有新警报/新活动马上知道；主动消息有权限就照常推；没权限也不会丢。

sender 由 QQ 后端（官方 / OneBot）在启动时注入：
      sender(channel, target_id, text) -> bool（是否发送成功）
      channel 取 "group" | "private"；target_id 为群 OpenID 或用户 OpenID / 群号。

配置（data/config.txt，全部可选）：
  WS_PRIORITY_TYPES       = alerts,events   主推栏目（可用中文：警报,活动）
  WS_PRIORITY_MIN_GAP     = 60             主推防抖最小间隔（秒）
  WS_PRIORITY_SKIP_IN_FULL= 1              周期全量推送里剔除主推栏目，避免重复
  WS_AUTO_PRIORITY        = 0              启动时对白名单群自动注册主推（默认关）
  WS_SWITCH_FILE          = data/ws_switches.json   聊天指令设置的开关存盘位置
  WS_ATTACH_ON_QUERY      = 1              是否允许「随查询附带」（1 开 / 0 关）
  WS_ATTACH_TTL           = 1800           暂存内容的保鲜期（秒），超过认为过期直接丢弃
  WS_ATTACH_GAP           = 20             同一会话两次附带的最小间隔（秒），防连发刷屏
  WS_ATTACH_MAX_BYTES     = 2000           附带内容的最大字节数，超出按整行截断
  WS_ACTIVE_MIN_GAP       = 300            周期主动推送最小间隔（秒）；内容没变一律不重发

官方主动消息频控（2026 版文档）：单关系 20 qpm、每个群每天最多 1000 条，
且接收方（群管理员 / 用户）必须在机器人资料卡开启「主动消息」通知，否则一律失败。
所以 WS_PUSH_SECONDS 只是「检查节拍」，真正发出去要满足：内容有变化 + 过了最小间隔。
"""

import hashlib
import inspect
import json
import os
import re
import threading
import time
from pathlib import Path

import wm
import worldstate as ws


HERE = Path(__file__).resolve().parent
DEFAULT_SWITCH_FILE = HERE / "data" / "ws_switches.json"
LAST_SENT_FILE = HERE / "data" / "ws_last_sent.json"   # 按栏目去重记录（落盘，防重启重发）
DEFAULT_FILTER_FILE = HERE / "data" / "ws_filters.json"  # 裂缝维度过滤（落盘，重启后恢复）
DEFAULT_AT_FILE = HERE / "data" / "ws_at_users.json"     # 谁发的监控指令（推送时 @ 他）
DEFAULT_SUB_FILE = HERE / "data" / "ws_subs.json"        # 成员级订阅：谁开了哪些栏目

_sender = None                       # 由 QQ 后端注入：sender(channel, target_id, text[, at_user]) -> bool
_monitors = {}                       # (channel, target_id) -> set(types)
_subs = {}                           # (channel, target_id) -> {type: set(uid)}  成员级订阅
                                     # uid 为空串 = 会话级订阅（开关恢复 / 自动主推，不属于某个人）
_at_users = {}                       # (channel, target_id) -> 发起监控的用户号（推送时 @ 他）
_fissure_filters = {}                # (channel, target_id) -> {栏目键: [token...]}
                                     # 栏目维度过滤：裂缝(纪/星球/任务) 与 赏金(集团/地区) 共用
                                     # 这张表（键是栏目名 fissures/steel/bounty）
_switches = {}                       # (channel, target_id) -> {type: bool}  聊天指令设的开关
_pending = {}                        # (channel, target_id) -> {"text":str,"at":float,"fails":int}
_attached_at = {}                    # (channel, target_id) -> 上次随查询附带的时刻
_last_sent = {}                      # (channel, target_id) -> {类型键: (该栏目内容签名, 主动推送成功时刻)}
                                        # 周期推送按栏目拆分后，去重粒度细化到单个栏目：
                                        # 只有内容变化的栏目才重发，避免每条都刷屏。
_seen_priority = {}                  # (channel, target_id) -> {type: set(条目键)}  主推基线
_priority_sent_at = {}               # (channel, target_id) -> 上次主推成功时刻
_next_probe = 0.0                    # 下次允许尝试主动发送的时刻（失败后退避探测）
_lock = threading.RLock()
_push_thread = None
_stop = threading.Event()
# 正在处理的「用户查询」计数：>0 时推送线程让行（用户查询优先，推送才排队）。
_query_depth = [0]

PROBE_GAP = 300.0                    # 主动通道不通时，每 5 分钟才再试一次（别空打接口）
# 默认【不】开主推即时推：警报 / 活动不再一出现就立刻单独发一条，而是随周期全量
# 拆分推送（等一轮抓取完成再发，结构统一、不杂乱）。需要即时主推时显式设
# WS_PRIORITY_TYPES=alerts,events 即可恢复旧行为。
PRIORITY_DEFAULT = ""
# 倒计时标准化：3天4小时5分 / 2小时30分10秒 / 5分30秒 / 30秒 一律换成 <T>
_DURATION = re.compile(r"(?:\d+(?:天|小时|分|秒)){1,4}")


def _num(name, default):
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


# ---------------- 用户查询优先：查询在处理时，推送让行 ----------------
# 规则只有一条：**推送排队，查询不排队**。用户发指令时（取数 / 渲染 / 发图）
# 推送线程不再发起新的主动发送，等查询处理完再继续，避免「回复要等前面
# 一堆监控图发完」的体感。推送本身已经按栏目串行 + 间隔，被让行的一轮下轮补。
def query_begin():
    with _lock:
        _query_depth[0] += 1


def query_end():
    with _lock:
        if _query_depth[0] > 0:
            _query_depth[0] -= 1


def query_busy():
    with _lock:
        return _query_depth[0] > 0


def attach_enabled():
    """是否允许把暂存的监控内容随查询回复带出（默认开）。"""
    v = (os.environ.get("WS_ATTACH_ON_QUERY") or "1").strip().lower()
    return v not in ("0", "false", "no", "off", "none", "null", "")


def attach_ttl():
    return _num("WS_ATTACH_TTL", 1800)


def attach_gap():
    return _num("WS_ATTACH_GAP", 20)


def attach_max_bytes():
    return int(_num("WS_ATTACH_MAX_BYTES", 2000))


def active_min_gap():
    """周期主动推送最小间隔（秒）。内容变化才推，但倒计时之类的字段会频繁变，
    所以再加一道下限，避免撞官方 20 qpm / 每群每天 1000 条的频控。"""
    return _num("WS_ACTIVE_MIN_GAP", 300)


def _on(name, default):
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        v = str(default).lower()
    return v not in ("0", "false", "no", "off", "none", "null", "")


def priority_types():
    """主推栏目（按 TYPES 展示序）。可写英文键或中文别名，逗号分隔；写 off = 关掉主推。"""
    raw = os.environ.get("WS_PRIORITY_TYPES")
    if raw is None or not raw.strip():
        raw = PRIORITY_DEFAULT
    if raw.strip().lower() in ("off", "none", "null", "0", "false", "no"):
        return []
    wanted = []
    for x in raw.replace("，", ",").split(","):
        x = x.strip().lower()
        if not x:
            continue
        k = ws.KEYWORDS.get(x, x)
        if k in ws.TYPES and k not in wanted:
            wanted.append(k)
    return [t for t in ws.TYPES if t in wanted]


def priority_min_gap():
    """主推防抖间隔（秒）。新条目立刻推，但同一波刷新别连发好几条。"""
    return _num("WS_PRIORITY_MIN_GAP", 60)


def priority_skip_in_full():
    """周期全量推送是否剔除主推栏目（避免同一条警报被推两遍）。"""
    return _on("WS_PRIORITY_SKIP_IN_FULL", True)


def push_split_max():
    """周期全量推送【按栏目拆分】后，单轮推送最多发几条独立消息（防「全部」刷屏）。

    默认 10：监控「全部」(约 18 个栏目) 时，单轮最多发 10 条，超出的最不紧急栏目
    本轮不发、等下轮内容变化再补。配合「按栏目独立去重」，稳态下通常只发 1~3 条
    （仅变化的栏目），这个上限只在「刚开启监控 / 大量栏目同时变化」时兜底。
    """
    return int(_num("WS_PUSH_SPLIT_MAX", 10))


def push_interval():
    """拆分推送时相邻两条消息之间的间隔（秒），避免一次性连发被官方频控。

    设 0 关闭间隔。默认 0.6 秒。
    """
    return _num("WS_PUSH_INTERVAL", 0.6)


def auto_priority_enabled():
    """启动时是否对白名单群自动注册主推监控。默认关，不擅自往群里发消息。"""
    return _on("WS_AUTO_PRIORITY", False)


# ---------------- 会话级开关：发消息设置，存盘，重启恢复 ----------------
def switch_file():
    raw = os.environ.get("WS_SWITCH_FILE")
    return Path(raw) if raw else DEFAULT_SWITCH_FILE


def _save_switches():
    """原子写盘（临时文件 + os.replace），磁盘不可写时静默降级为本次运行有效。"""
    if not _on("WS_SWITCH_PERSIST", True):
        return False
    p = switch_file()
    tmp = p.with_name(p.name + ".tmp")
    payload = {"version": 1,
               "sessions": {"%s|%s" % (ch, tid):
                            {t: bool(v) for t, v in sorted(sw.items())}
                            for (ch, tid), sw in _switches.items() if sw}}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def load_switches():
    """启动时恢复上次保存的开关，并给这些会话自动注册监控（只合并、不覆盖）。

    返回恢复的会话数。文件不存在 / 坏了 / 字段不认识都当空，
    绝不让一个坏 JSON 挡住机器人启动。
    """
    global _switches
    p = switch_file()
    data = {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8-sig"))
        sessions = (obj or {}).get("sessions") or {}
        if isinstance(sessions, dict):
            for k, v in sessions.items():
                if not isinstance(k, str) or "|" not in k or not isinstance(v, dict):
                    continue
                ch, tid = k.split("|", 1)
                if ch not in ("group", "private") or not tid:
                    continue
                sw = {t: bool(x) for t, x in v.items() if t in ws.TYPES}
                if sw:
                    data[(ch, tid)] = sw
    except Exception:
        data = {}
    with _lock:
        _switches = data
        for key, sw in data.items():
            on = {t for t, v in sw.items() if v}
            if on:
                if key not in _monitors:
                    _monitors[key] = set(on)
                else:
                    _monitors[key].update(on)
    if data:
        ws.ensure_active()
        _ensure_push_thread()
    return len(data)


def _expand_switch_types(types):
    """把解析层给的「__all__ / __priority__ / 单键」展开成真实栏目键列表。"""
    if types == "__all__":
        return list(ws.TYPES.keys())
    if types == "__priority__":
        default = [t for t in ("alerts", "events") if t in ws.TYPES]
        return list(priority_types() or default)
    if isinstance(types, (list, tuple, set)):
        out = []
        for t in types:
            if t in ws.TYPES and t not in out:
                out.append(t)
        return out
    return [types] if types in ws.TYPES else []


def set_switch(channel, target_id, types, on):
    """设置本会话某些栏目的主推开关，立即生效并存盘；返回实际生效的栏目键列表。

    on=True ：合并进本会话监控（不覆盖手动开的其它栏目）+ 设为主推；
    on=False：关掉主推，并从监控 / 周期全量推送里剔除该栏目。
    两个方向都会清掉该栏目的事件基线，避免「关了再开」把旧条目当新条目补推。
    """
    if channel is None or target_id in (None, ""):
        return []
    key = (channel, str(target_id))
    todo = _expand_switch_types(types)
    if not todo:
        return []
    with _lock:
        sw = _switches.setdefault(key, {})
        for t in todo:
            sw[t] = bool(on)
        mon = _monitors.get(key)
        if on:
            if mon is None:
                _monitors[key] = set(todo)
            else:
                mon.update(todo)
            # 主推开关是会话级设置，不属于某个成员：记成空 uid 的会话级订阅，
            # 个人「停止监控」不会把它清掉，只有「停止全部监控」才清。
            cols = _subs.setdefault(key, {})
            for t in todo:
                cols.setdefault(t, set()).add("")
        elif mon is not None:
            for t in todo:
                mon.discard(t)
            cols = _subs.get(key)
            if cols:
                for t in todo:
                    cols.pop(t, None)
                if not cols:
                    _subs.pop(key, None)
        seen = _seen_priority.get(key)
        if seen:
            for t in todo:
                seen.pop(t, None)
        # 关掉某栏目时，顺手清掉它的裂缝维度过滤（不再适用）
        if not on:
            ff = _fissure_filters.get(key)
            if ff:
                for t in todo:
                    ff.pop(t, None)
                if not ff:
                    _fissure_filters.pop(key, None)
        _save_switches()
        _save_subs()
        if not on and _fissure_filters:
            _save_filters()
    if on:
        ws.ensure_active()
        _ensure_push_thread()
    return todo


def reset_switches():
    """仅供离线测试：清空内存态开关，不动磁盘文件。"""
    with _lock:
        _switches.clear()


def filter_file():
    raw = os.environ.get("WS_FILTER_FILE")
    return Path(raw) if raw else DEFAULT_FILTER_FILE


def _save_filters():
    """原子写盘裂缝维度过滤；磁盘不可写时静默降级为本次运行有效。"""
    p = filter_file()
    tmp = p.with_name(p.name + ".tmp")
    payload = {"version": 1,
               "sessions": {"%s|%s" % (ch, tid):
                            {c: list(toks) for c, toks in f.items() if toks}
                            for (ch, tid), f in _fissure_filters.items() if f}}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def load_filters():
    """启动时恢复上次保存的裂缝维度过滤；坏文件 / 字段不认识都当空。"""
    p = filter_file()
    data = {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8-sig"))
        sessions = (obj or {}).get("sessions") or {}
        if isinstance(sessions, dict):
            for k, v in sessions.items():
                if not isinstance(k, str) or "|" not in k or not isinstance(v, dict):
                    continue
                ch, tid = k.split("|", 1)
                if ch not in ("group", "private") or not tid:
                    continue
                f = {c: list(toks) for c, toks in v.items()
                     if c in ("fissures", "steel", "bounty")
                     and isinstance(toks, list)}
                if f:
                    data[(ch, tid)] = f
    except Exception:
        data = {}
    with _lock:
        _fissure_filters.clear()
        _fissure_filters.update(data)
    return len(data)


def at_file():
    raw = os.environ.get("WS_AT_FILE")
    return Path(raw) if raw else DEFAULT_AT_FILE


def _save_at_users():
    """原子写盘「监控发起人」；磁盘不可写时静默降级为本次运行有效。"""
    p = at_file()
    tmp = p.with_name(p.name + ".tmp")
    payload = {"version": 1,
               "sessions": {"%s|%s" % (ch, tid): str(uid)
                            for (ch, tid), uid in _at_users.items() if uid}}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def load_at_users():
    """启动时恢复「监控发起人」；坏文件 / 字段不认识都当空（只是不 @ 人，不影响推送）。"""
    p = at_file()
    data = {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8-sig"))
        for k, uid in ((obj or {}).get("sessions") or {}).items():
            if not isinstance(k, str) or "|" not in k or not uid:
                continue
            ch, tid = k.split("|", 1)
            if ch in ("group", "private") and tid:
                data[(ch, tid)] = str(uid)
    except Exception:
        data = {}
    with _lock:
        _at_users.clear()
        _at_users.update(data)
    return len(data)


def at_user_of(channel, target_id):
    """本会话最近一次开监控的人；没记录就返回 None（推送时不 @ 任何人）。"""
    with _lock:
        return _at_users.get((channel, str(target_id)))


def sub_file():
    raw = os.environ.get("WS_SUB_FILE")
    return Path(raw) if raw else DEFAULT_SUB_FILE


def _save_subs():
    """原子写盘成员级订阅；磁盘不可写时静默降级为本次运行有效。

    只写「谁订阅了哪个栏目」，键带 channel，天然按会话隔离：
    一个群 / 一个私聊的清理动作永远只碰自己那一行。
    """
    p = sub_file()
    tmp = p.with_name(p.name + ".tmp")
    payload = {"version": 1,
               "sessions": {"%s|%s" % (ch, tid):
                            {t: sorted(u for u in us if u)
                             for t, us in sorted(cols.items()) if us}
                            for (ch, tid), cols in _subs.items() if cols}}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def load_subs():
    """启动时恢复成员级订阅；坏文件当空。

    老版本没有 ws_subs.json：用已经恢复出来的 _monitors 补成「会话级订阅」，
    这样「停止全部监控」清得掉、个人「停止监控」也不会误清别人的设置。
    反过来文件里有、开关里没有的栏目也补回监控，避免两处记录不同步。
    """
    p = sub_file()
    data = {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8-sig"))
        for k, cols in ((obj or {}).get("sessions") or {}).items():
            if not isinstance(k, str) or "|" not in k or not isinstance(cols, dict):
                continue
            ch, tid = k.split("|", 1)
            if ch not in ("group", "private") or not tid:
                continue
            got = {}
            for t, us in cols.items():
                if t in ws.TYPES and isinstance(us, list):
                    uids = {str(u) for u in us if u}
                    if uids:
                        got[t] = uids
            if got:
                data[(ch, tid)] = got
    except Exception:
        data = {}
    with _lock:
        _subs.clear()
        _subs.update(data)
        for key, types in _monitors.items():
            if not types:
                continue
            cols = _subs.setdefault(key, {})
            for t in types:
                cols.setdefault(t, {""})
        for key, cols in _subs.items():
            if cols:
                _monitors.setdefault(key, set()).update(cols)
    return len(data)


def subscribers_of(channel, target_id):
    """本会话每个栏目分别被谁订阅（调试 / 测试用，返回深拷贝）。"""
    with _lock:
        cols = _subs.get((channel, str(target_id))) or {}
        return {t: set(us) for t, us in cols.items()}


def my_types(channel, target_id, user):
    """这个人在本会话还订阅着哪些栏目（返回按 TYPES 顺序排列的键列表）。"""
    if not user:
        return []
    uid = str(user)
    with _lock:
        cols = _subs.get((channel, str(target_id))) or {}
        have = {t for t, us in cols.items() if uid in us}
    return [t for t in ws.TYPES if t in have]


def at_users_for(channel, target_id, ws_type=None):
    """这一条推送该 @ 谁。

    ws_type 给了就【只 @ 订阅了这一栏的人】：
      - 甲乙都要「警报」→ 同一条警报消息里一起 @ 甲乙（内容相同就一起 @）；
      - 甲要「警报」、乙要「裂缝」→ 本来就是两条独立消息（推送按栏目拆开串行发），
        各 @ 各的人，不会把乙 @ 到他要的裂缝消息上。
    该栏目只有会话级订阅（主推开关 / 自动注册）时没有具体人，不 @ 任何人。
    ws_type 为 None 时退回「本会话所有订阅者」。
    老数据没有订阅者表时退回单个发起人记录，保证升级后照常 @ 得到人。
    """
    key = (channel, str(target_id))
    with _lock:
        cols = _subs.get(key) or {}
        if ws_type is not None:
            uids = sorted({u for u in (cols.get(ws_type) or ()) if u})
            if uids:
                return uids
            if cols:
                return []
        else:
            uids = sorted({u for us in cols.values() for u in us if u})
            if uids:
                return uids
        one = _at_users.get(key)
    return [one] if one else []


def at_users_of(channel, target_id):
    """本会话所有开过监控的人（不分栏目）。"""
    return at_users_for(channel, target_id, None)



_sender_at = [None]      # 缓存的探测结果：sender 是否收第四参 at_user


def _sender_takes_at():
    """sender 是否支持 at_user 第四参。

    官方机器人主动消息不能 @ 人，注入的 sender 只有三个参数；OneBot 的可以。
    缓存一次（set_sender 时复位），别每条推送都反射一遍。
    """
    if _sender_at[0] is None:
        try:
            params = inspect.signature(_sender).parameters.values()
            _sender_at[0] = any(p.kind is p.VAR_POSITIONAL or p.name == "at_user"
                                for p in params)
        except (TypeError, ValueError):
            _sender_at[0] = False
    return _sender_at[0]


def _send_one(ch, tid, text, at_user=None):
    """调后端 sender 发一条：能给就把发起人全 @ 上，后端不支持就退回三参。

    at_user 允许单个号或号列表（群里多人开监控时逐个 @）。
    """
    if at_user and _sender_takes_at():
        return _sender(ch, tid, text, at_user)
    return _sender(ch, tid, text)


def effective_types(key, raw_types):
    """本会话真正要监控的栏目：原始监控集合 + 显式开启 - 显式关闭。"""
    with _lock:
        sw = dict(_switches.get(key) or {})
    out = set(raw_types or ())
    for t, v in sw.items():
        if v:
            out.add(t)
        else:
            out.discard(t)
    return out


def effective_priority(key, types):
    """本会话真正的主推栏目：全局默认 + 显式开启 - 显式关闭，且必须在监控范围内。"""
    with _lock:
        sw = dict(_switches.get(key) or {})
    base = set(priority_types())
    for t, v in sw.items():
        if v:
            base.add(t)
        else:
            base.discard(t)
    base &= set(types or ())
    return [t for t in ws.TYPES if t in base]


def switch_status_text(channel, target_id):
    """「主推状态」指令的回复正文。"""
    if channel is None or target_id in (None, ""):
        return "（无法识别会话，无法查看开关）"
    key = (channel, str(target_id))
    with _lock:
        sw = dict(_switches.get(key) or {})
        mon = set(_monitors.get(key) or ())
    types = effective_types(key, mon)
    prio = effective_priority(key, types)
    lines = ["⚙️ 本会话主推开关",
             "监控中：%s" % ("、".join(ws.TYPES[t][0] for t in ws.TYPES if t in types) or "（无）"),
             "主推：%s" % ("、".join(ws.TYPES[t][0] for t in prio) or "（无）")]
    if sw:
        on = [ws.TYPES[t][0] for t in ws.TYPES if sw.get(t) is True]
        off = [ws.TYPES[t][0] for t in ws.TYPES if sw.get(t) is False]
        if on:
            lines.append("已手动开启：%s" % "、".join(on))
        if off:
            lines.append("已手动关闭：%s" % "、".join(off))
    else:
        d = set(priority_types())
        lines.append("未单独设置，按默认主推（%s）" %
                     ("、".join(ws.TYPES[t][0] for t in ws.TYPES if t in d) or "无"))
    lines.append("设置会存盘，重启后自动恢复；发「活动=开启」「警报=关闭」可随时改。")
    return "\n".join(lines)


def group_monitor_text(channel, target_id):
    """「监控」（群内不带 @）的回复正文：本群监控状态总览。

    列出本群在监控哪些栏目、各栏目订阅人数（区分具体人 / 会话级）、主推栏目、
    维度过滤与发起人；空群也如实说明，不瞎编。
    """
    if channel is None or target_id in (None, ""):
        return "（无法识别会话，无法查看监控；群聊需在白名单内）"
    key = (channel, str(target_id))
    with _lock:
        mon = set(_monitors.get(key) or ())
        subs = {t: set(us) for t, us in (_subs.get(key) or {}).items()}
        ff = {c: list(toks) for c, toks in (_fissure_filters.get(key) or {}).items() if toks}
        at_uid = _at_users.get(key)
    types = effective_types(key, mon)
    prio = effective_priority(key, types)
    type_names = [ws.TYPES[t][0] for t in ws.TYPES if t in types]
    lines = ["📊 本群世界状态监控状态", ""]
    if not type_names:
        lines.append("（本群当前没有任何监控栏目）")
    else:
        lines.append("监控栏目（共 %d）：%s" % (len(type_names), "、".join(type_names)))
    if prio:
        lines.append("主推栏目：%s" % "、".join(ws.TYPES[t][0] for t in prio))
    # 各栏目订阅人数：区分「具体人」与「会话级（主推开关带来的，无具体人）」
    if subs:
        lines.append("")
        lines.append("各栏目订阅人数：")
        for t in ws.TYPES:
            us = subs.get(t)
            if not us:
                continue
            real = sorted(u for u in us if u)
            sess = "" in us
            parts = []
            if real:
                parts.append("%d 人" % len(real))
            if sess:
                parts.append("会话级")
            lines.append("  · %s：%s" % (ws.TYPES[t][0], " + ".join(parts) if parts else "0 人"))
    if ff:
        parts = []
        for c, toks in ff.items():
            cn = ws.TYPES.get(c, (c,))[0]
            parts.append("%s %s" % (cn, " ".join(toks)))
        lines.append("")
        lines.append("维度过滤：%s" % "；".join(parts))
    if at_uid:
        lines.append("监控发起人：已记录（推送时 @）")
    lines.append("")
    lines.append("· 发「监控 警报」等继续添加栏目")
    lines.append("· 发「停止监控」停你自己的；「停止全部监控」清本群所有人")
    return "\n".join(lines)


def my_monitor_text(channel, target_id, user):
    """「@机器人 监控」的回复正文：查看@的人（发指令者）自己的监控信息。

    user 为空（拿不到发指令的人）时如实说明、不瞎编。
    """
    if not user:
        return "（无法识别你是谁，无法查看你的监控；请 @ 机器人 再试一次）"
    uid = str(user)
    subs, ff = {}, {}
    if channel:
        key = (channel, str(target_id))
        with _lock:
            subs = {t: set(us) for t, us in (_subs.get(key) or {}).items()}
            ff = {c: list(toks) for c, toks in (_fissure_filters.get(key) or {}).items() if toks}
    my = [t for t in ws.TYPES if uid in (subs.get(t) or ())]
    names = [ws.TYPES[t][0] for t in my]
    lines = ["👤 你的世界状态监控（本%s）" % ("群" if channel == "group" else "私聊"), ""]
    if not names:
        lines.append("（你还没有开启任何监控）")
    else:
        lines.append("你订阅了 %d 个栏目：%s" % (len(names), "、".join(names)))
    my_ff = {c: toks for c, toks in ff.items() if c in my}
    if my_ff:
        cols = "、".join(ws.TYPES[c][0] for c in my_ff)
        parts = []
        for c, toks in my_ff.items():
            cn = ws.TYPES.get(c, (c,))[0]
            parts.append("%s %s" % (cn, " ".join(toks)))
        lines.append("本会话维度过滤（影响你订阅的%s）：%s" % (cols, "；".join(parts)))
    lines.append("")
    lines.append("· 发「监控 警报」等继续添加")
    lines.append("· 发「停止监控」停你名下全部；「停止监控 警报」只停警报")
    return "\n".join(lines)


def _sig(text):
    """内容签名：用来判断「世界状态有没有变化」，变了才有必要主动推。"""
    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()


def stable_sig(text):
    """【周期推送专用】剔除倒计时后再签名。

    卡片里每条都带「剩余：5分30秒」，用原文签名的话每 10 秒都变，
    「内容不变不重发」这道闸形同虚设，实际会每 5 分钟固定刷一条。
    把时长串换成 <T> 后，只有条目增删 / 奖励变化 / 状态变化才会触发推送。
    """
    return _sig(_DURATION.sub("<T>", text or ""))


def set_sender(fn):
    """注入主动发送回调（QQ 后端启动时调用）。

    换 sender 视为换了一条通道，清掉上次成功记录，避免新通道因为
    「内容跟旧通道一样」被误判为无需推送。
    """
    global _sender, _next_probe
    _sender = fn
    _next_probe = 0.0
    _sender_at[0] = None         # 换了通道，重新探测它收不收 at_user
    with _lock:
        _last_sent.clear()
        _priority_sent_at.clear()


def _flog(msg):
    """写流程日志（复用 qq_official 注入到 wm.LOG_HOOK 的 flog）；未注入时静默。"""
    hook = getattr(wm, "LOG_HOOK", None)
    if hook:
        try:
            hook("WS " + str(msg))
        except Exception:
            pass


def _save_last_sent():
    """把按栏目去重记录落盘（原子写），重启后能续上，避免把稳定内容再刷一遍。"""
    try:
        with _lock:
            data = {}
            for (ch, tid), m in _last_sent.items():
                data["%s|%s" % (ch, tid)] = {
                    t: [sig, round(float(ts), 3)]
                    for t, (sig, ts) in m.items()
                }
        LAST_SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LAST_SENT_FILE.with_name(LAST_SENT_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LAST_SENT_FILE)
    except Exception as e:
        _flog("去重记录落盘失败（忽略）: %s" % e)


def restore_last_sent():
    """启动时从磁盘恢复去重记录（须在 set_sender 清空之后调用）。

    这样即使机器人重启，只要世界状态内容没变，就不会把首轮监控内容再全发一遍；
    内容确实变了（签名不同）仍会正常推送，不影响「有变化才发」。
    """
    try:
        if not LAST_SENT_FILE.exists():
            return
        with open(LAST_SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _flog("读取去重记录失败（当作全新启动）: %s" % e)
        return
    restored = {}
    try:
        for key, m in (data or {}).items():
            if "|" not in key or not isinstance(m, dict):
                continue
            ch, tid = key.split("|", 1)
            restored[(ch, tid)] = {
                t: (v[0], float(v[1]))
                for t, v in m.items()
                if isinstance(v, (list, tuple)) and len(v) == 2
            }
    except Exception as e:
        _flog("解析去重记录失败（当作全新启动）: %s" % e)
        return
    with _lock:
        _last_sent.clear()
        _last_sent.update(restored)
    _flog("恢复去重记录：%d 个会话 / %d 条栏目" % (
        len(restored), sum(len(m) for m in restored.values())))


def start_monitor(channel, target_id, types, filters=None, at_user=None):
    """开启某会话的监控；types 为 worldstate.TYPES 的键集合。

    filters：可选 {栏目键: [过滤 token...]}，仅对 fissures / steel 有意义；
    写盘后重启自动恢复。
    at_user：发指令的人（群成员号 / 用户 OpenID）。推送时 @ 他，
             让「@机器人 监控 警报」开出来的推送找得到是谁开的。
    """
    key = (channel, str(target_id))
    uid = str(at_user) if at_user else ""
    with _lock:
        # 会话里可能不止一个人开过监控：这里只合并，不覆盖别人的栏目，
        # 否则群里 A 开了警报、B 再开裂缝，A 的警报就被顶掉了。
        _monitors.setdefault(key, set()).update(types)
        cols = _subs.setdefault(key, {})
        for t in types:
            cols.setdefault(t, set()).add(uid)
        _save_subs()
        # 本次指令点到的栏目以本次为准：带条件就设条件，不带条件就清掉旧条件
        # （否则「监控 裂缝」会静默沿用上次的「后纪中断」，看起来像没生效）
        ff = _fissure_filters.setdefault(key, {})
        for c in types:
            toks = (filters or {}).get(c)
            if toks:
                ff[c] = list(toks)
            else:
                ff.pop(c, None)
        for c, toks in (filters or {}).items():
            if toks and c not in types:
                ff[c] = list(toks)
        if not ff:
            _fissure_filters.pop(key, None)
        # 有过滤就落盘；即使本次是把条件清空，只要磁盘上已有文件也要重写，
        # 否则磁盘会残留上一次的条件，重启后又冒出来（不新建文件，避免无谓写盘）。
        if _fissure_filters or filter_file().exists():
            _save_filters()
        # 记住是谁开的监控：推送时 @ 回去（存盘，重启后照样 @ 得到人）
        if at_user and _at_users.get(key) != str(at_user):
            _at_users[key] = str(at_user)
            _save_at_users()
    ws.ensure_active()               # 拉起世界状态后台刷新
    _ensure_push_thread()


def stop_monitor(channel, target_id, types=None, user=None, everyone=False):
    """停止某会话的监控；返回是否确实停掉了什么。

    三种粒度，**都只作用于本条 (channel, target_id)**，绝不跨群、不跨私聊：
      everyone=True            「停止全部监控」：清本会话所有成员、所有栏目与开关；
      user + types             「停止监控 警报」：只撤这个人对这些栏目的订阅；
      user 不给 types          「停止监控」：只撤这个人名下的全部订阅；
      user 也不给（私聊 / 认不出人）：退回整会话清理。
    某个栏目撤到没人订阅才算真停（清过滤条件与去重记录），别人还在看就留着。
    """
    key = (channel, str(target_id))
    uid = str(user) if user else ""
    if everyone or not uid:
        with _lock:
            _pending.pop(key, None)
            _attached_at.pop(key, None)
            _last_sent.pop(key, None)
            _seen_priority.pop(key, None)
            _priority_sent_at.pop(key, None)
            had_switch = bool(_switches.pop(key, None))
            had_filter = bool(_fissure_filters.pop(key, None))
            had_at = _at_users.pop(key, None) is not None
            had_sub = bool(_subs.pop(key, None))
            removed = _monitors.pop(key, None) is not None
            if had_switch:
                _save_switches()
            if had_filter:
                _save_filters()
            if had_at:
                _save_at_users()
            if had_sub:
                _save_subs()
            changed = removed or had_switch or had_filter or had_at or had_sub
        if changed:
            _save_last_sent()        # 把「已移除该会话」的去重记录落盘
        return changed

    with _lock:
        cols = _subs.get(key)
        if not cols:
            return False
        target = [t for t in (types or cols.keys()) if t in cols]
        touched, gone = False, []
        for t in target:
            us = cols.get(t)
            if not us or uid not in us:
                continue
            us.discard(uid)
            touched = True
            if not us:               # 最后一个人撤了 → 这个栏目才真停
                cols.pop(t, None)
                gone.append(t)
        if not touched:
            return False
        if gone:
            mon = _monitors.get(key)
            if mon is not None:
                mon -= set(gone)
                if mon:
                    _monitors[key] = mon
                else:
                    _monitors.pop(key, None)
            _pending.pop(key, None)
            ff = _fissure_filters.get(key)
            if ff:
                for t in gone:
                    ff.pop(t, None)
                if not ff:
                    _fissure_filters.pop(key, None)
                _save_filters()
            ls = _last_sent.get(key)
            if ls:
                for t in gone:
                    ls.pop(t, None)
                if not ls:
                    _last_sent.pop(key, None)
            sp = _seen_priority.get(key)
            if sp:
                for t in gone:
                    sp.pop(t, None)
        if not cols:
            _subs.pop(key, None)
        # 推送 @ 的人走了：还有别人订阅就换成别人，没人了就清掉
        if _at_users.get(key) == uid:
            left = sorted({u for us in cols.values() for u in us if u})
            if left:
                _at_users[key] = left[0]
            else:
                _at_users.pop(key, None)
            _save_at_users()
        _save_subs()
    if gone:
        _save_last_sent()
    return True


def list_monitors():
    with _lock:
        return {k: sorted(v) for k, v in _monitors.items()}


def pending_count():
    """当前暂存的会话数（测试 / 状态查看用）。"""
    with _lock:
        return len(_pending)


def clear_pending():
    with _lock:
        _pending.clear()
        _attached_at.clear()
        _last_sent.clear()
        _seen_priority.clear()
        _priority_sent_at.clear()


def auto_start_priority(groups):
    """WS_AUTO_PRIORITY=1 时，对白名单群注册主推监控。

    只【合并】栏目，不覆盖用户手动开的监控范围；已经含主推栏目的会话跳过。
    返回实际改动的会话数。默认关，调用方可以无条件调用。
    """
    if not auto_priority_enabled():
        return 0
    prio = priority_types()
    if not prio or not groups:
        return 0
    changed = 0
    with _lock:
        for gid in groups:
            key = ("group", str(gid))
            cur = _monitors.get(key)
            hit = False
            if cur is None:
                _monitors[key] = set(prio)
                hit = True
            elif not set(prio) <= cur:
                cur.update(prio)
                hit = True
            if not hit:
                continue
            changed += 1
            # 自动注册属于会话级（没有具体发起人），记成空 uid 订阅
            cols = _subs.setdefault(key, {})
            for t in prio:
                cols.setdefault(t, set()).add("")
    if changed:
        _save_subs()
        ws.ensure_active()
        _ensure_push_thread()
    return changed


def notify_active_ready(channel, target_id):
    """收到 GROUP_MSG_RECEIVE / C2C_MSG_RECEIVE：接收方刚开启主动消息通知。

    官方主动消息是「按群 / 按人」在机器人资料卡单独开的开关，开关一打开就
    立刻解除 5 分钟退避并补推一轮，不用干等；同时清掉旧暂存，免得通道通了
    之后用户查询时又被贴一遍过期内容。没开监控时 push_once 会直接返回 0。
    """
    global _next_probe
    if target_id in (None, ""):
        return 0
    key = (channel, str(target_id))
    with _lock:
        _next_probe = 0.0
        _last_sent.pop(key, None)
        _pending.pop(key, None)
        _priority_sent_at.pop(key, None)   # 通道刚通，别被主推防抖挡住
    try:
        return push_once()
    except Exception:
        return 0


def _truncate(s, limit):
    """按【整行】截断（不切断一条数据、不切坏 UTF-8），并注明被省略。"""
    s = s or ""
    if limit <= 0 or len(s.encode("utf-8")) <= limit:
        return s
    out, used = [], 0
    for line in s.split("\n"):
        need = len(line.encode("utf-8")) + 1
        if used + need > limit:
            break                     # 这一行放不下就停，绝不留半行
        out.append(line)
        used += need
    body = "\n".join(out).rstrip()
    if not body:                      # 连第一行都超限：退化为按字节安全切
        body = s.encode("utf-8")[:limit].decode("utf-8", "ignore").rstrip()
    return body + "\n…（监控内容过长已省略，发「监控 <词>」可缩小范围）"


def pending_for(channel, target_id, covered=None):
    """取本会话待捎带的监控内容；没有 / 过期 / 太频繁 / 已被本次查询覆盖则返回空串。

    不消耗内容（下次查询仍可取），只用 WS_ATTACH_GAP 限频，避免用户连发几条时
    每条都重复贴一遍同样的监控。

    covered：本次查询已经包含的栏目集合。监控的内容如果全在里面（例如监控「裂缝」
    时用户正好查「裂缝」），就没有附带的必要，直接跳过、不重复贴一遍。
    """
    if not attach_enabled() or channel is None or target_id in (None, ""):
        return ""
    key = (channel, str(target_id))
    now = time.time()
    with _lock:
        p = _pending.get(key)
        if not p:
            return ""
        if now - float(p.get("at") or 0) > attach_ttl():
            _pending.pop(key, None)       # 太久了，别把半小时前的内容当现状
            return ""
        body = _pick(p, covered)
        if not body:
            return ""
        if now - _attached_at.get(key, 0.0) < attach_gap():
            return ""
        _attached_at[key] = now
        return _truncate(body, attach_max_bytes())


def _pick(p, covered):
    """从暂存记录里挑出本次要附带的内容（剔除已被本次查询覆盖的栏目）。"""
    parts = p.get("parts")
    if parts:
        keep = [(t, x) for t, x in parts if not (covered and t in set(covered))]
        if not keep:
            return ""
        return "\n\n".join(x for _, x in keep)
    # 兼容没有 parts 的旧结构（例如测试直接塞进来的）
    have = set(p.get("types") or ())
    if covered and have and have <= set(covered):
        return ""
    return p.get("text") or ""


def _col_text(state, t, key):
    """格式化某栏目；对 fissures / steel / bounty 应用本会话的维度过滤（key 为会话）。"""
    toks = (_fissure_filters.get(key) or {}).get(t) if key else None
    try:
        return ws.TYPES[t][1](state, toks) if toks else ws.TYPES[t][1](state)
    except Exception:
        return ""


def _parts_of(state, types, key=None):
    """按栏目拆分格式化结果，便于附带时精确剔除重复栏目（顺序按 TYPES 展示序）。

    key：会话键；提供时 fissures / steel 会应用该会话的维度过滤。
    """
    out = []
    for t in ws.TYPES:
        if t not in types:
            continue
        txt = _col_text(state, t, key)
        if txt:
            out.append((t, txt))
    return out


# ---------------- 事件主推：新警报 / 新活动立刻推 ----------------
def _event_keys(state, t, key=None):
    """把某栏目的当前条目压成一组稳定键，用来判断「有没有新东西」。

    键里含 expiry（条目的固定结束时刻，不会随刷新漂移）和奖励 / 描述，
    所以换节点、换奖励、换活动描述都算新条目；倒计时不进键。
    返回 None 表示本轮取不到可靠数据，调用方应跳过、不动基线。
    """
    try:
        if t == "alerts":
            out = set()
            for a in (state.get("alerts") or []):
                m = a.get("mission") or {}
                rw = a.get("reward") or {}
                out.add("|".join(str(x) for x in (
                    m.get("node"), m.get("type"), m.get("faction"),
                    ",".join(str(i) for i in (rw.get("items") or [])),
                    rw.get("credits"), a.get("expiry"))))
            return out
        if t == "events":
            return {"|".join(str(x) for x in (e.get("description"), e.get("expiry")))
                    for e in (state.get("events") or [])}
        # 其它栏目没有「条目列表」结构：用稳定化后的整栏文本当一个键，
        # 粒度粗一些，但任何栏目都能被配成主推。
        # fissures / steel 应用本会话维度过滤，使过滤后的「内容变化」才判定新增。
        toks = (_fissure_filters.get(key) or {}).get(t) if key else None
        txt = ws.TYPES[t][1](state, toks) if toks else ws.TYPES[t][1](state)
        return {stable_sig(txt)} if txt else set()
    except Exception:
        return None


# ---------------- 监控推送的文字标签（图文混排时放在图片前面） ----------------
# 只做关键字匹配，不碰任何渲染逻辑，原图完全不变。
# 顺序即优先级：更具体、或被更宽泛词包含的来源必须排前面
# （如"赏金轮换"含"赏金"、"钢铁裂缝"含"裂缝"）。
_MONITOR_LABEL_RULES = [
    # —— 具体来源优先 ——
    (("赏金轮换", "bountyrot"), "赏金轮换"),
    (("赏金", "悬赏", "bounty"), "赏金"),
    (("瓦奇娅", "varzia", "原初"), "瓦奇娅"),
    (("虚空商人", "baro", "白老头"), "商人"),
    (("信条武器", "信条", "tenet"), "信条武器"),
    (("终幕武器", "终幕", "coda"), "终幕武器"),
    (("钢铁裂缝", "steel"), "钢铁裂缝"),
    (("裂缝", "裂隙", "fissure"), "裂缝"),
    (("后纪中断", "中断", "强袭", "assault"), "中断"),
    (("突击", "sortie"), "突击"),
    (("仲裁", "arbitration", "arbi"), "仲裁"),
    # —— 平原各区域：每个区域独立标签（否则正文里内嵌的「赏金 ·」行会让
    #    平原栏目全被误标成「赏金」，监控全部时看起来一堆重复）——
    (("希图斯", "cetus"), "希图斯"),
    (("奥布山谷", "vallis", "山谷"), "奥布山谷"),
    (("魔胎之境", "cambion"), "魔胎之境"),
    (("双衍王境", "duviri", "王境"), "双衍王境"),
    # 回廊排在「双衍王境」之后：双衍王境块正文里内嵌了回廊明细，
    # 但标签只看首行，所以互不干扰；回廊独立推送时才落到这条。
    (("无尽回廊", "回廊", "灵化", "incarnon"), "回廊"),
    (("扎里曼", "zariman"), "扎里曼"),
    (("平原", "昼夜", "cycle"), "平原昼夜"),
    # —— 其余 ——
    (("地图", "星图"), "地图"),
    (("警报", "alert"), "警报"),
    (("活动", "event"), "活动"),
    (("入侵", "invasion"), "入侵"),
    (("虚空风暴", "虚空军团", "void"), "虚空风暴"),
    (("午夜电波", "电波", "nightwave", "夜波"), "电波"),
    (("Darvo", "特惠", "deal"), "特惠"),
    (("全局加成", "加成", "双倍"), "加成"),
    (("信使前哨", "outpost", "信使"), "信使前哨"),
    (("集团任务", "syndicate", "集团"), "集团任务"),
    (("建造进度", "construction", "建造"), "建造进度"),
    (("新闻", "news"), "新闻"),
]

# 平原各区域标签：多区域拼接（平原昼夜组合视图）时另判为「平原昼夜」。
_REGION_LABELS = ("希图斯", "奥布山谷", "魔胎之境", "双衍王境", "扎里曼")

# 栏目规范名（取自 worldstate.TYPES 的第二列），长的排前面供兜底匹配用。
_TYPE_NAMES_LONGEST_FIRST = tuple(sorted(
    {v[0] for v in ws.TYPES.values() if v and v[0]}, key=len, reverse=True))


def monitor_label(text):
    """监控推送图文混排时用的文字标签：返回「【来源监控信息如下】」。

    **只匹配首行**：每个栏目的标题都在首行（「希图斯（地球平原）」「🔄 赏金轮换」
    「💎 瓦奇娅」…），而正文里会内嵌「赏金 · …」这类跨栏目行——若按全文匹配，
    平原栏目会被正文里的「赏金」误标成「赏金」，监控全部时好几条全叫赏金、
    看着像重复。只认首行，每个栏目就能拿到自己的标签。

    规则按 _MONITOR_LABEL_RULES 顺序（更具体的来源在前），命中第一个即返回；
    一个都不命中返回通用「【监控信息如下】」。纯关键字匹配，不动任何渲染，原图不变。
    """
    text = text or ""
    head = text.split("\n", 1)[0]
    low = head.lower()
    matched = None
    for keys, source in _MONITOR_LABEL_RULES:
        for k in keys:
            if k and k.lower() in low:
                matched = source
                break
        if matched:
            break
    if not matched:
        # 兜底：直接拿 TYPES 里的栏目规范名去匹配首行（长的优先，避免「赏金」
        # 抢了「赏金轮换」）。这样以后 TYPES 新增栏目、忘了补规则表时，也不会
        # 掉进通用标签——本次「突击 / 仲裁」显示成通用标签就是这个坑。
        for name in _TYPE_NAMES_LONGEST_FIRST:
            if name and name in head:
                matched = name
                break
    if not matched:
        return "【监控信息如下】"
    # 平原昼夜 = 多个区域拼接而成的组合视图：首行只会落成第一个区域，
    # 整段出现 ≥2 个区域标题时归为「平原昼夜」，避免和各区域单栏目撞名。
    if matched in _REGION_LABELS:
        hits = sum(1 for r in _REGION_LABELS if r in text)
        if hits >= 2:
            return "【平原昼夜监控信息如下】"
    return "【%s监控信息如下】" % matched


def _push_priority(ch, tid, key, state, prio, now):
    """跑一轮主推检测，返回三态：1=成功发出 / 0=无新增或防抖跳过 / -1=发送失败。

    -1 表示「有新条目但没发出去，内容已进暂存」，调用方本轮要跳过周期推送，
    否则周期内容会覆盖暂存；而周期里又剔除了主推栏目，等于新警报在
    「主动权限没开、只能随消息捎带」时凭空消失。

    基线只在推送成功后才推进：发失败（40034105 / 频控 / 网络）时保留旧基线，
    下轮仍判定为「新增」自动重试，不会把新警报悄悄吞掉。
    """
    with _lock:
        seen = dict(_seen_priority.get(key) or {})
    fresh = {}
    for t in prio:
        keys = _event_keys(state, t, key)
        if keys is None:
            return 0                  # 数据不可靠，本轮不动基线
        fresh[t] = keys
    if any(t not in seen for t in prio):
        # 首次只记基线：开监控时不无故往群里发一条「当前现状」，
        # 之后出现新条目才推。
        with _lock:
            _seen_priority[key] = fresh
        return 0
    added_by_type = {t: fresh[t] - seen.get(t, set()) for t in prio}
    changed = [t for t in prio if added_by_type[t]]
    if not changed:
        # 没有新增：静默消化已结束的条目，不推「活动结束」刷屏
        with _lock:
            _seen_priority[key] = fresh
        return 0
    with _lock:
        last_at = _priority_sent_at.get(key, 0.0)
    if now - last_at < priority_min_gap():
        return 0                      # 防抖；基线仍是旧的，下轮还能补
    if query_busy():
        return 0                      # 用户查询优先：本轮不发，基线不动，下轮补
    # 主推也【按栏目拆分】成一条条发：每条的内容首行就是栏目标题
    # （「📢 警报（共 N）」「🎉 活动（共 N）」），send_active 才能匹配出
    # 【警报监控信息如下】/【活动监控信息如下】。以前拼成一条带
    # 「🚨 世界状态主推 · 新增 N 条」抬头的消息，抬头不是栏目名 →
    # 标签永远掉进通用的【监控信息如下】，而且和「分开一条条发」的约定相悖。
    total_added = sum(len(v) for v in added_by_type.values())
    sent_types, failed_parts, sent_any = [], [], False
    for t in changed:
        try:
            body = _col_text(state, t, key)
        except Exception:
            body = ""
        if not body:
            continue
        x = body + ("\n\n（新增 %d 条 · 即时推送，不等周期；发「停止监控」可关闭）"
                    % total_added)
        ok = False
        if _sender is not None and now >= _next_probe:
            try:
                ok = bool(_send_one(ch, tid, x, at_users_for(ch, tid, t)))
            except Exception:
                ok = False          # 被频控 / 网络抖动都不致命，下个周期再试
            _note_probe(ok)
        if ok:
            sent_types.append(t)
            sent_any = True
            if push_interval():
                time.sleep(push_interval())   # 排队发：相邻两条之间稍歇，防官方频控
        else:
            failed_parts.append((t, body))
    with _lock:
        if not failed_parts:
            # 全发成功：整份基线推进（顺带静默消化已结束的条目）
            _seen_priority[key] = fresh
            if sent_any:
                _priority_sent_at[key] = now
                _pending.pop(key, None)
            return 1 if sent_any else 0
        # 部分/全部失败：只有发成功的栏目推进基线，失败栏目保留旧基线，
        # 下轮仍判定为「新增」自动重试，不会把新警报悄悄吞掉。
        prev_seen = dict(_seen_priority.get(key) or {})
        for t in sent_types:
            prev_seen[t] = fresh[t]
        _seen_priority[key] = prev_seen
        if sent_any:
            _priority_sent_at[key] = now
        prev = _pending.get(key) or {}
        merged = list(prev.get("parts") or []) if prev.get("priority") else []
        have = {t for t, _ in merged}
        merged.extend((t, body) for t, body in failed_parts if t not in have)
        _pending[key] = {
            "text": "\n\n".join(b for _, b in merged),
            "at": now,
            "fails": int(prev.get("fails") or 0) + 1,
            "types": {t for t, _ in merged},
            "parts": merged,
            "priority": True,         # 标记：别被周期推送的成功/失败覆盖掉
        }
        return -1


def _ensure_push_thread():
    global _push_thread
    with _lock:
        if _push_thread and _push_thread.is_alive():
            return
        _stop.clear()
        t = threading.Thread(target=_push_loop, daemon=True, name="ws-push")
        _push_thread = t
        t.start()


def _push_loop():
    while not _stop.is_set():
        if _stop.wait(ws.WS_PUSH_SECONDS):
            break
        try:
            push_once()
        except Exception:
            pass          # 单轮异常不终止推送线程


def push_once():
    """跑一轮推送（抽出来便于离线测试）：主动成功即清暂存，失败则攒下来。

    WS_PUSH_SECONDS 只是「检查节拍」，不等于每轮都发消息。两路推送分开：
      ① 主推（警报 / 活动等）：出现【新条目】立刻单独发一条，只受
         WS_PRIORITY_MIN_GAP 防抖，不受下面 300 秒周期限制；
      ② 周期（其余栏目）：签名剔除倒计时，倒计时跳动不再算「内容变了」；
         内容真变了且距上次推送超过 WS_ACTIVE_MIN_GAP 才发。
    同一轮主推成功就不再发周期那条，避免连着两条刷屏；主推没发出去时本轮也
    跳过周期，防止周期内容覆盖掉主推暂存（那样新警报会被捎带通道漏掉）。
    主动通道不通时仍每轮刷新暂存（保鲜 + 失败计数），保证被动捎带拿到最新内容。

    每轮都按【会话】重算栏目：effective_types 应用「活动=开启 / 警报=关闭」
    这类开关，effective_priority 应用该会话对主推栏目的覆盖，互不影响其它群。
    """
    with _lock:
        # 有「显式开启」开关但监控注册表还没建起来的会话也要能推
        keys = sorted(set(_monitors) |
                      {k for k, sw in _switches.items() if any(sw.values())})
        items = [(k, effective_types(k, _monitors.get(k) or ())) for k in keys]
    if not items:
        return 0
    # 推送前确保数据新鲜；监控期间也持续「触碰」活动，保住后台刷新线程
    state = ws.ensure_fresh()
    if state is None:
        return 0
    sent = 0
    skip_full = priority_skip_in_full()
    for (ch, tid), types in items:
        ws.activity_touch()
        key = (ch, tid)
        now = time.time()
        types = set(types)
        # 每个会话单独算主推：聊天指令设的开关覆盖全局默认
        prio = effective_priority(key, types)

        # ① 事件主推：新警报 / 新活动立刻推
        prio_res = _push_priority(ch, tid, key, state, prio, now) if prio else 0
        if prio_res == 1:
            sent += 1
            continue                  # 本轮已经发过一条，周期那条等下一轮
        if prio_res == -1:
            continue                  # 主推没发出去，别让周期覆盖它的暂存

        # ② 周期推送：主推栏目默认从这里剔除，避免同一条警报推两遍。
        #    按栏目【拆分】成多条独立消息发送：每条 = 「【X监控信息如下】」文字标签
        #    （send_active 内由内容关键字自动匹配，如赏金/裂缝/商人…）+ 该栏目卡片图
        #    （图在下）。这样监控「全部」也不会把十几个栏目塞进一张超长图——超长图
        #    既容易加载失败、又会被 render_card 的页数上限截断（「超出了就没有了」）。
        #    每个栏目各自一张小图，清晰可靠。
        #    发送是【串行排队】的：本循环在本推送线程里一条条发，且两条之间停
        #    WS_PUSH_INTERVAL 秒，绝不会「一起发」刷屏；回复消息也是一问一答天然串行，
        #    所以全机器人所有外发消息都是一条条排队，不杂乱。
        #    各栏目按【内容签名】独立去重：只有变化的栏目才重发，避免每条都刷屏。
        full = set(types)
        if prio and skip_full:
            full -= set(prio)
        parts = _parts_of(state, full, key)
        if not parts:
            continue
        split_max = push_split_max()
        interval = push_interval()
        sent_any = False
        for t, x in parts[:split_max]:
            if query_busy():
                break                       # 用户查询优先：剩下的下轮再发
            sig_t = stable_sig(x)
            with _lock:
                rec = (_last_sent.get(key) or {}).get(t)
            if rec and rec[0] == sig_t:
                continue                    # 该栏目内容没变，跳过
            if rec and now - rec[1] < active_min_gap():
                continue                    # 该栏目刚推过，太频繁，跳过
            ok = False
            if _sender is not None and now >= _next_probe:
                try:
                    ok = bool(_send_one(ch, tid, x, at_users_for(ch, tid, t)))
                except Exception:
                    ok = False              # 被频控 / 网络抖动都不致命，下个周期再试
                _note_probe(ok)             # 只有真去试了才更新探测时刻
            with _lock:
                ls = _last_sent.setdefault(key, {})
                if ok:
                    ls[t] = (sig_t, now)
                    if not (_pending.get(key) or {}).get("priority"):
                        _pending.pop(key, None)  # 主动通道通了，不用再捎带
                    sent_any = True
                else:
                    prev = _pending.get(key) or {}
                    if prev.get("priority"):
                        # 主推暂存优先：只刷新时间与失败计数，不覆盖内容
                        prev["at"] = now
                        prev["fails"] = int(prev.get("fails") or 0) + 1
                        _pending[key] = prev
                    else:
                        # 多个栏目同时发不出去时要【累加】暂存：以前这里整份覆盖，
                        # 只留下最后失败的那个栏目，前面失败的栏目就悄悄丢了
                        # （用户最后随回复只拿到「仲裁」，看不到同一轮的「裂缝」）。
                        merged = list(prev.get("parts") or [])
                        have = {pt for pt, _ in merged}
                        if t not in have:
                            merged.append((t, x))
                        else:
                            merged = [(pt, x if pt == t else px)
                                      for pt, px in merged]
                        _pending[key] = {
                            "text": "\n\n".join(px for _, px in merged),
                            "at": now,
                            "fails": int(prev.get("fails") or 0) + 1,
                            "types": {pt for pt, _ in merged},
                            "parts": merged,
                        }
            if interval and ok:
                time.sleep(interval)        # 相邻两条之间稍歇，防官方频控
        if sent_any:
            sent += 1
    if sent:
        _save_last_sent()               # 本轮有改动 → 去重记录落盘，重启后不再重发
    return sent


def _note_probe(ok):
    """记录主动通道的探测结果：通了就每轮都发，不通则退避 5 分钟再试。

    官方机器人未开通主动消息配额时，每次都会返回 40034105；如果每 10 秒空打一次，
    长期跑等于持续骚扰接口。退避后权限一开通，最多 5 分钟内自动恢复。
    """
    global _next_probe
    _next_probe = 0.0 if ok else time.time() + PROBE_GAP
