import pytest

from common import crypto


def _derive(password: str, salt: bytes) -> bytes:
    return crypto.derive_session_key(password=password, salt=salt, iterations=1000)  # 低迭代次数加速测试


def test_matching_password_derives_same_key():
    salt = crypto.new_salt()
    key_a = _derive("correct-horse", salt)
    key_b = _derive("correct-horse", salt)
    assert key_a == key_b
    assert len(key_a) == crypto.KEY_BYTES


def test_mismatched_password_derives_different_key():
    salt = crypto.new_salt()
    key_a = _derive("correct-horse", salt)
    key_b = _derive("wrong-password", salt)
    assert key_a != key_b


def test_different_salt_derives_different_key_even_with_same_password():
    key_a = _derive("same-password", crypto.new_salt())
    key_b = _derive("same-password", crypto.new_salt())
    assert key_a != key_b


def test_encrypt_decrypt_roundtrip():
    key = _derive("pw", crypto.new_salt())
    cipher_a = crypto.SessionCipher(key, aad=b"session-1")
    cipher_b = crypto.SessionCipher(key, aad=b"session-1")
    wire = cipher_a.encrypt(b"hello world")
    assert cipher_b.decrypt(wire) == b"hello world"


def test_wrong_key_fails_to_decrypt():
    salt = crypto.new_salt()
    key_a = _derive("pw-a", salt)
    key_b = _derive("pw-b", salt)
    cipher_a = crypto.SessionCipher(key_a, aad=b"s")
    cipher_b = crypto.SessionCipher(key_b, aad=b"s")
    wire = cipher_a.encrypt(b"secret")
    with pytest.raises(crypto.CryptoError):
        cipher_b.decrypt(wire)


def test_tampered_ciphertext_fails_to_decrypt():
    key = _derive("pw", crypto.new_salt())
    cipher = crypto.SessionCipher(key)
    wire = bytearray(cipher.encrypt(b"payload"))
    wire[-1] ^= 0xFF
    with pytest.raises(crypto.CryptoError):
        cipher.decrypt(bytes(wire))


def test_mismatched_aad_fails_to_decrypt():
    key = _derive("pw", crypto.new_salt())
    cipher_a = crypto.SessionCipher(key, aad=b"session-A")
    cipher_b = crypto.SessionCipher(key, aad=b"session-B")
    wire = cipher_a.encrypt(b"data")
    with pytest.raises(crypto.CryptoError):
        cipher_b.decrypt(wire)


def test_short_ciphertext_rejected():
    key = _derive("pw", crypto.new_salt())
    cipher = crypto.SessionCipher(key)
    with pytest.raises(crypto.CryptoError):
        cipher.decrypt(b"short")


def test_nonce_is_random_each_call():
    key = _derive("pw", crypto.new_salt())
    cipher = crypto.SessionCipher(key)
    wire1 = cipher.encrypt(b"same-plaintext")
    wire2 = cipher.encrypt(b"same-plaintext")
    assert wire1 != wire2  # 不同随机 nonce -> 不同密文,防止重放/模式分析


def test_session_key_length_enforced():
    with pytest.raises(crypto.CryptoError):
        crypto.SessionCipher(b"too-short-key")


def test_b64_roundtrip():
    data = b"\x00\x01\xff\xfe some bytes"
    assert crypto.b64d(crypto.b64e(data)) == data


def test_b64d_rejects_invalid_input():
    with pytest.raises(crypto.CryptoError):
        crypto.b64d("not valid base64!!")
