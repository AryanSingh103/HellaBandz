"""External venue adapter: safe submission to a real exchange (demo first; production disabled).

Rules for every external order:

- Stable client order ids, chosen by the coordinator, never reused.
- Write-ahead journal: the attempt is written and fsynced as PENDING *before* the network call.
- An error or timeout after sending means the outcome is UNKNOWN. An unknown order is never retried
  blindly. New orders for that instrument are blocked until reconcile() asks the venue what happened.
- After a restart nothing is submitted until reconcile() has resolved every PENDING/UNKNOWN entry.
- A token-bucket rate limiter defers orders instead of exceeding the venue's limit.
- Production submission is refused unless QS_PRODUCTION_ORDERS=authorized, and even then this
  adapter only supports environment="demo" today.

The venue client is an interface. The Kalshi demo integration (hellabandz.api.accounts) keeps its
own contract model and journal; stock strategies are never routed to it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class VenueClient(Protocol):
    def submit(self, client_order_id: str, instrument: str, side: str, qty: int, limit_px: int) -> dict: ...
    def get_order(self, client_order_id: str) -> dict | None: ...


class ProductionDisabled(RuntimeError):
    pass


OPEN_STATES = {"PENDING", "UNKNOWN"}


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.state: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.state[rec["client_order_id"]] = {**self.state.get(rec["client_order_id"], {}), **rec}

    def write(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.state[rec["client_order_id"]] = {**self.state.get(rec["client_order_id"], {}), **rec}


@dataclass
class RateLimiter:
    rate_per_s: float
    burst: int
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self):
        self.tokens = float(self.burst)
        self.t = self.clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate_per_s)
        self.t = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class ExternalAdapter:
    def __init__(self, venue: str, client: VenueClient, journal: Journal, limiter: RateLimiter, environment: str = "demo"):
        if environment == "production":
            if os.environ.get("QS_PRODUCTION_ORDERS") != "authorized":
                raise ProductionDisabled("production order submission is disabled")
            raise ProductionDisabled("this adapter supports demo environments only")
        self.venue, self.client, self.journal, self.limiter, self.environment = venue, client, journal, limiter, environment
        self.reconciled = not any(r["status"] in OPEN_STATES for r in journal.state.values())

    def blocked(self, instrument: str) -> bool:
        return any(r["instrument"] == instrument and r["status"] in OPEN_STATES for r in self.journal.state.values())

    def submit(self, client_order_id: str, instrument: str, side: str, qty: int, limit_px: int) -> dict:
        if not self.reconciled:
            return {"status": "refused", "reason": "RECONCILE_REQUIRED_AFTER_RESTART"}
        if client_order_id in self.journal.state:
            return {"status": "refused", "reason": "CLIENT_ORDER_ID_ALREADY_USED", "known": self.journal.state[client_order_id]}
        if self.blocked(instrument):
            return {"status": "deferred", "reason": "UNRESOLVED_SUBMISSION_FOR_INSTRUMENT"}
        if not self.limiter.take():
            return {"status": "deferred", "reason": "RATE_LIMITED"}
        base = {"client_order_id": client_order_id, "venue": self.venue, "environment": self.environment,
                "instrument": instrument, "side": side, "qty": qty, "limit_px": limit_px}
        self.journal.write({**base, "status": "PENDING", "at": time.time()})
        try:
            ack = self.client.submit(client_order_id, instrument, side, qty, limit_px)
        except Exception as e:  # sent or not, we cannot tell: unknown, never retried blindly
            self.journal.write({"client_order_id": client_order_id, "status": "UNKNOWN", "error": f"{type(e).__name__}: {e}"[:300]})
            self.reconciled = False
            return {"status": "unknown", "reason": "OUTCOME_UNKNOWN_RECONCILE_BEFORE_RETRY"}
        status = "ACKED" if ack.get("accepted") else "REJECTED"
        self.journal.write({"client_order_id": client_order_id, "status": status, "venue_order_id": ack.get("order_id"),
                            "venue_response": ack})
        return {"status": status.lower(), **ack}

    def reconcile(self) -> list[dict]:
        """Ask the venue about every unresolved attempt. Returns what changed."""
        changes = []
        for coid, rec in sorted(self.journal.state.items()):
            if rec["status"] not in OPEN_STATES:
                continue
            found = self.client.get_order(coid)
            status = "NOT_RECEIVED" if found is None else ("ACKED" if found.get("accepted") else "REJECTED")
            if found is not None and found.get("filled_qty"):
                status = "FILLED" if found["filled_qty"] >= rec["qty"] else "PARTIALLY_FILLED"
            self.journal.write({"client_order_id": coid, "status": status, "reconciled": True, "venue_response": found})
            changes.append({"client_order_id": coid, "from": rec["status"], "to": status})
        self.reconciled = True
        return changes
