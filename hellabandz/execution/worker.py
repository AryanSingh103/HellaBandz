"""Execution worker: the only process allowed to mutate the financial ledger.

Loop: claim a batch of pending commands (in seq order) -> run them through the
deterministic engine -> in ONE transaction write ledger events, command
responses, projections and the state digest -> commit -> NOTIFY.

Crash safety: a crash before commit leaves commands pending (they are
re-processed after restart, producing identical events because the engine is
deterministic); a crash after commit loses nothing. Startup always rebuilds
state from a verified checkpoint plus the hash-chained suffix and refuses to run if the
replayed digest disagrees with the stored projection.

Single writer per portfolio is enforced with a session-level advisory lock.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from hellabandz.contracts import ApplyBar
from hellabandz.execution.db import apply_schema, database_url, decode_command
from hellabandz.ledger import EngineConfig, PortfolioState, canonical, check_invariants, process, replay, sha256_hex

log = logging.getLogger("worker")


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def public_state(s: PortfolioState) -> dict:
    snap = s.snapshot(include_decisions=False)
    snap["decisions"] = len(s.decisions)
    snap["equity"] = s.equity()
    snap["market_value"] = s.market_value()
    return snap


class StartupDigestMismatch(RuntimeError):
    pass


class Worker:
    def __init__(self, conn: psycopg.Connection, portfolio_id: str = "main", batch_size: int = 64,
                 cfg: EngineConfig = EngineConfig(), checkpoint_interval: int = 10_000):
        if checkpoint_interval < 0:
            raise ValueError("checkpoint_interval must be non-negative (0 disables checkpoints)")
        self.conn = conn
        self.pid = portfolio_id
        self.batch_size = batch_size
        self.cfg = cfg
        self.state = PortfolioState()
        self.checkpoint_interval = checkpoint_interval
        self.checkpoint_seq = 0
        self.crash_at = os.environ.get("QS_CRASH_AT")  # before_commit | after_commit
        self.crash_after = int(os.environ.get("QS_CRASH_AFTER_BATCHES", "1"))
        self.batches = 0

    # ------------------------------------------------------------------ recovery
    def recover(self) -> dict:
        t0 = time.perf_counter()
        checkpoint = self.conn.execute(
            "SELECT version, seq, head_hash, digest, snapshot, snapshot_hash FROM ledger_checkpoints "
            "WHERE portfolio_id = %s", (self.pid,)).fetchone()
        state = PortfolioState()
        if checkpoint is not None:
            version, seq, head, digest, snapshot, snapshot_hash = checkpoint
            if version != 1 or sha256_hex(canonical(snapshot)) != snapshot_hash:
                raise StartupDigestMismatch("checkpoint version/hash mismatch; refusing to start")
            state = PortfolioState.from_snapshot(snapshot)
            if (state.seq, state.head_hash, state.digest()) != (seq, head, digest):
                raise StartupDigestMismatch("checkpoint metadata/digest mismatch; refusing to start")
            anchor = self.conn.execute(
                "SELECT hash, event->>'hash' FROM ledger_events WHERE portfolio_id = %s AND seq = %s",
                (self.pid, seq)).fetchone()
            if anchor != (head, head):
                raise StartupDigestMismatch("checkpoint ledger anchor mismatch; refusing to start")
        checkpoint_seq = state.seq
        rows = self.conn.execute(
            "SELECT event FROM ledger_events WHERE portfolio_id = %s AND seq > %s ORDER BY seq",
            (self.pid, checkpoint_seq)).fetchall()
        t_load = time.perf_counter()
        state = replay([r[0] for r in rows], initial_state=state)
        check_invariants(state)
        stored = self.conn.execute(
            "SELECT seq, head_hash, digest FROM portfolio_state WHERE portfolio_id = %s", (self.pid,)).fetchone()
        if stored is None and state.seq:
            raise StartupDigestMismatch("nonempty ledger has no stored projection; refusing to start")
        if stored is not None and (stored[0] != state.seq or stored[1] != state.head_hash or stored[2] != state.digest()):
            raise StartupDigestMismatch(
                f"replayed state (seq={state.seq}, digest={state.digest()[:12]}) != stored projection "
                f"(seq={stored[0]}, digest={stored[2][:12]}); refusing to start")
        self.state = state
        self.checkpoint_seq = checkpoint_seq
        info = {"events": len(rows), "seq": state.seq, "digest": state.digest(),
                "checkpoint_seq": checkpoint_seq,
                "load_s": t_load - t0, "replay_s": time.perf_counter() - t_load,
                "total_s": time.perf_counter() - t0}
        self.conn.execute(
            "INSERT INTO telemetry (service, metric, value, labels) VALUES ('worker', 'recovery_seconds', %s, %s)",
            (info["total_s"], Jsonb({"events": len(rows), "checkpoint_seq": checkpoint_seq,
                                  "portfolio_id": self.pid})))
        return info

    def write_checkpoint(self) -> None:
        """Caller includes this write in the ledger transaction; never drop the log."""
        s = self.state
        snapshot = s.snapshot()
        self.conn.execute(
            "INSERT INTO ledger_checkpoints (portfolio_id, version, seq, head_hash, digest, snapshot, snapshot_hash) "
            "VALUES (%s,1,%s,%s,%s,%s,%s) ON CONFLICT (portfolio_id) DO UPDATE SET "
            "version = EXCLUDED.version, seq = EXCLUDED.seq, head_hash = EXCLUDED.head_hash, "
            "digest = EXCLUDED.digest, snapshot = EXCLUDED.snapshot, snapshot_hash = EXCLUDED.snapshot_hash, "
            "created_at = now()",
            (self.pid, s.seq, s.head_hash, s.digest(), Jsonb(snapshot), sha256_hex(canonical(snapshot))))

    # ------------------------------------------------------------------ main loop
    def process_batch(self) -> list[int]:
        """Process up to batch_size pending commands atomically. Returns processed seqs."""
        s = self.state
        start_seq = s.seq
        checkpoint_written = False
        try:
            with self.conn.transaction():
                rows = self.conn.execute(
                    "SELECT seq, payload FROM commands WHERE portfolio_id = %s AND status = 'pending' "
                    "ORDER BY seq LIMIT %s FOR UPDATE SKIP LOCKED", (self.pid, self.batch_size)).fetchall()
                if not rows:
                    return []
                events: list[tuple] = []
                responses: list[tuple] = []
                orders_new: list[tuple] = []
                fills: list[tuple] = []
                order_updates: list[tuple] = []
                equity_pts: dict[int, tuple] = {}
                for seq, payload in rows:
                    try:
                        cmd = decode_command(payload)
                    except Exception as e:  # API validates; this guards direct inserts
                        responses.append((Jsonb({"status": "invalid", "error": str(e)[:300]}), seq))
                        continue
                    evs, resp = process(s, cmd, self.cfg)
                    resp["event_seqs"] = [evs[0]["seq"], evs[-1]["seq"]] if evs else []
                    responses.append((Jsonb(resp), seq))
                    for e in evs:
                        events.append((self.pid, e["seq"], seq, e["type"], Jsonb(e), e["hash"]))
                        self._project(e, orders_new, fills, order_updates)
                    if isinstance(cmd, ApplyBar) and evs:
                        t = int(cmd.bar.ts.timestamp())
                        equity_pts[t] = (_ts(t), self.pid, s.equity(), s.cash, s.market_value())
                check_invariants(s)  # never commit a state that violates I-02/I-03/I-07
                cur = self.conn.cursor()
                if events:
                    cur.executemany(
                        "INSERT INTO ledger_events (portfolio_id, seq, command_seq, type, event, hash) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", events)
                cur.executemany(
                    "UPDATE commands SET status = 'done', response = %s, processed_at = now() WHERE seq = %s",
                    responses)
                if orders_new:
                    cur.executemany(
                        "INSERT INTO orders (order_id, portfolio_id, agent_id, symbol, side, order_type, requested_qty, "
                        "approved_qty, limit_px, status, outcome, rules, signal_ts) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", orders_new)
                if fills:
                    cur.executemany(
                        "INSERT INTO fills (bar_ts, portfolio_id, event_seq, order_id, agent_id, symbol, side, qty, px, fee) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", fills)
                for sql, args in order_updates:
                    cur.execute(sql, args)
                if equity_pts:
                    cur.executemany(
                        "INSERT INTO equity_points (ts, portfolio_id, equity, cash, market_value) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (portfolio_id, ts) DO UPDATE SET "
                        "equity = EXCLUDED.equity, cash = EXCLUDED.cash, market_value = EXCLUDED.market_value",
                        list(equity_pts.values()))
                cur.execute(
                    "INSERT INTO portfolio_state (portfolio_id, seq, head_hash, digest, state, equity, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s, now()) ON CONFLICT (portfolio_id) DO UPDATE SET seq = EXCLUDED.seq, "
                    "head_hash = EXCLUDED.head_hash, digest = EXCLUDED.digest, state = EXCLUDED.state, "
                    "equity = EXCLUDED.equity, updated_at = now()",
                    (self.pid, s.seq, s.head_hash, s.digest(), Jsonb(public_state(s)), s.equity()))
                if self.checkpoint_interval and s.seq - self.checkpoint_seq >= self.checkpoint_interval:
                    self.write_checkpoint()
                    checkpoint_written = True
                seqs = [r[0] for r in rows]
                cur.execute("SELECT pg_notify('cmd_done', %s)", (json.dumps({"p": self.pid, "seqs": seqs}),))
                self.batches += 1
                if self.crash_at == "before_commit" and self.batches >= self.crash_after:
                    log.error("QS_CRASH_AT=before_commit: simulating crash with an open transaction")
                    os._exit(137)
            if checkpoint_written:
                self.checkpoint_seq = s.seq
            if self.crash_at == "after_commit" and self.batches >= self.crash_after:
                log.error("QS_CRASH_AT=after_commit: simulating crash after commit")
                os._exit(137)
            return seqs
        except Exception:
            # transaction rolled back: rebuild in-memory state from the durable log
            log.exception("batch failed at seq %s; rolling back and recovering", start_seq)
            self.recover()
            raise

    def _project(self, e: dict, orders_new: list, fills: list, order_updates: list) -> None:
        t = e["type"]
        if t == "OrderAccepted":
            orders_new.append((e["order_id"], self.pid, e["agent_id"], e["symbol"], e["side"], e["order_type"],
                               e["requested_qty"], e["qty"], e["limit_px"], "working", e["outcome"], e["rules"],
                               _ts(e["signal_ts"])))
        elif t == "OrderRejected":
            orders_new.append((e["order_id"], self.pid, e["agent_id"], e["symbol"], e["side"], None,
                               e["requested_qty"], 0, None, "rejected", "REJECT", e["rules"], None))
        elif t == "OrderFilled":
            fills.append((_ts(e["bar_ts"]), self.pid, e["seq"], e["order_id"], e["agent_id"], e["symbol"], e["side"],
                          e["qty"], e["px"], e["fee"]))
            order_updates.append(("UPDATE orders SET filled_qty = filled_qty + %s, updated_at = now() "
                                  "WHERE portfolio_id = %s AND order_id = %s",
                                  (e["qty"], self.pid, e["order_id"])))
        elif t == "OrderClosed":
            order_updates.append(("UPDATE orders SET status = %s, updated_at = now() "
                                  "WHERE portfolio_id = %s AND order_id = %s",
                                  (e["reason"], self.pid, e["order_id"])))

    def run_forever(self, idle_wait_s: float = 0.5, rest_after_s: float = 3.0, rest_s: float = 0.25) -> None:
        listen = psycopg.connect(database_url(), autocommit=True)
        listen.execute("LISTEN cmd_new")
        last_beat = 0.0
        last_work = time.monotonic()
        while True:
            try:
                n = len(self.process_batch())
            except Exception:
                time.sleep(0.5)
                continue
            now = time.time()
            if now - last_beat > 2:
                self.conn.execute("INSERT INTO telemetry (service, metric, value, labels) VALUES "
                                  "('worker', 'heartbeat', %s, %s)", (self.state.seq, Jsonb({"portfolio_id": self.pid})))
                last_beat = now
            if n:
                last_work = time.monotonic()
            else:
                if time.monotonic() - last_work > rest_after_s:
                    # cmd_new is one global channel, so a command for any portfolio wakes every worker. A worker that
                    # has had no work of its own for a while rests between looks and discards the wake-ups it slept
                    # through, so idle portfolios stay cheap while another one is busy. Costs an idle portfolio at
                    # most rest_s of extra latency on its next command.
                    time.sleep(rest_s)
                    if sum(1 for _ in listen.notifies(timeout=0)):
                        continue
                for _ in listen.notifies(timeout=idle_wait_s, stop_after=1):
                    pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio", default=os.environ.get("QS_PORTFOLIO", "main"))
    ap.add_argument("--batch-size", type=int, default=int(os.environ.get("QS_BATCH_SIZE", "64")))
    ap.add_argument("--checkpoint-interval", type=int,
                    default=int(os.environ.get("QS_CHECKPOINT_INTERVAL", "10000")))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    conn = psycopg.connect(database_url(), autocommit=True)
    apply_schema(conn)
    log.info("waiting for single-writer lock on portfolio %s", args.portfolio)
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"writer:{args.portfolio}",))
    w = Worker(conn, args.portfolio, args.batch_size, checkpoint_interval=args.checkpoint_interval)
    info = w.recover()
    log.info("recovered portfolio %s: %d events replayed in %.3fs, digest %s", args.portfolio, info["events"],
             info["total_s"], info["digest"][:16])
    w.run_forever()


if __name__ == "__main__":
    main()
