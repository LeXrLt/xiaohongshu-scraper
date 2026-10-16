"""浏览器搜索的默认启动模式。"""

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
