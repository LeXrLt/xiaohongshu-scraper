"""SQLite → PostgreSQL 同步工具。

爬虫以 SQLite 为本地数据源；阅读器使用 PostgreSQL。这个模块只负责
幂等地把本地数据同步到阅读器数据库，不依赖任何外部编排服务。

手动同步：
    python pg_sync.py
"""
import json
import os
import sqlite3
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
_ADAPTER_DIR = ROOT
SCHEMA_PATH = ROOT / "schema.sql"
DB_PATH = ROOT / "data" / "xhs.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("xhs_pg_sync")


def get_pg_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "xhs_reader"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        dbname=os.getenv("POSTGRES_DB", "xhs_reader"),
    )


def init_schema(conn):
    if not SCHEMA_PATH.exists():
        return
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _pg_upsert(conn, table, data, conflict_cols):
    cols = list(data.keys())
    vals = [_pg_val(data[c]) for c in cols]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in conflict_cols)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON CONFLICT ({', '.join(conflict_cols)}) DO UPDATE SET {updates}")
    with conn.cursor() as cur:
        cur.execute(sql, vals)
    conn.commit()


def _pg_val(v):
    if isinstance(v, str):
        v = v.replace("\x00", "")
    if isinstance(v, (dict, list)):
        return Json(v)
    return v


def _sqlite_json(val, context: str = ""):
    """SQLite TEXT → PG JSONB。解析失败时记录警告而非静默丢弃。"""
    if val is None:
        return Json({})
    if isinstance(val, str):
        try:
            return Json(json.loads(val))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("JSON 解析失败%s，数据将被替换为空对象: %s (原始值前80字: %.80s)",
                           f" ({context})" if context else "", e, val)
            return Json({})
    return Json(val)


def _extract_from_raw(raw_json_str):
    """从 raw_json 中提取增强字段（参考 by_luzhe/substack 的字段设计）。

    提取：
    - content: 正文（raw_json.desc，比 description 更完整）
    - image_urls: 图片 URL 列表（从 image_list 提取）
    - note_url: 笔记原始链接
    - video_url: 如果 SQLite 字段为空，从 raw_json 提取
    """
    result = {}
    if not raw_json_str:
        return result

    try:
        raw = json.loads(raw_json_str) if isinstance(raw_json_str, str) else raw_json_str
    except (json.JSONDecodeError, TypeError):
        return result

    # 正文：优先用 raw_json.desc
    desc = raw.get("desc", "")
    if desc:
        result["content"] = desc

    # 笔记链接
    note_id = raw.get("note_id", "")
    if note_id:
        result["note_url"] = f"https://www.xiaohongshu.com/explore/{note_id}"

    # 图片 URL 列表
    image_urls = []
    for img in raw.get("image_list", []):
        url = img.get("url_default") or img.get("url") or ""
        if not url:
            for info in img.get("info_list", []):
                if info.get("image_scene") in ("WB_DFT", "WB_PRV"):
                    url = info.get("url", "")
                    break
        if url:
            image_urls.append(url)
    if image_urls:
        result["image_urls"] = image_urls

    # 视频链接（fallback）
    video = raw.get("video", {})
    media = video.get("media", {})
    stream = media.get("stream", {})
    for codec in ("h264", "h265", "av1"):
        items = stream.get(codec, [])
        if items:
            url = items[0].get("master_url") or ""
            if url:
                result["video_url_fallback"] = url
                break

    return result


def _scan_media_dirs(sq_conn):
    """对每条笔记正向计算预期媒体目录，检查存在性 → note_id → (相对路径, 是否有文件)。

    用 xhs_config.note_media_dir（与写入时完全一致的 sanitize 口径）算目录，
    避免旧实现用原始 title 反推导致的 sanitize/截断/同名标题匹配失败。
    """
    # 复用 xhs_config 中的媒体目录规则，避免同步路径与下载路径不一致。
    import sys as _sys
    scripts_dir = ROOT / "scripts"
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    try:
        import xhs_config
    except Exception:
        return {}

    data_root = ROOT / "data"
    mapping = {}
    try:
        note_ids = [r["note_id"] for r in sq_conn.execute("SELECT note_id FROM notes")]
    except Exception:
        return mapping

    for note_id in note_ids:
        # note_media_dir 按 sanitize(博主)/sanitize(标题) 算目录；查不到 title 时 fallback media/<note_id>/
        expected = xhs_config.note_media_dir(note_id, sq_conn)
        has_files = expected.exists() and expected.is_dir() and any(expected.iterdir())
        if not has_files:
            mapping[note_id] = ("", False)
            continue
        try:
            base = data_root if expected.is_relative_to(data_root) else _ADAPTER_DIR
            rel_path = str(expected.relative_to(base)).replace("\\", "/")
        except Exception:
            rel_path = ""
        mapping[note_id] = (rel_path, True)
    return mapping


