"""跨票务平台共用的演唱会结构和幂等存储逻辑。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any

from psycopg2 import sql

from database import Connection


@dataclass(frozen=True)
class ConcertDetail:
    """一个来源适配器解析出的活动和未来场次。"""

    concert_id: str
    title: str
    artist_name: str
    artist_id: str
    promoter: str
    category_name: str
    venue_name: str
    city: str
    country: str
    event_url: str
    image_url: str
    description: str
    occurrences: tuple[dict[str, Any], ...]
    source_hash: str
    raw_data: dict[str, Any]


_TABLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _table(name: str) -> sql.Identifier:
    if not _TABLE_NAME_RE.fullmatch(name):
        raise ValueError(f"非法演唱会表名: {name}")
    return sql.Identifier(name)


def _json(value: Any, default: Any) -> str:
    return json.dumps(default if value is None else value, ensure_ascii=False, separators=(",", ":"))


def mark_started_occurrences(
    conn: Connection,
    *,
    concerts_table: str,
    occurrences_table: str,
) -> int:
    """把指定来源的过期 upcoming 场次转成 started，并更新活动状态。"""
    concerts = _table(concerts_table)
    occurrences = _table(occurrences_table)
    with conn:
        cur = conn.execute(
            sql.SQL(
                """UPDATE {occurrences}
                   SET status='started', updated_at=CURRENT_TIMESTAMP
                   WHERE status='upcoming' AND start_at <= CURRENT_TIMESTAMP"""
            ).format(occurrences=occurrences)
        )
        conn.execute(
            sql.SQL(
                """UPDATE {concerts} AS c
                   SET status='ended', updated_at=CURRENT_TIMESTAMP
                   WHERE c.status='active'
                     AND EXISTS (
                         SELECT 1 FROM {occurrences} o
                         WHERE o.concert_id = c.concert_id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM {occurrences} o
                         WHERE o.concert_id = c.concert_id AND o.status='upcoming'
                     )"""
            ).format(concerts=concerts, occurrences=occurrences)
        )
        return cur.rowcount


def upsert_concert(
    conn: Connection,
    detail: ConcertDetail,
    *,
    concerts_table: str,
    occurrences_table: str,
    source: str,
) -> tuple[int, int]:
    """幂等写入一个来源的活动和未来场次，返回 (活动数, 场次数)。"""
    if not detail.occurrences:
        return 0, 0
    concerts = _table(concerts_table)
    occurrences = _table(occurrences_table)
    now = datetime.now(timezone.utc)
    with conn:
        conn.execute(
            sql.SQL(
                """INSERT INTO {concerts} (
                    concert_id, source, title, artist_name, artist_id, promoter, category_name,
                    venue_name, city, country, event_url, image_url, description, source_hash,
                    raw_data, status, first_seen_at, last_seen_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, 'active', %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (concert_id) DO UPDATE SET
                    title=COALESCE(NULLIF(EXCLUDED.title, ''), {concerts}.title),
                    artist_name=COALESCE(NULLIF(EXCLUDED.artist_name, ''), {concerts}.artist_name),
                    artist_id=COALESCE(NULLIF(EXCLUDED.artist_id, ''), {concerts}.artist_id),
                    promoter=COALESCE(NULLIF(EXCLUDED.promoter, ''), {concerts}.promoter),
                    category_name=COALESCE(NULLIF(EXCLUDED.category_name, ''), {concerts}.category_name),
                    venue_name=COALESCE(NULLIF(EXCLUDED.venue_name, ''), {concerts}.venue_name),
                    city=COALESCE(NULLIF(EXCLUDED.city, ''), {concerts}.city),
                    country=COALESCE(NULLIF(EXCLUDED.country, ''), {concerts}.country),
                    event_url=COALESCE(NULLIF(EXCLUDED.event_url, ''), {concerts}.event_url),
                    image_url=COALESCE(NULLIF(EXCLUDED.image_url, ''), {concerts}.image_url),
                    description=COALESCE(NULLIF(EXCLUDED.description, ''), {concerts}.description),
                    source_hash=COALESCE(NULLIF(EXCLUDED.source_hash, ''), {concerts}.source_hash),
                    raw_data=EXCLUDED.raw_data,
                    status='active',
                    last_seen_at=EXCLUDED.last_seen_at,
                    updated_at=CURRENT_TIMESTAMP"""
            ).format(concerts=concerts),
            (
                detail.concert_id,
                source,
                detail.title,
                detail.artist_name,
                detail.artist_id,
                detail.promoter,
                detail.category_name,
                detail.venue_name,
                detail.city,
                detail.country,
                detail.event_url,
                detail.image_url,
                detail.description,
                detail.source_hash,
                _json(detail.raw_data, {}),
                now,
                now,
            ),
        )
        for occurrence in detail.occurrences:
            conn.execute(
                sql.SQL(
                    """INSERT INTO {occurrences} (
                        occurrence_id, concert_id, source_event_id, start_at, end_at,
                        display_date, venue_name, ticket_url, ticket_status, source_hash,
                        raw_data, status, first_seen_at, last_seen_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (concert_id, source_event_id) DO UPDATE SET
                        occurrence_id=EXCLUDED.occurrence_id,
                        source_event_id=EXCLUDED.source_event_id,
                        start_at=EXCLUDED.start_at,
                        end_at=EXCLUDED.end_at,
                        display_date=EXCLUDED.display_date,
                        venue_name=EXCLUDED.venue_name,
                        ticket_url=EXCLUDED.ticket_url,
                        ticket_status=EXCLUDED.ticket_status,
                        source_hash=EXCLUDED.source_hash,
                        raw_data=EXCLUDED.raw_data,
                        status=CASE WHEN EXCLUDED.start_at > CURRENT_TIMESTAMP THEN 'upcoming' ELSE 'started' END,
                        last_seen_at=EXCLUDED.last_seen_at,
                        updated_at=CURRENT_TIMESTAMP"""
                ).format(occurrences=occurrences),
                (
                    occurrence["occurrence_id"],
                    detail.concert_id,
                    occurrence["source_event_id"],
                    occurrence["start_at"],
                    occurrence.get("end_at"),
                    occurrence.get("display_date", ""),
                    occurrence.get("venue_name", detail.venue_name),
                    occurrence.get("ticket_url", ""),
                    occurrence.get("ticket_status", ""),
                    occurrence.get("source_hash", detail.source_hash),
                    _json(occurrence.get("raw_data"), {}),
                    occurrence.get("status", "upcoming"),
                    now,
                    now,
                ),
            )
    return 1, len(detail.occurrences)
