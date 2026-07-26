# 中转服务器(relay)部署指南

本文讲清楚三件事:中转服务器**是干什么的**、**怎么把它跑起来**(三种方式)、
以及**上线之后怎么配 TLS / 开防火墙 / 做监控 / 评估资源占用**。

协议细节见 [relay_protocol.md](./relay_protocol.md),整体架构见
[architecture.md](./architecture.md)。

---

## 一、中转服务器是干什么的

远程桌面的正常路径是 **client 直连 host**:client 直接连到被控端监听的端口。

但现实中双方经常都在 NAT / 防火墙后面(家里的路由器、公司内网、手机热点、
运营商大内网 CGNAT),谁都没有可被对方访问的公网地址,直连就连不通。

这时就需要一台**有公网 IP 的机器**做中间人:host 和 client 各自**主动向外**
连到这台机器(向外的连接 NAT 是允许的),relay 把两条连接"缝"在一起,
之后双方的每一条消息都由 relay 原样搬运给对端。

```
        [被控端 host]                         [主控端 client]
         在 NAT 后面                            在 NAT 后面
              |                                      |
              |  主动向外连接                主动向外连接  |
              +------------>  [ relay ]  <------------+
                             有公网 IP
                        只负责搬运字节,不解密
```

关键点(也是整套设计的安全前提):

- relay **只搬运,不理解**。除了连接建立时的第一条注册消息(里面只有会话码
  和角色),之后所有数据 relay 都原样转发,不解析、不修改。
- host 与 client 之间是**端到端加密**的。密钥协商和加密都发生在两端,
  relay 手里从头到尾只有密文。**即使 relay 被完全攻破,攻击者也拿不到
  屏幕画面、按键或剪贴板内容。**
- 配对靠一个 6~32 位的**会话码**(host 启动时生成并展示给用户)。
  relay 用它把两条连接配成一对,配对成功后会话码就不再有用。

> 只要有一端能被直连,就不需要 relay——直连延迟更低、也不消耗你的服务器带宽。

---

## 二、三种部署方式

### 前置条件

- 一台有**公网 IP** 的 Linux 服务器(1 核 512MB 的最低配云主机就够跑起来)。
- Python 3.11 或更高版本(用 Docker 部署则不需要,镜像里自带)。
- 一个可以对外开放的端口,默认 **8770**。

---

### 方式一:直接用 python 运行(最简单,适合先跑通)

```bash
# 1) 拉代码
git clone <本仓库地址> RemoteDesktop
cd RemoteDesktop

# 2) 只装 relay 需要的依赖
#    relay 只用到 websockets,不需要 mss/pynput/pyperclip/Pillow
#    (那些是被控端 host 专用的:屏幕采集、输入注入、剪贴板)
python3 -m pip install "websockets>=13.0"

# 3) 启动
python3 -m relay.main --host 0.0.0.0 --port 8770
```

看到下面这行就说明起来了:

```
relay 服务器已启动,监听 ws://0.0.0.0:8770 (最大连接数=500, 转发限速=8.0MB/s, 注册限流=20次/分钟)
```

常用参数(完整列表见 `python3 -m relay.main --help`):

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8770` | 监听端口 |
| `--cert` / `--key` | 无 | TLS 证书与私钥,同时提供即启用 `wss://`(见第三节) |
| `--max-connections` | `500` | 最大并发连接数(一个已配对会话占 2 条) |
| `--max-frame-bytes` | `16777216` | 单条消息上限(16MB) |
| `--forward-rate-limit-mbps` | `8.0` | 每个会话每个方向的转发限速,`<=0` 表示不限速 |
| `--register-rate-limit` | `20` | 每个来源 IP 每分钟允许的注册次数,`<=0` 表示不限流 |
| `--pairing-wait-timeout` | `600` | host 等待 client 配对的最长秒数 |
| `--log-level` | `INFO` | 日志级别 |

#### 用 systemd 常驻(生产环境推荐)

直接 `python3 -m relay.main` 会随着 SSH 断开而退出。写一个 unit 让它开机自启、
挂了自动重启:

