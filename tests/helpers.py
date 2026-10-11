from datetime import datetime, timedelta, timezone

from hellabandz.contracts import (
    MICROS,
    ApplyBar,
    Bar,
    OpenPortfolio,
    OrderRequest,
    SetAllocation,
    SubmitOrder,
)
from hellabandz.ledger import EngineConfig, PortfolioState, process

T0 = datetime(2025, 1, 6, 14, 30, tzinfo=timezone.utc)
CFG = EngineConfig()


def ts(i: int) -> datetime:
    return T0 + timedelta(hours=i)


def usd(x: float) -> int:
    return int(round(x * MICROS))


def bar(i: int, o: float, c: float | None = None, *, sym: str = "SPY", h: float | None = None,
        lo: float | None = None, v: int = 1_000_000, stress: bool = False) -> ApplyBar:
    c = o if c is None else c
    h = max(o, c) if h is None else h
    lo = min(o, c) if lo is None else lo
    return ApplyBar(bar=Bar(symbol=sym, ts=ts(i), open=usd(o), high=usd(h), low=usd(lo), close=usd(c),
                            volume=v, stress=stress))


def order(coid: str, side: str = "BUY", qty: int = 10, *, agent: str = "a", sym: str = "SPY", sig: int = 0,
          order_type: str = "MKT", limit: float | None = None) -> SubmitOrder:
    return SubmitOrder(order=OrderRequest(
        client_order_id=coid, agent_id=agent, symbol=sym, side=side, qty=qty, order_type=order_type,
        limit_px=None if limit is None else usd(limit), signal_bar_ts=ts(sig)))


def fresh(cash: float = 10_000, allocs: dict[str, float] | None = None, cfg: EngineConfig = CFG,
          first_bar: ApplyBar | None = None) -> PortfolioState:
    s = PortfolioState()
    process(s, OpenPortfolio(initial_cash=usd(cash)), cfg)
    for a, c in (allocs or {"a": cash * 0.9}).items():
        process(s, SetAllocation(agent_id=a, capital=usd(c)), cfg)
    process(s, first_bar or bar(0, 100), cfg)
    return s
