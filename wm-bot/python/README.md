# wm.py — Warframe Market 物品 / 紫卡查询（纯 Python，零依赖）

只用 Python 标准库，桌面 / 手机 Termux / QPython 通用。整个文件夹拷到任意位置都能跑：
路径全部基于 Python 文件自身目录（`__file__`）解析，不写死盘符、不依赖当前工作目录、无 `../` 上层路径。

**运行方式只有一种**：`python qq_bot.py`（接 QQ）或 `python wm.py "WM Boltor Prime"`（命令行查价）。
没有 `.bat` / `.sh` 启动脚本，没有 `set` / `export` 命令。

**配置分两份（上传 GitHub 前先看）**：
- 本目录 `config.txt` 是**纯净模板**：不含任何凭证，随仓库上传，别在里面填密钥；
- `data/config.txt` 才是**真正生效**的：把模板复制一份到 `data/` 下（文件名不变），
  再填 `WM_QQ_APPID` / `WM_QQ_SECRET`。程序只读 `data/config.txt`，只改模板不生效。
- 项目根目录 `.gitignore` 已排除整个 `data/`：密钥、`data/groups.txt` 群白名单、
  缓存和日志都不会被提交；`data/groups.txt` 也要自己在 `data/` 下新建
  （每行一个 group_openid，不建 = 默认不监控任何群）。

**改设置只动一个文件**：`data/config.txt`，每行「名称 = 值」，每项都有中文注释说明用途；
记事本改完保存、重跑 Python 即生效。见本目录 `使用说明.txt`。

## 配置（data/config.txt，唯一需要改的文件）
| 名称 | 默认 | 说明 |
|---|---|---|
| `WM_CACHE_MINUTES` | `60` | 缓存新鲜度（**分钟**），想改成 15 分钟就写 `15`；超间隔先联网刷新缓存再回复 |
| `WM_QQ_BACKEND` | `official` | `official`=官方 QQ 机器人 API；`onebot`=OneBot v11 社区方案 |
| `WM_QQ_APPID` | 空（必填） | q.qq.com 管理端的 appid |
| `WM_QQ_SECRET` | 空（必填） | clientSecret，**等同账号凭证，勿外泄 / 勿提交仓库** |
| `WM_QQ_GROUPS_FILE` | `data/groups.txt` | 群白名单文件（相对路径按脚本同目录解析） |
| `WM_QQ_ALLOW_ALL_GROUPS` | `0` | `0`=只监控白名单群（正式）；`1`=临时监控所有群（仅抓 openid 用） |
| `WM_QQ_API_BASE` | `https://api.bot.qq.com` | 官方 OpenAPI 根地址（一般不用改） |
| `WM_QQ_INTENTS` | `33554432` | 事件订阅位掩码（`1<<25`，已含群@/群全量/单聊/通知开关/进退群；免 @ 与主动发言是**群资料页开关**，不用改这里） |
| `WM_FULLMSG_ACTIVE` | `1` | 全量群消息（免 @）回复方式：`1`=单纯发一条普通群消息（**不 @ 人、不引用**），`0`=走被动引用回复 |
| `WS_ACTIVE_MIN_GAP` | `300` | **周期**推送最小间隔（秒）；内容不变一律不重发，防刷屏与官方频控 |
| `WS_PRIORITY_TYPES` | `alerts,events` | **全局默认**主推栏目；写中文 `警报,活动,入侵` 也行，`off` 关掉主推。聊天指令开关按会话覆盖它 |
| `WS_PRIORITY_MIN_GAP` | `60` | 主推防抖间隔（秒）；主推**不受**上面 300 秒限制 |
| `WS_PRIORITY_SKIP_IN_FULL` | `1` | 周期推送剔除主推栏目，避免同一条警报推两遍；`0` 关 |
| `WS_SWITCH_FILE` | `data/ws_switches.json` | 「活动=开启」等聊天指令开关的存盘位置；**设置一次重启后仍在** |
| `WS_SWITCH_PERSIST` | `1` | `1`=存盘（默认）；`0`=只本次运行有效 |
| `WS_AUTO_PRIORITY` | `0` | 启动时对白名单群批量自动注册主推。**默认关**；一般改用聊天指令，更灵活且能记住 |
| `WM_SSL_CAFILE` | 空 | 手机端缺 CA 根证书时手动指定 pem/crt 路径；留空自动探测 Termux / Android / Linux |
| `WM_SSL_NOVERIFY` | `0` | `1`=强制不校验证书（不推荐）；`0` 时只在完全找不到 CA 时临时兜底并警告 |
| `WM_QQ_MODE` | `client` | 仅 OneBot：`client`/`server` |
| `WM_QQ_URL` | `ws://127.0.0.1:6700` | 仅 OneBot：WS 地址或监听地址 |
| `WM_QQ_TOKEN` | 空 | 仅 OneBot：access_token |

