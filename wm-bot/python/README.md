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
| `WM_QQ_INTENTS` | `33554432` | 事件订阅位掩码（群@消息 + 单聊） |
| `WM_QQ_MODE` | `client` | 仅 OneBot：`client`/`server` |
| `WM_QQ_URL` | `ws://127.0.0.1:6700` | 仅 OneBot：WS 地址或监听地址 |
| `WM_QQ_TOKEN` | 空 | 仅 OneBot：access_token |

> 程序读配置用 `config.py`（纯标准库）：已存在的环境变量优先于 `data/config.txt`，
> 这是给离线测试 / 临时覆盖留的口子，**日常不需要用**。
> ⚠️ `3~5 秒同域名访问间隔`、`1/3/7 分钟重试`、`全失败 1 小时` 是硬规则，
> **不提供任何配置项、不允许调小**（`test_pace_guard.py` 会验证）。

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
【物品】Boltor Prime
WM 在售（价格从低到高，共 291 条）：
1. prototype33333 — 5 铂金 ×1
...
🕒 数据更新于 2026-11-06T05:46:33Z（距今约 0分钟）· 来源：https://warframe.market/items/boltor_prime
游戏内发起交易：/w <对方游戏ID> Boltor Prime 5 铂金
```

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

> 群内**必须 @ 机器人**（从弹出的 @ 列表点选，只手打昵称不会触发 `GROUP_AT_MESSAGE_CREATE`）；
> 私聊不受白名单限制，直接发 `WM <名称>` 即可。
> 全程写覆盖式日志 `data/bot_flow.log`（每次启动清空重写，不含 secret）。

**官方后端要点**
- 鉴权：`POST /app/getAppAccessToken` 拿 `access_token`（7200s，自动刷新），WebSocket Identify；
- 事件：`GROUP_AT_MESSAGE_CREATE`（群@）/ `C2C_MESSAGE_CREATE`（单聊）；
- 被动回复带 **`msg_id`（取事件 `d.id`）**，5 分钟有效；普通群@/单聊**不能**用 `event_id`，用错报 `40034027`；
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
python test_official.py       # 官方对接 10 项（含 msg_id 请求体校验）
python test_onebot.py         # OneBot 7 项
python test_pace_guard.py     # 硬时间规则锁死（3~5s 节流 / 1/3/7min 重试 / 1h）
```

## 接口（已实测 2026-11-06）
- 物品：`GET /v2/orders/item/{slug}`（`data[]`，筛 `type=sell`；卖家 `user.ingameName`、价格 `platinum`）；
  slug 由用户输入名称规范化 + 有限候选探测，**不拉全站清单**。
  实测：`boltor_prime` → `404 app.item.notFound`，`boltor_prime_set` → `200` / 686 条在售订单。
- 紫卡：`GET /v1/auctions/search?type=riven&weapon_url_name={slug}`（`payload.auctions[]`）
- 必须带 `User-Agent` 头，否则 403。
- 官方公开限流 3 请求/秒；本项目 3~5 秒一发，远低于该限制，不存在需要绕开的前提。
