"""Cached risk inputs must equal the authoritative orders and cost bases."""

import pytest

from quantswarm.contracts import CancelOrder, OpenPortfolio, SetAllocation, SetKillSwitch
from quantswarm.ledger import LedgerInvariantError, PortfolioState, append, check_invariants, evaluate_order, process, replay
from tests.helpers import CFG, bar, fresh, order, usd


def assert_totals(s):
    agents = set(s.allocations) | set(s.agent_basis) | set(s.agent_reserved_cash) | {"unknown"}
    for agent in agents:
        basis = sum(p.basis for k, p in s.positions.items() if k.split("|", 1)[0] == agent)
        reserved = sum(o.reserved_cash for o in s.orders.values() if o.agent_id == agent)
        assert s.agent_basis.get(agent, 0) == basis
        assert s.agent_reserved_cash.get(agent, 0) == reserved
        assert s.agent_exposure(agent) == basis + reserved
    assert sum(s.agent_reserved_cash.values()) == s.reserved_cash


def test_incremental_totals_match_full_recomputation_after_every_event():
    s, independent = PortfolioState(), PortfolioState()
    commands = [OpenPortfolio(initial_cash=usd(100_000)),
                SetAllocation(agent_id="a", capital=usd(40_000)),
                SetAllocation(agent_id="ab", capital=usd(40_000)), bar(0, 100), bar(0, 20, sym="NVDA"),
                order("buy", qty=20), order("buy-other", qty=15, agent="ab", sym="NVDA"),
                bar(1, 100, v=500), bar(1, 20, sym="NVDA"),  # partial + complete buys
                order("sell", "SELL", 3, sig=1), bar(2, 101, v=200),  # partial sell, rounded basis
                CancelOrder(client_order_id="buy", agent_id="a"),
                CancelOrder(client_order_id="sell", agent_id="a"),
                order("expire", qty=8, sig=2, order_type="LMT", limit=95),
                *[bar(i, 101, v=0) for i in range(3, 8)],
                order("killed", qty=2, sig=7), SetKillSwitch(on=True, reason="test"),
                order("rejected", qty=1, sig=7), order("buy", qty=20)]
    log = []
    for cmd in commands:
        events, _ = process(s, cmd, CFG)
        log.extend(events)
        for event in events:
            append(independent, {k: v for k, v in event.items() if k not in ("seq", "prev_hash", "hash")})
            assert_totals(independent)
        check_invariants(s)
        assert_totals(s)
    assert replay(log).snapshot() == s.snapshot() == independent.snapshot()


def test_risk_check_does_not_scan_open_orders():
    class NoScan(dict):
        def values(self):
            raise AssertionError("risk check scanned open orders")

    s = fresh()
    process(s, order("working"), CFG)
    expected = evaluate_order(s, order("next").order, CFG)
    s.orders = NoScan(s.orders)
    assert evaluate_order(s, order("next").order, CFG) == expected


@pytest.mark.parametrize("cache", ["agent_basis", "agent_reserved_cash"])
def test_invariant_checker_detects_corrupt_incremental_totals(cache):
    s = fresh()
    getattr(s, cache)["a"] = 1
    with pytest.raises(LedgerInvariantError, match="full recomputation"):
        check_invariants(s)
