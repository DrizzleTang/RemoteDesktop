"""relay/ 中转服务器的单元与集成测试。

大部分用例通过在随机端口上启动一个真实的 ``websockets.serve``,再用
``websockets.connect`` 模拟 host / client 两端跑完整的注册 -> 配对 -> 转发
流程;另有若干纯逻辑单测直接调用 ``RelayServer`` 的内部方法(不需要网络),
用来覆盖 id 正则、注册限流、转发限速这些边界情况。
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from relay.server import ID_RE, RelayServer, _RateLimiter


# ---------------------------------------------------------------------------
# 纯逻辑单测(不需要网络/websocket)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("AB12", True),
        ("a" * 32, True),
        ("a" * 33, False),  # 超过最大长度
        ("abc", False),  # 长度不足 4
        ("", False),
        ("has space", False),
        ("has-dash", False),
        ("under_score", False),
        ("有中文", False),
    ],
)
def test_id_regex(value, expected):
    assert bool(ID_RE.match(value)) is expected


def test_register_rate_limit_blocks_after_threshold():
    server = RelayServer(register_rate_limit_count=3, register_rate_limit_window=60.0)
    ip = "1.2.3.4"
    assert server._check_register_rate_limit(ip) is True
    assert server._check_register_rate_limit(ip) is True
    assert server._check_register_rate_limit(ip) is True
    # 第 4 次在窗口内应被拒绝
    assert server._check_register_rate_limit(ip) is False


def test_register_rate_limit_is_per_ip():
    server = RelayServer(register_rate_limit_count=1, register_rate_limit_window=60.0)
    assert server._check_register_rate_limit("1.1.1.1") is True
    assert server._check_register_rate_limit("1.1.1.1") is False
    # 另一个 IP 不受影响
    assert server._check_register_rate_limit("2.2.2.2") is True


def test_rate_limiter_drops_when_exceeding_budget():
    limiter = _RateLimiter(10)  # 10 字节/秒,初始桶容量 10
    assert limiter.allow(5) is True  # 剩余 5
    assert limiter.allow(6) is False  # 预算不足,应被丢弃(而不是排队等待)
    assert limiter.allow(5) is True  # 恰好用完剩余的 5


def test_rate_limiter_unlimited_when_disabled():
    limiter = _RateLimiter(None)
    assert limiter.allow(10**9) is True


# ---------------------------------------------------------------------------
# 集成测试:真实起一个 websockets.serve,用 websockets.connect 模拟双端
# ---------------------------------------------------------------------------


async def _register(ws, role: str, session_id: str, v: int = 1) -> None:
    await ws.send(json.dumps({"t": "relay_register", "role": role, "id": session_id, "v": v}))


async def _start_server(**kwargs):
    """启动一个监听在随机端口上的 RelayServer,返回 (server, uri)。

    调用方需要负责在测试结束时调用返回的 cleanup 协程。
    """
    server = RelayServer(**kwargs)
    server.start_background_tasks()
    ws_server = await websockets.serve(server.handle_connection, "127.0.0.1", 0)
    port = ws_server.sockets[0].getsockname()[1]
    uri = f"ws://127.0.0.1:{port}"

    async def cleanup():
        ws_server.close()
        await ws_server.wait_closed()
        await server.stop_background_tasks()

    return server, uri, cleanup


@pytest.fixture
async def relay():
    server, uri, cleanup = await _start_server(
        register_timeout=5.0,
        pairing_wait_timeout=5.0,
        sweep_interval=0.2,
        forward_rate_limit_bytes_per_sec=None,  # 默认测试不做转发限速
    )
    try:
        yield server, uri
    finally:
        await cleanup()


async def test_invalid_id_rejected(relay):
    _server, uri = relay
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"t": "relay_register", "role": "host", "id": "a b", "v": 1}))
        reply = json.loads(await ws.recv())
        assert reply == {"t": "relay_error", "reason": "invalid_id"}
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await ws.recv()


async def test_invalid_role_rejected(relay):
    _server, uri = relay
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"t": "relay_register", "role": "server", "id": "ABCD1234", "v": 1}))
        reply = json.loads(await ws.recv())
        assert reply == {"t": "relay_error", "reason": "invalid_role"}
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await ws.recv()


async def test_malformed_first_message_closes_without_reply(relay):
    _server, uri = relay
    async with websockets.connect(uri) as ws:
        await ws.send("not json at all")
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await ws.recv()


async def test_host_not_found(relay):
    _server, uri = relay
    async with websockets.connect(uri) as ws:
        await _register(ws, "client", "NOHOST01")
        reply = json.loads(await ws.recv())
        assert reply == {"t": "relay_error", "reason": "host_not_found"}
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await ws.recv()


async def test_id_in_use_rejects_second_host(relay):
    _server, uri = relay
    session_id = "DUPHOST1"
    async with websockets.connect(uri) as host1:
        await _register(host1, "host", session_id)
        assert json.loads(await host1.recv()) == {"t": "relay_registered"}

        async with websockets.connect(uri) as host2:
            await _register(host2, "host", session_id)
            reply = json.loads(await host2.recv())
            assert reply == {"t": "relay_error", "reason": "id_in_use"}
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await host2.recv()


async def test_pairing_and_bidirectional_forwarding(relay):
    server, uri = relay
    session_id = "PAIRME01"

    async with websockets.connect(uri) as host_ws, websockets.connect(uri) as client_ws:
        await _register(host_ws, "host", session_id)
        assert json.loads(await host_ws.recv()) == {"t": "relay_registered"}
        assert session_id in server._active_ids

        await _register(client_ws, "client", session_id)
        assert json.loads(await client_ws.recv()) == {"t": "relay_paired"}
        assert json.loads(await host_ws.recv()) == {"t": "relay_paired"}

        # 配对之后 id 不再出现在等待队列里(转发模式中,但仍视为占用)
        assert session_id not in server._waiting
        assert session_id in server._active_ids

        # host -> client 二进制帧原样转发
        binary_payload = b"\x00\x01binary-ciphertext\xff"
        await host_ws.send(binary_payload)
        assert await client_ws.recv() == binary_payload

        # client -> host 文本帧原样转发
        await client_ws.send("hello-from-client")
        assert await host_ws.recv() == "hello-from-client"

        # 再来一轮,确认可以持续双向转发
        await host_ws.send("hello-from-host")
        assert await client_ws.recv() == "hello-from-host"

        # 一端断开应导致 relay 主动关闭另一端
        await client_ws.close()
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await host_ws.recv()

    # 会话结束后 id 应被释放
    assert session_id not in server._active_ids
    assert session_id not in server._waiting

    # 释放后可以重新注册同一个 id
    async with websockets.connect(uri) as host_ws2:
        await _register(host_ws2, "host", session_id)
        assert json.loads(await host_ws2.recv()) == {"t": "relay_registered"}


async def test_waiting_host_pairing_timeout_is_swept():
    server, uri, cleanup = await _start_server(
        register_timeout=5.0,
        pairing_wait_timeout=0.2,
        sweep_interval=0.1,
    )
    try:
        session_id = "TIMEOUT1"
        async with websockets.connect(uri) as host_ws:
            await _register(host_ws, "host", session_id)
            assert json.loads(await host_ws.recv()) == {"t": "relay_registered"}
            assert session_id in server._active_ids

            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(host_ws.recv(), timeout=3)

        assert session_id not in server._active_ids
        assert session_id not in server._waiting
    finally:
        await cleanup()


async def test_registration_timeout_disconnects_idle_connection():
    server, uri, cleanup = await _start_server(register_timeout=0.2, sweep_interval=1.0)
    try:
        async with websockets.connect(uri) as ws:
            # 不发送任何注册消息
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=3)
    finally:
        await cleanup()


async def test_forward_rate_limit_drops_oversized_message():
    # 极低的转发限速(16 字节/秒),用于验证超出预算的消息会被直接丢弃,
    # 而不是被缓冲后延迟送达。
    server, uri, cleanup = await _start_server(
        register_timeout=5.0,
        pairing_wait_timeout=5.0,
        sweep_interval=1.0,
        forward_rate_limit_bytes_per_sec=16,
    )
    try:
        session_id = "RATELIM1"
        async with websockets.connect(uri) as host_ws, websockets.connect(uri) as client_ws:
            await _register(host_ws, "host", session_id)
            await host_ws.recv()
            await _register(client_ws, "client", session_id)
            await client_ws.recv()
            await host_ws.recv()

            # 桶容量为 16 字节,一条 100 字节的消息永远无法一次放行 -> 应被丢弃
            await host_ws.send(b"x" * 100)
            # 紧接着发一条很小的消息,预算足够,应当能被转发
            await asyncio.sleep(0.05)
            await host_ws.send(b"yyyy")

            got = await asyncio.wait_for(client_ws.recv(), timeout=3)
            assert got == b"yyyy"  # 说明超预算的第一条消息被丢弃了,而不是排队后先送达
    finally:
        await cleanup()


async def test_max_connections_limit_rejects_new_connection():
    server, uri, cleanup = await _start_server(register_timeout=5.0, max_connections=1)
    try:
        async with websockets.connect(uri) as ws1:
            await _register(ws1, "host", "MAXCONN1")
            assert json.loads(await ws1.recv()) == {"t": "relay_registered"}

            async with websockets.connect(uri) as ws2:
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await asyncio.wait_for(ws2.recv(), timeout=3)
    finally:
        await cleanup()
