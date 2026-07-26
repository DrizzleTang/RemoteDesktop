"""relay 中转服务器核心逻辑。

实现 docs/relay_protocol.md 描述的协议:

- 连接建立后必须立即发送且仅发送一条 ``relay_register`` 文本 JSON 消息。
- host 注册 -> 进入"等待 client"状态,直到被配对、断开或超时(10 分钟)。
- client 注册(携带同一个 id)-> 与等待中的 host 配对,双方各自收到
  ``relay_paired``,随后进入纯转发模式。
- 配对成功后,relay 对之后收到的每一条消息(文本或二进制)原样转发给
  对端,不做任何解析或修改。
- 任意一端断开 -> relay 主动关闭另一端。
- 按来源 IP 限制注册频率;每连接若在注册超时时间内未完成注册则断开;
  每个已配对会话可选限速,超出时丢弃当前消息而不是缓冲排队。

``RelayServer`` 被设计成与具体的网络传输解耦:核心方法
``handle_connection`` 只依赖一个满足"类 websocket"接口的对象
(``recv``/``send``/``close``/``wait_closed``/``remote_address``),因此单元
测试既可以用真实的 ``websockets.serve`` 起服务做集成测试,也可以用简单的
fake/mock 对象直接调用 ``handle_connection`` 或更细粒度的内部方法。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Set

try:
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - 仅在 websockets 未安装时触发
    class ConnectionClosed(Exception):
        """websockets 未安装时的占位异常类型。"""


logger = logging.getLogger("relay.server")

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

# 最短 6 位(而不是更容易被撞中的 4 位):见"短会话码可能被抢注"的安全评估——
# 若运维通过 host --id 手动指定一个很短的会话码,在长达 10 分钟的等待配对
# 窗口内,攻击者有可能在合法 client 之前抢先以该 id 注册为 client 并完成配对
# (即便如此,由于应用层握手仍需要正确密码才能通过,攻击者拿到的只是一次
# "抢先配对"的机会,并不能绕过密码认证;但会造成合法 client 得到
# host_not_found 的拒绝服务)。6 位字母数字组合空间(62**6 ≈ 5.68×10^10)
# 配合按 IP 的注册频率限制,让这种抢注在实践中不再可行。
ID_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")
PROTOCOL_VERSION = 1

MAX_REGISTER_MESSAGE_BYTES = 2048
REGISTER_TIMEOUT_SECONDS = 60.0
PAIRING_WAIT_SECONDS = 10 * 60.0
SWEEP_INTERVAL_SECONDS = 60.0
REGISTER_RATE_LIMIT_COUNT = 20
REGISTER_RATE_LIMIT_WINDOW_SECONDS = 60.0
# 约 8 MB/s,作为每个已配对会话的默认转发限速(可通过构造参数关闭/调整)。
DEFAULT_FORWARD_RATE_LIMIT_BYTES_PER_SEC = 8 * 1024 * 1024


def _error_payload(reason: str) -> str:
    return json.dumps({"t": "relay_error", "reason": reason})


REGISTERED_MSG = json.dumps({"t": "relay_registered"})
PAIRED_MSG = json.dumps({"t": "relay_paired"})


class _RateLimiter:
    """令牌桶限速器,用于单个已配对会话的转发限速。

    超出预算时调用方应当**丢弃**当前消息,而不是等待或缓冲——这是避免
    relay 自己变成弱网场景下 bufferbloat 源头的关键。
    """

    def __init__(self, rate_bytes_per_sec: Optional[float]):
        if rate_bytes_per_sec is not None and rate_bytes_per_sec > 0:
            self.rate: Optional[float] = float(rate_bytes_per_sec)
            self.capacity = self.rate
        else:
            self.rate = None
            self.capacity = 0.0
        self.tokens = self.capacity
        self._last = time.monotonic()

    def allow(self, nbytes: int) -> bool:
        if self.rate is None:
            return True
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if nbytes <= self.tokens:
            self.tokens -= nbytes
            return True
        return False


@dataclass
class _WaitingHost:
    """一个正在等待 client 配对的 host 连接的状态。"""

    websocket: Any
    registered_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    peer: Any = None
    # host->client 和 client->host 两个方向各自独立的令牌桶限速器,避免
    # 双向流量共用同一个预算(否则总吞吐会被意外腰斩,且两个方向的突发
    # 流量会互相挤占对方的配额)。
    limiter_host_to_client: Optional[_RateLimiter] = None
    limiter_client_to_host: Optional[_RateLimiter] = None


class RelayServer:
    """relay 中转服务器的核心逻辑,不绑定具体的网络监听方式。

    典型用法::

        server = RelayServer()
        server.start_background_tasks()
        async with websockets.serve(server.handle_connection, host, port):
            ...

    单元测试可以直接构造 ``RelayServer()`` 并用 fake websocket 对象调用
    ``handle_connection`` / ``_read_registration`` 等方法,不需要真正监听
    网络端口。
    """

    def __init__(
        self,
        *,
        forward_rate_limit_bytes_per_sec: Optional[float] = DEFAULT_FORWARD_RATE_LIMIT_BYTES_PER_SEC,
        register_rate_limit_count: int = REGISTER_RATE_LIMIT_COUNT,
        register_rate_limit_window: float = REGISTER_RATE_LIMIT_WINDOW_SECONDS,
        register_timeout: float = REGISTER_TIMEOUT_SECONDS,
        pairing_wait_timeout: float = PAIRING_WAIT_SECONDS,
        sweep_interval: float = SWEEP_INTERVAL_SECONDS,
        max_connections: Optional[int] = None,
    ) -> None:
        self.forward_rate_limit_bytes_per_sec = forward_rate_limit_bytes_per_sec
        self.register_rate_limit_count = register_rate_limit_count
        self.register_rate_limit_window = register_rate_limit_window
        self.register_timeout = register_timeout
        self.pairing_wait_timeout = pairing_wait_timeout
        self.sweep_interval = sweep_interval
        self.max_connections = max_connections

        # id -> 等待配对的 host 状态
        self._waiting: Dict[str, _WaitingHost] = {}
        # 当前"被占用"的 id(等待中或已配对),用于 id_in_use 判断
        self._active_ids: Set[str] = set()
        # 来源 IP -> 最近一分钟内的注册尝试时间戳
        self._register_attempts: Dict[str, Deque[float]] = {}

        self._sweep_task: Optional[asyncio.Task] = None
        self.active_connection_count = 0
        # 进程启动时间点(RelayServer 在进程启动时构造,所以等价于进程运行时长的
        # 起点),供 /healthz 健康检查端点计算 uptime 用。用 monotonic 而不是
        # time.time(),避免系统时间被 NTP 校正后算出负数或跳变。
        self._started_at = time.monotonic()

    # ------------------------------------------------------------------
    # 健康检查用的聚合统计
    # ------------------------------------------------------------------

    def stats_snapshot(self) -> Dict[str, Any]:
        """返回一份用于健康检查/监控的**聚合**计数快照。

        故意只返回数量,不返回任何会话码(id)、对端 IP、时间戳等可用于
        定位具体用户的信息——/healthz 通常是公网可访问的,泄露等待中的
        会话码会直接让攻击者有机会抢注配对。

        字段说明:

        - ``status``:固定为 ``"ok"``,进程能响应就说明它活着。
        - ``waiting_hosts``:已注册但仍在等待 client 配对的 host 数量。
        - ``paired_sessions``:已经配对成功、正在转发数据的会话数量。
          ``_active_ids`` 同时包含"等待中"和"已配对"两类 id,因此把其中
          仍处于 ``_waiting`` 的部分刨掉,剩下的就是已配对会话。
        - ``active_connections``:当前进行中的 WebSocket 连接总数(一个已
          配对会话对应 2 条连接)。
        - ``uptime_seconds``:进程启动至今的秒数。
        """
        waiting_hosts = len(self._waiting)
        paired_sessions = sum(1 for sid in self._active_ids if sid not in self._waiting)
        return {
            "status": "ok",
            "waiting_hosts": waiting_hosts,
            "paired_sessions": paired_sessions,
            "active_connections": self.active_connection_count,
            "uptime_seconds": round(time.monotonic() - self._started_at, 3),
        }

    # ------------------------------------------------------------------
    # 后台清理任务:定期回收超过 10 分钟仍未配对的等待中 host
    # ------------------------------------------------------------------

    def start_background_tasks(self) -> None:
        if self._sweep_task is None:
            self._sweep_task = asyncio.ensure_future(self._sweep_loop())

    async def stop_background_tasks(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval)
            try:
                await self._sweep_expired_waiting_hosts()
            except Exception:  # pragma: no cover - 防御性:后台任务不应崩溃
                logger.exception("清理等待中 host 时发生异常")
            try:
                self._sweep_stale_register_attempts()
            except Exception:  # pragma: no cover - 防御性:后台任务不应崩溃
                logger.exception("清理注册限流记录时发生异常")

    def _sweep_stale_register_attempts(self) -> None:
        # _check_register_rate_limit 只会从 deque 里弹出过期的时间戳,不会
        # 删除已经清空的 IP 键本身;长期运行的 relay 进程如果被大量不同来源
        # IP 访问过(哪怕只访问一次),这个字典会无界增长。这里定期扫一遍,
        # 把窗口内已经没有任何有效时间戳的 IP 键彻底删除,释放内存。
        window_start = time.monotonic() - self.register_rate_limit_window
        stale_ips = [
            ip for ip, attempts in self._register_attempts.items()
            if not attempts or attempts[-1] < window_start
        ]
        for ip in stale_ips:
            del self._register_attempts[ip]

    async def _sweep_expired_waiting_hosts(self) -> None:
        now = time.monotonic()
        expired = [
            (session_id, entry)
            for session_id, entry in list(self._waiting.items())
            if now - entry.registered_at > self.pairing_wait_timeout
        ]
        for session_id, entry in expired:
            # 用身份比较避免和"恰好此时配对成功"发生竞态(配对会立刻从
            # self._waiting 中 pop 掉对应条目)。
            if self._waiting.get(session_id) is entry:
                logger.info("id=%s 等待配对超过 %.0f 秒,回收连接与 id", session_id, self.pairing_wait_timeout)
                del self._waiting[session_id]
                self._active_ids.discard(session_id)
                await self._safe_close(entry.websocket, 1000, "pairing_timeout")

    # ------------------------------------------------------------------
    # 按来源 IP 的注册频率限流
    # ------------------------------------------------------------------

    def _check_register_rate_limit(self, ip: str) -> bool:
        """返回 True 表示允许本次注册尝试,False 表示该 IP 已超过限流。"""
        if self.register_rate_limit_count <= 0:
            return True
        now = time.monotonic()
        window_start = now - self.register_rate_limit_window
        attempts = self._register_attempts.setdefault(ip, deque())
        while attempts and attempts[0] < window_start:
            attempts.popleft()
        if len(attempts) >= self.register_rate_limit_count:
            return False
        attempts.append(now)
        return True

    # ------------------------------------------------------------------
    # 连接入口
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_ip(websocket: Any) -> str:
        addr = getattr(websocket, "remote_address", None)
        if addr:
            try:
                return str(addr[0])
            except (TypeError, IndexError):
                pass
        return "unknown"

    async def handle_connection(self, websocket: Any) -> None:
        """处理一个已连接的 websocket,直到该连接结束。

        这是提供给 ``websockets.serve`` 的 handler,同时也是单元测试的
        主要切入点——只要传入的对象实现了 ``recv``/``send``/``close``/
        ``wait_closed``/``remote_address``,就不需要真正监听网络端口。
        """
        remote_ip = self._extract_ip(websocket)

        if self.max_connections is not None and self.active_connection_count >= self.max_connections:
            logger.warning("已达最大连接数 %d,拒绝来自 %s 的新连接", self.max_connections, remote_ip)
            await self._safe_close(websocket, 1013, "server_busy")
            return

        self.active_connection_count += 1
        try:
            if not self._check_register_rate_limit(remote_ip):
                logger.warning("ip=%s 注册尝试超过频率限制,拒绝连接", remote_ip)
                await self._safe_send(websocket, _error_payload("rate_limited"))
                await self._safe_close(websocket, 1008, "rate_limited")
                return

            registration = await self._read_registration(websocket)
            if registration is None:
                return

            role = registration["role"]
            session_id = registration["id"]

            if role == "host":
                await self._handle_host(websocket, session_id)
            else:
                await self._handle_client(websocket, session_id)
        except ConnectionClosed:
            pass
        except Exception:
            logger.exception("处理来自 %s 的连接时发生未预期错误", remote_ip)
            await self._safe_close(websocket, 1011, "internal_error")
        finally:
            self.active_connection_count -= 1

    # ------------------------------------------------------------------
    # 注册消息校验
    # ------------------------------------------------------------------

    async def _read_registration(self, websocket: Any) -> Optional[dict]:
        """读取并校验首条注册消息,失败时自行关闭连接并返回 None。"""
        remote_ip = self._extract_ip(websocket)
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.register_timeout)
        except asyncio.TimeoutError:
            logger.info("ip=%s 在 %.0f 秒内未发送注册消息,断开连接", remote_ip, self.register_timeout)
            await self._safe_close(websocket, 1008, "register_timeout")
            return None
        except ConnectionClosed:
            return None

        # 必须是文本帧
        if not isinstance(raw, str):
            logger.info("ip=%s 首条消息不是文本帧,断开连接", remote_ip)
            await self._safe_close(websocket, 1002, "invalid_registration")
            return None

        # 体积限制
        if len(raw.encode("utf-8")) > MAX_REGISTER_MESSAGE_BYTES:
            logger.info("ip=%s 注册消息超过 %d 字节,断开连接", remote_ip, MAX_REGISTER_MESSAGE_BYTES)
            await self._safe_close(websocket, 1009, "invalid_registration")
            return None

        # 必须是合法 JSON
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.info("ip=%s 注册消息不是合法 JSON,断开连接", remote_ip)
            await self._safe_close(websocket, 1002, "invalid_registration")
            return None

        if not isinstance(data, dict) or data.get("t") != "relay_register":
            logger.info("ip=%s 首条消息不是 relay_register,断开连接", remote_ip)
            await self._safe_close(websocket, 1002, "invalid_registration")
            return None

        session_id = data.get("id")
        role = data.get("role")

        if not isinstance(session_id, str) or not ID_RE.match(session_id):
            logger.info("ip=%s 提供的 id 不合法,断开连接", remote_ip)
            await self._safe_send(websocket, _error_payload("invalid_id"))
            await self._safe_close(websocket, 1008, "invalid_id")
            return None

        if role not in ("host", "client"):
            logger.info("ip=%s 提供的 role 不合法,断开连接", remote_ip)
            await self._safe_send(websocket, _error_payload("invalid_role"))
            await self._safe_close(websocket, 1008, "invalid_role")
            return None

        return {"id": session_id, "role": role}

    # ------------------------------------------------------------------
    # host / client 注册与配对
    # ------------------------------------------------------------------

    async def _handle_host(self, websocket: Any, session_id: str) -> None:
        if session_id in self._active_ids:
            logger.info("id=%s 已被占用,拒绝新的 host 注册", session_id)
            await self._safe_send(websocket, _error_payload("id_in_use"))
            await self._safe_close(websocket, 1008, "id_in_use")
            return

        entry = _WaitingHost(websocket=websocket, registered_at=time.monotonic())
        self._active_ids.add(session_id)
        self._waiting[session_id] = entry
        logger.info("id=%s host 注册成功,等待 client 配对", session_id)

        if not await self._safe_send(websocket, REGISTERED_MSG):
            self._discard_waiting(session_id, entry)
            return

        peer = await self._wait_for_pairing_or_disconnect(websocket, session_id, entry)
        if peer is None:
            return  # 配对前已断开(手动断开或等待超时被后台任务关闭)

        logger.info("id=%s host 配对成功,进入转发模式", session_id)
        if not await self._safe_send(websocket, PAIRED_MSG):
            await self._teardown_session(session_id, peer)
            return

        await self._forward_loop(websocket, peer, entry.limiter_host_to_client, session_id)

    async def _wait_for_pairing_or_disconnect(
        self, websocket: Any, session_id: str, entry: _WaitingHost
    ) -> Optional[Any]:
        closed_task = asyncio.ensure_future(websocket.wait_closed())
        paired_task = asyncio.ensure_future(entry.event.wait())
        try:
            done, _pending = await asyncio.wait(
                {closed_task, paired_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (closed_task, paired_task):
                if not task.done():
                    task.cancel()

        if paired_task not in done:
            logger.info("id=%s host 在配对前断开连接或被回收", session_id)
            self._discard_waiting(session_id, entry)
            return None
        return entry.peer

    def _discard_waiting(self, session_id: str, entry: _WaitingHost) -> None:
        if self._waiting.get(session_id) is entry:
            del self._waiting[session_id]
            self._active_ids.discard(session_id)

    async def _handle_client(self, websocket: Any, session_id: str) -> None:
        entry = self._waiting.pop(session_id, None)
        if entry is None:
            logger.info("id=%s 未找到等待中的 host,拒绝 client 配对", session_id)
            await self._safe_send(websocket, _error_payload("host_not_found"))
            await self._safe_close(websocket, 1008, "host_not_found")
            return

        entry.peer = websocket
        entry.limiter_host_to_client = _RateLimiter(self.forward_rate_limit_bytes_per_sec)
        limiter_client_to_host = _RateLimiter(self.forward_rate_limit_bytes_per_sec)
        entry.limiter_client_to_host = limiter_client_to_host
        entry.event.set()

        logger.info("id=%s client 配对成功,进入转发模式", session_id)
        if not await self._safe_send(websocket, PAIRED_MSG):
            await self._teardown_session(session_id, entry.websocket)
            return

        await self._forward_loop(websocket, entry.websocket, limiter_client_to_host, session_id)

    # ------------------------------------------------------------------
    # 纯转发模式
    # ------------------------------------------------------------------

    async def _forward_loop(
        self, own_ws: Any, peer_ws: Any, limiter: Optional[_RateLimiter], session_id: str
    ) -> None:
        try:
            while True:
                try:
                    message = await own_ws.recv()
                except ConnectionClosed:
                    break

                if isinstance(message, (bytes, bytearray)):
                    nbytes = len(message)
                else:
                    nbytes = len(message.encode("utf-8"))

                if limiter is not None and not limiter.allow(nbytes):
                    logger.debug("id=%s 转发超出限速,丢弃 %d 字节消息", session_id, nbytes)
                    continue

                try:
                    await peer_ws.send(message)
                except ConnectionClosed:
                    break
        finally:
            await self._teardown_session(session_id, peer_ws)

    async def _teardown_session(self, session_id: str, peer_ws: Any) -> None:
        # 无论哪一端先触发,都保证 id 只被释放一次;另一端会在它自己的
        # recv()/forward loop 中因连接被关闭而退出并再次调用本方法
        # (对 peer_ws 的重复 close 是安全的空操作)。
        if session_id in self._active_ids:
            self._active_ids.discard(session_id)
            logger.info("id=%s 会话结束,释放 id", session_id)
        await self._safe_close(peer_ws, 1000, "peer_disconnected")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe_send(websocket: Any, message: Any) -> bool:
        try:
            await websocket.send(message)
            return True
        except ConnectionClosed:
            return False
        except Exception:
            logger.exception("发送消息失败")
            return False

    @staticmethod
    async def _safe_close(websocket: Any, code: int = 1000, reason: str = "") -> None:
        try:
            await websocket.close(code, reason)
        except Exception:
            pass
