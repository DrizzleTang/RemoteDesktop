'use strict';

/*
 * 远程桌面 - 主控端(浏览器客户端)
 *
 * 协议约定详见仓库根目录 docs/architecture.md 与 common/protocol.py /
 * common/crypto.py(Python 侧的权威实现,这里是与之逐字节对应的 JS 实现)。
 *
 * 简要回顾:
 *   - 握手:host 发一条明文 kex_init(含 salt),双方各自用
 *     PBKDF2-HMAC-SHA256(密码, salt) -> HKDF-SHA256 派生出同一把
 *     AES-256-GCM 会话密钥,无需再回复握手消息。
 *   - 握手完成后,所有消息都是二进制帧:[12字节随机nonce][AES-GCM密文]。
 *     解密后的明文第 1 个字节是 msg_kind(1=控制JSON,2=视频帧),
 *     视频帧紧跟着 16 字节头部(大端):
 *       magic(1) seq(4) ts_ms(4) width(2) height(2) quality(1) fmt(1) flags(1)
 */

const MSG_CONTROL = 1;
const MSG_VIDEO_FRAME = 2;
const FRAME_MAGIC = 0xf1;
const FRAME_HEADER_BYTES = 16;
const MAX_FRAME_DIMENSION = 8192; // 防止被篡改的宽高把 canvas/内存撑爆
const HKDF_INFO = 'remotedesktop-session-key-v1';
const PING_INTERVAL_MS = 1000;
const MOUSE_MOVE_THROTTLE_MS = 33; // ~30Hz,弱网下没必要发更密
const REPLAY_WINDOW = 20000; // 会话内记住的最近 nonce 数量上限,防重放
const MIN_KEX_ITERATIONS = 100000; // 低于此值视为被篡改/降级攻击,直接拒绝
const MAX_KEX_ITERATIONS = 2000000;
const KEX_SALT_MIN_BYTES = 8;
const KEX_SALT_MAX_BYTES = 64;

const state = {
  ws: null,
  aesKey: null,
  saltBytes: null,
  handshakeStarted: false, // 已经处理过一次 kex_init;之后再收到的一律忽略
  seenNonces: new Set(),
  nonceOrder: [],
  connected: false,
  manualClose: false,
  connectionParams: null,
  reconnectAttempts: 0,
  reconnectTimer: null,
  pingTimer: null,
  pingSeq: 0,
  pingInflight: new Map(),
  screenWidth: 0,
  screenHeight: 0,
  startPerf: performance.now(),
};

// ---------------------------------------------------------------------------
// DOM 引用
// ---------------------------------------------------------------------------
const el = (id) => document.getElementById(id);

const connectScreen = el('connect-screen');
const sessionScreen = el('session-screen');
const connectForm = el('connect-form');
const connectBtn = el('connect-btn');
const connectStatus = el('connect-status');
const tabBtns = document.querySelectorAll('.tab-btn');
const directFields = el('direct-fields');
const relayFields = el('relay-fields');
const fieldAddress = el('field-address');
const fieldRelayAddress = el('field-relay-address');
const fieldRelayId = el('field-relay-id');
const fieldPassword = el('field-password');

const statusDot = el('status-dot');
const statusText = el('status-text');
const latencyBadge = el('latency-badge');
const qualitySelect = el('quality-select');
const statsText = el('stats-text');
const customPanel = el('custom-quality-panel');
const rangeScale = el('range-scale');
const rangeJpeg = el('range-jpeg');
const rangeFps = el('range-fps');
const canvas = el('screen');
const canvasHint = el('canvas-hint');
const btnDisconnect = el('btn-disconnect');
const btnFullscreen = el('btn-fullscreen');
const btnSendClipboard = el('btn-send-clipboard');
const toast = el('toast');
const clipboardFallback = el('clipboard-fallback');
const clipboardFallbackText = el('clipboard-fallback-text');

const ctx = canvas.getContext('2d');

let connectMode = 'direct';

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function nowMs() {
  return Math.round(performance.now() - state.startPerf);
}

