"""
会话加密:基于口令派生密钥 + AES-256-GCM 端到端加密。

握手流程(host 与 client 双方对称执行,relay 只转发密文/握手消息,
无法解密任何内容 —— 真正的口令永远不经过网络传输):

    1. host(在直连模式下是接受连接的一方;在中转模式下是配对成功后的一方)
       生成一个随机 salt,发送明文 kex_init 消息(仅含 salt 与 KDF 参数)。
    2. 双方各自独立计算:
           password_key  = PBKDF2-HMAC-SHA256(密码, salt, iterations)
           session_key   = HKDF-SHA256(password_key, salt, info)
       只有双方密码一致时,session_key 才会相同。
    3. client 收到 kex_init 后无需再回复握手消息,直接发送第一条加密的
       hello 控制消息。后续所有消息(控制信令与视频帧)均用 session_key
       做 AES-256-GCM 加解密。密码错误时,AEAD 解密会直接失败(认证标签
       不匹配),这本身就是隐式的身份验证,无需再单独设计一轮"验证密码
       是否正确"的消息。

设计取舍:这里刻意只用 PBKDF2 + HKDF + AES-GCM 三种算法,没有引入 ECDH
临时密钥交换(因此不具备前向安全性:若密码事后泄露,理论上可解密被
截获的历史流量)。原因是 PBKDF2/HKDF/AES-GCM 是所有现代浏览器
SubtleCrypto 都原生支持的标准算法,而 X25519 等曲线在 WebCrypto 中的
支持并不普遍,若要用纯 JS 重新实现椭圆曲线运算,引入自研/移植密码学
代码的正确性风险,反而得不偿失。若需要更强的前向安全性,建议部署在
wss:// (TLS) 之上,由传输层提供;应用层的 AES-GCM 加密仍然提供了
"中转服务器不可见明文内容"这一关键属性。

出于防暴力破解考虑,host/relay 侧应对握手失败次数做速率限制
(实现见 host/server.py 的 AuthRateLimiter)。

防重放:AES-GCM 本身只保证"篡改会被发现",并不保证"同一条密文不会被
原样重发"——一个能截获流量的中间人(例如被攻陷的中转服务器,或直连
模式下的链路层攻击者)完全可以把一条曾经真实出现过的合法密文
`[nonce][ciphertext]` 原样重发,解密照样成功。由于每次 `encrypt()` 都用
`os.urandom` 生成全新的随机 nonce,正常流量里同一个 nonce 不会出现第二
次;因此 `SessionCipher` 在会话内维护一个"最近见过的 nonce"集合,一旦
识别到重复的 nonce 就拒绝解密,从而挡住"原样重放历史密文"这类攻击
(例如重放一次鼠标点击或按键)。这个集合按插入顺序做了容量上限,避免
长连接下无限增长。
"""
from __future__ import annotations

import base64
import collections
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
HKDF_INFO = b"remotedesktop-session-key-v1"
REPLAY_WINDOW = 20_000  # 会话内记住的最近 nonce 数量上限


class CryptoError(Exception):
    """握手或加解密过程中的错误(不区分"密码错误"与"数据损坏",避免信息泄露)。"""


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(data: str) -> bytes:
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001 - 统一转换为协议层异常
        raise CryptoError(f"invalid base64: {exc}") from exc


def new_salt() -> bytes:
    return os.urandom(SALT_BYTES)


def derive_session_key(*, password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """由口令 + salt 派生出 AES-256-GCM 会话密钥(PBKDF2 -> HKDF 两段式派生)。"""
    pbkdf2 = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, iterations=iterations,
    )
    password_key = pbkdf2.derive(password.encode("utf-8"))

    hkdf = HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=HKDF_INFO)
    return hkdf.derive(password_key)


class SessionCipher:
    """封装某一条已建立连接的 AES-256-GCM 加解密操作,并做会话内防重放。"""

    def __init__(self, session_key: bytes, aad: bytes = b"", replay_window: int = REPLAY_WINDOW):
        if len(session_key) != KEY_BYTES:
            raise CryptoError("session key must be 32 bytes")
        self._aead = AESGCM(session_key)
        self._aad = aad
        self._replay_window = replay_window
        self._seen_nonces: set[bytes] = set()
        self._nonce_order: collections.deque[bytes] = collections.deque()

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext, self._aad)
        return nonce + ciphertext

    def decrypt(self, wire_bytes: bytes) -> bytes:
        if len(wire_bytes) < NONCE_BYTES + 16:  # 16 = GCM tag 长度
            raise CryptoError("ciphertext too short")
        nonce, ciphertext = wire_bytes[:NONCE_BYTES], wire_bytes[NONCE_BYTES:]
        if nonce in self._seen_nonces:
            raise CryptoError("replayed nonce detected")
        try:
            plaintext = self._aead.decrypt(nonce, ciphertext, self._aad)
        except InvalidTag as exc:
            raise CryptoError("decryption failed (wrong password or tampered data)") from exc
        self._remember_nonce(nonce)
        return plaintext

    def _remember_nonce(self, nonce: bytes) -> None:
        self._seen_nonces.add(nonce)
        self._nonce_order.append(nonce)
        if len(self._nonce_order) > self._replay_window:
            oldest = self._nonce_order.popleft()
            self._seen_nonces.discard(oldest)
