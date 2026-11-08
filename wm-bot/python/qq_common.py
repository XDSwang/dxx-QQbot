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
import os
import re
import time

import wm
import worldstate
import ws_monitor
import drops

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

# 交互查询前缀：只有以它开头（大小写不敏感，可带前导斜杠）的消息，才被当作
# 一次「要回复别人的查询」来处理；不带此前缀的普通闲聊一律静默，避免群友聊天
# 意外触发机器人。监控控制（监控/停止监控/主推开关）与主动推送不受此限制。
# 想换前缀：直接改下面这个常量即可（例如改成 "BOT/"）。
QUERY_PREFIX = "D/XX/"


# 全局「全部指令」查看词：发这些词返回合并总览（WM + 世界状态），不再是静默
GLOBAL_HELP_WORDS = {"帮助", "help", "?", "菜单", "指令", "命令", "功能", "总览", "全部指令", "全部命令"}
# 这些词【不】要求 D/XX/ 前缀：用户要求「发『帮助』两个字就出菜单」。
# 「?」故意不放进来（单字符太容易误触），要它仍可发 D/XX/? 。
BARELESS_HELP_WORDS = GLOBAL_HELP_WORDS - {"?"}

# 指令识别原则（别再"猜"）：只认【完整词】——整句命中或首词精确命中关键词表，
# 不做子串/后缀/模糊匹配。宁可当成闲聊静默，也不要把聊天里的字眼当指令，
# 否则群里正常说话也会被机器人接话（比"没回复"糟糕得多）。


def _is_global_help(text):
    """整句或首词命中全局帮助词（帮助 / ? / 菜单 / 指令 …）。"""
    t = (text or "").strip().lstrip("/").strip().lower()
    if not t:
        return False
    if t in GLOBAL_HELP_WORDS:
        return True
    first = t.split()[0] if t.split() else t
    return first in GLOBAL_HELP_WORDS


# 排障命令：发「自检」机器人回一段文字，列出手机端图片/Markdown 各部件状态，
# 不用翻日志就能知道「为什么全是纯文字」。
DIAG_WORDS = {"自检", "诊断", "排查", "diy", "diagnose", "check", "状态"}


def is_diagnostic(text):
    t = (text or "").strip().lower().lstrip("/").strip()
    if not t:
        return False
    return t in DIAG_WORDS or t.split()[0] in DIAG_WORDS


def build_diagnostic():
    """返回一段纯文字，汇总手机端图片渲染能力（render_card / Pillow / 字体 /
    Markdown 通道），便于直接在群里排查「全部变纯文字」的问题。"""
    out = ["🔧 机器人自检（手机端）"]
    # 1) render_card
    try:
        import render_card
        out.append("【图片渲染 render_card】存在 ✓")
        try:
            out.append("   详情：" + render_card.diagnose())
        except Exception as e:
            out.append("   详情：diagnose 异常 " + repr(e))
    except Exception as e:
        out.append("【图片渲染 render_card】✗ 缺失/导入失败：" + repr(e))
        out.append("   → 把电脑 python/ 下 render_card.py 拷到手机同目录并重载")
    # 2) Pillow
    try:
        import PIL
        out.append("【Pillow】" + PIL.__version__)
    except Exception as e:
        out.append("【Pillow】✗ 未安装：" + repr(e) + " → pip install pillow")
    # 3) 中文字体（已包含在上面的 diagnose 详情里，这里不再单列）
    # 4) Markdown 通道
    try:
        import qq_markdown
        if qq_markdown.enabled():
            out.append("【Markdown 通道】已启用：" + qq_markdown.resolved_mode())
        else:
            out.append("【Markdown 通道】已关闭（走富媒体图片 msg_type=7）")
    except Exception as e:
        out.append("【Markdown 通道】异常：" + repr(e))
    # 5) 出图模式
    out.append("【出图模式 WM_WS_CARD】" + (os.environ.get("WM_WS_CARD") or "always"))
    out.append("【结论】若上面任一 ✗，所有回复都会退回纯文字；复原对应文件/库即可恢复图片。")
    return "\n".join(out)


def build_full_help():
    """合并全部指令一览：WM 查价/紫卡/翻译 + 世界状态查询/监控/计划。"""
    wm_part = wm.format_reply({"served": "help"})
    return ("📖 全部指令一览（随时发「帮助」或「?」可再看）\n\n"
            + wm_part + "\n\n" + worldstate.HELP_TEXT)


