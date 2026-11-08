#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""updater.py — 自动更新的远程读取 / 配置合并 / 运行数据迁移（零第三方依赖）。

私有地址（GitHub 私有仓库）只存【真实值】，结构权威在公共仓库本体：
  run0 / run1     信号（放公共仓库 signals/，10 秒轮询零网络零 PAT；见 SIGNAL_DIR）
  config.txt      机器人真实配置（只存值；模板 + 描述在公共仓库 wm-bot/python/config.txt）
  runtime.json    服务端回写的「最新完整 + 旧数据迁移」结果（机器维护，用户只读）

公共仓库本体（随代码同仓同提交，永远和引擎同步）持有：
  wm-bot/python/config.txt   配置模板（字段 + 描述 + 空值），reconcile 的结构基准
  wm-bot/python/schema.json  运行数据迁移规则（改名 / 删除 / 保留 / 默认）

迁移一律【拉到服务端本地】完成：云端没有字段级读写能力，直接在那边改会留下
半迁移状态。流程 = 备份 → 校验 → 改名/删字段/补默认 → 原子替换 → 回写云端 → 记指纹。

同域名连续访问间隔固定 3.5 秒（规范 1.8 的 3~5 秒区间），状态落盘跨进程共享。
"""
import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 仓库根：公共仓库本体所在目录
STATE_DIR = HERE / "data"                       # 更新器自身状态（根 .gitignore 已忽略 data/）
# runN 只说明「公共库该不该更新」，属于代码发布，随 git pull 到每台服务端：
# 放公共仓库本体里，10 秒轮询零网络、零 PAT，私有库挂了也不影响判断要不要更新。
SIGNAL_DIR = HERE / "signals"

ERROR_LOG = STATE_DIR / "updater_error.log"     # 异常日志：覆盖式，只记异常
FLOW_LOG = STATE_DIR / "updater_flow.log"       # 流程日志：追加式，正常操作也记
PACE_FILE = STATE_DIR / "updater_pace.json"     # 同域名节流状态（落盘）
STATE_FILE = STATE_DIR / "update_state.json"    # 全局指纹（无服务归属时用）
BACKUP_ROOT = STATE_DIR / "backup"
LOCK_FILE = STATE_DIR / "updater.lock"

CONFIG_NAME = "config.txt"                      # 私有地址：机器人真实配置（只存值）
RUNTIME_NAME = "runtime.json"                   # 私有地址：服务端回写的迁移结果
OWNER_NAME = "owner.json"                       # 数据目录归属：防两个不同授权互写同一份数据
# 方案 A：结构权威在公共仓库本体（随代码同提交），私有库只存真实值。
# 模板 + 迁移规则从本地已 checkout 的公共仓库读，绝不读私有库的规则文件。
TEMPLATE_PATH = HERE / "wm-bot" / "python" / "config.txt"
SCHEMA_PATH = HERE / "wm-bot" / "python" / "schema.json"
SIGNAL_RE = re.compile(r"^run(\d+)$")
POLL_SECONDS = 10
HOST_GAP_SECONDS = 3.5                           # 写死：落在规范的 3~5 秒区间内
GITHUB_API = "https://api.github.com"
GITEE_API = "https://gitee.com/api/v5"
DATA_ANOMALY = "数据拉取异常"           # 主库与备用库全失败的统一报告用语
FAILOVER_MAX_ATTEMPTS = 3                 # 规范 1.1：静默重试最多 3 轮
FAILOVER_WINDOW = 5.0                    # 单轮超 5 秒视为窗口过长，回到第一步重拉
CONFIG_KV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


# ------------------------------ 日志双轨（规范 1.3） ------------------------------
def _log(path, level, msg, extra=""):
    line = "%s | %s | %s%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), level,
                                 msg, (" | " + extra) if extra else "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if level == "ERROR":
            path.write_text(line, encoding="utf-8")          # 覆盖式，只留最近一次异常
        else:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        print("[updater] 日志写入失败: %s" % e, file=sys.stderr)


def flog(msg):
    _log(FLOW_LOG, "INFO", msg)


def elog(msg, extra=""):
    _log(ERROR_LOG, "ERROR", msg, extra)
    print("[updater][ERROR] %s %s" % (msg, extra), file=sys.stderr)


# ------------------------------ 同域名节流（规范 1.8） ------------------------------
_PACE_LOCK = threading.Lock()


def _read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def pace_gate(host):
    """同一 host 连续访问前过闸：不足 3.5 秒就补齐，状态落盘跨进程共享。"""
    with _PACE_LOCK:
        state = _read_json(PACE_FILE, {})
        last = float(state.get(host, 0) or 0)
        wait = HOST_GAP_SECONDS - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        state[host] = time.time()
        try:
            _atomic_write(PACE_FILE, json.dumps(state, ensure_ascii=False))
        except Exception as e:
            elog("节流状态落盘失败", str(e))


# ------------------------------ 信号与状态 ------------------------------
class Signal(object):
    """一次轮询的结果：need_update 决定是否更新，fingerprint 决定有没有重复处理。"""

    def __init__(self, name, fingerprint, need_update):
        self.name = name
        self.fingerprint = fingerprint
        self.need_update = need_update

    def __repr__(self):
        return "Signal(%s, %s, need=%s)" % (self.name, self.fingerprint,
                                            self.need_update)


def pick_signal(pairs):
    """从 [(文件名, 指纹)] 里挑出信号：非零 runN = 要更新，run0 = 不更新。

    指纹随文件 sha / mtime 变化，所以「一直是 run1」不会重复跑；用户把内容一改
    （或改成 run2、run3…）就是一次新更新，比布尔开关更适合无人值守。
    """
    hits = [(n, f) for n, f in pairs if SIGNAL_RE.match(n)]
    if not hits:
        return Signal("(missing)", "none", False)
    for name, fp in hits:
        if int(SIGNAL_RE.match(name).group(1)) != 0:
            return Signal(name, fp, True)
    name, fp = hits[-1]
    return Signal(name, fp, False)


def state_file(service=None):
    """按服务隔离指纹：多服务共用一份 state 会互相误判「这个信号已处理过」。"""
    if not service:
        return STATE_FILE
    return STATE_DIR / "services" / service / "state.json"


def backup_root(service=None):
    if not service:
        return BACKUP_ROOT
    return STATE_DIR / "services" / service / "backup"


def load_state(service=None):
    return _read_json(state_file(service), {})


def save_state(state, service=None):
    _atomic_write(state_file(service),
                  json.dumps(state, ensure_ascii=False, indent=2) + "\n")


# ------------------------------ 私有地址后端 ------------------------------
class LocalStore(object):
    """本地目录（离线测试 / 内网挂载目录）。文件即私有地址上的那几个文件。"""

    def __init__(self, root):
        self.root = Path(root)

    def signals(self):
        out = []
        try:
            for p in sorted(self.root.glob("run*")):
                if SIGNAL_RE.match(p.name):
                    st = p.stat()
                    out.append((p.name, "%s:%s:%s" % (p.name, st.st_mtime_ns, st.st_size)))
        except Exception as e:
            elog("本地私有地址不可读", str(e))
        return out

    def signal(self):
        return pick_signal(self.signals())

    def read(self, name):
        p = self.root / name
        try:
            return p.read_text(encoding="utf-8") if p.exists() else None
        except Exception as e:
            elog("读取私有文件失败", "%s %s" % (name, e))
            return None

    def write(self, name, text):
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.root / name, text)

    def refresh(self):
        """本地文件每次都现读，无需失效缓存（统一 Store 接口）。"""
        return None


class GitHubStore(object):
    """GitHub 私有仓库（推荐）：Contents API 一次列目录就能拿到全部小文件内容。

    私有仓库 + fine-grained PAT 才是真私有；token 只放本地 update.cfg，不进 git。
    """

    def __init__(self, repo, token, path="", branch="main", api=GITHUB_API):
        self.repo = (repo or "").strip().strip("/")
        self.token = (token or "").strip()
        self.path = (path or "").strip().strip("/")
        self.branch = (branch or "main").strip()
        self.api = api.rstrip("/")
        self._listing = None

    def _url(self, suffix=""):
        base = "%s/repos/%s/contents" % (self.api, self.repo)
        if self.path:
            base += "/" + self.path
        if suffix:
            base += "/" + suffix
        return base + ("?ref=" + self.branch)

    def _pace_host(self):
        return urllib.parse.urlparse(self.api).netloc

    def _api(self, method, url, body=None):
        pace_gate(self._pace_host())
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "wm-bot-updater",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _dir(self):
        if self._listing is None:
            listing = self._api("GET", self._url())
            self._listing = ({item["name"]: item for item in listing}
                             if isinstance(listing, list) else {})
        return self._listing

    def signals(self):
        listing = self._dir()
        return [(n, "%s:%s" % (n, listing[n].get("sha", "")))
                for n in sorted(listing) if SIGNAL_RE.match(n)]

    def refresh(self):
        """列目录结果有缓存；一轮更新开始前必须失效，否则读的是上一次的旧配置。

        以前靠每 10 秒 signal() 顺带刷新，信号移到公共仓库后这条路径消失了，
        所以由 fetch_bundle 显式调用，接口对 LocalStore 同样成立。
        """
        self._listing = None

    def signal(self):
        self.refresh()                # 每轮重新列目录，否则永远看不到新的 runN
        return pick_signal(self.signals())

    def read(self, name):
        item = self._dir().get(name)
        if not item:
            return None
        if item.get("content"):
            return base64.b64decode(item["content"]).decode("utf-8")
        obj = self._api("GET", self._url(name))
        if not obj or not obj.get("content"):
            return None
        return base64.b64decode(obj["content"]).decode("utf-8")

    def write(self, name, text):
        # 并发写同文件时 GitHub 返回 409（sha 已变 = 别人先写了）。这里不自行重试同内容，
        # 避免用旧快照冲掉别人的更新；交由调用方（push_runtime）重拉-重合并后重试。
        listing = self._dir()
        body = {"message": "updater: migrated runtime data",
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "branch": self.branch}
        if name in listing:
            body["sha"] = listing[name]["sha"]
        self._api("PUT", self._url(name), body)
        self._listing = None


class GiteeStore(GitHubStore):
    """Gitee 备用数据后端：Contents API 形状与 GitHub 相同，但三处语义不同。

    - 认证用 access_token 查询参数，不认 Authorization: Bearer；
    - 新建文件是 POST，已存在文件才用 PUT（GitHub 两者都是 PUT）；
    - 更新同样要带当前文件 sha 判并发改写。
    只作为故障转移备用库，由编排层在「主库试满 3 轮」后 advance() 切过来。
    """

    def __init__(self, repo, token, path="", branch="main", api=GITEE_API):
        GitHubStore.__init__(self, repo, token, path, branch, api)

    def _api(self, method, url, body=None):
        pace_gate(self._pace_host())
        sep = "&" if "?" in url else "?"
        url = "%s%saccess_token=%s" % (url, sep, urllib.parse.quote(self.token))
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/json",
            "User-Agent": "wm-bot-updater",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def write(self, name, text):
        # 新建用 POST、更新用 PUT；sha 不匹配（并发改写）会报错，同样不自行重试
        # 同内容，交由 push_runtime 重拉-重合并，避免用旧快照冲掉别人的更新。
        listing = self._dir()
        body = {"message": "updater: migrated runtime data",
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "branch": self.branch}
        method = "PUT"
        if name in listing:
            body["sha"] = listing[name]["sha"]
        else:
            method = "POST"
        self._api(method, self._url(name), body)
        self._listing = None


def make_store(cfg, service=None):
    """按配置造【数据后端】；service 给定时自动切到该服务在私有仓库里的子目录。

    UPDATER_DATA_NAMESPACE 再插一层：同一套代码、不同授权的两台服务端各配一个，
    数据就不会落到同一份 runtime.json 上。这层是可替换的：现在用 GitHub 私有仓库
    文件代替数据库，以后换成数据库地址 / 第三方数据接口，只要按 Store 接口
    （signals / signal / read / write）加适配器并在这里注册，服务与迁移逻辑都不用动。
    """
    kind = (cfg.get("UPDATER_BACKEND") or "local").strip().lower()
    parts = [p for p in ((cfg.get("UPDATER_DATA_NAMESPACE") or "").strip().strip("/"),
                         (service or "").strip().strip("/")) if p]
    if kind == "github":
        path = (cfg.get("UPDATER_GITHUB_PATH") or "").strip().strip("/")
        for part in parts:
            path = (path + "/" + part) if path else part
        return GitHubStore(cfg.get("UPDATER_GITHUB_REPO"),
                           cfg.get("UPDATER_GITHUB_TOKEN"),
                           path,
                           cfg.get("UPDATER_GITHUB_BRANCH", "main"),
                           cfg.get("UPDATER_GITHUB_API", GITHUB_API))
    if kind == "gitee":
        path = (cfg.get("UPDATER_GITEE_PATH") or "").strip().strip("/")
        for part in parts:
            path = (path + "/" + part) if path else part
        return GiteeStore(cfg.get("UPDATER_GITEE_REPO"),
                          cfg.get("UPDATER_GITEE_TOKEN"),
                          path,
                          cfg.get("UPDATER_GITEE_BRANCH", "main"),
                          cfg.get("UPDATER_GITEE_API", GITEE_API))
    if kind == "local":
        root = Path(cfg.get("UPDATER_LOCAL_DIR") or str(HERE / "remote"))
        for part in parts:
            root = root / part
        return LocalStore(root)
    if kind in ("database", "http", "api"):
        raise NotImplementedError(
            "数据后端 %s 尚未实现：请按 Store 接口加适配器，"
            "地址 / 凭证填 UPDATER_DATA_URL / UPDATER_DATA_TOKEN" % kind)
    raise ValueError("未知的 UPDATER_BACKEND: %s" % kind)


class FailoverStore(object):
    """主 → 备（Gitee）数据后端链：同一时刻只用一个活跃后端。

    为什么不在 read/write 里自动切：主库必须试满 3 轮（含 5 秒窗口判定）才允许
    换 Gitee，否则一次网络抖动就跳到备用库，两边数据容易分叉。切换由编排层在
    单库 3 轮耗尽后显式 advance() 触发；claim_owner / push_config 等仍走同一个
    对象，自动跟随当前活跃后端。
    """

    def __init__(self, stores):
        self.stores = [s for s in stores if s is not None]
        self.index = 0

    @property
    def current(self):
        return self.stores[self.index]

    def label(self):
        return "主库" if self.index == 0 else "备用库(Gitee)"

    def has_next(self):
        return self.index + 1 < len(self.stores)

    def advance(self):
        if not self.has_next():
            return False
        self.index += 1
        flog("数据后端切换 → %s" % self.label())
        return True

    # Store 接口：全部代理到当前活跃后端
    def refresh(self):
        return self.current.refresh()

    def read(self, name):
        return self.current.read(name)

    def write(self, name, text):
        return self.current.write(name, text)

    def signals(self):
        return self.current.signals()

    def signal(self):
        return self.current.signal()


def as_failover(store):
    """旧调用直接传单个 Store 也能走新编排：包成单元素链。"""
    return store if isinstance(store, FailoverStore) else FailoverStore([store])


def make_backup_store(cfg, service=None):
    """造 Gitee 备用私有库；没填 repo/token 或关了故障转移就不启用。"""
    if str(cfg.get("UPDATER_FAILOVER") or "1").strip().lower() in (
            "0", "false", "no", "off"):
        return None
    if not (cfg.get("UPDATER_GITEE_REPO") or "").strip():
        return None
    if not (cfg.get("UPDATER_GITEE_TOKEN") or "").strip():
        return None
    sub = dict(cfg)
    sub["UPDATER_BACKEND"] = "gitee"       # 复用 make_store 的 namespace / service 路径
    return make_store(sub, service)


def make_store_chain(cfg, service=None):
    """装配主库 + Gitee 备用库；没配备用库时只有主库，行为与旧版一致。"""
    stores = [make_store(cfg, service)]
    backup = make_backup_store(cfg, service)
    if backup is not None:
        stores.append(backup)
    return FailoverStore(stores)


def make_signal_store(cfg, service=None):
    """信号与数据分离：默认读公共仓库本体 signals/，不需要 PAT、不产生 API 调用。

    全局信号在 signals/runN，每服务信号在 signals/<remote_path>/runN；随代码一起
    git pull 到每台服务端，所以所有服务端看到的是同一份发布指令。
    UPDATER_SIGNAL_SOURCE = data 时退回旧布局（信号也放数据后端），便于平滑迁移。
    """
    source = (cfg.get("UPDATER_SIGNAL_SOURCE") or "code").strip().lower()
    if source == "data":
        return make_store(cfg, service)
    if source != "code":
        raise ValueError("未知的 UPDATER_SIGNAL_SOURCE: %s" % source)
    sub = (service or "").strip().strip("/")
    return LocalStore(SIGNAL_DIR / sub if sub else SIGNAL_DIR)


def claim_owner(store, instance_id, service=None):
    """认领数据目录；已被别的实例认领就拒绝回写，返回 (ok, 已有实例名)。

    「服务端一样、授权不一样」时最怕两个实例误指同一份 runtime.json：数据会静默串。
    这里不阻断本地迁移，只让调用方跳过云端回写并留下异常日志，把误配暴露出来。
    """
    raw = store.read(OWNER_NAME)
    if raw:
        try:
            owner = (json.loads(raw).get("instance") or "").strip()
        except Exception:
            owner = ""
        if owner and owner == instance_id:
            return True, owner
        return False, owner or "(未知)"
    store.write(OWNER_NAME, json.dumps({
        "instance": instance_id,
        "service": service or "",
        "claimed_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        ensure_ascii=False) + "\n")
    return True, instance_id


def fetch_bundle(store):
    """拿到私有地址上的【真实值】(config/runtime)；模板 + 迁移规则从公共仓库本地读。

    方案 A：结构权威在公共仓库（模板 + 规则随代码同提交），私有库只存真实值。
    私有库改了，服务端拉下来就知道；服务端把结构 + 空字段同步回去。
    refresh 先行：私有库【只在真有更新时】被访问，不能拿上一轮缓存。
    """
    store.refresh()
    schema = load_local_schema()      # 规则在公共仓库本体，随代码演进，不在私有库
    runtime = {}
    raw_runtime = store.read(RUNTIME_NAME)
    if raw_runtime:
        runtime = json.loads(raw_runtime)
    return {"config": store.read(CONFIG_NAME),
            "schema": schema, "runtime": runtime}


# ------------------------------ 配置对账（方案 A） ------------------------------
def load_local_template():
    """读公共仓库本地的配置模板（结构 + 描述 + 空值）；这是 reconcile 的结构基准。"""
    try:
        if TEMPLATE_PATH.exists():
            return TEMPLATE_PATH.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def load_local_schema():
    """读公共仓库本体的迁移规则；随代码同提交，永远和引擎同步。"""
    try:
        if SCHEMA_PATH.exists():
            return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def reconcile_config(template_text, values_text, mode="structure"):
    """以【公共仓库模板】为结构基准，私有库真实值为覆盖，安全合并。

    - 模板有、私有值非空 → 用私有值（保留真实值，绝不被空值冲掉）
    - 模板有、私有值为空 → 保留模板行（空 / 默认），即「新字段先留空」
    - 私有有、模板没有 → 保留（绝不静默删除；方案 A 的兼容兜底）
    模板的注释 / 描述行原样保留，保证私有库里也能看到每个字段的说明。

    返回 (合并文本, 报告)。报告里的 new_fields 是「模板有、私有没有」的真·新字段，
    只有这些出现时才需要把结构回写私有库；removed 永远为空。
    """
    if mode == "overwrite":
        # 全量覆盖：按私有值原样写回，不参与模板对账（保留注释/字段全交给调用方）
        return (values_text or ""), {"mode": mode, "added": [], "removed": [],
                                     "kept": [], "new_fields": [], "extra_kept": [],
                                     "skipped": "全量覆盖：按私有值原样写回"}
    if not template_text:
        return (values_text or ""), {"mode": mode, "added": [], "removed": [],
                                     "kept": [], "new_fields": [], "extra_kept": [],
                                     "skipped": "公共仓库没有配置模板"}
    if not values_text:
        # 私有库没有任何真实值：直接给一份模板（全空），等私有库自己填
        return template_text, {"mode": mode, "added": [], "removed": [],
                               "kept": [], "new_fields": [], "extra_kept": [],
                               "skipped": "私有地址没有 config.txt，先用模板"}
    tmpl_keys = {}
    for line in template_text.splitlines():
        m = CONFIG_KV.match(line)
        if m:
            tmpl_keys[m.group(1)] = m.group(2)
    val_vals = {}
    for line in values_text.splitlines():
        m = CONFIG_KV.match(line)
        if m:
            val_vals[m.group(1)] = m.group(2)
    out = []
    added, kept = [], []
    for line in template_text.splitlines():
        m = CONFIG_KV.match(line)
        if not m:
            out.append(line)                      # 注释 / 描述原样保留
            continue
        key = m.group(1)
        if key in val_vals and val_vals[key].strip():
            out.append("%s = %s" % (key, val_vals[key].rstrip()))
            kept.append(key)
        else:
            out.append(line)                      # 模板空 / 默认：新字段先留空
            added.append(key)
    # 私有有、模板没有：保留，绝不删除（方案 A 兼容兜底）
    extra = sorted(set(val_vals) - set(tmpl_keys))
    for key in extra:
        out.append("%s = %s" % (key, val_vals[key].rstrip()))
    text = "\n".join(out)
    if template_text.endswith("\n"):
        text += "\n"
    new_fields = sorted(set(tmpl_keys) - set(val_vals))
    return text, {"mode": mode, "added": added, "removed": [], "kept": kept,
                  "new_fields": new_fields, "extra_kept": extra, "skipped": ""}


# ------------------------------ 运行数据迁移 ------------------------------
def _apply_key_rules(obj, rename, drop, stats):
    """递归改键名 / 删字段；返回新对象，不动原对象。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nk = rename.get(k, k)
            if nk != k:
                stats["renamed"].append("%s→%s" % (k, nk))
            if nk in drop:
                stats["dropped"].append(nk)
                continue
            out[nk] = _apply_key_rules(v, rename, drop, stats)
        return out
    if isinstance(obj, list):
        return [_apply_key_rules(x, rename, drop, stats) for x in obj]
    return obj


