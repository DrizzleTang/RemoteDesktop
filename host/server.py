"""
被控端 WebSocket 服务核心逻辑。

支持两种接入方式:
- 直连模式 (serve_direct):host 作为 WebSocket 服务端监听端口,client 直接连入。
  适合同一局域网,或已做好端口映射的场景。
- 中转模式 (serve_via_relay):host 作为 WebSocket 客户端主动连出到中转服务器
  并注册一个会话码,等待 client 通过同一个会话码经中转服务器配对。适合双方
  都在 NAT/防火墙之后、无法直连的场景——这正是"弱网+复杂网络环境"下最常见
  的情况。

无论哪种接入方式,配对成功后都会得到一个普通的 WebSocket 连接对象,
后续的密钥交换、加密、视频帧/输入/剪贴板收发逻辑完全一致,由 Session 类
统一处理。
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import websockets
from websockets.exceptions import ConnectionClosed

from common import crypto, protocol
from common.adaptive import AdaptiveController
from common.protocol import (
    T_BYE,
    T_CLIPBOARD,
    T_FRAME_ACK,
    T_HELLO,
    T_HELLO_ACK,
    T_KEY,
    T_MOUSE_BUTTON,
    T_MOUSE_MOVE,
    T_MOUSE_SCROLL,
    T_PING,
    T_PONG,
    T_QUALITY_SET,
    T_STATS,
)
from host.capture import ScreenCapture
from host.clipboard import ClipboardSync
from host.input_injector import InputInjector, InputUnavailableError

logger = logging.getLogger("host.server")

FRAME_ACK_BASE_TIMEOUT_S = 0.35
FRAME_ACK_MAX_TIMEOUT_S = 2.5
STATS_INTERVAL_S = 1.0
STATS_WINDOW_S = 5.0
HELLO_TIMEOUT_S = 8.0
MAX_FAILED_AUTH_PER_WINDOW = 5
AUTH_WINDOW_S = 60.0
AUTH_BLOCK_S = 120.0
MAX_WS_MESSAGE_BYTES = 8 * 1024 * 1024
SEND_QUEUE_MAXSIZE = 64


@dataclass
class HostConfig:
    password: str
    session_id: str
    bind_host: str = "0.0.0.0"
    port: int = 8765
    relay_url: str | None = None
    monitor_index: int = 1
    host_name: str = "host"


class AuthRateLimiter:
    """按来源 IP 限制握手失败次数,减缓密码暴力破解。"""

    def __init__(self):
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def is_blocked(self, ip: str) -> bool:
        until = self._blocked_until.get(ip)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._blocked_until[ip]
            return False
        return True

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        window_start = now - AUTH_WINDOW_S
        attempts = [t for t in self._failures.get(ip, []) if t >= window_start]
        attempts.append(now)
        self._failures[ip] = attempts
        if len(attempts) >= MAX_FAILED_AUTH_PER_WINDOW:
            self._blocked_until[ip] = now + AUTH_BLOCK_S
            logger.warning("IP %s 短时间内认证失败次数过多,临时封禁 %.0f 秒", ip, AUTH_BLOCK_S)

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)


def _extract_ip(ws) -> str:
    try:
        return ws.remote_address[0]
    except Exception:  # noqa: BLE001
        return "unknown"


class Session:
    """一次 host<->client 连接的完整生命周期:握手、鉴权、以及后续的
    视频帧 / 输入 / 剪贴板 / 心跳收发。"""

    def __init__(self, ws, config: HostConfig, capture_executor: ThreadPoolExecutor,
                 rate_limiter: AuthRateLimiter, peer_ip: str):
        self.ws = ws
        self.config = config
        self.capture_executor = capture_executor
        self.rate_limiter = rate_limiter
        self.peer_ip = peer_ip

        self.cipher: crypto.SessionCipher | None = None
        self.adaptive = AdaptiveController()
        self.capture = ScreenCapture(monitor_index=config.monitor_index)
        self.injector: InputInjector | None = None
        self.clipboard: ClipboardSync | None = None

        self.send_queue: asyncio.Queue = asyncio.Queue(maxsize=SEND_QUEUE_MAXSIZE)
        self._frame_seq = 0
        self._pending_frame_seq: int | None = None
        self._frame_sent_at: float = 0.0
        self._frame_ack_event = asyncio.Event()
        self._recent_frames: list[tuple[float, int]] = []  # (发送时间, 字节数),用于统计实际 fps/码率
        self._start_ts = time.monotonic()
        self._loop = asyncio.get_event_loop()

    def _now_ms(self) -> int:
        return int((time.monotonic() - self._start_ts) * 1000)

    async def _handshake(self) -> None:
        salt = crypto.new_salt()
        await self.ws.send(protocol.encode_kex_init(crypto.b64e(salt), crypto.PBKDF2_ITERATIONS))

        session_key = crypto.derive_session_key(password=self.config.password, salt=salt)
        self.cipher = crypto.SessionCipher(session_key, aad=salt)

        raw = await asyncio.wait_for(self.ws.recv(), timeout=HELLO_TIMEOUT_S)
        if not isinstance(raw, (bytes, bytearray)):
            raise protocol.ProtocolError("expected binary encrypted hello")
        plaintext = self.cipher.decrypt(bytes(raw))  # 密码错误会在此直接抛出 CryptoError
        kind, msg = protocol.decode_plaintext(plaintext)
        if kind != protocol.MSG_CONTROL or msg.get("t") != T_HELLO:
            raise protocol.ProtocolError("expected hello message")

        width, height = await self._loop.run_in_executor(self.capture_executor, lambda: self.capture.screen_size)
        try:
            self.injector = InputInjector(width, height)
            input_ok = True
        except InputUnavailableError as exc:
            logger.warning("输入注入不可用(仅能观看画面,无法操作): %s", exc)
            self.injector = None
            input_ok = False

        await self._send_control({
            "t": T_HELLO_ACK, "width": width, "height": height,
            "host_name": self.config.host_name, "input_available": input_ok,
        })

        self.clipboard = ClipboardSync(on_local_change=self._on_local_clipboard_change)
        self.clipboard.start()

    def _on_local_clipboard_change(self, text: str) -> None:
        try:
            payload = self.cipher.encrypt(protocol.encode_control({"t": T_CLIPBOARD, "text": text}))
        except Exception:  # noqa: BLE001
            return
        self._loop.call_soon_threadsafe(self._enqueue_nowait, payload)

    def _enqueue_nowait(self, payload: bytes) -> None:
        try:
            self.send_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.debug("发送队列已满,丢弃一条消息")

    async def _enqueue(self, payload: bytes) -> None:
        try:
            self.send_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.debug("发送队列已满,丢弃一条消息")

    async def _send_control(self, msg: dict) -> None:
        payload = self.cipher.encrypt(protocol.encode_control(msg))
        await self._enqueue(payload)

    def _current_ack_timeout(self) -> float:
        ewma = self.adaptive.ewma_latency_ms
        if ewma is None:
            return FRAME_ACK_BASE_TIMEOUT_S
        return min(FRAME_ACK_MAX_TIMEOUT_S, max(FRAME_ACK_BASE_TIMEOUT_S, (ewma / 1000) * 3))

    async def _writer_loop(self) -> None:
        while True:
            payload = await self.send_queue.get()
            await self.ws.send(payload)

    async def _recv_loop(self) -> None:
        async for raw in self.ws:
            if not isinstance(raw, (bytes, bytearray)):
                continue  # 握手完成后只处理二进制加密帧
            try:
                plaintext = self.cipher.decrypt(bytes(raw))
                kind, msg = protocol.decode_plaintext(plaintext)
            except (crypto.CryptoError, protocol.ProtocolError) as exc:
                logger.debug("丢弃无法解析的消息: %s", exc)
                continue
            if kind != protocol.MSG_CONTROL:
                continue
            await self._dispatch_control(msg)

    async def _dispatch_control(self, msg: dict) -> None:
        t = msg.get("t")
        try:
            if t == T_PING:
                await self._send_control({"t": T_PONG, "ts": msg.get("ts"), "n": msg.get("n"), "server_ts": self._now_ms()})
            elif t == T_FRAME_ACK:
                self._on_frame_ack(msg)
            elif t == T_MOUSE_MOVE:
                if self.injector:
                    self.injector.move(float(msg["x"]), float(msg["y"]))
            elif t == T_MOUSE_BUTTON:
                if self.injector:
                    self.injector.button(msg["btn"], bool(msg["down"]), msg.get("x"), msg.get("y"))
            elif t == T_MOUSE_SCROLL:
                if self.injector:
                    self.injector.scroll(float(msg.get("dx", 0)), float(msg.get("dy", 0)))
            elif t == T_KEY:
                if self.injector:
                    self.injector.key(code=msg.get("code"), key_char=msg.get("key"), down=bool(msg["down"]))
            elif t == T_CLIPBOARD:
                text = msg.get("text", "")
                if isinstance(text, str) and self.clipboard:
                    self.clipboard.apply_remote(text)
            elif t == T_QUALITY_SET:
                self._on_quality_set(msg)
            elif t == T_BYE:
                await self._safe_close()
        except InputUnavailableError:
            pass
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("忽略格式错误的控制消息 %s: %s", t, exc)

    def _on_frame_ack(self, msg: dict) -> None:
        if msg.get("seq") != self._pending_frame_seq:
            return  # 过期或重复的 ack
        latency_ms = max(0.0, (time.monotonic() - self._frame_sent_at) * 1000)
        self.adaptive.on_frame_ack(latency_ms)
        self._pending_frame_seq = None
        self._frame_ack_event.set()

    def _on_quality_set(self, msg: dict) -> None:
        mode = msg.get("mode")
        try:
            self.adaptive.set_mode(
                mode,
                custom_scale=msg.get("scale"),
                custom_jpeg_quality=msg.get("jpeg_q"),
                custom_max_fps=msg.get("max_fps"),
            )
        except ValueError:
            logger.debug("忽略非法画质模式: %s", mode)

    async def _frame_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            t0 = time.monotonic()
            params = self.adaptive.current_params()
            fn = functools.partial(
                self.capture.grab_and_encode, scale=params["scale"], jpeg_quality=int(params["jpeg_quality"]),
            )
            try:
                encoded = await loop.run_in_executor(self.capture_executor, fn)
            except Exception:  # noqa: BLE001
                logger.exception("屏幕采集失败,1 秒后重试")
                await asyncio.sleep(1.0)
                continue

            self._frame_seq += 1
            seq = self._frame_seq
            payload = protocol.encode_video_frame(
                seq=seq, ts_ms=self._now_ms(), width=encoded.width, height=encoded.height,
                quality=int(params["jpeg_quality"]), fmt=protocol.FMT_JPEG, keyframe=True,
                image_bytes=encoded.jpeg_bytes,
            )
            wire = self.cipher.encrypt(payload)

            self._pending_frame_seq = seq
            self._frame_ack_event.clear()
            self._frame_sent_at = time.monotonic()
            await self._enqueue(wire)

            now = time.monotonic()
            self._recent_frames.append((now, len(encoded.jpeg_bytes)))
            cutoff = now - STATS_WINDOW_S
            if len(self._recent_frames) > 256 or (self._recent_frames and self._recent_frames[0][0] < cutoff):
                self._recent_frames = [f for f in self._recent_frames if f[0] >= cutoff]

            try:
                await asyncio.wait_for(self._frame_ack_event.wait(), timeout=self._current_ack_timeout())
            except asyncio.TimeoutError:
                if self._pending_frame_seq == seq:
                    self._pending_frame_seq = None
                    self.adaptive.on_frame_timeout()

            target_fps = max(1.0, params["max_fps"])
            min_interval = 1.0 / target_fps
            elapsed = time.monotonic() - t0
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)

    def _actual_throughput(self) -> tuple[float, float]:
        now = time.monotonic()
        cutoff = now - STATS_WINDOW_S
        recent = [f for f in self._recent_frames if f[0] >= cutoff]
        if len(recent) < 2:
            return 0.0, 0.0
        span = max(0.5, now - recent[0][0])
        fps = len(recent) / span
        kbps = sum(b for _, b in recent) * 8 / 1000 / span
        return fps, kbps

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_S)
            snap = self.adaptive.stats_snapshot()
            fps, kbps = self._actual_throughput()
            snap["actual_fps"] = round(fps, 1)
            snap["actual_kbps"] = round(kbps, 1)
            await self._send_control({"t": T_STATS, **snap})

    async def _safe_close(self) -> None:
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    async def run(self) -> None:
        try:
            await self._handshake()
        except crypto.CryptoError:
            logger.info("认证失败(密码错误),来源 %s", self.peer_ip)
            self.rate_limiter.record_failure(self.peer_ip)
            await self._safe_close()
            return
        except (asyncio.TimeoutError, protocol.ProtocolError, ConnectionClosed, OSError) as exc:
            logger.info("握手未完成(来源 %s): %s", self.peer_ip, exc)
            await self._safe_close()
            return

        self.rate_limiter.record_success(self.peer_ip)
        logger.info("会话已建立,来源 %s", self.peer_ip)

        tasks = [
            asyncio.create_task(self._writer_loop(), name="writer"),
            asyncio.create_task(self._recv_loop(), name="recv"),
            asyncio.create_task(self._frame_loop(), name="frame"),
            asyncio.create_task(self._stats_loop(), name="stats"),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.clipboard:
                self.clipboard.stop()
            self.capture.close()
            await self._safe_close()
            logger.info("会话已结束,来源 %s", self.peer_ip)


class HostServer:
    def __init__(self, config: HostConfig):
        self.config = config
        self.rate_limiter = AuthRateLimiter()
        self.capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
        self._session_active = False

    async def _reject(self, ws, reason: str) -> None:
        try:
            await ws.send(json.dumps({"t": protocol.KEX_REJECT, "reason": reason}, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    async def _handle_ws(self, ws) -> None:
        peer_ip = _extract_ip(ws)
        if self.rate_limiter.is_blocked(peer_ip):
            logger.info("拒绝来源 %s:近期认证失败次数过多", peer_ip)
            await self._reject(ws, "rate_limited")
            return
        if self._session_active:
            logger.info("拒绝来源 %s:当前已有活跃会话", peer_ip)
            await self._reject(ws, "busy")
            return
        self._session_active = True
        try:
            session = Session(ws, self.config, self.capture_executor, self.rate_limiter, peer_ip)
            await session.run()
        finally:
            self._session_active = False

    async def serve_direct(self) -> None:
        async with websockets.serve(
            self._handle_ws, self.config.bind_host, self.config.port, max_size=MAX_WS_MESSAGE_BYTES,
        ):
            logger.info("直连模式监听 %s:%s", self.config.bind_host, self.config.port)
            await asyncio.Future()

    async def serve_via_relay(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.config.relay_url, max_size=MAX_WS_MESSAGE_BYTES) as ws:
                    await ws.send(json.dumps(
                        {"t": "relay_register", "role": "host", "id": self.config.session_id, "v": 1},
                        ensure_ascii=False,
                    ))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if reply.get("t") != "relay_registered":
                        logger.error("中转注册失败: %s", reply.get("reason", reply))
                        await asyncio.sleep(5)
                        continue
                    logger.info("已在中转服务器注册会话码 %s,等待 client 连接...", self.config.session_id)

                    paired = json.loads(await ws.recv())
                    if paired.get("t") != "relay_paired":
                        logger.warning("未能与 client 配对: %s", paired)
                        continue
                    backoff = 1.0
                    logger.info("已通过中转服务器与 client 配对成功")
                    await self._handle_ws(ws)
            except (OSError, ConnectionClosed, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                logger.warning("中转连接异常,%.0f 秒后重试: %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    async def serve(self) -> None:
        if self.config.relay_url:
            await self.serve_via_relay()
        else:
            await self.serve_direct()
