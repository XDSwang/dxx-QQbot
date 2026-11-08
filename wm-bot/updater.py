#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""updater.py — 自动更新的远程读取 / 配置合并 / 运行数据迁移（零第三方依赖）。

私有地址上的文件（名字写死为常量，由本模块统一提供）：
  run0 / run1     信号。文件名改成 run1 = 需要更新，run0 = 不需要；也认 run2、run3…
                  （递增版本号）。配合「指纹」做幂等，不会每 10 秒重复更新一次。
  config.txt      机器人配置（全字段模板；服务端本地的密钥 / 路径 / 白名单值默认保留）
  schema.json     最新字段结构 + 改名 / 删除规则（用户维护，服务端只读）
  runtime.json    服务端回写的「最新完整 + 旧数据迁移」结果（机器维护，用户只读）

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
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON_DIR = HERE / "python"
STATE_DIR = HERE / "data"                       # updater 自己的状态（根 .gitignore 已忽略 data/）
BOT_DATA_DIR = PYTHON_DIR / "data"              # 机器人运行数据（同样不进 git）

ERROR_LOG = STATE_DIR / "updater_error.log"     # 异常日志：覆盖式，只记异常
FLOW_LOG = STATE_DIR / "updater_flow.log"       # 流程日志：追加式，正常操作也记
PACE_FILE = STATE_DIR / "updater_pace.json"     # 同域名节流状态（落盘）
STATE_FILE = STATE_DIR / "update_state.json"    # 已处理的信号指纹 / 迁移版本
BACKUP_ROOT = STATE_DIR / "backup"
LOCK_FILE = STATE_DIR / "updater.lock"
BOT_CONFIG_FILE = BOT_DATA_DIR / "config.txt"

CONFIG_NAME = "config.txt"
SCHEMA_NAME = "schema.json"
RUNTIME_NAME = "runtime.json"
SIGNAL_RE = re.compile(r"^run(\d+)$")
POLL_SECONDS = 10
HOST_GAP_SECONDS = 3.5                           # 写死：落在规范的 3~5 秒区间内
GITHUB_API = "https://api.github.com"
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


def load_state():
    return _read_json(STATE_FILE, {})


def save_state(state):
    _atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


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

    def _api(self, method, url, body=None):
        pace_gate("api.github.com")
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

    def signal(self):
        self._listing = None          # 每轮重新列目录，否则永远看不到新的 runN
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
        listing = self._dir()
        body = {"message": "updater: migrated runtime data",
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "branch": self.branch}
        if name in listing:
            body["sha"] = listing[name]["sha"]
        self._api("PUT", self._url(name), body)
        self._listing = None


def make_store(cfg):
    """按配置造后端；后端可插拔，换私有地址不用改迁移逻辑。"""
    kind = (cfg.get("UPDATER_BACKEND") or "local").strip().lower()
    if kind == "github":
        return GitHubStore(cfg.get("UPDATER_GITHUB_REPO"),
                           cfg.get("UPDATER_GITHUB_TOKEN"),
                           cfg.get("UPDATER_GITHUB_PATH", ""),
                           cfg.get("UPDATER_GITHUB_BRANCH", "main"),
                           cfg.get("UPDATER_GITHUB_API", GITHUB_API))
    if kind == "local":
        return LocalStore(cfg.get("UPDATER_LOCAL_DIR") or str(HERE / "remote"))
    raise ValueError("未知的 UPDATER_BACKEND: %s" % kind)


def fetch_bundle(store):
    """一次拿到私有地址上的配置 / 结构规则 / 云端运行数据。"""
    schema = {}
    runtime = {}
    raw_schema = store.read(SCHEMA_NAME)
    raw_runtime = store.read(RUNTIME_NAME)
    if raw_schema:
        schema = json.loads(raw_schema)
    if raw_runtime:
        runtime = json.loads(raw_runtime)
    return {"config": store.read(CONFIG_NAME),
            "schema": schema, "runtime": runtime}


# ------------------------------ 配置合并 ------------------------------
def merge_config(local_text, remote_text, mode="structure"):
    """以私有地址的配置为【结构基准】，同步字段增删，但保留服务端本地的值。

    structure（默认）：remote 有这个键 → 用 local 的值（local 非空时）；
                      remote 新增键 → 补 remote 默认值；remote 删掉的键 → 不再写入。
    overwrite       ：整份照抄 remote（只有私有地址放的是含密钥的完整副本时才用）。

    为什么默认不是整份覆盖：仓库里的 python/config.txt 是公开模板，WM_QQ_APPID /
    WM_QQ_SECRET 是空的；覆盖过去服务端立刻连不上机器人。
    """
    if not remote_text:
        return local_text, {"mode": mode, "added": [], "removed": [], "kept": [],
                            "skipped": "私有地址没有 config.txt"}
    if mode == "overwrite" or not local_text:
        return remote_text, {"mode": mode, "added": [], "removed": [], "kept": [],
                             "skipped": ""}
    local_vals = {}
    for line in (local_text or "").splitlines():
        m = CONFIG_KV.match(line)
        if m:
            local_vals[m.group(1)] = m.group(2)
    remote_keys = set()
    added, kept = [], []
    out = []
    for line in remote_text.splitlines():
        m = CONFIG_KV.match(line)
        if not m:
            out.append(line)
            continue
        key, val = m.group(1), m.group(2)
        remote_keys.add(key)
        if key in local_vals and local_vals[key].strip():
            out.append("%s = %s" % (key, local_vals[key].rstrip()))
            kept.append(key)
        else:
            out.append(line)
            added.append(key)
    removed = sorted(set(local_vals) - remote_keys)
    text = "\n".join(out)
    if remote_text.endswith("\n"):
        text += "\n"
    return text, {"mode": mode, "added": added, "removed": removed,
                  "kept": kept, "skipped": ""}


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


def write_runtime_files(runtime, data_dir, backup=True):
    """备份 → 逐文件原子写 → 出错整体回滚。返回 (ok, 备份目录/错误)。"""
    data_dir = Path(data_dir)
    backup_dir = None
    written = []
    try:
        if backup:
            backup_dir = BACKUP_ROOT / time.strftime("%Y%m%d-%H%M%S")
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


def push_runtime(store, runtime):
    """把「最新完整 + 旧数据迁移」的结果回写私有地址（用户只读，服务端只写）。"""
    text = json.dumps(runtime, ensure_ascii=False, indent=2) + "\n"
    store.write(RUNTIME_NAME, text)
    return text
