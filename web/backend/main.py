"""HTTP API for the standalone Xiaohongshu reader."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager, closing
from datetime import date
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from auth import get_auth_settings, require_token, router as auth_router

from reader_config import (
    ReaderConfig,
    config_dict,
    get_connection,
    read_config,
    write_config,
)
from database import connect

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    get_auth_settings()  # Refuse to start with missing or invalid authentication config.
    # Docker's init scripts only run for a new PostgreSQL volume.  Run the
    # project's idempotent DDL once at API startup so existing volumes receive
    # newly added tables such as the Ticketmaster tables.
    conn = connect(initialize=True)
    conn.close()
    yield


app = FastAPI(title="XHS Reader API", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)
data_router = APIRouter(dependencies=[Depends(require_token)])
origins = [item.strip() for item in os.getenv("FRONTEND_ORIGIN", "http://localhost:3000").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "PUT", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class SettingsPayload(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    bloggers: list[str] = Field(default_factory=list)
    cookies: str = ""


def _db_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"PostgreSQL unavailable: {exc}")


# UNION (rather than UNION ALL) also terminates if legacy categories contain a cycle.
_CATEGORY_TREE_SQL = """
    WITH RECURSIVE selected_categories AS (
        SELECT category_id FROM merchant_categories WHERE category_id = %s AND status = 'active'
        UNION
        SELECT c.category_id FROM merchant_categories c
        JOIN selected_categories parent ON c.parent_id = parent.category_id
        WHERE c.status = 'active'
    ) SELECT category_id FROM selected_categories
