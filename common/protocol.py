"""
应用层通信协议定义。

设计目标(详见 docs/architecture.md):
- 握手阶段(kex_init)使用明文 JSON 文本帧,因为此时双方尚未协商出会话密钥。
- 握手完成后,所有消息(控制信令、视频帧、文件分块)一律封装为二进制帧,
  并使用 AES-256-GCM 加密(见 common/crypto.py),中转服务器 relay 只
  转发不可读的密文字节,天然具备端到端加密属性。

二进制帧(加密前的明文)结构:
    [1 字节 msg_kind][payload]

msg_kind:
    MSG_CONTROL (0x01)     -> payload 是 UTF-8 JSON 字节串,字段 "t" 表示消息类型
    MSG_VIDEO_FRAME (0x02) -> 关键帧:FRAME_HEADER + 整幅图像数据
    MSG_VIDEO_DELTA (0x03) -> 增量帧:FRAME_HEADER + 矩形数 + 若干"脏矩形"
    MSG_FILE_CHUNK (0x04)  -> 文件分块:FILE_CHUNK_HEADER + 原始字节

视频帧头部(FRAME_HEADER_STRUCT, 16 字节, 大端):
    magic(1)=0xF1, seq(uint32), ts_ms(uint32, 会话相对时间戳),
    width(uint16), height(uint16), quality(uint8, 1-100),
    fmt(uint8, 0=JPEG 1=WEBP), flags(uint8, bit0=关键帧)

增量帧在头部之后是:
    rect_count(uint16) + rect_count 个 [RECT_HEADER_STRUCT][图像字节]
RECT_HEADER_STRUCT(12 字节, 大端): x(uint16) y(uint16) w(uint16) h(uint16) size(uint32)

其中 x/y/w/h 是"编码后输出图像"坐标系下的像素坐标(即与最近一个关键帧
声明的 width/height 同一坐标系),客户端直接把每个矩形画到 canvas 的
对应位置即可,无需做任何坐标换算。

文件分块头部(FILE_CHUNK_HEADER_STRUCT, 8 字节, 大端):
    transfer_id(uint32), seq(uint32)
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 3

# ---- 明文握手阶段消息类型(仅用于 kex 之前,走 WebSocket 文本帧)----
# 握手只有 host -> client 单向的 kex_init(内含 salt + KDF 参数);client 收到后
# 直接本地派生密钥,无需再回复握手消息,详见 common/crypto.py 顶部说明。
KEX_INIT = "kex_init"
KEX_REJECT = "kex_reject"
# 重连时客户端可以先发一条明文 resume(只含公开的 token_id),双方直接用
# 上次会话下发的 secret 派生密钥,跳过昂贵的 PBKDF2。host 收到 kex_init
# 之后的第一条消息若是文本 resume 就走这条路径,是二进制就走密码路径。
KEX_RESUME = "resume"  # relay/host 侧在无法配对或版本不兼容/繁忙时明文告知原因

# ---- 加密后二进制帧的一级分类 ----
MSG_CONTROL = 0x01
MSG_VIDEO_FRAME = 0x02
MSG_VIDEO_DELTA = 0x03
MSG_FILE_CHUNK = 0x04

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
# 显示器切换
T_MONITOR_SET = "monitor_set"
T_MONITOR_INFO = "monitor_info"
# 观看者身份变化(多人观看时:谁持有操作权)
T_VIEWER_INFO = "viewer_info"
# 远端鼠标光标(位置 + 形状)。光标单独走控制消息,不混进画面帧——
# 这样光标移动不必触发画面区域重传,反而更省带宽。
T_CURSOR = "cursor"
# 重连恢复令牌:握手成功后由 host 下发,重连时用它跳过昂贵的 PBKDF2
T_RESUME_TOKEN = "resume_token"
# 被控端 -> 主控端的文件下载(仅当 host 启用了 --share-dir)
T_SHARE_LIST_REQ = "share_list_req"
T_SHARE_LIST = "share_list"
T_SHARE_GET = "share_get"
# 文件传输(client -> host 上传)
T_FILE_BEGIN = "file_begin"
T_FILE_END = "file_end"
T_FILE_ABORT = "file_abort"
T_FILE_PROGRESS = "file_progress"
T_FILE_DONE = "file_done"
T_FILE_ERROR = "file_error"

# ---- 画质档位(智能模式在这些档位之间自动切换;手动模式由用户直接选定)----
QUALITY_AUTO = "auto"
QUALITY_CUSTOM = "custom"
QUALITY_PRESETS = ("extreme", "smooth", "balanced", "clear", "hd")  # 由低到高

FRAME_HEADER_STRUCT = struct.Struct(">BIIHHBBB")  # magic,seq,ts_ms,w,h,quality,fmt,flags
RECT_HEADER_STRUCT = struct.Struct(">HHHHI")  # x,y,w,h,size
FILE_CHUNK_HEADER_STRUCT = struct.Struct(">II")  # transfer_id, seq
FRAME_MAGIC = 0xF1
FMT_JPEG = 0
FMT_WEBP = 1
# 客户端在 hello 里声明自己能解码哪些格式,host 据此选择编码格式。
# WebP 在增量帧的小矩形上比 JPEG 省 80% 以上(JPEG 每张图有约 600 字节的
# 固定头部,小图上头部比数据还大),但整屏编码耗时明显更高,因此关键帧要
# 按"瓶颈在网络还是在 CPU"动态选择。Safari 14 以下不支持 WebP,必须回落。
CODEC_NAMES = {FMT_JPEG: "jpeg", FMT_WEBP: "webp"}
CODEC_IDS = {name: fmt for fmt, name in CODEC_NAMES.items()}
DEFAULT_CODECS = ("jpeg",)
FLAG_KEYFRAME = 0b0000_0001

MAX_CONTROL_MESSAGE_BYTES = 64 * 1024  # 控制消息(JSON)上限,防止异常输入拖垮解析
MAX_CLIPBOARD_CHARS = 64 * 1024  # 单次剪贴板同步的最大字符数
MAX_RECTS_PER_FRAME = 512  # 单个增量帧允许携带的最大矩形数
FILE_CHUNK_BYTES = 64 * 1024  # 文件传输分块大小


class ProtocolError(ValueError):
    """协议格式错误(收到无法解析/不合法的帧)。"""


def encode_control(msg: dict[str, Any]) -> bytes:
    """将控制消息字典编码为待加密的明文 payload(带 msg_kind 前缀)。"""
    body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_CONTROL_MESSAGE_BYTES:
        raise ProtocolError(f"control message too large: {len(body)} bytes")
    return bytes([MSG_CONTROL]) + body


def _pack_frame_header(*, seq: int, ts_ms: int, width: int, height: int,
                       quality: int, fmt: int, keyframe: bool) -> bytes:
    return FRAME_HEADER_STRUCT.pack(
        FRAME_MAGIC, seq & 0xFFFFFFFF, ts_ms & 0xFFFFFFFF,
        width, height, quality, fmt, FLAG_KEYFRAME if keyframe else 0,
    )


def encode_video_frame(
    *, seq: int, ts_ms: int, width: int, height: int, quality: int,
    fmt: int, keyframe: bool, image_bytes: bytes,
) -> bytes:
    """将一整幅画面(关键帧)编码为待加密的明文 payload。"""
    header = _pack_frame_header(
        seq=seq, ts_ms=ts_ms, width=width, height=height,
        quality=quality, fmt=fmt, keyframe=keyframe,
    )
    return bytes([MSG_VIDEO_FRAME]) + header + image_bytes


def encode_video_delta(
    *, seq: int, ts_ms: int, width: int, height: int, quality: int, fmt: int,
    rects: list[tuple[int, int, int, int, bytes]],
) -> bytes:
    """将若干"脏矩形"编码为增量帧。

    rects 的每一项是 (x, y, w, h, image_bytes),坐标位于输出图像坐标系。
    width/height 仍然是整幅输出图像的尺寸,供客户端校验自己的 canvas 是否一致。
    """
    if len(rects) > MAX_RECTS_PER_FRAME:
        raise ProtocolError(f"too many rects in one delta frame: {len(rects)}")
    parts = [
        bytes([MSG_VIDEO_DELTA]),
        _pack_frame_header(
            seq=seq, ts_ms=ts_ms, width=width, height=height,
            quality=quality, fmt=fmt, keyframe=False,
        ),
        struct.pack(">H", len(rects)),
    ]
    for x, y, w, h, image_bytes in rects:
        parts.append(RECT_HEADER_STRUCT.pack(x, y, w, h, len(image_bytes)))
        parts.append(image_bytes)
    return b"".join(parts)


def encode_file_chunk(*, transfer_id: int, seq: int, data: bytes) -> bytes:
    """将一个文件分块编码为待加密的明文 payload。"""
    return (
        bytes([MSG_FILE_CHUNK])
        + FILE_CHUNK_HEADER_STRUCT.pack(transfer_id & 0xFFFFFFFF, seq & 0xFFFFFFFF)
        + data
    )


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


@dataclass
class VideoDelta:
    seq: int
    ts_ms: int
    width: int
    height: int
    quality: int
    fmt: int
    rects: list[tuple[int, int, int, int, bytes]]


@dataclass
class FileChunk:
    transfer_id: int
    seq: int
    data: bytes


def _unpack_frame_header(body: bytes) -> tuple[tuple, bytes]:
    if len(body) < FRAME_HEADER_STRUCT.size:
        raise ProtocolError("video frame header truncated")
    fields = FRAME_HEADER_STRUCT.unpack(body[: FRAME_HEADER_STRUCT.size])
    if fields[0] != FRAME_MAGIC:
        raise ProtocolError("bad video frame magic")
    return fields, body[FRAME_HEADER_STRUCT.size :]


def decode_plaintext(
    plaintext: bytes,
) -> tuple[int, dict[str, Any] | VideoFrame | VideoDelta | FileChunk]:
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
        (_, seq, ts_ms, width, height, quality, fmt, flags), image_bytes = _unpack_frame_header(body)
        return kind, VideoFrame(
            seq=seq, ts_ms=ts_ms, width=width, height=height,
            quality=quality, fmt=fmt, keyframe=bool(flags & FLAG_KEYFRAME),
            image_bytes=image_bytes,
        )

    if kind == MSG_VIDEO_DELTA:
        (_, seq, ts_ms, width, height, quality, fmt, _flags), rest = _unpack_frame_header(body)
        if len(rest) < 2:
            raise ProtocolError("delta frame rect count truncated")
        (rect_count,) = struct.unpack(">H", rest[:2])
        if rect_count > MAX_RECTS_PER_FRAME:
            raise ProtocolError(f"too many rects in delta frame: {rect_count}")
        cursor = 2
        rects: list[tuple[int, int, int, int, bytes]] = []
        for _ in range(rect_count):
            if len(rest) < cursor + RECT_HEADER_STRUCT.size:
                raise ProtocolError("delta frame rect header truncated")
            x, y, w, h, size = RECT_HEADER_STRUCT.unpack(
                rest[cursor : cursor + RECT_HEADER_STRUCT.size]
            )
            cursor += RECT_HEADER_STRUCT.size
            if len(rest) < cursor + size:
                raise ProtocolError("delta frame rect payload truncated")
            rects.append((x, y, w, h, rest[cursor : cursor + size]))
            cursor += size
        return kind, VideoDelta(
            seq=seq, ts_ms=ts_ms, width=width, height=height,
            quality=quality, fmt=fmt, rects=rects,
        )

    if kind == MSG_FILE_CHUNK:
        if len(body) < FILE_CHUNK_HEADER_STRUCT.size:
            raise ProtocolError("file chunk header truncated")
        transfer_id, seq = FILE_CHUNK_HEADER_STRUCT.unpack(
            body[: FILE_CHUNK_HEADER_STRUCT.size]
        )
        return kind, FileChunk(
            transfer_id=transfer_id, seq=seq, data=body[FILE_CHUNK_HEADER_STRUCT.size :]
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
