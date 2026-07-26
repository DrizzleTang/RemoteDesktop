"""Session 出站数据与握手路径的集成测试(用真实 Hub + 假采集器)。

其中"每条出站二进制消息都必须能解密"是一条**回归底线**:重构推流逻辑时
曾出现视频帧未加密就直接上线的缺陷,屏幕画面以明文传输。控制消息走的是
另一条会加密的路径,所以连接、鉴权、剪贴板等功能全部正常,单看功能测试
完全发现不了;客户端因解密失败静默丢帧,表现只是"画面不动"。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import crypto, protocol
from host.hub import SharedCaptureHub
from host.resume import ResumeTokenStore, derive_from_resume
from host.server import AuthRateLimiter, HostConfig, Session
from tests.fakes import FakeCapture, FakeWS

PASSWORD = "wire-test-password"


@pytest.fixture
async def env(monkeypatch, tmp_path):
    capture = FakeCapture()
    executor = ThreadPoolExecutor(max_workers=1)
    hub = SharedCaptureHub(capture, executor)
    ws = FakeWS()
    config = HostConfig(password=PASSWORD, session_id="TESTID01",
                        download_dir=tmp_path / "downloads",
                        show_cursor=False)  # 无显示环境,关掉光标采集
    store = ResumeTokenStore()
    session = Session(ws, config, hub, executor, AuthRateLimiter(), store,
                      "127.0.0.1", can_control=True)
    yield session, ws, hub, capture, store
    hub.close()


async def _wait(cond, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def _drive(session, ws, capture, *, wait_for_binary=3, timeout=5.0, codecs=None):
    """跑一次真实会话:完成握手,并持续制造画面变化直到收集够出站消息。"""
    task = asyncio.create_task(session.run())

    assert await _wait(lambda: ws.text_messages(), timeout), "host 未发出 kex_init"
    kex = protocol.parse_kex_text(ws.text_messages()[0])
    salt = crypto.b64d(kex["salt"])
    key = crypto.derive_session_key(password=PASSWORD, salt=salt, iterations=kex["iterations"])
    client_cipher = crypto.SessionCipher(key, aad=salt)

    hello = {"t": "hello"}
    if codecs is not None:
        hello["codecs"] = codecs
    await ws.incoming.put(client_cipher.encrypt(protocol.encode_control(hello)))

    deadline = asyncio.get_running_loop().time() + timeout
    while len(ws.binary_messages()) < wait_for_binary:
        if asyncio.get_running_loop().time() > deadline:
            break
        capture.mutate()  # 制造局部变化,保证持续有帧要发
        await asyncio.sleep(0.02)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return client_cipher


def _decode_all(ws, cipher):
    out = []
    for wire in ws.binary_messages():
        out.append(protocol.decode_plaintext(cipher.decrypt(bytes(wire))))
    return out


async def test_every_outbound_binary_message_is_encrypted(env):
    """核心回归用例:出站的每一条二进制消息都必须能被会话密钥解密。"""
    session, ws, hub, capture, _ = env
    cipher = await _drive(session, ws, capture)

    messages = ws.binary_messages()
    assert len(messages) >= 2, f"出站消息太少,无法验证(只有 {len(messages)} 条)"
    for i, wire in enumerate(messages):
        try:
            plaintext = cipher.decrypt(bytes(wire))
        except crypto.CryptoError as exc:
            pytest.fail(f"第 {i} 条出站消息无法解密,可能是漏加密直接上线: {exc}")
        kind, _ = protocol.decode_plaintext(plaintext)
        assert kind in (protocol.MSG_CONTROL, protocol.MSG_VIDEO_FRAME,
                        protocol.MSG_VIDEO_DELTA, protocol.MSG_FILE_CHUNK)


async def test_outbound_bytes_are_not_readable_as_plaintext_frames(env):
    """反向验证:漏加密时视频帧首字节恰好是 msg_kind、次字节是帧头 magic,
    会被 decode_plaintext 直接解析成功——这正是当初那个缺陷的特征。"""
    session, ws, hub, capture, _ = env
    await _drive(session, ws, capture)

    for wire in ws.binary_messages():
        raw = bytes(wire)
        assert not (raw[0] == protocol.MSG_VIDEO_FRAME and raw[1] == protocol.FRAME_MAGIC), \
            "检测到未加密的关键帧直接上线"
        assert not (raw[0] == protocol.MSG_VIDEO_DELTA and raw[1] == protocol.FRAME_MAGIC), \
            "检测到未加密的增量帧直接上线"


async def test_video_frames_actually_reach_the_wire(env):
    """确认画面帧确实被发出来了(否则上面两个用例可能因为"根本没发"而假通过)。"""
    session, ws, hub, capture, _ = env
    cipher = await _drive(session, ws, capture)
    kinds = [k for k, _ in _decode_all(ws, cipher)]
    assert protocol.MSG_VIDEO_FRAME in kinds, f"未发出任何画面帧,实际类型={kinds}"


async def test_hello_ack_reports_capabilities(env):
    session, ws, hub, capture, _ = env
    cipher = await _drive(session, ws, capture, wait_for_binary=2)
    for kind, msg in _decode_all(ws, cipher):
        if kind == protocol.MSG_CONTROL and msg.get("t") == protocol.T_HELLO_ACK:
            assert msg["width"] == 320 and msg["height"] == 240
            assert msg["can_control"] is True
            assert isinstance(msg["monitors"], list) and len(msg["monitors"]) == 2
            return
    pytest.fail("未收到 hello_ack")


async def test_codec_negotiation_selects_webp_when_supported(env):
    session, ws, hub, capture, _ = env
    cipher = await _drive(session, ws, capture, wait_for_binary=2, codecs=["webp", "jpeg"])
    for kind, msg in _decode_all(ws, cipher):
        if kind == protocol.MSG_CONTROL and msg.get("t") == protocol.T_HELLO_ACK:
            assert msg["codec"] == "webp"
            return
    pytest.fail("未收到 hello_ack")


async def test_codec_negotiation_falls_back_for_old_browsers(env):
    session, ws, hub, capture, _ = env
    cipher = await _drive(session, ws, capture, wait_for_binary=2, codecs=["jpeg"])
    for kind, msg in _decode_all(ws, cipher):
        if kind == protocol.MSG_CONTROL and msg.get("t") == protocol.T_HELLO_ACK:
            assert msg["codec"] == "jpeg"
            return
    pytest.fail("未收到 hello_ack")


async def test_unknown_codec_names_are_ignored(env):
    """客户端声明的编码格式来自不可信输入,未知名字必须被过滤掉。"""
    session, ws, hub, capture, _ = env
    cipher = await _drive(session, ws, capture, wait_for_binary=2,
                          codecs=["h265", "../../etc", "webp"])
    for kind, msg in _decode_all(ws, cipher):
        if kind == protocol.MSG_CONTROL and msg.get("t") == protocol.T_HELLO_ACK:
            assert msg["codec"] == "webp"
            return
    pytest.fail("未收到 hello_ack")


async def test_resume_token_is_issued_after_handshake(env):
    """握手成功后必须下发恢复令牌,否则重连还得重跑 PBKDF2。"""
    session, ws, hub, capture, store = env
    cipher = await _drive(session, ws, capture, wait_for_binary=3)
    for kind, msg in _decode_all(ws, cipher):
        if kind == protocol.MSG_CONTROL and msg.get("t") == protocol.T_RESUME_TOKEN:
            assert isinstance(msg["id"], str) and len(msg["id"]) >= 16
            assert len(crypto.b64d(msg["secret"])) == 32
            assert msg["ttl"] > 0
            return
    pytest.fail("未收到恢复令牌")


async def test_resume_path_skips_password_derivation(env):
    """用有效令牌重连时,双方直接用令牌派生密钥,不再需要密码。"""
    session, ws, hub, capture, store = env
    token_id, secret = store.issue()

    task = asyncio.create_task(session.run())
    assert await _wait(lambda: ws.text_messages())
    kex = protocol.parse_kex_text(ws.text_messages()[0])
    salt = crypto.b64d(kex["salt"])

    # 客户端先发明文 resume,再发用令牌密钥加密的 hello(全程不涉及密码)
    import json
    await ws.incoming.put(json.dumps({"t": protocol.KEX_RESUME, "id": token_id}))
    resume_cipher = crypto.SessionCipher(derive_from_resume(secret, salt), aad=salt)
    await ws.incoming.put(resume_cipher.encrypt(protocol.encode_control({"t": "hello"})))

    assert await _wait(lambda: ws.binary_messages()), "恢复路径未产生任何出站消息"
    kind, msg = protocol.decode_plaintext(resume_cipher.decrypt(bytes(ws.binary_messages()[0])))
    assert kind == protocol.MSG_CONTROL and msg["t"] == protocol.T_HELLO_ACK

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_resume_token_is_single_use(env):
    """同一个令牌不能用第二次,防止被截获后重复使用。"""
    session, ws, hub, capture, store = env
    token_id, secret = store.issue()
    assert store.consume(token_id) == secret
    assert store.consume(token_id) is None


async def test_invalid_resume_falls_back_to_password(env):
    """令牌过期/无效时必须退回密码认证,而不是直接断开。"""
    session, ws, hub, capture, store = env

    task = asyncio.create_task(session.run())
    assert await _wait(lambda: ws.text_messages())
    kex = protocol.parse_kex_text(ws.text_messages()[0])
    salt = crypto.b64d(kex["salt"])

    import json
    await ws.incoming.put(json.dumps({"t": protocol.KEX_RESUME, "id": "deadbeef" * 4}))
    # host 应回一条 resume_failed 提示,并改用密码密钥继续
    assert await _wait(lambda: len(ws.text_messages()) >= 2)
    reject = protocol.parse_kex_text(ws.text_messages()[1])
    assert reject["t"] == protocol.KEX_REJECT and reject["reason"] == "resume_failed"

    key = crypto.derive_session_key(password=PASSWORD, salt=salt, iterations=kex["iterations"])
    cipher = crypto.SessionCipher(key, aad=salt)
    await ws.incoming.put(cipher.encrypt(protocol.encode_control({"t": "hello"})))
    assert await _wait(lambda: ws.binary_messages()), "退回密码认证后应能正常握手"

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
