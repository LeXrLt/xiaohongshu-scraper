"""通用推送记录表管理模块。

用于追踪商户、演唱会等实体的推送状态与变更检测：
1. 实体未在表中出现时视为“从未推送”。
2. 支持记录推送成功、失败、重试及内容快照。
3. 支持基于 updated_at / content_hash 检测已推送实体的后续更新。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import json
from psycopg2.extras import Json, DictRow


ENTITY_TYPE_MERCHANT = "merchant"
ENTITY_TYPE_CONCERT = "ticketmaster_concert"
ENTITY_TYPE_OCCURRENCE = "ticketmaster_occurrence"
ENTITY_TYPE_NOTE = "xhs_note"

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

DEFAULT_CHANNEL = "default"


def get_push_record(
    conn,
    entity_type: str,
    entity_id: str,
    channel: str = DEFAULT_CHANNEL,
) -> dict[str, Any] | None:
    """获取指定实体的推送记录。若返回 None 则代表该实体从未推送过。"""
    cur = conn.execute(
        """SELECT * FROM push_records
           WHERE entity_type = %s AND entity_id = %s AND channel = %s""",
        (entity_type, str(entity_id), channel),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def is_pushed(
    conn,
    entity_type: str,
    entity_id: str,
    channel: str = DEFAULT_CHANNEL,
) -> bool:
    """快速判断实体是否已成功推送过。"""
    record = get_push_record(conn, entity_type, entity_id, channel)
    return bool(record and record.get("push_status") == STATUS_SUCCESS)


def find_unpushed_entities(
    conn,
    entity_type: str,
    channel: str = DEFAULT_CHANNEL,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    """查询尚未推送过（无记录或状态为 pending）的实体列表。"""
    limit_clause = "LIMIT %s" if limit is not None else ""
    params: list[Any] = [entity_type, channel]

    if entity_type == ENTITY_TYPE_MERCHANT:
        sql = f"""
            SELECT m.*, p.record_id AS push_record_id, p.push_status
            FROM merchants m
            LEFT JOIN push_records p
              ON p.entity_type = %s
             AND p.entity_id = m.merchant_id
             AND p.channel = %s
            WHERE p.record_id IS NULL OR p.push_status = 'pending'
            ORDER BY m.updated_at DESC
            {limit_clause}
        """
    elif entity_type == ENTITY_TYPE_CONCERT:
        sql = f"""
            SELECT c.*, p.record_id AS push_record_id, p.push_status
            FROM ticketmaster_concerts c
            LEFT JOIN push_records p
              ON p.entity_type = %s
             AND p.entity_id = c.concert_id
             AND p.channel = %s
            WHERE p.record_id IS NULL OR p.push_status = 'pending'
            ORDER BY c.updated_at DESC
            {limit_clause}
        """
    elif entity_type == ENTITY_TYPE_OCCURRENCE:
        sql = f"""
            SELECT o.*, c.title AS concert_title, c.artist_name,
                   p.record_id AS push_record_id, p.push_status
            FROM ticketmaster_concert_occurrences o
            JOIN ticketmaster_concerts c ON c.concert_id = o.concert_id
            LEFT JOIN push_records p
              ON p.entity_type = %s
             AND p.entity_id = o.occurrence_id
             AND p.channel = %s
            WHERE p.record_id IS NULL OR p.push_status = 'pending'
            ORDER BY o.start_at ASC
            {limit_clause}
        """
    else:
        raise ValueError(f"不支持的实体类型: {entity_type}")

    if limit is not None:
        params.append(limit)

    cur = conn.execute(sql, tuple(params))
    return [dict(row) for row in cur.fetchall()]


def find_updated_entities(
    conn,
    entity_type: str,
    channel: str = DEFAULT_CHANNEL,
    limit: int | None = 100,
) -> list[dict[str, Any]]:
    """查询已成功推送过、但后续库中有更新的实体列表。"""
    limit_clause = "LIMIT %s" if limit is not None else ""
    params: list[Any] = [entity_type, channel]

    if entity_type == ENTITY_TYPE_MERCHANT:
        sql = f"""
            SELECT m.*, p.last_pushed_at, p.content_hash AS pushed_hash, p.entity_updated_at AS pushed_updated_at
            FROM merchants m
            JOIN push_records p
              ON p.entity_type = %s
             AND p.entity_id = m.merchant_id
             AND p.channel = %s
            WHERE p.push_status = 'success'
              AND (
                (p.entity_updated_at IS NOT NULL AND m.updated_at > p.entity_updated_at)
                OR (p.entity_updated_at IS NULL AND m.updated_at > p.last_pushed_at)
              )
            ORDER BY m.updated_at DESC
            {limit_clause}
        """
    elif entity_type == ENTITY_TYPE_CONCERT:
        sql = f"""
            SELECT c.*, p.last_pushed_at, p.content_hash AS pushed_hash, p.entity_updated_at AS pushed_updated_at
            FROM ticketmaster_concerts c
            JOIN push_records p
              ON p.entity_type = %s
             AND p.entity_id = c.concert_id
             AND p.channel = %s
            WHERE p.push_status = 'success'
              AND (
                (p.entity_updated_at IS NOT NULL AND c.updated_at > p.entity_updated_at)
                OR (p.entity_updated_at IS NULL AND c.updated_at > p.last_pushed_at)
                OR (c.source_hash <> '' AND p.content_hash <> '' AND c.source_hash <> p.content_hash)
              )
            ORDER BY c.updated_at DESC
            {limit_clause}
        """
    elif entity_type == ENTITY_TYPE_OCCURRENCE:
        sql = f"""
            SELECT o.*, c.title AS concert_title, c.artist_name,
                   p.last_pushed_at, p.content_hash AS pushed_hash, p.entity_updated_at AS pushed_updated_at
            FROM ticketmaster_concert_occurrences o
            JOIN ticketmaster_concerts c ON c.concert_id = o.concert_id
            JOIN push_records p
              ON p.entity_type = %s
             AND p.entity_id = o.occurrence_id
             AND p.channel = %s
            WHERE p.push_status = 'success'
              AND (
                (p.entity_updated_at IS NOT NULL AND o.updated_at > p.entity_updated_at)
                OR (p.entity_updated_at IS NULL AND o.updated_at > p.last_pushed_at)
                OR (o.source_hash <> '' AND p.content_hash <> '' AND o.source_hash <> p.content_hash)
              )
            ORDER BY o.updated_at DESC
            {limit_clause}
        """
    else:
        raise ValueError(f"不支持的实体类型: {entity_type}")

    if limit is not None:
        params.append(limit)

    cur = conn.execute(sql, tuple(params))
    return [dict(row) for row in cur.fetchall()]


def mark_push_pending(
    conn,
    entity_type: str,
    entity_id: str,
    channel: str = DEFAULT_CHANNEL,
    commit: bool = True,
) -> None:
    """标记准备推送中（防并发重入）。"""
    conn.execute(
        """INSERT INTO push_records (
               entity_type, entity_id, channel, push_status, updated_at
           ) VALUES (%s, %s, %s, 'pending', CURRENT_TIMESTAMP)
           ON CONFLICT (entity_type, entity_id, channel) DO UPDATE SET
               push_status = 'pending',
               updated_at = CURRENT_TIMESTAMP""",
        (entity_type, str(entity_id), channel),
    )
    if commit:
        conn.commit()


def mark_push_success(
    conn,
    entity_type: str,
    entity_id: str,
    channel: str = DEFAULT_CHANNEL,
    entity_updated_at: datetime | None = None,
    content_hash: str = "",
    payload_snapshot: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    """标记推送成功。"""
    payload_json = Json(payload_snapshot or {})
    conn.execute(
        """INSERT INTO push_records (
               entity_type, entity_id, channel, push_status,
               content_hash, entity_updated_at, last_pushed_at,
               retry_count, error_message, payload_snapshot, updated_at
           ) VALUES (
               %s, %s, %s, 'success',
               %s, %s, CURRENT_TIMESTAMP,
               0, '', %s, CURRENT_TIMESTAMP
           )
           ON CONFLICT (entity_type, entity_id, channel) DO UPDATE SET
               push_status = 'success',
               content_hash = EXCLUDED.content_hash,
               entity_updated_at = EXCLUDED.entity_updated_at,
               last_pushed_at = CURRENT_TIMESTAMP,
               retry_count = 0,
               error_message = '',
               payload_snapshot = EXCLUDED.payload_snapshot,
               updated_at = CURRENT_TIMESTAMP""",
        (
            entity_type,
            str(entity_id),
            channel,
            content_hash or "",
            entity_updated_at,
            payload_json,
        ),
    )
    if commit:
        conn.commit()


def mark_push_failed(
    conn,
    entity_type: str,
    entity_id: str,
    error_message: str,
    channel: str = DEFAULT_CHANNEL,
    payload_snapshot: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    """记录推送失败并累加重试次数。"""
    payload_json = Json(payload_snapshot or {})
    conn.execute(
        """INSERT INTO push_records (
               entity_type, entity_id, channel, push_status,
               retry_count, error_message, payload_snapshot, updated_at
           ) VALUES (
               %s, %s, %s, 'failed',
               1, %s, %s, CURRENT_TIMESTAMP
           )
           ON CONFLICT (entity_type, entity_id, channel) DO UPDATE SET
               push_status = 'failed',
               retry_count = push_records.retry_count + 1,
               error_message = EXCLUDED.error_message,
               payload_snapshot = EXCLUDED.payload_snapshot,
               updated_at = CURRENT_TIMESTAMP""",
        (
            entity_type,
            str(entity_id),
            channel,
            str(error_message)[:1000],
            payload_json,
        ),
    )
    if commit:
        conn.commit()


def mark_push_skipped(
    conn,
    entity_type: str,
    entity_id: str,
    reason: str = "",
    channel: str = DEFAULT_CHANNEL,
    commit: bool = True,
) -> None:
    """标记跳过推送（例如低置信度商户，无需推送给用户）。"""
    conn.execute(
        """INSERT INTO push_records (
               entity_type, entity_id, channel, push_status,
               error_message, updated_at
           ) VALUES (
               %s, %s, %s, 'skipped',
               %s, CURRENT_TIMESTAMP
           )
           ON CONFLICT (entity_type, entity_id, channel) DO UPDATE SET
               push_status = 'skipped',
               error_message = EXCLUDED.error_message,
               updated_at = CURRENT_TIMESTAMP""",
        (entity_type, str(entity_id), channel, reason),
    )
    if commit:
        conn.commit()


def get_push_stats(conn, channel: str = DEFAULT_CHANNEL) -> list[dict[str, Any]]:
    """统计各实体类型的推送状态分布。"""
    cur = conn.execute(
        """SELECT entity_type, push_status, COUNT(*) AS count
           FROM push_records
           WHERE channel = %s
           GROUP BY entity_type, push_status
           ORDER BY entity_type, push_status""",
        (channel,),
    )
    return [dict(row) for row in cur.fetchall()]
