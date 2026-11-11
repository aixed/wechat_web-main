import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const exports = {};
vm.runInNewContext(ts.transpileModule(readFileSync(new URL('../src/protocolLogin.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText, { exports });
const { resolveProtocolLogin } = exports;

test('a new randomly generated RDV uses QR login', () => {
  assert.equal(resolveProtocolLogin('127f78ac', {}).mode, 'qr');
});

test('editing RDV to an unknown value never borrows an existing identity', () => {
  const result = resolveProtocolLogin('12787fac', { '127f78ac': { wxid: 'old-user', nickname: 'Previous' } });
  assert.equal(result.mode, 'qr');
  assert.equal(result.wxid, '');
  assert.equal(result.entry, undefined);
});

test('only an exact RDV match in the current users database enables old login', () => {
  const entry = { wxid: 'wxid_owner', nickname: 'Owner', avatar: 'avatar-test' };
  const result = resolveProtocolLogin('127F78AC', { '127f78ac': entry });
  assert.equal(result.mode, 'old');
  assert.equal(result.entry, entry);
});

test('the same RDV remains QR-only for another users empty mapping', () => {
  assert.equal(resolveProtocolLogin('127f78ac', { '127f78ac': { wxid: 'alice' } }).mode, 'old');
  assert.equal(resolveProtocolLogin('127f78ac', {}).mode, 'qr');
});

test('starting an instance without logging in does not create an old-login identity', () => {
  assert.equal(resolveProtocolLogin('127f78ac', { '127f78ac': { last_used_at: 1 } }).mode, 'qr');
});

test('a transient session identifier is not a saved WeChat identity', () => {
  assert.equal(resolveProtocolLogin('127f78ac', { '127f78ac': { wxid: '12345678901234567890123456789012' } }).mode, 'qr');
});
