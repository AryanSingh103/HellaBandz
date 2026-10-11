"""The coordinated runtime: one deterministic loop per portfolio.

Per market timestamp (one "cycle"):

  1. market-data gateway publishes the bars (canonical events, sequence numbers)
  2. ledger ApplyBar marks prices (and, in historical_sim, fills earlier orders from the bar)
  3. paper_clob only: the synthetic liquidity provider requotes; resting orders may trade
  4. each strategy reads the same immutable snapshot and emits intents
  5. the coordinator turns all intents into at most one order per instrument
  6. the execution authority cancels, transfers, submits, matches and applies fills
  7. attribution is checked against the ledger; venue orders are reconciled with the CLOB

Everything runs on one thread in a fixed order, so a run is reproducible from its inputs: the same
snapshot, strategy versions, budget and mode give the same decisions, orders, fills and digests.
Timings are measured with perf_counter_ns around each stage and reported as p50/p95/p99.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import numpy as np

from hellabandz.contracts import ApplyBar, Bar, MICROS, OpenPortfolio, SetAllocation
from hellabandz.ledger import check_invariants
from hellabandz.trading.attribution import AttributionBook
from hellabandz.trading.clob import InstrumentRule
from hellabandz.trading.coordinator import EXEC_AGENT, AccountView, ActiveStrategy, Coordinator, RiskBudget
from hellabandz.trading.execution import LEDGER_VENUE, ExecutionAuthority, LiquidityConfig, LocalLedger, VenueMode
from hellabandz.trading.instruments import Instrument
from hellabandz.trading.intents import Decision, Outcome
from hellabandz.trading.marketdata import DataClass, EventType, MarketDataGateway, MarketEvent
from hellabandz.trading.strategies import RuntimeStrategy

STAGES = ("market_data", "intents", "decision", "execution", "submit", "match", "reconcile", "cycle")


def percentiles(xs: list[int]) -> dict:
    if not xs:
        return {"n": 0}
    a = np.asarray(xs, dtype=np.float64) / 1000.0  # microseconds
    return {"n": len(xs), "p50_us": round(float(np.percentile(a, 50)), 2), "p95_us": round(float(np.percentile(a, 95)), 2),
            "p99_us": round(float(np.percentile(a, 99)), 2), "max_us": round(float(a.max()), 2)}


@dataclass
class CycleRecord:
    cycle: int
    ts: int
    decisions: list[Decision]
    equity: int


@dataclass
class RuntimeResult:
    run_id: str
    mode: str
    cycles: int
    equity_ts: list[int]
    equity: list[int]
    decisions: list[dict]
    intent_outcomes: dict[str, int]
    orders: list[dict]
    ledger_rejections: int
    trades: int
    fills: int
    attribution: dict
    timings: dict
    gateway: dict
    reconciliation_breaks: list[dict]
    ledger_digest: str
    head_hash: str
    clob_head_hash: str | None
    events: int
    exposure_bps_mean: float
    turnover: float
    fees: int
    slippage: int
    final_equity: int
    initial_cash: int
    cpu_s: float = 0.0
    peak_rss_mb: float | None = None
    shadow_orders: list[dict] = field(default_factory=list)


class CoordinatedRuntime:
    def __init__(self, run_id: str, mode: VenueMode, strategies: list[RuntimeStrategy], capital: dict[str, int],
                 budget: RiskBudget, instruments: dict[str, Instrument], data_class: DataClass,
                 initial_cash: int = 10_000 * MICROS, ledger=None, liquidity: LiquidityConfig | None = None,
                 keep_decisions: bool = True):
        self.run_id = run_id
        self.mode = mode
        self.strategies = sorted(strategies, key=lambda s: s.v.strategy_id)
        self.instruments = instruments
        self.initial_cash = initial_cash
        self.ledger = ledger or LocalLedger()
        self.gateway = MarketDataGateway(data_class)
        venue = LEDGER_VENUE[mode]
        self.active = {s.v.strategy_id: ActiveStrategy(s.v.strategy_id, s.v.version, s.v.universe, capital[s.v.strategy_id],
                                                       hold=s.hold) for s in self.strategies}
        self.book = AttributionBook(initial_cash, {k: a.capital for k, a in self.active.items()})
        self.coordinator = Coordinator(budget, instruments, venue, run_id)
        rules = [InstrumentRule(sym, tick=i.tick, lot=i.lot) for sym, i in sorted(instruments.items())]
        self.authority = ExecutionAuthority(mode, self.ledger, self.book, self.coordinator.state, rules, liquidity=liquidity)
        self.md_seq: dict[str, int] = {}
        self.timings: dict[str, list[int]] = {k: [] for k in STAGES}
        self.records: list[CycleRecord] = []
        self.keep_decisions = keep_decisions
        self.outcomes: dict[str, int] = {o.value: 0 for o in Outcome}
        self.n_decisions = 0
        self.exposure_bps: list[int] = []

    # ------------------------------------------------------------------ setup

    def open(self) -> None:
        for cmd in (OpenPortfolio(initial_cash=self.initial_cash),
                    SetAllocation(agent_id=EXEC_AGENT, capital=sum(a.capital for a in self.active.values()))):
            _, resp = self.ledger.process(cmd)
            if resp.get("status") != "ok":
                raise RuntimeError(f"ledger refused {cmd.kind}: {resp}")

    def warm(self, provider, start_ts: int) -> None:
        """Strategies and gateway see history strictly before start_ts (nothing from the evaluated period)."""
        hists = {}
        for sym in sorted({s for st in self.strategies for s in st.v.universe}):
            hist, ts = provider.history(sym, start_ts)
            hists[sym] = (ts, hist.close, hist.high, hist.low)
        for st in self.strategies:
            st.warm_universe({s: hists[s] for s in st.v.universe})
        cap = self.gateway.history_capacity
        for sym, (ts, c, h, lo) in hists.items():
            self.gateway.warm(sym, ts[-cap:], c[-cap:], h[-cap:], lo[-cap:])

    # ------------------------------------------------------------------ one cycle

    def step(self, bars: list[Bar]) -> CycleRecord:
        clock = time.perf_counter_ns
        c0 = clock()
        ts = int(bars[0].ts.timestamp())
        for b in sorted(bars, key=lambda x: x.symbol):
            seq = self.md_seq.get(b.symbol, 0) + 1
            self.md_seq[b.symbol] = seq
            self.gateway.publish(MarketEvent(instrument=b.symbol, venue="replay", source="snapshot",
                                             data_class=self.gateway.data_class, event_type=EventType.BAR, seq=seq,
                                             exchange_ts=ts, receive_ts=ts, open=b.open, high=b.high, low=b.low,
                                             close=b.close, volume=b.volume))
        for e in self.gateway.drain():
            bar = next(b for b in bars if b.symbol == e.instrument)
            self.authority.ledger_process(ApplyBar(bar=bar))
            self.authority.on_bar(e.instrument, e.close, e.volume, ts)
        c1 = clock()
        snap = self.gateway.snapshot()
        intents = [i for s in self.strategies for i in s.on_snapshot(snap)]
        c2 = clock()
        acct = AccountView.from_state(self.ledger.state)
        decisions = self.coordinator.decide(snap, intents, acct, self.book, self.active)
        c3 = clock()
        for d in decisions:
            self.authority.execute(d, clock)
        c4 = clock()
        s = self.ledger.state
        check_invariants(s)
        marks = {sym: v[1] for sym, v in s.last_bar.items()}
        acct = AccountView.from_state(s)
        self.book.check(s.cash, acct.positions, s.fees_paid, s.slippage_cost, marks)
        self.authority.reconcile()
        c5 = clock()
        for k, v in (("market_data", c1 - c0), ("intents", c2 - c1), ("decision", c3 - c2), ("execution", c4 - c3),
                     ("reconcile", c5 - c4), ("cycle", c5 - c0)):
            self.timings[k].append(v)
        eq = s.equity()
        self.exposure_bps.append((eq - s.cash) * 10_000 // eq if eq > 0 else 0)
        for d in decisions:
            self.n_decisions += 1
            for r in d.intents:
                self.outcomes[r.outcome.value] += 1
        rec = CycleRecord(self.coordinator.state.cycle, ts, decisions if self.keep_decisions else [], eq)
        self.records.append(rec)
        return rec

    def run(self, provider, start_ts: int, end_ts: int, max_cycles: int | None = None,
            overlay=None) -> RuntimeResult:
        cpu0 = time.process_time()
        groups: list[list[Bar]] = []
        for b in provider.bars(start_ts, end_ts):
            if groups and groups[-1][0].ts == b.ts:
                groups[-1].append(b)
            else:
                groups.append([b])
        for i, g in enumerate(groups[:max_cycles] if max_cycles else groups):
            self.step(overlay(i, g) if overlay else g)
        return self.result(time.process_time() - cpu0)

    def result(self, cpu_s: float = 0.0) -> RuntimeResult:
        import sys
        s = self.ledger.state
        marks = {sym: v[1] for sym, v in s.last_bar.items()}
        rep = self.authority.report
        timings = {k: percentiles(v) for k, v in self.timings.items() if k not in ("submit", "match")}
        timings["submit"] = percentiles(rep.timings_ns["submit"])
        timings["match"] = percentiles(rep.timings_ns["match"])
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = round(rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024, 1)
        except ImportError:  # resource is unavailable on Windows.
            rss_mb = None
        fills = [e for e in self.ledger.events if e["type"] == "OrderFilled"]
        notional = sum(e["qty"] * e["px"] for e in fills)
        return RuntimeResult(
            run_id=self.run_id, mode=self.mode.value, cycles=len(self.records),
            equity_ts=[r.ts for r in self.records], equity=[r.equity for r in self.records],
            decisions=[d.model_dump(mode="json") for r in self.records for d in r.decisions
                       if d.order or d.transfers or d.cancel_order_id or any(x.outcome == Outcome.REJECTED for x in d.intents)],
            intent_outcomes=dict(self.outcomes), orders=rep.submitted, ledger_rejections=len(rep.ledger_rejections),
            trades=len(rep.trades), fills=len(fills), attribution=self.book.report(marks), timings=timings,
            gateway=vars(self.gateway.stats).copy(), reconciliation_breaks=rep.reconciliation,
            ledger_digest=s.digest(), head_hash=s.head_hash,
            clob_head_hash=self.authority.clob.head_hash if self.authority.clob else None,
            events=len(self.ledger.events), exposure_bps_mean=float(np.mean(self.exposure_bps)) if self.exposure_bps else 0.0,
            turnover=notional / self.initial_cash, fees=s.fees_paid, slippage=s.slippage_cost, final_equity=s.equity(),
            initial_cash=self.initial_cash, cpu_s=round(cpu_s, 3), peak_rss_mb=rss_mb,
            shadow_orders=self.authority.shadow_orders)


def checkpoint(rt: "CoordinatedRuntime") -> bytes:
    """Full in-process state: gateway, strategies, coordinator, attribution, CLOB and in-process ledger.
    Local, trusted files only (pickle). The durable ledger in Postgres has its own recovery."""
    import pickle
    return pickle.dumps(rt, protocol=pickle.HIGHEST_PROTOCOL)


def restore(blob: bytes) -> "CoordinatedRuntime":
    import pickle
    rt = pickle.loads(blob)
    breaks = rt.authority.reconcile()
    if breaks:
        raise RuntimeError(f"restored runtime does not reconcile with its ledger: {breaks}")
    return rt


def utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)
