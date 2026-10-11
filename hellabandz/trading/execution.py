"""Execution authority: the only component that submits orders, one per portfolio.

Venue modes, and what a "fill" means in each:

- historical_sim: the ledger fills orders from bars with its documented model (next bar open +/-
  slippage, limit only if traded through, at most 1% of bar volume). Fills are labeled bar_sim.
- paper_clob: orders go to the internal CLOB (hellabandz.trading.clob). The other side is a
  simulated liquidity provider (participant "sim_lp") that posts a small quote ladder around each
  bar's close, sized from bar volume, or another simulated participant. That depth is synthetic: it
  is generated from OHLCV bars and is not real exchange depth. Fills are labeled paper_clob with
  liquidity "synthetic" or "internal", and enter the ledger only through ApplyVenueFill, which
  re-checks them.
- shadow: decisions and orders are recorded but nothing is submitted anywhere.
- external_demo / external_production: see hellabandz.trading.external. Not wired to the equity
  strategies. Production submission is disabled.

A paper fill is never recorded or shown as an external venue fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from hellabandz.contracts import (ApplyVenueFill, CancelOrder, OrderRequest, OrderType, Side, SubmitOrder,
                                  SubmitVenueOrder)
from hellabandz.ledger import EngineConfig, PortfolioState, process
from hellabandz.ledger.engine import BPS
from hellabandz.trading.attribution import AttributionBook, split
from hellabandz.trading.clob import Cancel, InstrumentRule, MatchingEngine, Submit
from hellabandz.trading.coordinator import EXEC_AGENT, CoordinatorState
from hellabandz.trading.intents import Decision


class VenueMode(StrEnum):
    HISTORICAL_SIM = "historical_sim"
    PAPER_CLOB = "paper_clob"
    SHADOW = "shadow"
    EXTERNAL_DEMO = "external_demo"
    EXTERNAL_PRODUCTION = "external_production"


LEDGER_VENUE = {VenueMode.HISTORICAL_SIM: "bar_sim", VenueMode.PAPER_CLOB: "paper_clob", VenueMode.SHADOW: "shadow"}
LP = "sim_lp"


class LocalLedger:
    """In-process ledger: the same `ledger.process` the execution worker runs, with its event log."""

    def __init__(self, cfg: EngineConfig = EngineConfig()):
        self.cfg = cfg
        self.state = PortfolioState()
        self.events: list[dict] = []

    def process(self, cmd) -> tuple[list[dict], dict]:
        evs, resp = process(self.state, cmd, self.cfg)
        self.events.extend(evs)
        return evs, resp


@dataclass
class LiquidityConfig:
    levels: int = 3
    half_spread_bps: int = 5
    step_bps: int = 5
    participation_bps: int = 100  # total quoted size per side = 1% of the bar's volume


class SyntheticLiquidityProvider:
    """Posts a symmetric ladder around the last close after each bar. Uses only data available at
    that bar. Every quote is an ordinary resting order of participant sim_lp in the CLOB."""

    def __init__(self, clob: MatchingEngine, cfg: LiquidityConfig = LiquidityConfig()):
        self.clob = clob
        self.cfg = cfg
        self.n = 0

    def requote(self, sym: str, close: int, volume: int, ts: int) -> list[dict]:
        rule = self.clob.rules[sym]
        events: list[dict] = []
        for o in self.clob.open_orders(LP):
            if o.symbol == sym:
                self.n += 1
                events += self.clob.process(Cancel(f"lp-x{self.n}", o.order_id, LP, reason="REQUOTE"))[0]
        size = max(rule.lot, (volume * self.cfg.participation_bps // BPS // self.cfg.levels) // rule.lot * rule.lot)
        for i in range(self.cfg.levels):
            off = self.cfg.half_spread_bps + i * self.cfg.step_bps
            bid = max(rule.tick, (close * (BPS - off) // BPS) // rule.tick * rule.tick)
            ask = -(-(close * (BPS + off)) // BPS // rule.tick) * rule.tick + rule.tick
            for side, px in (("BUY", bid), ("SELL", ask)):
                self.n += 1
                events += self.clob.process(Submit(f"lp-{self.n}", f"lp.{sym}.{ts}.{side[0]}{i}", LP, sym, side, px, size))[0]
        return events


@dataclass
class ExecutionReport:
    submitted: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    ledger_rejections: list[dict] = field(default_factory=list)
    reconciliation: list[dict] = field(default_factory=list)
    timings_ns: dict[str, list[int]] = field(default_factory=lambda: {"submit": [], "match": []})


class ExecutionAuthority:
    def __init__(self, mode: VenueMode, ledger, book: AttributionBook, coord_state: CoordinatorState,
                 rules: list[InstrumentRule], owner: str = EXEC_AGENT, liquidity: LiquidityConfig | None = None):
        if mode in (VenueMode.EXTERNAL_DEMO, VenueMode.EXTERNAL_PRODUCTION):
            raise ValueError("external venues are handled by hellabandz.trading.external, not this authority")
        self.mode = mode
        self.ledger = ledger
        self.book = book
        self.coord_state = coord_state
        self.owner = owner
        self.clob = MatchingEngine(rules) if mode == VenueMode.PAPER_CLOB else None
        self.lp = SyntheticLiquidityProvider(self.clob, liquidity or LiquidityConfig()) if self.clob else None
        self.shadow_orders: list[dict] = []
        self.clob_events: list[dict] = []
        self.marks: dict[str, int] = {}
        self.last_ts = 0
        self.n_cmd = 0
        self.report = ExecutionReport()

    # ------------------------------------------------------------------ ledger plumbing

    def ledger_process(self, cmd) -> tuple[list[dict], dict]:
        evs, resp = self.ledger.process(cmd)
        self.on_ledger_events(evs)
        return evs, resp

    def on_ledger_events(self, evs: list[dict]) -> None:
        """Attribute fills and close plans. Also keeps the CLOB in step with orders the ledger closed."""
        for e in evs:
            if e["type"] == "OrderFilled" and e["agent_id"] == self.owner:
                key = f"{e.get('venue', 'bar_sim')}:{e.get('trade_id', e['seq'])}"
                self.book.on_fill(key, e["order_id"], e["symbol"], e["side"], e["qty"], e["px"], e["fee"], e["slippage"])
            elif e["type"] == "OrderClosed":
                self.book.close_plan(e["order_id"])
                if self.clob is not None and e["reason"] != "filled" and e["order_id"] in self.clob.orders:
                    self._clob(Cancel(self._cid("lx"), e["order_id"], self.owner, reason="LEDGER_CLOSED"))

    def _cid(self, tag: str) -> str:
        self.n_cmd += 1
        return f"{tag}-{self.n_cmd}"

    def _clob(self, cmd) -> tuple[list[dict], dict]:
        evs, resp = self.clob.process(cmd)
        self.clob_events.extend(evs)
        self._route(evs)
        return evs, resp

    def _route(self, evs: list[dict]) -> None:
        """Send every trade involving the owner to the ledger, and release reservations the CLOB cancelled."""
        for e in evs:
            if e["type"] == "Trade" and self.owner in (e["maker"], e["taker"]):
                oid = e["maker_order_id"] if e["maker"] == self.owner else e["taker_order_id"]
                other = e["taker"] if e["maker"] == self.owner else e["maker"]
                self.report.trades.append(e)
                _, resp = self.ledger_process(ApplyVenueFill(
                    order_id=oid, trade_id=e["trade_id"], qty=e["qty"], px=e["px"],
                    reference_px=self.marks.get(e["symbol"], e["px"]), ts=_dt(self.last_ts),
                    liquidity="synthetic" if other == LP else "internal"))
                if resp["status"] not in ("filled", "duplicate"):
                    self.report.reconciliation.append({"break": "venue fill refused by ledger", "trade": e, "ledger": resp})
            elif e["type"] == "OrderCancelled" and e["participant"] == self.owner:
                if e["order_id"] in self.ledger.state.orders:
                    self.ledger_process(CancelOrder(client_order_id=e["order_id"], agent_id=self.owner))

    # ------------------------------------------------------------------ market data

    def on_bar(self, sym: str, close: int, volume: int, ts: int) -> None:
        self.marks[sym] = close
        self.last_ts = max(self.last_ts, ts)
        if self.lp is not None:
            evs = self.lp.requote(sym, close, volume, ts)
            self.clob_events.extend(evs)
            self._route(evs)

    # ------------------------------------------------------------------ decisions

    def execute(self, d: Decision, timer) -> None:
        if d.cancel_order_id:
            if self.clob is not None and d.cancel_order_id in self.clob.orders:
                self._clob(Cancel(self._cid("cx"), d.cancel_order_id, self.owner, reason="REPLACE"))
            if d.cancel_order_id in self.ledger.state.orders:
                self.ledger_process(CancelOrder(client_order_id=d.cancel_order_id, agent_id=self.owner))
        for t in d.transfers:
            self.book.transfer(t.instrument, t.from_strategy, t.to_strategy, t.qty, t.px)
        o = d.order
        if o is None:
            return
        if self.mode == VenueMode.SHADOW:
            self.shadow_orders.append(o.model_dump())
            return
        req = OrderRequest(client_order_id=o.client_order_id, agent_id=self.owner, symbol=o.instrument, side=Side(o.side),
                           qty=o.qty, order_type=OrderType.LMT, limit_px=o.limit_px, signal_bar_ts=_dt(d.ts))
        cmd = SubmitVenueOrder(order=req) if self.mode == VenueMode.PAPER_CLOB else SubmitOrder(order=req)
        self.coord_state.pending.add(o.client_order_id)
        t0 = timer()
        evs, resp = self.ledger.process(cmd)
        self.report.timings_ns["submit"].append(timer() - t0)
        self.coord_state.pending.discard(o.client_order_id)
        approved = resp.get("approved_qty", 0)
        self.report.submitted.append({"order_id": o.client_order_id, "side": o.side, "qty": o.qty, "approved": approved,
                                      "outcome": resp.get("outcome"), "rules": resp.get("rules", [])})
        if approved <= 0:
            self.report.ledger_rejections.append({"order_id": o.client_order_id, "rules": resp.get("rules", [])})
            self.on_ledger_events(evs)
            return
        self.book.plan(o.client_order_id, o.attribution if approved == o.qty else split(approved, o.attribution))
        self.on_ledger_events(evs)
        if self.clob is not None:
            t0 = timer()
            self._clob(Submit(self._cid("ord"), o.client_order_id, self.owner, o.instrument, o.side, o.limit_px, approved,
                              "IOC" if o.tif == "IOC" else "GTC"))
            self.report.timings_ns["match"].append(timer() - t0)

    def reconcile(self) -> list[dict]:
        """Ledger working venue orders must match the owner's resting CLOB orders exactly."""
        if self.clob is None:
            return []
        ledger_open = {k: o.remaining for k, o in self.ledger.state.orders.items()
                       if o.agent_id == self.owner and o.venue == "paper_clob"}
        clob_open = {o.order_id: o.remaining for o in self.clob.open_orders(self.owner)}
        breaks = []
        for oid in sorted(set(ledger_open) | set(clob_open)):
            if ledger_open.get(oid) != clob_open.get(oid):
                breaks.append({"order_id": oid, "ledger_remaining": ledger_open.get(oid), "clob_remaining": clob_open.get(oid)})
        self.report.reconciliation.extend(breaks)
        return breaks


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)
