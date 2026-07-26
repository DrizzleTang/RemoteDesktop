"""访问密码强度检查测试。"""
import pytest

from host import password_policy
from host.main import _random_password, _random_session_id


@pytest.mark.parametrize("weak", [
    "123456", "password", "admin", "qwerty", "111111", "abc123", "remotedesktop",
])
def test_common_weak_passwords_flagged(weak):
    assert password_policy.evaluate(weak), f"{weak} 应被判定为弱密码"
    assert password_policy.format_warning(weak) is not None


def test_short_password_flagged():
    problems = password_policy.evaluate("Ab3$")
    assert any("长度" in p for p in problems)


def test_all_digits_flagged():
    problems = password_policy.evaluate("94827163")
    assert any("数字" in p for p in problems)


def test_all_letters_flagged():
    problems = password_policy.evaluate("qwjklzxcv")
    assert any("字母" in p for p in problems)


def test_repeated_characters_flagged():
    problems = password_policy.evaluate("aaaaaaaa")
    assert any("相同" in p for p in problems)


@pytest.mark.parametrize("seq", ["123456", "abcdef", "654321", "fedcba"])
def test_sequential_password_flagged(seq):
    problems = password_policy.evaluate(seq)
    assert any("序列" in p for p in problems), f"{seq} 应被识别为连续序列"


def test_empty_password_flagged():
    assert password_policy.evaluate("") == ["密码为空"]


@pytest.mark.parametrize("strong", ["Kq7wZm2xVt", "correct-horse-battery-9", "Tr0ub4dor&3x"])
def test_strong_passwords_pass(strong):
    assert password_policy.evaluate(strong) == []
    assert password_policy.format_warning(strong) is None


def test_short_but_not_sequential_is_not_misflagged():
    # "ax9" 太短会被报长度问题,但不该被误判成连续序列
    problems = password_policy.evaluate("ax9")
    assert not any("序列" in p for p in problems)


def test_warning_text_is_actionable():
    warning = password_policy.format_warning("123456")
    assert "安全警告" in warning
    assert "自动生成" in warning  # 必须告诉用户怎么解决,而不只是报问题


def test_generated_password_is_strong():
    """自动生成的密码不应触发任何警告——否则每次启动都会吓用户一跳。"""
    for _ in range(50):
        assert password_policy.evaluate(_random_password()) == []


def test_generated_password_avoids_ambiguous_characters():
    """用户要照着控制台把密码念给对方听,不能有 0/O、1/l/I 这类易混字符。"""
    for _ in range(50):
        assert not (set(_random_password()) & set("0O1lI"))


def test_generated_session_id_matches_relay_regex():
    from relay.server import ID_RE

    for _ in range(50):
        assert ID_RE.match(_random_session_id()), "自动生成的会话码必须能通过中转服务器校验"
