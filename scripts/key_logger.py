"""端到端联调辅助脚本:创建一个真正获得输入焦点的 X11 窗口,记录收到的按键。

不属于产品代码,只在 scripts/e2e_smoke_test.py 里被当作子进程启动,用来
独立验证"host 端 pynput 输入注入"确实把按键送达了一个真实的目标窗口。

技术说明:host 对可打印字符使用 pynput 的 KeyCode.from_char 注入,在 X11
后端这条路径底层用的是 XSendEvent(直接投递给当前拥有输入焦点的窗口),
而不是 XTestFakeKeyEvent——这是 pynput 为兼容各种键盘布局刻意采用的方式
(见 pynput 源码 _xorg.py 的注释)。XSendEvent 生成的合成事件只会被"确实
选择了 KeyPress 事件掩码、且正处于输入焦点"的窗口收到,不会被基于 X
RECORD 扩展的全局按键监听器(比如 pynput.keyboard.Listener 自己)捕获到
——RECORD 的"设备事件"分类只反映真实/XTEST 级别的输入,不包含 XSendEvent。
因此验证字符注入必须像一个真实应用程序那样:创建窗口、设置输入焦点、
选择 KeyPress 事件掩码,再读取事件队列,而不能用全局监听器。
"""
import sys

from Xlib import X, XK, display

log_path = sys.argv[1]

d = display.Display()
screen = d.screen()
window = screen.root.create_window(
    0, 0, 200, 200, 0,
    screen.root_depth,
    X.InputOutput,
    X.CopyFromParent,
    event_mask=X.KeyPressMask | X.KeyReleaseMask,
)
window.map()
d.sync()
window.set_input_focus(X.RevertToParent, X.CurrentTime)
d.sync()

with open(log_path, "a", encoding="utf-8") as f:
    f.write("READY\n")
    f.flush()

NAMED_KEYSYMS = {
    XK.string_to_keysym(name): name
    for name in ("Return", "Escape", "BackSpace", "Tab", "Delete")
}

while True:
    event = d.next_event()
    if event.type == X.KeyPress:
        shift = 1 if (event.state & X.ShiftMask) else 0
        keysym = d.keycode_to_keysym(event.detail, shift) or d.keycode_to_keysym(event.detail, 0)
        name = NAMED_KEYSYMS.get(keysym) or XK.keysym_to_string(keysym) or f"keysym:{keysym}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{name}\n")
            f.flush()
