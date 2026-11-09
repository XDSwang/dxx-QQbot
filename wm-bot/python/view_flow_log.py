#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
view_flow_log.py — 独立查流程日志（零依赖，不 import 项目任何模块）

在 wm-bot/python/ 下直接跑，手机 Termux / 桌面都一样：
    python view_flow_log.py                        打印当天 bot_flow.log
    python view_flow_log.py 2026-11-08            打印指定日期的归档
    python view_flow_log.py --list                列出有记录的日期
    python view_flow_log.py 2026-11-08 --tail 30 只看那天末尾 30 行

当天记录就是 data/bot_flow.log，往前的日子在 data/bot_flow-YYYY-MM-DD.log
归档里（命名与 qq_official.py 的按天归档一致，这里写死以保持脚本独立）。
路径全部基于本文件所在目录解析，整个文件夹拷到任意位置都能用。
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


def parse_day(value):
    """严格解析 YYYY-MM-DD；非法输入抛 ValueError 并带上原文。"""
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("日期要写成 YYYY-MM-DD，收到：%s" % value)
    try:
        return datetime.date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise ValueError("日期要写成 YYYY-MM-DD，收到：%s" % value)


def log_path_for(day, today):
    """当天读固定文件，其它日期读按天归档；返回 (路径, 是否归档)。"""
    if day == today:
        return os.path.join(LOG_DIR, CURRENT_NAME), False
    return os.path.join(LOG_DIR, "%s%s%s" % (
        ARCHIVE_PREFIX, day.isoformat(), ARCHIVE_SUFFIX)), True


def archived_days():
    """列出有归档文件的日期（升序）；不认非法文件名。"""
    if not os.path.isdir(LOG_DIR):
        return []
    out = []
    for name in os.listdir(LOG_DIR):
        if not (name.startswith(ARCHIVE_PREFIX) and name.endswith(ARCHIVE_SUFFIX)):
            continue
        raw = name[len(ARCHIVE_PREFIX):-len(ARCHIVE_SUFFIX)]
        try:
            out.append(parse_day(raw).isoformat())
        except ValueError:
            continue
    return sorted(out)


def available_days(today):
    """当天有 bot_flow.log 也算一天，与归档合并去重。"""
    days = set(archived_days())
    if os.path.isfile(os.path.join(LOG_DIR, CURRENT_NAME)):
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
    args = ap.parse_args(argv)
    today = today or datetime.date.today()

    if args.list:
        days = available_days(today)
        if not days:
            print("还没有任何流程日志记录（目录：%s）" % LOG_DIR)
            return 1
        for d in days:
            print("%s%s" % (d, "  <- 当天" if d == today.isoformat() else ""))
        return 0

    try:
        day = parse_day(args.day) if args.day else today
    except ValueError as e:
        print("日期错误：%s" % e, file=sys.stderr)
        return 2
    path, _ = log_path_for(day, today)
    if not os.path.isfile(path):
        print("%s 没有记录（找的是 %s）" % (day.isoformat(), os.path.basename(path)))
        days = available_days(today)
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
