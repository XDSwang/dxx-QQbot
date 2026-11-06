#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缓存策略回归（离线）：验证「超间隔先刷新再回复，绝不发旧缓存」+「刷新失败不假死」。

- 逻辑用例用 stub 替换 refresh_once：不发 HTTP、不触碰节流，只验证决策；
- 端到端不 stub：真实走 pace_gate（真等 3~5 秒，不快进）+ mock_fetch + 写缓存；
- 失败用例：mock 故意失败，验证用户线程只尝试一次、立即返回明确报错（不发旧数据），
  1/3/7 分钟重试交给后台，不阻塞主线程（用户要求「演示真实才能反映问题」）。
"""
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["WM_SOURCE"] = "mock"

import wm


def _write_cache(path, age_ms, status="ok"):
    now = int(time.time() * 1000)
    wm.save_json(path, {"entries": {
        "item:boltor prime": {
            "type": "item", "name": "Boltor Prime",
            "data": {"price": 999, "orders": [], "count": 0},
            "ts": now - age_ms, "status": status,
        }
    }})


class TestCacheDecision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_cache, self.orig_pace = wm.CACHE_FILE, wm.PACE_FILE
        wm.CACHE_FILE = Path(self.tmp.name) / "cache.json"
        wm.PACE_FILE = Path(self.tmp.name) / "pace.json"
        self.orig_refresh = wm.refresh_once
        self.calls = []

        def stub(type_, name):
            self.calls.append((type_, name))
            entry = {"type": type_, "name": name,
                     "data": {"price": 1, "orders": [], "count": 0},
                     "ts": int(time.time() * 1000), "status": "ok"}
            return {"served": "fresh", "entry": entry, "fresh": True}

        wm.refresh_once = stub

    def tearDown(self):
        wm.refresh_once = self.orig_refresh
        wm.CACHE_FILE, wm.PACE_FILE = self.orig_cache, self.orig_pace
        self.tmp.cleanup()

    def test_fresh_cache_no_refresh(self):
        _write_cache(wm.CACHE_FILE, 60_000)              # 1 分钟前，未超 60 分钟
        r = wm.get_cached_or_refresh("item", "Boltor Prime")
        self.assertEqual(r["served"], "cache")
        self.assertEqual(self.calls, [])

    def test_stale_cache_refreshes_first(self):
        _write_cache(wm.CACHE_FILE, 70 * 60 * 1000)      # 70 分钟前
        r = wm.get_cached_or_refresh("item", "Boltor Prime")
        self.assertEqual(self.calls, [("item", "Boltor Prime")])
        self.assertEqual(r["served"], "fresh")

    def test_months_old_cache_never_served(self):
        _write_cache(wm.CACHE_FILE, 90 * 24 * 3600 * 1000)   # 90 天前
        r = wm.get_cached_or_refresh("item", "Boltor Prime")
        self.assertEqual(r["served"], "fresh")
        self.assertEqual(len(self.calls), 1)

    def test_no_cache_refreshes(self):
        r = wm.get_cached_or_refresh("item", "Boltor Prime")
        self.assertEqual(r["served"], "fresh")

    def test_error_status_refreshes(self):
        _write_cache(wm.CACHE_FILE, 60_000, status="error")
        r = wm.get_cached_or_refresh("item", "Boltor Prime")
        self.assertEqual(r["served"], "fresh")

    def test_mock_fake_price_never_served_as_cache(self):
        """离线测试误写进 data/cache.json 的 MockSeller 假价格，绝不能秒回给用户。"""
        now = int(time.time() * 1000)
        wm.save_json(wm.CACHE_FILE, {"entries": {
            "item:boltor prime": {
                "type": "item", "name": "Boltor Prime",
                "data": {"price": 10, "orders": [{"seller": "MockSeller1", "price": 10}]},
                "source": "mock（Boltor Prime 本地测试数据）",
                "ts": now, "status": "ok",
            }
        }})
        r = wm.get_cached_or_refresh("item", "Boltor Prime")
        self.assertEqual(self.calls, [("item", "Boltor Prime")],
                         "假缓存必须被视为无缓存、触发真实刷新")
        self.assertEqual(r["served"], "fresh")


class TestFailureDoesNotBlock(unittest.TestCase):
    """真实源挂掉时：单次尝试 + 立即明确报错，重试交给后台，主线程绝不假死。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_cache, self.orig_pace, self.orig_data = (
            wm.CACHE_FILE, wm.PACE_FILE, wm.DATA_DIR)
        wm.CACHE_FILE = Path(self.tmp.name) / "cache.json"
        wm.PACE_FILE = Path(self.tmp.name) / "pace.json"
        wm.DATA_DIR = Path(self.tmp.name)
        self.orig_retry = wm.retry_in_background
        self.retry_calls = []
        wm.retry_in_background = lambda t, n: (self.retry_calls.append((t, n)), True)[1]
        self.orig_fail = wm.MOCK_FAIL
        wm.MOCK_FAIL = 99        # mock 取数永远失败，模拟真实源连不上

    def tearDown(self):
        wm.MOCK_FAIL = self.orig_fail
        wm.retry_in_background = self.orig_retry
        wm.CACHE_FILE, wm.PACE_FILE, wm.DATA_DIR = (
            self.orig_cache, self.orig_pace, self.orig_data)
        self.tmp.cleanup()

    def test_failure_returns_immediately_no_stale_data(self):
        _write_cache(wm.CACHE_FILE, 90 * 24 * 3600 * 1000)   # 90 天前的旧缓存
        wm.save_json(wm.PACE_FILE, {"api.warframe.market": time.time()})
        t0 = time.time()
        r = wm.get_cached_or_refresh("item", "Boltor Prime")
        elapsed = time.time() - t0
        self.assertEqual(r["served"], "error")
        self.assertTrue(r["retrying"], "失败应把 1/3/7 分钟重试交给后台")
        self.assertEqual(self.retry_calls, [("item", "Boltor Prime")])
        # 真等 3~5 秒节流（不快进），但绝不等 1/3/7 分钟重试
        self.assertGreaterEqual(elapsed, 2.9, "同域名 3~5 秒节流被绕过")
        self.assertLess(elapsed, 9.0, "刷新失败把主线程阻塞了")
        # 旧缓存绝不被当结果发出，也不会被失败结果覆盖
        c = wm.load_json(wm.CACHE_FILE, {})
        self.assertEqual(c["entries"]["item:boltor prime"]["data"]["price"], 999)
        self.assertIn("暂时查不到最新数据", wm.format_reply(r))
        self.assertIn("后台", wm.format_reply(r))


