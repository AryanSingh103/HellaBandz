"""HTTP, browser sessions and streams must all enforce the same access boundary."""
import hashlib
import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hellabandz.api import operator_auth as auth


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("QS_AUTH_MODE", "operator")
    monkeypatch.setenv("QS_AUTH_REQUIRED", "1")
    monkeypatch.setenv("QS_API_TOKEN", "test-operator-key-" + "a" * 40)
    auth.sessions.clear()
    auth.attempts.clear()
    app = FastAPI()
    app.add_middleware(auth.AccessControl)
    app.include_router(auth.router)

    @app.get("/v1/private")
    def private():
        return {"secret": True}

    @app.post("/v1/private")
    def write():
        return {"written": True}

    @app.websocket("/v1/stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        await ws.send_json({"ok": True})
        await ws.close()

    return TestClient(app), {"Authorization": "Bearer " + "test-operator-key-" + "a" * 40}


def test_api_auth_fail_closed_http_and_config(protected, monkeypatch):
    c, headers = protected
    for method in (c.get, c.post):
        assert method("/v1/private").status_code == 401
        assert method("/v1/private", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert method("/v1/private", headers=headers).status_code == 200
    assert c.get("/v1/auth/status").json() == {"mode": "operator", "required": True, "authenticated": False}
    monkeypatch.delenv("QS_API_TOKEN")
    with pytest.raises(RuntimeError, match="QS_API_TOKEN"):
        auth.validate_configuration()
    monkeypatch.setenv("QS_API_TOKEN", "a" * 40)
    monkeypatch.setenv("QS_ALLOWED_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="explicit"):
        auth.validate_configuration()


def test_api_browser_session_cookie_logout_expiry_and_csrf(protected):
    c, headers = protected
    origin = {"Origin": "http://localhost:3000"}
    result = c.post("/v1/auth/session", headers={**headers, **origin})
    assert result.status_code == 200
    cookie = result.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/v1/private", headers=origin).status_code == 200
    assert c.post("/v1/private", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/v1/auth/session", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    value = c.cookies.get(auth.COOKIE)
    auth.sessions[hashlib.sha256(value.encode()).hexdigest()] = time.time() - 1
    assert c.get("/v1/private").status_code == 401
    c.post("/v1/auth/session", headers=headers)
    assert c.delete("/v1/auth/session").status_code == 200
    assert c.get("/v1/private").status_code == 401


def test_api_websocket_auth_and_origin(protected):
    c, headers = protected
    for request_headers in ({}, {"Origin": "http://localhost:3000"}, {**headers, "Origin": "https://evil.example"}):
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/v1/stream", headers=request_headers):
                pass
    with c.websocket_connect("/v1/stream", headers=headers) as ws:
        assert ws.receive_json()["ok"]
    c.post("/v1/auth/session", headers=headers)
    with c.websocket_connect("/v1/stream", headers={"Origin": "http://localhost:3000"}) as ws:
        assert ws.receive_json()["ok"]
    # Cookies alone cannot authorize a non-browser stream that omits Origin.
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/v1/stream"):
            pass


def test_api_login_rate_limit(protected):
    c, headers = protected
    for _ in range(5):
        assert c.post("/v1/auth/session").status_code == 401
    assert c.post("/v1/auth/session", headers=headers).status_code == 429