function concatBytes(...arrs) {
  let total = 0;
  for (const a of arrs) total += a.byteLength;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const a of arrs) {
    out.set(a instanceof Uint8Array ? a : new Uint8Array(a), offset);
    offset += a.byteLength;
  }
  return out;
}

function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function bytesToHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

// 防重放:AES-GCM 只保证篡改会被发现,不保证同一条密文不会被原样重发
// (例如被攻陷的中转服务器,或直连模式下的链路中间人)。正常流量的 nonce
// 每次都是新随机生成的,因此在会话内记住"最近见过的 nonce",拒绝重复值,
// 就能挡住"原样重放一条历史指令(比如一次点击/按键)"这类攻击。
function isNonceReplayed(nonceKey) {
  return state.seenNonces.has(nonceKey);
}

function rememberNonce(nonceKey) {
  state.seenNonces.add(nonceKey);
  state.nonceOrder.push(nonceKey);
  if (state.nonceOrder.length > REPLAY_WINDOW) {
    const oldest = state.nonceOrder.shift();
    state.seenNonces.delete(oldest);
  }
}

function showToast(msg, ms = 2500) {
  toast.textContent = msg;
  toast.style.display = 'block';
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { toast.style.display = 'none'; }, ms);
}

// ---------------------------------------------------------------------------
// 加密:PBKDF2 -> HKDF -> AES-256-GCM(全部使用浏览器原生 SubtleCrypto,
// 无需任何第三方密码学库,详见文件头注释里的取舍说明)
// ---------------------------------------------------------------------------

async function deriveSessionKey(password, saltBytes, iterations) {
  const enc = new TextEncoder();
  const pwMaterial = await crypto.subtle.importKey('raw', enc.encode(password), 'PBKDF2', false, ['deriveBits']);
  const pbkdf2Bits = await crypto.subtle.deriveBits(
    { name: 'PBKDF2', salt: saltBytes, iterations, hash: 'SHA-256' }, pwMaterial, 256,
  );
  const hkdfMaterial = await crypto.subtle.importKey('raw', pbkdf2Bits, 'HKDF', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt: saltBytes, info: enc.encode(HKDF_INFO) },
    hkdfMaterial, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt'],
  );
}

async function encryptMessage(kind, payloadBytes) {
  const plaintext = concatBytes(new Uint8Array([kind]), payloadBytes);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: state.saltBytes }, state.aesKey, plaintext,
  );
  return concatBytes(nonce, new Uint8Array(ciphertext));
}

async function decryptMessage(wireBytes) {
  const nonce = wireBytes.slice(0, 12);
  const ciphertext = wireBytes.slice(12);
  const nonceKey = bytesToHex(nonce);
  if (isNonceReplayed(nonceKey)) {
    throw new Error('replayed nonce detected');
  }
  const plaintextBuf = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: state.saltBytes }, state.aesKey, ciphertext,
  );
  rememberNonce(nonceKey);
  return new Uint8Array(plaintextBuf);
}

async function sendControl(obj) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN || !state.aesKey) return;
  const bytes = new TextEncoder().encode(JSON.stringify(obj));
  try {
    const wire = await encryptMessage(MSG_CONTROL, bytes);
    state.ws.send(wire.buffer);
  } catch (err) {
    console.warn('加密发送失败', err);
  }
}

// ---------------------------------------------------------------------------
// 连接与握手状态机
// ---------------------------------------------------------------------------

function setStatus(kind, text) {
  statusDot.className = 'status-dot ' + kind;
  statusText.textContent = text;
}

function setConnectError(text) {
  connectStatus.textContent = text;
  connectStatus.className = 'connect-status' + (text ? ' error' : '');
}

function describeRelayError(reason) {
  const map = {
    invalid_id: '会话码格式不正确',
    invalid_role: '内部协议错误(role)',
    id_in_use: '该会话码已被占用,请让被控端重新生成',
    host_not_found: '未找到该会话码对应的被控端,请确认对方已启动且会话码正确',
    rate_limited: '连接尝试过于频繁,请稍后再试',
  };
  return map[reason] || reason || '未知错误';
}

