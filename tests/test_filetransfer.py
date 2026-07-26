"""
host/filetransfer.py 的单元测试。

测试思路:每个用例都用 pytest 的 tmp_path 建一个独立的沙箱目录,
下载目录固定为 ``tmp_path/downloads``。这样一来 ``tmp_path`` 里除了
``downloads`` 不应该出现任何东西 —— 任何路径穿越如果得手,
落下来的文件就会出现在 ``tmp_path`` 里(甚至更外面),一眼就能看出来。
所以几乎每个用例最后都会真的去 ``iterdir()`` 断言文件系统的最终状态,
而不是只断言"抛没抛异常"。
"""
from __future__ import annotations

import os

import pytest

from host.filetransfer import (
    DEFAULT_FILENAME,
    MAX_NAME_BYTES,
    FileReceiver,
    FileTransferError,
    sanitize_filename,
)


# --------------------------------------------------------------------------
# 测试辅助
# --------------------------------------------------------------------------
def make_receiver(tmp_path, **kwargs) -> FileReceiver:
    """下载目录刻意放在 tmp_path 的子目录里,便于检测"跑到目录外"的文件。"""
    return FileReceiver(tmp_path / "downloads", **kwargs)


def names_in(directory) -> list[str]:
    """列出目录内的条目名(排序后),目录不存在则返回空列表。"""
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir())


def send_file(receiver: FileReceiver, transfer_id: int, name: str, data: bytes,
              chunk_size: int = 64 * 1024):
    """完整走一遍 begin -> write_chunk* -> finish,返回最终路径。"""
    receiver.begin(transfer_id, name, len(data))
    for seq, offset in enumerate(range(0, len(data), chunk_size)):
        receiver.write_chunk(transfer_id, seq, data[offset:offset + chunk_size])
    return receiver.finish(transfer_id)


# --------------------------------------------------------------------------
# 1. 正常路径
# --------------------------------------------------------------------------
def test_构造函数无副作用(tmp_path):
    """只是 new 一个 FileReceiver 不应该在磁盘上留下任何痕迹。"""
    make_receiver(tmp_path)
    assert names_in(tmp_path) == []


def test_单块完整传输内容逐字节一致(tmp_path):
    receiver = make_receiver(tmp_path)
    data = b"hello remote desktop\x00\xff\x01binary safe"

    path = send_file(receiver, 1, "note.txt", data)

    downloads = (tmp_path / "downloads").resolve()
    assert path.parent == downloads
    assert path.name == "note.txt"
    assert path.read_bytes() == data
    # 临时文件必须已经消失,目录里只剩最终文件
    assert names_in(tmp_path / "downloads") == ["note.txt"]
    assert names_in(tmp_path) == ["downloads"]
    assert receiver.active_count == 0


def test_多块传输并按顺序拼接(tmp_path):
    receiver = make_receiver(tmp_path)
    data = bytes(range(256)) * 40  # 10240 字节
    receiver.begin(7, "blob.bin", len(data))

    total = 0
    for seq, offset in enumerate(range(0, len(data), 1024)):
        total = receiver.write_chunk(7, seq, data[offset:offset + 1024])
        # write_chunk 的返回值是"目前已收到的总字节数",供 host 回传进度
        assert total == min(offset + 1024, len(data))
    assert total == len(data)
    assert receiver.active_count == 1

    path = receiver.finish(7)
    assert path.read_bytes() == data
    assert names_in(tmp_path / "downloads") == ["blob.bin"]
    assert receiver.active_count == 0


