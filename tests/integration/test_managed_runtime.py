"""Exercise the actual manager, API, children, queue and restart reconciliation."""
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx
import pytest

from hellabandz.agents.tournament import AGENTS
from hellabandz.contracts import OrderRequest
from hellabandz.ledger import canonical, sha256_hex
from tests.conftest import ROOT, wait_until

pytestmark = pytest.mark.db


@contextmanager
def manager(stack, tmp_path, **extra):
    with (tmp_path / "manager.log").open("a") as out:
        child = subprocess.Popen([sys.executable, "-m", "hellabandz.execution.manager"], cwd=ROOT,
                                 env=stack.env(QS_API=stack.base, QS_MANIFESTS=str(tmp_path), OPENAI_API_KEY="", **extra),
                                 stdout=out, stderr=subprocess.STDOUT)
        try:
            yield child
        except Exception:
            print((tmp_path / "manager.log").read_text()[-6000:])
            for path in (tmp_path / "job-logs").glob("*.log"):
                print(path.name, path.read_text()[-6000:])
            raise
        finally:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def runtime(c, pid):
    response = c.get("/v1/runtime", params={"portfolio_id": pid})
    response.raise_for_status()
    return response.json()


def freeze(c):
    dataset = next(d for d in c.get("/v1/datasets").json() if d["dataset_id"] == "synthetic-1h-seed42")
    manifest = {"run_id": "runtime-test", "dataset": dataset,
                "policy": {"initial_cash_micros": 10_000_000_000},
                "fitted": [{"spec": {"strategy_id": "strategy", "agent_id": "a", "family": "sma_crossover",
                                      "params": {"fast": 5, "slow": 20}, "universe": ["SPY"]}}],
                "allocations": {"by_strategy": {"strategy": {"capital_micros": 4_000_000_000}}}}
    assert c.post("/v1/tournaments", json={"run_id": manifest["run_id"], "manifest": manifest,
                                          "manifest_hash": sha256_hex(canonical(manifest))}).status_code == 200
    return dataset, manifest


@pytest.mark.skipif(sys.platform == "win32", reason="supervisor process lifecycle is verified by the Linux proof")
def test_managed_crash_queue_exactly_once_and_isolated_health(stack, tmp_path):
    stack.start_api()
    with httpx.Client(base_url=stack.base, timeout=10) as c, manager(stack, tmp_path):
        for pid in ("managed-a", "managed-b"):
            assert c.post("/v1/portfolios", json={"portfolio_id": pid}).status_code == 201
            wait_until(lambda: runtime(c, pid)["ready"])
            assert c.post("/v1/admin/portfolio", params={"portfolio_id": pid}, json={}).status_code == 200
        original = c.get("/v1/portfolio", params={"portfolio_id": "managed-a"}).json()
        assert c.post("/v1/runtime", params={"portfolio_id": "managed-a"}, json={"action": "crash"}).status_code == 200
        wait_until(lambda: runtime(c, "managed-a")["managed"]["status"] == "intentional_down")
        assert not runtime(c, "managed-a")["ready"]
        assert runtime(c, "managed-b")["ready"]
        from datetime import datetime, timezone
        order = OrderRequest(client_order_id="queued-retry", agent_id="a", symbol="SPY", side="BUY", qty=1,
                             signal_bar_ts=datetime(2024, 1, 1, tzinfo=timezone.utc))
        for _ in range(2):
            assert c.post("/v1/orders", params={"portfolio_id": "managed-a", "wait": False},
                          json=order.model_dump(mode="json")).status_code == 202
        assert runtime(c, "managed-a")["pending_commands"] == 1
        assert c.get("/v1/portfolio", params={"portfolio_id": "managed-a"}).json()["digest"] == original["digest"]
        c.post("/v1/runtime", params={"portfolio_id": "managed-a"}, json={"action": "restart"}).raise_for_status()
        wait_until(lambda: runtime(c, "managed-a")["ready"] and runtime(c, "managed-a")["pending_commands"] == 0)
        assert stack.sql("SELECT count(*) FROM ledger_events WHERE portfolio_id = 'managed-a' AND type = 'OrderRejected'")[0][0] == 1
        assert c.get("/v1/verify", params={"portfolio_id": "managed-a"}).json()["match"]
        assert c.get("/v1/portfolio", params={"portfolio_id": "managed-b"}).json()["state"]["cash"] == 10_000_000_000


