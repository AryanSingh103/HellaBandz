"""Authenticated audit status, independent verification and bounded sealing requests."""
import asyncio
import os
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v1/audit")
verification_lock = asyncio.Lock()


@router.get("/status")
async def status():
    from hellabandz.api.app import q
    from hellabandz.audit.traces import TABLES
    anchors = await q("SELECT anchor_id,manifest,manifest_hash,signature,created_at FROM audit_anchors WHERE signature IS NOT NULL ORDER BY created_at DESC LIMIT 1")
    count = (await q("SELECT count(*) AS n FROM audit_changes"))[0]["n"]
    service = await q("SELECT heartbeat,error,heartbeat > now()-interval '30 seconds' AS alive FROM audit_service_state WHERE id=1")
    requests = await q("SELECT request_id,created_at,completed_at,error FROM audit_requests ORDER BY created_at DESC LIMIT 1")
    record = anchors[0] if anchors else None
    return {"configured": bool(os.getenv("QS_AUDIT_SIGNER")), "cluster": "devnet", "signer": os.getenv("QS_AUDIT_SIGNER"),
            "coverage": list(TABLES), "latest": record,
            "pending_changes": max(0, count - record["manifest"]["journal"]["count"]) if record else count,
            "service": service[0] if service else None, "request": requests[0] if requests else None}


@router.post("/verify")
async def verify():
    from hellabandz.audit.traces import verify as verify_traces
    if verification_lock.locked():
        raise HTTPException(429, "verification already running")
    async with verification_lock:
        try:
            result = await asyncio.to_thread(verify_traces)
        except ValueError as exc:
            result = {"ok": False, "status": "mismatch", "error": str(exc)}
        except Exception as exc:
            result = {"ok": False, "status": "unavailable", "error": str(exc)}
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/request")
async def request_seal():
    from hellabandz.api.app import pool
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(424247)")
            existing = await (await conn.execute("SELECT request_id FROM audit_requests WHERE completed_at IS NULL LIMIT 1")).fetchone()
            if existing:
                return {"request_id": existing["request_id"], "status": "queued"}
            request_id = uuid.uuid4().hex
            await conn.execute("INSERT INTO audit_requests(request_id) VALUES (%s)", (request_id,))
    return {"request_id": request_id, "status": "queued"}


@router.get("/anchors/{anchor_id}")
async def receipt(anchor_id: str):
    from hellabandz.api.app import q
    records = await q("SELECT anchor_id,manifest,manifest_hash,signature FROM audit_anchors WHERE anchor_id=%s AND signature IS NOT NULL", (anchor_id,))
    if not records:
        raise HTTPException(404, "audit receipt not found")
    return JSONResponse(records[0], headers={"Content-Disposition": 'attachment; filename="audit-receipt.json"', "Cache-Control": "no-store"})