# ---------------- 帮助菜单配色：查询指令红、其它指令蓝 ----------------
# 卡片里只给「命中的片段」上色，其余保持墨色；卡片标题行不着色。
# 两个颜色和 WM 行情的「游戏中红 / 在线蓝」同一套，视觉统一。
HELP_RED = (198, 40, 40)      # 指令查询（WM 查价 / 紫卡 / 翻译 / 世界状态各栏目）
HELP_BLUE = (32, 92, 180)     # 其它指令（监控开关 / 停止监控 / 回复形式 / 帮助自检）

_HELP_QUERY_WORDS = (
    # WM 侧查询
    "WM", "ZK", "紫卡", "翻译", "报错",
    # 世界状态查询：栏目名 + 总览 / 计划
    "世界状态", "平原", "希图斯", "奥布山谷", "魔胎之境", "双衍", "扎里曼",
    "裂缝", "钢铁裂缝", "突击", "仲裁", "入侵", "警报", "活动",
    "商人", "瓦奇娅", "特惠", "电波", "加成",
    "赏金轮换", "赏金", "信条", "终幕", "新闻", "全部", "计划",
    # 遗物 / 掉落来源（官方中文导出）
    "遗物", "掉落", "获取",
)

_HELP_OTHER_WORDS = (
    # 监控 / 开关 / 辅助指令（长的写全，避免被上面的栏目名抢走）
    "停止全部监控", "停止监控", "取消全部监控", "取消监控", "监控全部", "监控", "主推状态",
    "活动=开启", "警报=开启", "活动=关闭", "警报=关闭",
    "全部=开启", "主推=开启",
    "开启", "关闭", "自检", "帮助", "文字", "图片",
)


def help_colors():
    """帮助卡片用的 {片段: 颜色}：查询指令红、其它指令蓝。"""
    out = {w: HELP_RED for w in _HELP_QUERY_WORDS if w}
    out.update({w: HELP_BLUE for w in _HELP_OTHER_WORDS if w})
    return out


# 回复形式前缀：默认按长度自动决定是否出图，也可用前缀强制
_IMG_PREFIXES = ("图片", "卡片", "image", "card", "图")
_TXT_PREFIXES = ("文字", "文本", "text")


class ImageReply(str):
    """既是纯文本（str），又附带卡片 PNG 列表 + 随图同发的可复制纯文本。

    继承 str 是为了不改现有调用方的判断（if not reply / 切片 / 写日志都照旧）；
    传输层只要看 getattr(reply, "pngs", None) 就能决定发图还是发文字；
    tail 是 /w 发起聊天指令，用户能长按复制，不压进图里（图里的字复制不了）。
    两个通道都把它与图片放进【同一条消息】：官方 API 用富媒体图文混排
    （msg_type=7 + content），OneBot 用「图片段 + 文本段」数组。
    """

    pngs = ()
    tail = ""

    def __new__(cls, text, pngs):
        obj = super().__new__(cls, text)
        obj.pngs = list(pngs or [])
        obj.tail = ""
        return obj


# 消息里可能残留的 @ 标记（传输层本该剥干净，这里兜底）：
#   OneBot 字符串协议  [CQ:at,qq=123,name=…]
#   官方机器人占位符    <@!机器人> / <@成员> / <@all>
#   被当成普通文字发来的  @昵称
_CQ_AT_ANY = re.compile(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]")
_ANGLE_AT = re.compile(r"<@(!?)([^>\s]{1,64})>")
_TEXT_AT = re.compile(r"[＠@]([^\s＠@]{1,32})")
# 昵称和指令之间常见的分隔符（"@机器人_平原" / "@机器人|平原"），剥完顺手去掉
_AT_SEP = "_-·—|｜:："