"""


@app.get("/api/health")
def health() -> dict[str, str]:
    try:
        with closing(get_connection()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return {"status": "ok"}
    except Exception as exc:
        raise _db_error(exc) from exc

@data_router.get("/api/settings")
def get_settings() -> dict[str, Any]:
    try:
        with closing(get_connection()) as conn:
            return config_dict(read_config(conn))
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.put("/api/settings")
def update_settings(payload: SettingsPayload) -> dict[str, Any]:
    try:
        with closing(get_connection()) as conn:
            config = write_config(conn, ReaderConfig(**payload.model_dump()))
            return config_dict(config)
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.get("/api/notes")
def list_notes(
    keyword: str | None = Query(default=None, max_length=120),
    blogger: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=24, ge=1, le=100),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if keyword and keyword.strip():
        clauses.append("(n.title ILIKE %s OR n.description ILIKE %s OR n.content ILIKE %s)")
        value = f"%{keyword.strip()}%"
        params.extend([value, value, value])
    if blogger and blogger.strip():
        clauses.append("(n.user_id ILIKE %s OR COALESCE(u.nickname, '') ILIKE %s)")
        value = f"%{blogger.strip()}%"
        params.extend([value, value])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    offset = (page - 1) * page_size
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM xhs_notes n LEFT JOIN xhs_users u ON u.user_id = n.user_id
                {where}
                """, params,
            )
            total = int(cur.fetchone()["total"])
            cur.execute(
                f"""
                SELECT n.note_id, n.user_id, COALESCE(u.nickname, '') AS nickname,
                       n.title, COALESCE(NULLIF(n.content, ''), n.description) AS description,
                       n.type, n.liked_count, n.collected_count, n.comment_count,
                       n.share_count, n.ip_location, n.topics, n.published_at,
                       n.cover_url, n.image_urls, n.note_url, n.media_path,
                       n.crawled_at
                FROM xhs_notes n LEFT JOIN xhs_users u ON u.user_id = n.user_id
                {where}
                ORDER BY NULLIF(n.published_at, '') DESC NULLS LAST,
                         n.crawled_at DESC NULLS LAST,
                         n.note_id DESC
                LIMIT %s OFFSET %s
                """, [*params, page_size, offset],
            )
            notes = [dict(row) for row in cur.fetchall()]
        return {"items": notes, "page": page, "page_size": page_size, "total": total}
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.get("/api/notes/{note_id}")
def get_note(note_id: str) -> dict[str, Any]:
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT n.*, COALESCE(u.nickname, '') AS nickname, u.avatar
                FROM xhs_notes n LEFT JOIN xhs_users u ON u.user_id = n.user_id
                WHERE n.note_id = %s
                """, (note_id,),
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Note not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.get("/api/merchant-categories")
def list_merchant_categories(
    keyword: str | None = Query(default=None, max_length=120),
) -> dict[str, Any]:
    """列出库中启用的分类及其全部子类的商户数，供推荐前定位需求。"""
    clauses = ["c.status = 'active'"]
    params: list[Any] = []
    if keyword and keyword.strip():
        clauses.append("(c.name ILIKE %s OR c.description ILIKE %s OR c.aliases::text ILIKE %s)")
        params.extend([f"%{keyword.strip()}%"] * 3)
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                WITH RECURSIVE category_tree AS (
                    SELECT category_id AS root_id, category_id
                    FROM merchant_categories WHERE status = 'active'
                    UNION
                    SELECT tree.root_id, c.category_id FROM category_tree tree
                    JOIN merchant_categories c ON c.parent_id = tree.category_id
                    WHERE c.status = 'active'
                )
                SELECT c.category_id, c.parent_id, c.name, c.description, c.aliases,
                       c.taxonomy_version, COUNT(DISTINCT links.merchant_id)::int AS merchant_count
                FROM merchant_categories c
                LEFT JOIN category_tree tree ON tree.root_id = c.category_id
                LEFT JOIN merchant_category_links links ON links.category_id = tree.category_id
                WHERE {' AND '.join(clauses)}
                GROUP BY c.category_id ORDER BY c.sort_order, c.category_id
                """, params,
            )
            items = [dict(row) for row in cur.fetchall()]
        return {"items": items, "total": len(items)}
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.get("/api/merchant-tags")
def list_merchant_tags(
    category_id: str | None = Query(default=None, max_length=80),
) -> dict[str, Any]:
    """分类内实际使用的标签；不传分类时返回全部启用标签。"""
    clauses = ["t.status = 'active'"]
    params: list[Any] = []
    if category_id and category_id.strip():
        clauses.append(f"""EXISTS (
            SELECT 1 FROM merchant_category_links c
            WHERE c.merchant_id = links.merchant_id AND c.category_id IN ({_CATEGORY_TREE_SQL})
        )""")
        params.append(category_id.strip())
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT t.tag_id, t.group_name, t.name, t.description,
                       COUNT(DISTINCT links.merchant_id)::int AS merchant_count
                FROM merchant_tags t
                LEFT JOIN merchant_tag_links links ON links.tag_id = t.tag_id
                WHERE {' AND '.join(clauses)}
                GROUP BY t.tag_id ORDER BY t.group_name, t.tag_id
                """, params,
            )
            items = [dict(row) for row in cur.fetchall()]
        return {"items": items, "total": len(items)}
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.get("/api/merchants")
def list_merchants(
    keyword: str | None = Query(default=None, max_length=120),
    city: str | None = Query(default=None, max_length=80),
    district: str | None = Query(default=None, max_length=80),
    category_id: str | None = Query(default=None, max_length=80),
    tag_ids: list[Annotated[str, Field(min_length=1, max_length=80)]] | None = Query(default=None, max_length=10),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=24, ge=1, le=100),
) -> dict[str, Any]:
    """返回商户摘要；详情和来源笔记由 /api/merchants/{id} 单独读取。"""
    clauses: list[str] = []
    params: list[Any] = []
    if keyword and keyword.strip():
        value = f"%{keyword.strip()}%"
        clauses.append("""(
            m.canonical_name ILIKE %s OR m.aliases::text ILIKE %s OR m.cuisine::text ILIKE %s
            OR m.reservation_info ILIKE %s
            OR EXISTS (
                SELECT 1 FROM merchant_tag_links search_link
                JOIN merchant_tags search_tag ON search_tag.tag_id = search_link.tag_id
                WHERE search_link.merchant_id = m.merchant_id AND search_tag.status = 'active'
                  AND (search_tag.name ILIKE %s OR search_tag.description ILIKE %s
                       OR search_link.evidence_text ILIKE %s)
            )
            OR EXISTS (
                SELECT 1 FROM merchant_notes search_note
                WHERE search_note.merchant_id = m.merchant_id AND search_note.evidence_text ILIKE %s
            )
            OR EXISTS (
                SELECT 1 FROM merchant_category_links search_category
                WHERE search_category.merchant_id = m.merchant_id AND search_category.evidence_text ILIKE %s
            )
        )""")
        params.extend([value] * 9)
    if city and city.strip():
        clauses.append("m.city ILIKE %s")
        params.append(f"%{city.strip()}%")
    if district and district.strip():
        clauses.append("m.district ILIKE %s")
        params.append(f"%{district.strip()}%")
    if category_id and category_id.strip():
        clauses.append(f"EXISTS (SELECT 1 FROM merchant_category_links filter_link WHERE filter_link.merchant_id = m.merchant_id AND filter_link.category_id IN ({_CATEGORY_TREE_SQL}))")
        params.append(category_id.strip())
    for tag_id in sorted(set(tag_ids or [])):
        clauses.append("""EXISTS (
            SELECT 1 FROM merchant_tag_links filter_tag
            JOIN merchant_tags active_tag ON active_tag.tag_id = filter_tag.tag_id AND active_tag.status = 'active'
            WHERE filter_tag.merchant_id = m.merchant_id AND filter_tag.tag_id = %s
        )""")
        params.append(tag_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    offset = (page - 1) * page_size
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM merchants m {where}", params)
            total = int(cur.fetchone()["total"])
            cur.execute(
                f"""
                SELECT m.merchant_id, m.canonical_name, m.aliases, m.city, m.district,
                       m.cuisine, m.price_min, m.price_max, m.currency, m.price_basis,
                       m.confidence, m.first_seen_at, m.last_seen_at, m.updated_at, m.reservation_info,
                       COALESCE((
                           SELECT string_agg(e.evidence_text, E'\\n') FROM (
                               SELECT DISTINCT LEFT(source.evidence_text, 1000) AS evidence_text
                               FROM merchant_notes source WHERE source.merchant_id = m.merchant_id
                                 AND source.evidence_text <> ''
                               ORDER BY evidence_text LIMIT 5
                           ) e
                       ), '') AS description,
                       COUNT(DISTINCT mn.note_id)::int AS note_count,
                       COALESCE(
                           jsonb_agg(DISTINCT jsonb_build_object(
                               'category_id', mc.category_id,
                               'name', mc.name,
                               'is_primary', mcl.is_primary
                           )) FILTER (WHERE mc.category_id IS NOT NULL),
                           '[]'::jsonb
                       ) AS categories,
                       COALESCE(
                           jsonb_agg(DISTINCT jsonb_build_object(
                               'tag_id', mt.tag_id,
                               'name', mt.name,
                               'group_name', mt.group_name,
                               'description', mt.description
                           )) FILTER (WHERE mt.tag_id IS NOT NULL),
                           '[]'::jsonb
                       ) AS tags
                FROM merchants m
                LEFT JOIN merchant_notes mn ON mn.merchant_id = m.merchant_id
                LEFT JOIN merchant_category_links mcl ON mcl.merchant_id = m.merchant_id
                LEFT JOIN merchant_categories mc ON mc.category_id = mcl.category_id AND mc.status = 'active'
                LEFT JOIN merchant_tag_links mtl ON mtl.merchant_id = m.merchant_id
                LEFT JOIN merchant_tags mt ON mt.tag_id = mtl.tag_id AND mt.status = 'active'
                {where}
                GROUP BY m.merchant_id
                ORDER BY m.updated_at DESC NULLS LAST, m.canonical_name ASC, m.merchant_id ASC
                LIMIT %s OFFSET %s
                """, [*params, page_size, offset],
            )
            items = [dict(row) for row in cur.fetchall()]
        return {"items": items, "page": page, "page_size": page_size, "total": total}
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.get("/api/merchants/{merchant_id}")
def get_merchant(
    merchant_id: str,
    notes_page: int = Query(default=1, ge=1, le=10000),
    notes_page_size: int | None = Query(default=None, ge=1, le=50),
) -> dict[str, Any]:
    """返回商户详情、分类和可追溯的来源笔记摘要。"""
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT merchant_id, canonical_name, aliases, city, district, cuisine,
                       price_min, price_max, currency, price_basis, reservation_info,
                       confidence, first_seen_at, last_seen_at, updated_at
                FROM merchants
                WHERE merchant_id = %s
                """, (merchant_id,),
            )
            merchant = cur.fetchone()
            if not merchant:
                raise HTTPException(status_code=404, detail="Merchant not found")

            cur.execute(
                """
                SELECT mc.category_id, mc.name, mc.description, mcl.is_primary,
                       mcl.evidence_text, mcl.confidence, mcl.taxonomy_version
                FROM merchant_category_links mcl
                JOIN merchant_categories mc ON mc.category_id = mcl.category_id
                WHERE mcl.merchant_id = %s
                ORDER BY mcl.is_primary DESC, mc.sort_order, mc.name
                """, (merchant_id,),
            )
            categories = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT mt.tag_id, mt.name, mt.group_name, mtl.evidence_text,
                       mtl.confidence
                FROM merchant_tag_links mtl
                JOIN merchant_tags mt ON mt.tag_id = mtl.tag_id
                WHERE mtl.merchant_id = %s
                ORDER BY mt.group_name, mt.name
                """, (merchant_id,),
            )
            tags = [dict(row) for row in cur.fetchall()]

            cur.execute(
                "SELECT COUNT(DISTINCT note_id)::int AS note_count, COUNT(*)::int AS notes_total "
                "FROM merchant_notes WHERE merchant_id = %s", (merchant_id,),
            )
            counts = dict(cur.fetchone())
            notes_pagination = ""
            notes_params: list[Any] = [merchant_id]
            if notes_page_size is not None:
                notes_pagination = "LIMIT %s OFFSET %s"
                notes_params.extend([notes_page_size, (notes_page - 1) * notes_page_size])
            cur.execute(
                f"""
                SELECT mn.note_id, mn.relation_type, mn.evidence_text, mn.confidence,
                       n.title, COALESCE(NULLIF(n.content, ''), n.description) AS content,
                       n.video_summary, n.image_summary, n.note_url, n.published_at, n.crawled_at
                FROM merchant_notes mn
                JOIN xhs_notes n ON n.note_id = mn.note_id
                WHERE mn.merchant_id = %s
                ORDER BY NULLIF(n.published_at, '') DESC NULLS LAST, n.crawled_at DESC NULLS LAST,
                         mn.note_id, mn.relation_type
                {notes_pagination}
                """, notes_params,
            )
            notes = [dict(row) for row in cur.fetchall()]
        result = dict(merchant)
        result["categories"] = categories
        result["tags"] = tags
        result["notes"] = notes
        result.update(counts)
        result["notes_page"] = notes_page
        result["notes_page_size"] = notes_page_size
        result["notes_has_more"] = notes_page_size is not None and notes_page * notes_page_size < counts["notes_total"]
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.get("/api/concerts/upcoming")
def list_upcoming_concerts(
    keyword: str | None = Query(default=None, max_length=120),
    artist: str | None = Query(default=None, max_length=120),
    venue: str | None = Query(default=None, max_length=120),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    """返回 Ticketmaster 中尚未开始的演唱会场次，按开始时间升序。"""
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date 不能晚于 to_date")
    clauses = ["c.status = 'active'", "o.status = 'upcoming'", "o.start_at > CURRENT_TIMESTAMP"]
    params: list[Any] = []
    if keyword and keyword.strip():
        value = f"%{keyword.strip()}%"
        clauses.append("(c.title ILIKE %s OR c.artist_name ILIKE %s OR c.venue_name ILIKE %s OR o.venue_name ILIKE %s OR c.promoter ILIKE %s OR c.description ILIKE %s)")
        params.extend([value] * 6)
    if artist and artist.strip():
        clauses.append("c.artist_name ILIKE %s")
        params.append(f"%{artist.strip()}%")
    if venue and venue.strip():
        clauses.append("(c.venue_name ILIKE %s OR o.venue_name ILIKE %s)")
        params.extend([f"%{venue.strip()}%"] * 2)
    if from_date:
        clauses.append("o.start_at >= (%s::date AT TIME ZONE 'Asia/Singapore')")
        params.append(from_date)
    if to_date:
        clauses.append("o.start_at < ((%s::date + INTERVAL '1 day') AT TIME ZONE 'Asia/Singapore')")
        params.append(to_date)
    where = " AND ".join(clauses)
    offset = (page - 1) * page_size
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM ticketmaster_concerts c
                JOIN ticketmaster_concert_occurrences o ON o.concert_id = c.concert_id
                WHERE {where}
                """, params,
            )
            total = int(cur.fetchone()["total"])
            cur.execute(
                f"""
                SELECT c.concert_id, c.title, c.artist_name, c.artist_id, c.promoter,
                       c.category_name, c.venue_name, c.city, c.country,
                       c.event_url, c.image_url, c.description,
                       o.occurrence_id, o.source_event_id, o.start_at, o.end_at,
                       o.display_date, o.venue_name AS occurrence_venue_name,
                       o.ticket_url, o.ticket_status, o.status AS occurrence_status
                FROM ticketmaster_concerts c
                JOIN ticketmaster_concert_occurrences o ON o.concert_id = c.concert_id
                WHERE {where}
                ORDER BY o.start_at ASC, c.artist_name ASC, c.concert_id ASC, o.occurrence_id ASC
                LIMIT %s OFFSET %s
                """, [*params, page_size, offset],
            )
            items = [dict(row) for row in cur.fetchall()]
        return {"items": items, "page": page, "page_size": page_size, "total": total}
    except HTTPException:
        raise
    except Exception as exc:
        raise _db_error(exc) from exc


