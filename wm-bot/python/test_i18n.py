#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""i18n / 翻译指令与双语回复回归（离线、零联网；联网抓取用 mock 跳过）。

运行：python test_i18n.py
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("WM_SOURCE", "mock")
import wm

TMP = None


def setUpModule():
    global TMP
    TMP = Path(tempfile.mkdtemp(prefix="wm-i18n-test-"))
    # 把所有数据文件指向临时目录，避免污染真实 data/
    wm.DATA_DIR = TMP
    wm.CACHE_FILE = TMP / "cache.json"
    wm.PACE_FILE = TMP / "pace.json"
    wm.SLUG_FILE = TMP / "slug.json"
    wm._ensure_i18n()          # 把内置 starter 写进临时目录


class TestI18n(unittest.TestCase):
    def test_is_cjk(self):
        self.assertTrue(wm.is_cjk("螺钉步枪"))
        self.assertFalse(wm.is_cjk("Boltor"))

    def test_zh_to_en_seed(self):
        self.assertEqual(wm.translate_zh_to_en("螺钉步枪"), "Boltor")
        self.assertEqual(wm.translate_zh_to_en("猛虎"), "Tigris")

    def test_en_to_zh_seed(self):
        self.assertEqual(wm.translate_en_to_zh("Boltor"), "螺钉步枪")
        self.assertEqual(wm.translate_en_to_zh("Tigris"), "猛虎")

    def test_zh_to_en_missing(self):
        self.assertIsNone(wm.translate_zh_to_en("随便一个不存在的词"))

    def test_translate_chinese_name_has_reply(self):
        # 翻译 <中文名> 必须有回复：中英对照 + 双链接 + 边界说明
        out = wm.translate_command("螺钉步枪")
        self.assertIn("🔤 名称翻译：Boltor / 螺钉步枪", out)
        self.assertIn("英文：Boltor", out)
        self.assertIn("中文：螺钉步枪", out)
        self.assertIn("wiki.warframe.com", out)
        self.assertIn("warframe.huijiwiki.com", out)
        self.assertIn("仅名称翻译", out)

    def test_zh_prime_suffix_exact_match(self):
        # 中文名带 Prime 后缀必须命中 Prime 版，不能被基础名截胡
        self.assertEqual(wm.translate_zh_to_en("螺钉步枪Prime"), "Boltor Prime")
        self.assertEqual(wm.translate_zh_to_en("猛虎Prime"), "Tigris Prime")
        out = wm.translate_command("螺钉步枪Prime")
        self.assertIn("🔤 名称翻译：Boltor Prime / 螺钉步枪Prime", out)
        self.assertIn("wiki.warframe.com/w/Boltor%20Prime", out)

    def test_en_multiword_prefers_longest(self):
        # 英文多词也应命中更贴近的 Prime 版，而非基础版
        self.assertEqual(wm.translate_en_to_zh("Boltor Prime Set"), "螺钉步枪Prime")

    def test_translate_command_cached(self):
        out = wm.translate_command("Boltor")
        self.assertIn("螺钉步枪", out)
        self.assertIn("wiki.warframe.com", out)
        self.assertIn("warframe.huijiwiki.com", out)

    def test_translate_command_usage(self):
        self.assertIn("用法", wm.translate_command(""))

    @patch.object(wm, "_wiki_fetch", return_value=None)   # 跳过 wiki 联网；自动还原
    @patch.object(wm, "sync_lexicon", return_value=(False, "词库同步失败：测试跳过联网"))
    def test_translate_command_unknown_blocked(self, _sync, _wiki):
        out = wm.translate_command("TotallyUnknownThing123")
        self.assertIn("Cloudflare", out)

    def test_query_chinese_name(self):
        r = wm.query("WM 螺钉步枪")
        self.assertEqual(r.get("served"), "fresh")
        self.assertEqual(r.get("en"), "Boltor")
        self.assertEqual(r.get("zh"), "螺钉步枪")

    def test_query_english_name_zh(self):
        r = wm.query("WM Boltor")
        self.assertEqual(r.get("served"), "fresh")
        self.assertEqual(r.get("en"), "Boltor")
        self.assertEqual(r.get("zh"), "螺钉步枪")

    def test_query_chinese_unknown(self):
        r = wm.query("WM 完全不存在的中文名xyz")
        self.assertEqual(r.get("served"), "error")
        self.assertIn("中英对照", r.get("errorMsg", ""))

    def test_format_reply_bilingual(self):
        entry = {
            "type": "item", "name": "Boltor", "zh": "螺钉步枪",
            "data": {"price": 40, "median": 59, "unit": "铂金", "count": 1,
                     "orders": [{"seller": "Zephroste", "price": 40, "quantity": 1}]},
            "source": "https://warframe.market/items/boltor_prime_set",
            "lastUpdated": "2026-11-06T00:00:00Z", "ts": 0, "status": "ok",
        }
        txt = wm.format_reply({"served": "cache", "entry": entry, "fresh": False,
                               "en": "Boltor", "zh": "螺钉步枪"})
        self.assertIn("【物品】Boltor / 螺钉步枪", txt)
        self.assertIn(
            "/w Zephroste Hi! I want to buy: Boltor for 40 platinum.", txt)
        self.assertIn(
            "/w Zephroste 你好，我想购买 螺钉步枪 40 铂金.", txt)
        # /w 指令结尾不带任何来源备注 / 机器人名后缀（用户明确不要，防止回归）
        self.assertNotIn("(wf.m via RabbitBot)", txt)
        self.assertNotIn("RabbitBot", txt)
        self.assertNotIn("wf.m via", txt)
        self.assertIn("来源：https://warframe.market/items/boltor_prime_set", txt)

    def test_format_reply_no_zh_falls_back(self):
        entry = {
            "type": "item", "name": "Rhino", "zh": None,
            "data": {"price": 10, "median": 12, "unit": "铂金", "count": 1,
                     "orders": [{"seller": "X", "price": 10, "quantity": 1}]},
            "source": "https://warframe.market/items/rhino",
            "lastUpdated": "2026-11-06T00:00:00Z", "ts": 0, "status": "ok",
        }
        txt = wm.format_reply({"served": "cache", "entry": entry, "fresh": False,
                               "en": "Rhino", "zh": None})
        self.assertIn("【物品】Rhino", txt)     # 无独立中文名 -> 不显示 / 中文
        self.assertNotIn("/ Rhino", txt)

    def test_get_cached_or_refresh_chinese(self):
        r = wm.get_cached_or_refresh("item", "螺钉步枪")
        self.assertEqual(r.get("served"), "fresh")
        self.assertEqual(r.get("en"), "Boltor")

    # ---------- 「翻译」主指令（wiki 页面正文不做，只做名称翻译）----------
    def test_translate_command_parse(self):
        import qq_common
        self.assertEqual(wm.parse_command("翻译 Boltor")["name"], "Boltor")
        self.assertEqual(wm.parse_command("翻译 螺钉步枪")["name"], "螺钉步枪")
        self.assertEqual(wm.parse_command("/翻译 Boltor")["type"], "translate")
        # 「wiki」指令已删除：不再触发、不再解析成翻译
        self.assertFalse(qq_common.is_wm_command("wiki Boltor"))
        self.assertFalse(qq_common.is_wm_command("/wiki Boltor"))
        self.assertNotEqual(wm.parse_command("wiki Boltor")["type"], "translate")
        self.assertTrue(qq_common.is_wm_command("翻译 螺钉步枪"))
        self.assertTrue(qq_common.is_wm_command("/翻译 Boltor"))

    def test_translate_reply_is_name_only(self):
        out = wm.translate_command("Boltor")
        self.assertIn("🔤 名称翻译：Boltor / 螺钉步枪", out)
        self.assertIn("仅名称翻译", out)
        self.assertIn("想看获取方式 / 锻造材料等 wiki 正文，请点", out)
        # 明确不假装提供正文：用法说明里写清边界
        self.assertIn("正文机器人抓不到", wm.translate_command(""))


