# WM 机器人（纯 Python 单端 · QQ 查价 · 按需缓存）

> ⚠️ 本项目**只维护 Python 端**（零依赖，仅标准库）。Node 端已删除。
> 整个文件夹可拷到电脑 / 手机（Termux / QPython）**任意位置**：路径全部基于 Python 文件自身目录
> （`__file__`）解析，不写死盘符、不依赖当前工作目录、无 `../` 上层路径。
> **没有 `.bat` / `.sh` 启动脚本，没有 `set` / `export` 命令。**

**怎么跑**：`python qq_bot.py`（接 QQ）或 `python wm.py "WM Boltor Prime"`（命令行查价）。

**配置分两份（上传 GitHub 前务必看）**：
- `wm-bot/python/config.txt`：**纯净模板**，不含任何凭证，随仓库上传，给别人照着改；
- `wm-bot/python/data/config.txt`：**真正生效**的配置文件。首次使用把模板复制一份到 `data/` 下
  （文件名不变），再填 `WM_QQ_APPID` / `WM_QQ_SECRET`。程序**只读这一份**，只改模板不生效。
- 项目根目录 `.gitignore` 已排除整个 `data/`：密钥、`data/groups.txt` 群白名单、缓存与日志
  都不会被提交。上传时不用额外操作，**别手动把 `data/` 加进版本控制**。
- `data/groups.txt` 也要自己新建（每行一个 group_openid）；不建 = 默认不监控任何群。

**怎么改设置**：只改 `wm-bot/python/data/config.txt` 一个文件——每行「名称 = 值」，每项都有中文注释；
记事本改完保存、重跑 Python 即生效。详见 `wm-bot/python/使用说明.txt` 与 `wm-bot/python/README.md`。

常用配置项（都在 `data/config.txt`，有注释）：
| 名称 | 默认 | 说明 |
|---|---|---|
| `WM_CACHE_MINUTES` | `60` | 缓存新鲜度（**分钟**），改数字即可，如 `15` |
| `WM_QQ_APPID` / `WM_QQ_SECRET` | 空（必填） | q.qq.com 申请的机器人凭证 |
| `WM_QQ_BACKEND` | `official` | `official` 官方 / `onebot` 社区方案 |
| `WM_QQ_ALLOW_ALL_GROUPS` | `0` | 正式运行必须 `0`；抓群 openid 时临时 `1` |

`data/config.txt` 含 AppSecret：它所在的 `data/` 已排除，仍别截图、别发群、别传网盘。

## ✅ 已确认接口（实测 apiVersion 0.25.0）
1. **WM = Warframe Market**（warframe.market）。
2. **物品**：`GET /v2/orders/item/{slug}` → `data[]`（筛 `type=sell`；卖家 `user.ingameName`、价格 `platinum`、数量 `quantity`）。
3. **紫卡 = Riven**：`GET /v1/auctions/search?type=riven&weapon_url_name={slug}` → `payload.auctions[]`。
4. **名称 → slug 按需探测**：只针对用户输入的这【一个】名称生成少量候选，**不预拉全站清单**。
   `Boltor Prime` 会优先试 `boltor_prime_set` —— WM 上真正挂订单的是「套装 / 部件」，
   父名称 `boltor_prime` 会返回 `404 app.item.notFound`；命中的 slug 记进 `data/slug.json`，
   以后同一物品只发 1 个请求。紫卡去掉 `Prime` 变体。
   缓存只按用户实际查过的 `类型:名称` 建立。
5. **游戏内交易指令**：`/w <对方游戏ID> <完整名称> <价格> 铂金`。

## 指令（聊天文本）
- `WM <完整名称>`（如 `WM Boltor Prime`）— 查物品；也支持中文名（如 `WM 螺钉步枪`，先翻英文再查价）
- `ZK <完整名称>`（如 `ZK Tigris`）— 查紫卡
- `翻译 <名称>`（如 `翻译 Boltor` / `翻译 螺钉步枪`）— **只做名称的中↔英翻译**，
  回复给中英对照 + 官方英文 wiki / 灰机中文 wiki 双链接；之后 `WM/ZK` 也能用中文名查价
- `翻译 同步` — 联网拉取全量中英对照词库（WFA_Lexicon，一次即可，之后查询不再联网）
- `/报错 WM <名称>` — 数据有误，强制阻塞再查一次
- 兼容旧别名：`/查询`、`/wm` + `物品|紫卡|ZK`（`wiki` 指令已删除，请用「翻译」）

> **重要边界**：wiki 页面本身还有「获取方式 / 锻造材料 / 数值」等大量正文，
> 机器人**抓不到也不提供**（两站 Cloudflare 反爬 + 页面由模板拼装）。
> 「翻译」指令只回名称对照和两个链接，要看正文请自己点开链接。

