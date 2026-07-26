"""容器 HEALTHCHECK 用的健康探针:请求 relay 的 /healthz 并校验返回内容。

为什么单独放一个文件,而不是在 Dockerfile 里写一行 ``python -c "..."``:
一行式的探针没法写注释,也没法处理"明文 / TLS 两种模式"这个分支。

relay 可能以两种模式运行:

- 不带 ``--cert/--key`` 时是明文,健康检查走 ``http://``;
- 带了 ``--cert/--key`` 时整个端口都是 TLS,健康检查必须走 ``https://``。

容器自己并不知道启动参数里有没有证书,所以这里先试 http、失败再试 https,
两者有一个成功就算健康。https 探测**故意不校验证书**:这是容器内部对
127.0.0.1 的自我探测,不存在中间人,而且证书上的域名通常和 127.0.0.1
对不上(自签证书更是直接校验不过),校验只会带来误报。

退出码约定:0 = 健康,1 = 不健康(docker 依此把容器标记为 unhealthy)。
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request

# 端口跟随 Dockerfile / compose 里的 RELAY_PORT 环境变量,默认 8770
PORT = os.environ.get("RELAY_PORT", "8770")
TIMEOUT_SECONDS = 5


def probe(url: str, context: ssl.SSLContext | None) -> dict:
    """请求一次 /healthz,返回解析后的 JSON;任何异常都向上抛。"""
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS, context=context) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP 状态码 {resp.status}")
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RuntimeError(f"响应体中的 status 不是 ok:{payload!r}")
    return payload


def main() -> int:
    insecure_context = ssl._create_unverified_context()
    attempts = (
        (f"http://127.0.0.1:{PORT}/healthz", None),
        (f"https://127.0.0.1:{PORT}/healthz", insecure_context),
    )

    errors = []
    for url, context in attempts:
        try:
            payload = probe(url, context)
        except Exception as exc:  # 连不上/超时/内容不对都算这一种模式探测失败
            errors.append(f"{url} -> {exc}")
            continue
        # 探测成功时把聚合计数打出来,docker inspect 的健康检查日志里能看到
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    for line in errors:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
