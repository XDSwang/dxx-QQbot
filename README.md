# WM 机器人（纯 Python 单端 · QQ 查价 · 按需缓存）

## 服务端统一运行（仓库根 `run.py`）

> 公共仓库是本体：代码 + `services.json` 服务清单，能放任意服务端跑。
> 数据后端（授权、配置、运行记录，本质该放数据库；现在先用 GitHub 私有仓库文件代替，
> 以后可换数据库地址 / 第三方数据接口）由**仓库根 `update.cfg` 优先配置**，不进 git。

服务端只跑这一条：`python run.py`

启动后：读 `update.cfg` → 装配数据后端 → 先同步代码（拷贝来的目录没有 `.git` 会自动自举，
**绝不覆盖本地较新的版本**）→ 按 `services.json` 逐个拉起服务 → 每 10 秒查信号。
一次 `git pull` 是整仓库的，但只停 / 迁 / 启【被触发的那个服务】；指纹、备份、进程按服务隔离。

- `services.json`（进 git）：服务清单，以后加服务只加一项，不改 `run.py`。
- **信号与数据分开**：`runN` 只说明公共库该不该更新，放在公共仓库本体
  `signals/{runN, <服务名>/runN}`（进 git，随代码 push，**不需要 PAT**）；
  私有库只放数据 `services/<服务名>/{config.txt, runtime.json, owner.json}`；结构权威
  （配置模板 + 迁移规则）在公共仓库 `wm-bot/python/`，随代码同提交，不在私有库。
  10 秒轮询只读本地信号，真有更新时才访问私有库，服务变多也不会拖慢轮询。
- 配置模板见 `update.cfg.example`；`update.cfg`、`data/`、`remote/` 都不进 git。
- 首次部署仍要手工放一次 `update.cfg` 和各服务自己的真实配置
  （wm-bot 是 `wm-bot/python/data/config.txt`）：PAT 不能经 git 下发。

⚠️ **代码必须先 push 到公共仓库**：本地提交没推上去，服务端 `git pull` 拉不到新代码；
即使把新版本整个拷过去，自举后本地领先远端，`--ff-only` 会拒绝非快进（不会回退，但也更新不了）。

### 主库挂了自动切 Gitee（故障转移）

- **数据拉取 / 回写**：主库（GitHub）最多 3 轮尝试；单轮超 5 秒记「窗口过长」并回到第一步
  重新拉取，以本地迁移结果为基准重合并云端独有文件（不冲掉别的设备刚写的数据）。
  3 轮都失败才自动切到 Gitee 备用私有库，用同样的 3 轮流程继续。主备全失败报
  **「数据拉取异常」**，本地数据始终完整，最新监控记录不会丢。
- **代码同步**：`git pull` 失败自动改从 `UPDATER_CODE_REPO_BACKUP`（Gitee 公共库镜像）拉，
  不改本地 `origin` 配置；两边都失败同样报「数据拉取异常」并作废本轮。
- 配置见 `update.cfg.example` 的 ①-b 段：Gitee 私有库要填自己的 `access_token`
  （`UPDATER_GITEE_TOKEN`），没填就不启用备用数据后端；公共库镜像是公开的，pull 不需要凭证。
- Gitee 与 GitHub 的 Contents API 差异（`access_token` 认证 / 新建用 POST / 更新用 PUT + sha）
  由 `updater.GiteeStore` 适配，其余 Store 接口与 `GitHubStore` 一致。

### 多台服务端 / 多个授权 / 其他平台

- **同授权不会并发**：官方网关 `max_concurrency=1`，本项目 `identify` 写死 `shard [0,1]`，
  一个 AppID 只能有一条网关连接，第二个会把第一个顶掉；OneBot 同一个 QQ 号同协议端同理。
  单机再有一份 `updater.lock` 挡住第二个 RUN。所以「两台机器同时写同一份 `runtime.json`」
  正常部署不会发生；误配时有 `owner.json` 认领兜底（跳过回写 + 异常日志），
  也可用 `UPDATER_PUSH_RUNTIME = 0`。
