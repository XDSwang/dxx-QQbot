#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""监控会话隔离测试（不联网、不碰真实 data/）

覆盖：
  - 解析：停止监控（自己）/ 停止全部监控（本群所有人）/ 停止监控 警报（单栏目）
  - 群隔离：A 群清理不影响 B 群
  - 成员隔离：同群一人停止只撤自己的栏目，别人的照常在
  - 停止全部：只清当前会话，别的群与私聊原样保留
  - 落盘：ws_subs.json 按会话存，重启能恢复
  - 推送 @ 人：发起人走了换下一个，没人了就清掉
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="scope_")
os.environ["WM_SOURCE"] = "mock"
os.environ["WS_PRIORITY_TYPES"] = ""
os.environ["WS_AUTO_PRIORITY"] = "0"
os.environ["WS_SWITCH_FILE"] = os.path.join(TMP, "ws_switches.json")
os.environ["WS_FILTER_FILE"] = os.path.join(TMP, "ws_filters.json")
os.environ["WS_AT_FILE"] = os.path.join(TMP, "ws_at_users.json")
os.environ["WS_SUB_FILE"] = os.path.join(TMP, "ws_subs.json")
os.environ["WS_PUSH_INTERVAL"] = "0"   # 测试不真睡

import wm
import worldstate as ws
import ws_monitor as M
import qq_common

wm.CACHE_FILE = os.path.join(TMP, "cache.json")
ws.WS_CACHE_FILE = Path(TMP) / "worldstate.json"
M.LAST_SENT_FILE = Path(TMP) / "ws_last_sent.json"

ok = total = 0


def t(name, cond):
    global ok, total
    total += 1
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if cond:
        ok += 1


def on(fn, *a, **kw):
    """调带副作用（拉后台刷新 / 起推送线程）的函数：测试里全拦掉。"""
    with patch.object(ws, "ensure_active", lambda: None), \
            patch.object(M, "_ensure_push_thread", lambda: None):
        return fn(*a, **kw)


def reset():
    M.clear_pending()
    M._monitors.clear()
    M._subs.clear()
    M._at_users.clear()
    M._fissure_filters.clear()
    M.reset_switches()


