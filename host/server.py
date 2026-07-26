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
后续的密钥交换、加密、收发逻辑完全一致,由 Session 类统一处理;而具体的
画面推流策略在 host/streamer.py 里。

多人观看:直连模式下可以允许额外的"只读观看者"接入(默认关闭,用
--max-viewers 开启)。第一个连上的会话持有操作权,其余会话只能看画面,
所有输入类消息都会被忽略。持有操作权的一方断开后,操作权自动移交给
仍在线的最早的观看者。中转模式受限于 relay 的一对一配对语义,只支持
一个会话。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed

from common import crypto, protocol
from common.adaptive import AdaptiveController
from common.protocol import (
    T_BYE,
    T_CLIPBOARD,
    T_FILE_ABORT,
    T_FILE_BEGIN,
    T_FILE_DONE,
    T_FILE_END,
    T_FILE_ERROR,
    T_FRAME_ACK,
    T_HELLO,
    T_HELLO_ACK,
    T_KEY,
    T_MONITOR_INFO,
    T_MONITOR_SET,
    T_MOUSE_BUTTON,
    T_MOUSE_MOVE,
    T_MOUSE_SCROLL,
    T_PING,
    T_PONG,
    T_QUALITY_SET,
    T_STATS,
    T_VIEWER_INFO,
)
from host.capture import ScreenCapture
from host.clipboard import ClipboardSync
from host.delta import DirtyTracker
from host.filetransfer import FileReceiver, FileTransferError
from host.input_injector import InputInjector, InputUnavailableError
from host.sendqueue import PRIORITY_CONTROL, PrioritySendQueue
from host.streamer import FrameStreamer

logger = logging.getLogger("host.server")

STATS_INTERVAL_S = 1.0
HELLO_TIMEOUT_S = 8.0
MAX_FAILED_AUTH_PER_WINDOW = 5
AUTH_WINDOW_S = 60.0
AUTH_BLOCK_S = 120.0
# 中转模式下,host 侧看到的"来源 IP"其实是中转服务器自己的地址,不是真正
# 发起连接的一方——relay 只透传密文,没有办法把真实来源信息可信地转交
# 给 host。也就是说,中转模式下这套限速本质上是"按这个 host 的中转身份
# 整体限速",而不是真正意义上的"按攻击者身份限速":一旦触发封禁,会连带
# 挡住此后经由同一中转服务器过来的所有人(包括真正的机主)。因此中转模式
# 下用一个明显更短的封禁时长,在"拖慢暴力破解"与"少殃及正常用户"之间
# 折中(直连模式下 peer_ip 是真实客户端地址,限速判断准确,继续用完整的
# AUTH_BLOCK_S)。详见 docs/architecture.md "安全设计取舍说明"。
RELAY_AUTH_BLOCK_S = 15.0
MAX_WS_MESSAGE_BYTES = 8 * 1024 * 1024
CLIPBOARD_MIN_APPLY_INTERVAL_S = 0.3  # 限制 clip 消息处理频率,避免被用来刷剪贴板子进程


@dataclass
class HostConfig:
    password: str
    session_id: str
    bind_host: str = "0.0.0.0"
    port: int = 8765
    relay_url: str | None = None
    monitor_index: int = 1
    host_name: str = "host"
    download_dir: Path = field(default_factory=lambda: Path.home() / "RemoteDesktop-收到的文件")
    max_viewers: int = 1  # 允许同时连接的会话总数(1 = 仅一个操作者,无额外观看者)
    allow_file_transfer: bool = True


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

    def record_failure(self, ip: str, block_seconds: float = AUTH_BLOCK_S) -> None:
        now = time.monotonic()
        window_start = now - AUTH_WINDOW_S
        attempts = [t for t in self._failures.get(ip, []) if t >= window_start]
        attempts.append(now)
        self._failures[ip] = attempts
        if len(attempts) >= MAX_FAILED_AUTH_PER_WINDOW:
            self._blocked_until[ip] = now + block_seconds
            logger.warning("IP %s 短时间内认证失败次数过多,临时封禁 %.0f 秒", ip, block_seconds)

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)


def _extract_ip(ws) -> str:
    try:
        return ws.remote_address[0]
    except Exception:  # noqa: BLE001
        return "unknown"


