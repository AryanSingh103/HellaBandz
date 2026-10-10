"""Risk rules, fill model and idempotency of the deterministic engine."""

from quantswarm.contracts import CancelOrder, SetKillSwitch
from quantswarm.ledger import check_invariants, process
from tests.helpers import CFG, bar, fresh, order, usd


def run(s, cmd):
    evs, resp = process(s, cmd, CFG)
    check_invariants(s)
    return evs, resp


# ---- I-01 idempotency ------------------------------------------------------------


def test_duplicate_order_has_no_second_effect():
    s = fresh()
    evs1, r1 = run(s, order("o1", qty=5))
    for _ in range(100):
        evs, r = run(s, order("o1", qty=5))
        assert evs == [] and r["status"] == "duplicate" and r["approved_qty"] == r1["approved_qty"]
    run(s, bar(1, 100))
    assert s.position("a", "SPY").qty == 5


def test_same_id_different_payload_is_conflict_not_overwrite():
    s = fresh()
    run(s, order("o1", qty=5))
    evs, r = run(s, order("o1", qty=50))
    assert evs == [] and r["status"] == "conflict"
    assert s.orders["o1"].qty == 5


def test_rejected_order_is_also_idempotent():
    s = fresh()
    _, r = run(s, order("bad", qty=0))
    assert r["rules"] == ["R6_INVALID_QTY"]
    evs, r2 = run(s, order("bad", qty=0))
    assert evs == [] and r2["status"] == "duplicate" and r2["outcome"] == "REJECT"


# ---- risk rules ---------------------------------------------------------------------


def test_r1_max_order_value_reduces():
    s = fresh(allocs={"a": 9_000})
    _, r = run(s, order("o1", qty=1_000))
    # 25% of $10k = $2,500 at the $102 reservation price (last close + 2% gap buffer) = 24 shares
    assert r["outcome"] == "REDUCE" and r["approved_qty"] == 24 and r["rules"] == ["R1_MAX_ORDER_VALUE"]


def test_r2_agent_allocation_limits_each_agent():
    s = fresh(allocs={"a": 1_000, "b": 1_000})
    _, r = run(s, order("o1", qty=50, agent="a"))
    assert r["approved_qty"] == 9 and "R2_AGENT_ALLOCATION" in r["rules"]
    _, r = run(s, order("o2", qty=50, agent="a"))
    assert r["status"] == "rejected" and r["rules"] == ["R2_AGENT_ALLOCATION"]
    _, r = run(s, order("o3", qty=5, agent="b"))
    assert r["outcome"] == "APPROVE"


def test_unallocated_agent_cannot_buy():
    s = fresh()
    _, r = run(s, order("o1", agent="stranger"))
    assert r["status"] == "rejected" and r["rules"] == ["R2_AGENT_ALLOCATION"]


def test_r4_concurrent_agents_cannot_overspend_shared_cash():
    s = fresh(cash=1_000, allocs={f"ag{i}": 100 for i in range(10)})
    total_reserved = 0
    for i in range(10):
        for j in range(5):
            run(s, order(f"o{i}.{j}", qty=1, agent=f"ag{i}"))
    total_reserved = s.reserved_cash
    assert total_reserved <= s.cash
    run(s, bar(1, 130))  # gap up 30%: fills limited by reservations, never debt
    assert s.cash >= 0


def test_r5_cannot_sell_more_than_held_or_short():
    s = fresh()
    _, r = run(s, order("s1", "SELL", 5))
    assert r["rules"] == ["R5_NO_POSITION"]
    run(s, order("b1", qty=10))
    run(s, bar(1, 100))
    _, r = run(s, order("s2", "SELL", 25, sig=1))
    assert r["outcome"] == "REDUCE" and r["approved_qty"] == 10
    _, r = run(s, order("s3", "SELL", 1, sig=1))
    assert r["rules"] == ["R5_NO_POSITION"]  # all 10 reserved by s2


def test_sell_is_per_agent_subaccount():
    s = fresh(allocs={"a": 4_000, "b": 4_000})
    run(s, order("b1", qty=10, agent="a"))
    run(s, bar(1, 100))
    _, r = run(s, order("s1", "SELL", 5, agent="b", sig=1))
    assert r["rules"] == ["R5_NO_POSITION"]


def test_r6_invalid_inputs():
    s = fresh()
    assert run(s, order("x1", qty=-3))[1]["rules"] == ["R6_INVALID_QTY"]
    assert run(s, order("x2", sym="ZZZ"))[1]["rules"] == ["R6_UNKNOWN_SYMBOL"]
    assert run(s, order("x3", order_type="LMT"))[1]["rules"] == ["R6_INVALID_LIMIT"]
    assert run(s, order("x4", order_type="LMT", limit=150))[1]["rules"] == ["R6_LIMIT_OUTSIDE_BAND"]
    assert run(s, order("x5", limit=99))[1]["rules"] == ["R6_INVALID_LIMIT"]


def test_r7_stale_and_future_signals_rejected():
    s = fresh()
    for i in range(1, 5):
        run(s, bar(i, 100))
    assert run(s, order("old", sig=1))[1]["rules"] == ["R7_STALE_SIGNAL"]
    assert run(s, order("future", sig=9))[1]["rules"] == ["R7_FUTURE_SIGNAL"]
    assert run(s, order("ok", sig=3))[1]["status"] == "accepted"


