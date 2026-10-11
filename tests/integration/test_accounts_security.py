"""Real application DB + HTTP authorization with mocked identity/exchange networks."""
import asyncio
import base64
import json
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quantswarm.api.accounts import signed_headers
from quantswarm.api.auth import internal_token
from quantswarm.ledger import canonical, sha256_hex

pytestmark = pytest.mark.db
ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def secure(stack, monkeypatch):
    from quantswarm.api.app import app
    monkeypatch.setenv("DATABASE_URL", stack.url)
    monkeypatch.setenv("QS_AUTH_DISABLED", "false")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "public-test-key")
    monkeypatch.setenv("QS_INTERNAL_SIGNING_KEY", "test-internal-signing-key-32-characters-minimum")
    monkeypatch.setenv("QS_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("QS_KALSHI_DEMO_ORDERS_ENABLED", "true")
    original_client = httpx.AsyncClient
    calls = []
    state = {"timeout_order": False, "auth_outage": False, "reject_key": False}

    def provider(request):
        calls.append(request)
        if request.url.host == "test.supabase.co":
            if state["auth_outage"]:
                raise httpx.ConnectError("provider unavailable", request=request)
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            who = {"alice": ALICE, "bob": BOB}.get(token)
            return httpx.Response(200, json={"id": who, "email": token + "@example.com"}) if who else httpx.Response(401)
        if request.url.path.endswith("/portfolio/balance"):
            if state["reject_key"]:
                return httpx.Response(401, json={"detail": "secret provider detail"})
            return httpx.Response(200, json={"balance": 500, "portfolio_value": 100})
        if request.url.path.endswith("/portfolio/events/orders"):
            if state["timeout_order"]:
                raise httpx.ReadTimeout("Uncertain exchange outcome", request=request)
            body = json.loads(request.content)
            return httpx.Response(201, json={"order_id": "exchange-order", "client_order_id": body["client_order_id"], "fill_count": "1.00", "remaining_count": "0.00"})
        if request.url.path.endswith("/portfolio/positions"):
            return httpx.Response(200, json={"market_positions": []})
        if request.url.path.endswith("/portfolio/orders"):
            assert "status" not in request.url.params  # IOC outcomes must be inspectable.
            return httpx.Response(200, json={"orders": []})
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, json={"markets": [{"ticker": "TEST-YES", "title": "Test market"}]})
        raise AssertionError(str(request.url))

    def mocked_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(provider)
        return original_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", mocked_client)
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as client:
        yield client, stack, calls, state


def headers(who="alice"):
    return {"Authorization": "Bearer " + who}


def provision(client, who="alice"):
    response = client.get("/v1/account", headers=headers(who))
    assert response.status_code == 200, response.text
    return response.json()["default_portfolio_id"]


def manifest(client, who="alice", run="T-alice"):
    value = {"run_id": run, "dataset": {"content_sha256": "test"}}
    response = client.post("/v1/tournaments", headers=headers(who), json={"run_id": run, "manifest": value, "manifest_hash": sha256_hex(canonical(value))})
    assert response.status_code == 200, response.text
    return run


def pem_key():
    return ed25519.Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def connect(client, who="alice", environment="demo"):
    key = pem_key()
    response = client.put("/v1/account/kalshi", headers=headers(who), json={"environment": environment, "api_key": "key-id-private-1234", "private_key": key})
    assert response.status_code == 200, response.text
    assert key not in response.text
    return key


def order_body():
    return {"ticker": "TEST-YES", "count": 1, "yes_price_cents": 50, "client_order_id": str(uuid.uuid4()), "acknowledge_demo_order": True}


def test_authentication_fails_closed_and_public_liveness(secure, monkeypatch):
    c, _, _, _ = secure
    assert c.get("/healthz").status_code == 200
    for path in ["/v1/portfolios", "/v1/jobs", "/v1/datasets", "/v1/portfolio", "/v1/tournaments"]:
        assert c.get(path).status_code == 401
        assert c.get(path, headers=headers("forged")).status_code == 401
    monkeypatch.delenv("SUPABASE_URL")
    assert c.get("/v1/portfolios", headers=headers()).status_code == 503
    assert c.get("/healthz").status_code == 200