def test_零字节文件是合法边界(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.begin(2, "empty.log", 0)
    path = receiver.finish(2)  # 一个分块都不发,直接 finish

    assert path.read_bytes() == b""
    assert path.stat().st_size == 0
    assert names_in(tmp_path / "downloads") == ["empty.log"]


def test_中文文件名正常工作(tmp_path):
    receiver = make_receiver(tmp_path)
    data = "季度总结,一切正常。\n".encode()

    path = send_file(receiver, 3, "2024 季度报告(最终版).docx", data)

    assert path.name == "2024 季度报告(最终版).docx"
    assert path.read_bytes() == data
    assert names_in(tmp_path / "downloads") == ["2024 季度报告(最终版).docx"]


def test_传输过程中临时文件是隐藏的part文件(tmp_path):
    """传了一半时,目录里不能出现叫最终名字的残缺文件。"""
    receiver = make_receiver(tmp_path)
    receiver.begin(4, "report.pdf", 10)
    receiver.write_chunk(4, 0, b"12345")

    entries = names_in(tmp_path / "downloads")
    assert entries != ["report.pdf"]
    assert len(entries) == 1
    assert entries[0].startswith(".") and entries[0].endswith(".part")

    receiver.write_chunk(4, 1, b"67890")
    receiver.finish(4)
    assert names_in(tmp_path / "downloads") == ["report.pdf"]


# --------------------------------------------------------------------------
# 2. 文件名消毒 / 路径穿越
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("attack", "expected"),
    [
        ("../../evil.txt", "evil.txt"),
        ("../../../../../../etc/passwd", "passwd"),
        ("/etc/passwd", "passwd"),
        ("/etc/cron.d/backdoor", "backdoor"),
        ("C:\\Windows\\System32\\evil.exe", "evil.exe"),
        ("..\\..\\evil", "evil"),
        ("..\\..\\..\\Windows\\evil.dll", "evil.dll"),
        ("....//....//evil.sh", "evil.sh"),
        ("~/.ssh/authorized_keys", "authorized_keys"),
        ("C:evil.exe", "C_evil.exe"),  # 驱动器相对路径,冒号被替换
    ],
)
def test_路径穿越被挡住且文件落在下载目录内(tmp_path, attack, expected):
    receiver = make_receiver(tmp_path)
    downloads = (tmp_path / "downloads").resolve()

    path = send_file(receiver, 1, attack, b"payload")

    assert path.name == expected
    # 关键断言:最终路径的父目录必须就是下载目录本身(不允许有子目录、更不许在外面)
    assert path.parent == downloads
    assert path.read_bytes() == b"payload"
    # tmp_path 下除了 downloads 不该多出任何东西
    assert names_in(tmp_path) == ["downloads"]
    assert names_in(downloads) == [expected]


def test_纯路径的文件名退化为默认名(tmp_path):
    """名字里除了路径分隔符什么都没有,消毒后为空,要用兜底名。"""
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, "../../", b"x")

    assert path.name == DEFAULT_FILENAME
    assert path.parent == (tmp_path / "downloads").resolve()
    assert names_in(tmp_path) == ["downloads"]


@pytest.mark.parametrize("raw", [".", "..", "...", "   ", "", "\x00", "\x00\x01\x02"])
def test_空或危险文件名退化为默认名(tmp_path, raw):
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, raw, b"x")

    assert path.name == DEFAULT_FILENAME
    assert names_in(tmp_path / "downloads") == [DEFAULT_FILENAME]
    assert names_in(tmp_path) == ["downloads"]


def test_空字节文件名被清理(tmp_path):
    """空字节能截断底层 C 字符串,让"显示的名字"和"实际创建的名字"不一致。"""
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, "safe.txt\x00.exe", b"x")

    assert "\x00" not in path.name
    assert path.name == "safe.txt.exe"
    assert names_in(tmp_path / "downloads") == ["safe.txt.exe"]


def test_控制字符和双向文本伪装符被清理(tmp_path):
    """U+202E(RLO)会让 "photo_gnp.exe" 在文件管理器里显示成 "photo_exe.png"。"""
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, "photo\u202Egnp.exe", b"x")

    assert "\u202E" not in path.name
    assert path.name == "photognp.exe"
    assert "\n" not in path.name and "\r" not in path.name

    path2 = send_file(receiver, 2, "a\r\nb\tc.txt", b"x")
    assert path2.name == "abc.txt"


def test_超长文件名被截断且保留扩展名(tmp_path):
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, "a" * 500 + ".txt", b"x")

    assert len(path.name.encode("utf-8")) <= MAX_NAME_BYTES
    assert path.name.endswith(".txt")
    assert path.exists()
    assert names_in(tmp_path / "downloads") == [path.name]


def test_超长中文文件名按字节截断(tmp_path):
    """一个中文字在 UTF-8 下是 3 字节,按字符数截断会超出文件系统限制。"""
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, "档" * 300 + ".docx", b"x")

    assert len(path.name.encode("utf-8")) <= MAX_NAME_BYTES
    assert path.name.endswith(".docx")
    # 截断不能把中文切成半个字(能 read_bytes 说明这个名字真的建出来了)
    assert path.read_bytes() == b"x"
    assert names_in(tmp_path / "downloads") == [path.name]


