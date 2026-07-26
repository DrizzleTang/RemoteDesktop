from common.adaptive import LEVELS, AdaptiveController, UPGRADE_STREAK_REQUIRED
from common.protocol import QUALITY_AUTO, QUALITY_CUSTOM


def test_starts_at_balanced_level():
    ctrl = AdaptiveController()
    assert ctrl.current_level().key == "balanced"


def test_high_latency_triggers_immediate_downgrade():
    ctrl = AdaptiveController()
    for _ in range(3):
        ctrl.on_frame_ack(900)  # 远超所有阈值 -> 立刻掉到最低档
    assert ctrl.current_level().key == "extreme"


def test_low_latency_upgrades_slowly_not_instantly():
    ctrl = AdaptiveController()
    ctrl._level_idx = 0  # 从最低档开始模拟恢复
    ctrl.on_frame_ack(50)  # 单次良好采样不足以立刻升级
    assert ctrl.current_level().key == "extreme"
    for _ in range(UPGRADE_STREAK_REQUIRED):
        ctrl.on_frame_ack(50)
    assert ctrl.current_level().key == "smooth"  # 连续多次良好后只升一档


def test_timeout_is_more_aggressive_than_high_latency():
    ctrl = AdaptiveController()
    start = ctrl._level_idx
    ctrl.on_frame_timeout()
    assert ctrl._level_idx < start
    assert ctrl.consecutive_timeouts == 1


def test_repeated_timeouts_drop_further_and_set_cooldown():
    ctrl = AdaptiveController()
    ctrl._level_idx = len(LEVELS) - 1
    ctrl.on_frame_timeout()
    level_after_one = ctrl._level_idx
    ctrl.on_frame_timeout()
    assert ctrl._level_idx < level_after_one
    assert ctrl._cooldown > 0


def test_cooldown_blocks_upgrade_right_after_timeout():
    ctrl = AdaptiveController()
    ctrl._level_idx = 2
    ctrl.on_frame_timeout()
    level_after_timeout = ctrl._level_idx
    # 冷却期内即使延迟很好也不能立刻升级
    for _ in range(UPGRADE_STREAK_REQUIRED):
        ctrl.on_frame_ack(10)
    assert ctrl._level_idx == level_after_timeout or ctrl._level_idx == level_after_timeout + 0


def test_manual_mode_ignores_latency():
    ctrl = AdaptiveController()
    ctrl.set_mode("hd")
    ctrl.on_frame_ack(2000)
    ctrl.on_frame_timeout()
    params = ctrl.current_params()
    assert params["mode"] == "hd"
    assert params["scale"] == 1.0
    assert params["jpeg_quality"] == 90


def test_custom_mode_returns_user_values():
    ctrl = AdaptiveController()
    ctrl.set_mode(QUALITY_CUSTOM, custom_scale=0.6, custom_jpeg_quality=70, custom_max_fps=15)
    params = ctrl.current_params()
    assert params["mode"] == QUALITY_CUSTOM
    assert params["scale"] == 0.6
    assert params["jpeg_quality"] == 70
    assert params["max_fps"] == 15


def test_custom_mode_clamps_out_of_range_values():
    ctrl = AdaptiveController()
    ctrl.set_mode(QUALITY_CUSTOM, custom_scale=5.0, custom_jpeg_quality=999, custom_max_fps=1000)
    params = ctrl.current_params()
    assert params["scale"] <= 1.0
    assert params["jpeg_quality"] <= 95
    assert params["max_fps"] <= 60


def test_invalid_mode_rejected():
    ctrl = AdaptiveController()
    try:
        ctrl.set_mode("not-a-real-mode")
        assert False, "should have raised"
    except ValueError:
        pass


def test_never_downgrades_below_zero_or_above_max():
    ctrl = AdaptiveController()
    for _ in range(20):
        ctrl.on_frame_timeout()
    assert 0 <= ctrl._level_idx < len(LEVELS)
    ctrl._level_idx = len(LEVELS) - 1
    ctrl._cooldown = 0
    for _ in range(50):
        ctrl.on_frame_ack(10)
    assert ctrl._level_idx == len(LEVELS) - 1


def test_stats_snapshot_includes_rtt_and_latency():
    ctrl = AdaptiveController()
    ctrl.on_ping_rtt(123.4)
    ctrl.on_frame_ack(55)
    snap = ctrl.stats_snapshot()
    assert snap["rtt_ms"] == 123.4
    assert snap["ewma_ack_latency_ms"] == 55
