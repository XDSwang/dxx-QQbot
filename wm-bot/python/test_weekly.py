#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周轮换（科研 / 执刑官 / 沉沦之地 / 钢铁商店 / 回廊预告 / 日历）回归测试。

全部离线：用一段手工拼的 flight HTML 冒充真实页面，验证
  1) initialData 反解（引号转义 / 取不到时返回 None）
  2) 七个板块的渲染要点（科研的偏差与变量、执刑官三节点、商店本周/下周、日历 kind）
  3) 指令精确匹配（多一个字就算闲聊）
  4) 缓存策略：未过期不重抓、过期先刷新、刷新失败沿用旧数据并标 stale
  5) 折行不超宽、说明行缩进对齐

不写任何真实 data/ 文件（WS_WEEKLY_FILE 指向临时目录）。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import weekly


def _page(obj):
    """拼一段与真实站点同形状的 flight HTML：JSON 里的引号被转义成 \\"。"""
    esc = json.dumps(obj, ensure_ascii=False).replace("\\", "\\\\").replace('"', '\\"')
    return ('<html><body><script>self.__next_f.push([1,"6:C\\n'
            '8:[\\"$\\",\\"$L14\\",null,{\\"view\\":\\"weekly\\",\\"initialData\\":'
            + esc + '}"]);</script></body></html>')


# 统一用「3 天后到期」：只影响「剩余 …」的长度，断言不看具体时间
EXPIRY = int((time.time() + 3 * 86400) * 1000)

SAMPLE = {
    "syndicates": [],
    "archimedea": [
        {"type": "深层科研", "expiry": EXPIRY, "missions": [
            {"faction": "低语者", "mission": "歼灭",
             "deviations": [{"name": "密闭装甲", "desc": "非弱点命中对敌人造成的伤害降低 90%。"}],
             "risks": [{"name": "吸血界影", "desc": "行动缓慢且不死的双衍王境界影。"}]},
            {"faction": "低语者", "mission": "生存",
             "deviations": [{"name": "殁世机甲激增", "desc": "殁世机甲出现的频率更高。"}],
             "risks": []},
        ]},
        {"type": "时光科研", "expiry": EXPIRY, "missions": [
            {"faction": "炽蛇军", "mission": "防御",
             "deviations": [{"name": "噪音抑制", "desc": "无人机在弗莱尔上方飞行。"}],
             "risks": [{"name": "敌军复生", "desc": "敌人的生命值会缓慢恢复。"}]},
        ]},
    ],
    "sortie": {"boss": "鬣狗群", "missions": []},
    "archonHunt": {
        "boss": "执刑官诡文枭主", "shard": "蔚蓝执刑官 Tau 融造源力石",
        "expiry": EXPIRY,
        "missions": [
            {"node": "Cambria", "system": "地球", "faction": "Grineer", "mission": "移动防御"},
            {"node": "Everest", "system": "地球", "faction": "Grineer", "mission": "拦截"},
            {"node": "Oro", "system": "地球", "faction": "Grineer", "mission": "刺杀"},
        ]},
    "descents": [{"floor": 1, "name": "炼狱 1", "expiry": EXPIRY, "challenges": [
        {"index": 1, "mode": "收集", "challenge": "坠落的碎片", "auras": []},
        {"index": 2, "mode": "", "challenge": "", "npc": "玛丽的圣所", "auras": []},
        {"index": 3, "mode": "挖掘", "challenge": "", "auras": []},
    ]}],
    "teshin": {"offer": "霰弹枪裂罅 Mod", "expiry": EXPIRY,
               "weeks": [{"offer": "霰弹枪裂罅 Mod"}, {"offer": "Umbra Forma 蓝图"},
                         {"offer": "赤毒 ×50,000"}]},
    "duviriCircuit": {
        "expiry": EXPIRY,
        "normal": [{"name": "Ivara"}, {"name": "Inaros"}, {"name": "Titania"}],
        "hard": [{"name": n} for n in ("布莱顿", "拉托", "空刃", "帕里斯", "苦无")],
        "normalWeeks": [{"items": [{"name": "Ivara"}]},
                        {"items": [{"name": "Nidus"}, {"name": "Octavia"}, {"name": "Harrow"}]}],
        "hardWeeks": [{"items": [{"name": "布莱顿"}]},
                      {"items": [{"name": "野猪"}, {"name": "咖玛腕甲枪"}]}],
    },
    "calendar": {"season": "夏季", "expiry": EXPIRY, "days": [
        {"day": 185, "events": [{"kind": "challenge", "name": "饿死野兽",
                                 "desc": "摧毁 75 个储存容器"}]},
        {"day": 192, "events": [{"kind": "reward", "name": "荧尘储藏箱"}]},
        {"day": 195, "events": [{"kind": "upgrade", "name": "精神反击",
                                 "desc": "有 10% 的几率触发 250 伤害。"}]},
    ]},
}