def test_超长扩展名也会被截断(tmp_path):
    """攻击者可以构造一个 500 字节的"扩展名"绕过只截主干的实现。"""
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, "x." + "e" * 500, b"x")

    assert len(path.name.encode("utf-8")) <= MAX_NAME_BYTES
    assert path.exists()


@pytest.mark.parametrize(
    "reserved", ["CON", "con", "NUL", "nul.txt", "COM1", "com1.log", "LPT9", "PRN", "AUX"]
)
def test_Windows保留设备名被改写(tmp_path, reserved):
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, reserved, b"x")

    stem = path.name.split(".", 1)[0].upper()
    assert stem not in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT9"}
    assert path.name.startswith("_")
    assert path.read_bytes() == b"x"
    assert names_in(tmp_path / "downloads") == [path.name]


def test_结尾的点和空格被剥掉(tmp_path):
    """Windows 会静默丢弃结尾的点/空格,"CON. " 实际会创建设备 CON。"""
    assert sanitize_filename("CON. ").upper().lstrip("_") == "CON"
    assert sanitize_filename("CON. ").startswith("_")
    assert sanitize_filename("evil.exe ") == "evil.exe"
    assert sanitize_filename("evil.exe...") == "evil.exe"

    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, "trail.txt. ", b"x")
    assert path.name == "trail.txt"
    assert names_in(tmp_path / "downloads") == ["trail.txt"]


def test_Windows非法字符被替换(tmp_path):
    """":" 还是 NTFS 备用数据流分隔符,能把数据藏进另一个流。"""
    receiver = make_receiver(tmp_path)
    path = send_file(receiver, 1, 'a:b"c<d>e|f?g*h.txt', b"x")

    assert not any(ch in path.name for ch in '<>:"|?*')
    assert path.name == "a_b_c_d_e_f_g_h.txt"
    assert names_in(tmp_path / "downloads") == [path.name]


def test_文件名不是字符串时拒绝(tmp_path):
    receiver = make_receiver(tmp_path)
    with pytest.raises(FileTransferError):
        receiver.begin(1, 12345, 10)  # type: ignore[arg-type]
    assert names_in(tmp_path) == []


def test_下载目录是符号链接时仍然可用(tmp_path):
    """兜底校验用的是 resolve() 后的路径,不能因为目录本身是软链接就误杀。"""
    real = tmp_path / "real_downloads"
    real.mkdir()
    link = tmp_path / "downloads"
    link.symlink_to(real, target_is_directory=True)

    receiver = FileReceiver(link)
    path = send_file(receiver, 1, "via_link.txt", b"data")

    assert path.read_bytes() == b"data"
    assert names_in(real) == ["via_link.txt"]


def test_下载目录内已存在同名符号链接时不写穿(tmp_path):
    """预置一个指向敏感文件的软链接,新文件必须改名,绝不能顺着链接写出去。"""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("原始内容")
    (downloads / "target.txt").symlink_to(outside)

    receiver = FileReceiver(downloads)
    path = send_file(receiver, 1, "target.txt", b"attacker data")

    assert path.name == "target (1).txt"
    assert outside.read_text() == "原始内容"  # 链接指向的文件没被改动
    assert names_in(downloads) == ["target (1).txt", "target.txt"]


# --------------------------------------------------------------------------
# 3. 同名不覆盖
# --------------------------------------------------------------------------
def test_同名文件自动改名且保留扩展名(tmp_path):
    receiver = make_receiver(tmp_path)

    first = send_file(receiver, 1, "报告.docx", b"first")
    second = send_file(receiver, 2, "报告.docx", b"second")
    third = send_file(receiver, 3, "报告.docx", b"third")

    assert first.name == "报告.docx"
    assert second.name == "报告 (1).docx"
    assert third.name == "报告 (2).docx"
    # 原文件内容不能被覆盖
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert third.read_bytes() == b"third"
    assert names_in(tmp_path / "downloads") == ["报告 (1).docx", "报告 (2).docx", "报告.docx"]


def test_同名不覆盖磁盘上已有的文件(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "data.bin").write_bytes(b"IMPORTANT")

    receiver = FileReceiver(downloads)
    path = send_file(receiver, 1, "data.bin", b"new")

    assert path.name == "data (1).bin"
    assert (downloads / "data.bin").read_bytes() == b"IMPORTANT"
    assert names_in(downloads) == ["data (1).bin", "data.bin"]


