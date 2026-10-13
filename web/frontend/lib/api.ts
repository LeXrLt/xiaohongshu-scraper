// 通过 Next.js 同源反代访问 Python API，避免浏览器跨域请求。
const API_URL = (process.env.NEXT_PUBLIC_API_URL || '').replace(/\/$/, '');
const API_KEY = process.env.NEXT_PUBLIC_API_AUTH_KEY || '';

type AccessToken = { value: string; expiresAt: number };
let cachedToken: AccessToken | undefined;
let pendingToken: Promise<AccessToken> | undefined;

async function getAccessToken(): Promise<AccessToken> {
  if (cachedToken && Date.now() < cachedToken.expiresAt) return cachedToken;
  if (!pendingToken) {
    pendingToken = (async () => {
      if (!API_KEY) throw new Error('请在项目根目录 .env 中配置 API_AUTH_KEY 并重启/重新构建前端');
      const startedAt = Date.now();
      const response = await fetch(`${API_URL}/api/auth/token`, {
        method: 'POST', headers: { 'X-API-Key': API_KEY }, cache: 'no-store',
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json() as {
        access_token: string; expires_in: number;
      };
      cachedToken = {
        value: data.access_token,
        // Refresh early; using request start time also accounts for network latency.
        expiresAt: startedAt + Math.max(0, data.expires_in * 1000 - 30_000),
      };
      return cachedToken;
    })().finally(() => { pendingToken = undefined; });
  }
  return pendingToken;
}

async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const send = (token: AccessToken) => {
    const headers = new Headers(init.headers);
    headers.set('Authorization', `Bearer ${token.value}`);
    return fetch(`${API_URL}${path}`, { ...init, headers, cache: 'no-store' });
  };
  const token = await getAccessToken();
  let response = await send(token);
  if (response.status === 401) {
    // A late 401 from an older request must not discard a newly refreshed token.
    if (cachedToken === token) cachedToken = undefined;
    response = await send(await getAccessToken());
  }
  return response;
}

export type Note = {
  note_id: string;
  user_id: string;
  nickname: string;
  title: string;
  description: string;
  type: string;
  liked_count: number;
  collected_count: number;
  comment_count: number;
  share_count: number;
  ip_location: string;
  topics?: string[];
  published_at: string;
  cover_url: string;
  image_urls?: string[];
  note_url: string;
  media_path: string;
  crawled_at?: string;
  content?: string;
  video_url?: string;
  video_transcript?: string;
  video_ocr_text?: string;
  video_summary?: string;
  image_ocr_text?: string;
  image_summary?: string;
  image_mermaid?: string;
  raw_data?: Record<string, unknown>;
};

export type Settings = { keywords: string[]; bloggers: string[]; cookies: string };

export type MerchantCategory = {
  category_id: string;
  name: string;
  description?: string;
  is_primary?: boolean;
  evidence_text?: string;
  confidence?: number | null;
  taxonomy_version?: string;
};

export type MerchantTag = {
  tag_id: string;
  name: string;
  group_name?: string;
  evidence_text?: string;
  confidence?: number | null;
};

export type MerchantNote = {
  note_id: string;
  relation_type: string;
  evidence_text: string;
  confidence?: number | null;
  title: string;
  content: string;
  note_url: string;
  published_at: string;
  crawled_at: string;
};

export type Merchant = {
  merchant_id: string;
  canonical_name: string;
  aliases: string[];
  city: string;
  district: string;
  cuisine: string[];
  price_min: number | null;
  price_max: number | null;
  currency: string;
  price_basis: string;
  reservation_info?: string;
  confidence: number | null;
  first_seen_at?: string | null;
  last_seen_at?: string | null;
  updated_at?: string | null;
  note_count?: number;
  categories?: MerchantCategory[];
  tags?: MerchantTag[];
  notes?: MerchantNote[];
};

export async function getNotes(keyword = '', blogger = '', page = 1, pageSize = 24) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (keyword.trim()) params.set('keyword', keyword.trim());
  if (blogger.trim()) params.set('blogger', blogger.trim());
  const response = await apiFetch(`/api/notes?${params}`);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<{ items: Note[]; total: number }>;
}

export async function getNote(noteId: string) {
  const response = await apiFetch(`/api/notes/${encodeURIComponent(noteId)}`);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<Note>;
}

export async function getSettings() {
  const response = await apiFetch('/api/settings');
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<Settings>;
}

export async function saveSettings(settings: Settings) {
  const response = await apiFetch('/api/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<Settings>;
}

export async function getMerchants(
  keyword = '', city = '', district = '', page = 1, pageSize = 24,
) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (keyword.trim()) params.set('keyword', keyword.trim());
  if (city.trim()) params.set('city', city.trim());
  if (district.trim()) params.set('district', district.trim());
  const response = await apiFetch(`/api/merchants?${params}`);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<{ items: Merchant[]; total: number; page: number; page_size: number }>;
}

export async function getMerchant(merchantId: string) {
  const response = await apiFetch(`/api/merchants/${encodeURIComponent(merchantId)}`);
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<Merchant>;
}
