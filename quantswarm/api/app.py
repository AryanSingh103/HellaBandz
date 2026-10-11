"""FastAPI gateway.

Writes only to the `commands` table (the durable input log) and reads
projections. It never mutates financial state: that is the execution worker's
exclusive job. Synchronous callers wait for the worker's NOTIFY with a
timeout; if the worker is down, commands stay queued and the API returns 202.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, field_validator

from quantswarm.contracts import (
    MICROS,
    Ident,
    ApplyBar,
    Bar,
    CancelOrder,
    OpenPortfolio,
    OrderRequest,
    SetAllocation,
    SetKillSwitch,
    SubmitOrder,
    command_id_for,
    usd_to_micros,
)
from quantswarm.execution.db import apply_schema, database_url, encode_command
from quantswarm.ledger import replay

from quantswarm.config import load_env  # noqa: E402

load_env()
from quantswarm.api.auth import authorize, claim, current_user, disabled, identity, owned, verify_token
log = logging.getLogger("api")
WAIT_TIMEOUT_S = float(os.environ.get("QS_API_WAIT_TIMEOUT", "5"))


class Hub:
    """Routes worker NOTIFY messages to waiting requests, and wakes the worker.

    Wake-ups are coalesced through one task: Postgres serializes NOTIFY at commit
    with a global lock, so notifying inside every command insert made concurrent
    inserts (and the worker's commits) queue behind each other."""

    def __init__(self) -> None:
        self.waiters: dict[int, asyncio.Future] = {}
        self.tasks: list[asyncio.Task] = []
        self.wake = asyncio.Event()

    async def waker(self) -> None:
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(database_url(), autocommit=True)
                while True:
                    await self.wake.wait()
                    self.wake.clear()
                    await conn.execute("SELECT pg_notify('cmd_new', '')")
            except Exception:
                log.exception("waker dropped; reconnecting")
                await asyncio.sleep(1)

    async def listen(self) -> None:
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(database_url(), autocommit=True)
                await conn.execute("LISTEN cmd_done")
                async for n in conn.notifies():
                    for seq in json.loads(n.payload)["seqs"]:
                        f = self.waiters.pop(seq, None)
                        if f is not None and not f.done():
                            f.set_result(None)
            except Exception:
                log.exception("listener dropped; reconnecting")
                await asyncio.sleep(1)


hub = Hub()
pool: AsyncConnectionPool


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    with psycopg.connect(database_url(), autocommit=True) as c:
        apply_schema(c)
    pool = AsyncConnectionPool(database_url(), min_size=2, max_size=int(os.environ.get("QS_API_POOL", "20")),
                               kwargs={"autocommit": True, "row_factory": dict_row}, open=False)
    await pool.open()
    hub.tasks = [asyncio.create_task(hub.listen()), asyncio.create_task(hub.waker())]
    yield
    for t in hub.tasks:
        t.cancel()
    await pool.close()


app = FastAPI(title="QuantSwarm API", version="0.1.0", lifespan=lifespan, dependencies=[Depends(authorize)])
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("QS_ALLOWED_ORIGINS", "http://localhost:3000").split(","), allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])


@app.get("/healthz")
async def liveness():
    return {"api": "ok", "auth_required": not disabled()}


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Invalid JSON may contain NaN or Infinity; never echo it into JSONResponse.
    return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]}
                                    for e in exc.errors()]}, status_code=422)


@app.middleware("http")
async def unique_portfolio_scope(request, call_next):
    if len(request.query_params.getlist("portfolio_id")) > 1:
        return JSONResponse({"detail": "portfolio_id must appear only once"}, status_code=422)
    return await call_next(request)


