'use strict';
/*
 * 客户端加密实现的测试,重点是**与 Python 侧交叉验证**。
 *
 * 两端各写一遍 PBKDF2/HKDF/AES-GCM,参数只要有一处对不上(迭代次数、
 * info 字符串、AAD、盐的用法),表现就是"连不上"或"画面不动",而各自
 * 单独跑测试都是绿的。所以这里直接用 Python 生成的密文来验证:JS 能解密
 * Python 加密的数据,才说明两侧真的一致。
 *
 * 测试向量在 fixtures/crypto_vectors.json,由 client/tests/gen_vectors.py 生成。
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const VECTORS = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures', 'crypto_vectors.json'), 'utf8'));

const b64 = (s) => Uint8Array.from(Buffer.from(s, 'base64'));

let mod;
test.before(async () => {
  mod = await import('../static/js/crypto.js');
});

test('与 Python 派生出同一把会话密钥', async () => {
  const cipher = await mod.SessionCipher.derive(
    VECTORS.password, b64(VECTORS.salt_b64), VECTORS.iterations);
  // 密钥本身不可导出,改为验证"能解开 Python 加密的数据"——等价且更贴近实际
  const plaintext = await cipher.decrypt(b64(VECTORS.control_ciphertext_b64));
  assert.deepEqual(Buffer.from(plaintext), Buffer.from(b64(VECTORS.control_plaintext_b64)));
});

test('能解出 Python 加密的中文控制消息', async () => {
  const cipher = await mod.SessionCipher.derive(
    VECTORS.password, b64(VECTORS.salt_b64), VECTORS.iterations);
  const plaintext = await cipher.decrypt(b64(VECTORS.control_ciphertext_b64));
  const msg = JSON.parse(Buffer.from(plaintext.subarray(1)).toString('utf8'));
  assert.equal(msg.t, 'hello_ack');
  assert.equal(msg.width, 1920);
  assert.equal(msg['中文'], '值');
});

test('恢复令牌路径与 Python 一致', async () => {
  const cipher = await mod.SessionCipher.fromResumeSecret(
    VECTORS.resume_secret_b64, b64(VECTORS.salt_b64));
  const plaintext = await cipher.decrypt(b64(VECTORS.resume_ciphertext_b64));
  const msg = JSON.parse(Buffer.from(plaintext.subarray(1)).toString('utf8'));
  assert.equal(msg.t, 'ping');
  assert.equal(msg.n, 7);
});

test('加解密可往返', async () => {
  const salt = b64(VECTORS.salt_b64);
  const a = await mod.SessionCipher.derive(VECTORS.password, salt, VECTORS.iterations);
  const b = await mod.SessionCipher.derive(VECTORS.password, salt, VECTORS.iterations);
  const data = new TextEncoder().encode('往返测试 roundtrip');
  const wire = await a.encrypt(data);
  assert.deepEqual(Buffer.from(await b.decrypt(wire)), Buffer.from(data));
});

test('密码不同则无法解密', async () => {
  const salt = b64(VECTORS.salt_b64);
  const a = await mod.SessionCipher.derive('密码A', salt, 100000);
  const b = await mod.SessionCipher.derive('密码B', salt, 100000);
  const wire = await a.encrypt(new TextEncoder().encode('secret'));
  await assert.rejects(() => b.decrypt(wire));
});

test('重放同一条密文会被拒绝', async () => {
  const salt = b64(VECTORS.salt_b64);
  const a = await mod.SessionCipher.derive(VECTORS.password, salt, VECTORS.iterations);
  const b = await mod.SessionCipher.derive(VECTORS.password, salt, VECTORS.iterations);
  const wire = await a.encrypt(new TextEncoder().encode('一次点击'));
  await b.decrypt(wire);
  await assert.rejects(() => b.decrypt(wire), /重放/);
});

test('密文被篡改会被拒绝', async () => {
  const salt = b64(VECTORS.salt_b64);
  const a = await mod.SessionCipher.derive(VECTORS.password, salt, VECTORS.iterations);
  const wire = await a.encrypt(new TextEncoder().encode('payload'));
  wire[wire.length - 1] ^= 0xff;
  const b = await mod.SessionCipher.derive(VECTORS.password, salt, VECTORS.iterations);
  await assert.rejects(() => b.decrypt(wire));
});

test('过短的密文被拒绝', async () => {
  const cipher = await mod.SessionCipher.derive(VECTORS.password, b64(VECTORS.salt_b64), 100000);
  await assert.rejects(() => cipher.decrypt(new Uint8Array(10)), /长度不足/);
});

// ---------------------------------------------------------------------------
// kex_init 参数校验:这是握手阶段唯一的明文消息,必须防降级篡改
// ---------------------------------------------------------------------------

test('合法的 kex_init 通过校验', () => {
  const got = mod.validateKexInit({ salt: VECTORS.salt_b64, iterations: 200000 });
  assert.equal(got.iterations, 200000);
  assert.equal(got.saltBytes.length, 16);
});

test('迭代次数被调低会被拒绝(防降级攻击)', () => {
  assert.throws(() => mod.validateKexInit({ salt: VECTORS.salt_b64, iterations: 1 }), /安全范围/);
  assert.throws(() => mod.validateKexInit({ salt: VECTORS.salt_b64, iterations: 0 }), /安全范围/);
});

test('迭代次数过大会被拒绝(防拖垮浏览器)', () => {
  assert.throws(
    () => mod.validateKexInit({ salt: VECTORS.salt_b64, iterations: 999999999 }), /安全范围/);
});

test('非整数/非数字的迭代次数被拒绝', () => {
  for (const bad of [1.5, '200000', null, undefined, NaN]) {
    assert.throws(() => mod.validateKexInit({ salt: VECTORS.salt_b64, iterations: bad }));
  }
});

test('salt 缺失或长度异常被拒绝', () => {
  assert.throws(() => mod.validateKexInit({ iterations: 200000 }), /salt 缺失/);
  assert.throws(
    () => mod.validateKexInit({ salt: Buffer.from('ab').toString('base64'), iterations: 200000 }),
    /长度不合法/);
  const huge = Buffer.alloc(200).toString('base64');
  assert.throws(() => mod.validateKexInit({ salt: huge, iterations: 200000 }), /长度不合法/);
});
