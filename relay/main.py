"""relay 中转服务器命令行入口。

用于部署在云端 Linux 服务器上,当 host 与 client 之间无法直连(NAT/防火墙)
时提供中转。不涉及 Windows 打包,纯 Python 进程即可运行。

用法示例::

    python3 -m relay.main --host 0.0.0.0 --port 8770

也可以直接运行本文件::

    python3 relay/main.py --port 8770 --max-connections 1000

启用 TLS(wss://)::

    python3 -m relay.main --port 8770 --cert /etc/letsencrypt/live/例.com/fullchain.pem \
                                      --key  /etc/letsencrypt/live/例.com/privkey.pem

健康检查::

    curl http://<relay 地址>:8770/healthz
"""

from __future__ import annotations

import argparse
import asyncio
import http
import json
import logging
import os
import signal
import ssl
from typing import Any, Callable, Optional

import websockets

from relay.server import RelayServer

logger = logging.getLogger("relay.main")

# 健康检查端点路径。除它之外的所有路径都不做特殊处理,继续走原本的
# WebSocket 升级流程,保证已有 host/client 的行为完全不变。
HEALTH_PATH = "/healthz"


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
    # --- TLS(wss://)相关参数 ---------------------------------------------
    # 为什么 relay 还需要 TLS?relay 转发的应用层数据本身已经是端到端加密的
    # (relay 只是搬运密文,看不到明文),但 relay <-> host/client 之间的
    # WebSocket 握手本身在 ws:// 下是**明文**的:注册消息里的会话码、以及
    # HTTP Upgrade 请求头都会裸奔在公网上。套一层 TLS 有两个实际收益:
    #   1) 隐藏会话码,避免旁路观察者抢在合法 client 之前用同一个 id 配对;
    #   2) 防止运营商 / 公司出口设备 / 酒店 Wi-Fi 之类的中间设备识别并干扰、
    #      篡改甚至直接掐断 WebSocket 升级握手(明文 80/8770 端口的
    #      Upgrade 请求很容易被这类设备"优化")。
    parser.add_argument(
        "--cert", default=None, metavar="PATH",
        help=(
            "TLS 证书文件(PEM,通常是 fullchain.pem);与 --key 必须同时提供。"
            "提供后 relay 以 wss:// 监听。注意:应用层数据本来就是端到端加密的,"
            "TLS 额外保护的是握手阶段——隐藏会话码,并防止中间设备干扰 WebSocket 升级。"
        ),
    )
    parser.add_argument(
        "--key", default=None, metavar="PATH",
        help="TLS 私钥文件(PEM,通常是 privkey.pem);与 --cert 必须同时提供。",
    )
    return parser.parse_args(argv)


def build_ssl_context(cert_path: Optional[str], key_path: Optional[str]) -> Optional[ssl.SSLContext]:
    """根据 --cert/--key 构造服务端 ``SSLContext``;两者都没给时返回 None(明文 ws://)。

    参数校验失败一律抛 ``SystemExit`` 并给出中文提示,让用户在启动阶段就
    发现配置问题,而不是等到客户端连不上才排查。
    """
    if not cert_path and not key_path:
        return None

    # 只给了其中一个:必然是配置写漏了,直接报错退出而不是悄悄退化成 ws://,
    # 否则用户会误以为自己已经开启了 TLS。
    if bool(cert_path) != bool(key_path):
        missing = "--key" if cert_path else "--cert"
        given = "--cert" if cert_path else "--key"
        raise SystemExit(
            f"错误:{given} 与 {missing} 必须同时提供才能启用 TLS(wss://),当前缺少 {missing}。\n"
            f"      要启用 TLS 请补上 {missing};要以明文 ws:// 运行请把 {given} 一起去掉。"
        )

    for label, path in (("证书(--cert)", cert_path), ("私钥(--key)", key_path)):
        if not os.path.isfile(path):
            raise SystemExit(
                f"错误:{label} 文件不存在或不是普通文件:{path}\n"
                f"      请检查路径是否写错;若用 certbot 签发,证书通常位于 "
                f"/etc/letsencrypt/live/<域名>/ 目录下,且需要有读取权限。"
            )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    except (ssl.SSLError, OSError) as exc:
        # 常见原因:证书与私钥不匹配、文件不是 PEM 格式、私钥被口令加密、
        # 以非 root 身份读取 /etc/letsencrypt 下的私钥而权限不足。
        raise SystemExit(
            f"错误:加载 TLS 证书/私钥失败:{exc}\n"
            f"      请确认 --cert 与 --key 是配套的 PEM 文件、未加密码保护,且当前用户有读取权限。"
        ) from exc
    return context


