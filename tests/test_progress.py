"""真实 PG 下验证重复运行、中断、旧断点迁移和阶段缓存。"""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

import database
import merchant
import xhs
import xhs_image
import xhs_storage
import xhs_video


def config():
    return merchant.MerchantAgentConfig("openai", "", "", "test", 1, 0, 1, 1, "v1", "v1")


def note(conn, nid="n1", text="Paper Moon 人均 500"):
    xhs_storage.upsert_note(conn, {"note_id": nid, "title": nid, "description": text})
    return xhs_storage.get_note(conn, nid)


def candidate(name="Paper Moon"):
    return {"name": name, "evidence": [name]}


def test_extract_advances_batches_and_remembers_empty_result(db_conn, monkeypatch):
    note(db_conn, "n1")
    note(db_conn, "n2")
    calls = []
    def agent(row, *_):
        calls.append(row["note_id"])
        return {"merchants": []}, "{}"
    monkeypatch.setattr(merchant, "call_agent", agent)
    for _ in range(3):
        merchant.extract_notes(db_conn, merchant._load_notes(db_conn, None, 1), config())
    assert calls == ["n1", "n2"]
    assert db_conn.execute("SELECT COUNT(*) FROM merchant_extraction_state WHERE status='completed'").fetchone()[0] == 2
    # 改模型或版本不会无意间从头花费模型调用。
    newer = replace(config(), model="new-model", extractor_version="v2")
    merchant.extract_notes(db_conn, [xhs_storage.get_note(db_conn, "n1")], newer)
    assert len(calls) == 2
    merchant.extract_notes(db_conn, [xhs_storage.get_note(db_conn, "n1")], newer, force=True)
    assert len(calls) == 3


def test_failed_note_rolls_back_partial_candidates_then_retries(db_conn, monkeypatch):
    row = note(db_conn)
    replies = iter([
        {"merchants": [candidate(), {**candidate(), "name": "500", "confidence": "bad"}]},
        {"merchants": [candidate()]},
    ])
    monkeypatch.setattr(merchant, "call_agent", lambda *_: (next(replies), "{}"))
    result = merchant.extract_notes(db_conn, [row], config())
    assert result["failed"] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM merchant_extraction_candidates").fetchone()[0] == 0
    assert db_conn.execute("SELECT status FROM merchant_extraction_state").fetchone()[0] == "failed"
    assert merchant.extract_notes(db_conn, merchant._load_notes(db_conn, None, 1), config())["accepted"] == 1
    assert merchant.promote_candidates(db_conn)["promoted"] == 0


