#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""物品名 -> slug 解析回归（离线，零联网）。

这次线上 404 的根因：WM 上「Boltor Prime」不是可交易条目，
真正挂订单的是「Boltor Prime Set」(boltor_prime_set)。

- 纯逻辑用例 stub 掉 http_get，只验证候选顺序与错误分类，不模拟同域名访问；
- 端到端用例只 stub 掉 urllib.request.urlopen（完整保留 http_get -> pace_gate），
  验证「每个候选请求都真等 3~5 秒同域名节流」，绝不快进（用户铁律）。

用法：python test_slug_resolution.py
"""
import io
import os
import sys
import json
import time
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["WM_SOURCE"] = "warframe"   # 走真实分支，但网络层被 stub，零联网

import wm

NOT_FOUND_BODY = b'{"apiVersion":"0.25.0","data":null,"error":{"request":["app.item.notFound"]}}'


class FakeResp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")
        self.headers = {}
        self.status = 200

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def order_row(price=40, seller="Tester"):
    return {"type": "sell", "platinum": price, "quantity": 1,
            "user": {"ingameName": seller}}


class TestCandidates(unittest.TestCase):
    def test_prime_prefers_set(self):
        self.assertEqual(wm.item_slug_candidates("Boltor Prime"),
                         ["boltor_prime_set", "boltor_prime"])

    def test_plain_item_keeps_base_first(self):
        self.assertEqual(wm.item_slug_candidates("Vitality"),
                         ["vitality", "vitality_set"])

    def test_part_name_not_suffixed(self):
        self.assertEqual(wm.item_slug_candidates("Boltor Prime Blueprint"),
                         ["boltor_prime_blueprint"])
        self.assertEqual(wm.item_slug_candidates("Saryn Prime Systems"),
                         ["saryn_prime_systems"])

    def test_invalid_name_has_no_candidate(self):
        self.assertEqual(wm.item_slug_candidates("中文名"), [])

    def test_never_fetches_full_index(self):
        src = (HERE / "wm.py").read_text(encoding="utf-8")
        self.assertNotIn('http_get("/v2/items")', src)
        self.assertNotIn('http_get("/v2/riven/weapons")', src)


class TestFetchItemBranches(unittest.TestCase):
    """stub http_get：只验证分支决策；不模拟同域名访问，节流由端到端用例覆盖。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = (wm.SLUG_FILE, wm.DATA_DIR, wm.SOURCE, wm.http_get)
        wm.SLUG_FILE = Path(self.tmp.name) / "slug.json"
        wm.DATA_DIR = Path(self.tmp.name)
        wm.SOURCE = "warframe"
        self.calls = []
        self.behavior = {}

        def fake_http(path, params=None):
            self.calls.append(path)
            b = self.behavior.get(path)
            if isinstance(b, Exception):
                raise b
            return b if b is not None else {"data": [order_row()]}

        wm.http_get = fake_http

    def tearDown(self):
        wm.SLUG_FILE, wm.DATA_DIR, wm.SOURCE, wm.http_get = self.orig
        self.tmp.cleanup()

    def test_first_candidate_404_falls_back_to_second(self):
        self.behavior = {
            "/v2/orders/item/boltor_prime_set": wm.ItemNotFound("404"),
            "/v2/orders/item/boltor_prime": {"data": [order_row(77)]},
        }
        r = wm.fetch_item("Boltor Prime")
        self.assertEqual(r["price"], 77)
        self.assertEqual(self.calls, ["/v2/orders/item/boltor_prime_set",
                                     "/v2/orders/item/boltor_prime"])
        self.assertEqual(wm.load_json(wm.SLUG_FILE, {}).get("item:boltor prime"),
                         "boltor_prime")

    def test_slug_cache_avoids_second_probe(self):
        wm.save_json(wm.SLUG_FILE, {"item:boltor prime": "boltor_prime_set"})
        r = wm.fetch_item("Boltor Prime")
        self.assertEqual(r["price"], 40)
        self.assertEqual(self.calls, ["/v2/orders/item/boltor_prime_set"])

    def test_network_error_does_not_keep_hitting_source(self):
        self.behavior = {
            "/v2/orders/item/boltor_prime_set": RuntimeError("网络错误 超时")
        }
        with self.assertRaises(RuntimeError):
            wm.fetch_item("Boltor Prime")
        self.assertEqual(len(self.calls), 1,
                         "非 404 错误不得继续探测下一个 slug（避免无谓打源）")

    def test_all_candidates_404_tells_user_full_name(self):
        self.behavior = {
            "/v2/orders/item/boltor_prime_set": wm.ItemNotFound("404"),
            "/v2/orders/item/boltor_prime": wm.ItemNotFound("404"),
        }
        with self.assertRaises(wm.ItemNotFound) as cm:
            wm.fetch_item("Boltor Prime")
        self.assertIn("Boltor Prime Set", str(cm.exception))


