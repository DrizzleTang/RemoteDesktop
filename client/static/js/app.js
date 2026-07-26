'use strict';
/*
 * 应用入口:把会话、渲染、输入采集、文件上传、界面这几块接起来。
 */

import { Session } from './session.js';
import { Renderer } from './render.js';
import { InputCapture } from './input.js';
import { FileUploader } from './transfer.js';
import { UI, el } from './ui.js';
import { CursorOverlay } from './cursor.js';
import { formatBytes } from './transfer.js';

const ui = new UI();
const session = new Session();
const renderer = new Renderer(ui.canvas);
const uploader = new FileUploader(session, {
  onProgress: ({ id, name, sent, total }) => ui.upsertTransfer(id, name, sent, total),
  onDone: ({ id, name, savedAs }) => {
    ui.upsertTransfer(id, name, 1, 1, 'done');
    ui.toastMessage(`文件已发送到对方电脑:${savedAs || name}`);
    setTimeout(() => ui.clearFinishedTransfers(), 4000);
  },
  onError: ({ id, name, message }) => {
    ui.setTransferError(id, name, message);
    ui.toastMessage(`文件发送失败:${message}`, 4000);
  },
});

const cursorOverlay = new CursorOverlay(el('remote-cursor'));

let connectMode = 'direct';
let fileTransferEnabled = false;
let shareAvailable = false;
// 正在从被控端下载的文件:transferId -> { name, size, chunks[], received }
const downloads = new Map();

// ---------------------------------------------------------------------------
// 输入采集:未连接或只读观看时,所有输入都不发送
// ---------------------------------------------------------------------------
new InputCapture(
  ui.canvas,
  (msg) => session.sendControl(msg),
  () => session.connected && session.canControl,
);

ui.canvas.addEventListener('mousedown', () => ui.hideCanvasHint());
ui.canvas.addEventListener('touchstart', () => ui.hideCanvasHint(), { passive: true });

// 在画面上按 Ctrl+V 粘贴时,把本机剪贴板内容送到对方
ui.canvas.addEventListener('paste', (e) => {
  if (!session.connected || !session.canControl) return;
  const text = e.clipboardData && e.clipboardData.getData('text');
  if (text) session.sendControl({ t: 'clip', text });
});

// ---------------------------------------------------------------------------
// 会话事件
// ---------------------------------------------------------------------------

session.on('status', ({ kind, text }) => {
  ui.setStatus(kind, text);
  if (kind === 'reconnecting') ui.showOverlay('连接已断开,正在自动重连…');
});

session.on('fatal', (message) => {
  ui.setConnectMessage(message);
  ui.setStatus('error', '连接失败');
  ui.setConnecting(false);
  ui.showConnectScreen();
});

session.on('ready', (msg) => {
  ui.setStatus('connected', msg.host_name ? `已连接: ${msg.host_name}` : '已连接');
  ui.setConnectMessage('');
  ui.setConnecting(false);
  ui.showSessionScreen();
  ui.hideOverlay();
  ui.setRole(msg.can_control !== false);
  ui.setMonitors(msg.monitors, msg.current_monitor);
  fileTransferEnabled = msg.file_transfer === true;
  el('btn-send-file').style.display = fileTransferEnabled ? '' : 'none';
  shareAvailable = msg.share_available === true;
  el('btn-remote-files').style.display = shareAvailable ? '' : 'none';
  cursorOverlay.reset();
  renderer.reset();
  if (msg.can_control !== false && msg.input_available === false) {
    ui.toastMessage('对方系统暂不支持输入注入,当前仅能观看画面', 4000);
  }
});

session.on('disconnected', () => {
  ui.showOverlay('连接已断开,正在自动重连…');
  renderer.reset();
  cursorOverlay.reset();
});

// 远端光标:截屏不含指针,由被控端单独采集后叠加绘制
session.on('cursor', (msg) => cursorOverlay.update(msg));

session.on('latency', (rtt) => ui.setLatency(rtt));
session.on('stats', (msg) => ui.setStats(msg));

session.on('keyframe', async (frame) => {
  try {
    await renderer.drawKeyframe(frame);
    ui.hideOverlay();
    session.sendControl({ t: 'frame_ack', seq: frame.seq, recv_ts: session.nowMs() });
  } catch (err) {
    console.warn('关键帧渲染失败', err);
  }
});

session.on('delta', async (frame) => {
  try {
    const drawn = await renderer.drawDelta(frame);
    // 即使因为缺少底图而没画,也要回 ack:否则被控端会一直等到超时,
    // 白白触发一次画质降级。ack 表达的是"这一帧我收到了"。
    session.sendControl({ t: 'frame_ack', seq: frame.seq, recv_ts: session.nowMs() });
    if (!drawn) console.debug('增量帧被跳过(等待关键帧同步)');
  } catch (err) {
    console.warn('增量帧渲染失败', err);
  }
});

