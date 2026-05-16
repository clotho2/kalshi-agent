"""Bearer-token auth for /api/control/*. Observer routes use require_observer_auth."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def _is_localhost(request: Request) -> bool:
    client = request.client
    if not client:
        return False
    return client.host in {"127.0.0.1", "::1", "localhost"}


def require_control_auth(expected_token: str):
    async def _dep(request: Request) -> None:
        token = _bearer(request.headers.get("authorization"))
        if token is None or not hmac.compare_digest(token, expected_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")
    return _dep


def require_observer_auth(expected_token: str):
    """Localhost: no auth required. Remote: bearer token required."""
    async def _dep(request: Request) -> None:
        if _is_localhost(request):
            return
        token = _bearer(request.headers.get("authorization"))
        if token is None or not hmac.compare_digest(token, expected_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required for remote access")
    return _dep
