"""被控端命令行入口。"""
from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import socket
import string

from host.server import HostConfig, HostServer


def _random_password(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _random_session_id(length: int = 8) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="远程桌面 - 被控端(运行在需要被控制的电脑上)")
    parser.add_argument("--port", type=int, default=8765, help="直连模式监听端口(默认 8765)")
    parser.add_argument("--bind-host", default="0.0.0.0", help="直连模式绑定地址(默认 0.0.0.0)")
    parser.add_argument("--password", default=None, help="访问密码(不指定则自动生成)")
    parser.add_argument("--id", dest="session_id", default=None, help="中转模式下的会话码(不指定则自动生成)")
    parser.add_argument("--relay", dest="relay_url", default=None,
                         help="中转服务器地址,例如 ws://relay.example.com:8770;不指定则使用直连模式")
    parser.add_argument("--monitor", type=int, default=1, help="要采集的显示器编号(默认 1,即主显示器)")
    parser.add_argument("--host-name", default=None, help="展示给对方的本机名称")
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
    )

    print("=" * 56)
    print(" 远程桌面 - 被控端 已启动")
    if args.relay_url:
        print(f" 连接方式: 中转服务器 ({args.relay_url})")
        print(f" 会话码 (ID): {session_id}")
    else:
        print(" 连接方式: 直连")
        print(f" 请确保对方可访问本机地址: <本机IP>:{args.port}")
    print(f" 访问密码: {password}")
    print(" 请将以上连接信息告知需要连接的一方,在网页客户端中填写")
    print(" 按 Ctrl+C 退出")
    print("=" * 56)

    server = HostServer(config)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
