"""浏览器搜索的默认启动模式。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import xhs_config
from xhs_fetcher import PlaywrightDomSearch


def test_dom_search_defaults_to_playwright(monkeypatch):
    calls = []
    monkeypatch.setattr(xhs_config, "DOM_BROWSER_MODE", "playwright")
    monkeypatch.setattr(
        PlaywrightDomSearch, "_find_real_browser",
        lambda: (_ for _ in ()).throw(AssertionError("不应检测本机浏览器")),
    )
    monkeypatch.setattr(
        PlaywrightDomSearch, "_init_playwright",
        lambda self, cookies, headless: calls.append((cookies, headless)),
    )

    PlaywrightDomSearch({"a1": "test"})

    assert calls == [({"a1": "test"}, True)]


def test_real_profile_requires_explicit_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(xhs_config, "DOM_BROWSER_MODE", "real_profile")
    monkeypatch.setattr(PlaywrightDomSearch, "_find_real_browser", staticmethod(lambda: None))
    monkeypatch.setattr(
        PlaywrightDomSearch, "_init_playwright",
        lambda self, cookies, headless: calls.append((cookies, headless)),
    )

    PlaywrightDomSearch({"a1": "test"})

    assert calls == [({"a1": "test"}, True)]


def test_playwright_homepage_does_not_wait_for_domcontentloaded(monkeypatch, tmp_path):
    import playwright.sync_api

    pw = MagicMock()
    page = pw.chromium.launch_persistent_context.return_value.new_page.return_value
    page.goto.return_value = SimpleNamespace(status=200)
    page.url = "https://www.xiaohongshu.com/explore"
    sync = MagicMock()
    sync.start.return_value = pw
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: sync)
    monkeypatch.setattr(xhs_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(xhs_config, "DOM_BROWSER_MODE", "playwright")
    monkeypatch.setattr(PlaywrightDomSearch, "_inject_cookies", lambda *args, **kwargs: None)

    PlaywrightDomSearch({})

    page.goto.assert_called_once_with(
        xhs_config.WEB_BASE + "/", wait_until="commit", timeout=30000
    )


def test_search_accepts_api_results_without_domcontentloaded():
    search = PlaywrightDomSearch.__new__(PlaywrightDomSearch)
    search._mode = "playwright"
    search._page = MagicMock()
    search._page.goto.return_value = SimpleNamespace(status=200)

    def on_goto(*args, **kwargs):
        response_handler = search._page.on.call_args.args[1]
        response_handler(SimpleNamespace(
            url="https://edith.xiaohongshu.com/api/sns/web/v1/search/notes",
            status=200,
            json=lambda: {"success": True, "data": {"items": [{"id": "note_1"}]}},
        ))
        return SimpleNamespace(status=200)

    search._page.goto.side_effect = on_goto
    result = search.search("香港咖啡")

    assert result["items"] == [{"id": "note_1"}]
    assert search._page.goto.call_args.kwargs["wait_until"] == "commit"
    search._page.remove_listener.assert_called_once()


def test_search_pauses_when_page_has_no_result_evidence(monkeypatch):
    search = PlaywrightDomSearch.__new__(PlaywrightDomSearch)
    search._mode = "playwright"
    search._page = MagicMock()
    search._page.goto.return_value = SimpleNamespace(status=200)
    monkeypatch.setattr(search, "_extract_state_js", lambda: None)
    monkeypatch.setattr(search, "_extract_state_html", lambda: None)
    monkeypatch.setattr(search, "_extract_from_dom", lambda: {"items": [], "has_more": False})

    with pytest.raises(RuntimeError, match="搜索页未加载出结果"):
        search.search("香港咖啡")

    assert search._page.wait_for_timeout.call_count == 3
    search._page.remove_listener.assert_called_once()
