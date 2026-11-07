#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pace_api.py — 节流器原子接口（pace_last_access / pace_record）离线测试

覆盖：
  - pace_last_access：没访问过返回 0；有记录返回上次访问时间；时钟回拨按 0 处理
  - pace_record：写入后能被 pace_last_access 读到；文件不存在自动创建
  - 不同域名互不干扰（一个 JSON 文件按域名键存）
  - pace_gate_ex 内部复用这两个原子函数（行为不变）
  - pace_record 只改自己那个域名的键，其它域名原样保留
运行：python test_pace_api.py
"""
import os
import sys
import time
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wm

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


from contextlib import contextmanager


@contextmanager
def isolated_pace():
    """临时 pace.json + 跳过 sleep，保证离线测试秒级完成。"""
    tmp = Path(tempfile.mkdtemp(prefix="pace-api-"))
    saved = wm.PACE_FILE
    try:
        with patch.object(wm, "PACE_FILE", tmp / "pace.json"):
            yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_last_access():
    print("[pace_last_access：查询上次访问时间]")
    with isolated_pace() as tmp:
        # 文件不存在
        check("文件不存在 -> 返回 0（没访问过）",
              wm.pace_last_access("api.warframe.market") == 0.0)
        # 写一条记录
        wm.pace_record("api.warframe.market", time.time() - 10)
        last = wm.pace_last_access("api.warframe.market")
        check("有记录 -> 返回上次访问时间（约 10 秒前）",
              8 < (time.time() - last) < 12)
        # 没记录的别的域名
        check("另一个域名没访问过 -> 返回 0",
              wm.pace_last_access("api.warframestat.us") == 0.0)
        # 损坏的非数字值
        wm.save_json(wm.PACE_FILE, {"bad.example.com": "not-a-number"})
        check("记录损坏（非数字）-> 返回 0",
              wm.pace_last_access("bad.example.com") == 0.0)
        # 时钟回拨：记录在未来
        wm.save_json(wm.PACE_FILE, {"future.example.com": time.time() + 9999})
        check("记录在未来（时钟回拨）-> 按 0 处理",
              wm.pace_last_access("future.example.com") == 0.0)


def test_record():
    print("[pace_record：记录本次访问时间]")
    with isolated_pace() as tmp:
        # 文件 / 目录不存在 -> 自动创建
        check("初始无 pace.json", not (tmp / "pace.json").exists())
        t0 = wm.pace_record("api.warframe.market")
        check("写入后文件自动创建", (tmp / "pace.json").exists())
        check("写入的就是真实当前时间", abs(t0 - time.time()) < 1)
        check("能被 pace_last_access 读到",
              abs(wm.pace_last_access("api.warframe.market") - t0) < 0.1)

        # 再写一个不同域名
        t1 = wm.pace_record("api.warframestat.us", time.time() - 30)
        check("第二个域名也写入", abs(wm.pace_last_access("api.warframestat.us") - t1) < 0.1)
        check("第一个域名记录原样保留（只改自己那个键）",
              abs(wm.pace_last_access("api.warframe.market") - t0) < 0.1)

        # pace.json 的格式就是「域名 -> 时间戳」扁平字典
        raw = json.load(open(tmp / "pace.json", encoding="utf-8"))
        check("pace.json 是 {域名: 时间戳} 的扁平 JSON",
              isinstance(raw, dict) and "api.warframe.market" in raw
              and "api.warframestat.us" in raw
              and isinstance(raw["api.warframe.market"], (int, float)))

        # 重写一个域名：其它域名不动
        t2 = wm.pace_record("api.warframe.market", time.time())
        raw2 = json.load(open(tmp / "pace.json", encoding="utf-8"))
        check("重写一个域名不影响其它域名",
              abs(raw2["api.warframestat.us"] - t1) < 0.1
              and abs(raw2["api.warframe.market"] - t2) < 0.1)


def test_gate_ex_uses_atoms():
    print("[pace_gate_ex 内部复用 pace_last_access / pace_record]")
    with isolated_pace() as tmp:
        seen = {"last": 0, "rec": 0}
        real_last = wm.pace_last_access
        real_rec = wm.pace_record

        def spy_last(d):
            seen["last"] += 1
            return real_last(d)

        def spy_rec(d, when=None):
            seen["rec"] += 1
            return real_rec(d, when)

        # 首次访问：last_access 返回 0 -> 按完整间隔等；mock 下 sleep 也走（lo=hi=0 跳过）
        with patch.object(wm, "pace_last_access", spy_last), \
             patch.object(wm, "pace_record", spy_rec), \
             patch.object(wm.time, "sleep", lambda s: None):
            wm.pace_gate_ex("api.warframestat.us", 3.0, 5.0)
        check("pace_gate_ex 调用了 pace_last_access 查上次", seen["last"] == 1)
        check("pace_gate_ex 调用了 pace_record 写本次", seen["rec"] == 1)
        check("pace_gate_ex 之后该域名有记录了",
              wm.pace_last_access("api.warframestat.us") > 0)

        # 第二次：刚访问过，需要按 3~5 秒等待（mock sleep 跳过，但 elapsed 已算对）
        seen = {"last": 0, "rec": 0}
        with patch.object(wm, "pace_last_access", spy_last), \
             patch.object(wm, "pace_record", spy_rec), \
             patch.object(wm.time, "sleep", lambda s: None):
            wm.pace_gate_ex("api.warframestat.us", 3.0, 5.0)
        check("第二次 pace_gate_ex 仍只查一次 last / 写一次 rec",
              seen["last"] == 1 and seen["rec"] == 1)

        # 不同域名互不干扰
        seen = {"last": 0, "rec": 0}
        with patch.object(wm, "pace_last_access", spy_last), \
             patch.object(wm, "pace_record", spy_rec), \
             patch.object(wm.time, "sleep", lambda s: None):
            wm.pace_gate_ex("api.warframe.market", 3.0, 5.0)
        check("另一个域名独立查/写", seen["last"] == 1 and seen["rec"] == 1)
        check("WM 域名记录与世界状态域名互不干扰",
              wm.pace_last_access("api.warframe.market") > 0
              and wm.pace_last_access("api.warframestat.us") > 0)


def test_gate_ex_wait_logic():
    print("[pace_gate_ex 等待逻辑：没访问过等满 / 刚访问过等差值]")
    with isolated_pace() as tmp:
        sleeps = []
        with patch.object(wm.time, "sleep", lambda s: sleeps.append(s)):
            # 没访问过 -> elapsed=0 -> wait = gap（3~5 秒随机）
            wm.pace_gate_ex("a.example.com", 3.0, 5.0)
            check("没访问过 -> 等满一个间隔（3~5 秒）",
                  3.0 <= sleeps[0] <= 5.0)
        # 刚访问过（记录在 1 秒前）-> wait = gap - 1（仍 2~4 秒）
        wm.pace_record("b.example.com", time.time() - 1)
        sleeps = []
        with patch.object(wm.time, "sleep", lambda s: sleeps.append(s)):
            wm.pace_gate_ex("b.example.com", 3.0, 5.0)
        check("刚访问过 1 秒 -> 只等 (gap - 1) 秒",
              2.0 <= sleeps[0] <= 4.0)
        # 间隔已过（记录在 10 秒前）-> wait = 0（不 sleep）
        wm.pace_record("c.example.com", time.time() - 10)
        sleeps = []
        with patch.object(wm.time, "sleep", lambda s: sleeps.append(s)):
            wm.pace_gate_ex("c.example.com", 3.0, 5.0)
        check("间隔已过 -> 不再 sleep（wait=0）",
              sleeps == [] or sleeps == [0.0] or sleeps[0] == 0.0)


if __name__ == "__main__":
    test_last_access()
    test_record()
    test_gate_ex_uses_atoms()
    test_gate_ex_wait_logic()
    print(f"\n结果：通过 {PASS} / 失败 {FAIL}")
    sys.exit(1 if FAIL else 0)
