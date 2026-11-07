#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回廊（无尽回廊）离线测试（不联网、不碰真实 data/）

数据来自 DE 官方端点 EndlessXpSchedule：
  EXC_NORMAL = 普通回廊本周三选一战甲；EXC_HARD = 钢铁回廊本周五把灵化武器。

覆盖：
  - 解析：官方时间戳、CategoryChoices 拆成 normal/hard、缺字段回 None
  - 格式化：战甲三选一、灵化武器、每周一轮换、取不到时如实提示
  - 指令：回廊 / 无尽回廊 / 灵化 / 钢铁回廊 / incarnon 都归到 circuit
  - 冲突：钢铁回廊里的「钢铁」不得被当成钢铁裂缝（查询与监控两侧）
  - 监控：监控 回廊 / 监控 钢铁回廊 / 监控 回廊警报（多栏目并存）
  - 开关：回廊=关闭 走通用开关，标签名与 TYPES 一致
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="circuit_")
os.environ["WM_SOURCE"] = "mock"
os.environ["WS_PRIORITY_TYPES"] = ""
os.environ["WS_AUTO_PRIORITY"] = "0"
os.environ["WS_ACTIVE_MIN_GAP"] = "0"
os.environ["WS_SWITCH_FILE"] = os.path.join(TMP, "ws_switches.json")
os.environ["WS_FILTER_FILE"] = os.path.join(TMP, "ws_filters.json")

import wm
import worldstate as ws
import ws_monitor as M
import qq_common

wm.CACHE_FILE = os.path.join(TMP, "cache.json")
ws.WS_CACHE_FILE = Path(TMP) / "worldstate.json"
M.LAST_SENT_FILE = Path(TMP) / "ws_last_sent.json"

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


def official_payload(categories=None):
    """官方 worldState.php 里 EndlessXpSchedule 的形状（$date/$numberLong 嵌套）。"""
    if categories is None:
        categories = [{"Category": "EXC_NORMAL",
                       "Choices": ["Ivara", "Inaros", "Titania"]},
                      {"Category": "EXC_HARD",
                       "Choices": ["Braton", "Lato", "Skana", "Paris", "Kunai"]}]
    return {"EndlessXpSchedule": [{
        "Activation": {"$date": {"$numberLong": "1793577600000"}},
        "Expiry": {"$date": {"$numberLong": "1794182400000"}},
        "CategoryChoices": categories}]}


def fetch_with(payload):
    """按官方返回结构跑一遍 _fetch_circuit（拦掉网络与节流）。"""
    with patch.object(ws, "_http_json", lambda *a, **k: payload), \
            patch.object(ws.wm, "pace_gate_ex", lambda *a, **k: None):
        return ws._fetch_circuit()