def _file_rule(schema, name, cloud_file):
    """文件级规则；没写 keep 时，用云端 runtime.json 的顶层键当最新结构。

    这就是「按私有地址上最新文件的字段安全写入」：云端那份有哪些字段，
    迁移后就只剩哪些字段，删掉的字段自然不再记录。
    """
    rule = dict((schema.get("files") or {}).get(name) or {})
    if rule.get("keep") is None and isinstance(cloud_file, dict):
        rule["keep"] = list(cloud_file.keys())
    return rule


def migrate_runtime(local_runtime, schema, cloud_runtime=None):
    """把服务端运行记录按最新结构安全迁移，返回 (新数据, 迁移报告)。

    rename：旧名→新名，递归作用于所有嵌套层级（含「sessions」里每个会话的字段）。
    keep  ：最新结构允许的顶层字段，其余一律不写入（删除的字段不再记录）。
    defaults：可选，schema 显式声明的新字段默认值；不声明就「没有就不写入」。
    """
    cloud_runtime = cloud_runtime or {}
    global_rename = dict(schema.get("rename") or {})
    global_drop = set(schema.get("drop") or [])
    # 「没有就不写入」：本地和云端都没有的文件不凭空造一份；schema 里写明
    # defaults 的新字段才补（见下面每个文件的 defaults 分支）。
    names = sorted(set(local_runtime) | set(cloud_runtime))
    result, report = {}, {"renamed": [], "dropped": [], "added": [],
                          "unreadable": [], "files": []}
    for name in names:
        rule = _file_rule(schema, name, cloud_runtime.get(name))
        rename = dict(global_rename)
        rename.update(rule.get("rename") or {})
        drop = set(global_drop) | set(rule.get("drop") or [])
        stats = {"renamed": [], "dropped": [], "added": []}
        if name in local_runtime and local_runtime[name] is not None:
            data = _apply_key_rules(local_runtime[name], rename, drop, stats)
        elif isinstance(cloud_runtime.get(name), dict):
            # 服务端本地没有、云端有：云端那份是上一次迁移的完整结果，照样是有效数据
            data = _apply_key_rules(cloud_runtime[name], rename, drop, stats)
        else:
            data = {}
        keep = rule.get("keep")
        if keep is not None and isinstance(data, dict):
            for k in list(data):
                if k not in keep:
                    del data[k]
                    stats["dropped"].append(k)
        if isinstance(data, dict):
            for k, v in (rule.get("defaults") or {}).items():
                if k not in data:
                    data[k] = v
                    stats["added"].append(k)
        result[name] = data
        report["files"].append(name)
        for key in ("renamed", "dropped", "added"):
            report[key].extend("%s:%s" % (name, x) for x in stats[key])
    return result, report


