"""端到端联调辅助:一个可以随时"拔网线"的 TCP 转发代理。

不属于产品代码。用途是模拟真实的网络中断:让客户端连到代理,代理把流量
转发给被控端,需要制造断线时调用 drop_all() 切断所有活动连接——**被控端
进程本身保持存活**。

为什么需要它:Playwright 的 set_offline() 只影响新发起的请求,不会切断
已经建立的 WebSocket,因此测不出"网络抖动后自动重连"这条路径。而这条路径
恰恰是本项目最关心的场景(弱网下断线重连是常态),尤其是重连时能否用
恢复令牌跳过昂贵的 PBKDF2——这一点必须在被控端存活的前提下才成立。
"""
from __future__ import annotations

import socket
import threading


class KillableProxy:
    def __init__(self, listen_port: int, target_port: int, host: str = "127.0.0.1"):
        self.listen_port = listen_port
        self.target_port = target_port
        self.host = host
        self._server: socket.socket | None = None
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.listen_port))
        self._server.listen(16)
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection((self.host, self.target_port), timeout=5)
            except OSError:
                client.close()
                continue
            with self._lock:
                self._conns.extend([client, upstream])
            threading.Thread(target=self._pump, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client), daemon=True).start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.close()
                except OSError:
                    pass

    def drop_all(self) -> int:
        """切断当前所有活动连接(模拟网络中断),返回被切断的套接字数量。"""
        with self._lock:
            conns, self._conns = self._conns, []
        for sock in conns:
            try:
                sock.close()
            except OSError:
                pass
        return len(conns)

    def stop(self) -> None:
        self._running = False
        self.drop_all()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
