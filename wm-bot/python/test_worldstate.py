#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_worldstate.py — 世界状态模块离线回归（零依赖，全部走 mock 源，不联网）

覆盖：
  - 用户查询指令：裂缝/钢铁裂缝/商人/平原/突击/仲裁/电波/入侵/特惠/信使/集团/建造
  - 【一次全量请求】不再按 16 个端点一个个查，一次请求就拿到全部数据
  - 【排序队列】递归扫描每一条数据的结束时刻：裂缝/钢铁裂缝/警报列表里每条单独排
  - 【队首 = 最快结束】主任务睡到队首，到点只查一次全量，没到点零请求
  - 【2/3/5 分钟节拍】队列为空 / 极短到期点兜底，且节拍计数落盘循环
  - 同域名 3~5 秒底线（mock 也走，中文回退那次也走；主任务到点那次也不漏）
  - 缓存优先：缓存里已有新数据就不联网，找不到才由主任务按排序队列查一次
  - 【单飞防重复查询】多个任务同时进来只执行最快到期的一个；一次全量写回全部
    数据后，其余任务开锁先查缓存即被满足，不各自再发请求
  - 【特殊任务（通用队列）】数据不在全量请求里（另一个域名/字段）的任务带 fn
    提交：主任务只按最快到期排序执行、不看内容；不被世界状态缓存「顺带满足」；
    比队首更早到期会提前唤醒主任务；失败按 10/30/60 退避重排
  - 商人未到访（14 天后抵达）、ISO 8601 / timeLeft 各种写法
  - 旧版按端点 parts 存的缓存就地迁移重排（不联网）
  - 查询 / 平原 / 计划 / 监控 / 触发词边界
