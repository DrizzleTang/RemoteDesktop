'use strict';
/*
 * 协议常量与二进制帧解析。
 *
 * 与 Python 侧 common/protocol.py 逐字节对应,任何一边改动都必须同步另一边。
 *
 * 加密后的每条 WebSocket 二进制消息 = [12字节随机nonce][AES-GCM密文]。
 * 解密后的明文第 1 个字节是 msg_kind:
 *   1 = 控制消息(UTF-8 JSON)
 *   2 = 关键帧    -> [16字节帧头][整幅JPEG]
 *   3 = 增量帧    -> [16字节帧头][矩形数(2)][若干 [12字节矩形头][JPEG] ]
 *   4 = 文件分块  -> [transfer_id(4)][seq(4)][原始字节]
 *
 * 帧头(大端): magic(1)=0xF1 seq(4) ts_ms(4) width(2) height(2)
 *              quality(1) fmt(1) flags(1, bit0=关键帧)
 * 矩形头(大端): x(2) y(2) w(2) h(2) size(4)
 * 矩形坐标位于"输出图像坐标系",即与最近一个关键帧声明的 width/height 同一
 * 坐标系,客户端直接把每个矩形画到 canvas 对应位置即可,无需坐标换算。
 */

export const MSG_CONTROL = 1;
export const MSG_VIDEO_FRAME = 2;
export const MSG_VIDEO_DELTA = 3;
export const MSG_FILE_CHUNK = 4;

export const FRAME_MAGIC = 0xf1;
export const FRAME_HEADER_BYTES = 16;
export const RECT_HEADER_BYTES = 12;
export const FILE_CHUNK_HEADER_BYTES = 8;

export const FMT_JPEG = 0;
export const FMT_WEBP = 1;
export const MIME_BY_FMT = { 0: 'image/jpeg', 1: 'image/webp' };

export const MAX_FRAME_DIMENSION = 8192; // 防止被篡改的宽高把 canvas/内存撑爆
export const MAX_RECTS_PER_FRAME = 512;
export const FILE_CHUNK_BYTES = 64 * 1024;

export class ProtocolError extends Error {}

function readFrameHeader(dv) {
  if (dv.getUint8(0) !== FRAME_MAGIC) throw new ProtocolError('帧头 magic 不匹配');
  const header = {
    seq: dv.getUint32(1),
    tsMs: dv.getUint32(5),
    width: dv.getUint16(9),
    height: dv.getUint16(11),
    quality: dv.getUint8(13),
    fmt: dv.getUint8(14),
    keyframe: (dv.getUint8(15) & 0x01) !== 0,
  };
  if (header.width === 0 || header.height === 0
      || header.width > MAX_FRAME_DIMENSION || header.height > MAX_FRAME_DIMENSION) {
    throw new ProtocolError(`帧尺寸异常: ${header.width}x${header.height}`);
  }
  return header;
}

/** 解析关键帧。body 是去掉 msg_kind 之后的字节。 */
export function decodeKeyframe(body) {
  if (body.length < FRAME_HEADER_BYTES) throw new ProtocolError('关键帧帧头被截断');
  const dv = new DataView(body.buffer, body.byteOffset, body.byteLength);
  const header = readFrameHeader(dv);
  return { ...header, imageBytes: body.subarray(FRAME_HEADER_BYTES) };
}

/** 解析增量帧,返回 { ...帧头, rects: [{x,y,w,h,imageBytes}] }。 */
export function decodeDelta(body) {
  if (body.length < FRAME_HEADER_BYTES + 2) throw new ProtocolError('增量帧帧头被截断');
  const dv = new DataView(body.buffer, body.byteOffset, body.byteLength);
  const header = readFrameHeader(dv);

  const rectCount = dv.getUint16(FRAME_HEADER_BYTES);
  if (rectCount > MAX_RECTS_PER_FRAME) throw new ProtocolError(`增量帧矩形数过多: ${rectCount}`);

  const rects = [];
  let cursor = FRAME_HEADER_BYTES + 2;
  for (let i = 0; i < rectCount; i++) {
    if (body.length < cursor + RECT_HEADER_BYTES) throw new ProtocolError('增量帧矩形头被截断');
    const x = dv.getUint16(cursor);
    const y = dv.getUint16(cursor + 2);
    const w = dv.getUint16(cursor + 4);
    const h = dv.getUint16(cursor + 6);
    const size = dv.getUint32(cursor + 8);
    cursor += RECT_HEADER_BYTES;
    if (body.length < cursor + size) throw new ProtocolError('增量帧矩形数据被截断');
    rects.push({ x, y, w, h, imageBytes: body.subarray(cursor, cursor + size) });
    cursor += size;
  }
  return { ...header, rects };
}

/** 把一个文件分块编码成待加密的明文(含 msg_kind 前缀)。 */
export function encodeFileChunk(transferId, seq, data) {
  const out = new Uint8Array(1 + FILE_CHUNK_HEADER_BYTES + data.length);
  const dv = new DataView(out.buffer);
  out[0] = MSG_FILE_CHUNK;
  dv.setUint32(1, transferId >>> 0);
  dv.setUint32(5, seq >>> 0);
  out.set(data, 1 + FILE_CHUNK_HEADER_BYTES);
  return out;
}

/** 解析文件分块(被控端 -> 主控端方向的下载)。 */
export function decodeFileChunk(body) {
  if (body.length < FILE_CHUNK_HEADER_BYTES) throw new ProtocolError('文件分块头部被截断');
  const dv = new DataView(body.buffer, body.byteOffset, body.byteLength);
  return {
    transferId: dv.getUint32(0),
    seq: dv.getUint32(4),
    data: body.subarray(FILE_CHUNK_HEADER_BYTES),
  };
}

/**
 * 探测浏览器能解码哪些格式,用于在 hello 里向被控端声明能力。
 * WebP 在增量帧的小矩形上比 JPEG 省 80% 以上,但 Safari 14 以下不支持,
 * 必须能优雅回落到 JPEG。
 */
export async function detectCodecs() {
  const codecs = ['jpeg'];
  try {
    // 1x1 的合法 WebP;能成功解码才说明真的支持
    const bytes = Uint8Array.from(atob(
      'UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=='), (c) => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/webp' }));
    bitmap.close();
    codecs.unshift('webp');
  } catch {
    // 不支持 WebP,保持只声明 jpeg
  }
  return codecs;
}
