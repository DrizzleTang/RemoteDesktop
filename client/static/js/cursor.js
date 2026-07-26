'use strict';
/*
 * 远端鼠标光标的叠加绘制。
 *
 * 屏幕截图本身**不包含**鼠标指针,所以光标由被控端单独采集后通过控制消息
 * 发过来。这样做还有一个好处:光标是移动最频繁的元素,如果画进画面帧里,
 * 它每动一下都会让所在区域变成"脏矩形"触发重传,很费带宽。
 *
 * 形状图像只在第一次出现时随消息带过来一次,之后只发形状 id,这里按 id
 * 缓存。系统光标形状总共就那么几种,命中率很高。
 *
 * 本地浏览器光标保持可见:高延迟下远端光标必然滞后,留着本地光标能给用户
 * 即时的位置反馈,两者之间的间距其实直观反映了当前延迟。
 */

const MAX_CACHED_SHAPES = 32;

export class CursorOverlay {
  constructor(element) {
    this.el = element;
    this._shapes = new Map(); // shape_id -> { url, w, h, hx, hy }
    this._currentShape = null;
    this.visible = false;
  }

  /** 处理一条 cursor 控制消息。 */
  update(msg) {
    if (typeof msg.x !== 'number' || typeof msg.y !== 'number') return;

    if (msg.sid && msg.img) this._cacheShape(msg);

    const shape = msg.sid ? this._shapes.get(msg.sid) : null;
    if (shape && shape !== this._currentShape) {
      this._currentShape = shape;
      this.el.style.backgroundImage = `url(${shape.url})`;
      this.el.style.width = `${shape.w}px`;
      this.el.style.height = `${shape.h}px`;
      this.el.dataset.hotX = String(shape.hx);
      this.el.dataset.hotY = String(shape.hy);
    }
    if (!this._currentShape) return; // 还没拿到形状,先不画

    // 位置用百分比表达,这样画面被 CSS 缩放显示时光标依然对得上
    this.el.style.left = `${(msg.x * 100).toFixed(3)}%`;
    this.el.style.top = `${(msg.y * 100).toFixed(3)}%`;
    // 热点是"光标图像里真正指向的那个点",要把图像整体偏移过去
    this.el.style.marginLeft = `${-this._currentShape.hx}px`;
    this.el.style.marginTop = `${-this._currentShape.hy}px`;
    this.show();
  }

  _cacheShape(msg) {
    if (this._shapes.has(msg.sid)) return;
    if (this._shapes.size >= MAX_CACHED_SHAPES) {
      // 简单的 FIFO 淘汰,顺手释放 blob URL,避免长会话累积内存
      const oldestKey = this._shapes.keys().next().value;
      const oldest = this._shapes.get(oldestKey);
      if (oldest) URL.revokeObjectURL(oldest.url);
      this._shapes.delete(oldestKey);
    }
    try {
      const bytes = Uint8Array.from(atob(msg.img), (c) => c.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: 'image/png' }));
      this._shapes.set(msg.sid, {
        url,
        w: Math.max(1, Math.min(128, msg.w || 24)),
        h: Math.max(1, Math.min(128, msg.h || 24)),
        hx: Math.max(0, Math.min(128, msg.hx || 0)),
        hy: Math.max(0, Math.min(128, msg.hy || 0)),
      });
    } catch (err) {
      console.warn('光标图像解码失败', err);
    }
  }

  show() {
    if (!this.visible) {
      this.visible = true;
      this.el.style.display = 'block';
    }
  }

  hide() {
    this.visible = false;
    this.el.style.display = 'none';
  }

  reset() {
    this.hide();
    for (const shape of this._shapes.values()) URL.revokeObjectURL(shape.url);
    this._shapes.clear();
    this._currentShape = null;
  }
}