session.on('clipboard', async (text) => {
  try {
    await navigator.clipboard.writeText(text);
    ui.toastMessage('已接收对方剪贴板内容');
  } catch {
    // 浏览器在没有用户手势时会拒绝写剪贴板,降级为手动复制
    ui.showClipboardFallback(text);
  }
});

session.on('monitor', (msg) => {
  if (msg.ok) {
    renderer.reset();
    ui.toastMessage('已切换显示器');
  } else {
    ui.toastMessage(`切换显示器失败:${msg.reason || '未知原因'}`);
  }
});

session.on('viewer', (msg) => {
  ui.setRole(msg.can_control === true);
  if (msg.can_control) {
    fileTransferEnabled = msg.file_transfer === true;
    el('btn-send-file').style.display = fileTransferEnabled ? '' : 'none';
    ui.toastMessage('原操作者已离开,你现在拥有操作权');
  }
});

session.on('file', (msg) => {
  if (msg.t === 'file_begin' && msg.dir === 'down') {
    // 被控端开始向我们推送一个文件
    downloads.set(msg.id, { name: msg.name, size: msg.size, chunks: [], received: 0 });
    ui.upsertTransfer(`d${msg.id}`, `⬇ ${msg.name}`, 0, msg.size);
    return;
  }
  if (msg.t === 'file_end' && downloads.has(msg.id)) {
    finishDownload(msg.id);
    return;
  }
  if (msg.t === 'file_error' && downloads.has(msg.id)) {
    const entry = downloads.get(msg.id);
    downloads.delete(msg.id);
    ui.setTransferError(`d${msg.id}`, entry.name, msg.message || '下载失败');
    return;
  }
  uploader.handleServerMessage(msg);
});

session.on('fileChunk', (chunk) => {
  const entry = downloads.get(chunk.transferId);
  if (!entry) return;
  // 复制一份:chunk.data 是解密缓冲区上的视图,缓冲区随后会被复用
  entry.chunks.push(new Uint8Array(chunk.data));
  entry.received += chunk.data.length;
  ui.upsertTransfer(`d${chunk.transferId}`, `⬇ ${entry.name}`, entry.received, entry.size);
});

function finishDownload(id) {
  const entry = downloads.get(id);
  downloads.delete(id);
  if (!entry) return;
  const blob = new Blob(entry.chunks, { type: 'application/octet-stream' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = entry.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  ui.upsertTransfer(`d${id}`, `⬇ ${entry.name}`, 1, 1, 'done');
  ui.toastMessage(`已下载:${entry.name}`);
  setTimeout(() => ui.clearFinishedTransfers(), 4000);
}

// ---------------------------------------------------------------------------
// 远端共享文件浏览
// ---------------------------------------------------------------------------

session.on('shareList', (msg) => {
  const list = el('remote-files-list');
  list.replaceChildren();

  // 注意:msg 里的文字全部来自被控端,属于不可信输入,一律用 textContent
  // 写入,绝不能拼进 innerHTML——否则一个恶意/被攻陷的被控端就能在主控端
  // 的页面里执行脚本。
  const showEmpty = (text) => {
    const div = document.createElement('div');
    div.className = 'remote-files-empty';
    div.textContent = text;
    list.appendChild(div);
  };

  if (!msg.ok) {
    showEmpty(typeof msg.message === 'string' ? msg.message : '无法读取共享目录');
    return;
  }
  if (!Array.isArray(msg.files) || msg.files.length === 0) {
    showEmpty('共享目录里没有文件');
    return;
  }
  for (const file of msg.files) {
    if (!file || typeof file.name !== 'string') continue;
    const row = document.createElement('div');
    row.className = 'remote-file-row';
    const name = document.createElement('div');
    name.className = 'remote-file-name';
    name.textContent = file.name;
    name.title = file.name;
    const size = document.createElement('div');
    size.className = 'remote-file-size';
    size.textContent = formatBytes(Number(file.size) || 0);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '下载';
    btn.addEventListener('click', () => {
      session.sendControl({ t: 'share_get', name: file.name });
      ui.toastMessage(`正在下载 ${file.name} …`);
    });
    row.append(name, size, btn);
    list.appendChild(row);
  }
});

el('btn-remote-files').addEventListener('click', () => {
  const panel = el('remote-files-panel');
  const showing = panel.style.display !== 'none';
  panel.style.display = showing ? 'none' : 'block';
  if (!showing) session.sendControl({ t: 'share_list_req' });
});

el('btn-remote-files-close').addEventListener('click', () => {
  el('remote-files-panel').style.display = 'none';
});

// ---------------------------------------------------------------------------
// 连接表单
// ---------------------------------------------------------------------------

document.querySelectorAll('.tab-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    connectMode = btn.dataset.mode;
    el('direct-fields').style.display = connectMode === 'direct' ? 'contents' : 'none';
    el('relay-fields').style.display = connectMode === 'relay' ? 'contents' : 'none';
  });
});

