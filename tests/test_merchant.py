"""商户候选数据结构和证据校验。"""

import json

from merchant import MerchantAgentConfig, promote_candidates, save_candidate, validate_candidate
from xhs_storage import _migrate


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


def test_merchant_tables_have_no_address_or_status_column(db_conn):
    columns = {
        row[1]
        for row in db_conn.execute("PRAGMA table_info(merchants)").fetchall()
    }
    assert "address" not in columns
    assert "status" not in columns


def test_remove_legacy_status_preserves_merchant_and_sources(db_conn):
    db_conn.execute("PRAGMA foreign_keys = ON")
    _note(db_conn)
    db_conn.execute("ALTER TABLE merchants ADD COLUMN status TEXT NOT NULL DEFAULT 'uncertain'")
    db_conn.execute(
        "INSERT INTO merchants (merchant_id, canonical_name, price_min) VALUES (?, ?, ?)",
        ("merchant-1", "Paper Moon", 500),
    )
    db_conn.execute(
        """INSERT INTO merchant_notes (merchant_id, note_id, relation_type, evidence_text)
           VALUES (?, ?, ?, ?)""",
        ("merchant-1", "note-merchant", "mentioned", "Paper Moon 人均 500"),
    )
    db_conn.execute(
        """INSERT INTO merchant_category_links (merchant_id, category_id, source_note_id)
           VALUES (?, ?, ?)""",
        ("merchant-1", "restaurant", "note-merchant"),
    )
    before = dict(db_conn.execute("SELECT * FROM merchants").fetchone())
    before.pop("status")
    sources_before = [tuple(row) for row in db_conn.execute("SELECT * FROM merchant_notes")]
    categories_before = [tuple(row) for row in db_conn.execute("SELECT * FROM merchant_category_links")]

    _migrate(db_conn)
    _migrate(db_conn)

    assert dict(db_conn.execute("SELECT * FROM merchants").fetchone()) == before
    assert [tuple(row) for row in db_conn.execute("SELECT * FROM merchant_notes")] == sources_before
    assert [tuple(row) for row in db_conn.execute("SELECT * FROM merchant_category_links")] == categories_before
    assert db_conn.execute("PRAGMA foreign_key_check").fetchall() == []


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


def test_candidate_write_and_promotion_are_idempotent(db_conn):
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

    promote_candidates(db_conn)
    promote_candidates(db_conn)
    merchants = db_conn.execute("SELECT * FROM merchants").fetchall()
    assert len(merchants) == 1
    assert merchants[0]["canonical_name"] == "Paper Moon"
    assert merchants[0]["price_min"] == 500
    assert "status" not in merchants[0].keys()
    source = db_conn.execute("SELECT * FROM merchant_notes").fetchall()
    assert len(source) == 1
    assert source[0]["note_id"] == "note-merchant"
    assert source[0]["merchant_id"] == merchants[0]["merchant_id"]
