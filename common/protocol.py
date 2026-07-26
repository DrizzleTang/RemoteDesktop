"""
应用层通信协议定义。

设计目标(详见 docs/architecture.md):
- 握手阶段(密钥交换 kex_init / kex_reply)使用明文 JSON 文本帧,
  因为此时双方尚未协商出会话密钥。
- 握手完成后,所有消息(无论控制信令还是视频帧)一律封装为二进制帧,
  并使用 AES-256-GCM 加密(见 common/crypto.py),中转服务器 relay 只
  转发不可读的密文字节,天然具备端到端加密属性。

二进制帧(加密前的明文)结构:
    [1 字节 msg_kind][payload]

msg_kind:
    MSG_CONTROL (0x01)     -> payload 是 UTF-8 JSON 字节串,字段 "t" 表示消息类型
    MSG_VIDEO_FRAME (0x02) -> payload 是 FRAME_HEADER_STRUCT 头部 + 图像二进制数据

视频帧头部(FRAME_HEADER_STRUCT, 16 字节, 大端):
    magic(1)=0xF1, seq(uint32), ts_ms(uint32, 会话相对时间戳),
    width(uint16), height(uint16), quality(uint8, 1-100),
    fmt(uint8, 0=JPEG 1=WEBP), flags(uint8, bit0=关键帧/完整帧)
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1

# ---- 明文握手阶段消息类型(仅用于 kex 之前,走 WebSocket 文本帧)----
# 握手只有 host -> client 单向的 kex_init(内含 salt + KDF 参数);client 收到后
# 直接本地派生密钥,无需再回复握手消息,详见 common/crypto.py 顶部说明。
KEX_INIT = "kex_init"
KEX_REJECT = "kex_reject"  # relay/host 侧在无法配对或版本不兼容/繁忙时明文告知原因

# ---- 加密后二进制帧的一级分类 ----
MSG_CONTROL = 0x01
MSG_VIDEO_FRAME = 0x02

# ---- MSG_CONTROL JSON 内的 "t" 字段取值 ----
T_HELLO = "hello"
T_HELLO_ACK = "hello_ack"
T_PING = "ping"
T_PONG = "pong"
T_FRAME_ACK = "frame_ack"
T_MOUSE_MOVE = "mmove"
T_MOUSE_BUTTON = "mbtn"
T_MOUSE_SCROLL = "mscroll"
T_KEY = "key"
T_CLIPBOARD = "clip"
T_QUALITY_SET = "quality"
T_STATS = "stats"
T_ERROR = "error"
T_BYE = "bye"

# ---- 画质档位(智能模式在这些档位之间自动切换;手动模式由用户直接选定)----
QUALITY_AUTO = "auto"
QUALITY_CUSTOM = "custom"
QUALITY_PRESETS = ("extreme", "smooth", "balanced", "clear", "hd")  # 由低到高

FRAME_HEADER_STRUCT = struct.Struct(">BIIHHBBB")  # magic,seq,ts_ms,w,h,quality,fmt,flags
FRAME_MAGIC = 0xF1
FMT_JPEG = 0
FMT_WEBP = 1
FLAG_KEYFRAME = 0b0000_0001

MAX_CONTROL_MESSAGE_BYTES = 64 * 1024  # 控制消息(JSON)上限,防止异常输入拖垮解析
MAX_CLIPBOARD_CHARS = 64 * 1024  # 单次剪贴板同步的最大字符数


class ProtocolError(ValueError):
    """协议格式错误(收到无法解析/不合法的帧)。"""


def encode_control(msg: dict[str, Any]) -> bytes:
    """将控制消息字典编码为待加密的明文 payload(带 msg_kind 前缀)。"""
    body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_CONTROL_MESSAGE_BYTES:
        raise ProtocolError(f"control message too large: {len(body)} bytes")
    return bytes([MSG_CONTROL]) + body


def encode_video_frame(
    *, seq: int, ts_ms: int, width: int, height: int, quality: int,
    fmt: int, keyframe: bool, image_bytes: bytes,
) -> bytes:
    """将一帧视频数据编码为待加密的明文 payload(带 msg_kind 前缀)。"""
    flags = FLAG_KEYFRAME if keyframe else 0
    header = FRAME_HEADER_STRUCT.pack(
        FRAME_MAGIC, seq & 0xFFFFFFFF, ts_ms & 0xFFFFFFFF,
        width, height, quality, fmt, flags,
    )
    return bytes([MSG_VIDEO_FRAME]) + header + image_bytes


@dataclass
class VideoFrame:
    seq: int
    ts_ms: int
    width: int
    height: int
    quality: int
    fmt: int
    keyframe: bool
    image_bytes: bytes


def decode_plaintext(plaintext: bytes) -> tuple[int, dict[str, Any] | VideoFrame]:
    """解码解密后的明文帧,返回 (msg_kind, 内容)。"""
    if len(plaintext) < 1:
        raise ProtocolError("empty plaintext frame")
    kind = plaintext[0]
    body = plaintext[1:]
    if kind == MSG_CONTROL:
        if len(body) > MAX_CONTROL_MESSAGE_BYTES:
            raise ProtocolError("control message too large")
        try:
            msg = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"invalid control json: {exc}") from exc
        if not isinstance(msg, dict) or "t" not in msg:
            raise ProtocolError("control message missing 't' field")
        return kind, msg
    if kind == MSG_VIDEO_FRAME:
        if len(body) < FRAME_HEADER_STRUCT.size:
            raise ProtocolError("video frame header truncated")
        magic, seq, ts_ms, width, height, quality, fmt, flags = FRAME_HEADER_STRUCT.unpack(
            body[: FRAME_HEADER_STRUCT.size]
        )
        if magic != FRAME_MAGIC:
            raise ProtocolError("bad video frame magic")
        image_bytes = body[FRAME_HEADER_STRUCT.size :]
        return kind, VideoFrame(
            seq=seq, ts_ms=ts_ms, width=width, height=height,
            quality=quality, fmt=fmt, keyframe=bool(flags & FLAG_KEYFRAME),
            image_bytes=image_bytes,
        )
    raise ProtocolError(f"unknown msg_kind: {kind}")


def encode_kex_init(salt_b64: str, iterations: int) -> str:
    return json.dumps({
        "t": KEX_INIT, "v": PROTOCOL_VERSION,
        "salt": salt_b64, "kdf": "pbkdf2-sha256", "iterations": iterations,
    }, ensure_ascii=False)


def parse_kex_text(text: str) -> dict[str, Any]:
    if len(text) > 8192:
        raise ProtocolError("kex message too large")
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid kex json: {exc}") from exc
    if not isinstance(msg, dict) or "t" not in msg:
        raise ProtocolError("kex message missing 't' field")
    return msg
