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
  - 顺带统一 HTTPS 出口：手机（Termux / QPython）常缺 CA 根证书，导致所有
    HTTPS 报 CERTIFICATE_VERIFY_FAILED。urlopen() / ssl_context() 会先自动找
    CA bundle，实在找不到才降级并给出醒目警告；Windows / macOS 一律不动。
"""
import os
import ssl
import sys
import urllib.request
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


# ---------------- 移动端 HTTPS / WSS 证书兼容 ----------------
# 手机上的 Python 常缺 CA 根证书 bundle，报：
#   <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
#    unable to get local issuer certificate (_ssl.c:1010)>
# 这不是网络问题、也不是 QQ 接口问题：取 token / 发消息 / 查 WM / 抓世界状态 /
# 连 wss 网关会【全部】失败，然后重连循环里刷屏。
# 对策：先自动找到 CA 并照常校验；实在找不到才降级，且打印明确警告。
_SSL_CHECKED = False
_SSL_CA = None
_SSL_UNVERIFIED = False


def _usable_ca(path):
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 100
    except OSError:
        return False


def _ca_candidates():
    """Termux / Android / 常见 Linux 的 CA bundle 候选（按优先级）。"""
    prefix = os.environ.get("PREFIX", "")
    out = [os.environ.get("WM_SSL_CAFILE"), os.environ.get("SSL_CERT_FILE")]
    if prefix:
        out += [os.path.join(prefix, "etc", "tls", "cert.pem"),
                os.path.join(prefix, "etc", "tls", "certs", "cacert.pem")]
    out += [
        "/data/data/com.termux/files/usr/etc/tls/cert.pem",
        "/data/data/com.termux/files/usr/etc/tls/certs/cacert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/cert.pem",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/usr/local/etc/openssl/cert.pem",
    ]
    try:                     # 可选：手机上装过 certifi 就直接用它的 bundle
        import certifi
        out.append(certifi.where())
    except Exception:
        pass
    return out


def _android_ca_dir():
    """Android 系统 CA 是散装 <hash>.0 文件，正好是 OpenSSL capath 格式。"""
    for d in ("/system/etc/security/cacerts",
              "/apex/com.android.conscrypt/cacerts"):
        try:
            if os.path.isdir(d) and any(f.endswith(".0") for f in os.listdir(d)):
                return d
        except OSError:
            pass
    return None


def reset_ssl_state():
    """仅供离线测试：清掉缓存的探测结果，不改磁盘配置。"""
    global _SSL_CHECKED, _SSL_CA, _SSL_UNVERIFIED
    _SSL_CHECKED = False
    _SSL_CA = None
    _SSL_UNVERIFIED = False


def ensure_ssl_ca(force=False):
    """探测并设置可用 CA；幂等，返回实际用到的路径（没改返回 None）。

    - Windows / macOS 用系统证书库，一律不动；
    - 系统默认 CA 已存在，一律不动（桌面零副作用）；
    - 只在「类 Unix 且默认 CA 缺失」时，把 Termux / Android / Linux 的 CA
      写进 SSL_CERT_FILE（或 SSL_CERT_DIR），**依然做证书校验**，这是最安全的一条路。
    """
    global _SSL_CHECKED, _SSL_CA
    if _SSL_CHECKED and not force:
        return _SSL_CA
    _SSL_CHECKED = True
    _SSL_CA = None
    if os.name == "nt" or sys.platform == "darwin":
        return None
    manual = os.environ.get("WM_SSL_CAFILE")
    if manual and _usable_ca(manual):
        os.environ["SSL_CERT_FILE"] = manual
        _SSL_CA = manual
        return manual
    try:
        d = ssl.get_default_verify_paths()
        if _usable_ca(d.cafile):
            return None
        if d.capath and os.path.isdir(d.capath):
            try:
                if os.listdir(d.capath):
                    return None
            except OSError:
                pass
    except Exception:
        pass
    ca = next((p for p in _ca_candidates() if _usable_ca(p)), None)
    if ca:
        os.environ["SSL_CERT_FILE"] = ca
        _SSL_CA = ca
        return ca
    dpath = _android_ca_dir()
    if dpath:
        os.environ.setdefault("SSL_CERT_DIR", dpath)
        _SSL_CA = dpath
        return dpath
    return None


def _noverify_requested():
    return os.environ.get("WM_SSL_NOVERIFY", "").strip().lower() in (
        "1", "true", "yes", "on")


def ssl_context():
    """给 wss 长连接用：和 urlopen 走同一套 CA / 降级策略。"""
    ensure_ssl_ca()
    if _SSL_UNVERIFIED or _noverify_requested():
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def is_ca_error(exc):
    """只认「本机缺 CA / 证书链不完整」，不吞掉超时、DNS、HTTP 等其它错误。"""
    s = str(exc).lower()
    return ("certificate_verify_failed" in s
            or "unable to get local issuer certificate" in s
            or "certificate verify failed" in s)


def urlopen(req, timeout=10):
    """项目内所有 HTTPS 的统一出口；请求签名与标准库 urlopen 一致。

    正常按系统证书校验；只有真撞到 CA 缺失错误时，才降级成不校验并重试一次，
    之后本进程一直走降级 context，避免每个请求都先白失败一次。
    降级会打印醒目警告 —— 它跳过证书校验，仅作为装不上 CA 的手机兜底。
    """
    global _SSL_UNVERIFIED
    ensure_ssl_ca()
    if _SSL_UNVERIFIED or _noverify_requested():
        return urllib.request.urlopen(
            req, timeout=timeout, context=ssl._create_unverified_context())
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except Exception as e:
        if not is_ca_error(e):
            raise
        _SSL_UNVERIFIED = True
        print("[ssl] ⚠️ 本机找不到 CA 根证书，HTTPS 证书校验已临时关闭。\n"
              "       建议修复（更安全）：Termux 执行 pkg install ca-certificates\n"
              "       或在 data/config.txt 写 WM_SSL_CAFILE = /路径/cert.pem\n"
              "       （关闭校验有中间人风险，仅作无法安装证书时的兜底）",
              file=sys.stderr)
        return urllib.request.urlopen(
            req, timeout=timeout, context=ssl._create_unverified_context())
