'use strict';
/*
 * 画面渲染:把收到的关键帧/增量帧画到 canvas 上。
 *
 * 关键帧 -> 重设 canvas 尺寸并整幅绘制。
 * 增量帧 -> canvas 保留上一帧内容,只把变化的矩形逐个贴回原位置。
 *
 * 每帧渲染完成后必须回一条 frame_ack,被控端据此测量"发送到看见"的往返
 * 延迟并驱动自适应画质——所以 ack 一定要在真正画完之后才发,不能在收到
 * 字节时就发,否则测出来的延迟会偏小、自适应会误判网络比实际更好。
 */

export class Renderer {
  constructor(canvas) {
    this.canvas = canvas;
    // alpha:false 让浏览器跳过与页面背景的合成,画不透明的桌面画面更快
    this.ctx = canvas.getContext('2d', { alpha: false });
    this.frameWidth = 0;
    this.frameHeight = 0;
    this.hasContent = false;
  }

  reset() {
    this.hasContent = false;
    this.frameWidth = 0;
    this.frameHeight = 0;
  }

  async drawKeyframe(frame) {
    const bitmap = await createImageBitmap(new Blob([frame.imageBytes], { type: 'image/jpeg' }));
    try {
      if (this.canvas.width !== frame.width || this.canvas.height !== frame.height) {
        // 改 canvas 尺寸会清空内容,所以只在尺寸真的变了时才改
        this.canvas.width = frame.width;
        this.canvas.height = frame.height;
      }
      this.ctx.drawImage(bitmap, 0, 0, frame.width, frame.height);
      this.frameWidth = frame.width;
      this.frameHeight = frame.height;
      this.hasContent = true;
    } finally {
      bitmap.close();
    }
  }

  async drawDelta(frame) {
    // 还没有底图就收到增量帧(例如刚重连、关键帧丢了),直接忽略:
    // 被控端会在 ack 超时后自动补发关键帧。
    if (!this.hasContent) return false;
    if (frame.width !== this.frameWidth || frame.height !== this.frameHeight) {
      // 输出分辨率变了却收到增量帧,说明与被控端状态不同步,等待关键帧
      return false;
    }

    // 并行解码所有矩形,再统一绘制:解码是异步的,批量并行比逐个 await 快得多
    const bitmaps = await Promise.all(frame.rects.map((rect) =>
      createImageBitmap(new Blob([rect.imageBytes], { type: 'image/jpeg' }))));
    try {
      for (let i = 0; i < bitmaps.length; i++) {
        const rect = frame.rects[i];
        this.ctx.drawImage(bitmaps[i], rect.x, rect.y);
      }
    } finally {
      for (const bitmap of bitmaps) bitmap.close();
    }
    return true;
  }
}
