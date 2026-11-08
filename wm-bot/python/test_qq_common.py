#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_common 路由 + 全局「全部指令」帮助（离线，不联网、不依赖 data/ 配置）。"""
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qq_common as q
import worldstate as ws

# 用宽松 handler（allow_all）绕过群白名单，专注测路由与帮助内容
H = q.make_handler(allow_all=True)
PRIV = {"channel": "private"}


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    return 1 if cond else 0


def main():
    ok = 0
    total = 0

    # 1) 全局帮助词识别
    for w in ("帮助", "help", "?", "菜单", "指令", "命令", "功能", "总览", "全部指令"):
        total += 1
        ok += check("全局帮助词命中: %r" % w, q._is_global_help(w))
    for w in ("平原", "你好", "xyz", "WM", "监控 裂缝"):
        total += 1
        ok += check("非帮助词不命中: %r" % w, not q._is_global_help(w))

    # 2) 总览内容合并了 WM 与世界状态两段
    full = q.build_full_help()
    total += 1
    ok += check("总览含 WM 指令格式", "指令格式" in full and "WM <物品名>" in full)
    total += 1
    ok += check("总览含世界状态说明", "世界状态查询" in full and "监控" in full)
    total += 1
    ok += check("总览有可随时复看提示", "随时发" in full)

    # 3) handler 路由（交互指令必须带 D/XX/ 前缀）：帮助词 -> 总览；世界状态词 -> 世界专项
    for w in ("D/XX/帮助", "D/XX/?", "D/XX/菜单", "D/XX/指令"):
        r = H(w, PRIV)
        total += 1
        ok += check("handler %r 返回总览" % w, isinstance(r, str) and "全部指令一览" in r)

    r_world = H("D/XX/世界", PRIV)
    total += 1
    ok += check("handler 'D/XX/世界' 返回世界专项（非总览）",
                isinstance(r_world, str) and "世界状态查询" in r_world
                and "全部指令一览" not in r_world)

    r_query = H("D/XX/平原", PRIV)
    total += 1
    ok += check("handler 'D/XX/平原' 走查询（非帮助）",
                isinstance(r_query, str) and "全部指令一览" not in r_query)

    # 3.5) 裸「帮助」两个字就出菜单（不要求 D/XX/ 前缀）
    GRP = {"channel": "group", "group_id": "G1"}
    for w in ("帮助", "help", "菜单", "指令", "命令", "功能", "总览"):
        r = H(w, GRP)
        total += 1
        ok += check("裸 %r（群内无前缀）也返回总览" % w,
                    isinstance(r, str) and "全部指令一览" in r)
    total += 1
    ok += check("裸「?」仍不触发（单字符易误触，需带前缀）", H("?", GRP) is None)
    total += 1
    ok += check("带前缀 D/XX/? 仍可出总览",
                isinstance(H("D/XX/?", GRP), str))
    total += 1
    ok += check("裸「帮助」不影响其它闲聊静默", H("今天打什么", GRP) is None)

    # 3.6) 帮助卡片配色：查询指令红、其它指令蓝
    cols = q.help_colors()
    total += 1
    ok += check("查询词是红色（平原/裂缝/WM/计划）",
                all(cols.get(w) == q.HELP_RED
                    for w in ("平原", "裂缝", "WM", "计划", "赏金")))
    total += 1
    ok += check("其它指令是蓝色（监控/停止监控/文字/自检）",
                all(cols.get(w) == q.HELP_BLUE
                    for w in ("监控", "停止监控", "文字", "自检")))
    total += 1
    ok += check("红蓝不重叠（同一片段的颜色唯一）",
                not (set(q._HELP_QUERY_WORDS) & set(q._HELP_OTHER_WORDS)))
    import render_card as R
    segs = dict(R._split_segments("活动=开启 关闭 帮助", cols))
    total += 1
    ok += check("「活动=开启」整段取蓝色（长片段优先于「活动」）",
                segs.get("活动=开启") == q.HELP_BLUE)
    total += 1
    ok += check("帮助卡片能渲染出图", bool(R.render(q.build_full_help(),
                                                    title="帮助", colors=cols)))

    # 3b) 不带 D/XX/ 前缀：除「帮助」类词外，其它交互指令必须静默（防群聊误触发）
    #     （「帮助」两个字就要出菜单，是用户明确要求的例外，见 3.5）
    for w in ("世界", "平原", "自检", "wm Boltor", "监控 裂缝", "你好啊"):
        r = H(w, PRIV)
        total += 1
        ok += check("handler %r 无前缀静默（None）" % w, r is None)

    # 3c) 完整指令才算数：带前缀但没精确命中 → 一律静默（不猜、不提示）
    #     理由：模糊/子串/后缀匹配会撞上正常聊天（例如「希图思」这种错别字、
    #     或聊天里出现栏目字眼），机器人跑去接话比"没回复"更糟。
    for w in ("D/XX/随便乱打", "D/XX/希图思", "D/XX/裂缝裂缝",
              "D/XX/今天打什么", "D/XX/平原好玩吗我想去"):
        r = H(w, PRIV)
        total += 1
        ok += check("handler %r 未精确命中 → 静默（None）" % w, r is None)

    # 4) 世界状态 HELP_WORDS 不再含 帮助/help/?（避免与全局总览冲突）
    total += 1
    ok += check("世界状态 HELP_WORDS 不含 帮助/help/?",
                not ({"帮助", "help", "?" } & ws.HELP_WORDS))

    # 5) 排障「自检」命令：识别 + handler 直接回诊断文字（不进图片/不静默）
    for w in ("自检", "诊断", "diy", "diagnose", "状态"):
        total += 1
        ok += check("排障词命中: %r" % w, q.is_diagnostic(w))
    for w in ("平原", "WM boltor", "你好", "自检一下"):
        total += 1
        ok += check("非排障词不命中: %r" % w, not q.is_diagnostic(w))
    r_diag = H("D/XX/自检", PRIV)
    total += 1
    ok += check("handler 'D/XX/自检' 返回诊断文字",
                isinstance(r_diag, str) and "机器人自检" in r_diag)
    total += 1
    ok += check("诊断文字含 render_card / Markdown / 出图模式",
                "render_card" in r_diag and "Markdown" in r_diag
                and "出图模式" in r_diag)
    total += 1
    ok += check("诊断不是图片（pngs 为空）",
                not getattr(r_diag, "pngs", None))

    # 6) maybe_image 在 render_card 不可用时，把失败原因直接带进回复文字
    import builtins
    real_import = builtins.__import__

    def _fail_render(*a, **k):
        if a[0] == "render_card":
            raise ImportError("simulated: no render_card on phone")
        return real_import(*a, **k)

    builtins.__import__ = _fail_render
    try:
        r_fail = q.maybe_image("【物品】测试\nWM 在售\n🟢 A — 1 铂金 ×1\n")
        total += 1
        ok += check("render_card 缺失时退回纯文字（str）", isinstance(r_fail, str))
        total += 1
        ok += check("回复带「图片未生成」告警，原因可见", "图片未生成" in r_fail)
    finally:
        builtins.__import__ = real_import

    # 7) @ 标记兜底剥离：三种残留形式都要能被剥掉，且句中 @ 不受影响
    cases = [
        ("[CQ:at,qq=123] D/XX/ 平原", "D/XX/ 平原"),
        ("<@!A1B2C3> D/XX/ 平原", "D/XX/ 平原"),
        ("@自动化服务查询 D/XX/ 平原", "D/XX/ 平原"),
        ("@甲 @乙 D/XX/ 平原", "D/XX/ 平原"),
        ("＠123 自检", "自检"),
    ]
    for raw, want in cases:
        total += 1
        got = q.strip_mentions(raw)[0]
        ok += check("剥 @ 标记 %r -> %r" % (raw, want), got == want)
    total += 1
    ok += check("句中 @ 保持原样（不误删正文）",
                q.strip_mentions("D/XX/ 我 @某人 打平原")[0] == "D/XX/ 我 @某人 打平原")
    total += 1
    ok += check("剥 @ 标记同时回传被剥掉的标识",
                q.strip_mentions("@甲 @乙 D/XX/ 平原")[1] == ["甲", "乙"])

    # 7.1) 「@机器人 + D/XX/ 指令」必须照常响应（线上故障：旧实现留着 CQ 码，前缀匹配失败）
    GAT = {"channel": "group", "group_id": "G1"}
    for raw in ("@自动化服务查询 D/XX/自检",
                "[CQ:at,qq=123]D/XX/自检",
                "<@!123>D/XX/自检"):
        total += 1
        r = H(raw, dict(GAT))
        ok += check("%r 带前缀仍响应" % raw, isinstance(r, str) and "出图模式" in r)

    # 7.2) @ 的是自己（标识与 self_id 对得上）时，免前缀也成立
    for raw in ("@123 自检", "＠123 自检", "<@!123> 自检"):
        total += 1
        r = H(raw, dict(GAT, self_id="123"))
        ok += check("%r 免前缀响应（@ 的是自己）" % raw,
                    isinstance(r, str) and "出图模式" in r)
    total += 1
    ok += check("@ 的是别人（self_id 对不上）时仍静默",
                H("@小明 自检", dict(GAT, self_id="123")) is None)

    print("\n结果：通过 %d / 失败 %d" % (ok, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
