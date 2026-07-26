"""共享采集管线与共享目录的测试。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from host.hub import SharedCaptureHub
from host.share import ShareDirectory, ShareError
from tests.fakes import FakeCapture


# ---------------------------------------------------------------------------
# 共享采集管线
# ---------------------------------------------------------------------------

@pytest.fixture
async def hub():
    capture = FakeCapture()
    executor = ThreadPoolExecutor(max_workers=1)
    h = SharedCaptureHub(capture, executor)
    yield h, capture
    h.close()


async def test_single_capture_serves_multiple_subscribers(hub):
    """核心用例:N 个观看者只应产生一次采集,而不是 N 次。

    这正是这次重构要解决的问题——原来每个会话各自采集+差分,CPU 和内存
    都是 O(N)。
    """
    h, capture = hub
    subs = [h.subscribe() for _ in range(3)]
    ticks = await asyncio.gather(*(s.next_tick() for s in subs))

    assert len({t.seq for t in ticks}) == 1, "三个订阅者应拿到同一拍画面"
    assert capture.grab_count == 1, f"应只采集一次,实际采集了 {capture.grab_count} 次"
    for s in subs:
        s.close()


async def test_scaled_result_is_cached_across_subscribers(hub):
    """同一档位的多个会话共用一份缩放结果,不重复缩放。"""
    h, capture = hub
    sub = h.subscribe()
    tick = await sub.next_tick()

    a = await h.scaled(tick, 0.5)
    b = await h.scaled(tick, 0.5)
    assert a is b, "同一拍同一缩放比应命中缓存"

    c = await h.scaled(tick, 1.0)
    assert c is not a
    sub.close()


async def test_scaled_is_not_computed_when_screen_is_static(hub):
    """画面静止时连缩放都不该做——差分在原始分辨率完成,不需要缩放图。"""
    h, capture = hub
    sub = h.subscribe()
    await sub.next_tick()          # 首帧
    tick = await sub.next_tick()   # 画面未变
    assert tick.dirty == [] or tick.dirty is None
    assert tick._scaled == {}, "静止帧不应产生任何缩放结果"
    sub.close()


async def test_capture_loop_stops_when_last_subscriber_leaves(hub):
    h, capture = hub
    sub = h.subscribe()
    await sub.next_tick()
    assert h.subscriber_count == 1
    sub.close()
    assert h.subscriber_count == 0
    await asyncio.sleep(0.05)
    before = capture.grab_count
    await asyncio.sleep(0.2)
    assert capture.grab_count == before, "没有订阅者时应停止采集"


async def test_slow_subscriber_only_gets_latest_tick(hub):
    """跟不上的订阅者应直接拿到最新一帧,而不是被积压的旧帧拖住。
    旧画面没有补发价值,会话侧会把跳过期间的脏区域累积起来。"""
    h, capture = hub
    sub = h.subscribe()
    await sub.next_tick()
    await asyncio.sleep(0.25)  # 期间管线推进了好几拍
    tick = await sub.next_tick()
    assert tick.seq >= 2
    assert sub._queue.qsize() == 0, "队列不应积压多帧"
    sub.close()


async def test_set_monitor_resets_diff_state(hub):
    """切显示器后画面内容整个换了,必须重置差分状态强制重发整帧。"""
    h, capture = hub
    sub = h.subscribe()
    await sub.next_tick()
    assert await h.set_monitor(0) is True
    tick = await sub.next_tick()
    assert tick.dirty is None, "切换显示器后应要求关键帧"
    sub.close()


async def test_set_monitor_rejects_invalid_index(hub):
    h, _ = hub
    assert await h.set_monitor(99) is False


# ---------------------------------------------------------------------------
# 共享目录(被控端 -> 主控端下载)
# ---------------------------------------------------------------------------

def test_list_files_only_returns_regular_files(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("nope")

    share = ShareDirectory(tmp_path)
    names = {f["name"] for f in share.list_files()}
    assert names == {"a.txt", "b.bin"}, "不应列出子目录,也不应递归"


def test_list_files_reports_size(tmp_path):
    (tmp_path / "a.txt").write_text("12345")
    share = ShareDirectory(tmp_path)
    assert share.list_files()[0]["size"] == 5


def test_symlinks_are_not_listed(tmp_path):
    secret = tmp_path.parent / "outside_secret.txt"
    secret.write_text("绝密")
    (tmp_path / "link.txt").symlink_to(secret)
    share = ShareDirectory(tmp_path)
    assert share.list_files() == [], "符号链接不应出现在共享列表里"


@pytest.mark.parametrize("evil", [
    "../outside.txt", "../../etc/passwd", "/etc/passwd", "C:\\Windows\\win.ini",
    "..\\..\\evil", "sub/deep.txt", "a\x00.txt", ".", "..", "",
])
def test_path_traversal_is_rejected(tmp_path, evil):
    (tmp_path / "a.txt").write_text("ok")
    share = ShareDirectory(tmp_path)
    with pytest.raises(ShareError):
        share.resolve_file(evil)


def test_symlink_escape_is_rejected(tmp_path):
    """即使文件名本身干净,也不能通过符号链接把内容指向共享目录之外。"""
    secret = tmp_path.parent / "outside_secret2.txt"
    secret.write_text("绝密")
    (tmp_path / "innocent.txt").symlink_to(secret)
    share = ShareDirectory(tmp_path)
    with pytest.raises(ShareError):
        share.resolve_file("innocent.txt")


def test_resolve_returns_real_file(tmp_path):
    target = tmp_path / "报告.pdf"
    target.write_bytes(b"PDF")
    share = ShareDirectory(tmp_path)
    assert share.resolve_file("报告.pdf") == target.resolve()


def test_directory_cannot_be_downloaded(tmp_path):
    (tmp_path / "folder").mkdir()
    share = ShareDirectory(tmp_path)
    with pytest.raises(ShareError):
        share.resolve_file("folder")


def test_missing_share_dir_raises(tmp_path):
    share = ShareDirectory(tmp_path / "不存在")
    with pytest.raises(ShareError):
        share.list_files()


def test_read_chunks_roundtrip(tmp_path):
    data = bytes(range(256)) * 100
    target = tmp_path / "blob.bin"
    target.write_bytes(data)
    share = ShareDirectory(tmp_path)
    got = b"".join(share.read_chunks(target, 1024))
    assert got == data