def strip_mentions(text, bot_name=None):
    """剥掉消息【开头】残留的 @ 标记，返回 (剩余文本, [标记字典])。

    每个标记形如 {"kind": "cq"|"angle"|"text", "id": 号/昵称, "bot": 是否机器人形态}：
      cq    → OneBot 字符串协议的 [CQ:at,qq=123]，id 是 QQ 号；
      angle → 官方占位符，带叹号的是【机器人】（<@!bot>），成员是 <@openid>；
      text  → 被当正文发来的 "@昵称"，id 是昵称。
    两个传输层正常已经剥过（onebot._parse_message 剥 CQ 码、qq_official.strip_mention
    剥 <@!openid>），但个别实现会把 @ 标记直接当正文塞进来：此时 "D/XX/" 前缀匹配
    不上，表现出来的就是「@机器人 + 带前缀指令」没反应、裸前缀却有反应。
    只剥开头的连续标记，句子中间的（"我 @某人 打平原"）保持原样，避免误删正文。

    bot_name：机器人完整昵称（WM_QQ_BOT_NAME）。明文 @ 只能靠"到空格为止"猜边界，
    昵称一长、用户又不打空格（"@自动化服务查询_D/XX/平原"）就会把指令一起吞掉；
    知道昵称时按昵称长度精确切开，剩下的才是指令。
    """
    t = text or ""
    marks = []
    while True:
        s = t.lstrip()
        m = _CQ_AT_ANY.match(s)
        if m:
            marks.append({"kind": "cq", "id": m.group(1).strip(), "bot": False})
            t = s[m.end():]
            continue
        m = _ANGLE_AT.match(s)
        if m:
            # 带叹号 = 官方机器人占位符；成员是 <@openid>，全体是 <@all>
            marks.append({"kind": "angle", "id": m.group(2), "bot": bool(m.group(1))})
            t = s[m.end():]
            continue
        m = _TEXT_AT.match(s)
        if m:
            tag, cut = m.group(1), m.end()
            # 昵称后面紧贴指令（"@自动化服务查询_D/XX/平原"）：按昵称长度切开，
            # 不能整段当昵称 —— 否则指令被吞光，表现为「@机器人 没反应」。
            if bot_name and tag.startswith(bot_name) and len(tag) > len(bot_name):
                tag, cut = bot_name, 1 + len(bot_name)
            marks.append({"kind": "text", "id": tag, "bot": False})
            t = s[cut:].lstrip(_AT_SEP)
            continue
        t = s
        break
    return t.strip(), marks


def _mentioned_me(marks, sid, bot_name):
    """被剥掉的 @ 标记里，有没有「就是我」。

    判断顺序（对上一条就算我，因为群里 @ 机器人通常只有它一个）：
      1. 官方 `<@!机器人>` 形态 —— 叹号是机器人专属，成员占位符没有；
      2. 号对得上事件的 self_id（OneBot 会给自己的 QQ 号）；
      3. 昵称对得上 WM_QQ_BOT_NAME（有的实现干脆把 @ 当正文发，只能靠昵称认）。
         配置写简称、实际昵称带后缀（"自动化服务查询_D/XX/"）也算 —— 按前缀认。
    两样身份都不知道（既没 self_id 也没配昵称）时，见到 CQ 码或 `<@!x>` 就按「就是我」
    处理 —— 宁可多回一句，也不要把「@机器人 平原」这种明确指令静默吞掉。
    """
    unknown = not sid and not bot_name
    for m in marks:
        if m["kind"] == "angle" and m["bot"]:
            return True
        if sid and m["id"] == sid:
            return True
        if bot_name and (m["id"] == bot_name or m["id"].startswith(bot_name)):
            return True
        if unknown and m["kind"] == "cq":
            return True
    return False


def split_style_prefix(text):
    """剥掉「图 / 文字」前缀，返回 (剩余文本, 强制出图, 强制文字)。"""
    t = (text or "").strip()
    low = t.lower()
    for p in _IMG_PREFIXES:
        if low.startswith(p) and len(t) > len(p):
            return t[len(p):].lstrip(" :：,，"), True, False
    for p in _TXT_PREFIXES:
        if low.startswith(p) and len(t) > len(p):
            return t[len(p):].lstrip(" :：,，"), False, True
    return t, False, False


def _card_mode():
    """always（默认，一律出图）/ auto（超长才出图）/ off（只发文字）。

    默认改为 always：用户要求「所有回复优先发图片」，文字只在发「文字 xxx」时给。
    """
    return (os.environ.get("WM_WS_CARD") or "always").strip().lower()


def _card_min_bytes():
    try:
        return int(os.environ.get("WM_WS_CARD_MIN") or 800)
    except ValueError:
        return 800


_card_warn_at = [0.0]


def _card_log(msg):
    """借 wm.LOG_HOOK（启动时指向 qq_official.flog）写流程日志。"""
    hook = getattr(wm, "LOG_HOOK", None)
    if callable(hook):
        try:
            hook(msg)
        except Exception:
            pass


def _card_warn(msg):
    """5 分钟节流，避免手机端每条消息都刷同一条故障日志。"""
    now = time.time()
    if now - _card_warn_at[0] >= 300:
        _card_warn_at[0] = now
        _card_log(msg)


