#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ws_layout.py — 裂缝分组排序 + 平原带赏金 的离线回归（零联网）

覆盖：
  - 遗物等级：古→前→中→后→安魂→全能，中英文别名 / 简写 / 未知等级
  - 九重天判定：比邻星域 / Proxima / Empyrean / Railjack / 常规星球
  - 裂缝：常规星球在上、九重天在下，两组各自按等级再按剩余时间排
  - 空组不显示、精简模式两组都露面、过期过滤、钢铁裂缝同样生效
  - 平原：五处时间各自带自己的赏金、集团映射、单条平原指令、无数据不报错
  - 稳定签名：赏金 / 裂缝倒计时跳动不算「内容变化」，不回归刷屏 bug
运行：python test_ws_layout.py
"""
import os
import re
import sys
import time
from unittest.mock import patch

os.environ["WS_SOURCE"] = "mock"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import worldstate as ws
import ws_monitor
import render_card as R

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  FAIL: " + name)


NOW = time.time()


def fiss(node, tier, secs=600, mt="歼灭", enemy="Grineer"):
    return {"node": node, "missionType": mt, "enemy": enemy,
            "tier": tier, "expiry": NOW + secs}


def jobs(n, obj="回收"):
    return [{"objective": f"{obj}{i + 1}", "minLevel": 5 + i * 10,
             "maxLevel": 15 + i * 10, "stages": 3} for i in range(n)]


BOARD = [
    {"name": "Ostron", "tag": "CetusSyndicate", "expiry": NOW + 1800, "jobs": jobs(3, "希图斯赏")},
    {"name": "索拉里斯", "tag": "SolarisSyndicate", "expiry": NOW + 1800, "jobs": jobs(3, "奥布赏")},
    {"name": "英择谛", "tag": "EntratiSyndicate", "expiry": NOW + 1800, "jobs": jobs(3, "魔胎赏")},
]


def plain_state():
    return {
        "cetusCycle": {"isDay": True, "expiry": NOW + 5000},
        "vallisCycle": {"isWarm": False, "expiry": NOW + 2500},
        "cambionCycle": {"isFass": True, "expiry": NOW + 800},
        "duviriCycle": {"state": "愤怒", "expiry": NOW + 1200},
        "zarimanCycle": {"state": "Grineer", "expiry": NOW + 900},
        "bountyBoard": BOARD,
    }


def test_tier():
    print("[遗物等级归一与排序]")
    for raw, want in (("古纪", "古纪"), ("Lith", "古纪"), ("古", "古纪"),
                      ("前纪", "前纪"), ("Meso", "前纪"), ("中纪", "中纪"),
                      ("Neo", "中纪"), ("后纪", "后纪"), ("Axi", "后纪"),
                      ("安魂", "安魂"), ("Requiem", "安魂"),
                      ("全能", "全能"), ("Omnia", "全能")):
        check(f"{raw} -> {want}", ws.tier_name(raw) == want)
    check("未知等级原样保留", ws.tier_name("???") == "???")
    check("空等级返回空串", ws.tier_name(None) == "")
    ranks = [ws.tier_rank(x) for x in ("安魂", "全能", "后纪", "古纪", "前纪", "中纪")]
    check("六档 rank 互不相同且落在 0..5", ranks == [4, 5, 3, 0, 1, 2])
    check("未知等级排最后", ws.tier_rank("???") == len(ws.TIER_ORDER))
    check("TIER_ORDER 顺序就是古前中后安魂全能",
          ws.TIER_ORDER == ("古纪", "前纪", "中纪", "后纪", "安魂", "全能"))


def test_railjack():
    print("[九重天判定]")
    yes = ("地球比邻星域/本达尔星团", "面纱比邻星域/R-9 星云",
           "Earth Proxima/Bendal Cluster", "Veil Proxima/R-9 Cloud",
           "Empyrean/Kuva", "Railjack/Neptune", "九重天/某节点")
    no = ("金星/Ishtar", "赤毒要塞/Pago", "虚空/风王塔", "扎里曼号/永视弧域",
          "赛德娜/Kelpie", "月球/Circulus")
    for n in yes:
        check(f"识别九重天 {n}", ws.is_railjack(fiss(n, "古纪")))
    for n in no:
        check(f"常规星球不误判 {n}", not ws.is_railjack(fiss(n, "古纪")))
    check("system 字段也参与判定",
          ws.is_railjack({"node": "某节点", "system": "Saturn Proxima"}))
    check("空字典不炸", not ws.is_railjack({}))


def test_fissure_layout():
    print("[裂缝：常规在上 / 九重天在下 / 组内排序]")
    normal = [
        fiss("赛德娜/Kelpie", "后纪", 900),
        fiss("金星/Ishtar", "古纪", 800),
        fiss("赤毒要塞/Pago", "安魂", 700),
        fiss("虚空/风王塔", "中纪", 600),
        fiss("扎里曼号/永视弧域", "全能", 500),
        fiss("谷神星/Casta", "前纪", 400),
        fiss("火卫一/Skyresh", "前纪", 300),   # 同档：剩余短的排前
    ]
    rj = [
        fiss("面纱比邻星域/R-9 星云", "后纪", 850),
        fiss("地球比邻星域/本达尔星团", "古纪", 750),
        fiss("土星比邻星域/水域星团", "前纪", 650),
        fiss("海王星比邻星域/女娲之矿", "中纪", 550),
        fiss("金星比邻星域/虹桥回声", "古纪", 450),
    ]
    out = ws._fmt_fissure_list(normal + rj, "🔥 裂缝")
    lines = out.splitlines()

    check("有常规星球分区", any("常规星球" in x for x in lines))
    check("有九重天分区", any("九重天" in x for x in lines))
    i_norm = next(i for i, x in enumerate(lines) if "常规星球" in x)
    i_rj = next(i for i, x in enumerate(lines) if "九重天" in x)
    check("常规星球在上面", i_norm < i_rj)
    check("两组之间留空行（卡片另起一块）", lines[i_rj - 1] == "")
    check("常规标题写共 7", "共 7" in lines[i_norm])
    check("九重天标题写共 5", "共 5" in lines[i_rj])

    def bands(segment):
        return [x.strip("— ") for x in segment if x.startswith("— ")]

    check("常规组等级顺序 古前中后安魂全能",
          bands(lines[i_norm:i_rj]) == ["古纪", "前纪", "中纪", "后纪", "安魂", "全能"])
    check("九重天组自己按等级排",
          bands(lines[i_rj:]) == ["古纪", "前纪", "中纪", "后纪"])
    check("九重天不与常规混排（等级标题在两组各出现一次）",
          bands(lines).count("古纪") == 2)

    norm_text = "\n".join(lines[i_norm:i_rj])
    check("同档按剩余时间升序（Skyresh 剩 5 分排在 Casta 前）",
          norm_text.index("Skyresh") < norm_text.index("Casta"))
    rj_text = "\n".join(lines[i_rj:])
    check("九重天同档也按剩余时间升序",
          rj_text.index("虹桥回声") < rj_text.index("本达尔星团"))
    check("首个九重天节点出现在所有常规节点之后",
          out.index("本达尔星团") > max(out.index("Ishtar"), out.index("Casta"),
                                        out.index("Kelpie"), out.index("风王塔"),
                                        out.index("Pago"), out.index("永视弧域"),
                                        out.index("Skyresh")))
    check("条目行保留节点/任务/敌人/等级",
          "1. 金星/Ishtar · 歼灭 · Grineer · 古纪" in out)
    check("条目行保留剩余时间", "剩余：" in out)
    check("每组编号从 1 重新开始",
          any(re.match(r"^1\. ", x) for x in lines[i_rj:]))

    # 单组：不要出现空组标题
    only_normal = ws._fmt_fissure_list(normal, "🔥 裂缝")
    check("全常规时不显示九重天", "九重天" not in only_normal)
    check("全常规时标题不带分组后缀", only_normal.startswith("🔥 裂缝（共 7）"))
    only_rj = ws._fmt_fissure_list(rj, "🔥 裂缝")
    check("全九重天时不显示常规星球", "常规星球" not in only_rj)
    check("全九重天时标题不带分组后缀", only_rj.startswith("🔥 裂缝（共 5）"))

    # 精简：两组都得露面
    old = ws._COMPACT
    try:
        ws._COMPACT = True
        brief = ws._fmt_fissure_list(normal + rj, "🔥 裂缝")
        bl = brief.splitlines()
        bi_rj = next(i for i, x in enumerate(bl) if "九重天" in x)
        check("精简模式仍有九重天分区", bi_rj > 0)
        check("精简模式常规组 2 条", len([x for x in bl[:bi_rj] if re.match(r"^\d+\. ", x)]) == 2)
        check("精简模式九重天组 2 条", len([x for x in bl[bi_rj:] if re.match(r"^\d+\. ", x)]) == 2)
        check("精简模式提示还有剩余", "还有" in brief)
    finally:
        ws._COMPACT = old

    # 过期 / 空
    expired = [fiss("金星/Ishtar", "古纪", -10), fiss("金星/Elara", "前纪", -20)]
    check("全过期如实提示", "都已结束" in ws._fmt_fissure_list(expired, "🔥 裂缝"))
    mixed = normal[:2] + expired
    m = ws._fmt_fissure_list(mixed, "🔥 裂缝")
    check("混合时过期条目不列出", "Elara" not in m and "共 2" in m)
    check("混合时注明另有几条已结束", "另有 2 条已结束" in m)
    check("空列表给当前无", "当前无" in ws._fmt_fissure_list([], "🔥 裂缝"))
    check("没有 expiry 的条目仍保留",
          "Ishtar" in ws._fmt_fissure_list([{"node": "金星/Ishtar", "tier": "古纪"}], "🔥 裂缝"))

    # 钢铁裂缝走同一个函数
    so = ws._fmt_steel({"steelPath": {"current": normal[:2] + rj[:2]}})
    check("钢铁裂缝也分区", "常规星球" in so and "九重天" in so)
    check("钢铁裂缝标题正确", so.startswith("⚙️ 钢铁裂缝 · 常规星球"))

    # 未知等级归到「其它」，不插队
    unk = ws._fmt_fissure_list([fiss("金星/A", "???"), fiss("金星/B", "古纪")], "🔥 裂缝")
    check("未知等级单独成『其它』组", "— 其它 —" in unk)
    check("未知等级排在已知之后", unk.index("— 其它 —") > unk.index("— 古纪 —"))

    check("帮助写明分组与排序",
          "九重天" in ws.HELP_TEXT and "古→前→中→后→安魂→全能" in ws.HELP_TEXT)


def test_bounty_layout():
    print("[平原：时间 + 自己的赏金]")
    s = plain_state()

    check("cetus 命中 Ostron", [b["name"] for b in ws._bounty_for(s, "cetus")] == ["Ostron"])
    check("vallis 命中索拉里斯", [b["name"] for b in ws._bounty_for(s, "vallis")] == ["索拉里斯"])
    check("cambion 命中英择谛", [b["name"] for b in ws._bounty_for(s, "cambion")] == ["英择谛"])
    check("双衍没有赏金不硬凑", ws._bounty_for(s, "duviri") == [])
    check("扎里曼没有赏金不硬凑", ws._bounty_for(s, "zariman") == [])

    cet = ws._fmt_cetus(s)
    check("希图斯时间行在第一行", cet.startswith("希图斯（地球平原）"))
    check("希图斯后面紧跟自己的赏金", "赏金 · Ostron" in cet)
    check("希图斯不含别人的赏金", "索拉里斯" not in cet and "英择谛" not in cet)
    check("赏金带任务目标", "希图斯赏1 Lv5-15 · 3 阶段" in cet)
    val = ws._fmt_vallis(s)
    check("奥布山谷只带索拉里斯", "索拉里斯" in val and "Ostron" not in val)
    cam = ws._fmt_cambion(s)
    check("魔胎之境只带英择谛", "英择谛" in cam and "Ostron" not in cam)

    allp = ws._fmt_cycles(s)
    check("平原组合含五处", all(k in allp for k in
                              ("希图斯", "奥布山谷", "魔胎之境", "双衍王境", "扎里曼")))
    check("五处之间空行分块", "\n\n" in allp)
    check("组合里三个集团都在", all(k in allp for k in ("Ostron", "索拉里斯", "英择谛")))
    blocks = allp.split("\n\n")
    check("平原正好分成五块", len(blocks) == 5)
    for i, gname in enumerate(("Ostron", "索拉里斯", "英择谛")):
        check(f"{gname} 在自己的平原块里", gname in blocks[i])
        check(f"{gname} 紧跟所属平原时间行",
              blocks[i].splitlines()[1].strip().startswith("赏金 ·"))
    check("双衍/扎里曼块不带赏金",
          "赏金 ·" not in blocks[3] and "赏金 ·" not in blocks[4])

    # 单个平原指令也带赏金
    check("单发「希图斯」也带赏金", "赏金 · Ostron" in ws.TYPES["cetus"][1](s))
    check("单发「奥布山谷」也带赏金", "赏金 · 索拉里斯" in ws.TYPES["vallis"][1](s))
    check("单发「魔胎之境」也带赏金", "赏金 · 英择谛" in ws.TYPES["cambion"][1](s))

    # 没有赏金数据：不报错、不出现空标题
    no_board = dict(s)
    no_board["bountyBoard"] = []
    nb = ws._fmt_cycles(no_board)
    check("无赏金时不报错且含五处", "希图斯" in nb and "扎里曼" in nb)
    check("无赏金时不出现赏金标题", "赏金 ·" not in nb)
    check("无赏金时时间行仍是单行", "希图斯（地球平原）：☀️ 白天" in nb)

    # 集团赏金（bounties.groups）：扎里曼的坚守者、火卫二的科维兽
    s2 = dict(s)
    s2["bounties"] = {"expiry": NOW + 3000, "groups": [
        {"tag": "ZarimanSyndicate", "name": "坚守者",
         "jobs": [{"node": "奥金工场", "mission": "虚空决战",
                   "challenge": "击杀 2 名虚空天使"}]},
        {"tag": "EntratiLabSyndicate", "name": "科维兽",
         "jobs": [{"node": "恶涌", "mission": "刺杀",
                   "challenge": "激活 1 个虚空泉脉"}]},
    ]}
    check("扎里曼命中坚守者",
          [b["name"] for b in ws._bounty_for(s2, "zariman")] == ["坚守者"])
    check("火卫二同时命中英择谛与科维兽",
          [b["name"] for b in ws._bounty_for(s2, "cambion")] == ["英择谛", "科维兽"])
    check("双衍仍不硬凑", ws._bounty_for(s2, "duviri") == [])
    zar = ws._fmt_zariman(s2)
    check("扎里曼块带集团赏金", "集团赏金 · 坚守者" in zar and "虚空天使" in zar)
    check("集团赏金一行写完节点·任务·挑战", "奥金工场 · 虚空决战 · 击杀 2 名虚空天使" in zar)
    check("火卫二块带科维兽", "集团赏金 · 科维兽" in ws._fmt_cambion(s2))

    # 回廊：英文武器名后面补中文（查不到就保持英文，不瞎编）
    circ = {"circuit": {"normal": ["Ivara", "Inaros"],
                        "hard": ["Braton", "Lato", "Kunai"]}}
    cl = "\n".join(ws._circuit_lines(circ, ""))
    check("灵化武器带中文名", "Braton（布莱顿）" in cl and "Kunai（苦无）" in cl)
    check("中文表查不到的战甲保持英文", "Ivara" in cl and "Ivara（" not in cl)

    # 地球黑夜：直接给夜灵三傻顺序
    night = dict(s)
    night["cetusCycle"] = {"isDay": False, "expiry": NOW + 600}
    check("地球黑夜给三傻", "Teralyst（兆力使）" in ws._fmt_cetus(night))
    check("地球白天不提三傻", "Teralyst" not in ws._fmt_cetus(s))

    # 活动：描述里只有地名时补星球
    ev = {"events": [{"description": "热美亚裂缝（奥布山谷）", "expiry": NOW + 600}]}
    check("活动补所在星球", "· 金星" in ws._fmt_events(ev))
    ev2 = {"events": [{"description": "尸鬼净化（希图斯 · 地球）", "expiry": NOW + 600}]}
    check("已有星球名不重复加", ws._fmt_events(ev2).count("地球") == 1)

    # 精简模式：每处只给 2 条
    old = ws._COMPACT
    try:
        ws._COMPACT = True
        brief = ws._fmt_cetus(s)
        check("精简模式每处赏金 2 条", brief.count("     - ") == 2)
        check("精简模式提示看全部", "发「赏金」看全部" in brief)
    finally:
        ws._COMPACT = old

    # 独立「赏金」栏目不受影响
    check("独立赏金栏目仍列三个集团",
          all(k in ws._fmt_bounty(s) for k in ("Ostron", "索拉里斯", "英择谛")))

    # 数据源偶尔不给目标名：不留在卡片里一个孤零零的问号
    blank = dict(s)
    blank["bountyBoard"] = [{"name": "Ostron", "tag": "CetusSyndicate",
                             "expiry": NOW + 1800,
                             "jobs": [{"objective": "?", "minLevel": 5,
                                       "maxLevel": 15, "stages": 3}]}]
    check("赏金目标缺失写「目标未给」", "目标未给 Lv5-15 · 3 阶段" in ws._fmt_cetus(blank))
    check("独立赏金栏目同样处理", "目标未给" in ws._fmt_bounty(blank))

    # 平原状态里的蓝圆点不能被当成 WM 卖家「在线」状态
    check("WM 卡片仍把状态 emoji 转中文标签",
          R._clean("2. 🔵 On — 20 铂金") == "2. [在线] On — 20 铂金")
    check("世界状态卡片不做状态替换",
          R._clean("🔵 温德(Vome)", status=False) == "温德(Vome)")
    check("世界状态卡片只删 emoji 不添标签",
          R._clean("🟢 白天 · 剩余 1小时", status=False) == "白天 · 剩余 1小时")

    # 查询入口端到端（只读我们喂进去的 state，不联网）
    with patch.object(ws, "get_state", lambda: s), \
         patch.object(ws, "ensure_active", lambda: None), \
         patch.object(ws, "activity_touch", lambda: None):
        q = ws.handle_query({"action": "query", "types": ["cycles"]})
    check("handle_query 平原含赏金", "赏金 · Ostron" in q and "赏金 · 英择谛" in q)
    check("handle_query 平原只输出一次希图斯", q.count("希图斯（地球平原）") == 1)

    # 赏金倒计时跳动不能让周期推送签名失效
    a = ws._fmt_cetus(s)
    s2 = plain_state()
    for b in s2["bountyBoard"]:
        b["expiry"] = NOW + 1700
    b = ws._fmt_cetus(s2)
    check("赏金倒计时变化不算内容变化",
          ws_monitor.stable_sig(a) == ws_monitor.stable_sig(b))
    s3 = plain_state()
    s3["bountyBoard"][0]["jobs"][0]["objective"] = "改了"
    check("赏金目标变化算内容变化",
          ws_monitor.stable_sig(a) != ws_monitor.stable_sig(ws._fmt_cetus(s3)))
    f1 = ws._fmt_fissure_list([fiss("金星/A", "古纪", 600)], "🔥 裂缝")
    f2 = ws._fmt_fissure_list([fiss("金星/A", "古纪", 500)], "🔥 裂缝")
    check("裂缝倒计时变化不算内容变化",
          ws_monitor.stable_sig(f1) == ws_monitor.stable_sig(f2))


def main():
    test_tier()
    test_railjack()
    test_fissure_layout()
    test_bounty_layout()
    print(f"\n结果：通过 {PASS} / 失败 {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
