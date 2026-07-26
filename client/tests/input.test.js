'use strict';
/*
 * 输入采集(尤其是触屏手势状态机)的测试。
 *
 * 触屏手势是一个有状态的判定过程:同一串 touchstart/touchmove/touchend,
 * 按时长和位移的不同要分别解释成"点击""拖动""长按右键""双指滚动"。
 * 这种逻辑很容易在改动中被破坏,而端到端测试只能覆盖其中一两条路径。
 *
 * 用一个假的 canvas 元素收集事件处理函数,再手工投递合成事件来驱动。
 */

const test = require('node:test');
const assert = require('node:assert/strict');

class FakeCanvas {
  constructor(rect = { left: 0, top: 0, width: 1000, height: 500 }) {
    this.handlers = new Map();
    this._rect = rect;
    this.focused = false;
  }

  addEventListener(type, handler) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(handler);
  }

  getBoundingClientRect() {
    return this._rect;
  }

  focus() {
    this.focused = true;
  }

  fire(type, event = {}) {
    const full = { preventDefault() {}, ...event };
    for (const handler of this.handlers.get(type) || []) handler(full);
  }
}

function touches(...points) {
  const list = points.map(([x, y]) => ({ clientX: x, clientY: y }));
  list.length = points.length;
  return list;
}

async function setup({ enabled = true } = {}) {
  const { InputCapture } = await import('../static/js/input.js');
  const canvas = new FakeCanvas();
  const sent = [];
  new InputCapture(canvas, (msg) => sent.push(msg), () => enabled);
  return { canvas, sent };
}

// ---------------------------------------------------------------------------
// 鼠标与键盘
// ---------------------------------------------------------------------------

test('鼠标坐标被归一化到 0~1', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('mousedown', { button: 0, clientX: 250, clientY: 125 });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].t, 'mbtn');
  assert.equal(sent[0].x, 0.25);
  assert.equal(sent[0].y, 0.25);
  assert.equal(sent[0].down, true);
});

test('画面外的坐标被夹紧到边界', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('mousedown', { button: 0, clientX: -500, clientY: 99999 });
  assert.equal(sent[0].x, 0);
  assert.equal(sent[0].y, 1);
});

test('鼠标中键/右键被正确识别', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('mousedown', { button: 1, clientX: 0, clientY: 0 });
  canvas.fire('mousedown', { button: 2, clientX: 0, clientY: 0 });
  assert.deepEqual(sent.map((m) => m.btn), ['middle', 'right']);
});

test('可打印字符带 key 字段,功能键只带 code', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('keydown', { code: 'KeyA', key: 'a' });
  canvas.fire('keydown', { code: 'ArrowLeft', key: 'ArrowLeft' });
  assert.equal(sent[0].key, 'a');
  assert.equal(sent[0].code, 'KeyA');
  assert.equal(sent[1].key, undefined, '功能键不应带 key 字段');
  assert.equal(sent[1].code, 'ArrowLeft');
});

test('按下与抬起都会发送(支持组合键与长按)', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('keydown', { code: 'ControlLeft', key: 'Control' });
  canvas.fire('keyup', { code: 'ControlLeft', key: 'Control' });
  assert.deepEqual(sent.map((m) => m.down), [true, false]);
});

test('未连接/只读观看时不发送任何输入', async () => {
  const { canvas, sent } = await setup({ enabled: false });
  canvas.fire('mousedown', { button: 0, clientX: 10, clientY: 10 });
  canvas.fire('keydown', { code: 'KeyA', key: 'a' });
  canvas.fire('wheel', { deltaX: 0, deltaY: 100 });
  assert.equal(sent.length, 0);
});

test('滚轮方向与浏览器相反(浏览器 deltaY 向下为正)', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('wheel', { deltaX: 0, deltaY: 100 });
  assert.equal(sent[0].t, 'mscroll');
  assert.ok(sent[0].dy < 0, '向下滚动应产生负的 dy');
});

// ---------------------------------------------------------------------------
// 触屏手势
// ---------------------------------------------------------------------------

test('轻点 = 左键单击', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('touchstart', { touches: touches([500, 250]) });
  canvas.fire('touchend', { touches: [] });

  const buttons = sent.filter((m) => m.t === 'mbtn');
  assert.equal(buttons.length, 2, '应产生一次按下+一次抬起');
  assert.equal(buttons[0].btn, 'left');
  assert.equal(buttons[0].down, true);
  assert.equal(buttons[1].down, false);
  assert.equal(buttons[0].x, 0.5);
});

test('按下时先把光标移过去(长按右键才会出现在正确位置)', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('touchstart', { touches: touches([250, 125]) });
  assert.equal(sent[0].t, 'mmove');
  assert.equal(sent[0].x, 0.25);
});

test('拖动 = 移动鼠标,不产生点击', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('touchstart', { touches: touches([100, 100]) });
  sent.length = 0;
  canvas.fire('touchmove', { touches: touches([400, 300]) });
  canvas.fire('touchend', { touches: [] });

  assert.ok(sent.some((m) => m.t === 'mmove'), '应有移动事件');
  assert.equal(sent.filter((m) => m.t === 'mbtn').length, 0, '拖动不应被判定为点击');
});

test('双指上下滑动 = 滚轮滚动', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('touchstart', { touches: touches([100, 200], [200, 200]) });
  sent.length = 0;
  canvas.fire('touchmove', { touches: touches([100, 260], [200, 260]) });

  const scrolls = sent.filter((m) => m.t === 'mscroll');
  assert.equal(scrolls.length, 1);
  assert.ok(scrolls[0].dy > 0, '向下滑动应产生正的 dy');
  assert.equal(scrolls[0].dx, 0);
});

test('双指手势不会被误判成点击', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('touchstart', { touches: touches([100, 200], [200, 200]) });
  canvas.fire('touchend', { touches: [] });
  assert.equal(sent.filter((m) => m.t === 'mbtn').length, 0);
});

test('长按 = 右键单击,且随后的抬起不再补一次左键', async (t) => {
  const { canvas, sent } = await setup();
  canvas.fire('touchstart', { touches: touches([500, 250]) });
  await new Promise((resolve) => setTimeout(resolve, 600)); // 超过长按阈值 500ms

  const rightClicks = sent.filter((m) => m.t === 'mbtn' && m.btn === 'right');
  assert.equal(rightClicks.length, 2, '长按应产生右键按下+抬起');

  sent.length = 0;
  canvas.fire('touchend', { touches: [] });
  assert.equal(sent.filter((m) => m.t === 'mbtn').length, 0, '长按后抬起不应再产生左键点击');
});

test('长按前移动了手指则不触发右键', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('touchstart', { touches: touches([100, 100]) });
  canvas.fire('touchmove', { touches: touches([400, 400]) });
  await new Promise((resolve) => setTimeout(resolve, 600));
  assert.equal(sent.filter((m) => m.btn === 'right').length, 0);
});

test('touchcancel 会清理状态,不残留待触发的长按', async () => {
  const { canvas, sent } = await setup();
  canvas.fire('touchstart', { touches: touches([500, 250]) });
  canvas.fire('touchcancel', {});
  sent.length = 0;
  await new Promise((resolve) => setTimeout(resolve, 600));
  assert.equal(sent.length, 0, 'touchcancel 之后不应再有任何事件');
});