```ini
# /etc/systemd/system/remotedesktop-relay.service
[Unit]
Description=RemoteDesktop relay 中转服务器
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# 不要用 root 跑:公网服务被攻破时,普通用户的损失面小得多
User=relay
WorkingDirectory=/opt/RemoteDesktop
ExecStart=/usr/bin/python3 -m relay.main --host 0.0.0.0 --port 8770
Restart=always
RestartSec=3
# 一点廉价的加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd --system --create-home relay
sudo systemctl daemon-reload
sudo systemctl enable --now remotedesktop-relay
sudo systemctl status remotedesktop-relay
journalctl -u remotedesktop-relay -f      # 看实时日志
```

---

### 方式二:Docker

镜像定义在 [`docker/Dockerfile.relay`](../docker/Dockerfile.relay)。
它基于 `python:3.11-slim`,**只装 websockets**、只 COPY `relay/` 目录、
以非 root 用户运行,并内置了调用 `/healthz` 的 `HEALTHCHECK`。

```bash
# 在仓库根目录执行(构建上下文必须是根目录,Dockerfile 里要 COPY relay/)
docker build -f docker/Dockerfile.relay -t remotedesktop-relay:latest .

docker run -d \
  --name relay \
  --restart unless-stopped \
  -p 8770:8770 \
  remotedesktop-relay:latest

docker logs -f relay                       # 看日志
docker inspect --format '{{.State.Health.Status}}' relay   # 看健康状态
```

---

### 方式三:docker compose(推荐,一条命令搞定)

配置在 [`docker/docker-compose.yml`](../docker/docker-compose.yml),已经带好了
端口映射、`restart: unless-stopped`、日志滚动,并把 TLS 证书挂载与资源限制
以注释形式准备好了,按需放开即可。

```bash
# 启动(在仓库根目录执行)
docker compose -f docker/docker-compose.yml up -d --build

# 看日志
docker compose -f docker/docker-compose.yml logs -f

# 更新代码后重建
docker compose -f docker/docker-compose.yml up -d --build

# 停止并删除容器
docker compose -f docker/docker-compose.yml down
```

---

## 三、配置 TLS(把 `ws://` 升级成 `wss://`)

### 为什么要配

这一点容易被误解,所以说清楚:

**应用层数据本来就已经是端到端加密的**,relay 看到的一直是密文。TLS
**不是**用来保护屏幕内容的——那件事已经做好了。

TLS 保护的是**握手阶段**。在 `ws://` 下,建立连接时的 HTTP Upgrade 请求
以及第一条注册消息(**里面带着会话码**)都是明文过网的。套一层 TLS 有两个
实际收益:

1. **隐藏会话码。** 会话码在网络上裸奔时,同一条链路上的旁路观察者可以读到它,
   然后抢在合法 client 之前用同一个会话码去配对。虽然他仍然过不了 host 的密码
   认证(拿不到任何画面),但足以让合法用户配对失败——一次实打实的拒绝服务。
2. **防止中间设备干扰握手。** 运营商、公司出口设备、酒店 Wi-Fi 之类的中间盒子
   会识别明文 HTTP 流量并"优化"它,明文的 WebSocket Upgrade 请求被改写甚至
   直接掐断的情况并不罕见。裹进 TLS 之后它们只能看到一条普通的加密连接。

**公网部署强烈建议开启 TLS。** 未开启时 relay 启动日志里会打一条 WARNING 提醒。

### 第 1 步:用 certbot 申请证书(Let's Encrypt,免费)

需要先有一个**解析到这台服务器的域名**(IP 是签不了证书的)。

```bash
sudo apt-get update && sudo apt-get install -y certbot

# standalone 模式会临时占用 80 端口完成域名校验,
# 所以要先确保 80 端口对外开放、且没有别的服务在监听
sudo certbot certonly --standalone -d relay.example.com
```

成功后证书在:

```
/etc/letsencrypt/live/relay.example.com/fullchain.pem   <- 传给 --cert
/etc/letsencrypt/live/relay.example.com/privkey.pem     <- 传给 --key
```

### 第 2 步:把证书传给 relay

```bash
python3 -m relay.main \
  --host 0.0.0.0 --port 8770 \
  --cert /etc/letsencrypt/live/relay.example.com/fullchain.pem \
  --key  /etc/letsencrypt/live/relay.example.com/privkey.pem
```

