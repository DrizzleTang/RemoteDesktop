'use strict';
/*
 * 应用入口:把会话、渲染、输入采集、文件上传、界面这几块接起来。
 */

import { Session } from './session.js';
import { Renderer } from './render.js';
import { InputCapture } from './input.js';
import { FileUploader } from './transfer.js';
import { UI, el } from './ui.js';

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

let connectMode = 'direct';
let fileTransferEnabled = false;

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
  renderer.reset();
  if (msg.can_control !== false && msg.input_available === false) {
    ui.toastMessage('对方系统暂不支持输入注入,当前仅能观看画面', 4000);
  }
});

session.on('disconnected', () => {
  ui.showOverlay('连接已断开,正在自动重连…');
  renderer.reset();
});

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

session.on('file', (msg) => uploader.handleServerMessage(msg));

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
