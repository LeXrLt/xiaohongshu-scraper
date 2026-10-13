"""PostgreSQL-backed settings used by the standalone reader API."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from dataclasses import asdict, dataclass
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from database import connect as get_connection


@dataclass
class ReaderConfig:
    keywords: list[str]
    bloggers: list[str]
    cookies: str


def ensure_config_table(conn) -> None:
    """Create the small settings table when running against an existing database."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reader_config (
                id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
                bloggers JSONB NOT NULL DEFAULT '[]'::jsonb,
                cookies TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("INSERT INTO reader_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    conn.commit()


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        item = str(item).strip()
        if item and item not in result:
            result.append(item)
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
    return value


def read_config(conn) -> ReaderConfig:
    ensure_config_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT keywords, bloggers, cookies FROM reader_config WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return ReaderConfig([], [], "")
    return ReaderConfig(
        keywords=_as_string_list(_json_value(row[0])),
        bloggers=_as_string_list(_json_value(row[1])),
        cookies=row[2] or "",
    )


def write_config(conn, config: ReaderConfig) -> ReaderConfig:
    config = ReaderConfig(
        keywords=_as_string_list(config.keywords),
        bloggers=_as_string_list(config.bloggers),
        cookies=(config.cookies or "").strip(),
    )
    ensure_config_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reader_config (id, keywords, bloggers, cookies, updated_at)
            VALUES (1, %s::jsonb, %s::jsonb, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET
                keywords = EXCLUDED.keywords,
                bloggers = EXCLUDED.bloggers,
                cookies = EXCLUDED.cookies,
                updated_at = NOW()
            """,
            (json.dumps(config.keywords, ensure_ascii=False),
             json.dumps(config.bloggers, ensure_ascii=False), config.cookies),
        )
    conn.commit()
    return config


def config_dict(config: ReaderConfig) -> dict[str, Any]:
    return asdict(config)