- **不同授权同机跑两个**：机器人代码的 `data/` 路径是按 `__file__` 写死的，两个进程共用会
  读同一份 `data/config.txt`、写同一批 `ws_*.json`。要么各放一份代码目录副本（各自 `data/`），
  要么先给机器人补数据目录注入；`services.json` 里两项 `data_dir` 相同会被启动自检拦下。
- **多台服务端**：单实例锁只在单机有效（本来就该各自独立）。每台一份 `update.cfg` / PAT，
  用 `SERVICES_ENABLED` 决定本台跑哪些服务；全局 `run1` 所有机器一起更新，属于预期的统一发布。
  配 `UPDATER_DATA_NAMESPACE` + `UPDATER_INSTANCE_ID` 让同代码不同授权的数据各占一层目录。
- **权限边界**：fine-grained PAT 是仓库级、不是目录级，每台服务端都能读到私有库里别的项目的
  文件；要按项目隔离只能一项目一私有仓库，或以后换真数据库做行级权限。私有库 `config.txt`
  存的是真实密钥值（这正是它必须永久 Private 的原因）；结构权威（空模板 + 迁移规则）在公共仓库，
  服务端按模板对账时绝不会用空模板冲掉私有库里的真实值。
- **其他平台**：代码零第三方依赖，但 `git` 是外部命令（Termux 需 `pkg install git`）；
  没有 git 时启动自检会明确报出来，服务照常跑、只是不会自动同步。

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
- **世界状态**（星际战甲国际服）：发 `警报` / `平原` / `突击` / `仲裁` / `裂缝` / `钢铁裂缝` / `赏金` … 任一词查询；
  `计划` 看到期时间表；发 `活动=开启` / `警报=关闭` 直接开关主推（会存盘、重启自动恢复），
  `监控 <词>`（或 `监控 全部`）也能开启推送、`停止监控` 停止、`主推状态` 查当前设置。
  其中 `警报` / `活动` 是**主推**：出现新条目立即单独推一条，不等 5 分钟周期。
  缓存按数据自身到期时间更新一次，不轮询。详见下方「世界状态查询」章节。
- 兼容旧别名：`/查询`、`/wm` + `物品|紫卡|ZK`（`wiki` 指令已删除，请用「翻译」）
- 任何时候发 `帮助`（或 `?` / `菜单`）都能调出**全部指令一览**（查价 / 紫卡 / 翻译 / 世界状态 / 监控 / 计划），不用死记。

> **重要边界**：wiki 页面本身还有「获取方式 / 锻造材料 / 数值」等大量正文，
> 机器人**抓不到也不提供**（两站 Cloudflare 反爬 + 页面由模板拼装）。
> 「翻译」指令只回名称对照和两个链接，要看正文请自己点开链接。

## 世界状态查询（星际战甲国际服）
数据源默认抓 **wf.wiki 的实时世界状态页**（`worldstate.wf.wiki`，Next.js 服务端渲染，
世界状态以 JSON 内嵌在页面 RSC 流里，**无 Cloudflare、程序化可直连、数据实时**）。
解析后转成统一内部结构，调度 / 缓存 / 查询 / 监控完全复用。旧的
`api.warframestat.us`（常被 Cloudflare 拦成 403）仍保留为可切换源（`WM_WS_SOURCE=warframe`）。

