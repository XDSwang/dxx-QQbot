#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wm.py — Warframe Market 物品 / 紫卡(Riven) 查询（独立 Python 脚本，零依赖）

可直接在桌面 / Termux / QPython 运行：
    python wm.py "WM Boltor Prime"        # 查物品
    python wm.py "ZK Tigris"              # 查紫卡 Riven
    python wm.py "/报错 WM Boltor Prime"  # 强制再查一次

也可作为模块被「技能 / QQ 机器人」import 复用：
    from wm import query, format_reply, parse_command

特性：
    - 按需缓存：只缓存用户实际查过的物品（key = 类型:名称），绝不预拉全站物品清单；
    - 名称解析同样按需：只对用户输入的这【一个】名称生成少量候选 slug
      （如 Boltor Prime -> boltor_prime_set），命中结果记进 data/slug.json；
      绝不预拉 /v2/items 全站清单，每个候选请求仍逐次走 3~5 秒节流；
    - 缓存新鲜度可配置：改同目录 data/config.txt 里的 WM_CACHE_MINUTES（单位：分钟，默认 60）；
    - 用户发起查询时才更新；缓存超过间隔就【先联网刷新缓存、再回复用户】，
      绝不把几天/几个月前的旧缓存当结果发出去；刷新失败则明确报错，不拿旧数据顶包；
    - 3~5 秒礼貌限流（文件持久 data/pace.json，断开/重启仍生效）：
      每个 HTTP 请求（含订单列表）都先过 pace_gate，mock 演示也真等；
    - 刷新失败：用户这次只尝试 1 次并立即收到明确报错（主线程不阻塞），
      1/3/7 分钟重试（累计最多 4 次）交给后台线程继续，成功后自动写缓存；
      CLI 手动运行 python wm.py 时仍走完整阻塞重试，人工等得起；
    - 所有数据文件都在脚本同目录的 data/ 下（基于 __file__ 解析，无绝对路径、无上层路径）。
