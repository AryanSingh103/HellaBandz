"""I-08: a worker crash cannot create a duplicate fill or lose a committed event."""

import random
import time

import pytest

from hellabandz.contracts import OpenPortfolio, SetAllocation
from tests.conftest import enqueue, expected_from_command_log, wait_until
from tests.helpers import bar, order, usd


def _seed_commands(st, n_bars: int = 30, seed: int = 1) -> int:
    rng = random.Random(seed)
    enqueue(st, OpenPortfolio(initial_cash=usd(10_000)), "open")
    for a in ("a", "b"):
        enqueue(st, SetAllocation(agent_id=a, capital=usd(4_000)), f"alloc-{a}")
    px = 100.0
    n = 3
    for t in range(n_bars):
        px = max(5.0, px * (1 + rng.uniform(-0.04, 0.04)))
        enqueue(st, bar(t, round(px, 2)), f"bar-{t}")
        n += 1
        for a in ("a", "b"):
            side = rng.choice(["BUY", "BUY", "SELL"])
            enqueue(st, order(f"{a}-{t}", side, rng.randint(1, 15), agent=a, sig=t), f"order:{a}-{t}")
            n += 1
    return n


def _assert_consistent(st):
    exp = expected_from_command_log(st)
    stored = st.sql("SELECT digest, seq FROM portfolio_state WHERE portfolio_id = 'main'")[0]
    assert stored[0] == exp.digest() and stored[1] == exp.seq
    # no order has more fills than approved
    over = st.sql("SELECT order_id FROM orders WHERE filled_qty > approved_qty")
    assert over == []
    # every event seq exactly once, contiguous
    seqs = [r[0] for r in st.sql("SELECT seq FROM ledger_events ORDER BY seq")]
    assert seqs == list(range(1, len(seqs) + 1))


@pytest.mark.db
@pytest.mark.parametrize("crash_at", ["before_commit", "after_commit"])
def test_crash_mid_stream_then_restart(stack, crash_at):
    total = _seed_commands(stack)
    stack.start_worker(QS_CRASH_AT=crash_at, QS_CRASH_AFTER_BATCHES="1", QS_BATCH_SIZE="16",
                       QS_CHECKPOINT_INTERVAL="1")
    stack.worker.wait(timeout=30)
    assert stack.worker.returncode == 137
    done = stack.sql("SELECT count(*) FROM commands WHERE status = 'done'")[0][0]
    events = stack.sql("SELECT count(*) FROM ledger_events")[0][0]
    if crash_at == "before_commit":
        assert done == 0 and events == 0  # nothing half-written
        assert stack.sql("SELECT count(*) FROM ledger_checkpoints")[0][0] == 0
    else:
        assert done == 16 and events > 0
        assert stack.sql("SELECT seq FROM ledger_checkpoints")[0][0] == events
    stack.start_worker()
    stack.wait_worker_ready()
    wait_until(lambda: stack.sql("SELECT count(*) FROM commands WHERE status = 'done'")[0][0] == total)
    _assert_consistent(stack)


@pytest.mark.db
def test_repeated_sigkill_under_load(stack):
    total = _seed_commands(stack, n_bars=60, seed=7)
    rng = random.Random(3)
    kills = 0
    while stack.sql("SELECT count(*) FROM commands WHERE status = 'pending'")[0][0] > 0 and kills < 15:
        stack.start_worker(QS_BATCH_SIZE="4", QS_CHECKPOINT_INTERVAL="10")
        stack.wait_worker_ready()
        time.sleep(rng.uniform(0.01, 0.15))
        stack.kill_worker()
        kills += 1
    stack.start_worker()
    stack.wait_worker_ready()
    wait_until(lambda: stack.sql("SELECT count(*) FROM commands WHERE status = 'done'")[0][0] == total)
    assert kills >= 2
    _assert_consistent(stack)


@pytest.mark.db
def test_worker_refuses_to_start_on_tampered_projection(stack):
    _seed_commands(stack, n_bars=3)
    stack.start_worker()
    stack.wait_worker_ready()
    wait_until(lambda: stack.sql("SELECT count(*) FROM commands WHERE status = 'pending'")[0][0] == 0)
    stack.kill_worker()
    stack.sql("UPDATE portfolio_state SET digest = 'tampered'")
    w = stack.start_worker()
    w.wait(timeout=30)
    assert w.returncode != 0
    assert b"refusing to start" in w.stdout.read()


@pytest.mark.db
def test_tampered_event_log_detected(stack):
    _seed_commands(stack, n_bars=3)
    stack.start_worker()
    stack.wait_worker_ready()
    wait_until(lambda: stack.sql("SELECT count(*) FROM commands WHERE status = 'pending'")[0][0] == 0)
    stack.kill_worker()
    stack.sql("UPDATE ledger_events SET event = jsonb_set(event, '{initial_cash}', '999999999999') "
              "WHERE type = 'PortfolioOpened'")
    w = stack.start_worker()
    w.wait(timeout=30)
    assert w.returncode != 0 and b"hash mismatch" in w.stdout.read()


@pytest.mark.db
def test_second_worker_blocks_on_single_writer_lock(stack):
    _seed_commands(stack, n_bars=2)
    stack.start_worker()
    stack.wait_worker_ready()
    first = stack.worker
    second = stack.start_worker()
    time.sleep(1.0)
    assert second.poll() is None
    assert stack.sql("SELECT count(*) FROM telemetry WHERE metric = 'recovery_seconds'")[0][0] == 1
    second.kill()
    stack.worker = first