@pytest.mark.skipif(sys.platform == "win32", reason="graceful supervisor SIGTERM is verified by the Linux proof")
def test_session_stop_while_worker_down_and_manager_restart_reconcile(stack, tmp_path):
    stack.start_api()
    with httpx.Client(base_url=stack.base, timeout=10) as c:
        dataset, manifest = freeze(c)
        body = {"request_id": "session-stop", "kind": "session", "dataset_id": dataset["dataset_id"],
                "run_id": manifest["run_id"], "portfolio_id": "session-stop", "steps": 100, "speed": 1}
        with manager(stack, tmp_path):
            response = c.post("/v1/jobs", json=body)
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            def job():
                return next(j for j in c.get("/v1/jobs").json() if j["job_id"] == job_id)
            wait_until(lambda: job()["progress"] >= 1)
            c.post("/v1/runtime", params={"portfolio_id": body["portfolio_id"]}, json={"action": "crash"}).raise_for_status()
            wait_until(lambda: runtime(c, body["portfolio_id"])["managed"]["status"] == "intentional_down")
            wait_until(lambda: runtime(c, body["portfolio_id"])["pending_commands"] > 0)
            started = time.monotonic()
            c.post(f"/v1/jobs/{job_id}/stop").raise_for_status()
            wait_until(lambda: job()["status"] == "stopped", timeout=8)
            assert time.monotonic() - started < 8
            assert job()["result"]["stopped"]
            assert runtime(c, body["portfolio_id"])["pending_commands"] > 0
            # Mimic persisted records left by an interrupted manager.
            stack.sql("INSERT INTO jobs (job_id, request_id, kind, dataset_id, run_id, config, status, stop_requested) "
                      "VALUES ('interrupted','interrupted','research',%s,'r','{}','running',TRUE)", (dataset["dataset_id"],))
            stack.sql("INSERT INTO jobs (job_id, request_id, kind, dataset_id, run_id, config, status, stop_requested) "
                      "VALUES ('queued-stopped','queued-stopped','research',%s,'r','{}','queued',TRUE)", (dataset["dataset_id"],))
            queued = c.post("/v1/jobs", json={"request_id": "queued-stop", "kind": "research",
                                             "dataset_id": dataset["dataset_id"]}).json()
            c.post(f"/v1/jobs/{queued['job_id']}/stop").raise_for_status()
        digest = c.get("/v1/portfolio", params={"portfolio_id": body["portfolio_id"]}).json()["digest"]
        with manager(stack, tmp_path):
            wait_until(lambda: runtime(c, body["portfolio_id"])["manager_alive"])
            wait_until(lambda: stack.sql("SELECT status FROM jobs WHERE job_id = 'interrupted'")[0][0] == "interrupted")
            wait_until(lambda: stack.sql("SELECT status FROM jobs WHERE job_id = 'queued-stopped'")[0][0] == "stopped")
            assert stack.sql("SELECT status FROM jobs WHERE job_id = %s", (queued["job_id"],))[0][0] == "stopped"
            assert runtime(c, body["portfolio_id"])["intentional_down"]
            assert job()["status"] == "stopped"
            assert c.get("/v1/portfolio", params={"portfolio_id": body["portfolio_id"]}).json()["digest"] == digest
            c.post("/v1/runtime", params={"portfolio_id": body["portfolio_id"]}, json={"action": "restart"}).raise_for_status()
            wait_until(lambda: runtime(c, body["portfolio_id"])["ready"] and runtime(c, body["portfolio_id"])["pending_commands"] == 0)
            assert c.get("/v1/verify", params={"portfolio_id": body["portfolio_id"]}).json()["match"]


