"""Private account provisioning and encrypted, read-only Kalshi connections."""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from decimal import Decimal
from typing import Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, SecretStr
from psycopg.types.json import Jsonb

from quantswarm.api.auth import claim, current_user

router = APIRouter(prefix="/v1/account")
BASES = {"demo": "https://external-api.demo.kalshi.co/trade-api/v2",
         "production": "https://external-api.kalshi.com/trade-api/v2"}


def account_user():
    user = current_user()
    if user is None or user.scope:
        raise HTTPException(401, "A signed-in account is required")
    return user


def cipher() -> Fernet:
    try:
        return Fernet(os.environ.get("QS_CREDENTIAL_ENCRYPTION_KEY", "").encode())
    except ValueError:
        raise HTTPException(503, "Kalshi credential storage is not configured on the server")


def private_key(pem: str):
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
            raise ValueError()
        if isinstance(key, rsa.RSAPrivateKey) and key.key_size < 2048:
            raise ValueError()
        return key
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise HTTPException(422, "Provide a valid unencrypted Kalshi Ed25519 or RSA (2048-bit minimum) private key in PEM format")


def signed_headers(api_key: str, pem: str, method: str, path: str, timestamp: str | None = None) -> dict[str, str]:
    timestamp = timestamp or str(int(time.time() * 1000))
    message = (timestamp + method.upper() + path.split("?", 1)[0]).encode()
    key = private_key(pem)
    if isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(message)
    else:
        signature = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": api_key, "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}


async def kalshi_get(environment: str, credentials: dict, path: str, params: dict | None = None):
    # Fixed host and GET-only transport: browser input cannot route requests or place orders.
    headers = signed_headers(credentials["api_key"], credentials["private_key"], "GET", "/trade-api/v2" + path)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(BASES[environment] + path, headers=headers, params=params)
    except httpx.HTTPError:
        raise HTTPException(502, "Kalshi is unavailable; retry the connection")
    if response.status_code in (401, 403):
        raise HTTPException(422, "Kalshi rejected these credentials. Check the key and selected environment.")
    if response.status_code == 429:
        raise HTTPException(429, "Kalshi rate limit reached; wait before retrying")
    if response.status_code != 200:
        raise HTTPException(502, "Kalshi could not complete the account request")
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except ValueError:
        raise HTTPException(502, "Kalshi returned an invalid response")


async def credentials_for(environment: str):
    from quantswarm.api import app as gateway
    user = account_user()
    rows = await gateway.q("SELECT encrypted_credentials FROM kalshi_connections WHERE owner_id = %s AND environment = %s", (user.id, environment))
    if not rows:
        raise HTTPException(404, "Connect this Kalshi environment first")
    try:
        return json.loads(cipher().decrypt(rows[0]["encrypted_credentials"].encode()))
    except (InvalidToken, ValueError, KeyError):
        raise HTTPException(503, "Stored credentials cannot be decrypted; restore the server encryption key or reconnect")


@router.get("")
async def account():
    from quantswarm.api import app as gateway
    user = account_user()
    pid = "u_" + user.id.replace("-", "")
    async with gateway.pool.connection() as conn, conn.transaction():
        # Serialize concurrent account bootstrap tabs without adopting legacy data.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (pid,))
        row = await (await conn.execute("SELECT owner_id FROM account_resources WHERE kind='portfolio' AND resource_id=%s", (pid,))).fetchone()
        if not row:
            await claim(conn, "portfolio", pid)
        elif str(row["owner_id"]) != user.id:
            raise HTTPException(409, "Account portfolio cannot be provisioned")
        await conn.execute("INSERT INTO managed_workers(portfolio_id) VALUES (%s) ON CONFLICT DO NOTHING", (pid,))
    return {"id": user.id, "email": user.email, "default_portfolio_id": pid,
            "execution_mode": "research_simulation", "kalshi_order_execution": False}


