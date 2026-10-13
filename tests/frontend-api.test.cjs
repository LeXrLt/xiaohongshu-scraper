// Run with: node --test tests/frontend-api.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../web/frontend/node_modules/typescript');

const source = ts.transpileModule(
  readFileSync(path.join(__dirname, '../web/frontend/lib/api.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } },
).outputText;

function loadApi(fetch, key = 'test-key') {
  let now = 1_000_000;
  const context = {
    exports: {}, fetch, Headers, URLSearchParams,
    process: { env: { NEXT_PUBLIC_API_AUTH_KEY: key } },
    Date: class extends Date { static now() { return now; } },
  };
  vm.runInNewContext(source, context);
  return { api: context.exports, advance: (ms) => { now += ms; } };
}

const json = (data, status = 200) => new Response(JSON.stringify(data), { status });
const token = (value = 'token-1') => json({ access_token: value, expires_in: 7200 });

test('concurrent data calls share one exchange and use Authorization', async () => {
  let exchanges = 0;
  const { api } = loadApi(async (url, init) => {
    if (url === '/api/auth/token') {
      exchanges++;
      assert.equal(init.method, 'POST');
      assert.equal(init.headers['X-API-Key'], 'test-key');
      assert.equal(init.cache, 'no-store');
      return token();
    }
    assert.equal(init.headers.get('Authorization'), 'Bearer token-1');
    assert.equal(init.headers.has('X-API-Key'), false);
    assert.equal(init.cache, 'no-store');
    return json({ items: [], total: 0 });
  });
  await Promise.all([api.getNotes(), api.getMerchants(), api.getSettings()]);
  await api.getNote('example');
  assert.equal(exchanges, 1);
});

test('token refreshes before its two-hour expiry', async () => {
  let exchanges = 0;
  const { api, advance } = loadApi(async (url) => {
    if (url === '/api/auth/token') { exchanges++; return token(); }
    return json({});
  });
  await api.getNotes();
  advance(7_169_000);
  await api.getNotes();
  assert.equal(exchanges, 1);
  advance(1_000);
  await api.getNotes();
  assert.equal(exchanges, 2);
});

test('401 exchanges a new token and retries a PUT with its body and headers', async () => {
  let exchanges = 0;
  let writes = 0;
  const settings = { keywords: ['美食'], bloggers: [], cookies: '' };
  const { api } = loadApi(async (url, init) => {
    if (url === '/api/auth/token') return token(`token-${++exchanges}`);
    writes++;
    assert.equal(init.method, 'PUT');
    assert.equal(init.headers.get('Content-Type'), 'application/json');
    assert.equal(init.body, JSON.stringify(settings));
    assert.equal(init.headers.get('Authorization'), `Bearer token-${writes}`);
    return writes === 1 ? json({}, 401) : json(settings);
  });
  assert.deepEqual(await api.saveSettings(settings), settings);
  assert.equal(exchanges, 2);
  assert.equal(writes, 2);
});

test('persistent 401 retries only once', async () => {
  let exchanges = 0;
  let calls = 0;
  const { api } = loadApi(async (url) => {
    if (url === '/api/auth/token') { exchanges++; return token(); }
    calls++;
    return json({ detail: 'Unauthorized' }, 401);
  });
  await assert.rejects(api.getNotes(), /Unauthorized/);
  assert.equal(exchanges, 2);
  assert.equal(calls, 2);
});

test('a late 401 reuses the refreshed token without another exchange', async () => {
  let exchanges = 0;
  let rejectLate;
  let lateStarted;
  const started = new Promise((resolve) => { lateStarted = resolve; });
  const { api } = loadApi(async (url, init) => {
    if (url === '/api/auth/token') return token(`token-${++exchanges}`);
    if (init.headers.get('Authorization') === 'Bearer token-1') {
      if (url.includes('late')) {
        lateStarted();
        return new Promise((resolve) => { rejectLate = () => resolve(json({}, 401)); });
      }
      return json({}, 401);
    }
    return json({});
  });
  const late = api.getNote('late');
  await started;
  await api.getNotes();
  rejectLate();
  await late;
  assert.equal(exchanges, 2);
});

test('failed key exchange is recoverable on a subsequent request', async () => {
  let exchanges = 0;
  let calls = 0;
  const { api } = loadApi(async (url) => {
    if (url === '/api/auth/token') {
      return ++exchanges === 1 ? json({ detail: 'Invalid API key' }, 401) : token();
    }
    calls++;
    return json({});
  });
  await assert.rejects(api.getNotes(), /Invalid API key/);
  assert.equal(calls, 0);
  await api.getNotes();
  assert.equal(exchanges, 2);
  assert.equal(calls, 1);
});

test('non-auth errors do not trigger retries', async () => {
  let exchanges = 0;
  let calls = 0;
  const { api } = loadApi(async (url) => {
    if (url === '/api/auth/token') { exchanges++; return token(); }
    calls++;
    return json({ detail: 'Unavailable' }, 503);
  });
  await assert.rejects(api.getNotes(), /Unavailable/);
  assert.equal(exchanges, 1);
  assert.equal(calls, 1);
});

test('missing environment key fails without sending a request', async () => {
  const { api } = loadApi(() => { throw new Error('unexpected request'); }, '');
  await assert.rejects(api.getNotes(), /API_AUTH_KEY/);
});
