#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裂缝维度过滤离线测试（不联网、不碰真实 data/）

覆盖：
  - 切词：纪（含单字/英文）、任务类型、地点、连写、无法识别残留
  - 规格解析：裂缝/钢铁裂缝栏目判定、「监控 裂缝 警报」混合栏目不串条件
  - 指令解析：用户举的 6 种写法 → 栏目 + AND 条件；不认识的词仍回提示
  - 匹配：AND 语义、英文纪名归一（Lith/Axi…）
  - 格式化：只输出命中条目，标题计数正确
  - 会话存储：start_monitor 存盘、load_filters 恢复、关开关/停止监控清条件
  - 推送：_parts_of 按会话过滤；push_once 只推命中内容
"""
import json
import os
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="ws_fissure_")
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
M.LAST_SENT_FILE = Path(TMP) / "ws_last_sent.json"

FF = os.environ["WS_FILTER_FILE"]
KA = ("group", "G1")
EXP = time.time() + 7200
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


def _fis(node, mt, tier, exp=EXP):
    return {"node": node, "missionType": mt, "enemy": "Grineer",
            "tier": tier, "expiry": exp}


# 常规裂缝：中文星球/中文任务；钢铁裂缝：英文纪名 + 无任务类型（真实数据情况）
FIS = [
    _fis("火星/Tharsis", "生存", "Neo"),
    _fis("地球/Everest", "防御", "Lith"),
    _fis("土星/Kronus", "中断", "Axi"),
]
STEEL = [
    _fis("冥王星/Cerberus", "?", "Axi"),
    _fis("地球/Everest", "?", "Meso"),
]


def mk_state(fissures=None, steel=None):
    return {"fissures": list(FIS if fissures is None else fissures),
            "steelPath": {"current": list(STEEL if steel is None else steel)}}


def once(state):
    with patch.object(ws, "ensure_fresh", lambda: state), \
         patch.object(ws, "activity_touch", lambda: None):
        return M.push_once()


def main():
    with ExitStack() as st:
        st.enter_context(patch.object(ws, "ensure_active", lambda: None))
        st.enter_context(patch.object(M, "_ensure_push_thread", lambda: None))
        sent = []
        M.set_sender(lambda ch, tid, text: (sent.append(text), True)[1])

        # ---------- 1) 切词 ----------
        print("[切词]")
        cases = {
            "后纪中断": ["中断", "后纪"],
            "火星中断": ["中断", "火星"],
            "古纪 中断": ["中断", "古纪"],
            "土星防御": ["防御", "土星"],
            "钢铁裂缝古纪中断": ["中断", "古纪"],
            "扎里曼中断": ["中断", "扎里曼"],
            "axi disruption": ["后纪", "disruption"],
            "中纪": ["中纪"],
        }
        for text, exp in cases.items():
            t("切词 %-16s -> %s" % (text, exp),
              sorted(ws.parse_fissure_spec(text)["tokens"]) == sorted(exp))
        t("单字「后」归一成后纪", ws.parse_fissure_tokens("后 中断") == ["中断", "后纪"])
        t("「中断」里的「中」不当纪",
          "中纪" not in ws.parse_fissure_tokens("中断"))
        t("空串不过滤", ws.parse_fissure_tokens("") == [])

        # ---------- 2) 栏目判定 / 残余词 ----------
        print("[规格解析]")
        t("钢铁 → steel", ws.parse_fissure_spec("钢铁裂缝中断")["column"] == "steel")
        t("显式裂缝 → fissures", ws.parse_fissure_spec("裂缝后纪")["column"] == "fissures")
        t("无栏目词但能识别 → 推断 fissures",
          ws.parse_fissure_spec("后纪中断")["column"] == "fissures")
        t("完全无关 → None", ws.parse_fissure_spec("警报") is None)
        sp = ws.parse_fissure_spec("不认识的词")
        t("无法识别的词 residual=True", sp is not None and sp["residual"] is True)
        sp = ws.parse_fissure_spec("裂缝警报")
        t("「裂缝 警报」不把警报当过滤条件", sp["tokens"] == [] and sp["residual"] is False)
        sp = ws.parse_fissure_spec("裂缝奥布山谷中断")
        t("地名不被栏目词吞掉（奥布山谷）",
          sp["column"] == "fissures" and "奥布山谷" in sp["tokens"])

        # ---------- 3) 监控指令解析 ----------
        print("[监控指令]")
        want = {
            "裂缝 后纪中断": (["fissures"], ["中断", "后纪"]),
            "后纪中断": (["fissures"], ["中断", "后纪"]),
            "裂缝火星中断": (["fissures"], ["中断", "火星"]),
            "钢铁裂缝中断": (["steel"], ["中断"]),
            "钢铁裂缝古纪中断": (["steel"], ["中断", "古纪"]),
            "裂缝后纪中断": (["fissures"], ["中断", "后纪"]),
            "钢铁裂缝 后纪 中断": (["steel"], ["中断", "后纪"]),
        }
        for text, (ty, toks) in want.items():
            c = ws._parse_monitor(text)
            t("监控 %-16s -> %s %s" % (text, ty, toks),
              c["action"] == "monitor" and c["types"] == ty
              and sorted(sum(c["fissure_filter"].values(), [])) == sorted(toks))
        t("裸「监控」仍回提示", ws._parse_monitor("")["action"] == "monitor_hint")
        t("不认识的词回提示", ws._parse_monitor("不认识的词")["action"] == "monitor_hint")
        c = ws._parse_monitor("裂缝 警报")
        t("「裂缝 警报」两类都监控且无过滤",
          sorted(c["types"]) == ["alerts", "fissures"] and "fissure_filter" not in c)
        c = ws._parse_monitor("裂缝后纪 警报")
        t("「裂缝后纪 警报」两类 + 裂缝过滤",
          sorted(c["types"]) == ["alerts", "fissures"]
          and c["fissure_filter"] == {"fissures": ["后纪"]})
        t("「平原」不受裂缝逻辑影响",
          ws._parse_monitor("平原") == {"action": "monitor", "types": ["cycles"]})
        t("「奥布山谷」仍开栏目而非裂缝过滤",
          ws._parse_monitor("奥布山谷") == {"action": "monitor", "types": ["vallis"]})

        # 查询路径同样支持过滤
        q = ws.parse_ws_command("裂缝 后纪中断")
        t("查询「裂缝 后纪中断」带 filter",
          q["action"] == "query" and q["types"] == ["fissures"]
          and sorted(q["filter"]) == ["中断", "后纪"])

        # ---------- 4) 匹配 ----------
        print("[匹配]")
        t("空条件全命中", all(ws._fissure_matches(f, []) for f in FIS))
        t("AND：中纪+生存 命中 1 条",
          len([f for f in FIS if ws._fissure_matches(f, ["中纪", "生存"])]) == 1)
        t("AND：中纪+中断 命中 0 条",
          len([f for f in FIS if ws._fissure_matches(f, ["中纪", "中断"])]) == 0)
        t("地点过滤 火星 命中 1 条",
          len([f for f in FIS if ws._fissure_matches(f, ["火星"])]) == 1)
        t("英文纪名 Axi 可被「后纪」命中",
          ws._fissure_matches(_fis("土星/Kronus", "中断", "Axi"), ["后纪"]))
        t("任务类型只匹配字段不做全句猜测",
          not ws._fissure_matches(_fis("土星/Kronus", "防御", "Axi"), ["中断"]))

        # ---------- 5) 格式化 ----------
        print("[格式化]")
        txt = ws._fmt_fissures(mk_state(), ["中纪"])
        t("过滤后只出命中条目", "火星/Tharsis" in txt and "地球/Everest" not in txt)
        t("标题计数按过滤后", "共 1" in txt)
        txt = ws._fmt_steel(mk_state(), ["后纪"])
        t("钢铁裂缝按纪过滤", "冥王星/Cerberus" in txt and "地球/ Everest" not in txt)
        txt = ws._fmt_fissures(mk_state(), ["安魂"])
        t("无命中时明确说无且带上条件", "无匹配" in txt and "安魂" in txt)
        t("不传条件照旧全量", "地球/Everest" in ws._fmt_fissures(mk_state()))

        # ---------- 6) 会话存储 ----------
        print("[会话存储]")
        reset()
        M.start_monitor(KA[0], KA[1], {"fissures"}, filters={"fissures": ["后纪", "中断"]})
        t("内存里存下过滤", M._fissure_filters.get(KA) == {"fissures": ["后纪", "中断"]})
        obj = json.loads(Path(FF).read_text(encoding="utf-8"))
        t("写盘内容正确",
          obj["sessions"]["group|G1"] == {"fissures": ["后纪", "中断"]})
        M._fissure_filters.clear()
        t("load_filters 返回恢复条数", M.load_filters() == 1)
        t("load_filters 还原条件", M._fissure_filters.get(KA) == {"fissures": ["后纪", "中断"]})
        # 关掉该栏目的开关 → 过滤与栏目一起清掉，避免残留条件影响下次整栏监控
        M.set_switch(KA[0], KA[1], "fissures", False)
        t("关闭栏目后清掉该栏过滤", "fissures" not in (M._fissure_filters.get(KA) or {}))
        M.start_monitor(KA[0], KA[1], {"steel"}, filters={"steel": ["古纪"]})
        M.stop_monitor(KA[0], KA[1])
        t("停止监控清掉全部过滤", KA not in M._fissure_filters)
        # 重发不带条件的「监控 裂缝」要清掉旧条件，否则会静默沿用上次过滤
        M.start_monitor(KA[0], KA[1], {"fissures"}, filters={"fissures": ["后纪"]})
        M.start_monitor(KA[0], KA[1], {"fissures"})
        t("重发无条件的监控会清掉旧过滤", KA not in M._fissure_filters)
        obj = json.loads(Path(FF).read_text(encoding="utf-8"))
        t("停止后写盘无该会话", "group|G1" not in obj["sessions"])

        # ---------- 7) 推送 ----------
        print("[推送]")
        reset()
        stt = mk_state()
        M.start_monitor(KA[0], KA[1], {"fissures"}, filters={"fissures": ["中纪"]})
        parts = M._parts_of(stt, {"fissures"}, KA)
        t("_parts_of 应用会话过滤",
          len(parts) == 1 and "火星/Tharsis" in parts[0][1]
          and "地球/Everest" not in parts[0][1])
        t("其它会话不加过滤",
          "地球/Everest" in M._parts_of(stt, {"fissures"}, ("group", "G2"))[0][1])
        sent.clear()
        once(stt)
        body = "\n".join(sent)
        t("push_once 只推命中条目",
          "火星/Tharsis" in body and "地球/Everest" not in body)
        t("推送日志按栏目独立（未混入其它栏目）", "钢铁裂缝" not in body)

        # ---------- 8) 聊天指令端到端 ----------
        print("[聊天指令]")
        reset()
        sent.clear()
        ctx = {"channel": "group", "group_id": "G1"}
        r = str(qq_common.handle_ws("监控 裂缝后纪中断", ctx))
        t("监控带条件回复写明条件", "只推送同时满足" in r and "后纪" in r and "中断" in r)
        t("监控带条件真的落库",
          M._fissure_filters.get(KA) == {"fissures": ["中断", "后纪"]})
        t("监控带条件只开裂缝", M._monitors.get(KA) == {"fissures"})
        r = str(qq_common.handle_ws("监控 钢铁裂缝中断", ctx))
        t("钢铁裂缝带任务过滤给出数据源提示", "不提供任务类型" in r)
        reset()
        r = str(qq_common.handle_ws("监控 不认识的词", ctx))
        t("不认识的词仍回提示、不开监控",
          "需要明确指定内容" in r and not M._monitors.get(KA))
        # 查询带过滤：走 handle_query，用 mock 状态
        reset()
        M.start_monitor(KA[0], KA[1], {"fissures"})
        with patch.object(ws, "get_state", lambda: mk_state()), \
             patch.object(ws, "activity_touch", lambda: None):
            out = str(qq_common.handle_ws("裂缝 中纪", ctx))
        t("查询带过滤只回命中条目", "火星/Tharsis" in out and "地球/Everest" not in out)

    print("\n%d/%d PASS, %d FAIL" % (ok, total, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