@router.get("/kalshi")
async def connections():
    from quantswarm.api import app as gateway
    user = account_user()
    rows = await gateway.q("SELECT environment, key_hint, verified_at FROM kalshi_connections WHERE owner_id = %s ORDER BY environment", (user.id,))
    try:
        cipher()
        storage_ready = True
    except HTTPException:
        storage_ready = False
    return {"connections": rows, "credential_storage_ready": storage_ready,
            "execution_enabled": False, "mode": "read_only", "supported_environments": list(BASES),
            "demo_orders_enabled": os.environ.get("QS_KALSHI_DEMO_ORDERS_ENABLED", "").lower() == "true"}


class ConnectionIn(BaseModel):
    environment: Literal["demo", "production"] = "demo"
    api_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    private_key: SecretStr = Field(min_length=32, max_length=20000)


@router.put("/kalshi")
async def connect(body: ConnectionIn):
    from quantswarm.api import app as gateway
    user = account_user()
    encryption = cipher()
    pem = body.private_key.get_secret_value().strip()
    private_key(pem)
    credentials = {"api_key": body.api_key, "private_key": pem}
    balance = await kalshi_get(body.environment, credentials, "/portfolio/balance")
    encrypted = encryption.encrypt(json.dumps(credentials).encode()).decode()
    await gateway.q("INSERT INTO kalshi_connections(owner_id,environment,encrypted_credentials,key_hint) VALUES (%s,%s,%s,%s) ON CONFLICT(owner_id,environment) DO UPDATE SET encrypted_credentials=excluded.encrypted_credentials,key_hint=excluded.key_hint,verified_at=now() RETURNING environment", (user.id, body.environment, encrypted, body.api_key[-4:]))
    return {"connected": True, "environment": body.environment, "balance": balance, "mode": "read_only", "execution_enabled": False}


@router.delete("/kalshi")
async def disconnect(environment: Literal["demo", "production"] = "demo"):
    from quantswarm.api import app as gateway
    user = account_user()
    await gateway.q("DELETE FROM kalshi_connections WHERE owner_id = %s AND environment = %s RETURNING environment", (user.id, environment))
    return {"disconnected": True, "environment": environment}


@router.get("/kalshi/snapshot")
async def snapshot(environment: Literal["demo", "production"] = "demo"):
    credentials = await credentials_for(environment)
    balance = await kalshi_get(environment, credentials, "/portfolio/balance")
    positions = await kalshi_get(environment, credentials, "/portfolio/positions", {"limit": 100})
    orders = await kalshi_get(environment, credentials, "/portfolio/orders", {"limit": 100})
    return {"environment": environment, "balance": balance, "positions": positions, "orders": orders,
            "mode": "read_only", "execution_enabled": False}


@router.get("/kalshi/markets")
async def markets(environment: Literal["demo", "production"] = "demo", cursor: str | None = Query(None, max_length=512)):
    credentials = await credentials_for(environment)
    return await kalshi_get(environment, credentials, "/markets", {"status": "open", "limit": 20, **({"cursor": cursor} if cursor else {})})


class DemoOrderIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    count: int = Field(ge=1, le=100, strict=True)
    yes_price_cents: int = Field(ge=1, le=99, strict=True)
    client_order_id: uuid.UUID
    acknowledge_demo_order: Literal[True]