def test_interrupt_rolls_back_note_and_releases_lock(db_conn, monkeypatch):
    row = note(db_conn)
    monkeypatch.setattr(merchant, "call_agent", lambda *_: ({"merchants": [candidate(), candidate("500")]}, "{}"))
    original = merchant.save_candidate
    calls = []
    def save(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return original(*args, **kwargs)
    monkeypatch.setattr(merchant, "save_candidate", save)
    with pytest.raises(KeyboardInterrupt):
        merchant.extract_notes(db_conn, [row], config())
    assert db_conn.execute("SELECT COUNT(*) FROM merchant_extraction_candidates").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM merchant_extraction_state").fetchone()[0] == 0
    other = database.connect()
    try:
        assert other.execute("SELECT pg_try_advisory_lock(hashtextextended('merchant-extract:n1', 0))").fetchone()[0]
    finally:
        other.close()


def test_concurrent_extractor_skips_claimed_note(db_conn, monkeypatch):
    row = note(db_conn)
    other = database.connect()
    try:
        other.execute("SELECT pg_advisory_lock(hashtextextended('merchant-extract:n1', 0))")
        monkeypatch.setattr(merchant, "call_agent", lambda *_: pytest.fail("不应并发调用模型"))
        assert merchant.extract_notes(db_conn, [row], config())["skipped"] == 1
    finally:
        other.close()


def test_legacy_candidates_are_used_as_progress(db_conn):
    row = note(db_conn)
    merchant.save_candidate(db_conn, row, candidate(), config())
    assert merchant._load_notes(db_conn, None, 10) == []
    assert merchant.promote_candidates(db_conn)["promoted"] == 1
    before = dict(db_conn.execute("SELECT * FROM merchants").fetchone())
    merchant.save_candidate(db_conn, row, candidate(), config())
    assert merchant.promote_candidates(db_conn)["promoted"] == 0
    assert dict(db_conn.execute("SELECT * FROM merchants").fetchone()) == before


def test_aliases_resolve_to_existing_merchant(db_conn):
    first = note(db_conn, "n1", "唐阁 Tang Court")
    second = note(db_conn, "n2", "唐阁")
    merchant.save_candidate(db_conn, first, {**candidate("唐阁 Tang Court"), "aliases": ["唐阁", "Tang Court"]}, config())
    merchant.promote_candidates(db_conn)
    merchant.save_candidate(db_conn, second, candidate("唐阁"), config())
    merchant.promote_candidates(db_conn)
    assert db_conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM merchant_notes").fetchone()[0] == 2


def test_dedupe_backs_up_and_preserves_source_links(db_conn, tmp_path):
    note(db_conn)
    for mid, name, aliases in [("a", "唐阁 Tang Court", ["唐阁"]), ("b", "唐阁", [])]:
        db_conn.execute("INSERT INTO merchants(merchant_id, canonical_name, aliases) VALUES (%s,%s,%s)", (mid, name, json.dumps(aliases)))
        db_conn.execute("INSERT INTO merchant_notes(merchant_id,note_id,relation_type,evidence_text) VALUES (%s,'n1','mentioned',%s)", (mid, name))
    db_conn.commit()
    assert len(merchant.deduplicate_merchants(db_conn)) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 2
    backup = tmp_path / "backup.json"
    merchant.deduplicate_merchants(db_conn, backup)
    assert len(json.loads(backup.read_text())["merchants"]) == 2
    assert db_conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 1
    assert "唐阁 Tang Court" in db_conn.execute("SELECT evidence_text FROM merchant_notes").fetchone()[0]
    assert merchant.deduplicate_merchants(db_conn) == []


@pytest.fixture
def crawl_session(db_conn, monkeypatch):
    @contextmanager
    def session(_):
        yield SimpleNamespace(account=SimpleNamespace(alias="rotated")), db_conn
    monkeypatch.setattr(xhs, "fetch_session", session)
    monkeypatch.setattr(xhs_storage, "write_markdown", lambda *_: None)
    return SimpleNamespace(keyword="香港", max_pages=1, pages=1, restart=False, download=False, analyze=False)


def test_search_continues_past_per_run_page_budget(db_conn, crawl_session, monkeypatch):
    pages = []
    def search(*_, page):
        pages.append(page)
        return {"items": [{"model_type": "note", "id": f"n{page}", "note_card": {"title": f"page {page}"}}], "has_more": page < 3}
    monkeypatch.setattr(xhs, "fetch_search", search)
    for _ in range(4):
        assert xhs.cmd_crawl_search(crawl_session) == 0
    assert pages == [1, 2, 3]
    assert xhs_storage.get_crawl_state(db_conn, "search:香港")["status"] == "completed"


def test_search_migrates_account_scoped_cursor(db_conn, crawl_session, monkeypatch):
    xhs_storage.update_crawl_state(db_conn, "search:香港:old-account", "search", "香港", "8", "completed")
    pages = []
    monkeypatch.setattr(xhs, "fetch_search", lambda *_, page: pages.append(page) or {"items": []})
    xhs.cmd_crawl_search(crawl_session)
    assert pages == [8]


def test_search_replays_interrupted_page_then_advances(db_conn, crawl_session, monkeypatch):
    data = {"items": [{"model_type": "note", "id": "n1", "note_card": {"title": "笔记"}}], "has_more": True}
    monkeypatch.setattr(xhs, "fetch_search", lambda *_, **__: data)
    monkeypatch.setattr(xhs.xhs_media, "post_process_note", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        xhs.cmd_crawl_search(crawl_session)
    assert xhs_storage.get_crawl_state(db_conn, "search:香港") is None
    monkeypatch.setattr(xhs.xhs_media, "post_process_note", lambda *_: None)
    xhs.cmd_crawl_search(crawl_session)
    assert xhs_storage.get_crawl_state(db_conn, "search:香港")["cursor"] == "2"
    assert db_conn.execute("SELECT COUNT(*) FROM xhs_notes").fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["user", "feed"])
def test_terminal_empty_cursor_does_not_restart(db_conn, crawl_session, monkeypatch, kind):
    crawl_session.user_id = "u1"
    crawl_session.category = "recommend"
    target = "u1" if kind == "user" else "recommend"
    xhs_storage.update_crawl_state(db_conn, f"{kind}:{target}", kind, target, "", "completed")
    monkeypatch.setattr(xhs, "fetch_user_notes", lambda *_: pytest.fail("已完成任务不应重新请求"))
    monkeypatch.setattr(xhs, "fetch_feed", lambda *_: pytest.fail("已完成任务不应重新请求"))
    assert xhs._crawl_notes(crawl_session, kind) == 0


def test_run_resumes_in_middle_of_page(db_conn, crawl_session, monkeypatch):
    crawl_session.max_notes = 1
    calls = []
    def search(*_, page):
        calls.append(page)
        return {"items": [{"model_type": "note", "id": f"n{i}", "note_card": {"title": str(i)}} for i in range(3)], "has_more": False}
    monkeypatch.setattr(xhs, "fetch_search", search)
    for _ in range(4):
        xhs.cmd_crawl_search(crawl_session)
    assert calls == [1, 1, 1]
    assert db_conn.execute("SELECT COUNT(*) FROM xhs_notes").fetchone()[0] == 3


def test_comments_subcomments_resume_independently(db_conn, crawl_session, monkeypatch):
    xhs_storage.upsert_note(db_conn, {"note_id": "n1", "xsec_token": "token"})
    args = SimpleNamespace(note_id="n1", max_pages=1, max_sub_pages=1, with_sub=True, restart=False)
    main_calls = []
    sub_calls = []
    def comments(*_, cursor):
        main_calls.append(cursor)
        return {"comments": [{"id": "c1", "content": "主评论", "sub_comment_has_more": True, "sub_comment_cursor": "s1"}], "has_more": False}
    def subs(*_, cursor):
        sub_calls.append(cursor)
        return {"comments": [{"id": "c" + cursor, "content": "子评论"}], "cursor": "s2" if cursor == "s1" else "", "has_more": cursor == "s1"}
    monkeypatch.setattr(xhs, "fetch_comments", comments)
    monkeypatch.setattr(xhs, "fetch_sub_comments", subs)
    for _ in range(3):
        xhs.cmd_comments(args)
    assert main_calls == [""]
    assert sub_calls == ["s1", "s2"]
    assert xhs_storage.count_comments(db_conn, "n1") == (1, 2)


def test_image_ocr_resumes_and_remembers_no_text(tmp_path, monkeypatch):
    paths = [tmp_path / f"img_{i}.jpg" for i in range(3)]
    for i, path in enumerate(paths):
        path.write_bytes(str(i).encode())
    calls = []
    fail = [True]
    def ocr(path):
        calls.append(path)
        if path == str(paths[1]) and fail[0]:
            raise RuntimeError("temporary failure")
        return (None, None) if path == str(paths[0]) else ([[None, "text"]], None)
    monkeypatch.setattr(xhs_image, "_get_ocr_engine", lambda: ocr)
    cache = tmp_path / "cache.json"
    xhs_image._do_ocr(paths, cache)
    fail[0] = False
    xhs_image._do_ocr(paths, cache)
    xhs_image._do_ocr(paths, cache)
    assert calls.count(str(paths[0])) == 1
    assert calls.count(str(paths[1])) == 2
    assert calls.count(str(paths[2])) == 1


def test_video_preserves_and_reuses_empty_successful_stage_cache(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    cache = tmp_path / "_cache"
    cache.mkdir()
    (cache / "audio.wav").write_bytes(b"audio")
    (cache / "transcript.json").write_text('{"text":"","segments":[],"duration":1}')
    (cache / "ocr.json").write_text('[]')
    monkeypatch.setattr(xhs_video, "has_ffmpeg", lambda: False)
    monkeypatch.setattr(xhs_video, "transcribe", lambda *_: pytest.fail("缓存成功空结果不应重做"))
    monkeypatch.setattr(xhs_video, "ocr_frames", lambda *_: pytest.fail("缓存成功空结果不应重做"))
    xhs_video.analyze_video(video, {"correct_mode": "none"})
    xhs_video.analyze_video(video, {"correct_mode": "none"})
    assert (cache / "transcript.json").exists()
    assert (cache / "ocr.json").exists()


def test_partial_analysis_cannot_erase_existing_or_manual_corrections(db_conn):
    note(db_conn)
    xhs_storage.update_video_analysis(db_conn, "n1", transcript="人工纠错", ocr_text="OCR", summary="agent纠错")
    xhs_storage.update_video_analysis(db_conn, "n1", transcript="旧缓存转录", ocr_text="[]", summary="")
    row = xhs_storage.get_note(db_conn, "n1")
    assert (row["video_transcript"], row["video_ocr_text"], row["video_summary"]) == ("人工纠错", "OCR", "agent纠错")
    xhs_storage.update_image_analysis(db_conn, "n1", ocr_text="文字", summary="摘要")
    xhs_storage.update_image_analysis(db_conn, "n1", ocr_text="", summary="")
    row = xhs_storage.get_note(db_conn, "n1")
    assert (row["image_ocr_text"], row["image_summary"]) == ("文字", "摘要")


def test_video_correction_no_change_is_completed_once(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    cache = tmp_path / "_cache"
    cache.mkdir()
    (cache / "transcript.json").write_text('{"text":"已正确","segments":[],"duration":1}')
    (cache / "ocr.json").write_text('[{"text":"已正确"}]')
    monkeypatch.setattr(xhs_video, "has_ffmpeg", lambda: False)
    calls = []
    monkeypatch.setattr(xhs_video, "_correct_transcript", lambda text, *_: calls.append(text) or text)
    xhs_video.analyze_video(video, {"correct_mode": "openai"})
    xhs_video.analyze_video(video, {"correct_mode": "openai"})
    assert calls == ["已正确"]


def test_frame_ocr_resumes_after_interruption(tmp_path, monkeypatch):
    frames = [tmp_path / f"frame_{i}.jpg" for i in range(3)]
    for i, frame in enumerate(frames):
        frame.write_bytes(str(i).encode())
    monkeypatch.setattr(xhs_video, "_get_ocr", lambda: object())
    calls = []
    interrupted = [True]
    def single(path, _):
        calls.append(path.name)
        if path == frames[1] and interrupted[0]:
            raise KeyboardInterrupt
        return None  # 成功识别，无文字。
    monkeypatch.setattr(xhs_video, "_ocr_single", single)
    cache = tmp_path / "frame_ocr.json"
    with pytest.raises(KeyboardInterrupt):
        xhs_video.ocr_frames(frames, cache_path=cache)
    interrupted[0] = False
    assert xhs_video.ocr_frames(frames, cache_path=cache) == []
    assert calls == [frames[0].name, frames[1].name, frames[1].name, frames[2].name]


def test_import_empty_result_remembers_progress(db_conn, tmp_path, monkeypatch):
    note(db_conn)
    path = tmp_path / "empty.json"
    path.write_text('{"note_id":"n1","merchants":[]}')
    merchant.import_candidates(db_conn, path, config())
    assert merchant._load_notes(db_conn, None, 10) == []
    monkeypatch.setattr(merchant, "call_agent", lambda *_: pytest.fail("导入的空结果也算完成"))
    assert merchant.extract_notes(db_conn, [xhs_storage.get_note(db_conn, "n1")], config())["skipped"] == 1


def test_completed_extraction_survives_promotion_failure(db_conn, monkeypatch):
    row = note(db_conn)
    calls = []
    monkeypatch.setattr(merchant, "call_agent", lambda *_: (calls.append(1) or {"merchants": [candidate()]}, "{}"))
    original = merchant.promote_candidates
    monkeypatch.setattr(merchant, "promote_candidates", lambda *_: (_ for _ in ()).throw(RuntimeError("晋级中断")))
    with pytest.raises(RuntimeError):
        merchant.extract_notes(db_conn, [row], config())
    monkeypatch.setattr(merchant, "promote_candidates", original)
    result = merchant.extract_notes(db_conn, merchant._load_notes(db_conn, None, 10), config())
    assert result["promoted"] == 1
    assert calls == [1]


def test_media_paths_are_unique_and_survive_title_changes(db_conn, tmp_path, monkeypatch):
    import xhs_config
    monkeypatch.setattr(xhs_config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(xhs_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(xhs_storage, "DATA_DIR", tmp_path)
    for nid in ("n1", "n2"):
        xhs_storage.upsert_note(db_conn, {"note_id": nid, "user_id": "u1", "title": "同一个标题"})
    first = xhs_config.note_media_dir("n1", db_conn)
    assert first != xhs_config.note_media_dir("n2", db_conn)
    first.mkdir(parents=True)
    (first / "img_01.jpg").write_bytes(b"downloaded")
    with db_conn:
        xhs_storage.update_media_state(db_conn, "n1")
    xhs_storage.upsert_note(db_conn, {"note_id": "n1", "user_id": "u1", "title": "新标题"})
    assert xhs_config.note_media_dir("n1", db_conn) == first


def test_refresh_unchanged_note_moves_to_queue_tail(db_conn, crawl_session, monkeypatch):
    from xhs_api import _normalize_note
    item = {"id": "n1", "note_card": {"title": "同一内容"}}
    xhs_storage.upsert_note(db_conn, {**_normalize_note(item), "xsec_token": "token"})
    db_conn.execute("UPDATE xhs_notes SET crawled_at=NOW()-INTERVAL '10 days'")
    db_conn.commit()
    calls = []
    monkeypatch.setattr(xhs, "fetch_note_detail", lambda *_args, **_kwargs: calls.append(1) or item)
    args = SimpleNamespace(max_age_hours=1, limit=1)
    xhs.cmd_refresh(args)
    xhs.cmd_refresh(args)
    assert calls == [1]


def test_cleanup_keeps_active_search_page_progress(db_conn, crawl_session, tmp_path, monkeypatch):
    import xhs_config
    monkeypatch.setattr(xhs_config, "MEDIA_DIR", tmp_path)
    xhs_storage.save_search_page(db_conn, "香港", 1, ["n1"])
    xhs_storage.update_crawl_state(db_conn, "search:香港", "search", "香港", "1", "running")
    db_conn.execute("UPDATE xhs_search_cache SET crawled_at=NOW()-INTERVAL '60 days'")
    db_conn.commit()
    xhs.cmd_cleanup(SimpleNamespace(dry_run=False, max_cache_days=30, vacuum=False))
    assert db_conn.execute("SELECT note_ids FROM xhs_search_cache").fetchone()[0] == ["n1"]


def test_cli_resumes_by_default_and_restart_is_explicit():
    parser = xhs.build_parser()
    assert parser.parse_args(["crawl-search", "香港"]).resume is True
    assert parser.parse_args(["crawl-search", "香港"]).restart is False
    assert parser.parse_args(["crawl-search", "香港", "--restart"]).restart is True


def test_failed_analysis_retries_after_crawl_is_already_completed(db_conn, crawl_session, monkeypatch):
    crawl_session.analyze = True
    pages = []
    monkeypatch.setattr(xhs, "fetch_search", lambda *_, page: pages.append(page) or {
        "items": [{"model_type": "note", "id": "n1", "note_card": {"title": "笔记"}}], "has_more": False})
    calls = []
    def analyze(*_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary OCR failure")
        return {"ocr_text": "", "completed": True}
    monkeypatch.setattr(xhs_image, "analyze_images", analyze)
    xhs.cmd_crawl_search(crawl_session)
    assert xhs_storage.get_crawl_state(db_conn, "search:香港")["status"] == "completed"
    assert xhs_storage.get_crawl_state(db_conn, "image-analysis:n1")["status"] == "failed"
    xhs.cmd_crawl_search(crawl_session)
    xhs.cmd_crawl_search(crawl_session)
    assert pages == [1]
    assert len(calls) == 2
    assert xhs_storage.get_crawl_state(db_conn, "image-analysis:n1")["status"] == "completed"


def test_download_failure_is_saved_for_later_retry(db_conn, tmp_path, monkeypatch):
    import xhs_media
    row = note(db_conn)
    attempts = []
    def download(*_, **__):
        attempts.append(1)
        return 0, 0, int(len(attempts) == 1), tmp_path
    monkeypatch.setattr(xhs_media, "download_media", download)
    args = SimpleNamespace(download=True, analyze=False)
    xhs_media.post_process_note(dict(row), db_conn, args)
    assert xhs_storage.get_crawl_state(db_conn, "media-download:n1")["status"] == "failed"
    xhs_media.resume_pending(db_conn, args)
    xhs_media.resume_pending(db_conn, args)
    assert len(attempts) == 2
    assert xhs_storage.get_crawl_state(db_conn, "media-download:n1")["status"] == "completed"


def test_cleanup_preserves_author_directory_containing_valid_note(db_conn, tmp_path, monkeypatch):
    import xhs_config
    monkeypatch.setattr(xhs_config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(xhs_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(xhs_storage, "DATA_DIR", tmp_path)
    xhs_storage.upsert_note(db_conn, {"note_id": "n1", "user_id": "u1", "title": "标题"})
    directory = xhs_config.note_media_dir("n1", db_conn)
    directory.mkdir(parents=True)
    downloaded = directory / "img_01.jpg"
    downloaded.write_bytes(b"valid download")
    with db_conn:
        xhs_storage.update_media_state(db_conn, "n1")
    xhs.cmd_cleanup(SimpleNamespace(dry_run=False, max_cache_days=30, vacuum=False))
    assert downloaded.read_bytes() == b"valid download"