> 程序读配置用 `config.py`（纯标准库）：已存在的环境变量优先于 `data/config.txt`，
> 这是给离线测试 / 临时覆盖留的口子，**日常不需要用**。
> ⚠️ `3~5 秒同域名访问间隔`、`1/3/7 分钟重试`、`全失败 1 小时` 是硬规则，
> **不提供任何配置项、不允许调小**（`test_pace_guard.py` 会验证）。
>
> 📱 **手机端证书报错**：若启动后一直刷 `CERTIFICATE_VERIFY_FAILED / unable to get local issuer
> certificate`，是手机 Python 缺 CA 根证书，不是网络问题。Termux 执行
> `pkg install ca-certificates` 后重跑即可；程序会自动探测 CA 路径，全找不到才临时
> 关闭校验并打印警告（有中间人风险）。详见 `data/config.txt`「四点八」与根 README「手机挂机」。

## 直接运行（CLI）
```bash
python wm.py "WM Boltor Prime"        # 查物品
python wm.py "ZK Tigris"              # 查紫卡
python wm.py "/报错 WM Boltor Prime"  # 强制再查一次
python wm.py                         # 打印帮助
```
> Git Bash 里 `/报错` 会被当路径，加引号或用 `python wm.py 报错 WM Boltor Prime`；从 QQ 进来不受影响。

输出示例：
```
【物品】Boltor Prime / 螺钉步枪Prime
WM 在售（共 707 条：🟢游戏中 37 · 🔵在线 8 · ⚪离线 662）
排序：🟢游戏中 → 🔵在线 → ⚪离线（同组内价格从低到高；离线太久没登录的沉底）
昵称颜色：游戏中=红 · 在线=蓝 · 离线=灰｜行首 [游戏中]/[在线]/[离线] 是状态标签
1. 🟢 Funnyfool — 47 铂金 ×1
2. 🟢 Insomnia — 48 铂金 ×3
...
（其余 692 条未列出，最低 40 铂金，多为离线卖家，价格低但多半联系不上）
🕒 数据更新于 2026-11-07T01:46:43Z（距今约 0分钟）· 来源：https://warframe.market/items/boltor_prime_set
游戏内发起交易（用第 1 家：🟢游戏中；想买别家把 ID 换掉即可）：
英文：/w Funnyfool Hi! I want to buy: Boltor Prime for 47 platinum.
中文：/w Funnyfool 你好，我想购买 螺钉步枪Prime 47 铂金.
```

> CLI / 纯文字模式是上面这个全文。QQ 里默认发**一条消息：上卡片图 + 下可复制文字**：
> 图里是价格列表（昵称按状态上色：游戏中红 / 在线蓝 / 离线灰，行首带 `[游戏中]` 等标签），
> 英中两条 `/w` 指令作为纯文本排在图片下方，长按就能复制。**指令不带任何来源备注**。
> 官方端默认走 `msg_type=2` Markdown 消息（**一张卡片图 + 图下两条可复制 `/w`**同属一条；
> 标题 / 统计 / 排序 / 订单 / 来源都在图里，**文字区不重复正文**），
> 图片首选「固定公网 URL + 覆盖式上传」：网盘/Git 里只存一张 `card.png` 反复覆盖；
> 没配才用官方素材管理换 `qqbot-media://` 引用兜底）；`WM_QQ_MARKDOWN = 0` 时改走
> `msg_type=7` + `content` 富媒体图文混排，OneBot 走「图片段 + 文本段」数组。
> **Markdown 文字没有颜色语法**，红蓝灰只存在于卡片图里：图片出不来颜色也就没了。

