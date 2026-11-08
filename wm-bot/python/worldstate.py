#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
worldstate.py — 星际战甲（国际服）世界状态查询 / 监控（独立 Python 模块，零依赖）

数据源（默认）：wf.wiki 的实时世界状态页 worldstate.wf.wiki —— 无 Cloudflare、程序化可直连，
        世界状态以 JSON 内嵌在页面 RSC 流里（self.__next_f.push），抓一次解析成统一结构。
        **一次请求拿回整个世界状态**，请求次数最少、最安全。
        主页一屏能看到的栏目全部可取：平原五处（希图斯/奥布山谷/魔胎/双衍/扎里曼）、裂缝、
        钢铁裂缝、警报、活动、入侵、午夜电波、虚空商人、瓦奇娅、Darvo 特惠、开放世界赏金、
        赏金轮换、信条/终幕武器、全局加成、新闻。
        另外两个补源：
          - 突击：wf.wiki 没有，从 DE 官方 api.warframe.com/cdn/worldState.php 取 Sorties[0]；
          - 仲裁：wf.wiki 没有，来自 arbi.wf.wiki 的仲裁轮换表（两张静态 JSON，按
            startTs + stepSec×序号 本地推算，7 天同步一次，平时零请求）。
        可切换：WM_WS_SOURCE = wfwiki（默认）| warframe | mock。
        warframe 模式走 WFCD / warframestat.us 的 /pc/zh（中文，失败退回 /pc；常被 Cloudflare 403），
        支持 WM_WS_API_BASE 多源逗号分隔 + 故障自动回退。

为什么不再按端点一个个查（用户指出的问题）：
    裂缝 / 钢铁裂缝 / 警报返回的都是【列表】，每条各有自己的结束时刻；
    按「端点」粒度排队，等于把一整个列表当成一个到期点，既不准又会让
    16 个端点各自打一次请求——即使每次都守 3~5 秒，请求数多、风控风险高，
    同时到期还会全挤在节流里排队，全部变慢。
    所以改成：一次全量请求 + 一个主任务 + 一条按【每条数据结束时刻】排序的队列。

统一调度（一个查询 / 一个主任务 / 一条排序队列）：
    - 每次全量抓取记下 fetched_at，递归扫描整个 state，
      把【每一条】数据的 expiry / activation / timeLeft 换算成绝对结束时刻；
      只留未来的，去重后按结束时刻升序排成 queue（裂缝列表里每条都单独排）。
    - 主任务只有【一个】后台循环：睡到 queue 队首（最快结束的那条）；
      到点发【一次全量请求】，写缓存，重新扫描 + 排序，继续睡。
      没到点就【零请求】，绝不反复查询。
    - 任意两次请求之间不短于 2 -> 3 -> 5 -> 2 分钟节拍（WS_STEP_GAPS 可改）：
      接口给出极短到期点、或队列为空（拿不到任何结束时刻）时，按节拍兜底再查一次全量，
      直到扫到新的未来结束时刻，再重新睡到它。
    - **请求次数只跟世界状态本身有关，与群数 / 用户数无关**：全世界状态只有这一条队列
      + 一份缓存（data/worldstate.json），所有群共用；用户查询只读缓存，不触发请求，
      所以群再多也不会把对端打爆（多出来的只有「发消息」，受 QQ 频控与推送间隔约束）。
    - 商人 active=False 时结束点就是下次抵达（可能十几天），期间零请求。
    - 【单飞 + 缓存优先，防重复查询】一次全量请求会把【全部】数据写回缓存，
      所以「待查任务」不能各自留着再查一遍：
        * 主任务锁同一时刻只放【一个】任务进查询；抢不到锁的任务不排队死等、
          更不自己发请求，等开锁后先到缓存里找需要的数据。
        * 多个任务同时进来时按【最快到期】排序，本轮只执行【一个】；
          其余加入「下一个抢主任务队列」，开锁后先查缓存 —— 缓存版本号
          （fetched_at）已变，说明它要的数据已被刚才那次全量请求覆盖，直接移除。
        * 实在缓存里找不到的，才继续按最快到期排队等下一轮，仍守 10/20/50 节拍
          与同域名 3~5 秒底线，绝不变成反复查询。
    - 【特殊任务（通用队列）】有些任务的数据【不在】全量请求里 —— 属于另一个
      域名 / 另一个字段 / 另一个接口。主任务对这类任务一视同仁：
      只按【最快到期】排序、到点执行【一个】，不需要知道过来的任务是什么：
        * submit_task(due, label, fn=可调用对象)：fn 自己负责查什么、走哪个域名、
          怎么节流（同域名 3~5 秒用 wm.pace_gate_ex(自己的域名, 3, 5)）。
        * fn=None 表示「世界状态刷新请求」，走上面的缓存优先 / 单飞流程。
        * 特殊任务到自己的 due 就醒，【不受】世界状态 10/20/50 节拍限制
          （它的数据不在全量请求里，节拍管不到它；间隔由 fn 自己守）。
        * 失败按 10/30/60 秒退避重新排队，不挡别的任务。
        * 特殊任务只存内存，重启后由提交方自行重提。
    - 同域名 3~5 秒硬规则：每次 HTTP 前都过 pace_gate_ex，中文回退那次也过，
      mock 源也真等。节拍 >= 10 秒，节流永远零等待、请求即时发出。
    - 网络失败退避 10 -> 30 -> 60 秒，之后保持 60 秒继续重试；成功后归零。
    - WS_WAKE_MAX：单次最长睡眠（默认 1 小时），醒来重排队列防休眠漏点。

查询指令（用户发的）：
    裂缝 / 钢铁裂缝 / 商人 / 平原 / 突击 / 仲裁 / 电波 /
    入侵 / 特惠 / 信使 / 集团 / 建造 / 警报 / 活动 / 全部
    「平原」= 希图斯 + 奥布山谷 + 魔胎之境 三处一次全给。
    被动查询直接读缓存秒回（不额外打接口）；监控推送也只读缓存。

缓存：data/worldstate.json（全量 state + 抓取时刻 + 排序后的到期队列）。不上传。
"""

import os
import re
import time
import json
import gzip
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

import wm   # 复用 pace_gate_ex / load_json / save_json / DATA_DIR / LOG_HOOK
import config as bot_config   # 统一 HTTPS 出口：手机端缺 CA 根证书时自动兼容

# ----------------------------- 配置 -----------------------------
def _ws_bases():
    """WM_WS_API_BASE 支持【逗号分隔的多个数据源】——主用在前，某个源整体失败
    （/pc/zh 与 /pc 都拿不到）自动回退下一个；全部失败才报错。
    默认只含官方 api.warframestat.us（该域名常被 Cloudflare 拦，建议把自建/镜像源放最前面）。
    每个源按【自己的域名】独立守 3~5 秒同域名硬规则。"""
    raw = os.environ.get("WM_WS_API_BASE") or "https://api.warframestat.us"
    out = [b.strip().rstrip("/") for b in raw.split(",") if b.strip()]
    return out or ["https://api.warframestat.us"]


def _domain_of(base):
    try:
        return urllib.parse.urlparse(base).netloc or base
    except Exception:
        return base


WS_API_BASE = _ws_bases()[0]      # 主源（保留旧符号，向后兼容 / 单源语义）
WS_DOMAIN = _domain_of(WS_API_BASE)
WS_LANG = os.environ.get("WM_WS_LANG") or "zh"
# 数据源：wfwiki（wf.wiki 内嵌实时数据，默认）| warframe（api.warframestat.us 系，
# 常被 Cloudflare 403）| mock（离线测试）。用 WM_WS_SOURCE 覆盖。
WS_SOURCE = os.environ.get("WS_SOURCE") or os.environ.get("WM_WS_SOURCE") or "wfwiki"
# wf.wiki 世界状态页面（Next.js，服务端渲染，数据内嵌在 RSC 流里，无 Cloudflare）。
WS_WFWIKI_URL = (os.environ.get("WM_WS_WFWIKI_URL") or "https://worldstate.wf.wiki").rstrip("/")
# 抓 HTML 用浏览器 UA（与 JSON 接口分开，避免被简单拦截）
WS_HTML_HEADERS = {
    "accept": "text/html,application/xhtml+xml",
    "accept-language": "zh-CN,zh;q=0.9",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}

# arbi.wf.wiki：仲裁轮换表（两张静态 JSON，openresty 直出、无 Cloudflare）。
# 仲裁每小时整点换图，站点不是「实时抓」，而是用 startTs + stepSec×序号 去
# seq[序号] 取节点码、再查 nodes 表 —— 也就是说只要定期同步这两张表（默认 7 天），
# 当前仲裁就能在本地算出来，查询时零请求、还不会因为网络抖动查不到。
ARBI_BASE = (os.environ.get("WM_WS_ARBI_URL") or "https://arbi.wf.wiki").rstrip("/")
ARBI_TTL = float(os.environ.get("WM_WS_ARBI_TTL") or 7 * 86400)
ARBI_SCHEDULE_URL = ARBI_BASE + "/data/arbys.schedule.v2.json"
ARBI_NODES_URL = ARBI_BASE + "/data/arbys.nodes.zh.json"

# 同域名硬规则：每次 HTTP 前都过 3~5 秒节流（与 WM 一致，不允许调小）
WS_PACE_MIN, WS_PACE_MAX = 3.0, 5.0

def _step_gaps():
    """两次全量请求之间的最小间隔（秒），默认 2/3/5 分钟循环。

    群再多也只走这一条队列、共用一份缓存，请求次数不随人数增长；
    这个参数只决定「世界状态最多多久刷一次」，可用 WS_STEP_GAPS=秒,秒… 覆盖。
    """
    raw = os.environ.get("WS_STEP_GAPS") or "120,180,300"
    out = []
    for p in str(raw).split(","):
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out.append(v)
    return tuple(out) or (120.0, 180.0, 300.0)


WS_STEP_GAPS = _step_gaps()           # 主任务节拍：任意两次全量请求之间的最小间隔
WS_RETRY_GAPS = (10.0, 30.0, 60.0)    # 网络失败退避
WS_PUSH_SECONDS = float(os.environ.get("WS_PUSH_SECONDS") or 10)
WS_WAKE_MAX = float(os.environ.get("WS_WAKE_MAX") or 3600)
WS_EXPIRY_GRACE = float(os.environ.get("WS_EXPIRY_GRACE") or 2)
HTTP_TIMEOUT = int(os.environ.get("WS_HTTP_TIMEOUT") or 15)

WS_HEADERS = {
    "accept": "application/json",
    "User-Agent": "wm-bot-python/0.1 (+Warframe world-state bot)",
}


def _log(msg):
    if wm.LOG_HOOK:
        try:
            wm.LOG_HOOK("WS " + str(msg))
        except Exception:
            pass


# 每条数据的到期字段（绝对时刻）+ 相对时长字段
_ABS_KEYS = ("expiry", "activation", "end", "start", "endDate", "startDate")
_REL_KEYS = ("timeLeft", "eta")

# 顶层字段 -> 中文名（「计划」里显示用；裂缝列表里的每条都会单独排进队列）
FIELD_LABELS = {
    "alerts": "警报", "events": "活动",
    "fissures": "裂缝", "steelPath": "钢铁裂缝",
    "cetusCycle": "希图斯", "vallisCycle": "奥布山谷", "cambionCycle": "魔胎之境",
    "voidTrader": "虚空商人", "sortie": "突击", "arbitration": "仲裁",
    "invasions": "入侵", "nightwave": "电波", "dailyDeals": "Darvo特惠",
    "sentientOutpost": "信使前哨", "syndicateMissions": "集团任务",
    "constructionProgress": "建造进度",
    # 主页面其它栏目（wf.wiki 主页一屏能看到的，全部都要能单独查）
    "duviriCycle": "双衍王境", "zarimanCycle": "扎里曼",
    "circuit": "回廊",
    "globalUpgrades": "全局加成", "tenetCoda": "信条/终幕",
    "bountyBoard": "赏金", "bounties": "赏金轮换",
    "varzia": "瓦奇娅", "news": "新闻",
}
CYCLE_UNITS = ("cetus", "vallis", "cambion", "duviri", "zariman")   # 「平原」显示组合用


# ----------------------------- 时间工具 -----------------------------
def _to_epoch(v):
    """把 ISO 字符串 / 数字时间戳转成 epoch 秒。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if v > 1e11:           # 看起来像毫秒
            v = v / 1000.0
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            m = re.match(r"(\d+):(\d+):(\d+)", s)
            if m:
                h, m2, s3 = (int(x) for x in m.groups())
                return time.time() + h * 3600 + m2 * 60 + s3
            return None
    return None


def _human(sec):
    sec = int(sec)
    if sec <= 0:
        return "已结束"
    d = sec // 86400
    h = (sec % 86400) // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if d:
        return f"{d}天{h}小时{m}分"
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _fmt_left(v):
    e = _to_epoch(v)
    if e is None:
        return "未知"
    return _human(e - time.time())


def _fmt_when(epoch):
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知"


def _parse_timeleft(v):
    """相对时长转秒：'01:23:45' / '0h 52m 12s' / '3d 4h 5m 6s'。"""
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    if not s:
        return None
    if re.fullmatch(r"\d{1,3}:\d{1,2}(:\d{1,2})?", s):
        p = [int(x) for x in s.split(":")]
        if len(p) == 2:
            return p[0] * 60 + p[1]
        return p[0] * 3600 + p[1] * 60 + p[2]
    m = re.fullmatch(r"(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", s)
    if m and any(m.groups()):
        d, h, mi, se = (int(x or 0) for x in m.groups())
        return d * 86400 + h * 3600 + mi * 60 + se
    return None


def collect_dues(state, fetched_at):
    """递归扫描全量 state，把【每一条】数据的结束时刻都收集成 (due, 中文标签)。

    裂缝 / 钢铁裂缝 / 警报这类列表里的每条会各自产生一个到期点，
    不会把一整个列表压成一个时间。
    """
    items = []

    def walk(node, label):
        if isinstance(node, dict):
            # 同一节点常同时给 expiry（绝对）和 timeLeft（相对，如 "0h 52m 12s"）。
            # 两者是同一时刻的两种写法：有绝对字段就【优先只用绝对的】，
            # 否则一条数据会在队列里排两次、造成到点后多查一次。
            abs_dues, rel_dues = [], []
            for k in _ABS_KEYS:
                v = node.get(k)
                if v is not None:
                    e = _to_epoch(v)
                    if e is not None:
                        abs_dues.append(e)
            if not abs_dues:
                for k in _REL_KEYS:
                    v = node.get(k)
                    if v is not None:
                        rel = _parse_timeleft(v)
                        if rel is not None:
                            rel_dues.append(fetched_at + rel)
            for e in (abs_dues or rel_dues):
                items.append((e, label))
            for v in node.values():
                walk(v, label)
        elif isinstance(node, list):
            for v in node:
                walk(v, label)

    for field, node in (state or {}).items():
        walk(node, FIELD_LABELS.get(field, field))
    return items


def build_queue(state, fetched_at, now=None):
    """按【最快结束】升序排好队列，交给唯一的主任务。只保留未来的到期点。"""
    now = float(now if now is not None else time.time())
    seen = set()
    q = []
    for due, label in sorted(collect_dues(state, fetched_at), key=lambda x: x[0]):
        if due <= now + WS_EXPIRY_GRACE:
            continue
        key = (round(due, 0), label)      # 秒级去重，不对 due 本身取整（绝不提前请求）
        if key in seen:
            continue
        seen.add(key)
        q.append({"due": due, "label": label})
    return q


# ----------------------------- 缓存 -----------------------------
WS_CACHE_FILE = wm.DATA_DIR / "worldstate.json"
# 缓存结构版本：数据字段有增减就 +1，旧缓存直接作废重抓一次。
# v2 = 补上主页面的仲裁 / 双衍 / 扎里曼 / 加成 / 信条 / 终幕 / 赏金 / 瓦奇娅 / 新闻。
# 为什么宁可重抓也不沿用：旧缓存里没有这些字段，沿用会让查询显示「（当前无）」，
# 而实际上接口是有数据的 —— 那是最糟的一种「看起来能用」的错误。
WS_CACHE_SCHEMA = 2


def get_cache():
    c = wm.load_json(WS_CACHE_FILE, {})
    if c and c.get("schema") != WS_CACHE_SCHEMA:
        _log(f"世界状态缓存结构过旧（schema={c.get('schema')!r}），作废并重抓一次")
        return {}
    # 兼容旧版按端点 parts 存的缓存：不联网，直接用已有全量 data 就地重排队列
    if c and "queue" not in c and isinstance(c.get("data"), dict) and c["data"]:
        t0 = float(c.get("fetched_at") or (float(c.get("ts") or 0) / 1000.0)
                   or time.time())
        c["fetched_at"] = t0
        c["queue"] = build_queue(c["data"], t0, time.time())
        c["step"] = 0
        c.pop("parts", None)
        wm.save_json(WS_CACHE_FILE, c)
    return c


