"""
鼠标 / 键盘输入注入。

坐标约定:client 发送的是归一化坐标 (0~1),乘以当前"真实屏幕分辨率"
(而不是当前视频编码使用的缩放分辨率)得到绝对像素坐标——这样无论智能
模式如何调整视频清晰度,鼠标点击位置始终精确对应远端真实屏幕。

按键约定:client 对可打印字符(字母/数字/符号,受 Shift/输入法布局影响
后的最终字符)使用浏览器 `KeyboardEvent.key`,按下时做一次"点按"(press+
release),不追踪长按重复;对功能键/修饰键(方向键、Ctrl/Shift/Alt/Enter/
F1-F12 等)使用 layout-无关的 `KeyboardEvent.code`,并严格保留 down/up
语义,以支持组合键(如按住 Ctrl 再点字符)与长按(方向键连续移动)。
"""
from __future__ import annotations

import importlib
import threading


class InputUnavailableError(RuntimeError):
    """当前系统环境无法执行输入注入(例如 Linux 下没有可用的显示服务器)。"""


def _load_pynput():
    try:
        mouse_mod = importlib.import_module("pynput.mouse")
        keyboard_mod = importlib.import_module("pynput.keyboard")
    except ImportError as exc:  # pynput 在无显示环境下会在 import 阶段直接抛错
        raise InputUnavailableError(
            "无法初始化输入注入模块(pynput)。Linux 下请确认有可用的图形显示环境"
            f"(DISPLAY 已设置);原始错误: {exc}"
        ) from exc
    return mouse_mod, keyboard_mod


_SPECIAL_CODE_NAMES: dict[str, str] = {
    "Enter": "enter", "NumpadEnter": "enter",
    "Escape": "esc",
    "Backspace": "backspace",
    "Tab": "tab",
    "Space": "space",
    "ShiftLeft": "shift_l", "ShiftRight": "shift_r",
    "ControlLeft": "ctrl_l", "ControlRight": "ctrl_r",
    "AltLeft": "alt_l", "AltRight": "alt_r",
    "MetaLeft": "cmd_l", "MetaRight": "cmd_r",
    "CapsLock": "caps_lock",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "Home": "home", "End": "end", "PageUp": "page_up", "PageDown": "page_down",
    "Delete": "delete", "Insert": "insert",
    "PrintScreen": "print_screen", "ScrollLock": "scroll_lock", "Pause": "pause",
    "ContextMenu": "menu",
    **{f"F{i}": f"f{i}" for i in range(1, 25)},
}

_BUTTON_NAMES = {"left": "left", "right": "right", "middle": "middle"}


class InputInjector:
    def __init__(self, screen_width: int, screen_height: int):
        mouse_mod, keyboard_mod = _load_pynput()
        self._mouse = mouse_mod.Controller()
        self._keyboard = keyboard_mod.Controller()
        self._Button = mouse_mod.Button
        self._Key = keyboard_mod.Key
        self._KeyCode = keyboard_mod.KeyCode
        self.screen_width = screen_width
        self.screen_height = screen_height
        self._lock = threading.Lock()

        self._special_keys: dict[str, object] = {}
        for code, key_attr in _SPECIAL_CODE_NAMES.items():
            key_obj = getattr(self._Key, key_attr, None)
            if key_obj is not None:
                self._special_keys[code] = key_obj

    def update_screen_size(self, width: int, height: int) -> None:
        self.screen_width = width
        self.screen_height = height

    def _to_pixels(self, nx: float, ny: float) -> tuple[int, int]:
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        return round(nx * self.screen_width), round(ny * self.screen_height)

    def move(self, nx: float, ny: float) -> None:
        with self._lock:
            self._mouse.position = self._to_pixels(nx, ny)

    def button(self, btn: str, down: bool, nx: float | None = None, ny: float | None = None) -> None:
        button_obj = getattr(self._Button, _BUTTON_NAMES.get(btn, ""), None)
        if button_obj is None:
            return
        with self._lock:
            if nx is not None and ny is not None:
                self._mouse.position = self._to_pixels(nx, ny)
            if down:
                self._mouse.press(button_obj)
            else:
                self._mouse.release(button_obj)

    def scroll(self, dx: float, dy: float) -> None:
        with self._lock:
            self._mouse.scroll(dx, dy)

    def key(self, *, code: str | None, key_char: str | None, down: bool) -> None:
        with self._lock:
            if key_char is not None and len(key_char) == 1 and code not in self._special_keys:
                if down:
                    key_code = self._KeyCode.from_char(key_char)
                    self._keyboard.press(key_code)
                    self._keyboard.release(key_code)
                return
            special = self._special_keys.get(code or "")
            if special is None:
                return
            if down:
                self._keyboard.press(special)
            else:
                self._keyboard.release(special)
