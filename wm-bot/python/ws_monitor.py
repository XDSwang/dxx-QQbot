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
import json
import os
import re
import threading
import time
from pathlib import Path

import worldstate as ws


HERE = Path(__file__).resolve().parent
DEFAULT_SWITCH_FILE = HERE / "data" / "ws_switches.json"

_sender = None                       # 由 QQ 后端注入：sender(channel, target_id, text) -> bool
_monitors = {}                       # (channel, target_id) -> set(types)
_switches = {}                       # (channel, target_id) -> {type: bool}  聊天指令设的开关
_pending = {}                        # (channel, target_id) -> {"text":str,"at":float,"fails":int}
_attached_at = {}                    # (channel, target_id) -> 上次随查询附带的时刻
_last_sent = {}                      # (channel, target_id) -> (周期内容签名, 主动推送成功时刻)
_seen_priority = {}                  # (channel, target_id) -> {type: set(条目键)}  主推基线
_priority_sent_at = {}               # (channel, target_id) -> 上次主推成功时刻
_next_probe = 0.0                    # 下次允许尝试主动发送的时刻（失败后退避探测）
_lock = threading.RLock()
_push_thread = None
_stop = threading.Event()

PROBE_GAP = 300.0                    # 主动通道不通时，每 5 分钟才再试一次（别空打接口）
PRIORITY_DEFAULT = "alerts,events"   # 默认主推：警报 + 活动
# 倒计时标准化：3天4小时5分 / 2小时30分10秒 / 5分30秒 / 30秒 一律换成 <T>
_DURATION = re.compile(r"(?:\d+(?:天|小时|分|秒)){1,4}")


def _num(name, default):
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


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
        elif mon is not None:
            for t in todo:
                mon.discard(t)
        seen = _seen_priority.get(key)
        if seen:
            for t in todo:
                seen.pop(t, None)
        _save_switches()
    if on:
        ws.ensure_active()
        _ensure_push_thread()
    return todo


def reset_switches():
    """仅供离线测试：清空内存态开关，不动磁盘文件。"""
    with _lock:
        _switches.clear()


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
    with _lock:
        _last_sent.clear()
        _priority_sent_at.clear()


def start_monitor(channel, target_id, types):
    """开启某会话的监控；types 为 worldstate.TYPES 的键集合。"""
    key = (channel, str(target_id))
    with _lock:
        _monitors[key] = set(types)
    ws.ensure_active()               # 拉起世界状态后台刷新
    _ensure_push_thread()


def stop_monitor(channel, target_id):
    """停止某会话的监控；返回是否确实停掉了什么。

    「停止监控」是明确的全停意图，所以连本会话存盘的开关一起清掉，
    否则下次启动 load_switches 又会自动恢复，看起来像指令没生效。
    """
    key = (channel, str(target_id))
    with _lock:
        _pending.pop(key, None)
        _attached_at.pop(key, None)
        _last_sent.pop(key, None)
        _seen_priority.pop(key, None)
        _priority_sent_at.pop(key, None)
        had_switch = bool(_switches.pop(key, None))
        if had_switch:
            _save_switches()
        removed = _monitors.pop(key, None) is not None
        return removed or had_switch


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
            if cur is None:
                _monitors[key] = set(prio)
                changed += 1
            elif not set(prio) <= cur:
                cur.update(prio)
                changed += 1
    if changed:
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


def _parts_of(state, types):
    """按栏目拆分格式化结果，便于附带时精确剔除重复栏目（顺序按 TYPES 展示序）。"""
    out = []
    for t in ws.TYPES:
        if t not in types:
            continue
        try:
            txt = ws.TYPES[t][1](state)
        except Exception:
            txt = ""
        if txt:
            out.append((t, txt))
    return out


# ---------------- 事件主推：新警报 / 新活动立刻推 ----------------
def _event_keys(state, t):
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
        txt = ws.TYPES[t][1](state)
        return {stable_sig(txt)} if txt else set()
    except Exception:
        return None


def _priority_text(state, changed, added):
    """主推消息：只装发生变化的栏目，短小、能一眼看到新警报的奖励。"""
    blocks = ["🚨 世界状态主推 · 新增 %d 条" % added]
    for t in changed:
        try:
            txt = ws.TYPES[t][1](state)
        except Exception:
            txt = ""
        if txt:
            blocks.append(txt)
    blocks.append("（新内容即时推送，不等周期；发「停止监控」可关闭）")
    return "\n\n".join(blocks)