> wf.wiki 覆盖：平原昼夜（希图斯 / 奥布山谷 / 魔胎之境 / 双衍王境 / 扎里曼）/ 裂缝 / 钢铁裂缝 /
> 警报 / 活动 / 入侵 / 午夜电波 / 虚空商人 / 瓦奇娅 / Darvo 特惠 / 开放世界赏金 / 赏金轮换 /
> 信条武器 / 终幕武器 / 全局加成 / 新闻 —— **主页一屏能看到的栏目全部可取、也都能单独查**。
> **突击** 另从 DE 官方端点（`api.warframe.com/cdn/worldState.php`）补抓
> （沙箱实测可直连、返回 `Sorties`，任务类型/减益/BOSS 已做中英映射）。
> **仲裁** 来自 `arbi.wf.wiki` 的仲裁轮换表（见下）。
> **仍不提供**：信使前哨 / 集团任务 / 建造进度（这几项 DE 原始数据里也没有；单条查询会如实提示）。
>
> 仲裁为什么不轮询：`arbi.wf.wiki` 把轮换固化成了两张静态 JSON
> （`/data/arbys.schedule.v2.json` 的 `startTs + stepSec(3600) + seq[]`，`/data/arbys.nodes.zh.json` 的节点中文表），
> 当前这张图 = `nodes[seq[(now-startTs)/3600]]`，**本地就能算**。所以两张表 7 天同步一次
> （合计约 440KB，落 `data/arbys_*.json`），平时查「仲裁」**零请求**；表跑出覆盖区间会自动重抓。

指令（聊天文本，群内 @机器人 或私聊直接发）：
- `警报` / `平原` / `希图斯` / `奥布` / `魔胎` / `双衍` / `扎里曼` / `突击` / `仲裁` /
  `裂缝` / `钢铁裂缝` / `赏金` / `赏金轮换` / `信条` / `终幕` / `加成` / `活动` /
  `入侵` / `电波` / `虚空商人` / `瓦奇娅` / `特惠` / `新闻` —— 各自查询对应栏目；
  `平原` 一次给五处昼夜，**每处时间后面紧跟该平原自己的赏金任务板**（希图斯→Ostron /
  奥布山谷→索拉里斯 / 魔胎之境→英择谛，双衍 / 扎里曼有数据时同样挂上）；
  `裂缝` / `钢铁裂缝` 分**常规星球（上）**与**九重天（下）**两块，各自按
  **古纪 → 前纪 → 中纪 → 后纪 → 安魂 → 全能** 排序，同档再按剩余时间升序，
  九重天（XX比邻星域）单独一组不跟常规星球混排；`全部` 给所有栏目
  （一次性给十几个栏目时会自动**精简**每栏条数，裂缝精简也保证两块各露面 2 条，
  保证一条消息发得完；要看全的请发单条指令）。
- **回复形式：默认一律发图**。所有回复（世界状态 / WM 查价 / 紫卡 / 翻译 / 帮助）都渲染成
  **白底卡片图**（分组标题带色条、剩余时间右对齐、超长自动双栏或分页）。
  图里的字在 QQ 里**选不中、复制不了**，要复制节点名时在该条指令前加「文字」两个字：
  `文字 裂缝` / `文字 WM Boltor Prime`（对 WM、紫卡、帮助同样生效）。
  **例外**：WM 交易回复的英中两条 `/w` 指令本身就是可复制纯文本，与卡片图合成**一条消息**，
  不用加前缀。官方端有两条可选实现（见下）：
  - **Markdown 消息（默认，`WM_QQ_MARKDOWN = native`）**：一条 `msg_type=2` 消息 =
    「完整卡片图」+「图片下方两条可复制 `/w` 指令」。★ 卡片图里已经画好标题、行情统计、
    排序说明、订单列表、来源网址，**图下面绝不重复正文**（真机截图确认过重复问题，已修）。
    只有主动发「文字 xxx」的纯文字回退才输出完整正文。官方 2026/04/23 起
    自定义 Markdown 已对单聊 / 群聊所有机器人开放，不用申请模板。
    图片**首选「固定公网 URL + 覆盖式上传」**：网盘 / Git 仓库 / 对象存储里只放**一张**
    `card.png`，每次把新卡片覆盖写进去，URL 永不变、不占免费图床额度、不攒过期素材；
    客户端按 `![img #618px #Npx](URL)` 自动解析（跟 HTML 邮件托管图片同一原理）。
    支持通用 PUT 覆盖（WebDAV / R2）与 GitHub/Gitee Contents API 覆盖两种零依赖后端，
    `?v=<图片哈希>` 只用来破 CDN 缓存，存储仍只有一张。没配覆盖式才退回官方**素材管理**
    `qqbot-media://` → 自备 POST 图床。**素材没拿到引用或发送失败都不会退化成
    「无图纯文字」**，一律自动回退下一条富媒体通道把卡片图发出去。
  - **富媒体图文混排（`msg_type=7` + `content`）**：`WM_QQ_MARKDOWN = 0` 时走这条，
    `/files` 上传拿 `file_info` 后同条带文字；OneBot 端始终是「图片段 + 文本段」一条消息。
  也可用 `图 xxx` 显式要求出图；画图依赖 **Pillow + 中文字体**，缺失或发图失败都会
  **自动退回纯文字**，不会不回话。想永久改回按长度自动/纯文字：`data/config.txt` 里
  `WM_WS_CARD = auto|off`。
  ⚠️ **Markdown 文字没有颜色语法**：「游戏中红 / 在线蓝 / 离线灰」是画在卡片图里的。
  若发现回复变成一大段纯文字、图片和颜色同时消失（平原/赏金/WM 全部如此），是手机端
  缺 Pillow / 中文字体 / `render_card.py` 导致卡片根本没渲染出来——**群里发「自检」**，
  机器人会直接回一段文字列出各部件状态；也可看启动日志 `[card] 图片能力自检`。
  补齐后重启即可恢复；只有主动发「文字 xxx」才是正常的纯文字。