def main():
    print("== 1) 解析官方结构 ==")
    c = fetch_with(official_payload())
    t("拿到两条轮换", bool(c) and len(c["normal"]) == 3 and len(c["hard"]) == 5)
    t("普通回廊是三选一战甲",
      c["normal"] == ["Ivara", "Inaros", "Titania"])
    t("钢铁回廊是五把灵化武器",
      c["hard"] == ["Braton", "Lato", "Skana", "Paris", "Kunai"])
    t("到期时刻换算正确", int(c["expiry"]) == 1794182400)
    t("起始时刻换算正确", int(c["activation"]) == 1793577600)

    only_hard = fetch_with(official_payload(
        [{"Category": "EXC_HARD", "Choices": ["Boltor"]}]))
    t("只有钢铁分类时 normal 为空", only_hard["normal"] == []
      and only_hard["hard"] == ["Boltor"])

    t("没有 EndlessXpSchedule 回 None", fetch_with({}) is None)
    t("空 CategoryChoices 回 None",
      fetch_with({"EndlessXpSchedule": [{"CategoryChoices": []}]}) is None)
    t("不认识的分类被忽略",
      fetch_with(official_payload([{"Category": "EXC_OTHER",
                                    "Choices": ["X"]}])) is None)
    t("分类里混了 null 不会留下空名",
      fetch_with(official_payload([{"Category": "EXC_HARD",
                                    "Choices": ["Braton", None, "$undefined"]}]))["hard"]
      == ["Braton"])

    print("\n== 2) 格式化 ==")
    st = {"circuit": c}
    txt = ws._fmt_circuit(st)
    t("有栏目标题", "🌀 无尽回廊" in txt)
    t("列出三套战甲", "Ivara、Inaros、Titania" in txt)
    t("列出五把灵化武器", "Braton、Lato、Skana、Paris、Kunai" in txt)
    t("标明每周一轮换", "每周一 00:00 UTC" in txt)
    t("标明名称沿用原名", "官方原名" in txt)
    t("缺数据时如实提示", "暂时取不到" in ws._fmt_circuit({}))
    t("空列表时不崩且给提示",
      "等刷新" in ws._fmt_circuit({"circuit": {"expiry": int(time.time() + 60),
                                              "normal": [], "hard": []}}))
    t("已过期写「刚刚结束」",
      "刚刚结束" in ws._fmt_circuit({"circuit": {
          "expiry": int(time.time() - 60), "normal": ["X"], "hard": []}}))

    print("\n== 2.5) 回廊并入双衍王境（王境里的玩法，归它这一块） ==")
    d = dict(ws._MOCK_WS)
    dt = ws._fmt_duviri(d)
    t("双衍王境块里带出回廊", "无尽回廊" in dt)
    t("王境块里列出三选一战甲", "Ivara、Inaros、Titania" in dt)
    t("王境块里列出灵化武器", "Braton、Lato、Skana、Paris、Kunai" in dt)
    t("回廊排在王境周期之后", dt.index("双衍王境") < dt.index("无尽回廊"))
    no_c = dict(d)
    no_c.pop("circuit")
    t("取不到回廊时不写空行", "无尽回廊" not in ws._fmt_duviri(no_c))
    t("回廊缺失不影响王境周期", "双衍王境：" in ws._fmt_duviri(no_c))
    ws._COMPACT = True
    try:
        t("「全部」精简模式不重复贴回廊", "无尽回廊" not in ws._fmt_duviri(d))
    finally:
        ws._COMPACT = False

    print("\n== 3) 栏目与指令 ==")
    t("TYPES 里有回廊", "circuit" in ws.TYPES and ws.TYPES["circuit"][0] == "回廊")
    for word in ("回廊", "无尽回廊", "灵化", "灵化武器", "钢铁回廊", "circuit"):
        r = ws.parse_ws_command(word)
        t(f"查询「{word}」→ circuit",
          r.get("action") == "query" and r.get("types") == ["circuit"])
    r = ws.parse_ws_command("钢铁回廊")
    t("钢铁回廊不被当成钢铁裂缝", r.get("types") == ["circuit"])
    r = ws.parse_ws_command("钢铁裂缝")
    t("钢铁裂缝仍是钢铁裂缝", r.get("types") == ["steel"])
    r = ws.parse_ws_command("裂缝后纪")
    t("裂缝过滤没被回廊影响",
      r.get("types") == ["fissures"] and r.get("filter") == ["后纪"])

    r = ws._parse_monitor("回廊")
    t("监控 回廊 只开回廊",
      r.get("action") == "monitor" and r.get("types") == ["circuit"])
    r = ws._parse_monitor("钢铁回廊")
    t("监控 钢铁回廊 不带出钢铁裂缝",
      r.get("action") == "monitor" and r.get("types") == ["circuit"])
    r = ws._parse_monitor("回廊 警报")
    t("监控 回廊 警报 两栏并存",
      r.get("action") == "monitor" and set(r.get("types") or []) == {"circuit", "alerts"})
    r = ws.parse_ws_command("回廊=关闭")
    t("回廊=关闭 解析成开关",
      r.get("action") == "switch" and "circuit" in str(r))
    t("灵化也能开关", ws.parse_ws_command("灵化=开启").get("action") == "switch")
    t("闲聊不误判", not qq_common.is_ws_command("回廊好玩吗"))

    print("\n== 4) 端到端（mock 状态 + 推送分条） ==")
    reset()
    try:
        with patch.object(ws, "ensure_active", lambda: None), \
                patch.object(M, "_ensure_push_thread", lambda: None):
            r = str(qq_common.handle_ws("回廊", CONTEXT))
            t("查询回廊有内容", "无尽回廊" in r and "Ivara" in r)
            r = str(qq_common.handle_ws("监控 回廊", CONTEXT))
            t("监控回廊确认", "回廊" in r and (KA in M._monitors))
            parts = M._parts_of(dict(ws._MOCK_WS), {"circuit", "alerts"}, KA)
            labels = [p[0] for p in parts]
            t("推送分条含回廊", "circuit" in labels)
            t("回廊那条正文正确",
              any("无尽回廊" in txt for k, txt in parts if k == "circuit"))
            t("新栏目可被 monitor_label 识别",
              "circuit" in ws.TYPES and ws.TYPES["circuit"][0] == "回廊")
            t("回廊推送标签就是「回廊」",
              M.monitor_label(ws._fmt_circuit(dict(ws._MOCK_WS))) == "【回廊监控信息如下】")
            t("双衍王境块不会被标成回廊",
              M.monitor_label(ws._fmt_duviri(dict(ws._MOCK_WS))) == "【双衍王境监控信息如下】")
            r = str(qq_common.handle_ws("双衍王境", CONTEXT))
            t("查双衍王境直接看到回廊",
              "双衍王境" in r and "无尽回廊" in r and "Ivara" in r)
    finally:
        reset()

    print("\n%d/%d PASS, %d FAIL" % (ok, total, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
