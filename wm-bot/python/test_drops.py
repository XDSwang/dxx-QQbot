#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遗物 / 掉落查询离线测试（不联网、不碰真实 data/）

覆盖：
  - 指令解析：遗物 / 虚空遗物 / relic / 掉落 / 获取 / 出处，闲聊不触发
  - 索引构建：uniqueName -> 中文名、蓝图名拼接、同名四档去重、配方材料解析
  - 遗物查询：单条详情（金/银/铜 + 四档概率）、同号多纪提示、查不到提示
  - 物品查询：精确名、多结果提示、Prime 部件反查遗物、主蓝图借用成品配方
  - 缓存：落盘 / 读取 / 过期重抓 / 重抓失败标 stale
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="drops_")
os.environ["WM_SOURCE"] = "mock"
os.environ["WS_DROPS_FILE"] = os.path.join(TMP, "drops.json")

import drops

ok = total = 0


def t(name, cond):
    global ok, total
    total += 1
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if cond:
        ok += 1


# ----------------------------- 假官方导出 -----------------------------
_WEAPONS = [
    {"uniqueName": "/Lotus/Weapons/Tenno/Rifle/Rifle", "name": "布莱顿"},
    {"uniqueName": "/Lotus/Weapons/Tenno/Pistols/PrimePistol", "name": "布里斯提卡 Prime"},
]
_RES = [
    {"uniqueName": "/Lotus/Types/Items/MiscItems/AlloyPlate", "name": "合金板"},
    {"uniqueName": "/Lotus/Weapons/Tenno/Pistols/PrimePistolBarrel",
     "name": "布里斯提卡 Prime 枪管"},
]
_UPGRADES = [
    {"uniqueName": "/Lotus/Upgrades/Mods/Rifle/TestMod", "name": "测试 Mod"},
]
_RECIPES = [
    {"uniqueName": "/Lotus/Types/Recipes/Weapons/BratonBlueprint",
     "resultType": "/Lotus/Weapons/Tenno/Rifle/Rifle",
     "buildPrice": 25000, "buildTime": 43200,
     "ingredients": [{"ItemType": "/Lotus/Types/Items/MiscItems/AlloyPlate",
                      "ItemCount": 300}]},
]
_REWARDS = [{"rewardName": "/Lotus/StoreItems/Upgrades/Mods/Rifle/TestMod",
             "rarity": "RARE", "tier": 0},
            {"rewardName": "/Lotus/StoreItems/Types/Recipes/Weapons/BratonBlueprint",
             "rarity": "COMMON", "tier": 0},
            {"rewardName": "/Lotus/StoreItems/Weapons/Tenno/Pistols/PrimePistolBarrel",
             "rarity": "UNCOMMON", "tier": 0}]
_RELICS = [
    # 同一遗物的四个精炼档位在官方导出里是四条同名的记录，奖励完全一致
    {"uniqueName": "/Lotus/Types/Game/Projections/T4TestBronze",
     "name": "后纪 B10 遗物", "relicRewards": _REWARDS},
    {"uniqueName": "/Lotus/Types/Game/Projections/T4TestPlatinum",
     "name": "后纪 B10 遗物", "relicRewards": _REWARDS},
    {"uniqueName": "/Lotus/Types/Game/Projections/T1TestA1",
     "name": "古纪 A1 遗物", "relicRewards": _REWARDS},
    {"uniqueName": "/Lotus/Types/Game/Projections/T4TestA1",
     "name": "后纪 A1 遗物", "relicRewards": _REWARDS},
]
_BY_FILE = {
    "ExportRelicArcane_zh.json": _RELICS,
    "ExportRecipes_zh.json": _RECIPES,
    "ExportResources_zh.json": _RES,
    "ExportWeapons_zh.json": _WEAPONS,
    "ExportWarframes_zh.json": [],
    "ExportSentinels_zh.json": [],
    "ExportUpgrades_zh.json": _UPGRADES,
}


def fake_index():
    return {name: "hash-" + str(i) for i, name in enumerate(drops.SRC_FILES)}


def fake_list(name, h):
    return _BY_FILE[name]


def build():
    with patch.object(drops, "_fetch_index", fake_index), \
            patch.object(drops, "_fetch_list", fake_list):
        return drops.build_index()


