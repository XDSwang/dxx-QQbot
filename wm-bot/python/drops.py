#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遗物 / 掉落来源查询：数据源是 DE 官方 PublicExport 的中文导出。

为什么用这套数据（而不是社区掉落表）：
  · `PublicExport/index_zh.txt.lzma` 是官方「各语言导出清单」，一行一个
    `文件名!校验哈希`。按清单取文件，上游更新（新 Prime、新遗物）后哈希自动变，
    永远不会指到过期文件，也不用把哈希写死在代码里。
  · `ExportRelicArcane_zh.json` 里遗物名（"后纪 B10 遗物"）、奖励引用全是官方中文，
    不需要自己编物品译名；奖励只给 uniqueName 路径，用同批导出的物品表
    （资源 / 武器 / 战甲 / 守护 / Mod）反查中文名，实测 19208 条奖励引用只有 1 条
    查不到（库狛项圈蓝图），比任何社区表都全。
  · `ExportRecipes_zh.json` 给制作配方（resultType + 材料路径 + 信用点 + 工时），
    名字同样靠物品表补出来，于是「获取方式」里能直接给出材料清单。

数据落地：
  · 首次构建要顺序抓 8 个文件（同域名 3~5 秒节流，约 35 秒），合成一份精简索引
    `data/drops.json`（只留查询要用的字段）；之后查询只读本地索引，秒回、零请求。
  · 索引 7 天过期（WS_DROPS_DAYS 可调），过期后台重抓；重抓失败沿用旧索引，
    但回复里会标「索引 N 天前」，不假装是新数据。

指令：
  · 遗物 后纪B10 / 遗物 A1 / 遗物 中纪A3  → 该遗物四档精炼的奖励与概率
  · 掉落 布里斯提卡Prime蓝图 / 掉落 布莱顿 → 哪些遗物出、制作要什么材料