> **为什么不是纯价格排序**：WM 上大量卖家早已离线（部分甚至 2035 封号），报价是几个月前挂的。
> 现在固定 **游戏中 → 在线 → 离线**，同组内价格从低到高；离线且超过
> `WM_OFFLINE_STALE_DAYS`（默认 30 天）没登录的沉到最后并标「N 天前在线」。
> 紫卡里「只有起拍价、没有一口价」的拍卖也排在同组后面并标「仅竞价」——
> 起拍 1 铂金不等于能 1 铂金买到。已流拍（closed）的拍卖直接跳过。

## 作为模块复用
```python
from wm import query, format_reply, get_cached_or_refresh
res = get_cached_or_refresh("item", "Boltor Prime")   # 超间隔先刷新再回复
print(format_reply(res))
```

## 行为规则
- **按需缓存**：只缓存用户实际查过的 `类型:名称`，落盘 `data/cache.json`；**不预拉全站物品清单**、不自启轮询。
- **用户发起才更新**：缓存未超 `data/config.txt` 的间隔 → 直接秒回；超过间隔（哪怕隔了几天 / 几个月）
  → **先联网刷新缓存再回复**，绝不发旧缓存。
- **刷新失败不假死**：单次尝试失败立即明确报错（不拿旧数据顶包），
  **1/3/7 分钟重试（最多 4 次）放后台线程**继续，成功后自动写缓存；
  避免真实源挂掉时主线程卡 11 分钟、错过 QQ 被动回复 5 分钟窗口。
- **礼貌限流（文件持久）**：每个 HTTP 请求（含订单列表）访问 `api.warframe.market` 前都保证间隔 3~5 秒，
  时间戳写 `data/pace.json`（跨进程 / 重启仍生效，绝不回拨）。**不得删除该文件**。
- **重试**：CLI 手动运行走完整 1/3/7 分钟阻塞重试（最多 4 次）；机器人走后台重试。
- **名称 → slug 按需探测**：只针对用户输入的这【一个】名称生成少量候选，**不拉全站清单**。
  `Boltor Prime` 优先试 `boltor_prime_set`（WM 上挂订单的是套装 / 部件，父名称 `boltor_prime`
  会返回 `404 app.item.notFound`）；命中的 slug 记进 `data/slug.json`，以后同物品只发 1 个请求。
  物品名写全（`Boltor Prime Set` / `Boltor Prime Blueprint` / `Saryn Prime Systems`）可少发一次请求；
  需用标准英文 slug；**中文名现已支持**：经本地翻译表 `data/i18n.json` 翻成英文再去查价，
  可用 `翻译 <名称>` 查中英对照、`翻译 同步` 拉全量词库（约 3447 条，之后纯本地）。
  未收录的中文名先发 `翻译 同步` 或直接发 `翻译 <名称>` 让机器人联网建翻译——这是"不拉全站清单"的取舍。
  每个候选请求仍各自走 3~5 秒同域名节流；只有明确的 404 才试下一个候选，超时 / 5xx 立即失败。
- **确定性错误不重试**：物品名不对（全部候选 404）会直接提示改用完整名称，
  不启动 1/3/7 分钟后台重试、CLI 也不空跑重试——避免对同域名做无意义请求。

## QQ 对接（统一入口 qq_bot.py，纯标准库）
两种后端共用 `qq_common.py`（消息判定 / 群白名单 / WM 触发）：
- **官方 QQ 机器人 API**（`qq_official.py`，默认）：正规无封号风险，需在 q.qq.com 申请 appid / secret；
  **2026 年官方已支持普通 QQ 群**，群的标识是 `group_openid`（字符串，不是数字群号）。
- **OneBot v11**（`onebot.py`）：社区方案，普通群可用，但腾讯不官方支持、**有封号风险**。

**接入步骤**
1. 打开 https://q.qq.com 创建机器人；把 `config.txt` 模板复制到 `data/config.txt`，把 **appid** / **clientSecret** 填进去
   （「设置 AI 服务」那步跳过：我们是原创机器人，不绑第三方 AI 框架）。