@data_router.get("/api/concerts/{concert_id}")
def get_concert(
    concert_id: str,
    occurrences_page: int = Query(default=1, ge=1, le=10000),
    occurrences_page_size: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """返回 Ticketmaster 活动详情和其全部（或分页）场次。"""
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT concert_id, source, title, artist_name, artist_id, promoter,
                       category_name, venue_name, city, country, event_url, image_url,
                       description, status, first_seen_at, last_seen_at, updated_at
                FROM ticketmaster_concerts
                WHERE concert_id = %s
                """, (concert_id,),
            )
            concert = cur.fetchone()
            if not concert:
                raise HTTPException(status_code=404, detail="Concert not found")
            cur.execute(
                "SELECT COUNT(*) AS total FROM ticketmaster_concert_occurrences WHERE concert_id = %s",
                (concert_id,),
            )
            occurrences_total = int(cur.fetchone()["total"])
            cur.execute(
                """
                SELECT occurrence_id, source_event_id, start_at, end_at, display_date,
                       venue_name, ticket_url, ticket_status, status, first_seen_at,
                       last_seen_at, updated_at
                FROM ticketmaster_concert_occurrences
                WHERE concert_id = %s
                ORDER BY start_at ASC, occurrence_id ASC
                LIMIT %s OFFSET %s
                """,
                (concert_id, occurrences_page_size, (occurrences_page - 1) * occurrences_page_size),
            )
            occurrences = [dict(row) for row in cur.fetchall()]
        result = dict(concert)
        result["occurrences"] = occurrences
        result["occurrences_page"] = occurrences_page
        result["occurrences_page_size"] = occurrences_page_size
        result["occurrences_total"] = occurrences_total
        result["occurrences_has_more"] = occurrences_page * occurrences_page_size < occurrences_total
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise _db_error(exc) from exc


app.include_router(data_router)
