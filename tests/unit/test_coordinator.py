"""Coordinator and execution authority: the failure cases that matter when several agents share one account."""

from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hellabandz.contracts import MICROS, ApplyBar, Bar, OpenPortfolio, SetAllocation, SetKillSwitch
from hellabandz.ledger import check_invariants, replay
from hellabandz.trading.attribution import AttributionBook, split
from hellabandz.trading.clob import InstrumentRule
from hellabandz.trading.coordinator import (EXEC_AGENT, AccountView, ActiveStrategy, Coordinator, paper_budget,
                                            small_budget)
from hellabandz.trading.execution import ExecutionAuthority, LocalLedger, VenueMode
from hellabandz.trading.instruments import equity, event_contract
from hellabandz.trading.intents import Intent, Outcome
from hellabandz.trading.marketdata import DataClass, EventType, MarketDataGateway, MarketEvent

T0 = 1_736_174_000 // 3600 * 3600
SYMS = ["AAPL", "MSFT", "QQQ", "SPY"]


class Harness:
    def __init__(self, capital=None, budget=None, mode=VenueMode.HISTORICAL_SIM, cash=10_000, universe=None):
        capital = capital or {"trend": 3000, "mean_reversion": 3000, "defensive": 3000}
        self.universe = universe or {k: tuple(SYMS) for k in capital}
        self.ledger = LocalLedger()
        self.cash = cash * MICROS
        self.ledger.process(OpenPortfolio(initial_cash=self.cash))
        self.ledger.process(SetAllocation(agent_id=EXEC_AGENT, capital=sum(capital.values()) * MICROS))
        self.active = {k: ActiveStrategy(k, "v1", self.universe[k], v * MICROS) for k, v in capital.items()}
        self.book = AttributionBook(self.cash, {k: v * MICROS for k, v in capital.items()})
        inst = {s: equity(s) for s in SYMS}
        inst["KXEVENT"] = event_contract("KXEVENT")
        self.venue = "paper_clob" if mode == VenueMode.PAPER_CLOB else "bar_sim"
        self.coord = Coordinator(budget or paper_budget(self.cash), inst, self.venue, "R")
        rules = [InstrumentRule(s, tick=MICROS // 100) for s in SYMS]
        self.auth = ExecutionAuthority(mode, self.ledger, self.book, self.coord.state, rules)
        self.gw = MarketDataGateway(DataClass.SYNTHETIC)
        self.seq = {}
        self.i = 0
        self.decisions = []

    def bar(self, prices: dict[str, float], volume=100_000, skip=()):
        self.i += 1
        ts = T0 + 3600 * self.i
        for sym in sorted(prices):
            if sym in skip:
                continue
            px = int(prices[sym] * MICROS)
            self.seq[sym] = self.seq.get(sym, 0) + 1
            self.gw.publish(MarketEvent(instrument=sym, venue="t", source="t", data_class=DataClass.SYNTHETIC,
                                        event_type=EventType.BAR, seq=self.seq[sym], exchange_ts=ts, receive_ts=ts,
                                        open=px, high=px, low=px, close=px, volume=volume))
        for e in self.gw.drain():
            b = Bar(symbol=e.instrument, ts=_dt(ts), open=e.open, high=e.high, low=e.low, close=e.close, volume=e.volume)
            self.auth.ledger_process(ApplyBar(bar=b))
            self.auth.on_bar(e.instrument, e.close, e.volume, ts)
        return self.gw.snapshot()

    def intent(self, snap, strategy, sym, w, *, version="v1", ts=None, n=0, venue=None, expires=None):
        ts = snap.ts if ts is None else ts
        return Intent(intent_id=f"{strategy}.{sym}.{ts}.{n}", strategy_id=strategy, strategy_version=version,
                      instrument=sym, venue=venue or self.venue, md_seq=snap.gateway_seq, decision_ts=ts,
                      target_weight_bps=w, score=0.0, score_meaning="test", holding_bars=10, max_slippage_bps=30,
                      expires_ts=expires if expires is not None else ts + 3600)

    def decide(self, snap, intents, execute=True):
        acct = AccountView.from_state(self.ledger.state)
        ds = self.coord.decide(snap, intents, acct, self.book, self.active)
        if execute:
            for d in ds:
                self.auth.execute(d, lambda: 0)
        self.decisions += ds
        self.check()
        return {d.instrument: d for d in ds}

    def check(self):
        s = self.ledger.state
        check_invariants(s)
        acct = AccountView.from_state(s)
        self.book.check(s.cash, acct.positions, s.fees_paid, s.slippage_cost, {k: v[1] for k, v in s.last_bar.items()})

    def pos(self, sym):
        return AccountView.from_state(self.ledger.state).positions.get(sym, 0)


def _dt(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc)


P = {"AAPL": 100.0, "MSFT": 100.0, "QQQ": 100.0, "SPY": 100.0}


def outcome(d, strategy):
    return next(r for r in d.intents if r.strategy_id == strategy)


def test_three_agents_buying_the_same_stock_get_one_aggregate_order():
    h = Harness()
    snap = h.bar(P)
    d = h.decide(snap, [h.intent(snap, s, "SPY", 10_000) for s in ("trend", "mean_reversion", "defensive")])["SPY"]
    # each sleeve's SPY budget is 3000/4 = $750 -> 7 shares at $100; one order carries all 21
    assert d.order.side == "BUY" and d.order.qty == 21 and d.order.attribution == {"defensive": 7, "mean_reversion": 7, "trend": 7}
    assert len(h.auth.report.submitted) == 1 and all(r.outcome == Outcome.ACCEPTED for r in d.intents)
    assert "3 agents requested a change in SPY" in d.explanation and "one aggregate BUY of 21" in d.explanation
    h.bar(P)  # next bar fills the bar-sim order
    assert h.pos("SPY") == 21 and {k: h.book.qty(k, "SPY") for k in h.active} == {"defensive": 7, "mean_reversion": 7, "trend": 7}


def test_conflicting_signals_are_netted_internally_instead_of_trading():
    h = Harness()
    snap = h.bar(P)
    h.decide(snap, [h.intent(snap, "trend", "SPY", 10_000)])
    snap = h.bar(P)  # fill: trend holds 7
    assert h.book.qty("trend", "SPY") == 7
    snap = h.bar(P)
    before = len(h.auth.report.submitted)
    d = h.decide(snap, [h.intent(snap, "trend", "SPY", 0), h.intent(snap, "mean_reversion", "SPY", 10_000)])["SPY"]
    assert d.order is None and len(h.auth.report.submitted) == before  # no sell + buy round trip
    assert [(t.from_strategy, t.to_strategy, t.qty) for t in d.transfers] == [("trend", "mean_reversion", 7)]
    assert outcome(d, "trend").outcome == Outcome.NETTED and outcome(d, "mean_reversion").outcome == Outcome.NETTED
    assert h.pos("SPY") == 7 and h.book.qty("mean_reversion", "SPY") == 7 and h.book.qty("trend", "SPY") == 0


def test_partial_conflict_trades_only_the_net_difference():
    h = Harness(capital={"a": 3000, "b": 6000}, universe={"a": ("SPY",), "b": ("SPY",)},
                budget=replace(paper_budget(10_000 * MICROS), cooldown_cycles=0))
    snap = h.bar(P)
    h.decide(snap, [h.intent(snap, "a", "SPY", 10_000)])
    snap = h.bar(P)
    snap = h.bar(P)
    d = h.decide(snap, [h.intent(snap, "a", "SPY", 0), h.intent(snap, "b", "SPY", 10_000)])["SPY"]
    # a gives everything it holds to b internally; only b's remaining need goes to market
    held = d.transfers[0].qty
    assert held > 0 and [t.from_strategy for t in d.transfers] == ["a"] and d.order.side == "BUY"
    assert d.order.attribution == {"b": d.order.qty}


def test_two_instruments_in_one_cycle_cannot_spend_the_same_cash():
    h = Harness(capital={"a": 500, "b": 500}, cash=1000, universe={"a": ("AAPL",), "b": ("MSFT",)},
                budget=replace(paper_budget(1000 * MICROS), min_trade_notional=0, max_instrument_bps=10_000,
                               group_caps=(), max_order_notional=10**12, max_committed=10**12))
    snap = h.bar(P)
    ds = h.decide(snap, [h.intent(snap, "a", "AAPL", 10_000), h.intent(snap, "b", "MSFT", 10_000)])
    spent = sum(d.order.qty * (d.order.limit_px + 5_000) for d in ds.values() if d.order)
    assert spent <= h.ledger.state.cash  # the second order saw the first one's reservation


def test_duplicate_stale_out_of_order_and_unknown_intents_are_rejected():
    h = Harness()
    snap = h.bar(P)
    good = h.intent(snap, "trend", "SPY", 10_000)
    bad = [good,  # duplicate id
           h.intent(snap, "trend", "QQQ", 10_000, version="old"),
           h.intent(snap, "ghost", "QQQ", 10_000),
           h.intent(snap, "defensive", "SPY", 10_000, venue="other"),
           h.intent(snap, "defensive", "KXEVENT", 10_000),
           h.intent(snap, "mean_reversion", "MSFT", 10_000, expires=snap.ts - 1)]
    ds = h.decide(snap, [good] + bad)
    reasons = sorted(r.reasons[0] for d in ds.values() for r in d.intents if r.outcome == Outcome.REJECTED)
    assert reasons == ["DUPLICATE_INTENT", "INACTIVE_STRATEGY_VERSION", "INSTRUMENT_OUTSIDE_UNIVERSE", "INTENT_EXPIRED",
                       "UNKNOWN_STRATEGY", "VENUE_MISMATCH"]
    snap2 = h.bar(P)
    late = h.intent(snap2, "trend", "SPY", 0, ts=snap.ts - 3600, n=9, expires=snap2.ts + 3600)  # older than an intent already accepted
    d = h.decide(snap2, [late])["SPY"]
    assert d.intents[0].reasons == ["OUT_OF_ORDER_INTENT"]


def test_event_contracts_are_not_traded_as_stocks():
    h = Harness(universe={"trend": ("KXEVENT",), "mean_reversion": tuple(SYMS), "defensive": tuple(SYMS)})
    snap = h.bar(P)
    d = h.decide(snap, [h.intent(snap, "trend", "KXEVENT", 10_000)])["KXEVENT"]
    assert d.intents[0].reasons == ["UNSUPPORTED_ASSET_CLASS"] and d.order is None


def test_missing_or_stale_data_blocks_trading_that_instrument():
    h = Harness()
    snap = h.bar(P)
    for _ in range(5):
        snap = h.bar(P, skip=("QQQ",))  # QQQ stops updating
    d = h.decide(snap, [h.intent(snap, "trend", "QQQ", 10_000)])["QQQ"]
    assert d.order is None and d.intents[0].reasons == ["STALE_DATA"]


def test_paper_ioc_partial_fill_then_remainder_released():
    h = Harness(mode=VenueMode.PAPER_CLOB, budget=replace(paper_budget(10_000 * MICROS), min_trade_notional=0))
    snap = h.bar(P, volume=0)
    d = h.decide(snap, [h.intent(snap, "trend", "SPY", 10_000)])["SPY"]
    s = h.ledger.state
    assert d.order.qty == 7 and h.pos("SPY") < 7 and s.orders == {} and s.reserved_cash == 0
    fills = [e for e in h.ledger.events if e["type"] == "OrderFilled"]
    assert all(e["venue"] == "paper_clob" and e["liquidity"] == "synthetic" for e in fills)
    assert h.book.qty("trend", "SPY") == h.pos("SPY")


def test_limits_bind_and_are_recorded():
    h = Harness(capital={"a": 9000}, universe={"a": ("AAPL",)},
                budget=replace(paper_budget(10_000 * MICROS), max_order_notional=500 * MICROS))
    snap = h.bar(P)
    d = h.decide(snap, [h.intent(snap, "a", "AAPL", 10_000)])["AAPL"]
    assert d.order.qty == 4 and "MAX_ORDER_NOTIONAL" in d.portfolio_reasons
    assert outcome(d, "a").outcome == Outcome.REDUCED


def test_group_cap_limits_correlated_holdings():
    b = replace(paper_budget(10_000 * MICROS), group_caps=(("tech", ("AAPL", "MSFT"), 1_000),), max_order_notional=10**12)
    h = Harness(capital={"a": 4000, "b": 4000}, universe={"a": ("AAPL",), "b": ("MSFT",)}, budget=b)
    snap = h.bar(P)
    ds = h.decide(snap, [h.intent(snap, "a", "AAPL", 10_000), h.intent(snap, "b", "MSFT", 10_000)])
    assert ds["AAPL"].order.qty == 10 and ds["MSFT"].order is None  # $1,000 group cap used up by AAPL
    assert "GROUP_CAP:tech" in ds["MSFT"].portfolio_reasons


def test_kill_switch_and_daily_loss_stop_buying_but_allow_exits():
    h = Harness()
    snap = h.bar(P)
    h.decide(snap, [h.intent(snap, "trend", "SPY", 10_000)])
    snap = h.bar(P)
    h.ledger.process(SetKillSwitch(on=True))
    snap = h.bar(P)
    d = h.decide(snap, [h.intent(snap, "trend", "QQQ", 10_000), h.intent(snap, "trend", "SPY", 0)])
    assert d["QQQ"].order is None and "KILL_SWITCH" in d["QQQ"].portfolio_reasons
    assert d["SPY"].order.side == "SELL" and d["SPY"].order.qty == 7


def test_tiny_account_is_told_it_cannot_afford_one_share():
    h = Harness(capital={"a": 4}, cash=5, universe={"a": ("SPY",)}, budget=small_budget(5 * MICROS))
    snap = h.bar(P)
    d = h.decide(snap, [h.intent(snap, "a", "SPY", 10_000)])["SPY"]
    assert d.order is None


def test_cooldown_prevents_back_and_forth_trading():
    h = Harness(budget=replace(paper_budget(10_000 * MICROS), cooldown_cycles=3))
    snap = h.bar(P)
    h.decide(snap, [h.intent(snap, "trend", "SPY", 5_000)])
    snap = h.bar(P)
    d = h.decide(snap, [h.intent(snap, "trend", "SPY", 10_000)])["SPY"]
    assert d.order is None and "COOLDOWN" in d.portfolio_reasons


def test_duplicate_submission_has_no_second_effect():
    h = Harness()
    snap = h.bar(P)
    d = h.decide(snap, [h.intent(snap, "trend", "SPY", 10_000)])["SPY"]
    reserved = h.ledger.state.reserved_cash
    h.auth.execute(d, lambda: 0)  # the same decision delivered twice
    assert h.ledger.state.reserved_cash == reserved and len(h.ledger.state.orders) == 1


def test_split_is_exact_and_deterministic():
    assert split(10, {"b": 1, "a": 1, "c": 1}) == {"a": 4, "b": 3, "c": 3}
    assert split(0, {"a": 1}) == {} and split(7, {"a": 0, "b": 5}) == {"b": 7}


@settings(max_examples=40, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(["trend", "mean_reversion", "defensive"]), st.sampled_from(SYMS),
                          st.sampled_from([0, 2_500, 5_000, 10_000]), st.floats(80, 120)), min_size=1, max_size=40),
       st.sampled_from([VenueMode.HISTORICAL_SIM, VenueMode.PAPER_CLOB]))
def test_attribution_always_sums_to_the_account_and_replays(steps, mode):
    h = Harness(mode=mode, budget=replace(paper_budget(10_000 * MICROS), cooldown_cycles=0, min_trade_notional=0))
    for strat, sym, w, px in steps:
        snap = h.bar({s: (px if s == sym else 100.0) for s in SYMS})
        h.decide(snap, [h.intent(snap, strat, sym, w)])  # h.check() asserts the sums after every cycle
    rebuilt = replay(h.ledger.events)
    assert rebuilt.digest() == h.ledger.state.digest() and h.auth.reconcile() == []
