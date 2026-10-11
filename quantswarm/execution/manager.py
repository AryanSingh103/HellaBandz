"""Local supervisor: owns only children created for explicitly managed portfolios.

The manager lock is database-scoped. On restart, running jobs become interrupted;
economic sessions are never automatically replayed. Worker recovery is safe and
independent of job recovery. No Docker socket or arbitrary process API is used.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from quantswarm.execution.db import apply_schema, database_url
from quantswarm.ingestion.registry import MANIFESTS, resolve

log = logging.getLogger("manager")
API = os.environ.get("QS_API", "http://api:8000")


def connect():
    return psycopg.connect(database_url(), autocommit=True, row_factory=dict_row)


def import_associations(conn):
    # A historical filename alone is insufficient: its recorded final digest
    # must agree with the actual portfolio, and its tournament must exist.
    for path in MANIFESTS.glob("*.session.json"):
        try:
            artifact = json.loads(path.read_text())
            run_id = artifact["run_id"]
            pid = artifact.get("portfolio_id") or path.name.removeprefix(run_id + ".").removesuffix(".session.json")
            from pydantic import TypeAdapter
            from quantswarm.contracts import Ident
            TypeAdapter(Ident).validate_python(pid)
            row = conn.execute("SELECT t.manifest FROM tournaments t JOIN portfolio_state s ON s.portfolio_id = %s "
                               "WHERE t.run_id = %s AND s.digest = %s", (pid, run_id, artifact["ledger_digest"])).fetchone()
            if row:
                conn.execute("INSERT INTO portfolio_context (portfolio_id, run_id, dataset_id, provenance) "
                             "VALUES (%s,%s,%s,'session artifact verified against portfolio digest') "
                             "ON CONFLICT DO NOTHING", (pid, run_id, row["manifest"]["dataset"]["dataset_id"]))
        except (ValueError, KeyError, OSError):
            log.warning("unusable historical session artifact: %s", path.name)


def run_job(job_id: str):
    with connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
        if not job or job["status"] != "running":
            return
        def stop_requested():
            status = conn.execute("SELECT stop_requested, status FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
            return status["stop_requested"] or status["status"] != "running"

        def progress(stage, value=None, total=None):
            conn.execute("UPDATE jobs SET stage = %s, progress = coalesce(%s, progress), "
                         "total = coalesce(%s,total), updated_at = now() WHERE job_id = %s AND status = 'running'",
                         (stage, value, total, job_id))
        try:
            from quantswarm.api.auth import disabled, internal_token
            api_headers = {}
            if not disabled():
                owner = conn.execute("SELECT owner_id FROM account_resources WHERE kind='job' AND resource_id=%s", (job_id,)).fetchone()
                if not owner:
                    raise RuntimeError("Unowned jobs cannot run with account authentication enabled")
                api_headers = {"Authorization": "Bearer " + internal_token(str(owner["owner_id"]), job)}
            path = resolve(job["dataset_id"])
            if job["kind"] == "research":
                from quantswarm.agents.tournament import run
                from quantswarm.agents.baseline import save_analytics
                count = 0
                def research_progress(agent, role, content):
                    nonlocal count
                    count += 1
                    progress(role, count, None)
                result = run(path, job["run_id"], API, research_progress, api_headers=api_headers)
                progress("benchmark")
                save_analytics(result["manifest"], path)
                progress("benchmark", count + 1, count + 1)
                result = {"run_id": result["run_id"], "manifest_hash": result["manifest_hash"]}
            else:
                import httpx
                from quantswarm.agents.session import Session, SessionStopped
                manifest = conn.execute("SELECT manifest FROM tournaments WHERE run_id = %s", (job["run_id"],)).fetchone()["manifest"]
                deadline = time.monotonic() + 60
                progress("waiting_for_worker")
                while True:
                    r = httpx.get(API + "/v1/runtime", params={"portfolio_id": job["portfolio_id"]}, timeout=5, headers=api_headers)
                    r.raise_for_status()
                    if r.json()["ready"]:
                        break
                    if stop_requested():
                        conn.execute("UPDATE jobs SET status = 'stopped', stage = 'stopped', finished_at = now() WHERE job_id = %s", (job_id,))
                        return
                    if time.monotonic() > deadline:
                        raise TimeoutError("managed worker did not become ready")
                    time.sleep(0.2)
                session = Session(API, manifest, path, job["portfolio_id"], stop_requested, progress, api_headers=api_headers)
                config = job["config"]
                try:
                    session.setup()
                    result = session.run(config["steps"], config["speed"], config["crash_at"])
                except SessionStopped:
                    result = {"run_id": job["run_id"], "portfolio_id": job["portfolio_id"], "stopped": True,
                              "detail": "Future session production stopped. Already queued commands and working orders are preserved.",
                              "verify": session.c.get("/v1/verify").json(), **session.stats}
                MANIFESTS.mkdir(parents=True, exist_ok=True)
                (MANIFESTS / f"{job['run_id']}.{job['portfolio_id']}.session.json").write_text(json.dumps(result, indent=2))
            status = "stopped" if stop_requested() else "completed"
            conn.execute("UPDATE jobs SET status = %s, stage = %s, result = %s, finished_at = now(), updated_at = now() "
                         "WHERE job_id = %s AND status = 'running'", (status, status, Jsonb(result), job_id))
        except Exception as exc:
            log.exception("job %s failed", job_id)
            # Do not echo provider exception bodies, credentials, or filesystem paths to the browser.
            error = f"{type(exc).__name__}: job failed; check dataset compatibility, provider availability and selected worker readiness."
            conn.execute("UPDATE jobs SET status = 'failed', stage = 'failed', error = %s, finished_at = now(), "
                         "updated_at = now() WHERE job_id = %s AND status = 'running'", (error, job_id))


def supervise():
    conn = connect()
    apply_schema(conn)
    if not conn.execute("SELECT pg_try_advisory_lock(424243) AS locked").fetchone()["locked"]:
        raise RuntimeError("another local manager already owns the supervisor lock")
    conn.execute("UPDATE jobs SET status = 'interrupted', stage = 'interrupted', finished_at = now(), "
                 "error = 'Manager restarted. Economic sessions are not automatically relaunched; use a fresh portfolio.' "
                 "WHERE status = 'running'")
    conn.execute("UPDATE jobs SET status = 'stopped', stage = 'stopped', finished_at = now(), "
                 "updated_at = now() WHERE status = 'queued' AND stop_requested")
    import_associations(conn)
    workers, children, logs = {}, {}, {}
    shutdown = False
    def stop(*args):
        nonlocal shutdown
        shutdown = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    MANIFESTS.mkdir(parents=True, exist_ok=True)
    log_dir = MANIFESTS / "job-logs"
    log_dir.mkdir(exist_ok=True)
    last_beat = 0
    try:
        while not shutdown:
            for row in conn.execute("SELECT * FROM managed_workers ORDER BY portfolio_id").fetchall():
                pid, generation = row["portfolio_id"], row["generation"]
                child_info = workers.get(pid)
                child = child_info[0] if child_info else None
                if child and (row["desired"] == "down" or child_info[1] != generation):
                    if child.poll() is None:
                        child.kill()
                    child.wait()
                    workers.pop(pid)
                    child = None
                if row["desired"] == "down":
                    conn.execute("UPDATE managed_workers SET status = 'intentional_down', updated_at = now() "
                                 "WHERE portfolio_id = %s AND status <> 'failed'", (pid,))
                    continue
                if child and child.poll() is not None:
                    conn.execute("UPDATE managed_workers SET status = 'failed', error = 'Execution recovery failed; restart to retry', "
                                 "desired = 'down', updated_at = now() WHERE portfolio_id = %s", (pid,))
                    workers.pop(pid)
                    continue
                if child is None:
                    # A fresh portfolio was atomically reserved by the API. The
                    # worker's own advisory lock remains the final single-writer guard.
                    child = subprocess.Popen([sys.executable, "-m", "quantswarm.execution.worker", "--portfolio", pid])
                    workers[pid] = (child, generation)
                    conn.execute("UPDATE managed_workers SET status = 'recovering', started_at = now(), "
                                 "updated_at = now() WHERE portfolio_id = %s", (pid,))
                else:
                    conn.execute("UPDATE managed_workers SET status = 'running', updated_at = now() "
                                 "WHERE portfolio_id = %s AND EXISTS (SELECT 1 FROM telemetry WHERE metric = 'heartbeat' "
                                 "AND labels->>'portfolio_id' = %s AND ts >= managed_workers.started_at "
                                 "AND ts > now() - interval '4 seconds')", (pid, pid))
            for job_id, child in list(children.items()):
                job = conn.execute("SELECT status, stop_requested, kind FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
                if job["stop_requested"] and job["kind"] == "research" and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
                    conn.execute("UPDATE jobs SET status = 'stopped', stage = 'stopped', finished_at = now() WHERE job_id = %s", (job_id,))
                if child.poll() is not None:
                    child.wait()
                    if job["status"] == "running" and not (job["stop_requested"] and job["kind"] == "research"):
                        conn.execute("UPDATE jobs SET status = 'failed', stage = 'failed', finished_at = now(), "
                                     "error = 'Job child exited unexpectedly; inspect local manager logs' "
                                     "WHERE job_id = %s AND status = 'running'", (job_id,))
                    children.pop(job_id)
                    logs.pop(job_id).close()
            if len(children) < 4:
                job = conn.execute("SELECT * FROM jobs WHERE status = 'queued' AND NOT stop_requested "
                                   "AND (kind <> 'research' OR NOT EXISTS "
                                   "(SELECT 1 FROM jobs WHERE kind = 'research' AND status = 'running')) "
                                   "ORDER BY created_at LIMIT 1").fetchone()
                if job:
                    conn.execute("UPDATE jobs SET status = 'running', stage = 'starting', started_at = now(), "
                                 "updated_at = now() WHERE job_id = %s", (job["job_id"],))
                    out = (log_dir / f"{job['job_id']}.log").open("a")
                    logs[job["job_id"]] = out
                    children[job["job_id"]] = subprocess.Popen(
                        [sys.executable, "-m", "quantswarm.execution.manager", "--job", job["job_id"]],
                        stdout=out, stderr=subprocess.STDOUT)
            if time.monotonic() - last_beat > 2:
                conn.execute("INSERT INTO telemetry (service, metric, value) VALUES ('manager','heartbeat',1)")
                last_beat = time.monotonic()
            time.sleep(0.2)
    finally:
        for child in [*children.values(), *(v[0] for v in workers.values())]:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        conn.execute("UPDATE jobs SET status = 'interrupted', stage = 'interrupted', finished_at = now(), "
                     "error = 'Manager stopped; select a fresh portfolio to repeat a session' WHERE status = 'running'")
        for out in logs.values():
            out.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run_job(args.job) if args.job else supervise()


if __name__ == "__main__":
    main()
