"""Batched Devnet commitments over all durable trading, agent and license records.

Only a manifest digest leaves the machine. Verification pins the latest proof
from the configured public signer, then reconstructs its consistent DB snapshot
by undoing journaled later changes. Raw records and signing keys stay local.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from quantswarm.audit import solana
from quantswarm.execution.db import apply_schema, database_url
from quantswarm.ledger import canonical, sha256_hex

TABLES = {
    "commands": ["seq"], "ledger_events": ["portfolio_id", "seq"],
    "portfolio_state": ["portfolio_id"], "orders": ["portfolio_id", "order_id"],
    "fills": ["portfolio_id", "event_seq", "bar_ts"], "equity_points": ["portfolio_id", "ts"],
    "agent_messages": ["id"], "tournaments": ["run_id"], "portfolio_context": ["portfolio_id"],
    "jobs": ["job_id"], "strategy_accounts": ["account_id"], "strategy_assets": ["asset_id"],
    "strategy_holdings": ["account_id", "asset_id"], "strategy_listings": ["listing_id"],
    "strategy_trades": ["request_id"], "review_analyses": ["digest"],
}
IMMUTABLE = {"ledger_events", "fills", "agent_messages", "strategy_trades", "review_analyses", "strategy_assets"}
PREFIX = "quantswarm:audit:v1:"
LOCK = 424246  # only serializes sealers, never trading transactions


class UnauthorizedSigner(ValueError):
    pass


def digest(value):
    return sha256_hex(canonical(value))


def key(table, row):
    # Same text and ordering as Postgres jsonb_build_array(... )::text COLLATE C.
    return json.dumps([row[k] for k in TABLES[table]], ensure_ascii=False)


def row_root(rows):
    return {"count": len(rows), "sha256": digest([rows[k] for k in sorted(rows)])}


def ranges(ids):
    result = []
    for i in sorted(ids):
        if result and i == result[-1][1] + 1:
            result[-1][1] = i
        else:
            result.append([i, i])
    return result


def capture(conn):
    """Caller must use REPEATABLE READ: journal and records share one snapshot."""
    conn.execute("SET LOCAL TIME ZONE 'UTC'")
    rows = {}
    for table in TABLES:
        records = conn.execute(sql.SQL("SELECT to_jsonb(t) AS row FROM {} t").format(sql.Identifier(table))).fetchall()
        rows[table] = {key(table, r["row"]): r["row"] for r in records}
    journal = [r["row"] for r in conn.execute("SELECT to_jsonb(t) AS row FROM audit_changes t ORDER BY id")]
    return rows, journal


def make_manifest(rows, journal, previous=None):
    return {"version": 1, "anchor_id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(), "cluster": "devnet",
            "signer": os.environ["QS_AUDIT_SIGNER"], "previous": previous,
            "tables": {t: row_root(rows[t]) for t in TABLES},
            "journal": {"ranges": ranges(r["id"] for r in journal), "count": len(journal), "sha256": digest(journal)}}


class StreamHash:
    """Hash a canonical JSON array without retaining its contents."""
    def __init__(self):
        self.hash = hashlib.sha256(b"[")
        self.count = 0

    def add(self, row):
        if self.count:
            self.hash.update(b",")
        self.hash.update(canonical(row).encode())
        self.count += 1

    def result(self):
        final = self.hash.copy()
        final.update(b"]")
        return {"count": self.count, "sha256": final.hexdigest()}


def stream_rows(conn, query):
    with conn.cursor(name="audit_" + uuid.uuid4().hex) as cur:
        cur.itersize = 500
        cur.execute(query)
        for record in cur:
            yield record["row"]


def stream_snapshot(conn, manifest=None):
    """Bounded baseline memory; verification retains only keys changed after a seal.

    Journal reverse folding keeps one before/after pair per changed record, rather
    than all versions of large portfolio state. Table and journal hashes stream.
    """
    conn.execute("SET LOCAL TIME ZONE 'UTC'")
    intervals = manifest["journal"]["ranges"] if manifest else []
    def sealed(i):
        return any(lo <= i <= hi for lo, hi in intervals)
    journal_hash = StreamHash()
    id_ranges = []
    for row in stream_rows(conn, "SELECT to_jsonb(t) AS row FROM audit_changes t ORDER BY id"):
        if manifest and not sealed(row["id"]):
            continue
        journal_hash.add(row)
        i = row["id"]
        if id_ranges and i == id_ranges[-1][1] + 1:
            id_ranges[-1][1] = i
        else:
            id_ranges.append([i, i])
    journal = {**journal_hash.result(), "ranges": id_ranges}
    if manifest and journal != manifest["journal"]:
        raise ValueError("sealed change journal differs (modified or missing history)")
    overlays = {t: {} for t in TABLES}
    pending = 0
    if manifest:
        for entry in stream_rows(conn, "SELECT to_jsonb(t) AS row FROM audit_changes t ORDER BY id DESC"):
            if sealed(entry["id"]):
                continue
            pending += 1
            t, before, after = entry["table_name"], entry["before_row"], entry["after_row"]
            if t not in TABLES:
                raise ValueError("unknown journal table")
            if t in IMMUTABLE and before is not None:
                raise ValueError(f"immutable trace rewritten: {t}")
            if before and after and key(t, before) != key(t, after):
                raise ValueError("record primary key rewritten")
            if t in ("commands", "tournaments") and before and after:
                allowed = {"status", "response", "processed_at"} if t == "commands" else {"solana_sig"}
                if {k: v for k, v in before.items() if k not in allowed} != {k: v for k, v in after.items() if k not in allowed}:
                    raise ValueError(f"immutable input rewritten: {t}")
            k = key(t, after if after is not None else before)
            existing = overlays[t].get(k)
            if existing:
                if existing["before"] != after:
                    raise ValueError(f"conflicting journal history: {t}")
                existing["before"] = before
            else:
                overlays[t][k] = {"after": after, "before": before}
    roots = {}
    for t, columns in TABLES.items():
        keys = sql.SQL(",").join(sql.Identifier("t", c) for c in columns)
        query = sql.SQL("SELECT to_jsonb(t) AS row FROM {} t ORDER BY jsonb_build_array({})::text COLLATE \"C\"").format(sql.Identifier(t), keys)
        overlay = overlays[t]
        ordered = iter(sorted(overlay))
        next_key = next(ordered, None)
        root = StreamHash()
        for row in stream_rows(conn, query):
            k = key(t, row)
            while next_key is not None and next_key < k:
                change = overlay[next_key]
                if change["after"] is not None:
                    raise ValueError(f"unlogged deletion: {t}")
                if change["before"] is not None:
                    root.add(change["before"])
                next_key = next(ordered, None)
            if next_key == k:
                change = overlay[k]
                if change["after"] != row:
                    raise ValueError(f"unlogged or conflicting change: {t}")
                if change["before"] is not None:
                    root.add(change["before"])
                next_key = next(ordered, None)
            else:
                root.add(row)
        while next_key is not None:
            change = overlay[next_key]
            if change["after"] is not None:
                raise ValueError(f"unlogged deletion: {t}")
            if change["before"] is not None:
                root.add(change["before"])
            next_key = next(ordered, None)
        roots[t] = root.result()
    if manifest and roots != manifest["tables"]:
        raise ValueError("sealed records differ: " + ", ".join(t for t in TABLES if roots[t] != manifest["tables"][t]))
    return roots, journal, pending


def check_snapshot(manifest, rows, journal):
    if manifest.get("version") != 1 or set(manifest.get("tables", {})) != set(TABLES):
        raise ValueError("unsupported audit coverage or manifest version")
    intervals = manifest["journal"]["ranges"]
    # IDs are compressed, not expanded: malformed huge ranges cannot allocate memory.
    def sealed(i):
        return any(lo <= i <= hi for lo, hi in intervals)
    prefix = [r for r in journal if sealed(r["id"])]
    if len(prefix) != manifest["journal"]["count"] or digest(prefix) != manifest["journal"]["sha256"]:
        raise ValueError("sealed change journal differs (modified or missing history)")
    suffix = [r for r in journal if not sealed(r["id"])]
    for entry in reversed(suffix):
        t, before, after = entry["table_name"], entry["before_row"], entry["after_row"]
        if t not in TABLES:
            raise ValueError("unknown journal table")
        if t in IMMUTABLE and before is not None:
            raise ValueError(f"immutable trace rewritten: {t}")
        if t == "commands" and before and after:
            allowed = {"status", "response", "processed_at"}
            if {k: v for k, v in before.items() if k not in allowed} != {k: v for k, v in after.items() if k not in allowed}:
                raise ValueError("command input rewritten")
        if t == "tournaments" and before and after:
            if {k: v for k, v in before.items() if k != "solana_sig"} != {k: v for k, v in after.items() if k != "solana_sig"}:
                raise ValueError("research manifest rewritten")
        if after is not None:
            k = key(t, after)
            if rows[t].get(k) != after:
                raise ValueError(f"unlogged or conflicting change: {t}")
            del rows[t][k]
        elif before is not None and key(t, before) in rows[t]:
            raise ValueError(f"deleted record reappeared without journal: {t}")
        if before is not None:
            k = key(t, before)
            if k in rows[t]:
                raise ValueError(f"conflicting record key: {t}")
            rows[t][k] = before
    results = {t: row_root(rows[t]) == manifest["tables"][t] for t in TABLES}
    if not all(results.values()):
        raise ValueError("sealed records differ: " + ", ".join(t for t, ok in results.items() if not ok))
    return {"tables": results, "pending_changes": len(suffix), "sealed_records": sum(x["count"] for x in manifest["tables"].values())}


def signer():
    value = os.environ.get("QS_AUDIT_SIGNER", "")
    if not value:
        raise RuntimeError("QS_AUDIT_SIGNER must pin the trusted public Devnet wallet")
    from solders.pubkey import Pubkey
    Pubkey.from_string(value)
    return value


def transaction_memo(signature):
    tx = solana.rpc("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                    "commitment": "finalized"}])
    if not tx or tx.get("meta", {}).get("err"):
        raise ValueError("audit transaction unavailable, not finalized, or failed")
    message = tx["transaction"]["message"]
    payer = message["accountKeys"][0]
    if payer.get("pubkey") != signer() or not payer.get("signer"):
        raise UnauthorizedSigner("audit transaction has an unauthorized signer")
    memos = [ix.get("parsed") for ix in message["instructions"] if ix.get("programId") == solana.MEMO_PROGRAM]
    if len(memos) != 1 or not isinstance(memos[0], str) or not memos[0].startswith(PREFIX):
        raise ValueError("transaction does not contain one audit memo")
    return memos[0]


def latest_chain():
    """Do not trust a DB-supplied 'latest' proof: discover it from the pinned wallet."""
    solana.require_devnet()
    before = None
    for _ in range(10):
        options = {"limit": 100, "commitment": "finalized"}
        if before:
            options["before"] = before
        records = solana.rpc("getSignaturesForAddress", [signer(), options])
        for record in records:
            if not record.get("err") and PREFIX in (record.get("memo") or ""):
                signature = record["signature"]
                try:
                    memo = transaction_memo(signature)
                except UnauthorizedSigner:
                    # Anyone may reference our address; only our fee-payer signature counts.
                    continue
                parts = memo[len(PREFIX):].split(":")
                if len(parts) != 2:
                    raise ValueError("invalid on-chain audit memo")
                return {"anchor_id": parts[0], "manifest_hash": parts[1], "signature": signature}
        if len(records) < 100:
            return None
        before = records[-1]["signature"]
    raise RuntimeError("audit discovery exceeded 1000 transactions; refusing to select an older proof")


def verify(url=None, chain=None):
    chain = chain or latest_chain()
    if not chain:
        return {"ok": False, "status": "not_anchored", "error": "No finalized audit proof yet"}
    with psycopg.connect(url or database_url(), row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        record = conn.execute("SELECT * FROM audit_anchors WHERE anchor_id=%s", (chain["anchor_id"],)).fetchone()
        if not record:
            raise ValueError("latest on-chain proof is missing from the database; possible rollback")
        manifest = record["manifest"]
        if digest(manifest) != chain["manifest_hash"] or record["manifest_hash"] != chain["manifest_hash"]:
            raise ValueError("manifest differs from latest on-chain commitment")
        if manifest.get("signer") != signer() or manifest.get("cluster") != "devnet":
            raise ValueError("manifest signer or network differs")
        if manifest.get("version") != 1 or set(manifest.get("tables", {})) != set(TABLES):
            raise ValueError("unsupported audit coverage or manifest version")
        roots, journal, pending = stream_snapshot(conn, manifest)
        details = {"tables": {t: True for t in TABLES}, "pending_changes": pending,
                   "sealed_records": sum(x["count"] for x in roots.values())}
    return {"ok": True, "status": "verified", **chain, **details, "sealed_at": manifest["created_at"],
            "cluster": "devnet", "explorer": solana.explorer(chain["signature"])}


def seal(url=None, force=False):
    url = url or database_url()
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as lock:
        if not lock.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK,)).fetchone()["ok"]:
            return {"status": "busy"}
        try:
            previous = latest_chain()
            if previous:
                verified = verify(url, previous)  # do not launder tampered history into a fresh seal
                if not force and verified["pending_changes"] == 0 and not lock.execute("SELECT 1 FROM audit_anchors WHERE signature IS NULL LIMIT 1").fetchone():
                    return {"status": "unchanged", **previous}
            elif lock.execute("SELECT 1 FROM audit_anchors WHERE signature IS NOT NULL LIMIT 1").fetchone():
                raise ValueError("stored proofs exist but no finalized chain proof was found")
            # Recover a previously persisted candidate after a signer crash.
            candidate = lock.execute("SELECT * FROM audit_anchors WHERE signature IS NULL ORDER BY created_at LIMIT 1").fetchone()
            if not candidate:
                with psycopg.connect(url, row_factory=dict_row) as conn:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    if previous:
                        old = conn.execute("SELECT manifest FROM audit_anchors WHERE anchor_id=%s", (previous["anchor_id"],)).fetchone()["manifest"]
                        stream_snapshot(conn, old)  # same snapshot: no verify/capture race
                    roots, journal, _ = stream_snapshot(conn)
                if previous and not force:
                    old = lock.execute("SELECT manifest FROM audit_anchors WHERE anchor_id=%s", (previous["anchor_id"],)).fetchone()["manifest"]
                    if old["journal"] == journal:
                        return {"status": "unchanged", **previous}
                manifest = {"version": 1, "anchor_id": uuid.uuid4().hex,
                            "created_at": datetime.now(timezone.utc).isoformat(), "cluster": "devnet",
                            "signer": signer(), "previous": previous, "tables": roots, "journal": journal}
                candidate = {"anchor_id": manifest["anchor_id"], "manifest": manifest, "manifest_hash": digest(manifest)}
                lock.execute("INSERT INTO audit_anchors(anchor_id,manifest,manifest_hash) VALUES (%s,%s,%s)",
                             (candidate["anchor_id"], Jsonb(manifest), candidate["manifest_hash"]))
            else:
                with psycopg.connect(url, row_factory=dict_row) as conn:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    stream_snapshot(conn, candidate["manifest"])
            kp = solana.load_keypair()
            if str(kp.pubkey()) != signer():
                raise ValueError("local signing key does not match pinned audit signer")
            # If it landed just before a crash, reuse the on-chain signature.
            if previous and previous["anchor_id"] == candidate["anchor_id"]:
                signature = previous["signature"]
            else:
                signature = solana.send_memo(PREFIX + candidate["anchor_id"] + ":" + candidate["manifest_hash"])
                for _ in range(60):
                    status = solana.rpc("getSignatureStatuses", [[signature]])["value"][0]
                    if status and status.get("confirmationStatus") == "finalized":
                        if status.get("err"):
                            raise RuntimeError("audit transaction failed")
                        break
                    time.sleep(1)
                else:
                    raise TimeoutError("audit transaction not finalized yet")
            if transaction_memo(signature) != PREFIX + candidate["anchor_id"] + ":" + candidate["manifest_hash"]:
                raise ValueError("on-chain audit memo mismatch")
            lock.execute("UPDATE audit_anchors SET signature=%s WHERE anchor_id=%s", (signature, candidate["anchor_id"]))
            receipt = {k: candidate[k] for k in ("anchor_id", "manifest", "manifest_hash")}
            receipt.update(signature=signature, explorer=solana.explorer(signature))
            directory = Path(os.getenv("QS_AUDIT_RECEIPTS", "data/audits"))
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (candidate["anchor_id"] + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
            return verify(url)
        finally:
            lock.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))


def service():
    interval = max(60, int(os.getenv("QS_AUDIT_INTERVAL", "300")))
    with psycopg.connect(database_url(), autocommit=True) as conn:
        apply_schema(conn)
    def heartbeat():
        while True:
            try:
                with psycopg.connect(database_url(), autocommit=True) as conn:
                    conn.execute("INSERT INTO audit_service_state(id,heartbeat) VALUES (1,now()) ON CONFLICT(id) DO UPDATE SET heartbeat=now()")
            except Exception:
                pass
            time.sleep(5)
    threading.Thread(target=heartbeat, daemon=True).start()
    next_seal = 0
    while True:
        try:
            with psycopg.connect(database_url(), autocommit=True, row_factory=dict_row) as conn:
                requests = conn.execute("SELECT request_id FROM audit_requests WHERE completed_at IS NULL ORDER BY created_at").fetchall()
                conn.execute("INSERT INTO audit_service_state(id,heartbeat) VALUES (1,now()) ON CONFLICT(id) DO UPDATE SET heartbeat=now()")
                if time.monotonic() >= next_seal:
                    delay = interval
                    try:
                        result = seal(force=bool(requests))
                        conn.execute("UPDATE audit_service_state SET error=NULL WHERE id=1")
                        print(json.dumps(result), flush=True)
                        if result.get("status") == "busy":
                            delay = 5
                        else:
                            for r in requests:
                                conn.execute("UPDATE audit_requests SET completed_at=now(),error=NULL WHERE request_id=%s", (r["request_id"],))
                    except Exception as exc:
                        conn.execute("UPDATE audit_service_state SET error=%s WHERE id=1", (str(exc),))
                        print(f"Audit sealing failed: {exc}", flush=True)
                        for r in requests:
                            conn.execute("UPDATE audit_requests SET completed_at=now(),error=%s WHERE request_id=%s", (str(exc), r["request_id"]))
                    next_seal = time.monotonic() + delay
        except Exception as exc:
            print(f"Audit service unavailable: {exc}", flush=True)
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seal", "verify", "service"])
    args = parser.parse_args()
    if args.action == "service":
        service()
    else:
        result = seal(force=True) if args.action == "seal" else verify()
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
