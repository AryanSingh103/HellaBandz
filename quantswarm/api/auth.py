"""Supabase identity verification and application resource authorization.

Financial data remains in TimescaleDB; Supabase owns identity only. Legacy
resources are deliberately unowned and are never claimed by a new account.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from starlette.requests import HTTPConnection


@dataclass(frozen=True)
class User:
    id: str
    email: str = ""
    scope: dict | None = None


identity: ContextVar[User | None] = ContextVar("identity", default=None)


def disabled() -> bool:
    return os.environ.get("QS_AUTH_DISABLED", "").lower() == "true"


def current_user() -> User | None:
    return identity.get()


def settings() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY") or os.environ.get("SUPABASE_ANON_KEY", "")
    if not url.startswith("https://") or not key:
        raise HTTPException(503, "Account authentication is not configured. Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY on the API.")
    return url, key


def internal_token(owner: str, job: dict) -> str:
    secret = os.environ.get("QS_INTERNAL_SIGNING_KEY", "")
    if len(secret) < 32:
        raise RuntimeError("QS_INTERNAL_SIGNING_KEY must contain at least 32 characters for managed jobs")
    scope = {"sub": owner, "exp": int(time.time()) + 86400,
             "portfolio": job.get("portfolio_id"), "run": job["run_id"], "kind": job["kind"]}
    payload = base64.urlsafe_b64encode(json.dumps(scope, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"qsjob.{payload}.{signature}"


async def verify_token(token: str) -> User:
    if token.startswith("qsjob."):
        try:
            _, payload, signature = token.split(".")
            secret = os.environ.get("QS_INTERNAL_SIGNING_KEY", "")
            if len(secret) < 32 or not hmac.compare_digest(signature, hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()):
                raise ValueError()
            scope = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if scope["exp"] <= time.time():
                raise ValueError()
            return User(str(uuid.UUID(scope["sub"])), scope=scope)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(401, "Invalid or expired job credentials")
    url, key = settings()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url + "/auth/v1/user", headers={"apikey": key, "Authorization": "Bearer " + token})
    except httpx.HTTPError:
        raise HTTPException(503, "Authentication provider is unavailable; retry sign in")
    if response.status_code in (401, 403):
        raise HTTPException(401, "Session expired or invalid; sign in again", headers={"WWW-Authenticate": "Bearer"})
    if response.status_code != 200:
        raise HTTPException(503, "Authentication provider is unavailable")
    try:
        data = response.json()
        if data.get("is_anonymous"):
            raise ValueError()
        return User(str(uuid.UUID(data["id"])), data.get("email") or "")
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "A registered account is required")


async def owned(kind: str, resource: str) -> None:
    user = current_user()
    if user is None and disabled():
        return
    if user is None:
        raise HTTPException(401, "Sign in required")
    from quantswarm.api import app as gateway
    rows = await gateway.q("SELECT owner_id FROM account_resources WHERE kind = %s AND resource_id = %s", (kind, resource))
    if not rows or str(rows[0]["owner_id"]) != user.id:
        raise HTTPException(404, "Resource not found in this account")
    if user.scope and resource != user.scope.get(kind):
        raise HTTPException(403, "Job credential is outside its resource scope")


async def claim(conn, kind: str, resource: str) -> None:
    user = current_user()
    if user is None and disabled():
        return
    if user is None or user.scope:
        raise HTTPException(403, "Account identity required to create resources")
    # Existing pre-auth data stays inaccessible. Never adopt it by guessing IDs.
    table, column = {"portfolio": ("managed_workers", "portfolio_id"), "run": ("tournaments", "run_id"),
                     "job": ("jobs", "job_id"), "request": ("jobs", "request_id")}[kind]
    if kind == "portfolio":
        found = await (await conn.execute("SELECT 1 FROM portfolio_state WHERE portfolio_id = %s UNION ALL SELECT 1 FROM commands WHERE portfolio_id = %s LIMIT 1", (resource, resource))).fetchone()
        if found:
            raise HTTPException(409, "Choose a fresh resource ID")
    existing = await (await conn.execute(f"SELECT 1 FROM {table} WHERE {column} = %s", (resource,))).fetchone()
    if existing:
        raise HTTPException(409, "Choose a fresh resource ID")
    row = await (await conn.execute("INSERT INTO account_resources(kind, resource_id, owner_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING owner_id", (kind, resource, user.id))).fetchone()
    if not row:
        raise HTTPException(409, "Choose a fresh resource ID")


async def authorize(request: HTTPConnection) -> None:
    identity.set(None)
    if request.scope["type"] == "websocket":
        return  # The stream authenticates its first frame before reading data.
    if request.url.path == "/healthz":
        return
    if disabled():
        return
    settings()
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not header[7:].strip():
        raise HTTPException(401, "Sign in required", headers={"WWW-Authenticate": "Bearer"})
    user = await verify_token(header[7:])
    identity.set(user)
    path = request.url.path
    if user.scope:
        if request.method != "POST" and path in {"/v1/tournaments", "/v1/agent-messages"}:
            raise HTTPException(403, "Job credentials may only publish their scoped research records")
        allowed = {"/v1/agent-messages", "/v1/tournaments"}
        if user.scope["kind"] == "session":
            allowed |= {"/v1/runtime", "/v1/admin/portfolio", "/v1/admin/allocations", "/v1/admin/kill-switch", "/v1/orders", "/v1/bars", "/v1/portfolio", "/v1/verify"}
        if path not in allowed and not (user.scope["kind"] == "session" and path.startswith("/v1/commands/")):
            raise HTTPException(403, "Endpoint is unavailable to job credentials")
    for key in ("portfolio_id", "run_id"):
        if len(request.query_params.getlist(key)) > 1:
            raise HTTPException(422, f"{key} must appear only once")
    body = {}
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            pass  # FastAPI performs normal body validation.
        if not isinstance(body, dict):
            body = {}
    # A portfolio parameter with a default must be authorized even if omitted.
    route = request.scope.get("route")
    defaults = {p.name: p.default for p in getattr(getattr(route, "dependant", None), "query_params", [])}
    for key in ("portfolio_id", "run_id"):
        sources = [request.path_params.get(key), request.query_params.get(key), body.get(key)]
        values = [value for value in sources if isinstance(value, str) and value]
        if len(set(values)) > 1:
            raise HTTPException(422, f"Body, path, and query {key} scopes must match")
    pid = request.query_params.get("portfolio_id") or body.get("portfolio_id") or defaults.get("portfolio_id")
    fresh_portfolio = request.method == "POST" and (path == "/v1/portfolios" or path == "/v1/jobs")
    if isinstance(pid, str) and pid and not fresh_portfolio:
        await owned("portfolio", pid)
    rid = request.path_params.get("run_id") or request.query_params.get("run_id") or body.get("run_id")
    if isinstance(rid, str) and rid:
        if not (request.method == "POST" and path == "/v1/tournaments" and not user.scope):
            await owned("run", rid)
    jid = request.path_params.get("job_id")
    if isinstance(jid, str):
        await owned("job", jid)
    # Both query and body can carry scope. Reject mismatches rather than checking only one.
    if body.get("portfolio_id") and request.query_params.get("portfolio_id") and body["portfolio_id"] != request.query_params["portfolio_id"]:
        raise HTTPException(422, "Body and query portfolio scopes must match")
