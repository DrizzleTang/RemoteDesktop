'use strict';
/*
 * 文件上传(主控端 -> 被控端)。
 *
 * 流程:file_begin(控制消息) -> 若干二进制分块 -> file_end(控制消息)。
 * 分块走已加密的同一条 WebSocket,被控端按 seq 顺序落盘。
 *
 * 背压很重要:文件动辄几十上百 MB,如果不加节制地把所有分块塞进
 * WebSocket,浏览器的发送缓冲会迅速堆积,把视频帧和鼠标键盘消息挤在后面
 * ——用户会感觉"传文件的时候远程桌面卡死了"。所以每发一块都检查
 * bufferedAmount,超过阈值就等一等再继续。被控端侧也把文件分块放在最低
 * 优先级的发送队列里,是同一个思路的两端呼应。
 */

import { FILE_CHUNK_BYTES, encodeFileChunk } from './protocol.js';

const MAX_BUFFERED_BYTES = 1 * 1024 * 1024; // 浏览器发送缓冲超过 1MB 就先歇一歇
const BUFFER_POLL_MS = 20;

export class FileUploader {
  constructor(session, callbacks = {}) {
    this.session = session;
    this.onProgress = callbacks.onProgress || (() => {});
    this.onDone = callbacks.onDone || (() => {});
    this.onError = callbacks.onError || (() => {});
    this._nextId = 1;
    this._active = new Map(); // transferId -> { file, cancelled }
  }

  /** 被控端回传的 file_progress / file_done / file_error 由这里消费。 */
  handleServerMessage(msg) {
    const entry = this._active.get(msg.id);
    if (msg.t === 'file_done') {
      this._active.delete(msg.id);
      this.onDone({ id: msg.id, name: entry ? entry.file.name : msg.path, savedAs: msg.path });
    } else if (msg.t === 'file_error') {
      if (entry) entry.cancelled = true;
      this._active.delete(msg.id);
      this.onError({ id: msg.id, name: entry ? entry.file.name : '', message: msg.message });
    }
  }

  cancel(transferId) {
    const entry = this._active.get(transferId);
    if (!entry) return;
    entry.cancelled = true;
    this._active.delete(transferId);
    this.session.sendControl({ t: 'file_abort', id: transferId });
  }

  async upload(file) {
    const transferId = this._nextId++;
    const entry = { file, cancelled: false };
    this._active.set(transferId, entry);

    this.onProgress({ id: transferId, name: file.name, sent: 0, total: file.size });
    await this.session.sendControl({
      t: 'file_begin', id: transferId, name: file.name, size: file.size,
    });

    let seq = 0;
    let offset = 0;
    try {
      while (offset < file.size) {
        if (entry.cancelled) return;
        if (!this.session.isOpen) throw new Error('连接已断开');

        const slice = file.slice(offset, Math.min(offset + FILE_CHUNK_BYTES, file.size));
        const bytes = new Uint8Array(await slice.arrayBuffer());
        await this.session.sendPlaintext(encodeFileChunk(transferId, seq, bytes));

        offset += bytes.length;
        seq += 1;
        this.onProgress({ id: transferId, name: file.name, sent: offset, total: file.size });

        // 背压:等浏览器把已排队的数据发出去一部分再继续,避免挤占画面与操作
        while (this.session.bufferedAmount > MAX_BUFFERED_BYTES && !entry.cancelled) {
          await sleep(BUFFER_POLL_MS);
        }
      }
      if (entry.cancelled) return;
      // 零字节文件也要走完 begin -> end 流程,被控端会生成一个空文件
      await this.session.sendControl({ t: 'file_end', id: transferId });
    } catch (err) {
      this._active.delete(transferId);
      this.session.sendControl({ t: 'file_abort', id: transferId });
      this.onError({ id: transferId, name: file.name, message: err.message || String(err) });
    }
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
