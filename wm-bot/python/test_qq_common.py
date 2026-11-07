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

    # 3b) 不带 D/XX/ 前缀：所有交互指令必须静默（防群聊误触发），主推不受影响
    for w in ("帮助", "世界", "平原", "自检", "wm Boltor", "监控 裂缝", "你好啊"):
        r = H(w, PRIV)
        total += 1
        ok += check("handler %r 无前缀静默（None）" % w, r is None)

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

    print("\n结果：通过 %d / 失败 %d" % (ok, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