def main():
    print("== 1) 指令解析 ==")
    t("遗物 后纪B10", drops.parse_command("遗物 后纪B10") ==
      {"action": "relic", "q": "后纪B10"})
    t("虚空遗物 后纪B10", drops.parse_command("虚空遗物 后纪B10")["q"] == "后纪B10")
    t("relic A1", drops.parse_command("relic A1")["action"] == "relic")
    t("掉落 布莱顿", drops.parse_command("掉落 布莱顿") ==
      {"action": "item", "q": "布莱顿"})
    t("获取 布莱顿", drops.parse_command("获取 布莱顿")["action"] == "item")
    t("@ 前缀斜杠也能认", drops.parse_command("/遗物 后纪B10")["action"] == "relic")
    t("闲聊不触发", drops.parse_command("我今天掉了三个遗物") is None)
    t("空文本不触发", drops.parse_command("") is None)
    t("is_drop_command 认遗物", drops.is_drop_command("遗物 后纪B10"))
    t("is_drop_command 不认闲聊", not drops.is_drop_command("遗物多不多"))

    print("== 2) 索引构建 ==")
    idx = build()
    t("遗物 3 个（同名四档只留一份）", len(idx["relics"]) == 3)
    t("后纪 B10 在索引里", "后纪B10" in idx["relics"])
    t("奖励 3 条", len(idx["relics"]["后纪B10"][1]) == 3)
    names = {n for n, _ in idx["relics"]["后纪B10"][1]}
    t("Mod 名从物品表补出", "测试 Mod" in names)
    t("蓝图名 = 成品名 + 蓝图", "布莱顿蓝图" in names)
    t("部件名从物品表补出", "布里斯提卡 Prime 枪管" in names)
    rarity = dict((n, r) for n, r in idx["relics"]["后纪B10"][1])
    t("稀有度映射 金/银/铜",
      (rarity["测试 Mod"], rarity["布里斯提卡 Prime 枪管"], rarity["布莱顿蓝图"]) == (2, 1, 0))
    t("反查表收录该物品", "后纪B10" in [k for k, _ in idx["by_item"]["布莱顿蓝图"]])
    t("配方材料解析成中文", idx["recipes"]["布莱顿"][2] == [["合金板", 300]])
    t("配方信用点与工时", idx["recipes"]["布莱顿"][:2] == [25000, 43200])

    print("== 3) 遗物查询 ==")
    txt = drops._relic_detail(idx, "后纪B10")
    t("标题带纪别与编号", txt.splitlines()[0].startswith("🔮 后纪 B10 遗物"))
    t("金行概率 2%/4%/6%/10%", "金 2%/4%/6%/10%" in txt)
    t("银行概率", "银 11%/13%/17%/20%" in txt)
    t("铜行概率", "铜 25.33%/23.33%/20%/16.67%" in txt)
    t("四档说明", "完好 / 卓越 / 无瑕 / 光辉" in txt)
    t("按 金->银->铜 排序", txt.index("金 2%") < txt.index("银 11%") < txt.index("铜 25.33%"))

    t("同号多纪给出提示", "2 个纪都叫这个号" in drops._relic_reply(idx, "A1"))
    t("指定纪别后给详情",
      drops._relic_reply(idx, "古纪A1").startswith("🔮 古纪 A1 遗物"))
    t("英文纪名也认", drops._relic_reply(idx, "axi b10").startswith("🔮 后纪 B10"))
    t("查不到给提示", "没找到" in drops._relic_reply(idx, "后纪 Z99"))
    t("只给纪别列编号", "后纪 遗物共 2 个" in drops._relic_reply(idx, "后纪"))
    t("空参数给用法", "用法" in drops._relic_reply(idx, ""))

    print("== 4) 物品查询 ==")
    txt = drops._item_reply(idx, "布里斯提卡 Prime 枪管")
    t("精确名直接命中", txt.splitlines()[0] == "📦 布里斯提卡 Prime 枪管")
    t("给出遗物来源", "遗物来源（3 个）" in txt and "后纪 B10 · 银" in txt)
    t("非 Prime 显示无遗物来源", "不是 Prime 部件" in drops._item_reply(idx, "布莱顿"))
    t("有配方给出材料", "合金板 ×300" in drops._item_reply(idx, "布莱顿"))
    t("主蓝图借用成品配方",
      "72 小时" not in drops._item_reply(idx, "布莱顿")
      and "12 小时" in drops._item_reply(idx, "布莱顿蓝图"))
    t("多结果给候选", "匹配到 3 个物品" in drops._item_reply(idx, "布"))
    t("查不到给提示", "没找到" in drops._item_reply(idx, "不存在的物品"))
    t("空参数给用法", "用法" in drops._item_reply(idx, ""))

    print("== 5) 缓存与过期 ==")
    path = Path(os.environ["WS_DROPS_FILE"])
    drops._write_cache(idx)
    t("索引已落盘", path.exists() and path.stat().st_size > 0)
    got = drops._read_cache()
    t("读回内容一致", got and len(got["relics"]) == len(idx["relics"]))
    t("版本不符当作没有缓存",
      (path.write_text(json.dumps({"version": 999, "relics": {}}), encoding="utf-8")
       or drops._read_cache() is None))

    drops._write_cache(dict(idx, built_at=0))
    with patch.object(drops, "build_index", lambda: dict(idx, built_at=__import__("time").time())):
        fresh = drops.load_index(force=True)
    t("force 会重抓并拿到新索引", fresh.get("stale") is None and fresh["built_at"] > 0)
    t("重抓后已写盘", json.loads(path.read_text(encoding="utf-8"))["built_at"] > 0)

    drops._INDEX[0] = None
    drops._write_cache(dict(idx, built_at=0))       # 过期
    with patch.object(drops, "build_index",
                      side_effect=drops.DropsUnavailable("断网")):
        stale = drops.load_index()
    t("重抓失败沿用旧索引并标 stale", stale.get("stale") is True)
    t("stale 会在回复里说明", "官方数据本次没刷新成功" in drops._stale_note(stale))

    print("== 6) 取不到数据时的提示 ==")
    drops._INDEX[0] = None
    path.unlink()
    with patch.object(drops, "build_index",
                      side_effect=drops.DropsUnavailable("断网")), \
            patch.object(drops, "_read_cache", lambda: None):
        msg = drops.format_reply({"action": "relic", "q": "后纪B10"})
    t("没有缓存又抓不到 -> 可读提示", "官方数据暂时取不到" in msg)

    print("\n%d/%d 通过" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
