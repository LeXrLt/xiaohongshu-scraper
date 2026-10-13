#!/usr/bin/env node
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';
import { ApiError, MerchantsClient } from './client.js';
import { createServer } from './server.js';

try {
  const client = new MerchantsClient({
    apiKey: process.env.MERCHANTS_API_KEY,
    baseUrl: process.env.MERCHANTS_API_URL || undefined,
    timeoutMs: process.env.MERCHANTS_API_TIMEOUT_MS === undefined
      ? undefined : Number(process.env.MERCHANTS_API_TIMEOUT_MS),
  });
  const server = createServer(client);
  await server.connect(new StdioServerTransport());
} catch (error) {
  // stdout belongs exclusively to the MCP JSON-RPC protocol.
  console.error(error instanceof ApiError ? error.message : 'merchants-mcp 启动失败。');
  process.exitCode = 1;
}
