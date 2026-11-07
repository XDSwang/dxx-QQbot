#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控推送「两手准备」+ 图片优先（离线，不联网、不依赖 data/ 配置）

覆盖：
  - 主动发送失败 → 暂存；成功 → 清暂存（不重复）
  - 随查询附带的限频 / 保鲜期 / 截断 / 总开关
  - stop_monitor 清暂存
  - handler 把监控内容拼在回复末尾（世界状态 / WM / 帮助都带上）
  - 默认图片优先；「文字」前缀强制纯文本（对 WM 也生效）
"""
import os
import sys
import time
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qq_common as q
import worldstate as ws
import ws_monitor as M
import tempfile as _tf
from pathlib import Path as _P
M.LAST_SENT_FILE = _P(_tf.gettempdir()) / "wm_ws_last_sent_testattach.json"


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    return 1 if cond else 0


KA = ("group", "G1")


def _seed(text="🛡️ 仲裁：赛德娜/Sangeru · 防御", at=None, fails=1, types=None):
    M._pending[KA] = {"text": text, "at": at if at is not None else time.time(),
                      "fails": fails,
                      "types": set(types) if types else set()}


def main():
    ok = 0
    total = 0
    M.clear_pending()
    M._monitors.clear()

    # ---------- 1) 主动发送失败 → 暂存 ----------
    print("[主动失败要攒下来]")
    M.set_sender(lambda ch, tid, text: False)
    M._monitors[KA] = {"fissures"}
    with patch.object(ws, "ensure_fresh", lambda: {}), \
         patch.object(ws, "format_for", lambda t: "🔥 裂缝（共 1）\n  1. 地球 · 生存 · 5 分钟"), \
         patch.object(ws, "activity_touch", lambda: None):
        sent = M.push_once()
        total += 1
        ok += check("主动失败时 sent=0", sent == 0)
        total += 1
        ok += check("内容进了暂存", M.pending_count() == 1)
        total += 1
        ok += check("fails 计数为 1", M._pending[KA]["fails"] == 1)
        M.push_once()
        total += 1
        ok += check("再次失败 fails 累加到 2", M._pending[KA]["fails"] == 2)

    # ---------- 2) 主动发送成功 → 清暂存 ----------
    print("[主动成功就不再附带]")
    M.set_sender(lambda ch, tid, text: True)
    with patch.object(ws, "ensure_fresh", lambda: {}), \
         patch.object(ws, "format_for", lambda t: "🔥 裂缝"), \
         patch.object(ws, "activity_touch", lambda: None):
        sent = M.push_once()
    total += 1
    ok += check("主动成功 sent=1", sent == 1)
    total += 1
    ok += check("暂存被清空", M.pending_count() == 0)

    # ---------- 2.5) 主动通道不通时退避探测，别每 10 秒空打接口 ----------
    print("[主动通道不通要退避]")
    calls = []
    M.set_sender(lambda ch, tid, text: calls.append(1) or False)
    M._monitors[KA] = {"fissures"}
    with patch.object(ws, "ensure_fresh", lambda: {}), \
         patch.object(ws, "format_for", lambda t: "🔥 裂缝"), \
         patch.object(ws, "activity_touch", lambda: None):
        M.push_once()
        first = len(calls)
        M.push_once()
    total += 1
    ok += check("失败一次后记录探测时刻", M._next_probe > time.time())
    total += 1
    ok += check("退避期内不再调用主动接口（%d 次）" % first, len(calls) == first == 1)
    total += 1
    ok += check("退避期内内容照样攒着", M.pending_count() == 1)
    # 退避到期后必须重新尝试（不能被每轮推后变成永远不试）
    M._next_probe = time.time() - 1
    with patch.object(ws, "ensure_fresh", lambda: {}), \
         patch.object(ws, "format_for", lambda t: "🔥 裂缝"), \
         patch.object(ws, "activity_touch", lambda: None):
        M.push_once()
    total += 1
    ok += check("退避到期后会重新尝试主动发送", len(calls) == first + 1)

    # ---------- 3) 随查询附带：首次给、紧接再取为空（限频） ----------
    print("[随查询附带的限频]")
    M.clear_pending()
    _seed("🛡️ 仲裁：赛德娜/Sangeru")
    got1 = M.pending_for(*KA)
    total += 1
    ok += check("首次能取到暂存内容", "Sangeru" in got1)
    total += 1
    ok += check("紧接着再取被限频（空串）", M.pending_for(*KA) == "")

    # 越过间隔后又能取到（且内容没被消耗掉）
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        got2 = M.pending_for(*KA)
    total += 1
    ok += check("间隔归零后可再取（内容不被消耗）", "Sangeru" in got2)

    # 别把别的会话的内容串过来
    total += 1
    ok += check("不会串到其它会话", M.pending_for("group", "G2") == "")

    # ---------- 4) 保鲜期 ----------
    print("[过期内容不再附带]")
    M._pending[KA] = {"text": "旧内容", "at": time.time() - 4 * 3600, "fails": 1}
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        total += 1
        ok += check("4 小时前的内容不再带出", M.pending_for(*KA) == "")
    total += 1
    ok += check("过期内容被清掉", M.pending_count() == 0)

    # ---------- 5) 截断 ----------
    print("[超长内容截断]")
    M.clear_pending()
    _seed("长" * 3000)
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0", "WS_ATTACH_MAX_BYTES": "300"},
                     clear=False):
        cut = M.pending_for(*KA)
    body_only = cut.split("\n…（")[0]
    total += 1
    ok += check("正文按字节截断到 300 以内", len(body_only.encode("utf-8")) <= 300)
    total += 1
    ok += check("截断处有省略说明", "已省略" in cut)

    # 多行内容要按整行截断，不能把一条数据的「剩余：xx」切成半行
    M.clear_pending()
    _seed("\n".join("第%02d行 内容填充填充" % i for i in range(1, 60)))
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0", "WS_ATTACH_MAX_BYTES": "200"},
                     clear=False):
        cut2 = M.pending_for(*KA)
    body2 = cut2.split("\n…（")[0].split("\n")
    total += 1
    ok += check("按整行截断（%d 行且每行完整）" % len(body2),
                bool(body2) and all(l.startswith("第") and "填充" in l for l in body2))
    total += 1
    ok += check("整行截断也没超过字节上限",
                len("\n".join(body2).encode("utf-8")) <= 200)

    # ---------- 6) 总开关 ----------
    print("[总开关 WS_ATTACH_ON_QUERY=0]")
    M.clear_pending()
    _seed("🛡️ 仲裁")
    with patch.dict(os.environ, {"WS_ATTACH_ON_QUERY": "0", "WS_ATTACH_GAP": "0"},
                     clear=False):
        total += 1
        ok += check("关掉后不附带", M.pending_for(*KA) == "")

    # ---------- 7) 停止监控清暂存 ----------
    print("[停止监控清暂存]")
    M.clear_pending()
    _seed("🛡️ 仲裁")
    M._monitors[KA] = {"fissures"}
    removed = M.stop_monitor(*KA)
    total += 1
    ok += check("停止监控返回 True", removed is True)
    total += 1
    ok += check("暂存同时被清", M.pending_count() == 0)

    # ---------- 8) handler 把监控内容拼到回复末尾 ----------
    print("[查询回复附带监控内容]")
    H = q.make_handler(allow_all=True)
    ctx = {"channel": "group", "group_id": "G1"}
    M.clear_pending()
    _seed("🛡️ 仲裁：赛德娜/Sangeru · 防御")
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        r = H("D/XX/帮助", ctx)
    total += 1
    ok += check("帮助回复里带出监控内容",
                isinstance(r, str) and "📬 监控推送" in r and "Sangeru" in r)
    total += 1
    ok += check("没有暂存时不加这一段",
                "📬 监控推送" not in q.attach_monitor("正文", ctx))

    # 监控开启确认 / 停止确认不该夹带（避免自己贴自己）
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        _seed("🛡️ 仲裁：赛德娜/Sangeru · 防御")
        total += 1
        ok += check("「停止监控」的确认不夹带",
                    "📬" not in H("D/XX/停止监控", ctx))

    # ---------- 8.5) 查询已覆盖监控栏目时不再重复附带 ----------
    print("[栏目重复时不重复附带]")
    M.clear_pending()
    _seed("🔥 裂缝（共 3）", types={"fissures"})
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        total += 1
        ok += check("监控「裂缝」+ 查询「裂缝」→ 不附带",
                    M.pending_for(*KA, covered={"fissures"}) == "")
        total += 1
        ok += check("监控「裂缝」+ 查询「仲裁」→ 照常附带",
                    "裂缝" in M.pending_for(*KA, covered={"arbitration"}))
    M.clear_pending()
    _seed("🔥 裂缝\n🛡️ 仲裁", types={"fissures", "arbitration"})
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        total += 1
        ok += check("监控两栏、查询只覆盖一栏 → 仍附带（另一栏是新内容）",
                    "仲裁" in M.pending_for(*KA, covered={"fissures"}))

    # 真实 push_once：两栏监控，只附带未被覆盖的那一栏
    M.clear_pending()
    M._monitors[KA] = {"fissures", "arbitration"}
    M.set_sender(lambda ch, tid, text: False)
    fake = {
        "fissures": [{"node": "地球/Bode", "missionType": "歼灭", "enemy": "Grineer",
                      "tier": "古纪", "expiry": time.time() + 600}],
        "arbitration": {"node": "土星/Kronus", "type": "防御", "enemy": "Corpus",
                        "expiry": time.time() + 900},
    }
    with patch.object(ws, "ensure_fresh", lambda: fake), \
         patch.object(ws, "activity_touch", lambda: None):
        M.push_once()
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        got = M.pending_for(*KA, covered={"fissures"})
    total += 1
    ok += check("分块去重：只附带仲裁，不再重贴裂缝",
                "仲裁" in got and "裂缝" not in got)
    M._monitors.clear()

    # handler 层要真的把 types 传下去
    M.clear_pending()
    _seed("🔥 裂缝（共 3）", types={"fissures"})
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        r_same = H("D/XX/裂缝", ctx)
    total += 1
    ok += check("handler 查询同栏目时不夹带", "📬 监控推送" not in r_same)

    # ---------- 9) 图片优先 / 文字前缀 ----------
    print("[默认一律出图，文字前缀可回退]")
    total += 1
    ok += check("默认模式是 always", q._card_mode() == "always")
    with patch.dict(os.environ, {"WM_WS_CARD": "always"}, clear=False):
        short = "✅ 已停止本会话的世界状态监控。"
        got = q.maybe_image(short)
        total += 1
        ok += check("短文本也出图（或无字体时退回原文）",
                    hasattr(got, "pngs") or got == short)
    with patch.dict(os.environ, {"WM_WS_CARD": "always"}, clear=False):
        total += 1
        ok += check("「文字」前缀压过默认出图",
                    q.maybe_image("正文内容", force_text=True) == "正文内容")

    # 「文字 WM xxx」剥前缀后仍能被识别为 WM 指令
    body, fi, ft = q.split_style_prefix("文字 WM Boltor Prime")
    total += 1
    ok += check("「文字 WM …」剥出纯文本并保留指令",
                body == "WM Boltor Prime" and ft and not fi)
    total += 1
    ok += check("剥前缀后 is_wm_command 仍命中", q.is_wm_command(body))

    # ---------- 10) 主动消息权限开通后：内容去重 + 最小间隔 ----------
    # 背景：官方主动消息 20 qpm、每群每天 1000 条。权限一开，若仍按
    # WS_PUSH_SECONDS=10 每轮发一条，一天 8640 条必然刷屏 + 撞频控。
    print("[主动推送去重 / 限频]")
    M.clear_pending()
    M._monitors.clear()
    M._monitors[KA] = {"fissures"}
    pushed = []
    M.set_sender(lambda ch, tid, text: pushed.append(text) or True)

    def _push_with(content):
        """直接指定本轮格式化结果，精确控制「内容有没有变」。"""
        with patch.object(ws, "ensure_fresh", lambda: {}), \
             patch.object(ws, "activity_touch", lambda: None), \
             patch.object(M, "_parts_of",
                          lambda state, types, key=None: [("fissures", content)]):
            return M.push_once()

    s1 = _push_with("🔥 裂缝")
    s2 = _push_with("🔥 裂缝")                  # 内容完全没变
    total += 1
    ok += check("首次主动推送成功", s1 == 1)
    total += 1
    ok += check("内容不变不重发", s2 == 0 and len(pushed) == 1)
    with patch.dict(os.environ, {"WS_ACTIVE_MIN_GAP": "0"}, clear=False):
        s3 = _push_with("🔥 裂缝（换了一批）")
    total += 1
    ok += check("内容变化后重新推", s3 == 1 and len(pushed) == 2)
    s4 = _push_with("🔥 裂缝（又变了）")
    total += 1
    ok += check("变化太频繁被最小间隔挡住", s4 == 0 and len(pushed) == 2)

    # ---------- 11) GROUP/C2C_MSG_RECEIVE：开关一开立刻补推 ----------
    print("[通知开关开启立刻解除退避]")
    M.clear_pending()
    M._monitors.clear()
    M._monitors[KA] = {"fissures"}
    pushed2 = []
    M.set_sender(lambda ch, tid, text: pushed2.append(text) or True)
    M._next_probe = time.time() + 999          # 模拟主动通道正在退避
    with patch.object(ws, "ensure_fresh", lambda: {}), \
         patch.object(ws, "format_for", lambda t: "🔥 裂缝"), \
         patch.object(ws, "activity_touch", lambda: None):
        n = M.notify_active_ready("group", "G1")
    total += 1
    ok += check("退避中收到开关事件也立刻补推", n == 1 and len(pushed2) == 1)
    total += 1
    ok += check("退避被解除", M._next_probe <= time.time())
    total += 1
    ok += check("补推成功后暂存被清空", M.pending_count() == 0)

    # ---------- 12) 任意群消息都能捎带监控（全量消息模式） ----------
    print("[闲聊也能捎带监控]")
    M.clear_pending()
    M._monitors.clear()
    M._pending[KA] = {"text": "🛡️ 仲裁：赛德娜", "at": time.time(),
                      "fails": 1, "types": set(),
                      "parts": [("arbitration", "🛡️ 仲裁：赛德娜")]}
    H2 = q.make_monitor_aware_handler(q.make_handler(allow_all=True))
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        r_idle = H2("随便闲聊一句", {"channel": "group", "group_id": "G1"})
    total += 1
    ok += check("闲聊也带出监控内容",
                isinstance(r_idle, str) and "📬 监控推送" in r_idle
                and "赛德娜" in r_idle)
    with patch.dict(os.environ, {"WS_ATTACH_GAP": "0"}, clear=False):
        r_other = H2("随便闲聊一句", {"channel": "group", "group_id": "G2"})
    total += 1
    ok += check("别的会话不会串到内容", r_other is None or "赛德娜" not in str(r_other))
    M.clear_pending()
    M._monitors.clear()

    print("\n结果：通过 %d / 失败 %d" % (ok, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    M.clear_pending()
    M._monitors.clear()
    code = main()
    M._monitors.clear()
    M.clear_pending()
    sys.exit(code)