function describeRejectReason(reason) {
  const map = {
    busy: '被控端当前已有其他人在连接',
    rate_limited: '近期密码错误次数过多,请稍后再试',
  };
  return map[reason] || reason || '连接被拒绝';
}

function buildWsUrl(mode, params) {
  if (mode === 'direct') {
    return `ws://${params.address}`;
  }
  return `ws://${params.relayAddress}`;
}

function connect(params) {
  state.connectionParams = params;
  state.manualClose = false;
  setStatus('connecting', '正在连接...');

  let ws;
  try {
    ws = new WebSocket(buildWsUrl(params.mode, params));
  } catch (err) {
    setConnectError('地址格式不正确: ' + err.message);
    return;
  }
  ws.binaryType = 'arraybuffer';
  state.ws = ws;
  state.aesKey = null;
  state.saltBytes = null;
  state.handshakeStarted = false;
  state.seenNonces = new Set();
  state.nonceOrder = [];

  ws.onopen = () => {
    if (params.mode === 'relay') {
      ws.send(JSON.stringify({ t: 'relay_register', role: 'client', id: params.relayId, v: 1 }));
      setStatus('connecting', '正在通过中转服务器配对...');
    }
  };

  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      handlePlaintextMessage(safeJsonParse(event.data));
      return;
    }
    handleEncryptedMessage(new Uint8Array(event.data));
  };

  ws.onerror = () => { /* onclose 会紧随其后处理,这里无需重复处理 */ };
  ws.onclose = () => handleClose();
}

function isPlainMessageObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function safeJsonParse(text) {
  try {
    const parsed = JSON.parse(text);
    return isPlainMessageObject(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

async function handleEncryptedMessage(wireBytes) {
  if (!state.aesKey) return;
  let plaintext;
  try {
    plaintext = await decryptMessage(wireBytes);
  } catch (err) {
    console.warn('解密失败,丢弃该消息', err);
    return;
  }
  if (plaintext.length < 1) return;
  const kind = plaintext[0];
  const body = plaintext.subarray(1);
  if (kind === MSG_CONTROL) {
    let msg;
    try {
      msg = JSON.parse(new TextDecoder().decode(body));
    } catch {
      return;
    }
    if (!isPlainMessageObject(msg)) return;
    handleControlMessage(msg);
  } else if (kind === MSG_VIDEO_FRAME) {
    handleVideoFrame(body);
  }
}

function handlePlaintextMessage(msg) {
  switch (msg.t) {
    case 'relay_paired':
      setStatus('connecting', '已与被控端配对,正在建立加密连接...');
      break;
    case 'relay_error':
      setConnectError('连接失败: ' + describeRelayError(msg.reason));
      setStatus('error', '连接失败');
      state.manualClose = true;
      state.ws && state.ws.close();
      showConnectScreen();
      break;
    case 'kex_init':
      onKexInit(msg);
      break;
    case 'kex_reject':
      setConnectError('连接被拒绝: ' + describeRejectReason(msg.reason));
      setStatus('error', '连接被拒绝');
      state.manualClose = true;
      state.ws && state.ws.close();
      showConnectScreen();
      break;
    default:
      break;
  }
}

async function onKexInit(msg) {
  // 握手只应该发生一次:一旦处理过第一条 kex_init(无论成功与否),后续
  // 连接生命周期内再收到的 kex_init 一律视为异常/攻击尝试并忽略,防止
  // 恶意中转方或链路中间人在会话中途注入一条新的 kex_init 让 client 重新
  // 派生密钥、进而扰乱或劫持已建立的加密会话状态。
  if (state.handshakeStarted) {
    console.warn('忽略重复的 kex_init(握手已完成或正在进行)');
    return;
  }
  state.handshakeStarted = true;

  const iterations = msg.iterations;
  if (typeof iterations !== 'number' || !Number.isInteger(iterations)
      || iterations < MIN_KEX_ITERATIONS || iterations > MAX_KEX_ITERATIONS) {
    console.error('kex_init 的 iterations 字段不合法或超出安全范围,拒绝握手', iterations);
    setConnectError('握手参数异常,已拒绝连接(可能存在中间人篡改)');
    state.ws && state.ws.close();
    return;
  }

  let saltBytes;
  try {
    saltBytes = base64ToBytes(msg.salt);
  } catch (err) {
    console.error('kex_init 的 salt 字段无法解码', err);
    setConnectError('握手参数异常,已拒绝连接');
    state.ws && state.ws.close();
    return;
  }
  if (saltBytes.length < KEX_SALT_MIN_BYTES || saltBytes.length > KEX_SALT_MAX_BYTES) {
    console.error('kex_init 的 salt 长度不合法', saltBytes.length);
    setConnectError('握手参数异常,已拒绝连接');
    state.ws && state.ws.close();
    return;
  }

  try {
    state.saltBytes = saltBytes;
    state.aesKey = await deriveSessionKey(state.connectionParams.password, state.saltBytes, iterations);
    setStatus('connecting', '正在验证密码...');
    await sendControl({ t: 'hello', client_name: navigator.userAgent.slice(0, 60) });
    startPingLoop();
  } catch (err) {
    console.error('密钥协商失败', err);
    setConnectError('密钥协商失败,请检查浏览器是否支持(需要 HTTPS 或 localhost 环境)');
    state.ws && state.ws.close();
  }
}

function handleControlMessage(msg) {
  switch (msg.t) {
    case 'hello_ack':
      onConnected(msg);
      break;
    case 'pong':
      handlePong(msg);
      break;
    case 'stats':
      updateStatsUI(msg);
      break;
    case 'clip':
      if (typeof msg.text === 'string') handleRemoteClipboard(msg.text);
      break;
    default:
      break;
  }
}

function onConnected(msg) {
  state.connected = true;
  state.reconnectAttempts = 0;
  state.screenWidth = msg.width;
  state.screenHeight = msg.height;
  setStatus('connected', msg.host_name ? `已连接: ${msg.host_name}` : '已连接');
  setConnectError('');
  showSessionScreen();
  if (msg.input_available === false) {
    showToast('对方系统暂不支持输入注入,当前仅能观看画面');
  }
}

function handlePong(msg) {
  const sentAt = state.pingInflight.get(msg.n);
  if (sentAt === undefined) return;
  state.pingInflight.delete(msg.n);
  const rtt = performance.now() - sentAt;
  updateLatencyUI(rtt);
}

function startPingLoop() {
  stopPingLoop();
  state.pingTimer = setInterval(() => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    state.pingSeq += 1;
    const n = state.pingSeq;
    state.pingInflight.set(n, performance.now());
    sendControl({ t: 'ping', ts: nowMs(), n });
  }, PING_INTERVAL_MS);
}

function stopPingLoop() {
  if (state.pingTimer) {
    clearInterval(state.pingTimer);
    state.pingTimer = null;
  }
  state.pingInflight.clear();
}

function handleClose() {
  stopPingLoop();
  state.ws = null;
  state.connected = false;
  if (state.manualClose) {
    setStatus('', '未连接');
    return;
  }
  setStatus('reconnecting', '连接已断开,正在自动重连...');
  scheduleReconnect();
}

function scheduleReconnect() {
  state.reconnectAttempts += 1;
  const delay = Math.min(10000, 1000 * 2 ** (state.reconnectAttempts - 1));
  clearTimeout(state.reconnectTimer);
  state.reconnectTimer = setTimeout(() => {
    if (!state.manualClose && state.connectionParams) connect(state.connectionParams);
  }, delay);
}

// ---------------------------------------------------------------------------
// 视频帧解码与渲染
// ---------------------------------------------------------------------------

function handleVideoFrame(body) {
  if (body.length < FRAME_HEADER_BYTES) return;
  const dv = new DataView(body.buffer, body.byteOffset, body.byteLength);
  if (dv.getUint8(0) !== FRAME_MAGIC) return;
  const seq = dv.getUint32(1);
  const width = dv.getUint16(9);
  const height = dv.getUint16(11);
  if (width === 0 || height === 0 || width > MAX_FRAME_DIMENSION || height > MAX_FRAME_DIMENSION) {
    console.warn('丢弃宽高异常的视频帧', width, height);
    return;
  }
  const imageBytes = body.subarray(FRAME_HEADER_BYTES);

  const blob = new Blob([imageBytes], { type: 'image/jpeg' });
  createImageBitmap(blob)
    .then((bitmap) => {
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      ctx.drawImage(bitmap, 0, 0, width, height);
      bitmap.close();
      sendControl({ t: 'frame_ack', seq, recv_ts: nowMs() });
    })
    .catch((err) => console.warn('视频帧解码失败', err));
}

// ---------------------------------------------------------------------------
// 状态栏 UI 更新
// ---------------------------------------------------------------------------

function updateLatencyUI(rttMs) {
  latencyBadge.textContent = `${Math.round(rttMs)} ms`;
  latencyBadge.className = 'badge ' + (rttMs < 100 ? 'good' : rttMs < 300 ? 'mid' : 'bad');
}

function updateStatsUI(msg) {
  const parts = [];
  if (msg.level_label) parts.push(msg.level_label);
  if (typeof msg.actual_fps === 'number') parts.push(`${msg.actual_fps.toFixed(1)} fps`);
  if (typeof msg.actual_kbps === 'number') parts.push(`${Math.round(msg.actual_kbps)} kbps`);
  statsText.textContent = parts.join(' · ');
}

// ---------------------------------------------------------------------------
// 界面切换
// ---------------------------------------------------------------------------

function showConnectScreen() {
  sessionScreen.style.display = 'none';
  connectScreen.style.display = 'flex';
  connectBtn.disabled = false;
  connectBtn.textContent = '连接';
}

function showSessionScreen() {
  connectScreen.style.display = 'none';
  sessionScreen.style.display = 'flex';
}

tabBtns.forEach((btn) => {
  btn.addEventListener('click', () => {
    tabBtns.forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    connectMode = btn.dataset.mode;
    directFields.style.display = connectMode === 'direct' ? 'contents' : 'none';
    relayFields.style.display = connectMode === 'relay' ? 'contents' : 'none';
  });
});

connectForm.addEventListener('submit', (e) => {
  e.preventDefault();
  setConnectError('');
  const password = fieldPassword.value;
  if (!password) {
    setConnectError('请输入访问密码');
    return;
  }
  let params;
  if (connectMode === 'direct') {
    const address = fieldAddress.value.trim();
    if (!address) { setConnectError('请输入被控端地址'); return; }
    params = { mode: 'direct', address, password };
  } else {
    const relayAddress = fieldRelayAddress.value.trim();
    const relayId = fieldRelayId.value.trim();
    if (!relayAddress || !relayId) { setConnectError('请输入中转服务器地址和会话码'); return; }
    params = { mode: 'relay', relayAddress, relayId, password };
  }
  connectBtn.disabled = true;
  connectBtn.textContent = '连接中...';
  state.reconnectAttempts = 0;
  connect(params);
});

btnDisconnect.addEventListener('click', () => {
  state.manualClose = true;
  clearTimeout(state.reconnectTimer);
  if (state.ws) {
    sendControl({ t: 'bye' });
    state.ws.close();
  }
  stopPingLoop();
  showConnectScreen();
  setStatus('', '未连接');
});

btnFullscreen.addEventListener('click', () => {
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else {
    sessionScreen.requestFullscreen().catch(() => showToast('浏览器不支持或拒绝了全屏请求'));
  }
});

// ---------------------------------------------------------------------------
// 画质切换
// ---------------------------------------------------------------------------

function sendCustomQuality() {
  const scale = parseFloat(rangeScale.value);
  const jpegQ = parseInt(rangeJpeg.value, 10);
  const fps = parseInt(rangeFps.value, 10);
  el('range-scale-val').textContent = `${Math.round(scale * 100)}%`;
  el('range-jpeg-val').textContent = String(jpegQ);
  el('range-fps-val').textContent = String(fps);
  sendControl({ t: 'quality', mode: 'custom', scale, jpeg_q: jpegQ, max_fps: fps });
}

qualitySelect.addEventListener('change', () => {
  const mode = qualitySelect.value;
  customPanel.style.display = mode === 'custom' ? 'flex' : 'none';
  if (mode === 'custom') {
    sendCustomQuality();
  } else {
    sendControl({ t: 'quality', mode });
  }
});

[rangeScale, rangeJpeg, rangeFps].forEach((input) => input.addEventListener('input', sendCustomQuality));

// ---------------------------------------------------------------------------
// 鼠标 / 键盘输入采集
// ---------------------------------------------------------------------------

function toNormalized(e) {
  const rect = canvas.getBoundingClientRect();
  const nx = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
  const ny = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
  return { nx, ny };
}

const BUTTON_NAMES = { 0: 'left', 1: 'middle', 2: 'right' };
let lastMouseMoveAt = 0;

canvas.addEventListener('mousemove', (e) => {
  const now = performance.now();
  if (now - lastMouseMoveAt < MOUSE_MOVE_THROTTLE_MS) return;
  lastMouseMoveAt = now;
  const { nx, ny } = toNormalized(e);
  sendControl({ t: 'mmove', x: nx, y: ny });
});

canvas.addEventListener('mousedown', (e) => {
  e.preventDefault();
  canvas.focus();
  canvasHint.classList.add('hidden');
  const { nx, ny } = toNormalized(e);
  sendControl({ t: 'mbtn', btn: BUTTON_NAMES[e.button] || 'left', down: true, x: nx, y: ny });
});

canvas.addEventListener('mouseup', (e) => {
  e.preventDefault();
  const { nx, ny } = toNormalized(e);
  sendControl({ t: 'mbtn', btn: BUTTON_NAMES[e.button] || 'left', down: false, x: nx, y: ny });
});

canvas.addEventListener('contextmenu', (e) => e.preventDefault());

canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  sendControl({ t: 'mscroll', dx: -e.deltaX / 100, dy: -e.deltaY / 100 });
}, { passive: false });

