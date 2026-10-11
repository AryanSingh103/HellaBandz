"""Internal CLOB: priority, partial fills, IOC, cancel, self-trade prevention, idempotency, replay."""

from hypothesis import given, settings
from hypothesis import strategies as st

from hellabandz.trading.clob import Cancel, InstrumentRule, MatchingEngine, Submit

T = 10_000  # one cent in micros
RULES = [InstrumentRule("SPY", tick=T, lot=1), InstrumentRule("QQQ", tick=T, lot=1)]
_n = iter(range(10**9))


def eng(stp="cancel_newest"):
    return MatchingEngine(RULES, stp=stp)


def sub(e, oid, who, side, px_cents, qty, tif="GTC", sym="SPY"):
    return e.process(Submit(f"c-{oid}", oid, who, sym, side, px_cents * T, qty, tif))


def trades(evs):
    return [(x["maker_order_id"], x["taker_order_id"], x["px"] // T, x["qty"]) for x in evs if x["type"] == "Trade"]


def test_two_buys_never_match_and_nothing_trades_without_a_crossing_seller():
    e = eng()
    sub(e, "b1", "alice", "BUY", 100, 5)
    evs, r = sub(e, "b2", "bob", "BUY", 105, 5)
    assert r["status"] == "resting" and trades(evs) == []
    evs, r = sub(e, "s1", "carol", "SELL", 106, 5)  # does not cross the best bid of 105
    assert r["status"] == "resting" and trades(evs) == []
    e.check()


def test_price_then_time_priority_and_maker_price():
    e = eng()
    sub(e, "a1", "alice", "SELL", 101, 3)
    sub(e, "a2", "bob", "SELL", 100, 3)   # better price, later
    sub(e, "a3", "carol", "SELL", 100, 3)  # same price as a2, later still
    evs, r = sub(e, "t1", "dave", "BUY", 101, 7)
    assert trades(evs) == [("a2", "t1", 100, 3), ("a3", "t1", 100, 3), ("a1", "t1", 101, 1)]
    assert r == {"status": "filled", "filled": 7, "remaining": 0}
    assert e.orders["a1"].remaining == 2
    e.check()


def test_partial_fill_rests_the_remainder_at_its_limit():
    e = eng()
    sub(e, "s1", "alice", "SELL", 100, 4)
    evs, r = sub(e, "b1", "bob", "BUY", 100, 10)
    assert trades(evs) == [("s1", "b1", 100, 4)] and r["status"] == "resting" and r["remaining"] == 6
    assert e.best_bid("SPY") == 100 * T and e.best_ask("SPY") is None


def test_ioc_cancels_what_cannot_fill_now_with_an_explicit_reason():
    e = eng()
    sub(e, "s1", "alice", "SELL", 100, 4)
    evs, r = sub(e, "b1", "bob", "BUY", 100, 10, tif="IOC")
    assert trades(evs) == [("s1", "b1", 100, 4)]
    assert r == {"status": "cancelled", "reason": "INSUFFICIENT_LIQUIDITY", "filled": 4, "remaining": 6}
    assert "b1" not in e.orders
    evs, r = sub(e, "b2", "bob", "BUY", 100, 1, tif="IOC")  # empty book: nothing is invented
    assert trades(evs) == [] and r["reason"] == "INSUFFICIENT_LIQUIDITY" and r["filled"] == 0


def test_cancel_removes_only_the_owners_resting_order():
    e = eng()
    sub(e, "b1", "alice", "BUY", 100, 5)
    assert e.process(Cancel("x1", "b1", "mallory"))[1] == {"status": "rejected", "reason": "NOT_OWNER"}
    evs, r = e.process(Cancel("x2", "b1", "alice"))
    assert r["status"] == "cancelled" and evs[-1]["reason"] == "USER" and e.best_bid("SPY") is None
    assert e.process(Cancel("x3", "b1", "alice"))[1]["reason"] == "NOT_FOUND"


def test_cancel_replace_loses_time_priority():
    e = eng()
    sub(e, "a1", "alice", "SELL", 100, 1)
    sub(e, "b1", "bob", "SELL", 100, 1)
    e.process(Cancel("x1", "a1", "alice"))
    sub(e, "a2", "alice", "SELL", 100, 1)  # replacement goes behind bob
    evs, _ = sub(e, "t1", "carol", "BUY", 100, 1)
    assert trades(evs) == [("b1", "t1", 100, 1)]


def test_self_trade_prevention_cancel_newest_keeps_the_resting_order():
    e = eng("cancel_newest")
    sub(e, "s1", "alice", "SELL", 100, 5)
    evs, r = sub(e, "b1", "alice", "BUY", 100, 5)
    assert trades(evs) == [] and r["reason"] == "SELF_TRADE_PREVENTION" and e.orders["s1"].remaining == 5
    # fills against other participants ahead of the own order still happen
    e2 = eng("cancel_newest")
    sub(e2, "s1", "bob", "SELL", 100, 2)
    sub(e2, "s2", "alice", "SELL", 101, 5)
    evs, r = sub(e2, "b1", "alice", "BUY", 101, 6)
    assert trades(evs) == [("s1", "b1", 100, 2)] and r == {"status": "cancelled", "reason": "SELF_TRADE_PREVENTION",
                                                            "filled": 2, "remaining": 4}


def test_self_trade_prevention_cancel_oldest_removes_resting_and_continues():
    e = eng("cancel_oldest")
    sub(e, "s1", "alice", "SELL", 100, 5)
    sub(e, "s2", "bob", "SELL", 100, 5)
    evs, r = sub(e, "b1", "alice", "BUY", 100, 5)
    assert [x["reason"] for x in evs if x["type"] == "OrderCancelled"] == ["SELF_TRADE_PREVENTION"]
    assert trades(evs) == [("s2", "b1", 100, 5)] and "s1" not in e.orders and r["status"] == "filled"


def test_invalid_orders_are_rejected_with_reasons():
    e = eng()
    assert sub(e, "o1", "a", "BUY", 0, 1)[1]["reason"] == "INVALID_PRICE"
    assert e.process(Submit("c-x", "o2", "a", "SPY", "BUY", 100 * T + 1, 1))[1]["reason"] == "INVALID_PRICE"  # off tick
    assert sub(e, "o3", "a", "BUY", 100, 0)[1]["reason"] == "INVALID_QUANTITY"
    assert sub(e, "o4", "a", "BUY", 100, 1, sym="XYZ")[1]["reason"] == "UNKNOWN_INSTRUMENT"
    assert sub(e, "o5", "a", "BUY", 100, 1, tif="FOK")[1]["reason"] == "INVALID_TIME_IN_FORCE"
    sub(e, "o6", "a", "BUY", 100, 1)
    assert e.process(Submit("c-new", "o6", "a", "SPY", "BUY", 100 * T, 1))[1]["reason"] == "DUPLICATE_ORDER_ID"


def test_commands_are_idempotent_and_reused_ids_conflict():
    e = eng()
    sub(e, "s1", "alice", "SELL", 100, 5)
    cmd = Submit("buy-1", "b1", "bob", "SPY", "BUY", 100 * T, 2)
    evs1, r1 = e.process(cmd)
    evs2, r2 = e.process(cmd)
    assert trades(evs1) == [("s1", "b1", 100, 2)] and evs2 == [] and r2["duplicate"] and e.orders["s1"].remaining == 3
    _, r3 = e.process(Submit("buy-1", "b9", "bob", "SPY", "BUY", 100 * T, 2))
    assert r3["status"] == "conflict"


def test_books_are_independent_per_instrument():
    e = eng()
    sub(e, "s1", "alice", "SELL", 100, 5, sym="QQQ")
    evs, r = sub(e, "b1", "bob", "BUY", 200, 5, sym="SPY")
    assert trades(evs) == [] and r["status"] == "resting"


order_flow = st.lists(st.tuples(
    st.sampled_from(["submit", "submit", "submit", "cancel"]),
    st.sampled_from(["alice", "bob", "carol"]),
    st.sampled_from(["BUY", "SELL"]),
    st.integers(95, 105), st.integers(1, 20), st.sampled_from(["GTC", "IOC"]),
    st.sampled_from(["SPY", "QQQ"]), st.integers(0, 60)), min_size=1, max_size=120)


def run_flow(flow, stp="cancel_newest"):
    e = MatchingEngine(RULES, stp=stp)
    log = []
    submitted: dict[str, int] = {}
    for i, (kind, who, side, px, qty, tif, sym, target) in enumerate(flow):
        if kind == "submit":
            cmd = Submit(f"c{i}", f"o{i}", who, sym, side, px * T, qty, tif)
            submitted[f"o{i}"] = qty
        else:
            cmd = Cancel(f"c{i}", f"o{target}", who)
        evs, _ = e.process(cmd)
        log += evs
        e.check()
    return e, log, submitted


@settings(max_examples=150, deadline=None)
@given(order_flow, st.sampled_from(["cancel_newest", "cancel_oldest"]))
def test_random_order_flow_keeps_invariants_and_replays_identically(flow, stp):
    e, log, submitted = run_flow(flow, stp)
    e2, log2, _ = run_flow(flow, stp)
    assert log == log2 and e.head_hash == e2.head_hash
    traded: dict[str, int] = {}
    cancelled: dict[str, int] = {}
    for x in log:
        if x["type"] == "Trade":
            assert x["maker"] != x["taker"]  # self-trade prevention always holds
            traded[x["maker_order_id"]] = traded.get(x["maker_order_id"], 0) + x["qty"]
            traded[x["taker_order_id"]] = traded.get(x["taker_order_id"], 0) + x["qty"]
        elif x["type"] == "OrderCancelled":
            cancelled[x["order_id"]] = cancelled.get(x["order_id"], 0) + x["remaining"]
    accepted = {x["order_id"] for x in log if x["type"] == "OrderAccepted"}
    for oid in accepted:  # quantity is conserved: submitted = traded + cancelled + still resting
        resting = e.orders[oid].remaining if oid in e.orders else 0
        assert submitted[oid] == traded.get(oid, 0) + cancelled.get(oid, 0) + resting
