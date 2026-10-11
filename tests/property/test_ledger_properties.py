"""Headline claim, attacked with random command sequences:

No sequence of duplicated, concurrent-interleaved, malformed or replayed agent
commands can create cash or shares out of nothing or double-apply a fill.

Checks after every command: cash/positions non-negative, reservations
consistent, accounting identity (I-02, I-03, I-07). At the end: event-log
replay reproduces the state digest (I-06/I-08) and re-sending any order is a
no-op (I-01).
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hellabandz.contracts import CancelOrder, SetKillSwitch
from hellabandz.ledger import PortfolioState, check_invariants, process, replay
from tests.helpers import CFG, bar, fresh, order
from tests.unit.test_incremental_exposure import assert_totals

AGENTS = ["a", "b", "c"]
SYMS = ["SPY", "NVDA"]

order_cmd = st.builds(
    lambda i, side, qty, agent, sym, lmt, lim_off: ("order", f"o{i}", side, qty, agent, sym, lmt, lim_off),
    st.integers(0, 40), st.sampled_from(["BUY", "BUY", "SELL"]), st.integers(-2, 60),
    st.sampled_from(AGENTS + ["rogue"]), st.sampled_from(SYMS), st.booleans(), st.floats(-0.12, 0.12),
)
bar_cmd = st.builds(lambda gap, move, vol, stress: ("bar", gap, move, vol, stress),
                    st.floats(-0.3, 0.3), st.floats(-0.35, 0.35), st.integers(0, 200_000), st.booleans())
other_cmd = st.one_of(
    st.builds(lambda i, a: ("cancel", f"o{i}", a), st.integers(0, 40), st.sampled_from(AGENTS)),
    st.builds(lambda on: ("kill", on), st.sampled_from([False, False, False, True])),
)
program = st.lists(st.one_of(order_cmd, order_cmd, order_cmd, order_cmd, bar_cmd, bar_cmd, other_cmd),
                   min_size=5, max_size=150)


def materialize(prog):
    price = {"SPY": 100.0, "NVDA": 20.0}
    t = 0
    sent = {}
    cmds = []
    for step in prog:
        if step[0] == "bar":
            _, gap, move, vol, stress = step
            t += 1
            for sym in SYMS:
                o = max(0.5, round(price[sym] * (1 + gap), 2))
                c = max(0.5, o * (1 + move))
                price[sym] = c
                cmds.append(bar(t, o, c, sym=sym, v=vol, stress=stress))
        elif step[0] == "order":
            _, coid, side, qty, agent, sym, lmt, off = step
            if coid in sent and sent[coid][0] != (side, qty, agent, sym):
                cmds.append(sent[coid][1])  # exact retry of earlier payload
                continue
            limit = round(price[sym] * (1 + off), 2) if lmt else None
            c = order(coid, side, qty, agent=agent, sym=sym, sig=t, order_type="LMT" if lmt else "MKT", limit=limit)
            sent.setdefault(coid, ((side, qty, agent, sym), c))
            cmds.append(c)
        elif step[0] == "cancel":
            cmds.append(CancelOrder(client_order_id=step[1], agent_id=step[2]))
        else:
            cmds.append(SetKillSwitch(on=step[1], reason="prop"))
    return cmds


def setup() -> PortfolioState:
    s = fresh(cash=10_000, allocs={"a": 4_000, "b": 3_000, "c": 2_000})
    process(s, bar(0, 20, sym="NVDA"), CFG)
    return s


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(program)
def test_no_sequence_breaks_the_ledger(prog):
    cmds = materialize(prog)
    s = setup()
    log = []
    seen_orders = {}
    for c in cmds:
        evs, resp = process(s, c, CFG)
        log.extend(evs)
        check_invariants(s)
        assert_totals(s)
        if c.kind == "submit_order":
            coid = c.order.client_order_id
            if coid in seen_orders:
                assert evs == [], "retried order must not emit events"
            seen_orders.setdefault(coid, c)
    # every fill belongs to one accepted order and never exceeds its approved qty
    filled: dict[str, int] = {}
    for e in log:
        if e["type"] == "OrderFilled":
            filled[e["order_id"]] = filled.get(e["order_id"], 0) + e["qty"]
    for oid, q in filled.items():
        assert q <= s.decisions[oid]["approved_qty"]
    # retry the entire order stream: no economic effect
    d = s.digest()
    for c in seen_orders.values():
        assert process(s, c, CFG)[0] == []
    assert s.digest() == d
    # deterministic replay from the hash-chained event log
    full = setup_events() + log
    assert replay(full).digest() == d


def setup_events():
    s = PortfolioState()
    out = []
    from hellabandz.contracts import OpenPortfolio, SetAllocation
    from tests.helpers import usd
    for cmd in [OpenPortfolio(initial_cash=usd(10_000)), SetAllocation(agent_id="a", capital=usd(4_000)),
                SetAllocation(agent_id="b", capital=usd(3_000)), SetAllocation(agent_id="c", capital=usd(2_000)),
                bar(0, 100), bar(0, 20, sym="NVDA")]:
        out.extend(process(s, cmd, CFG)[0])
    return out


@settings(max_examples=100, deadline=None)
@given(program)
def test_processing_is_deterministic(prog):
    cmds = materialize(prog)
    a, b = setup(), setup()
    for c in cmds:
        process(a, c, CFG)
        process(b, c, CFG)
    assert a.head_hash == b.head_hash and a.digest() == b.digest()


@settings(max_examples=100, deadline=None)
@given(program)
def test_checkpoint_suffix_matches_full_replay(prog):
    cmds = materialize(prog)
    s = setup()
    full = setup_events()
    cut = len(cmds) // 2
    for cmd in cmds[:cut]:
        full.extend(process(s, cmd, CFG)[0])
    saved = s.snapshot()
    suffix = []
    for cmd in cmds[cut:]:
        suffix.extend(process(s, cmd, CFG)[0])
    resumed = replay(suffix, initial_state=PortfolioState.from_snapshot(saved))
    assert resumed.snapshot() == replay(full + suffix).snapshot() == s.snapshot()