def collect_runtime(data_dir, names):
    """读服务端现有运行数据；坏文件【保留原样、不参与迁移】，绝不静默当空覆盖。"""
    out, bad = {}, []
    for name in names:
        p = Path(data_dir) / name
        if not p.exists():
            continue
        try:
            out[name] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            bad.append(name)
            elog("运行数据无法解析，本次跳过不覆盖", "%s %s" % (name, e))
    return out, bad


def write_runtime_files(runtime, data_dir, backup=True, backup_base=None):
    """备份 → 逐文件原子写 → 出错整体回滚。返回 (ok, 备份目录/错误)。

    backup_base 由调用方按服务传入，多个服务的备份不混在同一个目录里。
    """
    data_dir = Path(data_dir)
    backup_dir = None
    written = []
    try:
        if backup:
            root = Path(backup_base) if backup_base else BACKUP_ROOT
            backup_dir = root / time.strftime("%Y%m%d-%H%M%S")
            for name in runtime:
                src = data_dir / name
                if src.exists():
                    dst = backup_dir / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())
        for name, data in runtime.items():
            p = data_dir / name
            _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            written.append(p)
        return True, str(backup_dir) if backup_dir else ""
    except Exception as e:
        for p in written:
            if backup_dir and (backup_dir / p.name).exists():
                p.write_bytes((backup_dir / p.name).read_bytes())
        elog("运行数据写入失败，已回滚", str(e))
        return False, str(e)