def test_并发同名传输不会互相覆盖(tmp_path):
    """两个传输同时在途时磁盘上还没有最终文件名,靠内部占位表判重。"""
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "same.txt", 1)
    receiver.begin(2, "same.txt", 1)
    receiver.write_chunk(1, 0, b"A")
    receiver.write_chunk(2, 0, b"B")

    p1 = receiver.finish(1)
    p2 = receiver.finish(2)

    assert p1.name != p2.name
    assert p1.read_bytes() == b"A"
    assert p2.read_bytes() == b"B"
    assert names_in(tmp_path / "downloads") == sorted([p1.name, p2.name])


# --------------------------------------------------------------------------
# 4. 大小校验
# --------------------------------------------------------------------------
def test_实际发送超过声明大小被拒绝(tmp_path):
    """声明是小文件却猛灌数据 —— 典型的磁盘填充攻击。"""
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "lie.bin", 5)
    receiver.write_chunk(1, 0, b"12345")

    with pytest.raises(FileTransferError, match="超过"):
        receiver.write_chunk(1, 1, b"more data")

    # 传输已被内部中止:临时文件清掉,状态表清空
    assert names_in(tmp_path / "downloads") == []
    assert receiver.active_count == 0


def test_单个分块就超过声明大小被拒绝(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "lie.bin", 3)

    with pytest.raises(FileTransferError):
        receiver.write_chunk(1, 0, b"way too long")

    assert names_in(tmp_path / "downloads") == []
    assert receiver.active_count == 0


def test_实际发送少于声明大小时finish失败并清理(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "short.bin", 10)
    receiver.write_chunk(1, 0, b"1234")

    with pytest.raises(FileTransferError, match="不完整"):
        receiver.finish(1)

    # 残缺文件绝不能改名成正式文件名留在目录里
    assert names_in(tmp_path / "downloads") == []
    assert names_in(tmp_path) == ["downloads"]
    assert receiver.active_count == 0


def test_超过单文件大小上限被拒绝(tmp_path):
    receiver = make_receiver(tmp_path, max_file_bytes=10)

    with pytest.raises(FileTransferError, match="过大"):
        receiver.begin(1, "huge.bin", 11)

    # 被拒绝的传输连下载目录都不该创建
    assert names_in(tmp_path) == []
    assert receiver.active_count == 0

    # 上限之内的正常放行
    path = send_file(receiver, 2, "ok.bin", b"0123456789")
    assert path.read_bytes() == b"0123456789"


@pytest.mark.parametrize("bad_size", [-1, -1024, "10", 1.5, None, True])
def test_非法size被拒绝(tmp_path, bad_size):
    receiver = make_receiver(tmp_path)
    with pytest.raises(FileTransferError):
        receiver.begin(1, "x.bin", bad_size)  # type: ignore[arg-type]
    assert names_in(tmp_path) == []


@pytest.mark.parametrize("bad_id", [-1, 2 ** 32, "1", None, 1.0])
def test_非法transfer_id被拒绝(tmp_path, bad_id):
    receiver = make_receiver(tmp_path)
    with pytest.raises(FileTransferError):
        receiver.begin(bad_id, "x.bin", 1)  # type: ignore[arg-type]
    assert names_in(tmp_path) == []


# --------------------------------------------------------------------------
# 5. 并发与状态机
# --------------------------------------------------------------------------
def test_超过并发上限被拒绝(tmp_path):
    receiver = make_receiver(tmp_path, max_concurrent=2)
    receiver.begin(1, "a.bin", 4)
    receiver.begin(2, "b.bin", 4)

    with pytest.raises(FileTransferError, match="不能超过"):
        receiver.begin(3, "c.bin", 4)

    assert receiver.active_count == 2
    # 被拒的第三个传输不该留下任何临时文件
    assert len(names_in(tmp_path / "downloads")) == 2

    # 结束一个之后就能再开一个了(名额是可回收的)
    receiver.write_chunk(1, 0, b"aaaa")
    receiver.finish(1)
    receiver.begin(3, "c.bin", 4)
    assert receiver.active_count == 2