class TestSlug(unittest.TestCase):
    def test_slugify_by_query(self):
        self.assertEqual(wm.item_slug("Boltor Prime"), "boltor_prime")
        self.assertEqual(wm.item_slug("  Saryn Prime Systems  "), "saryn_prime_systems")
        self.assertEqual(wm.riven_slug("Tigris Prime"), "tigris")
        self.assertEqual(wm.riven_slug("Tigris"), "tigris")
        self.assertEqual(wm.item_slug("中文名"), "")


class TestEndToEndRefresh(unittest.TestCase):
    """不 stub：真走 pace_gate 3~5 秒 + mock_fetch，验证缓存先更新再回复。"""
    def test_expired_cache_triggers_real_refresh(self):
        tmp = tempfile.TemporaryDirectory()
        orig_cache, orig_pace = wm.CACHE_FILE, wm.PACE_FILE
        try:
            wm.CACHE_FILE = Path(tmp.name) / "cache.json"
            wm.PACE_FILE = Path(tmp.name) / "pace.json"
            _write_cache(wm.CACHE_FILE, 70 * 60 * 1000)
            # 刚访问过同域名 -> 必须真等 3~5 秒（不快进）
            wm.save_json(wm.PACE_FILE, {"api.warframe.market": time.time()})
            t0 = time.time()
            r = wm.get_cached_or_refresh("item", "Boltor Prime")
            elapsed = time.time() - t0
            self.assertEqual(r["served"], "fresh")
            self.assertGreaterEqual(elapsed, 2.9, "同域名 3~5 秒节流被绕过")
            c = wm.load_json(wm.CACHE_FILE, {})
            ts = c["entries"]["item:boltor prime"]["ts"]
            self.assertLess(abs(time.time() * 1000 - ts), 15_000, "缓存未先更新就回复")
        finally:
            wm.CACHE_FILE, wm.PACE_FILE = orig_cache, orig_pace
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
