'use client';

import Link from 'next/link';
import { FormEvent, useEffect, useState } from 'react';
import { getNotes, Note } from '../lib/api';

function Stat({ label, value }: { label: string; value: number }) {
  return <span>{label} {value.toLocaleString()}</span>;
}

export default function HomePage() {
  const [notes, setNotes] = useState<Note[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState('');
  const [blogger, setBlogger] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  async function load(event?: FormEvent) {
    event?.preventDefault(); setLoading(true); setError('');
    try { const result = await getNotes(keyword, blogger); setNotes(result.items); setTotal(result.total); }
    catch (err) { setError(err instanceof Error ? err.message : '读取帖子失败'); }
    finally { setLoading(false); }
  }

  useEffect(() => { void load(); }, []);

  return (
    <>
      <section className="hero">
        <div><p className="eyebrow">PERSONAL READER</p><h1>把抓到的内容，<em>安静地读完。</em></h1>
          <p className="intro">这里展示已经保存到 PostgreSQL 的小红书笔记。用关键词或博主快速找到想看的内容。</p></div>
        <Link className="secondary-button" href="/settings">管理阅读配置 →</Link>
      </section>
      <form className="searchbar" onSubmit={load}>
        <input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="搜索标题、正文或关键词" />
        <input value={blogger} onChange={(e) => setBlogger(e.target.value)} placeholder="博主 ID 或昵称" />
        <button type="submit">搜索</button>
      </form>
      <div className="result-line"><span>{loading ? '正在读取…' : `共 ${total} 篇笔记`}</span>{error && <span className="error">{error}</span>}</div>
      {loading ? <div className="empty">正在加载内容…</div> : notes.length === 0 ? <div className="empty"><strong>还没有找到笔记</strong><span>先运行爬虫，或调整搜索条件。</span></div> :
        <section className="grid">{notes.map((note) => <article className="card" key={note.note_id}>
          {note.cover_url ? <img src={note.cover_url} alt="" className="cover" loading="lazy" /> : <div className="cover placeholder">◈</div>}
          <div className="card-body"><div className="card-meta"><span>{note.nickname || note.user_id || '未知博主'}</span><span>{note.type === 'video' ? '视频' : '图文'}</span></div>
            <h2>{note.title || '无标题笔记'}</h2><p>{note.description || '暂无正文摘要'}</p>
            <div className="stats"><Stat label="赞" value={note.liked_count || 0} /><Stat label="藏" value={note.collected_count || 0} /><Stat label="评" value={note.comment_count || 0} /></div>
            <a className="read-link" href={note.note_url || `https://www.xiaohongshu.com/explore/${note.note_id}`} target="_blank" rel="noreferrer">打开原笔记 ↗</a>
          </div>
        </article>)}</section>}
    </>
  );
}
