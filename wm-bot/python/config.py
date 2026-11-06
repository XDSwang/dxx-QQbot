#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — 读取 data/config.txt，把配置交给各模块（纯标准库、零依赖）

同目录的 config.txt 是【纯净模板】：不含任何真实凭证，会随项目上传 GitHub。
真正生效的是 data/config.txt —— 把模板复制到 data/ 下再填写 appid / secret；
data/ 已由项目根目录的 .gitignore 排除，密钥与运行数据不会被提交。

为什么有这个文件：桌面 / 手机 Termux / QPython 都只用 Python。
改配置只需用记事本编辑 data/config.txt 这一个带注释的文本文件；
运行只需 python qq_bot.py，不需要 set / export / 任何 .bat / .sh 启动脚本。

规则：
  - 格式：每行「名称 = 值」；「#」之后是注释；空值跳过；值里不要带「#」；
  - 编码：UTF-8（兼容 Windows 记事本保存的 BOM）；
  - 优先级：已存在的环境变量 > data/config.txt > 程序内置默认值。
    apply() 用 setdefault，绝不覆盖已有 env（方便临时覆盖与离线测试）；
  - 路径：基于 __file__ 同目录解析，不依赖当前工作目录、无盘符、无上层路径；
  - 开关：flag() 把「0 / 空 / false / no / off」视为关，「1 / true / yes / on」视为开。
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "data" / "config.txt"   # 实际生效（含密钥，不上传）
TEMPLATE_FILE = HERE / "config.txt"           # 纯净模板（可上传）

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"", "0", "false", "no", "off", "n", "f", "none", "null"}


def read(path=None):
    """解析配置文件，返回 {大写名称: 字符串值}；文件不存在返回空字典。"""
    p = Path(path) if path else CONFIG_FILE
    if not p.is_absolute():
        p = HERE / p
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8-sig")
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()   # 去掉行内注释
        if not line or "=" not in line:
            continue
        key, val = line.split("=", 1)        # 值里允许再出现 =
        key = key.strip().upper()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1].strip()          # 容忍用引号包住值
        if key and val:                     # 留空 = 用默认值，不注入
            out[key] = val
    return out


def apply(path=None):
    """把 data/config.txt 的值补进 os.environ（不覆盖已有环境变量），返回本次读入的配置。"""
    cfg = read(path)
    if path is None and not CONFIG_FILE.exists() and TEMPLATE_FILE.exists():
        # clone 下来还没配置时给一句明确引导，避免「改了模板却不生效」的困惑
        print("[config] 还没有 data/config.txt：请把同目录 config.txt 模板复制到 "
              "data/config.txt 并填写凭证后重跑。", file=sys.stderr)
    for k, v in cfg.items():
        os.environ.setdefault(k, v)
    return cfg


def get(name, default=None):
    """读单个配置项（env > data/config.txt > default）；apply 尚未跑过时自动补跑。"""
    if name not in os.environ:
        apply()
    return os.environ.get(name, default)


def flag(name, default=False):
    """读开 / 关型配置：0 与空值 = 关，1 = 开。"""
    v = os.environ.get(name)
    if v is None:
        if name in read():
            v = read()[name]
        else:
            return default
    v = v.strip().lower()
    if v in _FALSE:
        return False
    if v in _TRUE:
        return True
    return default