def make_process_request(server: RelayServer) -> Callable[[Any, Any], Any]:
    """构造 ``websockets.serve(process_request=...)`` 回调,用于处理 /healthz。

    ``process_request`` 在 WebSocket 升级握手之前被调用:
    - 返回 ``None`` 表示"不拦截",继续完成 WebSocket 升级(保持原有行为);
    - 返回一个 ``Response`` 表示直接以普通 HTTP 响应结束这次请求。

    这里只拦截 ``/healthz``,其余任何路径(包括 ``/``)都返回 None 放行。
    注:websockets 只会把 HTTP ``GET`` 请求交到这里(其它 method 在更底层
    的 HTTP 解析阶段就被拒绝了),所以无需再判断 method。
    """

    def process_request(connection: Any, request: Any) -> Any:
        # request.path 是原始请求目标,可能带查询串(如 /healthz?from=uptime-robot),
        # 这里按 '?' 截断后再比较。
        path = request.path.split("?", 1)[0]
        if path != HEALTH_PATH:
            return None  # 放行:继续走 WebSocket 升级流程

        # 只输出聚合计数,绝不包含会话码 / 对端 IP 等敏感信息。
        body = json.dumps(server.stats_snapshot(), ensure_ascii=False) + "\n"
        response = connection.respond(http.HTTPStatus.OK, body)
        # respond() 默认给的是 text/plain,这里改成 JSON 方便监控系统解析。
        # websockets 的 Headers 是多值容器,直接赋值会“追加”而不是“覆盖”,
        # 所以必须先删除原有的 Content-Type。
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        return response

    return process_request


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

    # TLS 上下文在这里构造:参数有问题时会抛 SystemExit,启动阶段就报错退出。
    ssl_context = build_ssl_context(args.cert, args.key)
    scheme = "wss" if ssl_context is not None else "ws"

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
        # ssl=None 时 websockets 走明文 ws://,非 None 时走 wss://
        ssl=ssl_context,
        # 拦截 /healthz 普通 HTTP 请求,其余路径照常升级为 WebSocket
        process_request=make_process_request(server),
    ):
        logger.info(
            "relay 服务器已启动,监听 %s://%s:%d (最大连接数=%d, 转发限速=%s, 注册限流=%s次/分钟)",
            scheme, args.host, args.port, args.max_connections,
            f"{args.forward_rate_limit_mbps}MB/s" if args.forward_rate_limit_mbps > 0 else "无",
            args.register_rate_limit if args.register_rate_limit > 0 else "无",
        )
        if ssl_context is not None:
            logger.info(
                "已启用 TLS:客户端请使用 wss:// 连接。"
                "应用层数据本就端到端加密,TLS 额外隐藏了握手阶段的会话码,"
                "并避免中间网络设备干扰 WebSocket 升级握手。"
            )
        else:
            logger.warning(
                "当前为明文 ws:// 模式:WebSocket 握手(含会话码)不加密。"
                "公网部署建议通过 --cert/--key 启用 TLS。"
            )
        logger.info("健康检查端点:%s://%s:%d%s", "https" if ssl_context else "http",
                    args.host, args.port, HEALTH_PATH)
        await stop_event.wait()

    await server.stop_background_tasks()
    logger.info("relay 服务器已停止")


def main(argv=None) -> None:
    args = parse_args(argv)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
