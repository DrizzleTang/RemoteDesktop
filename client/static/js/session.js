'use strict';
/*
 * 会话管理:WebSocket 连接、握手、消息收发、断线自动重连。
 *
 * 对外通过一个极简的事件订阅接口把解析好的消息交给上层(渲染/UI/输入模块),
 * 自身不碰任何 DOM——这样连接逻辑可以独立于界面演进。
 */

import { SessionCipher, CryptoError, validateKexInit } from './crypto.js';
import {
  MSG_CONTROL, MSG_VIDEO_FRAME, MSG_VIDEO_DELTA,
  decodeKeyframe, decodeDelta, ProtocolError,
} from './protocol.js';

const PING_INTERVAL_MS = 1000;
const MAX_RECONNECT_DELAY_MS = 10000;

function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function safeJsonParse(text) {
  try {
    const parsed = JSON.parse(text);
    return isPlainObject(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

const RELAY_ERROR_TEXT = {
  invalid_id: '会话码格式不正确(应为 6-32 位字母或数字)',
  invalid_role: '内部协议错误(role)',
  id_in_use: '该会话码已被占用,请让被控端重新生成',
  host_not_found: '未找到该会话码对应的被控端,请确认对方已启动且会话码正确',
  rate_limited: '连接尝试过于频繁,请稍后再试',
};

const REJECT_TEXT = {
  busy: '被控端当前连接数已满',
  rate_limited: '近期密码错误次数过多,被控端已临时拒绝连接,请稍后再试',
};

export class Session {
  constructor() {
    this.ws = null;
    this.cipher = null;
    this.params = null;
    this.connected = false;
    this.manualClose = false;
    this.handshakeStarted = false;
    this.canControl = false;

    this._handlers = new Map();
    this._reconnectAttempts = 0;
    this._reconnectTimer = null;
    this._pingTimer = null;
    this._pingSeq = 0;
    this._pingInflight = new Map();
    this._startPerf = performance.now();
  }

  // ---- 极简事件订阅 ----
  on(event, handler) {
    if (!this._handlers.has(event)) this._handlers.set(event, []);
    this._handlers.get(event).push(handler);
  }

  _emit(event, payload) {
    for (const handler of this._handlers.get(event) || []) {
      try {
        handler(payload);
      } catch (err) {
        console.error(`事件处理出错 [${event}]`, err);
      }
    }
  }

  nowMs() {
    return Math.round(performance.now() - this._startPerf);
  }

  // ------------------------------------------------------------------
  // 连接
  // ------------------------------------------------------------------

  connect(params) {
    this.params = params;
    this.manualClose = false;
    this._emit('status', { kind: 'connecting', text: '正在连接...' });

    let ws;
    try {
      ws = new WebSocket(buildWsUrl(params));
    } catch (err) {
      this._emit('fatal', `地址格式不正确: ${err.message}`);
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    this.cipher = null;
    this.handshakeStarted = false;

    ws.onopen = () => {
      if (params.mode === 'relay') {
        ws.send(JSON.stringify({ t: 'relay_register', role: 'client', id: params.relayId, v: 1 }));
        this._emit('status', { kind: 'connecting', text: '正在通过中转服务器配对...' });
      }
    };
    ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        this._onPlaintext(safeJsonParse(event.data));
      } else {
        this._onEncrypted(new Uint8Array(event.data)).catch((err) => {
          console.warn('处理加密消息失败', err);
        });
      }
    };
    ws.onerror = () => { /* onclose 紧随其后统一处理 */ };
    ws.onclose = () => this._onClose();
  }

  disconnect() {
    this.manualClose = true;
    clearTimeout(this._reconnectTimer);
    this._stopPing();
    if (this.ws) {
      this.sendControl({ t: 'bye' });
      try { this.ws.close(); } catch { /* 忽略 */ }
    }
    this.connected = false;
    this._emit('status', { kind: '', text: '未连接' });
  }

  _onClose() {
    this._stopPing();
    this.ws = null;
    this.connected = false;
    this.canControl = false;
    if (this.manualClose) {
      this._emit('status', { kind: '', text: '未连接' });
      return;
    }
    this._emit('disconnected', null);
    this._emit('status', { kind: 'reconnecting', text: '连接已断开,正在自动重连...' });
    this._scheduleReconnect();
  }

  _scheduleReconnect() {
    this._reconnectAttempts += 1;
    // 指数退避,封顶 10 秒:弱网下短暂断线是常态,自动重连是"稳定连接"的必要部分
    const delay = Math.min(MAX_RECONNECT_DELAY_MS, 1000 * 2 ** (this._reconnectAttempts - 1));
    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(() => {
      if (!this.manualClose && this.params) this.connect(this.params);
    }, delay);
  }

  // ------------------------------------------------------------------
  // 消息处理
  // ------------------------------------------------------------------

  _onPlaintext(msg) {
    switch (msg.t) {
      case 'relay_paired':
        this._emit('status', { kind: 'connecting', text: '已与被控端配对,正在建立加密连接...' });
        break;
      case 'relay_error':
        this._fail(`连接失败: ${RELAY_ERROR_TEXT[msg.reason] || msg.reason || '未知错误'}`);
        break;
      case 'kex_init':
        this._onKexInit(msg);
        break;
      case 'kex_reject':
        this._fail(`连接被拒绝: ${REJECT_TEXT[msg.reason] || msg.reason || '未知原因'}`);
        break;
      default:
        break;
    }
  }

  _fail(message) {
    this.manualClose = true;
    clearTimeout(this._reconnectTimer);
    if (this.ws) {
      try { this.ws.close(); } catch { /* 忽略 */ }
    }
    this._emit('fatal', message);
  }

  async _onKexInit(msg) {
    // 握手只应发生一次。会话建立后再收到 kex_init 一律忽略,防止恶意中转方
    // 或链路中间人在会话中途注入新的 kex_init 让客户端重新派生密钥。
    if (this.handshakeStarted) {
      console.warn('忽略重复的 kex_init');
      return;
    }
    this.handshakeStarted = true;

    let validated;
    try {
      validated = validateKexInit(msg);
    } catch (err) {
      this._fail(`握手参数异常,已拒绝连接: ${err.message}`);
      return;
    }

    try {
      this.cipher = await SessionCipher.derive(
        this.params.password, validated.saltBytes, validated.iterations,
      );
      this._emit('status', { kind: 'connecting', text: '正在验证密码...' });
      await this.sendControl({ t: 'hello', client_name: navigator.userAgent.slice(0, 60) });
      this._startPing();
    } catch (err) {
      console.error('密钥协商失败', err);
      this._fail('密钥协商失败,请确认浏览器支持 WebCrypto(需 HTTPS 或 localhost 环境)');
    }
  }

  async _onEncrypted(wireBytes) {
    if (!this.cipher) return;
    let plaintext;
    try {
      plaintext = await this.cipher.decrypt(wireBytes);
    } catch (err) {
      if (!(err instanceof CryptoError)) console.warn('解密失败,丢弃该消息', err);
      return;
    }
    if (plaintext.length < 1) return;
    const kind = plaintext[0];
    const body = plaintext.subarray(1);

    try {
      if (kind === MSG_CONTROL) {
        let msg;
        try {
          msg = JSON.parse(new TextDecoder().decode(body));
        } catch {
          return;
        }
        if (!isPlainObject(msg)) return;
        this._onControl(msg);
      } else if (kind === MSG_VIDEO_FRAME) {
        this._emit('keyframe', decodeKeyframe(body));
      } else if (kind === MSG_VIDEO_DELTA) {
        this._emit('delta', decodeDelta(body));
      }
    } catch (err) {
      if (err instanceof ProtocolError) {
        console.warn('丢弃格式异常的帧:', err.message);
      } else {
        console.error('处理消息时出错', err);
      }
    }
  }

  _onControl(msg) {
    switch (msg.t) {
      case 'hello_ack':
        this.connected = true;
        this.canControl = msg.can_control !== false;
        this._reconnectAttempts = 0;
        this._emit('ready', msg);
        break;
      case 'pong': {
        const sentAt = this._pingInflight.get(msg.n);
        if (sentAt === undefined) return;
        this._pingInflight.delete(msg.n);
        this._emit('latency', performance.now() - sentAt);
        break;
      }
      case 'stats':
        this._emit('stats', msg);
        break;
      case 'clip':
        if (typeof msg.text === 'string') this._emit('clipboard', msg.text);
        break;
      case 'monitor_info':
        this._emit('monitor', msg);
        break;
      case 'viewer_info':
        this.canControl = msg.can_control === true;
        this._emit('viewer', msg);
        break;
      case 'file_progress':
      case 'file_done':
      case 'file_error':
        this._emit('file', msg);
        break;
      default:
        break;
    }
  }

  // ------------------------------------------------------------------
  // 发送
  // ------------------------------------------------------------------

  get isOpen() {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN && this.cipher !== null;
  }

  async sendControl(obj) {
    if (!this.isOpen) return;
    const bytes = new TextEncoder().encode(JSON.stringify(obj));
    const plaintext = new Uint8Array(1 + bytes.length);
    plaintext[0] = MSG_CONTROL;
    plaintext.set(bytes, 1);
    await this._sendPlaintext(plaintext);
  }

  /** 发送一条已经带好 msg_kind 前缀的明文(例如文件分块)。 */
  async sendPlaintext(plaintext) {
    if (!this.isOpen) return;
    await this._sendPlaintext(plaintext);
  }

  async _sendPlaintext(plaintext) {
    try {
      const wire = await this.cipher.encrypt(plaintext);
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(wire.buffer);
    } catch (err) {
      console.warn('加密发送失败', err);
    }
  }

  /** 已缓冲待发送的字节数,文件传输据此做背压,避免把视频帧挤掉。 */
  get bufferedAmount() {
    return this.ws ? this.ws.bufferedAmount : 0;
  }

  _startPing() {
    this._stopPing();
    // 心跳与视频帧流控解耦:即使画面卡住,用户也始终能看到准确的延迟数字
    this._pingTimer = setInterval(() => {
      if (!this.isOpen) return;
      this._pingSeq += 1;
      const n = this._pingSeq;
      this._pingInflight.set(n, performance.now());
      this.sendControl({ t: 'ping', ts: this.nowMs(), n });
    }, PING_INTERVAL_MS);
  }

  _stopPing() {
    if (this._pingTimer) {
      clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
    this._pingInflight.clear();
  }
}

function buildWsUrl(params) {
  // 页面本身用 https 打开时,浏览器会阻止连接明文 ws://(混合内容策略),
  // 这里跟随页面协议自动选择 ws/wss。
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const target = params.mode === 'direct' ? params.address : params.relayAddress;
  return `${scheme}://${target}`;
}
