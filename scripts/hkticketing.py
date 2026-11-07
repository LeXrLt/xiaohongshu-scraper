"""HKT 香港快達票演唱會抓取器。

网站是动态 SPA，列表和详情数据由公开 JSON GET 接口提供。浏览器上下文只用于
完成站点的 Baxia 校验并共享必要的会话状态；本模块不会登录、调用购票流程或发送
任何写请求。

常用命令::

    python scripts/hkticketing.py crawl --max-pages 20
    python scripts/hkticketing.py crawl --restart
    python scripts/hkticketing.py list --limit 100

抓取进度保存在 ``xhs_crawl_state`` 的 ``hkticketing:concerts`` 任务中。HKT 数据写入
独立的 ``hkticketing_concerts`` / ``hkticketing_concert_occurrences`` 表，再由统一视图
提供给 API、MCP 和推送模块。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / ".env", encoding="utf-8")

import xhs_storage  # noqa: E402
from concert_storage import (  # noqa: E402
    ConcertDetail,
    mark_started_occurrences as _mark_started_occurrences,
    upsert_concert as _upsert_concert,
)
from database import Connection, connect  # noqa: E402

SITE_BASE_URL = "https://hkt.hkticketing.com"
SITE_ALL_EVENTS_URL = f"{SITE_BASE_URL}/#/allEvents/index"
# 保留首页常量供已有调用方兼容；抓取入口使用全量演出页。
SITE_HOME_URL = f"{SITE_BASE_URL}/hant/#/home"
SITE_DETAIL_URL = f"{SITE_BASE_URL}/hant/#/allEvents/detail?projectId={{project_token}}"
TICKET_URL = f"{SITE_BASE_URL}/hant/#/allEvents/detail/selectTicket?activityId={{project_token}}"
API_BASE_URL = os.getenv("HKTICKETING_API_BASE_URL", "https://rest-sig.imaitix.com").rstrip("/")
LANG_TYPE = "3"
CONCERT_CATEGORY_NAME = "演唱會"
CONCERT_CATEGORY_NAMES = {"演唱會", "演唱会"}
SOURCE = "hkt.hkticketing.com"
TASK_ID = "hkticketing:concerts"
TASK_TYPE = "hkticketing_concerts"
TARGET_ID = "concerts"
CONCERTS_TABLE = "hkticketing_concerts"
OCCURRENCES_TABLE = "hkticketing_concert_occurrences"
HONG_KONG_TZ = ZoneInfo("Asia/Hong_Kong")

_SIGNATURE_EXCLUDED_PARAMS = {"langType"}
_COEUS_SPECIAL_PATHS = (
    "/user/loginUser",
    "/pro/projects",
    "/pro/eventDiscount",
    "/pro/prices",
    "/order/detail",
    "/user/userCenter",
    "/user/getDis",
    "/region/getRegionsByParentId",
    "/user/addBearer",
    "/user/modifyDis",
    "/user/modifyUser",
    "/user/userInfoGroupDesensitization",
    "/user/modifyUserBrief",
)


def _collapse(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _html_text(value: Any) -> str:
    """将 HKT 项目详情 HTML 转成可搜索的正文，去除脚本和样式。"""
    text = str(value or "")
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(
        r"<(?:br|hr|/p|/div|/li|/pre|/h[1-6])\b[^>]*>",
        "\n",
        text,
        flags=re.I,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text).replace("\xa0", " ")
    lines = [_collapse(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
    else:
        text = _collapse(value)
        if not re.fullmatch(r"\d{9,}(?:\.\d+)?", text):
            return None
        stamp = float(text)
    if stamp > 10_000_000_000:
        stamp /= 1000
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_chinese_datetime(value: Any) -> datetime | None:
    """解析 HKT 的 ``2026年11月21日 (星期六) 下午5時`` 展示日期。"""
    timestamp = _parse_timestamp(value)
    if timestamp is not None:
        return timestamp
    if value is None:
        return None
    text = _collapse(value).replace("／", "/")
    iso_text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=HONG_KONG_TZ)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    date_match = re.search(
        r"(?P<year>\d{4})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})\s*(?:月|[-/.])\s*"
        r"(?P<day>\d{1,2})\s*(?:日)?",
        text,
    )
    if not date_match:
        return None
    year = int(date_match.group("year"))
    month = int(date_match.group("month"))
    day = int(date_match.group("day"))
    time_match = re.search(
        r"(?P<period>上午|下午|中午|晚上|凌晨)?\s*(?P<hour>\d{1,2})"
        r"(?:\s*(?:時|时|:|：)\s*(?P<minute>\d{1,2}))?\s*(?:分)?",
        text[date_match.end():],
    )
    hour = 0
    minute = 0
    if time_match:
        hour = int(time_match.group("hour"))
        minute = int(time_match.group("minute") or 0)
        period = time_match.group("period") or ""
        if period in {"下午", "晚上"} and hour < 12:
            hour += 12
        elif period in {"上午", "凌晨"} and hour == 12:
            hour = 0
        elif period == "中午" and hour == 0:
            hour = 12
    try:
        local = datetime(year, month, day, hour, minute, tzinfo=HONG_KONG_TZ)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def _parse_event_start(event: dict[str, Any], project: dict[str, Any]) -> datetime | None:
    for key in ("eventCaption", "eventStartTime", "eventOriginalStartTime", "startTime"):
        parsed = _parse_chinese_datetime(event.get(key))
        if parsed is not None:
            return parsed
    return _parse_chinese_datetime(project.get("startTime"))


def _parse_event_end(event: dict[str, Any], start_at: datetime) -> datetime | None:
    for key in ("eventEndTime", "eventOriginalEndTime"):
        parsed = _parse_chinese_datetime(event.get(key))
        if parsed is not None and parsed > start_at:
            return parsed
    return None


def _occurrence_id(concert_id: str, source_event_id: str) -> str:
    raw = "\0".join((concert_id, source_event_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _promoter(project_detail: str) -> str:
    text = _html_text(project_detail)
    for line in text.splitlines():
        match = re.match(r"^\s*(?:主辦|主办)\s*[:：]\s*(.+?)\s*$", line)
        if match:
            return _collapse(match.group(1))
    match = re.search(r"(?:主辦|主办)\s*[:：]\s*([^\n]+)", text)
    return _collapse(match.group(1)) if match else ""


def _ticket_status(event: dict[str, Any]) -> str:
    prices = event.get("priceVoList") or []
    sell_out_values = [p.get("sellOut") for p in prices if isinstance(p, dict) and isinstance(p.get("sellOut"), bool)]
    if event.get("sellOut") is True or sell_out_values and all(sell_out_values):
        return "unavailable"
    if event.get("sellOut") is False or any(value is False for value in sell_out_values):
        return "available"
    return "unknown"


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _signature_value(value: Any) -> Any:
    """按 HKT 前端 hecate 签名器的规则规范化参数。"""
    if isinstance(value, dict):
        return {
            str(key): _signature_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_signature_value(item) for item in value]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _hkt_signature(params: dict[str, Any], timestamp_ms: int) -> str:
    """生成页面请求使用的 hecate MD5 签名。

    HKT 的请求封装会在签名之后再把 langType 注入 URL，因此该参数不参与
    签名。其余参数先按键排序并转成字符串，再追加 epeius 时间戳。
    """
    normalized = _signature_value({
        key: value
        for key, value in params.items()
        if key not in _SIGNATURE_EXCLUDED_PARAMS and value is not None
    })
    if not isinstance(normalized, dict):
        raise TypeError("HKT 请求签名参数必须是对象")
    normalized["epeius"] = str(timestamp_ms)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.md5(encoded.encode("utf-8")).hexdigest()


_VOLATILE_HASH_KEYS = {
    "systemTime",
    "countDownEndTime",
    "countDownEndTimeMillis",
    "countDownShowDay",
    "countDownState",
    "saleCount",
    "goodCount",
    "marginCount",
}


def _stable_for_hash(value: Any) -> Any:
    """去掉接口中的倒计时/库存计数，避免每次抓取产生伪变化。"""
    if isinstance(value, dict):
        return {
            key: _stable_for_hash(item)
            for key, item in value.items()
            if key not in _VOLATILE_HASH_KEYS
        }
    if isinstance(value, list):
        return [_stable_for_hash(item) for item in value]
    return value


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _collapse(value).casefold() in {"1", "true", "yes", "y"}


def is_concert_project(project: dict[str, Any]) -> bool:
    """校验分类筛选结果，显式排除停车场等已知非演唱会项目。"""
    type_name = _collapse(project.get("projectTypeName"))
    return type_name not in {"停車場", "停车场", "停车場", "停車场"}


def parse_project_list(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("HKT 项目列表响应缺少 data 对象")
    rows = data.get("dataList")
    if not isinstance(rows, list):
        raise ValueError("HKT 项目列表响应缺少 dataList 数组")
    projects = [row for row in rows if isinstance(row, dict) and row.get("projectToken") and is_concert_project(row)]
    return projects, data


def resolve_concert_category(payload: dict[str, Any]) -> str:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("HKT 分类响应缺少 data 数组")
    for row in rows:
        if isinstance(row, dict) and _collapse(row.get("projectTypeName")) in CONCERT_CATEGORY_NAMES:
            category_id = _collapse(row.get("projectTypeId"))
            if category_id:
                return category_id
    raise RuntimeError(f"HKT 分类列表中未找到 {CONCERT_CATEGORY_NAME}，已停止抓取")


def _category_project_count(payload: dict[str, Any], category_id: str) -> int | None:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or _collapse(row.get("projectTypeId")) != category_id:
            continue
        value = row.get("saleProjectCount")
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None
        return max(0, count)
    return None


def _validate_project_page(
    projects: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    expected_total: int | None,
    page: int,
) -> None:
    """阻止被风控降级的短列表被误判为完整结果。"""
    total_row = metadata.get("totalRow")
    try:
        total_row_value = int(total_row) if total_row is not None else None
    except (TypeError, ValueError):
        total_row_value = None
    if expected_total is not None and total_row_value is not None and total_row_value != expected_total:
        raise RuntimeError(
            "HKT 演唱會列表总数校验失败："
            f"分类声明 {expected_total} 场，项目接口返回 {total_row_value} 场"
        )

    next_page = _as_bool(metadata.get("nextPage"))
    try:
        total_page = max(1, int(metadata.get("totalPage") or page))
    except (TypeError, ValueError):
        total_page = page
    if next_page and page >= total_page:
        raise RuntimeError(
            f"HKT 项目列表分页字段不一致：第 {page} 页已达到 totalPage={total_page}，"
            "但 nextPage 仍为 true"
        )
    if not next_page and page < total_page:
        raise RuntimeError(
            f"HKT 项目列表分页字段不一致：第 {page} 页未声明下一页，但 totalPage={total_page}"
        )
    if (
        page == 1
        and not next_page
        and expected_total is not None
        and expected_total != len(projects)
    ):
        raise RuntimeError(
            "HKT 演唱會列表首屏数据不完整："
            f"分类声明 {expected_total} 场，首屏仅解析到 {len(projects)} 场"
        )


def parse_project_detail(
    project: dict[str, Any],
    *,
    now: datetime | None = None,
) -> ConcertDetail:
    token = _collapse(project.get("projectToken"))
    if not token:
        raise ValueError("HKT 项目详情缺少 projectToken")
    now_value = now or datetime.now(timezone.utc)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=HONG_KONG_TZ)
    now_utc = now_value.astimezone(timezone.utc)

    event_url = SITE_DETAIL_URL.format(project_token=token)
    ticket_url = TICKET_URL.format(project_token=token)
    venue = _collapse(project.get("siteName"))
    city = _collapse(project.get("cityName")) or "Hong Kong"
    description = _html_text(project.get("projectDetail"))
    occurrences: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    events = project.get("eventVoList") or []
    if not isinstance(events, list):
        raise ValueError("HKT 项目详情的 eventVoList 不是数组")
    for raw_event in events:
        if not isinstance(raw_event, dict):
            continue
        source_event_id = _collapse(raw_event.get("eventToken"))
        if not source_event_id or source_event_id in seen_event_ids:
            continue
        start_at = _parse_event_start(raw_event, project)
        if start_at is None or start_at <= now_utc:
            continue
        seen_event_ids.add(source_event_id)
        end_at = _parse_event_end(raw_event, start_at)
        occurrences.append({
            "occurrence_id": _occurrence_id(f"hkt:{token}", source_event_id),
            "source_event_id": source_event_id,
            "start_at": start_at,
            "end_at": end_at,
            "display_date": _collapse(raw_event.get("eventCaption")),
            "venue_name": venue,
            "ticket_url": ticket_url,
            "ticket_status": _ticket_status(raw_event),
            "status": "upcoming",
            "raw_data": raw_event,
        })

    raw_data = {
        "project": project,
        "source_url": event_url,
    }
    return ConcertDetail(
        concert_id=f"hkt:{token}",
        title=_collapse(project.get("projectName") or project.get("aliasName")) or f"hkt:{token}",
        artist_name="",
        artist_id="",
        promoter=_promoter(project.get("projectDetail")),
        category_name="Concerts",
        venue_name=venue,
        city=city,
        country="HK",
        event_url=event_url,
        image_url=(urljoin(SITE_BASE_URL, _collapse(project.get("imgUrl")))
                   if _collapse(project.get("imgUrl")) else ""),
        description=description,
        occurrences=tuple(occurrences),
        source_hash=_json_hash(_stable_for_hash(raw_data)),
        raw_data=raw_data,
    )


class HkticketingBrowser:
    """使用项目独立的持久化 Chromium Profile 读取 HKT 公开接口。"""

    def __init__(
        self,
        *,
        headed: bool = False,
        timeout_ms: int = 60_000,
        request_delay_ms: tuple[int, int] = (1_000, 2_500),
    ) -> None:
        low, high = request_delay_ms
        if low < 0 or high < low:
            raise ValueError("request_delay_ms 必须是非负的 (最小值, 最大值) 区间")
        self.headed = headed
        self.timeout_ms = timeout_ms
        self.request_delay_ms = request_delay_ms
        self._pw = None
        self._context = None
        self.page = None
        self._time_diff_ms = 0.0
        self._xsrf_token = ""
        self._xsrf_expires_at = 0.0
        self._mz_session = ""
        self._cna = ""

    def __enter__(self) -> "HkticketingBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("HKT 抓取需要 Playwright；请安装依赖并执行 playwright install chromium") from exc
        self._pw = sync_playwright().start()
        profile = Path(os.getenv("HKTICKETING_PROFILE_DIR", str(ROOT / "data" / "pw_profile_hkticketing")))
        profile.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": not self.headed,
            "locale": "zh-HK",
            "timezone_id": "Asia/Hong_Kong",
            "viewport": {"width": 1280, "height": 900},
        }
        executable = os.getenv("HKTICKETING_CHROMIUM_PATH", "").strip()
        if not executable:
            executable = shutil.which("chromium") or shutil.which("google-chrome") or ""
        if executable:
            kwargs["executable_path"] = executable
        if os.name != "nt":
            kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
        self._context = self._pw.chromium.launch_persistent_context(**kwargs)
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        try:
            self.page.goto(SITE_ALL_EVENTS_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            # Baxia 通过重导航完成验证；请求阶段会再次检查响应并重试。
            pass
        self.page.wait_for_timeout(3_000)
        self._refresh_request_context()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context is not None:
            self._context.close()
        if self._pw is not None:
            self._pw.stop()

    def _base_headers(self) -> dict[str, str]:
        if self.page is None:
            raise RuntimeError("浏览器尚未启动")
        referer = self.page.url.split("#", 1)[0]
        if not referer.startswith(SITE_BASE_URL):
            referer = f"{SITE_BASE_URL}/hant/"
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": SITE_BASE_URL,
            "Referer": referer,
            "User-Agent": self.page.evaluate("navigator.userAgent"),
        }

    def _refresh_request_context(self) -> None:
        """同步 HKT 页面使用的服务器时间、CSRF 和会话上下文。"""
        if self._context is None:
            raise RuntimeError("浏览器尚未启动")
        base_headers = self._base_headers()
        time_url = f"{API_BASE_URL}/system/time?_t={int(time.time() * 1000)}"
        time_response = self._context.request.get(
            time_url,
            headers=base_headers,
            timeout=self.timeout_ms,
        )
        if time_response.status >= 400:
            raise RuntimeError(f"HKT 服务器时间接口 HTTP {time_response.status}")
        try:
            server_seconds = float(time_response.text().strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("HKT 服务器时间接口响应无效") from exc
        self._time_diff_ms = server_seconds * 1000 - time.time() * 1000

        csrf_response = self._context.request.get(
            f"{API_BASE_URL}/api/getCsrfToken",
            headers=base_headers,
            timeout=self.timeout_ms,
        )
        if csrf_response.status >= 400:
            raise RuntimeError(f"HKT CSRF 接口 HTTP {csrf_response.status}")
        try:
            csrf_payload = csrf_response.json()
        except Exception as exc:
            raise RuntimeError("HKT CSRF 接口响应不是 JSON") from exc
        csrf_data = csrf_payload.get("data") if isinstance(csrf_payload, dict) else None
        if not isinstance(csrf_data, dict):
            csrf_data = csrf_payload if isinstance(csrf_payload, dict) else {}
        token = _collapse(csrf_data.get("xsrfToken"))
        if not token:
            raise RuntimeError("HKT CSRF 接口未返回 xsrfToken")
        self._xsrf_token = token
        try:
            expire_seconds = max(1.0, float(csrf_data.get("expireTime") or 60))
        except (TypeError, ValueError):
            expire_seconds = 60.0
        self._xsrf_expires_at = time.time() + max(1.0, expire_seconds - 5.0)
        self._mz_session = _collapse(csrf_response.headers.get("x-mz-session")) or self._mz_session
        cookies = self._context.cookies(SITE_BASE_URL)
        self._cna = next(
            (
                _collapse(cookie.get("value"))
                for cookie in cookies
                if cookie.get("name") == "cna"
            ),
            "",
        )

    def _ensure_request_context(self) -> None:
        if not self._xsrf_token or time.time() >= self._xsrf_expires_at:
            self._refresh_request_context()

    def _signed_headers(
        self,
        path: str,
        params: dict[str, Any],
        *,
        project_token: str = "",
    ) -> dict[str, str]:
        self._ensure_request_context()
        timestamp_ms = int(time.time() * 1000 + self._time_diff_ms)
        headers = self._base_headers()
        headers.update({
            "_r": str(random.randrange(100)),
            "epeius": str(timestamp_ms),
            "hecate": _hkt_signature(params, timestamp_ms),
            "Site": "m",
            "coeus": (
                "10010"
                if any(marker in path for marker in _COEUS_SPECIAL_PATHS)
                else "10086"
            ),
        })
        if self._cna:
            headers["cna"] = self._cna
        if self._xsrf_token:
            headers["X-XSRF-TOKEN"] = self._xsrf_token
        if self._mz_session:
            headers["X-Mz-Session"] = self._mz_session
        if project_token:
            headers["project-id"] = project_token
        return headers

    def _request(self, path: str, params: dict[str, Any], *, attempt: int = 0) -> dict[str, Any]:
        if self._context is None or self.page is None:
            raise RuntimeError("浏览器尚未启动")
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{API_BASE_URL}{path}?{query}" if query else f"{API_BASE_URL}{path}"
        project_token = _collapse(params.get("projectToken")) if path == "/api/pro/project" else ""
        headers = self._signed_headers(path, params, project_token=project_token)
        try:
            response = self._context.request.get(url, headers=headers, timeout=self.timeout_ms)
            if response.status >= 400:
                raise RuntimeError(f"HKT 接口 HTTP {response.status}: {path}")
            self._mz_session = _collapse(response.headers.get("x-mz-session")) or self._mz_session
            payload = response.json()
        except Exception as exc:
            if attempt < 2:
                self.page.wait_for_timeout(1_000)
                self._refresh_request_context()
                return self._request(path, params, attempt=attempt + 1)
            raise RuntimeError(f"HKT 接口请求失败: {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"HKT 接口响应不是 JSON 对象: {path}")
        if payload.get("data") is None or "非法请求" in _collapse(payload.get("msg")):
            if attempt < 2:
                try:
                    self.page.goto(SITE_ALL_EVENTS_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
                except Exception:
                    pass
                self.page.wait_for_timeout(2_000)
                self._refresh_request_context()
                return self._request(path, params, attempt=attempt + 1)
            raise RuntimeError(f"HKT 接口未通过站点校验: {path}")
        return payload

    def fetch_categories(self) -> dict[str, Any]:
        return self._request("/api/proType/getProjectTypeList", {"langType": LANG_TYPE})

    def fetch_projects(self, *, page: int, page_size: int, category_id: str) -> dict[str, Any]:
        if self.request_delay_ms[1] > 0:
            self.page.wait_for_timeout(random.randint(*self.request_delay_ms))
        return self._request(
            "/api/pro/customizableProjects",
            {
                "page": page,
                "pageSize": page_size,
                "projectClass": category_id,
                "city": "",
                "startTime": "",
                "endTime": "",
                "projectName": "",
                "areaId": "",
                "langType": LANG_TYPE,
            },
        )

    def fetch_project(self, project_token: str) -> dict[str, Any]:
        if self.request_delay_ms[1] > 0:
            self.page.wait_for_timeout(random.randint(*self.request_delay_ms))
        payload = self._request(
            "/api/pro/project",
            {"projectToken": project_token, "reqType": 1, "langType": LANG_TYPE},
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"HKT 项目详情缺少 data 对象: {project_token}")
        return data


def _cursor_value(value: Any) -> dict[str, int]:
    if not value:
        return {"page": 1, "index": 0}
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {"page": 1, "index": 0}
    return {
        "page": max(1, int(parsed.get("page") or 1)),
        "index": max(0, int(parsed.get("index") or 0)),
    } if isinstance(parsed, dict) else {"page": 1, "index": 0}


def _save_state(conn: Connection, *, page: int, index: int, status: str, error: str = "") -> None:
    cursor = "" if status == "completed" else json.dumps(
        {"page": max(1, int(page)), "index": max(0, int(index))},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    xhs_storage.update_crawl_state(conn, TASK_ID, TASK_TYPE, TARGET_ID, cursor, status, error)


def mark_started_occurrences(conn: Connection) -> int:
    return _mark_started_occurrences(
        conn,
        concerts_table=CONCERTS_TABLE,
        occurrences_table=OCCURRENCES_TABLE,
    )


def upsert_concert(conn: Connection, detail: ConcertDetail) -> tuple[int, int]:
    return _upsert_concert(
        conn,
        detail,
        concerts_table=CONCERTS_TABLE,
        occurrences_table=OCCURRENCES_TABLE,
        source=SOURCE,
    )


def crawl(
    conn: Connection,
    client: HkticketingBrowser,
    *,
    max_pages: int = 20,
    page_size: int = 12,
    restart: bool = False,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """抓取 HKT 演唱会列表和详情，按项目保存可恢复断点。"""
    if max_pages < 1:
        raise ValueError("max_pages 必须是正整数")
    if page_size < 1 or page_size > 100:
        raise ValueError("page_size 必须在 1 到 100 之间")
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (TASK_ID,))
    try:
        mark_started_occurrences(conn)
        if restart:
            _save_state(conn, page=1, index=0, status="running")
            state = None
        else:
            state = xhs_storage.get_crawl_state(conn, TASK_ID)
            if state and state["status"] == "completed":
                state = None
        cursor = _cursor_value(state["cursor"] if state else "")
        page = cursor["page"]
        index = cursor["index"]
        category_payload = client.fetch_categories()
        category_id = resolve_concert_category(category_payload)
        expected_total = _category_project_count(category_payload, category_id)
        pages = events_found = events_saved = occurrences_saved = 0
        seen_tokens: set[str] = set()
        while pages < max_pages:
            payload = client.fetch_projects(page=page, page_size=page_size, category_id=category_id)
            projects, metadata = parse_project_list(payload)
            try:
                _validate_project_page(
                    projects,
                    metadata,
                    expected_total=expected_total,
                    page=page,
                )
            except Exception as exc:
                _save_state(conn, page=page, index=index, status="failed", error=str(exc)[:1000])
                raise
            pages += 1
            events_found += len(projects)
            if index > len(projects):
                index = 0
            for item_index in range(index, len(projects)):
                item = projects[item_index]
                token = _collapse(item.get("projectToken"))
                if not token or token in seen_tokens:
                    _save_state(conn, page=page, index=item_index + 1, status="running")
                    continue
                seen_tokens.add(token)
                try:
                    detail = parse_project_detail(client.fetch_project(token), now=now)
                    saved_events, saved_occurrences = upsert_concert(conn, detail)
                except Exception as exc:
                    _save_state(conn, page=page, index=item_index, status="failed", error=str(exc)[:1000])
                    raise
                events_saved += saved_events
                occurrences_saved += saved_occurrences
                _save_state(conn, page=page, index=item_index + 1, status="running")
            index = 0
            next_page = _as_bool(metadata.get("nextPage"))
            total_page = int(metadata.get("totalPage") or page)
            if not next_page or page >= total_page:
                _save_state(conn, page=1, index=0, status="completed")
                return {
                    "pages": pages,
                    "events_found": events_found,
                    "events_saved": events_saved,
                    "occurrences_saved": occurrences_saved,
                    "status": "completed",
                }
            page += 1
            _save_state(conn, page=page, index=0, status="running")
        _save_state(conn, page=page, index=0, status="paused")
        return {
            "pages": pages,
            "events_found": events_found,
            "events_saved": events_saved,
            "occurrences_saved": occurrences_saved,
            "status": "paused",
        }
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (TASK_ID,))


def get_upcoming_concerts(conn: Connection, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    rows = conn.execute(
        """SELECT c.concert_id, c.title, c.artist_name, c.artist_id, c.promoter,
                  c.venue_name, c.city, c.event_url, c.image_url,
                  o.occurrence_id, o.start_at, o.display_date, o.ticket_url,
                  o.ticket_status, o.status
           FROM hkticketing_concerts c
           JOIN hkticketing_concert_occurrences o ON o.concert_id = c.concert_id
           WHERE o.status='upcoming' AND o.start_at > CURRENT_TIMESTAMP
           ORDER BY o.start_at ASC, c.title ASC, c.concert_id ASC
           LIMIT %s""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def cmd_crawl(args: argparse.Namespace) -> int:
    conn = connect(initialize=True)
    try:
        with HkticketingBrowser(headed=args.headed) as client:
            result = crawl(
                conn,
                client,
                max_pages=args.max_pages,
                page_size=args.page_size,
                restart=args.restart,
            )
    except Exception as exc:
        print(f"[ERR] HKT 抓取失败: {exc}", file=sys.stderr)
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
        print("没有找到尚未开始的 HKT 演唱会。")
        return 0
    for row in rows:
        start_at = row["start_at"].astimezone(HONG_KONG_TZ).strftime("%Y-%m-%d %H:%M")
        ticket = row["ticket_url"] or "（暂无购票链接）"
        print(f"{start_at} | {row['title']} | {row['venue_name']} | {ticket}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hkticketing", description="HKT 香港快達票演唱会抓取器")
    sub = parser.add_subparsers(dest="command", required=True)
    crawl_parser = sub.add_parser("crawl", help="抓取演唱会列表和详情")
    crawl_parser.add_argument("--max-pages", type=int, default=20, help="本次最多抓取列表页数（默认 20）")
    crawl_parser.add_argument("--page-size", type=int, default=12, help="每页项目数（默认 12，最大 100）")
    crawl_parser.add_argument("--restart", action="store_true", help="忽略已保存断点，从第一页重新发现活动")
    crawl_parser.add_argument("--headed", action="store_true", help="显示浏览器窗口，便于排查 Baxia 验证")
    crawl_parser.set_defaults(func=cmd_crawl)
    list_parser = sub.add_parser("list", help="列出数据库中尚未开始的 HKT 演唱会")
    list_parser.add_argument("--limit", type=int, default=100)
    list_parser.set_defaults(func=cmd_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
