# 远程桌面(RemoteDesktop)

一个自研的远程桌面连接软件,**不依赖 Windows 自带的远程桌面(RDP)**,
主要为**高延迟、网络质量差的场景**设计:目标是在弱网下依然能"稳定连上、
看得清、点得准",而不是追求局域网内的极致画质。

- 展示实时延迟(ms)
- 可操作对方的鼠标 / 键盘
- 剪贴板双向复制粘贴
- 清晰度可调节,默认「智能模式」自动检测网络状况并切换画质
- 支持直连和"中转服务器穿透 NAT"两种连接方式,断线自动重连
- 端到端加密(密码永不在网络上传输,中转服务器无法窥探屏幕/按键/剪贴板内容)

设计与实现细节见 [`docs/architecture.md`](docs/architecture.md)(架构图、
协议说明、弱网稳定性设计、与 TeamViewer / AnyDesk / 向日葵 / VNC / RDP 的
对比)。

## 三个组件

| 组件 | 运行在哪 | 是什么 |
|---|---|---|
| `host/` 被控端 | 需要被远程控制的电脑 | 常驻 Python 进程,负责截屏编码、接收并注入鼠标键盘、同步剪贴板 |
| `client/` 主控端 | 发起控制的电脑 | 一个网页(`client/static/`),`client/main.py` 只是本地起服务器 + 自动开浏览器的启动器 |
| `relay/` 中转服务器(可选) | 有公网 IP 的服务器 | 当 host 和 client 双方都在 NAT/防火墙之后、无法直连时用来穿透 |

## 快速开始(源码运行)

```bash
python3 -m venv venv && source venv/bin/activate   # Windows 用 venv\Scripts\activate
pip install -r requirements.txt
```

**在被控端电脑上**启动 host:

```bash
python -m host.main
```

会打印出连接密码(和可选的会话码);保持这个窗口开着。

**在主控端电脑上**启动 client:

```bash
python -m client.main
```

会自动打开浏览器,填入被控端的 `IP:端口` 和密码,点「连接」即可。

### 直连 vs 中转

- **直连**:双方在同一局域网,或被控端已做好端口映射/公网可达 ->
  client 里选「直连」,填被控端的 `IP:端口`。
- **中转**:双方都在 NAT/防火墙之后,互相连不通 -> 先在有公网 IP 的机器
  上跑中转服务器:

  ```bash
  python -m relay.main --port 8770
  ```

  host 启动时加上 `--relay ws://<中转服务器地址>:8770`,client 里选
  「中转」,填中转服务器地址 + host 启动时打印的会话码。

### 常用参数

```bash
python -m host.main --help
python -m client.main --help
python -m relay.main --help
```

比较常用的:`host` 的 `--password`(手动指定密码)、`--id`(手动指定
中转会话码)、`--monitor`(选择要采集的显示器)、`--relay`(中转服务器
地址);`client` 的 `--port`(本地网页端口)。

## Windows 一键使用(无需安装 Python)

不想装 Python 环境的话,可以直接拿打包好的 `.exe`:

1. **CI 自动构建(推荐)**:仓库 Actions 页面的 `build-windows`
   workflow 每次 push 都会在 `windows-latest` 上真实编译,下载
   `remotedesktop-windows` 构建产物,解压即得到 `RemoteDesktop-Host.exe`
   / `RemoteDesktop-Client.exe` / 绿色免安装 zip / 一键安装向导。
2. **打 tag 发布正式版**:`git tag v1.0.0 && git push origin v1.0.0`
   会自动创建 GitHub Release,附带一键安装包 `RemoteDesktopSetup.exe`
   与绿色版 `RemoteDesktop-Windows-Portable.zip`。
3. **本地自行构建**:装好 Python 3.10+ 后运行 `build\build_windows.bat`。

详见 [`build/README_WINDOWS_BUILD.md`](build/README_WINDOWS_BUILD.md)。
`relay` 是部署在云端 Linux 服务器上的组件,不需要、也不提供 Windows exe。

## 清晰度 / 画质模式

网页右上角「清晰度」下拉框:

- **智能(自动)**(默认):根据实时网络状况自动在「极速/流畅/均衡/清晰/
  高清」五档之间切换,网络变差立刻降档保操作流畅,变好后连续多次确认
  良好才慢慢升档,避免画质来回跳变。
- **手动固定档位**:极速 / 流畅 / 均衡 / 清晰 / 高清,固定使用该档位的
  分辨率缩放与 JPEG 质量,不再自动调整。
- **自定义**:自己拖动滑块设置分辨率缩放比例、JPEG 质量、最大帧率。

状态栏会实时显示延迟(ms,绿/黄/红分级)、当前画质档位、实际帧率与码率。

## 项目结构

```
common/     协议(protocol.py)、加密(crypto.py)、自适应画质算法(adaptive.py)——host/client 共用契约
host/       被控端:屏幕采集(capture.py)、输入注入(input_injector.py)、剪贴板(clipboard.py)、WebSocket 服务(server.py)
client/     主控端:client/static/ 下是纯浏览器网页(index.html/app.js/style.css),main.py 是本地启动器
relay/      中转服务器
tests/      单元测试(pytest)
scripts/    端到端联调脚本(scripts/e2e_smoke_test.py,需要 Xvfb/Playwright,见下文)
docs/       架构设计文档、relay 协议规范
build/      Windows 打包脚本(PyInstaller + Inno Setup)
.github/    GitHub Actions:Windows 构建流水线
```

## 测试

单元测试(协议编解码、加解密握手、自适应画质算法、认证限速、中转服务器
配对逻辑等,共 60+ 用例,不需要显示器/网络环境):

```bash
pip install pytest pytest-asyncio
pytest -q
```

端到端联调脚本(真实拉起 host + client + Xvfb 虚拟显示 + Playwright 驱动
真实 Chromium,完整走一遍连接/握手/画面渲染/鼠标键盘注入/延迟显示/剪贴板
双向同步/画质切换/断线重连,用于验证整条链路而非单个模块):

```bash
sudo apt-get install -y xvfb xdotool xclip   # Debian/Ubuntu
pip install playwright pytest-playwright
python3 scripts/e2e_smoke_test.py
```

## 安全说明

访问密码只用于本地派生加密密钥(PBKDF2-HMAC-SHA256 + HKDF-SHA256),
**从不在网络上传输**;握手完成后所有流量(视频画面、鼠标键盘、剪贴板)
均为 AES-256-GCM 加密,中转服务器只转发密文、无法窥探内容。密码错误会
被限速(单个来源 60 秒内失败 5 次后封禁 120 秒),减缓暴力破解。

这是一套刻意做了工程取舍的实现,不是形式化验证过的密码学协议;设计
细节与已知局限(例如未采用 ECDH 因而不具备前向安全性、中转模式下 host
观察到的来源 IP 是 relay 而非真实攻击者 IP)在
[`docs/architecture.md`](docs/architecture.md) 的「安全设计取舍说明」
一节中如实列出。生产环境建议额外在直连模式下用反向代理套一层
`wss://`(TLS)。