def maybe_image(text, force_image=False, force_text=False, title=None, colors=None,
                foot=None):
    """长文本 → 卡片图片；Pillow/字体缺失、渲染出错都原样返回文本，绝不打断回复。

    colors: {昵称: RGB}，只有 WM 行情卡片传，用来给卖家昵称按在线状态上色。
    foot: 自定义卡片页脚提示（遗物/掉落卡片用自己的指引，不套世界状态那套）。
    渲染失败时写一条节流日志（5 分钟一次），说明缺 Pillow 还是缺中文字体，
    免得手机端「图片和红蓝灰昵称突然都没了」却查不到原因。
    """
    if not isinstance(text, str) or not text:
        return text
    mode = _card_mode()
    if force_text or mode in ("off", "0", "false", "no", "none"):
        return text
    auto_img = mode in ("always", "on", "1", "true", "yes", "all")
    if not force_image and not auto_img:
        if len(text.encode("utf-8")) < _card_min_bytes():
            return text
    try:
        import render_card
    except ImportError as e:
        _card_warn(f"缺 render_card.py（{e}），所有回复改发纯文字、"
                   "红/蓝/灰昵称也没了；把电脑 python/ 下这个文件拷到手机即可")
        return ("⚠️【图片未生成】手机端缺 render_card.py，所有回复只能发纯文字。"
                "把电脑 python/ 下的 render_card.py 拷到手机同目录后重启即可恢复。\n"
                + text)
    try:
        pngs = render_card.render(text, title=title or "星际战甲 · 世界状态",
                                  colors=colors, foot=foot)
    except Exception as e:
        _card_warn(f"卡片渲染异常，改发纯文字: {e}")
        return (f"⚠️【图片渲染异常】{e}（render_card 已存在但出错，多为字体/权限）。\n"
                + text)
    if not pngs:
        try:
            detail = render_card.diagnose()
        except Exception:
            detail = "原因未知"
        _card_warn(f"卡片未生成，改发纯文字：{detail}")
        return (f"⚠️【图片未生成】{detail}（多为缺中文字体或 Pillow 版本问题，"
                "参考 README「手机必带文件」）。\n" + text)
    return ImageReply(text, pngs)


def attach_monitor(text, ctx=None, covered=None):
    """把本会话暂存的监控内容接到回复末尾（主动推送发不出去时的兜底通道）。

    官方机器人的主动消息要单独申请配额（没开通时返回 40034105 无权限），
    但被动回复一定有 5 分钟窗口。所以「监控」改成两手准备：
      主动通道能发就发；发不出去就攒着，用户下次查询时随回复一起带出。
    """
    if not isinstance(text, str) or not text:
        return text
    ch, tid = _ctx_ids(ctx)
    if ch is None:
        return text
    try:
        extra = ws_monitor.pending_for(ch, tid, covered=covered)
    except Exception:
        return text
    if not extra:
        return text
    return (text + "\n\n📬 监控推送（主动消息受限，随本次查询附带）\n" + extra)


def finalize(text, ctx=None, force_image=False, force_text=False,
             title=None, attach=True, covered=None, colors=None, foot=None):
    """统一出口：先捎带监控内容，再按配置渲染成卡片（失败自动退回文字）。

    covered：本次查询已包含的栏目；监控内容若全被覆盖就不重复附带。
    foot：卡片页脚提示（遗物/掉落的卡片要自己的指引文案）。
    """
    if not isinstance(text, str) or not text:
        return text
    if attach:
        text = attach_monitor(text, ctx, covered=covered)
    return maybe_image(text, force_image=force_image,
                       force_text=force_text, title=title, colors=colors, foot=foot)


def _wm_reply(result, ctx, force_img, force_txt):
    """WM 行情回复 = 白底卡片（价格列表、昵称按状态上色）+ 图片后的 /w 指令字符串。

    用户要求：/w 英中两条指令「拿出来发字符串」，不能压进图片（图里的字复制不了）；
    且指令结尾不带 (wf.m ...) 之类的来源备注。
    纯文字模式（「文字 WM …」）或没有订单时，直接回全文（指令本来就在里面）。
    """
    whisper = wm.build_whisper(result)
    title = "星际战甲 · 市场行情"
    colors = wm.seller_colors(result)
    if force_txt or not whisper:
        return finalize(wm.format_reply(result), ctx, force_image=force_img,
                        force_text=force_txt, title=title, colors=colors)
    # 正文先捎带监控内容，再渲染卡片；/w 指令作为 tail 与图片合成一条消息
    body = attach_monitor(wm.reply_body(result), ctx)
    reply = maybe_image(body, force_image=force_img, force_text=False,
                        title=title, colors=colors)
    if isinstance(reply, ImageReply):
        reply.tail = whisper
        return reply
    # 没画成图（off / auto 短文本 / 字体缺失）：退回正文 + /w 指令一起作为文本
    return body + ("\n\n" if body.strip() else "") + whisper