## ⚠️ 两类计时器（用户硬性要求，必须分清）
1. **同域名访问间隔（3~5 秒）**：真实源 `warframe` **和**离线 `mock` 演示都走 `pace_gate`，**永不快进**，
   `WM_MIN_GAP` / `WM_MAX_GAP` 在两种模式下都只允许调高、不允许压低。
   —— "演示真实才能反应问题"；`test_pace_guard.py` 把时间参数压到秒级，断言仍锁死硬下限。
2. **失败重试等待（1/3/7 分钟）**：`warframe` 真实源锁死；仅离线 `mock` 测试默认快进为 1/2/3 秒
   （仍演示"4 次重试"结构，不卡 11 分钟）。
   - **结论：演示里"访问 WM 域名"要真等 3~5 秒；只有"源挂了之后的重试等待"可快进。**

## 缓存与重试规则
- **按需缓存**：只缓存用户实际查过的 `类型:名称`，落盘 `data/cache.json`；不拉全站清单、不自启轮询。
- **用户发起查询时才更新**：缓存未超 `data/config.txt` 里的 `WM_CACHE_MINUTES` → 直接回缓存（秒回）；
  **超过间隔**（哪怕隔了几天 / 几个月没人查）→ **先联网刷新缓存、再回复用户**，绝不发旧缓存。
- **刷新失败不假死**：用户这次只尝试 1 次，立即收到明确报错（**不拿旧数据顶包**）；
  **1/3/7 分钟重试（累计最多 4 次）交给后台线程**继续，成功后自动写缓存，稍后重发同一指令即可秒回。
  这样真实源挂掉时主线程不会卡 11 分钟、也不会错过 QQ 被动回复的 5 分钟窗口。
  （手动跑 `python wm.py` 的 CLI 仍走完整阻塞重试，人工等得起。）
- **每个 HTTP 请求**（含订单列表）访问前都先过 `pace_gate` 3~5 秒节流，没有例外。
- **回复格式**：
  - 第一条 `【物品】英文名 / 中文名`（有中文名才加 `/`，如 `【物品】Boltor / 螺钉步枪`；
    战甲在中文 wiki 多沿用英文名，无中文名时只显示英文）。
  - 订单列表（价格从低到高，最多 15 条）+ `🕒 数据更新于 <时间>（距今 X）· 来源：<WM 完整网址>`。
  - **两条**游戏内发起交易指令：英文
    `/w <卖家> Hi! I want to buy: <英文名> for <价> platinum.`，
    中文 `/w <卖家> 你好，我想购买 <中文名> <价> 铂金.`；价格一致，来源行保持原样，不附加任何来源署名后缀。

## 路径与运行
- 数据文件都在 Python 文件同目录的 `data/`：`cache.json`、`pace.json`、`groups.txt`、`i18n.json`（中英翻译表）、`lexicon.json`（同步的全量词库）、`bot_flow.log`；
- `config.py` 读取 `wm-bot/python/data/config.txt`（UTF-8，支持注释 / BOM），把值交给各 Python 模块；
  同目录的 `config.txt` 只是可上传的纯净模板，程序不会读它；
- 时间戳固定写 `data/pace.json`，**不得删除**——删掉等于抹去上次访问时间，属规避规则。

## 本地回归检测（离线，不联网）
```bash
python test_config.py         # 配置文件解析 / 开关 / 路径 / 无启动脚本
python test_pace_guard.py     # 硬下限：3~5s 节流 / 1/3/7min 重试 / 1h 全失败
python test_cache_policy.py  # 超间隔先刷新、90 天旧缓存不发、失败不假死、真等 3~5s 端到端
python test_slug_resolution.py  # 物品名→slug 候选 / 确定性 404 / 候选间真等 3~5s
python test_official.py      # 官方 QQ 对接 10 项
python test_onebot.py        # OneBot 对接 7 项
python test_i18n.py          # 翻译指令 / wiki API 解析 / 全量词库 / 双语回复 / 中文名查价 27 项
```

## 手机挂机（Termux / QPython）
1. **先关掉电脑上还在运行的 `python qq_bot.py` 窗口**。同一个 AppID / Secret 只能有一个在线长连接，
   电脑和手机同时跑会互相顶掉、群事件收不到。`data/config.txt` 与 `data/groups.txt` 都不用改：
   凭证和群 openid 跟机器人 AppID 走，换设备不变。
2. 拷到手机任意目录（路径基于脚本自身目录，放哪都行）。**必带**：
   `qq_bot.py`、`qq_official.py`、`qq_common.py`、`onebot.py`、`config.py`、`data/config.txt`（填好凭证的那份）、`wm.py`、
   `data/groups.txt`。`data/cache.json` / `pace.json` / `slug.json` 可带可不带（不带则第一次查询自建）。
   **不用带**：6 个 `test_*.py`、`__pycache__/`、`data/bot_flow.log`、
   `data/items_index.json`（旧版全站索引残留，代码已不读）、`data/cache.json.mockbak`（测试备份）——
   这些都不参与运行，拷过去只是占空间。
