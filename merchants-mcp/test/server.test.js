import assert from 'node:assert/strict';
import { test } from 'node:test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createServer as createHttpServer } from 'node:http';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const entry = fileURLToPath(new URL('../src/index.js', import.meta.url));

test('stdio exposes the complete discovery/search/source workflow through authenticated HTTP', async (t) => {
  const requests = [];
  const http = createHttpServer((req, res) => {
    requests.push({ url: req.url, authorization: req.headers.authorization, key: req.headers['x-api-key'] });
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/auth/token') {
      assert.equal(req.headers['x-api-key'], 'stdio-test-secret');
      res.end(JSON.stringify({ access_token: 'stdio-token', token_type: 'bearer', expires_in: 7200 }));
      return;
    }
    if (req.headers.authorization !== 'Bearer stdio-token') {
      res.writeHead(401); res.end('{}'); return;
    }
    if (req.url.startsWith('/api/merchant-categories')) {
      res.end(JSON.stringify({ items: [{ category_id: 'dining.restaurant', name: '餐厅', merchant_count: 1 }], total: 1 }));
    } else if (req.url.startsWith('/api/merchant-tags')) {
      res.end(JSON.stringify({ items: [{ tag_id: 'sea', name: '海景' }], total: 1 }));
    } else if (req.url.startsWith('/api/merchants/m1')) {
      res.end(JSON.stringify({ merchant_id: 'm1', canonical_name: '海湾餐厅', note_count: 2, notes_total: 2,
        notes_has_more: true, notes: [{ note_id: 'n1', title: '探店', content: '海'.repeat(12001),
          evidence_text: '海湾餐厅有海景。', note_url: 'https://example.com/n1', published_at: '2026-10-01' }] }));
    } else if (req.url.startsWith('/api/merchants?')) {
      res.end(JSON.stringify({ items: [{ merchant_id: 'm1', canonical_name: '海湾餐厅', tags: [{ tag_id: 'sea' }] }],
        total: 1, page: 1, page_size: 10 }));
    } else { res.writeHead(404); res.end('{}'); }
  });
  http.listen(0, '127.0.0.1');
  await once(http, 'listening');
  t.after(() => new Promise((resolve) => { http.closeAllConnections(); http.close(resolve); }));
  const transport = new StdioClientTransport({
    command: process.execPath, args: [entry], stderr: 'pipe',
    env: { MERCHANTS_API_KEY: 'stdio-test-secret', MERCHANTS_API_URL: `http://127.0.0.1:${http.address().port}` },
  });
  let stderr = '';
  transport.stderr.on('data', (chunk) => { stderr += chunk; });
  const mcp = new Client({ name: 'merchants-test', version: '1.0.0' });
  t.after(() => mcp.close());
  await mcp.connect(transport);
  assert.match(mcp.getInstructions(), /先调用 list_merchant_categories/);
  const tools = (await mcp.listTools()).tools;
  assert.deepEqual(tools.map((tool) => tool.name).sort(), [
    'get_merchant_details', 'list_merchant_categories', 'list_merchant_tags', 'search_merchants',
  ]);
  for (const tool of tools) assert.equal(tool.annotations.readOnlyHint, true);
  const call = async (name, args = {}) => {
    const response = await mcp.callTool({ name, arguments: args });
    assert.equal(response.isError, undefined);
    return JSON.parse(response.content[0].text);
  };
  const categories = await call('list_merchant_categories');
  assert.equal(categories.items[0].category_id, 'dining.restaurant');
  await call('list_merchant_tags', { category_id: 'dining.restaurant' });
  const found = await call('search_merchants', { category_id: 'dining.restaurant', tag_ids: ['sea'], city: '香港' });
  assert.equal(found.items[0].merchant_id, 'm1');
  assert.equal(found.selected_category.name, '餐厅');
  const detail = await call('get_merchant_details', { merchant_id: 'm1' });
  assert.equal(detail.notes[0].content.length, 12000);
  assert.deepEqual(detail.notes[0].truncated_fields, ['content']);
  assert.equal(detail.notes[0].note_url, 'https://example.com/n1');
  assert.equal(detail.notes_has_more, true);
  const unknown = await mcp.callTool({ name: 'search_merchants', arguments: { category_id: 'guessed' } });
  assert.equal(unknown.isError, true);
  assert.match(unknown.content[0].text, /分类不存在/);
  const invalid = await mcp.callTool({ name: 'search_merchants', arguments: {} });
  assert.equal(invalid.isError, true);
  const tooManyNotes = await mcp.callTool({ name: 'get_merchant_details', arguments: { merchant_id: 'm1', notes_page_size: 11 } });
  assert.equal(tooManyNotes.isError, true);
  assert.equal(requests.filter((r) => r.url === '/api/auth/token').length, 1);
  const search = requests.find((r) => r.url.startsWith('/api/merchants?'));
  assert.equal(new URL(search.url, 'http://test').searchParams.get('tag_ids'), 'sea');
  assert.ok(requests.find((r) => r.url.includes('notes_page_size=5')));
  assert.equal(requests.filter((r) => r.url.startsWith('/api/merchants?')).length, 1);
  assert.equal(stderr, '');
});

test('missing key exits nonzero, prints only a sanitized error on stderr', async () => {
  await assert.rejects(promisify(execFile)(process.execPath, [entry], {
    env: { PATH: process.env.PATH }, timeout: 10000,
  }), (error) => {
    assert.equal(error.code, 1);
    assert.equal(error.stdout, '');
    assert.match(error.stderr, /MERCHANTS_API_KEY/);
    return true;
  });
});
