"""Browser workflows. Financial mutations still go through the command queue."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from hellabandz.api import app as gateway
from hellabandz.api.auth import claim, current_user, owned
from hellabandz.contracts import MICROS, Ident, StrategySpec
from hellabandz.execution.worker import public_state
from hellabandz.ingestion.registry import MANIFESTS, describe, registered, resolve
from hellabandz.ledger import PortfolioState, replay

router = APIRouter(prefix="/v1")


@router.get("/datasets")
async def datasets():
    return await asyncio.to_thread(lambda: [describe(k, v) for k, v in registered().items()])


@router.get("/capabilities")
async def capabilities():
    return {"execution": "historical simulated, long-only, cash-funded", "initial_capital": 10000,
            "research_provider": "OpenAI" if os.environ.get("OPENAI_API_KEY") else "labeled fixture/fallback",
            "speech_provider": "ElevenLabs" if os.environ.get("ELEVENLABS_API_KEY") else "browser fallback",
            "live_observation": False, "kalshi_discovery": False}


@router.get("/portfolios")
async def portfolios():
    user = current_user()
    if user:
        return await gateway.q(
            "SELECT a.resource_id AS portfolio_id, s.seq, s.digest, c.run_id, c.dataset_id, c.session_job_id, c.provenance, "
            "CASE WHEN w.portfolio_id IS NULL THEN 'external' ELSE 'supervisor' END AS ownership "
            "FROM account_resources a LEFT JOIN portfolio_state s ON s.portfolio_id=a.resource_id "
            "LEFT JOIN portfolio_context c ON c.portfolio_id=a.resource_id LEFT JOIN managed_workers w ON w.portfolio_id=a.resource_id "
            "WHERE a.kind='portfolio' AND a.owner_id=%s ORDER BY a.resource_id", (user.id,))
    return await gateway.q(
        "SELECT p.portfolio_id, s.seq, s.digest, c.run_id, c.dataset_id, c.session_job_id, c.provenance, "
        "CASE WHEN w.portfolio_id IS NULL THEN 'external' ELSE 'supervisor' END AS ownership "
        "FROM (SELECT portfolio_id FROM portfolio_state UNION SELECT portfolio_id FROM managed_workers "
        "UNION SELECT 'main') p LEFT JOIN portfolio_state s USING(portfolio_id) "
        "LEFT JOIN portfolio_context c USING(portfolio_id) LEFT JOIN managed_workers w USING(portfolio_id) "
        "ORDER BY p.portfolio_id")


@router.get("/context")
async def context(portfolio_id: Ident = "main"):
    rows = await gateway.q("SELECT c.*, t.manifest, t.manifest_hash FROM portfolio_context c "
                           "LEFT JOIN tournaments t USING(run_id) WHERE portfolio_id = %s", (portfolio_id,))
    return rows[0] if rows else {"portfolio_id": portfolio_id, "association": "unassociated", "manifest": None}


class PortfolioIn(BaseModel):
    portfolio_id: Ident


async def reserve_portfolio(conn, portfolio_id: str):
    exists = await (await conn.execute(
        "SELECT 1 FROM portfolio_state WHERE portfolio_id = %s UNION ALL "
        "SELECT 1 FROM managed_workers WHERE portfolio_id = %s UNION ALL "
        "SELECT 1 FROM commands WHERE portfolio_id = %s UNION ALL "
        "SELECT 1 FROM telemetry WHERE service = 'worker' AND labels->>'portfolio_id' = %s LIMIT 1",
        (portfolio_id,) * 4)).fetchone()
    if portfolio_id == "main" or exists:
        raise HTTPException(409, "choose a fresh isolated portfolio ID")
    await claim(conn, "portfolio", portfolio_id)
    await conn.execute("INSERT INTO managed_workers (portfolio_id) VALUES (%s)", (portfolio_id,))


@router.post("/portfolios", status_code=201)
async def create_portfolio(body: PortfolioIn):
    try:
        async with gateway.pool.connection() as conn, conn.transaction():
            await reserve_portfolio(conn, body.portfolio_id)
    except UniqueViolation:
        raise HTTPException(409, "portfolio ID already reserved")
    return {"portfolio_id": body.portfolio_id, "ownership": "supervisor", "status": "starting"}


@router.get("/runtime")
async def runtime(portfolio_id: Ident = "main"):
    beat = await gateway.health(portfolio_id)
    workers = await gateway.q("SELECT * FROM managed_workers WHERE portfolio_id = %s", (portfolio_id,))
    recovery = await gateway.q("SELECT ts, value, labels FROM telemetry WHERE service = 'worker' "
                               "AND metric = 'recovery_seconds' AND labels->>'portfolio_id' = %s "
                               "ORDER BY ts DESC LIMIT 1", (portfolio_id,))
    manager = await gateway.q("SELECT max(ts) AS ts FROM telemetry WHERE service = 'manager' AND metric = 'heartbeat'")
    w = workers[0] if workers else None
    manager_alive = bool(manager[0]["ts"] and (gateway.datetime.now(gateway.timezone.utc) - manager[0]["ts"]).total_seconds() < 6)
    intentional_down = bool(w and w["desired"] == "down" and w["status"] in ("stopping", "intentional_down"))
    fresh = bool(not w or (w.get("started_at") and beat["worker_last_heartbeat"] and
                          beat["worker_last_heartbeat"] >= w["started_at"]))
    alive = beat["worker_alive"] and fresh and not intentional_down and (not w or w["status"] != "failed")
    return {**beat, "portfolio_id": portfolio_id, "ownership": "supervisor" if w else "external",
            "manager_alive": manager_alive, "intentional_down": intentional_down,
            "worker_alive": alive,
            "ready": alive and (not w or w["status"] == "running"),
            "recovery": recovery[0] if recovery else None, "managed": w,
            "status": "intentional_down" if intentional_down else (w["status"] if w else "externally_managed")}


class WorkerAction(BaseModel):
    action: Literal["crash", "restart"]


@router.post("/runtime")
async def worker_action(body: WorkerAction, portfolio_id: Ident = "main"):
    rows = await gateway.q("UPDATE managed_workers SET desired = %s, generation = generation + 1, "
                           "status = %s, updated_at = now(), error = NULL WHERE portfolio_id = %s RETURNING portfolio_id",
                           ("down" if body.action == "crash" else "running",
                            "stopping" if body.action == "crash" else "starting", portfolio_id))
    if not rows:
        raise HTTPException(409, "external worker ownership: browser process controls are unavailable")
    return {"status": "requested", "action": body.action, "portfolio_id": portfolio_id}


@router.get("/commands/{seq}")
async def command(seq: int, portfolio_id: Ident = "main"):
    rows = await gateway.q("SELECT * FROM commands WHERE portfolio_id = %s AND seq = %s", (portfolio_id, seq))
    if not rows:
        raise HTTPException(404, "command not found in selected portfolio")
    return rows[0]


class JobIn(BaseModel):
    request_id: Ident
    kind: Literal["research", "session"]
    dataset_id: Ident
    run_id: Ident | None = None
    portfolio_id: Ident | None = None
    steps: int = Field(default=100, ge=1, le=10000)
    speed: float = Field(default=4, ge=0, le=100, allow_inf_nan=False)
    crash_at: float | None = Field(default=None, ge=0, lt=1, allow_inf_nan=False)


@router.post("/jobs", status_code=202)
async def launch_job(body: JobIn):
    config = body.model_dump(mode="json")
    existing = await gateway.q("SELECT * FROM jobs WHERE request_id = %s", (body.request_id,))
    if existing:
        await owned("request", body.request_id)
        if existing[0]["config"] != config:
            raise HTTPException(409, "request ID reused with different configuration")
        return existing[0]
    try:
        path = resolve(body.dataset_id)
        info = await asyncio.to_thread(describe, body.dataset_id, path)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not info["execution_usable"]:
        raise HTTPException(422, info["unavailable_reason"])
    job_id = "J" + uuid.uuid4().hex
    run_id = "T" + uuid.uuid4().hex[:20] if body.kind == "research" else body.run_id
    if body.kind == "session":
        if not body.portfolio_id or not run_id:
            raise HTTPException(422, "session requires a frozen run_id and a fresh portfolio_id")
        runs = await gateway.q("SELECT manifest FROM tournaments WHERE run_id = %s", (run_id,))
        if not runs:
            raise HTTPException(409, "frozen manifest and registered dataset must match")
        manifest = runs[0]["manifest"]
        if not isinstance(manifest.get("dataset"), dict):
            raise HTTPException(422, "frozen manifest requires dataset metadata")
        if manifest["dataset"].get("content_sha256") != info["content_sha256"]:
            raise HTTPException(409, "frozen manifest and registered dataset must match")
        try:
            # the passive core, when present, is a frozen slot like any fitted strategy
            entries = manifest["fitted"] + ([manifest["core"]] if manifest.get("core") else [])
            specs = [StrategySpec.model_validate(f["spec"]) for f in entries]
            if not specs or len({s.agent_id for s in specs}) != len(specs):
                raise ValueError("frozen strategy agents must be nonempty and unique")
            if manifest["policy"]["initial_cash_micros"] != 10000 * MICROS:
                raise ValueError("session policy requires fixed 10,000 quote-unit capital")
            if manifest["dataset"]["splits"] != info["splits"]:
                raise ValueError("frozen evaluation splits must match the registered snapshot")
            for spec in specs:
                cap = manifest["allocations"]["by_strategy"][spec.strategy_id]["capital_micros"]
                if not isinstance(cap, int) or not 0 <= cap <= 10000 * MICROS or not spec.universe or not set(spec.universe) <= set(info["symbols"]):
                    raise ValueError("invalid frozen allocation or universe")
            if sum(manifest["allocations"]["by_strategy"][s.strategy_id]["capital_micros"] for s in specs) > 10000 * MICROS:
                raise ValueError("frozen allocations exceed opening capital")
            from hellabandz.ledger import EngineConfig
            if "engine_config" in manifest and manifest["engine_config"] != EngineConfig().__dict__:
                raise ValueError("frozen execution configuration is incompatible with the current worker")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(422, "incomplete or incompatible frozen session manifest: " + str(exc)[:180])
    try:
        async with gateway.pool.connection() as conn, conn.transaction():
            await claim(conn, "request", body.request_id)
            await claim(conn, "job", job_id)
            if body.kind == "research":
                await claim(conn, "run", run_id)
            if body.kind == "session":
                await reserve_portfolio(conn, body.portfolio_id)
                await conn.execute("INSERT INTO portfolio_context (portfolio_id, run_id, dataset_id, session_job_id) "
                                   "VALUES (%s,%s,%s,%s)", (body.portfolio_id, run_id, body.dataset_id, job_id))
            await conn.execute("INSERT INTO jobs (job_id, request_id, kind, dataset_id, run_id, portfolio_id, config) "
                               "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                               (job_id, body.request_id, body.kind, body.dataset_id, run_id, body.portfolio_id, Jsonb(config)))
    except UniqueViolation:
        # Another identical request may have committed while this request validated its dataset.
        existing = await gateway.q("SELECT * FROM jobs WHERE request_id = %s", (body.request_id,))
        if existing and existing[0]["config"] == config:
            await owned("request", body.request_id)
            return existing[0]
        raise HTTPException(409, "portfolio or request ID already reserved")
    return (await gateway.q("SELECT * FROM jobs WHERE job_id = %s", (job_id,)))[0]


@router.get("/jobs")
async def jobs(portfolio_id: Ident | None = None, run_id: Ident | None = None):
    clauses, args = [], []
    user = current_user()
    if user:
        clauses.append("job_id IN (SELECT resource_id FROM account_resources WHERE kind='job' AND owner_id=%s)")
        args.append(user.id)
    if portfolio_id:
        clauses.append("portfolio_id = %s")
        args.append(portfolio_id)
    if run_id:
        clauses.append("run_id = %s")
        args.append(run_id)
    return await gateway.q("SELECT * FROM jobs" + (" WHERE " + " AND ".join(clauses) if clauses else "") +
                           " ORDER BY created_at DESC LIMIT 100", tuple(args))


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: Ident):
    rows = await gateway.q("UPDATE jobs SET stop_requested = TRUE, updated_at = now(), "
                           "stage = CASE WHEN status = 'queued' THEN 'stopped' ELSE stage END, "
                           "status = CASE WHEN status = 'queued' THEN 'stopped' ELSE status END, "
                           "finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END "
                           "WHERE job_id = %s AND status IN ('queued','running') RETURNING *", (job_id,))
    if not rows:
        raise HTTPException(409, "job is already terminal or unknown")
    return rows[0]


@router.get("/tournaments/{run_id}/manifest")
async def download_manifest(run_id: Ident):
    from fastapi.responses import Response
    from hellabandz.ledger import canonical
    rows = await gateway.q("SELECT manifest FROM tournaments WHERE run_id = %s", (run_id,))
    if not rows:
        raise HTTPException(404, "run not found")
    return Response(canonical(rows[0]["manifest"]), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})


@router.get("/history/{collection}")
async def history(collection: Literal["orders", "fills", "events"], portfolio_id: Ident = "main",
                  cursor: int | None = Query(None, ge=0), snapshot_seq: int | None = Query(None, ge=0),
                  limit: int = Query(40, ge=1, le=200), order_id: str | None = None,
                  agent_id: str | None = None, symbol: str | None = None, status: str | None = None,
                  outcome: str | None = None, event_type: str | None = None,
                  from_seq: int = Query(0, ge=0), to_seq: int | None = Query(None, ge=0),
                  from_time: datetime | None = None, to_time: datetime | None = None):
    if snapshot_seq is None:
        snapshot_seq = (await gateway.q("SELECT coalesce(max(seq),0) AS n FROM ledger_events WHERE portfolio_id = %s",
                                       (portfolio_id,)))[0]["n"]
    # A ledger sequence is an immutable cursor. Orders are reconstructed at the
    # frozen boundary so new fills cannot change a previously visited page.
    if collection == "orders":
        sql = ("WITH lifecycle AS (SELECT event->>'order_id' AS oid, min(seq) AS first_seq, "
               "coalesce(sum((event->>'qty')::bigint) FILTER (WHERE type = 'OrderFilled'),0) AS filled, "
               "(array_agg(event ORDER BY seq DESC) FILTER (WHERE type = 'OrderClosed'))[1] AS closed "
               "FROM ledger_events WHERE portfolio_id = %s AND seq <= %s AND event ? 'order_id' GROUP BY 1) "
               "SELECT o.*, e.seq AS cursor_seq, l.filled AS filled_qty, "
               "CASE WHEN l.closed IS NOT NULL THEN l.closed->>'reason' "
               "WHEN e.type = 'OrderRejected' THEN 'rejected' ELSE 'working' END AS status "
               "FROM lifecycle l JOIN ledger_events e ON e.portfolio_id = %s AND e.seq = l.first_seq "
               "JOIN orders o ON o.portfolio_id = e.portfolio_id AND o.order_id = l.oid WHERE e.seq >= %s")
        args: list = [portfolio_id, snapshot_seq, portfolio_id, from_seq]
        expressions = {"order_id": "o.order_id", "agent_id": "o.agent_id", "symbol": "o.symbol",
                       "outcome": "o.outcome", "event_type": "e.type",
                       "status": "CASE WHEN l.closed IS NOT NULL THEN l.closed->>'reason' "
                                 "WHEN e.type = 'OrderRejected' THEN 'rejected' ELSE 'working' END"}
        time_column = "e.created_at"
    else:
        sql = "SELECT e.*, e.seq AS cursor_seq FROM ledger_events e WHERE e.portfolio_id = %s AND e.seq <= %s AND e.seq >= %s"
        args = [portfolio_id, snapshot_seq, from_seq]
        if collection == "fills":
            sql += " AND e.type = 'OrderFilled'"
        expressions = {k: f"e.event->>'{k}'" for k in ("order_id", "agent_id", "symbol", "outcome")}
        expressions["event_type"] = "e.type"
        time_column = "e.created_at"
    for name, value in {"order_id": order_id, "agent_id": agent_id, "symbol": symbol, "status": status,
                        "outcome": outcome, "event_type": event_type}.items():
        if value and name in expressions:
            sql += " AND " + expressions[name] + (" ILIKE %s" if name == "order_id" else " = %s")
            args.append("%" + value + "%" if name == "order_id" else value)
    for bound, operation in [(cursor, "<"), (to_seq, "<=")]:
        if bound is not None:
            sql += f" AND e.seq {operation} %s"
            args.append(bound)
    for bound, operation in [(from_time, ">="), (to_time, "<=")]:
        if bound:
            sql += f" AND {time_column} {operation} %s"
            args.append(bound)
    rows = await gateway.q(sql + " ORDER BY e.seq DESC LIMIT %s", (*args, limit + 1))
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "snapshot_seq": snapshot_seq,
            "next_cursor": rows[-1]["cursor_seq"] if more else None}


@router.get("/orders/{order_id}")
async def order_detail(order_id: Ident, portfolio_id: Ident = "main"):
    orders = await gateway.q("SELECT * FROM orders WHERE portfolio_id = %s AND order_id = %s", (portfolio_id, order_id))
    commands = await gateway.q("SELECT * FROM commands WHERE portfolio_id = %s AND "
                               "(payload->'order'->>'client_order_id' = %s OR payload->>'client_order_id' = %s) "
                               "ORDER BY seq", (portfolio_id, order_id, order_id))
    if not orders and not commands:
        raise HTTPException(404, "order not found in selected portfolio")
    return {"order": orders[0] if orders else None, "commands": commands,
            "events": await gateway.q("SELECT * FROM ledger_events WHERE portfolio_id = %s "
                                       "AND event->>'order_id' = %s ORDER BY seq", (portfolio_id, order_id))}


@router.get("/replay/{seq}")
async def replay_event(seq: int, portfolio_id: Ident = "main"):
    rows = await gateway.q("SELECT event FROM ledger_events WHERE portfolio_id = %s AND seq <= %s ORDER BY seq",
                           (portfolio_id, seq))
    if not rows or rows[-1]["event"]["seq"] != seq:
        raise HTTPException(404, "event not found")
    def reconstruct():
        before = replay([r["event"] for r in rows[:-1]])
        after = replay([rows[-1]["event"]], initial_state=PortfolioState.from_snapshot(before.snapshot()))
        return {"portfolio_id": portfolio_id, "through_seq": seq, "progress": seq, "total": seq,
                "event": rows[-1]["event"], "before": public_state(before), "after": public_state(after),
                "before_digest": before.digest(), "after_digest": after.digest(), "read_only": True}
    return await asyncio.to_thread(reconstruct)


@router.get("/analytics/history")
async def analytics_history(portfolio_id: Ident = "main"):
    rows = await gateway.q("SELECT event FROM ledger_events WHERE portfolio_id = %s ORDER BY seq", (portfolio_id,))
    def derive():
        s = PortfolioState()
        agent_realized, agent_fees = {}, {}
        points = []
        for row in rows:
            event = row["event"]
            agent = event.get("agent_id")
            realized, fees = s.realized_pnl, s.fees_paid
            replay([event], initial_state=s)
            if agent:
                agent_realized[agent] = agent_realized.get(agent, 0) + s.realized_pnl - realized
                agent_fees[agent] = agent_fees.get(agent, 0) + s.fees_paid - fees
            if event["type"] == "BarApplied" or event["type"] == "OrderFilled":
                agents = {}
                for a in s.allocations:
                    mv = sum(p.qty * s.last_bar[k.split('|')[1]][1] for k, p in s.positions.items() if k.split('|')[0] == a)
                    basis = sum(p.basis for k, p in s.positions.items() if k.split('|')[0] == a)
                    agents[a] = {"pnl": agent_realized.get(a, 0) + mv - basis - agent_fees.get(a, 0), "exposure": mv}
                points.append({"seq": s.seq, "ts": event.get("ts", event.get("bar_ts")), "equity": s.equity(),
                               "exposure": s.market_value(), "agents": agents})
        # Keep the last point plus an evenly sampled real history; never fabricate samples.
        stride = max(1, len(points) // 1000)
        sampled = points[::stride]
        if points and (not sampled or sampled[-1] != points[-1]):
            sampled.append(points[-1])
        return {"points": sampled, "source": "verified portfolio ledger", "sample_stride": stride,
                "definitions": {"agent_pnl": "realized + marked holdings - remaining cost basis - agent fees (micro quote units)",
                                "exposure": "held quantity times latest applied close; excludes pending buy reservations",
                                "throughput": "processed portfolio commands per 10-second wall-clock bucket"}}
    result = await asyncio.to_thread(derive)
    contexts = await gateway.q("SELECT c.run_id, t.manifest_hash FROM portfolio_context c "
                               "JOIN tournaments t USING(run_id) WHERE portfolio_id = %s", (portfolio_id,))
    benchmark_path = MANIFESTS / f"{contexts[0]['run_id']}.analytics.json" if contexts else None
    if benchmark_path and benchmark_path.is_file():
        try:
            benchmark = json.loads(benchmark_path.read_text())
            if benchmark["run_id"] != contexts[0]["run_id"] or benchmark["manifest_hash"] != contexts[0]["manifest_hash"]:
                raise ValueError("benchmark provenance mismatch")
            result["benchmark"] = benchmark
        except (ValueError, OSError, KeyError, TypeError):
            result["benchmark"] = {"status": "unavailable", "reason": "baseline artifact is unreadable or does not match the frozen manifest"}
    else:
        result["benchmark"] = {"status": "unavailable", "reason": "no matching frozen-period baseline artifact"}
    return result
