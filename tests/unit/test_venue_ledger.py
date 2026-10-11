"""Venue orders and venue fills in the ledger: the paper exchange reports fills, the ledger re-checks them."""

from hellabandz.contracts import (ApplyVenueFill, CancelOrder, OpenPortfolio, OrderRequest, SetAllocation,
                                  SetKillSwitch, SubmitVenueOrder)
from hellabandz.ledger import check_invariants, process, replay
from hellabandz.ledger.state import PortfolioState
from tests.helpers import CFG, bar, fresh, order, ts, usd


def vorder(coid, side="BUY", qty=10, limit=100.0, *, agent="exec", sig=0, order_type="LMT"):
    return SubmitVenueOrder(order=OrderRequest(client_order_id=coid, agent_id=agent, symbol="SPY", side=side, qty=qty,
                                               order_type=order_type, limit_px=None if limit is None else usd(limit),
                                               signal_bar_ts=ts(sig)))


def vfill(trade, oid, qty, px, *, at=0, ref=100.0, liquidity="synthetic"):
    return ApplyVenueFill(order_id=oid, trade_id=trade, qty=qty, px=usd(px), reference_px=usd(ref), ts=ts(at),
                          liquidity=liquidity)


def run(s, *cmds):
    out = None
    events = []
    for c in cmds:
        evs, out = process(s, c, CFG)
        events += evs
    check_invariants(s)
    return out, events


def opened(agent="exec"):
    return fresh(allocs={agent: 9000})


def test_bar_sim_orders_snapshot_without_venue_field():
    s = opened("a")
    run(s, order("o1", agent="a"))
    assert "venue" not in s.snapshot()["orders"]["o1"]
    assert PortfolioState.from_snapshot(s.snapshot()).digest() == s.digest()


def test_venue_order_must_be_a_limit_order():
    s = opened()
    r, _ = run(s, vorder("v1", limit=None, order_type="MKT"))
    assert r["status"] == "rejected" and r["rules"] == ["R6_VENUE_REQUIRES_LIMIT"]


def test_venue_order_is_never_filled_or_expired_by_bars():
    s = opened()
    run(s, vorder("v1", qty=10, limit=101.0))
    for i in range(1, 10):
        run(s, bar(i, 95.0))  # trades far through the limit; a bar-sim order would fill and later expire
    assert s.orders["v1"].remaining == 10 and s.positions == {}


def test_venue_fill_moves_cash_position_fee_and_reservation_exactly():
    s = opened()
    run(s, vorder("v1", qty=10, limit=101.0))
    reserved = s.reserved_cash
    r, evs = run(s, vfill("t1", "v1", 4, 100.5))
    assert r["status"] == "filled" and r["remaining"] == 6
    fill = next(e for e in evs if e["type"] == "OrderFilled")
    assert fill["venue"] == "paper_clob" and fill["liquidity"] == "synthetic"
    assert fill["fee"] == 4 * CFG.fee_per_share and fill["slippage"] == 4 * usd(0.5)
    assert s.position("exec", "SPY").qty == 4 and s.position("exec", "SPY").basis == 4 * usd(100.5)
    assert s.cash == usd(10000) - 4 * usd(100.5) - fill["fee"]
    assert s.reserved_cash == reserved - (4 * usd(100.5) + fill["fee"])
    r, evs = run(s, vfill("t2", "v1", 6, 100.0))
    assert "v1" not in s.orders and [e["type"] for e in evs] == ["OrderFilled", "OrderClosed"]
    assert s.reserved_cash == 0


def test_venue_fill_is_idempotent_and_conflicting_reuse_is_refused():
    s = opened()
    run(s, vorder("v1", qty=10, limit=101.0))
    run(s, vfill("t1", "v1", 4, 100.0))
    before = s.digest()
    r, evs = run(s, vfill("t1", "v1", 4, 100.0))
    assert r["status"] == "duplicate" and evs == [] and s.digest() == before
    r, evs = run(s, vfill("t1", "v1", 5, 100.0))
    assert r["status"] == "conflict" and evs == [] and s.digest() == before


def test_invalid_venue_fills_are_recorded_not_applied():
    s = opened()
    run(s, vorder("v1", qty=10, limit=101.0, sig=0))
    cases = [(vfill("x1", "nope", 1, 100.0), "VENUE_FILL_UNKNOWN_ORDER"),
             (vfill("x2", "v1", 11, 100.0), "VENUE_FILL_QTY"),
             (vfill("x3", "v1", 1, 101.5), "VENUE_FILL_PRICE_OUTSIDE_LIMIT")]
    for cmd, reason in cases:
        before = s.digest()
        r, evs = run(s, cmd)
        assert r == {"status": "rejected", "reason": reason}
        assert [e["type"] for e in evs] == ["VenueFillRejected"] and s.digest() == before
    # the rejection itself is idempotent
    r, _ = run(s, vfill("x2", "v1", 11, 100.0))
    assert r["status"] == "duplicate" and r["rejected"] == "VENUE_FILL_QTY"


def test_bar_sim_order_cannot_receive_venue_fills():
    s = opened("exec")
    run(s, order("b1", agent="exec"))
    r, _ = run(s, vfill("t1", "b1", 1, 100.0))
    assert r["reason"] == "VENUE_FILL_UNKNOWN_ORDER"


def test_fill_before_the_decision_is_refused():
    s = opened()
    process(s, bar(1, 100.0), CFG)
    run(s, vorder("v1", qty=5, limit=101.0, sig=1))
    r, _ = run(s, vfill("t1", "v1", 5, 100.0, at=0))
    assert r["reason"] == "VENUE_FILL_BEFORE_DECISION"


def test_venue_sell_and_realized_pnl():
    s = opened()
    run(s, vorder("v1", qty=10, limit=101.0), vfill("t1", "v1", 10, 100.0))
    run(s, bar(1, 110.0), vorder("v2", side="SELL", qty=10, limit=109.0, sig=1))
    assert s.position("exec", "SPY").reserved == 10
    r, _ = run(s, vfill("t2", "v2", 10, 110.0, at=1, ref=110.0))
    assert r["status"] == "filled"
    assert s.realized_pnl == 10 * usd(10.0) and s.position("exec", "SPY").qty == 0


def test_cancel_and_kill_switch_release_venue_reservations():
    s = opened()
    run(s, vorder("v1", qty=10, limit=101.0), vorder("v2", qty=5, limit=101.0))
    run(s, CancelOrder(client_order_id="v1", agent_id="exec"))
    assert "v1" not in s.orders
    run(s, SetKillSwitch(on=True))
    assert s.orders == {} and s.reserved_cash == 0


def test_replay_reproduces_venue_state_and_digest():
    s = PortfolioState()
    _, evs = run(s, OpenPortfolio(initial_cash=usd(10000)), SetAllocation(agent_id="exec", capital=usd(9000)),
                 bar(0, 100.0), vorder("v1", qty=10, limit=101.0), vfill("t1", "v1", 3, 100.0),
                 vfill("bad", "v1", 99, 100.0), bar(1, 102.0), vorder("v2", side="SELL", qty=2, limit=101.0, sig=1),
                 vfill("t2", "v2", 2, 102.0, at=1, ref=102.0))
    rebuilt = replay(evs)
    assert rebuilt.digest() == s.digest() and rebuilt.head_hash == s.head_hash
    assert PortfolioState.from_snapshot(s.snapshot()).digest() == s.digest()
