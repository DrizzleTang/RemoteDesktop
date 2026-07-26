'use strict';
/*
 * 界面控制:状态栏、遮罩、提示、画质与显示器选择、文件传输进度。
 * 集中所有 DOM 操作,其他模块不直接碰 DOM。
 */

import { formatBytes } from './transfer.js';

export const el = (id) => document.getElementById(id);

export class UI {
  constructor() {
    this.connectScreen = el('connect-screen');
    this.sessionScreen = el('session-screen');
    this.statusDot = el('status-dot');
    this.statusText = el('status-text');
    this.latencyBadge = el('latency-badge');
    this.statsText = el('stats-text');
    this.qualitySelect = el('quality-select');
    this.monitorSelect = el('monitor-select');
    this.monitorWrap = el('monitor-wrap');
    this.customPanel = el('custom-quality-panel');
    this.canvas = el('screen');
    this.canvasHint = el('canvas-hint');
    this.overlay = el('screen-overlay');
    this.overlayText = el('screen-overlay-text');
    this.roleBadge = el('role-badge');
    this.toast = el('toast');
    this.connectStatus = el('connect-status');
    this.connectBtn = el('connect-btn');
    this.transferPanel = el('transfer-panel');
    this.transferList = el('transfer-list');
    this.clipboardFallback = el('clipboard-fallback');
    this.clipboardFallbackText = el('clipboard-fallback-text');
    this._toastTimer = null;
    this._transferRows = new Map();
  }

  // ---- 连接界面 ----

  showConnectScreen() {
    this.sessionScreen.style.display = 'none';
    this.connectScreen.style.display = 'flex';
    this.connectBtn.disabled = false;
    this.connectBtn.textContent = '连接';
  }

  showSessionScreen() {
    this.connectScreen.style.display = 'none';
    this.sessionScreen.style.display = 'flex';
  }

  setConnectMessage(text, isError = true) {
    this.connectStatus.textContent = text || '';
    this.connectStatus.className = 'connect-status' + (text && isError ? ' error' : '');
  }

  setConnecting(busy) {
    this.connectBtn.disabled = busy;
    this.connectBtn.textContent = busy ? '连接中...' : '连接';
  }

  // ---- 状态栏 ----

  setStatus(kind, text) {
    this.statusDot.className = 'status-dot ' + kind;
    this.statusText.textContent = text;
  }

  setLatency(rttMs) {
    this.latencyBadge.textContent = `${Math.round(rttMs)} ms`;
    // 绿/黄/红三档,让用户一眼看出当前网络状况
    this.latencyBadge.className = 'badge ' + (rttMs < 100 ? 'good' : rttMs < 300 ? 'mid' : 'bad');
  }

  setStats(msg) {
    const parts = [];
    if (msg.level_label) parts.push(msg.level_label);
    if (typeof msg.actual_fps === 'number') parts.push(`${msg.actual_fps.toFixed(1)} fps`);
    if (typeof msg.actual_kbps === 'number') parts.push(`${Math.round(msg.actual_kbps)} kbps`);
    if (typeof msg.capture_ms === 'number' && typeof msg.encode_ms === 'number') {
      // 把"采集+编码"耗时单独显示出来:如果这个数字很大而延迟不高,
      // 说明瓶颈在被控端 CPU 而不是网络,便于用户判断该降画质还是换网络
      parts.push(`本机 ${(msg.capture_ms + msg.encode_ms).toFixed(0)}ms`);
    }
    this.statsText.textContent = parts.join(' · ');
    this.statsText.title = msg.rects !== undefined
      ? `编码 ${msg.codec || '?'} · 本帧变化区域 ${msg.rects} 块 · 关键帧 ${msg.keyframes}`
        + ` · 增量帧 ${msg.deltas} · 静止跳过 ${msg.skipped}`
        + (msg.bw_saturated === true ? ' · 带宽已打满'
           : msg.bw_saturated === false ? ' · 带宽充裕(延迟来自链路本身)' : '')
      : '';
  }

  // ---- 画面遮罩 ----

  /**
   * 断线/重连期间盖一层遮罩。没有遮罩的话,画面会停在断线前的最后一帧,
   * 用户很可能对着一张"冻结的旧画面"继续操作,而这些操作全部丢失且毫无提示。
   */
  showOverlay(text) {
    this.overlayText.textContent = text;
    this.overlay.style.display = 'flex';
  }

  hideOverlay() {
    this.overlay.style.display = 'none';
  }

  setRole(canControl) {
    if (canControl) {
      this.roleBadge.style.display = 'none';
    } else {
      this.roleBadge.style.display = 'inline-block';
      this.roleBadge.textContent = '只读观看';
      this.roleBadge.title = '当前已有其他人持有操作权,你只能查看画面';
    }
  }

  // ---- 显示器下拉框 ----

  setMonitors(monitors, current) {
    this.monitorSelect.innerHTML = '';
    if (!monitors || monitors.length <= 2) {
      // 只有"全部显示器"+一个物理显示器时没有切换的意义,直接隐藏
      this.monitorWrap.style.display = 'none';
      return;
    }
    this.monitorWrap.style.display = 'flex';
    for (const monitor of monitors) {
      const option = document.createElement('option');
      option.value = String(monitor.index);
      option.textContent = monitor.label;
      if (monitor.index === current) option.selected = true;
      this.monitorSelect.appendChild(option);
    }
  }

  // ---- 提示 ----

  toastMessage(text, ms = 2600) {
    this.toast.textContent = text;
    this.toast.style.display = 'block';
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => { this.toast.style.display = 'none'; }, ms);
  }

  hideCanvasHint() {
    this.canvasHint.classList.add('hidden');
  }

  // ---- 文件传输进度 ----

  upsertTransfer(id, name, sent, total, state = 'active') {
    let row = this._transferRows.get(id);
    if (!row) {
      row = document.createElement('div');
      row.className = 'transfer-row';
      row.innerHTML = `
        <div class="transfer-name"></div>
        <div class="transfer-bar"><div class="transfer-bar-fill"></div></div>
        <div class="transfer-meta"></div>`;
      this.transferList.appendChild(row);
      this._transferRows.set(id, row);
    }
    this.transferPanel.style.display = 'block';
    row.querySelector('.transfer-name').textContent = name;
    const pct = total > 0 ? Math.min(100, (sent / total) * 100) : 100;
    row.querySelector('.transfer-bar-fill').style.width = `${pct}%`;
    row.className = `transfer-row ${state}`;
    const meta = row.querySelector('.transfer-meta');
    if (state === 'done') meta.textContent = '已完成';
    else if (state === 'error') meta.textContent = '失败';
    else meta.textContent = `${formatBytes(sent)} / ${formatBytes(total)}`;
  }

  setTransferError(id, name, message) {
    this.upsertTransfer(id, name || '文件', 0, 1, 'error');
    const row = this._transferRows.get(id);
    if (row) row.querySelector('.transfer-meta').textContent = message;
  }

  clearFinishedTransfers() {
    for (const [id, row] of this._transferRows) {
      if (row.classList.contains('done') || row.classList.contains('error')) {
        row.remove();
        this._transferRows.delete(id);
      }
    }
    if (this._transferRows.size === 0) this.transferPanel.style.display = 'none';
  }

  // ---- 剪贴板降级弹窗 ----

  showClipboardFallback(text) {
    this.clipboardFallbackText.value = text;
    this.clipboardFallback.style.display = 'block';
  }

  hideClipboardFallback() {
    this.clipboardFallback.style.display = 'none';
  }
}
