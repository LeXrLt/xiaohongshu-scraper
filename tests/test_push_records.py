"""推送记录表 (push_records) 及工具函数测试。"""

from datetime import datetime, timezone, timedelta
import pytest

from database import init_schema
import push_records


def _insert_test_merchant(conn, merchant_id="m-001", name="香港茶餐厅", updated_at=None):
    if updated_at:
        conn.execute(
            """INSERT INTO merchants (merchant_id, canonical_name, updated_at)
               VALUES (%s, %s, %s)""",
            (merchant_id, name, updated_at),
        )
    else:
        conn.execute(
            """INSERT INTO merchants (merchant_id, canonical_name)
               VALUES (%s, %s)""",
            (merchant_id, name),
        )
    conn.commit()


def _insert_test_concert(conn, concert_id="c-001", title="周杰伦演唱会", updated_at=None, source_hash="hash-1"):
    if updated_at:
        conn.execute(
            """INSERT INTO ticketmaster_concerts (concert_id, title, updated_at, source_hash)
               VALUES (%s, %s, %s, %s)""",
            (concert_id, title, updated_at, source_hash),
        )
    else:
        conn.execute(
            """INSERT INTO ticketmaster_concerts (concert_id, title, source_hash)
               VALUES (%s, %s, %s)""",
            (concert_id, title, source_hash),
        )
    conn.commit()