def is_ws_command(text):
    """判断一条文本是否是世界状态（查询 / 监控）指令。

    触发规则（与 WM 关键词零冲突）：
      - 以「监控 / monitor」或「停止监控 / 取消监控 / 停止」开头；
      - 或首词（CJK 无空格时即整句）命中 worldstate.KEYWORDS（警报/平原/突击…）；
      - 或整句命中帮助词（世界/帮助/ws…）。
    仅当明确是命令词才返回 True，避免群内闲聊被误触发。
    """
    t = (text or "").strip().lstrip("/").strip().lower()
    if not t:
        return False
    # 「活动=开启 / 警报=关闭 / 主推状态」优先：含「监」「停」等字也可能属于开关指令
    if worldstate.is_switch_command(t):
        return True
    first = t.split()[0] if t.split() else t
    if first.startswith("监控") or first.startswith("monitor"):
        return True
    if (first.startswith("停止监控") or first.startswith("取消监控")
            or first.startswith("停止")):
        return True
    if t in worldstate.HELP_WORDS or t in worldstate.PLAN_WORDS:
        return True
    if first in worldstate.KEYWORDS:
        return True
    if t in worldstate.KEYWORDS:
        return True
    # 带维度过滤的裂缝查询：「裂缝后纪中断」「钢铁裂缝古纪中断」。
    # 整句必须能完整切开（多一个无法识别的词就算闲聊），延续「完美匹配」原则。
    if first.startswith(("裂缝", "裂隙", "钢铁裂缝", "fissure")):
        spec = worldstate.parse_fissure_spec(t)
        # 必须真的带条件（「裂缝裂缝」这种空条件仍算闲聊，不当指令）
        if spec is not None and spec["tokens"] and not spec.get("residual"):
            return True
    return False


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