def test_重复的transfer_id被拒绝(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "a.bin", 4)
    receiver.write_chunk(1, 0, b"aaaa")

    with pytest.raises(FileTransferError, match="已在进行中"):
        receiver.begin(1, "b.bin", 4)

    assert receiver.active_count == 1
    # 第一个传输不能被第二次 begin 破坏
    path = receiver.finish(1)
    assert path.name == "a.bin"
    assert path.read_bytes() == b"aaaa"
    assert names_in(tmp_path / "downloads") == ["a.bin"]


def test_未知transfer_id的write_chunk被拒绝(tmp_path):
    """不能"顺手创建"传输,否则 begin 里的全部校验都能被绕过。"""
    receiver = make_receiver(tmp_path)

    with pytest.raises(FileTransferError, match="未知"):
        receiver.write_chunk(999, 0, b"data")

    assert names_in(tmp_path) == []
    assert receiver.active_count == 0


def test_未知transfer_id的finish被拒绝(tmp_path):
    receiver = make_receiver(tmp_path)
    with pytest.raises(FileTransferError, match="未知"):
        receiver.finish(999)
    assert names_in(tmp_path) == []


def test_finish之后再次操作同一id被拒绝(tmp_path):
    receiver = make_receiver(tmp_path)
    send_file(receiver, 1, "once.txt", b"data")

    with pytest.raises(FileTransferError):
        receiver.write_chunk(1, 1, b"more")
    with pytest.raises(FileTransferError):
        receiver.finish(1)
    assert names_in(tmp_path / "downloads") == ["once.txt"]


@pytest.mark.parametrize("bad_seq", [0, 2, 3, 99, -1])
def test_乱序seq被拒绝并中止传输(tmp_path, bad_seq):
    """底层 TCP 保证有序,乱序只可能是协议被破坏或有人在攻击。"""
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "ordered.bin", 8)
    receiver.write_chunk(1, 0, b"1234")  # 收下 seq=0,期望的下一个 seq 变成 1

    with pytest.raises(FileTransferError, match="顺序"):
        receiver.write_chunk(1, bad_seq, b"5678")

    assert receiver.active_count == 0
    assert names_in(tmp_path / "downloads") == []


def test_首个分块seq必须为0(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "ordered.bin", 4)

    with pytest.raises(FileTransferError, match="顺序"):
        receiver.write_chunk(1, 1, b"1234")

    assert names_in(tmp_path / "downloads") == []
    assert receiver.active_count == 0


def test_重复发送同一个seq被拒绝(tmp_path):
    """重放同一个分块会让实际字节数超出声明值,也是一种攻击。"""
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "dup.bin", 8)
    receiver.write_chunk(1, 0, b"1234")

    with pytest.raises(FileTransferError, match="顺序"):
        receiver.write_chunk(1, 0, b"1234")

    assert names_in(tmp_path / "downloads") == []


def test_非bytes的分块数据被拒绝(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "x.bin", 4)

    with pytest.raises(FileTransferError):
        receiver.write_chunk(1, 0, "文本")  # type: ignore[arg-type]

    # 参数校验失败不影响传输本身,补发正确的分块仍然可以完成
    receiver.write_chunk(1, 0, b"1234")
    assert receiver.finish(1).read_bytes() == b"1234"


# --------------------------------------------------------------------------
# 6. abort / cleanup_all
# --------------------------------------------------------------------------
def test_abort删除临时文件且不影响其他传输(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "keep.bin", 4)
    receiver.begin(2, "drop.bin", 4)
    receiver.write_chunk(1, 0, b"keep")
    receiver.write_chunk(2, 0, b"drop")
    assert len(names_in(tmp_path / "downloads")) == 2

    receiver.abort(2)

    assert receiver.active_count == 1
    remaining = names_in(tmp_path / "downloads")
    assert len(remaining) == 1
    assert "drop" not in remaining[0]

    # 另一个传输完全不受影响
    path = receiver.finish(1)
    assert path.read_bytes() == b"keep"
    assert names_in(tmp_path / "downloads") == ["keep.bin"]


def test_abort未知id静默返回(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.abort(999)  # 不应抛异常
    receiver.abort(0)
    assert names_in(tmp_path) == []


def test_abort之后同名文件名可以被再次使用(tmp_path):
    """取消掉的传输必须把预留的文件名还回来,否则名字会被永久占用。"""
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "again.txt", 4)
    receiver.abort(1)

    path = send_file(receiver, 2, "again.txt", b"data")
    assert path.name == "again.txt"  # 不是 "again (1).txt"
    assert names_in(tmp_path / "downloads") == ["again.txt"]