"""
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

try:
    import lzma                     # 解官方清单用；个别精简 Python 没编译进去
except ImportError:
    lzma = None

import wm

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"

VERSION = 1

# 官方导出根地址（可用 WM_DROP_BASE 换镜像）
DROP_BASE = (os.environ.get("WM_DROP_BASE")
             or "https://content.warframe.com/PublicExport").rstrip("/")
DOMAIN = "content.warframe.com"
INDEX_NAME = "index_zh.txt.lzma"

# 需要抓的文件：遗物与奖励、制作配方、以及用来把 uniqueName 翻成中文名的物品表
SRC_RELIC = "ExportRelicArcane_zh.json"
SRC_RECIPE = "ExportRecipes_zh.json"
SRC_NAMES = ("ExportResources_zh.json", "ExportWeapons_zh.json",
             "ExportWarframes_zh.json", "ExportSentinels_zh.json",
             "ExportUpgrades_zh.json")
SRC_FILES = (SRC_RELIC, SRC_RECIPE) + SRC_NAMES

# index_zh 拿不到 / 环境缺 lzma 时的兜底哈希（2026-11-08 抓取时的值；会自动过期失效，
# 失效后表现为「抓不到新索引」，此时继续用本地旧索引，不影响已有查询）
_FALLBACK_HASH = {
    "ExportRelicArcane_zh.json": "00_0Imx0GquR9jkKE8sKxD6eQ",
    "ExportRecipes_zh.json": "00_oac+LCAtIyqDxZF8Qe1ZZA",
    "ExportResources_zh.json": "00_y2eqwAykBxMUpJheJ24y2A",
    "ExportWeapons_zh.json": "00_wP1CHqHEVtVjyrOCaT2yIQ",
    "ExportWarframes_zh.json": "00_JdkQ9kJydzj6ausbzkerbA",
    "ExportSentinels_zh.json": "00_9XVxUBzLu5VzVKtKO8rVFg",
    "ExportUpgrades_zh.json": "00_CqiQNw4khHYdzvXXNM2KZQ",
}

UA = "wm-bot-python/0.1 (+Warframe relic/drop bot)"

# 纪别（顺序即稀有度递增，只用于排序与筛选）
TIERS = ("古纪", "前纪", "中纪", "后纪", "安魂", "先锋")
TIER_ALIAS = {
    "古纪": "古纪", "lith": "古纪", "前纪": "前纪", "meso": "前纪",
    "中纪": "中纪", "neo": "中纪", "后纪": "后纪", "axi": "后纪",
    "安魂": "安魂", "requiem": "安魂", "先锋": "先锋", "pioneer": "先锋",
}

# 稀有度：官方 rarity 字段 -> 0 铜 / 1 银 / 2 金
RARITY_CN = ("铜", "银", "金")
_RARITY_MAP = {"COMMON": 0, "UNCOMMON": 1, "RARE": 2}

# 官方掉落表的固定概率（完好 / 卓越 / 无瑕 / 光辉 四档精炼），按稀有度分组
REFINE = ("完好", "卓越", "无瑕", "光辉")
_CHANCE = {
    0: (25.33, 23.33, 20.00, 16.67),
    1: (11.00, 13.00, 17.00, 20.00),
    2: (2.00, 4.00, 6.00, 10.00),
}

RELIC_HEADS = ("虚空遗物", "遗物", "relic")
ITEM_HEADS = ("掉落", "获取方式", "获取", "出处", "来源", "drop", "where")

_INDEX = [None, 0.0]          # [索引, 载入时刻]：同进程内 60 秒内不重复读盘
_LOAD_LOCK = threading.Lock()
_BUILDING = [False]


class DropsUnavailable(RuntimeError):
    """索引既没有本地缓存、又抓不到官方数据时抛出（提示语在 message 里）。"""


def _log(msg):
    if getattr(wm, "LOG_HOOK", None):
        try:
            wm.LOG_HOOK("DROP " + str(msg))
        except Exception:
            pass


def _env_num(name, default, lo=1.0):
    try:
        v = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return v if v >= lo else default


def cache_file():
    """索引文件路径（测试可用 WS_DROPS_FILE 指到临时目录）。"""
    p = os.environ.get("WS_DROPS_FILE")
    return Path(p) if p else (DATA_DIR / "drops.json")


def ttl_seconds():
    """索引有效期：默认 7 天（新 Prime / 新遗物上线节奏远慢于此）。"""
    return _env_num("WS_DROPS_DAYS", 7.0) * 86400.0


# ------------------------------- HTTP -------------------------------

def _gate():
    """同域名 3~5 秒节流（与 WM / 世界状态共用同一份跨进程节流记录）。"""
    try:
        wm.pace_gate_ex(DOMAIN, 3.0, 5.0)
    except Exception:
        time.sleep(3.0)


def _http(url, timeout=90):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_index():
    """抓官方清单，返回 {文件名: 哈希}。失败返回兜底表（不抛：兜底还能用）。"""
    if lzma is None:
        _log("本机 Python 没有 lzma，直接用兜底哈希取文件")
        return dict(_FALLBACK_HASH)
    try:
        _gate()
        raw = _http(DROP_BASE + "/" + INDEX_NAME)
        text = lzma.decompress(raw).decode("utf-8", "replace")
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if "!" in line:
                name, h = line.split("!", 1)
                out[name.strip()] = h.strip()
        if out:
            return out
        _log("官方清单为空，改用兜底哈希")
    except Exception as e:
        _log("官方清单抓取失败，改用兜底哈希：%s" % e)
    return dict(_FALLBACK_HASH)


def _fetch_list(name, h):
    """按清单哈希抓一个导出文件，返回里面的数组（8 个文件都是单一数组）。"""
    url = "%s/Manifest/%s!%s" % (DROP_BASE, name, h)
    last = None
    for attempt in range(3):
        try:
            _gate()
            data = json.loads(_http(url).decode("utf-8", "replace"))
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        return v
                return []
            return data or []
        except Exception as e:
            last = e
            _log("抓 %s 第 %d 次失败：%s" % (name, attempt + 1, e))
            if attempt < 2:
                time.sleep(3.0)
    raise DropsUnavailable("抓取 %s 失败：%s" % (name, last))


# ------------------------------- 索引 -------------------------------

def _split_relic_name(name):
    """「后纪 B10 遗物」-> ("后纪", "B10")；认不出纪别返回 ("", "")。"""
    s = (name or "").replace("遗物", " ").strip()
    if not s:
        return "", ""
    first = s.split()[0]
    if first not in TIERS:
        return "", ""
    code = "".join(s[len(first):].split())
    return first, code


def _tier_idx(tier):
    try:
        return TIERS.index(tier)
    except ValueError:
        return len(TIERS)


def _split_key(key):
    """索引键「后纪B10」-> ("后纪", "B10")。"""
    tier = next((x for x in TIERS if key.startswith(x)), "")
    return tier, key[len(tier):]


def _rarity(reward):
    return _RARITY_MAP.get(str(reward.get("rarity") or "").upper(), 0)


def _reward_name(path, names, blueprints):
    """奖励只有 uniqueName；去掉 StoreItems 前缀后查物品表（查不到再退回文件名）。"""
    p = (path or "").replace("/StoreItems", "", 1)
    return (names.get(p) or blueprints.get(p)
            or names.get(path) or p.rsplit("/", 1)[-1])


def build_index():
    """抓齐官方导出并合成精简索引（首次约 35 秒，之后都走本地）。"""
    hashes = _fetch_index()
    raw = {}
    for name in SRC_FILES:
        h = hashes.get(name)
        if not h:
            raise DropsUnavailable("官方清单里没有 %s" % name)
        raw[name] = _fetch_list(name, h)

    names = {}
    for name in SRC_NAMES:
        for x in raw[name]:
            u, n = x.get("uniqueName"), x.get("name")
            if u and n:
                names.setdefault(u, n)

    # 蓝图本身没有 name 字段：用 resultType 名 + 「蓝图」拼出来
    blueprints = {}
    for x in raw[SRC_RECIPE]:
        nm = names.get(x.get("resultType"))
        if nm:
            blueprints[x.get("uniqueName") or ""] = (
                nm if nm.endswith("蓝图") else nm + "蓝图")

    recipes = {}
    for x in raw[SRC_RECIPE]:
        nm = names.get(x.get("resultType"))
        if not nm:
            continue
        if nm.endswith("蓝图"):
            nm = nm[:-2]
        parts = []
        for ing in (x.get("ingredients") or []):
            pn = (names.get(ing.get("ItemType"))
                  or str(ing.get("ItemType") or "").rsplit("/", 1)[-1])
            parts.append([pn, int(ing.get("ItemCount") or 1)])
        recipes.setdefault(nm, [int(x.get("buildPrice") or 0),
                                int(x.get("buildTime") or 0), parts])

    relics, by_item = {}, {}
    for x in raw[SRC_RELIC]:
        if "/Projections/" not in str(x.get("uniqueName") or ""):
            continue
        tier, code = _split_relic_name(x.get("name"))
        if not code:
            continue
        key = tier + code
        if key in relics:
            # 同一遗物的「完好 / 卓越 / 无瑕 / 光辉」四条奖励列表完全一致，
            # 概率差异由固定掉落表推，不用存四份。
            continue
        items = [[_reward_name(r.get("rewardName"), names, blueprints), _rarity(r)]
                 for r in (x.get("relicRewards") or [])]
        if not items:
            continue
        relics[key] = [_tier_idx(tier), items]
        for iname, rr in items:
            by_item.setdefault(iname, []).append([key, rr])

    idx = {
        "version": VERSION,
        "built_at": int(time.time()),
        "relics": relics,
        "by_item": by_item,
        "recipes": recipes,
    }
    _log("索引构建完成：遗物 %d 个 / 可查询物品 %d 个 / 配方 %d 条"
         % (len(relics), len(by_item), len(recipes)))
    return idx


def _read_cache():
    p = cache_file()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        _log("本地索引读取失败（当作没有缓存）：%s" % e)
        return None
    if not isinstance(d, dict) or d.get("version") != VERSION:
        _log("本地索引版本过旧，重抓一次")
        return None
    return d


def _write_cache(idx):
    p = cache_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        _log("索引落盘失败（本次仍可用）：%s" % e)


def load_index(force=False):
    """取索引：内存 -> 本地文件（未过期）-> 重抓。

    重抓失败时若有旧索引就沿用并打上 stale 标记（回复里会写「索引 N 天前」），
    既不会假装数据是新的，也不会因为一次网络抖动就整个功能不可用。
    """
    now = time.time()
    with _LOAD_LOCK:
        if not force and _INDEX[0] is not None and now - _INDEX[1] < 60:
            return _INDEX[0]
        old = _read_cache()
        if old is not None and not force:
            if now - float(old.get("built_at") or 0) < ttl_seconds():
                _INDEX[0], _INDEX[1] = old, now
                return old
        try:
            fresh = build_index()
        except Exception as e:
            if old is not None:
                old["stale"] = True
                _INDEX[0], _INDEX[1] = old, now
                return old
            raise DropsUnavailable(str(e))
        _write_cache(fresh)
        _INDEX[0], _INDEX[1] = fresh, now
        return fresh


def ensure_async():
    """启动时后台预抓一次：让第一次查询就能秒回，不卡 35 秒。"""
    def run():
        if _BUILDING[0]:
            return
        _BUILDING[0] = True
        try:
            load_index()
        except Exception as e:
            _log("后台预抓索引失败（查询时会再试）：%s" % e)
        finally:
            _BUILDING[0] = False

    t = threading.Thread(target=run, name="drops-prefetch")
    t.daemon = True
    t.start()
    return t


# ------------------------------- 指令 -------------------------------

def _relic_q_ok(q):
    """遗物查询的参数必须是「纪别 + 字母数字编号」这种形状。

    否则「@机器人 遗物多不多」这种闲聊也会被当成指令回一句，违反「完美匹配」原则。
    返回 True 的三种情况：只发「遗物」（给用法）、只给纪别（列编号）、给了编号。
    """
    raw = _norm_key(q).replace("遗物", "").replace("的", "")
    if not raw:
        return True
    for k in TIER_ALIAS:
        if raw.startswith(k):
            raw = raw[len(k):]
            break
    return raw == "" or (raw.isascii() and raw.isalnum())


def is_drop_command(text):
    """是否遗物 / 掉落查询：以明确词开头，且参数形状对得上（避免闲聊误触发）。"""
    cmd = parse_command(text)
    if not cmd:
        return False
    if cmd["action"] == "relic":
        return _relic_q_ok(cmd.get("q") or "")
    return True


def parse_command(text):
    s = (text or "").strip().lstrip("/").strip()
    if not s:
        return None
    low = s.lower()
    for h in RELIC_HEADS:
        if low.startswith(h):
            return {"action": "relic", "q": s[len(h):].strip(" ：:，,")}
    for h in ITEM_HEADS:
        if low.startswith(h):
            return {"action": "item", "q": s[len(h):].strip(" ：:，,")}
    return None


def _num(v):
    """概率显示：整数不带小数点，其它保留两位（25.33 / 2 / 16.67）。"""
    if abs(float(v) - round(float(v))) < 0.005:
        return "%d" % round(float(v))
    return ("%.2f" % float(v)).rstrip("0").rstrip(".")


def _chance_text(rr):
    return "/".join(_num(x) + "%" for x in _CHANCE.get(rr, (0, 0, 0, 0)))


def _norm_key(s):
    """查询归一：去空格、转小写（中文名里的空格在输入时常被漏掉）。"""
    return "".join(str(s or "").split()).lower()


def _stale_note(idx):
    if not idx.get("stale"):
        return ""
    days = max(0, int((time.time() - float(idx.get("built_at") or 0)) // 86400))
    return "\n（⚠️ 官方数据本次没刷新成功，用的是 %d 天前的索引）" % days


def _relic_candidates(idx, tier, code):
    """按 (纪别, 编号) 找遗物；没给纪别就跨纪别找同编号。"""
    relics = idx.get("relics") or {}
    if tier:
        key = tier + code
        return [key] if key in relics else []
    out = [key for key in relics if _split_key(key)[1] == code]
    out.sort(key=lambda k: (_tier_idx(_split_key(k)[0]), k))
    return out


def _relic_detail(idx, key):
    tier, code = _split_key(key)
    data = (idx.get("relics") or {}).get(key)
    if not data:
        return "（本地索引里没有这个遗物，稍后会自动重抓）"
    items = sorted(data[1], key=lambda it: (-it[1], it[0]))
    lines = ["🔮 %s %s 遗物（%d 项）" % (tier, code, len(items))]
    for rr in (2, 1, 0):
        group = [n for n, r in items if r == rr]
        if not group:
            continue
        lines.append("%s %s" % (RARITY_CN[rr], _chance_text(rr)))
        for n in group:
            lines.append("  · " + n)
    lines.append("依次为：%s（精炼度）" % " / ".join(REFINE))
    return "\n".join(lines) + _stale_note(idx)


def _relic_reply(idx, q):
    if not q:
        return ("🔮 用法：遗物 后纪B10 / 遗物 A1 / 遗物 中纪A3\n"
                "（也可以写 Lith/Meso/Neo/Axi 英文纪名）")
    raw = _norm_key(q).replace("遗物", "").replace("的", "")
    tier = ""
    for k, v in TIER_ALIAS.items():
        if raw.startswith(k):
            tier, raw = v, raw[len(k):]
            break
    code = raw.upper()
    if not code:
        if not tier:
            return ("🔮 没认出遗物编号。发「遗物 后纪B10」这种格式；"
                    "纪别可用 古纪/前纪/中纪/后纪/安魂。")
        same = [k for k in (idx.get("relics") or {})
                if k.startswith(tier)]
        if not same:
            return "🔮 本地索引里没有「%s」的遗物。" % tier
        same.sort()
        head = "、".join(same[:20])
        more = "（只列前 20）" if len(same) > 20 else ""
        return "🔮 %s 遗物共 %d 个%s\n%s\n发「遗物 %s」看某个的具体奖励。" % (
            tier, len(same), more, head, same[0])
    got = _relic_candidates(idx, tier, code)
    if not got:
        return ("🔮 没找到「%s%s」这个遗物。\n"
                "发「遗物 A1」可看同名编号分别属于哪些纪。" % (tier, code))
    if len(got) > 1:
        names = "、".join("%s %s" % _split_key(k) for k in got)
        return ("🔮 %s 有 %d 个纪都叫这个号：%s\n发「遗物 %s」看具体奖励。"
                % (code, len(got), names, got[0]))
    return _relic_detail(idx, got[0])


def _find_items(idx, q):
    """查物品中文名：遗物奖励与配方名一起找。

    先精确（忽略空格），再双向包含。精确优先很重要：查「布莱顿」时不能因为
    「布莱顿 Prime 枪管」也包含这三个字就把用户要的基础武器挤掉。
    """
    names = set(idx.get("by_item") or {}) | set(idx.get("recipes") or {})
    nq = _norm_key(q)
    if not nq:
        return []
    exact = sorted(n for n in names if _norm_key(n) == nq)
    if exact:
        return exact
    part = [n for n in names if nq in _norm_key(n) or _norm_key(n) in nq]
    part.sort(key=lambda n: (len(n), n))
    return part[:12]


def _find_recipes(idx, q):
    recipes = idx.get("recipes") or {}
    nq = _norm_key(q)
    exact = [n for n in recipes if _norm_key(n) == nq]
    if exact:
        return exact
    return [n for n in recipes if nq in _norm_key(n)][:8]


def _fmt_duration(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return ""
    h, m = sec // 3600, (sec % 3600) // 60
    if h and m:
        return "%d 小时 %d 分" % (h, m)
    if h:
        return "%d 小时" % h
    return "%d 分" % m


def _recipe_block(recipe):
    credit, sec, parts = recipe[0], recipe[1], recipe[2]
    bits = []
    if credit:
        bits.append("%s 信用点" % format(credit, ","))
    if sec:
        bits.append(_fmt_duration(sec))
    lines = ["🔧 制作：" + (" · ".join(bits) if bits else "可制作")]
    if parts:
        lines.append("  材料：" + "、".join("%s ×%d" % (n, c) for n, c in parts))
    else:
        # 布莱顿这类老蓝图在官方导出里 ingredients 就是空数组（材料写在游戏内蓝图页），
        # 不能凭空编材料，照实说明。
        lines.append("  材料：官方导出未列出（老蓝图，以游戏内蓝图页为准）")
    return lines


def _item_reply(idx, q):
    if not q:
        return ("📦 用法：掉落 布里斯提卡Prime蓝图 / 掉落 布莱顿\n"
                "会告诉你它出自哪些遗物（含稀有度与概率），以及制作材料。")
    found = _find_items(idx, q)
    if not found:
        return ("📦 没找到「%s」。\n"
                "· 试试官方中文名（如「布里斯提卡 Prime蓝图」「布莱顿」）\n"
                "· 或者先发「遗物 后纪B10」直接看某个遗物的奖励。" % q)
    if len(found) > 1:
        return ("📦 「%s」匹配到 %d 个物品：%s\n发完整名字再查一次。"
                % (q, len(found), "、".join(found[:12])))
    name = found[0]
    lines = ["📦 " + name]
    src = (idx.get("by_item") or {}).get(name) or []
    if src:
        uniq = {}
        for key, rr in src:
            uniq.setdefault(key, rr)
        ordered = sorted(uniq.items(),
                         key=lambda kv: (_tier_idx(_split_key(kv[0])[0]),
                                         -kv[1], kv[0]))
        lines.append("遗物来源（%d 个）：" % len(ordered))
        for key, rr in ordered[:12]:
            tier, code = _split_key(key)
            lines.append("  · %s %s · %s %s" % (tier, code, RARITY_CN[rr],
                                                _chance_text(rr)))
        if len(ordered) > 12:
            lines.append("  …还有 %d 个遗物" % (len(ordered) - 12))
    else:
        lines.append("遗物来源：无（不是 Prime 部件）")
    # 「Mesa Prime蓝图」这类主蓝图：配方的键是不带「蓝图」的成品名
    recipes = idx.get("recipes") or {}
    recipe = recipes.get(name)
    if recipe is None and name.endswith("蓝图"):
        recipe = recipes.get(name[:-2])
    if recipe:
        lines += _recipe_block(recipe)
    else:
        lines.append("制作：官方导出里没有它的配方（多为商店 / 集团 / 掉落产出）")
    return "\n".join(lines) + _stale_note(idx)


def format_reply(cmd):
    """把 parse_command 的结果渲染成回复文本（取不到数据时给可读提示）。"""
    if not cmd:
        return ("🔮 遗物 后纪B10 —— 看某个遗物的奖励\n"
                "📦 掉落 布里斯提卡Prime蓝图 —— 看它出自哪些遗物")
    try:
        idx = load_index()
    except DropsUnavailable as e:
        return ("（官方数据暂时取不到：%s）\n"
                "本机还没建过索引时需要联网抓一次；稍后再发一次这条指令即可。"
                % e)
    if cmd["action"] == "relic":
        return _relic_reply(idx, cmd.get("q") or "")
    return _item_reply(idx, cmd.get("q") or "")


def main(argv=None):
    """命令行自测：python drops.py 遗物 后纪B10 / python drops.py 掉落 布莱顿"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd = parse_command(" ".join(argv))
    print(format_reply(cmd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
