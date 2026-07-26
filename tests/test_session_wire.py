"""Session 出站数据的加密回归测试。

背景:重构推流逻辑时曾出现过一个严重缺陷——视频帧被直接放进发送队列而
没有加密,导致屏幕画面以明文上线。控制消息走的是另一条会加密的路径,所以
连接、鉴权、剪贴板等功能全部正常,单看功能测试完全发现不了;而客户端因为
解密失败会静默丢弃画面帧,表现只是"画面不动",极易被误判成性能问题。

因此这里直接盯住"线上字节":会话发出的每一条二进制消息都必须能用会话密钥
解密成功,且**不能**被当作明文协议帧解析出来。这是防止同类问题复发的底线。
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from common import crypto, protocol
from host.capture import CapturedFrame
from host.server import HostConfig, Session, AuthRateLimiter
from concurrent.futures import ThreadPoolExecutor

PASSWORD = "wire-test-password"


class FakeWS:
    """最小可用的 websocket 替身:记录出站消息,按脚本喂入入站消息。"""

    def __init__(self):
        self.sent: list = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.remote_address = ("127.0.0.1", 51234)

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return await self.incoming.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        return await self.incoming.get()

    async def close(self):
        self.closed = True

    def binary_messages(self):
        return [m for m in self.sent if isinstance(m, (bytes, bytearray))]

    def text_messages(self):
        return [m for m in self.sent if isinstance(m, str)]


class FakeCapture:
    def __init__(self, monitor_index=1):
        self.monitor_index = monitor_index

    def list_monitors(self):
        from host.capture import MonitorInfo
        return [MonitorInfo(0, 0, 0, 320, 240, False, "全部显示器 (320x240)"),
                MonitorInfo(1, 0, 0, 320, 240, True, "显示器 1 (320x240)")]

    @property
    def screen_size(self):
        return 320, 240

    def grab(self, *, scale):
        out_w, out_h = max(1, round(320 * scale)), max(1, round(240 * scale))
        # 用随机像素,保证差分检测每帧都能检出变化,从而持续产生画面帧
        rgb = np.random.randint(0, 255, (out_h, out_w, 3), dtype=np.uint8)
        return CapturedFrame(rgb=rgb, width=out_w, height=out_h, capture_ms=0.5)

    def encode_jpeg(self, rgb, quality):
        return b"\xff\xd8FULL\xff\xd9"

    def encode_region(self, rgb, rect, quality):
        return b"\xff\xd8RECT\xff\xd9"

    def set_monitor(self, index):
        return 0 <= index <= 1

    def close(self):
        pass


@pytest.fixture
async def patched_session(monkeypatch, tmp_path):
    monkeypatch.setattr("host.server.ScreenCapture", FakeCapture)

    ws = FakeWS()
    config = HostConfig(password=PASSWORD, session_id="TESTID01",
                        download_dir=tmp_path / "downloads")
    session = Session(ws, config, ThreadPoolExecutor(max_workers=1),
                      AuthRateLimiter(), "127.0.0.1", can_control=True)
    return session, ws


async def _drive(session, ws, *, wait_for_binary: int, timeout: float = 5.0):
    """跑一次真实会话:完成握手,直到收集到足够多的出站二进制消息。"""
    task = asyncio.create_task(session.run())

    # 等 host 发出明文 kex_init
    deadline = asyncio.get_event_loop().time() + timeout
    while not ws.text_messages() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert ws.text_messages(), "host 未发出 kex_init"

    kex = protocol.parse_kex_text(ws.text_messages()[0])
    salt = crypto.b64d(kex["salt"])
    key = crypto.derive_session_key(password=PASSWORD, salt=salt, iterations=kex["iterations"])
    client_cipher = crypto.SessionCipher(key, aad=salt)

    # 客户端发加密的 hello
    await ws.incoming.put(client_cipher.encrypt(protocol.encode_control({"t": "hello"})))

    while len(ws.binary_messages()) < wait_for_binary and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return client_cipher


async def test_every_outbound_binary_message_is_encrypted(patched_session):
    """核心回归用例:出站的每一条二进制消息都必须能被会话密钥解密。"""
    session, ws = patched_session
    client_cipher = await _drive(session, ws, wait_for_binary=3)

    messages = ws.binary_messages()
    assert len(messages) >= 2, f"出站消息太少,无法验证(只有 {len(messages)} 条)"

    for i, wire in enumerate(messages):
        try:
            plaintext = client_cipher.decrypt(bytes(wire))
        except crypto.CryptoError as exc:
            pytest.fail(f"第 {i} 条出站消息无法解密,可能是漏加密直接上线: {exc}")
        # 解密后必须是合法的协议帧
        kind, _ = protocol.decode_plaintext(plaintext)
        assert kind in (protocol.MSG_CONTROL, protocol.MSG_VIDEO_FRAME,
                        protocol.MSG_VIDEO_DELTA, protocol.MSG_FILE_CHUNK)


async def test_outbound_bytes_are_not_readable_as_plaintext_frames(patched_session):
    """反向验证:出站字节不应能被直接当作明文协议帧解析出来。

    漏加密时,视频帧的首字节恰好是 msg_kind(0x02)且第二字节是帧头 magic
    (0xF1),会被 decode_plaintext 直接解析成功——这正是当初那个缺陷的特征。
    """
    session, ws = patched_session
    await _drive(session, ws, wait_for_binary=3)

    for wire in ws.binary_messages():
        raw = bytes(wire)
        assert not (raw[0] == protocol.MSG_VIDEO_FRAME and raw[1] == protocol.FRAME_MAGIC), \
            "检测到未加密的关键帧直接上线"
        assert not (raw[0] == protocol.MSG_VIDEO_DELTA and raw[1] == protocol.FRAME_MAGIC), \
            "检测到未加密的增量帧直接上线"


async def test_video_frames_actually_reach_the_wire(patched_session):
    """确认画面帧确实被发出来了(否则上面两个用例可能因为"根本没发"而假通过)。"""
    session, ws = patched_session
    client_cipher = await _drive(session, ws, wait_for_binary=3)

    kinds = []
    for wire in ws.binary_messages():
        plaintext = client_cipher.decrypt(bytes(wire))
        kinds.append(protocol.decode_plaintext(plaintext)[0])
    assert protocol.MSG_VIDEO_FRAME in kinds, f"未发出任何画面帧,实际类型={kinds}"


async def test_hello_ack_reports_capabilities(patched_session):
    session, ws = patched_session
    client_cipher = await _drive(session, ws, wait_for_binary=2)

    for wire in ws.binary_messages():
        kind, msg = protocol.decode_plaintext(client_cipher.decrypt(bytes(wire)))
        if kind == protocol.MSG_CONTROL and msg.get("t") == protocol.T_HELLO_ACK:
            assert msg["width"] == 320 and msg["height"] == 240
            assert msg["can_control"] is True
            assert isinstance(msg["monitors"], list) and len(msg["monitors"]) == 2
            return
    pytest.fail("未收到 hello_ack")