"""
import os
import re
import sys
import io
import json
import time
import random
import gzip
import calendar
import threading
import urllib.parse
import urllib.request
import urllib.error
import statistics
from pathlib import Path

import config as bot_config          # 同目录 data/config.txt（纯文本、带注释，零依赖）
bot_config.apply()                   # 补进环境变量；已存在的 env 优先、不被覆盖

# ----------------------------- 配置 -----------------------------
# 所有路径都基于「脚本自身所在目录」解析：整个文件夹拷到手机任意位置都能跑，
# 不依赖当前工作目录、不写死盘符、也不出现 ../ 上层路径。
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
CACHE_FILE = DATA_DIR / "cache.json"
PACE_FILE = DATA_DIR / "pace.json"
# 只记录「用户查过的名称 -> 命中的 WM slug」，不是全站索引；物品名固定，永久有效。
SLUG_FILE = DATA_DIR / "slug.json"

# 数据源根地址：默认官方 API。若你的网络需要走代理 / 镜像，改 data/config.txt 的 WM_API_BASE 即可。
API = (os.environ.get("WM_API_BASE") or "https://api.warframe.market").rstrip("/")
UA = "wm-bot-python/0.1 (+local Warframe Market price bot)"
HEADERS = {
    "accept": "application/json",
    "Platform": "pc",
    "Language": "zh-hans",
    "User-Agent": UA,
}

SOURCE = os.environ.get("WM_SOURCE", "warframe")   # warframe（真实访问） | mock（离线假数据）

# 离线 mock 故意失败次数（仅测试重试/报错路径用）：WM_MOCK_FAIL=N 让前 N 次 mock 取数抛错。
# 例：WM_MOCK_FAIL=99 模拟源一直挂 -> 走 4 次重试 -> error；WM_MOCK_FAIL=2 模拟前 2 次失败、第 3 次成功。
MOCK_FAIL = int((os.environ.get("WM_MOCK_FAIL", "0") or 0))

# ---- 时间规则（用户硬性要求，禁止用改时间的方式规避）----
# 同域名访问间隔统一 3~5 秒（用户规则：所有项目 3~5 秒）。
# 真实源 warframe：所有时间下限锁死，env 只允许「调高」（更保守），压低一律无效。
# 离线 mock：仍模拟"访问同域名"，【同样真等 3~5 秒礼貌间隔】，以演示真实行为；
#   mock 下也不允许把 WM_MIN_GAP / WM_MAX_GAP 调小（_clamp 一律锁死硬下限）。
#   即：节流间隔永不快进，只有"失败重试等待"可在 mock 下调小。
REAL = SOURCE != "mock"
HARD_MIN_GAP = 3.0                      # 同域名访问间隔下限 3 秒（用户统一规则：所有项目 3~5 秒）
HARD_MAX_GAP = 5.0                      # 同域名访问间隔上限 5 秒
# 缓存新鲜度不是硬规则，由 WM_CACHE_MINUTES（分钟）配置，见下方 CACHE_MINUTES。
HARD_MICRO_S = [60, 180, 420]           # 失败重试 1/3/7 分钟
HARD_MACRO_S = 3600                     # 4 次全失败后间隔 1 小时


def _clamp(name, default, hard, cast=float):
    """env 只能把时间调大；小于硬下限一律 clamp 回硬下限。

    真实源与 mock 演示都不例外：同域名 3~5 秒节流不可快进（演示真实才能反映上线问题）。
    只有「失败重试等待」允许在 mock 下快进（见 MICRO_WAITS），两者互不相干。
    """
    try:
        v = cast(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        v = default
    return max(v, hard)


MIN_GAP = _clamp("WM_MIN_GAP", 3, HARD_MIN_GAP)
MAX_GAP = _clamp("WM_MAX_GAP", 5, HARD_MAX_GAP)
if MAX_GAP < MIN_GAP:
    MAX_GAP = MIN_GAP
# 缓存新鲜度：单位「分钟」，默认 60；改同目录 data/config.txt 的 WM_CACHE_MINUTES 即可（如 15）。
def _env_int(name, default, lo=0):
    try:
        return max(lo, int(float(os.environ.get(name, default))))
    except (ValueError, TypeError):
        return default


CACHE_MINUTES = _env_int("WM_CACHE_MINUTES", 60)
STALE_MS = CACHE_MINUTES * 60 * 1000
# 单次 HTTP 超时（秒）。超时就立刻明确报错，绝不让机器人主线程一直挂着假死。
HTTP_TIMEOUT = _env_int("WM_HTTP_TIMEOUT", 15, lo=3)
MACRO_WAIT_MS = int(_clamp("WM_MACRO_WAIT", 3600, HARD_MACRO_S, int)) * 1000
_env_micro = os.environ.get("WM_MICRO_WAITS")
_micro = None
if _env_micro is not None:
    try:
        _micro = [int(x) for x in _env_micro.split(",")]
    except (ValueError, TypeError):
        _micro = None

if REAL:
    # 真实源 warframe：失败重试 1/3/7 分钟锁死，env 只能调高、压低无效
    MICRO_WAITS = [
        max(HARD_MICRO_S[i], (_micro[i] if (_micro and i < len(_micro)) else HARD_MICRO_S[i]))
        for i in range(len(HARD_MICRO_S))
    ]
else:
    # 离线 mock：默认快进重试等待（1/2/3 秒），只演示"4 次重试"结构、不卡 11 分钟；
    # 想看真实节奏可设 WM_MICRO_WAITS=60,180,420。注意：同域名 5~9 秒节流仍真等，不快进。
    MICRO_WAITS = _micro if (_micro and len(_micro) >= 3) else [1, 2, 3]

MAX_ATTEMPTS = 4


# ----------------------------- 小工具 -----------------------------
def load_json(p, default):
    # utf-8-sig：手机编辑器手工改过 data/*.json 时可能带 BOM，带 BOM 也要能读
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(p, obj):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _entry_is_real(entry):
    """缓存条目是否来自真实源。

    离线测试产生的 MockSeller 假价格即使误写进 data/cache.json，也绝不能被
    当成有效缓存秒回给用户；视为无缓存 -> 触发真实联网刷新。
    """
    if not entry or entry.get("status") != "ok" or not entry.get("data"):
        return False
    s = str(entry.get("source", "")).lower()
    return "mock" not in s and "本地测试数据" not in s


# ----------------------------- 运行流程日志 -----------------------------
# 由 qq_official 注入 flog（写进 data/bot_flow.log）；CLI / 测试未注入时静默。
LOG_HOOK = None


def _log(msg):
    if LOG_HOOK:
        try:
            LOG_HOOK("WM " + str(msg))
        except Exception:
            pass


# --------------------- 礼貌限流（文件持久） ---------------------
# 三个原子函数，所有域名（WM / 世界状态 / wiki / 词库 / 特殊任务）共用同一套：
#   pace_last_access(domain)  查询某域名上次访问时间（没访问过返回 0.0）
#   pace_record(domain, when) 记录某域名本次访问时间（文件不存在自动建）
#   pace_gate_ex(domain, lo, hi) 阻塞节流：查上次 → 算间隔 → 必要时 sleep → 记录本次
# pace.json 格式：扁平的 {域名: 上次访问时间(epoch 秒)}，按域名键查/改。


def pace_last_access(domain):
    """查询某域名【上次访问时间】（epoch 秒）。

    - 文件不存在 / 没有该域名记录：返回 0.0（表示没访问过）；
    - 有记录：返回该域名上次访问的真实时刻（秒，epoch）；
    - 记录损坏（非数字）/ 时钟回拨（记录的时间在未来）：也返回 0.0，
      按「没访问过」处理，避免异常久等或跳过节流。
    """
    data = load_json(PACE_FILE, {})
    try:
        last = float(data.get(domain, 0.0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if last <= 0 or last > time.time():
        return 0.0
    return last


def pace_record(domain, when=None):
    """记录某域名【本次访问时间】（epoch 秒）写回 pace.json。

    - when 留空：取【真实当前时间】（time.time()），绝不写未来时间；
    - 文件 / 目录不存在：自动创建（save_json 内部已 mkdir）；
    - 其它域名的记录原样保留，只改这一个域名的键。
    用于「自己已经 sleep 过、只想登记一次访问」的场景；
    pace_gate_ex 内部也用它写盘。
    """
    t = float(when) if when is not None else time.time()
    data = load_json(PACE_FILE, {})
    data[domain] = t
    save_json(PACE_FILE, data)
    return t


def pace_gate_ex(domain, min_gap, max_gap):
    """通用版：可指定「间隔范围（秒）」的同类节流，写回本机真实当前时间。

    用于世界状态（api.warframestat.us）等其它域名：间隔由调用方传入，
    世界状态同样用 3~5 秒，与 WM 共用同一套「写盘持久 + 时钟回拨保护」逻辑。
    - min_gap/max_gap：相邻两次访问该域名的最小/最大间隔（随机取值，避免对齐突刺）；
    - 没访问过（pace_last_access 返回 0）：按完整间隔等待；
    - 时钟回拨（换新设备 / 未校时）时按完整间隔等待，不异常久等、也不跳过节流。
    内部 = pace_last_access 查上次 → 算 wait → 必要时 sleep → pace_record 写本次。
    """
    try:
        lo = float(min_gap)
        hi = float(max_gap)
    except (TypeError, ValueError):
        lo, hi = 3.0, 5.0
    if hi < lo:
        hi = lo
    last = pace_last_access(domain)              # 0.0 = 没访问过 / 回拨 → 按完整间隔等
    gap = random.uniform(lo, hi)
    elapsed = (time.time() - last) if last > 0 else 0.0
    if elapsed < 0:                              # 理论上 pace_last_access 已挡回拨，双保险
        elapsed = 0.0
    wait = max(0.0, gap - elapsed)
    if wait > 0:
        time.sleep(wait)
    pace_record(domain)                          # sleep 之后取真实当前时间写盘


def pace_gate(domain):
    """WM 专用：固定 3~5 秒同域名节流（保持原行为）。"""
    pace_gate_ex(domain, MIN_GAP, MAX_GAP)


# ----------------------------- HTTP -----------------------------
class ItemNotFound(RuntimeError):
    """WM 明确返回「物品不存在」（404 app.item.notFound）。

    这是确定性错误：物品名写错时重试 4 次也还是 404，不该浪费同域名请求，
    也不该启动 1/3/7 分钟后台重试；直接提示用户改用完整物品名即可。
    """


def http_get(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    pace_gate("api.warframe.market")
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with bot_config.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if e.code == 404 and "app.item.notFound" in body:
            raise ItemNotFound(f"物品不存在 {path}")
        raise RuntimeError(f"HTTP {e.code} {body[:200]}")
    except Exception as e:
        raise RuntimeError(f"网络错误 {e}")


# ----------------------------- 离线假数据 -----------------------------
def mock_fetch(type_, name):
    global MOCK_FAIL
    if MOCK_FAIL > 0:
        MOCK_FAIL -= 1
        raise RuntimeError("mock 故意失败（剩余 %d 次）" % MOCK_FAIL)
    base = 8 if type_ == "item" else 500
    # 故意混入三种在线状态（含 1 个 45 天没登录的），演示「在线优先」排序
    orders = []
    now = time.time()
    for i in range(1, 11):
        st = "ingame" if i <= 3 else ("online" if i <= 5 else "offline")
        days = 0 if st != "offline" else (45 if i >= 9 else i)
        row = {
            "seller": f"MockSeller{i}", "price": base + i * 2,
            "quantity": (i % 3) + 1,
        }
        row.update(_status_info(
            st, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days * 86400))))
        orders.append(row)
    orders, stats, samples, basis = _finish_rows(orders)
    prices = [o["price"] for o in samples]
    return {
        "price": prices[0],
        "median": int(statistics.median(prices)),
        "unit": "铂金",
        "count": len(orders),
        "orders": orders,
        "stats": stats,
        "priceBasis": basis,
    }


# --------------------- 中英文翻译（wiki 来源，本地缓存） ---------------------
# 来源：Warframe 官方英文 wiki（wiki.warframe.com）与灰机中文 wiki（warframe.huijiwiki.com）。
# 注意：这两个 wiki 都启用了 Cloudflare 浏览器验证，纯标准库 urllib 在桌面/手机上【无法自动抓取】；
# 因此把"查过的翻译"落成本地 data/i18n.json（首次运行用 wiki 数据内置一份 starter），
# 运行时只读写本地文件、不依赖外网；联网抓取逻辑仍保留，待网络环境可绕过时使用。
# 武器多有中文名；战甲在中文 wiki 多沿用英文名（故不单列，缺失时回退英文）。
def _i18n_path():
    return DATA_DIR / "i18n.json"

# 首次运行若无 i18n.json，写入这份内置 starter（取自灰机中文 wiki 真实中英对照，
# 武器名已用浏览器后端逐一核对；战甲在灰机用英文标题、无官方中文名，故不收录）。
# 联网抓取被 Cloudflare 挡住时会自动同步全量词库（见 sync_lexicon），亦可直接 edit 这里补条目。
_I18N_SEED = {
    # ---- 武器（灰机有官方中文名，已核对）----
    "boltor":            {"en": "Boltor", "zh": "螺钉步枪", "src": "wiki-seed"},
    "boltor prime":      {"en": "Boltor Prime", "zh": "螺钉步枪Prime", "src": "wiki-seed"},
    "braton":            {"en": "Braton", "zh": "布莱顿", "src": "wiki-seed"},
    "braton prime":      {"en": "Braton Prime", "zh": "布莱顿Prime", "src": "wiki-seed"},
    "latron":            {"en": "Latron", "zh": "拉特昂", "src": "wiki-seed"},
    "latron prime":      {"en": "Latron Prime", "zh": "拉特昂Prime", "src": "wiki-seed"},
    "paris":             {"en": "Paris", "zh": "帕里斯", "src": "wiki-seed"},
    "paris prime":       {"en": "Paris Prime", "zh": "帕里斯Prime", "src": "wiki-seed"},
    "galatine":          {"en": "Galatine", "zh": "迦伦提恩", "src": "wiki-seed"},
    "galatine prime":    {"en": "Galatine Prime", "zh": "迦伦提恩Prime", "src": "wiki-seed"},
    "orthos prime":      {"en": "Orthos Prime", "zh": "欧特鲁斯Prime", "src": "wiki-seed"},
    "tigris":            {"en": "Tigris", "zh": "猛虎", "src": "wiki-seed"},
    "tigris prime":      {"en": "Tigris Prime", "zh": "猛虎Prime", "src": "wiki-seed"},
    "hek":               {"en": "Hek", "zh": "海克", "src": "wiki-seed"},          # 已核对
    "boar":              {"en": "Boar", "zh": "野猪", "src": "wiki-seed"},          # 已核对
    "boar prime":        {"en": "Boar Prime", "zh": "野猪Prime", "src": "wiki-seed"},
    "lanka":             {"en": "Lanka", "zh": "兰卡", "src": "wiki-seed"},          # 已核对
    "lanka prime":       {"en": "Lanka Prime", "zh": "兰卡Prime", "src": "wiki-seed"},
    "rubico":            {"en": "Rubico", "zh": "绝路", "src": "wiki-seed"},         # 已核对
    "rubico prime":      {"en": "Rubico Prime", "zh": "绝路Prime", "src": "wiki-seed"},
    "gram":              {"en": "Gram", "zh": "格拉姆", "src": "wiki-seed"},         # 已核对
    "gram prime":        {"en": "Gram Prime", "zh": "格拉姆Prime", "src": "wiki-seed"},
    "furax":             {"en": "Furax", "zh": "弗拉克斯", "src": "wiki-seed"},      # 已核对
    "karak":             {"en": "Karak", "zh": "卡拉克", "src": "wiki-seed"},        # 已核对
    "dera":              {"en": "Dera", "zh": "德拉", "src": "wiki-seed"},           # 已核对
    "strun":             {"en": "Strun", "zh": "斯特朗", "src": "wiki-seed"},
    "strun prime":       {"en": "Strun Prime", "zh": "斯特朗Prime", "src": "wiki-seed"},
    "burston":           {"en": "Burston", "zh": "伯斯顿", "src": "wiki-seed"},
    "burston prime":     {"en": "Burston Prime", "zh": "伯斯顿Prime", "src": "wiki-seed"},
    "supra":             {"en": "Supra", "zh": "苏普拉", "src": "wiki-seed"},
    "stradavar":         {"en": "Stradavar", "zh": "斯特拉迪瓦", "src": "wiki-seed"},
    "stradavar prime":   {"en": "Stradavar Prime", "zh": "斯特拉迪瓦Prime", "src": "wiki-seed"},
    "sybaris":           {"en": "Sybaris", "zh": "席芭莉丝", "src": "wiki-seed"},
    "sybaris prime":     {"en": "Sybaris Prime", "zh": "席芭莉丝Prime", "src": "wiki-seed"},
    "grakata":           {"en": "Grakata", "zh": "狂鲨", "src": "wiki-seed"},
    "grakata prime":     {"en": "Grakata Prime", "zh": "狂鲨Prime", "src": "wiki-seed"},
    "gorgon":            {"en": "Gorgon", "zh": "灭杀者", "src": "wiki-seed"},
    "gorgon prime":      {"en": "Gorgon Prime", "zh": "灭杀者Prime", "src": "wiki-seed"},
    "corinth":           {"en": "Corinth", "zh": "科林斯", "src": "wiki-seed"},
    "corinth prime":     {"en": "Corinth Prime", "zh": "科林斯Prime", "src": "wiki-seed"},
    "setyr":             {"en": "Setyr", "zh": "塞多", "src": "wiki-seed"},
    "setyr prime":       {"en": "Setyr Prime", "zh": "塞多Prime", "src": "wiki-seed"},
    "drakgoon":          {"en": "Drakgoon", "zh": "碎裂者", "src": "wiki-seed"},
    "drakgoon prime":    {"en": "Drakgoon Prime", "zh": "碎裂者Prime", "src": "wiki-seed"},
    "ignis":             {"en": "Ignis", "zh": "金工火神", "src": "wiki-seed"},
    "daikyu":            {"en": "Daikyu", "zh": "大久和弓", "src": "wiki-seed"},
    "daikyu prime":      {"en": "Daikyu Prime", "zh": "大久和弓Prime", "src": "wiki-seed"},
    "cernos":            {"en": "Cernos", "zh": "西诺斯", "src": "wiki-seed"},
    "cernos prime":      {"en": "Cernos Prime", "zh": "西诺斯Prime", "src": "wiki-seed"},
    "dread":             {"en": "Dread", "zh": "恐惧", "src": "wiki-seed"},
    "bramma":            {"en": "Bramma", "zh": "布拉玛", "src": "wiki-seed"},
    "lenz":              {"en": "Lenz", "zh": "楞次弓", "src": "wiki-seed"},
    "mutalist cernos":   {"en": "Mutalist Cernos", "zh": "异融西诺斯", "src": "wiki-seed"},
    # 注意：战甲在灰机用英文标题、无官方中文名，故此处不收录战甲中文名（沿用英文）。
    # 仅收录「灰机确有官方中文名」的武器；未核实的不收录，避免编造中文名。
}

def _ensure_i18n():
    p = _i18n_path()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if p.exists():
        # 已有本地表：把代码里【新增】的 seed 条目合并进去（不覆盖用户已改/已抓的），
        # 让老用户也自动拿到扩充的翻译表，不用手动删文件。
        data = load_json(p, {"entries": {}})
        entries = data.setdefault("entries", {})
        merged = False
        for k, v in _I18N_SEED.items():
            if k not in entries:
                entries[k] = v
                merged = True
        if merged:
            save_json(p, data)
        return
    save_json(p, {"entries": dict(_I18N_SEED)})

def load_i18n():
    _ensure_i18n()
    return load_json(_i18n_path(), {"entries": {}})

def save_i18n(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_json(_i18n_path(), data)

def is_cjk(s):
    return any('\u4e00' <= ch <= '\u9fff' for ch in (s or ""))

def lookup_i18n(token):
    """按英文或中文名（可带多余词）查出翻译条目 {en,zh,src}；查不到返回 None。

    键是英文名（小写），但用户可能输入中文名 —— 这里同时做：
      1) 精确键匹配；2) 中文名精确匹配（必须排在子串前，否则 Prime 后缀被基础名截胡）；
      3) 英文子串匹配；4) 中文子串匹配。
    这样 `翻译 螺钉步枪`、`翻译 螺钉步枪Prime` 与 `翻译 Boltor` 都能各命中对应条目。
    """
    token = (token or "").strip().lower()
    if not token:
        return None
    entries = load_i18n().get("entries", {})
    if token in entries:
        return entries[token]
    # 中文名精确匹配优先：「螺钉步枪Prime」必须命中 Prime 版，不能被「螺钉步枪」先截胡
    for v in entries.values():
        zh = (v.get("zh") or "").lower()
        if zh and zh == token:
            return v
    # 子串匹配取「最长命中」：避免 "Boltor Prime Set" 被 "boltor"、
    # "螺钉步枪Prime" 被 "螺钉步枪" 这类基础名截胡
    best, best_len = None, 0
    for k, v in entries.items():
        if k and k in token and len(k) > best_len:
            best, best_len = v, len(k)
    if best:
        return best
    best, best_len = None, 0
    for v in entries.values():
        zh = (v.get("zh") or "").lower()
        if zh and zh in token and len(zh) > best_len:
            best, best_len = v, len(zh)
    if best:
        return best
    # 未命中 -> 查全量词库（若已同步过）；词库只做精确匹配，避免短词误命中
    return lookup_lexicon(token)

def translate_zh_to_en(text):
    """中文输入 -> 英文（用于去 WM 查价）。返回英文或 None。"""
    if not is_cjk(text):
        return None
    v = lookup_i18n(text)
    return v.get("en") if v else None

def translate_en_to_zh(en):
    """英文 -> 中文（用于回复展示 / 中文发起指令）。没有则返回 None。"""
    if not en:
        return None
    v = lookup_i18n(en)
    return v.get("zh") if v else None

def resolve_query_name(name):
    """把用户输入的名称归一为 (英文查询名, 中文展示名, 错误信息)。
    含中文 -> 查本地翻译表翻成英文；纯英文 -> 尝试取中文展示名（无则 None）。
    """
    name = (name or "").strip()
    if is_cjk(name):
        en = translate_zh_to_en(name)
        if not en:
            return None, None, (
                f"未收录「{name}」的中英对照，请改用 WM 英文名查询（如 Boltor Prime），"
                f"或把中英文名加进 data/i18n.json")
        return en, name, None
    return name, translate_en_to_zh(name), None

def _translate_reply(title, en, zh, note):
    """翻译指令统一回复：只给名称对照，并明确 wiki 正文（获取方式/材料）不在此提供。"""
    zh_disp = zh or "（无官方中文名，wiki 沿用英文）"
    head = f"🔤 {title}：{en}" + (f" / {zh}" if zh else "")
    u_en = urllib.parse.quote(en)
    u_zh = urllib.parse.quote(zh or en)
    return (f"{head}\n英文：{en}\n中文：{zh_disp}\n"
            f"想看获取方式 / 锻造材料等 wiki 正文，请点：\n"
            f"官方英文：https://wiki.warframe.com/w/{u_en}\n"
            f"灰机中文：https://warframe.huijiwiki.com/wiki/{u_zh}\n"
            f"（{note}；仅名称翻译，页面正文不在机器人内）")


def translate_command(name):
    """翻译 <名称>：只做中↔英【名称翻译】。

    明确边界：wiki 页面本身还有获取方式、锻造材料等大量正文，机器人抓不到
    （两站 Cloudflare + 模板化页面），所以本指令不假装在查 wiki，只给名称对照
    和两个 wiki 链接，正文请用户自己点开看。对外只有「翻译」这一个指令，没有 wiki 指令。
    """
    name = (name or "").strip()
    if not name:
        return ("用法：翻译 <英文名或中文名>，例如  翻译 Boltor  /  翻译 螺钉步枪\n"
                "作用：只做名称的中↔英翻译；之后 WM/ZK 也能直接用中文名查价。\n"
                "说明：wiki 页面的获取方式、锻造材料等正文机器人抓不到，请点回复里的链接查看。\n"
                "翻译 同步：联网拉全量中英对照词库（一次即可，之后查询不再联网）。")
    if name.lower() in ("同步", "sync", "更新", "update"):
        ok, msg = sync_lexicon()
        return msg
    data = load_i18n()
    entries = data.setdefault("entries", {})
    key = name.lower()
    # 1) 先看本地缓存（内置 starter / 用户手加 / 已抓取 / 已同步的全量词库）
    v = lookup_i18n(name)
    if v:
        en = v.get("en") or name
        return _translate_reply("名称翻译", en, v.get("zh"), f"来源：{v.get('src')}")
    # 2) 本地没有 -> 走 wiki 的 MediaWiki /api.php 机器接口取名称（住宅 IP 通常能过，机房 IP 会被 Cloudflare 拦）
    res = _wiki_fetch(name)
    if res:
        entries[key] = {"en": res["en"], "zh": res["zh"], "src": "wiki-live"}
        save_i18n(data)
        return _translate_reply("名称翻译（wiki 接口抓取并缓存）",
                                res["en"] or name, res["zh"], "已写入 data/i18n.json")
    # 3) wiki 被挡 -> 兜底：首次自动联网同步全量词库（GitHub raw，无 Cloudflare，实测可达）
    if not _lexicon_path().exists():
        ok, msg = sync_lexicon()
        if ok:
            v = lookup_lexicon(name) or lookup_i18n(name)
            if v:
                return _translate_reply("已从词库联网查到并缓存",
                                        v.get("en") or name, v.get("zh"),
                                        "来源：WFA_Lexicon 中英对照词库")
    # 4) 全失败：如实说明，不编造中文名
    return (f"⚠️ 本地翻译表与词库都没有「{name}」，且 wiki 接口被站点浏览器验证（Cloudflare）挡住，"
            f"无法自动获取名称翻译。\n可试：翻译 同步（重拉全量词库）；或把中英文名告诉我加进 data/i18n.json；"
            f"或直接用 WM 英文名查询。\n（获取方式/材料等 wiki 正文机器人不提供，请点 wiki 链接查看）")

WIKI_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

def _wiki_fetch(name):
    """自动抓取 wiki 中英文对照。走灰机 MediaWiki 官方 /api.php 机器接口
    （正确做法：返回结构化 JSON，不是抓网页 HTML）；遵守同域名 3~5 秒节流。
    返回 {"en":..., "zh":...}（无官方中文名时 zh 为 None）；被挡/超时/不存在返回 None。
    流程：① 按名直查 langlinks；② 直查未命中（页面不存在）则走搜索接口找正确标题再查。
    说明：Cloudflare 会拦「机房 IP + 非浏览器」的请求（本沙箱即如此，返回 403）；
    在用户自家电脑/手机（住宅 IP）上通常能正常过，故保留为 live 兜底。"""
    try:
        r = _wiki_api_langlinks(name)
    except urllib.error.HTTPError:
        return None  # 被 Cloudflare 拦（机房 IP）
    if r is _WIKI_MISSING:
        # 直查没页面 -> 用搜索接口找近似标题（用户可能只记得碎片名，如 士顿/伯斯顿）
        cand = _wiki_api_search(name)
        if cand:
            try:
                r2 = _wiki_api_langlinks(cand)
            except urllib.error.HTTPError:
                return None
            if isinstance(r2, dict):
                return r2
        return None
    return r

_WIKI_MISSING = object()  # langlinks 查询里页面不存在的标记（区别于被挡）

def _wiki_api_langlinks(name):
    """查某标题的规范标题 + 英文 langlink。
    返回 dict {en,zh}；页面不存在返回 _WIKI_MISSING；被 Cloudflare 拦抛 HTTPError。"""
    pace_gate("warframe.huijiwiki.com")
    params = {
        "action": "query", "format": "json",
        "titles": name, "prop": "langlinks",
        "lllang": "en", "redirects": "1",
    }
    url = "https://warframe.huijiwiki.com/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=WIKI_HEADERS)
    with bot_config.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8", "ignore"))
    pages = (data.get("query") or {}).get("pages") or {}
    for pid, pg in pages.items():
        if pg.get("missing") is not None or "title" not in pg:
            return _WIKI_MISSING  # 页面不存在
        canonical = pg.get("title") or name
        en = None
        for ll in (pg.get("langlinks") or []):
            if ll.get("lang") == "en":
                en = ll.get("*")
                break
        if is_cjk(canonical):
            zh = canonical
            en = en or name
        else:
            en = canonical
            zh = None  # 战甲类：wiki 沿用英文名，无官方中文名
        return {"en": en, "zh": zh}
    return _WIKI_MISSING

def _wiki_api_search(name):
    """MediaWiki 搜索接口（list=search）：用近似/碎片名找真实页面标题。
    返回最佳候选标题字符串；被挡/无结果返回 None。"""
    try:
        pace_gate("warframe.huijiwiki.com")
        params = {
            "action": "query", "format": "json",
            "list": "search", "srsearch": name, "srlimit": "5",
        }
        url = "https://warframe.huijiwiki.com/api.php?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=WIKI_HEADERS)
        with bot_config.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        data = json.loads(raw.decode("utf-8", "ignore"))
        hits = (data.get("query") or {}).get("search") or []
        for h in hits:
            t = h.get("title")
            if t:
                return t
    except Exception:
        return None
    return None


# --------------------- 中英对照大词库（WFA_Lexicon / GitHub raw，无 Cloudflare）---------------------
# 背景：wiki 两站都启用了 Cloudflare，机房 IP / 非浏览器请求一律 403；
# 而 GitHub raw 对标准库 urllib 直连可达（实测 200）。故引入社区维护的
# WFA_Lexicon 中英对照词库作为「100% 能联网」的兜底源。
# 首次同步后写入 data/lexicon.json，之后运行时纯本地、零外网、手机可用。
# 可用环境变量 WM_LEXICON_BASE 换成 Gitee 等镜像（国内网络不稳时）。
def _lexicon_path():
    return DATA_DIR / "lexicon.json"

LEXICON_BASE = (os.environ.get("WM_LEXICON_BASE") or
                "https://raw.githubusercontent.com/Richasy/WFA_Lexicon/master").rstrip("/")
LEXICON_DOMAIN = "raw.githubusercontent.com"
_LEXICON_FILES = ("WF_Dict.json", "WF_Sale.json")
_LEXICON_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def _lexicon_fetch_json(fn):
    pace_gate(LEXICON_DOMAIN)   # 同域名 3~5 秒节流（与 wiki 分开计）
    url = LEXICON_BASE + "/" + fn
    req = urllib.request.Request(url, headers={
        "User-Agent": _LEXICON_UA, "Accept": "application/json, */*"})
    with bot_config.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8", "ignore"))

def _norm_en(s):
    s = re.sub(r"\s+", " ", (s or "").strip())
    # 词库部分条目是全大写英文（RHINO / ASH PRIME NEUROPTICS），规范成正常大小写再展示
    if s and s.isupper() and re.search(r"[A-Z]", s):
        s = s.title()
    return s

def _lexicon_build(dict_rows, sale_rows):
    """词库 -> {英文小写: {en, zh, src, slug?}}。
    规则（重要）：
      - 战甲（type=Warframe）在中文 wiki / 游戏内均无官方中文名，词库里的
        「犀牛/咖喱/毒妈」是社区俗称，一律不收，zh 置 None，避免编造中文名；
      - zh 与 en 相同（忽略大小写）视为「词库沿用英文」，同样不算中文名；
      - Dict 的中文名干净（螺钉步枪/海克），用作展示名；
      - Sale 带 search=WM 查询码，仅补 slug 与英文索引，中文名带「一套」等
        部件后缀不用于展示（其独有条目 zh 置 None）。
    """
    out = {}
    stat = {"warframe_no_zh": 0, "slugs": 0}
    for row in (dict_rows or []):
        en = _norm_en(row.get("en"))
        if not en:
            continue
        key = en.lower()
        typ = row.get("type") or ""
        zh = _norm_en(row.get("zh"))
        if typ == "Warframe":
            stat["warframe_no_zh"] += 1
            zh = ""
        elif zh and zh.lower() == en.lower():
            zh = ""
        if key not in out:
            out[key] = {"en": en, "zh": zh or None, "src": "wfa-lexicon"}
        elif zh and not out[key].get("zh"):
            out[key]["zh"] = zh
    for row in (sale_rows or []):
        en = _norm_en(row.get("en"))
        slug = _norm_en(row.get("search"))
        if not en:
            continue
        key = en.lower()
        if key in out:
            if slug and not out[key].get("slug"):
                out[key]["slug"] = slug
                stat["slugs"] += 1
        else:
            # Sale 独有条目：中文名含「一套」等后缀，不作为展示名，仅建英文/slug 索引
            out[key] = {"en": en, "zh": None, "src": "wfa-lexicon", "slug": slug or None}
            if slug:
                stat["slugs"] += 1
    return out, stat

def sync_lexicon():
    """联网拉全量词库并落盘。返回 (是否成功, 摘要文本)。"""
    try:
        d = _lexicon_fetch_json(_LEXICON_FILES[0])
        s = _lexicon_fetch_json(_LEXICON_FILES[1])   # 两次之间 pace_gate 真等 3~5s
    except Exception as e:
        return False, f"词库同步失败：{e}"
    entries, stat = _lexicon_build(d, s)
    if not entries:
        return False, "词库同步失败：返回数据为空"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_json(_lexicon_path(), {"entries": entries, "syncedAt": time.time(),
                             "source": LEXICON_BASE})
    # 同步结果合并进 i18n.json（不覆盖用户手工/已抓取的条目）
    data = load_i18n()
    mine = data.setdefault("entries", {})
    added = 0
    for k, v in entries.items():
        if k not in mine:
            mine[k] = {"en": v["en"], "zh": v.get("zh"), "src": "wfa-lexicon"}
            added += 1
    if added:
        save_i18n(data)
    return True, (f"✅ 词库同步完成：Dict {len(d)} 条 / Sale {len(s)} 条，"
                  f"生成 {len(entries)} 条对照，新增写入 {added} 条；"
                  f"战甲 {stat['warframe_no_zh']} 条按无中文名处理；"
                  f"已缓存到 data/lexicon.json 与 data/i18n.json，之后查询不再联网。")

def lookup_lexicon(token):
    """词库精确匹配（英文键 / 中文名 / WM slug），不做模糊，避免短词误命中。"""
    t = (token or "").strip().lower()
    if not t:
        return None
    entries = load_json(_lexicon_path(), {"entries": {}}).get("entries", {})
    if t in entries:
        return entries[t]
    for v in entries.values():
        zh = (v.get("zh") or "").lower()
        if zh and zh == t:
            return v
        slug = (v.get("slug") or "").lower()
        if slug and slug == t:
            return v
    return None


# --------------------- 名称 → slug（纯文本变换，不拉全站清单）---------------------
def slugify(name):
    """把用户输入的名称规范化成 WM 的 url_name，只做文本变换、不发任何请求。

    基础 slug 规则是「小写 + 非字母数字变下划线」：Boltor Prime -> boltor_prime。
    注意：基础 slug 不一定是可交易条目，实际请求见 item_slug_candidates() 的候选探测。
    因此不需要预先拉取 /v2/items 全量物品清单；缓存也只按用户实际查过的
    「类型:名称」建立（见 cache.json），不对全站物品做任何预取。
    """
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def item_slug(name):
    return slugify(name)


# 名称里已经带部件 / 套装词时，不再补 _set 后缀（如 "Boltor Prime Blueprint"）。
_PART_RE = re.compile(
    r"\b(?:set|blueprint|bp|systems|chassis|neuroptics|barrel|receiver|stock|"
    r"blade|handle|grip|hilt|guard|ornament|pouch|link|string|"
    r"upper_limb|lower_limb|head|disc|star|套装|全套)\b", re.I)


def item_slug_candidates(name):
    """只针对用户输入的这【一个】名称生成少量候选 slug，绝不拉全站物品清单。

    WM 上 Prime 战甲 / 武器的可交易条目是「套装 / 部件」，父名称
    （Boltor Prime -> boltor_prime）在订单接口里返回 404 app.item.notFound；
    真正有订单的是 boltor_prime_set。故按常见后缀做有限探测。
    每个候选都是一次独立 HTTP 请求，逐次经过 pace_gate 的 3~5 秒同域名节流。
    """
    base = item_slug(name)
    if not base:
        return []
    low = (name or "").lower()
    if _PART_RE.search(low):
        return [base]
    # Prime 系列最常见是整套，优先探测可让多数查询只发 1 个请求
    cands = [base + "_set", base] if "prime" in low else [base, base + "_set"]
    out = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def _save_slug(key, slug):
    """记住这次命中的 slug：以后同一物品直接发 1 个请求，不必再探测。"""
    m = load_json(SLUG_FILE, {})
    if m.get(key) != slug:
        m[key] = slug
        save_json(SLUG_FILE, m)


def riven_slug(name):
    # 紫卡按武器族归档，去掉 Prime / p 变体（Tigris Prime -> tigris）
    stripped = re.sub(r"\b(prime|p)\b", "", (name or "").lower(), flags=re.I)
    return slugify(stripped)


# --------------------- 卖家在线状态：排序 / 可联系性 ---------------------
# WM 返回的每条订单 / 拍卖都带卖家状态：ingame（游戏中）、online（在线）、offline（离线）。
# 离线卖家的报价常常是几个月前挂的，号可能早已 2035（封禁）或弃坑——价格看着低却根本买不到。
# 排序规则（用户要求）：游戏中 → 在线 → 离线；同组内仍按价格从低到高；
# 离线且超过 WM_OFFLINE_STALE_DAYS 天没登录的再沉到最后（基本等于联系不上）。
STATUS_RANK = {"ingame": 0, "online": 1, "offline": 2}
RANK_UNKNOWN = 3                  # 老缓存 / 缺字段：不特殊对待，仍按价格排
STATUS_MARK = {"ingame": "🟢", "online": "🔵", "offline": "⚪"}
STATUS_TEXT = {"ingame": "游戏中", "online": "在线", "offline": "离线"}
OFFLINE_STALE_DAYS = _env_int("WM_OFFLINE_STALE_DAYS", 30)

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2}):(\d{2})")


def _iso_ts(s):
    """WM 的 ISO 时间（...Z / +00:00 / 带毫秒）→ epoch 秒；解析不了返回 None。"""
    m = _ISO_RE.match(str(s or "").strip())
    if not m:
        return None
    try:
        y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
        return calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0))
    except (ValueError, OverflowError):
        return None


def _status_info(status, last_seen):
    """统一卖家状态：rank 越小越靠前；stale=True 表示离线且太久没登录（多半买不到）。"""
    st = (status or "").strip().lower()
    if st not in STATUS_RANK:
        st = ""
    ts = _iso_ts(last_seen)
    days = None
    if ts is not None:
        days = max(0, int((time.time() - ts) // 86400))
    stale = bool(st == "offline" and days is not None
                 and OFFLINE_STALE_DAYS > 0 and days >= OFFLINE_STALE_DAYS)
    return {"status": st, "rank": STATUS_RANK.get(st, RANK_UNKNOWN),
            "lastSeenTs": ts, "days": days, "stale": stale}


def _order_sort_key(r):
    # 先按「现在能不能联系上」分组，组内价格从低到高；离线很久没登录的沉底。
    # 紫卡里没有一口价、只有起拍价的（bidOnly）排在同组后面：起拍 1 铂金不是能直接买到的价。
    return (r.get("rank", RANK_UNKNOWN), 1 if r.get("stale") else 0,
            1 if r.get("bidOnly") else 0, r.get("price") or 0)


def _finish_rows(rows):
    """统一排序 + 汇总。

    返回 (rows, stats, samples, basis)：
      stats   各状态条数（给回复里的概览行用）；
      samples 计算代表价 / 中位数用的样本：优先「现在就能交易」的报价，
              一个都没有才退回全部报价（否则最低价永远来自几个月没上线的号）；
      basis   "reachable" 表示代表价来自在线卖家，"all" 表示全离线仅供参照。
    """
    rows.sort(key=_order_sort_key)
    stats = {"ingame": 0, "online": 0, "offline": 0, "unknown": 0, "stale": 0}
    for r in rows:
        st = r.get("status") or "unknown"
        stats[st] = stats.get(st, 0) + 1
        if r.get("stale"):
            stats["stale"] += 1
    reachable = [r for r in rows if r.get("rank", RANK_UNKNOWN) <= 1 and not r.get("stale")]
    return rows, stats, (reachable or rows), ("reachable" if reachable else "all")


# ----------------------------- 取数 -----------------------------
def fetch_item(name):
    if SOURCE == "mock":
        pace_gate("api.warframe.market")   # 离线演示也遵守同域名 3~5 秒礼貌间隔（演示真实行为）
        return mock_fetch("item", name)

    key = f"item:{name.lower()}"
    cached = load_json(SLUG_FILE, {}).get(key)
    cands = []
    if cached:
        cands.append(cached)              # 上次命中的 slug 优先，通常 1 个请求就够
    for s in item_slug_candidates(name):
        if s not in cands:
            cands.append(s)
    if not cands:
        raise ItemNotFound(f"「{name}」不是有效英文物品名，请用 WM 英文名（如 Boltor Prime Set）")

    tried = []
    for slug in cands:
        tried.append(slug)
        _log(f"尝试订单 slug={slug}（{len(tried)}/{len(cands)}）")
        try:
            # 只查用户要的这 1 个物品；http_get 内每次都先过 3~5 秒节流
            j = http_get(f"/v2/orders/item/{slug}")
        except ItemNotFound:
            continue                       # 仅「明确物品不存在」才试下一个候选；
                                           # 超时 / 5xx / 429 一律立即失败，绝不继续打源
        orders = [o for o in (j.get("data") or []) if o.get("type") == "sell"]
        _save_slug(key, slug)
        if not orders:
            raise RuntimeError(f"「{name}」(slug={slug}) 当前无在售订单")
        rows = []
        for o in orders:
            price = o.get("platinum")
            if price is None:
                continue
            u = o.get("user") or {}
            row = {
                "seller": u.get("ingameName", "?"),
                "price": price,
                "quantity": o.get("quantity", 1),
            }
            row.update(_status_info(u.get("status"), u.get("lastSeen")))
            rows.append(row)
        if not rows:
            raise RuntimeError(f"「{name}」(slug={slug}) 订单无有效价格")
        rows, stats, samples, basis = _finish_rows(rows)
        prices = [r["price"] for r in samples]
        _log(f"slug 命中 {slug}，已记入局部缓存（下次只发 1 个请求）")
        return {
            "price": prices[0],
            "median": int(statistics.median(prices)),
            "unit": "铂金",
            "count": len(rows),
            "orders": rows,
            "stats": stats,
            "priceBasis": basis,
            "url": f"https://warframe.market/items/{slug}",   # WM 网页（具体物品页）
        }
    raise ItemNotFound(
        f"「{name}」在 WM 找不到对应可交易物品（已试：{', '.join(tried)}）。"
        "请用 WM 上的完整物品名，例如 Boltor Prime Set / Boltor Prime Blueprint / Saryn Prime Systems")


def fetch_riven(name):
    if SOURCE == "mock":
        pace_gate("api.warframe.market")   # 离线演示也遵守同域名 3~5 秒礼貌间隔（演示真实行为）
        return mock_fetch("riven", name)
    slug = riven_slug(name)
    if not slug:
        raise RuntimeError(f"「{name}」不是有效英文武器名，请用 WM 英文武器名（如 Tigris）")
    j = http_get("/v1/auctions/search", {"type": "riven", "weapon_url_name": slug})
    auctions = j.get("payload", {}).get("auctions") or []
    if not auctions:
        raise RuntimeError(f"「{name}」当前无紫卡拍卖")
    rows = []
    for a in auctions:
        if a.get("closed"):          # 已流拍的拍卖挂着也没意义，直接跳过
            continue
        buyout = a.get("buyout_price")
        price = buyout if buyout is not None else a.get("starting_price")
        if price is None:
            continue
        ow = a.get("owner") or {}
        row = {
            "seller": ow.get("ingame_name", "?"),
            "price": price,
            "quantity": a.get("quantity", 1),
            "bidOnly": buyout is None,      # 只有起拍价、没有一口价：不是能直接买到的价
        }
        row.update(_status_info(ow.get("status"), ow.get("last_seen")))
        rows.append(row)
    if not rows:
        raise RuntimeError(f"「{name}」拍卖无有效价格")
    rows, stats, samples, basis = _finish_rows(rows)
    prices = [r["price"] for r in samples]
    return {
        "price": prices[0],
        "median": int(statistics.median(prices)),
        "unit": "铂金",
        "count": len(rows),
        "orders": rows,
        "stats": stats,
        "priceBasis": basis,
        "url": f"https://warframe.market/auctions/riven/{slug}",   # WM 网页（紫卡拍卖页）
    }


# --------------------- 后台刷新状态锁（0/1 文件） ---------------------
def lock_path(key):
    safe = "".join(c if c.isalnum() else "_" for c in key.lower())
    return DATA_DIR / f"bg_refresh_{safe}.lock"


def acquire_lock(key):
    """原子获取锁：内容非 1 则写 1 返回 True；已是 1 返回 False（已有刷新在跑）。"""
    p = lock_path(key)
    try:
        if p.read_text(encoding="utf-8").strip() == "1":
            return False
    except Exception:
        pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text("1", encoding="utf-8")
    return True


def release_lock(key):
    try:
        lock_path(key).write_text("0", encoding="utf-8")
    except Exception:
        pass


def reset_all_locks():
    """进程启动时调用：清掉上次可能崩溃残留的 1（新进程没有任何后台刷新在跑）。"""
    try:
        if DATA_DIR.exists():
            for f in DATA_DIR.glob("bg_refresh_*.lock"):
                f.write_text("0", encoding="utf-8")
    except Exception:
        pass


# ----------------------------- 刷新 -----------------------------
def _write_cache(type_, name, result):
    """把一次成功的取数写进本地缓存（只写用户实际查过的「类型:名称」）。"""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # 来源显示为 WM 完整网址（具体物品/紫卡页面），mock 模式给本地数据标记
    src = result.get("url") or ("https://warframe.market" if SOURCE == "warframe"
                                else f"mock（{name} 本地测试数据）")
    entry = {
        "type": type_, "name": name, "zh": translate_en_to_zh(name),
        "data": result, "source": src,
        "lastUpdated": now_iso, "ts": int(time.time() * 1000), "status": "ok",
    }
    c = load_json(CACHE_FILE, {"entries": {}})
    c.setdefault("entries", {})[f"{type_}:{name.lower()}"] = entry
    save_json(CACHE_FILE, c)
    return {"served": "fresh", "entry": entry, "fresh": True}


def refresh_once(type_, name):
    """单次取数（不重试）：成功写缓存返回 fresh；失败返回 error。

    每次取数都经过 pace_gate 的 3~5 秒同域名节流（真实源与 mock 都一样）。
    失败时绝不写缓存、绝不拿旧数据顶包。
    """
    fetch = fetch_item if type_ == "item" else fetch_riven
    t0 = time.time()
    try:
        result = fetch(name)
        if not (result and (result.get("price") is not None or result.get("orders"))):
            raise RuntimeError("源站返回空数据")
        _log(f"刷新成功 type={type_} name={name} 耗时={time.time()-t0:.1f}s，已写入缓存")
        return _write_cache(type_, name, result)
    except ItemNotFound as e:
        _log(f"刷新失败（确定性：物品名不存在）type={type_} name={name} 原因={e}")
        return {"served": "error", "entry": None, "error": True,
                "errorMsg": str(e), "retrying": False, "permanent": True}
    except Exception as e:
        _log(f"刷新失败 type={type_} name={name} 耗时={time.time()-t0:.1f}s 原因={e}")
        return {"served": "error", "entry": None, "error": True,
                "errorMsg": str(e), "retrying": False}


def do_refresh(type_, name):
    """CLI 用：阻塞跑满 1/3/7 分钟重试（最多 4 次），人工等得起；机器人不调这个。"""
    last = None
    for i in range(MAX_ATTEMPTS):
        if i > 0:
            w = MICRO_WAITS[i - 1] if i - 1 < len(MICRO_WAITS) else 60
            _log(f"第 {i + 1}/{MAX_ATTEMPTS} 次重试前等待 {w}s")
            time.sleep(w)
        r = refresh_once(type_, name)
        if r.get("served") == "fresh" or r.get("permanent"):
            return r          # 物品名不对，重试 4 次也还是 404，别浪费同域名请求
        last = r
    return last or {"served": "error", "entry": None, "error": True, "errorMsg": "无数据"}


def retry_in_background(type_, name):
    """首次刷新失败后的补救：后台按 1/3/7 分钟继续重试（累计最多 4 次）。

    用户线程立即拿到明确报错，不会假死 11 分钟、也不会错过 QQ 被动回复的 5 分钟窗口；
    后台每次取数仍走 3~5 秒节流，成功后写缓存，用户稍后重发同一条指令即可秒回最新数据。
    0/1 状态锁防止同一物品被重复刷新。
    """
    key = f"{type_}:{name.lower()}"
    if not acquire_lock(key):
        _log(f"已有后台重试在跑，跳过: {key}")
        return False

    def run():
        try:
            for i in range(1, MAX_ATTEMPTS):
                w = MICRO_WAITS[i - 1] if i - 1 < len(MICRO_WAITS) else 60
                _log(f"后台重试 {i + 1}/{MAX_ATTEMPTS}：先等 {w}s（{key}）")
                time.sleep(w)
                if refresh_once(type_, name).get("served") == "fresh":
                    _log(f"后台重试成功，缓存已更新: {key}")
                    return
            _log(f"后台 {MAX_ATTEMPTS} 次全失败，{int(MACRO_WAIT_MS / 1000)}s 内不再重试: {key}")
        finally:
            release_lock(key)

    threading.Thread(target=run, daemon=True, name="wm-retry").start()
    return True


def schedule_refresh(type_, name):
    """[已停用] 早期版本的后台异步刷新，查询链路已不再调用。

    现在「刷新失败后的 1/3/7 分钟重试」走 retry_in_background()；
    0/1 状态锁与 reset_all_locks 仍被它使用，这里只保留兼容入口。
    """
    key = f"{type_}:{name.lower()}"
    if not acquire_lock(key):
        print(f"[wm] 跳过后台刷新（状态锁=1，已有刷新在运行）: {key}", file=sys.stderr)
        return

    def run():
        try:
            do_refresh(type_, name)
        except Exception:
            pass
        finally:
            release_lock(key)

    t = threading.Thread(target=run, daemon=True)
    t.start()


# ----------------------------- 查询 -----------------------------
def query(text_or_cmd, force=False):
    """统一查询入口。
    - 传字符串：按聊天指令解析（WM/ZK/物品/紫卡/报错）；
    - 传 dict {type,name}：直接查。
    返回 result dict（served: fresh/cache/error/help）。
    """
    if isinstance(text_or_cmd, dict):
        type_ = text_or_cmd["type"]
        name = text_or_cmd["name"]
        zh_disp = text_or_cmd.get("zh")
        if type_ == "translate":
            return {"served": "translate", "text": translate_command(name)}
    else:
        cmd = parse_command(text_or_cmd)
        if not cmd:
            return {"served": "help"}
        type_ = cmd["type"]
        name = cmd["name"]
        force = bool(force) or bool(cmd.get("force"))
        zh_disp = None

    # 中文输入 -> 翻成英文再去 WM 查（我们始终以英文查 WM）
    en_name, zh_disp, err = resolve_query_name(name)
    if err:
        return {"served": "error", "entry": None, "error": True,
                "errorMsg": err, "retrying": False, "permanent": True}
    name = en_name

    key = f"{type_}:{name.lower()}"
    c = load_json(CACHE_FILE, {"entries": {}})
    entry = c.get("entries", {}).get(key)
    now = time.time() * 1000
    has_data = _entry_is_real(entry)
    age = (now - entry.get("ts", 0)) if has_data else float("inf")
    # 本机时钟早于缓存时间（换设备 / 时钟回拨）：age 为负，按过期处理，
    # 绝不把旧机器上的缓存当「新鲜数据」秒回给用户。
    if age < 0:
        age = STALE_MS
    stale = age >= STALE_MS

    if force or not has_data or stale:
        # 超间隔 / 强制 / 无数据：阻塞刷新，先更新缓存再回复（不发旧缓存）
        result = do_refresh(type_, name)
    else:
        result = {"served": "cache", "entry": entry, "fresh": False, "stale": False}
    result["en"] = name
    result["zh"] = zh_disp
    return result



def get_cached_or_refresh(type_, name, force=False):
    """机器人统一入口：只在用户发起查询时更新。

    - 缓存未超过 WM_CACHE_MINUTES：直接回缓存（不联网）；
    - 超间隔 / 无缓存 / /报错 强制：先做一次联网刷新，成功写缓存后再回复，
      绝不把几天、几个月前的旧缓存当结果发给用户；
    - 刷新失败：立即返回明确报错（不发旧数据），1/3/7 分钟重试交给后台线程，
      避免真实源失败时主线程卡 11 分钟、错过 QQ 被动回复的 5 分钟窗口。
    """
    # 中文输入 -> 翻成英文再查（始终以英文查 WM）；纯英文 -> 取中文展示名
    en_name, zh_disp, err = resolve_query_name(name)
    if err:
        return {"served": "error", "entry": None, "error": True,
                "errorMsg": err, "retrying": False, "permanent": True}
    name = en_name
    key = f"{type_}:{name.lower()}"
    c = load_json(CACHE_FILE, {"entries": {}})
    entry = c.get("entries", {}).get(key)
    now = time.time() * 1000
    has_data = _entry_is_real(entry)
    age = (now - entry.get("ts", 0)) if has_data else float("inf")
    # 换到手机后若本机时钟早于缓存时间戳，age 为负 -> 强制联网刷新，
    # 不能因为「负数 < 间隔」就把几天前的旧缓存发出去。
    if age < 0:
        age = STALE_MS
    if has_data and age < STALE_MS and not force:
        _log(f"缓存命中 {key} 距今={age/60000:.1f}分钟 < {CACHE_MINUTES}分钟，直接回复（不联网）")
        r = {"served": "cache", "entry": entry, "fresh": False, "stale": False}
        r["en"] = name
        r["zh"] = zh_disp
        return r
    if not has_data:
        reason = "无缓存"
    elif force:
        reason = "用户 /报错 强制刷新"
    else:
        reason = f"缓存过期（距今 {age/60000:.1f}分钟 >= {CACHE_MINUTES}分钟）"
    _log(f"{key} {reason}，先联网刷新缓存再回复")
    r = refresh_once(type_, name)
    if r.get("served") == "fresh":
        r["en"] = name
        r["zh"] = zh_disp
        return r
    if r.get("permanent"):
        r["retrying"] = False   # 确定性错误：不启动 1/3/7 分钟后台重试
        _log(f"{key} 物品名不存在，直接提示用户改名，不安排后台重试")
        r["en"] = name
        r["zh"] = zh_disp
        return r
    r["retrying"] = retry_in_background(type_, name)
    r["en"] = name
    r["zh"] = zh_disp
    return r


# 兼容旧引用名；语义已改为「超间隔先刷新缓存再回复，失败立即报错、重试交后台」。
get_cached_or_schedule = get_cached_or_refresh


# ----------------------------- 指令解析 -----------------------------
def parse_command(text):
    t = (text or "").strip()
    force = False
    low = t.lower()
    if low.startswith("/报错") or low.startswith("/report"):
        force = True
        rest = t[3:].strip()
    else:
        rest = t
    rest = rest.lstrip("/").strip()
    parts = rest.split(None, 1)
    if not parts:
        return None
    head = parts[0].lower()
    name = parts[1].strip() if len(parts) > 1 else ""
    if head in ("zk", "紫卡", "riven", "card"):
        type_ = "riven"
    elif head in ("wm", "物品", "item"):
        type_ = "item"
    elif head in ("翻译", "translate", "trans"):
        # 翻译 <名称>：只做中↔英名称翻译
        return {"type": "translate", "name": name, "force": force}
    else:
        # 没识别到类型词：整段当作物品名（默认物品）
        type_ = "item"
        name = rest
    for p in ("紫卡", "riven", "card", "物品", "item"):
        name = re.sub(rf"^{p}\s*", "", name, flags=re.I).strip()
    if not name:
        return None
    return {"type": type_, "name": name, "force": force}


# 卖家状态 → 卡片里「昵称」的颜色（用户要求：游戏中红、在线蓝、离线灰）。
# 纯文字回复不受影响，只有白底卡片渲染时才用它给昵称上色。
STATUS_RGB = {
    "ingame": (198, 40, 40),      # 游戏中 → 红色昵称
    "online": (32, 92, 180),      # 在线 → 蓝色昵称
    "offline": (132, 136, 146),   # 离线 → 灰色昵称
}

# ----------------------------- 回复格式化 -----------------------------


def _reply_en_zh(result, entry):
    en = result.get("en") or entry.get("name") or ""
    zh = result.get("zh") or entry.get("zh") or translate_en_to_zh(en)
    return en, zh


def _body_lines(result, entry):
    """回复正文（不含「发起聊天」/w 指令），返回行列表。"""
    label = "物品" if entry["type"] == "item" else "紫卡"
    en, zh = _reply_en_zh(result, entry)
    # 第一条：英文名称 + / 中文名称（有中文名才加 /，战甲等国服沿用英文名时只显示英文）
    name_line = f"【{label}】{en}" + (f" / {zh}" if zh and zh != en else "")
    d = entry["data"]
    orders = d.get("orders", [])
    st = d.get("stats") or {}
    lines = [name_line]
    head = f"WM 在售（共 {d.get('count', len(orders))} 条"
    bits = [f"{STATUS_MARK[k]}{STATUS_TEXT[k]} {st[k]}"
            for k in ("ingame", "online", "offline") if st.get(k)]
    if bits:
        head += "：" + " · ".join(bits)
        if st.get("stale"):
            head += f"，其中 {st['stale']} 个 {OFFLINE_STALE_DAYS} 天没登录"
    lines.append(head + "）")
    if bits:
        lines.append("排序：🟢游戏中 → 🔵在线 → ⚪离线（同组内价格从低到高；离线太久没登录的沉底）")
        lines.append("昵称颜色：游戏中=红 · 在线=蓝 · 离线=灰｜行首 [游戏中]/[在线]/[离线] 是状态标签")
    if d.get("priceBasis") == "all" and st and not (st.get("ingame") or st.get("online")):
        lines.append("⚠️ 现在没有游戏中/在线的卖家，以下全是离线报价，多半联系不上，仅供参考。")
    for i, o in enumerate(orders[:15], 1):
        mark = STATUS_MARK.get(o.get("status") or "", "")
        pre = (mark + " ") if mark else ""
        extra = []
        if o.get("bidOnly"):
            extra.append("仅竞价")
        if o.get("status") == "offline" and o.get("days") is not None:
            extra.append("今天在线过" if o["days"] < 1 else f"{o['days']}天前在线")
        tail = (" · " + " · ".join(extra)) if extra else ""
        lines.append(f"{i}. {pre}{o['seller']} — {o['price']} 铂金 ×{o.get('quantity', 1)}{tail}")
    if len(orders) > 15:
        rest = orders[15:]
        off = sum(1 for r in rest if r.get("status") == "offline")
        # 「最低价」只算能直接买到的（有一口价的）；全是竞价时才退回起拍价
        cand = [r["price"] for r in rest if not r.get("bidOnly")] or [r["price"] for r in rest]
        line = f"（其余 {len(rest)} 条未列出，最低 {min(cand)} 铂金"
        line += "，多为离线卖家，价格低但多半联系不上）" if off * 2 >= len(rest) \
            else "，其中也有在线卖家）"
        lines.append(line)
    ts = entry.get("ts", 0)
    mins = max(0, int((time.time() * 1000 - ts) / 60000)) if ts else 0
    hm = f"{mins // 60}小时{mins % 60}分" if mins >= 60 else f"{mins}分钟"
    src = entry.get("source", "")
    if not src:
        src = "https://warframe.market"   # 旧缓存没有来源字段时才兜底
    # 非 http 的 source（如 mock 本地测试数据）如实显示，绝不伪装成真实网址
    lines.append(f"🕒 数据更新于 {entry.get('lastUpdated', '')}（距今约 {hm}）· 来源：{src}")
    return lines


def _whisper_lines(entry, en, zh):
    """英中两条 /w 发起聊天指令（不带任何来源备注 / 后缀）。

    用第 1 家卖家（排序后最优先、能直接交易的那个）；想买别家把 ID 换掉即可。
    """
    orders = ((entry or {}).get("data") or {}).get("orders") or []
    if not orders:
        return []
    top = orders[0]
    seller = top.get("seller", "")
    price = top.get("price", "")
    zh_name = zh if (zh and zh != en) else en
    return [
        f"英文：/w {seller} Hi! I want to buy: {en} for {price} platinum.",
        f"中文：/w {seller} 你好，我想购买 {zh_name} {price} 铂金.",
    ]


def _whisper_block(entry, en, zh):
    """纯文字回复里的完整「游戏内发起交易」区块（说明标题 + 英中指令）。"""
    lines = _whisper_lines(entry, en, zh)
    if not lines:
        return []
    d = (entry or {}).get("data") or {}
    top = d["orders"][0]
    who = STATUS_TEXT.get(top.get("status") or "", "")
    head2 = "游戏内发起交易（用第 1 家"
    if who:
        head2 += f"：{STATUS_MARK.get(top.get('status'), '')}{who}"
    head2 += "；想买别家把 ID 换掉即可）："
    return [head2] + lines


def build_whisper(result):
    """随卡片图同发的可复制字符串：只有英中两条 /w 指令。

    不压进卡片图（图里的字选不中、复制不了），由传输层与图片合成一条消息
    （官方富媒体图文混排 / OneBot 段数组），也不带 (wf.m ...) 之类的来源备注。
    """
    entry = result.get("entry")
    if not entry:
        return ""
    en, zh = _reply_en_zh(result, entry)
    return "\n".join(_whisper_lines(entry, en, zh))


def seller_colors(result):
    """返回 {卖家昵称: RGB}，供白底卡片给昵称按在线状态上色。

    游戏中 → 红，在线 → 蓝，离线 → 灰。纯文字回复不受影响。
    只覆盖实际列进回复的前 15 条，避免长列表里误伤同名文本。
    """
    entry = result.get("entry")
    if not entry or result.get("served") in ("translate", "help", "error"):
        return {}
    out = {}
    for o in ((entry.get("data") or {}).get("orders") or [])[:15]:
        col = STATUS_RGB.get((o.get("status") or "").strip().lower())
        seller = o.get("seller")
        if col and seller:
            out[seller] = col
    return out


def reply_body(result):
    """返回卡片正文（不含「游戏内发起交易」区块），供白底图片渲染。

    /w 指令改由 build_whisper 作为随图同发的可复制文字（不进图片），能长按复制。
    翻译 / 帮助 / 报错 / 无数据等非行情分支没有 /w 指令，直接复用 format_reply 全文。
    """
    entry = result.get("entry")
    if not entry or result.get("served") in ("translate", "help", "error"):
        return format_reply(result)
    return "\n".join(_body_lines(result, entry))


def format_reply(result):
    if result.get("served") == "translate":
        return result.get("text", "")
    if result.get("served") == "help":
        return (
            "指令格式（群内交互查询需带前缀 dxx ，防群聊误触发）：\n"
            "  dxx WM <物品名>      查物品（WM 英文名如 dxx WM Boltor Prime；也支持中文名）\n"
            "  dxx ZK <武器名>      查紫卡 Riven（WM 英文名如 dxx ZK Tigris）\n"
            "  dxx 翻译 <名称>      中↔英名称翻译（如 dxx 翻译 螺钉步枪）\n"
            "  dxx 翻译 同步        联网拉全量中英对照词库（一次即可，之后查询不再联网）\n"
            "  /dxx 报错 WM|ZK <名称>  数据有误，强制再查一次\n"
            "  注：世界状态查询（平原/警报/裂缝/计划）、监控开关、自检同样需 dxx  前缀\n"
            "      （@机器人 时不用前缀，直接发指令即可；回复和监控推送都会 @ 你）\n"
            "      （「帮助」是例外：直接发帮助两个字就能看这份菜单）；\n"
            "      只有「机器人主动推送」的外发监控消息不受此限制；\n"
            "      wiki 页面的获取方式/锻造材料等正文机器人抓不到，回复里会给两个 wiki 链接，请自己点开看。"
        )
    if result.get("served") == "error":
        if result.get("permanent"):
            # 物品名不对：说清怎么改，不要伪装成「暂时查不到」、也别让人空等后台重试
            return f"⚠️ {result.get('errorMsg', '物品名无法识别')}"
        tip = ("已在后台按 1/3/7 分钟自动重试，稍后重发同一条指令即可拿到最新结果。"
               if result.get('retrying') else "请稍后重试，或发送 /报错 强制再查一次。")
        return f"⚠️ 暂时查不到最新数据（{result.get('errorMsg', '未知错误')}）。{tip}"
    entry = result.get("entry")
    if not entry:
        return "（无数据）"
    en, zh = _reply_en_zh(result, entry)
    lines = _body_lines(result, entry)
    lines += _whisper_block(entry, en, zh)
    return "\n".join(lines)


# ----------------------------- CLI -----------------------------
def main():
    reset_all_locks()
    if len(sys.argv) < 2:
        print(format_reply({"served": "help"}))
        return
    text = " ".join(sys.argv[1:])
    result = query(text)
    print(format_reply(result))


if __name__ == "__main__":
    main()
