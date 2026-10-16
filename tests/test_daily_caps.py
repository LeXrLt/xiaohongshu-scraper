"""每日 API 上限可关闭；显式正数配置仍会拦截。"""

from unittest.mock import Mock

import pytest

import xhs_config
from xhs_fetcher import FatalRiskError, Fetcher


@pytest.fixture
def fetcher(monkeypatch):
    fetcher = Fetcher.__new__(Fetcher)
    fetcher.request_count = 10001
    fetcher._window_request_count = 0
    for name in ("_maybe_session_rest", "_predictive_cookie_check", "warmup", "_throttle"):
        monkeypatch.setattr(fetcher, name, Mock())
    monkeypatch.setattr(fetcher, "_should_rotate_for_freshness", Mock(return_value=False))
    monkeypatch.setattr(fetcher, "_call_raw", Mock(return_value={"success": True}))
    monkeypatch.setattr(xhs_config, "DAILY_HARD_CAP", 0)
    monkeypatch.setattr(xhs_config, "IP_DAILY_CAP", 0)
    return fetcher


def test_disabled_caps_allow_requests_above_old_limit(fetcher):
    assert fetcher.get("/test") == {"success": True}
    fetcher._call_raw.assert_called_once()


@pytest.mark.parametrize("cap,message", [
    ("DAILY_HARD_CAP", "单账号日抓硬上限"),
    ("IP_DAILY_CAP", "IP 日抓总上限"),
])
@pytest.mark.parametrize("count", [80, 81])
def test_positive_caps_still_block(fetcher, monkeypatch, cap, message, count):
    monkeypatch.setattr(xhs_config, cap, 80)
    fetcher.request_count = count
    with pytest.raises(FatalRiskError, match=message):
        fetcher.get("/test")
    fetcher._call_raw.assert_not_called()


def test_positive_caps_allow_requests_below_limit(fetcher, monkeypatch):
    monkeypatch.setattr(xhs_config, "DAILY_HARD_CAP", 80)
    monkeypatch.setattr(xhs_config, "IP_DAILY_CAP", 80)
    fetcher.request_count = 79
    assert fetcher.get("/test") == {"success": True}


def test_dom_fallback_allows_disabled_ip_cap(fetcher):
    assert fetcher._dom_search_fallback("/search", None)["code"] == -104


def test_dom_fallback_enforces_positive_ip_cap(fetcher, monkeypatch):
    monkeypatch.setattr(xhs_config, "IP_DAILY_CAP", 80)
    with pytest.raises(FatalRiskError, match="IP 日抓总上限"):
        fetcher._dom_search_fallback("/search", None)
