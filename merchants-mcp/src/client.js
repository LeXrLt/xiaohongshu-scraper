// Replace this with the production origin before publishing for key-only setup.
export const DEFAULT_API_URL = 'http://localhost:8000';

export class ApiError extends Error {}

export class MerchantsClient {
  constructor({ apiKey, baseUrl = DEFAULT_API_URL, timeoutMs = 15_000,
    fetchImpl = globalThis.fetch, now = Date.now } = {}) {
    if (!apiKey?.trim()) throw new ApiError('请配置 MERCHANTS_API_KEY（后端 .env 中的 API_AUTH_KEY）。');
    let url;
    try { url = new URL(baseUrl); } catch { throw new ApiError('MERCHANTS_API_URL 必须是有效的 HTTP(S) 地址。'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new ApiError('MERCHANTS_API_URL 必须是 HTTP(S) 地址，且不含账号、密码、查询参数或片段。');
    }
    if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 120_000) {
      throw new ApiError('MERCHANTS_API_TIMEOUT_MS 必须是 100 到 120000 之间的整数。');
    }
    this.apiKey = apiKey.trim();
    this.baseUrl = url.href.replace(/\/$/, '');
    this.timeoutMs = timeoutMs;
    this.fetch = fetchImpl;
    this.now = now;
    this.token = undefined;
    this.pendingToken = undefined;
  }

  async request(path, init) {
    try {
      return await this.fetch(`${this.baseUrl}${path}`, {
        ...init, cache: 'no-store', redirect: 'error',
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch {
      // Never surface fetch errors, response bodies or credentials to the agent/log.
      throw new ApiError('无法连接商户 API（连接失败、请求超时或被重定向），请检查 MERCHANTS_API_URL 和后端服务。');
    }
  }

  checkStatus(response) {
    if (response.ok) return;
    const messages = {
      401: '鉴权失败，请检查 MERCHANTS_API_KEY 是否有效，或密钥是否已轮换。',
      403: '当前密钥无权访问此资源。',
      404: '未找到商户或接口，请确认商户 ID 及后端版本。',
      422: '查询参数不合法，请检查分类、标签及分页参数。',
      429: '商户 API 请求过于频繁，请稍后重试。',
    };
    throw new ApiError(`商户 API HTTP ${response.status}：${messages[response.status] || '服务暂时不可用，请稍后重试。'}`);
  }

  async readJson(response) {
    this.checkStatus(response);
    try { return await response.json(); } catch { throw new ApiError('商户 API 返回了无效的 JSON。'); }
  }

  async getToken() {
    if (this.token && this.now() < this.token.expiresAt) return this.token;
    if (!this.pendingToken) {
      this.pendingToken = (async () => {
        const startedAt = this.now();
        const response = await this.request('/api/auth/token', {
          method: 'POST', headers: { 'X-API-Key': this.apiKey },
        });
        const data = await this.readJson(response);
        if (!data || typeof data.access_token !== 'string' || !data.access_token
          || data.token_type !== 'bearer' || !Number.isFinite(data.expires_in) || data.expires_in <= 0) {
          throw new ApiError('商户 API 返回了无效的 token 响应。');
        }
        this.token = {
          value: data.access_token,
          expiresAt: startedAt + data.expires_in * 1000 - Math.min(30_000, data.expires_in * 100),
        };
        return this.token;
      })().finally(() => { this.pendingToken = undefined; });
    }
    return this.pendingToken;
  }

  async get(path, params = {}) {
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(params)) {
      for (const entry of Array.isArray(value) ? value : [value]) {
        if (entry !== undefined && entry !== null && entry !== '') query.append(name, String(entry));
      }
    }
    const target = `${path}${query.size ? `?${query}` : ''}`;
    const send = (token) => this.request(target, {
      method: 'GET', headers: { Authorization: `Bearer ${token.value}` },
    });
    const token = await this.getToken();
    let response = await send(token);
    if (response.status === 401) {
      if (this.token === token) this.token = undefined;
      response = await send(await this.getToken());
    }
    return this.readJson(response);
  }

  categories(params) { return this.get('/api/merchant-categories', params); }
  tags(params) { return this.get('/api/merchant-tags', params); }
  search(params) { return this.get('/api/merchants', params); }
  detail(id, params) { return this.get(`/api/merchants/${encodeURIComponent(id)}`, params); }
}
