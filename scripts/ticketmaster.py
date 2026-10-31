"""Ticketmaster Singapore 演唱会抓取器。

Ticketmaster SG 的公开页面由反爬验证保护，直接用 ``requests`` 会得到 401。
本模块用 Playwright 通过浏览器完成验证，再读取两个已经存在于页面中的结构化来源：

* ``/categories/concerts`` 及其 ``get-more-game-list`` 分页片段负责发现活动页；
* 活动详情页的 ``calendarEvents`` JSON 负责场次、艺人、场馆和购票链接。

这些字段是 Ticketmaster 自己生成的事实，不需要再调用 AI。AI 若以后用于曲风、艺人
别名等推断，应放在独立的可选 enrichment 任务中，不应覆盖本模块的原始字段。

常用命令::

    python scripts/ticketmaster.py crawl --max-pages 20
    python scripts/ticketmaster.py crawl --restart
    python scripts/ticketmaster.py list --limit 100

抓取进度复用 ``xhs_crawl_state``，任务 ID 固定为 ``ticketmaster:concerts``。活动页
在详情页内逐个处理；每成功一个详情页就保存下一个索引，因此中断后只会重放最后一个
详情页，数据库主键和唯一约束保证重放幂等。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys
from typing import Any, Sequence
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / ".env", encoding="utf-8")

import xhs_storage  # noqa: E402
from database import Connection, connect  # noqa: E402


BASE_URL = "https://ticketmaster.sg"
CONCERTS_URL = f"{BASE_URL}/categories/concerts"
MORE_URL = f"{BASE_URL}/categories/get-more-game-list/concerts"
TASK_ID = "ticketmaster:concerts"
TASK_TYPE = "ticketmaster_concerts"
TARGET_ID = "concerts"
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")


@dataclass(frozen=True)
class ListingEvent:
    """活动列表中的一项；详情页会补全场次和艺人字段。"""

    concert_id: str
    title: str
    event_url: str
    image_url: str = ""
    display_date: str = ""
    unavailable: bool = False


@dataclass(frozen=True)
class ConcertDetail:
    """详情页中可确定解析的活动级字段和未来场次。"""

    concert_id: str
    title: str
    artist_name: str
    artist_id: str
    promoter: str
    category_name: str
    venue_name: str
    event_url: str
    image_url: str
    description: str
    occurrences: tuple[dict[str, Any], ...]
    source_hash: str
    raw_data: dict[str, Any]


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _html_text(value: str) -> str:
    """把详情页 HTML 片段转成可保存的正文摘要。"""
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", value or "", flags=re.I | re.S)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return _collapse(unescape(value))


class _ListingParser(HTMLParser):
    """解析列表页/分页 HTML 片段。

    使用标准库解析器，避免把 bs4 作为线上抓取的额外运行时依赖。页面的
    ``eventContainer`` 结构稳定，字段取自活动链接、日期、标题和图片 alt。
    """

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.event_depth: int | None = None
        self.current: dict[str, Any] | None = None
        self.captures: list[tuple[str, int, list[str]]] = []
        self.events: list[ListingEvent] = []

    @staticmethod
    def _attrs(attrs: Sequence[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = self._attrs(attrs)
        self_closing = tag.lower() in self._VOID_TAGS
        if not self_closing:
            self.depth += 1
        classes = set((attrs_dict.get("class") or "").split())
        if tag.lower() == "div" and "eventContainer" in classes:
            if self.current is not None:
                self._finish_event()
            event_url = ""
            self.current = {
                "concert_id": (attrs_dict.get("id") or "").strip(),
                "event_url": event_url,
                "title": "",
                "image_url": "",
                "display_date": "",
                "unavailable": False,
            }
            self.event_depth = self.depth
        if self.current is None:
            return
        if tag.lower() == "a" and "/activity/detail/" in attrs_dict.get("href", ""):
            href = urljoin(BASE_URL, attrs_dict["href"])
            self.current["event_url"] = href
            if not self.current["concert_id"]:
                self.current["concert_id"] = urlparse(href).path.rsplit("/", 1)[-1]
        elif tag.lower() == "img":
            self.current["image_url"] = urljoin(BASE_URL, attrs_dict.get("src", ""))
            if not self.current["title"]:
                self.current["title"] = _collapse(unescape(attrs_dict.get("alt", "")))
        if "event-unavailable" in classes:
            self.current["unavailable"] = True
        capture_kind = None
        if "date" in classes:
            capture_kind = "display_date"
        elif "multi_ellipsis" in classes:
            capture_kind = "title"
        elif "event-unavailable" in classes:
            capture_kind = "unavailable_text"
        if capture_kind and not self_closing:
            self.captures.append((capture_kind, self.depth, []))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        for _, _, parts in self.captures:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag.lower() not in self._VOID_TAGS:
            for index in range(len(self.captures) - 1, -1, -1):
                kind, capture_depth, parts = self.captures[index]
                if capture_depth == self.depth:
                    self.captures.pop(index)
                    value = _collapse("".join(parts))
                    if kind == "title" and value:
                        self.current["title"] = value
                    elif kind == "display_date" and value:
                        self.current["display_date"] = value
                    elif kind == "unavailable_text":
                        self.current["unavailable"] = True
                    break
            if self.event_depth == self.depth and tag.lower() == "div":
                self._finish_event()
            self.depth -= 1

    def _finish_event(self) -> None:
        if not self.current:
            return
        concert_id = str(self.current.get("concert_id") or "").strip()
        event_url = str(self.current.get("event_url") or "").strip()
        if not concert_id and event_url:
            concert_id = urlparse(event_url).path.rsplit("/", 1)[-1]
        if concert_id and event_url:
            self.events.append(
                ListingEvent(
                    concert_id=concert_id,
                    title=str(self.current.get("title") or "").strip(),
                    event_url=event_url,
                    image_url=str(self.current.get("image_url") or "").strip(),
                    display_date=str(self.current.get("display_date") or "").strip(),
                    unavailable=bool(self.current.get("unavailable")),
                )
            )
        self.current = None
        self.event_depth = None
        self.captures.clear()

    def close(self) -> None:
        super().close()
        self._finish_event()


def _next_offset(html: str) -> str | None:
    """读取列表片段里的下一页 offset；没有 more 按钮则返回 None。"""
    matches: list[str] = []
    for tag_match in re.finditer(r"<[^>]+>", html or "", flags=re.I | re.S):
        tag = tag_match.group(0)
        class_match = re.search(r"\bclass\s*=\s*[\"']([^\"']+)[\"']", tag, flags=re.I)
        if not class_match or "page-show-more" not in class_match.group(1).split():
            continue
        parameter_match = re.search(r"\bdata-parameter\s*=\s*[\"']([^\"']+)[\"']", tag, flags=re.I)
        if parameter_match:
            matches.append(parameter_match.group(1))
    if not matches:
        return None
    params = parse_qs(unescape(matches[-1]))
    value = (params.get("offset") or [""])[-1].strip()
    return value or None


def parse_listing_html(html: str) -> tuple[list[ListingEvent], str | None]:
    """解析初始列表或 ``get-more-game-list`` 返回的 HTML 片段。"""
    parser = _ListingParser()
    parser.feed(html or "")
    parser.close()
    # 同一活动可能以不同日期出现多次；详情页一次返回全部场次，因此每个活动页只抓一次。
    unique: dict[str, ListingEvent] = {}
    for event in parser.events:
        old = unique.get(event.concert_id)
        if old is None or (not old.title and event.title):
            unique[event.concert_id] = event
    return list(unique.values()), _next_offset(html)


def _extract_json_assignment(html: str, name: str) -> dict[str, Any]:
    match = re.search(
        rf"(?:(?:var|let|const)\s+|window\.){re.escape(name)}\s*=\s*(\{{.*?\}})\s*;",
        html or "",
        flags=re.S,
    )
    if not match:
        raise ValueError(f"详情页缺少 {name} JSON")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"详情页 {name} JSON 无法解析: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"详情页 {name} 不是对象")
    return value


def _extract_data_layer(html: str) -> dict[str, Any]:
    for match in re.finditer(r"dataLayer\.push\(\s*(\{.*?\})\s*\)", html or "", flags=re.S):
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event") == "EnterActivityDetail":
            return value
    return {}


def _extract_ticket_url(on_sale: Any) -> str:
    """从 Ticketmaster 的 HTML、URL 或小对象中取购票地址。"""
    if isinstance(on_sale, dict):
        for key in ("url", "href", "ticketUrl", "ticketURL", "bookingUrl"):
            value = _extract_ticket_url(on_sale.get(key))
            if value:
                return value
        return ""
    if not isinstance(on_sale, str):
        return ""
    decoded = unescape(on_sale).strip()
    match = re.search(r"\bhref\s*=\s*[\"']([^\"']+)", decoded, flags=re.I)
    value = match.group(1).strip() if match else decoded
    if not value or value.casefold().startswith(("javascript:", "#")):
        return ""
    if not match and not re.match(r"(?:https?:)?//|/", value, flags=re.I):
        return ""
    return urljoin(BASE_URL, value)


def _parse_start(value: Any, *, fallback_date: Any = None) -> datetime | None:
    """解析详情页的 ``clientDate`` 为带时区的 UTC datetime。

    页面当前使用 ISO 字符串，但部分活动仍只带展示用的
    ``02 Nov 2026 (Mon.) 08:00 pm``。两种格式都按新加坡时区解释；带
    明确 offset 的 ISO 值则保留其原时区。
    """
    candidates = [value, fallback_date]
    for candidate in candidates:
        if not candidate:
            continue
        if isinstance(candidate, (int, float)):
            # Unix 秒或毫秒时间戳在个别接口版本中会替代 clientDate。
            timestamp = float(candidate)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
        text = _collapse(str(candidate)).replace("/", "-")
        text = re.sub(r"\s+(?:SGT|GMT\+?0?8:?00)$", "", text, flags=re.I)
        if re.fullmatch(r"\d{9,}", text):
            timestamp = float(text)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
        try:
            iso = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if iso.tzinfo is None:
                iso = iso.replace(tzinfo=SINGAPORE_TZ)
            return iso.astimezone(timezone.utc)
        except ValueError:
            pass
        # 去掉展示日期中的星期，例如 ``(Mon.)`` 或 ``(星期一)``。
        display = re.sub(r"\([^)]*\)", " ", text)
        for fmt in (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
            "%d %b %Y %I:%M %p", "%d %B %Y %I:%M %p",
            "%d %b %Y %I:%M%p", "%d %B %Y %I:%M%p",
            "%d %b %Y", "%d %B %Y",
        ):
            try:
                local = datetime.strptime(display, fmt).replace(tzinfo=SINGAPORE_TZ)
                return local.astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def _occurrence_id(concert_id: str, source_event_id: str, start_at: datetime, venue: str) -> str:
    # Ticketmaster 的 event id 是场次的稳定身份；日期/场馆作为参数保留在
    # 函数签名中便于调用方表达来源，但不参与主键，活动改期时仍能更新同一行。
    raw = "\0".join((concert_id, source_event_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _detail_description(html: str) -> str:
    match = re.search(r"<section\b[^>]*\bid=[\"']synopsis[\"'][^>]*>(.*?)</section>", html or "", flags=re.I | re.S)
    return _html_text(match.group(1)) if match else ""


def parse_detail_html(
    html: str,
    concert_id: str,
    event_url: str,
    *,
    now: datetime | None = None,
) -> ConcertDetail:
    """解析详情页并只保留尚未开始的场次。"""
    now_value = now or datetime.now(timezone.utc)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=SINGAPORE_TZ)
    now_utc = now_value.astimezone(timezone.utc)
    calendar = _extract_json_assignment(html, "calendarEvents")
    data_layer = _extract_data_layer(html)
    events = calendar.get("events") or []
    if not isinstance(events, list):
        raise ValueError("calendarEvents.events 不是数组")

    title = _collapse(str(
        data_layer.get("eventTitle")
        or data_layer.get("activityName")
        or data_layer.get("title")
        or ""
    ))
    artist_name = _collapse(str(data_layer.get("artistName") or data_layer.get("artist") or ""))
    artist_id = _collapse(str(data_layer.get("artistId") or data_layer.get("artistID") or ""))
    venue = ""
    image_url = ""
    occurrences: list[dict[str, Any]] = []
    seen_source_event_ids: set[str] = set()
    for raw in events:
        if not isinstance(raw, dict):
            continue
        if not title:
            title = _collapse(str(raw.get("title") or raw.get("titleFormat") or ""))
        if not artist_name:
            artist_name = _collapse(str(raw.get("artistName") or raw.get("artist") or ""))
        if not artist_id:
            artist_id = _collapse(str(raw.get("artistId") or raw.get("artistID") or ""))
        venue = _collapse(str(
            raw.get("fieldName")
            or raw.get("venueName")
            or raw.get("venue")
            or raw.get("location")
            or venue
        ))
        image_url = str(raw.get("gameImage") or raw.get("gameImageThumbnail") or raw.get("image") or image_url)
        start_at = _parse_start(
            raw.get("clientDate") or raw.get("startDate") or raw.get("startTime"),
            fallback_date=raw.get("dateSort") or raw.get("date"),
        )
        if start_at is None or start_at <= now_utc:
            continue
        end_at = _parse_start(
            raw.get("endDate") or raw.get("endTime"),
            fallback_date=raw.get("dateEnd"),
        )
        if end_at is not None and end_at <= start_at:
            end_at = None
        source_event_id = str(raw.get("id") or "").strip()
        if not source_event_id:
            source_event_id = hashlib.sha256(
                "\0".join((str(raw.get("date") or ""), str(raw.get("title") or title), venue)).encode("utf-8")
            ).hexdigest()[:20]
        if source_event_id in seen_source_event_ids:
            continue
        seen_source_event_ids.add(source_event_id)
        ticket_url = _extract_ticket_url(
            raw.get("onSale")
            or raw.get("ticketUrl")
            or raw.get("ticketURL")
            or raw.get("bookingUrl")
        )
        occurrences.append({
            "occurrence_id": _occurrence_id(concert_id, source_event_id, start_at, venue),
            "source_event_id": source_event_id,
            "start_at": start_at,
            "end_at": end_at,
            "display_date": _collapse(str(raw.get("date") or raw.get("dateNoTime") or raw.get("dateNoTtime") or "")),
            "venue_name": venue,
            "ticket_url": ticket_url,
            "ticket_status": "available" if ticket_url else "unavailable",
            "status": "upcoming",
            "raw_data": raw,
        })

    title = title or concert_id
    if not venue:
        match = re.search(r"<[^>]+id=[\"']synopsisEventVenue[\"'][^>]*>(.*?)</", html or "", flags=re.I | re.S)
        venue = _html_text(match.group(1)) if match else ""
    if not image_url:
        image_match = re.search(r"<img[^>]+class=[\"'][^\"']*large-banner-d[^\"']*[\"'][^>]+src=[\"']([^\"']+)", html or "", flags=re.I)
        image_url = unescape(image_match.group(1)) if image_match else ""

    canonical = event_url or f"{BASE_URL}/activity/detail/{concert_id}"
    raw_data = {
        "calendar_events": calendar,
        "data_layer": data_layer,
        "source_url": canonical,
    }
    source_hash = hashlib.sha256(
        json.dumps({"calendar": calendar, "data_layer": data_layer}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ConcertDetail(
        concert_id=concert_id,
        title=title,
        artist_name=artist_name,
        artist_id=artist_id,
        promoter=_collapse(str(data_layer.get("promoter") or "")),
        category_name=_collapse(str(data_layer.get("parentCategoryName") or "Concerts")),
        venue_name=venue,
        event_url=canonical,
        image_url=urljoin(BASE_URL, image_url) if image_url else "",
        description=_detail_description(html),
        occurrences=tuple(occurrences),
        source_hash=source_hash,
        raw_data=raw_data,
    )


def _json(value: Any, default: Any) -> str:
    return json.dumps(default if value is None else value, ensure_ascii=False, separators=(",", ":"))


def mark_started_occurrences(conn: Connection) -> int:
    """把旧的 future 记录转成 started，查询时即可只看 upcoming。"""
    with conn:
        cur = conn.execute(
            """UPDATE ticketmaster_concert_occurrences
               SET status='started', updated_at=CURRENT_TIMESTAMP
               WHERE status='upcoming' AND start_at <= CURRENT_TIMESTAMP"""
        )
        conn.execute(
            """UPDATE ticketmaster_concerts AS c
               SET status='ended', updated_at=CURRENT_TIMESTAMP
               WHERE c.status='active'
                 AND EXISTS (
                     SELECT 1 FROM ticketmaster_concert_occurrences o
                     WHERE o.concert_id = c.concert_id
                 )
                 AND NOT EXISTS (
                     SELECT 1 FROM ticketmaster_concert_occurrences o
                     WHERE o.concert_id = c.concert_id AND o.status='upcoming'
                 )"""
        )
        return cur.rowcount


def upsert_concert(conn: Connection, detail: ConcertDetail) -> tuple[int, int]:
    """幂等写入一个活动及其未来场次，返回 (活动写入数, 场次写入数)。"""
    if not detail.occurrences:
        return 0, 0
    now = datetime.now(timezone.utc)
    with conn:
        conn.execute(
            """INSERT INTO ticketmaster_concerts (
                concert_id, source, title, artist_name, artist_id, promoter, category_name,
                venue_name, city, country, event_url, image_url, description, source_hash,
                raw_data, status, first_seen_at, last_seen_at, updated_at
            ) VALUES (%s, 'ticketmaster.sg', %s, %s, %s, %s, %s, %s, 'Singapore', 'SG',
                      %s, %s, %s, %s, %s, 'active', %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (concert_id) DO UPDATE SET
                title=COALESCE(NULLIF(EXCLUDED.title, ''), ticketmaster_concerts.title),
                artist_name=COALESCE(NULLIF(EXCLUDED.artist_name, ''), ticketmaster_concerts.artist_name),
                artist_id=COALESCE(NULLIF(EXCLUDED.artist_id, ''), ticketmaster_concerts.artist_id),
                promoter=COALESCE(NULLIF(EXCLUDED.promoter, ''), ticketmaster_concerts.promoter),
                category_name=COALESCE(NULLIF(EXCLUDED.category_name, ''), ticketmaster_concerts.category_name),
                venue_name=COALESCE(NULLIF(EXCLUDED.venue_name, ''), ticketmaster_concerts.venue_name),
                event_url=COALESCE(NULLIF(EXCLUDED.event_url, ''), ticketmaster_concerts.event_url),
                image_url=COALESCE(NULLIF(EXCLUDED.image_url, ''), ticketmaster_concerts.image_url),
                description=COALESCE(NULLIF(EXCLUDED.description, ''), ticketmaster_concerts.description),
                source_hash=COALESCE(NULLIF(EXCLUDED.source_hash, ''), ticketmaster_concerts.source_hash),
                raw_data=EXCLUDED.raw_data,
                status='active',
                last_seen_at=EXCLUDED.last_seen_at,
                updated_at=CURRENT_TIMESTAMP""",
            (
                detail.concert_id,
                detail.title,
                detail.artist_name,
                detail.artist_id,
                detail.promoter,
                detail.category_name,
                detail.venue_name,
                detail.event_url,
                detail.image_url,
                detail.description,
                detail.source_hash,
                _json(detail.raw_data, {}),
                now,
                now,
            ),
        )
        for occurrence in detail.occurrences:
            conn.execute(
                """INSERT INTO ticketmaster_concert_occurrences (
                    occurrence_id, concert_id, source_event_id, start_at, end_at,
                    display_date, venue_name, ticket_url, ticket_status, source_hash,
                    raw_data, status, first_seen_at, last_seen_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (concert_id, source_event_id) DO UPDATE SET
                    occurrence_id=EXCLUDED.occurrence_id,
                    source_event_id=EXCLUDED.source_event_id,
                    start_at=EXCLUDED.start_at,
                    end_at=EXCLUDED.end_at,
                    display_date=EXCLUDED.display_date,
                    venue_name=EXCLUDED.venue_name,
                    ticket_url=EXCLUDED.ticket_url,
                    ticket_status=EXCLUDED.ticket_status,
                    source_hash=EXCLUDED.source_hash,
                    raw_data=EXCLUDED.raw_data,
                    status=CASE WHEN EXCLUDED.start_at > CURRENT_TIMESTAMP THEN 'upcoming' ELSE 'started' END,
                    last_seen_at=EXCLUDED.last_seen_at,
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    occurrence["occurrence_id"],
                    detail.concert_id,
                    occurrence["source_event_id"],
                    occurrence["start_at"],
                    occurrence.get("end_at"),
                    occurrence.get("display_date", ""),
                    occurrence.get("venue_name", detail.venue_name),
                    occurrence.get("ticket_url", ""),
                    occurrence.get("ticket_status", ""),
                    occurrence.get("source_hash", detail.source_hash),
                    _json(occurrence.get("raw_data"), {}),
                    occurrence.get("status", "upcoming"),
                    now,
                    now,
                ),
            )
    return 1, len(detail.occurrences)