# ---------- 离线验证 MediaWiki /api.php 解析逻辑（不依赖联网）----------
import urllib.parse as _up

_BOLTOR_JSON = ('{"batchcomplete":"","query":{"redirects":[{"from":"Boltor","to":"螺钉步枪"}],'
                '"pages":{"3920":{"pageid":3920,"ns":0,"title":"螺钉步枪",'
                '"langlinks":[{"lang":"en","*":"Boltor"}]}}}}')
_HAIKE_JSON = ('{"batchcomplete":"","query":{"pages":{"3794":{"pageid":3794,"ns":0,'
               '"title":"海克","langlinks":[{"lang":"en","*":"Hek"}]}}}}')
_RHINO_JSON = ('{"batchcomplete":"","query":{"pages":{"123":{"pageid":123,"ns":0,'
               '"title":"Rhino"}}}}')
_MISSING_JSON = ('{"batchcomplete":"","query":{"pages":{"-1":{"ns":0,"title":"Nope",'
                 '"missing":"","contentmodel":"wikitext"}}}}')
_SEARCH_BOSHI_JSON = ('{"batchcomplete":"","query":{"searchinfo":{"totalhits":3},'
                      '"search":[{"ns":0,"title":"伯斯顿","pageid":1},'
                      '{"ns":0,"title":"伯斯顿Prime","pageid":2}]}}')