def test_push_records_table_structure(db_conn):
    """验证 push_records 表及字段完整存在。"""
    init_schema(db_conn)
    cols = {
        row[0]
        for row in db_conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'push_records'"
        ).fetchall()
    }
    expected = {
        "record_id",
        "entity_type",
        "entity_id",
        "channel",
        "push_status",
        "content_hash",
        "entity_updated_at",
        "last_pushed_at",
        "retry_count",
        "error_message",
        "payload_snapshot",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(cols)


def test_merchant_push_lifecycle(db_conn):
    """测试商户推送生命周期：无记录即未推 -> 推送成功 -> 状态更新。"""
    init_schema(db_conn)
    _insert_test_merchant(db_conn, "m-101", "兰芳园")

    # 1. 初始状态：未推送过
    assert push_records.get_push_record(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-101") is None
    assert not push_records.is_pushed(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-101")

    unpushed = push_records.find_unpushed_entities(db_conn, push_records.ENTITY_TYPE_MERCHANT)
    assert any(m["merchant_id"] == "m-101" for m in unpushed)

    # 2. 标记成功推送
    cur_merchant = db_conn.execute("SELECT * FROM merchants WHERE merchant_id = 'm-101'").fetchone()
    push_records.mark_push_success(
        db_conn,
        entity_type=push_records.ENTITY_TYPE_MERCHANT,
        entity_id="m-101",
        entity_updated_at=cur_merchant["updated_at"],
        payload_snapshot={"title": "兰芳园", "tags": ["茶餐厅"]},
    )

    # 3. 校验已推送
    record = push_records.get_push_record(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-101")
    assert record is not None
    assert record["push_status"] == push_records.STATUS_SUCCESS
    assert record["retry_count"] == 0
    assert record["payload_snapshot"]["title"] == "兰芳园"
    assert push_records.is_pushed(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-101")

    # 4. 未推送列表不再包含 m-101
    unpushed_after = push_records.find_unpushed_entities(db_conn, push_records.ENTITY_TYPE_MERCHANT)
    assert not any(m["merchant_id"] == "m-101" for m in unpushed_after)


def test_concert_push_lifecycle(db_conn):
    """测试演唱会推送生命周期。"""
    init_schema(db_conn)
    _insert_test_concert(db_conn, "c-201", "Taylor Swift The Eras Tour", source_hash="hash-taylor")

    # 未推送
    assert push_records.get_push_record(db_conn, push_records.ENTITY_TYPE_CONCERT, "c-201") is None
    unpushed = push_records.find_unpushed_entities(db_conn, push_records.ENTITY_TYPE_CONCERT)
    assert any(c["concert_id"] == "c-201" for c in unpushed)

    # 标记成功
    push_records.mark_push_success(
        db_conn,
        entity_type=push_records.ENTITY_TYPE_CONCERT,
        entity_id="c-201",
        content_hash="hash-taylor",
    )
    assert push_records.is_pushed(db_conn, push_records.ENTITY_TYPE_CONCERT, "c-201")

    unpushed_after = push_records.find_unpushed_entities(db_conn, push_records.ENTITY_TYPE_CONCERT)
    assert not any(c["concert_id"] == "c-201" for c in unpushed_after)


def test_find_updated_entities(db_conn):
    """测试检测已推送过但数据被后续更新的实体。"""
    init_schema(db_conn)
    t0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)

    _insert_test_merchant(db_conn, "m-update", "九记牛腩", updated_at=t0)

    # 先以 t0 状态成功推送
    push_records.mark_push_success(
        db_conn,
        entity_type=push_records.ENTITY_TYPE_MERCHANT,
        entity_id="m-update",
        entity_updated_at=t0,
    )

    # 此时不应出现在更新列表中
    updated_list = push_records.find_updated_entities(db_conn, push_records.ENTITY_TYPE_MERCHANT)
    assert not any(m["merchant_id"] == "m-update" for m in updated_list)

    # 数据被更新，updated_at 变为 t1
    db_conn.execute("UPDATE merchants SET canonical_name = '九记牛腩(总店)', updated_at = %s WHERE merchant_id = 'm-update'", (t1,))
    db_conn.commit()

    # 现在应该被识别为已更新！
    updated_list_after = push_records.find_updated_entities(db_conn, push_records.ENTITY_TYPE_MERCHANT)
    assert any(m["merchant_id"] == "m-update" for m in updated_list_after)

    # 再次推送并刷新时间
    push_records.mark_push_success(
        db_conn,
        entity_type=push_records.ENTITY_TYPE_MERCHANT,
        entity_id="m-update",
        entity_updated_at=t1,
    )

    # 恢复无待推更新状态
    assert not any(m["merchant_id"] == "m-update" for m in push_records.find_updated_entities(db_conn, push_records.ENTITY_TYPE_MERCHANT))


def test_push_failure_and_retry(db_conn):
    """测试推送失败记录与重试计数累加。"""
    init_schema(db_conn)
    _insert_test_merchant(db_conn, "m-err", "测试失败商户")

    # 第一次失败
    push_records.mark_push_failed(
        db_conn,
        entity_type=push_records.ENTITY_TYPE_MERCHANT,
        entity_id="m-err",
        error_message="Webhook 429 Too Many Requests",
    )
    rec1 = push_records.get_push_record(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-err")
    assert rec1["push_status"] == push_records.STATUS_FAILED
    assert rec1["retry_count"] == 1
    assert "429" in rec1["error_message"]

    # 第二次失败重试
    push_records.mark_push_failed(
        db_conn,
        entity_type=push_records.ENTITY_TYPE_MERCHANT,
        entity_id="m-err",
        error_message="Webhook 500 Internal Error",
    )
    rec2 = push_records.get_push_record(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-err")
    assert rec2["retry_count"] == 2
    assert "500" in rec2["error_message"]


def test_channel_isolation(db_conn):
    """测试多渠道隔离性：飞书与企业微信渠道互不干扰。"""
    init_schema(db_conn)
    _insert_test_merchant(db_conn, "m-channel", "多渠道测试店")

    # 仅向 feishu 渠道推送成功
    push_records.mark_push_success(
        db_conn,
        entity_type=push_records.ENTITY_TYPE_MERCHANT,
        entity_id="m-channel",
        channel="feishu",
    )

    # feishu 渠道：已推送
    assert push_records.is_pushed(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-channel", channel="feishu")

    # wechat 渠道：未推送过
    assert push_records.get_push_record(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-channel", channel="wechat") is None
    assert not push_records.is_pushed(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-channel", channel="wechat")
    unpushed_wechat = push_records.find_unpushed_entities(db_conn, push_records.ENTITY_TYPE_MERCHANT, channel="wechat")
    assert any(m["merchant_id"] == "m-channel" for m in unpushed_wechat)


def test_schema_reinit_preserves_records(db_conn):
    """测试重复执行 init_schema 不会清空已有的推送记录。"""
    init_schema(db_conn)
    _insert_test_merchant(db_conn, "m-persist", "持久化测试店")
    push_records.mark_push_success(
        db_conn,
        entity_type=push_records.ENTITY_TYPE_MERCHANT,
        entity_id="m-persist",
        payload_snapshot={"hello": "world"},
    )

    # 重复执行两次 init_schema
    init_schema(db_conn)
    init_schema(db_conn)

    rec = push_records.get_push_record(db_conn, push_records.ENTITY_TYPE_MERCHANT, "m-persist")
    assert rec is not None
    assert rec["push_status"] == push_records.STATUS_SUCCESS
    assert rec["payload_snapshot"] == {"hello": "world"}