2. 把机器人拉进你的普通群（群主操作）。平台那些「公开服务 / 群管理权限」开关都不用开。
3. 拿该群 **group_openid**：把 `data/config.txt` 里 `WM_QQ_ALLOW_ALL_GROUPS` 临时改成 `1`
   （或直接 `python qq_bot.py --allow-all-groups`），群里**从 @ 列表点选机器人**发 `WM Boltor Prime`，
   打开 `data/bot_flow.log` 抄 `群@消息 group_openid=...` 那串字符串，然后关掉程序、把开关改回 `0`。
4. 把 openid **手工**每行一个写进 `data/groups.txt`，保存后 `python qq_bot.py` 即可。

> 群内**默认要 @ 机器人**（从弹出的 @ 列表点选，只手打昵称不会触发 `GROUP_AT_MESSAGE_CREATE`）。
> 想**免 @**、并让监控能主动推送，要在手机 QQ 的【群资料页 → 机器人设置】按群单独开两个开关：
>
> | 设置项 | 作用 | 对应事件 |
> |---|---|---|
> | 机器人可获取的群聊消息范围 = **获取群内全部消息** | 群里每条消息都推给机器人，直接发 `WM <名称>` 即可 | `GROUP_MESSAGE_CREATE`（文档：`group_message_create.html`） |
> | 机器人主动在群聊内发言（蓝色开关） | 定时监控能主动发消息 | 开启时 `GROUP_MSG_RECEIVE`，关闭 `GROUP_MSG_REJECT` |
>
> 两者互不替代、每个群都要单独开；`WM_QQ_INTENTS` 保持 `33554432 (1<<25)` 即可，不用改。
> 未开第一个开关时仍只收 @消息，代码两种事件都接、都走同一套 handler。
> 免 @ 消息的回复是**单纯一条普通群消息**（`WM_FULLMSG_ACTIVE=1`：不带 `msg_id`、不 @ 人、
> 不显示引用）；@ 消息才走被动引用回复，两条通道失败互相回退。因为每条群消息都能回，
> 暂存的监控内容也能借任意一条聊天消息带出，效果等同主动推送（20 秒限频）。
> 私聊不受白名单限制，直接发 `WM <名称>` 即可；单聊的主动消息开关在机器人资料卡。
> 全程写覆盖式日志 `data/bot_flow.log`（每次启动清空重写，不含 secret）。

**回复形式与监控（两种后端通用）**
- **默认一律发卡片图**：世界状态 / WM 查价 / 紫卡 / 翻译 / 帮助都渲染成白底 PNG；
  官方端默认走「素材管理三步上传 → `qqbot-media://` 引用 → `msg_type=2` Markdown」，
  `WM_QQ_MARKDOWN = 0` 时改走「`/files` 上传拿 `file_info` → `msg_type=7`」，
  OneBot 走 message 段数组 + `base64://`；任一步失败自动退回纯文字。
  ⚠️ 画图依赖 **Pillow + 中文字体 + `render_card.py` 这个文件**。三者缺一就退回纯文字，
  表现为「回复变成一大段字、红/蓝/灰昵称全没了」（平原/赏金/WM 会同时如此）。
  群里发「自检」机器人会直接回一段文字说明缺哪样；启动时 `[card] 图片能力自检` 也打印同样结论。
  手机端修法：Termux `pip install pillow` + `pkg install noto-cjk-fonts`，
  或把字体路径写进 `WM_CARD_FONT`；QPython 同理。**拷文件时别漏 `render_card.py`**。
