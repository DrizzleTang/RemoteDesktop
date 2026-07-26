"""
剪贴板双向同步(host 侧)。

用轮询而不是系统级剪贴板事件监听,因为跨平台(尤其 Linux)可靠地监听
剪贴板变更事件成本很高;轮询间隔 0.8 秒对"复制粘贴"这种人类交互节奏
来说完全够用,同时避免了引入额外的平台相关依赖。

防回环:记录"最近一次由远端下发、由本地写入"的值,轮询检测到与之相同的
值时不重新广播出去,避免 host<->client 之间来回"回声"。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("host.clipboard")

POLL_INTERVAL_SECONDS = 0.8
MAX_CLIPBOARD_CHARS = 64 * 1024


class ClipboardUnavailableError(RuntimeError):
    pass


class ClipboardSync:
    def __init__(self, on_local_change: Callable[[str], None]):
        self._on_local_change = on_local_change
        self._last_value: str | None = None
        self._suppress_value: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._available = True

    def start(self) -> None:
        try:
            import pyperclip

            self._last_value = pyperclip.paste()
        except Exception as exc:  # noqa: BLE001 - 剪贴板不可用不应影响远程桌面主功能
            logger.warning("剪贴板不可用,已禁用剪贴板同步: %s", exc)
            self._available = False
            return
        self._thread = threading.Thread(target=self._poll_loop, name="clipboard-poll", daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        import pyperclip

        while not self._stop_event.is_set():
            time.sleep(POLL_INTERVAL_SECONDS)
            try:
                current = pyperclip.paste()
            except Exception as exc:  # noqa: BLE001
                logger.debug("读取剪贴板失败(忽略本次): %s", exc)
                continue
            if current == self._last_value:
                continue
            self._last_value = current
            if current == self._suppress_value:
                continue  # 这是我们自己刚写入的值,不要回传
            if not current or len(current) > MAX_CLIPBOARD_CHARS:
                continue
            try:
                self._on_local_change(current)
            except Exception:  # noqa: BLE001
                logger.exception("剪贴板变更回调执行失败")

    def apply_remote(self, text: str) -> None:
        if not self._available:
            return
        if len(text) > MAX_CLIPBOARD_CHARS:
            text = text[:MAX_CLIPBOARD_CHARS]
        try:
            import pyperclip

            self._suppress_value = text
            pyperclip.copy(text)
            self._last_value = text
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入剪贴板失败: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