def _rebase_runtime(migrated, schema, cloud):
    """以本地迁移结果为基准，补回云端独有文件（别的设备刚写的），再统一过一遍 schema。

    用于回写冲突后：本地这份是已迁移的完整结果，云端可能有别的服务端 / 本人刚写的
    字段，合进来再提交就不会把别人的数据冲掉。
    """
    union = dict(cloud)
    union.update(migrated)                  # 本地优先
    out, _ = migrate_runtime(union, schema, cloud)
    return out


def push_runtime(store, runtime, schema=None, service=None,
                 max_attempts=FAILOVER_MAX_ATTEMPTS, window=FAILOVER_WINDOW):
    """把本服务迁移结果回写私有库，带「重拉-重合并」兜底，防并发改写 / 写回卡死丢数据。

    风险：拉私有库与写回云端之间若另一台设备改了私有库（或本次写回卡住 / 失败），
    直接 PUT 会冲掉别人的数据 / 导致本地与云端不同步。
    对策：每轮记录耗时；遇到写回异常（含 409 并发改写）或单轮超 window 秒（窗口过长、
    易踩并发），就重新拉最新云端 runtime，以本地迁移结果为基准再合云端独有文件，最多
    max_attempts 轮；全失败报「私有库异常」并返回 False。本地数据始终完整，不会因回写
    失败而丢失。
    """
    schema = schema or {}
    text = json.dumps(runtime, ensure_ascii=False, indent=2) + "\n"
    last_err = ""
    for attempt in range(max_attempts):
        t0 = time.time()
        try:
            store.write(RUNTIME_NAME, text)
            flog("[%s] 运行数据已回写私有库（第 %d 轮）"
                         % (service or "default", attempt + 1))
            return True
        except Exception as e:
            elapsed = time.time() - t0
            last_err = str(e)
            if elapsed > window:
                flog("[%s] 回写耗时 %.1fs > %ds，疑似窗口过长（第 %d 轮）"
                             % (service or "default", elapsed, window, attempt + 1))
            else:
                flog("[%s] 回写失败（第 %d 轮）：%s"
                             % (service or "default", attempt + 1, last_err))
            # 重新拉最新云端 runtime，以本地迁移结果为基准再合云端独有文件后重试
            try:
                store.refresh()
                raw = store.read(RUNTIME_NAME)
                cloud = json.loads(raw) if raw else {}
            except Exception as e2:
                elog("[%s] 重新拉取私有库 runtime 失败"
                             % (service or "default"), str(e2))
                cloud = {}
            runtime = _rebase_runtime(runtime, schema, cloud)
            text = json.dumps(runtime, ensure_ascii=False, indent=2) + "\n"
    flog("[%s] 数据后端 %d 轮回写均失败（本地数据仍完整）：%s"
         % (service or "default", max_attempts, last_err))
    return False