def get_upcoming_concerts(conn: Connection, limit: int = 100) -> list[dict[str, Any]]:
    """读取下一场仍未开始的演唱会，方便 CLI/其他调用方复用。"""
    limit = max(1, min(int(limit), 1000))
    rows = conn.execute(
        """SELECT c.concert_id, c.title, c.artist_name, c.artist_id, c.promoter,
                  c.venue_name, c.city, c.event_url, c.image_url,
                  o.occurrence_id, o.start_at, o.display_date, o.ticket_url,
                  o.ticket_status, o.status
           FROM ticketmaster_concerts c
           JOIN ticketmaster_concert_occurrences o ON o.concert_id = c.concert_id
           WHERE o.status='upcoming' AND o.start_at > CURRENT_TIMESTAMP
           ORDER BY o.start_at ASC, c.artist_name ASC, c.concert_id ASC
           LIMIT %s""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


class TicketmasterBrowser:
    """用一个浏览器上下文完成反爬验证、列表分页和详情页读取。"""

    def __init__(
        self,
        *,
        headed: bool = False,
        timeout_ms: int = 60_000,
        challenge_timeout_ms: int = 90_000,
        request_delay_ms: tuple[int, int] = (1_500, 3_000),
    ) -> None:
        low, high = request_delay_ms
        if low < 0 or high < low:
            raise ValueError("request_delay_ms 必须是非负的 (最小值, 最大值) 区间")
        self.headed = headed
        self.timeout_ms = timeout_ms
        self.challenge_timeout_ms = challenge_timeout_ms
        self.request_delay_ms = request_delay_ms
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    def __enter__(self) -> "TicketmasterBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Ticketmaster 抓取需要 Playwright；请先安装 requirements.txt 并执行 playwright install") from exc
        self._pw = sync_playwright().start()
        kwargs: dict[str, Any] = {"headless": not self.headed}
        executable = os.getenv("TICKETMASTER_CHROMIUM_PATH", "").strip()
        if not executable:
            executable = shutil.which("chromium") or shutil.which("google-chrome") or ""
        if executable:
            kwargs["executable_path"] = executable
        if os.name != "nt":
            kwargs["args"] = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # Playwright otherwise exposes navigator.webdriver even in a
                # headed window.  Ticketmaster's EPS check treats that as a
                # signal that differs from a normal Chromium session.
                "--disable-blink-features=AutomationControlled",
            ]
        self._browser = self._pw.chromium.launch(**kwargs)
        browser_version = str(getattr(self._browser, "version", ""))
        chrome_major = browser_version.split(".", 1)[0] or "131"
        user_agent = os.getenv("TICKETMASTER_USER_AGENT", "").strip()
        if not user_agent:
            # 保持 UA 的 Chrome 主版本与实际 Playwright 浏览器一致，避免站点把
            # 固定旧 UA 与 navigator 特征的差异判为异常；仍可用环境变量覆盖。
            user_agent = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
            )
        self._context = self._browser.new_context(
            user_agent=user_agent,
            locale="en-SG",
            timezone_id="Asia/Singapore",
            viewport={"width": 1280, "height": 800},
        )
        # Keep the optional dependency genuinely optional.  The explicit
        # launch flag above covers the common webdriver check; the package
        # fills in the remaining browser-only properties when installed.
        try:
            from playwright_stealth import Stealth

            # Match the values advertised by the context.  The library's
            # defaults target a Windows Chrome profile, which would create a
            # Linux UA + Windows platform/language combination and make the
            # fingerprint less coherent.
            Stealth(
                navigator_languages_override=("en-SG", "en"),
                navigator_platform_override="Linux x86_64",
                navigator_user_agent_override=user_agent,
                sec_ch_ua_override=f'"Not:A-Brand";v="8", "Chromium";v="{chrome_major}"',
            ).apply_stealth_sync(self._context)
        except (ImportError, AttributeError, TypeError):
            pass
        self.page = self._context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()

    def _wait_ready(self, marker: str) -> str:
        if self.page is None:
            raise RuntimeError("浏览器尚未启动")
        deadline = self._now_ms() + self.challenge_timeout_ms
        latest = ""
        while self._now_ms() < deadline:
            try:
                latest = self.page.content()
            except Exception:
                # eps 验证通过时页面会自动重导航；Playwright 在这个极短窗口
                # 可能暂时拒绝读取 DOM，下一轮再取即可。
                self.page.wait_for_timeout(250)
                continue
            if marker in latest and "Your Browsing Activity Has Been Paused" not in latest:
                return latest
            self.page.wait_for_timeout(1000)
        raise RuntimeError(f"Ticketmaster 反爬验证未完成（页面未出现 {marker}）")

    @staticmethod
    def _now_ms() -> int:
        import time
        return int(time.monotonic() * 1000)

    def fetch_page(self, url: str, *, marker: str) -> str:
        if self.page is None:
            raise RuntimeError("浏览器尚未启动")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            # 反爬脚本可能在 domcontentloaded 后立即再次导航；继续轮询
            # 页面内容，若验证未完成再由 _wait_ready 给出统一错误。
            pass
        return self._wait_ready(marker)

    def fetch_listing_fragment(self, offset: str, _attempt: int = 0) -> str:
        if self.page is None:
            raise RuntimeError("浏览器尚未启动")
        url = f"{MORE_URL}?{urlencode({'type': 'card', 'offset': offset})}"
        value = self.page.evaluate(
            """async (url) => {
                const response = await fetch(url, {credentials: 'include'});
                return {status: response.status, text: await response.text()};
            }""",
            url,
        )
        if not isinstance(value, dict) or int(value.get("status", 0)) >= 400:
            raise RuntimeError(f"Ticketmaster 分页请求失败: {url}")
        text = str(value.get("text") or "")
        if "Your Browsing Activity Has Been Paused" in text or "identify" in text[:500]:
            if _attempt >= 2:
                raise RuntimeError(f"Ticketmaster 分页反爬验证未完成: {url}")
            # 让页面重新完成一次验证，再由下一次调用重试。
            self.fetch_page(CONCERTS_URL, marker="eventContainer")
            return self.fetch_listing_fragment(offset, _attempt + 1)
        return text

    def fetch_detail(self, event_url: str) -> str:
        if self.page is not None and self.request_delay_ms[1] > 0:
            low, high = self.request_delay_ms
            self.page.wait_for_timeout(random.randint(low, high))
        html = self.fetch_page(event_url, marker="calendarEvents")
        # 艺人 ID/名称由详情页稍后注入的 dataLayer 提供；给页面脚本一个短窗口，
        # 但即使某些活动没有该字段，也保留已解析的标题和场次。
        if "EnterActivityDetail" not in html and self.page is not None:
            for _ in range(12):
                self.page.wait_for_timeout(250)
                try:
                    html = self.page.content()
                except Exception:
                    continue
                if "EnterActivityDetail" in html:
                    break
        return html


def _cursor_value(value: Any) -> dict[str, Any]:
    if not value:
        return {"offset": None, "index": 0}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        # 兼容早期只保存 offset 的断点格式。
        return {"offset": str(value), "index": 0}
    return parsed if isinstance(parsed, dict) else {"offset": None, "index": 0}


def _save_state(conn: Connection, *, offset: str | None, index: int, status: str, error: str = "") -> None:
    cursor = "" if status == "completed" else json.dumps(
        {"offset": offset, "index": max(0, int(index))}, ensure_ascii=False, separators=(",", ":")
    )
    xhs_storage.update_crawl_state(conn, TASK_ID, TASK_TYPE, TARGET_ID, cursor, status, error)


def crawl(
    conn: Connection,
    client: TicketmasterBrowser,
    *,
    max_pages: int = 20,
    restart: bool = False,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """执行一轮可恢复抓取。

    ``max_pages`` 是本次列表页预算；一个列表页中的详情会全部处理完才进入下一页，
    因而预算耗尽不会丢掉页内进度。
    """
    if max_pages < 1:
        raise ValueError("max_pages 必须是正整数")
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (TASK_ID,))
    try:
        mark_started_occurrences(conn)
        if restart:
            _save_state(conn, offset=None, index=0, status="running")
            state = None
        else:
            state = xhs_storage.get_crawl_state(conn, TASK_ID)
            if state and state["status"] == "completed":
                # Ticketmaster 列表会持续增加新活动，completed 只表示上一轮已
                # 走到列表尾部；下一轮仍从第一页刷新，幂等键负责去重。只有
                # running/paused/failed 才从保存的页内断点继续。
                state = None

        cursor = _cursor_value(state["cursor"] if state else "")
        offset = cursor.get("offset")
        index = int(cursor.get("index") or 0)
        pages = events_found = events_saved = occurrences_saved = 0
        seen_concert_ids: set[str] = set()
        html = client.fetch_page(CONCERTS_URL, marker="eventContainer") if offset in (None, "") else ""

        while pages < max_pages:
            if offset not in (None, ""):
                html = client.fetch_listing_fragment(str(offset))
            listing, next_offset = parse_listing_html(html)
            pages += 1
            events_found += len(listing)
            if index > len(listing):
                index = 0
            for item_index in range(index, len(listing)):
                item = listing[item_index]
                if item.concert_id in seen_concert_ids:
                    # 同一活动可能因多场日期出现在相邻分页；详情页已经包含全部
                    # 场次，当前轮无需再次请求，但仍推进页内断点。
                    _save_state(conn, offset=offset, index=item_index + 1, status="running")
                    continue
                seen_concert_ids.add(item.concert_id)
                try:
                    detail = parse_detail_html(
                        client.fetch_detail(item.event_url),
                        item.concert_id,
                        item.event_url,
                        now=now,
                    )
                    saved_events, saved_occurrences = upsert_concert(conn, detail)
                except Exception as exc:
                    _save_state(conn, offset=offset, index=item_index, status="failed", error=str(exc)[:1000])
                    raise
                events_saved += saved_events
                occurrences_saved += saved_occurrences
                # 发生中断时重放当前活动页即可；写入本身是幂等的。
                _save_state(conn, offset=offset, index=item_index + 1, status="running")
            index = 0
            if not next_offset:
                _save_state(conn, offset=None, index=0, status="completed")
                return {
                    "pages": pages,
                    "events_found": events_found,
                    "events_saved": events_saved,
                    "occurrences_saved": occurrences_saved,
                    "status": "completed",
                }
            offset = str(next_offset)
            _save_state(conn, offset=offset, index=0, status="running")
            if pages >= max_pages:
                break

        _save_state(conn, offset=str(offset) if offset not in (None, "") else None, index=0, status="paused")
        return {
            "pages": pages,
            "events_found": events_found,
            "events_saved": events_saved,
            "occurrences_saved": occurrences_saved,
            "status": "paused",
        }
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (TASK_ID,))


def cmd_crawl(args: argparse.Namespace) -> int:
    # 不用 ``with conn`` 包住整个任务：存储函数内部会按记录提交，
    # psycopg2 不允许同一连接的 context manager 递归进入。
    conn = connect(initialize=True)
    try:
        with TicketmasterBrowser(headed=args.headed) as client:
            result = crawl(conn, client, max_pages=args.max_pages, restart=args.restart)
    except Exception as exc:
        print(f"[ERR] Ticketmaster 抓取失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = connect(initialize=True)
    try:
        rows = get_upcoming_concerts(conn, args.limit)
    finally:
        conn.close()
    if not rows:
        print("没有找到尚未开始的演唱会。")
        return 0
    for row in rows:
        start_at = row["start_at"].astimezone(SINGAPORE_TZ).strftime("%Y-%m-%d %H:%M")
        ticket = row["ticket_url"] or "（暂无购票链接）"
        print(f"{start_at} | {row['artist_name'] or row['title']} | {row['venue_name']} | {ticket}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ticketmaster", description="Ticketmaster SG 未开始演唱会抓取器")
    sub = parser.add_subparsers(dest="command", required=True)
    crawl_parser = sub.add_parser("crawl", help="抓取演唱会列表和详情")
    crawl_parser.add_argument("--max-pages", type=int, default=20, help="本次最多抓取列表页数（默认 20）")
    crawl_parser.add_argument("--restart", action="store_true", help="忽略已保存断点，从第一页重新发现活动")
    crawl_parser.add_argument("--headed", action="store_true", help="显示浏览器窗口，便于排查反爬验证")
    crawl_parser.set_defaults(func=cmd_crawl)
    list_parser = sub.add_parser("list", help="列出数据库中尚未开始的演唱会")
    list_parser.add_argument("--limit", type=int, default=100)
    list_parser.set_defaults(func=cmd_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

