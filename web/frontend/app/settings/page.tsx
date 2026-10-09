'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { getSettings, saveSettings } from '../../lib/api';

const initial = { keywords: '', bloggers: '', cookies: '' };

export default function SettingsPage() {
  const [form, setForm] = useState(initial);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  useEffect(() => { getSettings().then((value) => setForm({ keywords: value.keywords.join('\n'), bloggers: value.bloggers.join('\n'), cookies: value.cookies })).catch((err) => setError(err instanceof Error ? err.message : '读取设置失败')); }, []);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setStatus('保存中…'); setError('');
    try { await saveSettings({ keywords: form.keywords.split(/[,\n]/).map((x) => x.trim()).filter(Boolean), bloggers: form.bloggers.split(/[,\n]/).map((x) => x.trim()).filter(Boolean), cookies: form.cookies }); setStatus('已保存'); }
    catch (err) { setStatus(''); setError(err instanceof Error ? err.message : '保存失败'); }
  }
  return <section className="settings"><Link href="/" className="back">← 返回阅读</Link><p className="eyebrow">READER SETTINGS</p><h1>阅读配置</h1><p className="intro">每行一个值，也支持用逗号分隔。配置保存到 PostgreSQL，供 Python 后端读取。</p>
    <form onSubmit={submit} className="settings-form"><label>关键词 <span>用于后续抓取与筛选</span><textarea value={form.keywords} onChange={(e) => setForm({ ...form, keywords: e.target.value })} placeholder="旅行攻略\n咖啡店" /></label>
      <label>博主 <span>填写博主 ID 或主页标识</span><textarea value={form.bloggers} onChange={(e) => setForm({ ...form, bloggers: e.target.value })} placeholder="5f..." /></label>
      <label>Cookies <span>用于爬虫登录，请妥善保管</span><textarea className="cookies" value={form.cookies} onChange={(e) => setForm({ ...form, cookies: e.target.value })} placeholder="a1=...; web_session=..." /></label>
      <div className="form-actions"><button type="submit">保存设置</button>{status && <span className="success">{status}</span>}{error && <span className="error">{error}</span>}</div>
    </form>
  </section>;
}