def test_two_accounts_cannot_access_legacy_or_other_account_resources(secure):
    c, stack, _, _ = secure
    alice = provision(c)
    bob = provision(c, "bob")
    assert alice != bob
    assert provision(c) == alice
    assert [p["portfolio_id"] for p in c.get("/v1/portfolios", headers=headers()).json()] == [alice]
    for path in ["/v1/portfolio", "/v1/context", "/v1/runtime", "/v1/events", "/v1/history/events", "/v1/verify", "/v1/analytics/history"]:
        assert c.get(path, params={"portfolio_id": alice}, headers=headers("bob")).status_code == 404, path
        assert c.get(path, params={"portfolio_id": "main"}, headers=headers()).status_code == 404
    assert c.post("/v1/ask", headers=headers(), json={"question": "what is my balance?"}).status_code == 404
    assert c.post("/v1/admin/portfolio", headers=headers("bob"), params={"portfolio_id": alice}, json={"initial_cash_usd": "5"}).status_code == 404
    assert c.post("/v1/portfolios", headers=headers("bob"), json={"portfolio_id": alice}).status_code == 409
    assert stack.sql("SELECT count(*) FROM commands")[0][0] == 0


def test_runs_downloads_messages_and_mismatched_scopes_are_private(secure):
    c, _, _, _ = secure
    alice = provision(c)
    run_a = manifest(c)
    run_b = manifest(c, "bob", "T-bob")
    assert [r["run_id"] for r in c.get("/v1/tournaments", headers=headers()).json()] == [run_a]
    assert c.get(f"/v1/tournaments/{run_a}/manifest", headers=headers()).status_code == 200
    assert c.get(f"/v1/tournaments/{run_a}/manifest", headers=headers("bob")).status_code == 404
    body = {"run_id": run_a, "agent_id": "test", "role": "test", "content": {"private": "alice"}}
    assert c.post("/v1/agent-messages", headers=headers(), json=body).status_code == 200
    assert c.get("/v1/agent-messages", headers=headers("bob")).json() == []
    assert c.post("/v1/agent-messages", params={"run_id": run_a}, headers=headers(), json={**body, "run_id": run_b}).status_code == 422
    assert c.get(f"/v1/tournaments/{run_b}/manifest", params={"run_id": run_a}, headers=headers()).status_code == 422
    assert c.post("/v1/ask", params={"portfolio_id": alice}, headers=headers(), json={"question": "why?", "portfolio_id": "main"}).status_code == 422


def test_job_ownership_internal_scope_and_forged_job_token(secure):
    c, stack, _, _ = secure
    provision(c)
    # This registered fixture exists without external API keys.
    datasets = c.get("/v1/datasets", headers=headers()).json()
    dataset = next(d["dataset_id"] for d in datasets if d["execution_usable"])
    response = c.post("/v1/jobs", headers=headers(), json={"request_id": "req-private", "kind": "research", "dataset_id": dataset})
    assert response.status_code == 202, response.text
    job = response.json()
    assert c.get("/v1/jobs", headers=headers("bob")).json() == []
    assert c.post(f"/v1/jobs/{job['job_id']}/stop", headers=headers("bob"), json={}).status_code == 404
    assert c.post("/v1/jobs", headers=headers("bob"), json={"request_id": "req-private", "kind": "research", "dataset_id": dataset}).status_code == 404
    token = internal_token(ALICE, job)
    job_headers = {"Authorization": "Bearer " + token}
    body = {"run_id": job["run_id"], "agent_id": "test", "role": "test", "content": {}}
    assert c.post("/v1/agent-messages", headers=job_headers, json=body).status_code == 200
    assert c.post("/v1/agent-messages", headers=job_headers, json={**body, "run_id": "T-unowned"}).status_code == 404
    assert c.get("/v1/account/kalshi", headers=job_headers).status_code == 403
    assert c.get("/v1/agent-messages", headers=job_headers).status_code == 403
    assert c.post("/v1/agent-messages", headers={"Authorization": "Bearer " + token[:-1] + ("0" if token[-1] != "0" else "1")}, json=body).status_code == 401
    assert stack.sql("SELECT owner_id FROM account_resources WHERE kind='job' AND resource_id=%s", (job["job_id"],))[0][0] == uuid.UUID(ALICE)


