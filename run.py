#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run.py — 公共仓库根目录唯一运行入口（统一 RUN）。

服务端只跑这一条：python run.py

公共仓库是本体：代码 + services.json 服务清单，能放任意服务端跑。
数据后端（现在是 GitHub 私有仓库文件，代替数据库；以后可换数据库 / 第三方接口）
由根目录 update.cfg 优先配置，不进 git。

启动顺序：读 update.cfg → 装配数据后端 → 启动先同步代码（无 .git 时自举）
→ 必要时自动重启 RUN → 按 services.json 逐个拉起服务 → 每 10 秒查信号。
一次 git pull 是整仓库的，但只停 / 迁 / 启【被触发的那个服务】；
指纹、备份、进程按服务隔离，单实例锁全局一份。
"""
import atexit
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import updater

HERE = Path(__file__).resolve().parent          # 仓库根（公共仓库本体）
CFG_FILE = HERE / "update.cfg"
SERVICES_FILE = HERE / "services.json"

# 没传 svc 的调用（离线测试 / 单服务旧调用）仍落在 wm-bot 上
BOT_DATA_DIR = HERE / "wm-bot" / "python" / "data"
BOT_CONFIG = BOT_DATA_DIR / "config.txt"
DEFAULT_RUNTIME_FILES = (
    "ws_switches.json", "ws_filters.json", "ws_at_users.json",
    "ws_subs.json", "ws_last_sent.json",
)

DEFAULTS = {
    "UPDATER_BACKEND": "local",
    "UPDATER_LOCAL_DIR": str(HERE / "remote"),
    "UPDATER_GITHUB_REPO": "",
    "UPDATER_GITHUB_TOKEN": "",
    "UPDATER_GITHUB_PATH": "services",
    "UPDATER_GITHUB_BRANCH": "main",
    "UPDATER_GITHUB_API": updater.GITHUB_API,
    # 故障转移：主库 3 轮失败后自动切 Gitee，全失败报「数据拉取异常」
    "UPDATER_FAILOVER": "1",
    "UPDATER_GITEE_REPO": "",
    "UPDATER_GITEE_TOKEN": "",
    "UPDATER_GITEE_PATH": "services",
    "UPDATER_GITEE_BRANCH": "main",
    "UPDATER_GITEE_API": updater.GITEE_API,
    "UPDATER_CODE_REPO": "https://github.com/XDSwang/dxx-QQbot.git",
    "UPDATER_CODE_REPO_BACKUP": "git@gitee.com:xdswang/dxx-QQbot.git",
    "UPDATER_CODE_BRANCH": "main",
    "UPDATER_SYNC_ON_START": "1",
    "SERVICES_ENABLED": "",
    # 判断恒为 GIT 双源拷贝；UPDATER_SIGNAL_SOURCE 已停用，旧配置写了也被忽略
    # 两个私有库接口的 git 地址（SSH 已配好，不需要 token）
    "UPDATER_DATA_REPO": "git@github.com:XDSwang/dxx_private_data.git",
    "UPDATER_DATA_REPO_BACKUP": "git@gitee.com:xdswang/dxx_private_data.git",
    "UPDATER_DATA_BRANCH": "main",
    # 同一套代码、不同授权的多台服务端各配一个，数据目录就不会撞在一起
    "UPDATER_DATA_NAMESPACE": "",
    "UPDATER_INSTANCE_ID": "default",
    "UPDATER_POLL_SECONDS": "10",
    "UPDATER_GIT_PULL": "1",
    "UPDATER_CONFIG_MERGE": "structure",
    # 方案 A：按公共模板对账后，只有出现新字段才把结构回写私有库（避免每轮空提交）
    "UPDATER_PUSH_CONFIG": "1",
    # 每 10 秒检测私有库 config 是否被账号本人改过，改过就按模板迁移并热重载
    "UPDATER_SYNC_PRIVATE_CONFIG": "1",
    "UPDATER_PUSH_RUNTIME": "1",
}

STOP_WAIT_SECONDS = 3       # 规范 1.2 单次等待缺省 3 秒
GIT_TIMEOUT_SECONDS = 60
MAX_CRASH_RESTART = 3       # 规范 1.1 静默重试最多 3 次
SELF_FILES = (HERE / "run.py", HERE / "updater.py", HERE / "services.json")


def _off(value):
    return str(value or "").strip().lower() in ("0", "false", "no", "off")


def load_cfg(path=None):
    """读根目录优先配置 update.cfg；注释行与「名称 = 值」，缺的项用缺省补齐。"""
    cfg = dict(DEFAULTS)
    p = Path(path) if path else CFG_FILE
    if not p.exists():
        updater.elog("缺少根目录 update.cfg", "照 update.cfg.example 填好数据后端后再启动")
        return cfg
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


class Service(object):
    """services.json 里的一项；路径一律相对仓库根，解析成绝对路径。"""

    def __init__(self, raw, root):
        self.name = str(raw["name"])
        self.entry = str(raw["entry"])
        self.cwd = Path(root) / str(raw.get("cwd") or ".")
        self.data_dir = Path(root) / str(raw["data_dir"])
        self.config_file = self.data_dir / str(raw.get("config_file") or "config.txt")
        self.runtime_files = tuple(raw.get("runtime_files") or ())
        self.remote_path = str(raw.get("remote_path") or self.name)


def load_services(path=None):
    """读公共仓库本体的服务清单；以后加服务只改这个文件，不动 run.py。"""
    p = Path(path) if path else SERVICES_FILE
    if not p.exists():
        raise RuntimeError("缺少服务清单 services.json（应随公共仓库代码一起同步）")
    data = json.loads(p.read_text(encoding="utf-8"))
    items = data.get("services") or []
    if not items:
        raise RuntimeError("services.json 里没有任何服务")
    out = [Service(x, HERE) for x in items]
    names = [s.name for s in out]
    if len(set(names)) != len(names):
        raise RuntimeError("services.json 里有重名服务")
    for s in out:
        if "/" in s.name or "\\" in s.name or s.name in (".", ".."):
            raise RuntimeError("服务名不能带路径分隔符: %s" % s.name)
    return out


def enabled_services(services, cfg):
    """同一份公共仓库能放任意服务端跑，各自用自己 update.cfg 的白名单。"""
    raw = (cfg.get("SERVICES_ENABLED") or "").strip()
    if not raw:
        return services
    want = {x.strip() for x in raw.split(",") if x.strip()}
    unknown = want - {s.name for s in services}
    if unknown:
        raise RuntimeError("SERVICES_ENABLED 里有清单中不存在的服务: %s"
                           % ",".join(sorted(unknown)))
    return [s for s in services if s.name in want]


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def acquire_lock():
    """单实例锁：残留锁不算有效凭证，先确认 pid 不存活才清场覆盖（规范 1.5）。"""
    updater.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if updater.LOCK_FILE.exists():
        raw = updater.LOCK_FILE.read_text(encoding="utf-8").strip()
        if raw.isdigit() and _pid_alive(raw):
            updater.elog("已有统一 RUN 在运行", "pid=%s；不要开第二个 run.py" % raw)
            return False
        updater.LOCK_FILE.unlink()
    updater._atomic_write(updater.LOCK_FILE, str(os.getpid()))
    atexit.register(release_lock)
    return True


def release_lock():
    try:
        if updater.LOCK_FILE.exists():
            updater.LOCK_FILE.unlink()
    except Exception as e:
        updater.elog("清理实例锁失败", str(e))


class BotProcess(object):
    """服务子进程：正常退出 / 崩溃由主循环接管，stdout+stderr 直接继承终端。"""

    def __init__(self, service):
        if isinstance(service, str):
            service = Service({"name": "default", "entry": service, "cwd": ".",
                               "data_dir": "wm-bot/python/data",
                               "runtime_files": list(DEFAULT_RUNTIME_FILES)}, HERE)
        self.service = service
        self.p = None

    def start(self):
        cmd = [sys.executable, str(HERE / self.service.entry)]
        updater.flog("[%s] 启动服务 %s" % (self.service.name, " ".join(cmd)))
        self.p = subprocess.Popen(cmd, cwd=str(self.service.cwd))
        return self.p

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        if not self.alive():
            return True
        updater.flog("[%s] 停止服务（等待 %s 秒）"
                     % (self.service.name, STOP_WAIT_SECONDS))
        self.p.terminate()
        try:
            self.p.wait(timeout=STOP_WAIT_SECONDS)
            return True
        except subprocess.TimeoutExpired:
            # 无人值守场景：超时不静默跳过，明确记录后按「不再等待、继续流程」处理，
            # 强制结束是为了让迁移拿到独占的 data/，避免内存里的旧结构写回来。
            updater.elog("[%s] %s 秒未退出，强制结束"
                         % (self.service.name, STOP_WAIT_SECONDS),
                         "pid=%s 继续更新流程" % self.p.pid)
            self.p.kill()
            try:
                self.p.wait(timeout=STOP_WAIT_SECONDS)
            except Exception:
                pass
            return True


def _console(msg):
    """控制台提示 + 流程日志：配置没填、数据后端连不上这类问题必须当场看得见。"""
    print("[run] " + msg, flush=True)
    updater.flog(msg)


def mark_failed(sig, service=None):
    """失败信号落盘：同一个指纹不再每 10 秒重试（改 runN 内容或换版本号才重试）。"""
    st = updater.load_state(service)
    st["failed_signal"] = sig.fingerprint
    st["failed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    updater.save_state(st, service)


def _git(args, timeout=GIT_TIMEOUT_SECONDS):
    try:
        r = subprocess.run(["git"] + list(args), cwd=str(HERE),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def preflight(cfg, store, services=None, service_stores=None,
              root_signals=None, signal_stores=None):
    """启动自检：把「数据后端没配 / 没有 git / 信号或私有目录缺文件」打到控制台。

    只提示不退出：网络抖动时服务照常运行，只是暂时不会自动更新。
    返回问题列表，便于离线测试断言。
    """
    problems = []
    backend = (cfg.get("UPDATER_BACKEND") or "local").strip().lower()
    if backend == "github":
        repo = (cfg.get("UPDATER_GITHUB_REPO") or "").strip()
        token = (cfg.get("UPDATER_GITHUB_TOKEN") or "").strip()
        if not repo or "你的账号" in repo or not token or "github_pat_xxx" in token:
            problems.append("update.cfg 的 UPDATER_GITHUB_REPO / TOKEN 还是模板占位值")
    elif backend in ("database", "http", "api"):
        problems.append("数据后端 %s 尚未实现：请按 Store 接口加适配器" % backend)
    if not SERVICES_FILE.exists():
        problems.append("缺少 services.json 服务清单（属于公共仓库本体）")
    if not _off(cfg.get("UPDATER_GIT_PULL", "1")):
        # 手机 Termux / QPython 默认没有 git 二进制：不检查的话会「看起来在跑、
        # 自动更新其实一步都走不了」。git --version 失败要先于仓库检查。
        code, _, _ = _git(["--version"])
        if code != 0:
            problems.append("当前平台没有 git 命令（Termux 需 pkg install git），自动同步不可用")
        else:
            code, _, _ = _git(["rev-parse", "--show-toplevel"])
            if code != 0:
                if (cfg.get("UPDATER_CODE_REPO") or "").strip():
                    problems.append("当前不是 git 仓库：启动时会用 UPDATER_CODE_REPO 自动自举")
                else:
                    problems.append("当前不是 git 仓库且没配 UPDATER_CODE_REPO，自动同步不可用")
    if not (cfg.get("UPDATER_DATA_REPO") or "").strip() \
            and not (cfg.get("UPDATER_DATA_REPO_BACKUP") or "").strip():
        problems.append("GIT 双源同步没配 UPDATER_DATA_REPO / BACKUP，无法拷贝私有库")
    try:
        (root_signals or store).signal()
    except Exception as e:
        problems.append("GIT 双源同步自检失败：%s" % e)
    # 两个不同授权的服务共用同一个 data_dir = 共用 data/config.txt 和同一批运行记录，
    # 运行时必然串数据；注册表层先拦住，别等出了事再查。
    if services:
        seen = {}
        for s in services:
            key = str(s.data_dir)
            if key in seen and seen[key] != s.name:
                problems.append("[%s] 与 [%s] 共用 data_dir=%s：不同授权必须各给一份数据目录"
                                % (seen[key], s.name, key))
            else:
                seen[key] = s.name
    if not services:
        # 兼容没有服务清单的离线自检：单后端时也检查根目录下的配置 / 规则文件
        for name in (updater.CONFIG_NAME,):
            try:
                if store.read(name) is None:
                    problems.append("私有地址缺少 %s" % name)
            except Exception as e:
                problems.append("私有地址读取失败：%s" % e)
    else:
        for s in services:
            st = (service_stores or {}).get(s.name)
            sig_store = (signal_stores or {}).get(s.name) or st
            if st is None:
                continue
            try:
                sig_store.signal()
            except Exception as e:
                problems.append("[%s] GIT 双源同步自检失败：%s" % (s.name, e))
            try:
                for name in (updater.CONFIG_NAME,):
                    if st.read(name) is None:
                        problems.append("[%s] 私有目录缺少 %s" % (s.name, name))
            except Exception as e:
                problems.append("[%s] 私有目录读取失败：%s" % (s.name, e))
    for p in problems:
        _console("[warn] " + p)
    if not problems:
        _console("启动自检通过：数据后端与服务清单都可用")
    return problems


def self_fingerprint():
    """更新器自身文件的 mtime+size；git pull 改到它们就该重启进程。"""
    out = []
    for p in SELF_FILES:
        try:
            st = p.stat()
            out.append("%s:%s:%s" % (p.name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(p.name + ":missing")
    return "|".join(out)


def self_syntax_ok():
    """重启前先编译自检：拉下来的新代码有语法错误就别把正在跑的服务换掉。"""
    for p in SELF_FILES:
        try:
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except (OSError, SyntaxError) as e:
            updater.elog("更新器自身代码校验失败，保持旧进程运行", "%s %s" % (p.name, e))
            return False
    return True


def _as_list(bots):
    if isinstance(bots, dict):
        return list(bots.values())
    if isinstance(bots, (list, tuple)):
        return list(bots)
    return [bots]


def restart_self(bots):
    """git pull 改到 run.py / updater.py 时重启本进程，让新逻辑真正生效。

    execv 会替换进程映像，try/finally 不会再跑，所以先停所有服务、放掉单实例锁；
    失败则补回来，继续用旧进程服务，并留下异常日志。
    """
    if not self_syntax_ok():
        return False
    _console("更新器自身代码已更新，重启 run.py 使新逻辑生效")
    group = _as_list(bots)
    for b in group:
        b.stop()
    release_lock()
    try:
        os.execv(sys.executable,
                 [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])
    except Exception as e:
        updater.elog("重启统一 RUN 失败，继续用当前进程运行", str(e))
        acquire_lock()
        for b in group:
            b.start()
        return False
    return True


def git_pull(cfg):
    """代码同步：用 GIT 指令把两个公共库接口都拷贝下来，比版本用最新的那份。

    一个接口因网络失败就用拷贝成功的那个继续判断；两个都失败才算
    「数据拉取异常」，整次作废。本地版本比远端新时不回退、不覆盖本地较新的代码。
    """
    if _off(cfg.get("UPDATER_GIT_PULL", "1")):
        return True, "已关闭"
    branch = (cfg.get("UPDATER_CODE_BRANCH") or "main").strip()
    sources = [("主库(GitHub)", "origin")]
    backup = (cfg.get("UPDATER_CODE_REPO_BACKUP") or "").strip()
    if not _off(cfg.get("UPDATER_FAILOVER", "1")) and backup:
        sources.append(("备用库(Gitee)", backup))
    found = []
    failures = []
    for idx, (label, target) in enumerate(sources):
        code, out, err = _git(["fetch", "--no-tags", target, branch])
        if code != 0:
            failures.append("%s: %s" % (label, (err or out)[:200]))
            updater.flog("[%s] 代码拷贝失败：%s" % (label, (err or out)[:200]))
            continue
        c2, sha, _ = _git(["rev-parse", "FETCH_HEAD"])
        c3, ts, _ = _git(["log", "-1", "--format=%ct", "FETCH_HEAD"])
        if c2 or c3 or not sha or not ts.isdigit():
            failures.append("%s: 读不到版本" % label)
            continue
        found.append((int(ts), -idx, label, sha))
    if not found:
        updater.flog("两个公共库接口的 GIT 拷贝均失败")
        return False, "%s：主库与备用公共库 git fetch 均失败 | %s" % (
            updater.DATA_ANOMALY, " | ".join(failures)[:300])
    ts, _, label, sha = max(found)
    # 本地新用本地：本地 HEAD 的提交时间不早于远端就不回退
    lc, lts, _ = _git(["log", "-1", "--format=%ct", "HEAD"])
    if lc == 0 and lts.isdigit() and int(lts) >= ts:
        return True, "本地版本已是最新，不回退（%s）" % label
    code, out, err = _git(["merge", "--ff-only", sha])
    if code != 0:
        return False, "%s：%s 拷贝成功但无法快进合并 | %s" % (
            updater.DATA_ANOMALY, label, (err or out)[:300])
    return True, ("%s: " % label) + (out or ("已同步到 %s" % sha[:8]))[:200]


def sync_code(cfg):
    """启动先同步；拷贝来的目录没有 .git 时自举，但绝不覆盖本地较新的代码。"""
    if _off(cfg.get("UPDATER_GIT_PULL", "1")):
        return True, "已关闭"
    code, _, _ = _git(["rev-parse", "--show-toplevel"])
    if code != 0:
        repo = (cfg.get("UPDATER_CODE_REPO") or "").strip()
        if not repo:
            return False, "没有 .git 且未配 UPDATER_CODE_REPO，无法自动同步"
        branch = (cfg.get("UPDATER_CODE_BRANCH") or "main").strip()
        steps = [
            ["init"],
            ["add", "-A"],
            ["-c", "user.name=wm-bot-bootstrap", "-c",
             "user.email=bootstrap@localhost", "commit", "-m",
             "bootstrap: 拷贝来的本地版本"],
            ["remote", "add", "origin", repo],
            ["fetch", "origin", branch],
        ]
        for args in steps:
            c, o, e = _git(args, timeout=120)
            if c != 0:
                return False, "自举 git %s 失败：%s" % (" ".join(args), (e or o)[:200])
        updater.flog("首次运行：已把拷贝来的目录自举成 git 仓库（远端 %s）" % repo)
        # 不 checkout：拷贝来的版本可能比远端新。后续 git pull --ff-only 遇到
        # 本地领先会明确报错而不是回退，绝不会把新代码覆盖成远端旧版。
        return True, "bootstrap（本地文件保持原样）"
    return git_pull(cfg)


def _backup_files(backup_dir, names, data_dir, config_name):
    """写任何东西之前，把配置 + 所有将被覆盖的运行数据整份备份。"""
    bp = Path(backup_dir)
    bp.mkdir(parents=True, exist_ok=True)
    for name in list(names) + [config_name]:
        src = Path(data_dir) / name
        if src.exists():
            (bp / name).write_bytes(src.read_bytes())


def _restore_backup(backup_dir, data_dir):
    """更新中途失败：把已改过的运行数据 / 配置从备份还原。"""
    if not backup_dir:
        return
    p = Path(backup_dir)
    if not p.exists():
        return
    for src in p.iterdir():
        if src.is_file():
            (Path(data_dir) / src.name).write_bytes(src.read_bytes())


def do_update(cfg, store, bot, sig, svc=None, pull=True):
    """执行一个服务的一轮更新；无论成败都在 finally 里把该服务拉起来。

    svc 为空时退回单服务默认路径（离线测试 / 旧调用）；pull=False 用于全局信号
    已经统一 pull 过一次、后面多个服务只做迁移的场景。
    """
    data_dir = Path(svc.data_dir) if svc else BOT_DATA_DIR
    cfg_path = Path(svc.config_file) if svc else BOT_CONFIG
    runtime_files = svc.runtime_files if svc else DEFAULT_RUNTIME_FILES
    service = svc.name if svc else None
    updater.flog("[%s] GIT 双源检测到内容更新 %s 版本=%s"
                 % (service or "default", sig.name, sig.fingerprint))
    backup_dir = None
    succeeded = False       # 不叫 ok：do_update 里 ok 是 git_pull / 写文件的返回值
    try:
        if not bot.stop():
            return False
        if pull:
            ok, info = git_pull(cfg)
            if not ok:
                updater.elog("[%s] git pull 失败，本次更新作废" % (service or "default"),
                             info)
                return False
            updater.flog("[%s] git pull: %s" % (service or "default", info or "无变更"))

        backup_dir = str(updater.backup_root(service) /
                         (time.strftime("%Y%m%d-%H%M%S") + "-%d" % os.getpid()))
        bundle = updater.fetch_bundle_failover(store, service)
        schema = bundle["schema"]
        cloud = bundle["runtime"]
        names = set(runtime_files) | set(cloud) | set(schema.get("files") or {})
        _backup_files(backup_dir, sorted(names), data_dir, cfg_path.name)
        # ① 配置：以公共仓库模板为结构基准，私有库真实值为覆盖，安全对账
        #    （新字段留空、未知字段保留、密钥绝不被空值冲掉）
        config_report = {"skipped": "数据后端没有 config.txt"}
        template_text = updater.load_local_template()
        values_text = bundle["config"] or ""
        merged, config_report = updater.reconcile_config(
            template_text, values_text, cfg.get("UPDATER_CONFIG_MERGE", "structure"))
        updater._atomic_write(cfg_path, merged)
        # 只有出现模板里没有的新字段时才把结构回写私有库（把空位同步回去让本人填）
        if (not _off(cfg.get("UPDATER_PUSH_CONFIG", "1"))
                and config_report.get("new_fields")):
            try:
                updater.push_config(store, merged)
            except Exception as e:
                updater.elog("[%s] 配置结构回写私有库失败（服务端本地已更新）"
                             % (service or "default"), str(e))
        # ② 运行数据：按最新结构迁移（改名 / 删字段 / 补默认）
        local, bad = updater.collect_runtime(data_dir, sorted(names))
        migrated, report = updater.migrate_runtime(local, schema, cloud)
        for name in bad:
            migrated.pop(name, None)          # 坏文件原样保留，绝不覆盖
        ok, detail = updater.write_runtime_files(migrated, data_dir, backup=False)
        if not ok:
            raise RuntimeError(detail)
        # ③ 回写数据后端：拿到「最新完整 + 旧数据迁移」的结果。
        #    先认领：两个不同授权的实例误指同一目录时，宁可不回写也不能互相覆盖。
        if not _off(cfg.get("UPDATER_PUSH_RUNTIME", "1")):
            instance = (cfg.get("UPDATER_INSTANCE_ID") or "default").strip()
            try:
                owned, owner = updater.claim_owner(store, instance, service)
                if not owned:
                    updater.elog("[%s] 数据目录已由实例 %s 认领，跳过回写"
                                 % (service or "default", owner),
                                 "本实例=%s；给两台服务端各配 UPDATER_DATA_NAMESPACE" % instance)
                else:
                    updater.push_runtime_failover(store, migrated, schema, service)
            except Exception as e:
                updater.elog("[%s] 运行数据回写失败（服务端本地已迁移完成）"
                             % (service or "default"), str(e))
        st = updater.load_state(service)
        st.update({"last_signal": sig.fingerprint, "signal_name": sig.name,
                   "schema_version": schema.get("version"),
                   "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "failed_signal": ""})
        updater.save_state(st, service)
        updater.flog("[%s] 更新完成：配置 新增=%d 删除=%d 保留=%d；"
                     "运行数据 改名=%d 删除=%d 补默认=%d；未解析=%s"
                     % ((service or "default"),
                        len(config_report.get("added", [])),
                        len(config_report.get("removed", [])),
                        len(config_report.get("kept", [])),
                        len(report["renamed"]), len(report["dropped"]),
                        len(report["added"]), ",".join(bad) or "-"))
        succeeded = True
        return True
    except Exception as e:
        _restore_backup(backup_dir, data_dir)
        updater.elog("[%s] 更新失败，已还原备份并照常用旧数据启动服务"
                     % (service or "default"), str(e))
        return False
    finally:
        if not succeeded:
            # 失败信号必须落盘：否则每 10 秒轮询会把同一次失败更新重跑一遍。
            # 修好后改 runN 的内容或换成 run2/run3 产生新指纹才会再触发。
            try:
                mark_failed(sig, service)
            except Exception as e:
                updater.elog("记录失败信号失败", str(e))
        bot.start()


def sync_config(cfg, store, svc, bot):
    """配置专用同步：拉私有库真实值 → 按公共模板对账 → 写本地 → 重启机器人。

    用于「账号本人在网站 / git 改了私有库配置」后的热更新：不碰代码、不迁移运行数据。
    只有出现模板新字段才回写私有库（把空位同步回去让本人填）。返回是否应用成功。
    """
    cfg_path = Path(svc.config_file) if svc else BOT_CONFIG
    try:
        raw = store.read(updater.CONFIG_NAME)
    except Exception as e:
        updater.elog("[%s] 读私有配置失败" % (svc.name if svc else "default"), str(e))
        return False
    if raw is None:
        return False
    template_text = updater.load_local_template()
    merged, rep = updater.reconcile_config(
        template_text, raw, cfg.get("UPDATER_CONFIG_MERGE", "structure"))
    updater._atomic_write(cfg_path, merged)
    if (not _off(cfg.get("UPDATER_PUSH_CONFIG", "1")) and rep.get("new_fields")):
        try:
            updater.push_config(store, merged)
        except Exception as e:
            updater.elog("[%s] 配置结构回写失败" % (svc.name if svc else "default"), str(e))
    if bot.alive():
        bot.stop()
    bot.start()
    updater.flog("[%s] 私有库配置变更已按模板迁移并热重载"
                 % (svc.name if svc else "default"))
    return True


def main():
    """统一 RUN：装配 → 自检 → 启动先同步 → 拉起所有服务 → 每 10 秒查信号。"""
    cfg = load_cfg()
    if not CFG_FILE.exists():
        _console("[fatal] 缺少根目录 update.cfg（照 update.cfg.example 填数据后端）")
        return 1
    if not acquire_lock():
        return 1
    try:
        services = enabled_services(load_services(), cfg)
        root_store = updater.make_store_chain(cfg)
        service_stores = {s.name: updater.make_store_chain(cfg, s.remote_path)
                          for s in services}
        # 信号读公共仓库本体，数据读私有后端：10 秒轮询只在真有更新时才打私有库。
        root_signals = updater.make_signal_store(cfg, chain=root_store)
        signal_stores = {s.name: updater.make_signal_store(
            cfg, s.remote_path, chain=service_stores[s.name])
            for s in services}
    except Exception as e:
        _console("[fatal] 启动装配失败：%s" % e)
        updater.elog("启动装配失败", str(e))
        release_lock()
        return 1
    try:
        poll = max(3, int(cfg.get("UPDATER_POLL_SECONDS", "10")))
    except ValueError:
        poll = updater.POLL_SECONDS
    _console("统一 RUN 启动：服务=%s 数据后端=%s 轮询=%s 秒"
             % (",".join(s.name for s in services),
                cfg.get("UPDATER_BACKEND"), poll))
    preflight(cfg, root_store, services, service_stores,
              root_signals, signal_stores)
    before_fp = self_fingerprint()
    if not _off(cfg.get("UPDATER_SYNC_ON_START", "1")):
        ok, info = sync_code(cfg)
        _console("[sync] %s：%s" % ("成功" if ok else "失败", info))
        if not ok:
            updater.elog("启动时代码同步失败，照常用本地版本运行", info)
    if self_fingerprint() != before_fp and restart_self([]):
        return 0
    bots = {s.name: BotProcess(s) for s in services}
    for b in bots.values():
        b.start()
    self_fp = self_fingerprint()
    crashes = {s.name: 0 for s in services}
    applied_cfg_hash = {}        # 各服务私有库 config 的上次哈希；变了就热更新
    try:
        while True:
            time.sleep(poll)
            for name, b in bots.items():
                if b.alive():
                    crashes[name] = 0
                    continue
                crashes[name] += 1
                if crashes[name] > MAX_CRASH_RESTART:
                    # 规范 1.1：静默重试耗尽就停下并如实提示，不无限自动拉起
                    continue
                updater.flog("[%s] 服务已退出，第 %d 次自动拉起" % (name, crashes[name]))
                b.start()
            # 全局信号：整仓库只 pull 一次，所有服务一起迁移
            try:
                gsig = root_signals.signal()
            except Exception as e:
                updater.elog("检查全局信号失败，本轮跳过", str(e))
                gsig = None
            gst = updater.load_state("__global__")
            global_hit = (gsig is not None and gsig.need_update
                          and gsig.fingerprint != gst.get("last_signal")
                          and gsig.fingerprint != gst.get("failed_signal"))
            plan = []
            if global_hit:
                ok, info = git_pull(cfg)
                if not ok:
                    updater.elog("全局更新 git pull 失败，本轮作废", info)
                    gst["failed_signal"] = gsig.fingerprint
                    updater.save_state(gst, "__global__")
                else:
                    updater.flog("[global] git pull: %s" % (info or "无变更"))
                    plan = [(s, service_stores[s.name], bots[s.name], gsig, False)
                            for s in services]
                    gst["last_signal"] = gsig.fingerprint
                    updater.save_state(gst, "__global__")
            # 各服务独立信号：只更新被点名的那个，不惊动其他服务
            planned = {s.name for s, _, _, _, _ in plan}
            for s in services:
                if s.name in planned:
                    continue
                try:
                    sig = signal_stores[s.name].signal()
                except Exception as e:
                    updater.elog("[%s] 检查信号失败，本轮跳过" % s.name, str(e))
                    continue
                st = updater.load_state(s.name)
                if (not sig.need_update
                        or sig.fingerprint == st.get("last_signal")
                        or sig.fingerprint == st.get("failed_signal")):
                    continue
                plan.append((s, service_stores[s.name], bots[s.name], sig, True))
            for s, store, bot, sig, pull in plan:
                do_update(cfg, store, bot, sig, svc=s, pull=pull)
            # 私有库配置热更新（账号本人在网站 / git 改了 config）：只处理本轮没被
            # 代码更新碰过的服务，避免重复重启；检测哈希变化即按模板迁移 + 热重载。
            if not _off(cfg.get("UPDATER_SYNC_PRIVATE_CONFIG", "1")):
                for s in services:
                    if s.name in planned:
                        continue
                    st_store = service_stores[s.name]
                    try:
                        raw = st_store.read(updater.CONFIG_NAME)
                    except Exception as e:
                        updater.elog("[%s] 读私有配置失败" % s.name, str(e))
                        continue
                    if raw is None:
                        continue
                    h = hashlib.md5(raw.encode("utf-8")).hexdigest()
                    prev = applied_cfg_hash.get(s.name)
                    if prev is None:
                        # 首次观测：只记哈希，不触发重启，避免把「初始状态」当成变更
                        applied_cfg_hash[s.name] = h
                    elif h != prev:
                        applied_cfg_hash[s.name] = h
                        updater.flog("[%s] 检测到私有库配置变更，按模板迁移并重载" % s.name)
                        if sync_config(cfg, st_store, s, bots[s.name]):
                            # 重新读私有库，把「服务端刚回写的结构」也记下来，避免自触发
                            try:
                                raw2 = st_store.read(updater.CONFIG_NAME)
                                if raw2 is not None:
                                    applied_cfg_hash[s.name] = hashlib.md5(
                                        raw2.encode("utf-8")).hexdigest()
                            except Exception:
                                pass
            if self_fingerprint() != self_fp:
                # 代码已 pull 到磁盘，进程必须换成新版；无论重启成败都刷新指纹，
                # 避免失败时每 10 秒反复尝试重启。
                self_fp = self_fingerprint()
                restart_self(bots)
    except KeyboardInterrupt:
        updater.flog("收到 Ctrl+C，退出统一 RUN")
    finally:
        for b in bots.values():
            b.stop()
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
