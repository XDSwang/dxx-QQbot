#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_card 渲染 + qq_common 出图开关（离线，不联网，不依赖 Pillow 必须存在）。

覆盖：中文/emoji 清洗、单栏、双栏、分页、无字体回退、ImageReply 兼容 str、
「图 / 文字」前缀、auto/always/off 三种模式与字节阈值。
"""
import os
import sys
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qq_common as q
import render_card as R


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    return 1 if cond else 0


SHORT = "🔥 仲裁：赛德娜/Sangeru · 防御 · Infestation · Lv35-45"
LONG = "\n\n".join(
    "🔥 裂缝（共 6）\n" + "\n".join(
        "1. 节点%d · 间谍 · Grineer · 安魂\n   剩余：%d分11秒" % (i, 10 + i)
        for i in range(1, 7)
    )
    for _ in range(3)
)
MED = "\n\n".join(          # 单栏放不下、双栏刚好放得下
    "🔥 分组%d\n" % i + "\n".join("  行 %d-%d 内容填充" % (i, j) for j in range(1, 31))
    for i in range(1, 5)
)
HUGE = "\n\n".join(         # 双栏也放不下，必须分页
    "🔥 大组%d\n" % i + "\n".join("  行 %d-%d 内容填充" % (i, j) for j in range(1, 31))
    for i in range(1, 25)
)


def main():
    ok = total = 0

    # 1) emoji 清洗：emoji 去掉、中文与「·」保留
    total += 1
    ok += check("清 emoji 且保留中文标点", R._clean("⚔️ 突击 · Vay Hek") == "突击 · Vay Hek")
    total += 1
    ok += check("组合 emoji 也能清掉", R._clean("🛡️仲裁") == "仲裁")
    # 状态 emoji 不再被无声吞掉：转成可见的中文状态标签
    total += 1
    ok += check("订单行状态 emoji 转中文标签",
                R._clean("1. 🟢 InGame — 10 铂金") == "1. [游戏中] InGame — 10 铂金")
    total += 1
    ok += check("在线 emoji 转 [在线]",
                R._clean("2. 🔵 On — 20 铂金") == "2. [在线] On — 20 铂金")
    total += 1
    ok += check("离线 emoji 转 [离线]",
                R._clean("3. ⚪ Off — 30 铂金") == "3. [离线] Off — 30 铂金")
    total += 1
    ok += check("已有中文状态词不重复（🟢游戏中→游戏中）",
                R._clean("排序：🟢游戏中 → 🔵在线 → ⚪离线") == "排序：游戏中 → 在线 → 离线")
    segs = R._split_segments("1. [游戏中] InGame — 10 铂金",
                             {"InGame": (198, 40, 40)})
    total += 1
    ok += check("昵称片段按红色切出",
                any(s == "InGame" and c == (198, 40, 40) for s, c in segs))
    total += 1
    ok += check("非昵称片段保持墨色",
                all(c == R.INK for s, c in segs if s != "InGame"))
    total += 1
    ok += check("长昵称优先不让短名抢匹配",
                R._split_segments("Nickname", {"Nick": (1, 1, 1),
                                               "Nickname": (2, 2, 2)})[0][1] == (2, 2, 2))
    total += 1
    ok += check("传 colors 渲染不报错且仍是 PNG",
                R._load_fonts() is None or
                bool(R.render("1. 🟢 InGame — 10 铂金\n2. 🔵 On — 20 铂金",
                              colors={"InGame": (198, 40, 40),
                                      "On": (32, 92, 180)})))

    # 2) 块切分：按空行分组，首行为标题
    blocks = R._blocks(LONG)
    total += 1
    ok += check("按空行切出 3 个分组", len(blocks) == 3)
    total += 1
    ok += check("分组标题取首行（含 emoji，渲染前才清洗）",
                    R._clean(blocks[0]["title"]).startswith("裂缝"))

    # 3) 渲染：短文本单栏一张
    pngs = R.render(SHORT)
    if pngs is None:
        print("  （本机无 Pillow/中文字体，跳过渲染类用例）")
    else:
        total += 1
        ok += check("短文本渲染出 1 张", len(pngs) == 1)
        total += 1
        ok += check("返回真 PNG 字节（魔数 89 50）", pngs[0][:2] == b"\x89P")
        total += 1
        ok += check("PNG 体积合理（>5KB）", len(pngs[0]) > 5000)

        # 4) 中等长度：单栏放不下就自动改双栏
        from io import BytesIO
        from PIL import Image as _I

        med = R.render(MED)
        total += 1
        ok += check("中等长度仍能出图", bool(med))
        if med:
            w, h = _I.open(BytesIO(med[0])).size
            total += 1
            ok += check("自动改双栏（宽 %d = %d）" % (w, R.W_DOUBLE), w == R.W_DOUBLE)
            total += 1
            ok += check("双栏高度受控（%d ≤ %d）" % (h, R.MAX_H + 900), h <= R.MAX_H + 900)

        # 5) 极长：双栏也放不下，按块分页且每页单栏
        huge = R.render(HUGE)
        total += 1
        ok += check("极长内容分页（%d 页）" % len(huge or []), bool(huge) and len(huge) > 1)
        if huge:
            total += 1
            ok += check("分页每页回到单栏宽",
                        all(_I.open(BytesIO(p)).size[0] == R.W_SINGLE for p in huge))
            total += 1
            ok += check("分页数量有上限（≤4 页）", len(huge) <= 4)

    # 5) 无字体/无 Pillow：render 返回 None，不抛异常
    with patch.object(R, "_load_fonts", lambda: None):
        total += 1
        ok += check("无可用字体时 render 返回 None", R.render(LONG) is None)
        total += 1
        r_nofont = q.maybe_image(LONG)
        ok += check("无字体时退回纯文字（不抛异常）", isinstance(r_nofont, str))
        total += 1
        ok += check("退回文字含原始正文 + 图片未生成告警",
                    LONG in r_nofont and "图片未生成" in r_nofont)

    # 6) ImageReply 仍然是 str，旧调用方不受影响
    ir = q.ImageReply("文本内容", [b"x"])
    total += 1
    ok += check("ImageReply 是 str 子类", isinstance(ir, str))
    total += 1
    ok += check("可切片/判真/取长度", ir[:2] == "文本" and bool(ir) and len(ir) == 4)
    total += 1
    ok += check("pngs 挂在实例上", ir.pngs == [b"x"])

    # 7) 前缀解析
    total += 1
    ok += check("「图 裂缝」剥出强制图", q.split_style_prefix("图 裂缝") == ("裂缝", True, False))
    total += 1
    ok += check("「图片：仲裁」剥出强制图", q.split_style_prefix("图片：仲裁") == ("仲裁", True, False))
    total += 1
    ok += check("「文字 全部」剥出强制文字", q.split_style_prefix("文字 全部") == ("全部", False, True))
    total += 1
    ok += check("无前缀时原样返回", q.split_style_prefix("裂缝") == ("裂缝", False, False))

    # 8) 三种模式 + 阈值
    long_text = LONG * 3
    with patch.dict(os.environ, {"WM_WS_CARD": "off"}, clear=False):
        total += 1
        ok += check("off 模式不出图", not hasattr(q.maybe_image(long_text), "pngs"))
    with patch.dict(os.environ, {"WM_WS_CARD": "always"}, clear=False):
        got = q.maybe_image(SHORT)
        total += 1
        ok += check("always 模式连短文本也出图（或无字体时退回）",
                    hasattr(got, "pngs") or got == SHORT)
    with patch.dict(os.environ, {"WM_WS_CARD": "auto", "WM_WS_CARD_MIN": "100000"},
                     clear=False):
        total += 1
        ok += check("auto + 大阈值时不出图", not hasattr(q.maybe_image(LONG), "pngs"))
    with patch.dict(os.environ, {"WM_WS_CARD": "auto", "WM_WS_CARD_MIN": "10"},
                     clear=False):
        got = q.maybe_image(LONG)
        total += 1
        ok += check("auto + 小阈值时出图（或无字体时退回）",
                    hasattr(got, "pngs") or got == LONG)

    # 9) 强制文字优先级最高
    with patch.dict(os.environ, {"WM_WS_CARD": "always"}, clear=False):
        total += 1
        ok += check("force_text 压过 always", q.maybe_image(LONG, force_text=True) == LONG)

    print("\n结果：通过 %d / 失败 %d" % (ok, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