class _FakeResp:
    def __init__(self, body):
        self._b = body.encode("utf-8")
    def read(self):
        return self._b
    @property
    def headers(self):
        return {"Content-Encoding": None}
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def _fake_urlopen(url, *a, **k):
    if hasattr(url, "full_url"):   # urlopen 可能收到 Request 对象而非字符串
        url = url.full_url
    q = _up.parse_qs(_up.urlparse(url).query)
    if q.get("list") == ["search"]:
        return _FakeResp(_SEARCH_BOSHI_JSON)
    titles = (q.get("titles") or [""])[0]
    if titles == "Boltor":
        return _FakeResp(_BOLTOR_JSON)
    if titles == "海克":
        return _FakeResp(_HAIKE_JSON)
    if titles == "Rhino":
        return _FakeResp(_RHINO_JSON)
    if titles == "伯斯顿":
        return _FakeResp('{"batchcomplete":"","query":{"pages":{"1":{"pageid":1,"ns":0,'
                         '"title":"伯斯顿","langlinks":[{"lang":"en","*":"Burston"}]}}}}')
    return _FakeResp(_MISSING_JSON)

class TestWikiApiParse(unittest.TestCase):
    @patch("urllib.request.urlopen", _fake_urlopen)
    def test_langlinks_en_to_zh(self):
        # 英文 Boltor -> 跳转中文规范标题 + 英文 langlink
        r = wm._wiki_api_langlinks("Boltor")
        self.assertEqual(r, {"en": "Boltor", "zh": "螺钉步枪"})

    @patch("urllib.request.urlopen", _fake_urlopen)
    def test_langlinks_zh_to_en(self):
        # 中文 海克 -> 中文规范标题 + 英文 langlink
        r = wm._wiki_api_langlinks("海克")
        self.assertEqual(r, {"en": "Hek", "zh": "海克"})

    @patch("urllib.request.urlopen", _fake_urlopen)
    def test_langlinks_warframe_no_zh(self):
        # 战甲 Rhino：英文标题、无 zh langlink -> zh 为 None（无官方中文名）
        r = wm._wiki_api_langlinks("Rhino")
        self.assertEqual(r, {"en": "Rhino", "zh": None})

    @patch("urllib.request.urlopen", _fake_urlopen)
    def test_langlinks_missing(self):
        # 页面不存在 -> 返回 _WIKI_MISSING 标记（触发搜索兜底）
        r = wm._wiki_api_langlinks("Nope")
        self.assertIs(r, wm._WIKI_MISSING)

    @patch("urllib.request.urlopen", _fake_urlopen)
    def test_search_returns_title(self):
        self.assertEqual(wm._wiki_api_search("伯斯顿"), "伯斯顿")

    @patch("urllib.request.urlopen", _fake_urlopen)
    def test_fetch_via_search_fallback(self):
        # 直查未命中 -> 搜索找 伯斯顿 -> 再查 langlinks 拿到 Burston/伯斯顿
        r = wm._wiki_fetch("GhostName")
        self.assertEqual(r, {"en": "Burston", "zh": "伯斯顿"})


# ---------- 离线验证全量词库（WFA_Lexicon）解析 / 同步 / 兜底 ----------
import contextlib

_LEX_DICT = [
    {"id": 904, "type": "Weapon",   "zh": "螺钉步枪", "en": "Boltor"},
    {"id": 853, "type": "Weapon",   "zh": "海克",     "en": "Hek"},
    {"id": 861, "type": "Weapon",   "zh": "猛虎",     "en": "Tigris"},
    {"id": 1536, "type": "Warframe", "zh": "RHINO",   "en": "RHINO"},
    {"id": 1554, "type": "Warframe", "zh": "犀牛",    "en": "RHINO"},   # 社区俗称，必须忽略
    {"id": 1566, "type": "Warframe", "zh": "咖喱",    "en": "EXCALIBUR"},
    {"id": 9999, "type": "Weapon",   "zh": "测试之刃", "en": "Testblade"},  # 不在内置 seed，用于测兜底
]
_LEX_SALE = [
    {"id": 235, "type": "Weapon", "search": "boltor_prime_set",
     "zh": "螺钉步枪 PRIME 一套", "en": "Boltor Prime Set"},
    {"id": 1280, "type": "Warframe", "search": "rhino_prime_set",
     "zh": "犀牛 PRIME 一套", "en": "RHINO PRIME SET"},
]

