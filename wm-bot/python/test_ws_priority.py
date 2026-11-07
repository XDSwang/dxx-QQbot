#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""警报 / 活动「事件主推」离线测试（不联网、不读真实缓存、不改 data/config.txt）

覆盖：
  - 主推栏目配置（英文键 / 中文别名 / off 关掉主推）
  - 稳定签名：倒计时跳动不算内容变化，奖励 / 条数变化才算
  - 开监控先记基线不补发历史；新增条目立即推且绕过 300 秒周期限频
  - 内容不变不重推 / 条目结束静默 / 60 秒防抖
  - 推送失败保留基线自动重试 + 暂存可随消息捎带
  - 周期全量推送剔除主推栏目（开关可关）
  - WS_AUTO_PRIORITY 默认关、开启后只合并白名单群
"""
import os
import sys
import time
from contextlib import ExitStack
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 固定配置，避免测试结果受本机 data/config.txt 影响
os.environ["WM_SOURCE"] = "mock"
os.environ["WS_PRIORITY_TYPES"] = "alerts,events"
os.environ["WS_PRIORITY_MIN_GAP"] = "0"
os.environ["WS_PRIORITY_SKIP_IN_FULL"] = "1"
os.environ["WS_AUTO_PRIORITY"] = "0"
os.environ["WS_ACTIVE_MIN_GAP"] = "300"

import worldstate as ws
import ws_monitor as M
import tempfile as _tf
from pathlib import Path as _P
M.LAST_SENT_FILE = _P(_tf.gettempdir()) / "wm_ws_last_sent_testprio.json"


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    return 1 if cond else 0


KA = ("group", "G1")
EXP = time.time() + 7200


def _alert(node, item="Orokin 催化剂", expiry=EXP, credits=0):
    return {"mission": {"node": node, "type": "歼灭", "faction": "克隆尼",
                        "minEnemyLevel": 10, "maxEnemyLevel": 30},
            "reward": {"items": [item], "credits": credits},
            "expiry": expiry}


def _event(desc, expiry=EXP):
    return {"description": desc, "expiry": expiry}


def mk(alerts=(), events=()):
    return {"alerts": list(alerts), "events": list(events)}


A1 = _alert("地球/Everest")
A2 = _alert("火星/Wahiba", "Forma")
E1 = _event("orphys 的赠礼：双倍声望活动")


def reset():
    """清空所有会话状态；不重新绑定 sender。"""
    M.clear_pending()
    M._monitors.clear()
    M._last_sent.clear()
    M._next_probe = 0.0


def bind(calls, sender=None, monitors=None):
    """绑定一轮测试用的 sender + 监控注册表（会清周期记录，符合换通道语义）。"""
    reset()
    M._monitors.update(monitors if monitors is not None else {KA: {"alerts", "events"}})

    def _sender(ch, tid, text):
        calls.append(text)
        return True if sender is None else sender

    M.set_sender(_sender)
    M._next_probe = 0.0


def once(st, spy_parts=None, extra=()):
    """只跑一轮 push_once；不碰 sender、不重置任何推送记录。"""
    ctx = [patch.object(ws, "ensure_fresh", lambda: st),
           patch.object(ws, "activity_touch", lambda: None),
           patch.object(ws, "ensure_active", lambda: None)]
    if spy_parts is not None:
        real = M._parts_of

        def _spy(state, types):
            spy_parts.append(set(types))
            return real(state, types)
        ctx.append(patch.object(M, "_parts_of", _spy))
    ctx.extend(extra)
    with ExitStack() as stack:
        for c in ctx:
            stack.enter_context(c)
        return M.push_once()


def main():
    ok = total = 0

    def t(name, cond):
        nonlocal ok, total
        total += 1
        ok += check(name, cond)

    # ---------- 1) 配置与主推栏目解析 ----------
    print("[主推栏目配置]")
    reset()
    # 本模块顶部把 WS_PRIORITY_TYPES 设成了 alerts,events；这里临时清空才能真正测默认
    with patch.dict(os.environ, {"WS_PRIORITY_TYPES": ""}):
        t("默认不主推（等周期拆分推，不即时推）", M.priority_types() == [])
    with patch.dict(os.environ, {"WS_PRIORITY_TYPES": "警报,活动"}):
        t("中文别名也能配", M.priority_types() == ["alerts", "events"])
    with patch.dict(os.environ, {"WS_PRIORITY_TYPES": "alerts,invasions"}):
        t("可换成别的栏目", M.priority_types() == ["alerts", "invasions"])
    with patch.dict(os.environ, {"WS_PRIORITY_TYPES": "off"}):
        t("写 off 关掉主推", M.priority_types() == [])
    t("WS_AUTO_PRIORITY 默认关", M.auto_priority_enabled() is False)

    # ---------- 2) 稳定签名：倒计时不算变化 ----------
    print("[稳定签名]")
    a = "📢 警报（共 1）\n1. 地球 · 歼灭\n   奖励：Orokin 催化剂\n   剩余：5分30秒"
    b = "📢 警报（共 1）\n1. 地球 · 歼灭\n   奖励：Orokin 催化剂\n   剩余：4分12秒"
    t("倒计时从 5分30秒 变 4分12秒 → 签名不变", M.stable_sig(a) == M.stable_sig(b))
    t("原签名确实会变（证明确实修了东西）", M._sig(a) != M._sig(b))
    t("奖励变化 → 签名变", M.stable_sig(b) != M.stable_sig(b.replace("催化剂", "Forma")))
    t("条数变化 → 签名变", M.stable_sig(b) != M.stable_sig(b.replace("共 1", "共 2")))
    long1 = "💎 瓦奇娅：未到场 · 1小时20分30秒 后抵达"
    long2 = "💎 瓦奇娅：未到场 · 1小时19分10秒 后抵达"
    t("天/小时/分/秒复合时长也归一", M.stable_sig(long1) == M.stable_sig(long2))

    # ---------- 3) 开监控先记基线，不补发历史 ----------
    print("[首次只记基线]")
    calls = []
    bind(calls)
    sent = once(mk([A1], [E1]))
    t("首次不推历史内容", sent == 0)
    t("sender 一次都没调用", calls == [])
    t("基线里同时记了警报和活动",
      set(M._seen_priority.get(KA, {})) == {"alerts", "events"})
    t("警报基线 1 条", len(M._seen_priority[KA]["alerts"]) == 1)
    t("活动基线 1 条", len(M._seen_priority[KA]["events"]) == 1)

    # ---------- 4) 新警报立刻推，绕过 300 秒周期限频 ----------
    print("[新增警报立即推]")
    M._last_sent[KA] = ("周期刚推过", time.time())  # 模拟周期通道刚发过
    sent = once(mk([A1, A2], [E1]))
    t("新增一条警报 → sent=1", sent == 1)
    t("只发一条消息", len(calls) == 1)
    t("带了主推标题", "🚨 世界状态主推" in calls[0])
    t("带新警报节点", "火星/Wahiba" in calls[0])
    t("带奖励内容", "Forma" in calls[0])
    t("周期通道的 300 秒限频没拦住它", M._last_sent.get(KA, (None,))[0] == "周期刚推过")
    t("推送成功后基线推进到 2 条", len(M._seen_priority[KA]["alerts"]) == 2)

    # ---------- 5) 内容不变不重推 ----------
    print("[不重复推]")
    sent = once(mk([A1, A2], [E1]))
    t("内容不变 sent=0", sent == 0 and len(calls) == 1)
    sent = once(mk([A1, A2], [E1]))
    t("再多跑几轮也不推", sent == 0 and len(calls) == 1)

    # ---------- 6) 新增活动也推 ----------
    print("[新增活动立即推]")
    sent = once(mk([A1, A2], [E1, _event("TennoCon 预热：免费纹章")]))
    t("新增活动 → sent=1", sent == 1)
    t("只装变化的栏目（不含警报正文）",
      "🎉 活动" in calls[-1] and "📢 警报" not in calls[-1])
    t("带新活动描述", "TennoCon" in calls[-1])

    # ---------- 7) 条目结束静默，不推「活动结束」 ----------
    print("[条目结束静默]")
    before = len(calls)
    sent = once(mk([A1], [E1]))
    t("警报少一条、活动少一条 → 不推", sent == 0 and len(calls) == before)
    t("基线静默消化到 1 条", len(M._seen_priority[KA]["alerts"]) == 1)
    sent = once(mk([A1, A2], [E1]))
    t("之后 A2 重新出现仍算新增 → 推", sent == 1 and "火星/Wahiba" in calls[-1])

    # ---------- 8) 主推防抖 ----------
    print("[主推 60 秒防抖]")
    calls = []
    bind(calls)
    once(mk([A1], [E1]))
    old = set(M._seen_priority[KA]["alerts"])
    with patch.dict(os.environ, {"WS_PRIORITY_MIN_GAP": "300"}):
        M._priority_sent_at[KA] = time.time()
        sent = once(mk([A1, A2], [E1]))
        t("防抖期内不推", sent == 0 and calls == [])
        t("防抖时基线不推进（下轮还能补）",
          M._seen_priority[KA]["alerts"] == old)
        M._priority_sent_at.pop(KA, None)
        sent = once(mk([A1, A2], [E1]))
        t("防抖解除后补推", sent == 1 and "火星/Wahiba" in calls[0])

    # ---------- 9) 推送失败：保留基线 + 暂存 + 自动重试 ----------
    print("[失败不吞新警报]")
    calls = []
    bind(calls)
    once(mk([A1], [E1]))
    old = set(M._seen_priority[KA]["alerts"])
    bind_fail = []
    # 换一个会失败的 sender（不重建注册表，也不动基线）
    M.set_sender(lambda ch, tid, text: (bind_fail.append(text), False)[1])
    M._next_probe = 0.0
    sent = once(mk([A1, A2], [E1]))
    t("发送失败 sent=0", sent == 0)
    t("基线仍是旧的 1 条（没被吞）", M._seen_priority[KA]["alerts"] == old)
    t("内容进了暂存", M.pending_count() == 1)
    t("暂存里带新警报文本", "火星/Wahiba" in M._pending[KA]["text"])
    t("暂存的 types 只标变化栏目", M._pending[KA]["types"] == {"alerts"})
    calls2 = []
    M.set_sender(lambda ch, tid, text: calls2.append(text) or True)
    M._next_probe = 0.0
    sent = once(mk([A1, A2], [E1]))
    t("下一轮自动重试并成功", sent == 1 and "🚨" in calls2[0])
    t("成功后暂存清空", M.pending_count() == 0)
    t("成功后基线推进到 2 条", len(M._seen_priority[KA]["alerts"]) == 2)

    # ---------- 10) 暂存可随消息捎带、covered 剔除 ----------
    print("[失败时也能随消息捎带]")
    calls = []
    bind(calls)
    once(mk([A1], [E1]))
    M.set_sender(lambda *a: False)
    M._next_probe = 0.0
    once(mk([A1, A2], [E1]))
    got = M.pending_for(*KA)
    t("能取出暂存内容", "火星/Wahiba" in got)
    t("本次查询已覆盖 alerts → 不重复贴",
      M.pending_for(*KA, covered={"alerts"}) == "")

    # ---------- 10.5) 主动不通时，主推暂存不能被周期内容覆盖 ----------
    print("[主动不通时新警报仍能捎带]")
    calls = []
    bind(calls, sender=False,
         monitors={KA: {"alerts", "events", "fissures"}})
    once(mk([A1], [E1]))                 # 首次：记基线，周期写暂存
    t("首次周期暂存不含新警报（主推栏目已剔除）",
      "火星/Wahiba" not in (M._pending.get(KA) or {}).get("text", ""))
    once(mk([A1, A2], [E1]))             # 主推失败 → 覆盖暂存并跳过周期
    t("主推暂存带 priority 标记", M._pending[KA].get("priority") is True)
    t("暂存内容是新警报（没被周期覆盖）", "火星/Wahiba" in M._pending[KA]["text"])
    t("暂存 parts 只含变化栏目",
      [x[0] for x in M._pending[KA]["parts"]] == ["alerts"])
    once(mk([A1, A2], [E1]))             # 再失败一轮，仍不被覆盖
    t("多轮失败后暂存仍是新警报",
      M._pending[KA].get("priority") is True
      and "火星/Wahiba" in M._pending[KA]["text"])
    t("随消息捎带能拿到新警报", "火星/Wahiba" in M.pending_for(*KA))
    # 防抖期内周期发送成功，也不能顺手清掉主推暂存
    M.set_sender(lambda *a: True)
    M._next_probe = 0.0
    M._priority_sent_at[KA] = time.time()
    with patch.dict(os.environ, {"WS_PRIORITY_MIN_GAP": "300"}), \
         patch.object(M, "_parts_of",
                      lambda state, types: [("fissures", "🔥 裂缝（共 1）")]):
        once(mk([A1, A2], [E1]))
    t("周期推送成功也没清掉主推暂存",
      M._pending[KA].get("priority") is True
      and "火星/Wahiba" in M._pending[KA]["text"])

    # ---------- 11) 周期全量推送剔除主推栏目 ----------
    print("[周期推送不重复贴主推栏目]")
    calls = []
    bind(calls, monitors={KA: {"alerts", "events", "fissures"}})
    seen = []
    once(mk([A1], [E1]), spy_parts=seen)
    t("全量只格式化非主推栏目", seen == [{"fissures"}])
    with patch.dict(os.environ, {"WS_PRIORITY_SKIP_IN_FULL": "0"}):
        calls = []
        bind(calls, monitors={KA: {"alerts", "events", "fissures"}})
        seen2 = []
        once(mk([A1], [E1]), spy_parts=seen2)
        t("关掉开关后仍包含主推栏目",
          bool(seen2) and {"alerts", "events"} <= seen2[0])

    # ---------- 12) 关掉主推后行为回落为纯周期 ----------
    print("[主推可整体关闭]")
    with patch.dict(os.environ, {"WS_PRIORITY_TYPES": "off"}):
        t("配置成 off → 无主推栏目", M.priority_types() == [])
        calls = []
        bind(calls)
        states = iter([mk([A1], [E1]), mk([A1, A2], [E1])])
        with patch.object(ws, "ensure_fresh", lambda: next(states)), \
             patch.object(ws, "activity_touch", lambda: None), \
             patch.object(ws, "ensure_active", lambda: None):
            M.push_once()                     # 首次：周期按栏目拆分推（警报 + 活动各一条）
            first = len(calls)
            sent = M.push_once()              # 新增警报：不再即时主推，改走周期拆分
        t("关掉主推后首次周期按栏目拆分推两条（警报+活动）", first == 2)
        # 第二轮只有「警报」内容变了（多出 A2），但距首次发送不足 WS_ACTIVE_MIN_GAP(300s)
        # 周期限频，按栏目去重后被拦住、本轮不发（活动没变也不重发）。这正说明关掉主推后
        # 新增警报不再即时推，而是等周期限频过了再补。所以 sent=0、总条数仍为 2。
        t("关掉主推后新增警报被周期限频拦住、不再即时推",
          sent == 0 and len(calls) == 2)

    # ---------- 13) 稳定签名让周期推送不再因倒计时刷屏 ----------
    print("[周期签名剔除倒计时]")
    calls = []
    bind(calls, monitors={KA: {"fissures"}})
    texts = iter(["🔥 裂缝\n   剩余：5分30秒", "🔥 裂缝\n   剩余：4分12秒"])
    with patch.object(ws, "ensure_fresh", lambda: mk()), \
         patch.object(ws, "activity_touch", lambda: None), \
         patch.object(ws, "ensure_active", lambda: None), \
         patch.object(M, "_parts_of",
                      lambda state, types: [("fissures", next(texts))]):
        s1 = M.push_once()
        s2 = M.push_once()
    t("周期首次推送成功", s1 == 1)
    t("只有倒计时变了 → 不再重发", s2 == 0 and len(calls) == 1)

    # ---------- 14) 自动注册主推（默认关） ----------
    print("[WS_AUTO_PRIORITY 默认关 / 开启只合并白名单群]")
    reset()
    t("默认关 → 不注册任何会话", M.auto_start_priority({"G1", "G2"}) == 0)
    t("默认关 → 注册表仍为空", M.list_monitors() == {})
    with patch.dict(os.environ, {"WS_AUTO_PRIORITY": "1"}), \
         patch.object(ws, "ensure_active", lambda: None), \
         patch.object(M, "_ensure_push_thread", lambda: None):
        t("开关打开", M.auto_priority_enabled() is True)
        n = M.auto_start_priority({"G1", "G2"})
        t("两个白名单群都被注册", n == 2)
        t("注册的正是主推栏目",
          all(v == ["alerts", "events"] for v in M.list_monitors().values()))
        M._monitors[("group", "G1")].add("fissures")
        n2 = M.auto_start_priority({"G1", "G2"})
        t("已有主推栏目的会话不重复计数", n2 == 0)
        t("用户手动加的栏目没被覆盖",
          "fissures" in M._monitors[("group", "G1")])
        t("白名单外的群不会被自动注册", ("group", "G9") not in M._monitors)

    # ---------- 15) 停止监控清干净主推状态 ----------
    print("[停止监控清主推状态]")
    calls = []
    bind(calls)
    once(mk([A1], [E1]))
    t("停之前有基线", KA in M._seen_priority)
    t("stop_monitor 返回 True", M.stop_monitor(*KA) is True)
    t("基线被清掉", KA not in M._seen_priority)
    t("防抖时刻被清掉", KA not in M._priority_sent_at)

    # ---------- 16) 监控推送文字标签：按关键字匹配来源 ----------
    print("[监控文字标签 monitor_label]")
    t("赏金 → 赏金监控信息如下",
      M.monitor_label("开放世界赏金(3个集团)\n赏金 · 索拉里斯") == "【赏金监控信息如下】")
    t("希图斯 → 希图斯监控信息如下（只看首行）",
      M.monitor_label("希图斯（昼）\n夜灵平原 剩余") == "【希图斯监控信息如下】")
    t("虚空裂隙 → 裂缝监控信息如下",
      M.monitor_label("虚空裂隙\n中断事件") == "【裂缝监控信息如下】")
    t("后纪中断/强袭 → 中断监控信息如下",
      M.monitor_label("后纪中断\n强袭") == "【中断监控信息如下】")
    t("星图/地图 → 地图监控信息如下",
      M.monitor_label("星图轮换\n地图") == "【地图监控信息如下】")
    t("警报 → 警报监控信息如下",
      M.monitor_label("警报\n地球/Everest") == "【警报监控信息如下】")
    t("活动 → 活动监控信息如下",
      M.monitor_label("活动\norphys 的赠礼") == "【活动监控信息如下】")
    t("入侵 → 入侵监控信息如下",
      M.monitor_label("入侵\nGrineer 节点") == "【入侵监控信息如下】")
    t("赏金优先于平原（避免误标）",
      M.monitor_label("赏金 · 希图斯集团") == "【赏金监控信息如下】")
    t("裂缝优先于平原（避免误标）",
      M.monitor_label("裂缝\n奥布山谷附近") == "【裂缝监控信息如下】")
    # —— 关键回归：平原正文内嵌「赏金 ·」行，不能把整个栏目误标成「赏金」——
    t("平原内嵌赏金行 → 仍按首行标希图斯（不误标赏金）",
      M.monitor_label("希图斯（地球平原）：☀️ 白天 剩余 5分\n   赏金 · 希图斯（5 个）")
      == "【希图斯监控信息如下】")
    t("平原昼夜组合视图（≥2 区域）→ 平原昼夜",
      M.monitor_label("希图斯（地球平原）：☀️ 白天\n   赏金 · 希图斯\n\n"
                      "奥布山谷（金星平原）：🔥 温暖\n   赏金 · 索拉里斯")
      == "【平原昼夜监控信息如下】")
    t("赏金轮换 与 赏金 不再撞名",
      M.monitor_label("🔄 赏金轮换（本轮 A）\n  本轮奖励：x") == "【赏金轮换监控信息如下】"
      and M.monitor_label("🧾 开放世界赏金（3 个集团）") == "【赏金监控信息如下】")
    t("瓦奇娅 与 虚空商人 不再撞名",
      M.monitor_label("💎 瓦奇娅（原初重生）· Maroo 的市集") == "【瓦奇娅监控信息如下】"
      and M.monitor_label("🛸 虚空商人（白老头）：已在 飞船") == "【商人监控信息如下】")
    t("信条武器 / 终幕武器 不再都叫「武器」",
      M.monitor_label("🗡️ 信条武器：（当前无）") == "【信条武器监控信息如下】"
      and M.monitor_label("🔫 终幕武器：（当前无）") == "【终幕武器监控信息如下】")
    t("钢铁裂缝 与 裂缝 不再撞名",
      M.monitor_label("⚙️ 钢铁裂缝（共 2）") == "【钢铁裂缝监控信息如下】"
      and M.monitor_label("🔥 裂缝\n  - 地球") == "【裂缝监控信息如下】")
    t("无关键字 → 通用监控信息如下",
      M.monitor_label("") == "【监控信息如下】")
    t("None 也不报错", M.monitor_label(None) == "【监控信息如下】")
    t("大小写不敏感（Fissure）",
      M.monitor_label("FISSURE 虚空裂隙") == "【裂缝监控信息如下】")

    print("\n结果：通过 %d / 失败 %d" % (ok, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
