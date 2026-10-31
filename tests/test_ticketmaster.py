"""Ticketmaster SG 解析、幂等写入和断点恢复测试。"""

from datetime import datetime, timezone
import json

import pytest

import ticketmaster


LISTING = """
<div class="eventContainer" id="26sg_one">
  <div class="thumbnails"><a href="/activity/detail/26sg_one">
    <img src="https://static.ticketmaster.sg/one.jpg" alt="One Live">
    <div class="data"><div class="date">02 Nov 2026 (Mon.) 08:00 pm</div>
    <div class="multi_ellipsis">One Live</div></div>
  </a></div>
</div>
<div class="eventContainer" id="26sg_two">
  <div class="thumbnails"><a href="/activity/detail/26sg_two">
    <img src="https://static.ticketmaster.sg/two.jpg" alt="Two Live">
    <div class="data"><div class="date">03 Nov 2026 (Tue.) 08:00 pm</div>
    <div class="multi_ellipsis">Two Live</div></div>
  </a></div>
</div>
<div class="page-show-more" data-action-url="/categories/get-more-game-list/concerts"
     data-parameter="type=card&amp;offset=2" data-type="card"><a>more</a></div>
"""


def detail_html(title="One Live", artist="Singer One", event_id="101"):
    payload = {
        "events": [{
            "id": event_id,
            "title": title,
            "fieldName": "The Star Theatre",
            "clientDate": "2026-11-02 20:00:00",
            "date": "02 Nov 2026 (Mon.) 08:00 pm",
            "onSale": f'<a href="https://ticketmaster.sg/ticket/area/26sg_one/{event_id}">Find tickets</a>',
            "gameImage": "https://static.ticketmaster.sg/one.jpg",
        }],
    }
    return (
        f'<script>var calendarEvents = {json.dumps(payload, ensure_ascii=False)};</script>'
        f'<section id="synopsis"><p>About {artist}</p></section>'
        '<script>dataLayer.push({"event":"EnterActivityDetail",'
        f'"artistId":"77","artistName":{json.dumps(artist)},'
        '"parentCategoryName":"Concerts","promoter":"Promoter"});</script>'
    )


def test_listing_parser_extracts_events_and_next_offset():
    events, next_offset = ticketmaster.parse_listing_html(LISTING)
    assert [event.concert_id for event in events] == ["26sg_one", "26sg_two"]
    assert events[0].title == "One Live"
    assert events[0].event_url == "https://ticketmaster.sg/activity/detail/26sg_one"
    assert next_offset == "2"


def test_browser_validates_request_delay_range():
    with pytest.raises(ValueError, match="request_delay_ms"):
        ticketmaster.TicketmasterBrowser(request_delay_ms=(-1, 0))
    with pytest.raises(ValueError, match="request_delay_ms"):
        ticketmaster.TicketmasterBrowser(request_delay_ms=(2_000, 1_000))


def test_detail_parser_uses_structured_fields_and_filters_started():
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    detail = ticketmaster.parse_detail_html(
        detail_html(), "26sg_one", "https://ticketmaster.sg/activity/detail/26sg_one", now=now
    )
    assert detail.artist_name == "Singer One"
    assert detail.artist_id == "77"
    assert detail.venue_name == "The Star Theatre"
    assert len(detail.occurrences) == 1
    assert detail.occurrences[0]["ticket_url"].endswith("/101")

    started = ticketmaster.parse_detail_html(
        detail_html(), "26sg_one", "https://ticketmaster.sg/activity/detail/26sg_one",
        now=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    assert started.occurrences == ()


def test_detail_parser_accepts_display_date_and_direct_ticket_url():
    payload = {
        "events": [{
            "id": "102",
            "title": "Display Date Live",
            "fieldName": "Singapore Indoor Stadium",
            "date": "03 Nov 2026 (Tue.) 08:00 pm",
            "ticketUrl": "/ticket/area/display/102",
        }],
    }
    html = (
        f'<script>var calendarEvents = {json.dumps(payload)};</script>'
        '<script>dataLayer.push({"event":"EnterActivityDetail",'
        '"artistName":"Display Singer"});</script>'
    )
    detail = ticketmaster.parse_detail_html(
        html, "display", "https://ticketmaster.sg/activity/detail/display",
        now=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    assert len(detail.occurrences) == 1
    assert detail.occurrences[0]["start_at"].isoformat() == "2026-11-03T12:00:00+00:00"
    assert detail.occurrences[0]["ticket_url"] == "https://ticketmaster.sg/ticket/area/display/102"


def test_upsert_concert_is_idempotent(db_conn):
    detail = ticketmaster.parse_detail_html(
        detail_html(), "26sg_one", "https://ticketmaster.sg/activity/detail/26sg_one",
        now=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    assert ticketmaster.upsert_concert(db_conn, detail) == (1, 1)
    assert ticketmaster.upsert_concert(db_conn, detail) == (1, 1)
    assert db_conn.execute("SELECT COUNT(*) FROM ticketmaster_concerts").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM ticketmaster_concert_occurrences").fetchone()[0] == 1
    row = db_conn.execute("SELECT artist_name, ticket_url FROM ticketmaster_concerts c JOIN ticketmaster_concert_occurrences o USING (concert_id)").fetchone()
    assert row["artist_name"] == "Singer One"
    assert row["ticket_url"].endswith("/101")


class _FakeClient:
    def __init__(self, *, interrupt=False):
        self.interrupt = interrupt
        self.calls = []

    def fetch_page(self, url, *, marker):
        self.calls.append(("page", url, marker))
        return LISTING.split('<div class="page-show-more"', 1)[0]

    def fetch_listing_fragment(self, offset):
        raise AssertionError("fixture listing has no next page")

    def fetch_detail(self, event_url):
        self.calls.append(("detail", event_url))
        if self.interrupt and event_url.endswith("26sg_two"):
            raise KeyboardInterrupt
        concert_id = event_url.rsplit("/", 1)[-1]
        return detail_html(title=concert_id, artist=concert_id, event_id=concert_id)


def test_crawl_replays_only_uncommitted_detail_after_interrupt(db_conn):
    first = _FakeClient(interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        ticketmaster.crawl(
            db_conn, first, max_pages=20,
            now=datetime(2026, 10, 27, tzinfo=timezone.utc),
        )
    state = ticketmaster.xhs_storage.get_crawl_state(db_conn, ticketmaster.TASK_ID)
    assert json.loads(state["cursor"])["index"] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM ticketmaster_concerts").fetchone()[0] == 1

    second = _FakeClient()
    result = ticketmaster.crawl(
        db_conn, second, max_pages=20,
        now=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    assert result["status"] == "completed"
    assert [call[1].rsplit("/", 1)[-1] for call in second.calls if call[0] == "detail"] == ["26sg_two"]
    assert db_conn.execute("SELECT COUNT(*) FROM ticketmaster_concerts").fetchone()[0] == 2

    # 完成后的下一轮会刷新第一页，发现新活动的同时由主键保证幂等。
    third = _FakeClient()
    result = ticketmaster.crawl(
        db_conn, third, max_pages=20,
        now=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    assert result["status"] == "completed"
    assert any(call[0] == "page" for call in third.calls)
    assert db_conn.execute("SELECT COUNT(*) FROM ticketmaster_concerts").fetchone()[0] == 2