- **★ `活动=开启` / `警报=关闭` —— 发条消息就改设置，不用改配置、不用重启。** 开启 = 该栏目自动纳入
  本会话监控并设为主推；关闭 = 不主推，周期全量推送里也剔除。还认 `alerts=on` / `events=off` /
  `活动＝开启` / `活动 开启` / 一条多组 `活动=开启，警报=开启`；`主推=开启` 一键开默认栏目，
  `主推状态` 看当前开关。设置按会话存进 `data/ws_switches.json`，**重启自动恢复**，不同群互不干扰，
  也不会覆盖手动 `监控 <词>` 加的栏目。发 `停止监控` 会停止推送并清除该会话设置。
- `监控 <词>`（如 `监控 警报` / `监控 裂缝` / `监控 全部`）—— 开启**推送**，发 `停止监控` 即停。
  这是**一次性注册**（注册表在内存里，重启清空）；想「设置一次一直在」用上面的开关指令。
  `WS_AUTO_PRIORITY = 1`（默认 `0`）可在启动时对白名单群批量自动注册，但它每次重启都按配置重开。
  - **警报 / 活动主推**：出现新警报 / 新活动、奖励或描述变化时**立刻单独推一条精简消息**，
    不受 `WS_ACTIVE_MIN_GAP` 300 秒限制，只受 60 秒防抖；刚开监控只记基线、不补发历史，
    条目结束静默。栏目由 `WS_PRIORITY_TYPES` 配（默认 `alerts,events`，写 `off` 关掉主推）。
  - **其余栏目周期推**：内容变了才推且至少间隔 300 秒；签名剔除「剩余：5分30秒」这类倒计时，
    倒计时跳动不再刷屏；周期里默认剔除主推栏目，避免同一条警报推两遍。
  - **两手准备**：先尝试主动消息；官方主动消息要按群单独开「机器人主动在群聊内发言」，
    没开时固定 `40034105`，内容会**攒着**，等本会话里有人发消息时拼在回复末尾带出
    （被动窗口 5 分钟内必达；全量消息模式下任意一条群消息都算）。
    所以即使没开通配额，监控也不会白开。主推发送失败会保留基线自动重试，新警报不会丢。
- `计划` —— 查看本次抓取时刻、各类数据的到期时间、下次请求时刻；发 `世界` / `帮助` / `?` 看完整帮助。

