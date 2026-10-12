'use client';

import Link from 'next/link';
import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react';
import { getNote, getNotes, Note } from '../lib/api';

const PAGE_SIZE = 24;

function formatDate(value: string) {
  if (!value) return '发布日期未知';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(date);
}

function Stat({ label, value }: { label: string; value: number }) {
  return <span>{label} {value.toLocaleString()}</span>;
}

export default function HomePage() {
  const [notes, setNotes] = useState<Note[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState('');
  const [blogger, setBlogger] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState('');
  const [selectedNote, setSelectedNote] = useState<Note | null>(null);
  const [modalLoading, setModalLoading] = useState(false);
  const [modalError, setModalError] = useState('');
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const requestIdRef = useRef(0);
  const detailRequestIdRef = useRef(0);

  const loadPage = useCallback(async (pageToLoad: number, replace: boolean, activeKeyword: string, activeBlogger: string, requestId: number) => {
    if (replace) setLoading(true); else setLoadingMore(true);
    setError('');
    try {
      const result = await getNotes(activeKeyword, activeBlogger, pageToLoad, PAGE_SIZE);
      if (requestId !== requestIdRef.current) return;
      setNotes((current) => {
        if (replace) return result.items;
        const known = new Set(current.map((note) => note.note_id));
        return [...current, ...result.items.filter((note) => !known.has(note.note_id))];
      });
      setTotal(result.total);
      setPage(pageToLoad);
      setHasMore(pageToLoad * PAGE_SIZE < result.total && result.items.length > 0);
    } catch (err) {
      if (requestId === requestIdRef.current) setError(err instanceof Error ? err.message : '读取帖子失败');
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  const loadFirstPage = useCallback(() => {
    const requestId = ++requestIdRef.current;
    setNotes([]); setTotal(0); setPage(0); setHasMore(true);
    void loadPage(1, true, keyword, blogger, requestId);
  }, [blogger, keyword, loadPage]);

  function load(event: FormEvent) {
    event.preventDefault();
    loadFirstPage();
  }

  const loadNextPage = useCallback(() => {
    if (loading || loadingMore || !hasMore || page === 0) return;
    void loadPage(page + 1, false, keyword, blogger, requestIdRef.current);
  }, [blogger, hasMore, keyword, loadPage, loading, loadingMore, page]);

  function openNote(note: Note) {
    const detailRequestId = ++detailRequestIdRef.current;
    setSelectedNote(note);
    setModalLoading(true);
    setModalError('');
    void getNote(note.note_id).then((detail) => {
      if (detailRequestId === detailRequestIdRef.current) setSelectedNote(detail);
    }).catch((err) => {
      if (detailRequestId === detailRequestIdRef.current) setModalError(err instanceof Error ? err.message : '读取笔记详情失败');
    }).finally(() => {
      if (detailRequestId === detailRequestIdRef.current) setModalLoading(false);
    });
  }

  function handleCardKeyDown(event: KeyboardEvent<HTMLElement>, note: Note) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openNote(note);
    }
  }

  useEffect(() => {
    const requestId = ++requestIdRef.current;
    void loadPage(1, true, '', '', requestId);
  }, [loadPage]);

  useEffect(() => {
    if (!selectedNote) return;
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedNote(null);
    };
    document.addEventListener('keydown', closeOnEscape);
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', closeOnEscape);
      document.body.style.overflow = '';
    };
  }, [selectedNote]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasMore) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) loadNextPage();
    }, { rootMargin: '500px 0px' });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loadNextPage]);

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
        <>
        <section className="grid">{notes.map((note) => <article className="card" key={note.note_id} role="button" tabIndex={0} onClick={() => openNote(note)} onKeyDown={(event) => handleCardKeyDown(event, note)}>
          {note.cover_url ? <img src={note.cover_url} alt="" className="cover" loading="lazy" /> : <div className="cover placeholder">◈</div>}
          <div className="card-body"><div className="card-meta"><span>{note.nickname || note.user_id || '未知博主'}</span><span>{note.type === 'video' ? '视频' : '图文'}</span></div>
            <h2>{note.title || '无标题笔记'}</h2><p>{note.description || '暂无正文摘要'}</p>
            <time className="published-date" dateTime={note.published_at}>{formatDate(note.published_at)}</time>
            <div className="stats"><Stat label="赞" value={note.liked_count || 0} /><Stat label="藏" value={note.collected_count || 0} /><Stat label="评" value={note.comment_count || 0} /></div>
            <a className="read-link" href={note.note_url || `https://www.xiaohongshu.com/explore/${note.note_id}`} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>打开原笔记 ↗</a>
          </div>
        </article>)}</section>
        <div ref={sentinelRef} className="load-more" aria-live="polite">{loadingMore ? '正在加载更多…' : hasMore ? '向下滚动加载更多' : '已经看到全部笔记'}</div>
        </>}
      {selectedNote && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelectedNote(null); }}>
        <article className="note-modal" role="dialog" aria-modal="true" aria-labelledby="note-modal-title">
          <button className="modal-close" type="button" aria-label="关闭" onClick={() => setSelectedNote(null)}>×</button>
          {modalLoading ? <div className="modal-state">正在读取完整内容…</div> : modalError ? <div className="modal-state error">{modalError}</div> : <>
            <div className="modal-media">
              {(selectedNote.image_urls?.length ? selectedNote.image_urls : selectedNote.cover_url ? [selectedNote.cover_url] : []).map((url, index) => <img key={`${url}-${index}`} src={url} alt="" loading={index === 0 ? 'eager' : 'lazy'} />)}
            </div>
            <div className="modal-body"><div className="card-meta"><span>{selectedNote.nickname || selectedNote.user_id || '未知博主'}</span><time dateTime={selectedNote.published_at}>{formatDate(selectedNote.published_at)}</time></div>
              <h2 id="note-modal-title">{selectedNote.title || '无标题笔记'}</h2><p className="modal-content">{selectedNote.content || selectedNote.description || '暂无正文'}</p>
              <div className="stats"><Stat label="赞" value={selectedNote.liked_count || 0} /><Stat label="藏" value={selectedNote.collected_count || 0} /><Stat label="评" value={selectedNote.comment_count || 0} /></div>
              <a className="read-link" href={selectedNote.note_url || `https://www.xiaohongshu.com/explore/${selectedNote.note_id}`} target="_blank" rel="noreferrer">打开原笔记 ↗</a>
            </div>
          </>}
        </article>
      </div>}
    </>
  );
}
