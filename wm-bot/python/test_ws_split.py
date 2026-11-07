#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周期全量推送「按栏目拆分」离线测试（不联网、不改 data/config.txt）

覆盖：
  - 监控多个栏目时，每条栏目单独一条消息（文字标签 + 该栏目卡片图），而非一张大图
  - 按栏目独立去重：内容没变不重发，只有变化的栏目才重发
  - WS_PUSH_SPLIT_MAX 单轮最多发 N 条（监控「全部」防刷屏护栏）
  - 拆分后每条消息只含该栏目内容，不串台
  - 某栏目发送失败时进暂存，不污染其它栏目
  - monitor_label 对单栏目文本仍给出正确标签（赏金 / 商人 / 裂缝 …）
"""
import os
import sys
import time
from contextlib import ExitStack
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 固定配置，避免受本机 data/config.txt 影响
os.environ["WM_SOURCE"] = "mock"
os.environ["WS_PRIORITY_TYPES"] = "alerts,events"   # 主推栏目走另一条路，不进周期拆分
os.environ["WS_PRIORITY_SKIP_IN_FULL"] = "1"
os.environ["WS_ACTIVE_MIN_GAP"] = "0"
os.environ["WS_PUSH_INTERVAL"] = "0"                # 测试不真睡
os.environ["WS_PUSH_SPLIT_MAX"] = "10"

import worldstate as ws
import ws_monitor as M
import tempfile as _tf
from pathlib import Path as _P
# 去重记录落盘重定向到临时目录，绝不污染真实 data/ws_last_sent.json
M.LAST_SENT_FILE = _P(_tf.gettempdir()) / "wm_ws_last_sent_testsplit.json"

KA = ("group", "G1")

# 模拟的栏目文本（按 TYPES 展示序，含各栏目关键字，便于 monitor_label 正确匹配）
_PART_TEXT = {
    "bounty":      "🧾 开放世界赏金（2 个集团）\n  · 希图斯（5 个）",
    "voidtrader":  "🛸 虚空商人（白老头）：已在 飞船",
    "fissures":    "🌌 裂缝\n  - 地球 灭绝 20分钟",
    "nightwave":   "🌙 午夜电波\n  - 周常挑战 1/3",
}
_LABEL = {
    "bounty": "赏金",
    "voidtrader": "商人",
    "fissures": "裂缝",
    "nightwave": "电波",
}


def reset():
    M.clear_pending()
    M._monitors.clear()
    M._last_sent.clear()
    M._next_probe = 0.0


def fake_parts_factory(types):
    """返回一个按 TYPES 展示序、仅含给定栏目的 _parts_of 替身。"""
    def _fake(state, req_types):
        return [(t, _PART_TEXT[t]) for t in ws.TYPES
                if t in req_types and t in _PART_TEXT]
    return _fake


def bind(types):
    reset()
    M._monitors[KA] = set(types)
    M._next_probe = 0.0

    def _sender(ch, tid, text):
        calls.append((ch, tid, text))
        return True
    M.set_sender(_sender)


calls = []


def once(state, types):
    fp = fake_parts_factory(types)
    ctx = [patch.object(ws, "ensure_fresh", lambda: state),
           patch.object(ws, "activity_touch", lambda: None),
           patch.object(ws, "ensure_active", lambda: None),
           patch.object(M, "_parts_of", fp)]
    with ExitStack() as stack:
        for c in ctx:
            stack.enter_context(c)
        return M.push_once()


def main():
    ok = total = 0

    def t(name, cond):
        nonlocal ok, total
        total += 1
        ok += (1 if cond else 0)
        print(("  PASS  " if cond else "  FAIL  ") + name)
        return cond

    monitored = ["bounty", "voidtrader", "fissures", "nightwave"]

    # ---------- 1) 多栏目各自独立一条消息 ----------
    print("[多栏目按栏目拆分发送]")
    bind(monitored)
    calls.clear()
    sent = once({}, monitored)
    t("一轮返回 1（本会话至少发过一条）", sent == 1)
    t("4 个栏目 → 4 条独立消息", len(calls) == 4)
    got = {c[2] for c in calls}
    t("每条消息只含一个栏目、互不串台",
      got == set(_PART_TEXT[t] for t in monitored))
    t("消息都发到正确会话", all(c[0] == "group" and c[1] == "G1" for c in calls))

    # ---------- 2) monitor_label 对单栏目文本给出正确标签 ----------
    print("[单栏目标签匹配]")
    for tp in monitored:
        t("monitor_label(%s) -> %s" % (tp, _LABEL[tp]),
          M.monitor_label(_PART_TEXT[tp]) == "【%s监控信息如下】" % _LABEL[tp])

    # ---------- 3) 按栏目独立去重：内容没变不重发 ----------
    print("[按栏目去重]")
    bind(monitored)
    calls.clear()
    once({}, monitored)                # 首轮全发
    first = len(calls)
    calls.clear()
    again = once({}, monitored)        # 同样内容再跑一轮
    t("首轮发了 %d 条" % first, first == 4)
    t("内容不变 → 第二轮 0 条、sent=0", again == 0 and len(calls) == 0)

    # ---------- 4) 只有变化的栏目才重发 ----------
    print("[仅变化栏目重发]")
    bind(monitored)
    calls.clear()
    once({}, monitored)                # 首轮
    calls.clear()
    # 只改 bounty 文本（模拟赏金板刷新），其它不变
    changed = dict(_PART_TEXT)
    changed["bounty"] = "🧾 开放世界赏金（3 个集团）\n  · 奥布山谷（6 个）"
    with patch.dict(_PART_TEXT, changed):
        sent2 = once({}, monitored)
    t("只有 bounty 变化 → 仅发 1 条", len(calls) == 1 and sent2 == 1)
    t("变化的是赏金栏目", calls and calls[0][2] == changed["bounty"])

    # ---------- 5) WS_PUSH_SPLIT_MAX 单轮上限 ----------
    print("[单轮最多发 N 条]")
    # TYPES 展示序中 fissures 在 bounty 前、voidtrader 更靠后，
    # 所以 split_max=2 取到的是「裂缝 + 赏金」（按 TYPES 序的前两个）
    first_two = [t for t in ws.TYPES if t in monitored][:2]
    with patch.dict(os.environ, {"WS_PUSH_SPLIT_MAX": "2"}):
        bind(monitored)
        calls.clear()
        once({}, monitored)
        t("上限 2 → 只发 2 条", len(calls) == 2)
        t("发的是 TYPES 序前两个栏目（%s）" % "、".join(first_two),
          {c[2] for c in calls} == {_PART_TEXT[t] for t in first_two})

    # ---------- 6) 单栏目发送失败进暂存、不污染其它栏目 ----------
    print("[单栏目失败进暂存]")
    bind(monitored)
    calls.clear()
    fails = {"voidtrader"}             # 模拟「商人」那条发送失败

    def _sender_fail(ch, tid, text):
        if "虚空商人" in text:
            return False
        calls.append((ch, tid, text))
        return True
    M.set_sender(_sender_fail)
    sent3 = once({}, monitored)
    t("成功栏目发出 3 条", len(calls) == 3)
    t("本轮该会话记为已发（sent=1）", sent3 == 1)
    t("失败的栏目进了暂存", KA in M._pending and
      [p[0] for p in M._pending[KA]["parts"]] == ["voidtrader"])

    # ---------- 7) 主推栏目不进周期拆分 ----------
    print("[主推栏目走原路]")
    bind(["alerts", "events"])        # 纯主推栏目，周期 full 为空
    calls.clear()
    sent4 = once({"alerts": [_alert()], "events": [_event()]}, ["alerts", "events"])
    t("纯主推栏目 → 周期 0 条（交给主推分支）", sent4 == 0 and len(calls) == 0)

    # ---------- 8) 去重记录持久化：重启后内容没变不重发 ----------
    print("[去重记录持久化·防重启重发]")
    import tempfile
    from pathlib import Path
    tmpf = Path(tempfile.gettempdir()) / "wm_ws_last_sent_test.json"
    for p in (tmpf, tmpf.with_name(tmpf.name + ".tmp")):
        try:
            p.unlink()
        except OSError:
            pass
    with patch.object(M, "LAST_SENT_FILE", tmpf):
        bind(monitored)
        calls.clear()
        once({}, monitored)                    # 首轮全发，并自动落盘
        t("首轮发出 4 条", len(calls) == 4)
        t("去重记录已落盘", tmpf.exists())
        # 模拟重启：内存清空（set_sender 会清）+ 从磁盘恢复
        M._last_sent.clear()
        M.restore_last_sent()
        calls.clear()
        again = once({}, monitored)            # 内容没变 → 应 0 条
        t("重启后内容不变 → 不重发（0 条）", again == 0 and len(calls) == 0)
    for p in (tmpf, tmpf.with_name(tmpf.name + ".tmp")):
        try:
            p.unlink()
        except OSError:
            pass

    # ---------- 9) 用户查询优先：查询进行中推送让行 ----------
    print("[用户查询优先·推送让行]")
    bind(monitored)
    calls.clear()
    M.query_begin()
    try:
        busy_sent = once({}, monitored)
        t("查询处理中 → 推送一条都不发", busy_sent == 0 and len(calls) == 0)
        t("让行时不推进去重（下轮还能补）",
          not (M._last_sent.get(KA) or {}))
    finally:
        M.query_end()
    t("查询结束后不再让行", M.query_busy() is False)
    after = once({}, monitored)
    t("查询结束后正常补发（4 条）", after == 1 and len(calls) == 4)

    print("\n拆分测试：%d/%d 通过" % (ok, total))
    return 0 if ok == total else 1


def _alert(node="地球/Everest", item="Orokin 催化剂"):
    return {"mission": {"node": node, "type": "歼灭", "faction": "克隆尼",
                        "minEnemyLevel": 10, "maxEnemyLevel": 30},
            "reward": {"items": [item], "credits": 0},
            "expiry": time.time() + 7200}


def _event(desc="双倍声望"):
    return {"description": desc, "expiry": time.time() + 7200}


if __name__ == "__main__":
    sys.exit(main())
