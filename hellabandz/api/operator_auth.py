"""Single-operator API access: bearer keys for tools, expiring HttpOnly browser sessions."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

COOKIE = "qs_session"
sessions: dict[str, float] = {}
attempts: dict[str, deque] = defaultdict(deque)
router = APIRouter(prefix="/v1/auth")


def required() -> bool:
    return os.getenv("QS_AUTH_REQUIRED", "1") != "0"


def origins() -> list[str]:
    return [s.strip() for s in os.getenv("QS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001").split(",") if s.strip()]


def validate_configuration():
    if required() and len(os.getenv("QS_API_TOKEN", "")) < 32:
        raise RuntimeError("Set QS_API_TOKEN to a random key of at least 32 characters; run make auth-init")
    if "*" in origins():
        raise RuntimeError("QS_ALLOWED_ORIGINS must list explicit trusted UI origins")


def bearer_ok(headers) -> bool:
    token = os.getenv("QS_API_TOKEN", "")
    return len(token) >= 32 and hmac.compare_digest(headers.get("authorization", ""), "Bearer " + token)


def authenticated(headers, cookies) -> bool:
    if not required() or bearer_ok(headers):
        return True
    key = hashlib.sha256(cookies.get(COOKIE, "").encode()).hexdigest()
    return sessions.get(key, 0) > time.time()


class AccessControl:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if os.getenv("QS_AUTH_MODE", "accounts") != "operator" or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:
            pass
        cookies = {k: v.value for k, v in cookie.items()}
        allowed_origin = not origin or origin in origins()
        public = scope["path"] in ("/healthz", "/v1/auth/status", "/v1/auth/session")
        permitted = authenticated(headers, cookies)
        if scope["type"] == "websocket":
            # Cookie-authenticated browser streams require a trusted Origin.
            if not allowed_origin or not permitted or required() and not origin and not bearer_ok(headers):
                return await send({"type": "websocket.close", "code": 1008, "reason": "authentication required"})
        elif scope["method"] != "OPTIONS" and (not allowed_origin or not public and not permitted):
            status = 403 if not allowed_origin else 401
            return await JSONResponse({"detail": "untrusted browser origin" if status == 403 else "sign in to HellaBandz"},
                                      status_code=status)(scope, receive, send)
        return await self.app(scope, receive, send)


@router.get("/status")
def status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if os.getenv("QS_AUTH_MODE", "accounts") != "operator":
        return {"mode": "accounts", "required": os.getenv("QS_AUTH_DISABLED", "").lower() != "true", "authenticated": False}
    return {"mode": "operator", "required": required(), "authenticated": authenticated(request.headers, request.cookies)}


@router.post("/session")
def login(request: Request, response: Response):
    host = request.client.host if request.client else "unknown"
    recent = attempts[host]
    now = time.time()
    while recent and recent[0] < now - 60:
        recent.popleft()
    if len(recent) >= 5:
        raise HTTPException(429, "too many sign-in attempts; wait one minute")
    if not bearer_ok(request.headers):
        recent.append(now)
        raise HTTPException(401, "invalid access key")
    recent.clear()
    for key, expiry in list(sessions.items()):
        if expiry <= now:
            del sessions[key]
    value = secrets.token_urlsafe(32)
    sessions[hashlib.sha256(value.encode()).hexdigest()] = now + 8 * 3600
    response.set_cookie(COOKIE, value, max_age=8 * 3600, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}


@router.delete("/session")
def logout(request: Request, response: Response):
    sessions.pop(hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest(), None)
    response.delete_cookie(COOKIE, path="/")
    return {"authenticated": False}