async def submit(cmd: Any, command_id: str, portfolio_id: str, wait: bool) -> JSONResponse:
    kind, payload, h = encode_command(cmd)
    async with pool.connection() as conn:
        row = await (await conn.execute(
            "INSERT INTO commands (command_id, portfolio_id, kind, payload, payload_hash) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (command_id) DO NOTHING RETURNING seq",
            (command_id, portfolio_id, kind, Jsonb(payload), h))).fetchone()
        duplicate = row is None
        if duplicate:
            ex = await (await conn.execute(
                "SELECT seq, payload_hash, portfolio_id FROM commands WHERE command_id = %s", (command_id,))).fetchone()
            if ex["payload_hash"] != h:
                raise HTTPException(409, {"status": "conflict", "rules": ["R8_DUPLICATE_ID_DIFFERENT_PAYLOAD"],
                                          "command_id": command_id})
            seq = ex["seq"]
        else:
            seq = row["seq"]
            hub.wake.set()
    body: dict[str, Any] = {"command_id": command_id, "command_seq": seq, "duplicate_request": duplicate}
    if not wait:
        return JSONResponse({**body, "status": "queued"}, status_code=202)
    fut = hub.waiters.setdefault(seq, asyncio.get_running_loop().create_future())
    resp = await _response(seq)
    if resp is None:
        try:
            await asyncio.wait_for(asyncio.shield(fut), WAIT_TIMEOUT_S)
        except asyncio.TimeoutError:
            pass
        resp = await _response(seq)
    hub.waiters.pop(seq, None)
    if resp is None:
        return JSONResponse({**body, "status": "queued", "detail": "execution worker has not processed the command yet"},
                            status_code=202)
    return JSONResponse({**body, **resp})


async def _response(seq: int) -> dict | None:
    async with pool.connection() as conn:
        r = await (await conn.execute("SELECT status, response FROM commands WHERE seq = %s", (seq,))).fetchone()
    return r["response"] if r and r["status"] == "done" else None


# ----------------------------------------------------------------------------- commands


@app.post("/v1/orders")
async def post_order(order: OrderRequest, portfolio_id: Ident = "main", wait: bool = True):
    cmd = SubmitOrder(order=order)
    return await submit(cmd, f"{portfolio_id}:{command_id_for(cmd)}", portfolio_id, wait)


@app.post("/v1/orders/{client_order_id}/cancel")
async def cancel_order(client_order_id: Ident, agent_id: Ident, portfolio_id: Ident = "main", wait: bool = True):
    cmd = CancelOrder(client_order_id=client_order_id, agent_id=agent_id)
    return await submit(cmd, f"{portfolio_id}:{command_id_for(cmd)}", portfolio_id, wait)


class BarsIn(BaseModel):
    dataset_id: str = Field(default="live", max_length=64)
    bars: list[Bar] = Field(min_length=1, max_length=5_000)


@app.post("/v1/bars")
async def post_bars(body: BarsIn, portfolio_id: Ident = "main", wait: bool = True):
    user = current_user()
    dataset_id = f"user:{user.id}:{body.dataset_id}" if user else body.dataset_id
    async with pool.connection() as conn:
        cur = conn.cursor()
        await cur.executemany(
            "INSERT INTO market_bars (ts, dataset_id, symbol, open, high, low, close, volume, stress) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            [(b.ts, dataset_id, b.symbol, b.open, b.high, b.low, b.close, b.volume, b.stress) for b in body.bars])
    last = None
    for b in sorted(body.bars, key=lambda b: (b.ts, b.symbol)):
        cmd = ApplyBar(bar=b)
        last = await submit(cmd, f"{portfolio_id}:{command_id_for(cmd)}", portfolio_id, wait=False)
    if wait and last is not None:
        seq = json.loads(last.body)["command_seq"]
        fut = hub.waiters.setdefault(seq, asyncio.get_running_loop().create_future())
        if await _response(seq) is None:
            try:
                await asyncio.wait_for(asyncio.shield(fut), WAIT_TIMEOUT_S)
            except asyncio.TimeoutError:
                pass
        hub.waiters.pop(seq, None)
        resp = await _response(seq)
        return {"accepted": len(body.bars), "last_command_seq": seq, "processed": resp is not None}
    return {"accepted": len(body.bars), "last_command_seq": json.loads(last.body)["command_seq"],
            "processed": False}


class OpenIn(BaseModel):
    initial_cash_usd: str = "10000"

    @field_validator("initial_cash_usd")
    @classmethod
    def positive_money(cls, value: str) -> str:
        if usd_to_micros(value) <= 0:
            raise ValueError("opening capital must be positive after micro-unit rounding")
        return value


@app.post("/v1/admin/portfolio")
async def open_portfolio(body: OpenIn, portfolio_id: Ident = "main", wait: bool = True):
    return await submit(OpenPortfolio(initial_cash=usd_to_micros(body.initial_cash_usd)), f"open:{portfolio_id}",
                        portfolio_id, wait)


class AllocIn(BaseModel):
    agent_id: Ident
    capital_usd: str

    @field_validator("capital_usd")
    @classmethod
    def nonnegative_money(cls, value: str) -> str:
        from decimal import Decimal
        amount = usd_to_micros(value)
        if Decimal(value) < 0 or amount < 0:
            raise ValueError("allocation must be nonnegative; zero deallocates")
        return value


@app.post("/v1/admin/allocations")
async def set_allocation(body: AllocIn, portfolio_id: Ident = "main",
                         wait: bool = True,
                         idempotency_key: str | None = Header(default=None)):
    cmd = SetAllocation(agent_id=body.agent_id, capital=usd_to_micros(body.capital_usd))
    key = idempotency_key or str(uuid.uuid4())
    return await submit(cmd, f"alloc:{portfolio_id}:{key}", portfolio_id, wait)


class KillIn(BaseModel):
    on: bool
    reason: str = "manual"


@app.post("/v1/admin/kill-switch")
async def kill_switch(body: KillIn, portfolio_id: Ident = "main", idempotency_key: str | None = Header(default=None)):
    key = idempotency_key or str(uuid.uuid4())
    return await submit(SetKillSwitch(on=body.on, reason=body.reason), f"kill:{portfolio_id}:{key}", portfolio_id, True)


# ----------------------------------------------------------------------------- reads


async def q(sql: str, args: tuple = ()) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute(sql, args)).fetchall()


