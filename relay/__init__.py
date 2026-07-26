"""relay 中转服务器。

在 host 与 client 无法直连(NAT/防火墙)时,relay 负责把双方的原始
WebSocket 消息互相转发。relay **不参与、不理解**应用层握手与加密内容——
除了连接建立后的第一条注册/配对消息之外,relay 不会解析任何转发的数据。

详见 docs/relay_protocol.md。
"""
