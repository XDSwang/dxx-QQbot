#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机端 CA 根证书兼容离线测试（不联网、不改真实 data/）

复现的故障：
  <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
   unable to get local issuer certificate (_ssl.c:1010)>
覆盖：CA 探测（手动 / Termux / Android）、Windows 跳过、证书错误只降级一次、
非证书错误不吞、WM_SSL_NOVERIFY、ssl_context、请求签名兼容、取 token 端到端恢复。
"""
import io
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["WM_SOURCE"] = "mock"

import config

CA_ERR = ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
          "unable to get local issuer certificate (_ssl.c:1010)")
_SAVED = {k: os.environ.get(k) for k in
          ("SSL_CERT_FILE", "SSL_CERT_DIR", "WM_SSL_CAFILE",
           "WM_SSL_NOVERIFY", "PREFIX")}
ok = total = 0


def t(name, cond):
    global ok, total
    total += 1
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if cond:
        ok += 1


def clean_env():
    for k in ("SSL_CERT_FILE", "SSL_CERT_DIR", "WM_SSL_CAFILE",
              "WM_SSL_NOVERIFY", "PREFIX"):
        os.environ.pop(k, None)
    config.reset_ssl_state()


def restore_env():
    clean_env()
    for k, v in _SAVED.items():
        if v is not None:
            os.environ[k] = v
    config.reset_ssl_state()


class _Paths:
    cafile = "/definitely/not/here/cert.pem"
    capath = "/definitely/not/here/certs"


def linux():
    """强制走「类 Unix 且默认 CA 缺失」分支（测试机是 Windows 也能测手机逻辑）。"""
    return (patch.object(config.os, "name", "posix"),
            patch.object(config.sys, "platform", "linux"),
            patch.object(config.ssl, "get_default_verify_paths", lambda: _Paths()))


def main():
    from contextlib import ExitStack

    # ---------- 1) 错误判定：只认证书链缺失，不吞其它错误 ----------
    print("[证书错误判定]")
    t("识别 CERTIFICATE_VERIFY_FAILED", config.is_ca_error(CA_ERR))
    t("识别 unable to get local issuer",
      config.is_ca_error("unable to get local issuer certificate"))
    t("urllib.error.URLError 包装后仍能识别",
      config.is_ca_error(urllib.error.URLError(CA_ERR)))
    for s in ("timed out", "Name or service not known", "HTTP 401",
              "Connection refused", ""):
        t("不吞其它错误 %-28s" % s, not config.is_ca_error(s))

    # ---------- 2) CA 探测 ----------
    print("[CA 根证书探测]")
    clean_env()
    d = tempfile.mkdtemp(prefix="ssl_ca_")
    ca = Path(d) / "cacert.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n" + "X" * 400, encoding="utf-8")
    os.environ["WM_SSL_CAFILE"] = str(ca)
    with ExitStack() as st:
        for p in linux():
            st.enter_context(p)
        got = config.ensure_ssl_ca(force=True)
        t("手动 WM_SSL_CAFILE 生效",
          got == str(ca) and os.environ.get("SSL_CERT_FILE") == str(ca))
    clean_env()

    prefix = Path(tempfile.mkdtemp(prefix="ssl_termux_"))
    tls = prefix / "etc" / "tls"
    tls.mkdir(parents=True)
    (tls / "cert.pem").write_text("X" * 400, encoding="utf-8")
    os.environ["PREFIX"] = str(prefix)
    with ExitStack() as st:
        for p in linux():
            st.enter_context(p)
        got = config.ensure_ssl_ca(force=True)
        t("Termux $PREFIX/etc/tls/cert.pem 自动找到",
          got == str(tls / "cert.pem")
          and os.environ.get("SSL_CERT_FILE") == str(tls / "cert.pem"))
    clean_env()

    adir = tempfile.mkdtemp(prefix="ssl_android_")
    (Path(adir) / "0123abcd.0").write_text("X" * 200, encoding="utf-8")
    with ExitStack() as st:
        for p in linux():
            st.enter_context(p)
        st.enter_context(patch.object(config, "_ca_candidates", lambda: []))
        st.enter_context(patch.object(config, "_android_ca_dir", lambda: adir))
        got = config.ensure_ssl_ca(force=True)
        t("Android 散装 CA 目录写进 SSL_CERT_DIR",
          got == adir and os.environ.get("SSL_CERT_DIR") == adir)
    clean_env()

    os.environ["WM_SSL_CAFILE"] = str(ca)
    with patch.object(config.os, "name", "nt"):
        got = config.ensure_ssl_ca(force=True)
    t("Windows 用系统证书库，一律不动",
      got is None and "SSL_CERT_FILE" not in os.environ)
    clean_env()

    with ExitStack() as st:
        for p in linux():
            st.enter_context(p)
        st.enter_context(patch.object(config, "_ca_candidates", lambda: []))
        st.enter_context(patch.object(config, "_android_ca_dir", lambda: None))
        got = config.ensure_ssl_ca(force=True)
        t("一个 CA 都找不到时不硬改环境",
          got is None and "SSL_CERT_FILE" not in os.environ)

    # ---------- 3) urlopen：请求签名 + 降级 ----------
    print("[urlopen 统一出口]")
    clean_env()
    calls = []
    resp = object()

    def fake_ok(req, timeout=10, context=None):
        calls.append((req, timeout, context))
        return resp

    with patch.object(urllib.request, "urlopen", fake_ok):
        got = config.urlopen("REQ", timeout=7)
    t("正常请求返回响应", got is resp)
    t("正常请求签名不变（req + timeout，无 context）",
      calls == [("REQ", 7, None)])

    clean_env()
    calls = []

    def fake_ca(req, timeout=10, context=None):
        calls.append((req, timeout, context))
        if len(calls) == 1:
            raise urllib.error.URLError(CA_ERR)
        return resp

    with patch.object(urllib.request, "urlopen", fake_ca):
        got = config.urlopen("REQ", timeout=7)
        t("证书错误后自动重试成功", got is resp)
        t("第一次按正常校验发", calls[0][2] is None)
        t("第二次带不校验 context", isinstance(calls[1][2], ssl.SSLContext)
          and calls[1][2].verify_mode == ssl.CERT_NONE)
        config.urlopen("REQ2", timeout=7)
        t("降级后后续请求直接走降级，不再白失败一次",
          len(calls) == 3 and calls[2][2] is not None)
    t("降级状态已置位", config._SSL_UNVERIFIED is True)

    clean_env()
    calls = []

    def fake_timeout(req, timeout=10, context=None):
        calls.append(context)
        raise urllib.error.URLError("timed out")

    caught = False
    with patch.object(urllib.request, "urlopen", fake_timeout):
        try:
            config.urlopen("REQ")
        except urllib.error.URLError:
            caught = True
    t("非证书错误原样抛出、不降级", caught and len(calls) == 1
      and calls[0] is None and config._SSL_UNVERIFIED is False)

    clean_env()
    os.environ["WM_SSL_NOVERIFY"] = "1"
    calls = []
    with patch.object(urllib.request, "urlopen", fake_ok):
        config.urlopen("REQ", timeout=3)
    t("WM_SSL_NOVERIFY=1 直接走不校验", len(calls) == 1
      and isinstance(calls[0][2], ssl.SSLContext))
    clean_env()

    # ---------- 4) ssl_context ----------
    print("[ssl_context / wss]")
    clean_env()
    ctx = config.ssl_context()
    t("默认 wss context 开启证书校验",
      isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED)
    config._SSL_UNVERIFIED = True
    ctx2 = config.ssl_context()
    t("降级后 wss context 不校验", ctx2.verify_mode == ssl.CERT_NONE
      and ctx2.check_hostname is False)
    config.reset_ssl_state()
    t("reset_ssl_state 清掉降级标记", config._SSL_UNVERIFIED is False)

    # ---------- 5) 取 access_token 端到端：从证书错误里恢复 ----------
    print("[取 access_token 端到端]")
    import qq_official
    clean_env()
    calls = []

    def fake_token(req, timeout=10, context=None):
        calls.append(context)
        if len(calls) == 1:
            raise urllib.error.URLError(CA_ERR)
        return io.BytesIO(b'{"access_token":"TOK_OK","expires_in":"7200"}')

    with patch.object(urllib.request, "urlopen", fake_token):
        tok = qq_official.TokenManager("app", "sec").get_token()
    t("第一次证书失败后仍能拿到 token", tok == "TOK_OK" and len(calls) == 2)

    # ---------- 6) 所有 HTTPS 出口都已统一 ----------
    print("[全项目出口统一]")
    for name in ("qq_official.py", "wm.py", "worldstate.py"):
        src = (Path(HERE) / name).read_text(encoding="utf-8")
        t("%s 不再直接 urlopen" % name, "urllib.request.urlopen" not in src)
        t("%s 走 config.urlopen" % name, "bot_config.urlopen" in src)
    src = (Path(HERE) / "config.py").read_text(encoding="utf-8")
    t("config.py 是唯一直接调 urlopen 的模块", "urllib.request.urlopen" in src)
    src = (Path(HERE) / "onebot.py").read_text(encoding="utf-8")
    t("onebot wss 走 config.ssl_context", "bot_config.ssl_context" in src)

    restore_env()
    print("\n结果：通过 %d / 失败 %d" % (ok, total - ok))
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
