# 远程桌面(RemoteDesktop)

一个自研的远程桌面连接软件,**不依赖 Windows 自带的远程桌面(RDP)**,
主要为**高延迟、网络质量差的场景**设计:目标是在弱网下依然能"稳定连上、
看得清、点得准",而不是追求局域网内的极致画质。

📖 **[完整使用手册](docs/使用手册.md)** ← 第一次用请看这个

## 功能

- **弱网优先的自适应画质**:实时测量延迟,在 5 个档位间自动切换,快降慢升
- **增量传输**:只发送屏幕上变化的区域;画面静止时一个字节都不发
- **实时延迟显示**,并区分"网络慢"还是"对方电脑慢"
- **完整的鼠标键盘控制**,支持组合键、长按、滚轮
- **剪贴板双向同步**
- **文件传输**:拖拽即可把文件发到对方电脑
- **触屏支持**:手机/平板浏览器可用(轻点、拖动、长按右键、双指滚动)
- **多显示器切换**
- **多人只读观看**(可选),操作者离开时自动移交操作权
- **断线自动重连**,重连期间用遮罩防止对着冻结画面误操作
- **端到端加密**:密码永不上网,中转服务器无法窥探画面/按键/剪贴板
- **两种连接方式**:局域网直连,或经中转服务器穿透 NAT

## 三个组件

| 组件 | 运行在哪 | 是什么 |
|---|---|---|
| `host/` 被控端 | 需要被远程控制的电脑 | 常驻进程:截屏编码、注入鼠标键盘、同步剪贴板、接收文件 |
| `client/` 主控端 | 发起控制的电脑 | 一个网页(`client/static/`),`client/main.py` 只是本地起服务器 + 开浏览器的启动器 |
| `relay/` 中转服务器(可选) | 有公网 IP 的服务器 | 双方都在 NAT 后面时用来穿透 |

## 快速开始

### Windows(不需要装 Python)

1. 仓库 **Actions** 页 → 最新一次 `build-windows` → 下载 `remotedesktop-windows` 产物,解压
2. 在**被控端**电脑双击 `RemoteDesktop-Host.exe`,记下控制台显示的 IP 和密码
3. 在**主控端**电脑双击 `RemoteDesktop-Client.exe`,浏览器会自动打开,填入 IP 和密码

打 tag(`git tag v1.0.0 && git push origin v1.0.0`)会自动生成带一键安装包
`RemoteDesktopSetup.exe` 的 GitHub Release。详见 [build/README_WINDOWS_BUILD.md](build/README_WINDOWS_BUILD.md)。

### 从源码运行

```bash
python3 -m venv venv && source venv/bin/activate   # Windows 用 venv\Scripts\activate
pip install -r requirements.txt

python -m host.main      # 在被控端电脑上
python -m client.main    # 在主控端电脑上
```

### 中转模式(双方都连不通时)

在有公网 IP 的服务器上:

```bash
docker compose -f docker/docker-compose.yml up -d      # 或 python -m relay.main
```

被控端加上 `--relay ws://你的服务器:8770`,主控端在网页里选「中转」并填会话码。
部署细节见 [docs/deploy_relay.md](docs/deploy_relay.md)。

## 弱网下做了什么

远程桌面在弱网下最常见的失败不是"卡",而是**延迟越滚越大、鼠标越来越不跟手**——
根因是发送端在网络跟不上时仍持续产生新帧,数据在缓冲区越积越多(bufferbloat)。
本项目的应对:

1. **一帧一确认的窗口流控**:在途数据量始终有界,网络越差自然发得越慢,而不是堆积
2. **只传变化区域**:实测小块变化时流量仅为整屏传输的 8%;画面静止时完全不发送
3. **快降慢升的自适应画质**:变差立刻降档保操作,变好要连续多次确认才升档,避免画质闪烁
4. **控制信令优先**:鼠标键盘、剪贴板不会排在大视频帧后面
5. **断线指数退避自动重连**:弱网下短暂断线是常态

详细设计与同类方案(TeamViewer / AnyDesk / 向日葵 / VNC / RDP)对比见
[docs/architecture.md](docs/architecture.md)。

## 项目结构

```
common/     协议(protocol.py)、加密(crypto.py)、自适应画质算法(adaptive.py)
host/       被控端:采集(capture.py)、差分(delta.py)、推流(streamer.py)、
            输入注入(input_injector.py)、剪贴板、文件接收(filetransfer.py)、
            优先级发送队列(sendqueue.py)、托盘(tray.py)、会话编排(server.py)
client/     主控端:static/js/ 下是 ES 模块化的浏览器客户端,main.py 是本地启动器
relay/      中转服务器(零信任转发 + /healthz 健康检查 + 可选 TLS)
tests/      单元测试(248 个)
scripts/    端到端联调脚本
docs/       使用手册、架构设计、中转部署、relay 协议规范
build/      Windows 打包(PyInstaller + Inno Setup)
docker/     中转服务器容器化部署
.github/    CI:单元测试 + Windows 构建
```

## 测试

单元测试(不需要显示器,CI 每次 push 自动跑):

```bash
pip install pytest pytest-asyncio
pytest -q
```

端到端联调(真实拉起 host + client + Xvfb + Playwright 驱动真实 Chromium,
覆盖连接、增量编码、鼠标键盘触屏、剪贴板、文件传输、显示器切换、断线重连、
多人观看共 29 项检查):

```bash
sudo apt-get install -y xvfb xdotool xclip
pip install playwright && playwright install chromium
python3 scripts/e2e_smoke_test.py
```

## 安全

密码只在本地用于派生密钥(PBKDF2-HMAC-SHA256 + HKDF),**从不上网**;握手后所有
流量用 AES-256-GCM 加密并做会话内防重放,中转服务器只转发密文。密码错误会被限速。

这是一套做过明确工程取舍的实现,已知局限(未采用 ECDH 因而不具备前向安全性、
中转模式下限速会误伤等)在 [docs/architecture.md](docs/architecture.md) 的
「安全设计取舍说明」中如实列出。生产环境建议额外套一层 TLS。
