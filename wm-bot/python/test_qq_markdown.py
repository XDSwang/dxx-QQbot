#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_qq_markdown.py — Markdown 消息通道离线测试（不发真实 QQ 请求）

覆盖：
  1) 配置模式：默认 native / off / template 缺 ID 降级 / auto / 回退开关；
  2) 内容拆分：ImageReply 与纯文本回退、/w 提取不重复、标题与来源 URL；
  3) 原生 Markdown：★ 有图时只输出「图片 + 两条 /w」，不重复标题/正文/来源；
     无图纯文字回退仍输出完整正文、qqbot-media:// 引用、/w 原样不转义、
     长度截断保留 /w、图片尺寸按比例；
  4) 模板 Markdown：官方示例八个 key、values 为数组、image/content/link；
  5) 素材管理三步：prepare 字段（md5/sha1/size/content_type）、PUT 二进制且
     不带 QQBot 头、complete 拿 url、缓存命中不重传、超限删除旧素材；
  6) 图片来源优先级：静态 URL > 官方素材 > 自备图床 > 无图；
  7) 请求体：msg_type=2 / markdown / msg_id 有无 / 群与单聊路径；
  8) 接入链路：_dispatch_reply 开启时只发一条 md、失败自动回退富媒体、
     开关关闭不尝试、WM_QQ_MD_FALLBACK=0 不再发原通道；
     ★ 素材拿不到引用时绝不发「无图纯文字」，直接回退富媒体发图、不重复上传；
  9) 覆盖式固定公网图：通用 PUT / GitHub sha 覆盖、只占一张图、版本号破缓存、
     优先级高于官方素材、失败回落素材，以及 md_probe 最小用例 dry-run。