运行：python test_worldstate.py
"""
import os
import sys
import time
import shutil
import tempfile
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

# 必须在 import worldstate 之前确定走 mock 源（模块在导入时读取该环境变量）
os.environ["WS_SOURCE"] = "mock"

# 会话类落盘文件一律指到临时目录：测试绝不能覆盖本机正在跑的监控设置
_WS_TMP = tempfile.mkdtemp(prefix="ws-files-")
for _k, _n in (("WS_SWITCH_FILE", "ws_switches.json"),
               ("WS_FILTER_FILE", "ws_filters.json"),
               ("WS_AT_FILE", "ws_at_users.json"),
               ("WS_SUB_FILE", "ws_subs.json")):
    os.environ[_k] = str(Path(_WS_TMP) / _n)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import worldstate as ws
import ws_monitor
import qq_common
import wm

ws_monitor.LAST_SENT_FILE = Path(_WS_TMP) / "ws_last_sent.json"


PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


@contextmanager
def isolated():
    """临时缓存 + 跳过节流等待 + 不启动后台线程，保证离线测试秒级完成。"""
    tmp = Path(tempfile.mkdtemp(prefix="ws-test-"))
    saved = {k: getattr(ws, k) for k in ("_next_allowed", "_retry_due", "_failures")}
    saved_pending = list(ws._pending)
    ws._next_allowed = 0.0
    ws._retry_due = 0.0
    ws._failures = 0
    ws._pending[:] = []
    try:
        with patch.object(ws, "WS_CACHE_FILE", tmp / "worldstate.json"), \
             patch.object(wm, "pace_gate_ex", lambda *a, **k: None), \
             patch.object(ws, "ensure_active", lambda: None):
            yield tmp
    finally:
        for k, v in saved.items():
            setattr(ws, k, v)
        ws._pending[:] = saved_pending
        shutil.rmtree(tmp, ignore_errors=True)


def counted_fetch():
    """返回一个 (计数列表, 包装后的 fetch)，用于断言请求次数。"""
    calls = []
    real = ws.fetch_worldstate

    def counted():
        calls.append(1)
        return real()

    return calls, counted


# ----------------------------- 1. 用户查询指令 -----------------------------
def test_parse():
    print("[用户查询指令]")
    for kw, key in [("裂缝", "fissures"), ("钢铁裂缝", "steel"), ("商人", "voidtrader"),
                    ("虚空商人", "voidtrader"), ("平原", "cycles"), ("突击", "sortie"),
                    ("仲裁", "arbitration"), ("电波", "nightwave"), ("入侵", "invasions"),
                    ("特惠", "daily"), ("信使", "outpost"), ("集团", "syndicate"),
                    ("建造", "construction"), ("警报", "alerts"), ("活动", "events")]:
        c = ws.parse_ws_command(kw)
        check(f"解析 {kw} -> {key}", c.get("action") == "query" and key in c.get("types", []))
    check("解析 全部 -> 所有类型",
          len(ws.parse_ws_command("全部")["types"]) == len(ws.TYPES))
    check("平原就是全部平原（显示组合，不拆指令）",
          ws.parse_ws_command("平原")["types"] == ["cycles"])
    c = ws.parse_ws_command("监控 警报 裂缝")
    check("解析 监控 警报 裂缝", c["action"] == "monitor"
          and set(c["types"]) == {"alerts", "fissures"})
    check("解析 停止监控", ws.parse_ws_command("停止监控")["action"] == "stop")
    check("解析 计划", ws.parse_ws_command("计划")["action"] == "plan")
    # 监控安全化：裸「监控」/「监控 不认识的词」不再静默全开，给提示；只有「监控全部」全量
    check("裸 监控 -> 安全提示(monitor_hint)",
          ws.parse_ws_command("监控")["action"] == "monitor_hint")
    check("监控 乱码 -> 安全提示(monitor_hint)",
          ws.parse_ws_command("监控 不认识的东西")["action"] == "monitor_hint")
    c_all = ws.parse_ws_command("监控全部")
    check("监控全部 -> 全量监控", c_all["action"] == "monitor"
          and len(c_all["types"]) == len(ws.TYPES))
    check("monitor(英文)裸 -> 安全提示",
          ws.parse_ws_command("monitor")["action"] == "monitor_hint")
    check("闲聊不触发", not qq_common.is_ws_command("今天天气不错")
          and not qq_common.is_ws_command("wm boltor"))
    check("12 个查询指令全部能被触发词识别",
          all(qq_common.is_ws_command(k) for k in
              ("裂缝", "钢铁裂缝", "商人", "平原", "突击", "仲裁",
               "电波", "入侵", "特惠", "信使", "集团", "建造")))


# ----------------------------- 2. 一次全量请求 -----------------------------
def test_one_request():
    print("[一次全量请求：不再按端点一个个查]")
    with isolated():
        calls, counted = counted_fetch()
        with patch.object(ws, "fetch_worldstate", counted):
            ws.refresh_once()
        check("刷新只发【一次】全量请求（不是 16 个端点）", len(calls) == 1)
        state = ws.get_state()
        check("一次请求就拿到裂缝 / 钢铁裂缝 / 商人 / 三处平原 / 警报",
              all(state.get(k) for k in ("fissures", "steelPath", "voidTrader",
                                         "cetusCycle", "vallisCycle",
                                         "cambionCycle", "alerts")))
        check("已删除按端点取数的 fetch_unit / UNITS",
              not hasattr(ws, "fetch_unit") and not hasattr(ws, "UNITS"))
        check("全量接口是 /pc/zh 或退回 /pc（源码含 WS_API_BASE 拼接）",
              "WS_API_BASE" in open(os.path.join(HERE, "worldstate.py"),
                                    encoding="utf-8").read())


# ----------------------------- 3. 排序队列精确到每条 -----------------------------
def test_queue_per_entry():
    print("[排序队列：裂缝/钢铁裂缝/警报列表里每条各有自己的结束时刻]")
    with isolated():
        ws.refresh_once()
        q = ws.get_queue()
        labels = [x["label"] for x in q]
        check("裂缝两条各自排进队列", labels.count("裂缝") == 2)
        check("钢铁裂缝两条各自排进队列", labels.count("钢铁裂缝") == 2)
        check("警报两条各自排进队列", labels.count("警报") == 2)
        check("同一节点 expiry + timeLeft 不重复计数（希图斯只 1 个）",
              labels.count("希图斯") == 1)
        check("队列按结束时刻升序",
              all(q[i]["due"] <= q[i + 1]["due"] for i in range(len(q) - 1)))
        check("队首 = 最快结束的那条（魔胎之境 8 分钟）",
              q[0]["label"] == "魔胎之境")
        check("到期点数量 >= 12（不是按 16 个端点压成 16 个）", len(q) >= 12)
        keys = [(round(x["due"], 0), x["label"]) for x in q]
        check("同一 (结束时刻, 数据) 不重复排队", len(keys) == len(set(keys)))
        check("队列保留全部到期点（含十几小时后的商人/突击）",
              any(x["label"] == "虚空商人" for x in q)
              and any(x["label"] == "突击" for x in q))


# ----------------------------- 4. 主任务：到点才查，零反复查询 -----------------------------
def test_no_repeat():
    print("[主任务：睡到最快结束那条，没到点零请求]")
    with isolated():
        ws.refresh_once()
        c = ws.get_cache()
        head = c["queue"][0]["due"]
        check("next_due = 队首结束时刻（不是定时循环）", abs(ws.next_due() - head) < 2)
        check("当前未到期", not ws.is_due())

        calls, counted = counted_fetch()
        with patch.object(ws, "fetch_worldstate", counted):
            for _ in range(20):
                ws.handle_query({"action": "query", "types": ["fissures"]})
            ws.handle_query({"action": "query", "types": ["voidtrader"]})
            ws.handle_query({"action": "plan"})
        check("连续 22 次查询 / 计划 全部零请求（纯读缓存）", len(calls) == 0)

        # 模拟队首到期：先仍受 10 秒节拍保护，节拍过后才真的可以查
        c = ws.get_cache()
        c["queue"][0]["due"] = time.time() - 1
        wm.save_json(ws.WS_CACHE_FILE, c)
        check("队首刚到期但节拍未过 → 不立即再查（10s 兜底）", not ws.is_due())
        ws._next_allowed = 0.0                  # 模拟节拍时间已过
        check("节拍过后 is_due 为真", ws.is_due())
        with patch.object(ws, "fetch_worldstate", counted):
            ws._run_once()
        check("到点只发【一次】全量请求", len(calls) == 1)
        check("查完立刻重排，新队首仍是未来（不会反复查询）",
              ws.get_queue() and ws.get_queue()[0]["due"] > time.time())


# ----------------------------- 5. 2/3/5 分钟节拍 -----------------------------
def test_step_gaps():
    print("[2/3/5 分钟节拍：队列为空 / 极短到期点兜底]")
    with isolated():
        gaps = []
        for _ in range(4):
            ws.refresh_once()
            c = ws.get_cache()
            gaps.append(round(ws._next_allowed - c["fetched_at"]))
        check("节拍 120 -> 180 -> 300 -> 120 循环且落盘",
              gaps == [120, 180, 300, 120])
        check("build_queue 无到期信息时返回空", ws.build_queue({}, time.time()) == [])
        # 队列空时不连打：next_due 被节拍推到未来
        wm.save_json(ws.WS_CACHE_FILE, {
            "schema": ws.WS_CACHE_SCHEMA,
            "fetched_at": time.time(), "data": {"alerts": []}, "queue": [], "step": 0})
        ws._next_allowed = time.time() + 10
        check("队列为空时按节拍兜底，不立即再查", ws.next_due() > time.time())
        check("is_due 为假", not ws.is_due())


# ----------------------------- 6. 3~5 秒底线 -----------------------------
def test_pace():
    print("[同域名 3~5 秒硬规则]")
    seen = []
    with patch.object(wm, "pace_gate_ex", lambda d, lo, hi: seen.append((d, lo, hi))):
        ws.fetch_worldstate()                 # mock 源也必须过节流
    check("mock 源同样走同域名节流", len(seen) == 1)
    check("底线锁死 3~5 秒", seen[0][1] == 3.0 and seen[0][2] == 5.0)
    check("域名与 WM 独立", seen[0][0] == "api.warframestat.us")

    seen2 = []

    def fake_http(url):
        if "/pc/zh" in url:
            raise RuntimeError("中文端点不支持")
        return {"ok": 1}

    with patch.object(ws, "WS_SOURCE", "warframe"), \
         patch.object(ws, "_http_json", fake_http), \
         patch.object(wm, "pace_gate_ex", lambda *a, **k: seen2.append(1)):
        d = ws.fetch_worldstate()
    check("中文失败自动退回英文端点", d == {"ok": 1})
    check("回退那次也单独过一次节流（绝不连打）", len(seen2) == 2)


# ----------------------------- 6b. 主任务到点的节流 + 缓存优先 -----------------------------
def test_pace_main_task_and_cache_first():
    print("[主任务到点：3~5 秒节流一次不漏 + 缓存优先不重复查]")
    with isolated():
        calls, pace, pace_done = [], [], []
        real_fetch = ws.fetch_worldstate

        def counted():
            calls.append(1)
            return real_fetch()

        def fake_pace(d, lo, hi):
            pace.append((d, lo, hi))
            time.sleep(0.02)                  # 模拟节流真的耗时
            pace_done.append(time.time())

        with patch.object(wm, "pace_gate_ex", fake_pace), \
             patch.object(ws, "fetch_worldstate", counted):
            # ① 缓存为空：主任务首次建缓存，那一次请求必须过 3~5 秒节流
            ws._run_once()
            check("首次建缓存只发 1 次全量请求", len(calls) == 1)
            check("首次请求前确实调用了同域名 3~5 秒节流",
                  len(pace) == 1 and pace[0] == ("api.warframestat.us", 3.0, 5.0))
            c = ws.get_cache()
            check("缓存已建立：data / fetched_at / 排序队列 / step 齐全",
                  bool(c.get("data")) and bool(c.get("fetched_at"))
                  and isinstance(c.get("queue"), list) and "step" in c)
            check("fetched_at 取在节流之后（不会把到期点算早 3~5 秒）",
                  c["fetched_at"] >= pace_done[-1])

            # ② 缓存里已是新数据（队首还在未来）：再触发也不联网
            ws._run_once()
            check("缓存里已有新数据 → 不重复联网（缓存优先）", len(calls) == 1)

            # ③ 队首到期 + 节拍过后：才发第二次，且再次过节流
            c = ws.get_cache()
            c["queue"][0]["due"] = time.time() - 1
            wm.save_json(ws.WS_CACHE_FILE, c)
            ws._next_allowed = 0.0
            ws._run_once()
            check("队首到期且节拍过后，再发 1 次全量请求", len(calls) == 2)
            check("第二次请求同样过了 3~5 秒节流（一次都不漏）",
                  len(pace) == 2 and pace[1] == ("api.warframestat.us", 3.0, 5.0))
            check("刷新后队列重新排好，队首仍在未来",
                  bool(ws.get_queue()) and ws.get_queue()[0]["due"] > time.time())


# ----------------------------- 6c. 单飞 + 任务合并（防重复查询） -----------------------------
def test_task_single_flight():
    print("[主任务单飞：多个任务同时进来只查一次，其余开锁后先查缓存]")
    with isolated():
        now = time.time()
        # ① 缓存为空：模拟「全部」指令的十几个类型同时发现找不到数据
        results = [ws.submit_task(now + i * 60, "类型%d" % i) for i in range(1, 6)]
        check("缓存为空时 5 个任务都进「下一个抢主任务队列」",
              all(r == "queued" for r in results))
        check("队列按最快到期排序，队首是最早结束的那个",
              ws.pending_count() == 5 and ws._take_fastest()["label"] == "类型1")

        # ② 本轮只执行【一个】= 只发一次全量请求；一次全量写回全部数据
        calls, counted = counted_fetch()
        with patch.object(ws, "fetch_worldstate", counted):
            ws._run_once()
        check("5 个任务只执行一个 = 只发【一次】全量请求", len(calls) == 1)
        check("缓存里所有字段全都有了（其它任务都能找到数据）",
              len(ws.get_state() or {}) == len(ws._MOCK_WS))
        check("其余任务开锁后到缓存找数据即被满足，全部移除、不再排队",
              ws.pending_count() == 0)

        # ③ 再来一轮仍零请求，绝不反复查询
        with patch.object(ws, "fetch_worldstate", counted):
            ws._run_once()
        check("第二轮仍然零请求", len(calls) == 1)
        check("缓存已是新的 -> 再提交任务直接读缓存、不进队",
              ws.submit_task(now + 300, "类型9") == "cached")

        # ④ 已有任务持锁：后来者不抢锁、不自己发请求，只等开锁后读缓存
        c = ws.get_cache()
        c["queue"][0]["due"] = now - 1
        wm.save_json(ws.WS_CACHE_FILE, c)
        ws._next_allowed = 0.0
        ws._task_lock.acquire()
        try:
            with patch.object(ws, "LOCK_WAIT", 0), \
                 patch.object(ws, "fetch_worldstate", counted):
                ws._run_once()
            check("主任务锁被占用时后来者零请求（等开锁后读缓存）", len(calls) == 1)
        finally:
            ws._task_lock.release()

        # ⑤ 同一到期点的重复任务合并，不重复排队
        wm.save_json(ws.WS_CACHE_FILE, {})
        ws._pending[:] = []
        ws._next_allowed = 0.0
        check("同一 (到期时刻, 数据) 重复提交合并成一个任务",
              ws.submit_task(now + 600, "裂缝") == "queued"
              and ws.submit_task(now + 600, "裂缝") == "merged"
              and ws.pending_count() == 1)


# ----------------------------- 6d. 特殊任务（数据不在全量请求里） -----------------------------
def test_special_tasks():
    print("[特殊任务：主任务只排序执行，不需要知道任务是什么]")
    with isolated():
        # ① 世界状态缓存是新的，特殊任务照样执行（它的数据不在全量请求里）
        ws.refresh_once()
        calls, counted = counted_fetch()
        got = []

        def special_fn():
            got.append(time.time())
            return {"自定义域名": True}   # 模拟另一个接口/字段的数据

        r = ws.submit_task(time.time(), "另一个域名的数据", special_fn)
        check("特殊任务不被世界状态缓存挡住（不返回 cached）", r == "queued")
        with patch.object(ws, "fetch_worldstate", counted):
            ws._run_once()
        check("世界状态缓存是新的也照样执行特殊任务（零全量请求）",
              len(got) == 1 and len(calls) == 0)
        check("执行后任务出队", ws.pending_count() == 0)

        # ② 世界状态的全量刷新不会「顺带满足」特殊任务（版本号变了也不移除）
        def special2():
            got.append(2)
        ws.submit_task(time.time() + 5, "特殊B", special2)
        c = ws.get_cache()
        c["queue"][0]["due"] = time.time() - 1      # 队首到期：世界状态先刷新
        wm.save_json(ws.WS_CACHE_FILE, c)
        ws._next_allowed = 0.0
        with patch.object(ws, "fetch_worldstate", counted):
            ws._run_once()                          # 最快到期的是队首（世界状态）
        check("全量刷新后特殊任务仍在队列里（不被世界状态缓存满足）",
              ws.pending_count() == 1)
        check("未到期的特殊任务不提前执行（2 还没被调用）", 2 not in got)
        with ws._pending_lock:                       # 模拟特殊任务自己的到期时刻到了
            ws._pending[0]["due"] = time.time()
        ws._next_allowed = 0.0
        with patch.object(ws, "fetch_worldstate", counted):
            ws._run_once()                          # 下一轮才执行特殊任务
        check("下一轮执行特殊任务（本轮零全量请求）", 2 in got and len(calls) == 1)

        # ③ next_due 包含最快到期的特殊任务：主任务会为它提前醒来
        ws._pending[:] = []
        ws._next_allowed = 0.0
        ws.refresh_once()
        head = ws.get_cache()["queue"][0]["due"]
        ws.submit_task(head - 60, "更早的特殊任务", lambda: None)
        check("特殊任务比队首更早到期 -> next_due 跟着它提前",
              abs(ws.next_due() - (head - 60)) < 2)
        check("next_due 早于世界状态自己的允许时刻（主任务为特殊任务提前醒）",
              ws.next_due() < ws._ws_allowed())

        # ④ 失败退避重新排队：不炸主任务、不挡别的任务
        boom = [0]

        def failing():
            boom[0] += 1
            raise RuntimeError("那个域名接口挂了")

        ws._pending[:] = []
        ws.submit_task(time.time(), "会失败的特殊任务", failing)
        try:
            ws._run_once()
            ok = True
        except Exception:
            ok = False
        check("特殊任务失败不炸主任务（异常被吞掉）", ok)
        check("失败后按退避重新排队", ws.pending_count() == 1
              and boom[0] == 1)
        t = ws._take_fastest()
        check("退避计数记在任务上（下次 10/30/60）", t["fails"] == 1)
        check("重排后的执行时刻被退避推到未来（不连打）",
              t["due"] > time.time() + 5)

        # ⑤ 两种任务并存：仍一次只执行一个，按最快到期排序
        #    （先把世界状态队首改成已到期，再提交——真实流程里任务都是到期才提交的）
        ws._pending[:] = []
        order = []
        c = ws.get_cache()
        c["queue"][0]["due"] = time.time() - 1
        wm.save_json(ws.WS_CACHE_FILE, c)
        ws._next_allowed = 0.0
        r1 = ws.submit_task(time.time() + 0.01, "晚一点的特殊",
                            lambda: order.append("special"))
        r2 = ws.submit_task(time.time() - 1, "世界状态", None)
        check("两种任务都进队（世界状态已到期，不再判 cached）",
              r1 == "queued" and r2 == "queued")
        with patch.object(ws, "fetch_worldstate", counted):
            ws._run_once()
        check("两种任务并存：本轮只执行最快到期的那个（世界状态先）",
              order == [] and len(calls) == 2)
        check("特殊任务保留等自己的轮次（不被世界状态刷新满足）",
              ws.pending_count() == 1)
        with ws._pending_lock:
            ws._pending[0]["due"] = time.time()
        with patch.object(ws, "fetch_worldstate", counted):
            ws._run_once()
        check("下一轮才轮到特殊任务", order == ["special"] and len(calls) == 2)


# ----------------------------- 7. 结束时刻解析 / 商人 -----------------------------
def test_expiry_parsing():
    print("[结束时刻解析 / 商人下次抵达]")
    t = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t + 14 * 86400))
    q = ws.build_queue({"voidTrader": {"active": False, "activation": iso}}, t, t)
    check("ISO 8601 (...Z) 且商人未到访 → 到期点约 14 天后",
          any(abs(x["due"] - (t + 14 * 86400)) < 3 for x in q))
    for text, sec in (("0h 52m 12s", 3132), ("3d 4h 5m 6s", 273906),
                      ("01:23:45", 5025), ("00:42:10", 2530)):
        q2 = ws.build_queue({"cetusCycle": {"timeLeft": text}}, t, t)
        check(f"timeLeft {text} → {sec}s 后",
              any(abs(x["due"] - (t + sec)) < 3 for x in q2))
    # 同一节点同时给绝对与相对：只算一次
    q3 = ws.build_queue({"cetusCycle": {"expiry": int(t + 5025),
                                         "timeLeft": "01:23:45"}}, t, t)
    check("expiry + timeLeft 同时存在只排一个到期点", len(q3) == 1)
    check("列表里每条都单独排",
          len(ws.build_queue({"fissures": [{"expiry": t + 100},
                                           {"expiry": t + 200}]}, t, t)) == 2)


# ----------------------------- 8. 旧缓存迁移 -----------------------------
def test_migrate():
    print("[旧版按端点 parts 缓存就地重排 / 结构过旧则作废]")
    with isolated():
        old = {
            "schema": ws.WS_CACHE_SCHEMA,
            "parts": {"fissures": {"raw": [{"expiry": int(time.time() + 3600)}]}},
            "data": {"fissures": [{"expiry": int(time.time() + 3600)}]},
            "ts": int(time.time() * 1000),
        }
        wm.save_json(ws.WS_CACHE_FILE, old)
        calls, counted = counted_fetch()
        with patch.object(ws, "fetch_worldstate", counted):
            c = ws.get_cache()
        check("迁移零请求", len(calls) == 0)
        check("自动补出排序队列", isinstance(c.get("queue"), list) and len(c["queue"]) >= 1)
        check("清掉旧的 parts 结构", "parts" not in c)
        check("原有数据仍在（秒回可用）", bool(ws.get_state().get("fissures")))

    # 结构过旧（缺仲裁等栏目）的缓存不能拿来糊弄查询：作废，让主任务重抓一次
    with isolated():
        stale = {"data": {"fissures": [{"expiry": int(time.time() + 3600)}]},
                 "fetched_at": time.time(), "queue": [], "step": 0,
                 "ts": int(time.time() * 1000)}
        wm.save_json(ws.WS_CACHE_FILE, stale)
        check("缺 schema 的旧缓存被作废", ws.get_cache() == {})
        check("作废后查询 -> 立刻重抓一次并带上全部栏目",
              "仲裁" in ws.handle_query({"action": "query", "types": ["arbitration"]}))
        check("新缓存写入了结构版本", ws.get_cache().get("schema") == ws.WS_CACHE_SCHEMA)


# ----------------------------- 9. 查询 / 平原 / 计划 -----------------------------
def test_query_and_plan():
    print("[查询 / 平原 / 计划指令]")
    with isolated():
        ws.refresh_once()
        out = ws.handle_query({"action": "query", "types": list(ws.TYPES.keys())})
        for marker in ("警报", "希图斯", "奥布山谷", "魔胎之境", "突击", "仲裁",
                       "裂缝", "钢铁裂缝", "活动", "入侵", "电波", "虚空商人",
                       "Darvo", "信使", "集团", "建造"):
            check(f"块含 {marker}", marker in out)
        plain = ws.handle_query({"action": "query", "types": ["cycles"]})
        check("平原 = 三处平原全部",
              all(k in plain for k in ("希图斯", "奥布山谷", "魔胎之境")))
        check("平原不会重复输出单独三项", plain.count("希图斯") == 1)
        check("format_for 可用", "警报" in ws.format_for({"alerts"}))
        plan = ws.handle_plan()
        check("计划显示统一调度队列", "统一调度队列" in plan and "排序后的到期队列" in plan)
        check("计划显示最快结束的队首", "魔胎之境" in plan)
        r = qq_common.handle_ws("计划", {"channel": "group", "group_id": "G1"})
        check("handle_ws 计划 返回队列", "统一调度队列" in r and "仲裁" in r)
        r2 = qq_common.handle_ws("世界", {"channel": "group", "group_id": "G1"})
        check("handle_ws 帮助", "世界状态查询" in r2 and "WM" in r2)


# ----------------------------- 10. 监控 / 触发词边界 -----------------------------
def test_monitor_and_trigger():
    print("[监控注册表 / handle_ws 路由 / 触发词边界]")
    with isolated():
        ws.refresh_once()
        sent = []
        ws_monitor.set_sender(lambda ch, tid, text: sent.append((ch, tid, text)))
        ctx = {"channel": "group", "group_id": "GX"}
        check("handle_ws 查询返回文本", "警报" in qq_common.handle_ws("警报", ctx))
        ws_monitor.start_monitor("group", "g1", {"alerts"})
        check("start_monitor 注册", ("group", "g1") in ws_monitor.list_monitors())
        check("start_monitor 类型", ws_monitor.list_monitors()[("group", "g1")] == ["alerts"])
        check("stop_monitor 返回 True", ws_monitor.stop_monitor("group", "g1") is True)
        check("stop 后清空", ("group", "g1") not in ws_monitor.list_monitors())
        check("再次 stop 返回 False", ws_monitor.stop_monitor("group", "g1") is False)
        r = qq_common.handle_ws("监控 裂缝", ctx)
        check("handle_ws 监控确认", "已开始监控" in r
              and ("group", "GX") in ws_monitor.list_monitors())
        r = qq_common.handle_ws("停止监控", ctx)
        check("handle_ws 停止确认", "已停止" in r
              and ("group", "GX") not in ws_monitor.list_monitors())

    check("wm boltor 不算 ws", not qq_common.is_ws_command("wm boltor"))
    check("裂缝 算 ws", qq_common.is_ws_command("裂缝"))
    check("商人 算 ws", qq_common.is_ws_command("商人"))
    check("平原 算 ws", qq_common.is_ws_command("平原"))
    check("监控 算 ws", qq_common.is_ws_command("监控 全部"))
    check("计划 算 ws", qq_common.is_ws_command("计划"))
    check("停止监控 算 ws", qq_common.is_ws_command("停止监控"))


# ----------------------------- 12. 计划：无数据时的状态文案 -----------------------------
def test_plan_no_data():
    print("[计划：无数据时的状态文案]")
    with isolated():
        saved_run, saved_fail = ws._refresh_running, ws._failures
        try:
            ws._refresh_running = False
            ws._failures = 0
            p0 = ws.handle_plan()
            check("线程未起：显示「队列为空，尚未抓取过」",
                  "队列为空，尚未抓取过世界状态" in p0)

            ws._refresh_running = True
            ws._failures = 0
            p1 = ws.handle_plan()
            check("首次抓取进行中", "首次抓取进行中" in p1)

            ws._failures = 3
            p2 = ws.handle_plan()
            check("失败重试中显示次数", "已失败 3 次" in p2 and "持续重试" in p2)
            check("提示境外接口网络问题", "境外" in p2)
        finally:
            ws._refresh_running, ws._failures = saved_run, saved_fail


# ----------------------------- 13. 无数据：退避生效 + 403 文案 -----------------------------
def test_no_data_backoff():
    print("[无数据：退避真正生效 + 403 拦截文案]")
    with isolated():
        saved = (ws._retry_due, ws._last_fetch_error, ws._refresh_running)
        try:
            # 无数据时 next_due 必须尊重退避（之前直接返回 0 会空转猛刷被拦的接口）
            ws._retry_due = time.time() + 30
            check("无数据且退避中：next_due 推到未来", ws.next_due() > time.time())
            ws._retry_due = 0.0
            check("首拉（退避=0）：next_due 立即=0", ws.next_due() == 0.0)

            # 403 拦截文案（区别于「境外超时、网络恢复即可」）
            ws._last_fetch_error = "HTTP 403"
            ws._refresh_running = True
            ws._failures = 5
            p = ws.handle_plan()
            check("计划提示 Cloudflare 拦截并指引换源",
                  "Cloudflare" in p and "WM_WS_API_BASE" in p)

            # 查询遇 403 应给出换源指引，而非「稍后重试」
            with patch.object(ws, "refresh_once", side_effect=RuntimeError("HTTP 403")):
                r = ws.handle_query({"action": "query", "types": ["alerts"]})
            check("查询遇 403 提示换源", "Cloudflare" in r and "WM_WS_API_BASE" in r)
        finally:
            ws._retry_due, ws._last_fetch_error, ws._refresh_running = saved


# ----------------------------- 14. 多源故障转移 -----------------------------
def test_multi_source_failover():
    print("[多源故障转移：主源挂了自动用下一个 / 全部失败抛错]")
    old = os.environ.get("WM_WS_API_BASE")
    try:
        # 主源 down.example.com 必失败，次源 up.example.com 成功
        os.environ["WM_WS_API_BASE"] = "https://down.example.com,https://up.example.com"
        seen = []
        def fake_http(url):
            if "down.example.com" in url:
                raise RuntimeError("连接被拒")
            return {"alerts": [], "ok": True}
        with patch.object(ws, "WS_SOURCE", "warframe"), \
             patch.object(ws, "_http_json", fake_http), \
             patch.object(wm, "pace_gate_ex", lambda d, lo, hi: seen.append(d)):
            d = ws.fetch_worldstate()
        check("主源失败自动回退到次源拿数据", d.get("ok") is True)
        check("两个源各自按域名独立守了 3~5 秒",
              set(seen) == {"down.example.com", "up.example.com"})

        # 全部失败应抛 RuntimeError
        def fake_http2(url):
            raise RuntimeError("HTTP 403")
        os.environ["WM_WS_API_BASE"] = "https://a.example.com,https://b.example.com"
        with patch.object(ws, "WS_SOURCE", "warframe"), \
             patch.object(ws, "_http_json", fake_http2), \
             patch.object(wm, "pace_gate_ex", lambda d, lo, hi: None):
            raised = False
            try:
                ws.fetch_worldstate()
            except RuntimeError:
                raised = True
        check("全部源失败应抛 RuntimeError", raised)
    finally:
        if old is None:
            os.environ.pop("WM_WS_API_BASE", None)
        else:
            os.environ["WM_WS_API_BASE"] = old


# ----------------------------- 15. wf.wiki 适配器 -----------------------------
def test_wfwiki_adapter():
    print("[wf.wiki 适配：RSC 提取 / 字段映射 / 路由]")
    import json as _json
    now = int(time.time() * 1000)
    initial = {
        "server": "intl", "lang": "zh",
        "cycles": [
            {"id": "cetus", "label": "希图斯（平原）", "state": "白昼",
             "stateKey": "day", "expiry": now + 5025000},
            {"id": "vallis", "label": "奥布山谷", "state": "寒冷",
             "stateKey": "cold", "expiry": now + 1200000},
            {"id": "cambion", "label": "魔胎之境", "state": "Fass",
             "stateKey": "fass", "expiry": now + 5025000},
        ],
        "fissures": [
            {"node": "Bode", "system": "谷神星", "faction": "Grineer", "mission": "间谍",
             "tier": "前纪", "steelPath": False, "activation": now - 1000, "expiry": now + 3600000},
            {"node": "Taveuni", "system": "赤毒要塞", "faction": "Grineer", "mission": "生存",
             "tier": "安魂", "steelPath": True, "activation": now - 1000, "expiry": now + 2400000},
        ],
        "alerts": [
            {"node": "Stephano", "system": "天王星", "mission": "防御", "faction": "Grineer",
             "reward": {"credits": 10000, "items": ["Forma ×3"]}, "expiry": now + 7200000},
        ],
        "invasions": [
            {"node": "Titan", "system": "土星", "attackerFaction": "Corpus",
             "defenderFaction": "Grineer", "completion": 33, "completed": False,
             "attackerReward": {"credits": "$undefined", "items": ["某蓝图"]},
             "defenderReward": {"credits": "$undefined", "items": ["某枪管"]}},
        ],
        "events": [
            {"name": "热美亚裂缝", "node": "奥布山谷", "faction": "$undefined",
             "rewards": ["X"], "expiry": now + 86400000},
            {"name": "纪元行动", "node": "$undefined", "expiry": now + 86400000},
        ],
        "nightwave": {"season": 18, "phase": 0, "expiry": now + 864000000,
                      "challenges": [{"name": "特务", "desc": "完成一场任务。", "daily": True}]},
        "voidTrader": {"node": "Kronia 中继站", "activation": now - 86400000,
                       "expiry": now + 86400000, "active": True,
                       "inventory": [{"name": "某外观", "ducats": 163, "credits": 220022}]},
        "dailyDeals": [{"name": "弗拉克斯", "discount": 20, "original": 125,
                        "sale": 100, "expiry": now + 43200000}],
    }
    # 构造 wf.wiki 风格的 RSC 流：self.__next_f.push([1,"<转义>"])，行格式 <id>:<json>
    rsc_line = "4:" + _json.dumps(initial, ensure_ascii=False)
    push = "self.__next_f.push([1," + _json.dumps(rsc_line, ensure_ascii=False) + "])"
    html = "<html><body><script>" + push + "</script></body></html>"

    raw = ws._extract_wfwiki_data(html)
    check("RSC 提取到世界状态对象", isinstance(raw, dict) and "fissures" in raw)
    check("RSC 提取保留中文", raw["events"][0]["name"] == "热美亚裂缝")

    s = ws._wfwiki_to_state(raw)
    check("cetusCycle 白天", s["cetusCycle"]["isDay"] is True)
    check("vallisCycle 寒冷", s["vallisCycle"]["isWarm"] is False)
    check("cambionCycle Fass", s["cambionCycle"]["isFass"] is True)
    check("裂缝按 steelPath 拆分：普通 1 条", len(s["fissures"]) == 1)
    check("钢铁裂缝 1 条", len(s["steelPath"]["current"]) == 1)
    check("裂缝节点带星系前缀", s["fissures"][0]["node"] == "谷神星/Bode")
    check("警报奖励解析", "Forma ×3" in s["alerts"][0]["reward"]["items"])
    check("警报 credits 为数字", s["alerts"][0]["reward"]["credits"] == 10000)
    check("入侵进度换算百分比", s["invasions"][0]["progress"] == "33%")
    check("活动节点 $undefined 被清洗", "（" not in s["events"][1]["description"])
    check("活动带真实节点", "奥布山谷" in s["events"][0]["description"])
    check("电波赛季", s["nightwave"]["season"] == 18)
    check("商人已抵达", s["voidTrader"]["active"] is True)
    check("商人地点映射", s["voidTrader"]["location"] == "Kronia 中继站")
    check("商人清单 name->item", s["voidTrader"]["inventory"][0]["item"] == "某外观")
    check("特惠 original->originalPrice", s["dailyDeals"][0]["originalPrice"] == 125)
    check("毫秒转秒（expiry 落在秒级）", 1e9 < s["cetusCycle"]["expiry"] < 1e11)

    # 缺失类型提示说真话（wf.wiki 无突击）
    with patch.object(ws, "WS_SOURCE", "wfwiki"):
        check("wf.wiki 下突击提示不提供", "不提供" in ws._fmt_sortie(s))
    with patch.object(ws, "WS_SOURCE", "warframe"):
        check("warframe 下突击提示当前无", "当前无" in ws._fmt_sortie(s))

    # 路由：WS_SOURCE=wfwiki 时 fetch 走 _fetch_wfwiki
    with patch.object(ws, "WS_SOURCE", "wfwiki"), \
         patch.object(ws, "_fetch_wfwiki", lambda: {"fissures": [1, 2, 3]}):
        check("wfwiki 源路由到 _fetch_wfwiki", ws.fetch_worldstate() == {"fissures": [1, 2, 3]})


# ----------------------------- 16. 突击补源（DE 官方端点）-----------------------------
def test_sortie_supplement():
    print("[突击补源：官方 Sorties 映射 / wf.wiki 合并]")
    import json as _json
    check("任务码映射 MT_SABOTAGE", ws._code_cn(ws._MISSION_CN, "MT_SABOTAGE", "MT_") == "破坏")
    check("减益映射 LOW_ENERGY", ws._code_cn(ws._MODIFIER_CN, "SORTIE_MODIFIER_LOW_ENERGY",
                                            "SORTIE_MODIFIER_") == "能量缩减")
    check("未知码回退去前缀", ws._code_cn(ws._MISSION_CN, "MT_WEIRD_THING", "MT_") == "Weird Thing")
    check("BOSS 映射 HEK", ws._code_cn(ws._BOSS_CN, "SORTIE_BOSS_HEK", "SORTIE_BOSS_") == "Vay Hek")
    # 官方端点只给编码：缺表就会把 Electricity / Puncture 这类键 title 成英文丢给用户
    check("减益映射 ELECTRICITY 不再是英文",
          ws._code_cn(ws._MODIFIER_CN, "SORTIE_MODIFIER_ELECTRICITY",
                      "SORTIE_MODIFIER_") == "敌人元素强化(电击)")
    check("减益映射 PUNCTURE 不再是英文",
          ws._code_cn(ws._MODIFIER_CN, "SORTIE_MODIFIER_PUNCTURE",
                      "SORTIE_MODIFIER_") == "敌人物理强化(穿刺)")
    check("限定类减益有中文",
          ws._code_cn(ws._MODIFIER_CN, "SORTIE_MODIFIER_SHOTGUN_ONLY",
                      "SORTIE_MODIFIER_") == "霰弹枪限定")
    check("新版 Boss 编码 HYENA 走中文名",
          ws._code_cn(ws._BOSS_CN, "SORTIE_BOSS_HYENA", "SORTIE_BOSS_") == "鬣狗群")
    check("新旧两套 Boss 编码都能翻",
          ws._code_cn(ws._BOSS_CN, "SORTIE_BOSS_KELA", "SORTIE_BOSS_") == "Kela De Thaym"
          and ws._code_cn(ws._BOSS_CN, "SORTIE_BOSS_KRILA", "SORTIE_BOSS_") == "Kela De Thaym")
    check("修饰词表覆盖 WFCD 补的编码",
          all(k in ws._MODIFIER_CN for k in (
              "SORTIE_MODIFIER_IMPACT", "SORTIE_MODIFIER_SLASH",
              "SORTIE_MODIFIER_ELECTRICITY", "SORTIE_MODIFIER_VIRAL",
              "SORTIE_MODIFIER_SECONDARY_ONLY")))

    fake = {"Sorties": [{
        "Expiry": {"$date": {"$numberLong": "1794070800000"}},
        "Boss": "SORTIE_BOSS_HEK",
        "Variants": [
            {"missionType": "MT_LANDSCAPE", "modifierType": "SORTIE_MODIFIER_EXIMUS"},
            {"missionType": "MT_SABOTAGE", "modifierType": "SORTIE_MODIFIER_LOW_ENERGY"},
        ],
    }]}
    with patch.object(ws, "_http_json", lambda url, headers=None: fake), \
         patch.object(wm, "pace_gate_ex", lambda d, lo, hi: None):
        so = ws._fetch_sortie()
    check("突击 Boss", so["boss"] == "Vay Hek")
    check("突击 expiry 毫秒转秒", so["expiry"] == 1794070800.0)
    check("突击关卡数", len(so["variants"]) == 2)
    check("突击关卡类型", so["variants"][1]["node"] == "破坏")
    check("突击关卡减益", so["variants"][1]["modifier"] == "能量缩减")

    # wf.wiki 合并突击
    initial = {"fissures": [], "cycles": [], "alerts": [], "invasions": [],
               "events": [], "dailyDeals": []}
    rsc_line = "4:" + _json.dumps(initial, ensure_ascii=False)
    push = "self.__next_f.push([1," + _json.dumps(rsc_line, ensure_ascii=False) + "])"
    html = "<script>" + push + "</script>"
    with patch.object(ws, "_http_text", lambda url: html), \
         patch.object(ws, "get_state", lambda: {}), \
         patch.object(ws, "_fetch_sortie", lambda: so), \
         patch.object(ws, "_arbi_state", lambda now=None: None), \
         patch.object(wm, "pace_gate_ex", lambda d, lo, hi: None):
        st = ws._fetch_wfwiki()
    check("wf.wiki 合并了突击", st.get("sortie") == so)
    check("wf.wiki 合并时未误伤裂缝键", st.get("fissures") == [])

    # 旧缓存突击未过期应复用、不再请求
    calls = []
    with patch.object(ws, "_http_text", lambda url: html), \
         patch.object(ws, "get_state",
                      lambda: {"sortie": {"expiry": time.time() + 3600, "boss": "旧突击"}}), \
         patch.object(ws, "_fetch_sortie", lambda: calls.append(1) or so), \
         patch.object(ws, "_arbi_state", lambda now=None: None), \
         patch.object(wm, "pace_gate_ex", lambda d, lo, hi: None):
        st2 = ws._fetch_wfwiki()
    check("旧突击未过期被复用", st2["sortie"]["boss"] == "旧突击")
    check("旧突击未过期不再请求", not calls)


# ----------------------------- 17. 主页面其它栏目 -----------------------------
def _rsc_html(initial):
    """把 dict 包成 wf.wiki 风格的 RSC 流（<id>:<json> 单行）。"""
    import json as _json
    line = "4:" + _json.dumps(initial, ensure_ascii=False)
    return ("<html><body><script>self.__next_f.push([1,"
            + _json.dumps(line, ensure_ascii=False) + "])</script></body></html>")


def test_wfwiki_extra_sections():
    print("[主页面其它栏目：双衍/扎里曼/加成/信条/终幕/赏金/轮换/瓦奇娅/新闻]")
    now = int(time.time() * 1000)
    initial = {
        "fissures": [],          # 提取器以 fissures 作为世界状态对象的锚点
        "cycles": [
            {"id": "duviri", "label": "双衍王境", "state": "愤怒",
             "stateKey": "anger", "expiry": now + 7200000},
            {"id": "zariman", "label": "扎里曼", "state": "Corpus",
             "stateKey": "corpus", "expiry": now + 9000000},
        ],
        "globalUpgrades": [{"id": "x", "desc": "经验 2 倍", "expiry": now + 172800000}],
        "tenetCoda": {
            "expiry": now + 3600000,
            "tenet": [{"name": "信条·铁晶磁轨炮", "element": "火焰", "bonus": 26.3,
                       "icon": "$undefined"}],
            "codaBatch": "B", "codaExpiry": now + 86400000,
            "coda": [{"name": "终幕·低音爆囊", "element": "磁力", "bonus": 33.1}],
        },
        "bountyBoard": [{"tag": "CetusSyndicate", "name": "Ostron", "expiry": now + 5400000,
                         "jobs": [{"objective": "削减", "minLevel": 5, "maxLevel": 15,
                                   "stages": 3, "isVault": "$undefined", "rewards": []}]}],
        "bounties": {"expiry": now + 5400000, "rot": "C",
                     "rotRewards": [{"name": "Caliban 头部神经光元", "icon": "$undefined"}],
                     "vaultRot": "B", "vaultRewards": [{"name": "孢丝感染枪 枪机"}],
                     "groups": [{"tag": "ZarimanSyndicate", "name": "坚守者",
                                 "jobs": [{"node": "奥金工场", "mission": "虚空决战",
                                           "challenge": "击杀 2 名虚空天使",
                                           "ally": "$undefined"}]}]},
        "varzia": {"node": "Maroo 的市集", "activation": now - 86400000,
                   "expiry": now + 604800000, "active": True,
                   "inventory": [{"name": "Nidus Prime 组合包", "regalAya": 6,
                                  "aya": "$undefined"}]},
        "news": [{"id": "1", "zh": "开发者直播 198 总结", "en": "$undefined",
                  "other": "$undefined"},
                 {"id": "2", "zh": "$undefined", "en": "Community Stream Schedule",
                  "other": "$undefined"},
                 {"id": "3", "zh": "$undefined", "en": "$undefined", "other": "$undefined"}],
    }
    raw = ws._extract_wfwiki_data(_rsc_html(initial))
    s = ws._wfwiki_to_state(raw)

    check("双衍王境状态", s["duviriCycle"]["state"] == "愤怒")
    check("扎里曼状态", s["zarimanCycle"]["state"] == "Corpus")
    check("全局加成 desc", s["globalUpgrades"][0]["desc"] == "经验 2 倍")
    check("信条武器名", s["tenetCoda"]["tenet"][0]["name"] == "信条·铁晶磁轨炮")
    check("终幕批次", s["tenetCoda"]["codaBatch"] == "B")
    check("终幕武器名", s["tenetCoda"]["coda"][0]["name"] == "终幕·低音爆囊")
    check("赏金任务板集团名", s["bountyBoard"][0]["name"] == "Ostron")
    check("赏金任务目标", s["bountyBoard"][0]["jobs"][0]["objective"] == "削减")
    check("赏金轮换 rot", s["bounties"]["rot"] == "C")
    check("赏金轮换奖励", s["bounties"]["rotRewards"][0] == "Caliban 头部神经光元")
    check("瓦奇娅已到场", s["varzia"]["active"] is True)
    check("瓦奇娅商品价（御品阿耶）", s["varzia"]["inventory"][0]["regalAya"] == 6)
    check("新闻中文优先、空标题丢弃", len(s["news"]) == 2 and s["news"][0]["title"].startswith("开发者"))
    check("新闻无中文时回退英文", s["news"][1]["title"] == "Community Stream Schedule")

    check("_fmt_duviri 输出状态", "愤怒" in ws._fmt_duviri(s))
    check("_fmt_zariman 输出状态", "Corpus" in ws._fmt_zariman(s))
    check("平原组合含五处", all(k in ws._fmt_cycles(s)
                              for k in ("希图斯", "奥布山谷", "魔胎之境", "双衍王境", "扎里曼")))
    check("_fmt_upgrades 输出加成", "经验 2 倍" in ws._fmt_upgrades(s))
    check("_fmt_tenet 输出武器", "信条·铁晶磁轨炮" in ws._fmt_tenet(s))
    check("_fmt_coda 输出批次", "批次 B" in ws._fmt_coda(s))
    check("_fmt_bounty 输出集团", "Ostron" in ws._fmt_bounty(s))
    check("_fmt_bounty_rot 输出轮换", "本轮 C" in ws._fmt_bounty_rot(s))
    check("_fmt_varzia 输出商品", "Nidus Prime 组合包" in ws._fmt_varzia(s))
    check("_fmt_news 输出标题", "开发者直播 198 总结" in ws._fmt_news(s))

    for kw, key in [("双衍", "duviri"), ("王境", "duviri"), ("扎里曼", "zariman"),
                    ("加成", "upgrades"), ("信条", "tenet"), ("终幕", "coda"),
                    ("赏金", "bounty"), ("赏金轮换", "bountyrot"),
                    ("瓦奇娅", "varzia"), ("新闻", "news")]:
        c = ws.parse_ws_command(kw)
        check(f"解析 {kw} -> {key}", c.get("action") == "query" and key in c.get("types", []))
    check("赏金轮换不会被「赏金」重复命中",
          ws.parse_ws_command("监控 赏金轮换")["types"] == ["bountyrot"])
    check("新栏目都能被触发词识别",
          all(qq_common.is_ws_command(k) for k in
              ("双衍", "扎里曼", "加成", "信条", "终幕", "赏金", "瓦奇娅", "新闻")))
    check("帮助里列了仲裁/赏金/新闻",
          all(k in ws.HELP_TEXT for k in ("仲裁", "赏金", "新闻", "瓦奇娅")))
    check("仲裁不再报「数据源不提供」",
          "不提供" not in ws._fmt_arbitration(ws._MOCK_WS))


# ----------------------------- 18. 赏金板 / 平原显示（无板说明 + 过期标注）-----------------------------
def test_bounty_plain_display():
    print("[赏金板与平原显示：无赏金板说明 + 已轮换标注]")
    now = int(time.time() * 1000)
    s = {
        # 只有 Ostron 一个赏金板；双衍/扎里曼不应匹配到
        "bountyBoard": [
            {"tag": "CetusSyndicate", "name": "Ostron", "expiry": now + 5400000,
             "jobs": [{"objective": "削减", "minLevel": 5, "maxLevel": 15, "stages": 3}]},
        ],
        "cetusCycle": {"isDay": True, "expiry": now + 3600000},
        "vallisCycle": {"isWarm": False, "expiry": now + 3600000},
        "cambionCycle": {"isFass": True, "expiry": now + 3600000},
        "duviriCycle": {"state": "愤怒", "expiry": now + 7200000},
        "zarimanCycle": {"state": "Corpus", "expiry": now + 9000000},
    }

    cyc = ws._fmt_cycles(s)
    check("平原组合含五处", all(k in cyc for k in
                            ("希图斯", "奥布山谷", "魔胎之境", "双衍王境", "扎里曼")))
    check("双衍无赏金板时给原因说明", "无开放世界赏金任务板" in cyc)
    check("扎里曼无赏金板时给原因说明", cyc.count("无开放世界赏金任务板") >= 2)
    check("有赏金板的希图斯仍显示任务", "削减" in cyc)

    bounty = ws._fmt_bounty(s)
    check("赏金说明与平原是同一份数据", "与「平原」" in bounty)
    check("赏金输出 Ostron", "Ostron" in bounty)

    # 已过期的赏金板：标注「上一轮任务板」而非只写「已到期」
    s2 = {"bountyBoard": [
        {"tag": "CetusSyndicate", "name": "Ostron", "expiry": now - 1000,
         "jobs": [{"objective": "回收", "minLevel": 5, "maxLevel": 15, "stages": 3}]}]}
    b2 = ws._fmt_bounty(s2)
    check("过期赏金板标注上一轮任务板", "上一轮任务板" in b2)
    check("过期赏金板不再写已到期", "已到期" not in b2)


# ----------------------------- 19. 仲裁（arbi.wf.wiki 轮换表）-----------------------------
def _arbi_fixture():
    start = 1727884800            # 站点原始 startTs（2024-10-02T16:00Z）
    sched = {"schema": 2, "startTs": start, "stepSec": 3600,
             "nodes": ["SolNode81", "ClanNode10"], "seq": [0, 1, 0, 1, 0]}
    nodes = {"schema": 1, "nodes": {
        "SolNode81": {"nodeKey": "SolNode81", "missionNameZh": "生存",
                      "factionNameZh": "Corpus", "nameZh": "Palus",
                      "systemNameZh": "冥王星", "minEnemyLevel": 30, "maxEnemyLevel": 40},
        "ClanNode10": {"nodeKey": "ClanNode10", "missionNameZh": "防御",
                       "factionNameZh": "Grineer", "nameZh": "Romula",
                       "systemNameZh": "金星", "minEnemyLevel": 8, "maxEnemyLevel": 18},
    }}
    return start, sched, nodes


def test_arbitration():
    print("[仲裁：arbi.wf.wiki 轮换表本地推算]")
    start, sched, nodes = _arbi_fixture()
    a = ws._arbi_calc((sched, nodes["nodes"]), start + 3600 * 2 + 70)
    check("按 seq 索引取节点（第 2 格 -> seq[2]=0）", a and a["node"] == "冥王星/Palus")
    check("任务名中文", a["type"] == "生存")
    check("阵营中文", a["enemy"] == "Corpus")
    check("等级区间", a["level"] == "30-40")
    check("换图时刻 = 下一整点", a["expiry"] == start + 3600 * 3)
    check("换图开始时刻", a["start"] == start + 3600 * 2)
    a2 = ws._arbi_calc((sched, nodes["nodes"]), start + 3600 * 3 + 5)
    check("下一格换成第二个节点", a2["node"] == "金星/Romula")
    check("覆盖区间之外返回 None",
          ws._arbi_calc((sched, nodes["nodes"]), start + 3600 * 500) is None)
    check("坏表返回 None", ws._arbi_calc(({"stepSec": 0}, {}), time.time()) is None)
    check("_fmt_arbitration 显示换图倒计时",
          "换图" in ws._fmt_arbitration({"arbitration": a}))
    check("仲裁缺失时如实提示（不说「当前无」）",
          "轮换表暂不可用" in ws._fmt_arbitration({}))

    # 本地无表 -> 抓一次（两次请求）并落盘；7 天内不再抓
    with isolated() as tmp:
        calls = []
        pace = []

        def fake_json(url, headers=None):
            calls.append(url)
            return sched if url.endswith("arbys.schedule.v2.json") else nodes

        now = start + 3600 * 1 + 30
        with patch.object(ws, "_http_json", fake_json), \
             patch.object(wm, "pace_gate_ex",
                          lambda d, lo, hi: pace.append((d, lo, hi))):
            got = ws._arbi_state(now=now)
        check("首次本地无表 -> 抓两张表（2 个请求）", len(calls) == 2)
        check("两个请求前都过了同域名 3~5 秒节流",
              pace == [("arbi.wf.wiki", 3.0, 5.0)] * 2)
        check("抓到后立即能算出仲裁", got and got["node"] == "金星/Romula")
        check("轮换表已落盘", (tmp / "arbys_schedule.json").exists()
              and (tmp / "arbys_nodes.json").exists())
        with patch.object(ws, "_http_json", fake_json):
            again = ws._arbi_state(now=now)
        check("表未过期时零请求（本地算）", len(calls) == 2 and again == got)

        # 表过期 -> 自动重抓
        old = time.time() - (ws.ARBI_TTL + 60)
        for name in ("arbys_schedule.json", "arbys_nodes.json"):
            os.utime(tmp / name, (old, old))
        with patch.object(ws, "_http_json", fake_json):
            ws._arbi_state(now=now)
        check("表过期后自动重抓", len(calls) == 4)

    # 全量抓取时把仲裁并进 state
    with isolated():
        with patch.object(ws, "_http_text", lambda url: _rsc_html({"fissures": []})), \
             patch.object(ws, "_arbi_state",
                          lambda now=None: {"node": "冥王星/Palus", "type": "生存",
                                            "enemy": "Corpus", "level": "30-40",
                                            "expiry": time.time() + 600}), \
             patch.object(ws, "_fetch_sortie", lambda: None):
            st = ws._fetch_wfwiki()
        check("wf.wiki 合并了仲裁", st.get("arbitration", {}).get("node") == "冥王星/Palus")
        check("仲裁进入到期队列", any(q["label"] == "仲裁" for q in ws.build_queue(st, time.time())))


if __name__ == "__main__":
    test_parse()
    test_one_request()
    test_queue_per_entry()
    test_no_repeat()
    test_step_gaps()
    test_pace()
    test_pace_main_task_and_cache_first()
    test_task_single_flight()
    test_special_tasks()
    test_expiry_parsing()
    test_migrate()
    test_query_and_plan()
    test_monitor_and_trigger()
    test_plan_no_data()
    test_no_data_backoff()
    test_multi_source_failover()
    test_wfwiki_adapter()
    test_sortie_supplement()
    test_wfwiki_extra_sections()
    test_bounty_plain_display()
    test_arbitration()
    print(f"\n结果：通过 {PASS} / 失败 {FAIL}")
    sys.exit(1 if FAIL else 0)
