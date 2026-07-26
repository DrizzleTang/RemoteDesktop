'use strict';
/*
 * 输入采集:鼠标、键盘、以及触屏手势。
 *
 * 坐标一律用 0~1 的归一化值发送,由被控端按"当前真实屏幕分辨率"还原成
 * 绝对像素——这样无论智能模式把画面缩放到多少,点击位置始终精确对应。
 *
 * 按键:
 *  - 可打印字符走 KeyboardEvent.key(点按语义),兼容各种键盘布局与输入法
 *    产生的最终字符;
 *  - 功能键/修饰键走 layout 无关的 KeyboardEvent.code,并保留完整的
 *    按下/抬起语义,以支持组合键(Ctrl+C)与长按(方向键连续移动)。
 *
 * 触屏手势(手机/平板浏览器):
 *  - 单指轻点            -> 左键单击
 *  - 单指拖动            -> 移动鼠标
 *  - 单指长按(500ms)   -> 右键单击
 *  - 双指上下滑动        -> 滚轮滚动
 */

const MOUSE_MOVE_THROTTLE_MS = 33; // ~30Hz,弱网下没必要发更密
const TAP_MAX_MS = 300;            // 短于此时长且几乎没移动 -> 视为点击
const TAP_MAX_MOVE_PX = 12;
const LONG_PRESS_MS = 500;
const TOUCH_SCROLL_DIVISOR = 40;   // 触屏滑动像素 -> 滚轮"格"数的换算系数

const BUTTON_NAMES = { 0: 'left', 1: 'middle', 2: 'right' };

export class InputCapture {
  /**
   * @param canvas 画面元素
   * @param send   回调 send(controlMessageObject)
   * @param isEnabled 回调,返回当前是否允许发送输入(未连接/只读观看时为 false)
   */
  constructor(canvas, send, isEnabled) {
    this.canvas = canvas;
    this.send = send;
    this.isEnabled = isEnabled;
    this._lastMoveAt = 0;
    this._touch = null;
    this._longPressTimer = null;
    this._bind();
  }

  _normalized(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return { x: 0, y: 0 };
    return {
      x: Math.min(1, Math.max(0, (clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (clientY - rect.top) / rect.height)),
    };
  }

  _emit(msg) {
    if (!this.isEnabled()) return;
    this.send(msg);
  }

  _bind() {
    const canvas = this.canvas;

    // ---- 鼠标 ----
    canvas.addEventListener('mousemove', (e) => {
      const now = performance.now();
      if (now - this._lastMoveAt < MOUSE_MOVE_THROTTLE_MS) return;
      this._lastMoveAt = now;
      const { x, y } = this._normalized(e.clientX, e.clientY);
      this._emit({ t: 'mmove', x, y });
    });

    canvas.addEventListener('mousedown', (e) => {
      e.preventDefault();
      canvas.focus();
      const { x, y } = this._normalized(e.clientX, e.clientY);
      this._emit({ t: 'mbtn', btn: BUTTON_NAMES[e.button] || 'left', down: true, x, y });
    });

    canvas.addEventListener('mouseup', (e) => {
      e.preventDefault();
      const { x, y } = this._normalized(e.clientX, e.clientY);
      this._emit({ t: 'mbtn', btn: BUTTON_NAMES[e.button] || 'left', down: false, x, y });
    });

    canvas.addEventListener('contextmenu', (e) => e.preventDefault());

    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      this._emit({ t: 'mscroll', dx: -e.deltaX / 100, dy: -e.deltaY / 100 });
    }, { passive: false });

    // ---- 键盘 ----
    canvas.addEventListener('keydown', (e) => {
      e.preventDefault();
      const msg = { t: 'key', code: e.code, down: true };
      if (e.key && e.key.length === 1) msg.key = e.key;
      this._emit(msg);
    });

    canvas.addEventListener('keyup', (e) => {
      e.preventDefault();
      const msg = { t: 'key', code: e.code, down: false };
      if (e.key && e.key.length === 1) msg.key = e.key;
      this._emit(msg);
    });

    // ---- 触屏 ----
    canvas.addEventListener('touchstart', (e) => this._onTouchStart(e), { passive: false });
    canvas.addEventListener('touchmove', (e) => this._onTouchMove(e), { passive: false });
    canvas.addEventListener('touchend', (e) => this._onTouchEnd(e), { passive: false });
    canvas.addEventListener('touchcancel', () => this._cancelTouch(), { passive: false });
  }

  _onTouchStart(e) {
    e.preventDefault();
    this.canvas.focus();
    if (e.touches.length === 1) {
      const touch = e.touches[0];
      const { x, y } = this._normalized(touch.clientX, touch.clientY);
      this._touch = {
        startX: touch.clientX, startY: touch.clientY,
        lastY: touch.clientY, startedAt: performance.now(),
        moved: false, mode: 'single', longPressed: false,
      };
      // 手指按下先把光标移过去,这样长按触发右键时菜单出现在正确位置
      this._emit({ t: 'mmove', x, y });
      this._longPressTimer = setTimeout(() => {
        if (!this._touch || this._touch.moved) return;
        this._touch.longPressed = true;
        this._emit({ t: 'mbtn', btn: 'right', down: true, x, y });
        this._emit({ t: 'mbtn', btn: 'right', down: false, x, y });
      }, LONG_PRESS_MS);
    } else if (e.touches.length === 2) {
      this._clearLongPress();
      this._touch = {
        mode: 'scroll',
        lastY: (e.touches[0].clientY + e.touches[1].clientY) / 2,
        moved: true, longPressed: false,
      };
    }
  }

  _onTouchMove(e) {
    e.preventDefault();
    if (!this._touch) return;

    if (this._touch.mode === 'scroll' && e.touches.length >= 2) {
      const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      const dy = midY - this._touch.lastY;
      this._touch.lastY = midY;
      if (Math.abs(dy) >= 1) this._emit({ t: 'mscroll', dx: 0, dy: dy / TOUCH_SCROLL_DIVISOR });
      return;
    }

    if (this._touch.mode === 'single' && e.touches.length === 1) {
      const touch = e.touches[0];
      const dist = Math.hypot(touch.clientX - this._touch.startX, touch.clientY - this._touch.startY);
      if (dist > TAP_MAX_MOVE_PX) {
        this._touch.moved = true;
        this._clearLongPress();
      }
      const now = performance.now();
      if (now - this._lastMoveAt < MOUSE_MOVE_THROTTLE_MS) return;
      this._lastMoveAt = now;
      const { x, y } = this._normalized(touch.clientX, touch.clientY);
      this._emit({ t: 'mmove', x, y });
    }
  }

  _onTouchEnd(e) {
    e.preventDefault();
    this._clearLongPress();
    const touchState = this._touch;
    this._touch = null;
    if (!touchState || touchState.mode !== 'single' || touchState.longPressed) return;

    const heldMs = performance.now() - touchState.startedAt;
    if (!touchState.moved && heldMs <= TAP_MAX_MS) {
      const { x, y } = this._normalized(touchState.startX, touchState.startY);
      this._emit({ t: 'mbtn', btn: 'left', down: true, x, y });
      this._emit({ t: 'mbtn', btn: 'left', down: false, x, y });
    }
  }

  _cancelTouch() {
    this._clearLongPress();
    this._touch = null;
  }

  _clearLongPress() {
    if (this._longPressTimer) {
      clearTimeout(this._longPressTimer);
      this._longPressTimer = null;
    }
  }
}
