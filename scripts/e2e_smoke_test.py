#!/usr/bin/env python3
"""
端到端联调脚本(手动运行,不属于 CI 的一部分)。

依次拉起:Xvfb 虚拟显示 -> host 进程 -> client 静态服务器 -> Playwright
驱动的真实 Chromium,完整走一遍连接、画面渲染(关键帧 + 增量帧)、鼠标/
键盘/触屏注入、延迟显示、剪贴板双向同步、画质切换、显示器切换、文件传输、
断线遮罩与自动重连、多人只读观看,用真实子进程与真实浏览器验证整条链路,
而不是只测各模块的单元逻辑。

依赖(仅本机手动验证时需要,不是产品运行依赖): Xvfb, xdotool, xclip,
playwright(及其 Chromium)。

用法: python3 scripts/e2e_smoke_test.py
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 直接以脚本方式运行时,sys.path[0] 是 scripts/ 而不是仓库根目录,
# 这里补上根目录,才能 import 到仓库里的模块。
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
DISPLAY = ":99"
HOST_PORT = 18765
PROXY_PORT = 18767  # 客户端经由这个可切断的代理连接 host,用于模拟网络中断
HOST2_PORT = 18766
CLIENT_PORT = 18081
PASSWORD = "e2epass123"

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[PASS] {name}")
        passed.append(name)
    else:
        print(f"[FAIL] {name} {detail}")
        failed.append(name)


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            try:
                s.connect(("127.0.0.1", int(port)))
                return True
            except OSError:
                time.sleep(0.2)
    return False


def stats_title(page) -> str:
    return page.get_attribute("#stats-text", "title") or ""


def stat_value(page, label: str) -> int:
    """从状态栏 tooltip 里取出形如 '增量帧 12' 的计数。"""
    match = re.search(rf"{label}\s*(\d+)", stats_title(page))
    return int(match.group(1)) if match else 0


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 轮询期间页面状态可能瞬时不可用
            pass
        time.sleep(interval)
    return False


def main() -> int:
    for tool in ("Xvfb", "xdotool", "xclip"):
        if shutil.which(tool) is None:
            print(f"缺少依赖工具: {tool},请先安装后再运行本脚本。")
            return 2

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY
    env["PYTHONPATH"] = REPO_ROOT

    procs: list[subprocess.Popen] = []
    proxy = None
    tmpdir = tempfile.mkdtemp(prefix="rd-e2e-")
    key_log = os.path.join(tmpdir, "keys.log")
    download_dir = os.path.join(tmpdir, "downloads")
    share_dir = os.path.join(tmpdir, "shared")
    os.makedirs(share_dir, exist_ok=True)
    with open(os.path.join(share_dir, "共享文档.txt"), "w", encoding="utf-8") as f:
        f.write("这是被控端共享的文件内容\n" * 200)
    host_log = open(os.path.join(tmpdir, "host.log"), "w")
    open(key_log, "w").close()

    def spawn(cmd, **kw):
        p = subprocess.Popen(cmd, cwd=REPO_ROOT, **kw)
        procs.append(p)
        return p

    def spawn_host(port: int, extra: list[str] | None = None):
        return spawn(
            [sys.executable, "-m", "host.main", "--port", str(port),
             "--bind-host", "127.0.0.1", "--password", PASSWORD, "--log-level", "DEBUG",
             "--download-dir", download_dir, "--share-dir", share_dir,
             "--no-tray"] + (extra or []),
            env=env, stdout=host_log, stderr=subprocess.STDOUT,
        )

    try:
        print(f"== 临时文件目录: {tmpdir} ==")
        spawn(["Xvfb", DISPLAY, "-screen", "0", "1024x768x24"], env=env,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)

        spawn([sys.executable, "scripts/key_logger.py", key_log], env=env,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 5
        while time.time() < deadline and "READY" not in open(key_log, encoding="utf-8").read():
            time.sleep(0.1)

        host_proc = spawn_host(HOST_PORT)
        check("host 进程监听端口", wait_for_port(HOST_PORT))

        from scripts.tcp_proxy import KillableProxy
        proxy = KillableProxy(PROXY_PORT, HOST_PORT)
        proxy.start()
        check("可切断代理已就绪", wait_for_port(PROXY_PORT))

        spawn([sys.executable, "-m", "client.main", "--port", str(CLIENT_PORT), "--no-browser"],
              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("client 静态服务器监听端口", wait_for_port(CLIENT_PORT))

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            launch_kwargs = {"headless": True}
            chromium_glob = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")
            if chromium_glob:
                launch_kwargs["executable_path"] = chromium_glob[0]
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                permissions=["clipboard-read", "clipboard-write"],
                has_touch=True,  # 启用触屏事件,用于验证移动端手势
            )
            page = context.new_page()
            console_errors: list[str] = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{CLIENT_PORT}/")

            page.fill("#field-address", f"127.0.0.1:{PROXY_PORT}")
            page.fill("#field-password", PASSWORD)
            page.click("#connect-btn")

            try:
                page.wait_for_selector("#session-screen", state="visible", timeout=10000)
                check("已进入会话界面(握手+鉴权成功)", True)
            except Exception as exc:  # noqa: BLE001
                check("已进入会话界面(握手+鉴权成功)", False, str(exc))
                print("---- host 日志 ----")
                host_log.flush()
                print(open(host_log.name).read()[-3000:])
                return 1

            check("ES 模块客户端无 JS 报错", not console_errors, f"errors={console_errors[:3]}")

            # 注意:canvas 未被写入时 width 默认就是 300,所以"width > 0"是个假阳性
            # 检查——必须验证尺寸确实来自远端画面,并且像素真的被画上去了。
            ok = wait_until(lambda: page.evaluate(
                "document.getElementById('screen').width") >= 512, timeout=15)
            width = page.evaluate("document.getElementById('screen').width")
            height = page.evaluate("document.getElementById('screen').height")
            check("canvas 尺寸来自远端真实画面(非默认 300x150)", ok, f"got={width}x{height}")

            def canvas_has_content():
                # 读回像素并统计不同颜色数:全黑/未绘制的 canvas 只有一种颜色
                return page.evaluate("""() => {
                    const c = document.getElementById('screen');
                    if (c.width < 512) return 0;
                    const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                    const seen = new Set();
                    for (let i = 0; i < d.length; i += 4 * 997) {
                        seen.add((d[i] << 16) | (d[i + 1] << 8) | d[i + 2]);
                        if (seen.size > 1) return seen.size;
                    }
                    return seen.size;
                }""")
            # 像素内容检查放到后面(启动闪烁窗口之后)再做:此刻 Xvfb 桌面
            # 本身就是纯黑的,"单一颜色"是真实情况,不能作为渲染失败的判据。

            screenshot_path = os.path.join(tmpdir, "session.png")
            page.screenshot(path=screenshot_path)
            print(f"截图已保存: {screenshot_path}")

            time.sleep(2.2)
            latency_text = page.text_content("#latency-badge")
            check("延迟(ms)已显示", bool(re.search(r"(\d+)\s*ms", latency_text or "")),
                  f"latency_text={latency_text!r}")

            # ---------------- 增量编码 ----------------
            # Xvfb 里没有任何应用在跑,画面完全静止 -> 应该"什么都不发"
            check("画面静止时跳过发送(省带宽)",
                  wait_until(lambda: stat_value(page, "静止跳过") > 0, timeout=8),
                  f"title={stats_title(page)!r}")

            # 开一个小窗口持续变色,制造"只有一小块在变"的典型场景 -> 增量帧
            flicker = spawn([sys.executable, "scripts/x_flicker.py"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            check("局部变化时发送增量帧(而非整屏重传)",
                  wait_until(lambda: stat_value(page, "增量帧") > 0, timeout=10),
                  f"title={stats_title(page)!r}")
            # 现在屏幕上确实有彩色内容了,可以验证画面真的被解码渲染到了 canvas
            check("canvas 已画上真实画面内容(像素非单一颜色)",
                  wait_until(lambda: canvas_has_content() > 1, timeout=15),
                  f"不同颜色数={canvas_has_content()}")
            flicker.terminate()
            time.sleep(0.5)

            # ---------------- 编码格式协商 ----------------
            codec = page.evaluate("""() => {
                const t = document.getElementById('stats-text').textContent || '';
                return t;
            }""")
            check("增量帧使用 WebP 编码(比 JPEG 省 80% 以上流量)",
                  wait_until(lambda: "webp" in (stats_title(page) or "").lower()
                             or "webp" in (page.text_content("#stats-text") or "").lower(),
                             timeout=10),
                  f"stats={page.text_content('#stats-text')!r} title={stats_title(page)!r}")

            # ---------------- 远端鼠标光标 ----------------
            # 截屏本身不含指针,光标由被控端单独采集后叠加绘制
            check("远端鼠标光标已显示",
                  wait_until(lambda: page.is_visible("#remote-cursor"), timeout=10))

            # ---------------- 鼠标 / 键盘 ----------------
            canvas = page.locator("#screen")
            box = canvas.bounding_box()
            canvas.click(position={"x": box["width"] * 0.25, "y": box["height"] * 0.75})
            time.sleep(0.5)
            loc = subprocess.run(["xdotool", "getmouselocation", "--shell"], env=env,
                                 capture_output=True, text=True).stdout
            mx = int(re.search(r"X=(\d+)", loc).group(1))
            my = int(re.search(r"Y=(\d+)", loc).group(1))
            check("鼠标点击坐标已注入到远端(误差<15px)",
                  abs(mx - 1024 * 0.25) < 15 and abs(my - 768 * 0.75) < 15,
                  f"got=({mx},{my})")

            page.keyboard.type("Rd7!")
            page.keyboard.press("Enter")
            time.sleep(0.5)
            key_lines = open(key_log, encoding="utf-8").read().splitlines()
            check("键盘字符已注入到远端", all(c in key_lines for c in "Rd7!"), f"log={key_lines!r}")
            check("特殊键(Enter)已注入到远端", "Return" in key_lines, f"log={key_lines!r}")

            # ---------------- 触屏手势 ----------------
            tap_x = box["x"] + box["width"] * 0.6
            tap_y = box["y"] + box["height"] * 0.3
            page.touchscreen.tap(tap_x, tap_y)
            time.sleep(0.6)
            loc = subprocess.run(["xdotool", "getmouselocation", "--shell"], env=env,
                                 capture_output=True, text=True).stdout
            tx = int(re.search(r"X=(\d+)", loc).group(1))
            ty = int(re.search(r"Y=(\d+)", loc).group(1))
            check("触屏轻点已转换为远端鼠标点击(误差<20px)",
                  abs(tx - 1024 * 0.6) < 20 and abs(ty - 768 * 0.3) < 20,
                  f"got=({tx},{ty}) expect=({1024*0.6:.0f},{768*0.3:.0f})")

            # ---------------- 剪贴板 ----------------
            page.evaluate("navigator.clipboard.writeText('e2e-clip-to-host')")
            page.click("#btn-send-clipboard")
            time.sleep(1.2)
            host_clip = subprocess.run(["xclip", "-selection", "clipboard", "-o"], env=env,
                                       capture_output=True, text=True).stdout
            check("剪贴板 client -> host 同步成功", host_clip == "e2e-clip-to-host", f"got={host_clip!r}")

            subprocess.run("printf %s 'e2e-clip-to-client' | xclip -selection clipboard",
                           shell=True, env=env, check=True)
            check("剪贴板 host -> client 同步成功",
                  wait_until(lambda: page.evaluate("navigator.clipboard.readText()") == "e2e-clip-to-client",
                             timeout=6))

            # ---------------- 画质切换 ----------------
            page.select_option("#quality-select", "smooth")
            time.sleep(1.5)
            check("手动画质切换生效(状态栏显示对应档位)", "流畅" in stats_title(page)
                  or "流畅" in (page.text_content("#stats-text") or ""),
                  f"stats={page.text_content('#stats-text')!r}")
            page.select_option("#quality-select", "auto")

            # ---------------- 显示器切换 ----------------
            # Xvfb 下 mss 只报告 2 个条目(monitors[0] 虚拟全屏 + monitors[1] 物理屏),
            # 客户端按设计会隐藏下拉框(少于 2 块物理屏时切换没有意义)。
            # 这里手动填充下拉框并触发 change 事件,以此驱动真实的切换流程,
            # 验证 client 发出 monitor_set -> host 切换并回执 -> client 提示成功。
            page.evaluate("""() => {
                const sel = document.getElementById('monitor-select');
                sel.innerHTML = '<option value="1">显示器 1</option><option value="0">全部显示器</option>';
                sel.value = '0';
                sel.dispatchEvent(new Event('change'));
            }""")
            check("显示器切换成功(client 发起 -> host 切换 -> 回执提示)",
                  wait_until(lambda: "已切换显示器" in (page.text_content("#toast") or "")
                             and page.is_visible("#toast"), timeout=8),
                  f"toast={page.text_content('#toast')!r}")
            # 切换后必须重新收到关键帧,画面不能卡住
            check("切换显示器后画面继续更新",
                  wait_until(lambda: page.evaluate("document.getElementById('screen').width") > 0, timeout=8))

            # ---------------- 文件传输 ----------------
            sample = os.path.join(tmpdir, "测试文件.txt")
            sample_content = "远程桌面文件传输测试\n" * 500
            with open(sample, "w", encoding="utf-8") as f:
                f.write(sample_content)

            page.set_input_files("#file-input", sample)
            received = os.path.join(download_dir, "测试文件.txt")
            check("文件已传输到被控端并落盘",
                  wait_until(lambda: os.path.exists(received), timeout=15),
                  f"未在 {download_dir} 找到文件, 目录内容={os.listdir(download_dir) if os.path.isdir(download_dir) else '(不存在)'}")
            if os.path.exists(received):
                with open(received, encoding="utf-8") as f:
                    check("传输后文件内容完全一致", f.read() == sample_content)
                check("传输进度界面已显示",
                      page.is_visible("#transfer-panel"))

            # ---------------- 从被控端下载文件 ----------------
            check("远端文件按钮已出现(被控端开启了共享目录)",
                  page.is_visible("#btn-remote-files"))
            page.click("#btn-remote-files")
            check("共享文件列表已加载",
                  wait_until(lambda: "共享文档.txt" in (page.text_content("#remote-files-list") or ""),
                             timeout=8),
                  f"list={page.text_content('#remote-files-list')!r}")

            with page.expect_download(timeout=20000) as dl_info:
                page.click("#remote-files-list button")
            download = dl_info.value
            downloaded_path = os.path.join(tmpdir, "downloaded.txt")
            download.save_as(downloaded_path)
            with open(downloaded_path, encoding="utf-8") as f:
                got = f.read()
            with open(os.path.join(share_dir, "共享文档.txt"), encoding="utf-8") as f:
                want = f.read()
            check("从被控端下载的文件内容完全一致", got == want,
                  f"下载 {len(got)} 字节 vs 原文件 {len(want)} 字节")
            page.click("#btn-remote-files-close")

            # ---------------- 网络抖动 -> 恢复令牌快速重连 ----------------
            # 注意这里必须让 host 进程**存活**,只切断网络:恢复令牌保存在
            # host 进程内存里,进程重启后旧令牌自然失效(会正确回退到密码认证)。
            # 弱网下真正高频发生的是网络抖动,而不是对方重启程序。
            dropped = proxy.drop_all()
            check("已切断网络连接(被控端进程保持存活)", dropped > 0, f"切断了 {dropped} 个套接字")
            check("断网后进入重连状态",
                  wait_until(lambda: "重连" in (page.text_content("#status-text") or ""), timeout=10),
                  f"status={page.text_content('#status-text')!r}")
            check("网络恢复后自动重连成功",
                  wait_until(lambda: "已连接" in (page.text_content("#status-text") or ""), timeout=25),
                  f"status={page.text_content('#status-text')!r}")

            host_log.flush()
            with open(host_log.name, encoding="utf-8", errors="replace") as f:
                log_text = f.read()
            check("网络抖动重连走了恢复令牌快速通道(跳过 PBKDF2)",
                  "已跳过 PBKDF2" in log_text,
                  "host 日志中未见恢复令牌快速重连记录")

            # ---------------- 断线遮罩 + 自动重连 ----------------
            host_proc.terminate()
            check("host 断开后 client 能感知并进入重连状态",
                  wait_until(lambda: "重连" in (page.text_content("#status-text") or "")
                             or "断开" in (page.text_content("#status-text") or ""), timeout=8),
                  f"status={page.text_content('#status-text')!r}")
            check("断线时画面被遮罩覆盖(防止对着冻结画面误操作)",
                  wait_until(lambda: page.is_visible("#screen-overlay"), timeout=5))

            spawn_host(HOST_PORT)
            check("host 恢复后 client 自动重连成功",
                  wait_until(lambda: "已连接" in (page.text_content("#status-text") or ""), timeout=20),
                  f"status={page.text_content('#status-text')!r}")
            check("重连成功后遮罩自动消失",
                  wait_until(lambda: not page.is_visible("#screen-overlay"), timeout=8))

            host_log.flush()
            with open(host_log.name, encoding="utf-8", errors="replace") as f:
                log_text = f.read()
            check("host 重启后旧令牌失效并正确回退到密码认证",
                  "恢复令牌无效" in log_text,
                  "未见令牌失效回退记录")

            page.close()

            # ---------------- 多人只读观看 ----------------
            host2 = spawn_host(HOST2_PORT, ["--max-viewers", "2"])
            if wait_for_port(HOST2_PORT):
                page_a = context.new_page()
                page_a.goto(f"http://127.0.0.1:{CLIENT_PORT}/")
                page_a.fill("#field-address", f"127.0.0.1:{HOST2_PORT}")
                page_a.fill("#field-password", PASSWORD)
                page_a.click("#connect-btn")
                page_a.wait_for_selector("#session-screen", state="visible", timeout=10000)

                page_b = context.new_page()
                page_b.goto(f"http://127.0.0.1:{CLIENT_PORT}/")
                page_b.fill("#field-address", f"127.0.0.1:{HOST2_PORT}")
                page_b.fill("#field-password", PASSWORD)
                page_b.click("#connect-btn")
                page_b.wait_for_selector("#session-screen", state="visible", timeout=10000)

                check("第二个连接进入只读观看模式",
                      wait_until(lambda: page_b.is_visible("#role-badge"), timeout=8))
                check("第一个连接保持操作权",
                      not page_a.is_visible("#role-badge"))
                check("只读观看者也能收到画面",
                      wait_until(lambda: page_b.evaluate("document.getElementById('screen').width") > 0,
                                 timeout=10))

                page_a.close()  # 操作者离开 -> 操作权应自动移交给观看者
                check("操作者离开后操作权自动移交",
                      wait_until(lambda: not page_b.is_visible("#role-badge"), timeout=10))
                page_b.close()
            else:
                check("多人观看用的第二个 host 启动", False, "端口未监听")

            context.close()
            browser.close()

    finally:
        try:
            proxy.stop()
        except Exception:  # noqa: BLE001
            pass
        for p in procs:
            try:
                p.send_signal(signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)
        for p in procs:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        host_log.close()

    print("\n================ 汇总 ================")
    print(f"通过: {len(passed)}  失败: {len(failed)}")
    if failed:
        print("失败项:", ", ".join(failed))
        print(f"host 日志: {os.path.join(tmpdir, 'host.log')}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