def fetch_bundle_failover(store, service=None,
                          max_attempts=FAILOVER_MAX_ATTEMPTS,
                          window=FAILOVER_WINDOW):
    """拉私有库数据：主库最多 max_attempts 轮，单轮超 window 秒也记「窗口过长」，
    耗尽才切备用库（Gitee）用同样轮数继续；全失败报「数据拉取异常」并抛出。
    """
    fs = as_failover(store)
    last_err = ""
    while True:
        for attempt in range(max_attempts):
            t0 = time.time()
            try:
                bundle = fetch_bundle(fs)
                flog("[%s] 数据拉取成功（%s，第 %d 轮）"
                     % (service or "default", fs.label(), attempt + 1))
                return bundle
            except Exception as e:
                last_err = str(e)
                elapsed = time.time() - t0
                why = ("耗时 %.1fs > %ds，窗口过长" % (elapsed, window)
                       if elapsed > window else last_err)
                flog("[%s] %s 数据拉取失败（第 %d 轮）：%s"
                     % (service or "default", fs.label(), attempt + 1, why))
        if fs.advance():
            flog("[%s] 主库 %d 轮拉取失败，切换备用库继续"
                 % (service or "default", max_attempts))
            continue
        elog(DATA_ANOMALY, "%s：主库与备用库共 %d 轮拉取均失败（%s）"
             % (service or "default", max_attempts * len(fs.stores), last_err))
        raise RuntimeError(DATA_ANOMALY)


def push_runtime_failover(store, runtime, schema=None, service=None,
                          max_attempts=FAILOVER_MAX_ATTEMPTS,
                          window=FAILOVER_WINDOW):
    """回写运行数据：主库试满 3 轮 → 切 Gitee 再 3 轮 → 全失败报数据拉取异常。"""
    fs = as_failover(store)
    payload = runtime                      # 切库后仍以本地原始迁移结果为基准
    while True:
        if push_runtime(fs, payload, schema, service, max_attempts, window):
            return True
        if fs.advance():
            flog("[%s] 主库 %d 轮回写失败，切换备用库继续"
                 % (service or "default", max_attempts))
            continue
        elog(DATA_ANOMALY, "%s：主库与备用库回写均失败（本地数据仍完整）"
             % (service or "default"))
        return False


def push_config(store, text):
    """把合并后的配置（真实值 + 新字段空位）回写私有地址，让私有库结构与模板同步。

    只在新字段出现时才调用（见 do_update / sync_config），避免每轮空提交。
    """
    store.write(CONFIG_NAME, text)
    return text
