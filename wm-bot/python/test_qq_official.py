#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_official.broadcast_to_groups 的单元测试（mock token + 发消息通道，不联网）。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import qq_official


class _GoodToken:
    def get_token(self, force=False):
        return "FAKE_TOKEN"


class _BadToken:
    def get_token(self, force=False):
        raise RuntimeError("no token")


def _patch(token_obj, send_results):
    """替换 TokenManager 与 send_message；send_results 是逐群 (ok, info) 列表。"""
    qq_official.TokenManager = lambda appid, secret, api_base: token_obj
    calls = []

    def fake_send(token, appid, api_base, target_kind, target_id, content, msg_id=None):
        calls.append((target_kind, target_id, content))
        if send_results:
            return send_results.pop(0)
        return True, None

    qq_official.send_message = fake_send
    return calls


class TestBroadcast(unittest.TestCase):
    def test_empty_groups_returns_zero(self):
        calls = _patch(_GoodToken(), [])
        sent, failed = qq_official.broadcast_to_groups(
            "a", "s", "https://x", [], "hi")
        self.assertEqual((sent, failed), (0, 0))
        self.assertEqual(calls, [])

    def test_all_ok_counts_and_content(self):
        calls = _patch(_GoodToken(), [(True, None), (True, None), (True, None)])
        sent, failed = qq_official.broadcast_to_groups(
            "a", "s", "https://x", ["g1", "g2", "g3"], "即将更新，暂停服务")
        self.assertEqual((sent, failed), (3, 0))
        self.assertEqual([c[1] for c in calls], ["g1", "g2", "g3"])
        self.assertEqual(calls[0][2], "即将更新，暂停服务")
        self.assertEqual(calls[0][0], "group")

    def test_partial_failure_counted(self):
        calls = _patch(_GoodToken(),
                       [(True, None), (False, "40034105"), (True, None)])
        sent, failed = qq_official.broadcast_to_groups(
            "a", "s", "https://x", ["g1", "g2", "g3"], "更新完成，开始服务")
        self.assertEqual((sent, failed), (2, 1))
        self.assertEqual(len(calls), 3)

    def test_token_failure_returns_all_failed(self):
        calls = _patch(_BadToken(), [])
        sent, failed = qq_official.broadcast_to_groups(
            "a", "s", "https://x", ["g1", "g2"], "hi")
        self.assertEqual((sent, failed), (0, 2))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