def test_managed_session_token_can_only_queue_commands_for_its_portfolio(secure):
    c, _, _, _ = secure
    alice = provision(c)
    other = c.post("/v1/portfolios", headers=headers(), json={"portfolio_id": "alice-other"})
    assert other.status_code == 201
    run = manifest(c)
    token = internal_token(ALICE, {"portfolio_id": alice, "run_id": run, "kind": "session"})
    scoped = {"Authorization": "Bearer " + token}
    response = c.post("/v1/admin/portfolio", params={"portfolio_id": alice, "wait": False}, headers=scoped, json={"initial_cash_usd": "5"})
    assert response.status_code == 202, response.text
    seq = response.json()["command_seq"]
    assert c.get(f"/v1/commands/{seq}", params={"portfolio_id": alice}, headers=scoped).status_code == 200
    # A token also cannot touch a different portfolio belonging to the same owner.
    assert c.post("/v1/admin/portfolio", params={"portfolio_id": "alice-other", "wait": False}, headers=scoped, json={"initial_cash_usd": "5"}).status_code == 403


def test_default_portfolio_does_not_adopt_legacy_data(secure):
    c, stack, _, _ = secure
    pid = "u_" + ALICE.replace("-", "")
    stack.sql("INSERT INTO managed_workers(portfolio_id) VALUES (%s)", (pid,))
    assert c.get("/v1/account", headers=headers()).status_code == 409
    assert stack.sql("SELECT count(*) FROM account_resources")[0][0] == 0