class Session:
    """一次 host<->client 连接的完整生命周期:握手、鉴权,以及后续的
    画面推流 / 输入注入 / 剪贴板 / 文件接收 / 心跳收发。"""

    def __init__(self, ws, config: HostConfig, capture_executor: ThreadPoolExecutor,
                 rate_limiter: AuthRateLimiter, peer_ip: str, can_control: bool):
        self.ws = ws
        self.config = config
        self.capture_executor = capture_executor
        self.rate_limiter = rate_limiter
        self.peer_ip = peer_ip
        self.can_control = can_control

        self.cipher: crypto.SessionCipher | None = None
        self.adaptive = AdaptiveController()
        self.capture = ScreenCapture(monitor_index=config.monitor_index)
        self.injector: InputInjector | None = None
        self.clipboard: ClipboardSync | None = None
        self.files: FileReceiver | None = None
        self.streamer: FrameStreamer | None = None

        self.send_queue = PrioritySendQueue()
        self._last_clipboard_apply_ts = 0.0
        self._start_ts = time.monotonic()
        self._loop = asyncio.get_running_loop()
        self._closed = False

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now_ms(self) -> int:
        return int((time.monotonic() - self._start_ts) * 1000)

    def _enqueue_wire(self, wire_bytes: bytes, priority: int = PRIORITY_CONTROL) -> None:
        """把**已加密**的字节放入发送队列。"""
        self.send_queue.put_nowait(wire_bytes, priority)

    def _send_plaintext(self, plaintext: bytes, priority: int = PRIORITY_CONTROL) -> None:
        """加密一段明文 payload 后入队。

        所有出站数据都必须经过这里(或等价的加密步骤)——包括视频帧。
        画面内容是本项目最敏感的数据,一旦漏加密,中转服务器就能直接看到
        对方的屏幕,端到端加密的设计前提就被破坏了。
        """
        if self.cipher is None:
            return
        self._enqueue_wire(self.cipher.encrypt(plaintext), priority)

    def _send_control_nowait(self, msg: dict) -> None:
        try:
            self._send_plaintext(protocol.encode_control(msg), PRIORITY_CONTROL)
        except protocol.ProtocolError:
            logger.debug("控制消息过大,已丢弃: %s", msg.get("t"))

    async def _send_control(self, msg: dict) -> None:
        self._send_control_nowait(msg)

    # ------------------------------------------------------------------
    # 握手
    # ------------------------------------------------------------------

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

        monitors = await self._loop.run_in_executor(self.capture_executor, self.capture.list_monitors)
        width, height = await self._loop.run_in_executor(
            self.capture_executor, lambda: self.capture.screen_size
        )

        input_ok = False
        if self.can_control:
            try:
                self.injector = InputInjector(width, height)
                input_ok = True
            except InputUnavailableError as exc:
                logger.warning("输入注入不可用(仅能观看画面,无法操作): %s", exc)
                self.injector = None

        if self.can_control and self.config.allow_file_transfer:
            self.files = FileReceiver(self.config.download_dir)

        await self._send_control({
            "t": T_HELLO_ACK,
            "width": width, "height": height,
            "host_name": self.config.host_name,
            "input_available": input_ok,
            "can_control": self.can_control,
            "file_transfer": self.files is not None,
            "monitors": [
                {"index": m.index, "label": m.label, "width": m.width,
                 "height": m.height, "primary": m.is_primary}
                for m in monitors
            ],
            "current_monitor": self.capture.monitor_index,
        })

        self.clipboard = ClipboardSync(on_local_change=self._on_local_clipboard_change)
        self.clipboard.start()

        self.streamer = FrameStreamer(
            capture=self.capture, tracker=DirtyTracker(), adaptive=self.adaptive,
            executor=self.capture_executor, send=self._send_plaintext, now_ms=self._now_ms,
        )

    # ------------------------------------------------------------------
    # 剪贴板
    # ------------------------------------------------------------------

    def _on_local_clipboard_change(self, text: str) -> None:
        """由剪贴板轮询线程调用,需要跨线程投递到事件循环。"""
        if self.cipher is None:
            return
        try:
            payload = self.cipher.encrypt(protocol.encode_control({"t": T_CLIPBOARD, "text": text}))
        except Exception:  # noqa: BLE001
            return
        self._loop.call_soon_threadsafe(self._enqueue_wire, payload, PRIORITY_CONTROL)

    async def _on_clipboard_message(self, msg: dict) -> None:
        text = msg.get("text", "")
        if not isinstance(text, str) or not self.clipboard:
            return
        now = time.monotonic()
        if now - self._last_clipboard_apply_ts < CLIPBOARD_MIN_APPLY_INTERVAL_S:
            return  # 简单限流:防止刷 clip 消息导致反复拉起剪贴板子进程
        self._last_clipboard_apply_ts = now
        # pyperclip 在 Linux 下通过子进程(xclip/xsel)读写剪贴板,是阻塞调用,
        # 必须放到线程池里执行,避免卡住 asyncio 事件循环(进而卡住整条会话)。
        await self._loop.run_in_executor(None, self.clipboard.apply_remote, text)

    # ------------------------------------------------------------------
    # 收发循环
    # ------------------------------------------------------------------

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
            if kind == protocol.MSG_CONTROL:
                await self._dispatch_control(msg)
            elif kind == protocol.MSG_FILE_CHUNK:
                await self._on_file_chunk(msg)

    async def _dispatch_control(self, msg: dict) -> None:
        t = msg.get("t")
        try:
            # ---- 所有会话(含只读观看者)都能用的消息 ----
            if t == T_PING:
                await self._send_control({
                    "t": T_PONG, "ts": msg.get("ts"), "n": msg.get("n"), "server_ts": self._now_ms(),
                })
                return
            if t == T_FRAME_ACK:
                if self.streamer:
                    self.streamer.on_frame_ack(msg.get("seq"))
                return
            if t == T_QUALITY_SET:
                self._on_quality_set(msg)
                return
            if t == T_BYE:
                await self._safe_close()
                return

            # ---- 以下消息需要操作权 ----
            if not self.can_control:
                return

            if t == T_MOUSE_MOVE:
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
                await self._on_clipboard_message(msg)
            elif t == T_MONITOR_SET:
                await self._on_monitor_set(msg)
            elif t == T_FILE_BEGIN:
                await self._on_file_begin(msg)
            elif t == T_FILE_END:
                await self._on_file_end(msg)
            elif t == T_FILE_ABORT:
                await self._on_file_abort(msg)
        except InputUnavailableError:
            pass
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug("忽略格式错误的控制消息 %s: %s", t, exc)
        except Exception:  # noqa: BLE001 - 输入注入库(pynput/mss/pyperclip)可能抛出
            # 各种非 KeyError/ValueError/TypeError 的平台相关异常(例如 pynput 在
            # 某些按键释放路径上会抛出继承自 Exception 的 InvalidKeyException);
            # 一条畸形/极端的控制消息不应该把整个会话的 recv_loop 直接打崩。
            logger.exception("处理控制消息 %s 时发生未预期异常,已忽略", t)

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

    # ------------------------------------------------------------------
    # 显示器切换
    # ------------------------------------------------------------------

    async def _on_monitor_set(self, msg: dict) -> None:
        index = msg.get("index")
        if not isinstance(index, int):
            return
        ok = await self._loop.run_in_executor(self.capture_executor, self.capture.set_monitor, index)
        if not ok:
            await self._send_control({"t": T_MONITOR_INFO, "ok": False, "reason": "显示器编号无效"})
            return
        width, height = await self._loop.run_in_executor(
            self.capture_executor, lambda: self.capture.screen_size
        )
        if self.injector:
            self.injector.update_screen_size(width, height)
        if self.streamer:
            # 画面尺寸/内容整个换了,必须重置差分状态并强制发一个整帧
            self.streamer.request_keyframe()
        await self._send_control({
            "t": T_MONITOR_INFO, "ok": True, "index": index, "width": width, "height": height,
        })
        logger.info("已切换到显示器 %d (%dx%d)", index, width, height)

    # ------------------------------------------------------------------
    # 文件接收
    # ------------------------------------------------------------------

    async def _on_file_begin(self, msg: dict) -> None:
        if self.files is None:
            await self._send_control({"t": T_FILE_ERROR, "id": msg.get("id"), "message": "被控端已禁用文件传输"})
            return
        transfer_id, name, size = msg.get("id"), msg.get("name"), msg.get("size")
        try:
            await self._loop.run_in_executor(None, self.files.begin, transfer_id, name, size)
        except FileTransferError as exc:
            await self._send_control({"t": T_FILE_ERROR, "id": transfer_id, "message": str(exc)})
            return
        logger.info("开始接收文件: %s (%s 字节)", name, size)
        # 回一条 received=0 的进度,作为"已就绪、可以开始发分块"的确认
        await self._send_control({"t": protocol.T_FILE_PROGRESS, "id": transfer_id, "received": 0})

    async def _on_file_chunk(self, chunk) -> None:
        if not self.can_control or self.files is None:
            return
        try:
            received = await self._loop.run_in_executor(
                None, self.files.write_chunk, chunk.transfer_id, chunk.seq, chunk.data
            )
        except FileTransferError as exc:
            await self._send_control({"t": T_FILE_ERROR, "id": chunk.transfer_id, "message": str(exc)})
            await self._loop.run_in_executor(None, self.files.abort, chunk.transfer_id)
            return
        # 每 16 块回一次进度,避免进度消息本身占用过多带宽
        if chunk.seq % 16 == 0:
            await self._send_control({
                "t": protocol.T_FILE_PROGRESS, "id": chunk.transfer_id, "received": received,
            })

    async def _on_file_end(self, msg: dict) -> None:
        if self.files is None:
            return
        transfer_id = msg.get("id")
        try:
            path = await self._loop.run_in_executor(None, self.files.finish, transfer_id)
        except FileTransferError as exc:
            await self._send_control({"t": T_FILE_ERROR, "id": transfer_id, "message": str(exc)})
            return
        logger.info("文件接收完成: %s", path)
        await self._send_control({"t": T_FILE_DONE, "id": transfer_id, "path": path.name})

    async def _on_file_abort(self, msg: dict) -> None:
        if self.files is None:
            return
        await self._loop.run_in_executor(None, self.files.abort, msg.get("id"))

    # ------------------------------------------------------------------
    # 统计上报
    # ------------------------------------------------------------------

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_S)
            snap = self.adaptive.stats_snapshot()
            if self.streamer:
                snap.update(self.streamer.stats())
            await self._send_control({"t": T_STATS, **snap})

    def notify_control_granted(self) -> None:
        """由 HostServer 在操作权移交给本会话时调用。"""
        self.can_control = True
        try:
            width, height = self.capture.screen_size
            self.injector = InputInjector(width, height)
        except (InputUnavailableError, Exception):  # noqa: BLE001
            self.injector = None
        if self.config.allow_file_transfer and self.files is None:
            self.files = FileReceiver(self.config.download_dir)
        self._send_control_nowait({
            "t": T_VIEWER_INFO, "can_control": True,
            "input_available": self.injector is not None,
            "file_transfer": self.files is not None,
        })

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

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
            block_s = RELAY_AUTH_BLOCK_S if self.config.relay_url else AUTH_BLOCK_S
            self.rate_limiter.record_failure(self.peer_ip, block_seconds=block_s)
            await self._safe_close()
            return
        except (asyncio.TimeoutError, protocol.ProtocolError, ConnectionClosed, OSError) as exc:
            logger.info("握手未完成(来源 %s): %s", self.peer_ip, exc)
            await self._safe_close()
            return

        self.rate_limiter.record_success(self.peer_ip)
        logger.info("会话已建立,来源 %s(%s)", self.peer_ip, "操作者" if self.can_control else "只读观看")

        tasks = [
            asyncio.create_task(self._writer_loop(), name="writer"),
            asyncio.create_task(self._recv_loop(), name="recv"),
            asyncio.create_task(self.streamer.run(), name="frame"),
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
            if self.files:
                self.files.cleanup_all()
            # capture_executor 是单线程池,任务严格按提交顺序执行;取消推流
            # 协程只是不再提交新的采集调用,并不能中断一次已经在执行中的调用。
            # 这里提交一个空任务并等待它完成,借助"单线程 FIFO"的性质确保
            # 上一次采集已经结束,再去关闭底层的 mss 实例,避免跨线程并发访问。
            await self._loop.run_in_executor(self.capture_executor, lambda: None)
            self.capture.close()
            await self._safe_close()
            logger.info("会话已结束,来源 %s", self.peer_ip)


class HostServer:
    def __init__(self, config: HostConfig):
        self.config = config
        self.rate_limiter = AuthRateLimiter()
        self.capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
        self._sessions: list[Session] = []

    async def _reject(self, ws, reason: str) -> None:
        try:
            await ws.send(json.dumps({"t": protocol.KEX_REJECT, "reason": reason}, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    def _max_sessions(self) -> int:
        # 中转模式下 relay 是一对一配对的,多观看者无从谈起
        if self.config.relay_url:
            return 1
        return max(1, self.config.max_viewers)

    async def _handle_ws(self, ws) -> None:
        peer_ip = _extract_ip(ws)
        if self.rate_limiter.is_blocked(peer_ip):
            logger.info("拒绝来源 %s:近期认证失败次数过多", peer_ip)
            await self._reject(ws, "rate_limited")
            return
        if len(self._sessions) >= self._max_sessions():
            logger.info("拒绝来源 %s:已达最大会话数 %d", peer_ip, self._max_sessions())
            await self._reject(ws, "busy")
            return

        # 当前没有任何会话持有操作权时,新会话即为操作者
        can_control = not any(s.can_control for s in self._sessions)
        session = Session(ws, self.config, self.capture_executor, self.rate_limiter, peer_ip, can_control)
        self._sessions.append(session)
        try:
            await session.run()
        finally:
            if session in self._sessions:
                self._sessions.remove(session)
            if session.can_control:
                self._promote_next_controller()

    def _promote_next_controller(self) -> None:
        """操作者离开后,把操作权移交给仍在线的最早的观看者。

        所有会话都通过了同一个密码认证,彼此信任等级相同,因此这种自动移交
        不会带来额外的权限提升风险。"""
        for session in self._sessions:
            if not session.can_control:
                logger.info("操作权已移交给来源 %s", session.peer_ip)
                session.notify_control_granted()
                return

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
