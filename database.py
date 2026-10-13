"""爬虫与阅读器共用的 PostgreSQL 连接和表结构初始化。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extensions import connection
from psycopg2.extras import DictCursor
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.sql"
load_dotenv(ROOT / ".env")


class Connection(connection):
    def execute(self, query, params=None):
        """执行原生 PG SQL，返回支持字段名和位置索引的游标。"""
        cursor = self.cursor(cursor_factory=DictCursor)
        try:
            cursor.execute(query, params)
        except Exception:
            cursor.close()
            raise
        return cursor


def postgres_kwargs(*, readonly: bool = False) -> dict[str, Any]:
    user = os.getenv("POSTGRES_USER", "xhs_reader")
    password = os.getenv("POSTGRES_PASSWORD", "")
    if readonly:
        user = os.getenv("POSTGRES_READONLY_USER", user)
        password = os.getenv("POSTGRES_READONLY_PASSWORD", password)
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "user": user,
        "password": password,
        "dbname": os.getenv("POSTGRES_DB", "xhs_reader"),
        "connect_timeout": 10,
    }


def init_schema(conn: Connection) -> None:
    with conn, conn.cursor() as cursor:
        # 多个爬虫进程首次连接时串行执行幂等 DDL。
        cursor.execute("SELECT pg_advisory_xact_lock(734982106)")
        cursor.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def connect(*, initialize: bool = False, readonly: bool = False) -> Connection:
    conn = psycopg2.connect(connection_factory=Connection, **postgres_kwargs(readonly=readonly))
    try:
        if initialize:
            init_schema(conn)
        if readonly:
            conn.set_session(readonly=True, autocommit=True)
        return conn
    except Exception:
        conn.close()
        raise