def strip_query_prefix(text):
    """若以 QUERY_PREFIX（默认 D/XX/，大小写不敏感，可选前导斜杠）开头，剥掉前缀
    返回剩余文本；否则返回 None（表示这不是带前缀的显式查询）。

    前导斜杠会原样保留，这样「/报错」「/查询」这类纯斜杠触发词在剥掉前缀后仍然有效：
      例：D/XX/wm Boltor   → "wm Boltor"
          /D/XX/报错 WM x  → "/报错 WM x"（保留斜杠语义）
          /D/XX/平原        → "/平原"
          普通「你好」        → None
    """
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    pref = QUERY_PREFIX.lower()
    if low.startswith("/" + pref):
        # 保留用户的前导斜杠，交给后面的斜杠触发逻辑
        return ("/" + t[len(pref) + 1:].strip()).strip()
    if low.startswith(pref):
        return t[len(pref):].strip()
    return None


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
        # 先兜底剥掉开头残留的 @ 标记（CQ 码 / <@!id> / 明文 "@昵称"）：
        # 有的实现不单发 at 段而是把 @ 混进正文，不剥就会让 D/XX/ 前缀匹配失败。
        # 顺带一句：@ 的正是自己的话，也算「对着我说」，等价于 ctx["at_me"]。
        sid = str(ctx.get("self_id") or "").strip()
        # 昵称优先用传输层自动问到的（OneBot get_login_info），再退回配置兜底
        bot_name = (ctx.get("bot_name") or os.environ.get("WM_QQ_BOT_NAME") or "").strip()
        body, at_marks = strip_mentions(text, bot_name)
        at_me = bool(ctx.get("at_me")) or _mentioned_me(at_marks, sid, bot_name)
        # 「图 xxx」/「文字 xxx」前缀：强制出图 / 强制只发文字（默认一律出图）
        body, force_img, force_txt = split_style_prefix(body)
        # —— 所有「交互指令」必须带 D/XX/ 前缀，防群聊闲聊误触发 ——
        # 例外：机器人主动推送（send_active）是机器人外发，不是用户指令，不受影响。
        # 因此 WM 查价 / 世界状态查询 / 监控开关 / 帮助 / 自检 全部收进前缀；
        # 只有「主推信息」（机器人主动推出来的监控）不受限。
        q = strip_query_prefix(body)
        if q is None:
            # 例外 1：裸「帮助 / help / 菜单 / 指令…」不要求前缀，发这两个字就直接出菜单
            bare = (body or "").strip().lstrip("/").strip().lower()
            if bare in BARELESS_HELP_WORDS:
                q = bare
            elif at_me:
                # 例外 2：@ 了机器人 —— 这句话本来就是对着我说的，允许不带 D/XX/
                # （@ 本身已是明确意图，群里闲聊不会误触；仍要求是完整指令才会回）
                q = (body or "").strip().lstrip("/").strip()
                if not q:
                    return None
            else:
                # 没带前缀：普通闲聊，静默不回复（监控 / 主动推送照常工作）
                return None
        # 排障：发「自检」直接回一段文字，列出手机端图片/Markdown 各部件状态
        if is_diagnostic(q):
            return build_diagnostic()
        # 全局「全部指令」查看：返回合并总览（WM + 世界状态）
        if _is_global_help(q):
            return finalize(build_full_help(), ctx, force_image=force_img,
                            force_text=force_txt, title="星际战甲 · 全部指令",
                            colors=help_colors())
        # 世界状态指令（查询 / 监控 / 停止 / 主推开关）：带前缀才触发
        if is_ws_command(q):
            return handle_ws(q, ctx, force_image=force_img,
                             force_text=force_txt)
        # 遗物 / 掉落来源查询：同样带前缀（或 @机器人）才触发
        if drops.is_drop_command(q):
            return handle_drop(q, ctx, force_image=force_img,
                               force_text=force_txt)
        # WM 查询（wm / zk / 紫卡 / 翻译）：带前缀才触发
        if not is_wm_command(q):
            # 完整指令才算数：一条都没精确命中就当闲聊，静默（不猜、不提示）。
            # 宁可「发了不认识的指令没反应」，也不要因为模糊匹配去回应聊天内容。
            return None
        cmd = wm.parse_command(q)
        if not cmd:
            return finalize(wm.format_reply({"served": "help"}), ctx,
                            force_image=force_img, force_text=force_txt,
                            title="星际战甲 · 市场行情", colors=help_colors())
        if cmd.get("type") == "translate":
            # 翻译 <名称>：只做中↔英名称翻译，不直接查价、也不提供 wiki 正文
            return finalize(
                wm.format_reply({"served": "translate",
                                 "text": wm.translate_command(cmd["name"])}),
                ctx, force_image=force_img, force_text=force_txt,
                title="星际战甲 · 名称翻译")
        if cmd.get("force"):
            # /报错：强制忽略缓存重查一次（同样是单次尝试 + 失败立即报错，不卡 11 分钟）
            result = wm.get_cached_or_refresh(cmd["type"], cmd["name"], force=True)
        else:
            # 缓存未超间隔直接回；超间隔先刷新缓存再回复（绝不发几天前的旧缓存）
            result = wm.get_cached_or_refresh(cmd["type"], cmd["name"])
        return _wm_reply(result, ctx, force_img, force_txt)

    return handler


def make_monitor_aware_handler(base_handler):
    """包装一层：base 静默时，若本会话有暂存的监控内容，就随这条消息带出去。

    开了「机器人可获取的群聊消息范围 = 获取群内全部消息」之后，群里每条消息
    都会推过来；主动推送万一发不出去（对方关了主动发言 / 撞频控），任意一条
    消息都能当被动窗口，不必非等有人发查询指令。仍受 WS_ATTACH_GAP 限频。
    白名单外的会话不会有暂存记录，因此依旧严格静默、不会误回复。
    """
    def handler(text, ctx=None):
        reply = base_handler(text, ctx)
        if reply:
            return reply
        ch, tid = _ctx_ids(ctx)
        if ch is None:
            return None
        try:
            extra = ws_monitor.pending_for(ch, tid, covered=None)
        except Exception:
            return None
        if not extra:
            return None
        return finalize("📬 监控推送（主动消息受限，随本次消息附带）\n" + extra,
                        ctx, attach=False, title="星际战甲 · 监控推送")

    return handler