两种模式都支持：
1. **被动查询（秒回）**：直接读本地缓存（`data/worldstate.json`）即时回复，**缓存未到期 → 零网络请求**。
   更新按【数据自身的到期时间】驱动，不轮询：抓取时记录 `fetched_at`，把各字段的 `expiry` /
   `activation` / `timeLeft`（相对时长换算成「抓取时刻 + 剩余时长」的绝对时刻）去重升序写进 `queue`，
   后台线程长睡到队首那一刻，只发**一次**全量请求、重排队列后继续睡。
   例：虚空商人没来时，到期点就是他下次抵达时间（可能十几天），期间一次接口都不打；
   到他抵达 / 离场那一刻才更新一次。
2. **推送（监控）**：发「活动=开启」等开关指令（或群里发「监控 …」）后，机器人每 10 秒
   **检查**一次（10 秒是节拍，不是发送周期），直到「停止监控」。分两路：`警报` / `活动` 出现新条目
   立即单独推一条（主推，60 秒防抖）；其余栏目内容变了才推且至少间隔 `WS_ACTIVE_MIN_GAP`
   （默认 300 秒），倒计时不算变化。开关指令设的范围按会话保存，重启自动恢复。
   - 主动通道正常（OneBot，或官方已按群开「机器人主动在群聊内发言」）：直接推，不夹杂其它内容；
   - 主动通道不通（官方未开通配额 → `40034105 无权限`）：内容存入暂存区，用户下次查询时
     以「📬 监控推送（主动消息受限，随本次查询附带）」一段拼在回复末尾，跟正文同一张图。
     暂存有保鲜期（`WS_ATTACH_TTL` 默认 30 分钟）、同一会话附带间隔（`WS_ATTACH_GAP` 默认 20 秒）、
     最大长度（`WS_ATTACH_MAX_BYTES` 默认 2000 字节，按整行截断），可用 `WS_ATTACH_ON_QUERY = 0` 整体关掉。
     按栏目分块暂存：本次查询已包含的栏目会被剔除，不会把「裂缝」重复贴一遍。
   - 主动通道连续失败后会退避成 **5 分钟探测一次**，不再每 10 秒空打接口；配额一开通自动恢复。

> 节奏与 WM 硬规则一致：每次 HTTP 前都过 `pace_gate_ex`（同域名 **3~5 秒**，写死不可调小，
> 与 WM 共用 `pace.json`）。在此之上两次全量请求之间有 **10 → 20 → 50 → 10 秒**循环节拍
> （`WS_STEP_GAPS`，只用来到期点异常 / 队列为空时兜底，**不提供配置**，防止被调小后打爆接口）；
> 真正决定何时请求的仍是**队首那一条的结束时刻**，队首在十几分钟后，节拍早过了也只等到期点，
> **没有定时轮询**。接口失败退避 **10 → 30 → 60 秒**，之后保持 60 秒继续重试，成功后归零。
> 可改的只有数据源 / 语言 / 推送节奏 / 最长睡眠 / 到期宽限 / HTTP 超时 / 仲裁表地址与同步周期，
> 都在 `wm-bot/python/data/config.txt` 的「六、世界状态」段落里，附中文注释。

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
  - 订单列表（最多 15 条）+ `🕒 数据更新于 <时间>（距今 X）· 来源：<WM 完整网址>`。
    **排序按卖家能否马上交易**：游戏中 🟢 → 在线 🔵 → 离线 ⚪，同组内再按价格从低到高；
    离线且超过 `WM_OFFLINE_STALE_DAYS`（默认 30 天）没登录的沉到最后、并标出「N 天前在线」
    （这类号多已 2035 封号 / 弃坑，价格再低也买不到）。回复头部给出三种状态各多少条，
    代表价（最低价）只取在线卖家，全离线时才退回全部报价并明确警告。
  - **两条**游戏内发起交易指令：英文
    `/w <卖家> Hi! I want to buy: <英文名> for <价> platinum.`，
    中文 `/w <卖家> 你好，我想购买 <中文名> <价> 铂金.`；价格一致，来源行保持原样，不附加任何来源署名后缀。
    QQ 里这两条是随卡片图同属**一条消息**的可复制文字（不再单独占一条文本消息）。

