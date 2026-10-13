"""测试 xhs_storage 模块。"""
import json
from types import SimpleNamespace

import pytest
import psycopg2
import database
import xhs_storage

from xhs_storage import upsert_note, upsert_user, upsert_comment, get_note, save_search_page


class TestUpsertNote:
    def test_insert(self, db_conn, sample_note_item):
        from xhs_api import _normalize_note
        note = _normalize_note(sample_note_item)
        upsert_note(db_conn, note)
        row = get_note(db_conn, note["note_id"])
        assert row is not None
        assert row["note_id"] == "note_abc123"
        assert row["title"] == "测试笔记标题"
        assert row["liked_count"] == 12000

    def test_update_preserves_xsec_token(self, db_conn):
        """xsec_token 在更新时应保留非空旧值。"""
        note_v1 = {
            "note_id": "n1", "user_id": "u1", "title": "V1",
            "xsec_token": "token_v1", "xsec_source": "pc_search",
        }
        upsert_note(db_conn, note_v1)

        note_v2 = {
            "note_id": "n1", "user_id": "u1", "title": "V2",
            "xsec_token": "", "xsec_source": "",
        }
        upsert_note(db_conn, note_v2)

        row = get_note(db_conn, "n1")
        assert row["title"] == "V2"  # 标题更新
        assert row["xsec_token"] == "token_v1"  # token 保留

    def test_update_overwrites_with_nonempty_token(self, db_conn):
        """新 token 非空时应覆盖旧 token。"""
        note_v1 = {"note_id": "n2", "xsec_token": "old_token"}
        upsert_note(db_conn, note_v1)

        note_v2 = {"note_id": "n2", "xsec_token": "new_token"}
        upsert_note(db_conn, note_v2)

        row = get_note(db_conn, "n2")
        assert row["xsec_token"] == "new_token"


class TestUpsertUser:
    def test_insert(self, db_conn, sample_user_info):
        from xhs_api import _normalize_user
        user = _normalize_user(sample_user_info)
        upsert_user(db_conn, user)
        row = db_conn.execute("SELECT * FROM xhs_users WHERE user_id = %s", ("user_001",)).fetchone()
        assert row is not None
        assert row["nickname"] == "测试用户"
        assert row["fans_count"] == 56000


class TestUpsertComment:
    def test_insert(self, db_conn, sample_comment):
        from xhs_api import _normalize_comment
        c = _normalize_comment(sample_comment, "note_abc123")
        upsert_comment(db_conn, c)
        row = db_conn.execute("SELECT * FROM xhs_comments WHERE comment_id = %s", ("comment_001",)).fetchone()
        assert row is not None
        assert row["content"] == "这是评论内容"
        assert row["like_count"] == 42


class TestSearchCache:
    def test_save_and_read(self, db_conn):
        save_search_page(db_conn, "测试关键词", 1, ["n1", "n2", "n3"])
        row = db_conn.execute(
            "SELECT * FROM xhs_search_cache WHERE keyword = %s AND page = %s",
            ("测试关键词", 1),
        ).fetchone()
        assert row is not None
        ids = row["note_ids"]
        assert ids == ["n1", "n2", "n3"]

    def test_update_same_page(self, db_conn):
        save_search_page(db_conn, "香港", 1, ["old"])
        save_search_page(db_conn, "香港", 1, ["new"])
        rows = db_conn.execute("SELECT note_ids FROM xhs_search_cache").fetchall()
        assert [row[0] for row in rows] == [["new"]]


def test_note_is_visible_to_another_pg_connection_and_reader(db_conn, monkeypatch):
    note_id = "quote-'-%-中文"
    upsert_note(db_conn, {
        "note_id": note_id, "title": "探店", "topics": ["餐饮"],
        "raw": {"desc": "完整正文", "image_list": [{"url_default": "https://example.com/image.jpg"}]},
    })
    other = database.connect()
    try:
        note = xhs_storage.get_note(other, note_id)
        assert note["content"] == "完整正文"
        assert note["topics"] == ["餐饮"]
        assert note["raw_data"]["desc"] == "完整正文"
        assert note["image_urls"] == ["https://example.com/image.jpg"]
        assert note["note_url"].endswith(note_id)
    finally:
        other.close()
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "web/backend"))
    import main
    detail = main.get_note(note_id)
    assert detail["content"] == "完整正文"
    assert detail["topics"] == ["餐饮"]


def test_query_connection_enforces_readonly(db_conn):
    readonly = database.connect(readonly=True)
    try:
        assert readonly.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            readonly.execute("INSERT INTO xhs_notes (note_id) VALUES ('forbidden')")
    finally:
        readonly.close()


