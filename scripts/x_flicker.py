"""端到端联调辅助:在屏幕一小块区域上持续变色,用来驱动"增量帧"。

不属于产品代码。Xvfb 里没有任何应用在跑,画面是完全静止的——那样只能验证
"静止时不发数据"这条路径,验证不了增量编码。这个脚本开一个小窗口并不断
改变它的颜色,制造"只有一小块区域在变"的典型远程桌面场景。

用法: python3 scripts/x_flicker.py [宽 高 x y]
"""
import sys
import time

from Xlib import X, display

width = int(sys.argv[1]) if len(sys.argv) > 1 else 120
height = int(sys.argv[2]) if len(sys.argv) > 2 else 120
pos_x = int(sys.argv[3]) if len(sys.argv) > 3 else 400
pos_y = int(sys.argv[4]) if len(sys.argv) > 4 else 300

d = display.Display()
screen = d.screen()
window = screen.root.create_window(
    pos_x, pos_y, width, height, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
    background_pixel=screen.white_pixel, event_mask=X.ExposureMask,
)
window.map()
d.sync()

gc = window.create_gc()
colors = [0xFF0000, 0x00FF00, 0x0000FF, 0xFFFF00, 0xFF00FF, 0x00FFFF]
i = 0
while True:
    gc.change(foreground=colors[i % len(colors)])
    window.fill_rectangle(gc, 0, 0, width, height)
    d.sync()
    i += 1
    time.sleep(0.2)