def main():
    print("== 1) 指令解析 ==")
    c = ws.parse_ws_command("停止监控")
    t("停止监控 = 只停自己", c.get("action") == "stop" and c.get("scope") == "self")
    for word in ("停止全部监控", "取消全部监控", "停止所有监控",
                 "停止监控 全部", "停止全部"):
        c = ws.parse_ws_command(word)
        t("「%s」= 停本群所有人" % word,
          c.get("action") == "stop" and c.get("scope") == "all")
    c = ws.parse_ws_command("停止监控 警报")
    t("「停止监控 警报」只停该栏目",
      c.get("action") == "stop" and c.get("scope") == "self"
      and c.get("types") == ["alerts"])
    c = ws.parse_ws_command("停止全部监控 警报")
    t("「停止全部监控 警报」= 全群该栏目",
      c.get("action") == "stop" and c.get("scope") == "all"
      and c.get("types") == ["alerts"])
    t("不认识的栏目给提示、什么都不清",
      ws.parse_ws_command("停止监控 不认识的词").get("action") == "stop_hint")
    c = ws.parse_ws_command("停止 裂缝后纪中断")
    t("停止后接裂缝条件也能只停该栏目",
      c.get("types") == ["fissures"] and c.get("scope") == "self")

    print("== 2) 群与群之间互不影响 ==")
    reset()
    on(M.start_monitor, "group", "G1", {"alerts"}, at_user="u1")
    on(M.start_monitor, "group", "G2", {"fissures"}, at_user="u2")
    t("两个群各自注册", set(M._monitors) == {("group", "G1"), ("group", "G2")})
    t("G1 停下返回 True", on(M.stop_monitor, "group", "G1", user="u1") is True)
    t("G1 已清空", ("group", "G1") not in M._monitors)
    t("G2 原样保留", M._monitors.get(("group", "G2")) == {"fissures"})
    t("G2 的订阅者没被带走",
      M.subscribers_of("group", "G2") == {"fissures": {"u2"}})

    print("== 3) 同一个群里的成员互相隔离 ==")
    reset()
    on(M.start_monitor, "group", "G9", {"alerts"}, at_user="u1")
    on(M.start_monitor, "group", "G9", {"fissures"}, at_user="u2")
    t("同群两个栏目并存（后开的不覆盖先开的）",
      M._monitors.get(("group", "G9")) == {"alerts", "fissures"})
    t("u1 名下只有警报", M.my_types("group", "G9", "u1") == ["alerts"])
    on(M.stop_monitor, "group", "G9", user="u1")
    t("u1 停止只撤自己的警报", M._monitors.get(("group", "G9")) == {"fissures"})
    t("u2 的裂缝还在", M.subscribers_of("group", "G9") == {"fissures": {"u2"}})
    t("u1 再停一次返回 False（本来就没有）",
      on(M.stop_monitor, "group", "G9", user="u1") is False)

    print("== 4) 停止全部只清当前会话 ==")
    reset()
    on(M.start_monitor, "group", "G1", {"alerts"}, at_user="u1")
    on(M.start_monitor, "group", "G1", {"fissures"}, at_user="u2")
    on(M.start_monitor, "group", "G2", {"alerts"}, at_user="u3")
    on(M.start_monitor, "private", "P1", {"events"}, at_user="u4")
    on(M.set_switch, "group", "G1", ["alerts"], True)
    t("G1 有存盘开关", ("group", "G1") in M._switches)
    t("停止全部返回 True", on(M.stop_monitor, "group", "G1", everyone=True) is True)
    t("G1 监控全清", ("group", "G1") not in M._monitors)
    t("G1 订阅全清", M.subscribers_of("group", "G1") == {})
    t("G1 开关也清了", ("group", "G1") not in M._switches)
    t("G2 没被动", M._monitors.get(("group", "G2")) == {"alerts"})
    t("私聊没被动", M._monitors.get(("private", "P1")) == {"events"})

    print("== 5) 只停指定栏目 ==")
    reset()
    on(M.start_monitor, "group", "G5", {"alerts", "fissures"}, at_user="u1")
    on(M.stop_monitor, "group", "G5", types=["alerts"], user="u1")
    t("警报停了、裂缝还在", M._monitors.get(("group", "G5")) == {"fissures"})
    on(M.stop_monitor, "group", "G5", types=["fissures"], user="u1")
    t("最后一个栏目也停了，会话键消失", ("group", "G5") not in M._monitors)

    print("== 6) 会话级订阅不被个人停止误清 ==")
    reset()
    on(M.set_switch, "group", "G8", ["alerts"], True)   # 开关没写发起人 = 会话级
    t("开关注册成会话级订阅", M.subscribers_of("group", "G8") == {"alerts": {""}})
    t("会话级订阅不 @ 任何人（没有具体发起人）",
      M.at_users_of("group", "G8") == [])
    t("别人来停自己：返回 False",
      on(M.stop_monitor, "group", "G8", user="u9") is False)
    t("会话级监控还在", M._monitors.get(("group", "G8")) == {"alerts"})
    on(M.stop_monitor, "group", "G8", everyone=True)
    t("停止全部能清掉会话级", ("group", "G8") not in M._monitors)

    print("== 7) 私聊 ==")
    reset()
    on(M.start_monitor, "private", "P9", {"alerts"}, at_user="u1")
    on(M.stop_monitor, "private", "P9")          # 私聊没有成员维度，整会话清
    t("私聊整会话清理", ("private", "P9") not in M._monitors)

    print("== 8) 落盘与重启恢复 ==")
    reset()
    on(M.start_monitor, "group", "GA", {"alerts"}, at_user="u1")
    on(M.start_monitor, "group", "GB", {"fissures"}, at_user="u2")
    on(M.stop_monitor, "group", "GA", user="u1")
    saved = json.loads(Path(os.environ["WS_SUB_FILE"]).read_text(encoding="utf-8"))
    t("盘上只剩 GB", list((saved or {}).get("sessions") or {}) == ["group|GB"])
    M._subs.clear()
    M._monitors.clear()
    t("load_subs 恢复 1 个会话", M.load_subs() == 1)
    t("恢复后订阅正确", M.subscribers_of("group", "GB") == {"fissures": {"u2"}})
    t("恢复后监控也补回来了", M._monitors.get(("group", "GB")) == {"fissures"})

    # 重启后【必须自动续跑】：以前 load_subs 只恢复注册表、不拉线程，而
    # ws_switches.json 常常是空的，于是重启后「有监控却永远不推、数据也不刷新」。
    M._subs.clear()
    M._monitors.clear()
    started = []
    with patch.object(M.ws, "ensure_active",
                      lambda *a, **k: started.append("active")), \
         patch.object(M, "_ensure_push_thread",
                      lambda *a, **k: started.append("thread")):
        M.load_subs()
    t("恢复出监控时自动拉起刷新 + 推送线程",
      started == ["active", "thread"])

    # 盘上没订阅时不该无脑起线程（白跑后台刷新、白占资源）
    Path(os.environ["WS_SUB_FILE"]).write_text('{"version":1,"sessions":{}}',
                                              encoding="utf-8")
    M._subs.clear()
    M._monitors.clear()
    started2 = []
    with patch.object(M.ws, "ensure_active",
                      lambda *a, **k: started2.append("active")), \
         patch.object(M, "_ensure_push_thread",
                      lambda *a, **k: started2.append("thread")):
        M.load_subs()
    t("没有监控时不空转线程", started2 == [])

    print("== 9) 推送 @ 人 ==")
    reset()
    on(M.start_monitor, "group", "G7", {"alerts"}, at_user="u1")
    on(M.start_monitor, "group", "G7", {"fissures"}, at_user="u2")
    t("推送一次 @ 全部开监控的人", M.at_users_of("group", "G7") == ["u1", "u2"])
    t("最近发起人是 u2", M.at_user_of("group", "G7") == "u2")
    on(M.stop_monitor, "group", "G7", user="u2")
    t("u2 走了只剩 @ u1", M.at_users_of("group", "G7") == ["u1"])
    t("发起人记录改成 u1", M.at_user_of("group", "G7") == "u1")
    on(M.stop_monitor, "group", "G7", user="u1")
    t("没人了就清掉 @ 记录", M.at_user_of("group", "G7") is None)
    t("没人了也不 @ 任何人", M.at_users_of("group", "G7") == [])

    print("== 10) 指令入口按发言人隔离 ==")
    reset()
    ctx1 = {"channel": "group", "group_id": "GC", "user_id": "1001", "at_me": True}
    ctx2 = {"channel": "group", "group_id": "GC", "user_id": "1002", "at_me": True}
    h = qq_common.make_handler([], allow_all=True)
    plain = lambda txt, *a, **k: txt
    with patch.object(qq_common, "finalize", plain), \
            patch.object(ws, "ensure_active", lambda: None), \
            patch.object(M, "_ensure_push_thread", lambda: None):
        h("监控 警报", ctx1)
        h("监控 裂缝", ctx2)
        t("两人各开一栏", M._monitors.get(("group", "GC")) == {"alerts", "fissures"})
        h("停止监控", ctx1)
    t("1001 停止后只剩 1002 的裂缝",
      M._monitors.get(("group", "GC")) == {"fissures"})

    print("== 11) 推送按栏目分别 @ 人 ==")
    reset()
    M._next_probe = 0.0
    on(M.start_monitor, "group", "G9", {"alerts"}, at_user="u1")
    on(M.start_monitor, "group", "G9", {"alerts"}, at_user="u3")
    on(M.start_monitor, "group", "G9", {"fissures"}, at_user="u2")
    t("同一栏目多人订阅 -> 一起 @", M.at_users_for("group", "G9", "alerts") == ["u1", "u3"])
    t("另一栏目只 @ 自己要的人", M.at_users_for("group", "G9", "fissures") == ["u2"])
    t("没人订阅的栏目不 @ 人", M.at_users_for("group", "G9", "sortie") == [])
    on(M.set_switch, "group", "G9", {"sortie"}, True)
    t("会话级开关（全群）不 @ 具体人",
      M.at_users_for("group", "G9", "sortie") == [])

    sent = []

    def _sender_at(ch, tid, text, at_user=None):
        sent.append((text.splitlines()[0], at_user))
        return True

    M.set_sender(_sender_at)
    with patch.object(M.ws, "ensure_fresh", lambda: {}), \
            patch.object(M.ws, "activity_touch", lambda: None), \
            patch.object(M, "query_busy", lambda: False), \
            patch.object(M, "_parts_of",
                         lambda st, types, key=None: [
                             ("alerts", "📢 警报（共 1）\n  · 新警报"),
                             ("fissures", "🌌 裂缝\n  · 地球 灭绝")]):
        M.push_once()
    t("一轮两条消息，各 @ 各的订阅者",
      sent == [("📢 警报（共 1）", ["u1", "u3"]), ("🌌 裂缝", ["u2"])])

    print("\n%d/%d 通过" % (ok, total))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
