#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防规避回归检测（不联网）：把 env 全压到秒级，验证真实源下【硬】时间规则仍锁死。

缓存新鲜度 WM_CACHE_MINUTES 是用户要求的可配置参数（默认 60 分钟，可改任意分钟），
不属于硬下限，因此不在此锁死；本脚本只锁死：
同域名 3~5s 节流、1/3/7 分钟重试、4 次全失败后 1 小时。

用法：python test_pace_guard.py
被 `python -m unittest discover` 误导入时自动跳过（本文件是脚本式，不是 TestCase）。
"""
if __name__ != "__main__":
    import unittest
    raise unittest.SkipTest("脚本式测试，请直接 python test_pace_guard.py 运行")

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EVIL_ENV = dict(os.environ)
EVIL_ENV.update({
    "WM_SOURCE": "warframe",       # 真实源（只 import，不发任何 HTTP）
    "WM_MIN_GAP": "0.2",
    "WM_MAX_GAP": "0.5",
    "WM_MICRO_WAITS": "1,1,1",
    "WM_MACRO_WAIT": "1",
})

PROBE = (
    "import wm,json;"
    "print(json.dumps({"
    "'MIN_GAP':wm.MIN_GAP,'MAX_GAP':wm.MAX_GAP,"
    "'STALE_MS':wm.STALE_MS,'MICRO_WAITS':wm.MICRO_WAITS,"
    "'MACRO_WAIT_MS':wm.MACRO_WAIT_MS}))"
)
r = subprocess.run([sys.executable, "-c", PROBE], cwd=HERE, env=EVIL_ENV,
                   capture_output=True, text=True)
if r.returncode != 0:
    print("PROBE_FAILED:", r.stderr.strip())
    sys.exit(1)
v = json.loads(r.stdout.strip())

failed = False


def check(name, ok, detail=""):
    global failed
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    if not ok:
        failed = True


check("同域名间隔下限 >= 3s", v["MIN_GAP"] >= 3.0, f"实际 {v['MIN_GAP']}s")
check("同域名间隔上限 >= 5s", v["MAX_GAP"] >= 5.0, f"实际 {v['MAX_GAP']}s")
check("重试间隔 >= 1/3/7 分钟", v["MICRO_WAITS"][:3] >= [60, 180, 420],
      f"实际 {v['MICRO_WAITS'][:3]}")
check("全失败间隔 >= 1 小时", v["MACRO_WAIT_MS"] >= 3600 * 1000,
      f"实际 {v['MACRO_WAIT_MS'] / 1000:.0f}s")


def probe_cache(overrides):
    env = dict(os.environ)
    env["WM_SOURCE"] = "mock"          # 离线，不发 HTTP
    env.pop("WM_CACHE_MINUTES", None)
    env.update(overrides)
    # 隔离本机 data/config.txt：其中若设了 WM_CACHE_MINUTES=3，会在 wm 导入时被
    # config.apply() 注入 env，把「内置默认值 60」覆盖掉。这里先让 config 指向一个
    # 不存在的临时路径，测的才是代码内置默认值；覆盖场景由下面的 overrides 验证。
    code = ("import pathlib, tempfile, config\n"
            "config.CONFIG_FILE = pathlib.Path(tempfile.gettempdir()) / '__wm_bot_no_config__.txt'\n"
            "config.TEMPLATE_FILE = config.CONFIG_FILE\n"
            "import wm;print(wm.CACHE_MINUTES, wm.STALE_MS)")
    p = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env,
                        capture_output=True, text=True)
    if p.returncode != 0:
        return None
    m, ms = p.stdout.strip().split()
    return int(m), int(ms)


d = probe_cache({})
check("缓存间隔默认 60 分钟", d == (60, 60 * 60 * 1000), f"实际 {d}")
c = probe_cache({"WM_CACHE_MINUTES": "15"})
check("缓存间隔可按分钟配置（15 -> 900000ms）", c == (15, 15 * 60 * 1000), f"实际 {c}")

with open(os.path.join(HERE, "wm.py"), "r", encoding="utf-8") as f:
    src = f.read()
check("源码未直接把 env 当节流间隔用（需经 _clamp）",
      not ('os.environ.get("WM_MIN_GAP"' in src and "_clamp" not in src))
check("不再拉取全站物品/武器清单",
      'http_get("/v2/items")' not in src and 'http_get("/v2/riven/weapons")' not in src)
check("超间隔先联网刷新缓存再回复，失败不假死（重试交后台）",
      "refresh_once(type_, name)" in src
      and "retry_in_background" in src
      and "get_cached_or_refresh" in src)

print("PACE_GUARD_FAIL" if failed else "PACE_GUARD_OK")
sys.exit(1 if failed else 0)
