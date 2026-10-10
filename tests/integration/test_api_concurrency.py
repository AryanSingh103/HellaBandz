"""Real processes, real Postgres: idempotency and cash safety under concurrency (I-01, I-02, I-04)."""

import asyncio

import httpx
import pytest

from tests.conftest import wait_until

T0 = "2026-01-05T14:30:00Z"
T1 = "2026-01-05T15:30:00Z"


def _bar(ts, px, sym="SPY", vol=10_000_000):
    m = int(px * 1_000_000)
    return {"symbol": sym, "ts": ts, "open": m, "high": m, "low": m, "close": m, "volume": vol}


def _setup(st, agents: dict[str, str]):
    c = httpx.Client(base_url=st.base, timeout=30)
    assert c.post("/v1/admin/portfolio", json={"initial_cash_usd": "10000"}).json()["status"] == "ok"
    for a, cap in agents.items():
        assert c.post("/v1/admin/allocations", json={"agent_id": a, "capital_usd": cap},
                      headers={"Idempotency-Key": a}).json()["status"] == "ok"
    assert c.post("/v1/bars", json={"bars": [_bar(T0, 100)]}).json()["processed"]
    return c


@pytest.mark.db
def test_100_concurrent_retries_of_one_order_have_one_effect(stack):
    stack.start_worker()
    stack.wait_worker_ready()
    stack.start_api()
    c = _setup(stack, {"a": "5000"})
    order = {"client_order_id": "retry-me", "agent_id": "a", "symbol": "SPY", "side": "BUY", "qty": 10,
             "signal_bar_ts": T0}

    async def blast():
        async with httpx.AsyncClient(base_url=stack.base, timeout=30) as ac:
            return await asyncio.gather(*[ac.post("/v1/orders", json=order) for _ in range(100)])

    rs = asyncio.run(blast())
    assert all(r.status_code == 200 for r in rs)
    bodies = [r.json() for r in rs]
    assert {b["approved_qty"] for b in bodies} == {10}
    assert sum(not b["duplicate_request"] for b in bodies) == 1
    assert stack.sql("SELECT count(*) FROM commands WHERE command_id = 'order:retry-me'")[0][0] == 1
    assert stack.sql("SELECT count(*) FROM ledger_events WHERE type = 'OrderAccepted'")[0][0] == 1
    c.post("/v1/bars", json={"bars": [_bar(T1, 100)]})
    assert stack.sql("SELECT sum(qty) FROM fills")[0][0] == 10
    v = c.get("/v1/verify").json()
    assert v["match"] and v["chain_ok"]


@pytest.mark.db
def test_concurrent_oversized_orders_cannot_overdraw_shared_cash(stack):
    stack.start_worker(QS_BATCH_SIZE="8")
    stack.wait_worker_ready()
    stack.start_api()
    agents = {f"ag{i}": "1800" for i in range(5)}
    c = _setup(stack, agents)

    async def blast():
        async with httpx.AsyncClient(base_url=stack.base, timeout=60) as ac:
            reqs = [ac.post("/v1/orders", json={"client_order_id": f"big-{a}-{k}", "agent_id": a, "symbol": "SPY",
                                                "side": "BUY", "qty": 500, "signal_bar_ts": T0})
                    for a in agents for k in range(10)]
            return await asyncio.gather(*reqs)

    rs = [r.json() for r in asyncio.run(blast())]
    assert len(rs) == 50
    st = c.get("/v1/portfolio").json()["state"]
    assert 0 <= st["reserved_cash"] <= st["cash"]
    approved_value = sum(r["approved_qty"] for r in rs) * 100
    assert approved_value <= 10_000 * 0.9 + 1  # gross exposure cap
    assert any(r["status"] == "rejected" for r in rs)
    c.post("/v1/bars", json={"bars": [_bar(T1, 140)]})  # +40% gap: fills capped by reservations
    st = c.get("/v1/portfolio").json()["state"]
    assert st["cash"] >= 0 and st["reserved_cash"] <= st["cash"]
    assert c.get("/v1/verify").json()["match"]


@pytest.mark.db
def test_malformed_and_bypass_attempts(stack):
    stack.start_worker()
    stack.wait_worker_ready()
    stack.start_api()
    c = _setup(stack, {"a": "5000"})
    bad = [
        {"client_order_id": "x1", "agent_id": "a", "symbol": "SPY", "side": "HOLD", "qty": 1, "signal_bar_ts": T0},
        {"client_order_id": "x2", "agent_id": "a", "symbol": "SPY", "side": "BUY", "qty": 1.5, "signal_bar_ts": T0},
        {"client_order_id": "x 3", "agent_id": "a", "symbol": "SPY", "side": "BUY", "qty": 1, "signal_bar_ts": T0},
        {"client_order_id": "x4", "agent_id": "a", "symbol": "SPY", "side": "BUY", "qty": 1,
         "signal_bar_ts": "2026-01-05T14:30:00"},  # naive timestamp
    ]
    for b in bad:
        assert c.post("/v1/orders", json=b).status_code == 422
    r = c.post("/v1/orders", json={"client_order_id": "x5", "agent_id": "a", "symbol": "SPY", "side": "BUY",
                                   "qty": 0, "signal_bar_ts": T0}).json()
    assert r["status"] == "rejected" and r["rules"] == ["R6_INVALID_QTY"]
    r = c.post("/v1/orders", json={"client_order_id": "x6", "agent_id": "intruder", "symbol": "SPY", "side": "BUY",
                                   "qty": 1, "signal_bar_ts": T0}).json()
    assert r["rules"] == ["R2_AGENT_ALLOCATION"]
    assert stack.sql("SELECT count(*) FROM ledger_events WHERE type = 'OrderAccepted'")[0][0] == 0


@pytest.mark.db
def test_worker_down_commands_queue_then_process_in_order(stack):
    stack.start_api()
    c = httpx.Client(base_url=stack.base, timeout=30)
    r = c.post("/v1/admin/portfolio", json={"initial_cash_usd": "10000"})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    for i in range(20):
        c.post("/v1/orders?wait=false", json={"client_order_id": f"q{i}", "agent_id": "a", "symbol": "SPY",
                                               "side": "BUY", "qty": 1, "signal_bar_ts": T0})
    stack.start_worker()
    stack.wait_worker_ready()
    wait_until(lambda: stack.sql("SELECT count(*) FROM commands WHERE status = 'pending'")[0][0] == 0)
    # no bar yet -> unknown symbol; the point is ordered, exactly-once processing after an outage
    assert stack.sql("SELECT count(*) FROM ledger_events WHERE type = 'OrderRejected'")[0][0] == 20
    assert c.get("/v1/verify").json()["match"]
