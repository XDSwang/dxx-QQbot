#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赏金任务板离线测试（不联网、不碰真实 data/）

覆盖：
  - 解析：bountyBoard 的 rewards / isVault、bounties.groups 的 ally、字符串奖励兜底
  - 合并：六个集团一次给全（bountyBoard 3 + groups 3）、地区映射
  - 格式化：每档带奖励摘要、×N 只在数量 >1 时出现、超出写「等 N 项」、
            单集团过滤时全列、未匹配如实回「没有匹配」、赏金轮换不再重复集团
  - 指令：赏金 Ostron / 赏金坚守者 / 赏金 扎里曼 / 悬赏 科维兽 / 赏金轮换
  - 监控：监控 赏金 Ostron 带过滤；不认识的条件仍回提示（不开空过滤）
  - 会话：filters 落到 _fissure_filters 并作用到 _col_text
"""
import os
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="bounty_")
os.environ["WM_SOURCE"] = "mock"
os.environ["WS_PRIORITY_TYPES"] = ""
os.environ["WS_AUTO_PRIORITY"] = "0"
os.environ["WS_ACTIVE_MIN_GAP"] = "0"
os.environ["WS_SWITCH_FILE"] = os.path.join(TMP, "ws_switches.json")
os.environ["WS_FILTER_FILE"] = os.path.join(TMP, "ws_filters.json")
os.environ["WS_AT_FILE"] = os.path.join(TMP, "ws_at_users.json")
os.environ["WS_SUB_FILE"] = os.path.join(TMP, "ws_subs.json")

import wm
import worldstate as ws
import ws_monitor as M
import qq_common

wm.CACHE_FILE = os.path.join(TMP, "cache.json")
M.LAST_SENT_FILE = Path(TMP) / "ws_last_sent.json"

FF = os.environ["WS_FILTER_FILE"]
KA = ("group", "G1")
CONTEXT = {"channel": "group", "group_id": "G1", "message_type": "group"}
ok = total = 0


def t(name, cond):
    global ok, total
    total += 1
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if cond:
        ok += 1


def reset():
    M.clear_pending()
    M._monitors.clear()
    M.reset_switches()
    M._seen_priority.clear()
    M._priority_sent_at.clear()
    M._last_sent.clear()
    M._fissure_filters.clear()
    if os.path.exists(FF):
        os.remove(FF)


EXP = int(time.time() + 5400)


def raw_state():
    """一份贴近 wf.wiki 原始结构的数据（含 rewards / isVault / ally）。"""
    return {
        "bountyBoard": [
            {"name": "Ostron", "tag": "CetusSyndicate", "expiry": EXP * 1000,
             "jobs": [
                 {"objective": "回收", "minLevel": 5, "maxLevel": 15, "stages": 3,
                  "isVault": "$undefined",
                  "rewards": [{"name": "蓄能重划", "count": 1},
                              {"name": "奥席金属", "count": 100},
                              {"name": "内融核心", "count": 1},
                              {"name": "Gara 机体 蓝图", "count": 1},
                              {"name": "抵近射击", "count": 1},
                              {"name": "非晶态合金", "count": 2}]},
                 {"objective": "捕获", "minLevel": 40, "maxLevel": 60, "stages": 5,
                  "isVault": "$undefined",
                  "rewards": [{"name": "夜灵之息", "count": 5}]},
             ]},
            {"name": "索拉里斯", "tag": "SolarisSyndicate", "expiry": EXP * 1000,
             "jobs": [{"objective": "刺杀", "minLevel": 5, "maxLevel": 15, "stages": 3,
                       "isVault": "$undefined",
                       "rewards": [{"name": "心肌菌孢子", "count": 5}]}]},
            {"name": "英择谛", "tag": "EntratiSyndicate", "expiry": EXP * 1000,
             "jobs": [{"objective": "隔离库", "minLevel": 30, "maxLevel": 40, "stages": 4,
                       "isVault": True,
                       "rewards": [{"name": "残差·恶嗅", "count": 1},
                                   # 数据源偶尔直接给字符串，必须兜底不崩
                                   "前纪 V17"]}]},
        ],
        "bounties": {
            "expiry": EXP * 1000, "rot": "A",
            "rotRewards": [{"name": "Caliban 系统"}],
            "vaultRot": "B", "vaultRewards": [{"name": "孢丝感染枪 枪机"}],
            "groups": [
                {"name": "坚守者", "tag": "ZarimanSyndicate", "jobs": [
                    {"node": "永视弧域", "mission": "虚空洪流",
                     "challenge": "击杀 100 名敌人", "ally": ""}]},
                {"name": "科维兽", "tag": "CaviaSyndicate", "jobs": [
                    {"node": "不灭之地", "mission": "生存",
                     "challenge": "击败嘲讽低语或尖刻低语", "ally": ""}]},
                {"name": "六人组", "tag": "HexSyndicate", "jobs": [
                    {"node": "传承种收割", "mission": "传承种收割",
                     "challenge": "从上方击杀 10 名敌人", "ally": "阿米尔"}]},
            ],
        },
    }


def mk_state():
    return ws._wfwiki_to_state(raw_state())


def main():
    with ExitStack() as st:
        st.enter_context(patch.object(ws, "ensure_active", lambda: None))
        st.enter_context(patch.object(M, "_ensure_push_thread", lambda: None))
        M.set_sender(lambda ch, tid, text: True)

        # ---------- 1) 解析 ----------
        print("[解析：奖励 / 隔离库 / 同伴 / 字符串兜底]")
        s = mk_state()
        ostron = s["bountyBoard"][0]
        t("奖励被解析出来（6 项）", len(ostron["jobs"][0]["rewards"]) == 6)
        t("奖励数量写成 count", ostron["jobs"][0]["rewards"][1] ==
          {"name": "奥席金属", "count": 100})
        t("$undefined 的 isVault 当 False", ostron["jobs"][0]["isVault"] is False)
        t("isVault=true 的隔离库档被标出", s["bountyBoard"][2]["jobs"][0]["isVault"] is True)
        t("字符串奖励兜底成 count=1",
          s["bountyBoard"][2]["jobs"][0]["rewards"][1] == {"name": "前纪 V17", "count": 1})
        hexes = [g for g in s["bounties"]["groups"] if g["name"] == "六人组"][0]
        t("六人组带同伴名", hexes["jobs"][0]["ally"] == "阿米尔")
        t("无同伴的集团给空串", s["bounties"]["groups"][0]["jobs"][0]["ally"] == "")

        # ---------- 2) 合并与地区 ----------
        print("[合并：六个集团 + 地区]")
        board = ws._bounty_all(s)
        t("六个集团一次给全", [b["name"] for b in board] ==
          ["Ostron", "索拉里斯", "英择谛", "坚守者", "科维兽", "六人组"])
        regions = {b["name"]: b["region"] for b in board}
        t("Ostron 属希图斯", regions["Ostron"] == "希图斯")
        t("索拉里斯属福尔图娜", regions["索拉里斯"] == "福尔图娜")
        t("英择谛属魔胎之境", regions["英择谛"] == "魔胎之境")
        t("坚守者属扎里曼", regions["坚守者"] == "扎里曼")
        t("科维兽属火卫二", regions["科维兽"] == "火卫二")
        t("六人组属 1999", regions["六人组"] == "1999")

        # ---------- 3) 格式化 ----------
        print("[格式化：整栏 / 单集团 / 无匹配 / 轮换]")
        full = ws._fmt_bounty(s)
        for name in ("Ostron", "索拉里斯", "英择谛", "坚守者", "科维兽", "六人组"):
            t("整栏含集团 %s" % name, name in full)
        t("整栏带奖励", "蓄能重划" in full and "奥席金属×100" in full)
        t("count=1 不写 ×1", "蓄能重划×1" not in full)
        t("整栏每档奖励有上限（等 N 项）", "等 6 项" in full)
        t("挑战型显示节点·任务·挑战", "永视弧域 · 虚空洪流 · 击杀 100 名敌人" in full)
        t("六人组带同伴名", "（阿米尔）" in full)
        t("隔离库档照常显示", "隔离库 Lv30-40" in full)
        t("整栏带「与平原同一份数据」说明", "与「平原」" in full)

        only = ws._fmt_bounty(s, ["ostron"])
        t("单集团标题只写该集团", only.startswith("🧾 Ostron 赏金"))
        t("单集团列全部档位", "回收" in only and "捕获" in only)
        t("单集团列全部奖励（不省略）", "Gara 机体 蓝图" in only and "等 6 项" not in only)
        t("单集团不再加平原说明", "与「平原」" not in only)
        t("单集团按中文名过滤", "(扎里曼)" and ws._fmt_bounty(s, ["坚守者"]).startswith("🧾 坚守者"))
        t("单集团按地区过滤", ws._fmt_bounty(s, ["扎里曼"]).startswith("🧾 坚守者"))
        t("地区「福尔图娜」选中索拉里斯",
          ws._fmt_bounty(s, ["福尔图娜"]).startswith("🧾 索拉里斯"))
        t("未匹配如实回提示", "没有匹配" in ws._fmt_bounty(s, ["火星"]))

        rot = ws._fmt_bounty_rot(s)
        t("轮换保留本轮奖励", "本轮 A" in rot and "Caliban 系统" in rot)
        t("轮换保留宝库奖励", "宝库轮换 B" in rot)
        t("轮换不再重复集团列表", "坚守者" not in rot or "在「赏金」里" in rot)
        t("轮换指向赏金栏目", "在「赏金」里" in rot)

        # ---------- 4) 指令解析 ----------
        print("[指令：查询 / 监控]")
        cases = {
            "赏金": ("bounty", []),
            "赏金 Ostron": ("bounty", ["Ostron"]),
            "赏金ostron": ("bounty", ["Ostron"]),
            "赏金坚守者": ("bounty", ["坚守者"]),
            "赏金 扎里曼": ("bounty", ["扎里曼"]),
            "赏金 六人组": ("bounty", ["六人组"]),
            "悬赏 科维兽": ("bounty", ["科维兽"]),
            "赏金 福尔图娜": ("bounty", ["福尔图娜"]),
        }
        for text, (typ, flt) in cases.items():
            cmd = ws.parse_ws_command(text)
            t("查询 %-14s -> %s %s" % (text, typ, flt),
              cmd.get("types") == [typ] and cmd.get("filter") == flt)
        cmd = ws.parse_ws_command("赏金轮换")
        t("赏金轮换不被当成按「轮换」过滤",
          cmd.get("types") == ["bountyrot"] and not cmd.get("filter"))
        t("英文别名归一大小写", ws.parse_bounty_tokens("OSTRON") == ["Ostron"])
        t("地区别名归一到地区名", ws.parse_bounty_tokens("fortuna") == ["福尔图娜"])

        cmd = ws._parse_monitor("赏金 Ostron")
        t("监控 赏金 Ostron 带过滤",
          cmd.get("types") == ["bounty"] and cmd.get("bounty_filter") == ["Ostron"])
        cmd = ws._parse_monitor("赏金 Ostron 警报")
        t("赏金 + 其它栏目并列不吞掉",
          sorted(cmd.get("types")) == ["alerts", "bounty"]
          and cmd.get("bounty_filter") == ["Ostron"])
        cmd = ws._parse_monitor("赏金 火星")
        t("监控 赏金 不认识的条件回提示", cmd.get("action") == "monitor_hint")
        cmd = ws._parse_monitor("赏金")
        t("监控 赏金 无条件是整栏", cmd.get("action") == "monitor" and not cmd.get("bounty_filter"))

        # ---------- 5) 会话过滤生效 ----------
        print("[会话：filters 落到会话表并作用到栏目正文]")
        reset()
        M.start_monitor(KA[0], KA[1], {"bounty"}, filters={"bounty": ["Ostron"]})
        t("start_monitor 存下赏金过滤",
          (M._fissure_filters.get(KA) or {}).get("bounty") == ["Ostron"])
        txt = M._col_text(s, "bounty", KA)
        t("_col_text 只出 Ostron", "Ostron" in txt and "六人组" not in txt)
        M.stop_monitor(KA[0], KA[1])
        t("停止监控清掉赏金过滤", KA not in M._fissure_filters)

        # ---------- 6) 走完整指令链路 ----------
        print("[链路：handle_ws 查询 / 监控确认文案]")
        reset()
        with patch.object(ws, "get_state", lambda: s), \
             patch.object(ws, "activity_touch", lambda: None), \
             patch.object(ws, "ensure_fresh", lambda: s):
            out = str(qq_common.handle_ws("赏金 坚守者", CONTEXT))
        t("查询只回该集团", "坚守者" in out and "Ostron" not in out)
        reset()
        r = str(qq_common.handle_ws("监控 赏金 Ostron", CONTEXT))
        t("监控确认文案写明只看某集团", "只看赏金里的" in r and "Ostron" in r)
        t("监控实际带上赏金过滤",
          (M._fissure_filters.get(KA) or {}).get("bounty") == ["Ostron"])
        reset()
        r = str(qq_common.handle_ws("监控 赏金 火星", CONTEXT))
        t("监控不认识的条件不开监控", "需要明确指定内容" in r and not M._monitors.get(KA))

    print("\n%d/%d PASS, %d FAIL" % (ok, total, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
