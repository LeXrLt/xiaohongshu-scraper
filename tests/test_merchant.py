"""商户候选数据结构和证据校验。"""

import json

from merchant import MerchantAgentConfig, save_candidate, validate_candidate


def _note(conn):
    conn.execute(
        """INSERT INTO notes
           (note_id, title, description, content, content_hash, topics)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("note-merchant", "Paper Moon 探店", "正文", "Paper Moon 人均 500", "hash-1", "[]"),
    )
    conn.execute("INSERT INTO merchant_categories(category_id, name) VALUES (?, ?)", ("restaurant", "餐厅"))
    conn.commit()
    return conn.execute("SELECT * FROM notes WHERE note_id = ?", ("note-merchant",)).fetchone()


def test_merchant_tables_have_no_address_column(db_conn):
    columns = {
        row[1]
        for row in db_conn.execute("PRAGMA table_info(merchants)").fetchall()
    }
    assert "address" not in columns


def test_candidate_requires_source_evidence(db_conn):
    note = _note(db_conn)
    valid = {
        "name": "Paper Moon",
        "category_ids": ["restaurant"],
        "price": {"min": 500, "basis": "per_person"},
        "evidence": ["Paper Moon", "500"],
    }
    assert validate_candidate(valid, "Paper Moon 人均 500", {"restaurant"}) == []
    assert validate_candidate({**valid, "evidence": ["不存在"]}, "Paper Moon 人均 500", {"restaurant"})
    assert any("address" in error for error in validate_candidate(
        {**valid, "address": "不应进入结构"}, "Paper Moon 人均 500", {"restaurant"}
    ))


def test_candidate_write_is_idempotent(db_conn):
    note = _note(db_conn)
    config = MerchantAgentConfig(
        provider="openai", api_key="", base_url="", model="test", timeout_s=1,
        retries=0, batch_size=1, max_notes=1, extractor_version="v1", prompt_version="v1",
    )
    candidate = {
        "name": "Paper Moon",
        "category_ids": ["restaurant"],
        "price": {"min": 500, "basis": "per_person"},
        "evidence": ["Paper Moon", "500"],
    }
    save_candidate(db_conn, note, candidate, config)
    save_candidate(db_conn, note, candidate, config)
    row = db_conn.execute(
        "SELECT status, aliases_json, price_json FROM merchant_extraction_candidates"
    ).fetchone()
    assert row["status"] == "accepted"
    assert json.loads(row["price_json"])["min"] == 500
    assert db_conn.execute("SELECT COUNT(*) FROM merchant_extraction_candidates").fetchone()[0] == 1