@router.post("/kalshi/demo-orders")
async def demo_order(body: DemoOrderIn):
    """Explicit manual YES purchases on Kalshi's demo exchange only.

    The local attempt record prevents retries after uncertain network outcomes;
    the client must inspect exchange orders before choosing a new request ID.
    """
    from quantswarm.api import app as gateway
    user = account_user()
    if os.environ.get("QS_KALSHI_DEMO_ORDERS_ENABLED", "").lower() != "true":
        raise HTTPException(403, "Demo orders are disabled by the server. Production order execution is unavailable.")
    if body.count * body.yes_price_cents > 500:
        raise HTTPException(422, "Demo order premium is capped at $5 before exchange fees")
    credentials = await credentials_for("demo")
    payload = {"ticker": body.ticker, "side": "bid", "count": f"{body.count}.00",
               "price": f"{Decimal(body.yes_price_cents) / 100:.4f}", "client_order_id": str(body.client_order_id),
               "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
    async with gateway.pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (user.id,))
        existing = await (await conn.execute("SELECT 1 FROM kalshi_demo_order_attempts WHERE owner_id=%s AND client_order_id=%s", (user.id, body.client_order_id))).fetchone()
        if not existing:
            unresolved = await (await conn.execute("SELECT 1 FROM kalshi_demo_order_attempts WHERE owner_id=%s AND status IN ('submitting','unknown') LIMIT 1", (user.id,))).fetchone()
            if unresolved:
                raise HTTPException(409, "An earlier demo order has an unresolved outcome. Inspect exchange orders and acknowledge that attempt before another order.")
        rows = await (await conn.execute("INSERT INTO kalshi_demo_order_attempts(owner_id,client_order_id,payload) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING status", (user.id, body.client_order_id, Jsonb(payload)))).fetchall()
    if not rows:
        previous = (await gateway.q("SELECT payload,status,response FROM kalshi_demo_order_attempts WHERE owner_id=%s AND client_order_id=%s", (user.id, body.client_order_id)))[0]
        if previous["payload"] != payload:
            raise HTTPException(409, "Order request ID was already used with different details")
        if previous["status"] == "accepted":
            return {"environment": "demo", "order": previous["response"], "duplicate_request": True}
        raise HTTPException(409, "This order was already attempted. Refresh demo orders before creating another request; an uncertain attempt is never resent.")
    path = "/trade-api/v2/portfolio/events/orders"
    headers = signed_headers(credentials["api_key"], credentials["private_key"], "POST", path)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(BASES["demo"] + "/portfolio/events/orders", headers=headers, json=payload)
        if response.status_code not in (200, 201):
            status = "rejected" if 400 <= response.status_code < 500 else "unknown"
            await gateway.q("UPDATE kalshi_demo_order_attempts SET status=%s WHERE owner_id=%s AND client_order_id=%s RETURNING status", (status, user.id, body.client_order_id))
            raise HTTPException(502, "Kalshi did not confirm this demo order. Refresh exchange orders before trying a new request.")
        result = response.json()
        if not isinstance(result, dict) or not result.get("order_id"):
            raise ValueError()
    except (httpx.HTTPError, ValueError):
        await gateway.q("UPDATE kalshi_demo_order_attempts SET status='unknown' WHERE owner_id=%s AND client_order_id=%s RETURNING status", (user.id, body.client_order_id))
        raise HTTPException(502, "Demo order outcome is unknown. Refresh exchange orders; this request will not be resent.")
    await gateway.q("UPDATE kalshi_demo_order_attempts SET status='accepted', response=%s WHERE owner_id=%s AND client_order_id=%s RETURNING status", (Jsonb(result), user.id, body.client_order_id))
    return {"environment": "demo", "order": result, "duplicate_request": False}


@router.get("/kalshi/demo-orders")
async def demo_order_attempts():
    from quantswarm.api import app as gateway
    user = account_user()
    return await gateway.q("SELECT client_order_id,payload,status,response,created_at FROM kalshi_demo_order_attempts WHERE owner_id=%s ORDER BY created_at DESC LIMIT 50", (user.id,))


class AttemptReviewIn(BaseModel):
    acknowledge_exchange_orders_reviewed: Literal[True]


@router.post("/kalshi/demo-orders/{client_order_id}/acknowledge")
async def acknowledge_attempt(client_order_id: uuid.UUID, body: AttemptReviewIn):
    from quantswarm.api import app as gateway
    user = account_user()
    rows = await gateway.q("UPDATE kalshi_demo_order_attempts SET status='reviewed_unknown' WHERE owner_id=%s AND client_order_id=%s AND (status='unknown' OR (status='submitting' AND created_at < now() - interval '60 seconds')) RETURNING client_order_id,status", (user.id, client_order_id))
    if not rows:
        raise HTTPException(409, "Attempt cannot be acknowledged yet or does not belong to this account")
    return rows[0]
