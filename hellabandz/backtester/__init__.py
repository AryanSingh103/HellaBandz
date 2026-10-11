"""Deterministic event-time backtester.

A DataProvider supplies frozen bars and causal history. The backtester has no
accounting or fill logic of its own: it feeds ApplyBar/SubmitOrder commands into the same
`ledger.process` state machine the execution worker uses.

Event ordering (I-11): bars sorted by (ts, symbol); all bars at a timestamp
are applied (fills for older orders happen here) before any strategy sees that
timestamp; strategies then decide in strategy_id order, symbols ascending.
Orders decided on bar t can only fill on a later bar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Protocol

import numpy as np

from hellabandz.contracts import (
    MICROS,
    ApplyBar,
    Bar,
    Metrics,
    OpenPortfolio,
    OrderRequest,
    SetAllocation,
    StrategySpec,
    SubmitOrder,
)
from hellabandz.ingestion.dataset import BarTable
from hellabandz.ledger import EngineConfig, PortfolioState, canonical, check_invariants, process, sha256_hex
from hellabandz.strategies import ALLOCATION_FAMILIES, COORDINATED_ONLY_FAMILIES, BARS_PER_YEAR, HOLD_FAMILIES, Series, universe_weights

class DataProvider(Protocol):
    def bars(self, start_ts: int, end_ts: int) -> Iterator[Bar]: ...
    def history(self, symbol: str, end_ts: int) -> tuple[Series, np.ndarray]: ...


class SnapshotProvider:
    """Serves a frozen BarTable. history() never returns rows at or after end_ts."""

    def __init__(self, table: BarTable):
        self.t = table.sorted()
        self._by_symbol: dict[str, np.ndarray] = {s: np.flatnonzero(self.t.symbol == s) for s in self.t.symbols()}

    def symbols(self) -> list[str]:
        return list(self._by_symbol)

    def bars(self, start_ts: int, end_ts: int) -> Iterator[Bar]:
        lo, hi = np.searchsorted(self.t.ts, [start_ts, end_ts], side="left")
        t = self.t
        for i in range(lo, hi):
            yield Bar(symbol=t.symbol[i], ts=datetime.fromtimestamp(int(t.ts[i]), timezone.utc),
                      open=int(t.open[i]), high=int(t.high[i]), low=int(t.low[i]),
                      close=int(t.close[i]), volume=int(t.volume[i]))

    def history(self, symbol: str, end_ts: int) -> tuple[Series, np.ndarray]:
        idx = self._by_symbol[symbol]
        idx = idx[self.t.ts[idx] < end_ts]
        f = lambda a: a[idx].astype(np.float64) / MICROS  # noqa: E731
        return Series(f(self.t.close), f(self.t.high), f(self.t.low)), self.t.ts[idx]


@dataclass(frozen=True)
class Slot:
    spec: StrategySpec
    capital: int  # micros


def rebalance_order(state: PortfolioState, agent_id: str, symbol: str, w_bps: int, slot_capital: int,
                    close: int, n_symbols: int, threshold_bps: int = 1_000, hold: bool = False) -> tuple[str, int] | None:
    """Translate a target weight into a buy/sell quantity. Shared by backtests and live sessions.

    With `hold`, the slot is deployed once against its cost budget and never trimmed while the
    target stays long, so a held position is not sold into strength or topped up into weakness.
    While the kill switch blocks buying, an exit could not be reversed, so a hold slot stays as it is."""
    if any(o.agent_id == agent_id and o.symbol == symbol for o in state.orders.values()):
        return None  # one working order per agent/symbol
    if hold and state.kill_switch:
        return None
    per_symbol = slot_capital // n_symbols
    pos = state.position(agent_id, symbol)
    if w_bps == 0:
        return ("SELL", pos.qty) if pos.qty > 0 else None
    if hold:
        diff = per_symbol * w_bps // 10_000 - pos.basis
        q = diff // close
        return ("BUY", q) if diff * 10_000 >= per_symbol * threshold_bps and q > 0 else None
    diff = per_symbol * w_bps // 10_000 - pos.qty * close
    if abs(diff) * 10_000 < per_symbol * threshold_bps:
        return None
    q = abs(diff) // close
    if diff < 0:
        q = min(q, pos.qty)
    return (("BUY" if diff > 0 else "SELL"), q) if q > 0 else None


def reentry_capital(state, spec: StrategySpec, slot_capital: int, initial_cash: int, equity: int) -> int | None:
    """A hold slot that is fully in cash is re-sized to its original share of current equity before it
    re-enters, so realized gains are reinvested and losses shrink it. Returns None when no resize applies.
    Shared by backtests and live sessions."""
    if spec.family not in HOLD_FAMILIES or any(o.agent_id == spec.agent_id for o in state.orders.values()):
        return None
    if any(state.position(spec.agent_id, sym).qty for sym in spec.universe):
        return None
    return slot_capital * equity // initial_cash


@dataclass
class BacktestResult:
    run_id: str
    start_ts: int
    end_ts: int
    metrics: Metrics
    equity_ts: list[int]
    equity: list[int]
    fills: list[dict]
    rejections: int
    events: int
    state_digest: str
    trades_digest: str
    config_digest: str
    final_state: PortfolioState = field(repr=False)

    def summary(self) -> dict:
        return {"run_id": self.run_id, "start_ts": self.start_ts, "end_ts": self.end_ts,
                "metrics": self.metrics.model_dump(), "rejections": self.rejections, "events": self.events,
                "state_digest": self.state_digest, "trades_digest": self.trades_digest,
                "config_digest": self.config_digest}


def compute_metrics(equity: list[int], fills: list[dict], state: PortfolioState) -> Metrics:
    eq = np.array(equity, dtype=np.float64)
    init = state.initial_cash
    if len(eq) > 1:
        r = np.diff(eq) / eq[:-1]
        sd = r.std()
        sharpe = float(r.mean() / sd * math.sqrt(BARS_PER_YEAR)) if sd > 0 else 0.0
        peak = np.maximum.accumulate(eq)
        mdd = float(((peak - eq) / peak).max())
    else:
        sharpe, mdd = 0.0, 0.0
    notional = sum(f["qty"] * f["px"] for f in fills)
    final = int(eq[-1]) if len(eq) else init
    return Metrics(
        net_return=round((final - init) / init, 8), sharpe=round(sharpe, 6), max_drawdown=round(mdd, 8),
        n_fills=len(fills), turnover=round(notional / init, 6), fees_usd=state.fees_paid / MICROS,
        slippage_usd=state.slippage_cost / MICROS, final_equity_usd=final / MICROS,
    )


def run_backtest(provider: DataProvider, slots: list[Slot], start_ts: int, end_ts: int,
                 cfg: EngineConfig = EngineConfig(), initial_cash: int = 10_000 * MICROS,
                 run_id: str = "bt", check_every_step: bool = False) -> BacktestResult:
    if any(sl.spec.family in COORDINATED_ONLY_FAMILIES for sl in slots):
        raise ValueError("allocation and fixed-unit families require CoordinatedRuntime sizing; legacy backtester uses slots")
    s = PortfolioState()
    process(s, OpenPortfolio(initial_cash=initial_cash), cfg)
    slots = sorted(slots, key=lambda x: x.spec.strategy_id)
    for sl in slots:
        process(s, SetAllocation(agent_id=sl.spec.agent_id, capital=sl.capital), cfg)

    # Precompute causal signals on history strictly before end_ts (nothing after the split leaks in).
    sig: dict[tuple[str, str], dict[int, int]] = {}
    for sl in slots:
        histories = {sym: provider.history(sym, end_ts) for sym in sl.spec.universe}
        weights = universe_weights(sl.spec.family, sl.spec.params, histories)
        for sym, (_, ts) in histories.items():
            sig[(sl.spec.strategy_id, sym)] = dict(zip(ts.tolist(), weights[sym].tolist()))

    capital = {sl.spec.strategy_id: sl.capital for sl in slots}
    fills: list[dict] = []
    eq_ts: list[int] = []
    eq: list[int] = []
    rejections = 0
    n_events = s.seq

    def group_by_ts(it: Iterator[Bar]):
        cur: list[Bar] = []
        for b in it:
            if cur and b.ts != cur[0].ts:
                yield cur
                cur = []
            cur.append(b)
        if cur:
            yield cur

    for group in group_by_ts(provider.bars(start_ts, end_ts)):
        ts = int(group[0].ts.timestamp())
        closes = {}
        for b in group:
            evs, _ = process(s, ApplyBar(bar=b), cfg)
            n_events += len(evs)
            fills.extend(e for e in evs if e["type"] == "OrderFilled")
            closes[b.symbol] = b.close
        for sl in slots:
            spec = sl.spec
            targets = {sym: sig[(spec.strategy_id, sym)].get(ts, 0) for sym in sorted(spec.universe) if sym in closes}
            if any(targets.values()):
                resized = reentry_capital(s, spec, sl.capital, initial_cash, s.equity())
                if resized is not None and resized != capital[spec.strategy_id]:
                    evs, resp = process(s, SetAllocation(agent_id=spec.agent_id, capital=resized), cfg)
                    n_events += len(evs)
                    if resp["status"] == "ok":
                        capital[spec.strategy_id] = resized
            for sym, w in targets.items():
                act = rebalance_order(s, spec.agent_id, sym, w, capital[spec.strategy_id], closes[sym],
                                      len(spec.universe), hold=spec.family in HOLD_FAMILIES)
                if act is None:
                    continue
                side, qty = act
                req = OrderRequest(client_order_id=f"{run_id}.{spec.strategy_id}.{sym}.{ts}", agent_id=spec.agent_id,
                                   symbol=sym, side=side, qty=qty, signal_bar_ts=group[0].ts)
                evs, resp = process(s, SubmitOrder(order=req), cfg)
                n_events += len(evs)
                rejections += resp["status"] == "rejected"
        if check_every_step:
            check_invariants(s)
        eq_ts.append(ts)
        eq.append(s.equity())

    check_invariants(s)
    fill_view = [{k: f[k] for k in ("order_id", "agent_id", "symbol", "side", "qty", "px", "fee", "bar_ts")} for f in fills]
    return BacktestResult(
        run_id=run_id, start_ts=start_ts, end_ts=end_ts, metrics=compute_metrics(eq, fill_view, s),
        equity_ts=eq_ts, equity=eq, fills=fill_view, rejections=rejections, events=n_events,
        state_digest=s.digest(), trades_digest=sha256_hex(canonical(fill_view)), config_digest=cfg.digest(),
        final_state=s,
    )