el('connect-form').addEventListener('submit', (e) => {
  e.preventDefault();
  ui.setConnectMessage('');
  const password = el('field-password').value;
  if (!password) {
    ui.setConnectMessage('请输入访问密码');
    return;
  }
  let params;
  if (connectMode === 'direct') {
    const address = el('field-address').value.trim();
    if (!address) { ui.setConnectMessage('请输入被控端地址'); return; }
    params = { mode: 'direct', address, password };
  } else {
    const relayAddress = el('field-relay-address').value.trim();
    const relayId = el('field-relay-id').value.trim();
    if (!relayAddress || !relayId) { ui.setConnectMessage('请输入中转服务器地址和会话码'); return; }
    params = { mode: 'relay', relayAddress, relayId, password };
  }
  ui.setConnecting(true);
  session.connect(params);
});

// ---------------------------------------------------------------------------
// 工具栏
// ---------------------------------------------------------------------------

el('btn-disconnect').addEventListener('click', () => {
  session.disconnect();
  renderer.reset();
  ui.hideOverlay();
  ui.showConnectScreen();
});

el('btn-fullscreen').addEventListener('click', () => {
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else {
    ui.sessionScreen.requestFullscreen().catch(() => ui.toastMessage('浏览器不支持或拒绝了全屏请求'));
  }
});

el('btn-send-clipboard').addEventListener('click', async () => {
  if (!session.canControl) {
    ui.toastMessage('只读观看模式下无法发送剪贴板');
    return;
  }
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      session.sendControl({ t: 'clip', text });
      ui.toastMessage('已发送本机剪贴板内容');
    } else {
      ui.toastMessage('本机剪贴板为空');
    }
  } catch {
    ui.toastMessage('无法读取本机剪贴板(浏览器权限限制),可改用在画面上按 Ctrl+V');
  }
});

ui.qualitySelect.addEventListener('change', () => {
  const mode = ui.qualitySelect.value;
  ui.customPanel.style.display = mode === 'custom' ? 'flex' : 'none';
  if (mode === 'custom') sendCustomQuality();
  else session.sendControl({ t: 'quality', mode });
});

function sendCustomQuality() {
  const scale = parseFloat(el('range-scale').value);
  const jpegQ = parseInt(el('range-jpeg').value, 10);
  const fps = parseInt(el('range-fps').value, 10);
  el('range-scale-val').textContent = `${Math.round(scale * 100)}%`;
  el('range-jpeg-val').textContent = String(jpegQ);
  el('range-fps-val').textContent = String(fps);
  session.sendControl({ t: 'quality', mode: 'custom', scale, jpeg_q: jpegQ, max_fps: fps });
}

['range-scale', 'range-jpeg', 'range-fps'].forEach((id) =>
  el(id).addEventListener('input', sendCustomQuality));

ui.monitorSelect.addEventListener('change', () => {
  session.sendControl({ t: 'monitor_set', index: parseInt(ui.monitorSelect.value, 10) });
});

// ---------------------------------------------------------------------------
// 文件传输:按钮选择 + 拖拽到画面
// ---------------------------------------------------------------------------

const fileInput = el('file-input');
el('btn-send-file').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  for (const file of fileInput.files) uploader.upload(file);
  fileInput.value = '';
});

const dropZone = el('canvas-wrap');
['dragenter', 'dragover'].forEach((type) =>
  dropZone.addEventListener(type, (e) => {
    if (!fileTransferEnabled || !session.canControl) return;
    e.preventDefault();
    dropZone.classList.add('dragover');
  }));

['dragleave', 'drop'].forEach((type) =>
  dropZone.addEventListener(type, (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
  }));

dropZone.addEventListener('drop', (e) => {
  if (!fileTransferEnabled || !session.canControl) {
    ui.toastMessage('当前无法发送文件');
    return;
  }
  const files = e.dataTransfer && e.dataTransfer.files;
  if (!files || files.length === 0) return;
  for (const file of files) uploader.upload(file);
});

// ---------------------------------------------------------------------------
// 剪贴板降级弹窗
// ---------------------------------------------------------------------------

el('clipboard-fallback-copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(ui.clipboardFallbackText.value);
    ui.toastMessage('已复制');
  } catch {
    ui.clipboardFallbackText.select();
    document.execCommand('copy');
  }
  ui.hideClipboardFallback();
});

el('clipboard-fallback-close').addEventListener('click', () => ui.hideClipboardFallback());

// 5 秒后自动隐藏"点击画面开始操作"提示
setTimeout(() => ui.hideCanvasHint(), 5000);
