"""Portfolio state and the pure event-application function.

`apply_event` is the ONLY code that mutates financial state. The execution
worker, the backtester, recovery replay and the tests all go through it.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from typing import Any

GENESIS_HASH = "0" * 64


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode()
    return hashlib.sha256(s).hexdigest()


def pos_key(agent_id: str, symbol: str) -> str:
    return f"{agent_id}|{symbol}"


@dataclass
class Position:
    qty: int = 0
    reserved: int = 0  # shares locked by open sell orders
    basis: int = 0  # total cost basis in micros (fees excluded)


@dataclass
class OpenOrder:
    order_id: str
    agent_id: str
    symbol: str
    side: str
    order_type: str
    limit_px: int | None
    qty: int  # approved quantity
    remaining: int
    reserved_cash: int  # BUY only: cash still locked for this order
    signal_ts: int  # epoch seconds of the bar the decision was based on
    created_seq: int
    bars_seen: int = 0


@dataclass
class PortfolioState:
    opened: bool = False
    initial_cash: int = 0
    cash: int = 0
    reserved_cash: int = 0
    realized_pnl: int = 0
    fees_paid: int = 0
    slippage_cost: int = 0
    peak_equity: int = 0
    kill_switch: bool = False
    kill_reason: str = ""
    allocations: dict[str, int] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    orders: dict[str, OpenOrder] = field(default_factory=dict)
    last_bar: dict[str, list[int]] = field(default_factory=dict)  # symbol -> [ts, close]
    decisions: dict[str, dict] = field(default_factory=dict)  # client_order_id -> summary
    agent_basis: dict[str, int] = field(default_factory=dict)
    agent_reserved_cash: dict[str, int] = field(default_factory=dict)
    seq: int = 0
    head_hash: str = GENESIS_HASH

    # ---- derived values ----
    def position(self, agent_id: str, symbol: str) -> Position:
        return self.positions.get(pos_key(agent_id, symbol)) or Position()

    def market_value(self) -> int:
        mv = 0
        for key, p in self.positions.items():
            if p.qty:
                mv += p.qty * self.last_bar[key.split("|", 1)[1]][1]
        return mv

    def equity(self) -> int:
        return self.cash + self.market_value()

    def agent_exposure(self, agent_id: str) -> int:
        """Cost-based exposure: basis of held positions + cash locked by open buys."""
        return self.agent_basis.get(agent_id, 0) + self.agent_reserved_cash.get(agent_id, 0)

    def snapshot(self, include_decisions: bool = True) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)
             if f.name not in ("allocations", "positions", "orders", "last_bar", "decisions",
                               "agent_basis", "agent_reserved_cash")}
        d["agent_basis"] = dict(self.agent_basis)
        d["agent_reserved_cash"] = dict(self.agent_reserved_cash)
        d["allocations"] = dict(self.allocations)
        d["last_bar"] = {k: list(v) for k, v in self.last_bar.items()}
        d["positions"] = {k: asdict(v) for k, v in sorted(self.positions.items()) if v.qty or v.reserved or v.basis}
        d["orders"] = {k: asdict(v) for k, v in sorted(self.orders.items())}
        if include_decisions:
            d["decisions"] = deepcopy(self.decisions)
        return d

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> PortfolioState:
        """Restore a complete checkpoint, including retry decisions and risk caches."""
        data = deepcopy(snapshot)
        if set(data) != {f.name for f in fields(cls)}:
            raise LedgerInvariantError("checkpoint has missing or unknown state fields")
        data["positions"] = {k: Position(**v) for k, v in data["positions"].items()}
        data["orders"] = {k: OpenOrder(**v) for k, v in data["orders"].items()}
        state = cls(**data)
        check_invariants(state)
        return state

    def digest(self) -> str:
        """Digest of economic state: cash, reservations, positions, working orders,
        allocations, prices and risk flags. Excludes the idempotency table
        (`decisions`, covered by the event hash chain) and the chain head itself,
        so independent runs that produced the same economics compare equal."""
        snap = self.snapshot(include_decisions=False)
        snap.pop("head_hash")
        snap.pop("seq")
        # Caches are validated against the underlying positions/orders. Excluding
        # them preserves digests of logs written before incremental sums existed.
        snap.pop("agent_basis")
        snap.pop("agent_reserved_cash")
        return sha256_hex(canonical(snap))


class LedgerInvariantError(AssertionError):
    pass


def check_invariants(s: PortfolioState) -> None:
    """Invariants I-02, I-03, I-07. Cheap enough to run after every command."""
    def fail(msg: str) -> None:
        raise LedgerInvariantError(msg)

    if s.cash < 0:
        fail(f"negative cash {s.cash}")
    if s.reserved_cash < 0 or s.reserved_cash > s.cash:
        fail(f"reserved cash {s.reserved_cash} outside [0, cash={s.cash}]")
    if sum(o.reserved_cash for o in s.orders.values()) != s.reserved_cash:
        fail("order reservations do not sum to reserved cash")
    sell_reserved: dict[str, int] = {}
    for o in s.orders.values():
        if o.remaining <= 0 or o.remaining > o.qty:
            fail(f"order {o.order_id} remaining {o.remaining} outside (0, {o.qty}]")
        if o.side == "SELL":
            k = pos_key(o.agent_id, o.symbol)
            sell_reserved[k] = sell_reserved.get(k, 0) + o.remaining
        elif o.reserved_cash < 0:
            fail(f"order {o.order_id} negative reservation")
    for k, p in s.positions.items():
        if p.qty < 0 or p.basis < 0:
            fail(f"negative position {k}: {p}")
        if p.reserved != sell_reserved.get(k, 0) or p.reserved > p.qty:
            fail(f"position {k} reserved {p.reserved} mismatch")
    basis = sum(p.basis for p in s.positions.values())
    agent_basis: dict[str, int] = {}
    agent_reserved: dict[str, int] = {}
    for key, p in s.positions.items():
        agent = key.split("|", 1)[0]
        agent_basis[agent] = agent_basis.get(agent, 0) + p.basis
    for o in s.orders.values():
        agent_reserved[o.agent_id] = agent_reserved.get(o.agent_id, 0) + o.reserved_cash
    if {k: v for k, v in s.agent_basis.items() if v} != {k: v for k, v in agent_basis.items() if v}:
        fail("incremental agent basis differs from full recomputation")
    if {k: v for k, v in s.agent_reserved_cash.items() if v} != {k: v for k, v in agent_reserved.items() if v}:
        fail("incremental agent reservations differ from full recomputation")
    if s.cash + basis != s.initial_cash + s.realized_pnl - s.fees_paid:
        fail("accounting identity violated: cash + basis != initial + realized - fees")


def apply_event(s: PortfolioState, ev: dict) -> None:
    t = ev["type"]
    if t == "PortfolioOpened":
        s.opened = True
        s.initial_cash = s.cash = s.peak_equity = ev["initial_cash"]
    elif t == "AllocationSet":
        s.allocations[ev["agent_id"]] = ev["capital"]
    elif t == "OrderAccepted":
        s.decisions[ev["order_id"]] = {
            "h": ev["payload_hash"], "outcome": ev["outcome"],
            "approved_qty": ev["qty"], "rules": ev["rules"],
        }
        s.orders[ev["order_id"]] = OpenOrder(
            order_id=ev["order_id"], agent_id=ev["agent_id"], symbol=ev["symbol"],
            side=ev["side"], order_type=ev["order_type"], limit_px=ev["limit_px"],
            qty=ev["qty"], remaining=ev["qty"], reserved_cash=ev["reserved_cash"],
            signal_ts=ev["signal_ts"], created_seq=s.seq,
        )
        if ev["side"] == "BUY":
            s.reserved_cash += ev["reserved_cash"]
            _add(s.agent_reserved_cash, ev["agent_id"], ev["reserved_cash"])
        else:
            s.positions.setdefault(pos_key(ev["agent_id"], ev["symbol"]), Position()).reserved += ev["qty"]
    elif t == "OrderRejected":
        s.decisions[ev["order_id"]] = {
            "h": ev["payload_hash"], "outcome": "REJECT", "approved_qty": 0, "rules": ev["rules"],
        }
    elif t == "BarApplied":
        s.last_bar[ev["symbol"]] = [ev["ts"], ev["close"]]
        for o in s.orders.values():
            if o.symbol == ev["symbol"] and o.signal_ts < ev["ts"]:
                o.bars_seen += 1
        s.peak_equity = max(s.peak_equity, s.equity())
    elif t == "OrderFilled":
        o = s.orders[ev["order_id"]]
        p = s.positions.setdefault(pos_key(o.agent_id, o.symbol), Position())
        q, px, fee = ev["qty"], ev["px"], ev["fee"]
        if o.side == "BUY":
            cost = q * px + fee
            o.reserved_cash -= cost
            s.reserved_cash -= cost
            s.cash -= cost
            p.qty += q
            p.basis += q * px
            _add(s.agent_reserved_cash, o.agent_id, -cost)
            _add(s.agent_basis, o.agent_id, q * px)
        else:
            removed = p.basis * q // p.qty
            p.basis -= removed
            _add(s.agent_basis, o.agent_id, -removed)
            p.qty -= q
            p.reserved -= q
            s.realized_pnl += q * px - removed
            s.cash += q * px - fee
        o.remaining -= q
        s.fees_paid += fee
        s.slippage_cost += ev["slippage"]
    elif t == "OrderClosed":
        o = s.orders.pop(ev["order_id"])
        s.reserved_cash -= o.reserved_cash
        _add(s.agent_reserved_cash, o.agent_id, -o.reserved_cash)
        if o.side == "SELL":
            s.positions[pos_key(o.agent_id, o.symbol)].reserved -= o.remaining
    elif t == "KillSwitchSet":
        s.kill_switch = ev["on"]
        s.kill_reason = ev["reason"] if ev["on"] else ""
    else:
        raise ValueError(f"unknown event type {t}")


def _add(totals: dict[str, int], agent: str, delta: int) -> None:
    value = totals.get(agent, 0) + delta
    if value:
        totals[agent] = value
    else:
        totals.pop(agent, None)


def append(s: PortfolioState, ev: dict) -> dict:
    """Apply an event and stamp it with sequence number and hash chain."""
    apply_event(s, ev)
    s.seq += 1
    stamped = {"seq": s.seq, "prev_hash": s.head_hash, **ev}
    stamped["hash"] = sha256_hex(s.head_hash + canonical(ev))
    s.head_hash = stamped["hash"]
    return stamped


def replay(events: list[dict], verify_chain: bool = True,
           initial_state: PortfolioState | None = None) -> PortfolioState:
    """Rebuild state from a stored event log, verifying the hash chain."""
    s = initial_state if initial_state is not None else PortfolioState()
    for stored in events:
        ev = {k: v for k, v in stored.items() if k not in ("seq", "prev_hash", "hash")}
        if verify_chain:
            if stored["seq"] != s.seq + 1 or stored["prev_hash"] != s.head_hash:
                raise LedgerInvariantError(f"event chain broken at seq {stored['seq']}")
            if sha256_hex(s.head_hash + canonical(ev)) != stored["hash"]:
                raise LedgerInvariantError(f"event hash mismatch at seq {stored['seq']}")
        append(s, ev)
    return s
