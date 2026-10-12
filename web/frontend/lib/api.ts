// 通过 Next.js 同源反代访问 Python API，避免浏览器跨域请求。
const API_URL = (process.env.NEXT_PUBLIC_API_URL || '').replace(/\/$/, '');

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

export async function getNotes(keyword = '', blogger = '', page = 1, pageSize = 24) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (keyword.trim()) params.set('keyword', keyword.trim());
  if (blogger.trim()) params.set('blogger', blogger.trim());
  const response = await fetch(`${API_URL}/api/notes?${params}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<{ items: Note[]; total: number }>;
}

export async function getNote(noteId: string) {
  const response = await fetch(`${API_URL}/api/notes/${encodeURIComponent(noteId)}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<Note>;
}

export async function getSettings() {
  const response = await fetch(`${API_URL}/api/settings`, { cache: 'no-store' });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<Settings>;
}

export async function saveSettings(settings: Settings) {
  const response = await fetch(`${API_URL}/api/settings`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<Settings>;
}
