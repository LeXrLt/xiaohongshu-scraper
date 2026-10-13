"""Environment-configured API key exchange and short-lived bearer tokens."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from functools import lru_cache
from time import time
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

ISSUER = "xhs-reader-api"
AUDIENCE = "xhs-reader"
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@dataclass(frozen=True)
class AuthSettings:
    api_key: str
    token_ttl_seconds: int


@lru_cache(maxsize=1)
def get_auth_settings() -> AuthSettings:
    key = os.getenv("API_AUTH_KEY", "").strip()
    if len(key.encode("utf-8")) < 32:
        raise RuntimeError("Set API_AUTH_KEY in .env to a random key of at least 32 bytes")
    try:
        ttl = int(os.getenv("API_TOKEN_TTL_SECONDS", "7200"))
    except ValueError as exc:
        raise RuntimeError("API_TOKEN_TTL_SECONDS must be a positive integer") from exc
    if ttl <= 0:
        raise RuntimeError("API_TOKEN_TTL_SECONDS must be a positive integer")
    return AuthSettings(api_key=key, token_ttl_seconds=ttl)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/token", response_model=TokenResponse)
def exchange_token(
    response: Response,
    api_key: Annotated[str | None, Depends(api_key_header)],
    settings: Annotated[AuthSettings, Depends(get_auth_settings)],
) -> TokenResponse:
    if api_key is None or not hmac.compare_digest(
        api_key.encode("utf-8"), settings.api_key.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")
    now = int(time())
    token = jwt.encode(
        {"sub": "api-client", "iat": now, "exp": now + settings.token_ttl_seconds,
         "iss": ISSUER, "aud": AUDIENCE},
        settings.api_key, algorithm="HS256",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenResponse(access_token=token, expires_in=settings.token_ttl_seconds)


def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[AuthSettings, Depends(get_auth_settings)],
) -> None:
    if credentials is not None:
        try:
            jwt.decode(
                credentials.credentials, settings.api_key, algorithms=["HS256"],
                issuer=ISSUER, audience=AUDIENCE,
                options={"require": ["sub", "iat", "exp", "iss", "aud"]},
            )
            return
        except jwt.InvalidTokenError:
            pass
    raise HTTPException(
        status_code=401, detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