启动日志会变成 `监听 wss://0.0.0.0:8770`,并额外打印一行 TLS 已启用的说明。

- `--cert` 和 `--key` **必须同时提供**;只给一个会直接报错退出(而不是悄悄
  退化成明文,免得你以为自己开了 TLS 其实没开)。
- 文件不存在、私钥和证书不配套、私钥带密码、当前用户没有读权限,都会在
  **启动阶段**就报出中文错误信息并退出。

之后 host 与 client 填写中转地址时,把 `ws://` 换成 **`wss://`**:

```bash
# 被控端
python -m host.main --relay wss://relay.example.com:8770
```

client 网页里的中转服务器地址同样填 `wss://relay.example.com:8770`。

### 在 Docker 里用证书

放开 `docker/docker-compose.yml` 里 `volumes` 和 `command` 两段注释即可。
有两个坑要注意:

1. **不要只挂载 live 目录下的单个文件。** `/etc/letsencrypt/live/<域名>/*.pem`
   其实是指向 `../../archive/` 的符号链接,续期后链接目标会变,单文件绑定挂载
   会指向旧证书。整个 `/etc/letsencrypt` 只读挂进去最省事:
   `- /etc/letsencrypt:/etc/letsencrypt:ro`
2. **权限。** 容器里以非 root 用户 `relay` 运行,而 `privkey.pem` 默认只有 root
   能读(0600),直接挂载会因为读不到私钥而启动失败。推荐用 certbot 的
   deploy-hook 把证书复制到一个专用目录并放开读权限,再挂那个目录:

```bash
# /etc/letsencrypt/renewal-hooks/deploy/copy-to-relay.sh
#!/bin/sh
set -e
mkdir -p /opt/relay-certs
cp /etc/letsencrypt/live/relay.example.com/fullchain.pem /opt/relay-certs/
cp /etc/letsencrypt/live/relay.example.com/privkey.pem   /opt/relay-certs/
chmod 644 /opt/relay-certs/fullchain.pem
chmod 640 /opt/relay-certs/privkey.pem
# 让证书组可读,并把容器里的 relay 用户加进这个组;或者直接 chown 到对应 uid
docker restart remotedesktop-relay
```

```bash
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/copy-to-relay.sh
sudo certbot renew --dry-run     # 演练一次,确认 hook 能跑
```

> 证书 90 天到期,certbot 装好后一般会自带续期定时器
> (`systemctl list-timers | grep certbot` 确认)。**续期后 relay 需要重启**才会
>加载新证书——上面的 deploy-hook 里已经带了 `docker restart`,直接 python
> 运行的话把它换成 `systemctl restart remotedesktop-relay`。

### 本地自测用的自签证书

只想验证 TLS 通路能不能跑通、没有域名时:

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /tmp/k.pem -out /tmp/c.pem -days 365 -subj "/CN=localhost"

python3 -m relay.main --port 8770 --cert /tmp/c.pem --key /tmp/k.pem
```

自签证书**不会被客户端信任**,只适合本地联调,不要用在生产环境。

---

## 四、防火墙要放行哪些端口

| 端口 | 协议 | 什么时候需要 | 说明 |
| --- | --- | --- | --- |
| **8770** | TCP | **必须** | relay 监听端口,host 和 client 都要能连上(改了 `--port` 就放行对应端口) |
| 80 | TCP | 仅申请/续期证书时 | `certbot --standalone` 做域名校验用;用完可以关掉 |
| 22 | TCP | 你自己 SSH 用 | 建议限制来源 IP |

relay 只需要**入站** TCP 8770,不需要任何 UDP,也不需要额外的出站规则。

```bash
# ufw(Ubuntu/Debian)
sudo ufw allow 8770/tcp
sudo ufw status

