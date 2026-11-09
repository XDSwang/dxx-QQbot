#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qq_markdown.py — QQ 官方「Markdown 消息」(msg_type=2) 通道：上图片、下字符串

为什么现在能做（官方文档 2026/04/23 能力更新）：
  1. 单聊 / 群聊的【自定义 Markdown】已开放给所有机器人，无需单独申请模板；
  2. 图片走【固定公网 URL + 覆盖式上传】（首选，跟 HTML 邮件托管图片同一原理）：
     只用一个对象/一个文件，每次把新卡片 PNG **覆盖**写进去，URL 永不变、
     不占免费额度、不会攒出 90 天过期的素材。Markdown 里就写普通图片标签
     ![img #618px #900px](https://.../card.png?v=ab12cd34)，客户端自动解析。
     支持两种零依赖覆盖式后端：
       a. 通用 PUT 覆盖（网盘 WebDAV / 对象存储 / R2 / 任意可 PUT 的固定地址）
       b. GitHub / Gitee Contents API 覆盖（GET 拿 sha → PUT 同路径带 sha，
          公开链接固定为 raw / jsdelivr 地址）
     末尾 `?v=<图片哈希>` 只为破 CDN/客户端缓存，存储仍然只有一张图。
  3. 没配置上面两种时，才退回官方【素材管理】二进制三步上传换 `qqbot-media://`：
       POST /v2/media-images/prepare → PUT presigned → POST complete
     再不行用自备 POST 图床；全都拿不到引用就回退富媒体发图（绝不发无图纯文字）。
  4. 一条消息 =【完整卡片图】+【图片下方两条可复制 /w 指令】。
     ★ 卡片图里已经画了标题 / 行情统计 / 排序说明 / 订单列表 / 来源网址，
       所以 Markdown 文字里**绝不再重复一遍正文**（真机实测曾出现「图下面又
       重复 WM 在售、排序、订单」，用户明确要求图下面只要两条 /w）。
       只有纯文字回退（没图，如主动发「文字 xxx」）才输出完整标题 + 正文 + /w + 来源。

仍然存在的平台硬限制（如实告知，别期待落空）：
  - Markdown 文字样式只有 加粗 / 斜体 / 下划线 / 删除线 / 链接 / 列表 / 引用 /
    分割线，**没有颜色语法**。「游戏中红 / 在线蓝 / 离线灰」是卡片图里 Pillow
    画出来的，所以只要卡片能渲染，颜色就在图里；纯 Markdown 文字本身不带色。
  - 兜底通道（官方素材）单机器人上限 100 张、滑动 90 天过期。按图片 SHA1 缓存，
    同一张卡片不重复上传；WM_QQ_MD_MEDIA_MAX 默认只留 3 张，每上传一张新卡片
    就把多余的旧素材删掉（DELETE /v2/media-images/{image_id}/delete），
    平台长期只占 1~3 张配额；想改 1 / 5 张直接改这个数值即可。
    首选的覆盖式公网图没有这个限制。
  - 模板 Markdown（custom_template_id）仍需在后台创建 + 审核拿 ID，本模块兼容。
  - 【需要你实测】QQ 各版本客户端对 Markdown 图片标签里的公网 https 链接是否放行、
    有没有域名白名单，官方文档没有明确保证。请跑 `python md_probe.py --send`
    发一条「一张图 + 一个内容字符串」的最小消息，看手机 QQ 里到底渲不渲染；
    不渲染就设 WM_QQ_MARKDOWN = 0 走已验证的富媒体图文混排。

默认【开启 native】：开启后失败自动回退原富媒体图片 / 纯文本，不让查询石沉大海。
WM_QQ_MARKDOWN = 0 可关掉，回到原来的 msg_type=7 图文混排。

零第三方依赖；所有 HTTPS 走 bot_config.urlopen（统一 CA / 证书降级出口）。
"""
import base64
import hashlib
import json
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config as bot_config

HERE = Path(__file__).resolve().parent
DEFAULT_MAX_CHARS = 3000
DEFAULT_MEDIA_MAX = 3           # 平台上限 100；默认只留 3 张，小阈值滚动清理
# 官方模板示例里的八个变量，params 顺序固定
TEMPLATE_KEYS = ("title", "image", "para1", "para2", "desc",
                 "content", "link_introduction", "link")

_OFF = {"", "0", "false", "no", "off", "none", "null", "n", "f"}
_NATIVE = {"1", "true", "yes", "on", "y", "t", "native", "content", "raw"}
_TEMPLATE = {"template", "tpl", "mdtpl"}
_AUTO = {"auto", "2"}

# 卡片图没拿到引用时，宁可不发 Markdown 也不能让用户看到「无图纯文字」：
# 上层看到这个前缀就跳过 msg_id 重试，直接回退富媒体图片通道。
NO_IMAGE = "NO_IMAGE"

_SRC_RE = re.compile(r"来源[：:]\s*(https?://\S+)")
_WHISPER_RE = re.compile(r"^\s*(英文|中文)[：:]\s*/w\s", re.I)
_HOST_RE = re.compile(r"^https?://([^/]+)")


# ------------------------------ 配置 ------------------------------
def mode():
    """WM_QQ_MARKDOWN：native(默认) / template / auto / off。"""
    raw = (os.environ.get("WM_QQ_MARKDOWN") or "native").strip().lower()
    if raw in _OFF:
        return "off"
    if raw in _NATIVE:
        return "native"
    if raw in _TEMPLATE:
        return "template"
    if raw in _AUTO:
        return "auto"
    return "off" if raw in _OFF else "native"


def enabled():
    return mode() != "off"


def template_id():
    return (os.environ.get("WM_QQ_MD_TEMPLATE_ID") or "").strip()


def resolved_mode():
    m = mode()
    if m == "off":
        return "off"
    if m == "auto":
        return "template" if template_id() else "native"
    if m == "template" and not template_id():
        return "native"          # 没有审核过的模板 ID，退原生 markdown
    return m


def fallback_enabled():
    """markdown 失败后是否回退原通道（默认开；设 0 用于调试平台返回码）。"""
    return (os.environ.get("WM_QQ_MD_FALLBACK") or "1").strip().lower() \
        not in _OFF


def max_chars():
    try:
        return max(300, int(os.environ.get("WM_QQ_MD_MAX_CHARS")
                            or DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS


def media_max():
    try:
        return max(1, min(95, int(os.environ.get("WM_QQ_MD_MEDIA_MAX")
                                  or DEFAULT_MEDIA_MAX)))
    except (TypeError, ValueError):
        return DEFAULT_MEDIA_MAX


def media_cache_file():
    p = (os.environ.get("WM_QQ_MD_MEDIA_CACHE") or "data/md_images.json").strip()
    q = Path(p)
    return q if q.is_absolute() else HERE / q


def image_width():
    try:
        return max(120, min(1200, int(os.environ.get("WM_QQ_MD_IMAGE_WIDTH")
                                      or 618)))
    except (TypeError, ValueError):
        return 618


def static_image_url():
    return (os.environ.get("WM_QQ_MD_IMAGE_URL") or "").strip()


def upload_endpoint():
    return (os.environ.get("WM_QQ_MD_IMAGE_UPLOAD") or "").strip()


# --- 覆盖式固定公网图（首选通道）：只占一个存储对象，每次覆盖同一张 ---
def overwrite_put_url():
    """通用覆盖上传端点：把 PNG 二进制 PUT/POST 到这个固定地址。"""
    return (os.environ.get("WM_QQ_MD_IMAGE_PUT_URL") or "").strip()


def overwrite_put_method():
    return (os.environ.get("WM_QQ_MD_IMAGE_PUT_METHOD") or "PUT").strip().upper()


def overwrite_put_token():
    """原样放进 Authorization 头（自己写完整值，如 Bearer xxx / token xxx）。"""
    return (os.environ.get("WM_QQ_MD_IMAGE_PUT_TOKEN") or "").strip()


def github_repo():
    return (os.environ.get("WM_QQ_MD_GITHUB_REPO") or "").strip().strip("/")


def github_token():
    return (os.environ.get("WM_QQ_MD_GITHUB_TOKEN") or "").strip()


def github_path():
    return (os.environ.get("WM_QQ_MD_GITHUB_PATH") or "wm/card.png").strip().lstrip("/")


def github_branch():
    return (os.environ.get("WM_QQ_MD_GITHUB_BRANCH") or "main").strip()


def github_api():
    return (os.environ.get("WM_QQ_MD_GITHUB_API") or "https://api.github.com").strip()


def github_raw():
    raw = (os.environ.get("WM_QQ_MD_GITHUB_RAW") or "").strip()
    if raw:
        return raw
    return (f"https://raw.githubusercontent.com/{github_repo()}/"
            f"{github_branch()}/{github_path()}")


def overwrite_public_url():
    """固定公开地址：显式配置 > GitHub/Gitee raw > 上传端点自身。"""
    static = static_image_url()
    if static:
        return static
    if github_repo() and github_token():
        return github_raw()
    return overwrite_put_url()


def overwrite_configured():
    return bool(overwrite_put_url() or (github_repo() and github_token()))


def bust_enabled():
    return (os.environ.get("WM_QQ_MD_IMAGE_BUST") or "1").strip().lower() not in _OFF


def overwrite_state_file():
    p = (os.environ.get("WM_QQ_MD_IMAGE_STATE")
         or "data/md_image_state.json").strip()
    q = Path(p)
    return q if q.is_absolute() else HERE / q


# ------------------------------ 底层 HTTP ------------------------------
def _request(method, url, token=None, appid=None, body=None, data=None,
             content_type=None, timeout=15, headers_extra=None):
    """统一请求；PUT 预签名 URL 时不带 QQBot 鉴权头（COS 会拒绝多余头）。

    headers_extra 给网盘 / GitHub / 对象存储放自己的鉴权头，不污染 QQBot 鉴权。
    """
    headers = dict(headers_extra or {})
    raw = None
    if body is not None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        raw = data
        if content_type:
            headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"QQBot {token}"
        headers["X-Union-Appid"] = str(appid or "")
    req = urllib.request.Request(url, data=raw, method=method, headers=headers)
    try:
        with bot_config.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(text) if text.strip() else {}
            except ValueError:
                return True, {"raw": text}
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"
    except Exception as e:
        return False, str(e)


def _api(token, appid, api_base, path, body, method="POST", timeout=15):
    return _request(method, api_base.rstrip("/") + path, token=token,
                    appid=appid, body=body, timeout=timeout)


def _png_size(png):
    """纯标准库读 PNG 宽高（IHDR），失败给个默认值。"""
    try:
        if png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) >= 24:
            w, h = struct.unpack(">II", png[16:24])
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return 1000, 600


def image_markup_size(png):
    """按原图比例算 Markdown 图片标注，形如 '#618px #1240px'。"""
    w, h = _png_size(png)
    tw = image_width()
    th = max(1, int(round(h * tw / float(w))))
    return f"#{tw}px #{th}px"


# ------------------------------ 素材管理 ------------------------------
def _cache_load():
    p = media_cache_file()
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _cache_save(cache):
    p = media_cache_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)                       # 原子替换，手机断电也不写坏
    except Exception:
        pass


def delete_media_image(image_id, token, appid, api_base):
    """DELETE /v2/media-images/{image_id}/delete，释放平台配额。失败不抛。"""
    if not image_id:
        return False
    ok, _ = _request(
        "DELETE", api_base.rstrip("/") + f"/v2/media-images/{image_id}/delete",
        token=token, appid=appid, timeout=10)
    return ok


def _prune_cache(cache, token, appid, api_base):
    """超过阈值就按上传时刻删最旧的，平台与本地一起清。

    平台删除失败时把条目放回账本，避免「本地以为删了、平台其实还占着配额」——
    下次上传新卡片时还会再试删它，不会悄悄留下一张永远清不掉的旧素材。
    """
    over = len(cache) - media_max()
    if over <= 0:
        return
    for key in sorted(cache, key=lambda k: cache[k].get("at", 0))[:over]:
        item = cache.pop(key, None) or {}
        if not delete_media_image(item.get("image_id"), token, appid, api_base):
            cache[key] = item


def upload_media_image(png, token, appid, api_base, file_name="card.png"):
    """本地 PNG → 官方素材 qqbot-media:// 引用；任何失败返回空串。

    按 SHA1 缓存：同一张卡片不重复上传；缓存超限自动删最旧素材释放 100 张配额。
    """
    if not png:
        return ""
    digest = hashlib.sha1(png).hexdigest()
    cache = _cache_load()
    hit = cache.get(digest)
    if isinstance(hit, dict) and str(hit.get("url", "")).startswith("qqbot-media://"):
        hit["at"] = time.time()
        _cache_save(cache)
        return hit["url"]

    md5 = hashlib.md5(png).hexdigest()
    sha1 = digest
    ok, prep = _api(token, appid, api_base, "/v2/media-images/prepare", {
        "file_name": file_name,
        "file_size": len(png),
        "content_type": "image/png",
        "md5": md5,
        "sha1": sha1,
    }, timeout=15)
    if not ok or not isinstance(prep, dict):
        return ""
    upload_id = prep.get("upload_id")
    presigned = prep.get("presigned_url")
    if not upload_id or not presigned:
        return ""

    ok, _ = _request("PUT", presigned, data=png,
                     content_type="image/png", timeout=25)
    if not ok:
        return ""

    ok, done = _api(token, appid, api_base, "/v2/media-images/complete", {
        "upload_id": upload_id, "md5": md5,
    }, timeout=15)
    if not ok or not isinstance(done, dict):
        return ""
    url = str(done.get("url") or "")
    if not url.startswith("qqbot-media://"):
        return ""
    cache[digest] = {"url": url, "image_id": done.get("image_id", ""),
                     "at": time.time()}
    _prune_cache(cache, token, appid, api_base)
    _cache_save(cache)
    return url


# ------------------------------ 覆盖式上传（固定一张图） ------------------------------
def _state_load():
    p = overwrite_state_file()
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _state_save(state):
    p = overwrite_state_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def bust_url(url, png):
    """固定 URL 覆盖后内容变了、地址没变，客户端/CDN 可能还给旧图。

    加一个只跟图片内容有关的 `?v=<sha1前8位>` 破缓存；存储对象仍然只有一个，
    不产生新图、不占额度。qqbot-media:// 不是 HTTP 链接，不加。
    """
    if not url or not bust_enabled():
        return url
    if not url.startswith(("http://", "https://")):
        return url
    tag = hashlib.sha1(png).hexdigest()[:8]
    return f"{url}{'&' if '?' in url else '?'}v={tag}"


def _put_overwrite(png):
    """通用覆盖：把 PNG 二进制直接写到一个固定地址（WebDAV / R2 / 网盘 / 图床）。"""
    ep = overwrite_put_url()
    if not ep:
        return False
    headers = {}
    tok = overwrite_put_token()
    if tok:
        headers["Authorization"] = tok
    ok, _ = _request(overwrite_put_method(), ep, data=png,
                     content_type="image/png", headers_extra=headers, timeout=25)
    return ok


def _github_overwrite(png):
    """GitHub / Gitee Contents API：GET 取 sha → PUT 同路径覆盖，公开 URL 恒定。"""
    repo, tok, path = github_repo(), github_token(), github_path()
    if not (repo and tok):
        return False
    api = github_api().rstrip("/")
    branch = github_branch()
    base = f"{api}/repos/{repo}/contents/{urllib.parse.quote(path, safe='/')}"
    headers = {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "wm-qq-bot",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    query = "?ref=" + urllib.parse.quote(branch)
    if "gitee.com" in api:                 # Gitee 用 access_token 查询参数
        query += "&access_token=" + urllib.parse.quote(tok)
    ok, cur = _request("GET", base + query, headers_extra=headers, timeout=15)
    sha = cur.get("sha", "") if ok and isinstance(cur, dict) else ""

    body = {"message": "wm card overwrite",
            "content": base64.b64encode(png).decode("ascii"),
            "branch": branch}
    if sha:
        body["sha"] = sha                  # 已存在的文件必须带 sha 才是「覆盖」
    ok, _ = _request("PUT", base, body=body, headers_extra=headers, timeout=25)
    return ok


def overwrite_upload(png):
    """覆盖式上传到固定公网地址，返回带版本号的可引用 URL；失败返回空串。

    同一张卡片不重复上传（按 SHA1 记住），换了内容才覆盖写一次 ——
    因此无论查多少次，网盘/Git 里永远只有一张 card.png。
    """
    if not png or not overwrite_configured():
        return ""
    digest = hashlib.sha1(png).hexdigest()
    url = overwrite_public_url()
    if not url:
        return ""
    state = _state_load()
    if state.get("sha1") == digest and state.get("url") == url:
        return bust_url(url, png)

    ok = _put_overwrite(png)
    if not ok:
        ok = _github_overwrite(png)
    if not ok:
        return ""
    _state_save({"sha1": digest, "url": url, "at": time.time()})
    return bust_url(url, png)


def upload_png(png, endpoint=None, timeout=12):
    """可选：用户自备图床（官方素材之外的备选），返回公网 URL，失败返回空串。

    请求体：{"filename":"card.png","content_type":"image/png","data":"<base64>"}
    响应兼容：纯 URL 文本 / {"url"} / {"data":{"url"}} / {"link"} 等。
    """
    ep = endpoint or upload_endpoint()
    if not ep or not png:
        return ""
    body = {"filename": "card.png", "content_type": "image/png",
            "data": base64.b64encode(png).decode("ascii")}
    ok, resp = _request("POST", ep, body=body, timeout=timeout)
    if not ok:
        return ""
    if isinstance(resp, str):
        raw = resp.strip()
        return raw if raw.startswith(("http://", "https://")) else ""
    if not isinstance(resp, dict):
        return ""
    for path in (("url",), ("link",), ("data", "url"), ("data", "link")):
        cur = resp
        for key in path:
            cur = cur.get(key) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, str) and cur.startswith(("http://", "https://")):
            return cur
    return ""


def resolve_image_url(pngs, token=None, appid=None, api_base=None):
    """图片地址优先级：覆盖式固定公网图 > 静态 URL > 官方素材 > 自备图床 > 无图。

    固定公网 URL 是首选：跟 HTML 邮件的托管图片一样，客户端直接按图片标签解析，
    只占一个存储对象、不依赖平台素材配额。官方素材 qqbot-media:// 退为兜底。
    """
    if not pngs:
        return ""
    png = pngs[0]
    if overwrite_configured():
        u = overwrite_upload(png)
        if u:
            return u
    static = static_image_url()
    if static:
        return bust_url(static, png)
    if token and api_base:
        url = upload_media_image(png, token, appid, api_base)
        if url:
            return url
    if upload_endpoint():
        return upload_png(png)
    return ""


# ------------------------------ 内容拆分 ------------------------------
def _host(url):
    m = _HOST_RE.match(url or "")
    return m.group(1) if m else "来源"


def split_reply(reply, tail=None):
    """回复 → (标题, 正文行, /w 指令, 来源 URL)。

    兼容 ImageReply（tail 里是 /w）与纯文本回退（/w 已拼在正文末尾）。
    """
    full = str(reply or "")
    lines = [ln.rstrip() for ln in full.splitlines()]
    tail = (tail if tail is not None
            else str(getattr(reply, "tail", "") or "")).strip()

    source = ""
    for ln in lines:
        m = _SRC_RE.search(ln)
        if m:
            source = m.group(1).rstrip("·,，;；)） ")
            break

    in_body = [ln for ln in lines if _WHISPER_RE.match(ln)]
    if not tail and in_body:
        tail = "\n".join(x.strip() for x in in_body)
    whisper = tail.strip()

    first_idx = next((i for i, ln in enumerate(lines) if ln.strip()), 0)
    title = re.sub(r"[#*`\[\]>|]", "", lines[first_idx].strip())[:48] \
        or "星际战甲 · 查询"
    body = lines[first_idx + 1:]
    body = [ln for ln in body
            if not _SRC_RE.search(ln)
            and not (whisper and _WHISPER_RE.match(ln))]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return title, body, whisper, source


def _esc(s):
    """正文转义，避免卖家昵称里的 * _ [ ] 打乱排版。

    /w 指令两行【绝不转义】——复制过去要能直接粘进游戏聊天框。
    """
    return re.sub(r"([\\`*_\[\]])", r"\\\1", s or "")


def build_native(title, body, whisper, source="", image="", size="#618px #900px",
                 at_tag=""):
    """有图 = 卡片图 + 图下两条 /w；图里已有全文，不再重复任何正文。

    没图（纯文字回退）才输出完整标题 + 正文 + /w + 来源链接。
    at_tag：官方 @ 用户标签（`<qqbot-at-user id="member_openid" />`），只在
    「这条消息 @ 了机器人」时由上层传入，放在消息最前面 —— @ 了我，就 @ 回去。
    模板 markdown 的变量是纯文本，塞不进标签，因此只在原生模式下生效。
    """
    tail_md = "\n\n" + whisper.strip() if whisper else ""
    head_at = (at_tag.strip() + "\n") if at_tag and at_tag.strip() else ""

    if image:
        # 卡片图自带标题 / 统计 / 排序 / 订单 / 来源，文字部分只保留可复制指令。
        # 多一个字都会跟图里的内容重复 —— 这是用户看真机截图后的明确要求。
        return head_at + f"![img {size}]({image})" + tail_md

    head = ["# " + _esc(title), ""]
    for ln in body:
        head.append(_esc(ln) if ln.strip() else "")
    text = "\n".join(head).rstrip()
    link_md = ""
    if source:
        link_md = "\n\n[🔗 " + _esc(_host(source)) + "](" + source + ")"
    budget = max(120, max_chars() - len(tail_md) - len(link_md) - 2)
    if len(text) > budget:
        text = text[:budget] + "\n…（内容过长已截断，发「文字 + 指令」看全文）"
    return head_at + text + tail_md + link_md


def build_template(title, body, whisper, source="", image=""):
    """模板 markdown：按官方示例八个变量填 params。

    有图时同样遵守「图里已有全文，文字只留 /w」：para/desc 留空、title 用
    通用短标题，content 只放两条可复制指令，避免把订单列表再打一遍。
    """
    if image:
        content = whisper.strip()
        para1 = para2 = desc = ""
        title_out = "星际战甲 · 行情"
    else:
        plain = [ln.strip() for ln in body if ln.strip()]
        para1 = plain[0] if plain else ""
        para2 = ""
        for ln in plain[1:5]:
            if ln.startswith(("排序", "昵称颜色", "状态", "说明")):
                para2 = (para2 + " " + ln).strip()
        content_lines = list(body)
        if whisper:
            if content_lines and content_lines[-1].strip():
                content_lines.append("")
            content_lines.extend(whisper.splitlines())
        content = "\n".join(content_lines)
        desc = "查询结果 · 价格从低到高"
        title_out = title

    limit = max(120, max_chars() - 200)
    if len(content) > limit:
        content = content[:limit] + "\n…（已截断，发「文字 + 指令」看全文）"

    values = {
        "title": _esc(title_out),
        "image": image,
        "para1": _esc(para1),
        "para2": _esc(para2),
        "desc": desc,
        "content": content,
        "link_introduction": _esc(_host(source)) if source else "星际战甲",
        "link": source or "https://warframe.market",
    }
    return [{"key": k, "values": [values[k]]} for k in TEMPLATE_KEYS]


def build_markdown(reply, pngs=None, tail=None, image=None,
                   token=None, appid=None, api_base=None, at_tag=""):
    """构造请求体的 markdown 对象，返回 (mode, obj, size)。

    at_tag：@ 用户标签，只有原生 markdown 能带；模板模式忽略（变量是纯文本）。
    """
    m = resolved_mode()
    if m == "off":
        return "off", {}, ""
    title, body, whisper, source = split_reply(reply, tail)
    if image is None:
        image = resolve_image_url(pngs, token, appid, api_base)
    size = image_markup_size(pngs[0]) if pngs else "#618px #900px"
    if m == "native":
        return m, {"content": build_native(title, body, whisper, source,
                                           image, size, at_tag=at_tag)}, size
    return m, {"custom_template_id": template_id(),
               "params": build_template(title, body, whisper, source, image)}, size


# ------------------------------ 发送 ------------------------------
def _default_post(token, appid, api_base, path, body, timeout=15):
    return _api(token, appid, api_base, path, body, "POST", timeout)


def send(token, appid, api_base, kind, target_id, markdown_obj,
         msg_id=None, post=None, timeout=15):
    """发一条 msg_type=2 markdown 消息，返回 (ok, info)。"""
    path = (f"/v2/groups/{target_id}/messages" if kind == "group"
            else f"/v2/users/{target_id}/messages")
    body = {"msg_type": 2, "markdown": markdown_obj}
    if msg_id:
        body["msg_id"] = msg_id
    return (post or _default_post)(token, appid, api_base, path, body, timeout)


def send_reply(token, appid, api_base, kind, target_id, reply,
               msg_id=None, pngs=None, tail=None, post=None, at_tag="",
               on_built=None):
    """一步到位：把 handler 回复尝试发成 markdown 消息。

    返回 (ok, info, mode)。图片的二进制上传在这里完成（走官方素材管理）。
    at_tag：只在「@ 了机器人」时由上层传入，写进原生 markdown 最前面。
    on_built：可选回调 (mode, markdown_obj)，在组装完成、发送给客户端之前调用，
    让上层拿到【代码层原样、未被渲染】的 mk 原文做链路日志；回调异常不影响发送。
    失败由上层回退，不吞异常、不阻塞主通道。
    """
    m = resolved_mode()
    if m == "off":
        return False, "markdown 未开启", "off"
    try:
        # 有卡片图就必须拿到图片引用。否则即便 /messages 发送成功，
        # 用户看到的也只是没有红/蓝/灰的一堆纯文字（这正是要避免的回退形态），
        # 所以直接判失败，把机会让给富媒体图片通道。
        image = None
        if pngs:
            image = resolve_image_url(pngs, token, appid, api_base)
            if not image:
                return False, (NO_IMAGE + ": 卡片图未取到素材引用，"
                               "回退富媒体图片通道"), m
        m, md, _ = build_markdown(reply, pngs=pngs, tail=tail, image=image,
                                  token=token, appid=appid, api_base=api_base,
                                  at_tag=at_tag)
        if on_built is not None:
            try:
                on_built(m, md)
            except Exception:
                pass
        ok, info = send(token, appid, api_base, kind, target_id, md,
                        msg_id=msg_id, post=post)
        return bool(ok), info, m
    except Exception as e:
        return False, str(e), m
