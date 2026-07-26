"""
带优先级的发送队列。

为什么需要优先级:host 与 client 之间只有一条 WebSocket 连接,视频帧、
控制信令(pong / 剪贴板 / 统计)、文件分块都挤在这条连接上。视频帧动辄
几十上百 KB,而控制信令通常只有几十字节。如果用单一 FIFO 队列,一条
"用户刚刚复制的文本"可能要排在一个大视频帧后面才能发出去,白白增加
延迟——而弱网下 `ws.send()` 本身就可能要花不少时间。

于是分三档:

    PRIORITY_CONTROL (0)  控制信令,必须最快送达(心跳、剪贴板、ack、错误)
    PRIORITY_VIDEO   (1)  视频帧,允许在拥塞时丢弃(旧画面没有重传价值)
    PRIORITY_FILE    (2)  文件分块,最低优先级(慢一点没关系,但不能丢)

写出协程总是先取高优先级档位里的数据。每一档都有各自的容量上限:

- 控制档满了说明连发送小消息都跟不上,此时丢弃是合理的降级(比如疯狂
  刷新的统计消息),但这种情况极罕见。
- 视频档特意只留很小的容量:一帧一确认的流控本身已经保证在途帧数很少,
  这里的上限是"万一"的兜底,宁可丢掉过时的帧,也不要让队列堆积成
  bufferbloat——这正是本项目在弱网下保持操作跟手的核心思路。
- 文件档满时**不能丢**(丢了文件就损坏了),所以 put 返回 False,由调用方
  改为等待(异步背压),而不是静默丢弃。
"""
from __future__ import annotations

import asyncio
from collections import deque

PRIORITY_CONTROL = 0
PRIORITY_VIDEO = 1
PRIORITY_FILE = 2

DEFAULT_MAXSIZES = (256, 4, 32)


class PrioritySendQueue:
    """三档优先级的发送队列,供单个写出协程消费。"""

    def __init__(self, maxsizes: tuple[int, int, int] = DEFAULT_MAXSIZES):
        self._queues: list[deque[bytes]] = [deque(), deque(), deque()]
        self._maxsizes = maxsizes
        self._event = asyncio.Event()
        self.dropped_counts = [0, 0, 0]

    def _try_put(self, payload: bytes, priority: int) -> bool:
        queue = self._queues[priority]
        if len(queue) >= self._maxsizes[priority]:
            return False
        queue.append(payload)
        self._event.set()
        return True

    def put_nowait(self, payload: bytes, priority: int = PRIORITY_CONTROL) -> bool:
        """入队。返回 False 表示该档位已满、本次数据被丢弃。"""
        if self._try_put(payload, priority):
            return True
        self.dropped_counts[priority] += 1
        return False

    async def put(self, payload: bytes, priority: int = PRIORITY_FILE) -> None:
        """入队并在队列满时等待(背压)。用于不允许丢弃的数据,例如文件分块。

        这里刻意不走 put_nowait:队列满时的等待重试属于正常的背压行为,
        不是"丢弃",不应污染 dropped_counts 这个用于诊断拥塞的计数。
        """
        while not self._try_put(payload, priority):
            await asyncio.sleep(0.02)

    def _pop_highest(self) -> bytes | None:
        for queue in self._queues:
            if queue:
                return queue.popleft()
        return None

    async def get(self) -> bytes:
        """取出下一条待发送数据,优先取高优先级档位。队列全空时挂起等待。"""
        while True:
            payload = self._pop_highest()
            if payload is not None:
                return payload
            # 先清标志再复查一次,关掉"清标志与生产者 set() 之间"的竞态窗口:
            # 若在 clear() 之后队列里其实已经有数据,这里能立刻取到,而不会
            # 因为信号被清掉而永久挂起。
            self._event.clear()
            payload = self._pop_highest()
            if payload is not None:
                return payload
            await self._event.wait()

    def qsize(self, priority: int) -> int:
        return len(self._queues[priority])

    def total_size(self) -> int:
        return sum(len(q) for q in self._queues)
