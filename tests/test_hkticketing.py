"""HKT 香港快達票演唱会解析、分表幂等和断点测试。"""

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

import hkticketing
import push_records

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def project(token: str, title: str, event_token: str, *, type_name: str = "流行音樂") -> dict:
    return {
        "projectToken": token,
        "projectName": title,
        "projectTypeName": type_name,
        "cityName": "香港",
        "siteName": "亞洲國際博覽館Arena",
        "imgUrl": "https://g-assets.maitix.com/181/example.jpg",
        "projectDetail": (
            "<div><strong><span>主辦：</span></strong>萬星娛樂</div>"
            "<p>節目簡介﹕</p><p>演出介紹</p>"
        ),
        "eventVoList": [{
            "eventToken": event_token,
            "eventCaption": "2026年11月21日 (星期六) 下午5時",
            "priceVoList": [{"price": "688", "sellOut": True}, {"price": "988", "sellOut": False}],
        }],
    }


def list_payload(
    projects: list[dict],
    *,
    next_page: bool = False,
    page: int = 1,
    total_row: int | None = None,
) -> dict:
    return {
        "code": "200",
        "data": {
            "dataList": projects,
            "page": page,
            "pageSize": 10,
            "totalPage": 2 if next_page else 1,
            "totalRow": len(projects) if total_row is None else total_row,
            "nextPage": next_page,
        },
        "success": True,
    }


def test_category_and_project_filters_only_keep_concerts():
    category_id = hkticketing.resolve_concert_category({
        "data": [{"projectTypeId": "133002", "projectTypeName": "演唱會"}],
    })
    assert category_id == "133002"
    rows, metadata = hkticketing.parse_project_list({
        **list_payload([
            project("p1", "演唱會一", "e1"),
            project("parking", "場館泊車證", "e2", type_name="停車場"),
        ]),
    })
    assert [row["projectToken"] for row in rows] == ["p1"]
    assert metadata["nextPage"] is False


def test_hkt_signature_matches_page_request_contract():
    params = {
        "page": 1,
        "pageSize": 12,
        "projectClass": "133002",
        "city": "",
        "startTime": "",
        "endTime": "",
        "projectName": "",
        "areaId": "",
        "langType": "3",
    }
    expected = "0be4b9cb64975caa398d5994b6d43f4d"
    assert hkticketing._hkt_signature(params, 1793956488767) == expected
    assert hkticketing._hkt_signature({**params, "langType": "999"}, 1793956488767) == expected


def test_incomplete_first_page_is_rejected_before_completion(db_conn):
    projects = [
        project("p1", "HKT One", "e1"),
        project("p2", "HKT Two", "e2"),
    ]

    class IncompleteClient(_FakeClient):
        def fetch_categories(self):
            return {
                "data": [{
                    "projectTypeId": "133002",
                    "projectTypeName": "演唱會",
                    "saleProjectCount": 3,
                }],
            }

        def fetch_projects(self, *, page, page_size, category_id):
            return list_payload(projects, total_row=2)

    with pytest.raises(RuntimeError, match="列表总数校验失败"):
        hkticketing.crawl(db_conn, IncompleteClient(projects), now=NOW)
    state = hkticketing.xhs_storage.get_crawl_state(db_conn, hkticketing.TASK_ID)
    assert state["status"] == "failed"
    assert "3 场" in state["last_error"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026年11月21日 (星期六) 下午5時", "2026-11-21T09:00:00+00:00"),
        ("2026年11月21日 中午12時30分", "2026-11-21T04:30:00+00:00"),
        ("2026年11月21日 上午10時", "2026-11-21T02:00:00+00:00"),
    ],
)
def test_chinese_datetime_is_converted_from_hong_kong_time(value, expected):
    assert hkticketing._parse_chinese_datetime(value).isoformat() == expected