def test_r9_kill_switch_blocks_buys_allows_sells_and_cancels_working_buys():
    s = fresh()
    run(s, order("b1", qty=10))
    run(s, bar(1, 100))
    run(s, order("b2", qty=5, sig=1))
    _, r = run(s, SetKillSwitch(on=True, reason="test"))
    assert r["kill_switch"] and "b2" not in s.orders and s.reserved_cash == 0
    assert run(s, order("b3", qty=1, sig=1))[1]["rules"] == ["R9_KILL_SWITCH"]
    assert run(s, order("s1", "SELL", 10, sig=1))[1]["status"] == "accepted"


def test_kill_switch_trips_on_drawdown():
    s = fresh(allocs={"a": 9_000})
    for k in range(4):
        run(s, order(f"b{k}", qty=25, sig=k))
        run(s, bar(k + 1, 100))
    run(s, bar(5, 100, 40))  # 60% crash on a ~90% invested book
    assert s.kill_switch and s.kill_reason == "drawdown"


# ---- fill model (I-05) --------------------------------------------------------------


def test_no_fill_on_the_decision_bar_fill_at_next_open_with_slippage():
    s = fresh()
    evs, _ = run(s, order("o1", qty=10))
    assert not [e for e in evs if e["type"] == "OrderFilled"]
    evs, _ = run(s, bar(1, 101, 105))
    f = [e for e in evs if e["type"] == "OrderFilled"][0]
    assert f["px"] == usd(101) * 10_005 // 10_000 + (1 if usd(101) * 10_005 % 10_000 else 0)
    assert f["qty"] == 10 and f["fee"] == 10 * CFG.fee_per_share


def test_limit_buy_only_fills_when_traded_through():
    s = fresh()
    run(s, order("l1", qty=10, order_type="LMT", limit=98))
    evs, _ = run(s, bar(1, 100, 99, lo=98.5))
    assert not [e for e in evs if e["type"] == "OrderFilled"]
    evs, _ = run(s, bar(2, 99, 97, lo=96))
    f = [e for e in evs if e["type"] == "OrderFilled"][0]
    assert f["px"] == usd(98)


def test_volume_cap_partial_fill_then_expiry_releases_cash():
    s = fresh()
    run(s, order("o1", qty=20))
    run(s, bar(1, 100, v=500))  # 1% of 500 = 5 shares per bar
    assert s.orders["o1"].remaining == 15 and s.position("a", "SPY").qty == 5
    for i in range(2, 2 + CFG.expiry_bars):
        run(s, bar(i, 100, v=0))
    assert "o1" not in s.orders and s.reserved_cash == 0


def test_gap_up_partial_fill_never_exceeds_reservation():
    s = fresh(allocs={"a": 9_000})
    run(s, order("o1", qty=20))
    run(s, bar(1, 150))  # +50% gap, reservation sized at +2%
    assert s.position("a", "SPY").qty < 20
    assert s.orders["o1"].reserved_cash >= 0


def test_stress_bar_uses_stress_slippage():
    s = fresh()
    run(s, order("o1", qty=10))
    evs, _ = run(s, bar(1, 100, stress=True))
    f = [e for e in evs if e["type"] == "OrderFilled"][0]
    assert f["px"] == usd(100) * (10_000 + CFG.stress_slippage_bps) // 10_000


def test_cancel_releases_and_only_owner_can_cancel():
    s = fresh(allocs={"a": 4_000, "b": 4_000})
    run(s, order("o1", qty=10))
    assert run(s, CancelOrder(client_order_id="o1", agent_id="b"))[1]["status"] == "not_found"
    assert run(s, CancelOrder(client_order_id="o1", agent_id="a"))[1]["status"] == "cancelled"
    assert s.reserved_cash == 0
    assert run(s, CancelOrder(client_order_id="o1", agent_id="a"))[1]["status"] == "not_found"


# ---- input ordering / validity (I-11, failure behavior) -----------------------------


def test_out_of_order_and_duplicate_bars_rejected_without_effect():
    s = fresh()
    run(s, bar(3, 100))
    before = s.digest()
    for b in (bar(2, 100), bar(3, 100)):
        evs, r = run(s, b)
        assert evs == [] and r["reason"] == "BAR_OUT_OF_ORDER"
    assert s.digest() == before


def test_invalid_ohlc_rejected():
    s = fresh()
    evs, r = run(s, bar(1, 100, 101, h=99))
    assert evs == [] and r["reason"] == "BAR_INVALID_OHLC"


def test_realized_pnl_and_accounting_identity():
    s = fresh()
    run(s, order("b1", qty=10))
    run(s, bar(1, 100))
    run(s, order("s1", "SELL", 10, sig=1))
    run(s, bar(2, 110))
    assert s.position("a", "SPY").qty == 0
    buy_px = usd(100) * 10_005 // 10_000
    sell_px = usd(110) * 9_995 // 10_000
    assert s.realized_pnl == 10 * (sell_px - buy_px)
    assert s.cash == usd(10_000) + s.realized_pnl - s.fees_paid
