'use strict';
/*
 * 会话加密:PBKDF2 -> HKDF -> AES-256-GCM,全部使用浏览器原生 SubtleCrypto,
 * 不引入任何第三方密码学库。与 Python 侧 common/crypto.py 逐参数对应。
 *
 * 握手:host 发一条明文 kex_init(含随机 salt 与 KDF 迭代次数),双方各自用
 * PBKDF2-HMAC-SHA256(密码, salt, iterations) -> HKDF-SHA256 派生出同一把
 * AES-256-GCM 会话密钥,密码本身永远不经过网络。client 无需回复握手消息,
 * 直接发第一条加密的 hello;host 解密成功即视为密码正确。
 *
 * 防重放:AES-GCM 只保证"篡改会被发现",不保证"同一条密文不会被原样重发"。
 * 一个被攻陷的中转服务器或链路中间人可以把截获的合法密文原样再发一遍,
 * 解密照样成功——如果那条消息是"鼠标点击"或"某个按键",就会被重复执行。
 * 正常流量里每条消息的 nonce 都是新随机生成的,因此这里在会话内记住见过的
 * nonce 并拒绝重复值,即可挡住这类重放。
 */

const HKDF_INFO = 'remotedesktop-session-key-v1';
const REPLAY_WINDOW = 20000; // 会话内记住的最近 nonce 数量上限

// 对未鉴权的 kex_init 参数做范围校验,防止中间人把 KDF 强度改弱,
// 诱导客户端用被弱化的密钥加密第一条消息,从而降低离线暴力破解的门槛;
// 也防止用超大的 iterations/salt 拖垮浏览器标签页。
export const MIN_KEX_ITERATIONS = 100000;
export const MAX_KEX_ITERATIONS = 2000000;
export const KEX_SALT_MIN_BYTES = 8;
export const KEX_SALT_MAX_BYTES = 64;

export class CryptoError extends Error {}

export function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function bytesToHex(bytes) {
  let out = '';
  for (let i = 0; i < bytes.length; i++) out += bytes[i].toString(16).padStart(2, '0');
  return out;
}

function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

/** 校验 kex_init 的参数,返回 { saltBytes, iterations };不合法则抛 CryptoError。 */
export function validateKexInit(msg) {
  const iterations = msg.iterations;
  if (typeof iterations !== 'number' || !Number.isInteger(iterations)
      || iterations < MIN_KEX_ITERATIONS || iterations > MAX_KEX_ITERATIONS) {
    throw new CryptoError('握手参数 iterations 超出安全范围(可能存在中间人篡改)');
  }
  if (typeof msg.salt !== 'string') throw new CryptoError('握手参数 salt 缺失');
  let saltBytes;
  try {
    saltBytes = base64ToBytes(msg.salt);
  } catch {
    throw new CryptoError('握手参数 salt 无法解码');
  }
  if (saltBytes.length < KEX_SALT_MIN_BYTES || saltBytes.length > KEX_SALT_MAX_BYTES) {
    throw new CryptoError('握手参数 salt 长度不合法');
  }
  return { saltBytes, iterations };
}

/** 一条已建立连接的加解密上下文(含会话内防重放状态)。 */
export class SessionCipher {
  constructor(aesKey, saltBytes) {
    this.aesKey = aesKey;
    this.saltBytes = saltBytes; // 同时作为 AES-GCM 的附加认证数据(AAD)
    this._seenNonces = new Set();
    this._nonceOrder = [];
  }

  static async derive(password, saltBytes, iterations) {
    const enc = new TextEncoder();
    const pwMaterial = await crypto.subtle.importKey(
      'raw', enc.encode(password), 'PBKDF2', false, ['deriveBits'],
    );
    const pbkdf2Bits = await crypto.subtle.deriveBits(
      { name: 'PBKDF2', salt: saltBytes, iterations, hash: 'SHA-256' }, pwMaterial, 256,
    );
    const hkdfMaterial = await crypto.subtle.importKey('raw', pbkdf2Bits, 'HKDF', false, ['deriveKey']);
    const aesKey = await crypto.subtle.deriveKey(
      { name: 'HKDF', hash: 'SHA-256', salt: saltBytes, info: enc.encode(HKDF_INFO) },
      hkdfMaterial, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt'],
    );
    return new SessionCipher(aesKey, saltBytes);
  }

  async encrypt(plaintext) {
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const ciphertext = await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: this.saltBytes }, this.aesKey, plaintext,
    );
    return concatBytes(nonce, new Uint8Array(ciphertext));
  }

  async decrypt(wireBytes) {
    if (wireBytes.length < 12 + 16) throw new CryptoError('密文长度不足');
    const nonce = wireBytes.slice(0, 12);
    const ciphertext = wireBytes.slice(12);
    const nonceKey = bytesToHex(nonce);
    if (this._seenNonces.has(nonceKey)) throw new CryptoError('检测到重放的 nonce,已丢弃');
    const plaintextBuf = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: this.saltBytes }, this.aesKey, ciphertext,
    );
    this._rememberNonce(nonceKey);
    return new Uint8Array(plaintextBuf);
  }

  _rememberNonce(nonceKey) {
    this._seenNonces.add(nonceKey);
    this._nonceOrder.push(nonceKey);
    if (this._nonceOrder.length > REPLAY_WINDOW) {
      this._seenNonces.delete(this._nonceOrder.shift());
    }
  }
}