class TestPermanentError(unittest.TestCase):
    """物品名不对是确定性错误：不启动 1/3/7 分钟后台重试，CLI 也不空跑重试。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = (wm.CACHE_FILE, wm.PACE_FILE, wm.SLUG_FILE, wm.DATA_DIR,
                     wm.SOURCE, wm.retry_in_background, wm.http_get)
        wm.CACHE_FILE = Path(self.tmp.name) / "cache.json"
        wm.PACE_FILE = Path(self.tmp.name) / "pace.json"
        wm.SLUG_FILE = Path(self.tmp.name) / "slug.json"
        wm.DATA_DIR = Path(self.tmp.name)
        wm.SOURCE = "warframe"
        self.retries = []
        wm.retry_in_background = lambda t, n: (self.retries.append((t, n)), True)[1]

        def fake_http(path, params=None):
            raise wm.ItemNotFound("物品不存在 " + path)

        wm.http_get = fake_http

    def tearDown(self):
        (wm.CACHE_FILE, wm.PACE_FILE, wm.SLUG_FILE, wm.DATA_DIR, wm.SOURCE,
         wm.retry_in_background, wm.http_get) = self.orig
        self.tmp.cleanup()

    def test_permanent_error_no_background_retry(self):
        r = wm.get_cached_or_refresh("item", "Boltor Prime")
        self.assertEqual(r["served"], "error")
        self.assertTrue(r["permanent"])
        self.assertFalse(r["retrying"])
        self.assertEqual(self.retries, [], "确定性 404 不该浪费后台重试")
        reply = wm.format_reply(r)
        self.assertIn("完整物品名", reply)
        self.assertNotIn("后台", reply)

    def test_cli_stops_on_permanent_error(self):
        r = wm.do_refresh("item", "Boltor Prime")
        self.assertTrue(r.get("permanent"), "CLI 不该对 404 跑满 4 次重试")


class TestThrottleAcrossCandidates(unittest.TestCase):
    """只 stub urlopen，保留 http_get -> pace_gate：候选请求之间必须真等 3~5 秒。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = (wm.PACE_FILE, wm.SLUG_FILE, wm.DATA_DIR, wm.SOURCE,
                     urllib.request.urlopen)
        wm.PACE_FILE = Path(self.tmp.name) / "pace.json"
        wm.SLUG_FILE = Path(self.tmp.name) / "slug.json"
        wm.DATA_DIR = Path(self.tmp.name)
        wm.SOURCE = "warframe"
        self.stamps = []

        def fake_urlopen(req, timeout=None):
            self.stamps.append(time.time())
            url = req.full_url
            if "vitality_set" in url:
                return FakeResp({"data": [order_row(12)]})
            raise urllib.error.HTTPError(url, 404, "Not Found", {},
                                         io.BytesIO(NOT_FOUND_BODY))

        urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        urllib.request.urlopen = self.orig[4]
        (wm.PACE_FILE, wm.SLUG_FILE, wm.DATA_DIR,
         wm.SOURCE) = self.orig[:4]
        self.tmp.cleanup()

    def test_each_candidate_waits_3_to_5_seconds(self):
        r = wm.fetch_item("Vitality")
        self.assertEqual(r["price"], 12)
        self.assertEqual(len(self.stamps), 2, "应恰好探测 2 个候选")
        gap = self.stamps[1] - self.stamps[0]
        self.assertGreaterEqual(gap, 2.9, "候选请求之间绕过了 3~5 秒同域名节流")
        self.assertLessEqual(gap, 5.6, "节流间隔不应超过 5 秒太多")
        self.assertEqual(wm.load_json(wm.SLUG_FILE, {}).get("item:vitality"),
                         "vitality_set")
        pace = wm.load_json(wm.PACE_FILE, {})
        self.assertIn("api.warframe.market", pace, "节流时间戳必须落盘")


if __name__ == "__main__":
    unittest.main(verbosity=2)
