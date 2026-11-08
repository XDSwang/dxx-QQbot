#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周轮换：科研 / 执刑官猎杀 / 沉沦之地 / 钢铁商店 / 回廊预告 / 1999 日历。

数据源 todo.wf.wiki/weekly（第三方中文站，与项目已在用的 wf.wiki 同一家）：
  · 页面是 Next.js 服务端渲染，HTML 里内嵌 React Server Components 的 flight 数据，
    其中 `initialData` 就是整页 JSON（约 22KB）。所以不抓 DOM、不依赖 CSS 类名，
    站点改版只要还走 initialData 就不受影响。
  · 一次请求拿全七个板块，而且已经是中文，省掉自己拼译名。

为什么需要它：DE 官方 worldState.php 没有科研（Deep Archimedea）与执刑官猎杀，
这两块是玩家最常问、又只能靠人工整理的部分。

缓存（规范 1.9）：
  · 落盘 data/weekly.json，全世界一份，所有群 / 用户共用，请求数不随群数增长；
  · 新鲜度 WS_WEEKLY_MINUTES，默认 30 分钟（每周换榜一次，半小时足够）；
  · 查询只读缓存；刷新由 ensure_async 的后台线程做；
  · 刷新失败沿用旧数据，但回复里标明「数据时间」，不假装是新数据。

指令：
  · 每周 / 周报          本周七项总览
  · 科研                深层科研 + 时光科研（关卡、偏差、变量）
  · 执刑官              执刑官猎杀（Boss、源力石、三节点）
  · 沉沦之地            本周沉沦之地楼层与挑战
  · 钢铁商店            Teshin 荣誉商店 8 周循环
  · 回廊预告            普通 / 钢铁回廊本周与下周
  · 日历                1999 日历当季挑战与增益
