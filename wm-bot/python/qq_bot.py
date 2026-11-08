#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qq_bot.py — WM→QQ 机器人统一入口

两种对接后端，由 data/config.txt 里的 WM_QQ_BACKEND 选择（默认 official）：
  - official（默认）：官方 QQ 机器人 API（q.qq.com，正规无封号风险）
      见 qq_official.py
  - onebot          ：OneBot v11 社区方案（NapCat / Lagrange / go-cqhttp，普通群可用，
                      但腾讯不官方支持、有封号风险；手机挂机常用反向 WS）
      见 onebot.py

两种后端共用 qq_common.py 里的消息判定 / 群白名单 / WM 查询触发逻辑。

群白名单（两种后端通用）：
  - 默认【不监控任何群】。只有 data/groups.txt 里写进的群号 / 群 OpenID，才被监听；
  - 白名单群内，指令需带 D/XX/ 前缀（或直接 @机器人）才回复，避免群聊误触发；
  - 私聊不受白名单限制（一对一，无刷屏风险），照常响应 WM/ZK 查询。

消息规则：触发词 WM / ZK（带不带斜杠均可，大小写不敏感）、/报错（强制刷新）、/查询；
          复用 wm.py 规则：3~5 秒同域名节流、data/config.txt 里 WM_CACHE_MINUTES 分钟缓存
          （超间隔先联网刷新缓存再回复，不发旧数据）、订单列表（价格从低到高）、
          单条 /w 提醒、WM 完整网页来源。

配置：所有要改的参数（appid / secret / 缓存分钟数 / 白名单 / 调试开关）全在
      同目录 data/config.txt，每行「名称 = 值」，文件内有中文注释；改完重启生效。
      不需要启动脚本、不需要环境变量命令、不需要改代码。

运行（整个文件夹拷到任意目录，路径全部基于 __file__ 解析）：
    python qq_bot.py                       # 官方（默认）
    python qq_bot.py --allow-all-groups       # 临时抓群 openid 用（非正式模式）
"""
import os
import sys

import config as bot_config
import qq_common
import drops
import wm


def _run_onebot():
    """OneBot v11 后端（社区方案）。"""
    from onebot import OneBotClient

    wm.reset_all_locks()
    mode = os.environ.get("WM_QQ_MODE", "client")
    url = os.environ.get("WM_QQ_URL", "ws://127.0.0.1:6700")
    token = os.environ.get("WM_QQ_TOKEN")
    groups_file = os.environ.get("WM_QQ_GROUPS_FILE",
                                 str(qq_common.DEFAULT_GROUPS_FILE))
    allowed = qq_common.load_allowed_groups(groups_file)
    allow_all = bot_config.flag("WM_QQ_ALLOW_ALL_GROUPS")
    base_handler = qq_common.make_handler(allowed, allow_all=allow_all)
    client = OneBotClient(
        url=url, token=token, mode=mode,
        handler=qq_common.make_monitor_aware_handler(base_handler),
    )
    import ws_monitor
    ws_monitor.set_sender(client.send_active)
    saved_n = ws_monitor.load_switches()
    saved_f = ws_monitor.load_filters()
    if saved_n:
        print(f"[qq_bot] 已恢复 {saved_n} 个会话保存的主推开关"
              "（发「主推状态」可查看）", file=sys.stderr)
    if saved_f:
        print(f"[qq_bot] 已恢复 {saved_f} 个会话的裂缝过滤条件", file=sys.stderr)
    # 恢复「监控是谁开的」：之后这个会话的推送会 @ 他
    saved_a = ws_monitor.load_at_users()
    if saved_a:
        print(f"[qq_bot] 已恢复 {saved_a} 个会话的监控发起人", file=sys.stderr)
    # 恢复成员级订阅（谁开了哪个栏目）：群里「停止监控」只撤自己，「停止全部监控」才清全群
    saved_s = ws_monitor.load_subs()
    if saved_s:
        print(f"[qq_bot] 已恢复 {saved_s} 个会话的成员订阅", file=sys.stderr)
    auto_n = ws_monitor.auto_start_priority(allowed if not allow_all else ())
    if auto_n:
        print(f"[qq_bot] WS_AUTO_PRIORITY 已开启：{auto_n} 个白名单群自动主推 "
              f"{ws_monitor.priority_types()}（新条目即时推送）", file=sys.stderr)
    # 遗物 / 掉落索引：后台预抓一次（首次约 30 秒），之后查询只读本地索引秒回
    drops.ensure_async()
    print(f"[qq_bot] WM→QQ（OneBot {mode}）启动，URL={url}",
          file=sys.stderr)
    if allow_all:
        print("[qq_bot] ⚠️ 已设监控【所有群】（调试用，"
              "正式运行请勿开启）", file=sys.stderr)
    else:
        if allowed:
            print(f"[qq_bot] 群白名单已加载 {len(allowed)} 个：{sorted(allowed)}",
                  file=sys.stderr)
        else:
            print("[qq_bot] 群白名单为空（未配置群号）：默认不监控任何群。"
                  f"请在 {groups_file} 每行写入一个群号。", file=sys.stderr)
    print("[qq_bot] 白名单群内发送「WM <物品名>」或「ZK <武器名>」即可查询；"
          "「/报错 WM <名称>」强制刷新。", file=sys.stderr)
    client.run_forever()


def _startup_selfcheck():
    """开机把「图片 / Markdown 能不能发」一次说清，手机端排障不用翻日志。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(here, "render_card.py")):
        print("[card] ⚠️ 缺少 render_card.py！请把电脑 python/ 目录下这个文件一起拷过来，"
              "否则所有回复只能发纯文字、红/蓝/灰昵称也不会有。", file=sys.stderr)
    if not os.path.exists(os.path.join(here, "qq_markdown.py")):
        print("[md] ⚠️ 缺少 qq_markdown.py，Markdown 通道不可用（自动回退富媒体图片）",
              file=sys.stderr)
    try:
        import render_card
        card = render_card.diagnose()
    except Exception as e:
        card = f"自检失败：{e}（render_card.py 是否拷到手机了？）"
    print(f"[card] 图片能力自检：{card}", file=sys.stderr)
    try:
        import qq_markdown
        if qq_markdown.enabled():
            print(f"[md] Markdown 消息已启用：{qq_markdown.resolved_mode()}"
                  "（一条消息：上卡片图 + 下可复制字符串）", file=sys.stderr)
        else:
            print("[md] Markdown 消息已关闭：走富媒体图片 msg_type=7 图文混排",
                  file=sys.stderr)
    except Exception as e:
        print(f"[md] Markdown 自检失败：{e}", file=sys.stderr)


def main():
    bot_config.apply()   # 幂等：读同目录 data/config.txt（wm 模块导入时已读过，这里再保底）
    ca = bot_config.ensure_ssl_ca()   # 手机端缺 CA 根证书时自动找 bundle / 兜底
    if ca:
        print(f"[ssl] 已启用 CA 根证书：{ca}", file=sys.stderr)
    _startup_selfcheck()
    # 纯 Python 的调试开关：临时抓群 openid 用，不改 data/config.txt
    if "--allow-all-groups" in sys.argv[1:]:
        os.environ["WM_QQ_ALLOW_ALL_GROUPS"] = "1"
    backend = os.environ.get("WM_QQ_BACKEND", "official").lower()
    if backend == "onebot":
        _run_onebot()
    else:
        from qq_official import main as official_main
        official_main()


if __name__ == "__main__":
    main()
