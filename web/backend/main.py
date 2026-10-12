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
