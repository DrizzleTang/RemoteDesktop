"""relay 中转服务器命令行入口。

用于部署在云端 Linux 服务器上,当 host 与 client 之间无法直连(NAT/防火墙)
时提供中转。不涉及 Windows 打包,纯 Python 进程即可运行。

用法示例::

    python3 -m relay.main --host 0.0.0.0 --port 8770

也可以直接运行本文件::

    python3 relay/main.py --port 8770 --max-connections 1000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

import websockets

from relay.server import RelayServer

logger = logging.getLogger("relay.main")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="RemoteDesktop relay 中转服务器:在 host/client 无法直连时转发原始 WebSocket 消息。",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址(默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8770, help="监听端口(默认 8770)")
    parser.add_argument(
        "--max-connections", type=int, default=500,
        help="最大并发连接数,超过后拒绝新连接(默认 500)",
    )
    parser.add_argument(
        "--max-frame-bytes", type=int, default=16 * 1024 * 1024,
        help="单条 WebSocket 消息(帧)允许的最大字节数(默认 16MB)",
    )
    parser.add_argument(
        "--forward-rate-limit-mbps", type=float, default=8.0,
        help="每个已配对会话的转发限速,单位 MB/s(默认 8;<=0 表示不限速)",
    )
    parser.add_argument(
        "--register-rate-limit", type=int, default=20,
        help="每个来源 IP 每分钟允许的 relay_register 尝试次数(默认 20;<=0 表示不限流)",
    )
    parser.add_argument(
        "--register-timeout", type=float, default=60.0,
        help="连接建立后等待注册消息的超时时间,单位秒(默认 60)",
    )
    parser.add_argument(
        "--pairing-wait-timeout", type=float, default=600.0,
        help="host 等待 client 配对的最长时间,单位秒(默认 600,即 10 分钟)",
    )
    parser.add_argument(
        "--sweep-interval", type=float, default=60.0,
        help="后台清理超时等待中 host 的扫描间隔,单位秒(默认 60)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="日志级别(默认 INFO)",
    )
    return parser.parse_args(argv)


def _build_server(args: argparse.Namespace) -> RelayServer:
    rate_limit_bytes = None
    if args.forward_rate_limit_mbps and args.forward_rate_limit_mbps > 0:
        rate_limit_bytes = args.forward_rate_limit_mbps * 1024 * 1024

    return RelayServer(
        forward_rate_limit_bytes_per_sec=rate_limit_bytes,
        register_rate_limit_count=args.register_rate_limit,
        register_timeout=args.register_timeout,
        pairing_wait_timeout=args.pairing_wait_timeout,
        sweep_interval=args.sweep_interval,
        max_connections=args.max_connections,
    )


async def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = _build_server(args)
    server.start_background_tasks()

    stop_event = asyncio.Event()

    def _request_stop(*_args) -> None:
        logger.info("收到退出信号,准备关闭 relay 服务器")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # 部分平台(如 Windows)不支持 add_signal_handler
            pass

    async with websockets.serve(
        server.handle_connection,
        args.host,
        args.port,
        max_size=args.max_frame_bytes,
    ):
        logger.info(
            "relay 服务器已启动,监听 %s:%d (最大连接数=%d, 转发限速=%s, 注册限流=%s次/分钟)",
            args.host, args.port, args.max_connections,
            f"{args.forward_rate_limit_mbps}MB/s" if args.forward_rate_limit_mbps > 0 else "无",
            args.register_rate_limit if args.register_rate_limit > 0 else "无",
        )
        await stop_event.wait()

    await server.stop_background_tasks()
    logger.info("relay 服务器已停止")


def main(argv=None) -> None:
    args = parse_args(argv)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
