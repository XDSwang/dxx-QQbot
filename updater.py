#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""updater.py — 自动更新的远程读取 / 配置合并 / 运行数据迁移（零第三方依赖）。

私有地址（GitHub 私有仓库）只存【真实值】，结构权威在公共仓库本体：
  更新判断        用 GIT 指令把两个私有库接口都拷贝(fetch)下来，比版本选最新；
                  拷贝无内容变化 = 不用更新
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
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent          # 仓库根：公共仓库本体所在目录
STATE_DIR = HERE / "data"                       # 更新器自身状态（根 .gitignore 已忽略 data/）
# 下面的 pick_signal 与 LocalStore.signals() 只用于离线测试的假信号源，
# 不参与「要不要更新」的判断；判断统一走 GitSignalStore 的 GIT 双源拷贝。
SIGNAL_DIR = HERE / "signals"

ERROR_LOG = STATE_DIR / "updater_error.log"     # 异常日志：覆盖式，只记异常
FLOW_LOG = STATE_DIR / "updater_flow.log"       # 流程日志：追加式，正常操作也记
PACE_FILE = STATE_DIR / "updater_pace.json"     # 同域名节流状态（落盘）
STATE_FILE = STATE_DIR / "update_state.json"    # 全局指纹（无服务归属时用）
BACKUP_ROOT = STATE_DIR / "backup"
# 锁单独放用户主目录：手机共享存储（FUSE）不实现 flock，锁放那里会直接崩。
LOCK_DIR = Path.home() / ".dxx-updater"
LOCK_FILE = LOCK_DIR / "updater.lock"
LOCK_UNSUPPORTED_ERRNOS = (9, 13, 22, 38, 95)   # EBADF/EACCES/EINVAL/ENOSYS/EOPNOTSUPP

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
PACE_EXEMPT_HOSTS = ("localhost", "127.0.0.1", "::1")   # 规范 1.8：只有自己的地址豁免
GIT_NET_COMMANDS = ("fetch", "push", "clone", "ls-remote", "pull")
UPDATE_CFG_NAME = "update.cfg"          # 私有库根目录里的真实配置（共享命名，统一入口）
GITHUB_API = "https://api.github.com"
GITEE_API = "https://gitee.com/api/v5"
DATA_ANOMALY = "数据拉取异常"           # 主库与备用库全失败的统一报告用语
FAILOVER_MAX_ATTEMPTS = 3                 # 规范 1.1：静默重试最多 3 轮
FAILOVER_WINDOW = 5.0                    # 单轮超 5 秒视为窗口过长，回到第一步重拉
GIT_SYNC_TIMEOUT = 20                     # 网络 IO 超时：超时明确报错，不是静默等待（规范 1.2）
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


def normalize_host(value):
    """把任意 GIT 地址 / 裸域名归一到同一个节流 key；本地路径返回空串。

    同一域名含子域：api.github.com 与 github.com 归到 github.com；
    git@gitee.com:owner/repo.git 与 https://gitee.com/... 归到 gitee.com。
    由公共层统一提供，各模块不要自己切字符串。
    """
    s = (value or "").strip()
    if not s:
        return ""
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith(("/", ".", "~")):
        return ""          # 本地文件系统路径不是地址访问，不排队
    if "://" in s:
        host = urllib.parse.urlparse(s).netloc.split("@")[-1].split(":")[0]
    elif "@" in s and ":" in s:
        host = s.split("@", 1)[1].split(":", 1)[0]
    else:
        host = s.split("/")[0].split(":")[0]
    host = host.lower().rstrip(".")
    for prefix in ("api.", "www.", "git."):
        if host.startswith(prefix) and len(host) > len(prefix):
            host = host[len(prefix):]
            break
    return host


def parse_repo_identity(value):
    """从 git 地址解析 (host, owner/repo 路径)，把 https 与 ssh 两种写法归一成同一仓库身份。

    支持 https://host/owner/repo(.git) 与 git@host:owner/repo(.git)（含 ssh:// 前缀）；
    解析不出合法 host + 路径（本地路径 / 裸字符串）返回 None。
    """
    s = (value or "").strip()
    if not s:
        return None
    if (len(s) >= 3 and s[1] == ":" and s[0].isalpha()
            and s[2] in ("\\", "/")) or s.startswith(("/", ".", "~")):
        return None          # 本地文件系统路径不是 git 地址
    if "://" in s:
        rest = s.split("://", 1)[1]
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        host, _, path = rest.partition("/")
    elif "@" in s and ":" in s and s.index("@") < s.index(":"):
        rest = s.split("@", 1)[1]
        host, _, path = rest.partition(":")
    else:
        return None
    host = host.split(":")[0].lower().rstrip(".")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or path.count("/") < 1:
        return None
    return host, path