@contextlib.contextmanager
def _fresh_data():
    old = wm.DATA_DIR
    wm.DATA_DIR = Path(tempfile.mkdtemp(prefix="wm-lex-test-"))
    try:
        yield wm.DATA_DIR
    finally:
        wm.DATA_DIR = old

class TestLexicon(unittest.TestCase):
    def test_build_weapon_zh(self):
        entries, stat = wm._lexicon_build(_LEX_DICT, _LEX_SALE)
        self.assertEqual(entries["boltor"]["zh"], "螺钉步枪")
        self.assertEqual(entries["hek"]["zh"], "海克")
        self.assertEqual(entries["boltor"]["src"], "wfa-lexicon")

    def test_build_warframe_no_zh(self):
        # 战甲无官方中文名：词库里的「犀牛/咖喱」是社区俗称，一律不收
        entries, stat = wm._lexicon_build(_LEX_DICT, _LEX_SALE)
        self.assertIsNone(entries["rhino"]["zh"])
        self.assertIsNone(entries["excalibur"]["zh"])
        self.assertGreaterEqual(stat["warframe_no_zh"], 3)
        # 词库里战甲英文是全大写，展示前规范化
        self.assertEqual(entries["rhino"]["en"], "Rhino")

    def test_build_sale_slug_and_no_suffix_zh(self):
        entries, stat = wm._lexicon_build(_LEX_DICT, _LEX_SALE)
        # Sale 独有条目带「一套」后缀，不作为展示中文名，但保留 WM slug
        self.assertIsNone(entries["boltor prime set"]["zh"])
        self.assertEqual(entries["boltor prime set"]["slug"], "boltor_prime_set")
        self.assertGreaterEqual(stat["slugs"], 1)

    def test_sync_and_lookup_offline(self):
        # 用样本替换真实联网；验证落盘 + 精确查找（英文/中文/slug）
        with _fresh_data():
            with patch.object(wm, "_lexicon_fetch_json",
                              side_effect=lambda fn: _LEX_DICT if "Dict" in fn else _LEX_SALE):
                ok, msg = wm.sync_lexicon()
            self.assertTrue(ok, msg)
            self.assertIn("同步完成", msg)
            self.assertTrue(wm._lexicon_path().exists())
            self.assertEqual(wm.lookup_lexicon("螺钉步枪")["en"], "Boltor")
            self.assertEqual(wm.lookup_lexicon("Hek")["zh"], "海克")
            self.assertEqual(wm.lookup_lexicon("boltor_prime_set")["en"], "Boltor Prime Set")
            # 同步后，中文查价的翻译链也能走通全量词库
            self.assertEqual(wm.translate_zh_to_en("猛虎"), "Tigris")

    def test_translate_sync_command(self):
        with patch.object(wm, "sync_lexicon", return_value=(True, "✅ 词库同步完成：测试")):
            self.assertIn("同步完成", wm.translate_command("同步"))
            self.assertIn("同步完成", wm.translate_command("sync"))

    def test_translate_unknown_auto_sync_fallback(self):
        # wiki 被挡 -> 首次自动同步词库 -> 从词库联网查到并缓存
        with _fresh_data():
            def fake_sync():
                entries, _ = wm._lexicon_build(_LEX_DICT, _LEX_SALE)
                wm.DATA_DIR.mkdir(parents=True, exist_ok=True)
                wm.save_json(wm._lexicon_path(), {"entries": entries})
                return True, "ok"
            with patch.object(wm, "_wiki_fetch", return_value=None), \
                 patch.object(wm, "sync_lexicon", side_effect=fake_sync):
                out = wm.translate_command("Testblade")   # 不在内置 seed -> 必然走兜底链
            self.assertIn("已从词库联网查到", out)
            self.assertIn("测试之刃", out)
            self.assertIn("wiki.warframe.com", out)


if __name__ == "__main__":
    unittest.main()