# ---------------- 监控推送的文字标签（图文混排时放在图片前面） ----------------
# 只做关键字匹配，不碰任何渲染逻辑，原图完全不变。
_MONITOR_LABEL_RULES = [
    (("赏金", "赏金轮换", "bounty"), "赏金"),
    (("虚空裂隙", "裂缝", "裂隙", "fissure", "Fissure"), "裂缝"),
    (("后纪中断", "中断", "强袭", "assault"), "中断"),
    (("希图斯", "奥布山谷", "魔胎之境", "双衍王境", "扎里曼", "夜灵平原",
      "金星平原", "火卫二", "平原",
      "cetus", "vallis", "cambion", "duviri", "zariman"), "平原"),
    (("地图", "星图"), "地图"),
    (("警报", "alert"), "警报"),
    (("活动", "event"), "活动"),
    (("入侵", "invasion"), "入侵"),
    (("虚空风暴", "虚空军团", "void"), "虚空风暴"),
    (("钢铁裂缝", "steel"), "钢铁裂缝"),
    (("午夜电波", "电波", "nightwave"), "电波"),
    (("虚空商人", "瓦奇娅", "baro"), "商人"),
    (("Darvo", "特惠", "deal"), "特惠"),
    (("信条", "终幕"), "武器"),
    (("全局加成", "加成"), "加成"),
]


def monitor_label(text):
    """监控推送图文混排时用的文字标签：从内容里匹配关键字，返回「【来源监控信息如下】」。

    规则按 _MONITOR_LABEL_RULES 顺序（更具体的来源排在前面），命中第一个即返回；
    一个关键字都不命中返回通用的「【监控信息如下】」。纯关键字匹配，不动任何卡片
    渲染，原图完全不变——和 WM 交易卡片的「图片 + content 文字」是同一套机制，
    只是这里把文字标签放在图片前面。
    """
    t = (text or "").lower()
    for keys, source in _MONITOR_LABEL_RULES:
        for k in keys:
            if k and k.lower() in t:
                return "【%s监控信息如下】" % source
    return "【监控信息如下】"


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
        keys = _event_keys(state, t)
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
    text = _priority_text(state, changed,
                          sum(len(v) for v in added_by_type.values()))
    ok = False
    if _sender is not None and now >= _next_probe:
        try:
            ok = bool(_sender(ch, tid, text))
        except Exception:
            ok = False              # 被频控 / 网络抖动都不致命，下个周期再试
        _note_probe(ok)
    with _lock:
        if ok:
            _seen_priority[key] = fresh
            _priority_sent_at[key] = now
            _pending.pop(key, None)
            return 1
        parts = []
        for t in changed:
            try:
                parts.append((t, ws.TYPES[t][1](state)))
            except Exception:
                pass
        prev = _pending.get(key) or {}
        _pending[key] = {
            "text": text,
            "at": now,
            "fails": int(prev.get("fails") or 0) + 1,
            "types": set(changed),
            "parts": parts,
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

        # ② 周期推送：主推栏目默认从这里剔除，避免同一条警报推两遍
        full = set(types)
        if prio and skip_full:
            full -= set(prio)
        parts = _parts_of(state, full)
        if not parts:
            continue
        text = "\n\n".join(x for _, x in parts)
        sig = stable_sig(text)
        with _lock:
            last = _last_sent.get(key)      # 只有「主动推送成功过」才有记录
        if last:
            if last[0] == sig:
                continue
            if now - last[1] < active_min_gap():
                continue
        ok = False
        if _sender is not None and now >= _next_probe:
            try:
                ok = bool(_sender(ch, tid, text))
            except Exception:
                ok = False          # 被频控 / 网络抖动都不致命，下个周期再试
            _note_probe(ok)         # 只有真去试了才更新探测时刻，否则退避会被无限推后
        with _lock:
            if ok:
                _last_sent[key] = (sig, now)
                if not (_pending.get(key) or {}).get("priority"):
                    _pending.pop(key, None)  # 主动通道通了，不用再捎带
                sent += 1
            else:
                prev = _pending.get(key) or {}
                if prev.get("priority"):
                    # 主推暂存优先：只刷新时间与失败计数，不覆盖内容
                    prev["at"] = now
                    prev["fails"] = int(prev.get("fails") or 0) + 1
                    _pending[key] = prev
                else:
                    _pending[key] = {
                        "text": text,
                        "at": now,
                        "fails": int(prev.get("fails") or 0) + 1,
                        "types": set(full),
                        "parts": parts,
                    }
    return sent


def _note_probe(ok):
    """记录主动通道的探测结果：通了就每轮都发，不通则退避 5 分钟再试。

    官方机器人未开通主动消息配额时，每次都会返回 40034105；如果每 10 秒空打一次，
    长期跑等于持续骚扰接口。退避后权限一开通，最多 5 分钟内自动恢复。
    """
    global _next_probe
    _next_probe = 0.0 if ok else time.time() + PROBE_GAP