class TestExtract(unittest.TestCase):
    def test_parse_flight_payload(self):
        d = weekly.extract_initial_data(_page(SAMPLE))
        self.assertIsNotNone(d)
        self.assertEqual([g["type"] for g in d["archimedea"]], ["深层科研", "时光科研"])

    def test_missing_marker_returns_none(self):
        self.assertIsNone(weekly.extract_initial_data("<html>没有内嵌数据</html>"))

    def test_broken_json_returns_none(self):
        html = '<script>self.__next_f.push([1,"{\\"initialData\\":{坏了"])</script>'
        self.assertIsNone(weekly.extract_initial_data(html))

    def test_plain_unescaped_marker_also_works(self):
        html = '<script>{"initialData":{"a":1}}</script>'
        self.assertEqual(weekly.extract_initial_data(html), {"a": 1})


class TestRender(unittest.TestCase):
    def setUp(self):
        self.d = weekly.extract_initial_data(_page(SAMPLE))

    def test_research_lists_missions_and_modifiers(self):
        t = weekly._fmt_research(self.d)
        for want in ("深层科研", "时光科研", "歼灭 · 低语者", "偏差 密闭装甲",
                     "变量 吸血界影", "偏差 噪音抑制"):
            self.assertIn(want, t)

    def test_research_long_desc_wrapped_with_indent(self):
        t = weekly._fmt_research(self.d)
        self.assertIn("      非弱点命中对敌人造成的伤害降低 90%。", t)

    def test_archon_has_boss_shard_and_nodes(self):
        t = weekly._fmt_archon(self.d)
        self.assertIn("执刑官诡文枭主", t)
        self.assertIn("蔚蓝执刑官 Tau 融造源力石", t)
        self.assertIn("Cambria · 地球 · Grineer · 移动防御", t)
        self.assertIn("3. Oro · 地球 · Grineer · 刺杀", t)

    def test_descent_uses_npc_for_gate_nodes(self):
        t = weekly._fmt_descent(self.d)
        self.assertIn("01 收集 · 坠落的碎片", t)
        self.assertIn("02 守关 · 玛丽的圣所", t)
        self.assertIn("03 挖掘", t)

    def test_teshin_marks_this_and_next_week(self):
        t = weekly._fmt_teshin(self.d)
        self.assertIn("本周：霰弹枪裂罅 Mod", t)
        self.assertIn("1. 霰弹枪裂罅 Mod（本周）", t)
        self.assertIn("2. Umbra Forma 蓝图（下周）", t)
        self.assertIn("3. 赤毒 ×50,000（第 3 周）", t)

    def test_circuit_shows_next_week(self):
        t = weekly._fmt_circuit(self.d)
        self.assertIn("普通（战甲三选一）：Ivara、Inaros、Titania", t)
        self.assertIn("下周普通战甲：Nidus、Octavia、Harrow", t)
        self.assertIn("下周钢铁武器：野猪、咖玛腕甲枪", t)

    def test_calendar_maps_upgrade_to_gain(self):
        t = weekly._fmt_calendar(self.d)
        self.assertIn("第 195 天 · 增益 精神反击", t)
        self.assertIn("第 192 天 · 奖励 荧尘储藏箱", t)
        self.assertNotIn("upgrade", t)

    def test_reply_does_not_exceed_card_width(self):
        for act, fn in weekly._ACTIONS.items():
            for line in fn(self.d).splitlines():
                # 标题行不折行（emoji + 区域名 + 剩余时间），正文按 44 折
                self.assertLessEqual(weekly._dw(line), 52, "%s 行超宽：%r" % (act, line))

    def test_empty_data_says_no_data(self):
        for act in ("research", "archon", "descent", "teshin", "circuit", "calendar"):
            self.assertIn("没有数据", weekly._ACTIONS[act]({}))