@app.get("/v1/health")
async def health(portfolio_id: Ident = "main"):
    rows = await q("SELECT max(ts) AS last_beat FROM telemetry WHERE service = 'worker' AND metric = 'heartbeat' "
                   "AND labels->>'portfolio_id' = %s AND ts > now() - interval '1 hour'", (portfolio_id,))
    pending = await q("SELECT count(*) AS n FROM commands WHERE status = 'pending' AND portfolio_id = %s", (portfolio_id,))
    last = rows[0]["last_beat"]
    alive = last is not None and (datetime.now(timezone.utc) - last).total_seconds() < 6
    return {"api": "ok", "worker_alive": alive, "worker_last_heartbeat": last, "pending_commands": pending[0]["n"]}


@app.get("/v1/portfolio")
async def portfolio(portfolio_id: Ident = "main"):
    rows = await q("SELECT seq, head_hash, digest, state, equity, updated_at FROM portfolio_state WHERE portfolio_id = %s",
                   (portfolio_id,))
    if not rows:
        raise HTTPException(404, "portfolio not opened")
    return rows[0]


@app.get("/v1/orders")
async def orders(portfolio_id: Ident = "main", agent_id: str | None = None, status: str | None = None,
                 limit: int = Query(200, ge=1, le=2000)):
    sql = "SELECT * FROM orders WHERE portfolio_id = %s"
    args: list[Any] = [portfolio_id]
    if agent_id:
        sql += " AND agent_id = %s"
        args.append(agent_id)
    if status:
        sql += " AND status = %s"
        args.append(status)
    return await q(sql + " ORDER BY updated_at DESC LIMIT %s", (*args, limit))


@app.get("/v1/fills")
async def fills(portfolio_id: Ident = "main", limit: int = Query(200, ge=1, le=5000)):
    return await q("SELECT * FROM fills WHERE portfolio_id = %s ORDER BY event_seq DESC LIMIT %s", (portfolio_id, limit))


@app.get("/v1/events")
async def events(portfolio_id: Ident = "main", after_seq: int = 0, limit: int = Query(200, ge=1, le=5000)):
    return await q("SELECT seq, command_seq, type, event, created_at FROM ledger_events WHERE portfolio_id = %s "
                   "AND seq > %s ORDER BY seq LIMIT %s", (portfolio_id, after_seq, limit))


@app.get("/v1/equity")
async def equity(portfolio_id: Ident = "main", limit: int = Query(5000, ge=1, le=50_000)):
    return await q("SELECT ts, equity, cash, market_value FROM equity_points WHERE portfolio_id = %s "
                   "ORDER BY ts DESC LIMIT %s", (portfolio_id, limit))


