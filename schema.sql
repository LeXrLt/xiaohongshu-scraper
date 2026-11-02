-- 爬虫与阅读器共用的 PostgreSQL 表结构；初始化与迁移均可重复执行。

CREATE TABLE IF NOT EXISTS xhs_notes (
    note_id TEXT PRIMARY KEY,
    user_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    type TEXT DEFAULT '',
    liked_count INTEGER DEFAULT 0,
    collected_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    share_count INTEGER DEFAULT 0,
    ip_location TEXT DEFAULT '',
    topics JSONB DEFAULT '[]',
    published_at TEXT DEFAULT '',
    xsec_token TEXT DEFAULT '',
    xsec_source TEXT DEFAULT '',
    -- 视频相关
    video_url TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    video_duration INTEGER DEFAULT 0,
    video_transcript TEXT DEFAULT '',
    video_ocr_text TEXT DEFAULT '',
    video_summary TEXT DEFAULT '',
    -- 图片相关
    image_ocr_text TEXT DEFAULT '',
    image_summary TEXT DEFAULT '',
    image_mermaid TEXT DEFAULT '',
    image_urls JSONB DEFAULT '[]',
    -- v2 新增：从 raw_data 提取的增强字段
    content TEXT DEFAULT '',                -- 正文（从 raw_data.desc 提取，比 description 更完整）
    note_url TEXT DEFAULT '',               -- 笔记原始链接
    -- v2 新增：本地文件路径
    media_path TEXT DEFAULT '',             -- 本地媒体目录相对路径（data/media/<博主>/<笔记>/）
    has_local_media BOOLEAN DEFAULT FALSE, -- 是否有本地下载的图片/视频
    -- 通用
    content_hash TEXT DEFAULT '',
    raw_data JSONB DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'ready',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    crawled_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS xhs_users (
    user_id TEXT PRIMARY KEY,
    nickname TEXT DEFAULT '',
    avatar TEXT DEFAULT '',
    description TEXT DEFAULT '',
    fans_count INTEGER DEFAULT 0,
    follow_count INTEGER DEFAULT 0,
    notes_count INTEGER DEFAULT 0,
    location TEXT DEFAULT '',
    raw_data JSONB DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'ready',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    crawled_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS xhs_comments (
    comment_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL DEFAULT '',
    parent_id TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    content TEXT DEFAULT '',
    like_count INTEGER DEFAULT 0,
    ip_location TEXT DEFAULT '',
    pictures JSONB DEFAULT '[]',
    target_comment_id TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    raw_data JSONB DEFAULT '{}',
    status VARCHAR(50) DEFAULT 'ready',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    crawled_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS xhs_search_cache (
    keyword TEXT NOT NULL,
    page INTEGER NOT NULL,
    note_ids JSONB DEFAULT '[]',
    crawled_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (keyword, page)
);

CREATE TABLE IF NOT EXISTS xhs_crawl_state (
    task_id TEXT PRIMARY KEY,
    task_type TEXT DEFAULT '',
    target_id TEXT DEFAULT '',
    cursor TEXT DEFAULT '',
    status TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_xhs_notes_user ON xhs_notes(user_id);
CREATE INDEX IF NOT EXISTS idx_xhs_notes_crawled ON xhs_notes(crawled_at);
CREATE INDEX IF NOT EXISTS idx_xhs_comments_note ON xhs_comments(note_id);
CREATE INDEX IF NOT EXISTS idx_xhs_comments_parent ON xhs_comments(parent_id);

-- 商户索引：商户本身不保存门店地址，地点只作为笔记抽取证据的辅助字段。
CREATE TABLE IF NOT EXISTS merchant_categories (
    category_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    sort_order INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deprecated')),
    taxonomy_version TEXT NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merchant_tags (
    tag_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    taxonomy_version TEXT NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    cuisine JSONB NOT NULL DEFAULT '[]'::jsonb,
    price_min INTEGER,
    price_max INTEGER,
    currency TEXT NOT NULL DEFAULT '',
    price_basis TEXT NOT NULL DEFAULT 'unknown',
    reservation_info TEXT NOT NULL DEFAULT '',
    confidence REAL,
    first_seen_at TEXT,
    last_seen_at TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merchant_category_links (
    merchant_id TEXT NOT NULL REFERENCES merchants(merchant_id),
    category_id TEXT NOT NULL REFERENCES merchant_categories(category_id),
    source_note_id TEXT NOT NULL DEFAULT '',
    evidence_text TEXT NOT NULL DEFAULT '',
    confidence REAL,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    taxonomy_version TEXT NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (merchant_id, category_id, source_note_id)
);

CREATE TABLE IF NOT EXISTS merchant_tag_links (
    merchant_id TEXT NOT NULL REFERENCES merchants(merchant_id),
    tag_id TEXT NOT NULL REFERENCES merchant_tags(tag_id),
    source_note_id TEXT NOT NULL DEFAULT '',
    evidence_text TEXT NOT NULL DEFAULT '',
    confidence REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (merchant_id, tag_id, source_note_id)
);

CREATE TABLE IF NOT EXISTS merchant_notes (
    merchant_id TEXT NOT NULL REFERENCES merchants(merchant_id),
    note_id TEXT NOT NULL REFERENCES xhs_notes(note_id),
    relation_type TEXT NOT NULL,
    evidence_text TEXT NOT NULL DEFAULT '',
    evidence_start INTEGER,
    evidence_end INTEGER,
    confidence REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (merchant_id, note_id, relation_type)
);

CREATE TABLE IF NOT EXISTS merchant_extraction_candidates (
    candidate_id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES xhs_notes(note_id),
    source_content_hash TEXT NOT NULL DEFAULT '',
    candidate_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    category_candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
    tag_candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    price JSONB NOT NULL DEFAULT '{}'::jsonb,
    reservation_info TEXT NOT NULL DEFAULT '',
    relation_type TEXT NOT NULL DEFAULT 'mentioned',
    evidence_text TEXT NOT NULL DEFAULT '',
    confidence REAL,
    model_name TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    extractor_version TEXT NOT NULL DEFAULT 'v1',
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (note_id, source_content_hash, extractor_version, candidate_name)
);

-- 兼容已存在的 PostgreSQL 商户候选表。
ALTER TABLE merchant_extraction_candidates
    ADD COLUMN IF NOT EXISTS tag_candidates JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE merchant_extraction_candidates
    ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ;

-- 完成标记独立于候选：没有商户的笔记也必须记录进度。
CREATE TABLE IF NOT EXISTS merchant_extraction_state (
    note_id TEXT PRIMARY KEY REFERENCES xhs_notes(note_id),
    source_content_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    error_message TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_merchants_name ON merchants(canonical_name);
CREATE INDEX IF NOT EXISTS idx_merchants_city_district ON merchants(city, district);
CREATE INDEX IF NOT EXISTS idx_merchant_categories_parent ON merchant_categories(parent_id);
CREATE INDEX IF NOT EXISTS idx_merchant_category_links_category ON merchant_category_links(category_id);
CREATE INDEX IF NOT EXISTS idx_merchant_tags_group ON merchant_tags(group_name);
CREATE INDEX IF NOT EXISTS idx_merchant_tag_links_tag ON merchant_tag_links(tag_id);
CREATE INDEX IF NOT EXISTS idx_merchant_notes_note ON merchant_notes(note_id);
CREATE INDEX IF NOT EXISTS idx_merchant_candidates_note ON merchant_extraction_candidates(note_id);

-- Ticketmaster SG 演唱会：页面本身已经提供结构化演出和艺人字段，不经过商户索引。
-- 主表按活动页保存艺人/演唱会信息；场次表按 Ticketmaster 的 event id 保存日期和购票链接。
CREATE TABLE IF NOT EXISTS ticketmaster_concerts (
    concert_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'ticketmaster.sg',
    title TEXT NOT NULL DEFAULT '',
    artist_name TEXT NOT NULL DEFAULT '',
    artist_id TEXT NOT NULL DEFAULT '',
    promoter TEXT NOT NULL DEFAULT '',
    category_name TEXT NOT NULL DEFAULT 'Concerts',
    venue_name TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT 'Singapore',
    country TEXT NOT NULL DEFAULT 'SG',
    event_url TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    raw_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended', 'cancelled', 'unknown')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ticketmaster_concert_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    concert_id TEXT NOT NULL REFERENCES ticketmaster_concerts(concert_id) ON DELETE CASCADE,
    source_event_id TEXT NOT NULL DEFAULT '',
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ,
    display_date TEXT NOT NULL DEFAULT '',
    venue_name TEXT NOT NULL DEFAULT '',
    ticket_url TEXT NOT NULL DEFAULT '',
    ticket_status TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    raw_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'upcoming' CHECK (status IN ('upcoming', 'started', 'cancelled', 'unknown')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (concert_id, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_ticketmaster_concerts_artist
    ON ticketmaster_concerts(artist_name);
CREATE INDEX IF NOT EXISTS idx_ticketmaster_concerts_status
    ON ticketmaster_concerts(status);
CREATE INDEX IF NOT EXISTS idx_ticketmaster_occurrences_start
    ON ticketmaster_concert_occurrences(start_at);
CREATE INDEX IF NOT EXISTS idx_ticketmaster_occurrences_upcoming
    ON ticketmaster_concert_occurrences(status, start_at);
CREATE INDEX IF NOT EXISTS idx_ticketmaster_occurrences_concert
    ON ticketmaster_concert_occurrences(concert_id);

-- 阅读器配置：单实例设置，关键词/博主用 JSON 数组保存，cookie 由 API 统一读取。
CREATE TABLE IF NOT EXISTS reader_config (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
    bloggers JSONB NOT NULL DEFAULT '[]'::jsonb,
    cookies TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO reader_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- v3: 对齐 substack 模板，补齐 status / updated_at（幂等迁移已存在的旧表）
ALTER TABLE xhs_notes    ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'ready';
ALTER TABLE xhs_notes    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE xhs_users    ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'ready';
ALTER TABLE xhs_users    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE xhs_comments ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'ready';
ALTER TABLE xhs_comments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- 移除旧版商户核实状态，重复执行时跳过已删除的字段。
ALTER TABLE merchants DROP COLUMN IF EXISTS status;

-- 推送记录表：通用多态记录库中商户、演唱会等实体的推送状态。
-- 规则：若表中没有某个实体的记录（或当前渠道下无记录），说明该实体尚未推送过。
CREATE TABLE IF NOT EXISTS push_records (
    record_id BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,                  -- 'merchant', 'ticketmaster_concert', 'ticketmaster_occurrence' 等
    entity_id TEXT NOT NULL,                    -- 实体主键 (merchant_id, concert_id 等)
    channel TEXT NOT NULL DEFAULT 'default',    -- 推送渠道标识，如 'default', 'feishu', 'wechat_work', 'webhook'
    push_status TEXT NOT NULL DEFAULT 'pending' CHECK (push_status IN ('pending', 'success', 'failed', 'skipped')),
    content_hash TEXT DEFAULT '',               -- 推送时实体的内容哈希（用于检测内容更新）
    entity_updated_at TIMESTAMPTZ,              -- 对应实体自身的更新时间
    last_pushed_at TIMESTAMPTZ,                 -- 最近一次成功推送时间
    retry_count INTEGER NOT NULL DEFAULT 0,     -- 推送/重试次数
    error_message TEXT DEFAULT '',              -- 异常/失败信息
    payload_snapshot JSONB DEFAULT '{}'::jsonb, -- 推送内容摘要/快照
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_type, entity_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_push_records_lookup
    ON push_records (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_push_records_status
    ON push_records (channel, push_status);
