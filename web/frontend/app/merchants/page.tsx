'use client';

import Link from 'next/link';
import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react';
import { getMerchant, getMerchants, Merchant } from '../../lib/api';

const PAGE_SIZE = 24;

function formatDate(value?: string | null) {
  if (!value) return '更新时间未知';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}

function formatPrice(merchant: Merchant) {
  if (merchant.price_min == null && merchant.price_max == null) return '价格未知';
  const currency = merchant.currency ? `${merchant.currency} ` : '';
  if (merchant.price_min != null && merchant.price_max != null && merchant.price_min !== merchant.price_max) {
    return `${currency}${merchant.price_min}–${merchant.price_max}`;
  }
  return `${currency}${merchant.price_min ?? merchant.price_max}`;
}

function listValue(value?: string[]) {
  return (value || []).filter(Boolean);
}

export default function MerchantsPage() {
  const [merchants, setMerchants] = useState<Merchant[]>([]);
  const [total, setTotal] = useState(0);
  const [keyword, setKeyword] = useState('');
  const [city, setCity] = useState('');
  const [district, setDistrict] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<Merchant | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const requestId = useRef(0);
  const detailRequestId = useRef(0);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const inFlight = useRef(false);
  const activeFilters = useRef({ keyword: '', city: '', district: '' });

  const loadPage = useCallback(async (pageToLoad: number, replace: boolean, filters: typeof activeFilters.current, id: number) => {
    inFlight.current = true;
    if (replace) setLoading(true); else setLoadingMore(true);
    setError('');
    try {
      const result = await getMerchants(filters.keyword, filters.city, filters.district, pageToLoad, PAGE_SIZE);
      if (id !== requestId.current) return;
      setMerchants((current) => {
        const known = new Set(replace ? [] : current.map((merchant) => merchant.merchant_id));
        const incoming = result.items.filter((merchant) => {
          if (known.has(merchant.merchant_id)) return false;
          known.add(merchant.merchant_id);
          return true;
        });
        return replace ? incoming : [...current, ...incoming];
      });
      setTotal(result.total);
      setPage(pageToLoad);
      setHasMore(pageToLoad * result.page_size < result.total && result.items.length > 0);
    } catch (err) {
      if (id === requestId.current) setError(err instanceof Error ? err.message : '读取商户失败');
    } finally {
      if (id === requestId.current) {
        inFlight.current = false;
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  const load = useCallback((activeKeyword: string, activeCity: string, activeDistrict: string) => {
    const id = ++requestId.current;
    const filters = { keyword: activeKeyword, city: activeCity, district: activeDistrict };
    activeFilters.current = filters;
    setMerchants([]);
    setTotal(0);
    setPage(0);
    setHasMore(true);
    setLoadingMore(false);
    void loadPage(1, true, filters, id);
  }, [loadPage]);

  const loadNextPage = useCallback(() => {
    if (inFlight.current || loading || loadingMore || !hasMore || page === 0) return;
    void loadPage(page + 1, false, activeFilters.current, requestId.current);
  }, [hasMore, loadPage, loading, loadingMore, page]);

  useEffect(() => { void load('', '', ''); }, [load]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasMore || error || selected) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadNextPage();
    }, { rootMargin: '500px 0px' });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [error, hasMore, loadNextPage, selected]);

  useEffect(() => {
    if (!selected) return;
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setSelected(null);
    };
    document.addEventListener('keydown', closeOnEscape);
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', closeOnEscape);
      document.body.style.overflow = '';
    };
  }, [selected]);

  function submit(event: FormEvent) {
    event.preventDefault();
    void load(keyword, city, district);
  }

  function openMerchant(merchant: Merchant) {
    const id = ++detailRequestId.current;
    setSelected(merchant);
    setDetailLoading(true);
    setDetailError('');
    void getMerchant(merchant.merchant_id).then((detail) => {
      if (id === detailRequestId.current) setSelected(detail);
    }).catch((err) => {
      if (id === detailRequestId.current) setDetailError(err instanceof Error ? err.message : '读取商户详情失败');
    }).finally(() => {
      if (id === detailRequestId.current) setDetailLoading(false);
    });
  }

  function handleKeyDown(event: KeyboardEvent<HTMLElement>, merchant: Merchant) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openMerchant(merchant);
    }
  }

  return (
    <>
      <section className="page-heading">
        <div><p className="eyebrow">MERCHANT INDEX</p><h1>找到值得再次选择的商户。</h1>
          <p className="intro">从已抽取的笔记中整理出的商户索引，覆盖餐饮、出行、医疗和休闲服务。列表只展示摘要，点击卡片查看来源和证据。</p></div>
        <Link className="secondary-button" href="/">回到笔记 →</Link>
      </section>
      <form className="searchbar merchant-search" onSubmit={submit}>
        <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索商户、别名或菜系" />
        <input value={city} onChange={(event) => setCity(event.target.value)} placeholder="城市" />
        <input value={district} onChange={(event) => setDistrict(event.target.value)} placeholder="区域" />
        <button type="submit">搜索</button>
      </form>
      <div className="result-line"><span>{loading ? '正在读取…' : `共 ${total} 家商户`}</span>{error && <span className="error">{error}</span>}</div>
      {loading ? <div className="empty">正在加载商户…</div> : merchants.length === 0 ?
        <div className="empty"><strong>{error ? '商户加载失败' : '没有找到商户'}</strong>
          {error ? <button className="merchant-retry" type="button" onClick={() => load(activeFilters.current.keyword, activeFilters.current.city, activeFilters.current.district)}>重新加载</button> :
            <span>试试调整搜索条件，或稍后再来看看。</span>}</div> :
        <>
        <section className="merchant-grid">
          {merchants.map((merchant) => {
            const categories = (merchant.categories || []).map((category) => category.name).filter(Boolean);
            const tags = (merchant.tags || []).map((tag) => tag.name).filter(Boolean);
            const cuisine = listValue(merchant.cuisine);
            return <article className="merchant-card" key={merchant.merchant_id} role="button" tabIndex={0} onClick={() => openMerchant(merchant)} onKeyDown={(event) => handleKeyDown(event, merchant)}>
              <span className="merchant-mark">商</span>
              <h2>{merchant.canonical_name || '未命名商户'}</h2>
              <p className="merchant-place">{[merchant.city, merchant.district].filter(Boolean).join(' · ') || '地点未知'}</p>
              <div className="merchant-tags">{[...categories, ...tags, ...cuisine].slice(0, 3).map((tag) => <span key={tag}>{tag}</span>)}</div>
              <div className="merchant-summary"><span>{formatPrice(merchant)}</span><span>{merchant.note_count || 0} 篇来源</span></div>
              <div className="merchant-card-footer"><span>更新于 {formatDate(merchant.updated_at)}</span><span className="detail-hint">查看详情 ↗</span></div>
            </article>;
          })}
        </section>
        <div ref={sentinelRef} className="load-more" role="status" aria-live="polite">
          {loadingMore ? '正在加载更多商户…' : error ?
            <button className="merchant-retry" type="button" onClick={loadNextPage}>加载失败，点击重试</button> :
            hasMore ? '向下滚动，发现更多商户' : '已经看到全部商户'}
        </div>
        </>}
      {selected && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelected(null); }}>
        <article className="merchant-modal" role="dialog" aria-modal="true" aria-labelledby="merchant-modal-title">
          <button className="modal-close" type="button" aria-label="关闭" onClick={() => setSelected(null)}>×</button>
          {detailLoading ? <div className="modal-state">正在读取商户详情…</div> : detailError ? <div className="modal-state error">{detailError}</div> : <div className="merchant-detail-body">
            <p className="eyebrow">MERCHANT DETAIL</p>
            <div className="merchant-detail-heading"><h2 id="merchant-modal-title">{selected.canonical_name || '未命名商户'}</h2><p className="merchant-place">{[selected.city, selected.district].filter(Boolean).join(' · ') || '地点未知'}</p></div>
            <div className="merchant-detail-grid"><div><small>价格</small><strong>{formatPrice(selected)}</strong></div><div><small>来源笔记</small><strong>{selected.note_count || selected.notes?.length || 0} 篇</strong></div><div><small>置信度</small><strong>{selected.confidence == null ? '未知' : `${Math.round(selected.confidence * 100)}%`}</strong></div></div>
            {listValue(selected.aliases).length > 0 && <div className="detail-section"><small>别名</small><p>{listValue(selected.aliases).join('、')}</p></div>}
            {listValue(selected.cuisine).length > 0 && <div className="detail-section"><small>经营类型</small><p>{listValue(selected.cuisine).join('、')}</p></div>}
            {selected.reservation_info && <div className="detail-section"><small>预约信息</small><p>{selected.reservation_info}</p></div>}
            <div className="detail-section"><small>分类</small><div className="merchant-tags">{(selected.categories || []).length ? selected.categories?.map((category) => <span key={category.category_id}>{category.name}</span>) : <span>暂无分类</span>}</div></div>
            <div className="detail-section"><small>标签</small><div className="merchant-tags">{(selected.tags || []).length ? selected.tags?.map((tag) => <span key={tag.tag_id}>{tag.name}</span>) : <span>暂无标签</span>}</div></div>
            <div className="detail-section"><small>来源笔记与证据</small>{(selected.notes || []).length ? <div className="merchant-notes">{selected.notes?.map((note) => <div className="merchant-note" key={`${note.note_id}-${note.relation_type}`}><div><strong>{note.title || '无标题笔记'}</strong><span>{note.relation_type}</span></div><p>{note.evidence_text || note.content?.slice(0, 140) || '暂无证据摘要'}</p><a href={note.note_url || `https://www.xiaohongshu.com/explore/${note.note_id}`} target="_blank" rel="noreferrer">打开原笔记 ↗</a></div>)}</div> : <p>暂无来源笔记</p>}</div>
          </div>}
        </article>
      </div>}
    </>
  );
}
