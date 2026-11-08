#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run.py — 项目根唯一运行入口：守护 qq_bot.py + 每 10 秒检查私有地址自动更新。

用法（服务端只跑这一条）：
    python run.py

配置在同目录 update.cfg（不进 git，含私有地址 token）；照着 update.cfg.example 填。

一次更新的顺序，任何一步失败都不会把半套状态留给机器人：
    停机器人 → git pull --ff-only → 拉私有地址 → 备份 → 合并配置
    → 迁移运行数据 → 回写云端 → 记录指纹 → 启机器人

信号用「文件名 + 指纹」判定：run1（或 run2、run3…）表示要更新，run0 表示不用。
指纹没变就不重复更新，所以云端不复位 run0 也不会每 10 秒重跑一次。
"""
import atexit
import os
import subprocess
import sys
import time
from pathlib import Path

import updater

HERE = Path(__file__).resolve().parent
CFG_FILE = HERE / "update.cfg"
BOT_DATA_DIR = HERE / "python" / "data"
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
    "UPDATER_GITHUB_PATH": "",
    "UPDATER_GITHUB_BRANCH": "main",
    "UPDATER_GITHUB_API": updater.GITHUB_API,
    "UPDATER_POLL_SECONDS": "10",
    "UPDATER_GIT_PULL": "1",
    "UPDATER_CONFIG_MERGE": "structure",
    "UPDATER_PUSH_RUNTIME": "1",
    "UPDATER_BOT_ENTRY": "python/qq_bot.py",
}

STOP_WAIT_SECONDS = 3       # 规范 1.2 单次等待缺省 3 秒
GIT_TIMEOUT_SECONDS = 60
MAX_CRASH_RESTART = 3       # 规范 1.1 静默重试最多 3 次
SELF_FILES = (HERE / "run.py", HERE / "updater.py")   # 改到更新器自身就要重启才生效


def load_cfg(path=None):
    """读 update.cfg；注释行与「名称 = 值」，缺的项用缺省补齐。"""
    cfg = dict(DEFAULTS)
    p = Path(path) if path else CFG_FILE
    if not p.exists():
        updater.elog("缺少 update.cfg", "照 update.cfg.example 填好私有地址后再启动")
        return cfg
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


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
            updater.elog("已有更新服务在运行", "pid=%s；不要开第二个 run.py" % raw)
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
    """机器人子进程：正常退出 / 崩溃由主循环接管，stdout+stderr 直接继承终端。"""

    def __init__(self, entry):
        self.entry = entry
        self.p = None

    def start(self):
        cmd = [sys.executable, str(HERE / self.entry)]
        updater.flog("启动机器人 %s" % " ".join(cmd))
        self.p = subprocess.Popen(cmd, cwd=str(HERE))
        return self.p

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        if not self.alive():
            return True
        updater.flog("停止机器人（等待 %s 秒）" % STOP_WAIT_SECONDS)
        self.p.terminate()
        try:
            self.p.wait(timeout=STOP_WAIT_SECONDS)
            return True
        except subprocess.TimeoutExpired:
            # 无人值守场景：超时不静默跳过，明确记录后按「不再等待、继续流程」处理，
            # 强制结束是为了让迁移拿到独占的 data/，避免内存里的旧结构写回来。
            updater.elog("机器人 %s 秒未退出，强制结束", "pid=%s 继续更新流程" % self.p.pid)
            self.p.kill()
            try:
                self.p.wait(timeout=STOP_WAIT_SECONDS)
            except Exception:
                pass
            return True


def _console(msg):
    """控制台提示 + 流程日志：配置没填、私有地址连不上这类问题必须当场看得见。"""
    print("[run] " + msg, flush=True)
    updater.flog(msg)


def mark_failed(sig):
    """失败信号落盘：同一个指纹不再每 10 秒重试（改 runN 内容或换版本号才重试）。"""
    st = updater.load_state()
    st["failed_signal"] = sig.fingerprint
    st["failed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    updater.save_state(st)


def preflight(cfg, store):
    """启动自检：把「配置没填 / 不在 git 仓库 / 私有地址没放文件」直接打到控制台。

    只提示不退出：网络抖动时机器人照常运行，只是暂时不会自动更新。
    返回问题列表，便于离线测试断言。
    """
    problems = []
    backend = (cfg.get("UPDATER_BACKEND") or "local").strip().lower()
    if backend == "github":
        repo = (cfg.get("UPDATER_GITHUB_REPO") or "").strip()
        token = (cfg.get("UPDATER_GITHUB_TOKEN") or "").strip()
        if not repo or "你的账号" in repo or not token or "github_pat_xxx" in token:
            problems.append("update.cfg 的 UPDATER_GITHUB_REPO / TOKEN 还是模板占位值")
    if str(cfg.get("UPDATER_GIT_PULL", "1")).lower() not in ("0", "false", "no", "off"):
        try:
            r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(HERE),
                               capture_output=True, text=True,
                               timeout=GIT_TIMEOUT_SECONDS)
            if r.returncode != 0:
                problems.append("当前目录不在 git 仓库里：服务端要 clone 整个 dxx-QQbot，"
                                "只拷 wm-bot/ 会让 git pull 一直失败")
        except Exception as e:
            problems.append("检查 git 仓库失败：%s" % e)
    try:
        sig = store.signal()
        if sig.name == "(missing)":
            path = (cfg.get("UPDATER_GITHUB_PATH") or "").strip() or "仓库根目录"
            problems.append("私有地址 %s 里没有 runN 信号文件（先放一个 run0）" % path)
        for name in (updater.CONFIG_NAME, updater.SCHEMA_NAME):
            if store.read(name) is None:
                problems.append("私有地址缺少 %s（配置同步 / 字段改名规则不会生效）" % name)
    except Exception as e:
        problems.append("私有地址读取失败：%s" % e)
    for p in problems:
        _console("[warn] " + p)
    if not problems:
        _console("启动自检通过：私有地址与 git 仓库都可用")
    return problems


def self_fingerprint():
    """更新器自身两个文件的 mtime+size；git pull 改到它们就该重启进程。"""
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


def restart_self(bot):
    """git pull 改到 run.py / updater.py 时重启本进程，让新逻辑真正生效。

    execv 会替换进程映像，try/finally 不会再跑，所以先停机器人、放掉单实例锁；
    失败则补回来，继续用旧进程服务，并留下异常日志。
    """
    if not self_syntax_ok():
        return False
    _console("更新器自身代码已更新，重启 run.py 使新逻辑生效")
    bot.stop()
    release_lock()
    try:
        os.execv(sys.executable,
                 [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])
    except Exception as e:
        updater.elog("重启更新器失败，继续用当前进程运行", str(e))
        acquire_lock()
        bot.start()
        return False
    return True


def git_pull(cfg):
    """代码与配置必须同一版：pull 失败就整次作废，不迁数据、不更新指纹。"""
    if str(cfg.get("UPDATER_GIT_PULL", "1")).lower() in ("0", "false", "no", "off"):
        return True, "已关闭"
    try:
        r = subprocess.run(["git", "pull", "--ff-only"], cwd=str(HERE),
                           capture_output=True, text=True,
                           timeout=GIT_TIMEOUT_SECONDS)
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()[:300]
    return True, (r.stdout or "").strip()[:200]


def _backup_files(backup_dir, names):
    """写任何东西之前，把配置 + 所有将被覆盖的运行数据整份备份。"""
    bp = Path(backup_dir)
    bp.mkdir(parents=True, exist_ok=True)
    for name in list(names) + ["config.txt"]:
        src = BOT_DATA_DIR / name
        if src.exists():
            (bp / name).write_bytes(src.read_bytes())


def _restore_backup(backup_dir):
    """更新中途失败：把已改过的运行数据 / 配置从备份还原。"""
    if not backup_dir:
        return
    p = Path(backup_dir)
    if not p.exists():
        return
    for src in p.iterdir():
        if src.is_file():
            (BOT_DATA_DIR / src.name).write_bytes(src.read_bytes())


def do_update(cfg, store, bot, sig):
    """执行一轮更新；无论成败都在 finally 里把机器人拉起来，不让服务停摆。"""
    updater.flog("检测到更新信号 %s 指纹=%s" % (sig.name, sig.fingerprint))
    backup_dir = None
    succeeded = False       # 不叫 ok：do_update 里 ok 是 git_pull / 写文件的返回值
    try:
        if not bot.stop():
            return False
        ok, info = git_pull(cfg)
        if not ok:
            updater.elog("git pull 失败，本次更新作废", info)
            return False
        updater.flog("git pull: %s" % (info or "无变更"))

        backup_dir = str(updater.BACKUP_ROOT /
                         (time.strftime("%Y%m%d-%H%M%S") + "-%d" % os.getpid()))
        bundle = updater.fetch_bundle(store)
        schema = bundle["schema"]
        cloud = bundle["runtime"]
        names = set(DEFAULT_RUNTIME_FILES) | set(cloud) | set(schema.get("files") or {})
        _backup_files(backup_dir, sorted(names))
        # ① 配置：结构同步（增删字段）+ 服务端本地值保留
        config_report = {"skipped": "云端没有 config.txt"}
        if bundle["config"]:
            local_cfg = BOT_CONFIG.read_text(encoding="utf-8") if BOT_CONFIG.exists() else ""
            merged, config_report = updater.merge_config(
                local_cfg, bundle["config"], cfg.get("UPDATER_CONFIG_MERGE", "structure"))
            updater._atomic_write(BOT_CONFIG, merged)
        # ② 运行数据：按最新结构迁移（改名 / 删字段 / 补默认）
        local, bad = updater.collect_runtime(BOT_DATA_DIR, sorted(names))
        migrated, report = updater.migrate_runtime(local, schema, cloud)
        for name in bad:
            migrated.pop(name, None)          # 坏文件原样保留，绝不覆盖
        ok, detail = updater.write_runtime_files(migrated, BOT_DATA_DIR,
                                                 backup=False)  # 上面已统一备份
        if not ok:
            raise RuntimeError(detail)
        # ③ 回写云端：让私有地址拿到「最新完整 + 旧数据迁移」的结果
        if str(cfg.get("UPDATER_PUSH_RUNTIME", "1")).lower() not in ("0", "false", "no", "off"):
            try:
                updater.push_runtime(store, migrated)
            except Exception as e:
                updater.elog("运行数据回写私有地址失败（服务端本地已迁移完成）", str(e))
        st = updater.load_state()
        st.update({"last_signal": sig.fingerprint, "signal_name": sig.name,
                   "schema_version": schema.get("version"),
                   "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "failed_signal": ""})
        updater.save_state(st)
        updater.flog("更新完成：配置 新增=%d 删除=%d 保留=%d；运行数据 改名=%d 删除=%d 补默认=%d；未解析=%s"
                     % (len(config_report.get("added", [])),
                        len(config_report.get("removed", [])),
                        len(config_report.get("kept", [])),
                        len(report["renamed"]), len(report["dropped"]),
                        len(report["added"]), ",".join(bad) or "-"))
        succeeded = True
        return True
    except Exception as e:
        _restore_backup(backup_dir)
        updater.elog("更新失败，已还原备份并照常用旧数据启动机器人", str(e))
        return False
    finally:
        if not succeeded:
            # 失败信号必须落盘：否则每 10 秒轮询会把同一次失败更新重跑一遍。
            # 修好后改 runN 的内容或换成 run2/run3 产生新指纹才会再触发。
            try:
                mark_failed(sig)
            except Exception as e:
                updater.elog("记录失败信号失败", str(e))
        bot.start()


def main():
    cfg = load_cfg()
    if not acquire_lock():
        return 1
    try:
        store = updater.make_store(cfg)
    except Exception as e:
        updater.elog("私有地址配置有误", str(e))
        return 1
    try:
        poll = max(3, int(cfg.get("UPDATER_POLL_SECONDS", "10")))
    except ValueError:
        poll = updater.POLL_SECONDS
    bot = BotProcess(cfg.get("UPDATER_BOT_ENTRY") or DEFAULTS["UPDATER_BOT_ENTRY"])
    bot.start()
    _console("更新服务启动：后端=%s 轮询=%s 秒 入口=%s"
             % (cfg.get("UPDATER_BACKEND"), poll, bot.entry))
    preflight(cfg, store)
    self_fp = self_fingerprint()
    crashes = 0
    try:
        while True:
            time.sleep(poll)
            if not bot.alive():
                crashes += 1
                if crashes > MAX_CRASH_RESTART:
                    # 规范 1.1：静默重试耗尽就停下并如实提示，不无限自动拉起
                    updater.elog("机器人连续崩溃超过 %d 次，暂停自动拉起"
                                 % MAX_CRASH_RESTART, "请查看机器人输出；修复后重启 run.py")
                    continue
                updater.flog("机器人已退出，第 %d 次自动拉起" % crashes)
                bot.start()
                continue
            crashes = 0
            try:
                sig = store.signal()
            except Exception as e:
                updater.elog("检查私有地址失败，本轮跳过", str(e))
                continue
            st = updater.load_state()
            if (not sig.need_update or sig.fingerprint == st.get("last_signal")
                    or sig.fingerprint == st.get("failed_signal")):
                continue
            do_update(cfg, store, bot, sig)
            if self_fingerprint() != self_fp:
                # 代码已 pull 到磁盘，进程必须换成新版，否则后续轮询仍跑旧逻辑。
                # 无论重启成败都刷新指纹，避免失败时每 10 秒反复尝试重启。
                self_fp = self_fingerprint()
                restart_self(bot)
    except KeyboardInterrupt:
        updater.flog("收到 Ctrl+C，退出更新服务")
    finally:
        bot.stop()
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