canvas.addEventListener('keydown', (e) => {
  e.preventDefault();
  const payload = { t: 'key', code: e.code, down: true };
  if (e.key && e.key.length === 1) payload.key = e.key;
  sendControl(payload);
});

canvas.addEventListener('keyup', (e) => {
  e.preventDefault();
  const payload = { t: 'key', code: e.code, down: false };
  if (e.key && e.key.length === 1) payload.key = e.key;
  sendControl(payload);
});

canvas.addEventListener('paste', (e) => {
  const text = e.clipboardData && e.clipboardData.getData('text');
  if (text) sendControl({ t: 'clip', text });
});

// ---------------------------------------------------------------------------
// 剪贴板
// ---------------------------------------------------------------------------

btnSendClipboard.addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      sendControl({ t: 'clip', text });
      showToast('已发送本机剪贴板内容');
    } else {
      showToast('本机剪贴板为空');
    }
  } catch (err) {
    showToast('无法读取本机剪贴板(浏览器权限限制),请在被控端手动复制');
  }
});

async function handleRemoteClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    showToast('已接收对方剪贴板内容');
  } catch (err) {
    clipboardFallbackText.value = text;
    clipboardFallback.style.display = 'block';
  }
}

el('clipboard-fallback-copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(clipboardFallbackText.value);
    showToast('已复制');
  } catch {
    clipboardFallbackText.select();
    document.execCommand('copy');
  }
  clipboardFallback.style.display = 'none';
});

el('clipboard-fallback-close').addEventListener('click', () => {
  clipboardFallback.style.display = 'none';
});

// 5 秒后自动隐藏"点击画面开始操作"提示
setTimeout(() => canvasHint.classList.add('hidden'), 5000);
