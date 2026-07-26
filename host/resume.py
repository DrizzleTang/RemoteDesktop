"""
重连恢复令牌。

问题:每次(重)连接都要跑一遍 20 万次迭代的 PBKDF2。本机实测约 30ms,
浏览器的 WebCrypto 通常更慢,手机上可能到 100-300ms。弱网下断线重连是
常态,这笔开销直接拖长了每次恢复的时间——而恢复速度恰恰是弱网体验的关键。

做法:握手成功后,host 通过**已加密**的通道下发一个一次性令牌
(token_id + 32 字节随机 secret)。客户端断线重连时,先用明文告知 token_id,
双方直接用 secret 走 HKDF 派生会话密钥,跳过 PBKDF2。

安全性:
- secret 只在已加密的通道里传输过一次,窃听者拿不到;
- token_id 是公开的(和会话码同级),单独拿到没有意义;
- **一次性**:用过即作废,重放同一个令牌会失败;
- **短时效**:默认 5 分钟过期,过期即清除;
- 令牌只是"跳过密码派生"的快捷方式,拿不到令牌的一方仍然必须走密码认证;
- 客户端只把令牌存在内存里(不写 localStorage),页面关闭即消失。

会话结束时必须调用 clear() 作废所有未使用的令牌。
"""
from __future__ import annotations

import os
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_TTL_SECONDS = 300.0
SECRET_BYTES = 32
KEY_BYTES = 32
HKDF_INFO = b"remotedesktop-resume-key-v1"
MAX_TOKENS = 8  # 单个 host 同时有效的令牌数上限,防止内存无限增长


class ResumeTokenStore:
    """host 侧的令牌仓库。"""

    def __init__(self, ttl: float = TOKEN_TTL_SECONDS):
        self.ttl = ttl
        self._tokens: dict[str, tuple[bytes, float]] = {}  # id -> (secret, 过期时刻)

    def issue(self) -> tuple[str, bytes]:
        """签发一个新令牌,返回 (token_id, secret)。"""
        self._purge_expired()
        if len(self._tokens) >= MAX_TOKENS:
            # 丢掉最早过期的那个,保证容量有界
            oldest = min(self._tokens, key=lambda k: self._tokens[k][1])
            del self._tokens[oldest]
        token_id = secrets.token_hex(16)
        secret = os.urandom(SECRET_BYTES)
        self._tokens[token_id] = (secret, time.monotonic() + self.ttl)
        return token_id, secret

    def consume(self, token_id: str) -> bytes | None:
        """取出并**立即作废**指定令牌。不存在或已过期返回 None。"""
        self._purge_expired()
        if not isinstance(token_id, str):
            return None
        entry = self._tokens.pop(token_id, None)
        if entry is None:
            return None
        secret, expires_at = entry
        if time.monotonic() > expires_at:
            return None
        return secret

    def clear(self) -> None:
        self._tokens.clear()

    def _purge_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._tokens.items() if now > exp]
        for key in expired:
            del self._tokens[key]

    def __len__(self) -> int:
        self._purge_expired()
        return len(self._tokens)


def derive_from_resume(secret: bytes, salt: bytes) -> bytes:
    """用恢复令牌的 secret + 本次连接的新 salt 派生会话密钥。

    每次重连都用新的 salt,所以即使同一个 secret 被(不该发生地)重复使用,
    两次会话的密钥也不同。
    """
    hkdf = HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=HKDF_INFO)
    return hkdf.derive(secret)
