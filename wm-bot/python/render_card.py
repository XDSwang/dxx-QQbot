#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_card.py — 把世界状态文本渲染成白底卡片图片（PNG）

为什么用 Pillow 而不是 OpenCV：
  cv2.putText 走 Hershey 矢量字，OpenCV 4.x 下中文全变 "?"；5.0 虽支持中文但字体
  内置不可换、emoji 仍然全是 "?"。Pillow + 系统 TTF 才能既画中文又控字重字号。

设计要点：
  - 输入就是 worldstate 里已经排好的文本（不重复实现业务格式化），这里只负责版式；
  - 无 Pillow / 找不到中文字体时 render() 返回 None，调用方原样发文本，绝不报错；
  - 单栏放不下自动双栏，再放不下才分页（每页独立 PNG）；
  - 图上必须带更新时间：图片是快照，用户要能判断新旧。
"""

import os
import re
import sys
import time
import platform
import ctypes
from pathlib import Path


def _preload_raqm():
    """Android / QPython 等环境里，Pillow 的 _imagingft 在加载字体时会需要原生库
    libraqm.so，但运行环境常没装（报错 'library libraqm.so not found'）。
    在导入 PIL 前，把设备上可能存在的 libraqm.so 先用 ctypes 加载进来，
    这样后续 _imagingft 的 DT_NEEDED 依赖就能被「已加载的库」满足，字体即可正常加载。
    找不到就跳过——本函数绝不抛错，也不影响 Windows / macOS 正常环境。
    """
    if platform.system().lower() in ("windows", "darwin"):
        return
    cands = [
        str(Path(__file__).resolve().parent / "libs" / "libraqm.so"),  # 项目自带（用户放进来即可）
        os.environ.get("PREFIX", "") + "/lib/libraqm.so",              # Termux
        "/data/data/com.termux/files/usr/lib/libraqm.so",
        "/system/lib64/libraqm.so", "/vendor/lib64/libraqm.so",
        "/system/lib/libraqm.so",
    ]
    for c in cands:
        if c and os.path.exists(c):
            try:
                ctypes.CDLL(c)
                break
            except Exception:
                pass


_preload_raqm()

try:                                     # Pillow 是可选依赖，缺失时整个模块降级
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception:                        # pragma: no cover - 环境相关
    _PIL_OK = False

# ------------------------------ 版式常量 ------------------------------
W_SINGLE = 1000        # 单栏画布宽
W_DOUBLE = 1240        # 双栏画布宽（更宽，每列才不会太窄）
PAD = 44               # 四周留白
MAX_H = 2600           # 单张图最大高度，超过就改双栏
COL_GAP = 40           # 双栏间距
LINE_H = 34            # 正文行高
SEC_GAP = 26           # 分组之间的间距
BODY_SIZE = 21
SEC_SIZE = 25
TITLE_SIZE = 32
RIGHT_SIZE = 18
INK = (28, 30, 36)
MUT = (132, 136, 146)
HAIR = (226, 228, 234)
# 分组标题左侧色条，按顺序取色，纯装饰但让分区一眼可辨
PALETTE = [(198, 40, 40), (32, 92, 180), (24, 128, 96), (176, 102, 10),
           (108, 60, 160), (16, 116, 140), (150, 50, 96)]

# 字体候选：按「粗体, 常规」成对给出，找到第一对能同时加载的即用
_FONT_PAIRS = [
    ("msyhbd.ttc", "msyh.ttc"),                      # Windows 微软雅黑
    ("msyhbd.ttf", "msyh.ttf"),
    ("Dengb.ttf", "Deng.ttf"),                       # Windows 等线
    ("simhei.ttf", "simhei.ttf"),                    # Windows 黑体
    ("SourceHanSansSC-Bold.otf", "SourceHanSansSC-Regular.otf"),
    ("NotoSansCJKsc-Bold.otf", "NotoSansCJKsc-Regular.otf"),
    ("NotoSansCJK-Bold.ttc", "NotoSansCJK-Regular.ttc"),
    ("NotoSansSC-Bold.otf", "NotoSansSC-Regular.otf"),
    ("PingFang.ttc", "PingFang.ttc"),                # macOS
    ("Hiragino Sans GB.ttc", "Hiragino Sans GB.ttc"),
    ("wqy-zenhei.ttc", "wqy-zenhei.ttc"),            # Linux 文泉驿
    ("NotoSansMonoCJKsc-Regular.otf", "NotoSansMonoCJKsc-Regular.otf"),
    # 手机 / Termux / Android 常见字体名（Termux 装 noto-cjk-fonts 后就是这些）
    ("NotoSansCJK-Bold.ttc", "NotoSansCJK-Regular.ttc"),
    ("NotoSansCJKsc-Bold.otf", "NotoSansCJKsc-Regular.otf"),
    ("NotoSansSC-Bold.otf", "NotoSansSC-Regular.otf"),
    ("DroidSansFallback.ttf", "DroidSansFallback.ttf"),
]

# ------------------------------ 运行平台判断 ------------------------------
# 跨平台核心：用 Python 标准库判定当前 OS，路径统一用 pathlib 自动适配分隔符，
# 绝不在代码里写死 Windows 盘符、也不假定某一种分隔符（Windows \ / Termux / 古老 / 等）。
_SYS = (platform.system() or "").lower()          # windows / linux / darwin / android ...
_IS_TERMUX = bool(os.environ.get("TERMUX_VERSION")) \
    or "/com.termux/" in (os.environ.get("PREFIX", "") or "") \
    or Path("/data/data/com.termux").exists()
_PREFIX = Path(os.environ.get("PREFIX") or "/data/data/com.termux/files/usr")


def _system_font_dirs():
    """按当前平台给出系统字体目录（字符串列表）。全部用 Path 拼，跨 Windows/Termux/Linux/macOS。

    关键点：不同系统字体目录差异很大，绝不能把所有目录堆在一起后靠 Path 容错——
    例如在手机端列出 Windows 盘符路径虽不报错但永不可达；这里按 _SYS 精确分支。
    """
    home = Path.home()
    if "windows" in _SYS:
        return [str(Path("C:/Windows/Fonts")),
                str(home / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts")]
    if "darwin" in _SYS:
        return [str(Path("/System/Library/Fonts")),
                str(Path("/Library/Fonts")),
                str(home / "Library" / "Fonts")]
    # Linux / Android / Termux / 其它 POSIX
    dirs = [str(Path("/usr/share/fonts")),
            str(Path("/usr/local/share/fonts")),
            str(Path("/usr/share/fonts/truetype")),
            str(home / ".fonts"),
            str(home / ".local" / "share" / "fonts")]
    if _IS_TERMUX:
        dirs += [str(_PREFIX / "share" / "fonts"),
                 str(_PREFIX / "share" / "fonts" / "noto"),
                 str(_PREFIX / "etc" / "fonts")]
    dirs += [str(Path("/system/fonts")), str(Path("/system/fonts/noto"))]
    return dirs


# 系统字体目录（按平台自动选）；便携的 python/fonts 由 _portable_font_dirs 单独处理
_FONT_DIRS = _system_font_dirs()

_font_cache = {}
_font_ready = None      # None=还没探测；False=没有可用中文字体


def _portable_font_dirs():
    """唯一默认字体目录：脚本所在目录（python/）下的 fonts/。

    不依赖运行目录(CWD)，也不扫项目根——避免 PC→手机同步后多路径混乱与重复显示。
    字体随 python/ 同步到手机，天然跟着代码走；高级用户可用 WM_CARD_FONT 环境变量覆盖。
    """
    here = Path(__file__).resolve().parent
    return [here / "fonts"]


# 项目内唯一含敏感数据的目录（密钥 / 群白名单 / 缓存 / 日志），绝不外泄。
# 自检要打印检索路径便于排障，但凡出现这个绝对路径就脱敏，项目其余路径照常可见。
_DATA_DIR_STR = str(Path(__file__).resolve().parent / "data")


def _safe(p):
    """打印路径前把 data/ 真实绝对路径脱敏，避免泄露敏感目录。"""
    s = str(p)
    if _DATA_DIR_STR and _DATA_DIR_STR in s:
        rel = s[len(_DATA_DIR_STR):].lstrip("/\\")
        return "<项目>/data%s（已隐藏）" % ("/" + rel if rel else "")
    return s


_env_font_warn = ""   # 跨平台路径告警（Windows 盘符路径误用在手机端时填充），供 diagnose 显示


def _env_font_path(key):
    """读 WM_CARD_FONT / WM_CARD_FONT_BOLD 并规整。

    若在非 Windows 平台发现 Windows 风格盘符（如 `C:\\...` 或 `C:/...`）或反斜杠路径，
    记下告警——这种路径在 Termux/Android 上必然找不到，多半是 PC 上配的路径被同步到了手机。
    """
    global _env_font_warn
    raw = os.environ.get(key)
    if not raw:
        return None
    low = raw.replace("\\", "/").lower()
    if "windows" not in _SYS and (len(raw) > 1 and raw[1:2] == ":"
                                  or "\\" in raw or low.startswith("c:/")
                                  or low.startswith("d:/") or low.startswith("e:/")):
        _env_font_warn = ("WM_CARD_FONT 含 Windows 风格路径「%s」，但当前是 %s 平台，"
                          "盘符/反斜杠在手机端无效；请改成 Termux 上的真实路径，"
                          "或把字体放进 python/fonts/ 后删除该配置项"
                          % (_safe(raw), _SYS or "非Windows"))
    return str(Path(raw))   # Path 自动按当前 OS 规整分隔符


def _candidate_files():
    """字体候选：环境变量 WM_CARD_FONT（高级） > 默认 python/fonts 下任意字体文件。

    只认这一个默认目录，不再扫系统字体目录——手机端 /system/fonts 里的
    NotoSansCJK 之类的字体依赖 libraqm，加载失败会污染诊断信息，且和我们的
    自带字体无关；统一用 python/fonts 里的字体最可控。
    """
    env = _env_font_path("WM_CARD_FONT")
    if env:
        yield env, _env_font_path("WM_CARD_FONT_BOLD") or env
    for d in _portable_font_dirs():
        if d.is_dir():
            for ext in ("*.ttf", "*.ttc", "*.otf"):
                for p in sorted(d.glob(ext)):
                    s = str(p)
                    yield s, s


def _load_fonts():
    """探测并缓存 (body, sec, title, right)，失败返回 None。"""
    global _font_ready
    if _font_ready is not None:
        return _font_ready or None
    if not _PIL_OK:
        _font_ready = False
        return None
    seen = set()
    for bold_p, reg_p in _candidate_files():
        if reg_p in seen:
            continue
        seen.add(reg_p)
        try:
            body = ImageFont.truetype(reg_p, BODY_SIZE)
            sec = ImageFont.truetype(bold_p, SEC_SIZE)
            title = ImageFont.truetype(bold_p, TITLE_SIZE)
            right = ImageFont.truetype(reg_p, RIGHT_SIZE)
        except Exception:
            continue
        # 校验真的能画中文：画一个汉字看宽度是否 > 0 且不是豆腐块
        try:
            if body.getmask("星").size[0] <= 0:
                continue
        except Exception:
            pass
        _font_ready = (body, sec, title, right)
        return _font_ready
    _font_ready = False
    return None


def available():
    """调用方据此决定走图还是走文本。"""
    return _load_fonts() is not None


def diagnose():
    """说明「图片为什么没出来」，供开机自检与日志使用。

    找不到中文字体时，只报默认字体目录是否存在、里面有无字体文件，
    以及发现字体却加载失败的具体报错（多半是缺原生库 libraqm，而非字体问题）。
    """
    if not _PIL_OK:
        return ("缺 Pillow：pip install pillow（Termux 也可 pkg install python-pillow）；"
                "当前只能发纯文字")
    if available():
        return "OK：Pillow + 中文字体可用，可正常出图"
    d = _portable_font_dirs()[0]
    lines = ["Pillow 已装但找不到可用中文字体。"]
    lines.append("  默认字体目录 = %s" % _safe(d))
    if d.is_dir():
        files = [p.name for p in d.iterdir()
                 if p.suffix.lower() in (".ttf", ".ttc", ".otf")]
        lines.append("  ✔ 存在，字体文件：%s" % (files or "无（目录为空，请放入中文字体）"))
    else:
        lines.append("  ✘ 不存在 → 请新建该目录并放入任意中文字体（如 wqy-microhei.ttc）后重启")
    # 发现字体但加载失败 → 报具体错误（最常见：libraqm.so 缺失）
    seen = set()
    for bold_p, reg_p in _candidate_files():
        try:
            ImageFont.truetype(reg_p, 10)
        except Exception as e:
            msg = "%s → %s" % (Path(reg_p).name, e)
            if msg not in seen:
                seen.add(msg)
                lines.append("  字体加载失败：" + msg)
    if _env_font_warn:
        lines.append("  ⚠ " + _env_font_warn)
    if any("libraqm" in l for l in lines):
        lines.append("  ↳ libraqm.so 是 Pillow 在手机端渲染文字需要的原生库，环境没装才会加载失败；")
        lines.append("    这不是字体问题。修法：①改用 Termux 运行并 pkg install python-pillow libraqm；")
        lines.append("    ②或把 libraqm.so 放进 python/libs/ 后重启（程序会自动预加载）。")
    else:
        lines.append("  解决：确保默认字体目录存在且含中文字体后重启。")
    return "\n".join(lines)


# ------------------------------ 文本处理 ------------------------------
# 只清 emoji / 变体选择符 / 零宽字符；「·」「—」「…」这些正常标点必须保留
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"      # 表情、旗帜、符号补充
    "\U00002600-\U000027BF"       # 杂项符号、装饰符号
    "\U00002B00-\U00002BFF"       # 箭头/星形补充
    "\U0001F1E6-\U0001F1FF"       # 区域指示符
    "\uFE0F\uFE0E\u200B-\u200F\u20E3]"
)
# 注意：箭头 → (U+2192) 不清掉，排序说明「游戏中 → 在线 → 离线」要用它。


# 卡片里状态 emoji 画不出来（Pillow 会变豆腐块），直接删掉又会让「谁在线」完全看不出来。
# 规则：emoji 后面本来就跟了中文状态词（如「🟢游戏中」）→ 只保留中文词；
#       订单行里 emoji 后直接是昵称（如「🟢 Nick」）→ 换成 [游戏中] 标签。
# 再配合 seller_colors 给昵称上色（游戏中红 / 在线蓝 / 离线灰），状态一眼可辨。
_STATUS_TAG = {"🟢": "游戏中", "🔵": "在线", "⚪": "离线"}
_STATUS_RE = re.compile("([🟢🔵⚪])\\s*(游戏中|在线|离线)?")


def _status_sub(m):
    word = _STATUS_TAG.get(m.group(1), "")
    if m.group(2):                    # 后面已有中文状态词，避免重复
        return word
    return "[" + word + "] "


def _clean(s, status=True):
    """其余 emoji 与多余空白去掉；保留中英文与常用标点。

    status=True 时把 🟢/🔵/⚪ 转成中文状态标签，这是【WM 行情卡片】专用的：
    世界状态卡片里「🔵 温德(Vome)」也是蓝色圆点，若照样替换会变成
    「[在线] 温德(Vome)」，凭空多一个在线标签。由 render() 按卡片类型开关。
    """
    raw = s or ""
    s = _STATUS_RE.sub(_status_sub, raw) if status else raw
    s = _EMOJI_RE.sub("", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def _wrap(draw, text, font, maxw):
    """按像素宽度折行：优先在空格 / 中点 / 斜杠 / 逗号处断开，中文可逐字断。"""
    if not text:
        return [""]
    if draw.textlength(text, font=font) <= maxw:
        return [text]
    out, buf = [], ""
    for ch in text:
        trial = buf + ch
        if draw.textlength(trial, font=font) <= maxw:
            buf = trial
            continue
        # 断行点回退：在最近的软分隔处切开
        cut = -1
        for i in range(len(buf) - 1, max(0, len(buf) - 12), -1):
            if buf[i] in " ·/、，,)）":
                cut = i + 1
                break
        if cut > 0:
            out.append(buf[:cut].rstrip())
            buf = buf[cut:].lstrip() + ch
        else:
            out.append(buf)
            buf = ch
    if buf:
        out.append(buf)
    return out


def _blocks(text):
    """按空行切块；每块第一行是分组标题（原文本里带 emoji 的那行）。"""
    blocks, cur = [], None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = None
            continue
        if cur is None:
            cur = {"title": line.strip(), "rows": []}
        else:
            cur["rows"].append(line)
    if cur:
        blocks.append(cur)
    return blocks


def _prepare(blocks, draw, fonts, maxw, status=True):
    """把每个块折算成「已折行的可视行 + 高度」，供排版与分栏共用一份结果。

    两步走：先收集逻辑行（并处理「剩余：」合并到上一行右侧），再按可用宽度折行。
    有右侧时间戳的行要给时间戳预留宽度，否则右对齐的文字会压在正文上。
    """
    body, sec, _title, right = fonts
    prep = []
    for b in blocks:
        logical = []          # {"raw","px","right"}
        for line in b["rows"]:
            indent = len(line) - len(line.lstrip(" "))
            px = min(indent * 11, 66)
            txt = _clean(line.strip(), status)
            if not txt:
                continue
            # 「剩余：xx」单独成行时，贴到上一行右侧，省一行高度也更好读
            if txt.startswith("剩余：") and logical:
                logical[-1]["right"] = txt[3:].strip()
                continue
            logical.append({"raw": txt, "px": px, "right": ""})

        rows = []
        for r in logical:
            gutter = 0
            if r["right"]:
                gutter = int(draw.textlength(r["right"], font=right)) + 14
            avail = max(120, maxw - 16 - r["px"] - gutter)
            for i, seg in enumerate(_wrap(draw, r["raw"], body, avail)):
                rows.append((seg, r["right"] if i == 0 else "",
                             r["px"] if i == 0 else r["px"] + 14))

        tsegs = _wrap(draw, _clean(b["title"], status), sec, maxw - 22)
        h = len(tsegs) * (SEC_SIZE + 6) + 22 + len(rows) * LINE_H + SEC_GAP
        prep.append({"title": tsegs, "rows": rows, "h": h})
    return prep


def _split_segments(txt, colors):
    """把一行按「卖家昵称」切成 [(文本, 颜色), ...]；没命中就整行用墨色。"""
    if not txt:
        return [(txt, INK)]
    names = sorted((n for n in (colors or {}) if n), key=len, reverse=True)
    if not names:
        return [(txt, INK)]
    pattern = re.compile("|".join(re.escape(n) for n in names))
    out, pos = [], 0
    for m in pattern.finditer(txt):
        if m.start() > pos:
            out.append((txt[pos:m.start()], INK))
        seg = m.group(0)
        out.append((seg, colors[seg]))
        pos = m.end()
    if pos < len(txt):
        out.append((txt[pos:], INK))
    return out


def _draw_segments(d, x, y, txt, font, colors):
    """一行里命中「卖家昵称」的片段按状态颜色绘制，其余用默认墨色。

    colors: {昵称: RGB}。用 textlength 逐段推进 x，避免中文/英文混排错位。
    """
    cx = x
    for seg, color in _split_segments(txt, colors):
        if not seg:
            continue
        d.text((cx, y), seg, font=font, fill=color)
        cx += d.textlength(seg, font=font)


def _draw_block(d, blk, x, y, w, color, fonts, colors=None):
    body, sec, _title, right = fonts
    tsegs = blk["title"]
    # 色条高度跟着标题行数走
    d.rounded_rectangle([x, y + 3, x + 5, y + len(tsegs) * (SEC_SIZE + 6) + 3], 2, fill=color)
    ty = y
    for seg in tsegs:
        d.text((x + 16, ty), seg, font=sec, fill=INK)
        ty += SEC_SIZE + 6
    d.line([(x, ty + 6), (x + w, ty + 6)], fill=HAIR, width=1)
    yy = ty + 22
    for txt, rt, px in blk["rows"]:
        _draw_segments(d, x + 16 + px, yy, txt, body, colors)
        if rt:
            rw = d.textlength(rt, font=right)
            d.text((x + w - rw, yy + 5), rt, font=right, fill=MUT)
        yy += LINE_H
    return yy


def _new_page(w, h):
    img = Image.new("RGB", (w, h), "white")
    return img, ImageDraw.Draw(img)


def _header(img, d, w, title, subtitle, fonts):
    _body, _sec, tf, _right = fonts
    d.text((PAD, 34), title, font=tf, fill=INK)
    if subtitle:
        d.text((PAD, 34 + TITLE_SIZE + 10), subtitle, font=fonts[0], fill=MUT)
    y = 34 + TITLE_SIZE + (46 if subtitle else 20)
    d.line([(PAD, y), (w - PAD, y)], fill=INK, width=3)
    return y + 26


def render(text, title="星际战甲 · 世界状态", subtitle=None, footer=True,
           colors=None):
    """
    文本 → 卡片 PNG 列表（每项是 bytes）。失败/不可用时返回 None。
    subtitle 不给时自动写「数据源 · 更新时间」，让图自带时间戳。
    超长处理顺序：单栏 → 双栏 → 分页。
    colors: {昵称: RGB}，命中昵称按状态上色（WM 行情卡片用）。
    """
    fonts = _load_fonts()
    if fonts is None:
        return None
    blocks = _blocks(_clean_lines_preserved(text))
    if not blocks:
        return None
    if subtitle is None:
        subtitle = "国际服 · 更新 " + time.strftime("%m-%d %H:%M:%S")
    if colors is not None:
        # WM 行情卡片：提示怎么复制正文、/w 指令在哪（与图同属一条消息）
        foot_txt = ("「文字 WM 物品名」可复制正文；/w 发起聊天指令见本条消息的文字"
                    if footer else None)
    else:
        foot_txt = ("发「裂缝」「突击」「仲裁」等单词看单栏明细；「计划」看到期时间表。"
                    if footer else None)

    # 先用临时画布做测量
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10), "white"))

    def page_h(prep):
        return 26 + sum(b["h"] for b in prep) + (34 if foot_txt else 0) + PAD

    # 状态 emoji → 中文标签只在 WM 行情卡片启用（见 _clean 的说明）
    status = colors is not None
    head_h = 34 + TITLE_SIZE + 46 + 26
    # ① 单栏
    prep = _prepare(blocks, probe, fonts, W_SINGLE - PAD * 2, status)
    total = head_h + page_h(prep)
    if total <= MAX_H + 200:
        return [_emit(prep, W_SINGLE, PAD, PAD * 2, title, subtitle, foot_txt,
                      fonts, 0, colors)]
    # ② 双栏：先按列宽重算每块高度，再找让两列最接近的切分点（块不切断）
    colw = (W_DOUBLE - PAD * 2 - COL_GAP) // 2
    if colw >= 380:
        at_col = _prepare(blocks, probe, fonts, colw - 16, status)
        hs = [b["h"] for b in at_col]
        total2 = sum(hs)
        best, best_gap = 1, None
        acc = 0
        for i in range(1, len(hs) + 1):
            acc += hs[i - 1]
            gap = abs(acc - (total2 - acc))
            if best_gap is None or gap < best_gap:
                best, best_gap = i, gap
        lp, rp = at_col[:best], at_col[best:]
        h = max(sum(b["h"] for b in lp), sum(b["h"] for b in rp))
        if h + head_h + PAD <= MAX_H + 600:
            return [_emit2(lp, rp, colw, h, title, subtitle, foot_txt, fonts, colors)]
    # ③ 分页：按块切，每页塞到 MAX_H 为止
    pages, cur, cur_h = [], [], 0
    for b in prep:
        if cur and cur_h + b["h"] + head_h > MAX_H:
            pages.append(cur)
            cur, cur_h = [], 0
        cur.append(b)
        cur_h += b["h"]
    if cur:
        pages.append(cur)
    out = []
    for i, pg in enumerate(pages[:4]):     # 最多 4 页，防炸
        t = title if len(pages) == 1 else f"{title}（{i + 1}/{len(pages)}）"
        out.append(_emit(pg, W_SINGLE, PAD, PAD * 2, t, subtitle, foot_txt,
                         fonts, 0, colors))
    return out


def _clean_lines_preserved(text):
    """块切分前只统一换行符，缩进要留给 _prepare 算层级。"""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _emit(prep, w, x0, margin, title, subtitle, foot, fonts, color_off, colors=None):
    h = 34 + TITLE_SIZE + 46 + 26 + sum(b["h"] for b in prep) + (34 if foot else 0) + PAD
    img, d = _new_page(w, h)
    y = _header(img, d, w, title, subtitle, fonts)
    for i, b in enumerate(prep):
        y = _draw_block(d, b, x0, y, w - x0 * 2,
                        PALETTE[(i + color_off) % len(PALETTE)], fonts, colors)
    if foot:
        d.text((PAD, h - PAD - 6), foot, font=fonts[0], fill=MUT)
    return _to_png(img)


def _emit2(lp, rp, colw, h, title, subtitle, foot, fonts, colors=None):
    """双栏排版：两列各自从表头下方开始，列高取较长的一列。"""
    w = W_DOUBLE
    img, d = _new_page(w, 34 + TITLE_SIZE + 46 + 26 + h + (34 if foot else 0) + PAD)
    y0 = _header(img, d, w, title, subtitle, fonts)
    x_r = PAD + colw + COL_GAP
    y = y0
    for i, b in enumerate(lp):
        y = _draw_block(d, b, PAD, y, colw, PALETTE[i % len(PALETTE)], fonts, colors)
    y2 = y0
    for i, b in enumerate(rp):
        y2 = _draw_block(d, b, x_r, y2, colw,
                         PALETTE[(i + 3) % len(PALETTE)], fonts, colors)
    if foot:
        d.text((PAD, img.size[1] - PAD - 6), foot, font=fonts[0], fill=MUT)
    return _to_png(img)


def _to_png(img):
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


if __name__ == "__main__":       # 手工预览：python render_card.py 全部 out.png
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import worldstate as W
    cmd = sys.argv[1] if len(sys.argv) > 1 else "全部"
    dest = sys.argv[2] if len(sys.argv) > 2 else "card_preview.png"
    txt = W.handle_query(W.parse_ws_command(cmd))
    pngs = render(txt)
    if not pngs:
        print("渲染不可用（缺 Pillow 或中文字体）")
    else:
        for i, p in enumerate(pngs):
            name = dest if len(pngs) == 1 else dest.replace(".png", f"_{i+1}.png")
            Path(name).write_bytes(p)
            print(f"{name}  {len(p)/1024:.2f} KB")