## 路径与运行
- 数据文件都在 Python 文件同目录的 `data/`：`cache.json`、`pace.json`、`worldstate.json`（世界状态缓存）、
  `arbys_schedule.json` / `arbys_nodes.json`（仲裁轮换表，7 天一同步）、`groups.txt`、`i18n.json`（中英翻译表）、
  `lexicon.json`（同步的全量词库）、`bot_flow.log`；
- `config.py` 读取 `wm-bot/python/data/config.txt`（UTF-8，支持注释 / BOM），把值交给各 Python 模块；
  同目录的 `config.txt` 只是可上传的纯净模板，程序不会读它；
- 时间戳固定写 `data/pace.json`，**不得删除**——删掉等于抹去上次访问时间，属规避规则。

## 本地回归检测（离线，不联网）
```bash
python test_config.py         # 配置文件解析 / 开关 / 路径 / 无启动脚本
python test_pace_guard.py     # 硬下限：3~5s 节流 / 1/3/7min 重试 / 1h 全失败
python test_cache_policy.py  # 超间隔先刷新、90 天旧缓存不发、失败不假死、真等 3~5s 端到端
python test_slug_resolution.py  # 物品名→slug 候选 / 确定性 404 / 候选间真等 3~5s
python test_wm_rank.py       # 卖家在线状态排序：游戏中→在线→离线、离线久未登录沉底、代表价只取在线、紫卡仅竞价 46 项
python test_official.py      # 官方 QQ 对接 10 项
python test_onebot.py        # OneBot 对接 7 项
python test_i18n.py          # 翻译指令 / wiki API 解析 / 全量词库 / 双语回复 / 中文名查价 30 项
python test_pace_api.py      # 节流三原子函数：pace_last_access / pace_record / pace_gate_ex 22 项
python test_worldstate.py    # 世界状态：全量请求 / 排序队列 / 节拍 / 特殊任务 / 多源 / wf.wiki 适配 / 仲裁轮换表 239 项
python test_qq_common.py     # QQ 公共路由 / 全局「帮助」总览 25 项
python test_ws_attach.py     # 监控暂存 / 附带 / 限频保鲜截断 / 退避 45 项
python test_ws_priority.py   # 警报/活动主推：基线 / 立即推 / 防抖 / 失败重试 / 捎带 / 稳定签名 69 项
python test_ws_switch.py     # 「活动=开启」聊天开关：解析 / 会话覆盖 / 存盘重启 / 白名单 83 项
python test_ws_layout.py     # 裂缝分组排序 + 平原带赏金 + 卡片状态标签隔离 107 项
python test_ssl_compat.py    # 手机 CA 证书：探测 / 降级重试 / 请求签名 / 取 token 恢复 34 项
python test_qq_markdown.py   # Markdown 消息：覆盖式固定公网图 / 素材三步上传 / 八键模板 / 图下不重复正文只留 /w / 无图不退化 / 3 张滚动清理 / probe 123 项
python test_card.py          # 卡片渲染：中文/emoji 清洗、单栏/双栏/分页、无字体回退、图/文字前缀 35 项
python -m unittest discover  # unittest 风格用例汇总（107 项，脚本式测试自动跳过）
```

### 实测 QQ 到底渲不渲染 Markdown 公网图
官方文档没有保证图片标签一定接受任意公网 `https` 链接，**别靠猜**，发一条最小消息验证：

```bash
cd python
python md_probe.py                                          # dry-run：打印「一张图 + 一个内容字符串」请求体，生成网页预览，不联网
python md_probe.py --upload --image overwrite               # 真上传卡片图拿固定 URL，但仍不发送
python md_probe.py --send --group <group_openid> --image overwrite   # 真发一条到群里看效果
python md_probe.py --send --group <group_openid> --image media       # 换官方素材 qqbot-media:// 对照
```