def get_state():
    """合并后的全量 state，供各格式化函数直接用。"""
    return get_cache().get("data")


def get_queue():
    return get_cache().get("queue") or []


def _save(state, fetched_at, queue, step):
    wm.save_json(WS_CACHE_FILE, {
        "schema": WS_CACHE_SCHEMA,
        "ts": int(fetched_at * 1000),
        "fetched_at": fetched_at,
        "data": state,
        "queue": queue,
        "step": step,
    })


# ----------------------------- 取数：一次全量请求 -----------------------------
def _http_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or WS_HEADERS)
    try:
        with bot_config.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}")
    except Exception as e:
        raise RuntimeError(f"网络错误 {e}")


def _http_text(url):
    """抓 HTML（wf.wiki 是网页内嵌数据，不是 JSON 接口）。"""
    req = urllib.request.Request(url, headers=WS_HTML_HEADERS)
    try:
        with bot_config.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}")
    except Exception as e:
        raise RuntimeError(f"网络错误 {e}")


# ----------------------------- wf.wiki 适配器 -----------------------------
# wf.wiki 是 Next.js 应用，世界状态数据以 JSON 形式内嵌在
#   self.__next_f.push([1,"<转义后的 RSC 文本>"])
# 的 RSC 流里（initialData）。抓 HTML -> 反转义拼回 -> 找含 fissures 的对象。
_RSC_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)