def derive_alternate_url(value):
    """同一仓库的另一种协议写法：HTTPS ⇄ SSH（含 git@ 与 ssh://）。

    认证各设备自理（SSH 用密钥、HTTPS 用凭据管理器），代码不碰；仅用于
    「同一仓库自动尝试两种协议」兜底，不依赖设备恰好支持某一种传输。
    """
    ident = parse_repo_identity(value)
    if not ident:
        return ""
    host, path = ident
    if (value or "").strip().startswith("http"):
        return "git@%s:%s.git" % (host, path)
    return "https://%s/%s.git" % (host, path)


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


def _pace_lock_path():
    """闸门锁统一放 LOCK_DIR（用户主目录），避开手机共享存储不支持 flock 的位置。"""
    return LOCK_DIR / (PACE_FILE.name + ".lock")


def _hold_pace_lock(f):
    """跨进程互斥：避免两个进程读到同一个 last 后同时放行（规范 1.8）。"""
    f.seek(0, os.SEEK_END)
    if f.tell() == 0:
        f.write(b"0")
        f.flush()
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        for _ in range(3):          # 规范 1.1：静默重试最多 3 次
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                time.sleep(1)
        return False
    import fcntl
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except OSError as e:
        # 手机共享存储（FUSE）不实现 flock：降级为无锁，仍按落盘状态排队
        if e.errno in LOCK_UNSUPPORTED_ERRNOS:
            elog("文件系统不支持文件锁",
                 "errno=%s，节流降级为无锁排队" % e.errno)
            return False
        raise
    return True


def pace_gate(host):
    """同一 host 连续访问前过闸：不足 3.5 秒就补齐，状态落盘跨进程共享。

    host 可以传完整 GIT 地址或裸域名；统一由 normalize_host 归一，别各自计时。
    线程锁管同进程并发，OS 文件锁管跨进程并发，两者共用同一份落盘状态。
    """
    key = normalize_host(host)
    if not key or key in PACE_EXEMPT_HOSTS:
        return
    with _PACE_LOCK:
        lock_path = _pace_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(lock_path), "a+b") as lf:
            if not _hold_pace_lock(lf):
                elog("同域名闸门跨进程锁未拿到",
                     "仍按落盘状态排队，不跳过间隔")
            state = _read_json(PACE_FILE, {})
            last = float(state.get(key, 0) or 0)
            wait = HOST_GAP_SECONDS - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            state[key] = time.time()
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


def prune_backups(service=None, keep=3):
    """清理 backup_root(service) 下最旧的整份备份目录，只留最近 keep 份。

    备份子目录命名 ``<时间戳>-<进程号>``（前缀 %Y%m%d-%H%M%S），字典序即时间序。
    只删 backup_root 内部的纯时间戳子目录，**绝不碰运行数据根目录**。
    返回实际删除的目录数。
    """
    if keep is None or keep < 0:
        keep = 0
    root = backup_root(service)
    if not root.exists():
        return 0
    subs = [p for p in root.iterdir() if p.is_dir()]
    subs.sort(key=lambda p: p.name, reverse=True)  # 最新在前
    removed = 0
    for p in subs[keep:]:
        shutil.rmtree(str(p), ignore_errors=True)
        removed += 1
    return removed


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
        self.refresh()                # 每轮重新列目录，否则看不到信号变化（仅底层能力）
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


