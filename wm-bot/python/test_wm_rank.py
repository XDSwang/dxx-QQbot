#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卖家在线状态排序（离线，不联网）。

用户的实际痛点：WM 上大量卖家早已离线（甚至 2035 封号），报价是几个月前挂的，
价格看着最低却根本买不到。这里覆盖：
  - 状态解析：ingame / online / offline / 缺字段；
  - 排序：游戏中 → 在线 → 离线；组内价格从低到高；离线太久没登录的沉底；
  - 代表价（price / median）只取「现在能交易」的报价，全离线才退回全部；
  - 紫卡：closed 拍卖跳过、无一口价（bidOnly）排在同组后面；
  - 回复文案：状态概览、状态图标、离线天数、全离线警告、/w 用第一家；
  - mock 数据也带状态，便于离线演示。
"""
import os
import sys
import time
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["WM_SOURCE"] = "mock"   # 只 import，不发 HTTP；需要真实分支时用例内临时改 SOURCE

import wm

# 离线测试用 mock 假价格，绝不能写进真实 data/cache.json，
# 否则下次启动机器人会把 MockSeller 当真实价格秒回给用户。
import tempfile
from pathlib import Path
_TEST_DATA = tempfile.TemporaryDirectory()
wm.DATA_DIR = Path(_TEST_DATA.name)
wm.CACHE_FILE = wm.DATA_DIR / "cache.json"
wm.PACE_FILE = wm.DATA_DIR / "pace.json"
wm.SLUG_FILE = wm.DATA_DIR / "slug.json"


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    return 1 if cond else 0


def ago(days=0, hours=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() - days * 86400 - hours * 3600))


def row(price, status="offline", seller="S", days=0, **kw):
    r = {"seller": seller, "price": price, "quantity": 1}
    r.update(wm._status_info(status, ago(days)))
    r.update(kw)
    return r


def item_payload(rows):
    """把内部行还原成 WM /v2/orders 原始结构，喂给 fetch_item。"""
    return {"data": [
        {"type": "sell", "platinum": r["price"], "quantity": 1,
         "user": {"ingameName": r["seller"], "status": r.get("status") or None,
                  "lastSeen": r.get("_last")}}
        for r in rows
    ]}


def test_iso_and_status():
    print("[时间与状态解析]")
    ok = total = 0
    total += 1
    ok += check("解析 ...Z", wm._iso_ts("2026-11-06T13:42:50Z") is not None)
    total += 1
    ok += check("解析 +00:00 带毫秒",
                wm._iso_ts("2026-11-06T16:04:46.101+00:00") is not None)
    total += 1
    ok += check("Z 与 +00:00 指向同一时刻",
                wm._iso_ts("2026-11-06T13:42:50Z")
                == wm._iso_ts("2026-11-06T13:42:50.000+00:00"))
    total += 1
    ok += check("空 / 乱码返回 None",
                wm._iso_ts("") is None and wm._iso_ts("昨天") is None)
    # 2026-11-06T13:42:50Z 是 UTC，换算后应比本地 2026-11-06 13:42 早 8 小时
    total += 1
    ok += check("按 UTC 解析（不落到本机时区）",
                wm._iso_ts("2026-11-06T13:42:50Z")
                == wm._iso_ts("2026-11-06T13:42:50Z") - 0
                and wm._iso_ts("2026-11-06T13:42:50Z") % 86400 == 13 * 3600 + 42 * 60 + 50)

    total += 1
    ok += check("ingame/online/offline 的名次",
                (wm._status_info("ingame", ago()).get("rank"),
                 wm._status_info("ONLINE", ago()).get("rank"),
                 wm._status_info("offline", ago()).get("rank")) == (0, 1, 2))
    total += 1
    ok += check("缺字段不特殊对待（rank=3，仍按价格排）",
                wm._status_info(None, None).get("rank") == wm.RANK_UNKNOWN)
    total += 1
    ok += check("离线 45 天标记 stale",
                wm._status_info("offline", ago(45)).get("stale") is True)
    total += 1
    ok += check("离线 1 天不算 stale",
                wm._status_info("offline", ago(1)).get("stale") is False)
    total += 1
    ok += check("在线 45 天（异常数据）也不标 stale",
                wm._status_info("online", ago(45)).get("stale") is False)
    total += 1
    ok += check("天数计算正确（5 天前 -> 5）",
                wm._status_info("offline", ago(5, 3)).get("days") == 5)
    return ok, total


def test_sorting():
    print("[排序与代表价]")
    ok = total = 0
    rows = [
        row(10, "offline", "cheap_offline"),
        row(300, "online", "online_guy"),
        row(120, "ingame", "ingame_guy"),
        row(50, "offline", "stale_offline", days=90),
        row(200, "ingame", "ingame2"),
    ]
    rows, stats, samples, basis = wm._finish_rows(rows)
    total += 1
    ok += check("游戏中排最前（哪怕更贵）", rows[0]["seller"] == "ingame_guy")
    total += 1
    ok += check("前两个都是游戏中且价格升序",
                [r["seller"] for r in rows[:2]] == ["ingame_guy", "ingame2"])
    total += 1
    ok += check("在线排第三", rows[2]["seller"] == "online_guy")
    total += 1
    ok += check("离线里价格低的在前、stale 沉底",
                [r["seller"] for r in rows[3:]] == ["cheap_offline", "stale_offline"])
    total += 1
    ok += check("statistics 汇总正确",
                stats == {"ingame": 2, "online": 1, "offline": 2,
                          "unknown": 0, "stale": 1})
    total += 1
    ok += check("代表价取在线/游戏中的最低（120，而不是 10）",
                samples[0]["price"] == 120 and basis == "reachable")

    only_off = [row(10, "offline", "a"), row(20, "offline", "b", days=99)]
    _r, _s, smp, bs = wm._finish_rows(only_off)
    total += 1
    ok += check("全离线时退回全部报价（basis=all）", bs == "all" and smp[0]["price"] == 10)

    no_status = [{"seller": "x", "price": 9}, {"seller": "y", "price": 3}]
    _r, _s, smp, bs = wm._finish_rows(no_status)
    total += 1
    ok += check("老缓存无状态时仍按价格排", [r["price"] for r in _r] == [3, 9])
    total += 1
    ok += check("无状态数据也能算代表价", bs == "all" and smp[0]["price"] == 3)

    bid = [{"seller": "b1", "price": 1, "bidOnly": True, **wm._status_info("ingame", ago())},
           {"seller": "b2", "price": 900, "bidOnly": False, **wm._status_info("ingame", ago())}]
    _r, _s, _smp, _b = wm._finish_rows(bid)
    total += 1
    ok += check("同为游戏中：一口价排在仅竞价前面",
                [r["seller"] for r in _r] == ["b2", "b1"])
    return ok, total


def test_fetch_item():
    print("[fetch_item 接入]")
    ok = total = 0
    raw = [
        {"type": "sell", "platinum": 20, "quantity": 3,
         "user": {"ingameName": "Off", "status": "offline", "lastSeen": ago(3)}},
        {"type": "sell", "platinum": 99, "quantity": 1,
         "user": {"ingameName": "InGame", "status": "ingame", "lastSeen": ago()}},
        {"type": "sell", "platinum": 60, "quantity": 1,
         "user": {"ingameName": "On", "status": "online", "lastSeen": ago()}},
        {"type": "buy", "platinum": 1, "quantity": 1,
         "user": {"ingameName": "Buyer", "status": "ingame", "lastSeen": ago()}},
    ]
    with patch.object(wm, "http_get", lambda p, params=None: {"data": raw}), \
         patch.object(wm, "SOURCE", "warframe"), \
         patch.object(wm, "load_json", lambda p, d: {} if "slug" in str(p) else d), \
         patch.object(wm, "save_json", lambda p, o: None):
        r = wm.fetch_item("Boltor Prime")
    total += 1
    ok += check("只取 sell（不把收购单当在售）", r["count"] == 3)
    total += 1
    ok += check("第一条是游戏中的卖家", r["orders"][0]["seller"] == "InGame")
    total += 1
    ok += check("第二条是在线", r["orders"][1]["seller"] == "On")
    total += 1
    ok += check("离线报价排在最后", r["orders"][2]["seller"] == "Off")
    total += 1
    ok += check("代表价 99（在线最低），不是 20", r["price"] == 99)
    total += 1
    ok += check("带 seller 状态与天数",
                r["orders"][2]["status"] == "offline" and r["orders"][2]["days"] == 3)
    total += 1
    ok += check("stats 概览可用",
                r["stats"]["ingame"] == 1 and r["stats"]["online"] == 1
                and r["stats"]["offline"] == 1)
    return ok, total


def test_fetch_riven():
    print("[fetch_riven 接入]")
    ok = total = 0
    payload = {"payload": {"auctions": [
        {"buyout_price": None, "starting_price": 1, "closed": False,
         "owner": {"ingame_name": "Bidder", "status": "ingame", "last_seen": ago()}},
        {"buyout_price": 900, "starting_price": 500, "closed": False,
         "owner": {"ingame_name": "Buyout", "status": "ingame", "last_seen": ago()}},
        {"buyout_price": 5, "starting_price": 5, "closed": True,
         "owner": {"ingame_name": "Closed", "status": "ingame", "last_seen": ago()}},
        {"buyout_price": 700, "starting_price": 700, "closed": False,
         "owner": {"ingame_name": "Away", "status": "offline", "last_seen": ago(60)}},
    ]}}
    with patch.object(wm, "http_get", lambda p, params=None: payload), \
         patch.object(wm, "SOURCE", "warframe"):
        r = wm.fetch_riven("Tigris")
    total += 1
    ok += check("已流拍（closed）跳过", all(o["seller"] != "Closed" for o in r["orders"]))
    total += 1
    ok += check("一口价排在仅竞价前面",
                [o["seller"] for o in r["orders"]] == ["Buyout", "Bidder", "Away"])
    total += 1
    ok += check("仅竞价带 bidOnly 标记",
                r["orders"][1]["bidOnly"] is True and r["orders"][0]["bidOnly"] is False)
    total += 1
    ok += check("离线 60 天标记 stale 并沉底",
                r["orders"][-1]["stale"] is True)
    total += 1
    ok += check("代表价 900（游戏中一口价最低）", r["price"] == 900)
    return ok, total


def test_reply_text():
    print("[回复文案]")
    ok = total = 0

    def entry(orders, stats, basis, count=None):
        return {"type": "item", "name": "Boltor Prime", "zh": "螺钉步枪",
                "ts": int(time.time() * 1000),
                "lastUpdated": "2026-11-07T01:00:00Z",
                "source": "https://warframe.market/items/boltor_prime_set",
                "data": {"count": count or len(orders), "orders": orders,
                         "stats": stats, "priceBasis": basis}}

    orders = [row(99, "ingame", "InGame"), row(60, "online", "On"),
              row(20, "offline", "Off", days=3)]
    orders, stats, _s, basis = wm._finish_rows(orders)
    txt = wm.format_reply({"entry": entry(orders, stats, basis), "en": "Boltor Prime"})
    total += 1
    ok += check("概览行含三种状态计数", "🟢游戏中 1" in txt and "⚪离线 1" in txt)
    total += 1
    ok += check("说明排序规则", "游戏中 → 🔵在线 → ⚪离线" in txt or "🟢游戏中 → 🔵在线" in txt)
    total += 1
    ok += check("每条带状态图标", "1. 🟢 InGame" in txt and "3. ⚪ Off" in txt)
    total += 1
    ok += check("离线标出天数", "3天前在线" in txt)
    total += 1
    ok += check("/w 用游戏中的第一家", "/w InGame Hi! I want to buy" in txt)
    total += 1
    ok += check("有在线卖家时不出全离线警告", "全是离线报价" not in txt)

    # 图片 + 字符串：正文不压 /w 指令，发起聊天指令单独给出、且不带任何备注
    _res = {"entry": entry(orders, stats, basis), "en": "Boltor Prime"}
    body = wm.reply_body(_res)
    wh = wm.build_whisper(_res)
    total += 1
    ok += check("正文不压 /w 指令（留给同条消息的可复制文字）", "/w " not in body)
    total += 1
    ok += check("/w 指令单独给出", "/w InGame Hi! I want to buy" in wh)
    total += 1
    ok += check("/w 指令不带 (wf.m ...) 备注", "wf.m" not in wh and "RabbitBot" not in wh)
    total += 1
    ok += check("/w 字符串只有英中两行", len(wh.splitlines()) == 2)
    # 昵称上色映射：游戏中红 / 在线蓝 / 离线灰
    cols = wm.seller_colors(_res)
    total += 1
    ok += check("游戏中昵称=红", cols.get("InGame") == wm.STATUS_RGB["ingame"])
    total += 1
    ok += check("在线昵称=蓝", cols.get("On") == wm.STATUS_RGB["online"])
    total += 1
    ok += check("离线昵称=灰", cols.get("Off") == wm.STATUS_RGB["offline"])
    total += 1
    ok += check("颜色映射只含列出的卖家", set(cols) == {"InGame", "On", "Off"})

    only = [row(10, "offline", "a", days=2), row(20, "offline", "b", days=40)]
    only, stats2, _s2, basis2 = wm._finish_rows(only)
    txt2 = wm.format_reply({"entry": entry(only, stats2, basis2), "en": "Boltor Prime"})
    total += 1
    ok += check("全离线时明确警告", "全是离线报价" in txt2)
    total += 1
    ok += check("概览提示 30 天没登录的数量", "1 个 30 天没登录" in txt2)

    # 老缓存（没有 stats）不能崩，也不能瞎标状态
    old = {"type": "item", "name": "Boltor Prime", "zh": "",
           "ts": 0, "lastUpdated": "", "source": "",
           "data": {"count": 2, "orders": [{"seller": "Old", "price": 12, "quantity": 1}]}}
    txt3 = wm.format_reply({"entry": old, "en": "Boltor Prime"})
    total += 1
    ok += check("老缓存无状态也能正常输出", "Old — 12 铂金" in txt3 and "排序：" not in txt3)
    return ok, total


def test_mock():
    print("[mock 演示数据带状态]")
    ok = total = 0
    with patch.object(wm, "pace_gate", lambda d: None):
        r = wm.mock_fetch("item", "Boltor Prime")
    total += 1
    ok += check("mock 三条状态都有",
                r["stats"]["ingame"] > 0 and r["stats"]["online"] > 0
                and r["stats"]["offline"] > 0)
    total += 1
    ok += check("mock 第一条是游戏中",
                r["orders"][0]["status"] == "ingame")
    total += 1
    ok += check("mock 也标出 stale（45 天没登录）", r["stats"]["stale"] >= 1)
    total += 1
    ok += check("mock 代表价来自在线报价", r["priceBasis"] == "reachable")
    return ok, total


def main():
    ok = total = 0
    for fn in (test_iso_and_status, test_sorting, test_fetch_item,
               test_fetch_riven, test_reply_text, test_mock):
        o, t = fn()
        ok += o
        total += t
    print("\n结果：通过 %d / 失败 %d" % (ok, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
