"""Browser API regressions run against disposable databases, never demo portfolios."""
import json
from datetime import datetime, timezone

import httpx
import psycopg
import pytest

from quantswarm.contracts import ApplyBar, Bar, CancelOrder, OpenPortfolio, OrderRequest, SetAllocation, SubmitOrder
from quantswarm.execution.db import apply_schema
from quantswarm.execution.worker import Worker
from quantswarm.ledger import canonical, sha256_hex
from tests.conftest import enqueue

pytestmark = pytest.mark.db
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def client(stack):
    stack.start_api()
    return httpx.Client(base_url=stack.base, timeout=30)


def test_manifest_hash_run_identity_and_immutable_retries(stack):
    c = client(stack)
    manifest = {"run_id": "T-frozen", "dataset": {"content_sha256": "abc"}}
    body = {"run_id": manifest["run_id"], "manifest": manifest, "manifest_hash": sha256_hex(canonical(manifest))}
    assert c.post("/v1/tournaments", json=body).status_code == 200
    assert c.post("/v1/tournaments", json=body).status_code == 200
    assert c.post("/v1/tournaments", json={**body, "manifest_hash": "bad"}).status_code == 422
    assert c.post("/v1/tournaments", json={**body, "run_id": "another"}).status_code == 422
    changed = {**manifest, "changed": True}
    assert c.post("/v1/tournaments", json={**body, "manifest": changed, "manifest_hash": sha256_hex(canonical(changed))}).status_code == 409
    r = c.post("/v1/tournaments", content=json.dumps({**body, "manifest": {**manifest, "bad": float('nan')}}),
               headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert c.get("/v1/tournaments").json()[0]["manifest"] == manifest


def test_invalid_money_and_identifiers_never_insert_commands(stack):
    c = client(stack)
    for value in ["abc", "-1", "NaN", "Infinity", "1e1000", "-0.0000001"]:
        assert c.post("/v1/admin/portfolio", json={"initial_cash_usd": value}).status_code == 422
        assert c.post("/v1/admin/allocations", json={"agent_id": "a", "capital_usd": value}).status_code == 422
    for value in ["0", "0.0000001"]:
        assert c.post("/v1/admin/portfolio", json={"initial_cash_usd": value}).status_code == 422
    for endpoint in ["/v1/portfolio", "/v1/health", "/v1/runtime", "/v1/events", "/v1/orders", "/v1/verify", "/v1/analytics/throughput"]:
        assert c.get(endpoint, params={"portfolio_id": "a&portfolio_id=main"}).status_code == 422
        assert c.get(endpoint + "?portfolio_id=a&portfolio_id=main").status_code == 422
    assert c.post("/v1/ask", json={"question": "why?", "portfolio_id": "a&portfolio_id=main"}).status_code == 422
    for question in ["", "   ", "x" * 501]:
        assert c.post("/v1/ask", json={"question": question}).status_code == 422
    assert stack.sql("SELECT count(*) FROM commands")[0][0] == 0
    assert c.get("/v1/verify").json()["status"] == "not_opened"
    assert c.post("/v1/admin/allocations", params={"wait": False}, json={"agent_id": "a", "capital_usd": "0"}).status_code == 202


def test_additive_migration_and_same_order_id_are_portfolio_isolated(stack):
    with psycopg.connect(stack.url, autocommit=True) as conn:
        conn.execute("ALTER TABLE orders DROP CONSTRAINT orders_pkey")
        conn.execute("ALTER TABLE orders ADD PRIMARY KEY (order_id)")
        one = Worker(conn, "one")
        def send(w, command, key):
            enqueue(stack, command, w.pid + ":" + key, w.pid)
            w.process_batch()
        def setup(w):
            send(w, OpenPortfolio(initial_cash=10_000_000_000), "open")
            send(w, SetAllocation(agent_id="a", capital=5_000_000_000), "alloc")
            send(w, ApplyBar(bar=Bar(symbol="SPY", ts=T0, open=100_000_000, high=100_000_000,
                                     low=100_000_000, close=100_000_000, volume=100000)), "bar0")
            send(w, SubmitOrder(order=OrderRequest(client_order_id="same", agent_id="a", symbol="SPY", side="BUY",
                                                   qty=10, signal_bar_ts=T0)), "same")
        setup(one)
        digest, events, orders = one.state.digest(), stack.sql("SELECT event FROM ledger_events ORDER BY seq"), stack.sql("SELECT * FROM orders")
        apply_schema(conn)
        assert stack.sql("SELECT event FROM ledger_events ORDER BY seq") == events
        assert stack.sql("SELECT * FROM orders") == orders
        assert Worker(conn, "one").recover()["digest"] == digest
        two = Worker(conn, "two")
        setup(two)
        send(two, CancelOrder(client_order_id="same", agent_id="a"), "cancel")
        from datetime import timedelta
        send(one, ApplyBar(bar=Bar(symbol="SPY", ts=T0 + timedelta(hours=1), open=100_000_000, high=100_000_000,
                                   low=100_000_000, close=100_000_000, volume=100000)), "bar1")
        assert stack.sql("SELECT portfolio_id, filled_qty, status FROM orders ORDER BY portfolio_id") == [
            ("one", 10, "filled"), ("two", 0, "cancelled")]
        assert Worker(conn, "one").recover()["digest"] == one.state.digest()
        assert Worker(conn, "two").recover()["digest"] == two.state.digest()
    c = client(stack)
    assert c.get("/v1/verify", params={"portfolio_id": "one"}).json()["match"]
    assert c.get("/v1/verify", params={"portfolio_id": "two"}).json()["match"]
    history = c.get("/v1/analytics/history", params={"portfolio_id": "one"}).json()
    point = history["points"][-1]
    assert point["equity"] == one.state.equity()
    assert point["exposure"] == one.state.market_value()
    assert point["agents"]["a"]["pnl"] == one.state.realized_pnl + one.state.market_value() - one.state.position("a", "SPY").basis - one.state.fees_paid
    assert history["benchmark"]["status"] == "unavailable"


def test_history_pages_find_old_orders_and_replay_without_mutation(stack):
    with psycopg.connect(stack.url, autocommit=True) as conn:
        worker = Worker(conn, "history")
        enqueue(stack, OpenPortfolio(initial_cash=10_000_000_000), "history-open", "history")
        worker.process_batch()
        for i in range(350):
            enqueue(stack, SubmitOrder(order=OrderRequest(client_order_id=f"old-{i:03d}", agent_id="a", symbol="SPY",
                                                         side="BUY", qty=1, signal_bar_ts=T0)), f"history-{i}", "history")
        while worker.process_batch():
            pass
        digest = worker.state.digest()
    c = client(stack)
    first = c.get("/v1/history/orders", params={"portfolio_id": "history", "limit": 20}).json()
    assert first["items"][0]["order_id"] == "old-349"
    second = c.get("/v1/history/orders", params={"portfolio_id": "history", "limit": 20,
                                               "cursor": first["next_cursor"], "snapshot_seq": first["snapshot_seq"]}).json()
    assert not {r["order_id"] for r in first["items"]} & {r["order_id"] for r in second["items"]}
    found = c.get("/v1/history/orders", params={"portfolio_id": "history", "order_id": "old-000"}).json()
    assert len(found["items"]) == 1
    detail = c.get("/v1/orders/old-000", params={"portfolio_id": "history"}).json()
    assert detail["events"][0]["type"] == "OrderRejected" and detail["commands"][0]["kind"] == "submit_order"
    replayed = c.get("/v1/replay/2", params={"portfolio_id": "history"}).json()
    assert replayed["through_seq"] == 2 and replayed["before"]["decisions"] == 0 and replayed["after"]["decisions"] == 1
    assert c.get("/v1/portfolio", params={"portfolio_id": "history"}).json()["digest"] == digest
    answer = c.post("/v1/ask", json={"portfolio_id": "history", "question": "Why was the last order rejected?"}).json()
    assert "old-349" in answer["answer"] and "sequence 351" in answer["answer"]
    assert answer["mode"] == "deterministic ledger"


def test_jobs_allowlist_retries_and_malformed_manifest_fail_usefully(stack):
    c = client(stack)
    datasets = c.get("/v1/datasets").json()
    fixture = next(d for d in datasets if d["dataset_id"] == "synthetic-1h-seed42")
    assert fixture["execution_usable"] and fixture["synthetic"]
    body = {"request_id": "research-retry", "kind": "research", "dataset_id": fixture["dataset_id"]}
    first = c.post("/v1/jobs", json=body)
    assert first.status_code == 202
    assert c.post("/v1/jobs", json=body).json()["job_id"] == first.json()["job_id"]
    assert c.post("/v1/jobs", json={**body, "steps": 2}).status_code == 409
    assert c.post("/v1/jobs", json={**body, "request_id": "bad-path", "dataset_id": "../.env"}).status_code == 422
    assert c.post("/v1/jobs", json={**body, "request_id": "unknown", "dataset_id": "unknown"}).status_code == 422
    malformed = {"run_id": "bad-run", "dataset": {"content_sha256": fixture["content_sha256"]}}
    assert c.post("/v1/tournaments", json={"run_id": "bad-run", "manifest": malformed,
                                          "manifest_hash": sha256_hex(canonical(malformed))}).status_code == 200
    assert c.post("/v1/jobs", json={**body, "request_id": "bad-session", "kind": "session", "run_id": "bad-run",
                                   "portfolio_id": "fresh"}).status_code == 422
    assert not stack.sql("SELECT 1 FROM managed_workers WHERE portfolio_id = 'fresh'")
    assert c.post(f"/v1/jobs/{first.json()['job_id']}/stop").json()["status"] == "stopped"
    assert c.post("/v1/runtime", json={"action": "crash"}).status_code == 409


def test_latest_rejection_and_breakdown_exclude_later_reductions(stack):
    with psycopg.connect(stack.url, autocommit=True) as conn:
        worker = Worker(conn, "rejection")
        commands = [OpenPortfolio(initial_cash=10_000_000_000), SetAllocation(agent_id="a", capital=5_000_000_000),
                    ApplyBar(bar=Bar(symbol="SPY", ts=T0, open=100_000_000, high=100_000_000,
                                     low=100_000_000, close=100_000_000, volume=10000)),
                    SubmitOrder(order=OrderRequest(client_order_id="short-rejected", agent_id="a", symbol="SPY", side="SELL", qty=1, signal_bar_ts=T0)),
                    SubmitOrder(order=OrderRequest(client_order_id="later-reduced", agent_id="a", symbol="SPY", side="BUY", qty=10000, signal_bar_ts=T0))]
        for i, command in enumerate(commands):
            enqueue(stack, command, f"rejection-{i}", "rejection")
            worker.process_batch()
    c = client(stack)
    result = c.get("/v1/analytics/rejections", params={"portfolio_id": "rejection"}).json()
    assert sum(r["n"] for r in result) == 1
    assert "R5" in result[0]["rule"]
    answer = c.post("/v1/ask", json={"portfolio_id": "rejection", "question": "Why was the latest order rejected?"}).json()
    assert "short-rejected" in answer["answer"] and "later-reduced" not in answer["answer"]