class GitRepoStore(object):
    """纯 GIT 数据后端：读用 git show 取单个文件，回写就是 commit + git push。

    不需要任何密钥 / Contents API：私有库就是普通 git 仓库，本机已有的 HTTPS 凭证或
    SSH key 即可读写。只读真正用到的那几个文件，不落地整个仓库；判断层
    GitSignalStore 已把两个源 fetch 到同一个 gitsync 镜像，这里直接复用对象。
    """

    def __init__(self, url, path="services", branch="main", service=None):
        self.url = (url or "").strip()
        self.path = (path or "").strip().strip("/")
        self.branch = (branch or "main").strip()
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", service or "global")
        self.mirror = STATE_DIR / "gitsync" / (service or "__global__")
        self.base_ref = "refs/updater/" + key
        self._ready = False

    def _host(self):
        if "://" in self.url:
            return urllib.parse.urlparse(self.url).netloc.split("@")[-1].split(":")[0]
        if "@" in self.url:
            return self.url.split("@", 1)[1].split(":", 1)[0]
        return self.url

    def _rel(self, name):
        return (self.path + "/" + name) if self.path else name

    def _ensure(self):
        if (self.mirror / ".git").exists():
            return True
        self.mirror.mkdir(parents=True, exist_ok=True)
        code, out, err = git_cmd(["init", "--quiet"], cwd=self.mirror)
        if code != 0:
            elog("GIT 数据镜像初始化失败", err or out)
            return False
        return True

    def refresh(self):
        """把本源最新 commit 拉到专用 ref；不写 FETCH_HEAD，不干扰判断层。

        同一仓库配齐另一种协议兜底：主协议失败自动换一种试，不依赖设备
        恰好支持某一种传输（SSH 密钥 / HTTPS 凭据各环境自理）。
        """
        if not self._ensure():
            raise RuntimeError("GIT 数据镜像不可用")
        candidates = [self.url]
        alt = derive_alternate_url(self.url)
        if alt and alt != self.url:
            candidates.append(alt)
        last_err = ""
        for cand in candidates:
            code, out, err = git_cmd(
                ["fetch", "--no-tags", "-f", cand,
                 "%s:%s" % (self.branch, self.base_ref)], cwd=self.mirror)
            if code == 0:
                break
            last_err = (err or out)[:200]
        else:
            raise RuntimeError("GIT 拉取失败：%s" % last_err)
        code, sha, err = git_cmd(["rev-parse", self.base_ref], cwd=self.mirror)
        if code != 0 or not sha:
            raise RuntimeError("GIT 版本读取失败：%s" % (err or "")[:200])
        self._ready = True
        return sha

    def read(self, name):
        if not self._ready:
            self.refresh()
        code, out, _ = git_cmd(
            ["show", "%s:%s" % (self.base_ref, self._rel(name))],
            cwd=self.mirror, strip=False)
        return out if code == 0 else None

    def write(self, name, text):
        """基于本源最新 commit 造一个新提交并上传；非快进被拒就抛给上层重合并。"""
        base = self.refresh()
        blob_file = self.mirror / ".git" / ("updater_blob_%d" % os.getpid())
        idx_file = self.mirror / ".git" / ("updater_index_%d" % os.getpid())
        env = {"GIT_INDEX_FILE": str(idx_file)}
        try:
            blob_file.write_text(text, encoding="utf-8")
            code, blob, err = git_cmd(["hash-object", "-w", str(blob_file)],
                                      cwd=self.mirror)
            if code != 0 or not blob:
                raise RuntimeError("GIT 写对象失败：%s" % (err or "")[:200])
            for args in (["read-tree", base],
                         ["update-index", "--add", "--cacheinfo",
                          "100644,%s,%s" % (blob, self._rel(name))]):
                code, out, err = git_cmd(args, cwd=self.mirror, env=env)
                if code != 0:
                    raise RuntimeError("GIT 构造提交失败：%s" % (err or out)[:200])
            code, tree, err = git_cmd(["write-tree"], cwd=self.mirror, env=env)
            if code != 0 or not tree:
                raise RuntimeError("GIT 写树失败：%s" % (err or "")[:200])
            code, commit, err = git_cmd(
                ["-c", "user.name=wm-bot-updater",
                 "-c", "user.email=updater@localhost",
                 "commit-tree", tree, "-p", base, "-m", "updater: " + name],
                cwd=self.mirror)
            if code != 0 or not commit:
                raise RuntimeError("GIT 建提交失败：%s" % (err or "")[:200])
            code, out, err = git_cmd(
                ["push", self.url, "%s:refs/heads/%s" % (commit, self.branch)],
                cwd=self.mirror)
            if code != 0:
                raise RuntimeError("GIT 上传失败（可能被别的提交抢先）：%s"
                                   % (err or out)[:200])
            git_cmd(["update-ref", self.base_ref, commit], cwd=self.mirror)
        finally:
            for p in (blob_file, idx_file):
                try:
                    p.unlink()
                except OSError:
                    pass

    def signals(self):
        return []                     # 判断恒由 GitSignalStore 的 GIT 双源拷贝负责

    def signal(self):
        return Signal("(git)", self.base_ref, False)


