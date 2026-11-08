# 自定义 Markdown 使用说明（当前问题专版）

> 只记录本次这件事：**把「图片」和「/w 字符串」合成一条消息，并且不再重复正文**。
> 完整接入流程见 `QQ官方机器人接入指南.md`。整理于 2026-11-07。

---

## 1. 遇到的两个问题

1. **两条消息**：查 WM / 紫卡时，卡片图发一条、交易 `/w` 指令再发一条文本，用户要求合成一条。
2. **内容重复**：改成 Markdown 一条消息后，卡片图里本来就画了标题、`WM 在售`、排序说明、订单列表、来源网址，结果图片下面又把同样的正文发了一遍（Boltor Prime 截图）。用户明确要求：**图片下面只要两条 `/w` 字符串**。

---

## 2. 解决思路（一句话）

用 QQ 官方 **`msg_type = 2` 自定义 Markdown**：`markdown.content` 里同时写【一个图片标签】和【内容字符串】，客户端自己把它们渲染成**同一条消息**——上图片、下字符串。文字区只放可复制的 `/w`，因为全文已经在卡片图里了。

图片不放 base64、不放二进制进文本，而是给一个引用让客户端自己拉。**当前这个引用是 QQ 官方素材 `qqbot-media://`，不是任何公网网址、不是第三方图床** —— 你没指定过图床，所以一个都没用（详见第 4 节）。

---

## 3. 真正发出去的请求体

```
POST https://api.bot.qq.com/v2/groups/{group_openid}/messages
Authorization: QQBot <access_token>
X-Union-Appid: <appid>
Content-Type: application/json
```

```json
{
  "msg_type": 2,
  "markdown": {
    "content": "![img #618px #371px](qqbot-media://image/v1/<官方返回的引用>)\n\n英文：/w ItsSaulGoodman Hi! I want to buy: Boltor Prime for 49 platinum.\n中文：/w ItsSaulGoodman 你好，我想购买 螺钉步枪Prime 49 铂金."
  },
  "msg_id": "可选"
}
```

- 上面图片标签里就是**当前实际用的形态**：`qqbot-media://` 是 QQ 官方素材引用，不是网址。只有以后你自己填了公网图床，这里才会变成 `https://...`。
- 单聊把路径换成 `/v2/users/{user_openid}/messages`。
- `msg_id` **可选**：被动回复带上；主动普通群消息不带（不 @ 人、不引用）。
- `#618px #371px` 是按 PNG 原图宽高比例算出来的显示尺寸，宽度由 `WM_QQ_MD_IMAGE_WIDTH` 控制。
- 官方 2026/04/23 起，自定义 Markdown 对单聊 / 群聊所有机器人开放，**不需要模板审核**；只有 `template` 模式才要后台审核拿 `custom_template_id`。

---

## 4. 图片引用从哪来（关键）

> **先说清楚：你没有指定过任何图床 / 网盘 / Git 地址，所以一个都没用。**
> 当前实际用的是 **QQ 官方素材引用 `qqbot-media://`** —— 由 QQ 官方 API 返回的内部引用，
> **不是公网网址、不是第三方服务、不需要你提供任何链接**。
> 核对依据：`data/config.txt` 里所有图片地址项都还是注释状态（生效的只有
> `WM_QQ_MD_IMAGE_WIDTH`、`WM_QQ_MD_IMAGE_BUST`、`WM_QQ_MD_MEDIA_MAX` 三个数值项）；
> `data/md_image_state.json` 不存在 → 覆盖式上传一次都没跑过；
> `data/md_images.json` 里只有一条 `qqbot-media://image/v1/...`。
> 文档 / 代码里出现的 `https://你的网盘/...`、`https://example.com/...`、
> `raw.githubusercontent.com` 全是**占位示例或默认推导模板**，不是正在使用的地址。

Markdown 图片标签只认引用，**不能直接塞 base64 或二进制**。程序按这个优先级找：
`覆盖式公网图 → 静态 URL → 官方素材 → 自备 POST 图床`。前两项没配置，所以**每次都落到官方素材**。