# firewalld(CentOS/RHEL)
sudo firewall-cmd --permanent --add-port=8770/tcp
sudo firewall-cmd --reload
```

> **云服务器还有一层。** 阿里云/腾讯云/AWS 的「安全组」是独立于系统防火墙的,
> 必须在控制台里也放行 8770,否则本机 `curl` 通、外网连不上。

验证端口确实通了:

```bash
curl http://relay.example.com:8770/healthz     # 明文模式
curl https://relay.example.com:8770/healthz    # TLS 模式
```

---

## 五、用 `/healthz` 做监控

relay 内置了一个健康检查端点,直接在 WebSocket 的同一个端口上用普通 HTTP GET 访问:

```bash
$ curl http://127.0.0.1:8770/healthz
{"status": "ok", "waiting_hosts": 1, "paired_sessions": 3, "active_connections": 7, "uptime_seconds": 86400.12}
```

返回字段:

| 字段 | 含义 |
| --- | --- |
| `status` | 固定 `"ok"`,能返回就说明进程活着且事件循环没有卡死 |
| `waiting_hosts` | 已注册、正在等 client 来配对的 host 数量 |
| `paired_sessions` | 已配对、正在转发数据的会话数量 |
| `active_connections` | 当前 WebSocket 连接总数(一个已配对会话算 2 条) |
| `uptime_seconds` | 进程启动至今的秒数,可用来发现意外重启 |

**这个端点只返回聚合计数,不包含任何会话码、IP、用户标识。** 它被设计成即使
暴露在公网上也无所谓——但如果你不需要外部访问,用防火墙或反向代理把它限制在
内网当然更好。

其它路径(包括 `/`)的行为完全不变,仍然走正常的 WebSocket 升级流程。

### 接监控系统

```bash
# 1) Uptime Kuma / UptimeRobot 等:HTTP(s) 监控
#    URL 填 http://relay.example.com:8770/healthz
#    关键字监控填 "ok"

# 2) cron + 告警脚本:进程挂了就重启
* * * * * curl -fsS --max-time 5 http://127.0.0.1:8770/healthz >/dev/null \
          || systemctl restart remotedesktop-relay

# 3) Docker:镜像里已内置 HEALTHCHECK,直接看容器状态
docker inspect --format '{{.State.Health.Status}}' remotedesktop-relay
```

容器健康检查脚本是 [`docker/healthcheck.py`](../docker/healthcheck.py),它会先试
`http://` 再试 `https://`,所以**不管有没有开 TLS 都能正常工作**。

---

## 六、资源占用预估

relay 干的活非常轻:它不解码、不编码、不落盘,只是把收到的字节原样发给对端。
CPU 主要花在 TCP/TLS 收发和 WebSocket 帧的拆装上。

以下是**粗略估算**,实际值取决于画面复杂度、分辨率和网络质量,请以自己的压测为准:

| 项目 | 估算 | 说明 |
| --- | --- | --- |
| 内存(空载) | 约 40~60 MB | Python 解释器 + websockets 库的基础开销 |
| 内存(每个会话) | 约 0.2~1 MB | 两条连接的收发缓冲;突发大帧时会短时升高 |
| 内存(默认满载 250 会话) | 约 200~350 MB | `--max-connections 500` ÷ 2 |
| CPU | 每 10 个活跃会话约 0.1~0.3 核 | 只做字节搬运;开 TLS 会再多一点加解密开销 |
| 带宽 | **瓶颈通常在这里** | 见下 |

**带宽才是真正的成本。** 一个会话的画面流经 relay 时要**进一次、出一次**:
host 发 1 MB/s 给 relay,relay 就要发 1 MB/s 给 client。典型的远程桌面码率在
0.2~2 Mbps(静止画面几乎为 0,拖窗口/看视频时飙升),所以:

- 10 个并发会话、平均 1.5 Mbps → 出口带宽约 15 Mbps。
- 云主机一般**按出口流量计费**,长期跑要留意账单。
- 默认 `--forward-rate-limit-mbps 8.0` 给每个会话每个方向设了 8 MB/s 的**上限**
  (这是保护 relay 不被单个会话打爆的封顶值,不是常态用量);超限时 relay
  会**丢弃**当前消息而不是缓冲排队——排队只会让延迟雪崩。

**选型建议:** 1 核 1GB、带宽 5~10 Mbps 的入门云主机足以支撑十几个并发会话。
先按最低配起,盯着 `/healthz` 里的 `paired_sessions` 和监控面板上的带宽曲线再决定要不要升级。

---

## 七、安全注意事项