- **Markdown 消息（官方端默认 `WM_QQ_MARKDOWN = native`）**：一条消息 =
  「完整卡片图」+「图片下方两条可复制 `/w` 指令」。★ 卡片图里已画好标题、行情统计、
  排序说明、订单列表、来源网址，**图下面不再重复正文**（真机截图反馈后修正）；
  只有「文字 xxx」纯文字回退才输出完整标题 + 正文 + `/w` + 来源。官方 2026/04/23 起
  自定义 Markdown 已对单聊/群聊所有机器人开放，无需模板审核。
  **图片首选固定公网 URL + 覆盖式上传**（跟 HTML 邮件托管图片同原理）：网盘 / Git 仓库 /
  对象存储里永远只有一张 `card.png`，每次覆盖写，URL 永不变、不占免费额度。两种零依赖后端：
  ① 通用 PUT 覆盖 `WM_QQ_MD_IMAGE_PUT_URL` + `WM_QQ_MD_IMAGE_URL`（+ 可选 TOKEN / METHOD）；
  ② GitHub/Gitee Contents API 覆盖 `WM_QQ_MD_GITHUB_REPO / TOKEN / PATH / BRANCH`，
  公开链接固定为 raw 地址。覆盖后地址不变可能显示旧图，`WM_QQ_MD_IMAGE_BUST = 1`
  给 URL 加 `?v=<图片哈希>` 破缓存，存储仍只有一张、不产生新图。
  没配覆盖式 → 官方**素材管理**（`prepare` → `PUT` → `complete`）换 `qqbot-media://`
  → 自备 POST 图床兜底（按 SHA1 缓存；`WM_QQ_MD_MEDIA_MAX` 默认只留 3 张，上传新图后删最旧的）。
  平台文字样式**没有颜色**，颜色仍靠卡片图。**素材拿不到引用就直接回退富媒体发图，
  不会发出「无图纯文字」**；发送失败同样自动回退富媒体/纯文本，不会丢消息。
  **真机实测**：`python md_probe.py`（dry-run 看最小请求体 + 网页预览）→
  `python md_probe.py --send --group <openid> --image overwrite` 发「一张图 + 一个内容字符串」
  看 QQ 是否真渲染；不渲染换 `--image media` 对照或设 `WM_QQ_MARKDOWN = 0`。
  模板模式设 `WM_QQ_MARKDOWN = template` 并填 `WM_QQ_MD_TEMPLATE_ID`（官方示例八键：
  `title / image / para1 / para2 / desc / content / link_introduction / link`）。
- **世界状态卡片排版**：`裂缝` / `钢铁裂缝` 分「常规星球（上）」「九重天（下）」两块，
  各自按 **古纪 → 前纪 → 中纪 → 后纪 → 安魂 → 全能** 排序、同档再按剩余时间升序；
  九重天（XX比邻星域）单独成组，不与常规星球混排。`平原` 一次给五处昼夜，
  **每处时间行后紧跟该平原自己的赏金任务板**（希图斯→Ostron / 奥布山谷→索拉里斯 /
  魔胎之境→英择谛），单发「希图斯」等单词也带。`全部` 精简时裂缝两块各露面 2 条、
  每处赏金 2 条；要看全的请发单条指令。
- **遗物 / 掉落来源（官方中文数据）**：`遗物 后纪B10` 给该遗物四档精炼
  （完好/卓越/无瑕/光辉）的全部奖励与概率；`遗物 A1` 先告诉你这个编号属于哪些纪，
  免得查错；`掉落 布里斯提卡Prime蓝图` 反查它出自哪些遗物（稀有度 + 概率），
  `掉落 布莱顿` 给制作材料 / 信用点 / 工时。数据来自 DE 官方 `PublicExport`
  中文导出：先取 `index_zh.txt.lzma` 清单拿**当前**哈希，再抓遗物 / 配方 / 物品表，
  奖励里的 `uniqueName` 用同一批物品表反查中文名（19208 条引用只 1 条查不到），
  名字都是官方中文、不用自己编。索引落 `data/drops.json`（约 0.4 MB，800 个遗物 /
  1474 条配方），查询只读本地零请求；`WS_DROPS_DAYS`（默认 7 天）过期后台重抓，
  重抓失败沿用旧索引并在回复里标「N 天前」，不假装是新数据。首次构建顺序抓 8 个文件、
  守同域名 3~5 秒节流（约 30 秒），启动时 `drops.ensure_async()` 后台预抓。
- **监控推送按栏目 @ 到人**：同一条内容被多人订阅 → 一条消息里一起 @（`@甲 @乙 📢 警报`）；
  各人要的栏目不同 → 本来就是按栏目拆开的两条消息，各 @ 各的人。会话级订阅
  （`活动=开启` 这类全群开关）没有具体人，不 @。发送在推送线程里**串行排队**，
  相邻两条停 `WS_PUSH_INTERVAL`（默认 0.6 秒），不连发刷频控。