**① 当前实际在用：官方素材 `qqbot-media://`**
本地 PNG 二进制走 QQ 官方【素材管理】三步上传：
`POST /v2/media-images/prepare` → `PUT <presigned_url>` 直传 PNG → `POST /v2/media-images/complete` 拿 `url`。
拿到的引用写进图片标签即可，全程只跟 `api.bot.qq.com` 打交道。按图片 SHA1 缓存，同一张卡片不重复上传；**清理阈值默认只留 3 张**（`WM_QQ_MD_MEDIA_MAX`），平台长期只占 1~3 张配额。你那张能正常显示的截图就是这条通道出来的。

**② 可选、默认未启用：覆盖式固定公网图**
只是预留能力：**你自己填了配置才会启用**，不填就永远不会碰任何第三方网址。若以后想用（网盘 / Git 仓库里永远只有一张 `card.png`，每次覆盖写、URL 不变），在 `python/data/config.txt` 填：

```ini
# 方式 A：通用 PUT（网盘 WebDAV / 对象存储 / R2）
WM_QQ_MD_IMAGE_URL        = https://你的网盘/wm/card.png   # 公开访问地址（占位示例，不是实际地址）
WM_QQ_MD_IMAGE_PUT_URL    = https://你的网盘/wm/card.png   # 上传端点
WM_QQ_MD_IMAGE_PUT_TOKEN  = Bearer xxxxx
# WM_QQ_MD_IMAGE_PUT_METHOD = PUT

# 方式 B：GitHub / Gitee Contents API（GET 取 sha → PUT 同路径带 sha 覆盖）
WM_QQ_MD_GITHUB_REPO   = 你的名/仓库
WM_QQ_MD_GITHUB_TOKEN  = ghp_xxxx
WM_QQ_MD_GITHUB_PATH   = wm/card.png
WM_QQ_MD_GITHUB_BRANCH = main
```

图片 SHA1 没变就不重复上传；变了才覆盖一次。地址不变可能显示旧图 → `WM_QQ_MD_IMAGE_BUST = 1` 自动加 `?v=<图片哈希>` 破缓存，**存储仍然只有一张**。官方没保证任意公网 https 图片都被 QQ 放行，所以这条通道必须自己跑 `--send` 实测（第 7 节）。

**③ 其余备选**：静态 `WM_QQ_MD_IMAGE_URL` / 自备 POST 图床 `WM_QQ_MD_IMAGE_UPLOAD`，同样默认未启用。

### 清理规则（默认 3 张）

| 问题 | 答案 |
| --- | --- |
| 什么时候触发 | 每成功上传**一张新卡片**后检查本地账本 `data/md_images.json`，超过 3 张就从最早上传的删起，保持平台始终 ≤ 3 张 |
| 同一张图反复查 | 命中 SHA1 缓存 → 不上传、也不触发删除，只占 1 张 |
| 删哪张 / 怎么删 | 按上传时间删最旧：`DELETE /v2/media-images/{image_id}/delete`。平台删除失败会把条目留回账本，下次上传新图再试，不会悄悄残留 |
| 影响已发消息吗 | 不影响，平台发送时已转存副本；只是该引用立即失效 |
| 想改成 1 张 / 5 张 | 改 `python/data/config.txt` 的 `WM_QQ_MD_MEDIA_MAX`（范围 1~95）后重启。1 张最省额度，但来回查不同物品会反复重传；3 张是折中 |
| 边界 | 只认本地账本；换设备 / 删过 `md_images.json` 后，平台上的历史遗留素材不会被自动扫到 |

---

## 5. 「不重复正文」是怎么落地的