def handle_ws(text, ctx=None, force_image=False, force_text=False):
    """世界状态指令的统一入口：查询 / 监控 / 开关 / 停止监控 / 帮助。"""
    cmd = worldstate.parse_ws_command(text)
    action = cmd.get("action")
    if action == "switch_status":
        ch, tid = _ctx_ids(ctx)
        return finalize(ws_monitor.switch_status_text(ch, tid), ctx,
                        force_image=force_image, force_text=force_text,
                        attach=False, title="星际战甲 · 主推开关")
    if action == "switch":
        ch, tid = _ctx_ids(ctx)
        if ch is None:
            return finalize("（无法识别会话，无法设置开关；群聊需在白名单内）", ctx,
                            force_image=force_image, force_text=force_text,
                            attach=False, title="星际战甲 · 主推开关")
        on_list, off_list = [], []
        for item in cmd["items"]:
            got = ws_monitor.set_switch(ch, tid, item["types"], item["on"])
            (on_list if item["on"] else off_list).extend(got)
        parts = []
        if on_list:
            n = "、".join(worldstate.TYPES[t][0] for t in on_list)
            parts.append("✅ 已开启「%s」主推\n新条目出现立即单独推一条（%d 秒防抖）；"
                         "已自动纳入本会话监控，不用再发「监控 …」。"
                         % (n, int(ws_monitor.priority_min_gap())))
        if off_list:
            n = "、".join(worldstate.TYPES[t][0] for t in off_list)
            parts.append("⛔ 已关闭「%s」主推\n该栏目不再主推，周期全量推送里也会剔除。"
                         % n)
        if not parts:
            parts.append("（没有识别到可设置的栏目，试试「活动=开启」）")
        parts.append("💾 设置已保存，重启后自动恢复。\n"
                     "发「主推状态」看当前开关；"
                     "「停止监控」只停自己的，「停止全部监控」清本群所有人的设置。")
        return finalize("\n\n".join(parts), ctx,
                        force_image=force_image, force_text=force_text,
                        attach=False, title="星际战甲 · 主推开关")
    if action == "help":
        return finalize(worldstate.HELP_TEXT + "\n\n" + wm.format_reply({"served": "help"}),
                        ctx, force_image=force_image, force_text=force_text,
                        title="星际战甲 · 世界状态指令", colors=help_colors())
    if action in ("stop", "stop_hint"):
        ch, tid = _ctx_ids(ctx)
        if ch is None:
            return finalize("（无法识别会话，停止监控失败）", ctx,
                            force_image=force_image, force_text=force_text,
                            attach=False,
                            title="星际战甲 · 世界状态监控")
        if action == "stop_hint":
            return finalize(
                "⚠️ 没认出要停哪个栏目，什么都没改。\n"
                "· 停自己的监控：发「停止监控」\n"
                "· 停本群所有人的监控：发「停止全部监控」\n"
                "· 只停某个栏目：发「停止监控 警报」",
                ctx, force_image=force_image, force_text=force_text, attach=False,
                title="星际战甲 · 世界状态监控")
        scope = cmd.get("scope") or "self"
        types = cmd.get("types") or None
        names = "、".join(worldstate.TYPES[t][0] for t in (types or [])
                         if t in worldstate.TYPES)
        user = ((ctx.get("user_id") or ctx.get("sender_id"))
                if ch == "group" else None)
        # 会话键已在 ws_monitor 内部按 (channel, target_id) 锁定：
        # 这里无论停自己还是停全群，都只动当前这个群 / 这个私聊。
        if scope == "all":
            stopped = ws_monitor.stop_monitor(ch, tid, types=types, everyone=True)
            msg = ("✅ 已停止本群所有人的监控%s，并清除本群保存的开关设置\n"
                   "（只影响本群，其它群和私聊的监控原样保留；"
                   "想重新开启，发「监控 警报」即可）。"
                   % ("（%s）" % names if names else ""))
            if not stopped:
                msg = "（本群没有进行中的监控）"
            return finalize(msg, ctx, force_image=force_image, force_text=force_text,
                            attach=False, title="星际战甲 · 世界状态监控")
        stopped = ws_monitor.stop_monitor(ch, tid, types=types, user=user)
        if not stopped:
            msg = ("（你名下没有进行中的监控）"
                   if user else "（本会话没有进行中的监控）")
            if user:
                msg += "\n要停掉本群所有人的监控，发「停止全部监控」。"
        else:
            msg = ("✅ 已停止你在本%s的监控%s\n"
                   "（本群其他成员、其它群与私聊都不受影响；"
                   "想重新开启，发「监控 警报」即可）。"
                   % ("群" if ch == "group" else "私聊",
                      "（%s）" % names if names else "")
                   if user else
                   "✅ 已停止本会话的世界状态监控，并清除已保存的开关设置\n"
                   "（想重新开启，发「活动=开启」或「监控 警报」即可）。")
        return finalize(msg, ctx, force_image=force_image, force_text=force_text,
                        attach=False, title="星际战甲 · 世界状态监控")
    if action == "monitor":
        ch, tid = _ctx_ids(ctx)
        if ch is None:
            return finalize("（无法识别会话，无法开启监控；群聊需在白名单内）", ctx,
                            force_image=force_image, force_text=force_text,
                            title="星际战甲 · 世界状态监控")
        filters = cmd.get("fissure_filter") or None
        bounty_toks = cmd.get("bounty_filter") or None
        if bounty_toks:
            filters = dict(filters or {})
            filters["bounty"] = bounty_toks
        # 记下发起人：之后这个会话的监控推送会 @ 他（私聊没有 @ 概念，不记）
        at_user = (ctx.get("user_id") or ctx.get("sender_id")) if ch == "group" else None
        ws_monitor.start_monitor(ch, tid, set(cmd["types"]), filters=filters,
                                 at_user=at_user)
        names = "、".join(worldstate.TYPES[t][0] for t in cmd["types"] if t in worldstate.TYPES)
        msg = ("✅ 已开始监控：%s\n每 %d 秒检查一次，内容有变化才推送"
               "（周期最小间隔 %d 秒，倒计时跳动不算变化）。"
               % (names, int(worldstate.WS_PUSH_SECONDS),
                  int(ws_monitor.active_min_gap())))
        if filters:
            if filters.get("bounty"):
                msg += ("\n🔎 只看赏金里的 %s（其余集团已忽略）。"
                        "\n💡 想改回全部集团，再发一次不带条件的「监控 赏金」即可。"
                        % "、".join(filters["bounty"]))
            else:
                conds = "、".join("「%s」" % " ".join(toks)
                                 for toks in filters.values() if toks)
                msg += ("\n🔎 只推送同时满足 %s 的%s条目（其余已忽略）。"
                        "\n💡 想改成整栏推送，再发一次不带条件的「监控 %s」即可。"
                        % (conds, "钢铁之路" if "steel" in filters else "普通", names))
        prio = [t for t in ws_monitor.priority_types() if t in set(cmd["types"])]
        if prio:
            pn = "、".join(worldstate.TYPES[t][0] for t in prio)
            msg += ("\n📌 %s 为主推：出现新条目立即单独推一条，不受周期限制"
                    "（%d 秒防抖）；刚开监控先记现状，不补发历史。"
                    % (pn, int(ws_monitor.priority_min_gap())))
        msg += ("\n💡 也可以发「活动=开启」「警报=关闭」单独开关主推（会存盘，重启后仍在）；"
                "\n发送「停止监控」只停你自己的（加「全部」才停本群所有人）。")
        return finalize(msg, ctx, force_image=force_image, force_text=force_text,
                        title="星际战甲 · 世界状态监控")
    if action == "monitor_hint":
        # 裸「监控」或「监控 不认识的词」：不静默全开，提示正确用法（防误开全量监控）
        return finalize(
            "⚠️ 「监控」需要明确指定内容，已停止，未开启任何监控。\n"
            "· 监控全部栏目：发完整的「监控全部」\n"
            "· 监控指定栏目：发「监控 警报」「监控 平原」「监控 裂缝」等\n"
            "· 查看可监控栏目：发「世界」或「帮助」\n"
            "· 停止自己的监控：发「停止监控」（只停某个栏目：停止监控 警报）",
            ctx, force_image=force_image, force_text=force_text, attach=False,
            title="星际战甲 · 世界状态监控")
    if action in ("query", "plan"):
        txt = worldstate.handle_query(cmd)
        title = ("星际战甲 · 更新计划" if action == "plan"
                 else "星际战甲 · 世界状态")
        return finalize(txt, ctx, force_image=force_image,
                        force_text=force_text, title=title,
                        covered=set(cmd.get("types") or ()))
    return finalize(worldstate.HELP_TEXT, ctx, force_image=force_image,
                    force_text=force_text, title="星际战甲 · 世界状态指令")


def handle_drop(text, ctx=None, force_image=False, force_text=False):
    """遗物 / 掉落查询的统一入口（数据源与缓存都在 drops 模块里）。"""
    cmd = drops.parse_command(text)
    return finalize(drops.format_reply(cmd), ctx,
                    force_image=force_image, force_text=force_text,
                    title="星际战甲 · 遗物 / 掉落",
                    foot="「遗物 后纪B10」看某遗物奖励；「掉落 名称」反查出自哪些遗物。")


def _ctx_ids(ctx):
    """从归一化后的 ctx 取出 (channel, target_id)，供监控注册/推送使用。
    channel 统一为 "group" | "private"；target_id 为群号/群OpenID 或 用户OpenID。"""
    ctx = ctx or {}
    channel = ctx.get("channel") or ctx.get("message_type")
    if channel == "group":
        return "group", ctx.get("group_id")
    if channel in ("private", "c2c"):
        tid = ctx.get("group_id") or ctx.get("user_id") or ctx.get("openid")
        return "private", tid
    return None, None

