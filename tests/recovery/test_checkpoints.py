"""Checkpoint recovery preserves the full replay contract and fails closed."""

import copy

import psycopg
import pytest

from quantswarm.contracts import SetKillSwitch
from quantswarm.execution.worker import StartupDigestMismatch, Worker
from quantswarm.ledger import LedgerInvariantError, PortfolioState, process, replay
from tests.conftest import enqueue
from tests.recovery.test_crash_recovery import _seed_commands


def seeded_worker(stack, conn):
    _seed_commands(stack, n_bars=8)
    w = Worker(conn, batch_size=1, checkpoint_interval=10)
    w.recover()
    while w.process_batch():
        pass
    # Ensure a nonempty suffix, even if the workload ended at a checkpoint.
    enqueue(stack, SetKillSwitch(on=True, reason="suffix"), "suffix")
    w.process_batch()
    return w


@pytest.mark.db
def test_checkpoint_recovery_replays_only_suffix_and_preserves_retry_decisions(stack):
    with psycopg.connect(stack.url, autocommit=True) as conn:
        w = seeded_worker(stack, conn)
        cp_seq = w.checkpoint_seq
        expected = w.state.snapshot()
        restarted = Worker(conn)
        info = restarted.recover()
        assert 0 < cp_seq < w.state.seq
        assert info["checkpoint_seq"] == cp_seq
        assert info["events"] == w.state.seq - cp_seq
        assert restarted.state.snapshot() == expected
        events = [r[0] for r in stack.sql("SELECT event FROM ledger_events ORDER BY seq")]
        assert replay(events).snapshot() == expected
        # The snapshot hash includes decisions, although the economic digest does not.
        assert restarted.state.decisions == w.state.decisions
        from quantswarm.execution.db import decode_command
        payload = stack.sql("SELECT payload FROM commands WHERE kind = 'submit_order' LIMIT 1")[0][0]
        assert process(restarted.state, decode_command(payload), restarted.cfg)[0] == []


@pytest.mark.db
@pytest.mark.parametrize("damage", ["snapshot", "decisions", "digest", "version", "anchor", "suffix", "missing_suffix", "projection"])
def test_checkpoint_recovery_refuses_corruption(stack, damage):
    with psycopg.connect(stack.url, autocommit=True) as conn:
        w = seeded_worker(stack, conn)
        if damage == "snapshot":
            stack.sql("UPDATE ledger_checkpoints SET snapshot = jsonb_set(snapshot, '{cash}', '1')")
        elif damage == "decisions":
            stack.sql("UPDATE ledger_checkpoints SET snapshot = jsonb_set(snapshot, '{decisions}', '{}')")
        elif damage == "digest":
            stack.sql("UPDATE ledger_checkpoints SET digest = 'bad'")
        elif damage == "version":
            stack.sql("UPDATE ledger_checkpoints SET version = 999")
        elif damage == "anchor":
            stack.sql("UPDATE ledger_events SET hash = 'bad' WHERE seq = %s", (w.checkpoint_seq,))
        elif damage == "suffix":
            stack.sql("UPDATE ledger_events SET event = jsonb_set(event, '{hash}', '\"bad\"') WHERE seq = %s",
                      (w.state.seq,))
        elif damage == "missing_suffix":
            stack.sql("DELETE FROM ledger_events WHERE seq = %s", (w.state.seq,))
        else:
            stack.sql("UPDATE portfolio_state SET digest = 'bad'")
        with pytest.raises((StartupDigestMismatch, LedgerInvariantError)):
            Worker(conn).recover()


@pytest.mark.db
def test_checkpoint_write_rolls_back_with_batch(stack, monkeypatch):
    with psycopg.connect(stack.url, autocommit=True) as conn:
        w = seeded_worker(stack, conn)
        before = w.state.snapshot()
        cp_before = stack.sql("SELECT seq, snapshot_hash FROM ledger_checkpoints")
        enqueue(stack, SetKillSwitch(on=False, reason="rollback"), "rollback")
        w.checkpoint_interval = 1
        original = w.write_checkpoint
        def fail_after_write():
            original()
            raise RuntimeError("injected checkpoint failure")
        monkeypatch.setattr(w, "write_checkpoint", fail_after_write)
        with pytest.raises(RuntimeError, match="injected"):
            w.process_batch()
        assert w.state.snapshot() == before
        assert stack.sql("SELECT seq, snapshot_hash FROM ledger_checkpoints") == cp_before
        assert stack.sql("SELECT status FROM commands WHERE command_id = 'rollback'")[0][0] == "pending"


@pytest.mark.db
def test_full_audit_detects_tampering_in_trusted_checkpoint_prefix(stack):
    with psycopg.connect(stack.url, autocommit=True) as conn:
        seeded_worker(stack, conn)
        stack.sql("UPDATE ledger_events SET event = jsonb_set(event, '{initial_cash}', '1') WHERE seq = 1")
        # The fast startup deliberately trusts the committed prefix summary.
        assert Worker(conn).recover()["checkpoint_seq"] > 1
        with pytest.raises(LedgerInvariantError, match="hash mismatch"):
            replay([r[0] for r in stack.sql("SELECT event FROM ledger_events ORDER BY seq")])


def test_checkpoint_snapshot_roundtrip_and_suffix_chain_verification():
    from tests.helpers import CFG, bar, order, fresh
    s = fresh()
    process(s, order("partial", qty=10), CFG)
    process(s, bar(1, 100, v=200), CFG)
    snapshot = s.snapshot()
    restored = PortfolioState.from_snapshot(snapshot)
    suffix, _ = process(s, bar(2, 101), CFG)
    assert replay(suffix, initial_state=restored).snapshot() == s.snapshot()
    assert snapshot["orders"]["partial"]["remaining"] == 8  # no shared mutable objects
    damaged = copy.deepcopy(suffix)
    damaged[0]["prev_hash"] = "bad"
    with pytest.raises(LedgerInvariantError):
        replay(damaged, initial_state=PortfolioState.from_snapshot(snapshot))
