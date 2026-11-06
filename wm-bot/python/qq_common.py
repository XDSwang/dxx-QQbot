#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qq_common.py — QQ 对接共享逻辑（与传输层无关）

无论走【官方 QQ 机器人 API】（q.qq.com）还是【OneBot v11】（社区逆向方案），
消息判定 / 群白名单 / WM 查询触发 都共用这一份，避免两套实现各写一遍。

对外导出：
  - is_wm_command(text) -> bool
  - load_allowed_groups(path) -> set
  - make_handler(allowed_groups=None, allow_all=False) -> (text, ctx) -> str|None
"""
from pathlib import Path

import wm

HERE = Path(__file__).resolve().parent
# 群白名单文件：每行一个群号 / 群 OpenID（# 开头为注释，行内 # 后为备注）。
# 只有写进这个文件的群，才会被监控「/wm」「/zk」开头消息并回复；其它群一律不回。
#   - OneBot 方案：填普通 QQ 群号（整数）
#   - 官方方案：填群 OpenID（字符串，如 "xxxxxxxx-yyyy"）
DEFAULT_GROUPS_FILE = HERE / "data" / "groups.txt"

# 无斜杠首词触发（wm / zk / 紫卡 / 翻译，大小写不敏感）
WORD_TRIGGERS = {"wm", "zk", "紫卡", "翻译", "translate", "trans"}
# 斜杠指令触发（/wm /zk /报错 /查询 /翻译）
SLASH_TRIGGERS = {"wm", "zk", "报错", "查询", "翻译", "translate", "trans"}


def is_wm_command(text):
    """判断一条文本是否是 WM 查询指令。"""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    if low.startswith("/"):
        body = low[1:].strip()
        first = body.split()[0] if body.split() else ""
        return first in SLASH_TRIGGERS
    first = low.split()[0] if low.split() else ""
    return first in WORD_TRIGGERS


def resolve_path(p):
    """相对路径一律按「脚本同目录」解析；绝对路径原样返回。

    整个文件夹拷到手机任意位置都能跑：不依赖当前工作目录、不写死盘符、不出现 ../ 上层路径。
    """
    q = Path(p)
    return q if q.is_absolute() else (HERE / q)


def load_allowed_groups(path):
    """读取群白名单，返回群号/OpenID 字符串集合（去注释、去行内备注）。"""
    allowed = set()
    p = resolve_path(path)
    if not p.exists():
        return allowed
    # utf-8-sig：手机上的编辑器保存时可能带 BOM，BOM 粘在首行会被误当成群标识
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        gid = line.split("#", 1)[0].strip()   # 支持 "群号 # 备注"
        if gid:
            allowed.add(gid)
    return allowed


def make_handler(allowed_groups=None, allow_all=False):
    """
    返回一个 handler(text, ctx) -> str|None，供两种传输层共用。

    - allowed_groups=None 或 allow_all=True：宽松，所有群都处理（测试 / 调试用）；
    - 正式运行传 allowed_groups（群号/OpenID 集合）且 allow_all=False：
      只有白名单内的群才回复，且需消息以 /wm 或 /zk 开头，避免刷屏。
    私聊（channel != "group"）不受群白名单限制（一对一，无刷屏风险）。

    ctx 字段约定（两种传输层归一化后传入）：
      - channel / message_type："group" | "private" | "c2c"
      - group_id：群号（OneBot，整数）或群 OpenID（官方，字符串）
    """

    def handler(text, ctx=None):
        ctx = ctx or {}
        channel = ctx.get("channel") or ctx.get("message_type")
        if channel == "group":
            # 群聊：严格按白名单；不在名单内直接静默（不回复）
            gid = str(ctx.get("group_id", ""))
            if not allow_all and allowed_groups is not None and gid not in allowed_groups:
                # 白名单外的群：严格静默，不回复、不打印任何提示；
                # 不自动写入 groups.txt（否则等于监控所有群，违反"只监控白名单群"的铁律）。
                return None
        if not is_wm_command(text):
            return None
        cmd = wm.parse_command(text)
        if not cmd:
            return wm.format_reply({"served": "help"})
        if cmd.get("type") == "translate":
            # 翻译 <名称>：只做中↔英名称翻译，不直接查价、也不提供 wiki 正文
            return wm.format_reply({"served": "translate", "text": wm.translate_command(cmd["name"])})
        if cmd.get("force"):
            # /报错：强制忽略缓存重查一次（同样是单次尝试 + 失败立即报错，不卡 11 分钟）
            result = wm.get_cached_or_refresh(cmd["type"], cmd["name"], force=True)
        else:
            # 缓存未超间隔直接回；超间隔先刷新缓存再回复（绝不发几天前的旧缓存）
            result = wm.get_cached_or_refresh(cmd["type"], cmd["name"])
        return wm.format_reply(result)

    return handler
