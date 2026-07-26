"""
共享目录:让主控端可以从被控端**下载**文件。

安全前提:被控端不应该允许对方读取任意路径——那等于把整块硬盘暴露出去。
因此下载能力必须由机主显式开启(`--share-dir`),且只能访问该目录下**直接
子文件**,不递归进子目录、不跟随符号链接。

客户端请求文件时只能给出文件名(不是路径)。校验分三层:
1. 名字里不允许出现路径分隔符、`..`、空字节;
2. 拼接后 resolve(),要求其父目录严格等于共享目录的 resolve() 结果——
   这一层能挡住通过符号链接绕出去的情况;
3. 必须是常规文件(不是目录、设备、FIFO)。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("host.share")

MAX_LIST_ENTRIES = 500  # 目录里文件太多时截断,避免一条控制消息超过大小上限


class ShareError(Exception):
    """共享目录相关的错误,消息会回传给主控端展示,因此用可读的中文。"""


class ShareDirectory:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()

    def _resolved_root(self) -> Path:
        try:
            return self.root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ShareError("共享目录不存在或无法访问") from exc

    def list_files(self) -> list[dict]:
        """列出共享目录下的直接子文件(不递归)。"""
        root = self._resolved_root()
        if not root.is_dir():
            raise ShareError("共享路径不是一个目录")
        entries = []
        try:
            for item in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if len(entries) >= MAX_LIST_ENTRIES:
                    logger.info("共享目录文件过多,列表已截断到 %d 条", MAX_LIST_ENTRIES)
                    break
                try:
                    # 用 lstat 判断是否为符号链接:不跟随链接,避免把链接指向的
                    # 目录外文件暴露出去
                    if item.is_symlink() or not item.is_file():
                        continue
                    entries.append({"name": item.name, "size": item.stat().st_size})
                except OSError:
                    continue
        except OSError as exc:
            raise ShareError("读取共享目录失败") from exc
        return entries

    def resolve_file(self, name: str) -> Path:
        """把客户端给的文件名解析成真实路径,不合法则抛 ShareError。"""
        if not isinstance(name, str) or not name:
            raise ShareError("文件名无效")
        if "\x00" in name or "/" in name or "\\" in name or name in (".", ".."):
            raise ShareError("文件名无效")

        root = self._resolved_root()
        candidate = (root / name)
        if candidate.is_symlink():
            raise ShareError("不允许下载符号链接")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ShareError("文件不存在") from exc
        # 最后一道防线:确认它确实就在共享目录下(而不是被链接绕出去了)
        if resolved.parent != root:
            raise ShareError("文件不在共享目录内")
        if not resolved.is_file():
            raise ShareError("目标不是普通文件")
        return resolved

    def read_chunks(self, path: Path, chunk_size: int):
        """按块读取文件内容(生成器,阻塞 I/O,调用方需放到线程池里)。"""
        with open(path, "rb") as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    return
                yield data
