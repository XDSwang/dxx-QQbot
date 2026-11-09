#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
view_flow_log.py — 独立查流程日志（零依赖，不 import 项目任何模块）

在 wm-bot/python/ 下直接跑，手机 Termux / 桌面都一样：
    python view_flow_log.py                        打印当天 bot_flow.log
    python view_flow_log.py 2026-11-08            打印指定日期的归档
    python view_flow_log.py --list                列出有记录的日期
    python view_flow_log.py 2026-11-08 --tail 30 只看那天末尾 30 行
    python view_flow_log.py --log updater         查更新器流程日志
    python view_flow_log.py --log trace           查自检 / 链路日志

三份流程日志都按天归档（命名与写入端一致，这里写死以保持脚本独立）：
    bot      data/bot_flow.log             → bot_flow-YYYY-MM-DD.log
    trace    data/selfcheck_trace.log      → selfcheck_trace-YYYY-MM-DD.log
    updater  ../../data/updater_flow.log  → updater_flow-YYYY-MM-DD.log
当天记录读固定文件，往前的日子读归档；路径全部基于本文件所在目录解析，
整个文件夹拷到任意位置都能用。
"""
import argparse
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "data")
CURRENT_NAME = "bot_flow.log"
ARCHIVE_PREFIX = "bot_flow-"
ARCHIVE_SUFFIX = ".log"


def log_spec(kind):
    """返回 (目录, 当天文件名, 归档前缀)；bot 用模块全局，离线测试可 patch。"""
    if kind == "bot":
        return LOG_DIR, CURRENT_NAME, ARCHIVE_PREFIX
    if kind == "trace":
        return os.path.join(HERE, "data"), "selfcheck_trace.log", "selfcheck_trace-"
    if kind == "updater":
        return os.path.join(HERE, "..", "..", "data"), "updater_flow.log", "updater_flow-"
    raise ValueError("未知日志类型：%s" % kind)


def parse_day(value):
    """严格解析 YYYY-MM-DD；非法输入抛 ValueError 并带上原文。"""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("日期要写成 YYYY-MM-DD，收到：%s" % value)
    try:
        return datetime.date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise ValueError("日期要写成 YYYY-MM-DD，收到：%s" % value)


def log_path_for(day, today, spec=None):
    """当天读固定文件，其它日期读按天归档；返回 (路径, 是否归档)。"""
    directory, current, prefix = spec or log_spec("bot")
    if day == today:
        return os.path.join(directory, current), False
    return os.path.join(directory, "%s%s%s" % (
        prefix, day.isoformat(), ARCHIVE_SUFFIX)), True


def archived_days(spec=None):
    """列出有归档文件的日期（升序）；不认非法文件名。"""
    directory, _, prefix = spec or log_spec("bot")
    if not os.path.isdir(directory):
        return []
    out = []
    for name in os.listdir(directory):
        if not (name.startswith(prefix) and name.endswith(ARCHIVE_SUFFIX)):
            continue
        raw = name[len(prefix):-len(ARCHIVE_SUFFIX)]
        try:
            out.append(parse_day(raw).isoformat())
        except ValueError:
            continue
    return sorted(out)


def available_days(today, spec=None):
    """当天固定文件也算一天，与归档合并去重。"""
    spec = spec or log_spec("bot")
    directory, current, _ = spec
    days = set(archived_days(spec))
    if os.path.isfile(os.path.join(directory, current)):
        days.add(today.isoformat())
    return sorted(days)


def read_lines(path, tail=None):
    """原样读出行（去掉行尾换行）；tail>0 时只留末尾 N 行。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    if tail and tail > 0:
        return lines[-tail:]
    return lines


def main(argv=None, today=None):
    """命令行入口：today 可注入便于离线测试；返回退出码（0 正常 / 1 无记录或读失败 / 2 参数错误）。"""
    ap = argparse.ArgumentParser(
        description="打印机器人流程日志：默认当天，可指定日期 / 列日期 / 看末尾 N 行")
    ap.add_argument("day", nargs="?", help="日期 YYYY-MM-DD；不给就是当天")
    ap.add_argument("--list", action="store_true", help="列出有记录的日期")
    ap.add_argument("--tail", type=int, default=0, metavar="N",
                    help="只打印末尾 N 行")
    ap.add_argument("--log", default="bot", choices=("bot", "trace", "updater"),
                    help="日志类型：bot 收发（默认）/ trace 自检链路 / updater 更新器")
    args = ap.parse_args(argv)
    today = today or datetime.date.today()
    spec = log_spec(args.log)

    if args.list:
        days = available_days(today, spec)
        if not days:
            print("还没有任何流程日志记录（类型：%s，目录：%s）"
                  % (args.log, spec[0]))
            return 1
        for d in days:
            print("%s%s" % (d, "  <- 当天" if d == today.isoformat() else ""))
        return 0

    try:
        day = parse_day(args.day) if args.day else today
    except ValueError as e:
        print("日期错误：%s" % e, file=sys.stderr)
        return 2
    path, _ = log_path_for(day, today, spec)
    if not os.path.isfile(path):
        print("%s 没有记录（找的是 %s）" % (day.isoformat(), os.path.basename(path)))
        days = available_days(today, spec)
        if days:
            print("有记录的日期：" + ", ".join(days))
        return 1
    try:
        lines = read_lines(path, args.tail)
    except OSError as e:
        print("读取失败：%s | %s" % (path, e), file=sys.stderr)
        return 1
    print("===== %s | %s | %d 行%s =====" % (
        day.isoformat(), os.path.basename(path), len(lines),
        ("（末尾 %d 行）" % args.tail) if args.tail and args.tail > 0 else ""))
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
