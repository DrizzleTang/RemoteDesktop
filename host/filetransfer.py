"""
文件接收(host 被控端侧):把主控端上传的文件安全地落盘。

协议流程(定义见 common/protocol.py,本模块只负责"落盘"这一段语义):
    client --> host  控制消息 file_begin {id, name, size}
    client --> host  若干 MSG_FILE_CHUNK 二进制帧 {transfer_id, seq, data}
    client --> host  控制消息 file_end {id}   (中途取消则发 file_abort {id})

安全模型
--------
发送方虽然已经通过了密码认证并且信道是加密的,但 **认证 != 可信**:
密码可能被猜到/泄露,主控端浏览器也可能被 XSS 或恶意页面劫持。因此本模块
把 file_begin / 文件分块里的一切内容都当作**敌意输入**来处理,重点防御:

  1. 路径穿越:``../../etc/passwd``、``/etc/passwd``、``C:\\Windows\\evil.exe``、
     ``..\\..\\evil`` —— 一律只取 basename,并且在真正落盘前用 ``Path.resolve()``
     复核最终路径确实是 download_dir 的**直接子项**(这一步能同时挡住符号链接、
     以及消毒逻辑本身可能存在的疏漏,是最后一道防线)。
  2. 文件名注入:空字节(截断 C 字符串)、控制字符、RLO 等双向文本控制符
     (用于伪装扩展名 ``photo_gnp.exe`` -> 显示成 ``photo_exe.png``)、
     Windows 保留设备名(``CON``/``NUL``/``COM1``…)、Windows 非法字符
     (``: " < > | ? *``,其中 ``:`` 还是 NTFS 数据流分隔符)、超长文件名。
  3. 覆盖已有文件:同名时自动改名为 ``报告 (1).docx``,永不覆盖;
     并且用 ``O_EXCL`` 打开临时文件、用 ``os.path.lexists`` 判重,
     使得"预先在下载目录里放一个指向 /etc/shadow 的符号链接"也无法生效。
  4. 撒谎的 size:声明 100 字节却猛灌 10GB —— 累计字节数超过声明值立即中止。
  5. 资源耗尽:限制单文件大小、并发传输数(同时也就限制了文件句柄数),
     并在开始前检查磁盘剩余空间。
  6. 残缺文件冒充完整文件:先写隐藏的 ``.xxx.part`` 临时文件,
     只有在收到 file_end 且字节数完全对得上时,才 ``os.replace()`` 原子改名。

线程模型
--------
本模块的所有方法都是**阻塞磁盘 I/O**,调用方(host/server.py)应当在
线程池里调用(如 ``asyncio.to_thread``),不要直接在事件循环里调用。
内部用一把锁保护全部状态与写操作,因此多线程并发调用是安全的。
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger("host.filetransfer")

# ---- 对外可调的默认阈值 ----
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024  # 单文件 2GB 上限
DEFAULT_MAX_CONCURRENT = 4  # 同时进行的传输数上限(同时也是同时打开的句柄数上限)

# ---- 内部安全常量 ----
# 文件名长度上限按**字节**算:绝大多数文件系统(ext4/APFS/NTFS)限制的是
# 单个路径分量的字节数(255),而一个中文字在 UTF-8 下占 3 字节,
# 按字符数截断会在中文文件名上超限。
MAX_NAME_BYTES = 255
# 协议规定分块 64KB;远大于此说明发送方要么实现有误要么在做内存放大攻击。
MAX_CHUNK_BYTES = 4 * 1024 * 1024
# 同名文件最多尝试到 "name (9999).ext",再多就认为是有人在刷目录。
MAX_DUPLICATE_SUFFIX = 9999
# 消毒后为空时使用的兜底名(刻意用纯 ASCII,避免落到某些不支持 UTF-8 的卷上出问题)。
DEFAULT_FILENAME = "unnamed_file"
# 临时文件后缀。前面还会加 "." 前缀,让它在 Unix 下是隐藏文件,
# 不至于让用户在文件管理器里看到一个传了一半的文件并误以为它是完整的。
TEMP_SUFFIX = ".part"
# 磁盘剩余空间保护:不允许一次传输把磁盘吃到只剩这么点,免得把被控机搞死机。
MIN_FREE_DISK_BYTES = 64 * 1024 * 1024

# Windows 保留设备名。在 Windows 上创建这些名字的文件会打开设备而不是文件,
# 可能导致进程挂死或写到串口;即使 host 跑在 Linux 上,这些文件后续被拷到
# Windows 机器上也会出问题,所以统一挡掉。
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Windows 下文件名里非法的字符;":" 同时是 NTFS 备用数据流(ADS)分隔符,
# "evil.txt:hidden.exe" 这种写法能把数据藏进另一个流里,必须替换掉。
_ILLEGAL_CHARS = '<>:"|?*'

# 需要整类剔除的 Unicode 分类:
#   Cc = 控制字符(含 \x00 空字节:能截断底层 C 字符串,造成"看到的名字"与
#        "实际创建的名字"不一致)
#   Cf = 格式控制符(含 U+202E RIGHT-TO-LEFT OVERRIDE,经典的扩展名伪装手法)
#   Cs = 代理对码点(合法 UTF-8 里不该出现,编码时会炸)
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


class FileTransferError(Exception):
    """文件传输相关的错误。

    这里的错误消息会被 host/server.py 通过 file_error 控制消息原样回传给
    主控端并直接展示给用户,所以必须写成**可读的中文**,并且不要包含
    服务端的绝对路径等敏感信息(避免向不完全可信的对端泄露目录结构)。
    """


# --------------------------------------------------------------------------
# 文件名消毒
# --------------------------------------------------------------------------
def _truncate_utf8(text: str, max_bytes: int) -> str:
    """把字符串截断到不超过 max_bytes 字节,且不切断多字节字符。

    直接对 UTF-8 字节切片可能把一个中文字切成半个,再解码就会抛异常;
    这里用 errors="ignore" 让残缺的尾字节被丢弃,保证结果总是合法字符串。
    """
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8", "surrogatepass")[:max_bytes]
    return encoded.decode("utf-8", "ignore")


def _fit_name(stem: str, suffix: str, limit: int = MAX_NAME_BYTES) -> str:
    """拼接 stem+suffix 并压到 limit 字节以内,**扩展名优先保留**。

    保留扩展名很重要:用户靠扩展名识别文件类型,把 ``很长的名字.docx``
    截成 ``很长的名..`` 会让文件无法双击打开。但如果扩展名本身就长得离谱
    (攻击者可以构造 300 字节的"扩展名"),就不能无条件保留,
    此时把扩展名也截断,最多让它占一半配额。
    """
    suffix = _truncate_utf8(suffix, max(limit // 2, 0))
    room = limit - len(suffix.encode("utf-8"))
    stem = _truncate_utf8(stem, room)
    return stem + suffix


def _split_ext(name: str) -> tuple[str, str]:
    """拆成 (主干, 扩展名)。os.path.splitext 会正确处理 ".bashrc" 这种全是扩展名的情况。"""
    return os.path.splitext(name)


def sanitize_filename(raw: str) -> str:
    """把对端发来的任意字符串消毒成一个**安全的单层文件名**。

    这个函数只做"尽量得到一个可用名字",不负责最终安全性 ——
    真正的兜底是 FileReceiver 里 resolve() 之后的目录归属校验。
    """
    if not isinstance(raw, str):
        return DEFAULT_FILENAME

    # 1) Unicode 规范化到 NFC:macOS 发来的是 NFD 分解形式,不统一会导致
    #    "看起来同名但判重失败",也会让下面的保留名比较出现绕过。
    name = unicodedata.normalize("NFC", raw)

    # 2) 剔除空字节 / 控制字符 / 双向文本伪装字符(见 _DROP_CATEGORIES 说明)。
    name = "".join(ch for ch in name if unicodedata.category(ch) not in _DROP_CATEGORIES)

    # 3) 只取 basename。必须同时按 "/" 和 "\\" 切:发送方可能是 Windows,
    #    Python 在 Linux 上不认为 "\\" 是分隔符,于是 "..\\..\\evil" 会被
    #    当成一个普通文件名原样落盘 —— 这个文件本身无害,但把它拷到 Windows
    #    上、或者后续代码用 ntpath 处理时就会变成真正的穿越。
    for separator in ("/", "\\"):
        name = name.rsplit(separator, 1)[-1]

    # 4) 替换 Windows 非法字符 / NTFS 数据流分隔符。
    #    经过第 3 步后 "C:\\Windows\\evil.exe" 已经只剩 "evil.exe",
    #    但 "C:evil.exe" 这种"驱动器相对路径"写法没有分隔符,靠这一步兜住。
    name = "".join("_" if ch in _ILLEGAL_CHARS else ch for ch in name)

    # 5) 去掉首尾空白,以及**结尾的点和空格**:Windows 在创建文件时会静默
    #    丢弃结尾的 "." 和 " ",于是 "CON." / "evil.exe " 能绕过按字符串做的
    #    保留名/扩展名检查,却创建出 "CON" / "evil.exe"。先剥掉再检查。
    name = name.strip().rstrip(" .")

    # 6) "." 与 ".." 是目录自身与上级目录,绝不能作为文件名。
    #    经过第 5 步它们已经变成空串,这里再显式挡一次,防止将来改动第 5 步时回归。
    if name in ("", ".", ".."):
        return DEFAULT_FILENAME

    # 7) Windows 保留设备名。判断的是**第一个点之前**的部分,
    #    因为 Windows 认为 "CON.txt" 同样是设备 CON。加下划线前缀而不是拒收,
    #    以免用户一个正常命名的 "NUL.log" 日志文件传不上来。
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name

    # 8) 长度限制(按字节),扩展名优先保留。
    stem, suffix = _split_ext(name)
    name = _fit_name(stem, suffix)

    # 9) 截断后可能又变成空串(例如原名是 300 字节的纯扩展名),再兜一次底。
    name = name.strip().rstrip(" .")
    if name in ("", ".", ".."):
        return DEFAULT_FILENAME
    return name


# --------------------------------------------------------------------------
# 单次传输的状态
# --------------------------------------------------------------------------
@dataclass
class _Transfer:
    transfer_id: int
    display_name: str  # 消毒后的文件名(不含去重后缀),finish 时用它重新选目标名
    declared_size: int
    final_path: Path  # begin 时预留的最终路径
    temp_path: Path  # 正在写的 .part 临时文件
    handle: BinaryIO
    received: int = 0
    next_seq: int = 0  # 期望的下一个分块序号


class FileReceiver:
    """接收并落盘主控端上传的文件。

    一个会话(一条 WebSocket 连接)配一个实例;会话断开时**必须**调用
    cleanup_all(),否则临时文件和文件句柄会泄漏。
    """

    def __init__(
        self,
        download_dir: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ) -> None:
        # 构造函数刻意不做任何文件系统操作(不 mkdir、不 resolve):
        # host 启动时就会创建 FileReceiver,但用户可能整场会话都不传文件,
        # 那就不该凭空在用户磁盘上多出一个空目录。目录在 begin() 里按需创建。
        self._download_dir = Path(download_dir)
        self._max_file_bytes = int(max_file_bytes)
        self._max_concurrent = int(max_concurrent)
        self._transfers: dict[int, _Transfer] = {}
        # 已被在途传输"占位"的最终文件名。磁盘上此时还不存在这个名字
        # (我们写的是 .part),所以光靠 lexists() 判重会让两个并发传输
        # 在 finish 时抢同一个名字、后者覆盖前者。
        self._reserved: set[str] = set()
        # 一把锁保护全部状态。写盘也在锁内完成:同一个 transfer 的两次
        # write_chunk 如果并发,"检查 seq -> 写 -> 累加"必须是原子的,
        # 否则会写出交错的垃圾数据。
        self._lock = threading.RLock()

    # ---------------- 内部工具 ----------------
    def _ensure_root(self) -> Path:
        """确保下载目录存在,返回**解析后**的绝对路径。

        解析放在这里而不是构造函数里,是因为目录可能在会话中途才被创建
        (或被换成符号链接),每次都重新解析才能拿到当下真实的位置。
        """
        root = self._download_dir.expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FileTransferError(f"无法创建下载目录:{exc.strerror or exc}") from exc
        return root.resolve()

    @staticmethod
    def _check_inside(root: Path, path: Path) -> None:
        """最后一道防线:确认 path 解析后确实是 root 的**直接子项**。

        即使前面的消毒有疏漏(新的 Unicode 花招、平台差异、未来改坏了代码),
        只要这里过不了就绝不落盘。用 resolve() 是为了把符号链接、"..",
        以及 Windows 的 8.3 短名等都展开成真实路径再比较。
        要求 parent 完全相等(而不是"在 root 之下"),是因为消毒后只允许
        单层文件名,出现子目录就说明消毒被绕过了。
        """
        resolved = path.resolve()
        if resolved.parent != root:
            # 刻意不把 resolved 的完整路径回传给对端,避免泄露目录结构。
            raise FileTransferError("文件名非法:目标路径超出了下载目录范围")

    def _reserve_target(self, root: Path, name: str) -> Path:
        """挑一个当前没被占用的最终文件名并预留下来(调用方必须持锁)。

        判重用 os.path.lexists 而不是 Path.exists:后者会跟随符号链接,
        一个指向不存在目标的悬空链接会被判定为"不存在",于是我们选中这个名字,
        虽然 os.replace 不跟随符号链接(替换的是链接本身)不至于写穿,
        但用 lexists 更直白 —— 只要那个名字被任何东西占着就换一个。
        """
        stem, suffix = _split_ext(name)
        for index in range(MAX_DUPLICATE_SUFFIX + 1):
            candidate = name if index == 0 else _fit_name(f"{stem} ({index})", suffix)
            if not candidate or candidate in self._reserved:
                continue
            path = root / candidate
            if os.path.lexists(path):
                continue
            self._check_inside(root, path)
            self._reserved.add(candidate)
            return path
        raise FileTransferError("同名文件过多,请先清理下载目录后重试")

    def _temp_path_for(self, root: Path, transfer_id: int, name: str) -> Path:
        """构造临时文件路径 ``.<名字>.<id>.part``。

        带上 transfer_id 是为了让同一会话里多个同名文件的并发传输不会写到
        同一个临时文件上。整体长度同样要压到 255 字节以内,否则在文件名
        本来就接近上限时,加了前后缀反而会因为 ENAMETOOLONG 打不开。
        """
        tail = f".{transfer_id}{TEMP_SUFFIX}"
        room = MAX_NAME_BYTES - len(tail.encode("utf-8")) - 1  # 1 是前导 "."
        stem, suffix = _split_ext(name)
        temp_name = "." + _fit_name(stem, suffix, limit=max(room, 1)) + tail
        path = root / temp_name
        self._check_inside(root, path)
        return path

    def _check_disk_space(self, root: Path, size: int) -> None:
        """开传之前先看看磁盘装不装得下,别等写满了才失败。

        这是"尽力而为"的检查:拿不到磁盘信息就跳过,不能因为统计失败
        就把正常的文件传输给挡了。
        """
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            return
        if size > max(free - MIN_FREE_DISK_BYTES, 0):
            raise FileTransferError("被控端磁盘剩余空间不足,已拒绝接收该文件")

    def _close_and_unlink(self, transfer: _Transfer) -> None:
        """关闭句柄并删除临时文件。本函数**不抛异常**,供各条清理路径复用。"""
        try:
            transfer.handle.close()
        except OSError as exc:
            logger.warning("关闭临时文件句柄失败(已忽略): %s", exc)
        try:
            os.unlink(transfer.temp_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("删除临时文件失败(已忽略): %s", exc)

    def _drop(self, transfer: _Transfer) -> None:
        """把一次传输从状态表里摘掉,并释放它占用的文件名(调用方必须持锁)。"""
        self._transfers.pop(transfer.transfer_id, None)
        self._reserved.discard(transfer.final_path.name)

    # ---------------- 对外接口 ----------------
    def begin(self, transfer_id: int, name: str, size: int) -> None:
        """开始一次传输:校验参数、消毒文件名、创建临时文件。

        任何不合法的输入都抛 FileTransferError,调用方应把消息回传给主控端。
        """
        # bool 是 int 的子类,True 会被当成 1 混过 isinstance 检查,单独挡掉。
        if isinstance(transfer_id, bool) or not isinstance(transfer_id, int):
            raise FileTransferError("传输编号无效")
        if not 0 <= transfer_id <= 0xFFFFFFFF:
            # 协议里 transfer_id 是 uint32,超出范围说明对端在乱发。
            raise FileTransferError("传输编号超出取值范围")
        if isinstance(size, bool) or not isinstance(size, int):
            raise FileTransferError("文件大小无效")
        if size < 0:
            raise FileTransferError("文件大小无效")
        if size > self._max_file_bytes:
            limit_mb = self._max_file_bytes / (1024 * 1024)
            raise FileTransferError(f"文件过大,单个文件不能超过 {limit_mb:.0f} MB")
        if not isinstance(name, str):
            raise FileTransferError("文件名无效")

        safe_name = sanitize_filename(name)

        with self._lock:
            if transfer_id in self._transfers:
                # 重复的 id 会让两次传输互相踩状态,必须拒绝。
                raise FileTransferError("该传输编号已在进行中")
            if len(self._transfers) >= self._max_concurrent:
                # 同时限制了打开的文件句柄数与磁盘占用,防止对端刷一万个 file_begin。
                raise FileTransferError(
                    f"同时进行的文件传输不能超过 {self._max_concurrent} 个,请稍后再试"
                )

            root = self._ensure_root()
            self._check_disk_space(root, size)
            final_path = self._reserve_target(root, safe_name)
            temp_path = self._temp_path_for(root, transfer_id, safe_name)

            # O_EXCL:目标已存在(包括是符号链接)就直接失败,绝不跟随链接写到别处;
            # O_NOFOLLOW 再加一层保险。权限 0600:传输过程中的文件只有属主可读,
            # 避免多用户机器上其他用户偷看正在传的内容。
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0)  # Windows 上必须显式二进制
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(temp_path, flags, 0o600)
            except FileExistsError:
                # 同一 id 的临时文件还在(上次会话崩溃遗留),不静默覆盖。
                raise FileTransferError("临时文件已存在,请稍后重试") from None
            except OSError as exc:
                self._reserved.discard(final_path.name)
                raise FileTransferError(f"无法创建临时文件:{exc.strerror or exc}") from exc

            try:
                handle = os.fdopen(fd, "wb")
            except OSError as exc:  # pragma: no cover - fdopen 失败极罕见
                os.close(fd)
                self._reserved.discard(final_path.name)
                raise FileTransferError(f"无法打开临时文件:{exc.strerror or exc}") from exc

            self._transfers[transfer_id] = _Transfer(
                transfer_id=transfer_id,
                display_name=safe_name,
                declared_size=size,
                final_path=final_path,
                temp_path=temp_path,
                handle=handle,
            )
        # 只记录消毒后的名字:原始名字里可能有控制字符,直接进日志会污染终端/日志文件。
        logger.info("开始接收文件 id=%s name=%s size=%d", transfer_id, safe_name, size)

    def write_chunk(self, transfer_id: int, seq: int, data: bytes) -> int:
        """写入一个分块,返回该传输**目前已接收的总字节数**。"""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise FileTransferError("分块数据无效")
        data = bytes(data)
        if len(data) > MAX_CHUNK_BYTES:
            # 协议规定 64KB,远超说明对端有问题;这里挡一下避免异常大的单次写入。
            raise FileTransferError("分块过大,协议不允许")

        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                # 绝不"顺手创建"一个传输:那等于让对端跳过 begin 的全部校验
                # (文件名消毒、大小上限、并发上限)直接往磁盘写东西。
                raise FileTransferError("未知的传输编号,请重新发起传输")

            if seq != transfer.next_seq:
                # 底层是 WebSocket over TCP,本身保证有序且不丢包。收到乱序 seq
                # 只可能是协议被破坏或有人在构造攻击(比如想跳过某段偏移做空洞写),
                # 因此不做重排缓冲(那本身就是内存耗尽的攻击面),直接中止这次传输。
                self._close_and_unlink(transfer)
                self._drop(transfer)
                raise FileTransferError(
                    f"分块顺序错误(期望 {transfer.next_seq},收到 {seq}),传输已中止"
                )

            if transfer.received + len(data) > transfer.declared_size:
                # 声明小文件却猛灌数据 —— 典型的磁盘填充攻击,立刻中止并清理。
                self._close_and_unlink(transfer)
                self._drop(transfer)
                raise FileTransferError("接收到的数据超过了声明的文件大小,传输已中止")

            try:
                transfer.handle.write(data)
            except OSError as exc:
                self._close_and_unlink(transfer)
                self._drop(transfer)
                raise FileTransferError(f"写入文件失败:{exc.strerror or exc}") from exc

            transfer.received += len(data)
            transfer.next_seq += 1
            return transfer.received

    def finish(self, transfer_id: int) -> Path:
        """收尾:校验字节数,再把临时文件原子改名为最终文件名,返回最终路径。"""
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                raise FileTransferError("未知的传输编号,请重新发起传输")

            if transfer.received != transfer.declared_size:
                # 少收了(对端提前发 file_end / 连接被截断)也要清理,
                # 绝不把残缺文件改名成正式文件名让用户误用。
                self._close_and_unlink(transfer)
                self._drop(transfer)
                raise FileTransferError(
                    f"文件不完整:声明 {transfer.declared_size} 字节,"
                    f"实际收到 {transfer.received} 字节"
                )

            try:
                transfer.handle.flush()
                # fsync 保证数据真正落到盘上再改名。否则断电后可能出现
                # "文件名是完整的,内容却是空洞"的最糟情况。
                os.fsync(transfer.handle.fileno())
            except OSError as exc:  # pragma: no cover - 取决于具体文件系统
                logger.warning("刷盘失败(继续尝试改名): %s", exc)
            try:
                transfer.handle.close()
            except OSError as exc:
                self._close_and_unlink(transfer)
                self._drop(transfer)
                raise FileTransferError(f"写入文件失败:{exc.strerror or exc}") from exc

            root = self._ensure_root()
            # 从 begin 到现在可能过了很久,预留的名字也许已经被别的程序占用了,
            # 所以在改名前重新挑一次(先释放自己的占位,让它有机会拿回原名)。
            self._reserved.discard(transfer.final_path.name)
            try:
                final_path = self._reserve_target(root, transfer.display_name)
            except FileTransferError:
                self._close_and_unlink(transfer)
                self._transfers.pop(transfer_id, None)
                raise
            transfer.final_path = final_path

            try:
                # os.replace 是原子操作(同一文件系统内),用户要么看不到这个文件,
                # 要么看到的就是完整文件,不存在"看到一半"的中间态。
                os.replace(transfer.temp_path, final_path)
            except OSError as exc:
                self._close_and_unlink(transfer)
                self._drop(transfer)
                raise FileTransferError(f"保存文件失败:{exc.strerror or exc}") from exc

            self._drop(transfer)

        logger.info("文件接收完成 id=%s name=%s", transfer_id, final_path.name)
        return final_path

    def abort(self, transfer_id: int) -> None:
        """取消传输并删除临时文件。

        对不存在的 transfer_id 静默返回:file_abort 与 file_end 可能竞争,
        也可能因为写入出错我们已经内部中止过了,这些情况都不该再报错刷屏。
        """
        with self._lock:
            transfer = self._transfers.get(transfer_id)
            if transfer is None:
                return
            self._close_and_unlink(transfer)
            self._drop(transfer)
        logger.info("文件传输已取消 id=%s", transfer_id)

    def cleanup_all(self) -> None:
        """会话结束时调用:中止全部在途传输,清理所有临时文件。保证不抛异常。"""
        with self._lock:
            transfers = list(self._transfers.values())
            for transfer in transfers:
                try:
                    self._close_and_unlink(transfer)
                except Exception:  # noqa: BLE001 - 清理路径必须吞掉一切异常
                    logger.exception("清理传输 %s 时出错", transfer.transfer_id)
                finally:
                    self._drop(transfer)
            self._transfers.clear()
            self._reserved.clear()
        if transfers:
            logger.info("会话结束,已清理 %d 个未完成的文件传输", len(transfers))

    @property
    def active_count(self) -> int:
        """当前在途(已 begin 未 finish/abort)的传输数量。"""
        with self._lock:
            return len(self._transfers)
