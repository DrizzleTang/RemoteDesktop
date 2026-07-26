"""
访问密码强度评估。

自动生成的密码强度是够的,但用户用 `--password` 手动指定时,很可能会图省事
设成 `123456` 这类弱密码。远程桌面的密码一旦被猜中,对方就能完全操作这台
电脑,风险远高于一般网站账号,所以这里在启动时给出明确警告。

只警告、不强制拒绝:用户可能是在完全隔离的内网里做临时测试,强制拒绝会
打断合理用法。但警告必须写得足够醒目、说清后果。
"""
from __future__ import annotations

import string

MIN_RECOMMENDED_LENGTH = 8

# 常见弱密码(小写比较)。不追求穷尽,覆盖最常被自动化脚本优先尝试的即可。
COMMON_WEAK_PASSWORDS = {
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "password", "passwd", "admin", "root", "guest", "test", "demo",
    "qwerty", "abc123", "111111", "000000", "666666", "888888",
    "iloveyou", "letmein", "welcome", "monkey", "dragon",
    "remote", "desktop", "remotedesktop", "changeme", "secret",
}


def evaluate(password: str) -> list[str]:
    """返回该密码存在的问题列表(中文描述)。空列表表示没有明显问题。"""
    problems: list[str] = []
    if not password:
        return ["密码为空"]

    lowered = password.lower()
    if lowered in COMMON_WEAK_PASSWORDS:
        problems.append("这是一个广为人知的弱密码,几乎必然出现在攻击者的字典里")

    if len(password) < MIN_RECOMMENDED_LENGTH:
        problems.append(f"长度只有 {len(password)} 位,建议至少 {MIN_RECOMMENDED_LENGTH} 位")

    if password.isdigit():
        problems.append("全是数字,可穷举的组合非常少")
    elif password.isalpha():
        problems.append("全是字母,建议混入数字或符号")

    if len(set(password)) == 1:
        problems.append("所有字符都相同")

    if _is_sequential(password):
        problems.append("是连续的键盘/数字序列(如 123456、abcdef)")

    return problems


def _is_sequential(password: str) -> bool:
    """检测形如 123456 / abcdef / 654321 的连续序列。"""
    if len(password) < 4:
        return False
    lowered = password.lower()
    if not all(c in string.digits or c in string.ascii_lowercase for c in lowered):
        return False
    deltas = {ord(b) - ord(a) for a, b in zip(lowered, lowered[1:])}
    return deltas in ({1}, {-1})


def format_warning(password: str) -> str | None:
    """如果密码有问题,返回一段可直接打印到控制台的多行中文警告;否则返回 None。"""
    problems = evaluate(password)
    if not problems:
        return None
    lines = [
        "!" * 56,
        " 安全警告:你指定的访问密码强度不足",
    ]
    lines.extend(f"   - {p}" for p in problems)
    lines.extend([
        " 任何人猜中这个密码,就能完全操作这台电脑(看屏幕、动鼠标键盘、读剪贴板)。",
        " 建议:不加 --password 参数,让程序自动生成一个强随机密码。",
        "!" * 56,
    ])
    return "\n".join(lines)