- **WM 交易回复 = 一条消息（卡片图 + 可复制文字）**：图里是价格列表，卖家昵称按状态上色
  （**游戏中红 / 在线蓝 / 离线灰**），行首 `[游戏中]`/`[在线]`/`[离线]` 是状态标签；
  英中两条 `/w` 发起聊天指令作为纯文本与图片同发，长按就能复制，不带任何来源备注。
  官方端走富媒体 `msg_type=7` + `content` 图文混排，OneBot 走 message 段数组，都是一条；
  官方端混排被拒或设 `WM_QQ_MIX_MESSAGE = 0` 时才退回图一条 + 文本一条。
  要复制正文（裂缝节点名等）仍可在指令前加「文字」：`文字 裂缝` / `文字 WM Boltor Prime`。
  永久改行为：`data/config.txt` 的 `WM_WS_CARD = always|auto|off`。
- **★ 发消息直接开关主推（推荐）**：群里 / 私聊发 `活动=开启`、`警报=关闭` 立即生效，
  **不用改配置、不用重启**。开启 = 自动纳入本会话监控并设为主推；关闭 = 不主推且周期推送里也剔除。
  也支持 `alerts=on` / `events=off` / 全角 `活动＝开启` / `活动 开启` / 多组 `活动=开启，警报=开启`；
  `主推=开启` 一键开默认栏目、`主推状态` 查看当前开关。
  设置按会话存到 `data/ws_switches.json`，启动时 `load_switches()` 自动恢复并重新注册监控，
  不同会话互不影响，也**不覆盖**手动 `监控 <词>` 加的栏目。发 `停止监控` 会停止并清除该会话设置。
- **监控 = 警报/活动主推 + 周期兜底 + 随消息捎带**：`监控 <词>` 后每 `WS_PUSH_SECONDS` 秒
  **检查**一次（10 秒是节拍，不是发送周期）。`监控 <词>` 是一次性注册（重启清空），
  想「设置一次一直在」用上面的开关指令；`WS_AUTO_PRIORITY = 1` 可批量自动注册（默认 `0`）。
  - **主推**：`警报` / `活动` 出现新条目、奖励或描述变化时**立刻单独发一条精简消息**，
    不受 `WS_ACTIVE_MIN_GAP` 300 秒限制，只受 `WS_PRIORITY_MIN_GAP`（默认 60 秒）防抖。
    刚开监控只记基线、**不补发历史**；条目结束静默（不推「活动结束」）；发送失败会保留基线
    自动重试并进暂存，新警报不会丢。栏目由 `WS_PRIORITY_TYPES` 配，写 `off` 可关掉主推。
  - **周期**：其余栏目「内容变了才推」，且距上次推送超过 `WS_ACTIVE_MIN_GAP`（默认 300 秒）。
    签名会剔除「剩余：5分30秒」这类倒计时，倒计时跳动不再刷屏（旧版实际每 5 分钟固定推一条，
    已修复）；默认把主推栏目从周期里剔除（`WS_PRIORITY_SKIP_IN_FULL = 1`），不重复贴。
  - **两手准备**：先试主动发送（默认发卡片图，发不出去回退纯文本）；群资料页没开
    「主动在群聊内发言」时固定 `40034105`，内容进暂存区，等本会话有人发消息时拼在回复
    末尾一起出图（开了全量消息后任意一条群消息都能当这个窗口）。
    官方频控：单关系 20 qpm、每群每天 1000 条，别把 `WS_ACTIVE_MIN_GAP` 调到 0。
  暂存保鲜 `WS_ATTACH_TTL`、附带间隔 `WS_ATTACH_GAP`、长度上限 `WS_ATTACH_MAX_BYTES`，
  总开关 `WS_ATTACH_ON_QUERY`；主动通道失败后退避 5 分钟，收到 `*_MSG_RECEIVE` 会立刻重置并补推。

**官方后端要点**
- 鉴权：`POST /app/getAppAccessToken` 拿 `access_token`（7200s，自动刷新），WebSocket Identify；
- 事件（全部属于 `1<<25`）：`GROUP_AT_MESSAGE_CREATE`（群@）/ `GROUP_MESSAGE_CREATE`（群全量，免 @）/
  `C2C_MESSAGE_CREATE`（单聊）/ `GROUP_MSG_RECEIVE|REJECT` 与 `C2C_MSG_RECEIVE|REJECT`（通知开关）/
  `GROUP_ADD_ROBOT|GROUP_DEL_ROBOT|FRIEND_ADD|FRIEND_DEL`（关系变更）；
  开关事件体：群用 `group_openid` + `op_member_openid`，单聊用 `openid`（不是 `user_openid`）。