@app.get("/v1/analytics/agents")
async def agent_analytics(portfolio_id: Ident = "main"):
    """Per-agent activity from real tables (TigerData)."""
    return await q(
        "SELECT agent_id, count(*) FILTER (WHERE outcome = 'APPROVE') AS approved, "
        "count(*) FILTER (WHERE outcome = 'REDUCE') AS reduced, count(*) FILTER (WHERE outcome = 'REJECT') AS rejected, "
        "coalesce(sum(filled_qty), 0) AS filled_qty FROM orders WHERE portfolio_id = %s GROUP BY agent_id ORDER BY agent_id",
        (portfolio_id,))


@app.get("/v1/analytics/rejections")
async def rejection_analytics(portfolio_id: Ident = "main"):
    return await q("SELECT rule, count(*) AS n FROM orders, unnest(rules) AS rule WHERE portfolio_id = %s "
                   "AND outcome = 'REJECT' GROUP BY rule ORDER BY n DESC", (portfolio_id,))


@app.get("/v1/analytics/throughput")
async def throughput_analytics(portfolio_id: Ident = "main"):
    return await q("SELECT time_bucket('10 seconds', processed_at) AS bucket, count(*) AS commands "
                   "FROM commands WHERE portfolio_id = %s AND status = 'done' "
                   "GROUP BY bucket ORDER BY bucket", (portfolio_id,))


@app.get("/v1/bars")
async def bars(dataset_id: str = "live", symbol: str | None = None, limit: int = Query(2000, ge=1, le=20_000)):
    user = current_user()
    if user:
        from quantswarm.ingestion.registry import registered
        if dataset_id not in registered():
            dataset_id = f"user:{user.id}:{dataset_id}"
    if symbol:
        return await q("SELECT * FROM market_bars WHERE dataset_id = %s AND symbol = %s ORDER BY ts DESC LIMIT %s",
                       (dataset_id, symbol, limit))
    return await q("SELECT * FROM market_bars WHERE dataset_id = %s ORDER BY ts DESC LIMIT %s", (dataset_id, limit))


@app.get("/v1/verify")
async def verify(portfolio_id: Ident = "main"):
    """Independently replay the hash-chained event log and compare to the stored projection (I-06/I-08)."""
    # one snapshot for both reads: while the worker is committing batches, two separate
    # statements could see the log and the projection at different seqs and report a false mismatch
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        rows = await (await conn.execute(
            "SELECT event FROM ledger_events WHERE portfolio_id = %s ORDER BY seq", (portfolio_id,))).fetchall()
        stored = await (await conn.execute(
            "SELECT seq, head_hash, digest FROM portfolio_state WHERE portfolio_id = %s", (portfolio_id,))).fetchall()
    if not rows and (not stored or stored[0]["seq"] == 0):
        return {"status": "not_opened", "events": 0, "chain_ok": None, "match": None,
                "error": None, "stored_digest": None, "replayed_digest": None, "head_hash": None}
    try:
        s = replay([r["event"] for r in rows])
        chain_ok = True
        err = None
    except AssertionError as e:
        chain_ok, err, s = False, str(e), None
    st = stored[0] if stored else None
    return {
        "events": len(rows), "chain_ok": chain_ok, "error": err,
        "replayed_digest": s.digest() if s else None, "stored_digest": st["digest"] if st else None,
        "match": bool(s and st and s.digest() == st["digest"] and s.head_hash == st["head_hash"]),
        "head_hash": s.head_hash if s else None,
    }


# ----------------------------------------------------------------------------- research records


class TournamentIn(BaseModel):
    run_id: Ident
    manifest: dict
    manifest_hash: str
    solana_sig: str | None = None

    @field_validator("manifest")
    @classmethod
    def finite_json(cls, value: dict) -> dict:
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("manifest must contain finite JSON values") from exc
        return value


