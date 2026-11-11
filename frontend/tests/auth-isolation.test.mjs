import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const code = ts.transpileModule(readFileSync(new URL('../src/api.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

function tab(localStorage = storage(), fetchImpl) {
  const sessionStorage = storage();
  const requests = [];
  const exports = {};
  vm.runInNewContext(code, {
    exports, AbortController, URLSearchParams,
    window: { localStorage, sessionStorage, setTimeout, clearTimeout },
    fetch: async (url, options) => {
      requests.push({ url, options });
      const data = fetchImpl ? await fetchImpl(url, options) :
        url === '/api/auth/login' ? { ok: true, user_id: JSON.parse(options.body).key + '-user' } : { accounts: [] };
      return { json: async () => data };
    },
  });
  return { api: exports, sessionStorage, localStorage, requests };
}

test('new tabs verify their credential before using any saved user identity', async () => {
  const local = storage();
  local.setItem('wechat_web_access_key', 'alice');
  local.setItem('wechat_web_user_id', 'bob-user');
  const current = tab(local);
  assert.equal(current.api.userStorageKey('login-form'), 'login-form:unverified');
  await current.api.getAccounts();
  assert.equal(current.api.userStorageKey('login-form'), 'login-form:alice-user');
  assert.equal(current.requests[0].url, '/api/auth/login');
  assert.equal(current.requests[1].options.headers['X-Access-Key'], 'alice');
});

test('two tabs keep separate credentials, account selections and form storage', async () => {
  const local = storage();
  const alice = tab(local);
  alice.api.setAccessKey('alice');
  await alice.api.loginWithKey('alice');
  alice.api.setActiveAgentId('alice-session');
  const bob = tab(local);
  bob.api.setAccessKey('bob');
  await bob.api.loginWithKey('bob');
  bob.api.setActiveAgentId('bob-session');
  await alice.api.loginWithKey('alice');
  await alice.api.getAccounts();
  await bob.api.getAccounts();
  assert.equal(alice.api.getAccessKey(), 'alice');
  assert.equal(alice.api.getActiveAgentId(), 'alice-session');
  assert.equal(bob.api.getActiveAgentId(), 'bob-session');
  assert.notEqual(alice.api.userStorageKey('login-form'), bob.api.userStorageKey('login-form'));
  assert.equal(local.getItem('wechat_web_user_id'), 'bob-user');
  assert.equal(alice.requests.at(-1).options.headers['X-Access-Key'], 'alice');
  assert.equal(bob.requests.at(-1).options.headers['X-Access-Key'], 'bob');
});

test('a late login response cannot overwrite the user selected by a newer login', async () => {
  let finishAlice;
  const current = tab(storage(), async (_url, options) => {
    if (JSON.parse(options.body).key === 'alice') return new Promise((resolve) => { finishAlice = resolve; });
    return { ok: true, user_id: 'bob-user' };
  });
  current.api.setAccessKey('alice');
  const oldLogin = current.api.loginWithKey('alice');
  current.api.setAccessKey('bob');
  await current.api.loginWithKey('bob');
  finishAlice({ ok: true, user_id: 'alice-user' });
  assert.equal((await oldLogin).error, 'auth_changed');
  assert.equal(current.api.userStorageKey('login-form'), 'login-form:bob-user');
});

test('account data arriving after a key change is discarded', async () => {
  let finishAccounts;
  const current = tab(storage(), async (url, options) => {
    if (url === '/api/auth/login') return { ok: true, user_id: JSON.parse(options.body).key + '-user' };
    return new Promise((resolve) => { finishAccounts = resolve; });
  });
  current.api.setAccessKey('alice');
  await current.api.loginWithKey('alice');
  const accounts = current.api.getAccounts();
  current.api.clearAccessKey();
  current.api.setAccessKey('bob');
  await current.api.loginWithKey('bob');
  finishAccounts({ accounts: [{ id: 'private-alice-session' }] });
  const result = await accounts;
  assert.equal(result.error, 'auth_changed');
  assert.equal(result.accounts, undefined);
});