| 环节 | 做法 |
| --- | --- |
| `split_reply()` | 从回复里拆出 `(标题, 正文, /w 指令, 来源)`；`ImageReply.tail` 里就是英中两条 `/w` |
| `build_native()` | **有图时直接返回** `![img ...](URL)` + 两条 `/w`，不再输出大标题、`WM 在售`、排序、订单列表、`🔗 来源` |
| `/w` 两行 | **绝不转义**，复制进游戏聊天框可以直接粘贴（正文才转义 `* _ [ ]` 等） |
| 纯文字回退（没图） | 只有主动发「文字 xxx」这种没图的情况，才输出完整标题 + 正文 + `/w` + 来源链接 |
| 模板模式 | 同理：`content` 只放 `/w`，`para1 / para2 / desc` 留空 |

---

## 6. 两道保险

- **绝不发「无图纯文字」**：有卡片图却拿不到图片引用时返回 `NO_IMAGE`，跳过毫无意义的 `msg_id` 重试，直接回退富媒体通道把图发出去（否则红 / 蓝 / 灰昵称全没了）。
- **发送失败自动回退**：Markdown 发不出去 → 回退 `msg_type=7` 富媒体图文混排 / 纯文本，`/w` 指令绝不丢。`WM_QQ_MD_FALLBACK = 0` 可只看平台返回码（但无图仍会强制回退）。

---

## 7. 三步用起来

1. `python/data/config.txt` 确认 `WM_QQ_MARKDOWN = native`（默认已开）。**图片那几项一个都不用填** —— 当前就是 QQ 官方素材通道，不需要任何网址。只有你主动指定图床时才填第 4 节 ② 的配置，token 只写 `data/` 下，别写会上传 GitHub 的 `config.txt` 模板。
2. 手机端把新的 `python/qq_markdown.py` 拷过去覆盖（连同 `render_card.py`、`md_probe.py`），**重启** `python qq_bot.py`。
3. 想再自检一次真实渲染：

```bash
cd wm-bot/python
python md_probe.py                                            # 不联网：打印最小请求体 + 生成网页预览
python md_probe.py --send --group <openid> --image media      # 当前实际通道（官方素材），发一条看 QQ
# 仅当你自己填了图床配置，才用下面两条；没填会拿不到 URL
python md_probe.py --upload --image overwrite                 # 真上传拿固定 URL，仍不发送
python md_probe.py --send --group <openid> --image overwrite  # 测公网图是否被放行
```

手机 QQ 里**图和字出现在同一条消息** = 转换成功。要关掉 Markdown：`WM_QQ_MARKDOWN = 0`，回到已验证的富媒体 `msg_type=7 + content` 图文混排。

> ⚠️ 如实说明：当前默认的官方素材通道，已经在你那张截图里验证过能正常显示；**公网图通道默认未启用**，官方也没保证任意 https 图片都被放行，只有你以后填了图床才需要跑一次 `--send` 确认。

---

## 8. 本次相关的坑

| 现象 | 原因 / 处理 |
| --- | --- |
| 红 / 蓝 / 灰昵称没了 | Markdown 文字**没有颜色语法**，颜色只存在于卡片图里；手机没装 Pillow 或没有中文字体时图渲染不出来 → 装 `pillow` / `noto-cjk-fonts`，图回来颜色就回来 |
| 图下面重复正文 | 跑的是旧 `qq_markdown.py` → 覆盖新版并重启 |
| 公网图不显示 | 客户端不放行该 URL → 用 `--image media` 对照，或设 `WM_QQ_MARKDOWN = 0` |
| 覆盖后还是旧图 | CDN / 客户端缓存 → 保持 `WM_QQ_MD_IMAGE_BUST = 1` |
| `40035007` 素材超限 | 程序自动删最旧素材；若以后启用了覆盖式固定图可根治（当前没启用） |

---

## 9. 相关文件

- `python/qq_markdown.py`：构造 `msg_type=2` 请求体、图片上传 / 素材管理、内容拆分、发送
- `python/qq_official.py`：`_dispatch_reply()` 里 Markdown 优先 → 失败回退富媒体
- `python/md_probe.py`：最小「一张图 + 一个内容字符串」真机自检
- `python/config.txt`（模板）/ `python/data/config.txt`（唯一生效配置）
- `Markdown消息效果预览.html`：完整行情卡片的新版式；`Markdown最小用例.html`：最小用例对照