- 被动回复带 **`msg_id`（取事件 `d.id`，全量群消息文档明确「可用于被动回复」）**，5 分钟有效；
  普通群@/单聊**不能**用 `event_id`，用错报 `40034027`；非 @ 群消息被动回复若被拒，
  自动去掉 `msg_id` 改走主动通道重试一次；
- 来源显示 WM 完整网址：物品 `https://warframe.market/items/<slug>`、紫卡 `https://warframe.market/auctions/riven/<slug>`。

**OneBot 拓扑**：`WM_QQ_MODE=client` 正向 WS（脚本连 OneBot，PC 同机）；
`WM_QQ_MODE=server` 反向 WS（脚本起服务端等 OneBot 连，手机常用，配合 `WM_QQ_URL`）。

## 群白名单（两种方案通用）
- 默认**不监控任何群**；只有写进 `data/groups.txt`（每行一个，`#` 注释）的群才监听；
- 白名单群内仅 `WM` / `ZK` / `翻译`（带不带斜杠均可）/ `/报错` / `/查询` 触发，闲聊一律静默；
- 私聊不受限制；修改后重启程序生效。**纯手工填写，程序不会自动发现 / 自动写入 openid**。

## 离线测试（不连真实 QQ、不发 HTTP）
```bash
python test_config.py          # data/config.txt 解析 / 开关 / 路径 / 无启动脚本
python test_cache_policy.py   # 超间隔先刷新、90 天旧缓存不发、失败不假死、真等 3~5s
python test_slug_resolution.py  # 名称→slug 候选、确定性 404、候选间真等 3~5s
python test_official.py       # 官方对接 35 项（含 msg_id / 图文混排请求体校验）
python test_onebot.py         # OneBot 8 项（含出图段数组 / 纯文本回退）
python test_ws_attach.py      # 监控暂存 / 附带 / 限频 / 截断 / 退避 45 项
python test_ws_priority.py    # 警报/活动主推：基线 / 立即推 / 防抖 / 失败重试 / 捎带 / 稳定签名 69 项
python test_ws_switch.py      # 「活动=开启」聊天开关：解析 / 会话覆盖 / 存盘重启 / 白名单 83 项
python test_ws_layout.py      # 裂缝分组排序 + 平原带赏金 + 状态标签隔离 107 项
python test_ssl_compat.py     # 手机 CA 证书：探测 / 降级重试 / 请求签名 / 取 token 恢复 34 项
python test_qq_markdown.py    # Markdown 消息：覆盖式固定公网图 / 素材三步上传 / 八键模板 / 图下不重复正文只留 /w / 无图不退化 / 3 张滚动清理 / probe 123 项
python md_probe.py           # Markdown 真机自检：一张图 + 一个内容字符串（默认 dry-run，不联网）
python test_card.py           # 卡片渲染 35 项
python test_drops.py          # 遗物/掉落：指令解析 / 索引构建 / 奖励概率 / 反查 / 缓存过期 48 项
python test_ws_scope.py       # 监控会话隔离：成员级订阅 / 停止全部 / 分栏目 @ 人 52 项
python test_pace_guard.py     # 硬时间规则锁死（3~5s 节流 / 1/3/7min 重试 / 1h）
```

## 接口（已实测 2026-11-06）
- 物品：`GET /v2/orders/item/{slug}`（`data[]`，筛 `type=sell`；卖家 `user.ingameName`、价格 `platinum`）；
  slug 由用户输入名称规范化 + 有限候选探测，**不拉全站清单**。
  实测：`boltor_prime` → `404 app.item.notFound`，`boltor_prime_set` → `200` / 686 条在售订单。
- 紫卡：`GET /v1/auctions/search?type=riven&weapon_url_name={slug}`（`payload.auctions[]`）
- 必须带 `User-Agent` 头，否则 403。
- 官方公开限流 3 请求/秒；本项目 3~5 秒一发，远低于该限制，不存在需要绕开的前提。