def _sqlite_has_table(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


# ── SQLite → PG 同步 ──

def sync_sqlite_to_pg(pg_conn):
    """读取本地 SQLite 数据库，同步到阅读器 PostgreSQL。"""
    db_path = DB_PATH
    if not db_path.exists():
        logger.warning("  未找到 xhs.db")
        return

    sq = sqlite3.connect(str(db_path))
    sq.row_factory = sqlite3.Row
    # 与 xhs_storage.py 保持一致：WAL 模式 + busy_timeout
    sq.execute("PRAGMA journal_mode=WAL")
    sq.execute("PRAGMA busy_timeout=30000")
    # 确保 xhs.py 子进程的 WAL 已刷入主库
    sq.execute("PRAGMA wal_checkpoint(FULL)")

    # 预扫描 media 目录（传入 SQLite 连接用于 title→note_id 匹配）
    media_map = _scan_media_dirs(sq)

    now = datetime.now(timezone.utc)
    synced = 0

    # 同步 notes
    for row in sq.execute("SELECT * FROM notes"):
        # 从 raw_json 提取增强字段
        enriched = _extract_from_raw(row["raw_json"])

        video_url = row["video_url"] or enriched.get("video_url_fallback", "")
        _media = media_map.get(row["note_id"], ("", False))

        _pg_upsert(pg_conn, "xhs_notes", {
            "note_id": row["note_id"],
            "user_id": row["user_id"] or "",
            "title": row["title"] or "",
            "description": row["description"] or "",
            "content": enriched.get("content", row["description"] or ""),
            "type": row["type"] or "",
            "liked_count": row["liked_count"] or 0,
            "collected_count": row["collected_count"] or 0,
            "comment_count": row["comment_count"] or 0,
            "share_count": row["share_count"] or 0,
            "ip_location": row["ip_location"] or "",
            "topics": _sqlite_json(row["topics"]),
            "published_at": row["published_at"] or "",
            "xsec_token": row["xsec_token"] or "",
            "xsec_source": row["xsec_source"] or "",
            "video_url": video_url,
            "cover_url": row["cover_url"] or "",
            "video_duration": row["video_duration"] or 0,
            "video_transcript": row["video_transcript"] or "",
            "video_ocr_text": row["video_ocr_text"] or "",
            "video_summary": row["video_summary"] or "",
            "image_ocr_text": row["image_ocr_text"] or "",
            "image_summary": row["image_summary"] or "",
            "image_mermaid": row["image_mermaid"] or "",
            "image_urls": Json(enriched.get("image_urls", [])),
            "note_url": enriched.get("note_url", f"https://www.xiaohongshu.com/explore/{row['note_id']}"),
            "content_hash": row["content_hash"] or "",
            "raw_data": _sqlite_json(row["raw_json"], context=f"note {row['note_id']}"),
            "status": "ready",
            "media_path": _media[0],
            "has_local_media": _media[1],
            "updated_at": now,
            "crawled_at": row["crawled_at"] or now,
        }, ["note_id"])
        synced += 1

    # 同步 users
    for row in sq.execute("SELECT * FROM users"):
        _pg_upsert(pg_conn, "xhs_users", {
            "user_id": row["user_id"],
            "nickname": row["nickname"] or "",
            "avatar": row["avatar"] or "",
            "description": row["description"] or "",
            "fans_count": row["fans_count"] or 0,
            "follow_count": row["follow_count"] or 0,
            "notes_count": row["notes_count"] or 0,
            "location": row["location"] or "",
            "raw_data": _sqlite_json(row["raw_json"], context=f"user {row['user_id']}"),
            "status": "ready",
            "updated_at": now,
            "crawled_at": row["crawled_at"] or now,
        }, ["user_id"])
        synced += 1

    # 同步 comments
    for row in sq.execute("SELECT * FROM comments"):
        _pg_upsert(pg_conn, "xhs_comments", {
            "comment_id": row["comment_id"],
            "note_id": row["note_id"] or "",
            "parent_id": row["parent_id"] or "",
            "user_id": row["user_id"] or "",
            "nickname": row["nickname"] or "",
            "content": row["content"] or "",
            "like_count": row["like_count"] or 0,
            "ip_location": row["ip_location"] or "",
            "pictures": _sqlite_json(row["pictures_json"], context=f"comment {row['comment_id']}"),
            "target_comment_id": row["target_comment_id"] or "",
            "created_at": row["created_at"] or "",
            "raw_data": _sqlite_json(row["raw_json"], context=f"comment {row['comment_id']}"),
            "status": "ready",
            "updated_at": now,
            "crawled_at": row["crawled_at"] or now,
        }, ["comment_id"])
        synced += 1

    # 同步 search_cache
    for row in sq.execute("SELECT * FROM search_cache"):
        _pg_upsert(pg_conn, "xhs_search_cache", {
            "keyword": row["keyword"],
            "page": row["page"],
            "note_ids": _sqlite_json(row["note_ids_json"]),
        }, ["keyword", "page"])
        synced += 1

    # 同步 crawl_state
    for row in sq.execute("SELECT * FROM crawl_state"):
        _pg_upsert(pg_conn, "xhs_crawl_state", {
            "task_id": row["task_id"],
            "task_type": row["task_type"] or "",
            "target_id": row["target_id"] or "",
            "cursor": row["cursor"] or "",
            "status": row["status"] or "",
            "last_error": row["last_error"] or "",
        }, ["task_id"])
        synced += 1

    # 商户索引（老 SQLite 数据库没有这些表时跳过，升级后 connect 会自动创建）。
    if _sqlite_has_table(sq, "merchant_categories"):
        for row in sq.execute("SELECT * FROM merchant_categories"):
            _pg_upsert(pg_conn, "merchant_categories", {
                "category_id": row["category_id"],
                "parent_id": row["parent_id"] or "",
                "name": row["name"] or "",
                "description": row["description"] or "",
                "aliases": _sqlite_json(row["aliases"]),
                "sort_order": row["sort_order"] or 0,
                "status": row["status"] or "active",
                "taxonomy_version": row["taxonomy_version"] or "v1",
                "created_at": row["created_at"] or now,
                "updated_at": row["updated_at"] or now,
            }, ["category_id"])
            synced += 1

    if _sqlite_has_table(sq, "merchants"):
        for row in sq.execute("SELECT * FROM merchants"):
            _pg_upsert(pg_conn, "merchants", {
                "merchant_id": row["merchant_id"],
                "canonical_name": row["canonical_name"] or "",
                "aliases": _sqlite_json(row["aliases"]),
                "city": row["city"] or "",
                "district": row["district"] or "",
                "cuisine": _sqlite_json(row["cuisine"]),
                "price_min": row["price_min"],
                "price_max": row["price_max"],
                "currency": row["currency"] or "",
                "price_basis": row["price_basis"] or "unknown",
                "reservation_info": row["reservation_info"] or "",
                "status": row["status"] or "uncertain",
                "confidence": row["confidence"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "updated_at": row["updated_at"] or now,
            }, ["merchant_id"])
            synced += 1

    if _sqlite_has_table(sq, "merchant_category_links"):
        for row in sq.execute("SELECT * FROM merchant_category_links"):
            _pg_upsert(pg_conn, "merchant_category_links", {
                "merchant_id": row["merchant_id"],
                "category_id": row["category_id"],
                "source_note_id": row["source_note_id"] or "",
                "evidence_text": row["evidence_text"] or "",
                "confidence": row["confidence"],
                "is_primary": bool(row["is_primary"]),
                "taxonomy_version": row["taxonomy_version"] or "v1",
                "created_at": row["created_at"] or now,
            }, ["merchant_id", "category_id", "source_note_id"])
            synced += 1

    if _sqlite_has_table(sq, "merchant_notes"):
        for row in sq.execute("SELECT * FROM merchant_notes"):
            _pg_upsert(pg_conn, "merchant_notes", {
                "merchant_id": row["merchant_id"],
                "note_id": row["note_id"],
                "relation_type": row["relation_type"],
                "evidence_text": row["evidence_text"] or "",
                "evidence_start": row["evidence_start"],
                "evidence_end": row["evidence_end"],
                "confidence": row["confidence"],
                "created_at": row["created_at"] or now,
            }, ["merchant_id", "note_id", "relation_type"])
            synced += 1

    if _sqlite_has_table(sq, "merchant_extraction_candidates"):
        for row in sq.execute("SELECT * FROM merchant_extraction_candidates"):
            _pg_upsert(pg_conn, "merchant_extraction_candidates", {
                "candidate_id": row["candidate_id"],
                "note_id": row["note_id"],
                "source_content_hash": row["source_content_hash"] or "",
                "candidate_name": row["candidate_name"] or "",
                "aliases": _sqlite_json(row["aliases_json"]),
                "category_candidates": _sqlite_json(row["category_candidates_json"]),
                "city": row["city"] or "",
                "district": row["district"] or "",
                "price": _sqlite_json(row["price_json"]),
                "reservation_info": row["reservation_info"] or "",
                "relation_type": row["relation_type"] or "mentioned",
                "evidence_text": row["evidence_text"] or "",
                "confidence": row["confidence"],
                "model_name": row["model_name"] or "",
                "prompt_version": row["prompt_version"] or "",
                "extractor_version": row["extractor_version"] or "v1",
                "status": row["status"] or "pending",
                "error_message": row["error_message"] or "",
                "raw_response": row["raw_response"] or "",
                "created_at": row["created_at"] or now,
                "updated_at": row["updated_at"] or now,
            }, ["candidate_id"])
            synced += 1

    sq.close()
    logger.info(f"  同步 {synced} rows from SQLite to PG")
    return synced



def main(argv=None) -> None:
    """执行一次完整的 SQLite → PostgreSQL 同步。"""
    parser = argparse.ArgumentParser(description="同步本地 SQLite 数据到 PostgreSQL")
    parser.parse_args(argv)
    conn = get_pg_connection()
    try:
        init_schema(conn)
        count = sync_sqlite_to_pg(conn)
        print(f"同步完成：{count or 0} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