def test_websocket_authenticates_first_frame_before_data(secure):
    c, _, _, _ = secure
    alice = provision(c)
    with c.websocket_connect(f"/v1/stream?portfolio_id={alice}") as ws:
        ws.send_json({"access_token": "bob"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with c.websocket_connect(f"/v1/stream?portfolio_id={alice}") as ws:
        ws.send_json({"access_token": "invalid"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_valid_stream_sends_owned_events_and_quiet_disconnect_cleans_up(secure):
    c, stack, _, _ = secure
    alice = provision(c)
    # Public projection fields are enough to exercise real stream serialization.
    from psycopg.types.json import Jsonb
    seq = stack.sql("INSERT INTO commands(command_id,portfolio_id,kind,payload,payload_hash) VALUES ('stream-command',%s,'test','{}','hash') RETURNING seq", (alice,))[0][0]
    stack.sql("INSERT INTO ledger_events(portfolio_id,seq,command_seq,type,event,hash) VALUES (%s,1,%s,'Test',%s,'hash')", (alice, seq, Jsonb({"seq": 1, "type": "Test", "private": "alice"})))
    with c.websocket_connect(f"/v1/stream?portfolio_id={alice}") as ws:
        ws.send_json({"access_token": "alice"})
        result = ws.receive_json()
        assert result["events"][0]["private"] == "alice"
    bob = provision(c, "bob")
    with c.websocket_connect(f"/v1/stream?portfolio_id={bob}") as ws:
        ws.send_json({"access_token": "bob"})
    # Context exit must finish even without events to make a send detect closure.


def test_provider_outage_failed_key_replace_and_expired_job_tokens(secure):
    c, stack, _, state = secure
    key = connect(c)
    state["reject_key"] = True
    response = c.put("/v1/account/kalshi", headers=headers(), json={"environment": "demo", "api_key": "replacement", "private_key": key})
    assert response.status_code == 422
    assert key not in response.text and "secret provider detail" not in response.text
    assert stack.sql("SELECT key_hint FROM kalshi_connections")[0][0] == "1234"
    state["auth_outage"] = True
    assert c.get("/v1/portfolios", headers=headers()).status_code == 503
    state["auth_outage"] = False
    import hashlib
    import hmac
    import os
    payload = base64.urlsafe_b64encode(json.dumps({"sub": ALICE, "exp": 1, "kind": "research", "run": "T"}).encode()).decode().rstrip("=")
    signature = hmac.new(os.environ["QS_INTERNAL_SIGNING_KEY"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert c.get("/v1/portfolios", headers=headers(f"qsjob.{payload}.{signature}")).status_code == 401


def test_private_ingested_bars_do_not_leak_to_other_accounts(secure):
    c, _, _, _ = secure
    alice = provision(c)
    bar = {"symbol": "SPY", "ts": "2024-01-01T00:00:00Z", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 10}
    response = c.post("/v1/bars", headers=headers(), params={"portfolio_id": alice, "wait": False}, json={"dataset_id": "private-feed", "bars": [bar]})
    assert response.status_code == 200, response.text
    assert len(c.get("/v1/bars", params={"dataset_id": "private-feed"}, headers=headers()).json()) == 1
    assert c.get("/v1/bars", params={"dataset_id": "private-feed"}, headers=headers("bob")).json() == []
def test_kalshi_keys_are_encrypted_isolated_and_environment_specific(secure, monkeypatch):
    c, stack, calls, _ = secure
    key = connect(c)
    stored = stack.sql("SELECT encrypted_credentials FROM kalshi_connections")[0][0]
    assert key not in stored and "BEGIN PRIVATE KEY" not in stored
    assert c.get("/v1/account/kalshi", headers=headers("bob")).json()["connections"] == []
    assert c.get("/v1/account/kalshi/snapshot", headers=headers("bob")).status_code == 404
    assert c.get("/v1/account/kalshi/snapshot", params={"environment": "production"}, headers=headers()).status_code == 404
    assert c.get("/v1/account/kalshi/snapshot", headers=headers()).json()["balance"]["balance"] == 500
    assert c.get("/v1/account/kalshi/markets", headers=headers()).status_code == 200
    assert all(call.url.host == "external-api.demo.kalshi.co" for call in calls if call.url.host != "test.supabase.co")
    monkeypatch.setenv("QS_CREDENTIAL_ENCRYPTION_KEY", "malformed")
    assert c.get("/v1/account/kalshi", headers=headers()).json()["credential_storage_ready"] is False
    response = c.put("/v1/account/kalshi", headers=headers(), json={"api_key": "new", "private_key": key, "environment": "demo"})
    assert response.status_code == 503 and key not in response.text
    assert c.delete("/v1/account/kalshi", headers=headers("bob")).status_code == 200
    assert len(stack.sql("SELECT * FROM kalshi_connections")) == 1


def test_demo_orders_are_capped_explicit_and_idempotent(secure, monkeypatch):
    c, _, calls, _ = secure
    connect(c)
    body = order_body()
    response = c.post("/v1/account/kalshi/demo-orders", headers=headers(), json=body)
    assert response.status_code == 200, response.text
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json=body).json()["duplicate_request"] is True
    assert len([r for r in calls if r.method == "POST"]) == 1
    sent = next(r for r in calls if r.method == "POST")
    payload = json.loads(sent.content)
    assert sent.url.host == "external-api.demo.kalshi.co"
    assert payload["side"] == "bid" and payload["price"] == "0.5000" and payload["count"] == "1.00"
    assert payload["time_in_force"] == "immediate_or_cancel"
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json={**body, "yes_price_cents": 51}).status_code == 409
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json={**order_body(), "count": 100}).status_code == 422
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json={**order_body(), "acknowledge_demo_order": False}).status_code == 422
    assert c.post("/v1/account/kalshi/production-orders", headers=headers(), json=order_body()).status_code in (404, 405)
    monkeypatch.setenv("QS_KALSHI_DEMO_ORDERS_ENABLED", "false")
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json=order_body()).status_code == 403


def test_unknown_demo_attempt_blocks_new_ids_until_reviewed(secure):
    c, _, calls, state = secure
    connect(c)
    state["timeout_order"] = True
    body = order_body()
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json=body).status_code == 502
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json=body).status_code == 409
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json=order_body()).status_code == 409
    attempts = c.get("/v1/account/kalshi/demo-orders", headers=headers()).json()
    assert attempts[0]["status"] == "unknown"
    assert c.get("/v1/account/kalshi/demo-orders", headers=headers("bob")).json() == []
    ack_path = f"/v1/account/kalshi/demo-orders/{body['client_order_id']}/acknowledge"
    assert c.post(ack_path, headers=headers("bob"), json={"acknowledge_exchange_orders_reviewed": True}).status_code == 409
    assert c.post(ack_path, headers=headers(), json={"acknowledge_exchange_orders_reviewed": True}).status_code == 200
    state["timeout_order"] = False
    assert c.post("/v1/account/kalshi/demo-orders", headers=headers(), json=order_body()).status_code == 200
    assert len([r for r in calls if r.method == "POST"]) == 2


@pytest.mark.parametrize("kind", ["ed25519", "rsa"])
def test_signatures_match_official_timestamp_method_path_contract(kind):
    key = ed25519.Ed25519PrivateKey.generate() if kind == "ed25519" else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    signed = signed_headers("id", pem, "get", "/trade-api/v2/markets?limit=20", "123")
    signature = base64.b64decode(signed["KALSHI-ACCESS-SIGNATURE"])
    if kind == "ed25519":
        key.public_key().verify(signature, b"123GET/trade-api/v2/markets")
    else:
        key.public_key().verify(signature, b"123GET/trade-api/v2/markets", padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