def make_store(cfg, service=None):
    """按配置造【数据后端】；service 给定时自动切到该服务在私有仓库里的子目录。

    UPDATER_DATA_NAMESPACE 再插一层：同一套代码、不同授权的两台服务端各配一个，
    数据就不会落到同一份 runtime.json 上。这层是可替换的：默认用 GIT 指令读写
    私有仓库文件（不需要任何密钥 / 第三方接口），以后换成数据库地址 / 第三方数据接口，
    只要按 Store 接口（signals / signal / read / write / refresh）加适配器并在这里注册，
    服务与迁移逻辑都不用动。
    """
    kind = (cfg.get("UPDATER_BACKEND") or "local").strip().lower()
    parts = [p for p in ((cfg.get("UPDATER_DATA_NAMESPACE") or "").strip().strip("/"),
                         (service or "").strip().strip("/")) if p]
    if kind == "git":
        path = (cfg.get("UPDATER_GIT_PATH") or "services").strip().strip("/")
        for part in parts:
            path = (path + "/" + part) if path else part
        return GitRepoStore(cfg.get("UPDATER_DATA_REPO"), path,
                            cfg.get("UPDATER_DATA_BRANCH", "main"), service)
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
            "数据后端 %s 尚未实现：请按 Store 接口加适配器并在 make_store 里注册"
            % kind)
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

    def set_index(self, i):
        """GIT 双源比版本后直接把活跃后端切到最新那份，不必等 3 轮失败。"""
        if 0 <= i < len(self.stores) and i != self.index:
            self.index = i
            flog("数据后端按版本切换 → %s" % self.label())

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
    """造备用数据后端；关了故障转移 / 没配备用地址就不启用。"""
    if str(cfg.get("UPDATER_FAILOVER") or "1").strip().lower() in (
            "0", "false", "no", "off"):
        return None
    kind = (cfg.get("UPDATER_BACKEND") or "local").strip().lower()
    if kind == "git":
        backup_url = (cfg.get("UPDATER_DATA_REPO_BACKUP") or "").strip()
        if not backup_url:
            return None
        sub = dict(cfg)
        sub["UPDATER_DATA_REPO"] = backup_url
        return make_store(sub, service)
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


# --------------------- GIT 双源拷贝：更新判断 ---------------------
def _git_net_url(args):
    """从网络 git 指令里取出目标地址；本地指令返回 None（不排队）。"""
    if not args or str(args[0]) not in GIT_NET_COMMANDS:
        return None
    for a in list(args)[1:]:
        s = str(a)
        if s.startswith("-"):
            continue
        if "://" in s:
            return s
        if "@" in s and ":" in s and s.index("@") < s.index(":"):
            return s
    return None


