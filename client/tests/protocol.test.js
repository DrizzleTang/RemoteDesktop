'use strict';
/*
 * 二进制协议解析的测试。
 *
 * 这些字节直接来自网络,是最典型的不可信输入:一个被攻陷的中转方或恶意
 * 被控端可以构造任意畸形帧。解析器必须对截断、超长、越界、数量爆炸等情况
 * 明确报错,而不是抛出难以预料的运行时异常或吃掉大量内存。
 *
 * 同时用 Python 生成的真实帧做交叉验证,确保两侧的字节布局完全一致。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const VECTORS = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures', 'crypto_vectors.json'), 'utf8'));
const b64 = (s) => Uint8Array.from(Buffer.from(s, 'base64'));

let P;
test.before(async () => {
  P = await import('../static/js/protocol.js');
});

// ---------------------------------------------------------------------------
// 与 Python 交叉验证
// ---------------------------------------------------------------------------

test('能解析 Python 生成的关键帧', () => {
  const plaintext = b64(VECTORS.keyframe_plaintext_b64);
  assert.equal(plaintext[0], P.MSG_VIDEO_FRAME);
  const frame = P.decodeKeyframe(plaintext.subarray(1));
  assert.equal(frame.seq, 7);
  assert.equal(frame.tsMs, 999);
  assert.equal(frame.width, 640);
  assert.equal(frame.height, 480);
  assert.equal(frame.quality, 50);
  assert.equal(frame.fmt, P.FMT_JPEG);
  assert.equal(frame.keyframe, true);
  assert.deepEqual(Buffer.from(frame.imageBytes), Buffer.from('\xff\xd8IMG\xff\xd9', 'binary'));
});

test('能解析 Python 生成的增量帧', () => {
  const plaintext = b64(VECTORS.delta_plaintext_b64);
  assert.equal(plaintext[0], P.MSG_VIDEO_DELTA);
  const frame = P.decodeDelta(plaintext.subarray(1));
  assert.equal(frame.seq, 42);
  assert.equal(frame.width, 800);
  assert.equal(frame.fmt, P.FMT_WEBP);
  assert.equal(frame.rects.length, 2);
  assert.deepEqual(
    frame.rects.map((r) => [r.x, r.y, r.w, r.h]), [[10, 20, 30, 40], [100, 200, 8, 8]]);
  assert.deepEqual(Buffer.from(frame.rects[0].imageBytes), Buffer.from([1, 2, 3]));
  assert.deepEqual(Buffer.from(frame.rects[1].imageBytes), Buffer.from([0xaa, 0xbb]));
});

test('能解析 Python 生成的文件分块', () => {
  const plaintext = b64(VECTORS.file_chunk_plaintext_b64);
  assert.equal(plaintext[0], P.MSG_FILE_CHUNK);
  const chunk = P.decodeFileChunk(plaintext.subarray(1));
  assert.equal(chunk.transferId, 5);
  assert.equal(chunk.seq, 3);
  assert.equal(Buffer.from(chunk.data).toString(), 'hello-chunk');
});

test('文件分块编码可被 Python 侧格式解析(布局自检)', () => {
  const out = P.encodeFileChunk(0x01020304, 0x05060708, new Uint8Array([9, 9]));
  assert.equal(out[0], P.MSG_FILE_CHUNK);
  const dv = new DataView(out.buffer, out.byteOffset, out.byteLength);
  assert.equal(dv.getUint32(1), 0x01020304);
  assert.equal(dv.getUint32(5), 0x05060708);
  assert.deepEqual(Buffer.from(out.subarray(9)), Buffer.from([9, 9]));
});

// ---------------------------------------------------------------------------
// 畸形输入
// ---------------------------------------------------------------------------

function makeHeader({ magic = 0xf1, seq = 1, ts = 1, w = 100, h = 100,
                      quality = 70, fmt = 0, flags = 1 } = {}) {
  const buf = new Uint8Array(P.FRAME_HEADER_BYTES);
  const dv = new DataView(buf.buffer);
  dv.setUint8(0, magic);
  dv.setUint32(1, seq);
  dv.setUint32(5, ts);
  dv.setUint16(9, w);
  dv.setUint16(11, h);
  dv.setUint8(13, quality);
  dv.setUint8(14, fmt);
  dv.setUint8(15, flags);
  return buf;
}

test('帧头被截断时报错', () => {
  assert.throws(() => P.decodeKeyframe(new Uint8Array(5)), P.ProtocolError);
  assert.throws(() => P.decodeDelta(new Uint8Array(5)), P.ProtocolError);
});

test('magic 不匹配时报错', () => {
  assert.throws(() => P.decodeKeyframe(makeHeader({ magic: 0x00 })), /magic/);
});

test('宽高为 0 或过大时报错(防止撑爆 canvas/内存)', () => {
  assert.throws(() => P.decodeKeyframe(makeHeader({ w: 0 })), /尺寸异常/);
  assert.throws(() => P.decodeKeyframe(makeHeader({ h: 0 })), /尺寸异常/);
  assert.throws(() => P.decodeKeyframe(makeHeader({ w: 60000 })), /尺寸异常/);
  assert.throws(() => P.decodeKeyframe(makeHeader({ h: 60000 })), /尺寸异常/);
});

test('增量帧矩形数超上限时报错', () => {
  const body = new Uint8Array(P.FRAME_HEADER_BYTES + 2);
  body.set(makeHeader({ flags: 0 }), 0);
  new DataView(body.buffer).setUint16(P.FRAME_HEADER_BYTES, 60000);
  assert.throws(() => P.decodeDelta(body), /矩形数过多/);
});

test('增量帧矩形头部被截断时报错', () => {
  const body = new Uint8Array(P.FRAME_HEADER_BYTES + 2 + 4); // 矩形头需要 12 字节
  body.set(makeHeader({ flags: 0 }), 0);
  new DataView(body.buffer).setUint16(P.FRAME_HEADER_BYTES, 1);
  assert.throws(() => P.decodeDelta(body), /矩形头被截断/);
});

test('增量帧矩形数据长度撒谎时报错', () => {
  const body = new Uint8Array(P.FRAME_HEADER_BYTES + 2 + P.RECT_HEADER_BYTES + 2);
  body.set(makeHeader({ flags: 0 }), 0);
  const dv = new DataView(body.buffer);
  dv.setUint16(P.FRAME_HEADER_BYTES, 1);
  const off = P.FRAME_HEADER_BYTES + 2;
  dv.setUint16(off, 0); dv.setUint16(off + 2, 0);
  dv.setUint16(off + 4, 10); dv.setUint16(off + 6, 10);
  dv.setUint32(off + 8, 99999); // 声明 99999 字节,实际只有 2 字节
  assert.throws(() => P.decodeDelta(body), /矩形数据被截断/);
});

test('矩形数为 0 的增量帧是合法的', () => {
  const body = new Uint8Array(P.FRAME_HEADER_BYTES + 2);
  body.set(makeHeader({ flags: 0 }), 0);
  const frame = P.decodeDelta(body);
  assert.equal(frame.rects.length, 0);
});

test('文件分块头部被截断时报错', () => {
  assert.throws(() => P.decodeFileChunk(new Uint8Array(3)), /头部被截断/);
});

test('零字节数据的文件分块是合法的', () => {
  const chunk = P.decodeFileChunk(new Uint8Array(P.FILE_CHUNK_HEADER_BYTES));
  assert.equal(chunk.data.length, 0);
});

test('解析不会因为 subarray 偏移而错位', () => {
  // 真实场景里 body 总是从更大的解密缓冲区上 subarray 出来的,
  // DataView 必须带上正确的 byteOffset,否则会读到相邻字节
  const outer = new Uint8Array(64 + P.FRAME_HEADER_BYTES + 3);
  outer.fill(0xcc);
  outer.set(makeHeader({ seq: 12345 }), 64);
  outer.set([1, 2, 3], 64 + P.FRAME_HEADER_BYTES);
  const frame = P.decodeKeyframe(outer.subarray(64));
  assert.equal(frame.seq, 12345);
  assert.deepEqual(Buffer.from(frame.imageBytes), Buffer.from([1, 2, 3]));
});
