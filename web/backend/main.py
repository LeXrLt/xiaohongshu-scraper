"""HTTP API for the standalone Xiaohongshu reader."""

from __future__ import annotations

import os
from contextlib import closing
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

from reader_config import (
    ReaderConfig,
    config_dict,
    get_connection,
    read_config,
    write_config,
)

load_dotenv()

app = FastAPI(title="XHS Reader API", version="1.0.0")
origins = [item.strip() for item in os.getenv("FRONTEND_ORIGIN", "http://localhost:3000").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "PUT", "OPTIONS"],
    allow_headers=["*"],
)


class SettingsPayload(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    bloggers: list[str] = Field(default_factory=list)
    cookies: str = ""


def _db_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"PostgreSQL unavailable: {exc}")


@app.get("/api/health")
def health() -> dict[str, str]:
    try:
        with closing(get_connection()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return {"status": "ok"}
    except Exception as exc:
        raise _db_error(exc) from exc

# End of API routes.
@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    try:
        with closing(get_connection()) as conn:
            return config_dict(read_config(conn))
    except Exception as exc:
        raise _db_error(exc) from exc


@app.put("/api/settings")
def update_settings(payload: SettingsPayload) -> dict[str, Any]:
    try:
        with closing(get_connection()) as conn:
            config = write_config(conn, ReaderConfig(**payload.model_dump()))
            return config_dict(config)
    except Exception as exc:
        raise _db_error(exc) from exc


@app.get("/api/notes")
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


@app.get("/api/notes/{note_id}")
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


@app.get("/api/merchants")
def list_merchants(
    keyword: str | None = Query(default=None, max_length=120),
    city: str | None = Query(default=None, max_length=80),
    district: str | None = Query(default=None, max_length=80),
    category_id: str | None = Query(default=None, max_length=80),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=24, ge=1, le=100),
) -> dict[str, Any]:
    """返回商户摘要；详情和来源笔记由 /api/merchants/{id} 单独读取。"""
    clauses: list[str] = []
    params: list[Any] = []
    if keyword and keyword.strip():
        value = f"%{keyword.strip()}%"
        clauses.append("(m.canonical_name ILIKE %s OR m.aliases::text ILIKE %s OR m.cuisine::text ILIKE %s)")
        params.extend([value, value, value])
    if city and city.strip():
        clauses.append("m.city ILIKE %s")
        params.append(f"%{city.strip()}%")
    if district and district.strip():
        clauses.append("m.district ILIKE %s")
        params.append(f"%{district.strip()}%")
    if category_id and category_id.strip():
        clauses.append("EXISTS (SELECT 1 FROM merchant_category_links filter_link WHERE filter_link.merchant_id = m.merchant_id AND filter_link.category_id = %s)")
        params.append(category_id.strip())
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
                       m.status, m.confidence, m.first_seen_at, m.last_seen_at, m.updated_at,
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
                               'group_name', mt.group_name
                           )) FILTER (WHERE mt.tag_id IS NOT NULL),
                           '[]'::jsonb
                       ) AS tags
                FROM merchants m
                LEFT JOIN merchant_notes mn ON mn.merchant_id = m.merchant_id
                LEFT JOIN merchant_category_links mcl ON mcl.merchant_id = m.merchant_id
                LEFT JOIN merchant_categories mc ON mc.category_id = mcl.category_id
                LEFT JOIN merchant_tag_links mtl ON mtl.merchant_id = m.merchant_id
                LEFT JOIN merchant_tags mt ON mt.tag_id = mtl.tag_id
                {where}
                GROUP BY m.merchant_id
                ORDER BY m.updated_at DESC NULLS LAST, m.canonical_name ASC
                LIMIT %s OFFSET %s
                """, [*params, page_size, offset],
            )
            items = [dict(row) for row in cur.fetchall()]
        return {"items": items, "page": page, "page_size": page_size, "total": total}
    except Exception as exc:
        raise _db_error(exc) from exc


@app.get("/api/merchants/{merchant_id}")
def get_merchant(merchant_id: str) -> dict[str, Any]:
    """返回商户详情、分类和可追溯的来源笔记摘要。"""
    try:
        with closing(get_connection()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT merchant_id, canonical_name, aliases, city, district, cuisine,
                       price_min, price_max, currency, price_basis, reservation_info,
                       status, confidence, first_seen_at, last_seen_at, updated_at
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
                """
                SELECT mn.note_id, mn.relation_type, mn.evidence_text, mn.confidence,
                       n.title, COALESCE(NULLIF(n.content, ''), n.description) AS content,
                       n.note_url, n.published_at, n.crawled_at
                FROM merchant_notes mn
                JOIN xhs_notes n ON n.note_id = mn.note_id
                WHERE mn.merchant_id = %s
                ORDER BY n.published_at DESC NULLS LAST, n.crawled_at DESC NULLS LAST
                """, (merchant_id,),
            )
            notes = [dict(row) for row in cur.fetchall()]
        result = dict(merchant)
        result["categories"] = categories
        result["tags"] = tags
        result["notes"] = notes
        result["note_count"] = len(notes)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise _db_error(exc) from exc