def test_detail_parser_maps_promoter_ticket_status_and_does_not_guess_artist():
    detail = hkticketing.parse_project_detail(
        project("50000001723001", "2026永恆開端 Desire 4 演唱會香港站", "60000002288002"),
        now=NOW,
    )
    assert detail.concert_id == "hkt:50000001723001"
    assert detail.artist_name == ""
    assert detail.artist_id == ""
    assert detail.promoter == "萬星娛樂"
    assert detail.city == "香港"
    assert detail.country == "HK"
    assert detail.event_url.endswith("#/allEvents/detail?projectId=50000001723001")
    assert detail.occurrences[0]["start_at"].isoformat() == "2026-11-21T09:00:00+00:00"
    assert detail.occurrences[0]["ticket_status"] == "available"
    assert "節目簡介" in detail.description
    assert detail.occurrences[0]["ticket_url"].endswith("activityId=50000001723001")


def test_source_hash_ignores_volatile_hkt_counters():
    first = project("p1", "HKT Live", "e1")
    second = deepcopy(first)
    first["systemTime"] = "100"
    second["systemTime"] = "200"
    first["eventVoList"][0]["priceVoList"][0]["saleCount"] = 1
    second["eventVoList"][0]["priceVoList"][0]["saleCount"] = 99
    assert (
        hkticketing.parse_project_detail(first, now=NOW).source_hash
        == hkticketing.parse_project_detail(second, now=NOW).source_hash
    )


def test_upsert_is_idempotent_and_unified_view_keeps_platforms_separate(db_conn):
    detail = hkticketing.parse_project_detail(
        project("50000001723001", "HKT Live", "60000002288002"),
        now=NOW,
    )
    assert hkticketing.upsert_concert(db_conn, detail) == (1, 1)
    assert hkticketing.upsert_concert(db_conn, detail) == (1, 1)
    db_conn.execute(
        """INSERT INTO ticketmaster_concerts
           (concert_id, source, title, city, country)
           VALUES ('tm:1', 'ticketmaster.sg', 'TM Live', 'Singapore', 'SG')"""
    )
    db_conn.commit()
    assert db_conn.execute("SELECT COUNT(*) FROM hkticketing_concerts").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM hkticketing_concert_occurrences").fetchone()[0] == 1
    rows = db_conn.execute(
        "SELECT concert_id, source, city, local_timezone FROM concerts_unified ORDER BY concert_id"
    ).fetchall()
    assert [(row["concert_id"], row["source"]) for row in rows] == [
        ("hkt:50000001723001", "hkt.hkticketing.com"),
        ("tm:1", "ticketmaster.sg"),
    ]
    assert rows[0]["local_timezone"] == "Asia/Hong_Kong"
    unpushed = push_records.find_unpushed_entities(
        db_conn, push_records.ENTITY_TYPE_CONCERT, limit=None
    )
    assert {row["concert_id"] for row in unpushed} == {"hkt:50000001723001", "tm:1"}


class _FakeClient:
    def __init__(self, projects: list[dict], *, interrupt_token: str = ""):
        self.projects = projects
        self.interrupt_token = interrupt_token
        self.calls: list[tuple[str, str]] = []

    def fetch_categories(self):
        return {"data": [{"projectTypeId": "133002", "projectTypeName": "演唱會"}]}

    def fetch_projects(self, *, page, page_size, category_id):
        self.calls.append(("list", str(page)))
        return list_payload(self.projects, page=page)

    def fetch_project(self, project_token):
        self.calls.append(("detail", project_token))
        if project_token == self.interrupt_token:
            raise KeyboardInterrupt
        return next(item for item in self.projects if item["projectToken"] == project_token)


def test_crawl_resumes_after_an_interrupted_detail(db_conn):
    projects = [
        project("p1", "HKT One", "e1"),
        project("p2", "HKT Two", "e2"),
    ]
    first = _FakeClient(projects, interrupt_token="p2")
    with pytest.raises(KeyboardInterrupt):
        hkticketing.crawl(db_conn, first, now=NOW)
    state = hkticketing.xhs_storage.get_crawl_state(db_conn, hkticketing.TASK_ID)
    assert json.loads(state["cursor"])["index"] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM hkticketing_concerts").fetchone()[0] == 1

    second = _FakeClient(projects)
    result = hkticketing.crawl(db_conn, second, now=NOW)
    assert result["status"] == "completed"
    assert [token for kind, token in second.calls if kind == "detail"] == ["p2"]
    assert db_conn.execute("SELECT COUNT(*) FROM hkticketing_concerts").fetchone()[0] == 2