def test_download_updates_media_metadata_directly(db_conn, tmp_path, monkeypatch):
    import xhs_config
    import xhs_media
    monkeypatch.setattr(xhs_config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(xhs_storage, "DATA_DIR", tmp_path)
    upsert_user(db_conn, {"user_id": "u1", "nickname": "作者"})
    upsert_note(db_conn, {"note_id": "media", "user_id": "u1", "title": "探店"})
    assert get_note(db_conn, "media")["has_local_media"] is False
    directory = xhs_config.note_media_dir("media", db_conn)
    directory.mkdir(parents=True)
    (directory / "img_01.jpg").write_bytes(b"existing download")
    xhs_media.download_media("media", db_conn, with_video=False)
    note = get_note(db_conn, "media")
    assert note["has_local_media"] is True
    assert note["media_path"] == "media/作者/探店"


def test_incomplete_refresh_preserves_analysis_and_media_urls(db_conn):
    upsert_note(db_conn, {
        "note_id": "n1", "content_hash": "same", "description": "正文",
        "video_transcript": "已转录", "image_ocr_text": "已识别",
        "raw": {"desc": "正文", "image_list": [{"url_default": "https://example.com/image.jpg"}]},
    })
    before = get_note(db_conn, "n1")
    upsert_note(db_conn, {"note_id": "n1", "content_hash": "same"})
    after = get_note(db_conn, "n1")
    for field in ("content", "raw_data", "image_urls", "video_transcript", "image_ocr_text", "crawled_at"):
        assert after[field] == before[field]


def test_failed_write_rolls_back_and_connection_remains_usable(db_conn):
    with pytest.raises(psycopg2.Error):
        upsert_note(db_conn, {"note_id": "invalid", "raw": {"text": "\x00"}})
    upsert_note(db_conn, {"note_id": "valid"})
    assert get_note(db_conn, "invalid") is None
    assert get_note(db_conn, "valid") is not None


def test_crawl_state_can_resume_after_update(db_conn):
    xhs_storage.update_crawl_state(db_conn, "task-1", "user", "u1", "page-1", "running")
    xhs_storage.update_crawl_state(db_conn, "task-1", "user", "u1", "page-2", "paused", "限流")
    state = xhs_storage.get_crawl_state(db_conn, "task-1")
    assert (state["cursor"], state["status"], state["last_error"]) == ("page-2", "paused", "限流")
    assert db_conn.execute("SELECT COUNT(*) FROM xhs_crawl_state").fetchone()[0] == 1


def test_exports_support_pg_json_and_timestamps(db_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(xhs_storage, "OUTPUT_DIR", tmp_path)
    upsert_note(db_conn, {"note_id": "export", "title": "导出", "topics": ["香港"]})
    xhs_storage.upsert_comment(db_conn, {"comment_id": "c1", "note_id": "export", "content": "评论"})
    path = xhs_storage.write_json(db_conn, path=tmp_path / "notes.json")
    data = json.loads(path.read_text())
    assert data[0]["topics"] == ["香港"]
    assert isinstance(data[0]["raw_data"], dict)
    files = xhs_storage.write_markdown_files(db_conn, "export")
    assert "comments.md" in {file.name for file in files}
    openpyxl = pytest.importorskip("openpyxl")
    path = xhs_storage.write_xlsx(db_conn, path=tmp_path / "notes.xlsx")
    workbook = openpyxl.load_workbook(path)
    assert workbook["Notes"].max_row == 2
    assert workbook["Comments"].max_row == 2
    workbook.close()


def test_cleanup_preview_and_execution(db_conn, tmp_path, monkeypatch):
    from xhs import cmd_cleanup
    import xhs_config
    monkeypatch.setattr(xhs_config, "MEDIA_DIR", tmp_path)
    save_search_page(db_conn, "empty", 1, [])
    save_search_page(db_conn, "keep", 1, ["n1"])
    xhs_storage.update_crawl_state(db_conn, "old", "user", "u1", "", "completed")
    xhs_storage.update_crawl_state(db_conn, "recent", "user", "u1", "", "completed")
    db_conn.execute("UPDATE xhs_crawl_state SET updated_at=NOW()-INTERVAL '10 days' WHERE task_id='old'")
    db_conn.commit()
    args = SimpleNamespace(dry_run=True, max_cache_days=30, max_state_days=7, vacuum=True)
    assert cmd_cleanup(args) == 0
    assert db_conn.execute("SELECT COUNT(*) FROM xhs_search_cache").fetchone()[0] == 2
    db_conn.commit()
    args.dry_run = False
    assert cmd_cleanup(args) == 0
    assert db_conn.execute("SELECT keyword FROM xhs_search_cache").fetchone()[0] == "keep"
    assert [row[0] for row in db_conn.execute("SELECT task_id FROM xhs_crawl_state")] == ["recent"]
