import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ApiError, MerchantsClient } from '../src/client.js';

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const token = (value = 'token-1', expires = 7200) => json({ access_token: value, token_type: 'bearer', expires_in: expires });
const client = (fetchImpl, options) => new MerchantsClient({ apiKey: 'test-secret', fetchImpl, ...options });

test('concurrent API reads share a token, which is reused and kept out of query strings', async () => {
  let exchanges = 0;
  let reads = 0;
  const api = client(async (url, init) => {
    assert.equal(init.redirect, 'error');
    assert.equal(init.cache, 'no-store');
    assert.ok(init.signal instanceof AbortSignal);
    assert.equal(url.includes('test-secret'), false);
    if (url.endsWith('/api/auth/token')) {
      exchanges++;
      assert.equal(init.method, 'POST');
      assert.equal(init.headers['X-API-Key'], 'test-secret');
      return token();
    }
    reads++;
    assert.deepEqual(init.headers, { Authorization: 'Bearer token-1' });
    return json({ items: [], total: 0 });
  });
  await Promise.all([api.categories(), api.tags(), api.search()]);
  await api.categories();
  assert.equal(exchanges, 1);
  assert.equal(reads, 4);
});

test('refreshes early and accounts for time spent exchanging tokens', async () => {
  let now = 0;
  let exchanges = 0;
  const api = client(async (url) => {
    if (url.endsWith('/token')) { exchanges++; now += 1000; return token(); }
    return json({});
  }, { now: () => now });
  await api.categories();
  now = 7_169_999;
  await api.categories();
  assert.equal(exchanges, 1);
  now = 7_170_000;
  await api.categories();
  assert.equal(exchanges, 2);
});

test('short-lived tokens can still be reused', async () => {
  let exchanges = 0;
  const api = client(async (url) => {
    if (url.endsWith('/token')) { exchanges++; return token('short', 1); }
    return json({});
  }, { now: () => 0 });
  await api.categories();
  await api.categories();
  assert.equal(exchanges, 1);
});

test('401 renews token and retries once with identical encoded filters', async () => {
  let exchanges = 0;
  const urls = [];
  const api = client(async (url, init) => {
    if (url.endsWith('/token')) return token(`token-${++exchanges}`);
    urls.push(url);
    assert.equal(init.headers.Authorization, `Bearer token-${urls.length}`);
    return urls.length === 1 ? json({}, 401) : json({ items: [], total: 0 });
  });
  await api.search({ category_id: 'dining', tag_ids: ['sea', 'quiet'], keyword: '海景', page: 2 });
  assert.equal(exchanges, 2);
  assert.equal(urls[0], urls[1]);
  const query = new URL(urls[0]).searchParams;
  assert.deepEqual(query.getAll('tag_ids'), ['sea', 'quiet']);
  assert.equal(query.get('keyword'), '海景');
  assert.equal(query.get('page'), '2');
});

test('a late concurrent 401 reuses the token refreshed by another request', async () => {
  let exchanges = 0;
  let releaseLate;
  let signalStarted;
  const started = new Promise((resolve) => { signalStarted = resolve; });
  const api = client(async (url, init) => {
    if (url.endsWith('/token')) return token(`token-${++exchanges}`);
    if (init.headers.Authorization === 'Bearer token-1') {
      if (url.includes('late')) {
        signalStarted();
        return new Promise((resolve) => { releaseLate = () => resolve(json({}, 401)); });
      }
      return json({}, 401);
    }
    return json({});
  });
  const late = api.detail('late');
  await started;
  await api.categories();
  releaseLate();
  await late;
  assert.equal(exchanges, 2);
});

test('persistent 401 stops after one retry', async () => {
  let exchanges = 0;
  let reads = 0;
  const api = client(async (url) => {
    if (url.endsWith('/token')) { exchanges++; return token(); }
    reads++;
    return json({ detail: 'test-secret' }, 401);
  });
  await assert.rejects(api.categories(), (error) => {
    assert.match(error.message, /401/);
    assert.equal(error.message.includes('test-secret'), false);
    return true;
  });
  assert.equal(exchanges, 2);
  assert.equal(reads, 2);
});

test('failed exchange does not reach data API and can recover', async () => {
  let exchanges = 0;
  let reads = 0;
  const api = client(async (url) => {
    if (url.endsWith('/token')) return ++exchanges === 1 ? json({}, 401) : token();
    reads++;
    return json({});
  });
  await assert.rejects(api.categories(), /401/);
  assert.equal(reads, 0);
  await api.categories();
  assert.equal(exchanges, 2);
  assert.equal(reads, 1);
});

test('other HTTP errors are not retried or leaked', async () => {
  for (const status of [403, 404, 422, 429, 503]) {
    let reads = 0;
    const api = client(async (url) => {
      if (url.endsWith('/token')) return token();
      reads++;
      return json({ detail: 'test-secret database-password' }, status);
    });
    await assert.rejects(api.categories(), (error) => {
      assert.match(error.message, new RegExp(String(status)));
      assert.doesNotMatch(error.message, /test-secret|database-password/);
      return true;
    });
    assert.equal(reads, 1);
  }
});

test('invalid token responses and invalid JSON fail cleanly', async () => {
  for (const response of [json(null), json({}), json({ access_token: 'x', token_type: 'bearer', expires_in: -1 }),
    new Response('not-json')]) {
    const api = client(async () => response);
    await assert.rejects(api.categories(), ApiError);
  }
});

test('network errors are sanitized and do not leak keys', async () => {
  const api = client(async () => { throw new Error('test-secret socket error'); });
  await assert.rejects(api.categories(), (error) => {
    assert.match(error.message, /无法连接/);
    assert.doesNotMatch(error.message, /test-secret/);
    return true;
  });
});

test('base URL prefixes and special characters in merchant IDs are preserved safely', async () => {
  let detailUrl;
  const api = client(async (url) => {
    if (url.endsWith('/token')) return token();
    detailUrl = url;
    return json({});
  }, { baseUrl: 'https://example.com/backend/' });
  await api.detail('id/?&中文', { notes_page: 2 });
  assert.equal(detailUrl, 'https://example.com/backend/api/merchants/id%2F%3F%26%E4%B8%AD%E6%96%87?notes_page=2');
});

test('missing secrets, unsafe URLs and invalid timeouts fail before making requests', () => {
  assert.throws(() => new MerchantsClient(), /MERCHANTS_API_KEY/);
  for (const baseUrl of ['bad-url', 'file:///etc/passwd', 'https://key@example.com',
    'https://example.com?key=secret', 'https://example.com#secret']) {
    assert.throws(() => client(() => {}, { baseUrl }), /MERCHANTS_API_URL/);
  }
  for (const timeoutMs of [0, NaN, 120001, 1.5]) {
    assert.throws(() => client(() => {}, { timeoutMs }), /MERCHANTS_API_TIMEOUT_MS/);
  }
});
