#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
md_probe.py — QQ 自定义 Markdown「真实渲染」自检：一张图 + 一个内容字符串

用途：官方文档没保证 Markdown 图片标签一定接受任意公网 https 链接，
所以别靠猜 —— 发一条最小消息，看手机 QQ 里到底渲不渲染。

  # ① 只看请求体 / 本地网页预览（不联网、不发送、最安全）
  python md_probe.py

  # ② 真实上传卡片图拿 URL，但还是不发送
  python md_probe.py --upload --image overwrite

  # ③ 真的发到群里，看 QQ 是否显示「上图片 + 下字符串」的一条消息
  python md_probe.py --send --group <group_openid> --image overwrite

  # ④ 改用官方素材 qqbot-media:// 或静态 URL 对照测试
  python md_probe.py --send --group <group_openid> --image media
  python md_probe.py --send --user  <user_openid>  --url https://.../card.png

最小消息体只有两行：
  ![img #618px #Npx](图片URL)
  内容字符串……

不带标题、不带 /w、不带来源链接，方便判断「图能不能出来」和
「文字能不能跟图在同一条消息里」。--image 可选：
  auto(默认) / overwrite(覆盖式固定公网图) / static / media(官方素材) /
  upload(POST 图床) / url(直接给 --url)

零第三方依赖；不主动联网，只有显式 --upload 或 --send 才发请求。
"""
import argparse
import base64
import json
import os
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config as bot_config          # noqa: E402

PLACEHOLDER = "https://example.com/wm-card.png"
DEFAULT_CONTENT = (
    "内容字符串测试：如果这段文字和上面的图片出现在同一条消息里，"
    "说明自定义 Markdown 转换成功。"
)


# ------------------------------ 最小测试图 ------------------------------
def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def solid_png(w=600, h=240):
    """纯标准库画一张红/蓝/灰三条横带的 PNG，手机没装 Pillow 也能测。"""
    rows = []
    for y in range(h):
        if y < h // 3:
            band = (198, 40, 40)          # 游戏中 · 红
        elif y < 2 * h // 3:
            band = (32, 92, 180)          # 在线 · 蓝
        else:
            band = (132, 136, 146)        # 离线 · 灰
        rows.append(b"\x00" + bytes(band) * w)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + _chunk(b"IEND", b""))


def make_png():
    """优先用真实卡片（Pillow 可用时），否则退回纯标准库色带图。"""
    try:
        import render_card
        pages = render_card.render(
            "Markdown 渲染自检\n这是一张真实卡片图\n红 / 蓝 / 灰靠图片承载",
            title="Markdown 图片自检",
        )
        if pages and pages[0]:
            return pages[0], "render_card 真实卡片"
    except Exception:
        pass
    return solid_png(), "纯标准库色带图（未装 Pillow）"


# ------------------------------ 本地网页预览 ------------------------------
def html_preview(png, url, content, md_text, body, path):
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;"))
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Markdown 最小用例 · 一张图 + 一个内容字符串</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;padding:28px;background:#f2f3f5;color:#1f2329;
 font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;font-size:14px;line-height:1.7}}
.wrap{{max-width:1000px;margin:0 auto}}h2{{font-size:19px;margin:0 0 6px}}
.sub{{color:#646a73;margin:0 0 20px;font-size:13px}}
.grid{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px}}
.card{{background:#fff;border:1px solid #e5e6eb;border-radius:12px;padding:18px;
 box-shadow:0 2px 10px rgba(0,0,0,.04)}}
.tag{{display:inline-block;background:#e8f3ff;color:#1664d9;border-radius:5px;
 padding:2px 9px;font-size:12px;margin-bottom:12px}}
.bubble{{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:14px}}
.bubble img{{width:100%;border-radius:8px;border:1px solid #e5e6eb;display:block}}
.txt{{margin-top:12px;white-space:pre-wrap;word-break:break-word;font-size:14px}}
pre{{background:#f7f8fa;border:1px solid #e5e6eb;border-radius:8px;padding:12px;
 overflow:auto;font-size:12px;line-height:1.6;white-space:pre-wrap;word-break:break-all}}
.note{{margin-top:18px;background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;
 padding:12px;font-size:13px}}
code{{background:#f2f3f5;padding:1px 5px;border-radius:4px}}
</style></head><body><div class="wrap">
<h2>Markdown 最小用例 · 一张图 + 一个内容字符串</h2>
<p class="sub">左边是「QQ 里应该看到的样子」，右边是真正发出去的请求体。
本地预览把图片换成了同一张 PNG 的 data URI，<b>只是为了离线能看图</b>；
真实发送用的是图片 URL，请运行 <code>python md_probe.py --send</code> 实测。</p>
<div class="grid">
 <div class="card"><span class="tag">① 客户端应该看到的一条消息</span>
  <div class="bubble"><img src="{data_uri}" alt="卡片图">
   <div class="txt">{esc(content)}</div></div>
  <p class="sub" style="margin:12px 0 0">图片 URL（真实发送时）：<br>{esc(url)}</p>
 </div>
 <div class="card"><span class="tag">② 实际 POST 的请求体</span>
  <pre>{esc(json.dumps(body, ensure_ascii=False, indent=2))}</pre>
  <p class="sub" style="margin:12px 0 0">markdown.content 原文：</p>
  <pre>{esc(md_text)}</pre>
 </div>
</div>
<div class="note"><b>怎么判断成功：</b>手机 QQ 里这一条消息同时出现「图片」和「内容字符串」=
转换成功，可以放心用 <code>WM_QQ_MARKDOWN = native</code>；只出现文字或整条发不出去 =
平台不放行该图片 URL，换 <code>--image media</code> 用官方素材对照，
或设 <code>WM_QQ_MARKDOWN = 0</code> 走已验证的富媒体图文混排。
Markdown 文字本身没有颜色语法，红蓝灰只存在于卡片图里。</div>
</div></body></html>"""
    Path(path).write_text(html, encoding="utf-8")
    return str(Path(path))


# ------------------------------ 主流程 ------------------------------
def main():
    ap = argparse.ArgumentParser(description="QQ Markdown 真实渲染自检")
    ap.add_argument("--send", action="store_true", help="真的发一条消息（默认只 dry-run）")
    ap.add_argument("--upload", action="store_true",
                    help="dry-run 时也真实上传图片拿 URL")
    ap.add_argument("--group", help="群 group_openid")
    ap.add_argument("--user", help="单聊 user_openid")
    ap.add_argument("--msg-id", help="被动回复的 msg_id（默认不带＝普通群消息）")
    ap.add_argument("--image", default="auto",
                    choices=["auto", "overwrite", "static", "media", "upload", "url"],
                    help="图片来源通道，默认 auto 按优先级自动选")
    ap.add_argument("--url", help="直接指定图片 URL（--image url 或覆盖用）")
    ap.add_argument("--content", default=DEFAULT_CONTENT, help="内容字符串")
    ap.add_argument("--html", default=str(HERE.parent / "Markdown最小用例.html"),
                    help="本地预览页输出路径")
    args = ap.parse_args()

    bot_config.apply()
    import qq_markdown as M          # 放这里，避免 dry-run 也触发无关导入
    png, origin = make_png()
    need_net = args.send or args.upload
    token = appid = api_base = None

    if need_net:
        import qq_official as Q
        appid = os.environ.get("WM_QQ_APPID") or bot_config.get("WM_QQ_APPID")
        secret = os.environ.get("WM_QQ_SECRET") or bot_config.get("WM_QQ_SECRET")
        api_base = (os.environ.get("WM_QQ_API_BASE")
                    or bot_config.get("WM_QQ_API_BASE", Q.DEFAULT_API_BASE))
        if args.send and not (appid and secret):
            print("✗ 缺少 WM_QQ_APPID / WM_QQ_SECRET（写在 data/config.txt）",
                  file=sys.stderr)
            return 2
        if appid and secret:
            token = Q.TokenManager(appid, secret, api_base).get_token()

    # 图片 URL：dry-run 不联网就用占位地址，真实上传才走对应通道
    url = args.url or ""
    if need_net and not url:
        if args.image == "auto":
            url = M.resolve_image_url([png], token, appid, api_base)
        elif args.image == "overwrite":
            url = M.overwrite_upload(png)
        elif args.image == "static":
            url = M.bust_url(M.static_image_url(), png)
        elif args.image == "media":
            url = M.upload_media_image(png, token, appid, api_base)
        elif args.image == "upload":
            url = M.upload_png(png)
    if not url:
        url = args.url or PLACEHOLDER
        if need_net:
            print(f"⚠️ --image {args.image} 没拿到图片 URL，改用占位地址 {url}；"
                  "真实发送时 QQ 里会看不到图，请检查配置。", file=sys.stderr)

    size = M.image_markup_size(png)
    md_text = f"![img {size}]({url})\n\n{args.content}"
    body = {"msg_type": 2, "markdown": {"content": md_text}}
    if args.msg_id:
        body["msg_id"] = args.msg_id

    print("=" * 66)
    print(f"图片：{origin}，{len(png)} 字节，标注 {size}")
    print(f"通道：{args.image}｜固定 URL：{url}")
    print(f"模式：{'真实发送' if args.send else 'dry-run（不发送）'}")
    print(f"覆盖式上传已配置：{'是' if M.overwrite_configured() else '否'}"
          f"｜Markdown 模式：{M.resolved_mode()}")
    print("-" * 66)
    print("请求体（一张图 + 一个内容字符串）：")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    print("-" * 66)

    try:
        page = html_preview(png, url, args.content, md_text, body, args.html)
        print(f"本地网页预览已生成：{page}")
    except Exception as e:
        print(f"（预览页生成失败，不影响发送：{e}）")

    if not args.send:
        print("这是 dry-run，没有联网发送。真机实测：")
        print("  python md_probe.py --send --group <group_openid> --image overwrite")
        return 0

    if not (args.group or args.user):
        print("✗ --send 需要 --group <group_openid> 或 --user <user_openid>",
              file=sys.stderr)
        return 2
    import qq_official as Q
    kind = "group" if args.group else "user"
    target = args.group or args.user
    path = (f"/v2/groups/{target}/messages" if kind == "group"
            else f"/v2/users/{target}/messages")
    ok, info = Q._api_post(token, appid, api_base, path, body)
    print(f"平台返回：{'成功' if ok else '失败'} -> {info}")
    if ok:
        print("现在去看手机 QQ：图片和内容字符串是否在同一条消息里。")
    else:
        print("失败常见原因：主动发言权限未开、msg_id 过期、图片 URL 不被放行、"
              "Markdown 能力未开通；对照 --image media 再测一次。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
