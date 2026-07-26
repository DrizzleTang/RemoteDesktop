#!/usr/bin/env python3
"""
端到端联调脚本(手动运行,不属于 CI 的一部分)。

依次拉起:Xvfb 虚拟显示 -> host 进程 -> client 静态服务器 -> Playwright
驱动的真实 Chromium,完整走一遍连接、画面渲染、鼠标/键盘注入、延迟显示、
剪贴板双向同步、画质切换、断线自动重连,用真实子进程与真实浏览器验证
整条链路,而不是只测各模块的单元逻辑。

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
DISPLAY = ":99"
HOST_PORT = 18765
CLIENT_PORT = 18081
PASSWORD = "e2epass123"

passed = []
failed = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[PASS] {name}")
        passed.append(name)
    else:
        print(f"[FAIL] {name} {detail}")
        failed.append(name)


def wait_for_port(port: str, timeout: float = 10.0) -> bool:
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


def main() -> int:
    for tool in ("Xvfb", "xdotool", "xclip"):
        if shutil.which(tool) is None:
            print(f"缺少依赖工具: {tool},请先安装后再运行本脚本。")
            return 2

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY
    env["PYTHONPATH"] = REPO_ROOT

    procs: list[subprocess.Popen] = []
    tmpdir = tempfile.mkdtemp(prefix="rd-e2e-")
    key_log = os.path.join(tmpdir, "keys.log")
    host_log = open(os.path.join(tmpdir, "host.log"), "w")
    open(key_log, "w").close()

    def spawn(cmd, **kw):
        p = subprocess.Popen(cmd, cwd=REPO_ROOT, **kw)
        procs.append(p)
        return p

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

        spawn(
            [sys.executable, "-m", "host.main", "--port", str(HOST_PORT),
             "--bind-host", "127.0.0.1", "--password", PASSWORD, "--log-level", "DEBUG"],
            env=env, stdout=host_log, stderr=subprocess.STDOUT,
        )
        check("host 进程监听端口", wait_for_port(str(HOST_PORT)))

        spawn([sys.executable, "-m", "client.main", "--port", str(CLIENT_PORT), "--no-browser"],
              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("client 静态服务器监听端口", wait_for_port(str(CLIENT_PORT)))

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            launch_kwargs = {"headless": True}
            chromium_glob = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")
            if chromium_glob:
                launch_kwargs["executable_path"] = chromium_glob[0]
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{CLIENT_PORT}/")

            page.fill("#field-address", f"127.0.0.1:{HOST_PORT}")
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

            deadline = time.time() + 10
            width = 0
            while time.time() < deadline:
                width = page.evaluate("document.getElementById('screen').width")
                if width and width > 0:
                    break
                time.sleep(0.3)
            check("画面帧已渲染到 canvas", bool(width and width > 0), f"width={width}")

            screenshot_path = os.path.join(tmpdir, "session.png")
            page.screenshot(path=screenshot_path)
            print(f"截图已保存: {screenshot_path}")

            time.sleep(2.2)
            latency_text = page.text_content("#latency-badge")
            m = re.search(r"(\d+)\s*ms", latency_text or "")
            check("延迟(ms)已显示", bool(m), f"latency_text={latency_text!r}")

            canvas = page.locator("#screen")
            box = canvas.bounding_box()
            canvas.click(position={"x": box["width"] * 0.25, "y": box["height"] * 0.75})
            time.sleep(0.5)
            loc = subprocess.run(["xdotool", "getmouselocation", "--shell"], env=env,
                                  capture_output=True, text=True).stdout
            mx = int(re.search(r"X=(\d+)", loc).group(1))
            my = int(re.search(r"Y=(\d+)", loc).group(1))
            expect_x, expect_y = 1024 * 0.25, 768 * 0.75
            check(
                "鼠标点击坐标已注入到远端(误差<15px)",
                abs(mx - expect_x) < 15 and abs(my - expect_y) < 15,
                f"got=({mx},{my}) expect=({expect_x},{expect_y})",
            )

            page.keyboard.type("Rd7!")
            page.keyboard.press("Enter")
            time.sleep(0.5)
            key_log_lines = open(key_log, encoding="utf-8").read().splitlines()
            check("键盘字符已注入到远端", all(c in key_log_lines for c in "Rd7!"),
                  f"log_lines={key_log_lines!r}")
            check("特殊键(Enter)已注入到远端", "Return" in key_log_lines, f"log_lines={key_log_lines!r}")

            page.evaluate("navigator.clipboard.writeText('e2e-clip-to-host')")
            page.click("#btn-send-clipboard")
            time.sleep(1.2)
            host_clip = subprocess.run(["xclip", "-selection", "clipboard", "-o"], env=env,
                                        capture_output=True, text=True).stdout
            check("剪贴板 client -> host 同步成功", host_clip == "e2e-clip-to-host", f"got={host_clip!r}")

            subprocess.run("printf %s 'e2e-clip-to-client' | xclip -selection clipboard",
                            shell=True, env=env, check=True)
            client_clip = None
            deadline = time.time() + 6
            while time.time() < deadline:
                client_clip = page.evaluate("navigator.clipboard.readText()")
                if client_clip == "e2e-clip-to-client":
                    break
                time.sleep(0.3)
            check("剪贴板 host -> client 同步成功", client_clip == "e2e-clip-to-client", f"got={client_clip!r}")

            page.select_option("#quality-select", "smooth")
            time.sleep(1.5)
            stats_text = page.text_content("#stats-text")
            check("手动画质切换生效(状态栏显示对应档位)", "流畅" in (stats_text or ""), f"stats={stats_text!r}")

            host_proc = procs[2]  # Xvfb, key_logger, host, client -> index 2 是 host
            host_proc.terminate()
            deadline = time.time() + 6
            status = ""
            while time.time() < deadline:
                status = page.text_content("#status-text") or ""
                if "重连" in status or "断开" in status:
                    break
                time.sleep(0.3)
            check("host 断开后 client 能感知并进入重连状态", "重连" in status or "断开" in status, f"status={status!r}")

            spawn(
                [sys.executable, "-m", "host.main", "--port", str(HOST_PORT),
                 "--bind-host", "127.0.0.1", "--password", PASSWORD, "--log-level", "DEBUG"],
                env=env, stdout=host_log, stderr=subprocess.STDOUT,
            )
            deadline = time.time() + 15
            status = ""
            while time.time() < deadline:
                status = page.text_content("#status-text") or ""
                if "已连接" in status:
                    break
                time.sleep(0.5)
            check("host 恢复后 client 自动重连成功", "已连接" in status, f"status={status!r}")

            context.close()
            browser.close()

    finally:
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
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