### relay 能看到什么、不能看到什么

**看不到(重要):**

- ❌ 屏幕画面、按键、鼠标动作、剪贴板内容 —— 全是 AES-256-GCM 密文。
- ❌ 连接密码 —— 密码从不在网络上传输,只参与两端本地的密钥推导。
- ❌ 任何应用层报文的结构 —— relay 配对之后就只是个字节管道。

**能看到:**

- ✅ 会话码(配对必需)、双方的 IP 和端口、流量大小与时间。
- ✅ 在 `ws://` 明文模式下,链路上的旁路观察者也能看到会话码。→ **所以要开 TLS**。

**结论:即使 relay 被完全攻破,攻击者拿到的也只是加密流量和会话码,
拿不到任何屏幕或按键内容。** 这是端到端加密带来的实实在在的好处,也意味着
你可以放心地把 relay 部署在一台不那么可信的廉价机器上。

### 上线检查清单

- [ ] **开 TLS。** 公网部署配好 `--cert/--key`,隐藏会话码、避免握手被中间设备干扰。
- [ ] **别用 root 跑。** systemd 里指定 `User=`,Docker 镜像已内置非 root 用户。
- [ ] **保留注册限流。** 默认 `--register-rate-limit 20`(每 IP 每分钟 20 次)
      是抵御会话码枚举的主要手段,除非有明确理由,不要设成 0 关掉它。
- [ ] **保留连接数上限。** `--max-connections` 防止连接耗尽把内存吃光。
- [ ] **最小化开放端口。** 只放行 8770(和你自己的 SSH),别把 relay 所在机器
      当跳板机用。
- [ ] **及时打补丁。** `pip install -U websockets`,或重新构建 Docker 镜像
      (基础镜像 `python:3.11-slim` 的安全更新也会一起带进来)。
- [ ] **注意日志。** relay 的日志里会出现会话码(用于排障)。会话码是一次性的、
      用完即弃,但仍然建议按需调低日志级别、并给日志文件设好权限和轮转。

### 已知局限

- **host 看到的来源 IP 是 relay 的 IP**,不是 client 的真实 IP。所以 host 侧
  基于 IP 的认证限流在中转模式下粒度会变粗(所有中转来的连接看起来同源)。
- 当前握手**不具备前向安全性**(未采用 ECDH):如果密码在未来泄露,且攻击者
  完整录下了历史流量,理论上可以解密那些历史会话。
- relay 是一个**单点**:它挂了,所有中转中的会话都会断(host/client 会自动重连,
  但需要 relay 恢复)。对可用性有要求就上多台 + 前面挂负载均衡——注意
  **同一个会话的 host 和 client 必须落到同一台 relay**(会话状态在进程内存里,
  不共享),所以要按会话码做一致性哈希,不能用轮询。

---

## 八、排障速查

| 现象 | 可能原因 | 怎么查 |
| --- | --- | --- |
| 本机 curl 通,外网连不上 | 云厂商安全组没放行 | 控制台安全组加 8770/tcp 入站规则 |
| 客户端连接立刻断开 | 会话码格式不对(必须 6~32 位字母数字) | 看 relay 日志里的 `invalid_id` |
| 提示 `host_not_found` | host 还没连上 relay,或会话码输错,或 host 等待超过 10 分钟被回收 | 先确认 host 端已连上并显示了会话码 |
| 提示 `id_in_use` | 同一个会话码已经有 host 在等或已配对 | 换一个会话码,或等旧会话结束 |
| 提示 `rate_limited` | 该 IP 一分钟内注册超过 20 次 | 稍等一分钟;确属正常业务可调大 `--register-rate-limit` |
| 启动报"证书/私钥文件不存在" | 路径写错或没有读权限 | `ls -l` 确认路径;非 root 运行时注意 `/etc/letsencrypt` 的权限 |
| 开了 TLS 后连不上 | 客户端仍在用 `ws://` | 地址改成 `wss://`;自签证书客户端不信任 |
| 画面卡顿但网络没跑满 | 触发了转发限速,消息被丢弃 | 日志调到 `--log-level DEBUG` 看有没有"超出限速"记录,按需调大 `--forward-rate-limit-mbps` |
