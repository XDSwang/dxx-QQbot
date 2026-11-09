#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聊天指令级主推开关「活动=开启 / 警报=关闭」离线测试（不联网、不碰真实 data/）

覆盖：
  - 指令解析：中文/英文/全角/空格写法、单条多组、全部/主推、状态查询、防误判
  - 会话开关：开启自动注册监控且只合并不覆盖、关闭从周期全量剔除、主推按会话覆盖
  - 存盘与重启：原子写盘、load_switches 恢复、坏文件不崩、PERSIST=0 不写盘
  - push_once 实际推送行为、停止监控清开关、handle_ws 回复与白名单隔离
"""
import json
import os
import sys
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="ws_switch_")
os.environ["WM_SOURCE"] = "mock"
os.environ["WS_PRIORITY_TYPES"] = "alerts,events"
os.environ["WS_PRIORITY_MIN_GAP"] = "0"
os.environ["WS_ACTIVE_MIN_GAP"] = "300"
os.environ["WS_PRIORITY_SKIP_IN_FULL"] = "1"
os.environ["WS_AUTO_PRIORITY"] = "0"
os.environ["WS_SWITCH_PERSIST"] = "1"
os.environ["WS_SWITCH_FILE"] = os.path.join(TMP, "ws_switches.json")
os.environ["WS_FILTER_FILE"] = os.path.join(TMP, "ws_filters.json")
os.environ["WS_AT_FILE"] = os.path.join(TMP, "ws_at_users.json")
os.environ["WS_SUB_FILE"] = os.path.join(TMP, "ws_subs.json")

import wm
import worldstate as ws
import ws_monitor as M
import qq_common

wm.CACHE_FILE = os.path.join(TMP, "cache.json")   # 离线测试绝不污染真实缓存
from pathlib import Path as _P
M.LAST_SENT_FILE = _P(TMP) / "ws_last_sent.json"   # 去重记录也写临时目录

SW = os.environ["WS_SWITCH_FILE"]
KA = ("group", "G1")
EXP = time.time() + 7200
ok = total = 0


def t(name, cond):
    global ok, total
    total += 1
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if cond:
        ok += 1


def check(name, cond):
    t(name, cond)


def reset():
    M.clear_pending()
    M._monitors.clear()
    M.reset_switches()
    M._seen_priority.clear()
    M._priority_sent_at.clear()
    M._last_sent.clear()
    M._next_probe = 0.0
    if os.path.exists(SW):
        os.remove(SW)


def _alert(node, item="Orokin 催化剂", expiry=EXP):
    return {"mission": {"node": node, "type": "歼灭", "faction": "克隆尼"},
            "reward": {"items": [item], "credits": 0}, "expiry": expiry}


def _event(desc, expiry=EXP):
    return {"description": desc, "expiry": expiry}


def mk(alerts=(), events=()):
    return {"alerts": list(alerts), "events": list(events)}


A1, A2 = _alert("地球/Everest"), _alert("火星/Wahiba", "Forma")
E1, E2 = _event("双倍声望活动"), _event("Forma 赠送活动")


def once(state):
    with patch.object(ws, "ensure_fresh", lambda: state), \
         patch.object(ws, "activity_touch", lambda: None):
        return M.push_once()


def main():
    with ExitStack() as st:
        # 不拉起真实后台线程、不触发真实世界状态请求
        st.enter_context(patch.object(ws, "ensure_active", lambda: None))
        st.enter_context(patch.object(M, "_ensure_push_thread", lambda: None))
        M.set_sender(lambda ch, tid, text: True)

        # ---------- 1) 指令解析：用户明确要的写法 ----------
        print("[开关指令解析]")
        c = ws.parse_ws_command("活动=开启")
        t("活动=开启 -> switch", c["action"] == "switch")
        t("活动=开启 命中 events/on", c["items"] == [{"types": "events", "on": True}])
        c = ws.parse_ws_command("警报=关闭")
        t("警报=关闭 命中 alerts/off",
          c["items"] == [{"types": "alerts", "on": False}])
        for s in ("alerts=on", "活动 开启", "开启 活动", "活动＝开启", "活动:开启",
                  "活动：开启", "/活动=开启"):
            c = ws.parse_ws_command(s)
            t("兼容写法 %-12s" % s, c["action"] == "switch"
              and c["items"][0]["types"] == ("events" if "活动" in s else "alerts")
              and c["items"][0]["on"] is True)
        for s in ("events=off", "警报 关闭", "关闭 警报", "警报＝关闭", "alerts=false"):
            c = ws.parse_ws_command(s)
            t("关闭写法 %-14s" % s, c["action"] == "switch"
              and c["items"][0]["on"] is False)

        c = ws.parse_ws_command("活动=开启，警报=开启")
        t("一条消息两组（逗号）", c["action"] == "switch" and len(c["items"]) == 2
          and all(x["on"] for x in c["items"]))
        c = ws.parse_ws_command("活动=开启 警报=关闭")
        t("一条消息两组（空格）", len(c["items"]) == 2
          and c["items"][1] == {"types": "alerts", "on": False})
        t("全部=开启 -> __all__",
          ws.parse_ws_command("全部=开启")["items"] == [{"types": "__all__", "on": True}])
        t("主推=开启 -> __priority__",
          ws.parse_ws_command("主推=开启")["items"] == [{"types": "__priority__", "on": True}])
        t("主推状态 -> switch_status",
          ws.parse_ws_command("主推状态")["action"] == "switch_status")
        t("status 也算状态查询",
          ws.parse_ws_command("status")["action"] == "switch_status")

        # ---------- 1b) 区域全名：推送标签写的就是这个名字，必须能直接开关/查询 ----------
        # 曾经「奥布山谷=关闭」静默无反应：KEYWORDS 里只有「奥布」「山谷」，
        # 没有机器人自己印在标签上的「奥布山谷」全名。
        print("[区域全名]")
        for name, key in (("希图斯", "cetus"), ("奥布山谷", "vallis"),
                          ("魔胎之境", "cambion"), ("双衍王境", "duviri"),
                          ("扎里曼", "zariman"), ("地球平原", "cetus"),
                          ("金星平原", "vallis"), ("火卫二", "cambion")):
            c = ws.parse_ws_command("%s=关闭" % name)
            t("%s=关闭 -> %s/off" % (name, key),
              c.get("action") == "switch"
              and c.get("items") == [{"types": key, "on": False}])
            c = ws.parse_ws_command("%s=开启" % name)
            t("%s=开启 -> %s/on" % (name, key),
              c.get("action") == "switch"
              and c.get("items") == [{"types": key, "on": True}])
            t("%s 是 ws 指令（可直接查询）" % name,
              qq_common.is_ws_command(name))
        for s in ("奥布山谷 关闭", "关闭 奥布山谷"):
            c = ws.parse_ws_command(s)
            t("自然语序 %-12s" % s, c.get("action") == "switch"
              and c.get("items") == [{"types": "vallis", "on": False}])

        # ---------- 1c) 全覆盖：机器人推出来的每个标签名，都必须能直接开关 ----------
        # 原则：标签上印什么名字，用户就该能拿这个名字关掉它（仍然只做精确匹配）。
        print("[标签名全覆盖]")
        names = {}
        for _t in ws.TYPES:
            try:
                _txt = ws.TYPES[_t][1]({})
            except Exception:
                _txt = ""
            _n = M.monitor_label(_txt).strip("【】").replace("监控信息如下", "")
            names.setdefault(_n, []).append(_t)
        for _n in names:
            c = ws.parse_ws_command("%s=关闭" % _n)
            t("标签名 %-6s 可关闭" % _n, c.get("action") == "switch")
            t("标签名 %-6s 可开启" % _n,
              ws.parse_ws_command("%s=开启" % _n).get("action") == "switch")
        for chat in ("奥布山谷好玩吗", "我要去集团任务了", "建造进度慢死了",
                     "信使前哨在哪", "集团任务做完了"):
            t("闲聊不触发 %-12s" % chat, not qq_common.is_ws_command(chat))

        # ---------- 2) 不能把普通聊天 / 别的指令吞掉 ----------
        print("[触发词边界 / 防误判]")
        for s in ("你好呀", "裂缝", "监控 警报", "停止监控", "计划", "活动=哈哈哈",
                  "活动=开启 你好呀", "WM boltor prime", "活动", "全部"):
            c = ws.parse_ws_command(s)
            t("不是开关指令 %-18s" % s, c["action"] != "switch")
        t("活动=开启 是 ws 指令", qq_common.is_ws_command("活动=开启"))
        t("警报=关闭 是 ws 指令", qq_common.is_ws_command("警报=关闭"))
        t("主推状态 是 ws 指令", qq_common.is_ws_command("主推状态"))
        t("普通闲聊不是 ws 指令", not qq_common.is_ws_command("大家好啊"))
        t("闲聊里含区域名也不触发", not qq_common.is_ws_command("奥布山谷好玩吗"))
        t("帮助文本写明开关用法", "活动=开启" in ws.HELP_TEXT
          and "主推状态" in ws.HELP_TEXT and "重启后自动恢复" in ws.HELP_TEXT)

        # ---------- 3) 会话开关：只合并，不覆盖 ----------
        print("[会话开关状态]")
        reset()
        got = M.set_switch("group", "G1", "events", True)
        t("开启返回生效栏目", got == ["events"])
        t("自动注册本会话监控", M.list_monitors().get(KA) == ["events"])
        M._monitors[KA].add("fissures")
        M.set_switch("group", "G1", "alerts", True)
        t("第二次开启不覆盖手动加的栏目",
          set(M.list_monitors()[KA]) == {"events", "fissures", "alerts"})
        t("两个开关都记下",
          {k: v for k, v in M._switches[KA].items()} == {"events": True, "alerts": True})
        types = M.effective_types(KA, {"events", "fissures", "alerts"})
        t("有效栏目含显式开启", types == {"events", "fissures", "alerts"})
        t("有效主推按全局默认+会话覆盖", M.effective_priority(KA, types)
          == ["alerts", "events"])

        M.set_switch("group", "G1", "events", False)
        types2 = M.effective_types(KA, {"events", "fissures", "alerts"})
        t("关闭后栏目被剔除", "events" not in types2)
        t("关闭后主推被剔除", "events" not in M.effective_priority(KA, types2))
        t("关闭不影响另一个会话",
          M.effective_priority(("group", "G2"), {"alerts", "events"})
          == ["alerts", "events"])
        t("关闭再开启可恢复", bool(M.set_switch("group", "G1", "events", True)))

        # ---------- 4) 存盘 / 重启恢复 ----------
        print("[存盘与重启恢复]")
        reset()
        M.set_switch("group", "G1", ["events", "alerts"], True)
        M.set_switch("private", "U9", "events", False)
        t("开关文件已写出", os.path.exists(SW))
        data = json.loads(open(SW, encoding="utf-8").read())
        t("文件结构 version/sessions", data.get("version") == 1
          and isinstance(data.get("sessions"), dict))
        t("两个会话分别保存", set(data["sessions"]) == {"group|G1", "private|U9"})
        t("布尔值正确", data["sessions"]["group|G1"] == {"alerts": True, "events": True}
          and data["sessions"]["private|U9"] == {"events": False})
        t("没有 .tmp 残留", not os.path.exists(SW + ".tmp"))

        M._monitors.clear()
        M.reset_switches()
        t("清空后注册表为空", M.list_monitors() == {})
        n = M.load_switches()
        t("load_switches 恢复 2 个会话", n == 2)
        t("开启的会话自动注册监控",
          set(M.list_monitors().get(KA, [])) == {"events", "alerts"})
        t("只关闭的会话不会自动注册", ("private", "U9") not in M.list_monitors())
        t("恢复后开关内容正确",
          M._switches[KA] == {"events": True, "alerts": True})

        # 已有监控时恢复只合并
        M._monitors[KA] = {"fissures"}
        M.load_switches()
        t("恢复不覆盖已有监控栏目", "fissures" in M.list_monitors()[KA])

        open(SW, "w", encoding="utf-8").write("{bad json")
        t("坏 JSON 不抛异常且当空", M.load_switches() == 0)
        os.remove(SW)
        t("文件不存在也正常", M.load_switches() == 0)
        open(SW, "w", encoding="utf-8").write(json.dumps({
            "version": 1, "sessions": {"weird|G1": {"events": True},
                                       "group|G1": {"nonexistent_type": True},
                                       "group|G2": "not a dict"}}))
        t("脏字段被过滤", M.load_switches() == 0)

        reset()
        os.environ["WS_SWITCH_PERSIST"] = "0"
        M.set_switch("group", "G1", "events", True)
        t("PERSIST=0 不写盘", not os.path.exists(SW))
        os.environ["WS_SWITCH_PERSIST"] = "1"

        # ---------- 5) push_once 真的按会话开关推 ----------
        print("[push_once 按会话开关推送]")
        reset()
        calls = []
        M.set_sender(lambda ch, tid, text: calls.append(text) or True)
        M._next_probe = 0.0
        M.set_switch("group", "G1", "events", True)     # 只开活动
        t("只开了活动 -> 监控只有 events", M.list_monitors().get(KA) == ["events"])
        once(mk([A1], [E1]))                            # 记基线
        t("首次只记基线不补发历史", len(calls) == 0)
        once(mk([A1, A2], [E1]))                        # 只新增警报
        t("未开启的栏目新增不推", len(calls) == 0)
        once(mk([A1, A2], [E1, E2]))                    # 新增活动
        t("已开启栏目新增立即推", len(calls) == 1)
        t("推的是该栏目内容（活动）+ 即时推送说明",
          bool(calls) and "🎉 活动" in calls[0] and "即时推送" in calls[0])
        t("标签是【活动监控信息如下】而非通用",
          bool(calls) and M.monitor_label(calls[0]) == "【活动监控信息如下】")
        t("只装活动不装警报", bool(calls)
          and "Forma 赠送活动" in calls[0] and "火星/Wahiba" not in calls[0])

        reset()
        calls = []
        M.set_sender(lambda ch, tid, text: calls.append(text) or True)
        M._next_probe = 0.0
        M._monitors[KA] = {"alerts", "events"}
        M.set_switch("group", "G1", "events", False)    # 关掉活动
        once(mk([A1], [E1]))
        once(mk([A1], [E1, E2]))
        t("关掉的栏目新增活动也不推", len(calls) == 0)
        t("关掉的栏目不进周期全量", M.effective_types(KA, {"alerts", "events"})
          == {"alerts"})

        # ---------- 6) 关闭再开启不把历史当新增 ----------
        print("[关闭再开启 / 防抖基线]")
        reset()
        calls = []
        M.set_sender(lambda ch, tid, text: calls.append(text) or True)
        M._next_probe = 0.0
        M._monitors[KA] = {"events"}
        M.set_switch("group", "G1", "events", True)
        once(mk([], [E1]))
        M.set_switch("group", "G1", "events", False)
        t("关闭时清掉事件基线", "events" not in (M._seen_priority.get(KA) or {}))
        M.set_switch("group", "G1", "events", True)
        once(mk([], [E1]))
        t("重新开启只记现状、不补发历史", len(calls) == 0)
        once(mk([], [E1, E2]))
        t("重新开启后新条目仍会推", len(calls) == 1)

        # ---------- 7) handle_ws 回复 / 状态 / 停止 / 白名单 ----------
        print("[handle_ws 路由与回复]")
        reset()
        ctx = {"channel": "group", "group_id": "G1"}
        r = qq_common.handle_ws("活动=开启", ctx)
        t("开启回复含栏目与存盘提示", "已开启" in r and "活动" in r
          and "重启后自动恢复" in r)
        t("确认回复不捎带监控内容", "📬 监控推送" not in str(r))
        r = qq_common.handle_ws("活动=开启，警报=关闭", ctx)
        t("一条消息多组都生效", "已开启" in r and "已关闭" in r
          and M._switches[KA] == {"events": True, "alerts": False})
        # 区域全名（标签上印的名字）端到端：能关、能给出「已关闭」回复
        hr = qq_common.handle_ws("奥布山谷=关闭", ctx)
        t("奥布山谷=关闭 有回复且写明已关闭",
          "已关闭" in str(hr) and "奥布山谷" in str(hr))
        t("奥布山谷=关闭 写进会话开关", M._switches[KA].get("vallis") is False)
        r = qq_common.handle_ws("主推状态", ctx)
        t("状态回复列出监控/主推/手动开关",
          "⚙️ 本会话主推开关" in r and "已手动开启" in r and "已手动关闭" in r)
        r = qq_common.handle_ws("活动=开启", {})
        t("无法识别会话时给出明确提示", "无法识别会话" in r)
        r = qq_common.handle_ws("停止监控", ctx)
        t("停止监控提示会清除设置", "清除" in r)
        t("停止监控清掉内存开关", KA not in M._switches)
        if os.path.exists(SW):
            d = json.loads(open(SW, encoding="utf-8").read())
            t("停止监控写回空 sessions", d["sessions"] == {})
        else:
            t("停止监控写回空 sessions", False)

        h = qq_common.make_handler({"G1"}, allow_all=False)
        t("白名单内的群能设置开关", h("dxx 活动=开启", ctx) is not None)
        t("白名单外的群严格静默",
          h("dxx 活动=开启", {"channel": "group", "group_id": "G9"}) is None)
        t("白名单外的群不会留下开关", ("group", "G9") not in M._switches)

    print("\n结果：通过 %d / 失败 %d" % (ok, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