def git_cmd(args, cwd=None, timeout=GIT_SYNC_TIMEOUT, env=None, strip=True):
    """跑一条 git 指令，返回 (returncode, stdout, stderr)；失败不抛，交给编排层判。

    env 供 GIT_INDEX_FILE 这类只影响单次调用的 plumbing 变量使用；
    strip=False 用于 git show 取文件内容，保留首尾空白与末尾换行。
    fetch / push / clone / pull 都是地址访问：统一在这里自动过同域名闸门，
    调用点不必各自记得 pace_gate，判断层 / 代码同步 / 数据读写都不会漏。
    """
    url = _git_net_url(args)
    if url:
        pace_gate(url)
    try:
        run_env = dict(os.environ)
        # 无凭据时禁止 git 弹交互式「Username for ...」验证提示，直接失败并让
        # 编排层回退到备用协议 / 备用源（手机没配 GitHub 凭据时尤其必要）。
        run_env["GIT_TERMINAL_PROMPT"] = "0"
        if env:
            run_env.update(env)
        r = subprocess.run(["git"] + list(args), cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", env=run_env)
        out = r.stdout or ""
        return r.returncode, (out.strip() if strip else out), (r.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


class GitSignalStore(object):
    """用 GIT 拷贝结果判断更新：两个私有库接口都 fetch 下来，谁的版本新用谁。

    拷贝下来没有内容变化（HEAD 与上次处理过的相同）就是「不用更新」；
    一个接口因网络失败就用拷贝成功的那个继续判断；两个都失败才记
    「数据拉取异常」，本地数据保持原样。本地比远端新时不回退、不覆盖本地
    （数据文件依旧由 Store 的重拉-重合并保护）。选中版本后把后端链切到
    同一接口，读 / 写都走那份最新的数据。
    """

    def __init__(self, cfg, service=None, chain=None):
        self.cfg = cfg
        self.service = service
        self.state_key = service or "__global__"
        self.chain = chain
        self.branch = (cfg.get("UPDATER_DATA_BRANCH") or "main").strip()
        self.mirror = STATE_DIR / "gitsync" / self.state_key
        self.last = None

    def sources(self):
        pairs = [("主库(GitHub)", self.cfg.get("UPDATER_DATA_REPO")),
                 ("备用库(Gitee)", self.cfg.get("UPDATER_DATA_REPO_BACKUP"))]
        out = []
        for label, url in pairs:
            url = (url or "").strip()
            if url:
                out.append((label, url))
        if not out:
            # 老配置只填了 owner/repo：拼成 GitHub 地址，仍能做一次拷贝
            repo = (self.cfg.get("UPDATER_GITHUB_REPO") or "").strip()
            if repo and "你的账号" not in repo:
                out.append(("主库(GitHub)", "https://github.com/%s.git" % repo))
        return out

    def _ensure_repo(self):
        if (self.mirror / ".git").exists():
            return True
        self.mirror.mkdir(parents=True, exist_ok=True)
        code, out, err = git_cmd(["init", "--quiet"], cwd=self.mirror)
        if code != 0:
            elog("git 同步缓存目录初始化失败", err or out)
            return False
        return True

    def _repo_candidates(self, url):
        """给一个源 URL 配齐另一种协议的等价写法；协议失败自动换一种试。

        同一仓库身份的 HTTPS / SSH 写法都被 normalize_host 归一到同一 host，
        所以两种协议视作同一源，节流与版本比较不会把它们当两个不同源。
        """
        alts = [(url, "主协议")]
        alt = derive_alternate_url(url)
        if alt and alt != url and alt not in [a for a, _ in alts]:
            alts.append((alt, "备用协议"))
        return alts

    def sync(self):
        if not self._ensure_repo():
            elog(DATA_ANOMALY, "git 缓存目录不可用，两个接口都没拷贝成功")
            return None
        found = []
        for idx, (label, url) in enumerate(self.sources()):
            success_url = None
            for cand, kind in self._repo_candidates(url):
                code, out, err = git_cmd(["fetch", "--no-tags", cand, self.branch],
                                         cwd=self.mirror)
                if code == 0:
                    success_url = cand
                    break
                flog("[%s] %s（%s）拷贝失败：%s"
                     % (self.state_key, label, kind, (err or out)[:200]))
            if success_url is None:
                flog("[%s] %s 两种协议均拷贝失败，跳过" % (self.state_key, label))
                continue
            c2, sha, _ = git_cmd(["rev-parse", "FETCH_HEAD"], cwd=self.mirror)
            c3, ts, _ = git_cmd(["log", "-1", "--format=%ct", "FETCH_HEAD"],
                                cwd=self.mirror)
            if c2 or c3 or not sha or not ts.isdigit():
                flog("[%s] %s 拷贝成功但读不到版本，跳过" % (self.state_key, label))
                continue
            found.append({"label": label, "url": success_url, "sha": sha,
                          "ts": int(ts), "index": idx})
        if not found:
            elog(DATA_ANOMALY,
                 "%s：两个接口的 GIT 拷贝均失败（本地数据保持原样）" % self.state_key)
            return None
        # 比版本：commit 时间新者优先；同时间优先主库，避免两边来回抖
        best = max(found, key=lambda x: (x["ts"], -x["index"]))
        if self.chain is not None and hasattr(self.chain, "set_index"):
            self.chain.set_index(best["index"])
        self.last = best
        flog("[%s] GIT 拷贝成功 %d 个接口，选用 %s 版本 %s"
             % (self.state_key, len(found), best["label"], best["sha"][:8]))
        return best

    def signal_from(self, base, state_key):
        """用本轮已拷好的结果派生某个服务的 Signal：一轮只拷一次，不重复 fetch。"""
        if base is None:
            return Signal("(git-sync-failed)", "none", False)
        prev = load_state(state_key).get("last_signal")
        changed = base["sha"] != prev
        if not changed:
            flog("[%s] 拷贝没有内容更新，本轮不用更新" % state_key)
        return Signal("git:" + base["label"], base["sha"], changed)

    def signal(self):
        return self.signal_from(self.sync(), self.state_key)

    def signals(self):
        best = self.last or self.sync()
        if best is None:
            return []
        return [("git:" + best["label"], best["sha"])]

    def refresh(self):
        return self.sync()


def read_root_file(base, name, state_key="__global__"):
    """从本轮已拷贝的结果里读私有库【根目录】文件，不再发任何网络请求。

    update.cfg 这类全局配置放在私有库根目录、不在 services/ 下，所以不走
    Store 的 path 拼接，直接用本轮选定的 commit 版本在镜像里取文件。
    """
    if not base or not base.get("sha"):
        return None
    mirror = STATE_DIR / "gitsync" / state_key
    code, out, _ = git_cmd(
        ["show", "%s:%s" % (base["sha"], name)], cwd=mirror, strip=False)
    return out if code == 0 else None


def make_signal_store(cfg, service=None, chain=None):
    """更新判断统一走 GIT 双源拷贝：git fetch 两个私有库接口，比版本选最新。

    判断方式固定，旧配置里残留的任何信号源设置都会被忽略。
    chain 传入时，选中的接口同步成为数据后端活跃源。
    """
    return GitSignalStore(cfg, service, chain)


class CodeSignalStore(GitSignalStore):
    """盯公共仓库代码的版本：只看两个公共库源，状态与私有库信号完全分开。

    为什么必须独立：私有库没有新提交时，数据信号会一直报「不用更新」；
    但用户修 bug 只 push 公共库代码，手机也必须自动同步（无人值守的根诉求）。
    """

    def __init__(self, cfg):
        GitSignalStore.__init__(self, cfg, service="__code__")
        self.branch = (cfg.get("UPDATER_CODE_BRANCH") or "main").strip()

    def sources(self):
        pairs = [("主库(GitHub)", self.cfg.get("UPDATER_CODE_REPO")),
                 ("备用库(Gitee)", self.cfg.get("UPDATER_CODE_REPO_BACKUP"))]
        return [(label, (url or "").strip())
                for label, url in pairs if (url or "").strip()]


def make_code_signal_store(cfg):
    """配了公共库地址才启用代码版本轮询；没配就保持旧行为（只在启动时同步）。"""
    for key in ("UPDATER_CODE_REPO", "UPDATER_CODE_REPO_BACKUP"):
        if (cfg.get(key) or "").strip():
            return CodeSignalStore(cfg)
    return None


def derive_signal(store, base, state_key):
    """一轮只拷一次：把本轮拷贝结果派生给各服务；旧 Store 仍走自己的 signal()。"""
    fn = getattr(store, "signal_from", None)
    if fn is not None:
        return fn(base, state_key)
    return store.signal()


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


def reconcile_config(template_text, values_text, mode="structure", local_text=""):
    """以【公共仓库模板】为结构基准，按「私有库 > 本地当前运行配置 > 模板默认/空」安全合并。

    - 模板有、私有值非空 → 用私有值（保留真实值，绝不被空值冲掉）
    - 私有值为空、本地当前配置有值 → 用本地当前配置的值（防止别的设备推来的旧版
      空值把本地原本有值的字段冲空；本地运行过就有最新值）
    - 两者都没有 → 保留模板行（空 / 默认），即「新字段先留空」
    - 私有有、模板没有 → 保留（绝不静默删除；方案 A 的兼容兜底）
    模板的注释 / 描述行原样保留，保证私有库里也能看到每个字段的说明。

    返回 (合并文本, 报告)。报告里的 new_fields 是「模板有、私有没有」的字段（含用本地值
    补上的），出现时需要把结果回写私有库；removed 永远为空。
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
    if not values_text and not local_text:
        # 私有库和本地都没有任何值：直接给一份模板（全空），等私有库自己填
        return template_text, {"mode": mode, "added": [], "removed": [],
                               "kept": [], "from_local": [],
                               "new_fields": [], "extra_kept": [],
                               "skipped": "私有地址没有 config.txt，先用模板"}
    tmpl_keys = {}
    for line in template_text.splitlines():
        m = CONFIG_KV.match(line)
        if m:
            tmpl_keys[m.group(1)] = m.group(2)
    val_vals = {}
    for line in (values_text or "").splitlines():
        m = CONFIG_KV.match(line)
        if m:
            val_vals[m.group(1)] = m.group(2)
    # 本地当前运行配置：私有库缺值时的第二来源（防止旧版空值冲掉本地真实值）
    local_vals = {}
    for line in (local_text or "").splitlines():
        m = CONFIG_KV.match(line)
        if m:
            local_vals[m.group(1)] = m.group(2)
    out = []
    added, kept, from_local = [], [], []
    for line in template_text.splitlines():
        m = CONFIG_KV.match(line)
        if not m:
            out.append(line)                      # 注释 / 描述原样保留
            continue
        key = m.group(1)
        if key in val_vals and val_vals[key].strip():
            out.append("%s = %s" % (key, val_vals[key].rstrip()))
            kept.append(key)
        elif key in local_vals and local_vals[key].strip():
            # 私有库没值、本地运行过有值：用本地当前值，绝不冲成空
            out.append("%s = %s" % (key, local_vals[key].rstrip()))
            kept.append(key)
            from_local.append(key)
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
                  "from_local": from_local,
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
    # 内容与云端一致就跳过：否则每轮都会造一个「updater: runtime.json」新提交，
    # commit 时间戳不同 → sha 不同 → 下一轮被判成「有新数据」→ 无限重启循环。
    try:
        current = store.read(RUNTIME_NAME)
    except Exception:
        current = None
    if current is not None and current.strip() == text.strip():
        flog("[%s] 运行数据无变化，跳过回写（避免空提交触发更新循环）"
             % (service or "default"))
        return True
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


# ------------------- 运行记录类（纯文本 / 本地优先 / 备份到私有库） -------------------
# 与模板类（config.txt：私有库为准）相反：这类文件条目允许增删（如群白名单 groups.txt），
# 本地就是最新；私有库只作备份，本地丢了能从私有库恢复。
TEXT_RUNTIME_SUBDIR = "data"          # 私有库里落点：<服务目录>/data/<文件名>


def read_text_runtime(store, names):
    """从私有库读纯文本运行记录；读失败 / 不存在返回 None（本次跳过，不覆盖本地）。"""
    out = {}
    for name in names:
        try:
            out[name] = store.read(TEXT_RUNTIME_SUBDIR + "/" + name)
        except Exception as e:
            elog("文本运行记录读取失败（本次跳过该文件）", "%s %s" % (name, e))
            out[name] = None
    return out


def merge_text_runtime(data_dir, names, cloud_texts):
    """合并纯文本运行记录：本地优先，本地空时由私有库恢复。

    返回 [(name, text, write_local, write_cloud)]；text 为 None 表示读取失败、跳过。
      - 本地有内容 → 用本地；与私有库不同才回写（本地删掉的条目也随之同步删除）
      - 本地空 / 不存在、私有库有 → 用私有库内容恢复本地
      - 两边都空 → 什么都不做
    """
    plan = []
    for name in names:
        p = Path(data_dir) / name
        local = ""
        try:
            if p.exists():
                local = p.read_text(encoding="utf-8-sig")
        except Exception as e:
            elog("文本运行记录读取失败，本次不覆盖", "%s %s" % (name, e))
            plan.append((name, None, False, False))
            continue
        cloud = cloud_texts.get(name) or ""
        if local.strip():
            plan.append((name, local, False, local != cloud))
        elif cloud.strip():
            plan.append((name, cloud, True, False))
        else:
            plan.append((name, "", False, False))
    return plan


def push_text_runtime(store, name, text):
    """把本地文本运行记录回写私有库 data/<name>（本地为准）；只在内容不同时调用。"""
    if text and not text.endswith("\n"):
        text += "\n"
    store.write(TEXT_RUNTIME_SUBDIR + "/" + name, text)
    return text
