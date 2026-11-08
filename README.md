# dxx-QQbot — 运行与部署

> 两个仓库分工：**公共库（本仓库）＝代码本体**（代码 + `services.json` 服务清单），
> **你的私有库 ＝数据**（真实配置值、运行记录、启动器 `bootstrap.py` 与凭证）。
> 长期运行的是本仓库根目录的 `run.py`；私有库的 `bootstrap.py` 只在首次部署跑一次，
> 跑完就 `execv` 成 `run.py`，之后不再常驻。
>
> 路径一律按**项目根目录相对书写**，本文不写任何盘符，需要绝对路径时自己拼：
> 私有库根 → `bootstrap.py`、`bootstrap.cfg`、`services/wm-bot/config.txt`；
> 公共库根（启动器默认克隆到私有库目录下的 `dxx-QQbot/`）→ `run.py`、`update.cfg`、
> `services.json`、`wm-bot/python/data/config.txt`。
> **所有仓库地址都是 `bootstrap.cfg` / `update.cfg` 里的参数**，代码与模板里不写死账号或仓库名。
> 术语：GIT 拷贝语境里说的「源 / 接口」指**仓库地址**，不是第三方数据接口；
> 后者是以后扩展、当前不生效。判断层只跑 `git fetch`，不需要任何 token。

## 一、运行流程（已经部署好的机器）

### 前置条件

- Python 3 与 git 可用；`git@github.com` / `git@gitee.com` 的 SSH key 都已配好
  （GIT 双源拷贝走 SSH，不需要任何 token）。
- 本仓库根目录有 `update.cfg`（由启动器生成，不进 git），里面是数据后端与访问凭证。
- `wm-bot/python/data/config.txt` 里有真实值（首次部署由私有库带过来，之后由服务端对账维护）。

### 启动

```bash
cd dxx-QQbot
python run.py
```

### 启动后依次发生什么

1. 读根目录 `update.cfg`，装配数据后端（目前用 GitHub 私有库文件代替数据库）。
2. 启动自检：git、`services.json`、数据后端、GIT 双源拷贝连通性；
   有问题只告警，照常用本地版本继续运行。
3. 代码同步：用 GIT 指令把 GitHub / Gitee 两个公共库都 `fetch` 拷贝下来，
   比 commit 版本选最新，本地领先不回退，`merge --ff-only` 落地；
   两个接口都失败报「数据拉取异常」，作废本轮但不影响服务。
4. 按 `services.json` 拉起服务（当前只有 `wm-bot` → `wm-bot/python/qq_bot.py`）；
   服务崩溃最多自动拉起 3 次，超过就如实记日志、不再无限重启。
5. 每 10 秒做一轮更新判断：把两个私有库接口都 `fetch` 拷贝下来，比版本选最新，
   数据读写也切到同一接口。**HEAD 没变＝拷贝无内容变化＝不用更新**，本轮直接跳过；
   一个接口失败就用拷贝成功的那个，两个都失败报「数据拉取异常」，本地数据保持原样。
6. 真有更新才做功：拉配置 → 以公共模板为结构基准、私有真实值为覆盖对账（`structure`）
   → 备份 / 迁移 → 回写私有库 → 只停、迁、启被触发的那个服务。
   代码文件变了则 `run.py` 自重启换到新版。
7. 私有库 `config.txt` 被账号本人改过（网页或 `git push`）→ 下轮检测哈希变化
   → 按模板迁移并热重载；新字段留空，真实密钥不会被空模板冲掉。

### 日常改东西怎么生效

| 想改什么 | 在哪改 | 服务端怎么跟上 |
|---|---|---|
| 机器人配置 / 密钥 | 私有库 `services/wm-bot/config.txt`（网页改或 push） | 下轮拷贝发现新 commit，自动迁移 + 热重载 |
| 代码 | 本公共库，提交后 push 到 GitHub（Gitee 镜像保持同步） | 下轮代码同步双源拷贝自动对齐 |
| 加服务 / 多机分工 | `services.json`、`SERVICES_ENABLED`、`UPDATER_DATA_NAMESPACE` | 各服务独立信号，互不惊动 |

改完必须 push：本地提交没推上去，服务端 GIT 双源拷贝也拉不到。

### 停止与日志

- `Ctrl+C` 正常退出。不要开第二个 `python run.py`：`data/updater.lock` 会拦住，
  且同一个 AppID 只能有一条网关长连接。
- 流程日志 `data/updater_flow.log`、异常日志 `data/updater_error.log`、
  机器人收发日志 `wm-bot/python/data/bot_flow.log`。

### 三条边界

1. **更新判断恒为 GIT 双源拷贝**：只看拷贝下来的版本有没有变化，
   不看任何信号文件，也没有可切换的开关。