@app.post("/v1/tournaments")
async def save_tournament(body: TournamentIn):
    from quantswarm.ledger import canonical, sha256_hex
    if body.manifest.get("run_id") != body.run_id:
        raise HTTPException(422, "body run_id must match manifest.run_id")
    if sha256_hex(canonical(body.manifest)) != body.manifest_hash:
        raise HTTPException(422, "manifest_hash does not match canonical manifest content")
    user = current_user()
    if user and not user.scope:
        existing_owner = await q("SELECT owner_id FROM account_resources WHERE kind='run' AND resource_id=%s", (body.run_id,))
        if existing_owner:
            await owned("run", body.run_id)
        else:
            async with pool.connection() as conn, conn.transaction():
                await claim(conn, "run", body.run_id)
    await q("INSERT INTO tournaments (run_id, manifest, manifest_hash) VALUES (%s, %s, %s) "
            "ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
            (body.run_id, Jsonb(body.manifest), body.manifest_hash))
    existing = (await q("SELECT manifest, manifest_hash FROM tournaments WHERE run_id = %s", (body.run_id,)))[0]
    if existing["manifest"] != body.manifest or existing["manifest_hash"] != body.manifest_hash:
        raise HTTPException(409, "finalized run is immutable; revised content requires a new run_id")
    return {"ok": True}


@app.get("/v1/tournaments")
async def list_tournaments(limit: int = Query(20, ge=1, le=200)):
    user = current_user()
    if user:
        return await q("SELECT t.run_id, t.created_at, t.manifest_hash, t.manifest FROM tournaments t JOIN account_resources a ON a.kind='run' AND a.resource_id=t.run_id WHERE a.owner_id=%s ORDER BY t.created_at DESC LIMIT %s", (user.id, limit))
    return await q("SELECT run_id, created_at, manifest_hash, manifest FROM tournaments "
                   "ORDER BY created_at DESC LIMIT %s", (limit,))


class AgentMessageIn(BaseModel):
    run_id: Ident
    agent_id: Ident
    role: str
    content: dict
    portfolio_id: Ident | None = None


