"""Exercise the actual HTTP authentication boundary without requiring PostgreSQL."""

import sys
from pathlib import Path
from time import time
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web/backend"))
import auth
import main

TEST_KEY = "test-only-api-key-0123456789abcdef0123456789"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_AUTH_KEY", TEST_KEY)
    monkeypatch.setenv("API_TOKEN_TTL_SECONDS", "7200")
    auth.get_auth_settings.cache_clear()
    # Unauthorized requests must never reach PostgreSQL, including settings writes.
    monkeypatch.setattr(main, "get_connection", MagicMock(side_effect=AssertionError("DB accessed")))
    try:
        with TestClient(main.app) as instance:
            yield instance
    finally:
        auth.get_auth_settings.cache_clear()


def token_headers(client):
    response = client.post("/api/auth/token", headers={"X-API-Key": TEST_KEY})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def signed_token(**changes):
    now = int(time())
    payload = {"sub": "api-client", "iat": now, "exp": now + 7200,
               "iss": auth.ISSUER, "aud": auth.AUDIENCE}
    payload.update(changes)
    return jwt.encode(payload, TEST_KEY, algorithm="HS256")


def test_key_exchange_returns_two_hour_token(client):
    response = client.post("/api/auth/token", headers={"X-API-Key": TEST_KEY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    data = response.json()
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 7200
    payload = jwt.decode(data["access_token"], TEST_KEY, algorithms=["HS256"],
                         audience=auth.AUDIENCE, issuer=auth.ISSUER)
    assert payload["exp"] - payload["iat"] == 7200
    assert TEST_KEY not in response.text


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"},
                                    {"Authorization": f"Bearer {TEST_KEY}"}])
def test_missing_or_wrong_key_cannot_obtain_token(client, headers):
    assert client.post("/api/auth/token", headers=headers).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/notes"), ("GET", "/api/notes/example"),
    ("GET", "/api/merchants"), ("GET", "/api/merchants/example"),
    ("GET", "/api/merchant-categories"), ("GET", "/api/merchant-tags"),
    ("GET", "/api/settings"), ("PUT", "/api/settings"),
])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": TEST_KEY},
                                    {"Authorization": f"Bearer {TEST_KEY}"}])
def test_all_data_routes_require_token_before_db_access(client, method, path, headers):
    response = client.request(method, path, headers=headers, json={} if method == "PUT" else None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    main.get_connection.assert_not_called()


@pytest.mark.parametrize("token", [
    "garbage", signed_token(exp=int(time()) - 1),
    signed_token(aud="another-app"), signed_token(iss="another-issuer"),
    signed_token(iat=int(time()) + 3600),
    jwt.encode({"sub": "api-client"}, TEST_KEY, algorithm="HS256"),
    jwt.encode({"exp": int(time()) + 7200}, "different-key-0123456789abcdef0123456789", algorithm="HS256"),
    jwt.encode({"exp": int(time()) + 7200}, "", algorithm="none"),
    jwt.encode({"exp": int(time()) + 7200}, TEST_KEY + "-extra", algorithm="HS384"),
])
def test_expired_forged_or_malformed_tokens_are_rejected(client, token):
    response = client.get("/api/notes", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    main.get_connection.assert_not_called()


def test_valid_token_can_read_and_write_settings(client, monkeypatch):
    connection = MagicMock()
    monkeypatch.setattr(main, "get_connection", lambda: connection)
    monkeypatch.setattr(main, "read_config", lambda conn: main.ReaderConfig(["旅行"], [], ""))
    monkeypatch.setattr(main, "write_config", lambda conn, config: config)
    headers = token_headers(client)
    response = client.get("/api/settings", headers=headers)
    assert response.status_code == 200
    assert response.json()["keywords"] == ["旅行"]
    response = client.put("/api/settings", headers=headers,
                          json={"keywords": ["美食"], "bloggers": [], "cookies": ""})
    assert response.status_code == 200
    assert response.json()["keywords"] == ["美食"]


def test_configurable_ttl_and_key_rotation(client, monkeypatch):
    old_headers = token_headers(client)
    monkeypatch.setenv("API_AUTH_KEY", "rotated-key-0123456789abcdef0123456789")
    monkeypatch.setenv("API_TOKEN_TTL_SECONDS", "60")
    auth.get_auth_settings.cache_clear()  # Models settings reload on process restart.
    assert client.get("/api/notes", headers=old_headers).status_code == 401
    assert client.post("/api/auth/token", headers={"X-API-Key": TEST_KEY}).status_code == 401
    response = client.post("/api/auth/token", headers={"X-API-Key": auth.get_auth_settings().api_key})
    assert response.status_code == 200
    assert response.json()["expires_in"] == 60


def test_cors_allows_token_exchange_and_data_preflight(client):
    for method, headers, path in [("POST", "x-api-key", "/api/auth/token"),
                                  ("PUT", "authorization,content-type", "/api/settings")]:
        response = client.options(path, headers={
            "Origin": main.origins[0], "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        })
        assert response.status_code == 200
    main.get_connection.assert_not_called()


def test_health_check_remains_accessible_without_token(client, monkeypatch):
    monkeypatch.setattr(main, "get_connection", MagicMock(return_value=MagicMock()))
    assert client.get("/api/health").json() == {"status": "ok"}


@pytest.mark.parametrize("key,ttl", [("", "7200"), ("short", "7200"),
                                    (TEST_KEY, "0"), (TEST_KEY, "-1"), (TEST_KEY, "abc")])
def test_invalid_config_refuses_startup(monkeypatch, key, ttl):
    monkeypatch.setenv("API_AUTH_KEY", key)
    monkeypatch.setenv("API_TOKEN_TTL_SECONDS", ttl)
    auth.get_auth_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError):
            with TestClient(main.app):
                pass
    finally:
        auth.get_auth_settings.cache_clear()