手机 QQ 里**图片和文字在同一条消息里** = 转换成功；只出文字或整条发不出去 =
平台不放行该图片 URL，换 `--image media` 对照，或设 `WM_QQ_MARKDOWN = 0`
走已验证的富媒体图文混排。预览页：`wm-bot/Markdown最小用例.html`。

## 手机挂机（Termux / QPython）
1. **先关掉电脑上还在运行的 `python qq_bot.py` 窗口**。同一个 AppID / Secret 只能有一个在线长连接，
   电脑和手机同时跑会互相顶掉、群事件收不到。`data/config.txt` 与 `data/groups.txt` 都不用改：
   凭证和群 openid 跟机器人 AppID 走，换设备不变。
2. 拷到手机任意目录（路径基于脚本自身目录，放哪都行）。**必带**（少一个就可能出问题）：
   `qq_bot.py`、`qq_official.py`、`qq_common.py`、`onebot.py`、`config.py`、`data/config.txt`（填好凭证的那份）、`wm.py`、
   `worldstate.py`、`ws_monitor.py`、`render_card.py`、`qq_markdown.py`、`data/groups.txt`。
   ⚠️ **`render_card.py` 不能漏**：它是画白底卡片图的模块，缺了会静默退回纯文字，
   表现为「回复变成一大段文字、红/蓝/灰昵称全没了」。`qq_markdown.py` 是默认开启的
   Markdown 通道，缺了会走富媒体兜底（仍能用，但少一条通道）。
   `data/cache.json` / `pace.json` / `worldstate.json` / `slug.json`
   / `ws_switches.json`（聊天指令保存的开关，不带则当前设备按默认）可带可不带（不带则第一次查询自建）。
   **不用带**：所有 `test_*.py`、`__pycache__/`、`data/bot_flow.log`、
   `data/items_index.json`（旧版全站索引残留，代码已不读）、`data/cache.json.mockbak`（测试备份）——
   这些都不参与运行，拷过去只是占空间。
3. Termux：`pkg install python` 后 `python qq_bot.py`；QPython 用「运行脚本」打开 `qq_bot.py`。
   **想出卡片图还要装两样**（否则回复永远是一大段纯文字、红/蓝/灰昵称也没了）：
   `pip install pillow` 和 `pkg install noto-cjk-fonts`；QPython 可把任意中文 TTF 路径
   写进 `data/config.txt` 的 `WM_CARD_FONT`。启动时 `[card] 图片能力自检` 会告诉你缺哪样。
   挂机先执行 `termux-wake-lock`（可配合 `tmux`），否则锁屏后进程被杀、长连接断开。
   手机需能访问 `api.bot.qq.com` 与 `api.warframe.market`；建议打开系统自动校时
   （若手机时钟比拷过去的缓存时间早，程序会按「缓存过期」联网刷新，不会发旧数据）。
4. 接 QQ 只有一种入口：`python qq_bot.py`（纯标准库零依赖）：
   - **官方 QQ 机器人 API**（默认）：正规无封号风险，需在 q.qq.com 申请 appid / secret；
     **2026 年官方已支持普通群**（用 `group_openid`，不是数字群号）。
   - **OneBot v11**：把 `data/config.txt` 里 `WM_QQ_BACKEND` 改成 `onebot`；有封号风险。
5. **群白名单**：默认不监控任何群，只有把群 openid 手工写进 `data/groups.txt` 的群才监听并回复；
   群里必须从「@ 列表点选机器人」才会触发事件。
6. **一直刷 `[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate`**：
   手机上的 Python 缺 CA 根证书，取 token / 发消息 / 查 WM / 连 wss 会**全部**失败并重连刷屏，
   不是网络或 QQ 接口问题。Termux 先执行 `pkg install ca-certificates` 再重跑（最推荐，保持证书校验）；
   QPython 可执行 `pip install certifi`，把 `python -m certifi` 输出的路径写进
   `data/config.txt` 的 `WM_SSL_CAFILE = /路径/cacert.pem`。
   程序已自动探测 Termux / Android / Linux 常见 CA，实在找不到会临时关闭校验并打印醒目警告
   让机器人先跑起来（有中间人风险，建议尽快装证书）。Windows / macOS 不受影响。