@app.post("/v1/agent-messages")
async def post_agent_message(m: AgentMessageIn):
    await q("INSERT INTO agent_messages (run_id, agent_id, role, content, portfolio_id) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (m.run_id, m.agent_id, m.role, Jsonb(m.content), m.portfolio_id))
    return {"ok": True}


@app.get("/v1/agent-messages")
async def agent_messages(run_id: Ident | None = None, limit: int = 200, portfolio_id: Ident | None = None):
    if portfolio_id:
        return await q("SELECT m.* FROM agent_messages m JOIN portfolio_context c USING(run_id) "
                       "WHERE c.portfolio_id = %s AND (m.portfolio_id IS NULL OR m.portfolio_id = %s) "
                       "ORDER BY id LIMIT %s", (portfolio_id, portfolio_id, limit))
    if run_id:
        return await q("SELECT * FROM agent_messages WHERE run_id = %s ORDER BY id LIMIT %s", (run_id, limit))
    user = current_user()
    if user:
        return await q("SELECT m.* FROM agent_messages m JOIN account_resources a ON a.kind='run' AND a.resource_id=m.run_id WHERE a.owner_id=%s ORDER BY id DESC LIMIT %s", (user.id, min(max(limit, 1), 2000)))
    return await q("SELECT * FROM agent_messages ORDER BY id DESC LIMIT %s", (limit,))


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    portfolio_id: Ident = "main"

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question cannot be blank")
        return value.strip()


@app.post("/v1/ask")
async def ask(body: AskIn):
    await owned("portfolio", body.portfolio_id)
    from quantswarm.agents.ask import answer

    ctx = {
        "portfolio": (await q("SELECT state, equity, digest FROM portfolio_state WHERE portfolio_id = %s",
                              (body.portfolio_id,)) or [None])[0],
        "recent_orders": await q("SELECT order_id, agent_id, symbol, side, requested_qty, approved_qty, filled_qty, "
                                 "status, outcome, rules FROM orders WHERE portfolio_id = %s ORDER BY updated_at DESC "
                                 "LIMIT 40", (body.portfolio_id,)),
        "rejections": await q("SELECT rule, count(*) AS n FROM orders, unnest(rules) AS rule WHERE portfolio_id = %s "
                              "AND outcome = 'REJECT' GROUP BY rule", (body.portfolio_id,)),
        "latest_rejection": (await q("SELECT seq, event FROM ledger_events WHERE portfolio_id = %s "
                                      "AND type = 'OrderRejected' ORDER BY seq DESC LIMIT 1",
                                      (body.portfolio_id,)) or [None])[0],
        "tournament": (await q("SELECT t.manifest FROM tournaments t JOIN portfolio_context c USING(run_id) "
                               "WHERE c.portfolio_id = %s", (body.portfolio_id,)) or [None])[0],
    }
    from quantswarm.ingestion.dataset import unit_context
    ctx["units"] = unit_context((ctx.get("tournament") or {}).get("manifest", {}).get("dataset", {}))
    try:
        return await asyncio.wait_for(asyncio.to_thread(answer, body.question, ctx), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(504, "answer timed out; retry the read-only question")
    except Exception:
        log.exception("Ask failed")
        raise HTTPException(502, "answer provider failed; retry the read-only question")


class SpeakIn(BaseModel):
    text: str = Field(max_length=1200)


@app.post("/v1/speak")
async def speak(body: SpeakIn):
    """ElevenLabs text-to-speech. Returns 501 without a key; the UI then falls back to browser speech."""
    from fastapi.responses import Response

    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise HTTPException(501, "ELEVENLABS_API_KEY not configured")
    import httpx

    voice = os.environ.get("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                             headers={"xi-api-key": key, "accept": "audio/mpeg"},
                             json={"text": body.text, "model_id": os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")})
    except httpx.TimeoutException:
        raise HTTPException(504, "speech provider timed out; retry playback")
    except httpx.HTTPError:
        raise HTTPException(502, "speech provider unavailable; retry playback")
    if r.status_code != 200:
        raise HTTPException(502, f"elevenlabs error {r.status_code}")
    return Response(content=r.content, media_type="audio/mpeg")


# ----------------------------------------------------------------------------- streaming


@app.websocket("/v1/stream")
async def stream(ws: WebSocket, portfolio_id: Ident = "main", after_seq: int = 0):
    if len(ws.query_params.getlist("portfolio_id")) > 1:
        await ws.close(code=1008, reason="portfolio_id must appear only once")
        return
    await ws.accept()
    token = None
    if not disabled():
        try:
            # Tokens never travel in URLs, browser history, or access logs.
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
            token = first.get("access_token") if isinstance(first, dict) else None
            if not isinstance(token, str) or len(token) > 16384:
                raise HTTPException(401, "Sign in required")
            user = await verify_token(token)
            if user.scope:
                raise HTTPException(403, "Account session required")
            identity.set(user)
            await owned("portfolio", portfolio_id)
        except (HTTPException, ValueError, asyncio.TimeoutError, WebSocketDisconnect):
            await ws.close(code=1008, reason="Account session or portfolio access invalid")
            return
    last = after_seq
    checked_at = asyncio.get_running_loop().time()

    async def disconnected():
        # Read the disconnect frame even on a quiet portfolio with no events.
        try:
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass
        except (WebSocketDisconnect, RuntimeError):
            return

    disconnect_task = asyncio.create_task(disconnected())
    try:
        while True:
            if disconnect_task.done():
                return
            if token and asyncio.get_running_loop().time() - checked_at > 60:
                try:
                    identity.set(await verify_token(token))
                    await owned("portfolio", portfolio_id)
                except HTTPException:
                    await ws.close(code=1008, reason="Session expired; reconnect")
                    return
                checked_at = asyncio.get_running_loop().time()
            rows = await q("SELECT seq, type, event FROM ledger_events WHERE portfolio_id = %s AND seq > %s "
                           "ORDER BY seq LIMIT 500", (portfolio_id, last))
            if rows:
                last = rows[-1]["seq"]
                await ws.send_json({"type": "events", "events": [r["event"] for r in rows]})
                st = await q("SELECT seq, digest, equity, state FROM portfolio_state WHERE portfolio_id = %s", (portfolio_id,))
                if st:
                    await ws.send_json({"type": "portfolio", "portfolio": st[0]}, mode="text")
            await asyncio.wait({disconnect_task}, timeout=0.3)
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        disconnect_task.cancel()
        await asyncio.gather(disconnect_task, return_exceptions=True)


def micros(x: int) -> float:
    return x / MICROS


from quantswarm.api.workflows import router as workflow_router  # noqa: E402
app.include_router(workflow_router)
from quantswarm.api.accounts import router as account_router  # noqa: E402
app.include_router(account_router)