2. **数据读写层另有兜底**：主库 3 轮拉取 / 回写失败（单轮超 5 秒记「窗口过长」，
   回到第一步重新拉）才切 Gitee 备用私有库再 3 轮；主备全失败报「数据拉取异常」，
   本地最新监控记录不会丢。这一层需要 `UPDATER_GITEE_TOKEN` 才启用；
   **判断层走 SSH，不需要任何 token**，不填只是少了回写兜底。
3. **`update.cfg` 是数据后端装配入口，别删**：当前只装 GitHub 私有库文件的读写。
   数据库 / 第三方数据接口的 `UPDATER_DATA_URL` / `UPDATER_DATA_TOKEN` 是
   **以后扩展的预留位，现在别填**（填了只会明确报「未实现」）。
   判断层与读写层是分开的两套：将来真接接口只换数据后端 Store
   （在 `updater.make_store` 注册适配器），GIT 双源拷贝的逻辑一行都不用动。
   `update.cfg`、`data/`、`bootstrap.cfg` 都不进 git。

## 二、部署流程（全新机器 / 手机 Termux / QPython）

### 准备

- 装 Python 3 与 git（Termux：`pkg install git python ca-certificates`）。
- **两个私有库地址各有一条能免密 `git fetch` 的通道**：本机已保存 git 凭证就直接用
  HTTPS（如 `https://github.com/你的账号/你的私有库.git`）；手机 / 没存凭证的机器用 SSH。
  两端是同一个库的两份镜像，不要求同一种方式，谁通就用谁。
- 一个 GitHub **fine-grained PAT**：只授权你自己的私有库，权限
  `Contents: Read and write`。它是给数据读写层读配置 / 回写记录用的，
  **判断层不需要它**；已有同权限的 PAT 直接复用，一个就够，不必额外生成。
- 把 `bootstrap.cfg` 里标【必填】的仓库地址占位换成自己的；换账号 / 换库只改这几行。

### 步骤

```bash
# 1. 先克隆【私有库】（不是公共库）：启动器和真实配置都在里面
#    地址填你自己的私有库；没配 SSH 时才用 https://<PAT>@github.com/... 这种形式
git clone <你的私有库 SSH 地址> bot
cd bot

# 2. 生成启动器配置（bootstrap.cfg 已被 .gitignore 忽略，不会进仓库）
cp bootstrap.cfg.example bootstrap.cfg
#    编辑 bootstrap.cfg：
#      PRIVATE_TOKEN = 真实 PAT（必填；占位值会直接报错退出）
#      DEPLOY_DIR    = 留空则把公共库克隆到 bot/dxx-QQbot/；
#                      本机已有公共库副本时填它的绝对路径，避免两套 data 目录
#      GITEE_TOKEN    = 可选；只有想让数据回写也能切 Gitee 兜底才填，判断层不需要
#      其余项（主/备公共库、两个私有库 GIT 地址、轮询 10 秒等）用默认即可

# 3. 一键启动
python bootstrap.py
```

`bootstrap.py` 只做四件事，然后把自己换成 `run.py`：

1. 克隆 / 更新公共库：主库失败自动改从 Gitee 备用公共库拷，两个都失败才报错；
   目标已有公共库（含 `.git`）时对两个接口依次 `pull --ff-only`。
2. 写出公共库根目录 `update.cfg`（数据后端指向私有库）。
3. 本地没有 `wm-bot/python/data/config.txt` 时，从私有库
   `services/wm-bot/config.txt` 拷一份过来（含真实密钥，不用手工填）。
4. `execv` 成 `python run.py`，进入上面的「运行流程」。

### 部署完成怎么确认

- 控制台出现「统一 RUN 启动」和「启动自检通过」；群里 @ 机器人发 `帮助` 有回复。
- 群白名单 `wm-bot/python/data/groups.txt` 是本地文件、不进 git，也不在私有库里：
  换设备要自己拷过去或新建（每行一个 `group_openid`），不建＝默认不监控任何群。
- 之后崩溃重启、隔天重启都只需 `cd dxx-QQbot && python run.py`；
  也可以再跑一次 `python bootstrap.py`（重新 pull + 生成 `update.cfg`），效果相同。

### 安全

- 私有库**永久 Private**：别改 Public、别加陌生合作者，git 历史里存过的密钥不可逆。
- PAT 只给这一个仓库；`bootstrap.cfg` / `update.cfg` 绝不提交。
- 换设备时凭证与群 openid 跟着机器人 AppID 走、不用改值；
  但同一 AppID 只能有一条长连接，换手机跑之前先关掉电脑上那个 `run.py`。
