"""被控端命令行入口。"""
from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import socket
import string
from pathlib import Path

from host import password_policy
from host.server import HostConfig, HostServer
from host.tray import TrayIcon

DEFAULT_DOWNLOAD_DIR = Path.home() / "RemoteDesktop-收到的文件"


def _random_password(length: int = 10) -> str:
    """生成访问密码。

    两点约束:
    1. 去掉容易看错的字符(0/O、1/l/I)——用户要照着控制台把密码念给对方听;
    2. 保证同时含有大写、小写、数字。纯随机抽样有相当概率抽出全字母的组合,
       那样会被 host/password_policy 判定为弱密码,自相矛盾。
    """
    lowers = [c for c in string.ascii_lowercase if c not in "l"]
    uppers = [c for c in string.ascii_uppercase if c not in "OI"]
    digits = [c for c in string.digits if c not in "01"]
    alphabet = lowers + uppers + digits

    chars = [secrets.choice(lowers), secrets.choice(uppers), secrets.choice(digits)]
    chars += [secrets.choice(alphabet) for _ in range(max(0, length - 3))]
    # 用 SystemRandom.shuffle 打乱,避免"前三位固定是小写/大写/数字"的规律
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _random_session_id(length: int = 10) -> str:
    # 字母数字混合(而不是纯数字)以获得更大的猜测空间,降低中转模式下
    # 会话码在配对等待期间被抢注/暴力枚举的可行性(见 docs/relay_protocol.md
    # 与 relay/server.py 里 ID_RE 的说明)。
    alphabet = "".join(c for c in (string.ascii_uppercase + string.digits) if c not in "O0I1")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="远程桌面 - 被控端(运行在需要被控制的电脑上)")
    parser.add_argument("--port", type=int, default=8765, help="直连模式监听端口(默认 8765)")
    parser.add_argument("--bind-host", default="0.0.0.0", help="直连模式绑定地址(默认 0.0.0.0)")
    parser.add_argument("--password", default=None, help="访问密码(不指定则自动生成强随机密码)")
    parser.add_argument("--id", dest="session_id", default=None, help="中转模式下的会话码(不指定则自动生成)")
    parser.add_argument("--relay", dest="relay_url", default=None,
                        help="中转服务器地址,例如 ws://relay.example.com:8770;不指定则使用直连模式")
    parser.add_argument("--monitor", type=int, default=1, help="要采集的显示器编号(默认 1,即主显示器)")
    parser.add_argument("--host-name", default=None, help="展示给对方的本机名称")
    parser.add_argument("--download-dir", default=str(DEFAULT_DOWNLOAD_DIR),
                        help=f"接收对方传来的文件的保存目录(默认 {DEFAULT_DOWNLOAD_DIR})")
    parser.add_argument("--no-file-transfer", action="store_true", help="禁用文件接收功能")
    parser.add_argument("--max-viewers", type=int, default=1,
                        help="允许同时连接的会话总数(默认 1)。大于 1 时,第一个连上的持有操作权,"
                             "其余为只读观看者。仅直连模式有效,中转模式恒为 1")
    parser.add_argument("--no-tray", action="store_true", help="不显示系统托盘图标")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    password = args.password or _random_password()
    session_id = args.session_id or _random_session_id()
    try:
        host_name = args.host_name or socket.gethostname()
    except Exception:  # noqa: BLE001
        host_name = "host"

    config = HostConfig(
        password=password, session_id=session_id, bind_host=args.bind_host, port=args.port,
        relay_url=args.relay_url, monitor_index=args.monitor, host_name=host_name,
        download_dir=Path(args.download_dir).expanduser(),
        max_viewers=max(1, args.max_viewers),
        allow_file_transfer=not args.no_file_transfer,
    )

    if args.relay_url:
        connect_hint = f"中转 · 会话码 {session_id}"
    else:
        connect_hint = f"直连 · 端口 {args.port}"

    print("=" * 56)
    print(" 远程桌面 - 被控端 已启动")
    if args.relay_url:
        print(f" 连接方式: 中转服务器 ({args.relay_url})")
        print(f" 会话码 (ID): {session_id}")
    else:
        print(" 连接方式: 直连")
        print(f" 请确保对方可访问本机地址: <本机IP>:{args.port}")
    print(f" 访问密码: {password}")
    if config.allow_file_transfer:
        print(f" 接收文件保存到: {config.download_dir}")
    if config.max_viewers > 1 and not args.relay_url:
        print(f" 最多同时 {config.max_viewers} 个连接(第 1 个可操作,其余只能观看)")
    print(" 请将以上连接信息告知需要连接的一方,在网页客户端中填写")
    print(" 按 Ctrl+C 退出")
    print("=" * 56)

    # 用户手动指定了弱密码时给出醒目警告(自动生成的密码不会触发)
    if args.password:
        warning = password_policy.format_warning(args.password)
        if warning:
            print(warning)

    server = HostServer(config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tray: TrayIcon | None = None
    if not args.no_tray:
        tray = TrayIcon(
            host_name=host_name, connect_hint=connect_hint, password=password,
            on_quit=lambda: loop.call_soon_threadsafe(loop.stop),
        )
        tray.start()

    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        if tray:
            tray.stop()
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
