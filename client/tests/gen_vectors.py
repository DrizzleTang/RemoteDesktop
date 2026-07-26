#!/usr/bin/env python3
"""生成 Python <-> JS 加密/协议交叉验证向量。

两端各自实现了一遍 PBKDF2/HKDF/AES-GCM 和二进制帧布局,参数只要有一处
对不上(迭代次数、info 字符串、AAD、字段顺序),表现就是"连不上"或
"画面不动",而各自单独跑测试都是绿的。所以用固定输入生成一份向量,让
JS 测试去解 Python 产出的数据,才能真正锁住两侧的一致性。

改动协议或加密参数后需要重新运行本脚本:
    PYTHONPATH=. python3 client/tests/gen_vectors.py
"""
import base64
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from common import crypto, protocol  # noqa: E402
from host.resume import derive_from_resume  # noqa: E402

OUT = pathlib.Path(__file__).parent / "fixtures" / "crypto_vectors.json"


def main() -> None:
    # 固定输入,保证每次生成的向量可复现(密文里的 nonce 是随机的,
    # 但只要能被解开就说明两侧一致,不需要密文本身逐字节稳定)
    salt = bytes(range(16))
    password = "测试密码-Test123"
    iterations = 100000  # 取允许范围的下限,让 JS 测试跑得快些

    key = crypto.derive_session_key(password=password, salt=salt, iterations=iterations)
    cipher = crypto.SessionCipher(key, aad=salt)
    control = protocol.encode_control(
        {"t": "hello_ack", "width": 1920, "height": 1080, "中文": "值"})

    resume_secret = bytes(range(32, 64))
    resume_cipher = crypto.SessionCipher(derive_from_resume(resume_secret, salt), aad=salt)

    delta = protocol.encode_video_delta(
        seq=42, ts_ms=12345, width=800, height=600, quality=70, fmt=protocol.FMT_WEBP,
        rects=[(10, 20, 30, 40, b"\x01\x02\x03"), (100, 200, 8, 8, b"\xaa\xbb")],
    )
    keyframe = protocol.encode_video_frame(
        seq=7, ts_ms=999, width=640, height=480, quality=50, fmt=protocol.FMT_JPEG,
        keyframe=True, image_bytes=b"\xff\xd8IMG\xff\xd9",
    )
    chunk = protocol.encode_file_chunk(transfer_id=5, seq=3, data=b"hello-chunk")

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "password": password,
        "salt_b64": b64(salt),
        "iterations": iterations,
        "session_key_b64": b64(key),
        "control_ciphertext_b64": b64(cipher.encrypt(control)),
        "control_plaintext_b64": b64(control),
        "resume_secret_b64": b64(resume_secret),
        "resume_key_b64": b64(derive_from_resume(resume_secret, salt)),
        "resume_ciphertext_b64": b64(resume_cipher.encrypt(
            protocol.encode_control({"t": "ping", "n": 7}))),
        "delta_plaintext_b64": b64(delta),
        "keyframe_plaintext_b64": b64(keyframe),
        "file_chunk_plaintext_b64": b64(chunk),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {OUT}")


if __name__ == "__main__":
    main()