def test_cleanup_all清理所有在途传输(tmp_path):
    receiver = make_receiver(tmp_path)
    for tid in range(1, 4):
        receiver.begin(tid, f"f{tid}.bin", 100)
        receiver.write_chunk(tid, 0, b"partial")
    assert receiver.active_count == 3
    assert len(names_in(tmp_path / "downloads")) == 3

    receiver.cleanup_all()

    assert receiver.active_count == 0
    assert names_in(tmp_path / "downloads") == []
    assert names_in(tmp_path) == ["downloads"]


def test_cleanup_all保留已完成的文件且可重复调用(tmp_path):
    receiver = make_receiver(tmp_path)
    send_file(receiver, 1, "done.txt", b"done")
    receiver.begin(2, "half.bin", 100)
    receiver.write_chunk(2, 0, b"half")

    receiver.cleanup_all()
    receiver.cleanup_all()  # 幂等,且绝不抛异常

    assert receiver.active_count == 0
    assert names_in(tmp_path / "downloads") == ["done.txt"]


def test_cleanup_all之后可以继续使用(tmp_path):
    receiver = make_receiver(tmp_path)
    receiver.begin(1, "x.bin", 100)
    receiver.cleanup_all()

    path = send_file(receiver, 1, "x.bin", b"ok")
    assert path.read_bytes() == b"ok"
    assert names_in(tmp_path / "downloads") == ["x.bin"]


# --------------------------------------------------------------------------
# 7. 资源泄漏
# --------------------------------------------------------------------------
@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="需要 /proc 才能数文件句柄")
def test_文件句柄不泄漏(tmp_path):
    """finish / abort / cleanup_all 三条路径都必须把句柄还回去。"""
    def open_fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    receiver = make_receiver(tmp_path)
    baseline = open_fds()

    # 正常完成
    send_file(receiver, 1, "a.bin", b"data")
    assert open_fds() == baseline

    # 主动取消
    receiver.begin(2, "b.bin", 100)
    receiver.abort(2)
    assert open_fds() == baseline

    # 出错中止(声明 1 字节却发 5 字节)
    receiver.begin(3, "c.bin", 1)
    with pytest.raises(FileTransferError):
        receiver.write_chunk(3, 0, b"toolong")
    assert open_fds() == baseline

    # finish 校验失败
    receiver.begin(4, "d.bin", 100)
    receiver.write_chunk(4, 0, b"short")
    with pytest.raises(FileTransferError):
        receiver.finish(4)
    assert open_fds() == baseline

    # 会话结束清理
    for tid in range(5, 9):
        receiver.begin(tid, f"e{tid}.bin", 100)
    assert open_fds() == baseline + 4
    receiver.cleanup_all()
    assert open_fds() == baseline


# --------------------------------------------------------------------------
# 8. sanitize_filename 的直接单元测试(纯函数,不碰磁盘)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("normal.txt", "normal.txt"),
        ("报告.docx", "报告.docx"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\evil.exe", "evil.exe"),
        ("/absolute/path.bin", "path.bin"),
        ("with space.txt", "with space.txt"),
        (".hidden", ".hidden"),
        ("a\x00b.txt", "ab.txt"),
        ("", DEFAULT_FILENAME),
        ("..", DEFAULT_FILENAME),
        ("/", DEFAULT_FILENAME),
        ("\\", DEFAULT_FILENAME),
        (None, DEFAULT_FILENAME),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename结果永远是单层且不超长():
    """遍历一批恶意样本,断言输出永远满足两个不变式。"""
    samples = [
        "../" * 50 + "evil",
        "..\\" * 50 + "evil",
        "\x00" * 100 + "x",
        "长" * 1000 + ".tar.gz",
        "CON",
        "a" * 300,
        "  ...  ",
        "C:\\x\\y\\z.txt",
        "\u202Etxt.exe",
        "?" * 100,
    ]
    for raw in samples:
        name = sanitize_filename(raw)
        assert name, f"{raw!r} 消毒后为空"
        assert "/" not in name and "\\" not in name, f"{raw!r} -> {name!r} 仍含分隔符"
        assert name not in (".", ".."), f"{raw!r} -> {name!r}"
        assert len(name.encode("utf-8")) <= MAX_NAME_BYTES, f"{raw!r} -> {name!r} 超长"