3. Termux：`pkg install python` 后 `python qq_bot.py`；QPython 用「运行脚本」打开 `qq_bot.py`。
   挂机先执行 `termux-wake-lock`（可配合 `tmux`），否则锁屏后进程被杀、长连接断开。
   手机需能访问 `api.bot.qq.com` 与 `api.warframe.market`；建议打开系统自动校时
   （若手机时钟比拷过去的缓存时间早，程序会按「缓存过期」联网刷新，不会发旧数据）。
4. 接 QQ 只有一种入口：`python qq_bot.py`（纯标准库零依赖）：
   - **官方 QQ 机器人 API**（默认）：正规无封号风险，需在 q.qq.com 申请 appid / secret；
     **2026 年官方已支持普通群**（用 `group_openid`，不是数字群号）。
   - **OneBot v11**：把 `data/config.txt` 里 `WM_QQ_BACKEND` 改成 `onebot`；有封号风险。
5. **群白名单**：默认不监控任何群，只有把群 openid 手工写进 `data/groups.txt` 的群才监听并回复；
   群里必须从「@ 列表点选机器人」才会触发事件。

## 目录
- `wm-bot/QQ群接入与授权操作说明.md`：**单独文档**，专讲加群、平台「添加群号授权」、群里「设置权限」、
  数字群号 vs `group_openid`、白名单与群相关排障；群这块配置较繁琐时直接看它
- `wm-bot/QQ官方机器人接入指南.md`：整体接入流程（凭证、启动、配置、通用排错）
- `wm-bot/python/wm.py`：查价 / 按需缓存 / 节流 / 名称 slug（CLI + 可 import）
- `wm-bot/python/config.txt`：可上传的**纯净模板**（带中文注释，凭证两项留空）
- `wm-bot/python/data/config.txt`：**唯一生效**的配置文件（由模板复制后填写，不上传）；`config.py`：读取它的纯标准库模块
- `.gitignore`：排除整个 `data/`，上传 GitHub 时无需额外操作
- `wm-bot/python/qq_bot.py` / `wm-bot/python/qq_official.py` / `wm-bot/python/onebot.py` / `wm-bot/python/qq_common.py`：QQ 对接
- `wm-bot/python/test_config.py` / `test_pace_guard.py` / `test_cache_policy.py` / `test_slug_resolution.py` / `test_official.py` / `test_onebot.py` / `test_i18n.py`：离线回归
- `wm-bot/python/data/`（运行时生成）：`cache.json`、`pace.json`、`slug.json`、`groups.txt`、`i18n.json`（中英翻译表，首次运行写内置 starter）、`lexicon.json`（`翻译 同步` 拉的全量词库）、`bot_flow.log`

## 已知取舍 / 规划中
- **名称翻译（仅名称，不含 wiki 正文）**：`WM/ZK` 支持中文名查询（先经本地表翻成英文再去 WM 查价）；
  `翻译 <名称>` 只回中英对照 + 双 wiki 链接。命中后回复自动带 `英文名 / 中文名` 与中英文双 `/w` 指令。
  - **翻译表三层来源**：① 代码内置 starter（57 条，武器中文名已逐一核对）；
    ② `翻译 同步` 联网拉取的 **WFA_Lexicon 中英对照大词库**（GitHub raw，实测 200 可达，
    Dict 2531 + Sale 2354 → 约 3447 条对照，落 `data/lexicon.json` + 合并进 `data/i18n.json`）；
    ③ wiki 的 MediaWiki `/api.php`（`prop=langlinks` + `list=search` 兜底，仅在住宅 IP 上可用）。
  - **Cloudflare 现实**：wiki 两站会拦「机房 IP + 非浏览器」请求（开发沙箱实测 403），
    所以 wiki 正文（获取方式、锻造材料等）**机器人一律抓不到**，也不假装提供，只给链接让用户自己看。
    这也是把指令从 `wiki` 正名为 `翻译` 的原因。
  - **战甲无官方中文名**：词库里的「犀牛 / 咖喱 / 毒妈」是社区俗称，一律不收；战甲只显示英文。
  - 同步过一次词库后，运行时纯本地、零外网，手机 QPython / Termux 也能用；未收录的名字发 `翻译 同步` 重拉即可。
  - 同域名 3~5 秒节流对 GitHub raw 同步同样生效（两个文件之间真等）。
- **slug 候选**：纯文本规范化 + 少量常见后缀候选（`_set` 等），不拉全站清单；物品名写全（如 `Boltor Prime Set`、
  `Boltor Prime Blueprint`、`Saryn Prime Systems`）可少发一次请求；全部候选都 404 时明确提示改用完整名称，且**不启动后台重试**。
- **平台**：真实源默认 `pc`，未暴露 platform 参数。
- **紫卡词条/属性搜索**（按 +伤害、-后坐力、暴击等筛选拍卖）：本期未做。