被 `python -m unittest discover` 误导入时自动跳过（本文件是脚本式，不是 TestCase）。
"""
if __name__ != "__main__":
    import unittest
    raise unittest.SkipTest("脚本式测试，请直接 python test_qq_markdown.py 运行")

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["WM_SOURCE"] = "mock"
os.environ.setdefault("WM_QQ_MARKDOWN", "native")

_TMP = tempfile.TemporaryDirectory()
os.environ["WM_QQ_MD_MEDIA_CACHE"] = os.path.join(_TMP.name, "md_images.json")
os.environ["WM_QQ_MD_IMAGE_STATE"] = os.path.join(_TMP.name, "md_image_state.json")
os.environ["WM_QQ_MD_IMAGE_URL"] = ""
os.environ["WM_QQ_MD_IMAGE_UPLOAD"] = ""
os.environ["WM_QQ_MD_TEMPLATE_ID"] = ""
os.environ["WM_QQ_MD_IMAGE_PUT_URL"] = ""
os.environ["WM_QQ_MD_GITHUB_REPO"] = ""
os.environ["WM_QQ_MD_GITHUB_TOKEN"] = ""

import qq_markdown as M
import qq_common
import qq_official

OK = [0, 0]


def check(desc, cond):
    OK[0] += 1
    if not cond:
        OK[1] += 1
        print("  FAIL: " + desc)


def section(name):
    print("\n--- " + name + " ---")


PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
       + b"\x00\x00\x04\x00" + b"\x00\x00\x08\x00" + b"\x00" * 40)

BODY = ("【紫卡】Tigris / 猛虎\n"
        "WM 在售（共 100 条：🟢游戏中 10 · 🔵在线 6 · ⚪离线 84）\n"
        "排序：🟢游戏中 → 🔵在线 → ⚪离线\n"
        "1. 🟢 -Ar.Luna- — 800 铂金 ×1\n"
        "2. 🔵 Burn*marks — 999 铂金 ×1\n"
        "🕒 数据更新于 2026-11-07T06:28:42Z · "
        "来源：https://warframe.market/auctions/riven/tigris")
WHISPER = ("英文：/w -Ar.Luna- Hi! I want to buy: Tigris for 800 platinum.\n"
           "中文：/w -Ar.Luna- 你好，我想购买 猛虎 800 铂金.")


def image_reply():
    r = qq_common.ImageReply(BODY, [PNG])
    r.tail = WHISPER
    return r


def plain_reply():
    return BODY + "\n\n" + WHISPER


def clean_cache():
    for key in ("WM_QQ_MD_MEDIA_CACHE", "WM_QQ_MD_IMAGE_STATE"):
        p = Path(os.environ[key])
        if p.exists():
            p.unlink()


def fake_http(handler):
    """把 config.urlopen 换成记录 Request 的假实现；handler(req)->dict/str。"""
    calls = []

    def _fake(req, timeout=10):
        calls.append(req)
        resp = handler(req)
        raw = resp if isinstance(resp, (str, bytes)) else json.dumps(resp)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return io.BytesIO(raw)

    return calls, _fake


def media_handler(req):
    """素材管理三步 + 删除的标准响应。"""
    url = req.full_url
    if url.endswith("/v2/media-images/prepare"):
        return {"upload_id": "U1",
                "presigned_url": "https://cos.example.com/put",
                "expires_in": 3600}
    if url.endswith("/v2/media-images/complete"):
        return {"image_id": "I1",
                "url": "qqbot-media://image/v1/TOKEN123",
                "ttl": 7776000}
    if "/delete" in url:
        return {}
    if "cos.example.com" in url:
        return {}
    return {"id": "MSG_OK"}


# ---------------------------------------------------------------- 1
section("1) 配置模式")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "native"}):
    check("默认/显式 native", M.mode() == "native" and M.enabled())
    check("native 解析为 native", M.resolved_mode() == "native")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "0"}):
    check("0 = 关闭", M.mode() == "off" and not M.enabled())
    check("关闭时 resolved=off", M.resolved_mode() == "off")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "template",
                             "WM_QQ_MD_TEMPLATE_ID": ""}):
    check("模板缺 ID 自动降级原生", M.resolved_mode() == "native")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "template",
                             "WM_QQ_MD_TEMPLATE_ID": "101993071_1658748972"}):
    check("有 ID 走模板", M.resolved_mode() == "template")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "auto",
                             "WM_QQ_MD_TEMPLATE_ID": "TPL1"}):
    check("auto + ID → 模板", M.resolved_mode() == "template")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "auto",
                             "WM_QQ_MD_TEMPLATE_ID": ""}):
    check("auto 无 ID → 原生", M.resolved_mode() == "native")
with patch.dict(os.environ, {"WM_QQ_MD_FALLBACK": "1"}):
    check("默认允许回退", M.fallback_enabled())
with patch.dict(os.environ, {"WM_QQ_MD_FALLBACK": "0"}):
    check("可关闭回退", not M.fallback_enabled())

# ---------------------------------------------------------------- 2
section("2) 内容拆分")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "native"}):
    t, b, w, src = M.split_reply(image_reply())
    check("标题取首行", t == "【紫卡】Tigris / 猛虎")
    check("来源 URL 抽出", src == "https://warframe.market/auctions/riven/tigris")
    check("/w 来自 tail", w == WHISPER)
    check("正文不含来源行", all("来源：" not in x for x in b))
    check("正文不含 /w", all("/w " not in x for x in b))

    t2, b2, w2, src2 = M.split_reply(plain_reply())
    check("纯文本回退也能抽出 /w", w2 == WHISPER)
    check("纯文本回退不重复 /w", sum(1 for x in b2 if "/w " in x) == 0)
    check("纯文本回退标题一致", t2 == t)

# ---------------------------------------------------------------- 3
section("3) 原生 Markdown 版式")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "native",
                             "WM_QQ_MD_MAX_CHARS": "3000"}):
    md = M.build_native("【紫卡】Tigris / 猛虎",
                        ["WM 在售（共 100 条）", "1. 🟢 -Ar.Luna- — 800 铂金 ×1"],
                        WHISPER,
                        "https://warframe.market/auctions/riven/tigris",
                        "qqbot-media://image/v1/TOKEN123",
                        "#618px #1236px")
    lines = md.splitlines()
    i_img = next(i for i, x in enumerate(lines) if "![img" in x)
    i_w = next(i for i, x in enumerate(lines) if x.startswith("英文：/w"))
    check("★ 有图时不额外放大标题（卡片图里已有）", not md.startswith("# "))
    check("图片标签含 qqbot-media 引用",
          "![img #618px #1236px](qqbot-media://image/v1/TOKEN123)" in md)
    check("图片是消息第一行", i_img == 0)
    check("★ 图下不再重复正文（WM 在售 / 排序 / 订单列表）",
          "WM 在售" not in md and "🟢 -Ar.Luna-" not in md and "排序" not in md)
    check("★ 图下不再重复来源链接", "🔗" not in md and "warframe.market" not in md)
    check("/w 字符串在图片下方", i_w > i_img)
    check("/w 指令原样可复制（无反斜杠）",
          "英文：/w -Ar.Luna- Hi! I want to buy: Tigris for 800 platinum." in md)
    check("中文 /w 也在", "中文：/w -Ar.Luna- 你好，我想购买 猛虎 800 铂金." in md)
    check("★ 整条只有「图片 + 两条 /w」",
          len([x for x in lines if x.strip()]) == 3)
    check("正文元字符被转义（无图纯文字回退）", r"Burn\*marks" in M.build_native(
        "T", ["2. 🔵 Burn*marks — 999 铂金 ×1"], "", "", "", ""))

    # 没图（「文字 xxx」纯文字回退）：完整标题 + 正文 + /w + 来源，一个都不能丢
    plain_md = M.build_native(
        "【紫卡】Tigris / 猛虎",
        ["WM 在售（共 100 条）", "1. 🟢 -Ar.Luna- — 800 铂金 ×1"],
        WHISPER, "https://warframe.market/auctions/riven/tigris", "", "")
    check("无图仍输出标题与正文",
          "# 【紫卡】" in plain_md and "WM 在售" in plain_md)
    check("无图仍输出 /w 与来源链接",
          WHISPER in plain_md and "🔗 warframe.market" in plain_md)

    long = M.build_native("T", ["X" * 4000], WHISPER, "", "")
    check("超长时截断", "已截断" in long)
    check("截断仍保留 /w", WHISPER in long)

    # @ 了机器人才 @ 回去：标签写在消息最前面，正文 / 图 / /w 一格不动
    at_md = M.build_native("T", ["X"], WHISPER, "", "https://x/y.png",
                           "#618px #900px", at_tag='<qqbot-at-user id="MO1" />')
    at_lines = at_md.splitlines()
    check("@ 标签在消息最前面", at_lines[0] == '<qqbot-at-user id="MO1" />')
    check("@ 标签下一行才是图片", "![img" in at_lines[1])
    check("不传 at_tag 时没有 @ 标签", "<qqbot-at-user" not in md)

    w, h = M._png_size(PNG)
    check("PNG 宽高解析", (w, h) == (1024, 2048))
    check("图片尺寸按比例换算", M.image_markup_size(PNG) == "#618px #1236px")

# ---------------------------------------------------------------- 4
section("4) 模板 Markdown 八键")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "template",
                             "WM_QQ_MD_TEMPLATE_ID": "101993071_1658748972"}):
    mode, obj, _ = M.build_markdown(image_reply(), pngs=[PNG], tail=WHISPER,
                                    image="qqbot-media://image/v1/TOKEN123")
    check("走模板模式", mode == "template")
    check("custom_template_id 正确",
          obj["custom_template_id"] == "101993071_1658748972")
    keys = [p["key"] for p in obj["params"]]
    check("八个 key 齐全且顺序固定", keys == list(M.TEMPLATE_KEYS))
    check("所有 values 都是数组",
          all(isinstance(p["values"], list) and len(p["values"]) == 1
              for p in obj["params"]))
    v = {p["key"]: p["values"][0] for p in obj["params"]}
    check("image 填素材引用", v["image"] == "qqbot-media://image/v1/TOKEN123")
    check("★ 模板标题不与卡片内物品名重复", "Tigris" not in v["title"])
    check("content 含 /w 指令", "/w -Ar.Luna-" in v["content"])
    check("★ 模板 content 只留 /w、不重复正文", "WM 在售" not in v["content"])
    check("★ 模板 para/desc 留空不重复正文",
          v["para1"] == "" and v["para2"] == "" and v["desc"] == "")
    check("link 填来源网址", v["link"].startswith("https://warframe.market/"))
    # 模板变量是纯文本，@ 标签塞不进去：宁可这条通道不 @，也不要显示成一段标签原文
    mode_t, obj_t, _ = M.build_markdown(
        image_reply(), pngs=[PNG], tail=WHISPER,
        image="qqbot-media://image/v1/TOKEN123",
        at_tag='<qqbot-at-user id="MO1" />')
    check("模板模式忽略 @ 标签",
          "qqbot-at-user" not in json.dumps(obj_t, ensure_ascii=False))
    check("link_introduction 是域名", v["link_introduction"] == "warframe.market")

# ---------------------------------------------------------------- 5
section("5) 官方素材管理三步上传")
clean_cache()
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "native"}):
    calls, fake = fake_http(media_handler)
    with patch.object(M.bot_config, "urlopen", fake):
        url = M.upload_media_image(PNG, "TOK", "APP", "https://api.bot.qq.com")
    check("拿到 qqbot-media 引用", url == "qqbot-media://image/v1/TOKEN123")
    methods = [(r.get_method(), r.full_url) for r in calls]
    check("三步 + 顺序正确",
          [m for m, _ in methods] == ["POST", "PUT", "POST"])
    check("prepare 路径", methods[0][1].endswith("/v2/media-images/prepare"))
    check("PUT 走预签名 URL", "cos.example.com" in methods[1][1])
    check("complete 路径", methods[2][1].endswith("/v2/media-images/complete"))

    prep = json.loads(calls[0].data.decode("utf-8"))
    import hashlib
    check("prepare 带 file_size", prep["file_size"] == len(PNG))
    check("prepare 带 md5", prep["md5"] == hashlib.md5(PNG).hexdigest())
    check("prepare 带 sha1", prep["sha1"] == hashlib.sha1(PNG).hexdigest())
    check("prepare 带 content_type", prep["content_type"] == "image/png")
    check("prepare 文件名是 png", prep["file_name"] == "card.png")
    check("PUT 是原始二进制", calls[1].data == PNG)
    check("PUT Content-Type 一致",
          calls[1].headers.get("Content-type") == "image/png"
          or calls[1].get_header("Content-type") == "image/png")
    check("PUT 不带 QQBot 鉴权头",
          "Authorization" not in dict(calls[1].headers))
    done = json.loads(calls[2].data.decode("utf-8"))
    check("complete 带 upload_id/md5",
          done["upload_id"] == "U1" and done["md5"] == prep["md5"])

    caches = json.loads(Path(os.environ["WM_QQ_MD_MEDIA_CACHE"]).read_text("utf-8"))
    check("缓存写入", len(caches) == 1 and "TOKEN123" in json.dumps(caches))

    # 第二次：应命中缓存，一步 HTTP 都不发
    calls2, fake2 = fake_http(media_handler)
    with patch.object(M.bot_config, "urlopen", fake2):
        url2 = M.upload_media_image(PNG, "TOK", "APP", "https://api.bot.qq.com")
    check("缓存命中返回同一引用", url2 == url)
    check("缓存命中不重复上传", len(calls2) == 0)

# 素材上传失败不抛、返回空串
clean_cache()
def bad_handler(req):
    return {"code": 40035007, "message": "quota"}
with patch.object(M.bot_config, "urlopen", fake_http(bad_handler)[1]):
    check("prepare 失败返回空串",
          M.upload_media_image(PNG, "TOK", "APP", "https://api.bot.qq.com") == "")

# 超配额自动删除最旧素材
clean_cache()
cache = {}
for i in range(5):
    cache[f"k{i}"] = {"url": f"qqbot-media://image/v1/T{i}",
                      "image_id": f"I{i}", "at": float(i)}
M._cache_save(cache)
with patch.dict(os.environ, {"WM_QQ_MD_MEDIA_MAX": "3"}), \
     patch.object(M, "delete_media_image", return_value=True) as dele:
    M._prune_cache(cache, "TOK", "APP", "https://api.bot.qq.com")
    check("超限裁到配额内", len(cache) == 3)
    check("删的是最旧的", dele.call_count == 2)
    check("保留的是最新的", set(cache) == {"k2", "k3", "k4"})

# 平台删除失败时不能把账本丢掉，否则那张旧素材永远清不掉
clean_cache()
cache = {}
for i in range(4):
    cache[f"k{i}"] = {"url": f"qqbot-media://image/v1/T{i}",
                      "image_id": f"I{i}", "at": float(i)}
with patch.dict(os.environ, {"WM_QQ_MD_MEDIA_MAX": "3"}), \
     patch.object(M, "delete_media_image", return_value=False):
    M._prune_cache(cache, "TOK", "APP", "https://api.bot.qq.com")
    check("删除失败保留账本（下次还会重试）",
          len(cache) == 4 and "k0" in cache)
_saved_max = os.environ.pop("WM_QQ_MD_MEDIA_MAX", None)
check("默认清理阈值是 3 张", M.media_max() == 3)
if _saved_max is not None:
    os.environ["WM_QQ_MD_MEDIA_MAX"] = _saved_max

# ---------------------------------------------------------------- 6
section("6) 图片来源优先级")
clean_cache()
with patch.dict(os.environ, {"WM_QQ_MD_IMAGE_URL": "https://cdn.example/card.png",
                             "WM_QQ_MD_IMAGE_UPLOAD": ""}):
    u_static = M.resolve_image_url([PNG], "TOK", "APP", "https://api.bot.qq.com")
    check("静态 URL 优先且不上传",
          u_static.startswith("https://cdn.example/card.png"))
    check("固定 URL 默认带 ?v= 破 CDN 缓存", "?v=" in u_static)
with patch.dict(os.environ, {"WM_QQ_MD_IMAGE_URL": "",
                             "WM_QQ_MD_IMAGE_UPLOAD": ""}):
    clean_cache()
    calls, fake = fake_http(media_handler)
    with patch.object(M.bot_config, "urlopen", fake):
        u = M.resolve_image_url([PNG], "TOK", "APP", "https://api.bot.qq.com")
    check("无静态 URL 时走官方素材", u == "qqbot-media://image/v1/TOKEN123")
    check("素材上传确实发了请求", len(calls) == 3)
    check("没图时返回空串", M.resolve_image_url([]) == "")

# 自备图床（官方素材之外的备选）
clean_cache()
def bed_handler(req):
    if "cos.example" in req.full_url:
        return {}
    if req.full_url.endswith("/prepare"):
        return {"code": 40035007}
    return {"data": {"url": "https://bed.example/x.png"}}
with patch.dict(os.environ, {"WM_QQ_MD_IMAGE_URL": "",
                             "WM_QQ_MD_IMAGE_UPLOAD": "https://bed.example/up"}):
    with patch.object(M.bot_config, "urlopen", fake_http(bed_handler)[1]):
        u = M.upload_png(PNG)
    check("自备图床可解析 data.url", u == "https://bed.example/x.png")

# ---------------------------------------------------------------- 7
section("7) 请求体")
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "native"}):
    calls, fake = fake_http(lambda req: {"id": "OK"})
    with patch.object(M.bot_config, "urlopen", fake):
        ok, info = M.send("TOK", "APP", "https://api.bot.qq.com", "group",
                          "G1", {"content": "# 标题"}, msg_id="M1")
        ok2, _ = M.send("TOK", "APP", "https://api.bot.qq.com", "user",
                        "U1", {"content": "# 标题"}, msg_id=None)
    check("群发送成功", ok)
    b1 = json.loads(calls[0].data.decode("utf-8"))
    b2 = json.loads(calls[1].data.decode("utf-8"))
    check("msg_type = 2", b1["msg_type"] == 2)
    check("markdown 字段存在", b1["markdown"]["content"] == "# 标题")
    check("被动回复带 msg_id", b1["msg_id"] == "M1")
    check("不放 event_id", "event_id" not in b1)
    check("主动/普通消息不带 msg_id", "msg_id" not in b2)
    check("群路径", calls[0].full_url.endswith("/v2/groups/G1/messages"))
    check("单聊路径", calls[1].full_url.endswith("/v2/users/U1/messages"))
    check("带 QQBot 鉴权头",
          calls[0].headers.get("Authorization") == "QQBot TOK"
          or calls[0].get_header("Authorization") == "QQBot TOK")
    check("带 X-Union-Appid", "APP" in str(calls[0].headers))

# ---------------------------------------------------------------- 8
section("8) send_reply 与接入链路")
clean_cache()
with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "native",
                             "WM_QQ_MD_IMAGE_URL": "",
                             "WM_QQ_MD_IMAGE_UPLOAD": ""}):
    calls, fake = fake_http(media_handler)
    with patch.object(M.bot_config, "urlopen", fake):
        ok, info, mode = M.send_reply(
            "TOK", "APP", "https://api.bot.qq.com", "group", "G1",
            image_reply(), msg_id="M1", pngs=[PNG], tail=WHISPER)
    check("send_reply 成功", ok and mode == "native")
    msg_calls = [r for r in calls if r.full_url.endswith("/messages")]
    check("只发一条消息", len(msg_calls) == 1)
    body = json.loads(msg_calls[0].data.decode("utf-8"))
    check("该条是 msg_type=2", body["msg_type"] == 2)
    check("同条内既有图片又有 /w",
          "qqbot-media://" in body["markdown"]["content"]
          and "/w -Ar.Luna-" in body["markdown"]["content"])
    check("★ 真实接入链路里图下不重复正文",
          "WM 在售" not in body["markdown"]["content"]
          and "🔗" not in body["markdown"]["content"])
    check("不带 @ 占位符", "<@" not in body["markdown"]["content"])

    # @ 了机器人：同一条 markdown 最前面多一行官方 @ 用户标签（member_openid）
    clean_cache()
    calls_at, fake_at = fake_http(media_handler)
    with patch.object(M.bot_config, "urlopen", fake_at):
        ok_at, _, _ = M.send_reply(
            "TOK", "APP", "https://api.bot.qq.com", "group", "G1",
            image_reply(), msg_id="M1", pngs=[PNG], tail=WHISPER,
            at_tag='<qqbot-at-user id="MO1" />')
    at_msg = [r for r in calls_at if r.full_url.endswith("/messages")]
    at_content = json.loads(at_msg[0].data.decode("utf-8"))["markdown"]["content"]
    check("at_tag 透传到 markdown 最前面",
          ok_at and at_content.startswith('<qqbot-at-user id="MO1" />\n'))
    check("@ 之后仍是图片 + /w",
          "![img" in at_content and "/w -Ar.Luna-" in at_content)

    # 失败不抛异常
    clean_cache()
    def fail_handler(req):
        raise OSError("network down")
    with patch.object(M.bot_config, "urlopen", fake_http(fail_handler)[1]):
        ok3, info3, _ = M.send_reply(
            "TOK", "APP", "https://api.bot.qq.com", "group", "G1",
            image_reply(), msg_id="M1", pngs=[PNG], tail=WHISPER)
    check("网络异常不抛、返回失败交给上层回退", not ok3)

    # _dispatch_reply：markdown 开启时不再走富媒体/文本
    clean_cache()
    calls2, fake2 = fake_http(media_handler)
    bot = qq_official.OfficialBot("APP", "SEC",
                                  lambda text, ctx: image_reply())
    bot.token_mgr.get_token = lambda *a, **k: "TOK"
    with patch.object(M.bot_config, "urlopen", fake2), \
         patch.object(qq_official, "send_message") as send_text, \
         patch.object(qq_official.OfficialBot, "_reply_images") as send_img:
        bot._dispatch_reply("TOK", "group", "G1", "wm tigris", {}, "M1")
        check("markdown 成功时不发纯文本", send_text.call_count == 0)
        check("markdown 成功时不发富媒体图", send_img.call_count == 0)
    md_msgs = [r for r in calls2 if r.full_url.endswith("/messages")]
    check("接入链路只产生一条消息", len(md_msgs) == 1)

    # @ 了机器人：同一条链路，markdown 首行多一个官方 @ 用户标签
    clean_cache()
    calls3, fake3 = fake_http(media_handler)
    with patch.object(M.bot_config, "urlopen", fake3), \
         patch.object(qq_official, "send_message") as send_text3, \
         patch.object(qq_official.OfficialBot, "_reply_images") as send_img3:
        bot._dispatch_reply("TOK", "group", "G1", "平原",
                            {"at_me": True, "member_openid": "MO1"}, "M1")
        check("@ 消息仍只走一条 markdown",
              send_text3.call_count == 0 and send_img3.call_count == 0)
    md3 = [r for r in calls3 if r.full_url.endswith("/messages")]
    c3 = json.loads(md3[0].data.decode("utf-8"))["markdown"]["content"]
    check("@ 了机器人：回复首行 @ 回去",
          c3.startswith('<qqbot-at-user id="MO1" />'))
    check("@ 回去不影响卡片图与 /w", "![img" in c3 and "/w -Ar.Luna-" in c3)

    # markdown 失败 → 回退原富媒体图
    clean_cache()
    def md_fail(req):
        # 平台拒绝 markdown 业务码时真实 API 会返回 HTTP 4xx；用 HTTPError 模拟
        if req.full_url.endswith("/messages"):
            import urllib.error
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(b'{"code":40035010,"message":"bad media"}'))
        return media_handler(req)
    calls3, fake3 = fake_http(md_fail)
    bot2 = qq_official.OfficialBot("APP", "SEC",
                                   lambda text, ctx: image_reply())
    bot2.token_mgr.get_token = lambda *a, **k: "TOK"
    with patch.object(M.bot_config, "urlopen", fake3), \
         patch.object(qq_official.OfficialBot, "_reply_images",
                      return_value=qq_official._ImageSendResult(True, True)) as fb:
        bot2._dispatch_reply("TOK", "group", "G1", "wm tigris", {}, "M1")
        check("markdown 失败回退富媒体图片", fb.call_count >= 1)

    # 关闭 markdown → 完全不尝试素材/消息上传（走原通道）
    with patch.dict(os.environ, {"WM_QQ_MARKDOWN": "0"}), \
         patch.object(qq_official.OfficialBot, "_reply_images",
                      return_value=qq_official._ImageSendResult(True, True)) as fb2:
        bot2._dispatch_reply("TOK", "group", "G1", "wm tigris", {}, "M1")
        check("开关关闭走原富媒体通道", fb2.call_count >= 1)

    # 素材上传失败，但 /messages 本身能发成功：绝不能退化成「无图纯文字」
    clean_cache()
    def media_fail(req):
        if req.full_url.endswith("/v2/media-images/prepare"):
            return {"code": 40035007, "message": "media limit"}
        return media_handler(req)          # /messages 返回 MSG_OK
    calls_ni, fake_ni = fake_http(media_fail)
    with patch.object(M.bot_config, "urlopen", fake_ni):
        ok_ni, info_ni, _ = M.send_reply(
            "TOK", "APP", "https://api.bot.qq.com", "group", "G1",
            image_reply(), msg_id="M1", pngs=[PNG], tail=WHISPER)
    check("拿不到图片引用时判为失败",
          not ok_ni and str(info_ni).startswith(M.NO_IMAGE))
    check("拿不到图片引用时不发无图 Markdown",
          not [r for r in calls_ni if r.full_url.endswith("/messages")])

    # 接入链路：直接回退富媒体发图，不做无意义的 msg_id 重试上传
    clean_cache()
    calls_fb, fake_fb = fake_http(media_fail)
    bot_ni = qq_official.OfficialBot("APP", "SEC",
                                     lambda text, ctx: image_reply())
    bot_ni.token_mgr.get_token = lambda *a, **k: "TOK"
    with patch.object(M.bot_config, "urlopen", fake_fb), \
         patch.object(qq_official.OfficialBot, "_reply_images",
                      return_value=qq_official._ImageSendResult(True, True)) as fb_ni:
        bot_ni._dispatch_reply("TOK", "group", "G1", "wm tigris", {}, "M1")
        check("素材失败时回退富媒体图片", fb_ni.call_count == 1)
    check("素材失败时没有无图 Markdown 消息",
          not [r for r in calls_fb if r.full_url.endswith("/messages")])
    check("没图时跳过 msg_id 重试（只上传一次）",
          len([r for r in calls_fb
               if r.full_url.endswith("/prepare")]) == 1)

    # 即便调试时关了回退开关，没图也要把卡片图发出去
    clean_cache()
    with patch.dict(os.environ, {"WM_QQ_MD_FALLBACK": "0"}), \
         patch.object(M.bot_config, "urlopen", fake_http(media_fail)[1]), \
         patch.object(qq_official.OfficialBot, "_reply_images",
                      return_value=qq_official._ImageSendResult(True, True)) as fb_dbg:
        bot_ni._dispatch_reply("TOK", "group", "G1", "wm tigris", {}, "M1")
        check("调试关闭回退但没图时仍回退发图", fb_dbg.call_count == 1)

    # WM_QQ_MD_FALLBACK=0：真正发送失败后不再发原通道
    clean_cache()
    with patch.dict(os.environ, {"WM_QQ_MD_FALLBACK": "0"}), \
         patch.object(M.bot_config, "urlopen", fake_http(md_fail)[1]), \
         patch.object(qq_official.OfficialBot, "_reply_images") as fb3, \
         patch.object(qq_official, "send_message") as st3:
        bot2._dispatch_reply("TOK", "group", "G1", "wm tigris", {}, "M1")
        check("禁止回退时不再发富媒体", fb3.call_count == 0)
        check("禁止回退时不再发纯文本", st3.call_count == 0)

# ---------------------------------------------------------------- 9
section("9) 覆盖式固定公网图（永远只有一张 card.png）")
PNG2 = PNG + b"another-card"
PUT = "https://cdn.example.com/wm/card.png"
ENV_PUT = {"WM_QQ_MD_IMAGE_PUT_URL": PUT,
           "WM_QQ_MD_IMAGE_PUT_TOKEN": "Bearer ABC",
           "WM_QQ_MD_GITHUB_REPO": "", "WM_QQ_MD_GITHUB_TOKEN": "",
           "WM_QQ_MD_IMAGE_URL": "", "WM_QQ_MD_IMAGE_UPLOAD": "",
           "WM_QQ_MD_IMAGE_BUST": "1"}

clean_cache()
with patch.dict(os.environ, ENV_PUT):
    calls_p, fake_p = fake_http(lambda req: {})
    with patch.object(M.bot_config, "urlopen", fake_p):
        u1 = M.overwrite_upload(PNG)
        u2 = M.overwrite_upload(PNG)       # 同一张卡片：不该重复上传
        u3 = M.overwrite_upload(PNG2)      # 内容变了：覆盖同一个地址
    puts = [r for r in calls_p if "cdn.example.com" in r.full_url]
    check("覆盖式返回固定公网 URL", u1.startswith(PUT + "?v="))
    check("PUT 直传 PNG 二进制", len(puts) == 2 and puts[0].data == PNG)
    check("PUT 声明 image/png",
          puts[0].get_header("Content-type") == "image/png")
    check("PUT 用自己的鉴权头、不带 QQBot",
          "QQBot" not in str(puts[0].headers) and "ABC" in str(puts[0].headers))
    check("同一张卡片不重复上传", u2.startswith(PUT) and len(puts) == 2)
    check("换内容仍覆盖同一个地址",
          u3.startswith(PUT + "?v=") and puts[1].full_url == puts[0].full_url)
    check("内容变化才换版本号", u1.split("v=")[-1] != u3.split("v=")[-1])

    clean_cache()
    calls_r, fake_r = fake_http(media_handler)
    with patch.object(M.bot_config, "urlopen", fake_r):
        ur = M.resolve_image_url([PNG], "TOK", "APP", "https://api.bot.qq.com")
    check("resolve 优先覆盖式公网图", ur.startswith(PUT + "?v="))
    check("优先通道不再调官方素材",
          not [r for r in calls_r if "/v2/media-images/prepare" in r.full_url])

# GitHub / Gitee Contents API 覆盖：GET 拿 sha → PUT 同路径
clean_cache()
gh_calls = []
def gh_handler(req):
    gh_calls.append(req)
    if req.get_method() == "GET":
        return {"sha": "OLD_SHA", "content": ""}
    return {"content": {"name": "card.png"}}
ENV_GH = {"WM_QQ_MD_IMAGE_PUT_URL": "", "WM_QQ_MD_GITHUB_REPO": "me/wm-card",
          "WM_QQ_MD_GITHUB_TOKEN": "ghp_TEST",
          "WM_QQ_MD_GITHUB_PATH": "wm/card.png",
          "WM_QQ_MD_GITHUB_BRANCH": "main",
          "WM_QQ_MD_GITHUB_API": "https://api.github.com",
          "WM_QQ_MD_IMAGE_URL": "", "WM_QQ_MD_IMAGE_BUST": "0"}
with patch.dict(os.environ, ENV_GH):
    with patch.object(M.bot_config, "urlopen", fake_http(gh_handler)[1]):
        ug = M.overwrite_upload(PNG)
    check("GitHub 覆盖返回固定 raw 链接",
          ug == "https://raw.githubusercontent.com/me/wm-card/main/wm/card.png")
    get_req, put_req = gh_calls[0], gh_calls[1]
    gh_body = json.loads(put_req.data.decode("utf-8"))
    check("先 GET 取旧文件 sha",
          get_req.get_method() == "GET" and "ref=main" in get_req.full_url)
    check("PUT 带 sha 才是覆盖而非新建", gh_body["sha"] == "OLD_SHA")
    check("PUT 内容是 PNG 的 base64",
          base64.b64decode(gh_body["content"]) == PNG)
    check("PUT 固定分支与路径",
          gh_body["branch"] == "main"
          and "/contents/wm/card.png" in put_req.full_url)
    check("关闭 bust 后 URL 不带参数", "?" not in ug)

# 覆盖式失败 → 回落官方素材，不让查询没图
clean_cache()
def put_fail(req):
    if "cdn.example.com" in req.full_url:
        raise OSError("disk full")
    return media_handler(req)
with patch.dict(os.environ, ENV_PUT):
    with patch.object(M.bot_config, "urlopen", fake_http(put_fail)[1]):
        uf = M.resolve_image_url([PNG], "TOK", "APP", "https://api.bot.qq.com")
    check("覆盖式失败回落官方素材",
          uf == "qqbot-media://image/v1/TOKEN123")

# 最小用例自检：一张图 + 一个内容字符串，dry-run 不联网
probe_html = os.path.join(_TMP.name, "probe.html")
probe = subprocess.run(
    [sys.executable, os.path.join(HERE, "md_probe.py"), "--html", probe_html],
    capture_output=True, text=True, timeout=90)
check("probe dry-run 打印图片标签 + 内容字符串",
      "![img" in probe.stdout and "内容字符串测试" in probe.stdout)
check("probe dry-run 明确不发消息", "dry-run" in probe.stdout)
check("probe 生成网页预览", Path(probe_html).exists())

print("\n结果：通过 %d / 失败 %d" % (OK[0] - OK[1], OK[1]))
sys.exit(1 if OK[1] else 0)
