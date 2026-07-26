"""优先级发送队列测试。"""
from __future__ import annotations

import asyncio

import pytest

from host.sendqueue import (
    PRIORITY_CONTROL,
    PRIORITY_FILE,
    PRIORITY_VIDEO,
    PrioritySendQueue,
)


async def test_control_messages_jump_ahead_of_video():
    """核心用例:控制信令必须优先于视频帧发出。

    弱网下一个视频帧可能有几十上百 KB,如果用单一 FIFO,一条几十字节的
    剪贴板/心跳消息要排在它后面,延迟会明显变差。
    """
    q = PrioritySendQueue()
    q.put_nowait(b"video-1", PRIORITY_VIDEO)
    q.put_nowait(b"file-1", PRIORITY_FILE)
    q.put_nowait(b"control-1", PRIORITY_CONTROL)

    assert await q.get() == b"control-1"
    assert await q.get() == b"video-1"
    assert await q.get() == b"file-1"


async def test_same_priority_is_fifo():
    q = PrioritySendQueue()
    for i in range(5):
        q.put_nowait(f"c{i}".encode(), PRIORITY_CONTROL)
    got = [await q.get() for _ in range(5)]
    assert got == [b"c0", b"c1", b"c2", b"c3", b"c4"]


async def test_get_blocks_until_data_arrives():
    q = PrioritySendQueue()

    async def produce_later():
        await asyncio.sleep(0.05)
        q.put_nowait(b"late", PRIORITY_CONTROL)

    asyncio.create_task(produce_later())
    assert await asyncio.wait_for(q.get(), timeout=1.0) == b"late"


async def test_video_queue_drops_when_full_instead_of_growing():
    """视频档满时必须丢弃而不是无限堆积——过时的画面没有重传价值,
    堆积只会让延迟越来越大(bufferbloat)。"""
    q = PrioritySendQueue(maxsizes=(10, 2, 10))
    assert q.put_nowait(b"v1", PRIORITY_VIDEO) is True
    assert q.put_nowait(b"v2", PRIORITY_VIDEO) is True
    assert q.put_nowait(b"v3", PRIORITY_VIDEO) is False  # 被丢弃
    assert q.qsize(PRIORITY_VIDEO) == 2
    assert q.dropped_counts[PRIORITY_VIDEO] == 1


async def test_put_applies_backpressure_for_files():
    """文件分块不能丢(丢了文件就损坏),队列满时应等待而不是丢弃。"""
    q = PrioritySendQueue(maxsizes=(10, 10, 1))
    q.put_nowait(b"f1", PRIORITY_FILE)

    async def drain_later():
        await asyncio.sleep(0.05)
        await q.get()

    asyncio.create_task(drain_later())
    await asyncio.wait_for(q.put(b"f2", PRIORITY_FILE), timeout=2.0)
    assert q.dropped_counts[PRIORITY_FILE] == 0


async def test_interleaved_producers_still_respect_priority():
    q = PrioritySendQueue()
    order = []

    async def consumer():
        for _ in range(6):
            order.append(await q.get())

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)  # 让消费者先挂起在 get 上

    q.put_nowait(b"v1", PRIORITY_VIDEO)
    q.put_nowait(b"v2", PRIORITY_VIDEO)
    q.put_nowait(b"c1", PRIORITY_CONTROL)
    q.put_nowait(b"f1", PRIORITY_FILE)
    q.put_nowait(b"c2", PRIORITY_CONTROL)
    q.put_nowait(b"v3", PRIORITY_VIDEO)

    await asyncio.wait_for(task, timeout=2.0)
    # 第一条可能已被挂起的消费者立即取走,但其余必须严格按优先级顺序
    assert order[-1] == b"f1", f"文件分块应最后发出,实际顺序 {order}"
    assert order.index(b"c2") < order.index(b"v3"), f"控制消息应优先于后入队的视频帧: {order}"


async def test_total_size_and_qsize():
    q = PrioritySendQueue()
    q.put_nowait(b"a", PRIORITY_CONTROL)
    q.put_nowait(b"b", PRIORITY_VIDEO)
    q.put_nowait(b"c", PRIORITY_FILE)
    assert q.total_size() == 3
    assert q.qsize(PRIORITY_CONTROL) == 1
    assert q.qsize(PRIORITY_VIDEO) == 1
    assert q.qsize(PRIORITY_FILE) == 1


async def test_queue_survives_clear_and_refill():
    """回归:内部用 Event 做唤醒,清标志与入队之间不能出现丢唤醒导致的死等。"""
    q = PrioritySendQueue()
    for _ in range(50):
        q.put_nowait(b"x", PRIORITY_CONTROL)
        assert await asyncio.wait_for(q.get(), timeout=1.0) == b"x"