## 目录
- `wm-bot/自定义Markdown使用说明.md`：**单独文档**，只讲本次「图片 + `/w` 合成一条消息、不重复正文」是怎么用 `msg_type=2` 自定义 Markdown 解决的（请求体、当前用的 QQ 官方素材引用、三步实测、排障）
- `wm-bot/QQ群接入与授权操作说明.md`：**单独文档**，专讲加群、平台「添加群号授权」、群里「设置权限」、
  数字群号 vs `group_openid`、白名单与群相关排障；群这块配置较繁琐时直接看它
- `wm-bot/QQ官方机器人接入指南.md`：整体接入流程（凭证、启动、配置、通用排错）
- `wm-bot/python/wm.py`：查价 / 按需缓存 / 节流 / 名称 slug（CLI + 可 import）
- `wm-bot/python/worldstate.py`：星际战甲世界状态查询 / 监控（默认接入 wf.wiki，可切 warframestat.us，CLI + 可 import）
- `wm-bot/python/ws_monitor.py`：世界状态主动推送监控（注册表 + 会话开关存盘 + 推送线程，由 QQ 后端注入 sender）
- `wm-bot/python/config.txt`：可上传的**纯净模板**（带中文注释，凭证两项留空）
- `wm-bot/python/data/config.txt`：**唯一生效**的配置文件（由模板复制后填写，不上传）；`config.py`：读取它的纯标准库模块
- `.gitignore`：排除整个 `data/`，上传 GitHub 时无需额外操作
- `wm-bot/python/qq_bot.py` / `wm-bot/python/qq_official.py` / `wm-bot/python/onebot.py` / `wm-bot/python/qq_common.py` / `wm-bot/python/render_card.py`：QQ 对接 + 卡片渲染
- `wm-bot/python/test_config.py` / `test_pace_guard.py` / `test_pace_api.py` / `test_cache_policy.py` / `test_slug_resolution.py` / `test_wm_rank.py` / `test_official.py` / `test_onebot.py` / `test_i18n.py` / `test_worldstate.py` / `test_qq_common.py` / `test_card.py` / `test_ws_attach.py`：离线回归
- `wm-bot/python/data/`（运行时生成）：`cache.json`、`pace.json`、`worldstate.json`、`slug.json`、`groups.txt`、`i18n.json`（中英翻译表，首次运行写内置 starter）、`lexicon.json`（`翻译 同步` 拉的全量词库）、`bot_flow.log`

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
- **世界状态（已实现）**：默认接入 **wf.wiki**（`worldstate.wf.wiki`，实时、无 Cloudflare），
  覆盖五处平原昼夜 / 裂缝 / 钢铁裂缝 / 警报 / 活动 / 入侵 / 午夜电波 / 虚空商人 / 瓦奇娅 /
  Darvo 特惠 / 开放世界赏金 / 赏金轮换 / 信条·终幕武器 / 全局加成 / 新闻；**突击**另从 DE 官方端点 `api.warframe.com/cdn/worldState.php` 补抓。
  wf.wiki 与官方端点都不提供的 仲裁 / 信使 / 集团 / 建造 会如实提示。
  可切 `WM_WS_SOURCE=warframe` 走 `api.warframestat.us` 系（覆盖最全，含仲裁，但常被 Cloudflare 403）。
  支持被动查询（秒回 + 到期才更新一次的缓存）与监控推送（每 10 秒检查、只读缓存；
  发 `活动=开启` / `警报=关闭` 按会话开关并存盘、重启恢复，`监控 <词>` 一次性注册；
  `警报`/`活动` 新条目立即推，其余栏目内容变化才推；主动发不出去时随消息附带），
  见上方「世界状态查询」章节。
- **平台**：真实源默认 `pc`，未暴露 platform 参数。
- **紫卡词条/属性搜索**（按 +伤害、-后坐力、暴击等筛选拍卖）：本期未做。