class TestCommand(unittest.TestCase):
    def test_heads_hit(self):
        cases = {"每周": "all", "周报": "all", "科研": "research", "深层科研": "research",
                 "执刑官": "archon", "沉沦之地": "descent", "钢铁商店": "teshin",
                 "回廊预告": "circuit", "日历": "calendar", "1999": "calendar"}
        for text, action in cases.items():
            self.assertEqual(weekly.parse_command(text), {"action": action}, text)

    def test_chat_not_triggered(self):
        for text in ("今天吃什么", "科研好难", "每周几更新", "日历在哪买", ""):
            self.assertIsNone(weekly.parse_command(text), text)

    def test_prefix_slash_tolerated(self):
        self.assertTrue(weekly.is_weekly_command("/科研"))
        self.assertTrue(weekly.is_weekly_command("  每周  "))


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_file = os.environ.get("WS_WEEKLY_FILE")
        os.environ["WS_WEEKLY_FILE"] = str(Path(self.tmp.name) / "weekly.json")
        weekly._CACHE[0], weekly._CACHE[1] = None, 0.0
        self.orig_build = weekly.build

    def tearDown(self):
        weekly.build = self.orig_build
        if self.orig_file is None:
            os.environ.pop("WS_WEEKLY_FILE", None)
        else:
            os.environ["WS_WEEKLY_FILE"] = self.orig_file
        weekly._CACHE[0], weekly._CACHE[1] = None, 0.0
        self.tmp.cleanup()

    def _write(self, age_sec):
        weekly._write_cache({"version": weekly.VERSION,
                             "built_at": int(time.time() - age_sec),
                             "data": SAMPLE})

    def test_fresh_cache_no_refetch(self):
        self._write(60)
        weekly.build = lambda: self.fail("未过期不该重抓")
        d = weekly.load()
        self.assertEqual(d["data"]["archonHunt"]["boss"], "执刑官诡文枭主")
        self.assertFalse(d.get("stale"))

    def test_expired_cache_refetches(self):
        self._write(3600)
        calls = []

        def fake():
            calls.append(1)
            return {"version": weekly.VERSION, "built_at": int(time.time()),
                    "data": {"archonHunt": {"boss": "新"}}}
        weekly.build = fake
        d = weekly.load()
        self.assertEqual(calls, [1])
        self.assertEqual(d["data"]["archonHunt"]["boss"], "新")

    def test_failed_refresh_keeps_old_with_stale_note(self):
        self._write(3 * 3600)

        def boom():
            raise weekly.WeeklyUnavailable("网络不通")
        weekly.build = boom
        d = weekly.load()
        self.assertTrue(d.get("stale"))
        self.assertIn("没刷新成功", weekly._stale_note(d))

    def test_no_cache_and_failed_refresh_raises(self):
        def boom():
            raise weekly.WeeklyUnavailable("网络不通")
        weekly.build = boom
        with self.assertRaises(weekly.WeeklyUnavailable):
            weekly.load()

    def test_short_hint_empty_without_cache(self):
        self.assertEqual(weekly.short_hint(), "")

    def test_short_hint_lists_research_with_cache(self):
        self._write(60)
        self.assertIn("深层科研 2 关", weekly.short_hint())

    def test_format_reply_reports_unavailable(self):
        def boom():
            raise weekly.WeeklyUnavailable("网络不通")
        weekly.build = boom
        out = weekly.format_reply({"action": "archon"})
        self.assertIn("每周数据暂时取不到", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