"""
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import wm

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"

VERSION = 1

WEEKLY_URL = (os.environ.get("WM_WEEKLY_URL")
              or "https://todo.wf.wiki/weekly")
DOMAIN = "todo.wf.wiki"

UA = "wm-bot-python/0.1 (+Warframe weekly rotation bot)"

# 指令词 -> 板块。顺序有意义：长词在前，避免「沉沦」抢在「沉沦之地」前面。
HEADS = (
    ("每周", "all"), ("周报", "all"), ("weekly", "all"),
    ("深层科研", "research"), ("时光科研", "research"),
    ("科研", "research"), ("archimedea", "research"),
    ("执刑官", "archon"), ("archon", "archon"),
    ("沉沦之地", "descent"), ("沉沦", "descent"), ("descent", "descent"),
    ("钢铁商店", "teshin"), ("teshin", "teshin"), ("商店", "teshin"),
    ("回廊预告", "circuit"), ("回廊轮换", "circuit"),
    ("1999日历", "calendar"), ("1999", "calendar"),
    ("日历", "calendar"), ("hex", "calendar"),
)

# 日历最多显示多少天的条目（15 天全展开会把卡片撑到三四页）
CAL_DAYS = 10

# 帮助菜单里的一段（由 qq_common 的「全部指令」拼进去，避免两处各写一套）
HELP_TEXT = (
    "  每周：每周 / 科研 / 执刑官 / 沉沦之地 / 钢铁商店 / 回廊预告 / 日历\n"
    "        · 科研      深层科研 + 时光科研（关卡 · 派系 · 偏差与变量说明）\n"
    "        · 执刑官    执刑官猎杀（Boss · 源力石 · 三个节点）\n"
    "        · 沉沦之地  本周楼层与全部挑战\n"
    "        · 钢铁商店  Teshin 荣誉商店 8 周循环\n"
    "        · 回廊预告  普通 / 钢铁回廊本周与下周\n"
    "        · 日历      1999 日历当季挑战与增益\n"
    "        （数据来自社区中文站 todo.wf.wiki，缓存 30 分钟，不随群数增加请求）\n"
)

_CACHE = [None, 0.0]          # [数据, 载入时刻]：同进程 60 秒内不重复读盘
_LOAD_LOCK = threading.Lock()
_BUILDING = [False]


class WeeklyUnavailable(RuntimeError):
    """既没有本地缓存、又抓不到数据时抛出（提示语在 message 里）。"""


def _log(msg):
    if getattr(wm, "LOG_HOOK", None):
        try:
            wm.LOG_HOOK("WEEKLY " + str(msg))
        except Exception:
            pass


def _env_num(name, default, lo=1.0):
    try:
        v = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return v if v >= lo else default


def cache_file():
    """缓存文件路径（测试可用 WS_WEEKLY_FILE 指到临时目录）。"""
    p = os.environ.get("WS_WEEKLY_FILE")
    return Path(p) if p else (DATA_DIR / "weekly.json")


def ttl_seconds():
    """新鲜度：默认 30 分钟（本周榜单不会半小时就变）。"""
    return _env_num("WS_WEEKLY_MINUTES", 30.0) * 60.0


# ------------------------------- HTTP -------------------------------

def _gate():
    """同域名 3~5 秒节流（与 WM / 世界状态共用同一份跨进程节流记录）。"""
    try:
        wm.pace_gate_ex(DOMAIN, 3.0, 5.0)
    except Exception:
        time.sleep(3.0)


def _http_text(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ------------------------------- 解析 -------------------------------

def _unescape_flight(s):
    """把 flight 字符串里的转义还原成真 JSON 文本。

    RSC 的 push 载荷是「JS 字符串里套 JSON」，引号写成 \\"，反斜杠写成 \\\\。
    直接链式 replace 会被 \\\\" 这种组合坑到，所以用占位符把反斜杠先摘出来。
    """
    return (s.replace("\\\\", "\x00")
             .replace('\\"', '"')
             .replace("\\n", "\n")
             .replace("\\t", "\t")
             .replace("\x00", "\\"))


def extract_initial_data(html):
    """从页面 HTML 里取出 initialData 那份 JSON（取不到返回 None）。"""
    mark = '\\"initialData\\"'
    i = html.find(mark)
    if i >= 0:
        text = _unescape_flight(html[i:])
    else:
        # 万一哪天改成不转义内联，也认一下
        i = html.find('"initialData"')
        if i < 0:
            return None
        text = html[i:]
    j = text.find("{", text.find("initialData"))
    if j < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[j:])
    except ValueError as e:
        _log("initialData 解析失败：%s" % e)
        return None
    return obj if isinstance(obj, dict) else None


def build():
    """抓一次页面并解析，返回待落盘的结构。"""
    last = None
    for attempt in range(3):
        try:
            _gate()
            html = _http_text(WEEKLY_URL)
            data = extract_initial_data(html)
            if not data:
                raise WeeklyUnavailable("页面里没找到 initialData（站点可能改版）")
            return {"version": VERSION, "built_at": int(time.time()), "data": data}
        except Exception as e:
            last = e
            _log("抓取第 %d 次失败：%s" % (attempt + 1, e))
            if attempt < 2:
                time.sleep(3.0)
    raise WeeklyUnavailable("抓取 %s 失败：%s" % (WEEKLY_URL, last))


def _read_cache():
    p = cache_file()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _log("缓存读取失败（当作没有缓存）：%s" % e)
        return None
    if not isinstance(d, dict) or d.get("version") != VERSION:
        _log("缓存版本过旧，重抓一次")
        return None
    return d


def _write_cache(d):
    p = cache_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        _log("缓存落盘失败（本次仍可用）：%s" % e)


def load(force=False):
    """取数据：内存 -> 本地缓存（未过期）-> 重抓；重抓失败沿用旧数据并标记 stale。"""
    now = time.time()
    with _LOAD_LOCK:
        if not force and _CACHE[0] is not None and now - _CACHE[1] < 60:
            return _CACHE[0]
        old = _read_cache()
        if old is not None and not force:
            if now - float(old.get("built_at") or 0) < ttl_seconds():
                _CACHE[0], _CACHE[1] = old, now
                return old
        try:
            fresh = build()
        except Exception as e:
            if old is not None:
                old["stale"] = True
                _CACHE[0], _CACHE[1] = old, now
                return old
            raise WeeklyUnavailable(str(e))
        _write_cache(fresh)
        _CACHE[0], _CACHE[1] = fresh, now
        return fresh


def ensure_async():
    """启动时后台预抓一次，让第一次查询就能秒回。"""
    def run():
        if _BUILDING[0]:
            return
        _BUILDING[0] = True
        try:
            load()
        except Exception as e:
            _log("后台预抓失败（查询时会再试）：%s" % e)
        finally:
            _BUILDING[0] = False

    t = threading.Thread(target=run, name="weekly-prefetch")
    t.daemon = True
    t.start()
    return t


def has_cache():
    """本地是否已有可用缓存（只读盘，绝不发请求）——给世界状态卡片做可选补充行。"""
    try:
        return _read_cache() is not None
    except Exception:
        return False


def short_hint():
    """世界状态卡片用的一行科研提示：有缓存才给，没有就返回空串（不为这行发请求）。"""
    try:
        d = _read_cache()
    except Exception:
        return ""
    groups = ((d or {}).get("data") or {}).get("archimedea") or []
    if not groups:
        return ""
    parts = ["%s %d 关" % (g.get("type") or "科研", len(g.get("missions") or []))
             for g in groups]
    return "    科研：" + " · ".join(parts) + "（发「科研」看偏差与变量）"


# ------------------------------- 渲染小工具 -------------------------------

def _human(sec):
    sec = int(sec)
    if sec <= 0:
        return "已结束"
    d, h, m, s = sec // 86400, (sec % 86400) // 3600, (sec % 3600) // 60, sec % 60
    if d:
        return "%d天%d小时%d分" % (d, h, m)
    if h:
        return "%d小时%d分" % (h, m)
    if m:
        return "%d分%d秒" % (m, s)
    return "%d秒" % s


def _left(ms):
    """毫秒时间戳 -> 「 · 剩余 19小时37分」；已过期如实写，不写「剩余 已结束」。"""
    try:
        e = float(ms) / 1000.0
    except (TypeError, ValueError):
        return ""
    if e <= 0:
        return ""
    left = e - time.time()
    if left <= 0:
        return " · 刚刚结束（等刷新）"
    return " · 剩余 " + _human(left)


def _dw(s):
    """显示宽度：中文 / 全角标点占 2，其余占 1（卡片是等宽网格排版）。"""
    return sum(2 if ("\u4e00" <= ch <= "\u9fff" or ch in "（）·，、：；！？") else 1
               for ch in (s or ""))


def _wrap(text, width=44, indent="      "):
    """按显示宽度折行，续行用 indent 对齐（避免卡片里换行后错位）。"""
    out, line = [], ""
    for ch in text:
        if line and _dw(line) + _dw(ch) > width:
            out.append(line.rstrip())
            line = indent + ch
        else:
            line += ch
    if line.strip():
        out.append(line.rstrip())
    return out


def _short(text, limit=30):
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…"


def _note_lines(tag, name, desc):
    """一条修正：「偏差 密闭装甲：说明…」，一行放得下就一行，放不下就另起一行说明。

    换行后的第一行也要带缩进，否则续行会顶到左边框（卡片按等宽网格排版，错位很明显）。
    """
    head = "    %s %s：" % (tag, name or "?")
    body = head + (desc or "")
    if _dw(body) <= 44:
        return [body]
    if not desc:
        return [head.rstrip("：")]
    return [head] + _wrap("      " + desc, 44, "      ")


def _stale_note(d):
    if not d.get("stale"):
        return ""
    age = max(0, int(time.time() - float(d.get("built_at") or 0)))
    if age >= 3600:
        when = "%d 小时前" % (age // 3600)
    else:
        when = "%d 分钟前" % max(1, age // 60)
    return "\n（⚠️ 本周数据这次没刷新成功，用的是 %s 的缓存）" % when


# ------------------------------- 各板块 -------------------------------

def _fmt_research(data):
    groups = data.get("archimedea") or []
    if not groups:
        return "🧪 科研：（本周没有数据）"
    out = []
    for g in groups:
        missions = g.get("missions") or []
        out.append("🧪 %s（%d 关%s）" % (g.get("type") or "科研", len(missions),
                                        _left(g.get("expiry"))))
        for i, m in enumerate(missions, 1):
            out.append("  %02d %s · %s" % (i, m.get("mission") or "?",
                                           m.get("faction") or "?"))
            for mod in (m.get("deviations") or []):
                out += _note_lines("偏差", mod.get("name"), mod.get("desc"))
            for mod in (m.get("risks") or []):
                out += _note_lines("变量", mod.get("name"), mod.get("desc"))
        out.append("")
    out.append("发「执刑官」「沉沦之地」看本周其它轮换。")
    return "\n".join(out).rstrip()


def _fmt_archon(data):
    a = data.get("archonHunt") or {}
    if not a:
        return "🟣 执刑官猎杀：（本周没有数据）"
    out = ["🟣 执刑官猎杀" + _left(a.get("expiry"))]
    if a.get("boss"):
        out.append("  Boss：%s" % a["boss"])
    if a.get("shard"):
        out.append("  源力石：%s" % a["shard"])
    for i, m in enumerate(a.get("missions") or [], 1):
        out.append("  %d. %s · %s · %s · %s"
                   % (i, m.get("node") or "?", m.get("system") or "?",
                      m.get("faction") or "?", m.get("mission") or "?"))
    return "\n".join(out)


def _fmt_descent(data):
    items = data.get("descents") or []
    if not items:
        return "🕳️ 沉沦之地：（本周没有数据）"
    out = []
    for d in items:
        ch = d.get("challenges") or []
        out.append("🕳️ 沉沦之地 · %s（%d 项%s）"
                   % (d.get("name") or "?", len(ch), _left(d.get("expiry"))))
        for c in ch:
            out += _wrap("  %02d %s" % (c.get("index") or 0, _descent_step(c)),
                         44, "      ")
    return "\n".join(out)


def _descent_step(c):
    """一关的说明：普通关是「模式 · 挑战名」，守关节点只有 npc（玛丽的圣所这种）。"""
    mode = (c.get("mode") or "").strip()
    challenge = (c.get("challenge") or "").strip()
    npc = (c.get("npc") or "").strip()
    if mode and challenge:
        return "%s · %s" % (mode, challenge)
    if mode:
        return mode
    if challenge:
        return challenge
    if npc:
        return "守关 · %s" % npc
    return "（未公布）"


def _fmt_teshin(data):
    t = data.get("teshin") or {}
    if not t:
        return "🏅 钢铁商店：（本周没有数据）"
    out = ["🏅 钢铁商店 · 8 周循环" + _left(t.get("expiry")),
           "  本周：%s" % (t.get("offer") or "?")]
    weeks = t.get("weeks") or []
    if weeks:
        for i, w in enumerate(weeks, 1):
            tag = "本周" if i == 1 else ("下周" if i == 2 else "第 %d 周" % i)
            out.append("    %d. %s（%s）" % (i, w.get("offer") or "?", tag))
    return "\n".join(out)


def _fmt_circuit(data):
    c = data.get("duviriCircuit") or {}
    if not c:
        return "🌀 回廊轮换：（本周没有数据）"
    out = ["🌀 回廊轮换" + _left(c.get("expiry"))]
    nrm = [x.get("name") for x in (c.get("normal") or []) if x.get("name")]
    hard = [x.get("name") for x in (c.get("hard") or []) if x.get("name")]
    if nrm:
        out.append("  普通（战甲三选一）：" + "、".join(nrm))
    if hard:
        out += _wrap("  钢铁（灵化武器五选二）：" + "、".join(hard), 44, "    ")
    nw = (c.get("normalWeeks") or [])
    hw = (c.get("hardWeeks") or [])
    if len(nw) > 1:
        nxt = [x.get("name") for x in (nw[1].get("items") or []) if x.get("name")]
        if nxt:
            out.append("  下周普通战甲：" + "、".join(nxt))
    if len(hw) > 1:
        nxt = [x.get("name") for x in (hw[1].get("items") or []) if x.get("name")]
        if nxt:
            out += _wrap("  下周钢铁武器：" + "、".join(nxt), 44, "    ")
    return "\n".join(out)


_KIND_CN = {"challenge": "挑战", "buff": "增益", "upgrade": "增益", "reward": "奖励"}


def _fmt_calendar(data):
    c = data.get("calendar") or {}
    days = c.get("days") or []
    if not days:
        return "📅 1999 日历：（本季没有数据）"
    out = ["📅 1999 日历 · %s（%d 天%s）"
           % (c.get("season") or "?", len(days), _left(c.get("expiry")))]
    shown = 0
    for d in days:
        if shown >= CAL_DAYS:
            break
        for ev in (d.get("events") or []):
            kind = _KIND_CN.get(ev.get("kind"), ev.get("kind") or "?")
            desc = _short(ev.get("desc"), 24)
            line = "  第 %s 天 · %s %s" % (d.get("day"), kind, ev.get("name") or "?")
            if desc:
                line += "：" + desc
            out += _wrap(line, 44, "      ")
            shown += 1
    rest = sum(len(d.get("events") or []) for d in days) - shown
    if rest > 0:
        out.append("  …… 还有 %d 项（这里只列前 %d 项）" % (rest, CAL_DAYS))
    return "\n".join(out)


def _fmt_all(data):
    """本周总览：每块一两行，想看细节再发对应指令。"""
    out = ["📅 本周轮换总览"]
    for g in (data.get("archimedea") or []):
        ms = g.get("missions") or []
        out.append("🧪 %s（%d 关%s）"
                   % (g.get("type") or "科研", len(ms), _left(g.get("expiry"))))
        names = " / ".join(m.get("mission") or "?" for m in ms)
        facs = {(m.get("faction") or "") for m in ms} - {""}
        if len(facs) == 1:
            names += "（%s）" % list(facs)[0]
        if names:
            out += _wrap("    " + names, 44, "    ")
    a = data.get("archonHunt") or {}
    if a:
        line = "🟣 执刑官：%s · %s" % (a.get("boss") or "?", a.get("shard") or "?")
        out += _wrap(line, 44, "    ")
        nodes = " → ".join("%s %s %s" % (m.get("node") or "?", m.get("system") or "?",
                                         m.get("mission") or "?")
                           for m in (a.get("missions") or []))
        if nodes:
            out += _wrap("    节点：" + nodes, 44, "      ")
    for d in (data.get("descents") or []):
        out.append("🕳️ 沉沦之地：%s（%d 项挑战）"
                   % (d.get("name") or "?", len(d.get("challenges") or [])))
    t = data.get("teshin") or {}
    if t:
        out.append("🏅 钢铁商店：%s" % (t.get("offer") or "?"))
    c = data.get("duviriCircuit") or {}
    if c:
        nrm = [x.get("name") for x in (c.get("normal") or []) if x.get("name")]
        hard = [x.get("name") for x in (c.get("hard") or []) if x.get("name")]
        if nrm:
            out.append("🌀 回廊 · 战甲：" + "、".join(nrm))
        if hard:
            out += _wrap("🌀 回廊 · 灵化：" + "、".join(hard), 44, "    ")
    cal = data.get("calendar") or {}
    if cal:
        days = cal.get("days") or []
        first = days[0].get("day") if days else "?"
        out.append("📅 1999 日历：%s · 第 %s 天起（%d 天排期）"
                   % (cal.get("season") or "?", first, len(days)))
    out.append("细节：发「科研」「执刑官」等指令名看单项")
    return "\n".join(out)


_ACTIONS = {
    "all": _fmt_all,
    "research": _fmt_research,
    "archon": _fmt_archon,
    "descent": _fmt_descent,
    "teshin": _fmt_teshin,
    "circuit": _fmt_circuit,
    "calendar": _fmt_calendar,
}


# ------------------------------- 指令 -------------------------------

def parse_command(text):
    """解析每周轮换指令；整句必须只由指令词组成，多一个字都算闲聊（返回 None）。"""
    s = (text or "").strip().lstrip("/").strip()
    if not s:
        return None
    low = "".join(s.split()).lower()
    for head, action in HEADS:
        if low == head:
            return {"action": action}
    return None


def is_weekly_command(text):
    return parse_command(text) is not None


def format_reply(cmd):
    """把 parse_command 的结果渲染成回复文本。"""
    if not cmd:
        return ("📅 每周轮换：科研 / 执刑官 / 沉沦之地 / 钢铁商店 / 回廊预告 / 日历\n"
                "（也可以直接发「每周」看全部总览）")
    action = cmd.get("action") or "all"
    try:
        d = load()
    except WeeklyUnavailable as e:
        return ("（每周数据暂时取不到：%s）\n"
                "本机还没缓存过时需要联网抓一次；稍后再发一次这条指令即可。"
                % e)
    fn = _ACTIONS.get(action) or _fmt_all
    return fn(d.get("data") or {}) + _stale_note(d)


def main(argv=None):
    """命令行自测：python weekly.py 科研 / python weekly.py 每周"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    print(format_reply(parse_command(" ".join(argv))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