def test_session_waits_for_command_before_using_projection_and_can_stop():
    from hellabandz.agents.session import Session, SessionStopped
    session = Session.__new__(Session)
    calls = []
    def respond(request):
        calls.append(request)
        if request.method == "POST":
            assert request.url.params["wait"] == "false"
            return httpx.Response(202, json={"command_seq": 7, "status": "queued"})
        return httpx.Response(200, json={"status": "done", "response": {"status": "accepted", "outcome": "APPROVE"}})
    session.c = httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond))
    session.progress = lambda *args: None
    session.stop_requested = lambda: False
    assert session.post("/v1/orders")["outcome"] == "APPROVE"
    assert calls[-1].url.path == "/v1/commands/7"
    session.stop_requested = lambda: len(calls) > 2
    with pytest.raises(SessionStopped):
        session.post("/v1/orders")


@pytest.mark.skipif(sys.platform == "win32", reason="supervisor process lifecycle is verified by the Linux proof")
def test_real_research_job_persists_manifest_baseline_and_session(stack, tmp_path):
    from hellabandz.ingestion.dataset import load_snapshot, write_snapshot
    table, _ = load_snapshot(ROOT / "data/fixtures/synthetic_1h")
    times = sorted(set(table.ts))
    table = table.select(table.ts < times[120])
    snapshots = tmp_path / "snapshots"
    write_snapshot(table, snapshots / "short", {"dataset_id": "runtime-synthetic", "source": "synthetic test fixture", "interval_s": 3600})
    stack.start_api(QS_SNAPSHOTS=str(snapshots), QS_MANIFESTS=str(tmp_path), OPENAI_API_KEY="")
    with httpx.Client(base_url=stack.base, timeout=10) as c, manager(stack, tmp_path, QS_SNAPSHOTS=str(snapshots), QS_TOURNAMENT_PROCS="1"):
        research = c.post("/v1/jobs", json={"request_id": "real-research", "kind": "research", "dataset_id": "runtime-synthetic"})
        assert research.status_code == 202
        job_id = research.json()["job_id"]
        def job(ident):
            return next(j for j in c.get("/v1/jobs").json() if j["job_id"] == ident)
        wait_until(lambda: job(job_id)["status"] in ("completed", "failed"), timeout=90)
        assert job(job_id)["status"] == "completed", job(job_id)
        run_id = job(job_id)["run_id"]
        frozen = c.get(f"/v1/tournaments/{run_id}/manifest").json()
        assert len(frozen["proposals"]) == len(AGENTS) and len(frozen["fitted"]) == len(AGENTS)
        assert all(p["source"] == "fixture" for p in frozen["proposals"])
        assert (tmp_path / f"{run_id}.json").is_file()
        assert (tmp_path / f"{run_id}.analytics.json").is_file()
        session = c.post("/v1/jobs", json={"request_id": "real-session", "kind": "session", "dataset_id": "runtime-synthetic",
                                           "run_id": run_id, "portfolio_id": "research-session", "steps": 5, "speed": 0})
        assert session.status_code == 202
        session_id = session.json()["job_id"]
        wait_until(lambda: job(session_id)["status"] in ("completed", "failed"), timeout=60)
        assert job(session_id)["status"] == "completed", job(session_id)
        assert job(session_id)["result"]["steps"] == 5
        assert c.get("/v1/verify", params={"portfolio_id": "research-session"}).json()["match"]
        analytics = c.get("/v1/analytics/history", params={"portfolio_id": "research-session"}).json()
        assert analytics["benchmark"]["status"] == "available"
        assert analytics["benchmark"]["manifest_hash"] == sha256_hex(canonical(frozen))
        assert analytics["points"][-1]["equity"] == c.get("/v1/portfolio", params={"portfolio_id": "research-session"}).json()["equity"]