def _find_dict_with(node, key):
    """递归找到第一个含指定 key 的 dict。"""
    if isinstance(node, dict):
        if key in node:
            return node
        for v in node.values():
            r = _find_dict_with(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_dict_with(v, key)
            if r is not None:
                return r
    return None


def _extract_wfwiki_data(html):
    """从 wf.wiki 的 HTML 里抽出 world state 的 initialData 对象。"""
    chunks = _RSC_RE.findall(html or "")
    if not chunks:
        raise RuntimeError("wf.wiki 页面结构变化：找不到 RSC 数据块")
    texts = []
    for c in chunks:
        # RSC 里的字符串是 JS 字符串字面量，json.loads('"..."') 可正确反转义
        # （\\uXXXX、\\" 等）；个别无法解析的原样保留。
        try:
            texts.append(json.loads('"' + c + '"'))
        except Exception:
            texts.append(c)
    rsc = "".join(texts)
    for ln in rsc.split("\n"):
        m = re.match(r"^([0-9a-fA-F]+):(.*)$", ln, re.S)
        if not m:
            continue
        try:
            obj = json.loads(m.group(2))
        except Exception:
            continue
        found = _find_dict_with(obj, "fissures")
        if found is not None:
            return found
    raise RuntimeError("wf.wiki 数据解析失败：未找到世界状态对象")


def _ms(v):
    """wf.wiki 的时间是毫秒时间戳 -> epoch 秒。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f / 1000.0 if f > 1e11 else f


def _clean(v):
    """wf.wiki 用 "$undefined" 表示空值，统一清成 None。"""
    if v in (None, "", "$undefined"):
        return None
    return v


def _reward(r):
    """赏金奖励归一项：{name, count}。数据源偶尔直接给字符串，两种都兜底。"""
    if isinstance(r, dict):
        return {"name": _clean(r.get("name")) or "?", "count": r.get("count") or 1}
    return {"name": _clean(r) or "?", "count": 1}


def _wfwiki_to_state(raw):
    """把 wf.wiki 的 initialData 转成内部统一的 state 结构（与 warframestat 同键），
    这样调度 / 缓存 / 查询 / 监控全部零改动。"""
    def loc(system, node):
        system = _clean(system)
        node = _clean(node)
        if system and node:
            return f"{system}/{node}"
        return node or "?"

    def items(lst):
        return [str(x) for x in (lst or []) if x is not None]

    s = {}

    # 平原昼夜（cycles 里有 earth/cetus/vallis/cambion/duviri/zariman）
    cyc = {c.get("id"): c for c in (raw.get("cycles") or []) if c.get("id")}
    cet = cyc.get("cetus") or cyc.get("earth")
    if cet:
        s["cetusCycle"] = {"isDay": cet.get("stateKey") == "day",
                           "expiry": _ms(cet.get("expiry"))}
    if cyc.get("vallis"):
        v = cyc["vallis"]
        s["vallisCycle"] = {"isWarm": v.get("stateKey") == "warm", "expiry": _ms(v.get("expiry"))}
    if cyc.get("cambion"):
        c = cyc["cambion"]
        s["cambionCycle"] = {"isFass": c.get("stateKey") == "fass", "expiry": _ms(c.get("expiry"))}
    # 主页顶部那排还有双衍王境（喜悦/愤怒/悲伤/恐惧）和扎里曼（Corpus/Grineer）
    if cyc.get("duviri"):
        d = cyc["duviri"]
        s["duviriCycle"] = {"state": _clean(d.get("state")) or _clean(d.get("stateKey")),
                            "stateKey": d.get("stateKey"), "expiry": _ms(d.get("expiry"))}
    if cyc.get("zariman"):
        z = cyc["zariman"]
        s["zarimanCycle"] = {"state": _clean(z.get("state")) or _clean(z.get("stateKey")),
                             "stateKey": z.get("stateKey"), "expiry": _ms(z.get("expiry"))}

    # 裂缝 / 钢铁裂缝：数据源有两种形态——
    #   ① fissures 每条带 steelPath 标志（旧形态）；
    #   ② 单独的 steelPath.current 列表（现形态，2026-11 起）。
    # 两种都要吃，并按 node+expiry 去重，避免钢铁整栏漏成「当前无」或重复。
    def _fis_item(f):
        return {
            "node": loc(f.get("system"), f.get("node")),
            # 任务类型在 wf.wiki 里是 missionType 字段（mission 常为 null），优先取它
            "missionType": _clean(f.get("missionType") or f.get("mission")) or "?",
            # wf.wiki 给的字段是 enemy（旧代码读 faction，导致敌人一律显示 ?）
            "enemy": _clean(f.get("enemy") or f.get("faction")) or "?",
            "tier": _clean(f.get("tier")) or "",
            "expiry": _ms(f.get("expiry")),
        }

    fiss, steel, seen = [], [], set()
    for f in (raw.get("fissures") or []):
        item = _fis_item(f)
        key = (item["node"], item["expiry"], item["tier"])
        if key in seen:
            continue
        seen.add(key)
        (steel if f.get("steelPath") else fiss).append(item)
    sp_raw = raw.get("steelPath")
    if isinstance(sp_raw, dict):
        for f in (sp_raw.get("current") or []):
            item = _fis_item(f)
            key = (item["node"], item["expiry"], item["tier"])
            if key in seen:
                continue
            seen.add(key)
            steel.append(item)
    s["fissures"] = fiss
    if steel:
        s["steelPath"] = {"current": steel}

    # 警报
    alerts = []
    for a in (raw.get("alerts") or []):
        rw = a.get("reward") or {}
        cr = rw.get("credits")
        alerts.append({
            "mission": {"node": loc(a.get("system"), a.get("node")),
                        "type": a.get("mission") or "?",
                        "faction": a.get("faction") or "?"},
            "reward": {"items": items(rw.get("items")),
                       "credits": cr if isinstance(cr, (int, float)) else None},
            "expiry": _ms(a.get("expiry")),
        })
    s["alerts"] = alerts

    # 入侵
    invs = []
    for inv in (raw.get("invasions") or []):
        ar = inv.get("attackerReward") or {}
        dr = inv.get("defenderReward") or {}
        comp = inv.get("completion")
        invs.append({
            "node": loc(inv.get("system"), inv.get("node")),
            "progress": f"{comp}%" if comp is not None else "?",
            "attackerReward": {"items": items(ar.get("items"))},
            "defenderReward": {"items": items(dr.get("items"))},
        })
    s["invasions"] = invs

    # 活动（wf.wiki 用 name/node，内部用 description）
    events = []
    for ev in (raw.get("events") or []):
        desc = _clean(ev.get("name")) or _clean(ev.get("description")) or "?"
        node = _clean(ev.get("node"))
        if node:
            desc = f"{desc}（{node}）"
        events.append({"description": desc, "expiry": _ms(ev.get("expiry"))})
    s["events"] = events

    # 午夜电波
    nw = raw.get("nightwave")
    if nw:
        chals = [{"title": ch.get("name") or ch.get("desc") or "?",
                  "reputation": ch.get("reputation")} for ch in (nw.get("challenges") or [])]
        s["nightwave"] = {"season": nw.get("season"), "expiry": _ms(nw.get("expiry")),
                          "challenges": chals}

    # 虚空商人
    vt = raw.get("voidTrader")
    if vt:
        s["voidTrader"] = {
            "active": bool(vt.get("active")),
            "location": vt.get("node") or "?",
            "activation": _ms(vt.get("activation")),
            "expiry": _ms(vt.get("expiry")),
            "inventory": [{"item": it.get("name") or "?", "ducats": it.get("ducats"),
                           "credits": it.get("credits")} for it in (vt.get("inventory") or [])],
        }

    # Darvo 每日特惠
    deals = [{"item": dd.get("name") or "?",
              "originalPrice": dd.get("original"), "salePrice": dd.get("sale"),
              "discount": dd.get("discount"), "expiry": _ms(dd.get("expiry"))}
             for dd in (raw.get("dailyDeals") or [])]
    s["dailyDeals"] = deals

    # 瓦奇娅（原初重生商店，附加项）
    vz = raw.get("varzia")
    if vz:
        s["varzia"] = {
            "active": bool(vz.get("active")), "node": vz.get("node"),
            "activation": _ms(vz.get("activation")), "expiry": _ms(vz.get("expiry")),
            "inventory": [{"item": it.get("name") or "?", "regalAya": _clean(it.get("regalAya")),
                           "aya": _clean(it.get("aya"))} for it in (vz.get("inventory") or [])],
        }

    # 全局加成（「经验 2 倍」这类，主页顶部单独一条）
    s["globalUpgrades"] = [
        {"desc": _clean(u.get("desc")) or "?", "expiry": _ms(u.get("expiry"))}
        for u in (raw.get("globalUpgrades") or [])
    ]

    # 信条武器 / 终幕武器（两套独立轮换，各自有时间）
    tc = raw.get("tenetCoda")
    if tc:
        def _weapons(lst):
            return [{"name": _clean(w.get("name")) or "?",
                     "element": _clean(w.get("element")) or "",
                     "bonus": w.get("bonus")} for w in (lst or [])]
        s["tenetCoda"] = {
            "expiry": _ms(tc.get("expiry")),
            "tenet": _weapons(tc.get("tenet")),
            "codaBatch": tc.get("codaBatch"),
            "codaExpiry": _ms(tc.get("codaExpiry")),
            "coda": _weapons(tc.get("coda")),
        }

    # 开放世界赏金任务板（Ostron / 索拉里斯 / 英择谛 …）
    # 每档带 rewards（wf.wiki 页面上那一堆「蓄能重划 / 奥席金属 ×100 …」），
    # 之前只取目标/等级/阶段把它丢了，用户对着网页看会对不上。
    board = []
    for b in (raw.get("bountyBoard") or []):
        jobs = [{"objective": _clean(j.get("objective")) or "?",
                 "minLevel": j.get("minLevel"), "maxLevel": j.get("maxLevel"),
                 "stages": j.get("stages"),
                 "isVault": bool(_clean(j.get("isVault")) or False),
                 "rewards": [_reward(r) for r in (j.get("rewards") or [])]}
                for j in (b.get("jobs") or [])]
        board.append({"name": _clean(b.get("name")) or "?", "tag": b.get("tag"),
                      "expiry": _ms(b.get("expiry")), "jobs": jobs})
    s["bountyBoard"] = board

    # 赏金轮换（本轮奖励 + 宝库轮换 + 各集团赏金）
    bt = raw.get("bounties")
    if bt:
        def _rw(x):
            # 数据源偶尔把奖励名直接塞成字符串（而非 {name:...}），两种都兜底，避免整段崩溃
            return _clean(x.get("name")) or "?" if isinstance(x, dict) else (_clean(x) or "?")
        s["bounties"] = {
            "expiry": _ms(bt.get("expiry")),
            "rot": bt.get("rot"),
            "rotRewards": [_rw(r) for r in (bt.get("rotRewards") or [])],
            "vaultRot": bt.get("vaultRot"),
            "vaultRewards": [_rw(r) for r in (bt.get("vaultRewards") or [])],
            "groups": [{"name": _clean(g.get("name")) or "?", "tag": g.get("tag"),
                        "jobs": [{"node": _clean(j.get("node")) or "?",
                                  "mission": _clean(j.get("mission")) or "?",
                                  "challenge": _clean(j.get("challenge")) or "",
                                  # 六人组（1999）每条带同伴名：阿米尔 / 昆西 / 亚瑟 / 碧 / 莱蒂
                                  "ally": _clean(j.get("ally")) or ""}
                                 for j in (g.get("jobs") or [])]}
                       for g in (bt.get("groups") or [])],
        }

    # 新闻（主页最后一栏；官方只给部分语言的标题，取到的第一个非空标题）
    news = []
    for n in (raw.get("news") or []):
        title = _clean(n.get("zh")) or _clean(n.get("en")) or _clean(n.get("other"))
        if not title:
            continue
        news.append({"title": str(title), "link": n.get("link") or ""})
    s["news"] = news

    return s


# --------------------- 突击补源（wf.wiki 不提供，从 DE 官方端点取）---------------------
# DE 官方 worldState.php 的 Sorties[0] 用内部码（MT_*/SORTIE_MODIFIER_*/SORTIE_BOSS_*），
# 这里做保守映射：认识的翻中文，不认识的原样保留（去掉前缀、下划线转空格），绝不瞎编。
OFFICIAL_WS_URL = os.environ.get("WM_WS_OFFICIAL_URL") or "https://api.warframe.com/cdn/worldState.php"

_MISSION_CN = {
    "MT_SABOTAGE": "破坏", "MT_MOBILE_DEFENSE": "移动防御", "MT_DEFENSE": "防御",
    "MT_SURVIVAL": "生存", "MT_EXTERMINATION": "歼灭", "MT_INTEL": "间谍",
    "MT_ASSASSINATION": "刺杀", "MT_RESCUE": "救援", "MT_CAPTURE": "捕获",
    "MT_EXCAVATE": "挖掘", "MT_TERRITORY": "拦截", "MT_HIVE": "清巢",
    "MT_ASSAULT": "强袭", "MT_ARTIFACT": "中断", "MT_VOID_CASCADE": "虚空覆涌",
    "MT_ALCHEMY": "转化", "MT_LANDSCAPE": "平原", "MT_CORRUPTION": "腐化",
    "MT_ENDLESS_CAPTURE": "无尽捕获",
}
_MODIFIER_CN = {
    "SORTIE_MODIFIER_EXIMUS": "精英驻地",
    "SORTIE_MODIFIER_LOW_ENERGY": "能量缩减",
    "SORTIE_MODIFIER_HAZARD_RADIATION": "辐射灾害",
    "SORTIE_MODIFIER_HAZARD_FIRE": "火灾",
    "SORTIE_MODIFIER_HAZARD_ICE": "冰冻",
    "SORTIE_MODIFIER_HAZARD_MAGNETIC": "磁力灾害",
    "SORTIE_MODIFIER_HAZARD_COLD": "寒冷",
    "SORTIE_MODIFIER_ARMOR": "强化护甲",
    "SORTIE_MODIFIER_SHIELDS": "强化护盾",
    "SORTIE_MODIFIER_ENEMY_PHYSICAL": "敌人物理强化",
    "SORTIE_MODIFIER_ENEMY_ELEMENTAL": "敌人元素强化",
    # 以下按 WFCD/warframe-worldstate-data 的 data/zh/sortieData.json（modifierTypes）
    # 补齐：官方端点只给 SORTIE_MODIFIER_* 编码，缺表就会把 Electricity/Puncture
    # 这类键直接 title 成英文塞给用户。已有译名保持不动，只补本表没有的键。
    "SORTIE_MODIFIER_IMPACT": "敌人物理强化(冲击)",
    "SORTIE_MODIFIER_PUNCTURE": "敌人物理强化(穿刺)",
    "SORTIE_MODIFIER_SLASH": "敌人物理强化(切割)",
    "SORTIE_MODIFIER_MAGNETIC": "敌人元素强化(磁力)",
    "SORTIE_MODIFIER_CORROSIVE": "敌人元素强化(腐蚀)",
    "SORTIE_MODIFIER_VIRAL": "敌人元素强化(病毒)",
    "SORTIE_MODIFIER_ELECTRICITY": "敌人元素强化(电击)",
    "SORTIE_MODIFIER_RADIATION": "敌人元素强化(辐射)",
    "SORTIE_MODIFIER_GAS": "敌人元素强化(毒气)",
    "SORTIE_MODIFIER_FIRE": "敌人元素强化(火焰)",
    "SORTIE_MODIFIER_EXPLOSION": "敌人元素强化(爆炸)",
    "SORTIE_MODIFIER_FREEZE": "敌人元素强化(冰冻)",
    "SORTIE_MODIFIER_TOXIN": "敌人元素强化(毒素)",
    "SORTIE_MODIFIER_POISON": "敌人元素强化(毒素)",
    "SORTIE_MODIFIER_HAZARD_FOG": "浓雾",
    "SORTIE_MODIFIER_SECONDARY_ONLY": "副武器限定",
    "SORTIE_MODIFIER_SHOTGUN_ONLY": "霰弹枪限定",
    "SORTIE_MODIFIER_SNIPER_ONLY": "狙击枪限定",
    "SORTIE_MODIFIER_RIFLE_ONLY": "步枪限定",
    "SORTIE_MODIFIER_MELEE_ONLY": "近战限定",
    "SORTIE_MODIFIER_BOW_ONLY": "弓限定",
}
_BOSS_CN = {
    "SORTIE_BOSS_HEK": "Vay Hek", "SORTIE_BOSS_SARGAS_RUK": "Sargas Ruk",
    "SORTIE_BOSS_KRILA": "Kela De Thaym", "SORTIE_BOSS_TYL_REGOR": "Tyl Regor",
    "SORTIE_BOSS_LECH_KRIL": "Lech Kril", "SORTIE_BOSS_AMBULAS": "Ambulas",
    "SORTIE_BOSS_JACKAL": "豺狼", "SORTIE_BOSS_PHORID": "Phorid",
    "SORTIE_BOSS_BOREAL": "诡文枭主", "SORTIE_BOSS_NEF_ANYO": "Nef Anyo",
    "SORTIE_BOSS_ALAD_V": "Alad V", "SORTIE_BOSS_HYENA": "鬣狗群",
    # 官方不同版本用过两套 Boss 编码（旧 KRILA/TYL_REGOR/NEF_ANYO，
    # 新 KELA/TYL/NEF），两套都留着，遇到哪套都能翻。
    "SORTIE_BOSS_KELA": "Kela De Thaym", "SORTIE_BOSS_TYL": "Tyl Regor",
    "SORTIE_BOSS_KRIL": "Lech Kril", "SORTIE_BOSS_RUK": "Sargas Ruk",
    "SORTIE_BOSS_VOR": "Vor 上尉", "SORTIE_BOSS_NEF": "Nef Anyo",
    "SORTIE_BOSS_ALAD": "Alad V", "SORTIE_BOSS_RAPTOR": "猛禽",
    "SORTIE_BOSS_LEPHANTIS": "Lephantis", "SORTIE_BOSS_INFALAD": "异融者Alad V",
    "SORTIE_BOSS_CORRUPTED_VOR": "堕落 Vor", "SORTIE_BOSS_AMAR": "欺谋狼主",
    "SORTIE_BOSS_NIRA": "混沌蛇主", "SORTIE_BOSS_PAAZUL": "帕祖",
}


def _code_cn(table, code, prefix):
    if not code:
        return "?"
    if code in table:
        return table[code]
    return code.replace(prefix, "").replace("_", " ").strip().title()


def _fetch_sortie():
    """从 DE 官方端点抓当日突击（Sorties[0]），转成内部 sortie 结构。"""
    dom = _domain_of(OFFICIAL_WS_URL)
    wm.pace_gate_ex(dom, WS_PACE_MIN, WS_PACE_MAX)
    d = _http_json(OFFICIAL_WS_URL, headers=WS_HTML_HEADERS)
    arr = (d or {}).get("Sorties") or []
    if not arr:
        return None
    so = arr[0]
    exp = _ms(((so.get("Expiry") or {}).get("$date") or {}).get("$numberLong"))
    variants = []
    for v in (so.get("Variants") or []):
        variants.append({
            "node": _code_cn(_MISSION_CN, v.get("missionType"), "MT_"),
            "modifier": _code_cn(_MODIFIER_CN, v.get("modifierType"), "SORTIE_MODIFIER_"),
        })
    return {"boss": _code_cn(_BOSS_CN, so.get("Boss"), "SORTIE_BOSS_"),
            "expiry": exp, "variants": variants}


# 无尽回廊也只在 DE 官方端点里：EndlessXpSchedule[0]。
# EXC_NORMAL = 普通回廊本周三选一战甲；EXC_HARD = 钢铁回廊本周五把灵化武器。
# 轮换是每周一 00:00 UTC 由服务端写的，能直接读到，不用本地静态表推算。
_CIRCUIT_CAT = {"EXC_NORMAL": "normal", "EXC_HARD": "hard"}


def _fetch_circuit():
    """从 DE 官方端点抓本周回廊轮换（EndlessXpSchedule[0]）→ 内部 circuit 结构。

    normal = 普通回廊 3 套战甲，hard = 钢铁回廊 5 把灵化武器；
    名称保持官方英文原名（中文译名各服不一致，硬映射容易出错）。
    """
    dom = _domain_of(OFFICIAL_WS_URL)
    wm.pace_gate_ex(dom, WS_PACE_MIN, WS_PACE_MAX)
    d = _http_json(OFFICIAL_WS_URL, headers=WS_HTML_HEADERS)
    arr = (d or {}).get("EndlessXpSchedule") or []
    if not arr:
        return None
    it = arr[0] or {}
    got = {}
    for c in (it.get("CategoryChoices") or []):
        key = _CIRCUIT_CAT.get(c.get("Category"))
        if not key:
            continue
        got[key] = [x for x in (_clean(n) for n in (c.get("Choices") or [])) if x]
    if not got:
        return None
    return {"activation": _ms(((it.get("Activation") or {}).get("$date")
                               or {}).get("$numberLong")),
            "expiry": _ms(((it.get("Expiry") or {}).get("$date")
                           or {}).get("$numberLong")),
            "normal": got.get("normal") or [],
            "hard": got.get("hard") or []}


# --------------------- 仲裁补源（arbi.wf.wiki 轮换表，本地推算）---------------------
# 仲裁每小时整点换一张图。arbi.wf.wiki 并不「实时抓接口」，它把轮换表固化成了
# 两张静态 JSON：schedule = {startTs, stepSec, nodes[节点码], seq[序号->nodes 下标]}，
# nodes = {节点码: 中文名/任务/阵营/等级}。当前这张图 = nodes[seq[(now-startTs)//stepSec]]。
# 所以只要每周同步一次这两张表，查询时纯本地计算，零请求、也不会因网络抖动查空。
def _arbi_paths():
    """轮换表缓存与 worldstate.json 同目录（测试里跟着一起隔离）。"""
    base = Path(str(WS_CACHE_FILE)).parent
    return base / "arbys_schedule.json", base / "arbys_nodes.json"


def _arbi_read():
    """读本地轮换表 -> (schedule, nodes)；缺失/损坏返回 None。"""
    p1, p2 = _arbi_paths()
    try:
        sched = json.loads(p1.read_text(encoding="utf-8"))
        nodes = json.loads(p2.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(sched, dict) or not sched.get("seq") or not sched.get("nodes"):
        return None
    table = (nodes or {}).get("nodes") if isinstance(nodes, dict) else None
    if not table:
        return None
    return sched, table


def _arbi_fresh():
    p1, p2 = _arbi_paths()
    try:
        return (time.time() - min(p1.stat().st_mtime, p2.stat().st_mtime)) <= ARBI_TTL
    except OSError:
        return False


def _arbi_fetch():
    """抓两张轮换表并落盘。同一个域名，照样每次请求前过 3~5 秒节流。"""
    dom = _domain_of(ARBI_BASE)
    hdr = dict(WS_HTML_HEADERS)
    hdr["accept"] = "application/json"
    wm.pace_gate_ex(dom, WS_PACE_MIN, WS_PACE_MAX)
    sched = _http_json(ARBI_SCHEDULE_URL, headers=hdr)
    wm.pace_gate_ex(dom, WS_PACE_MIN, WS_PACE_MAX)
    nodes = _http_json(ARBI_NODES_URL, headers=hdr)
    if not isinstance(sched, dict) or not sched.get("seq") or not sched.get("nodes"):
        raise RuntimeError("仲裁轮换表主表结构不符（缺少 seq/nodes）")
    if not isinstance(nodes, dict) or not nodes.get("nodes"):
        raise RuntimeError("仲裁节点表结构不符（缺少 nodes）")
    p1, p2 = _arbi_paths()
    try:
        wm.save_json(p1, sched)
        wm.save_json(p2, nodes)
    except Exception as e:            # 落盘失败不影响本次计算，只是下次还得再抓
        _log(f"仲裁轮换表落盘失败（不影响本次使用）：{e}")
    return sched, nodes["nodes"]


def _arbi_calc(tables, now):
    """用轮换表算出 now 时刻的那一张仲裁；表没覆盖到 now 时返回 None。"""
    sched, nodes = tables
    seq = sched.get("seq") or []
    pool = sched.get("nodes") or []
    try:
        step = int(sched.get("stepSec") or 3600)
        start = float(sched.get("startTs") or 0)
    except (TypeError, ValueError):
        return None
    if step <= 0 or not seq or not pool or start <= 0:
        return None
    idx = int((now - start) // step)
    if not (0 <= idx < len(seq)):
        return None
    sel = seq[idx]
    if not isinstance(sel, int) or not (0 <= sel < len(pool)):
        return None
    key = pool[sel]
    info = nodes.get(key) or {}
    system = _clean(info.get("systemNameZh"))
    name = _clean(info.get("nameZh"))
    node = f"{system}/{name}" if system and name else (name or system or str(key))
    lo, hi = info.get("minEnemyLevel"), info.get("maxEnemyLevel")
    level = f"{lo}-{hi}" if lo is not None and hi is not None else ""
    return {
        "node": node,
        "type": _clean(info.get("missionNameZh")) or "?",
        "enemy": _clean(info.get("factionNameZh")) or "?",
        "level": level,
        "key": key,
        "start": start + idx * step,
        "expiry": start + (idx + 1) * step,     # 下一次整点换图
    }


def _arbi_state(now=None):
    """当前仲裁（本地推算）。表缺失/过期才联网；联网失败也优先用旧表。"""
    now = float(now if now is not None else time.time())
    fetched = False
    tables = _arbi_read() if _arbi_fresh() else None
    if tables is None:
        try:
            tables = _arbi_fetch()
            fetched = True
        except Exception as e:
            _log(f"仲裁轮换表更新失败，先用本地旧表：{e}")
            tables = _arbi_read()
    if not tables:
        return None
    res = _arbi_calc(tables, now)
    if res is not None:
        return res
    if not fetched:                     # 旧表已经跑出覆盖区间：再抓一次新的
        try:
            res = _arbi_calc(_arbi_fetch(), now)
        except Exception as e:
            _log(f"仲裁轮换表重抓失败：{e}")
    return res


def _fetch_wfwiki():
    """从 wf.wiki 抓一次全量世界状态（HTML 内嵌数据）。
    wf.wiki 不提供突击，这里再从 DE 官方端点补抓（旧缓存未过期则复用，
    避免每次都多打一个 ~160KB 的大请求；突击每天才变一次）。"""
    dom = _domain_of(WS_WFWIKI_URL)
    wm.pace_gate_ex(dom, WS_PACE_MIN, WS_PACE_MAX)
    html = _http_text(WS_WFWIKI_URL)
    raw = _extract_wfwiki_data(html)
    state = _wfwiki_to_state(raw)
    if not state:
        raise RuntimeError("wf.wiki 返回的世界状态为空")
    # 突击补源：缓存里已有且未过期就直接复用，否则现抓一次
    old = (get_state() or {}).get("sortie")
    if old and old.get("expiry") and old["expiry"] > time.time() + 60:
        state["sortie"] = old
    else:
        try:
            so = _fetch_sortie()
            if so:
                state["sortie"] = so
            elif old:
                state["sortie"] = old
        except Exception as e:
            _log(f"突击补抓失败（不影响其它数据）：{e}")
            if old:
                state["sortie"] = old
    # 回廊补源：同上，本周轮换结束前直接复用缓存（一周才变一次），否则现抓一次
    old_c = (get_state() or {}).get("circuit")
    if old_c and old_c.get("expiry") and old_c["expiry"] > time.time() + 60:
        state["circuit"] = old_c
    else:
        try:
            cc = _fetch_circuit()
            if cc:
                state["circuit"] = cc
            elif old_c:
                state["circuit"] = old_c
        except Exception as e:
            _log(f"回廊补抓失败（不影响其它数据）：{e}")
            if old_c:
                state["circuit"] = old_c
    # 仲裁补源：arbi.wf.wiki 轮换表本地推算（表 7 天一同步，平时零请求）
    try:
        arb = _arbi_state()
        if arb:
            state["arbitration"] = arb
        else:
            old_a = (get_state() or {}).get("arbitration") or {}
            if old_a.get("expiry") and old_a["expiry"] > time.time():
                state["arbitration"] = old_a
    except Exception as e:
        _log(f"仲裁推算失败（不影响其它数据）：{e}")
    return state


def fetch_worldstate():
    """【一次】全量请求：依次尝试 WM_WS_API_BASE 列出的多个源（逗号分隔），
    主用在前，某个源整体失败（/pc/zh 与 /pc 都失败）自动回退下一个；
    全部失败才抛错。每个源按【自己的域名】独立守 3~5 秒同域名硬规则；
    中文端点失败退回英文端点时也要再过一次节流，绝不连打两个请求。
    mock 源同样先过一次同域名节流（硬规则不分真实/mock）再返回内置样例。
    """
    if WS_SOURCE == "mock":
        wm.pace_gate_ex(_domain_of(WS_API_BASE), WS_PACE_MIN, WS_PACE_MAX)
        return dict(_MOCK_WS)
    if WS_SOURCE == "wfwiki":
        return _fetch_wfwiki()
    last_err = ""
    for base in _ws_bases():
        domain = _domain_of(base)
        try:
            wm.pace_gate_ex(domain, WS_PACE_MIN, WS_PACE_MAX)
            try:
                return _http_json(f"{base}/pc/{WS_LANG}")
            except Exception:
                wm.pace_gate_ex(domain, WS_PACE_MIN, WS_PACE_MAX)
                return _http_json(f"{base}/pc")
        except Exception as e:
            last_err = str(e)
            _log(f"世界状态数据源 {domain} 不可用：{e}；尝试下一个")
            continue
    raise RuntimeError(last_err or "所有世界状态数据源均不可用")


# ----------------------------- 唯一主任务 -----------------------------
_refresh_thread = None
_refresh_running = False
_last_activity = 0.0
_rlock = threading.Lock()
# 主任务锁：同一时刻只放【一个】任务进查询。抢不到锁的任务不自己发请求，
# 等开锁后先到缓存里找需要的数据（一次全量请求会覆盖全部数据）。
_task_lock = threading.Lock()
LOCK_WAIT = float(os.environ.get("WS_LOCK_WAIT") or 25)  # 等锁上限，防死锁
_pending = []              # 「下一个抢主任务队列」：{"due","label","gen","fn","fails"}
                          # fn=None 是世界状态刷新请求；fn 非 None 是特殊任务
                          # （数据不在全量请求里，主任务只排序执行、不看内容）
_pending_lock = threading.Lock()
_stop_ev = threading.Event()
_retry_due = 0.0
_failures = 0
_last_fetch_error = ""     # 最近一次全量抓取失败的原因（用于提示文案区分 403/超时）
_next_allowed = 0.0        # 节拍：任意两次全量请求之间的最早允许时刻


def activity_touch():
    global _last_activity
    _last_activity = time.time()


def ensure_active():
    """启动【唯一】的后台主任务线程（若未运行）。"""
    global _refresh_thread, _refresh_running
    activity_touch()
    with _rlock:
        if _refresh_thread and _refresh_thread.is_alive():
            return
        _stop_ev.clear()
        _refresh_running = True
        t = threading.Thread(target=_refresh_loop, daemon=True, name="ws-refresh")
        _refresh_thread = t
        t.start()


def refresh_once():
    """主任务唯一执行体：一次全量请求 -> 写缓存 -> 按每条结束时刻重排队列。

    fetched_at 必须取【同域名 3~5 秒节流 + HTTP 完成之后】的真实时刻：
    节流可能真的 sleep 3~5 秒，若在请求前取时间，timeLeft 这类相对时长
    换算出来的绝对到期点会整体提前 3~5 秒，主任务就会早醒、早发请求。
    """
    global _failures, _retry_due, _next_allowed
    state = fetch_worldstate()                    # 只发这一个请求（内部必过 3~5 秒节流）
    t0 = time.time()                             # 数据真正到手的时刻
    if not isinstance(state, dict):
        raise RuntimeError("世界状态返回异常（非 JSON 对象）")
    queue = build_queue(state, t0, t0)
    step = int(get_cache().get("step") or 0)
    gap = WS_STEP_GAPS[step % len(WS_STEP_GAPS)]   # 第一次 10，之后 20、50、10…
    _next_allowed = t0 + gap                    # 下一拍最早允许时刻：10/20/50 循环
    _save(state, t0, queue, (step + 1) % len(WS_STEP_GAPS))
    _failures = 0
    _retry_due = 0.0
    head = queue[0]["due"] if queue else None
    _log(f"全量刷新一次，扫到 {len(queue)} 个到期点；"
         f"队首 {_fmt_when(head) if head else '无（按节拍兜底）'}；下次不早于 {_fmt_when(_next_allowed)}")
    return queue


# ----------------------------- 任务合并 / 单飞队列 -----------------------------
def _cache_gen():
    """缓存版本号 = 抓取时刻。一次全量请求写回缓存后版本号就会变。"""
    return float(get_cache().get("fetched_at") or 0)


def submit_task(due, label="刷新", fn=None):
    """提交一个任务给唯一主任务。

    - fn=None：世界状态刷新请求（数据在全量请求里，可被缓存满足）。
    - fn=callable：特殊任务 —— 数据【不在】全量请求里（另一个域名 / 字段 / 接口）。
      主任务不关心它是什么，只按【最快到期】排序、到点执行这一个；
      查什么、哪个域名、怎么节流全由 fn 自己负责（同域名 3~5 秒走
      wm.pace_gate_ex(自己的域名, 3, 5)）。

    同一 (到期时刻, 数据, fn) 合并，不重复加；队列始终按【最快到期】升序。
    返回 "cached"（缓存已有，仅世界状态任务）/ "merged"（已合并）/ "queued"（进队等锁）。
    """
    due = float(due)
    if fn is None:
        # 特殊任务的数据不在全量请求里，世界状态缓存再新也满足不了它，必须执行
        c = get_cache()
        if c.get("data") and time.time() < next_due():
            return "cached"
    gen = _cache_gen()
    with _pending_lock:
        for t in _pending:
            if (t["label"] == label and abs(t["due"] - due) < 1
                    and t.get("fn") is fn):
                return "merged"
        _pending.append({"due": due, "label": label, "gen": gen,
                         "fn": fn, "fails": 0})
        _pending.sort(key=lambda x: x["due"])
        return "queued"


def pending_count():
    with _pending_lock:
        return len(_pending)


def _take_fastest():
    """按最快到期取出【一个】任务的快照（不管到没到期；测试 / 查看用）。"""
    with _pending_lock:
        if not _pending:
            return None
        _pending.sort(key=lambda x: x["due"])
        return dict(_pending[0])


def _take_due_task(now=None):
    """取出【已到期】且最快到期的任务；没有到期的返回 None。

    主任务只执行到期的任务：特殊任务没到自己的 due 绝不提前执行。
    """
    now = float(now if now is not None else time.time())
    with _pending_lock:
        due = [t for t in _pending if float(t["due"]) <= now + WS_EXPIRY_GRACE]
        if not due:
            return None
        due.sort(key=lambda x: x["due"])
        return dict(due[0])


def _drop_task(task):
    with _pending_lock:
        for i, t in enumerate(_pending):
            if (t["due"] == task["due"] and t["label"] == task["label"]
                    and t.get("fn") is task.get("fn")):
                _pending.pop(i)
                return True
    return False


def _requeue_task(task, gap, fails):
    """特殊任务失败后按退避间隔重新排队（不挡别的任务）。"""
    with _pending_lock:
        _pending.append({"due": time.time() + float(gap), "label": task["label"],
                         "gen": _cache_gen(), "fn": task["fn"], "fails": int(fails)})
        _pending.sort(key=lambda x: x["due"])


def _reap_pending():
    """开锁后：其它任务先到缓存里找需要的数据。

    一次全量请求把【全部】数据写回缓存，所以【世界状态类】任务版本号变新
    即已被满足，直接移出；还没到期的也不该现在查。特殊任务（带 fn）的数据
    不在全量请求里，全量刷新【满足不了】它，必须保留等自己的执行轮。
    返回被满足（移出）的任务数。
    """
    c = get_cache()
    gen = _cache_gen()
    now = time.time()
    with _pending_lock:
        before = len(_pending)
        if c.get("data"):
            _pending[:] = [t for t in _pending
                           if t.get("fn") is not None
                           or (gen <= t["gen"] and float(t["due"]) <= now + WS_EXPIRY_GRACE)]
        _pending.sort(key=lambda x: x["due"])
        return before - len(_pending)


def _submit_due_entries():
    """把队列里【已到期】的条目按结束时刻提交成任务（每个都先查缓存）。"""
    now = time.time()
    n = 0
    for item in get_queue():
        due = float(item["due"])
        if due <= now + WS_EXPIRY_GRACE:
            if submit_task(due, item.get("label") or "刷新") == "queued":
                n += 1
    return n


def _run_once():
    """主任务单次执行：抢锁 -> 只执行【最快到期的一个】任务 -> 开锁。

    任务分两种，主任务【不需要知道过来的任务是什么】：
      - 特殊任务（带 fn）：到点调用它的 fn。fn 查什么 / 走哪个域名 / 怎么节流，
        全由 fn 自己决定；主任务只负责排序和执行这一个。
        失败按 10/30/60 秒退避重新排队，不挡别的任务、不炸主循环。
      - 世界状态刷新请求（fn=None）：进锁后双检缓存，确实找不到才发
        【一次】全量请求（必过 fetch_worldstate() 内部的同域名 3~5 秒节流）。

    - 抢不到锁：已有任务在执行，本任务【不发请求】；等它开锁后走下一轮。
    - 本轮只执行【一个】任务；其余留在「下一个抢主任务队列」继续按到期排序。
    """
    global _retry_due, _failures, _last_fetch_error
    c0 = get_cache()
    # 还没到点（队首未到 / 节拍未到 / 没有到期的特殊任务）：纯读缓存、零请求
    if c0.get("data") and time.time() < next_due():
        return c0.get("queue") or []

    if not _task_lock.acquire(blocking=True, timeout=LOCK_WAIT):
        # 已有任务在执行：不抢、不再发请求，直接读缓存（开锁后下一轮自会找到）
        return get_queue()
    try:
        task = _take_due_task()          # 只取【已到期】且最快到期的那一个
        if task is not None and task.get("fn") is not None:
            # ---- 特殊任务：只执行，不看内容 ----
            try:
                task["fn"]()
                _drop_task(task)
                _log(f"特殊任务已执行：{task['label']}")
            except Exception as e:
                fails = int(task.get("fails") or 0) + 1
                gap = WS_RETRY_GAPS[min(fails - 1, len(WS_RETRY_GAPS) - 1)]
                _drop_task(task)
                _requeue_task(task, gap, fails)
                _log(f"特殊任务失败（第 {fails} 次）：{task['label']}：{e}；{gap:g}s 后重试")
            return get_queue()

        # ---- 世界状态刷新请求 ----
        c = get_cache()
        # 进锁后双检：缓存已是新的（队首未到 / 节拍未到）→ 不联网
        if c.get("data") and time.time() < _ws_allowed():
            return c.get("queue") or []
        if task is not None and c.get("data") and _cache_gen() > task["gen"]:
            # 别的任务刚全量刷新过并写了缓存：直接去缓存找，不再请求
            _drop_task(task)
            return c.get("queue") or []
        try:
            result = refresh_once()            # 【本轮唯一的一次】全量请求
            if task is not None:
                _drop_task(task)
            return result
        except Exception as e:
            _failures += 1
            gap = WS_RETRY_GAPS[min(_failures - 1, len(WS_RETRY_GAPS) - 1)]
            _retry_due = time.time() + gap
            _last_fetch_error = str(e)
            _log(f"全量刷新失败（第 {_failures} 次）：{e}；{gap:g}s 后退避重试")
    finally:
        _task_lock.release()                  # 开锁：其它任务醒来先去缓存找
        _reap_pending()                       # 被全量覆盖的任务直接移除（特殊任务保留）
    return None


def _ws_allowed():
    """世界状态全量请求的最早允许时刻（队首到期 / 节拍 / 退避 三者取最大）。"""
    c = get_cache()
    q = c.get("queue") or []
    head = float(q[0]["due"]) if q else 0.0
    return max(head, _next_allowed, _retry_due)


def next_due():
    """主任务下次醒来的时刻 = min(世界状态允许时刻, 最快到期的特殊任务)。

    特殊任务到自己的 due 就醒，【不受】世界状态 10/20/50 节拍限制：
    它的数据不在全量请求里，节拍管不到它（间隔由它自己的 fn 守）。

    无数据时：按退避节奏（_retry_due）重试，不空转猛刷。
    首拉（_retry_due=0）立即试，失败后 10/30/60 秒退避真正生效。
    """
    if not get_cache().get("data"):
        return max(0.0, _retry_due)
    with _pending_lock:
        special = [float(t["due"]) for t in _pending if t.get("fn") is not None]
    sp = min(special) if special else float("inf")
    return min(_ws_allowed(), sp)


def is_due():
    return time.time() >= next_due()


def _refresh_loop():
    """【唯一主循环】睡到最快到期那一刻 -> 只执行一个任务 -> 继续睡。

    醒来时刻 = min(世界状态队首 / 节拍 / 退避, 最快到期的特殊任务)。
    世界状态：把已到期的条目提交成任务（先查缓存，找不到才进队）。
    特殊任务：到自己的 due 就醒，直接执行它的 fn（主任务不看内容）。
    锁内只执行最快到期的那一个；世界状态一次全量写回全部数据后，其余
    世界状态任务开锁先查缓存即被满足，特殊任务保留等自己的轮次。
    """
    global _refresh_running
    try:
        while not _stop_ev.is_set():
            now = time.time()
            due = next_due()
            if due > now:
                wait = min(due - now, WS_WAKE_MAX)
                if wait > 0 and _stop_ev.wait(wait):
                    break
                continue
            _submit_due_entries()     # 已到期条目 -> 任务（先查缓存，找不到才进队）
            _run_once()               # 抢锁，本轮只执行最快到期的一个
            # 刷新后立刻重排：若新队首在未来，下一轮自然长睡，不会反复查询
            if _stop_ev.wait(0):
                break
    finally:
        _refresh_running = False


def ensure_fresh():
    """兼容监控推送：只读缓存，不阻塞刷新（刷新由唯一主任务按队列做）。"""
    return get_state()


# ----------------------------- 各类型格式化 -----------------------------
# 「全部」= 26 个栏目一次全给，整条消息会很长（QQ 单条有上限）。所以「全部」走精简：
# 每个列表只给最关键的几条，条数够看、一条消息能发完；要看全的就发单条指令。
_COMPACT = False


def _cap(full, brief):
    return brief if _COMPACT else full


def _lack(word):
    """某类型在【当前数据源】确实不提供时，给出如实提示
    （wf.wiki 的路子里只剩 信使前哨 / 集团任务 / 建造进度 三项没源）。"""
    if WS_SOURCE == "wfwiki":
        return f"{word}：（wf.wiki 数据源不提供此项）"
    return f"{word}：（当前无）"


def _cycle_left(epoch, text=None):
    """平原昼夜的剩余时间尾巴：绝对时刻优先；已过点写「刚刚结束（等刷新）」，
    绝不写「剩余 已结束」；只有接口只给相对时长（timeLeft）时才用它。"""
    e = _to_epoch(epoch)
    if e is None:
        return f" · 剩余 {text}" if text else ""
    left = e - time.time()
    if left <= 0:
        return " · 刚刚结束（等刷新）"
    return f" · 剩余 {_human(left)}"


# ---------------- 平原：时间 + 自己的赏金 ----------------
# 「平原」一次给五处时间，那每处也应该带自己的赏金任务板，
# 免得还要再单独发一次「赏金」去猜哪个集团属于哪个平原。
PLAIN_BOUNTY = {
    "cetus":   ("cetussyndicate", "ostron", "希图斯", "夜灵", "cetus"),
    "vallis":  ("solarissyndicate", "索拉里斯", "solaris", "fortuna", "奥布", "vallis"),
    "cambion": ("entratisyndicate", "英择谛", "entrati", "魔胎", "cambion"),
    "duviri":  ("duvirisyndicate", "duviri", "双衍"),
    "zariman": ("zarimansyndicate", "zariman", "扎里曼"),
}


def _bounty_for(s, key):
    """按 bountyBoard 的 tag / name 挑出属于这个平原的集团。"""
    keys = PLAIN_BOUNTY.get(key) or ()
    return [b for b in (s.get("bountyBoard") or [])
            if any(k in f"{b.get('tag') or ''} {b.get('name') or ''}".lower()
                   for k in keys)]


def _job_text(j):
    """赏金任务一行：目标 + 等级 + 阶段数；数据源没给目标时写「目标未给」。"""
    lo, hi = j.get("minLevel"), j.get("maxLevel")
    lv = f" Lv{lo}-{hi}" if lo is not None and hi is not None else ""
    st = j.get("stages")
    obj = j.get("objective") or "?"
    if obj in ("?", "$undefined"):
        obj = "目标未给"
    return f"{obj}{lv}" + (f" · {st} 阶段" if st else "")


def _bounty_lines(groups, limit):
    out = []
    for b in groups:
        jobs = b.get("jobs") or []
        out.append(f"   赏金 · {b.get('name') or '?'}（{len(jobs)} 个"
                   f"{_bounty_board_rot(b)}）")
        for j in jobs[:limit]:
            out.append(f"     - {_job_text(j)}")
        more = len(jobs) - min(len(jobs), limit)
        if more > 0:
            out.append(f"     …… 还有 {more} 个（发「赏金」看全部）")
    return out


def _plain_with_bounty(s, key, head):
    """平原时间行后面紧跟它自己的赏金；该区域没有赏金板就如实说明原因，避免看着像「没数据/出错」。"""
    out = [head]
    groups = _bounty_for(s, key)
    if groups:
        out += _bounty_lines(groups, _cap(5, 2))
    else:
        # 双衍王境 / 扎里曼 这类区域是独立周期（愤怒/喜悦、Corpus 等），
        # 本身没有 Ostron 那种开放世界赏金任务板，所以没有赏金行是正常的。
        out.append("    （该区域无开放世界赏金任务板 · 非集团赏金区，正常）")
    return "\n".join(out)


def _fmt_cetus(s):
    c = s.get("cetusCycle") or {}
    is_day = c.get("isDay")
    state = "☀️ 白天" if is_day else "🌙 黑夜"
    return _plain_with_bounty(s, "cetus", "希图斯（地球平原）：" + state
                              + _cycle_left(c.get("expiry"), c.get("timeLeft")))


def _fmt_vallis(s):
    v = s.get("vallisCycle") or {}
    warm = v.get("isWarm")
    state = "🔥 温暖" if warm else "❄️ 寒冷"
    return _plain_with_bounty(s, "vallis", "奥布山谷（金星平原）：" + state
                              + _cycle_left(v.get("expiry"), v.get("timeLeft")))


def _fmt_cambion(s):
    c = s.get("cambionCycle") or {}
    fass = c.get("isFass")
    state = "🔴 法斯(Fass)" if fass else "🔵 温德(Vome)"
    return _plain_with_bounty(s, "cambion", "魔胎之境（火卫二平原）：" + state
                              + _cycle_left(c.get("expiry"), c.get("timeLeft")))


def _circuit_lines(s, indent="  "):
    """回廊轮换的正文行（不含标题）：普通回廊三选一战甲 + 钢铁回廊五把灵化武器。

    「回廊」独立栏目与「双衍王境」平原块共用这一份渲染，避免两处各写一套走样。
    """
    c = s.get("circuit") or {}
    nrm = c.get("normal") or []
    hard = c.get("hard") or []
    out = []
    if nrm:
        out.append(f"{indent}· 普通回廊 · 战甲三选一：" + "、".join(nrm))
    if hard:
        out.append(f"{indent}· 钢铁回廊 · 灵化武器：" + "、".join(hard))
    return out


def _fmt_duviri(s):
    """双衍王境：区域周期 + 无尽回廊（回廊是王境内的常驻玩法，归这一块一起给）。"""
    d = s.get("duviriCycle") or {}
    head = ("🌀 双衍王境：" + (d.get("state") or "?") + _cycle_left(d.get("expiry"))
            if d else "🌀 双衍王境：（当前无）")
    out = [_plain_with_bounty(s, "duviri", head)]
    # 「全部」这种一次十几栏的精简模式里回廊另有独立块，这里不重复贴一遍
    lines = _circuit_lines(s, "     ")
    if lines and not _COMPACT:
        out.append("    无尽回廊（本周"
                   + _cycle_left((s.get("circuit") or {}).get("expiry")) + "）")
        out += lines
    return "\n".join(out)


def _fmt_zariman(s):
    z = s.get("zarimanCycle") or {}
    if not z:
        return _plain_with_bounty(s, "zariman", "🛰️ 扎里曼：（当前无）")
    return _plain_with_bounty(s, "zariman", "🛰️ 扎里曼："
                              + (z.get("state") or "?") + _cycle_left(z.get("expiry")))


def _fmt_cycles(s):
    # 每处平原之间留空行：卡片里就是「一处平原一块」，时间 + 自己的赏金绑在一起
    return "\n\n".join([_fmt_cetus(s), _fmt_vallis(s), _fmt_cambion(s),
                        _fmt_duviri(s), _fmt_zariman(s)])


def _fmt_alerts(s):
    items = s.get("alerts") or []
    if not items:
        return "📢 警报：（当前无）"
    out = [f"📢 警报（共 {len(items)}）"]
    for i, a in enumerate(items[:_cap(10, 3)], 1):
        m = a.get("mission") or {}
        node = m.get("node", "?")
        mt = m.get("type", "?")
        fac = m.get("faction", "?")
        lv = m.get("minEnemyLevel")
        hv = m.get("maxEnemyLevel")
        lvl = f"Lv{lv}-{hv}" if lv is not None else ""
        rw = a.get("reward") or {}
        ritems = rw.get("items") or []
        cred = rw.get("credits")
        parts = []
        if ritems:
            parts.append("、".join(str(x) for x in ritems))
        if cred:
            parts.append(f"{cred} 信誉")
        reward = "、".join(parts) if parts else "无"
        out.append(f"{i}. {node} · {mt} · {fac} {lvl}")
        out.append(f"   奖励：{reward}")
        out.append(f"   剩余：{_fmt_left(a.get('expiry'))}")
    return "\n".join(out)


# ---------------- 裂缝：常规星球 / 九重天 分区 + 遗物等级排序 ----------------
# 用户口径「古前中后安魂全能」= 古纪 / 前纪 / 中纪 / 后纪 / 安魂 / 全能。
# 九重天（Railjack / Empyrean）在 wf.wiki 里写成「XX比邻星域」，
# 和常规星球混在一起显示很乱，所以单独成组放在下面，组内自己排。
TIER_ORDER = ("古纪", "前纪", "中纪", "后纪", "安魂", "全能")
TIER_ALIAS = {
    "lith": "古纪", "古": "古纪", "古纪": "古纪",
    "meso": "前纪", "前": "前纪", "前纪": "前纪",
    "neo": "中纪", "中": "中纪", "中纪": "中纪",
    "axi": "后纪", "后": "后纪", "后纪": "后纪",
    "requiem": "安魂", "安魂": "安魂",
    "omnia": "全能", "全能": "全能",
}
RAILJACK_MARKERS = ("比邻星域", "proxima", "empyrean", "railjack", "九重天")

# 裂缝过滤用的「任务类型」词表（中文优先，附带英文；长词在前，避免「移动防御」
# 被「防御」抢先截断）。用来把「火星中断」「后纪中断」这类连写过滤词切成独立条件。
# 星球/地点名单独放在下面的 FISSURE_LOCATION_TOKENS，切完后再整体当子串匹配节点。
FISSURE_MISSION_TOKENS = (
    "移动防御", "无尽捕获", "虚空覆涌", "虚空风暴", "元素转换", "前哨战", "强袭",
    "刺杀", "挖掘", "爆发", "拦截", "救援", "间谍", "破坏", "生存", "防御",
    "歼灭", "捕获", "中断",
    "转化", "腐化", "清巢", "空战", "平原", "突袭", "圣殿",
    "Disruption", "Defense", "Survival", "Extermination", "Capture", "Sabotage",
    "Spy", "Rescue", "Interception", "Assault", "Assassination", "Excavate",
    "Mobile Defense", "Void Cascade", "Alchemy", "Landscape", "Corruption",
    "Hive", "Archwing", "Railjack",
)

# 裂缝过滤里可单独出现的「地点」词（星球/中继/开放世界）；用于把「火星中断」切成
# [火星, 中断]，同时把完全无法识别的残留词判为「不认识」而不是默默开一个空过滤。
FISSURE_LOCATION_TOKENS = (
    "比邻星域", "赤毒要塞", "库瓦要塞", "扎里曼", "奥布山谷", "魔胎之境",
    "双衍王境", "夜灵平原", "希图斯", "九重天", "火卫一", "火卫二",
    "地球", "金星", "火星", "水星", "木星", "土星", "天王星", "海王星",
    "冥王星", "月球", "谷神星", "欧罗巴", "赛德娜", "虚空",
)


def _tokenize_fissure(text):
    """切裂缝过滤词，返回 (conds, leftover)。

    conds：已识别的必需条件（任务类型 / 归一后的中文纪名 / 地点名）；
    leftover：无法识别的残留词（去空白后），非空表示用户写了不认识的词。
    """
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return [], ""
    conds = []
    # 任务类型最先切：「中断」整体取走后，单字纪别名「中」才不会误命中
    for m in sorted(FISSURE_MISSION_TOKENS, key=len, reverse=True):
        ml = m.lower()
        if ml and ml in t:
            conds.append(ml)
            t = t.replace(ml, "")
    for era in sorted(set(TIER_ALIAS.keys()), key=len, reverse=True):   # 纪（长词优先）
        if era and era in t:
            conds.append(TIER_ALIAS[era].lower())
            t = t.replace(era, "")
    for loc in sorted(FISSURE_LOCATION_TOKENS, key=len, reverse=True):  # 地点（长词优先）
        if loc in t:
            conds.append(loc)
            t = t.replace(loc, "")
    return conds, re.sub(r"\s+", "", t)


def parse_fissure_tokens(text):
    """把裂缝过滤词串切成一组【必需条件】（AND）：每个条件都是裂缝条目的子串。

    识别（长词优先）：先切任务类型词表，再切纪别名，最后切地点名；纪别名归一成
    中文纪名。连写也能切：『后纪中断』→ [后纪, 中断]；『火星中断』→ [火星, 中断]。
    无法识别的残留词按整体子串收进条件（调用方可用 parse_fissure_spec 的
    residual 判断是否为「不认识」）。空列表表示不过滤（整栏）。
    """
    conds, leftover = _tokenize_fissure(text)
    if leftover:
        conds.append(leftover)
    return conds


def parse_fissure_spec(text):
    """解析「裂缝/钢铁裂缝 + 过滤词」规格；与裂缝无关返回 None。

    返回 {"column": "fissures"|"steel", "tokens": [...], "residual": bool}。
    column：显式含「钢铁/steel」→ steel，否则含「裂缝/fissure」→ fissures；
    都没有但过滤词命中裂缝概念（纪/任务类型/地点）→ 推断 fissures；否则 None。
    residual：为 True 表示还有无法识别的词（监控路径据此回提示，避免误开空过滤）。
    """
    low = (text or "").strip().lower()
    for w in ("监控", "monitor"):          # 监控路径可能还带着前缀
        if low.startswith(w):
            low = low[len(w):].strip()
    if not low:
        return None
    has_steel = any(w in low for w in ("钢铁裂缝", "钢铁", "steelpath", "steel"))
    has_fissure = any(w in low for w in ("裂缝", "裂隙", "fissure"))
    col = "steel" if has_steel else ("fissures" if has_fissure else None)
    ft = low
    for w in ("钢铁裂缝", "钢铁", "裂缝", "裂隙", "fissure", "steel", "steelpath"):
        ft = ft.replace(w, "")
    # 其它栏目词（警报/活动/平原…）不是裂缝条件，先剔掉，避免「监控 裂缝 警报」变成「裂缝里含警报」；
    # 但地名（扎里曼/奥布山谷…）本身又是栏目词，必须保留，否则「裂缝 扎里曼中断」会丢掉地点。
    reserved_loc = {loc.lower() for loc in FISSURE_LOCATION_TOKENS}
    for kw, k in KEYWORDS.items():
        if k in ("fissures", "steel", "__all__"):
            continue
        if any(kw in loc for loc in reserved_loc):   # 地名本身或地名片段（奥布/山谷）
            continue
        if kw in ft:
            ft = ft.replace(kw, "")
    conds, leftover = _tokenize_fissure(ft)
    if col is None:
        col = "fissures" if (conds or leftover) else None
    if not col:
        return None
    return {"column": col, "tokens": conds, "residual": bool(leftover)}


def _fissure_matches(f, tokens):
    """裂缝条目是否命中全部过滤条件（AND）：每个 token 命中任一字段即算满足。"""
    if not tokens:
        return True
    node = str(f.get("node") or "").lower()
    mt = str(f.get("missionType") or "").lower()
    enemy = str(f.get("enemy") or "").lower()
    tier_raw = str(f.get("tier") or "").lower()
    # 数据源可能给英文纪名（Lith/Neo…），过滤词已归一成中文，这里一并归一后比对
    tier = TIER_ALIAS.get(tier_raw, tier_raw)
    for tok in tokens:
        if tok in tier or tok in tier_raw or tok in node or tok in mt or tok in enemy:
            continue
        return False
    return True


def tier_name(tier):
    """中英文 / 简写遗物等级归一成中文名；认不出就原样返回。"""
    key = str(tier or "").strip().lower()
    return TIER_ALIAS.get(key, str(tier or "").strip())


def tier_rank(tier):
    """排序权重：古→前→中→后→安魂→全能，未知等级排最后不让它们插队。"""
    name = tier_name(tier)
    return TIER_ORDER.index(name) if name in TIER_ORDER else len(TIER_ORDER)


def is_railjack(f):
    """九重天裂缝判定：节点/星系/任务名里带比邻星域或 Railjack 关键字。"""
    blob = " ".join(str(f.get(k) or "") for k in
                    ("node", "system", "missionType", "enemy", "tier")).lower()
    return any(m in blob for m in RAILJACK_MARKERS)


def _fmt_fissure_list(items, title, tokens=None):
    """常规星球在上、九重天在下；各组内按遗物等级再按剩余时间升序。

    tokens：裂缝过滤条件（AND，每条都是条目字段的子串）；为空不过滤。
    """
    if tokens:
        items = [f for f in items if _fissure_matches(f, tokens)]
    if not items:
        if tokens:
            return f"{title}：（按「{' '.join(tokens)}」过滤后当前无匹配）"
        return f"{title}：（当前无）"
    # 已过期的条目不再列出（缓存没赶上刷新时会堆一批「剩余：已结束」，看着像还有）
    now = time.time()
    live = [f for f in items
            if _to_epoch(f.get("expiry")) is None or _to_epoch(f.get("expiry")) > now]
    expired = len(items) - len(live)
    if not live:
        return f"{title}：（缓存里的 {len(items)} 条都已结束，等下一次刷新）"

    normal = [f for f in live if not is_railjack(f)]
    rj = [f for f in live if is_railjack(f)]

    def sort_key(f):
        e = _to_epoch(f.get("expiry"))
        return (tier_rank(f.get("tier")), e if e is not None else float("inf"))

    normal.sort(key=sort_key)
    rj.sort(key=sort_key)

    groups = [(n, g) for n, g in (("常规星球", normal), ("九重天", rj)) if g]
    # 单栏给全（每组最多 30 条）；一次问多个栏目走精简，两组各露面 2 条，
    # 免得九重天全被排在前面的常规星球挤掉。
    per = _cap(30, 2 if len(groups) > 1 else 4)
    out = []
    for gi, (gname, group) in enumerate(groups):
        if gi:
            out.append("")            # 空行 = 卡片里另起一个分区，视觉上上下两块
        head = (f"{title} · {gname}（共 {len(group)}）" if len(groups) > 1
                else f"{title}（共 {len(group)}）")
        out.append(head)
        pick = group[:per]
        last_tier = None
        for num, f in enumerate(pick, 1):
            tn = tier_name(f.get("tier"))
            band = tn if tn in TIER_ORDER else "其它"
            if band != last_tier:
                out.append(f"— {band} —")
                last_tier = band
            out.append(f"{num}. {f.get('node', '?')} · {f.get('missionType', '?')} · "
                       f"{f.get('enemy', '?')} · {tn or '?'}")
            out.append(f"   剩余：{_fmt_left(f.get('expiry'))}")
        rest = len(group) - len(pick)
        if rest > 0:
            out.append(f"   …… 还有 {rest} 条（单独发「{title.split()[-1]}」看全部）")
    if expired:
        out.append(f"   （另有 {expired} 条已结束，未列出）")
    return "\n".join(out)


def _fmt_fissures(s, tokens=None):
    return _fmt_fissure_list(s.get("fissures") or [], "🔥 裂缝", tokens)


def _fmt_steel(s, tokens=None):
    sp = s.get("steelPath") or {}
    return _fmt_fissure_list(sp.get("current") or [], "⚙️ 钢铁裂缝", tokens)


def _fmt_sortie(s):
    so = s.get("sortie") or {}
    if not so:
        return _lack("⚔️ 突击")
    boss = so.get("boss") or so.get("faction") or "?"
    exp = _fmt_left(so.get("expiry"))
    variants = so.get("variants") or []
    out = [f"⚔️ 突击 · {boss} · 结束 {exp}"]
    for i, v in enumerate(variants[:3], 1):
        node = v.get("node", "?")
        mt = v.get("modifier") or v.get("missionType") or "?"
        desc = v.get("modifierDescription") or ""
        out.append(f"  {i}. {node} · {mt}" + (f"：{desc}" if desc else ""))
    return "\n".join(out)


def _fmt_circuit(s):
    """无尽回廊本周轮换：普通回廊 3 套战甲 + 钢铁回廊 5 把灵化武器。"""
    c = s.get("circuit") or {}
    if not c:
        return "🌀 无尽回廊：（暂时取不到，稍后自动重试）"
    out = [f"🌀 无尽回廊（本周{_cycle_left(c.get('expiry'))}）"]
    out += _circuit_lines(s)
    if not (c.get("normal") or c.get("hard")):
        out.append("  （本轮轮换数据为空，等刷新）")
    out.append("（每周一 00:00 UTC 轮换；名称沿用官方原名）")
    return "\n".join(out)


def _fmt_arbitration(s):
    a = s.get("arbitration") or {}
    if not a:
        return ("🛡️ 仲裁：（轮换表暂不可用 —— arbi.wf.wiki 没取到数据，"
                "后台会自动重试；稍后再发「仲裁」即可）")
    node = a.get("node", "?")
    mt = a.get("type") or a.get("missionType") or "?"
    en = a.get("enemy") or "?"
    lv = a.get("level") or ""
    line = f"🛡️ 仲裁：{node} · {mt} · {en}" + (f" · Lv{lv}" if lv else "")
    if a.get("expiry"):
        left = _fmt_left(a.get("expiry"))
        if left == "已结束":
            line += "\n   换图：本条已换图，等下一次刷新（每小时整点换）"
        else:
            line += f"\n   换图：{left} 后（每小时整点换）"
    return line


def _fmt_events(s):
    items = s.get("events") or []
    if not items:
        return "🎉 活动：（当前无）"
    out = [f"🎉 活动（共 {len(items)}）"]
    for i, ev in enumerate(items[:_cap(6, 2)], 1):
        desc = ev.get("description") or ev.get("headline") or "?"
        if len(desc) > 80:
            desc = desc[:80] + "…"
        out.append(f"{i}. {desc}")
        out.append(f"   剩余：{_fmt_left(ev.get('expiry'))}")
    return "\n".join(out)


def _fmt_invasions(s):
    items = s.get("invasions") or []
    if not items:
        return "⚔️ 入侵：（当前无）"
    out = [f"⚔️ 入侵（共 {len(items)}）"]
    for i, inv in enumerate(items[:_cap(8, 2)], 1):
        node = inv.get("node", "?")
        prog = inv.get("progress") or "?"
        ar = inv.get("attackerReward") or {}
        dr = inv.get("defenderReward") or {}
        aitems = ar.get("items") or []
        ditems = dr.get("items") or []
        out.append(f"{i}. {node} · 进度 {prog}")
        if aitems:
            out.append(f"   进攻：{'、'.join(str(x) for x in aitems)}")
        if ditems:
            out.append(f"   防守：{'、'.join(str(x) for x in ditems)}")
    return "\n".join(out)


def _fmt_nightwave(s):
    nw = s.get("nightwave") or {}
    if not nw:
        return "📻 电波：（当前无赛季）"
    season = nw.get("season") or nw.get("title") or "?"
    exp = _fmt_left(nw.get("expiry"))
    chal = nw.get("challenges") or []
    out = [f"📻 电波（赛季 {season}）" + (f" · 结束 {exp}" if exp else "")]
    for c in chal[:_cap(8, 2)]:
        title = c.get("title") or c.get("desc") or "?"
        rep = c.get("reputation")
        out.append(f"  - {title}" + (f"（{rep} 声望）" if rep else ""))
    return "\n".join(out)


def _fmt_voidtrader(s):
    vt = s.get("voidTrader") or {}
    if not vt:
        return "🛸 虚空商人（白老头）：数据缺失"
    active = vt.get("active")
    exp = _fmt_left(vt.get("expiry"))
    loc = vt.get("location") or "?"
    inv = vt.get("inventory") or []
    if active:
        out = [f"🛸 虚空商人（白老头）：已在 {loc}"]
        for it in inv[:_cap(10, 3)]:
            name = it.get("item") or "?"
            duc = it.get("ducats")
            cr = it.get("credits")
            out.append(f"  - {name} · {duc} 杜卡德 / {cr} 现金" if duc is not None else f"  - {name}")
    else:
        out = [f"🛸 虚空商人（白老头）：{exp} 后抵达（地点 {loc}）"]
    return "\n".join(out)


def _fmt_daily(s):
    items = s.get("dailyDeals") or []
    if not items:
        return "🏷️ Darvo 每日特惠：（当前无）"
    out = [f"🏷️ Darvo 每日特惠（共 {len(items)}）"]
    for i, d in enumerate(items[:_cap(6, 2)], 1):
        name = d.get("item") or "?"
        op = d.get("originalPrice")
        sp_ = d.get("salePrice")
        disc = d.get("discount")
        out.append(f"{i}. {name} — {op}→{sp_}" + (f"（-{disc}%）" if disc else ""))
    return "\n".join(out)


def _rot_note(epoch):
    """轮换尾巴：没到点给倒计时，已过点就如实说「本轮已到期」，不写「已结束 后换」。"""
    e = _to_epoch(epoch)
    if e is None:
        return ""
    left = e - time.time()
    if left <= 0:
        return " · 本轮已到期，等下一次换"
    return f" · {_human(left)} 后换"


def _bounty_board_rot(b):
    """赏金板的轮换尾巴：已轮换时明确标「上一轮任务板」，避免把过期旧任务当成当前。"""
    e = _to_epoch(b.get("expiry"))
    if e is None:
        return ""
    left = e - time.time()
    if left <= 0:
        return " · 本轮已轮换（上一轮任务板，等刷新）"
    return f" · {_human(left)} 后换"


# 六个赏金集团所属地区：数据源只给集团名，不给地区
# （坚守者在扎里曼、科维兽在火卫二的 Sanctum、六人组在 1999）
BOUNTY_REGION = {
    "Ostron": "希图斯",
    "索拉里斯": "福尔图娜",
    "英择谛": "魔胎之境",
    "坚守者": "扎里曼",
    "科维兽": "火卫二",
    "六人组": "1999",
}

# 赏金过滤别名：用户可能按集团名、地区名或英文写，统一归一到上面那张表里的名字
BOUNTY_ALIAS = {
    "ostron": "Ostron", "cetus": "希图斯", "希图斯": "希图斯", "地球": "希图斯",
    "solaris": "索拉里斯", "fortuna": "福尔图娜", "福尔图娜": "福尔图娜", "金星": "福尔图娜",
    "entrati": "英择谛", "魔胎": "魔胎之境", "deimos": "魔胎之境",
    "holdfast": "扎里曼", "zariman": "扎里曼",
    "cavia": "科维兽", "sanctum": "科维兽", "火卫二": "科维兽",
    "hex": "六人组", "hollvania": "1999",
}

# 归一后可被赏金板匹配的名字（集团名 + 地区名），用于判断过滤词是否认得
BOUNTY_KNOWN = frozenset(BOUNTY_REGION) | frozenset(BOUNTY_REGION.values())


def parse_bounty_tokens(text):
    """赏金过滤词：把「Ostron / 坚守者 / 扎里曼 / fortuna」等归一成集团名或地区名。

    空列表表示不过滤（6 个集团全给）；无法识别的残留原样返回，
    由格式化阶段如实回「没有匹配 … 的赏金板」，不会静默变成整栏。
    """
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return []
    toks = []
    for alias in sorted(BOUNTY_ALIAS, key=len, reverse=True):
        if alias and alias in t:
            norm = BOUNTY_ALIAS[alias]
            if norm not in toks:
                toks.append(norm)
            t = t.replace(alias, "")
    t = re.sub(r"\s+", "", t)
    if t:
        toks.append(t)
    return toks


def _bounty_all(s):
    """六个赏金集团合一：bountyBoard 给 Ostron / 索拉里斯 / 英择谛（带奖励），
    bounties.groups 给 坚守者 / 科维兽 / 六人组（带节点 / 任务 / 挑战 / 同伴）。"""
    out = []
    for b in (s.get("bountyBoard") or []):
        name = b.get("name") or "?"
        out.append({"name": name, "region": BOUNTY_REGION.get(name, ""),
                    "expiry": b.get("expiry"), "jobs": b.get("jobs") or []})
    bt = s.get("bounties") or {}
    for g in (bt.get("groups") or []):
        name = g.get("name") or "?"
        out.append({"name": name, "region": BOUNTY_REGION.get(name, ""),
                    "expiry": bt.get("expiry"), "jobs": g.get("jobs") or []})
    return out


def _bounty_job_line(j):
    """一档赏金：奖励型给「目标 LvX-Y · N 阶段」；挑战型给「节点 · 任务 · 挑战（同伴）」。"""
    if j.get("node"):
        line = f"{j.get('node')} · {j.get('mission') or '?'}"
        if j.get("challenge"):
            line += f" · {j['challenge']}"
        if j.get("ally"):
            line += f"（{j['ally']}）"
        return line
    return _job_text(j)


def _reward_text(j, limit):
    """奖励摘要：取前 limit 项，数量 >1 才写 ×N，超出补「等 N 项」。"""
    rs = j.get("rewards") or []
    if not rs:
        return ""
    parts = []
    for r in rs[:limit]:
        c = r.get("count")
        parts.append(str(r.get("name") or "?")
                     + (f"×{c}" if isinstance(c, int) and c > 1 else ""))
    return "、".join(parts) + (f" 等 {len(rs)} 项" if len(rs) > limit else "")


def _fmt_upgrades(s):
    ups = s.get("globalUpgrades") or []
    if not ups:
        return "✨ 全局加成：（当前无）"
    out = [f"✨ 全局加成（共 {len(ups)}）"]
    for u in ups[:6]:
        out.append(f"  - {u.get('desc') or '?'} · 剩余 {_fmt_left(u.get('expiry'))}")
    return "\n".join(out)


def _weapon_lines(items, limit=10):
    rows = []
    for i, w in enumerate(items[:limit], 1):
        el = w.get("element") or ""
        bo = w.get("bonus")
        tail = " · ".join(x for x in [el, (f"+{bo}%" if isinstance(bo, (int, float)) else "")]
                          if x)
        rows.append(f"{i}. {w.get('name') or '?'}" + (f"（{tail}）" if tail else ""))
    return rows


def _fmt_tenet(s):
    tc = s.get("tenetCoda") or {}
    items = tc.get("tenet") or []
    if not items:
        return "🗡️ 信条武器：（当前无）"
    out = [f"🗡️ 信条武器（{len(items)} 件{_rot_note(tc.get('expiry'))}）"] + _weapon_lines(items, _cap(10, 4))
    return "\n".join(out)


def _fmt_coda(s):
    tc = s.get("tenetCoda") or {}
    items = tc.get("coda") or []
    if not items:
        return "🔫 终幕武器：（当前无）"
    batch = tc.get("codaBatch")
    head = f"🔫 终幕武器（{len(items)} 件" + (f" · 批次 {batch}" if batch else "")
    head += _rot_note(tc.get("codaExpiry")) + "）"
    return "\n".join([head] + _weapon_lines(items, _cap(10, 4)))


def _fmt_bounty(s, tokens=None):
    """赏金任务板：六个集团一次列全，每档带奖励摘要。

    带 tokens（集团名 / 地区名）时只留匹配的集团，并把该集团每档奖励全列出来
    （整栏时只给前几项，避免一条消息长到超出 QQ 上限）。
    """
    board = _bounty_all(s)
    only = [t.lower() for t in (tokens or []) if t]
    if only:
        board = [b for b in board
                 if any(t in (b.get("name") or "").lower()
                        or t in (b.get("region") or "").lower() for t in only)]
        if not board:
            return "🧾 赏金：没有匹配「%s」的赏金板" % "、".join(tokens)
    if not board:
        return "🧾 赏金：（当前无）"
    one = bool(only) and len(board) == 1       # 单集团：全部档位 + 全部奖励
    n_jobs = 99 if only else _cap(3, 2)        # 带过滤时全列，整栏时只给前几档
    n_rw = 99 if only else _cap(5, 3)
    out = []
    if one:
        b0 = board[0]
        tail = f" · {b0['region']}" if b0.get("region") else ""
        out.append(f"🧾 {b0.get('name')} 赏金"
                   f"（{len(b0.get('jobs') or [])} 个{tail}{_bounty_board_rot(b0)}）")
    else:
        out.append(f"🧾 开放世界赏金（{len(board)} 个集团）")
    for b in board:
        jobs = b.get("jobs") or []
        if not one:
            tail = f" · {b['region']}" if b.get("region") else ""
            out.append(f"  · {b.get('name') or '?'}（{len(jobs)} 个{tail}"
                       f"{_bounty_board_rot(b)}）")
        for j in jobs[:n_jobs]:
            out.append(f"      - {_bounty_job_line(j)}")
            rw = _reward_text(j, n_rw)
            if rw:
                out.append(f"        奖励：{rw}")
        if len(jobs) > n_jobs:
            out.append(f"      …… 还有 {len(jobs) - n_jobs} 个"
                       f"（发「赏金 {b.get('name') or ''}」看全部）")
    if not one and not only:
        # 说明「赏金」和「平原」的关系：同一份赏金板的不同视图，免得用户以为两套数据
        out.append("（与「平原」各区域附带的任务板是同一份数据；"
                   "发「赏金 <集团名>」可看单个集团全部档位与奖励）")
    return "\n".join(out)


def _fmt_bounty_rot(s):
    """赏金轮换：只讲本轮/宝库奖励，各集团的具体任务与奖励归「赏金」栏目，
    两边不重复（否则监控两个栏目时会看到同一批内容各推一次）。"""
    bt = s.get("bounties") or {}
    if not bt:
        return "🔄 赏金轮换：（当前无）"
    rot = bt.get("rot") or "?"
    out = [f"🔄 赏金轮换（本轮 {rot}{_rot_note(bt.get('expiry'))}）"]
    rw = bt.get("rotRewards") or []
    if rw:
        out.append("  本轮奖励：" + "、".join(str(x) for x in rw[:_cap(8, 6)]))
    vw = bt.get("vaultRewards") or []
    if vw:
        out.append(f"  宝库轮换 {bt.get('vaultRot') or '?'}："
                   + "、".join(str(x) for x in vw[:_cap(8, 6)]))
    if not rw and not vw:
        out.append("  （本轮奖励数据源暂未给出）")
    out.append("（坚守者 / 科维兽 / 六人组 等集团的任务与奖励在「赏金」里）")
    return "\n".join(out)


def _fmt_varzia(s):
    v = s.get("varzia") or {}
    if not v:
        return "💎 瓦奇娅：（当前无）"
    if not v.get("active"):
        return f"💎 瓦奇娅：未到场 · {_fmt_left(v.get('activation'))} 后抵达"
    out = [f"💎 瓦奇娅（原初重生）· {v.get('node') or 'Maroo 的市集'}"
           f" · 剩余 {_fmt_left(v.get('expiry'))}"]
    inv = v.get("inventory") or []
    for it in inv[:_cap(8, 3)]:
        price = []
        for key, unit in (("regalAya", "御品阿耶"), ("aya", "阿耶")):
            v = it.get(key)
            if isinstance(v, (int, float)) and v:      # 只认数字，$undefined 一律不显示
                price.append(f"{v:g} {unit}")
        out.append(f"  - {it.get('item') or '?'}" + (f"（{' / '.join(price)}）" if price else ""))
    if len(inv) > 8:
        out.append(f"  …… 还有 {len(inv) - 8} 件")
    return "\n".join(out)


def _fmt_news(s):
    items = s.get("news") or []
    if not items:
        return "📰 新闻：（当前无）"
    lim = _cap(8, 3)
    out = [f"📰 新闻（最新 {min(lim, len(items))} 条，共 {len(items)} 条）"]
    for i, n in enumerate(items[:lim], 1):
        t = str(n.get("title") or "?")
        if len(t) > 56:
            t = t[:56] + "…"
        out.append(f"{i}. {t}")
    return "\n".join(out)


def _fmt_outpost(s):
    o = s.get("sentientOutpost") or {}
    if not o:
        return _lack("📡 信使前哨")
    active = o.get("active")
    loc = o.get("location") or ""
    exp = _fmt_left(o.get("expiry"))
    if active:
        return f"📡 信使前哨：活跃" + (f" · 地点 {loc}" if loc else "")
    return f"📡 信使前哨：未活跃" + (f" · {exp} 后" if exp else "")


def _fmt_syndicate(s):
    items = s.get("syndicateMissions") or []
    if not items:
        return _lack("🤝 集团任务")
    out = [f"🤝 集团任务（共 {len(items)}）"]
    for sm in items[:_cap(8, 2)]:
        name = sm.get("syndicate") or "?"
        nodes = sm.get("nodes") or []
        out.append(f"  - {name}：" + ("、".join(str(n) for n in nodes) if nodes else "?"))
    return "\n".join(out)


def _fmt_construction(s):
    cp = s.get("constructionProgress") or {}
    if not cp:
        return _lack("🏗️ 建造进度")
    out = ["🏗️ 建造进度"]
    for k, v in cp.items():
        if isinstance(v, dict):
            pct = v.get("pct") or v.get("progress") or v.get("percentage")
            out.append(f"  - {k}：{pct if pct is not None else v}")
        else:
            out.append(f"  - {k}：{v}")
    return "\n".join(out)


# ----------------------------- 类型注册表 -----------------------------
TYPES = {
    "alerts":       ("警报", _fmt_alerts),
    "cycles":       ("平原昼夜", _fmt_cycles),
    "cetus":        ("希图斯", _fmt_cetus),
    "vallis":       ("奥布山谷", _fmt_vallis),
    "cambion":      ("魔胎之境", _fmt_cambion),
    "duviri":       ("双衍王境", _fmt_duviri),
    "zariman":      ("扎里曼", _fmt_zariman),
    "circuit":      ("回廊", _fmt_circuit),
    "sortie":       ("突击", _fmt_sortie),
    "arbitration":  ("仲裁", _fmt_arbitration),
    "fissures":     ("裂缝", _fmt_fissures),
    "steel":        ("钢铁裂缝", _fmt_steel),
    "bounty":       ("赏金", _fmt_bounty),
    "bountyrot":    ("赏金轮换", _fmt_bounty_rot),
    "tenet":        ("信条武器", _fmt_tenet),
    "coda":         ("终幕武器", _fmt_coda),
    "upgrades":     ("全局加成", _fmt_upgrades),
    "events":       ("活动", _fmt_events),
    "invasions":    ("入侵", _fmt_invasions),
    "nightwave":    ("电波", _fmt_nightwave),
    "voidtrader":   ("虚空商人", _fmt_voidtrader),
    "daily":        ("Darvo特惠", _fmt_daily),
    "varzia":       ("瓦奇娅", _fmt_varzia),
    "outpost":      ("信使前哨", _fmt_outpost),
    "syndicate":    ("集团任务", _fmt_syndicate),
    "construction": ("建造进度", _fmt_construction),
    "news":         ("新闻", _fmt_news),
}

KEYWORDS = {
    "警报": "alerts", "alert": "alerts", "alerts": "alerts",
    "平原": "cycles", "cycle": "cycles", "昼夜": "cycles", "cycles": "cycles",
    "平原昼夜": "cycles",
    # 区域名必须和推送标签一模一样：机器人推的是「奥布山谷/魔胎之境/双衍王境」，
    # 用户照着打「奥布山谷=关闭」却匹配不上（旧表只有「奥布」「山谷」），
    # 会静默无反应。这里把全名与常见别称都精确列进来（仍不做模糊匹配）。
    "希图斯": "cetus", "cetus": "cetus", "地球平原": "cetus",
    "奥布": "vallis", "vallis": "vallis", "山谷": "vallis",
    "奥布山谷": "vallis", "金星平原": "vallis",
    "魔胎": "cambion", "cambion": "cambion",
    "魔胎之境": "cambion", "火卫二": "cambion", "火卫二平原": "cambion",
    "双衍": "duviri", "duviri": "duviri", "王境": "duviri", "双衍王境": "duviri",
    "扎里曼": "zariman", "zariman": "zariman",
    # 回廊（无尽回廊）只在 DE 官方端点 EndlessXpSchedule 里，wf.wiki 没有；
    # 灵化 = 钢铁回廊的灵化武器轮换，同一个栏目。
    "回廊": "circuit", "无尽回廊": "circuit", "circuit": "circuit",
    "钢铁回廊": "circuit", "灵化": "circuit", "灵化武器": "circuit",
    "incarnon": "circuit",
    "突击": "sortie", "sortie": "sortie",
    "仲裁": "arbitration", "arbitration": "arbitration", "arb": "arbitration",
    "裂缝": "fissures", "fissure": "fissures", "fissures": "fissures",
    "钢铁": "steel", "钢铁裂缝": "steel", "steel": "steel", "steelpath": "steel",
    "赏金": "bounty", "悬赏": "bounty", "bounty": "bounty",
    "赏金轮换": "bountyrot", "轮换": "bountyrot", "bountyrot": "bountyrot",
    "信条": "tenet", "tenet": "tenet", "信条武器": "tenet",
    "终幕": "coda", "coda": "coda", "coda武器": "coda", "终幕武器": "coda",
    "加成": "upgrades", "双倍": "upgrades", "upgrades": "upgrades",
    "活动": "events", "event": "events", "events": "events",
    "入侵": "invasions", "invasion": "invasions", "invasions": "invasions",
    "电波": "nightwave", "nightwave": "nightwave", "夜波": "nightwave",
    "虚空商人": "voidtrader", "商人": "voidtrader", "baro": "voidtrader",
    "voidtrader": "voidtrader", "白老头": "voidtrader",
    "特惠": "daily", "daily": "daily", "darvo": "daily",
    "瓦奇娅": "varzia", "varzia": "varzia", "原初": "varzia",
    "信使": "outpost", "outpost": "outpost", "信使前哨": "outpost",
    "集团": "syndicate", "syndicate": "syndicate", "集团任务": "syndicate",
    "建造": "construction", "construction": "construction", "建造进度": "construction",
    "新闻": "news", "news": "news",
    "全部": "__all__", "all": "__all__", "全": "__all__",
}

# ----------------------------- 主推开关指令 -----------------------------
# 群里 / 私聊直接发消息就能改开关，不用改 data/config.txt、不用重启：
#   活动=开启   警报=关闭   alerts=on   events=off
#   活动 开启   关闭 警报   活动=开启，警报=开启（一条消息可写多组）
#   全部=开启 / 主推=开启 / 主推状态
# 「开启」= 该栏目纳入本会话监控 + 设为主推（新条目立即单独推）；
# 「关闭」= 该栏目不再主推，也从周期全量推送里剔除。设置会存盘，重启仍在。
SWITCH_ON_WORDS = {"开启", "开", "打开", "启用", "on", "1", "true", "yes", "enable"}
SWITCH_OFF_WORDS = {"关闭", "关", "关掉", "停用", "禁用", "取消", "off",
                    "0", "false", "no", "disable"}
SWITCH_STATUS_WORDS = {"主推状态", "开关状态", "监控状态", "推送状态",
                       "状态", "status", "switch", "switches"}
SWITCH_ALL_WORDS = {"全部", "所有", "all", "全"}
SWITCH_PRIO_WORDS = {"主推", "推送", "priority", "push"}

# 全角符号归一：＝ / ： / ， / 、 / ； 一律换成半角，手机上打字不必切输入法
_SWITCH_TRANS = str.maketrans({"＝": "=", "：": ":", "，": ",",
                               "、": ",", "；": ";", "；": ";"})
_SWITCH_PAIR = re.compile(r"([^\s=:,;]+)\s*[=:]\s*([^\s=:,;]+)")
_SWITCH_ONLY_SEP = re.compile(r"^[\s,;]*$")


def _switch_value(word):
    w = (word or "").strip().lower()
    if w in SWITCH_ON_WORDS:
        return True
    if w in SWITCH_OFF_WORDS:
        return False
    return None


def _switch_types(word):
    """左词 -> 栏目键 / 特殊标记；不认识返回 None（不吞掉别的指令）。"""
    w = (word or "").strip().lower()
    if w in SWITCH_ALL_WORDS:
        return "__all__"
    if w in SWITCH_PRIO_WORDS:
        return "__priority__"
    k = KEYWORDS.get(w)
    if k == "__all__":
        return "__all__"
    return k


def parse_switch_command(text):
    """解析开关指令，返回 switch / switch_status 命令字典，不是开关指令返回 None。

    解析结果里的 types 可能是：
      - 单个栏目键（alerts / events / ...）
      - "__all__"     展开成全部栏目
      - "__priority__" 展开成当前默认主推栏目（由 ws_monitor 决定）
    """
    t = (text or "").strip().lstrip("/").strip().lower()
    if not t:
        return None
    if t in SWITCH_STATUS_WORDS:
        return {"action": "switch_status"}
    norm = t.translate(_SWITCH_TRANS)
    pairs = _SWITCH_PAIR.findall(norm)
    residue = _SWITCH_PAIR.sub("", norm)
    if pairs:
        if not _SWITCH_ONLY_SEP.match(residue):
            return None                 # 混了别的内容，别把聊天误判成开关
        items = []
        for k, v in pairs:
            on = _switch_value(v)
            types = _switch_types(k)
            if on is None or not types:
                return None
            items.append({"types": types, "on": on})
        return {"action": "switch", "items": items}
    # 没等号时兼容「活动 开启」「关闭 警报」两种自然语言写法
    parts = [p for p in re.split(r"[\s,;]+", norm) if p]
    if len(parts) == 2:
        a, b = parts
        on, types = _switch_value(b), _switch_types(a)
        if on is None or not types:
            on, types = _switch_value(a), _switch_types(b)
        if on is not None and types:
            return {"action": "switch", "items": [{"types": types, "on": on}]}
    return None


def is_switch_command(text):
    return parse_switch_command(text) is not None


HELP_WORDS = {"世界", "世界状态", "worldstate", "ws"}  # 世界状态专项总览（全部命令见「帮助」/「?」）
PLAN_WORDS = {"计划", "plan", "schedule", "到期", "刷新计划"}

# 「停止全部监控」的口语写法：出现在停止指令里就表示清理本会话所有人
STOP_ALL_WORDS = ("所有人", "全部人", "全部成员", "所有成员", "全群", "整群",
                  "全部", "所有", "全", "all")


def _parse_stop(t):
    """解析停止指令：默认只停「自己」，带「全部 / 所有 / 全群」才停本会话所有人。

    只解析语义，真正的清理由 ws_monitor.stop_monitor 按会话键执行，
    绝不影响其它群 / 私聊。
    """
    s = t
    for w in ("停止监控", "取消监控", "结束监控", "unmonitor", "stop", "停止", "取消"):
        if s.startswith(w):
            s = s[len(w):].strip(" /:：,，")
            break
    scope_all = any(w in s for w in STOP_ALL_WORDS)
    for w in sorted(STOP_ALL_WORDS, key=len, reverse=True):
        s = s.replace(w, " ")
    s = s.strip(" /:：,，")
    for w in ("监控", "monitor"):
        if s.startswith(w):
            s = s[len(w):]
            break
    s = s.strip(" /:：,，")
    if not s:
        return {"action": "stop", "scope": "all" if scope_all else "self"}
    sub = _parse_monitor(s)
    if sub.get("action") == "monitor":
        out = {"action": "stop", "scope": "all" if scope_all else "self",
               "types": sub["types"]}
        for k in ("fissure_filter", "bounty_filter"):
            if sub.get(k):
                out[k] = sub[k]
        return out
    return {"action": "stop_hint"}


# ----------------------------- 指令解析 -----------------------------
def parse_ws_command(text):
    """只做解析，不含任何 ctx / 发送逻辑（与 qq_common 解耦）。"""
    t = (text or "").strip().lstrip("/").strip().lower()
    if not t:
        return {"action": "help"}
    sw = parse_switch_command(t)
    if sw:
        return sw
    first = t.split()[0] if t.split() else t
    if first.startswith("监控"):
        return _parse_monitor(t[2:].strip())
    if first.startswith("monitor"):
        return _parse_monitor(t[7:].strip())
    if first.startswith(("停止", "取消", "stop", "unmonitor")):
        return _parse_stop(t)
    if t in HELP_WORDS:
        return {"action": "help"}
    if t in PLAN_WORDS:
        return {"action": "plan"}
    # 显式带「裂缝 / 钢铁裂缝」的查询也按维度过滤（监控与查询通用）。
    # 回廊词里的「钢铁」不是裂缝：先剔掉再判，否则「钢铁回廊」会被当钢铁裂缝。
    fis_t = t
    for w in ("无尽回廊", "钢铁回廊", "灵化武器", "灵化", "回廊", "incarnon", "circuit"):
        fis_t = fis_t.replace(w, "")
    if any(w in fis_t for w in ("裂缝", "裂隙", "fissure", "钢铁裂缝", "钢铁", "steel")):
        spec = parse_fissure_spec(fis_t)
        if spec is not None:
            return {"action": "query", "types": [spec["column"]], "filter": spec["tokens"]}
    # 赏金可按集团 / 地区缩小（连写也行：「赏金Ostron」「赏金坚守者」「赏金 扎里曼」）；
    # 「赏金轮换」是另一个栏目，必须排除，否则会被当成按「轮换」过滤
    if first.startswith(("赏金", "悬赏")) and not first.startswith("赏金轮换"):
        rest = t[len(first[:2]):]
        return {"action": "query", "types": ["bounty"],
                "filter": parse_bounty_tokens(rest)}
    key = KEYWORDS.get(first)
    if not key and t in KEYWORDS:
        key = KEYWORDS[t]
    if not key:
        return {"action": "help"}
    if key == "__all__":
        return {"action": "query", "types": list(TYPES.keys())}
    return {"action": "query", "types": [key]}


def _parse_monitor(rest):
    low = rest.lower()
    # 显式「全部 / all / 全」才全量监控；裸「监控」或不认识的内容都给提示，绝不静默全开
    if low in ("全部", "all", "全"):
        return {"action": "monitor", "types": list(TYPES.keys())}
    if low == "":
        return {"action": "monitor_hint"}
    # 其它（非裂缝）栏目关键词：与显式「裂缝」词可同时出现（如「监控 裂缝后纪 警报」）
    other_cols = set()
    for kw, k in KEYWORDS.items():
        if k in ("fissures", "steel", "__all__"):
            continue
        if kw in low:
            other_cols.add(k)
    # 赏金可按集团 / 地区缩小：「监控 赏金 Ostron」「监控 赏金坚守者」
    bounty_toks = []
    if ("赏金" in low or "悬赏" in low) and "赏金轮换" not in low:
        rest = low.replace("悬赏", "").replace("赏金", "")
        for kw, k in KEYWORDS.items():          # 其它栏目词是并列栏目，不是赏金条件
            if k in ("bounty", "bountyrot", "__all__"):
                continue
            if kw in rest:
                rest = rest.replace(kw, "")
        bounty_toks = parse_bounty_tokens(rest)
    # 回廊（无尽回廊 / 钢铁回廊 / 灵化）不是裂缝：先剔掉这些词再判裂缝，
    # 否则「监控 钢铁回廊」里的「钢铁」会被当成钢铁裂缝，多开一个栏目。
    fis_low = low
    for w in ("无尽回廊", "钢铁回廊", "灵化武器", "灵化", "回廊", "incarnon", "circuit"):
        fis_low = fis_low.replace(w, "")
    # 只有在「没有其它栏目」或「显式写了裂缝/钢铁裂缝」时才解析裂缝过滤，
    # 否则「监控 平原」里的杂词会被误当成裂缝条件。
    explicit_fis = any(w in fis_low for w in ("裂缝", "裂隙", "fissure", "steel"))
    spec = parse_fissure_spec(fis_low) if (explicit_fis or not other_cols) else None
    # 没写「裂缝/钢铁裂缝」又带无法识别的词 → 当不认识，绝不默默开一个永远空的过滤监控
    if spec is not None and not explicit_fis and spec.get("residual"):
        spec = None
    if spec is not None and not other_cols:
        out = {"action": "monitor", "types": [spec["column"]]}
        if spec["tokens"]:
            out["fissure_filter"] = {spec["column"]: spec["tokens"]}
        return out
    types = set()
    used = []
    # 显式写了裂缝时，地名已被当成裂缝过滤条件，不再额外开对应栏目
    # （「监控 裂缝扎里曼中断」只要裂缝，不要外加扎里曼赏金）
    loc_words = {loc.lower() for loc in FISSURE_LOCATION_TOKENS}
    drop_loc = spec is not None and explicit_fis
    # 长的先匹配：「赏金轮换」不会再被「赏金」重复命中一次
    for kw, k in sorted(KEYWORDS.items(), key=lambda kv: -len(kv[0])):
        if k == "__all__" or k in ("fissures", "steel"):
            continue
        if drop_loc and any(kw in loc for loc in loc_words):
            continue
        if kw not in low:
            continue
        if any(kw != u and kw in u for u in used):
            continue
        used.append(kw)
        types.add(k)
    if spec is not None:
        types.add(spec["column"])
    if not types:
        return {"action": "monitor_hint"}
    # 赏金只按集团 / 地区过滤；写了不属于这儿的词（如「赏金 火星」）按不认识处理，
    # 绝不默默开一个永远推不出东西的过滤监控
    if bounty_toks and not all(t in BOUNTY_KNOWN for t in bounty_toks):
        return {"action": "monitor_hint"}
    out = {"action": "monitor", "types": sorted(types)}
    if spec is not None and spec["tokens"]:
        out["fissure_filter"] = {spec["column"]: spec["tokens"]}
    if bounty_toks:
        out["bounty_filter"] = bounty_toks
    return out


HELP_TEXT = (
    "🌐 世界状态查询（星际战甲国际服，一次全量取回，按结束时间排序更新）：\n"
    "  平原：平原（五处时间一起给，各处附自己的赏金）/ 希图斯 / 奥布山谷 / 魔胎之境 / 双衍 / 扎里曼\n"
    "  战斗：裂缝（常规星球在上、九重天在下，各按 古→前→中→后→安魂→全能 排序）\n"
    "        钢铁裂缝 / 突击 / 仲裁 / 入侵 / 警报 / 活动\n"
    "        回廊（本周普通回廊三选一战甲 + 钢铁回廊五把灵化武器，每周一 00:00 UTC 换；\n"
    "             它就在双衍王境里，查「双衍王境」也会一起给）\n"
    "  轮换：商人 / 瓦奇娅 / 特惠 / 电波 / 加成\n"
    "  赏金：赏金（六个集团一次给全，每档带奖励）/ 赏金轮换（本轮 + 宝库奖励）\n"
    "        单个集团：赏金 Ostron / 赏金 坚守者 / 赏金 扎里曼（也可 监控 赏金 Ostron）\n"
    "        六集团：Ostron · 索拉里斯 · 英择谛 · 坚守者(扎里曼) · 科维兽(火卫二) · 六人组(1999)\n"
    "  装备：信条 / 终幕\n"
    "  其它：新闻 / 全部\n"
    "  监控 <词>     在本会话开启推送（如 监控 警报 / 监控 裂缝 / 监控 平原）\n"
    "                  ⚠️ 裸「监控」或「监控 不认识的词」不会全开，会提示你怎样用；\n"
    "                     要监控全部栏目，必须发完整的「监控全部」。\n"
    "  裂缝可按维度缩小：纪（古/前/中/后/安魂/全能）+ 星球（火星…）+ 任务类型（中断…）\n"
    "                     例：监控 裂缝 后纪   监控 裂缝 火星 中断   监控 钢铁裂缝 古纪\n"
    "                     多条件 AND；连写也行（后纪中断 = 后纪且中断）。\n"
    "                     ⚠️ 裂缝=普通裂缝，钢铁裂缝=钢铁之路，两栏分开算：\n"
    "                        「监控 裂缝 后纪」只看普通；「监控 钢铁裂缝 后纪」只看钢铁。\n"
    "  停止监控       只停你自己的推送（群里别人开的、别的群、私聊都不受影响）\n"
    "  停止监控 警报  只停你监控的某个栏目（可写多个：停止监控 警报 裂缝）\n"
    "  停止全部监控   停掉本群所有人的监控 + 清除本群的开关设置（只影响本群）\n"
    "  ★ 发消息直接开关主推，不用改配置、不用重启（设置会存盘，重启后自动恢复）：\n"
    "      活动=开启    警报=开启    活动=关闭    警报=关闭\n"
    "      也支持 alerts=on / events=off / 活动 开启 / 关闭 警报 / 活动=开启，警报=开启\n"
    "      全部=开启（所有栏目）/ 主推=开启（警报+活动）/ 主推状态（看当前开关）\n"
    "    开启 = 自动纳入本会话监控并设为主推；关闭 = 不主推，周期推送里也剔除。\n"
    "  ★ 警报 / 活动默认主推：出现新条目立即单独推一条（不等 5 分钟周期，60 秒防抖）；\n"
    "    刚开监控只记现状、不补发历史；其余栏目内容变了才推（倒计时不算变化）。\n"
    "  计划           查看按最快结束排好的队列与下次更新时间\n"
    "  回复形式：默认一律发白底卡片图；要复制文字就在指令前加「文字」两个字，\n"
    "        例如「文字 D/XX/裂缝」「文字 D/XX/WM Boltor Prime」。\n"
    "  ⚠️ 所有交互指令在群聊都需带 D/XX/ 前缀，避免群友闲聊误触发；例如：\n"
    "        D/XX/平原  D/XX/警报  D/XX/裂缝  D/XX/计划  D/XX/监控警报  D/XX/停止全部监控\n"
    "        （@机器人 也能直接用，此时不用打 D/XX/，回复和监控推送都会 @ 你）\n"
    "        ★ 例外：直接发「帮助」两个字就出这份菜单，不用加前缀。\n"
    "        只有「机器人主动推送」的外发监控消息不受此限制（它本来就不是用户指令）。\n"
    "  说明：一次全量请求取回全部数据，把【每一条】的结束时刻排序交给唯一主任务；\n"
    "        睡到最快结束那条才查【一次】，查完重排，没到点零请求；\n"
    "        两次请求间隔按 10/20/50 秒节拍兜底，且每次都守同域名 3~5 秒底线；\n"
    "        仲裁来自 arbi.wf.wiki 的轮换表（本地推算，7 天同步一次，平时零请求）；\n"
    "        突击与回廊来自 DE 官方 api.warframe.com（回廊一周才变，未过期复用缓存）；\n"
    "        查询与监控推送都只读缓存，不额外打接口。\n"
    "        监控推送分两路：警报/活动新条目立即主推；其余栏目内容变化才周期推。\n"
    "        两手准备：能主动发就发；官方主动消息未开通时会攒着，\n"
    "        等本会话有人发消息时随回复一起带出（被动窗口必达）。"
)


# ----------------------------- 对外查询入口 -----------------------------
def _ordered_types(types):
    """含「平原(cycles)」时跳过三个单独平原项，避免重复。"""
    types = list(types)
    if "cycles" in types:
        for sub in CYCLE_UNITS:
            if sub in types:
                types.remove(sub)
    return types


def describe_plan():
    """按最快结束排序后的到期队列（纯读缓存，零请求）。"""
    c = get_cache()
    if not c.get("data"):
        # 尚未成功抓取过：如实反映后台主任务状态，避免「看起来像坏了」
        if _refresh_running:
            if _failures:
                if "403" in _last_fetch_error:
                    return (f"（尚未抓到世界状态数据：后台主任务已失败 {_failures} 次，"
                            f"接口被 Cloudflare 拦截（HTTP 403），程序化请求被拒。\n"
                            f"这不是超时、重试无效：请在 data/config.txt 设 "
                            f"WM_WS_API_BASE 指向可用镜像/自建源根地址"
                            f"（程序自动拼 /pc/zh）。）")
                return (f"（尚未抓到世界状态数据：后台主任务已失败 {_failures} 次、"
                        f"正在持续重试。\n"
                        f"数据源 api.warframestat.us 在境外，常超时或被限流；"
                        f"本机网络能直连后会立刻自动恢复，无需手动操作。）")
            return "（首次抓取进行中，正在请求 api.warframestat.us，请稍候…）"
        return "（队列为空，尚未抓取过世界状态）"
    q = c.get("queue") or []
    gaps = "/".join(f"{g / 60:g}分" if g >= 60 else f"{g:g}秒"
                    for g in WS_STEP_GAPS)
    lines = [
        f"抓取时刻：{_fmt_when(c.get('fetched_at', 0))}",
        "主任务：一个后台循环 / 一次全量请求 / 一条按结束时刻排序的队列",
        f"节拍：{gaps}循环（任意两次请求的最小间隔，与群数无关）；"
        f"同域名底线 3~5 秒（硬规则）",
        f"下次请求：{_fmt_when(next_due())}",
        "",
        f"排序后的到期队列（共 {len(q)} 个到期点，裂缝/警报列表里每条单独排）：",
    ]
    now = time.time()
    for i, item in enumerate(q[:15], 1):
        lines.append(f"  {i:2d}. {item['label']}：{_fmt_when(item['due'])}"
                     f"（{_human(item['due'] - now)} 后）")
    if len(q) > 15:
        lines.append(f"  …… 还有 {len(q) - 15} 个到期点")
    return "\n".join(lines)


def handle_plan():
    return ("🗓️ 世界状态统一调度队列（按最快结束排序，"
            "到点只查一次全量）\n" + describe_plan())


def _blocks_for(types, state, filters=None):
    """按类型列表拼回复块。「全部」这类一次性给十几个栏目时自动走精简模式，
    避免一条消息长到超出 QQ 单条上限（要看全的还是发单条指令）。

    filters：{栏目键: 过滤 token 列表}，仅对命中的栏目应用过滤（用于裂缝维度过滤）。
    """
    filters = filters or {}
    global _COMPACT
    _COMPACT = len(types) > 12
    try:
        blocks = []
        for t in types:
            if t not in TYPES:
                continue
            toks = filters.get(t)
            blocks.append(TYPES[t][1](state, toks) if toks else TYPES[t][1](state))
        if _COMPACT:
            # 当前数据源确实没有的栏目（如 wf.wiki 没有信使/集团/建造）在「全部」里
            # 直接省掉，别用三行「不提供此项」占位；单条查询时仍然如实提示。
            blocks = [b for b in blocks if "数据源不提供此项" not in b]
        return blocks
    finally:
        _COMPACT = False


def handle_query(cmd):
    """被动查询：直接读缓存秒回；只有【完全没有数据】时才查一次全量。"""
    if cmd.get("action") == "plan":
        return handle_plan()
    activity_touch()
    # 先把首次数据补齐再拉起后台主任务，避免两边同时发现「缓存为空」各发一次全量请求
    if get_state() is None:
        _run_once()
    ensure_active()
    state = get_state()
    if state is None:
        if "403" in _last_fetch_error:
            return ("⚠️ 世界状态接口被 Cloudflare 拦截（HTTP 403），程序化请求被拒。\n"
                    "这不是超时、重试无效：请在 data/config.txt 末尾加一行\n"
                    "  WM_WS_API_BASE = https://可用镜像或自建源的根地址\n"
                    "（程序会自动拼 /pc/zh），或改用能绕过拦截的网络后再试。")
        return "⚠️ 暂时拿不到世界状态数据（接口超时或被限流），请稍后重试。"
    types = _ordered_types(cmd.get("types") or list(TYPES.keys()))
    flt = cmd.get("filter")
    filters = {types[0]: flt} if (flt and types) else {}
    return "\n\n".join(_blocks_for(types, state, filters))


def format_for(types):
    """供监控推送调用：拿当前缓存（不阻塞刷新）格式化。"""
    state = get_state()
    if state is None:
        return ""
    return "\n\n".join(_blocks_for(_ordered_types(types), state))


# ----------------------------- 离线样例（测试用 mock） -----------------------------
_MOCK_WS = {
    "alerts": [
        {"mission": {"node": "火星/Olympus", "type": "歼灭", "faction": "Grineer",
                     "minEnemyLevel": 15, "maxEnemyLevel": 17},
         "reward": {"items": ["Neo N12 遗物"], "credits": 6500},
         "expiry": int(time.time() + 3600)},
        {"mission": {"node": "金星/Venera", "type": "捕获", "faction": "Corpus",
                     "minEnemyLevel": 12, "maxEnemyLevel": 14},
         "reward": {"items": ["Meso F3 遗物"], "credits": 4200},
         "expiry": int(time.time() + 1800)},
    ],
    "cetusCycle": {"isDay": True, "timeLeft": "01:23:45",
                   "expiry": int(time.time() + 5025)},
    "vallisCycle": {"isWarm": False, "timeLeft": "00:42:10",
                    "expiry": int(time.time() + 2530)},
    "cambionCycle": {"isFass": True, "timeLeft": "00:08:00",
                     "expiry": int(time.time() + 480)},
    "duviriCycle": {"state": "愤怒", "stateKey": "anger",
                    "expiry": int(time.time() + 3600)},
    "zarimanCycle": {"state": "Corpus", "stateKey": "corpus",
                     "expiry": int(time.time() + 5400)},
    "circuit": {"activation": int(time.time() - 86400),
                "expiry": int(time.time() + 5 * 86400),
                "normal": ["Ivara", "Inaros", "Titania"],
                "hard": ["Braton", "Lato", "Skana", "Paris", "Kunai"]},
    "sortie": {"boss": "屠夫", "expiry": int(time.time() + 86400),
               "variants": [
                   {"node": "赛德娜/Hydron", "modifier": "神盾枪兵",
                    "modifierDescription": "敌人手持神盾"},
                   {"node": "冥王星/Pluto", "modifier": "虚空之体"},
               ]},
    "arbitration": {"node": "土星/Kronus", "type": "防御", "enemy": "Corrupted",
                    "level": "30-40", "key": "SolNode1",
                    "start": int(time.time()) - 1800,
                    "expiry": int(time.time()) + 1800},
    "fissures": [
        {"node": "地球/Vault", "missionType": "拦截", "enemy": "Grineer",
         "tier": "Lith", "expiry": int(time.time() + 7200)},
        {"node": "火星/Tharsis", "missionType": "生存", "enemy": "Corpus",
         "tier": "Neo", "expiry": int(time.time() + 5400)},
    ],
    "steelPath": {"current": [
        {"node": "冥王星/Cerberus", "missionType": "突击", "enemy": "Corpus",
         "tier": "Axi", "expiry": int(time.time() + 3600)},
        {"node": "地球/ Everest", "missionType": "防御", "enemy": "Grineer",
         "tier": "Meso", "expiry": int(time.time() + 900)},
    ]},
    "events": [
        {"description": "orphys 的赠礼：双倍声望活动", "expiry": int(time.time() + 172800)},
    ],
    "invasions": [
        {"node": "谷神星/Caelus", "progress": "50%",
         "attackerReward": {"items": ["控管局认证"]},
         "defenderReward": {"items": ["中毒突击卡"]}},
    ],
    "nightwave": {"season": "间歇性 S14", "expiry": int(time.time() + 432000),
                  "challenges": [
                      {"title": "完成 3 次突击", "reputation": 5000},
                      {"title": "击杀 25 个敌人", "reputation": 1000}]},
    # active=True 时 expiry 是离场；active=False 时 activation 是下次抵达
    "voidTrader": {"active": True, "location": "神殿中继站",
                   "activation": int(time.time() - 86400),
                   "expiry": int(time.time() + 86400 * 2),
                   "inventory": [
                       {"item": " Prime 追击者之钥", "ducats": 325, "credits": 35000}]},
    "dailyDeals": [
        {"item": "幽灵猫宠物蛋", "originalPrice": 100, "salePrice": 50, "discount": 50},
    ],
    "globalUpgrades": [{"desc": "经验 2 倍", "expiry": int(time.time() + 2 * 86400)}],
    "tenetCoda": {
        "expiry": int(time.time() + 3600 * 5),
        "tenet": [{"name": "信条·铁晶磁轨炮", "element": "火焰", "bonus": 26.3},
                  {"name": "信条·莉薇娅", "element": "火焰", "bonus": 26.3}],
        "codaBatch": "B",
        "codaExpiry": int(time.time() + 86400),
        "coda": [{"name": "终幕·低音爆囊", "element": "磁力", "bonus": 33.1}],
    },
    "bountyBoard": [
        {"name": "Ostron", "tag": "CetusSyndicate", "expiry": int(time.time() + 5400),
         "jobs": [{"objective": "削减", "minLevel": 5, "maxLevel": 15, "stages": 3,
                   "isVault": False,
                   "rewards": [{"name": "奥席金属", "count": 100},
                               {"name": "内融核心", "count": 1},
                               {"name": "Gara 机体 蓝图", "count": 1}]},
                  {"objective": "捕获", "minLevel": 10, "maxLevel": 20, "stages": 2,
                   "rewards": [{"name": "夜灵之息", "count": 5}]}]},
        {"name": "索拉里斯", "tag": "SolarisSyndicate", "expiry": int(time.time() + 5400),
         "jobs": [{"objective": "挖掘", "minLevel": 15, "maxLevel": 25, "stages": 3,
                   "rewards": [{"name": "心肌菌孢子", "count": 5}]}]},
    ],
    "bounties": {
        "expiry": int(time.time() + 5400), "rot": "C",
        "rotRewards": ["Caliban 头部神经光元", "雷霆暴君"],
        "vaultRot": "B", "vaultRewards": ["孢丝感染枪 枪机"],
        "groups": [{"name": "坚守者", "jobs": [
            {"node": "奥金工场", "mission": "虚空决战", "challenge": "击杀 2 名虚空天使",
             "ally": ""}]},
            {"name": "六人组", "jobs": [
                {"node": "传承种收割", "mission": "传承种收割",
                 "challenge": "从上方击杀 10 名敌人", "ally": "阿米尔"}]}],
    },
    "varzia": {"active": True, "node": "Maroo 的市集",
               "activation": int(time.time()) - 86400,
               "expiry": int(time.time() + 86400 * 7),
               "inventory": [{"item": "Nidus Prime 组合包", "regalAya": 6},
                             {"item": "Saryn Prime 组合包", "regalAya": 6}]},
    "news": [{"title": "开发者直播 198 总结", "link": "https://www.warframe.com/"},
             {"title": "「绝对是 Lotus」T恤现已推出", "link": "https://www.warframe.com/"}],
    "sentientOutpost": {"active": True, "location": "海王星/Lua",
                        "expiry": int(time.time() + 3600)},
    "syndicateMissions": [
        {"syndicate": "Steel Meridian", "nodes": ["火星/Ares"]},
    ],
    "constructionProgress": {"fomorian": {"pct": "42%"}},
}


if __name__ == "__main__":
    # 命令行快速自检：python worldstate.py [类型|全部|计划|帮助]
    import sys
    import tempfile
    from pathlib import Path
    arg = (sys.argv[1] if len(sys.argv) > 1 else "帮助")
    # 自检走离线样例、跳过节流等待（运行态仍严格守 3~5 秒），
    # 且缓存落到临时文件，绝不覆盖运行态 data/worldstate.json（否则会把假数据留给机器人）。
    WS_SOURCE = "mock"
    WS_PACE_MIN, WS_PACE_MAX = 0.0, 0.0
    WS_CACHE_FILE = Path(tempfile.gettempdir()) / "wm-ws-cli-selftest.json"
    cmd = parse_ws_command(arg)
    if cmd["action"] == "plan":
        refresh_once()
        print(handle_plan())
    elif cmd["action"] == "query":
        refresh_once()
        state = get_state()
        types = _ordered_types(cmd.get("types") or list(TYPES.keys()))
        print("\n\n".join(TYPES[t][1](state) for t in types if t in TYPES))
    else:
        print(HELP_TEXT)
