"""Internal central limit order book: a deterministic, single-writer matching engine.

Rules (each one is tested in tests/unit/test_clob.py):

- One bid book and one ask book per instrument. Prices are integer micros on the instrument's tick
  grid; quantities are integer lots. No floats anywhere.
- Price-time priority: the best price matches first; at one price, the order that rested first
  matches first. Time is the engine's own sequence number, never a wall clock.
- Limit orders only. GTC rests whatever does not match; IOC cancels whatever does not match
  immediately, with reason INSUFFICIENT_LIQUIDITY. A market order is an IOC with an aggressive limit,
  chosen by the caller, so the worst acceptable price is always explicit.
- A trade happens only between a buy and a sell that cross. Two buys never match, and the engine
  never creates liquidity: every trade has a resting maker order that a participant submitted.
- Trades execute at the maker's (resting) price.
- Self-trade prevention: an incoming order never trades with a resting order of the same
  participant. Mode "cancel_newest" (default) cancels the rest of the incoming order; mode
  "cancel_oldest" cancels the resting order and keeps matching.
- Cancellation removes a resting order. Cancel/replace is cancel followed by a new submit, which
  goes to the back of the queue at its price (documented, not an amend that keeps priority).
- Every command carries a command_id. Repeating a command returns the first response and emits
  nothing; reusing a command_id with a different payload is a conflict. Order ids are never reused.
- Events are numbered and hash-chained. Replaying the same commands from an empty engine produces
  the same events, hashes, and book.
"""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Literal

from hellabandz.ledger.state import GENESIS_HASH, canonical, sha256_hex

Side = Literal["BUY", "SELL"]
TimeInForce = Literal["GTC", "IOC"]


@dataclass(frozen=True)
class InstrumentRule:
    symbol: str
    tick: int  # price increment in micros
    lot: int = 1  # quantity increment in execution lots
    min_px: int = 1
    max_px: int = 10**15


@dataclass(frozen=True)
class Submit:
    command_id: str
    order_id: str
    participant: str
    symbol: str
    side: Side
    px: int
    qty: int
    tif: TimeInForce = "GTC"


@dataclass(frozen=True)
class Cancel:
    command_id: str
    order_id: str
    participant: str
    reason: str = "USER"


@dataclass
class Resting:
    order_id: str
    participant: str
    symbol: str
    side: Side
    px: int
    qty: int  # original quantity
    remaining: int
    seq: int  # arrival sequence: time priority


@dataclass
class _Book:
    bids: dict[int, deque] = field(default_factory=dict)
    asks: dict[int, deque] = field(default_factory=dict)
    bid_px: list[int] = field(default_factory=list)  # ascending; best bid is last
    ask_px: list[int] = field(default_factory=list)  # ascending; best ask is first


class MatchingEngine:
    def __init__(self, instruments: list[InstrumentRule], stp: Literal["cancel_newest", "cancel_oldest"] = "cancel_newest"):
        if stp not in ("cancel_newest", "cancel_oldest"):
            raise ValueError("unknown self-trade prevention mode")
        self.rules = {r.symbol: r for r in instruments}
        self.stp = stp
        self.books: dict[str, _Book] = {r.symbol: _Book() for r in instruments}
        self.orders: dict[str, Resting] = {}
        self.used_order_ids: set[str] = set()
        self.commands: dict[str, tuple[str, dict]] = {}  # command_id -> (payload hash, response)
        self.seq = 0
        self.trade_seq = 0
        self.head_hash = GENESIS_HASH

    # ------------------------------------------------------------------ reads

    def best_bid(self, symbol: str) -> int | None:
        b = self.books[symbol]
        return b.bid_px[-1] if b.bid_px else None

    def best_ask(self, symbol: str) -> int | None:
        b = self.books[symbol]
        return b.ask_px[0] if b.ask_px else None

    def depth(self, symbol: str, levels: int = 5) -> dict:
        """Aggregated levels with the participants resting there. Callers must label simulated depth."""
        b = self.books[symbol]

        def side(prices, book):
            out = []
            for px in prices[:levels]:
                q = book[px]
                out.append({"px": px, "qty": sum(o.remaining for o in q),
                            "participants": sorted({o.participant for o in q})})
            return out
        return {"symbol": symbol, "bids": side(list(reversed(b.bid_px)), b.bids), "asks": side(b.ask_px, b.asks)}

    def open_orders(self, participant: str | None = None) -> list[Resting]:
        return sorted((o for o in self.orders.values() if participant is None or o.participant == participant),
                      key=lambda o: o.seq)

    # ------------------------------------------------------------------ writes

    def process(self, cmd: Submit | Cancel) -> tuple[list[dict], dict]:
        h = sha256_hex(canonical({"kind": type(cmd).__name__, **asdict(cmd)}))
        prior = self.commands.get(cmd.command_id)
        if prior is not None:
            if prior[0] != h:
                return [], {"status": "conflict", "reason": "COMMAND_ID_REUSED_WITH_DIFFERENT_PAYLOAD"}
            return [], {**prior[1], "duplicate": True}
        events: list[dict] = []
        resp = self._submit(cmd, events) if isinstance(cmd, Submit) else self._cancel(cmd, events)
        self.commands[cmd.command_id] = (h, resp)
        return events, resp

    def _emit(self, events: list[dict], ev: dict) -> None:
        self.seq += 1
        body = {"seq": self.seq, **ev}
        self.head_hash = sha256_hex(self.head_hash + canonical(body))
        events.append({**body, "hash": self.head_hash})

    def _validate(self, c: Submit) -> str | None:
        r = self.rules.get(c.symbol)
        if r is None:
            return "UNKNOWN_INSTRUMENT"
        if c.side not in ("BUY", "SELL"):
            return "INVALID_SIDE"
        if c.tif not in ("GTC", "IOC"):
            return "INVALID_TIME_IN_FORCE"
        if not isinstance(c.qty, int) or isinstance(c.qty, bool) or c.qty <= 0 or c.qty % r.lot:
            return "INVALID_QUANTITY"
        if not isinstance(c.px, int) or isinstance(c.px, bool) or c.px % r.tick or not r.min_px <= c.px <= r.max_px:
            return "INVALID_PRICE"
        if c.order_id in self.used_order_ids:
            return "DUPLICATE_ORDER_ID"
        return None

    def _submit(self, c: Submit, events: list[dict]) -> dict:
        reason = self._validate(c)
        base = {"order_id": c.order_id, "participant": c.participant, "symbol": c.symbol}
        if reason:
            self._emit(events, {"type": "OrderRejected", **base, "reason": reason})
            return {"status": "rejected", "reason": reason, "filled": 0}
        self.used_order_ids.add(c.order_id)
        self._emit(events, {"type": "OrderAccepted", **base, "side": c.side, "px": c.px, "qty": c.qty, "tif": c.tif})
        book = self.books[c.symbol]
        remaining = c.qty
        filled = 0
        stp_stop = False
        while remaining > 0:
            if c.side == "BUY":
                if not book.ask_px or book.ask_px[0] > c.px:
                    break
                level_px, level = book.ask_px[0], book.asks[book.ask_px[0]]
            else:
                if not book.bid_px or book.bid_px[-1] < c.px:
                    break
                level_px, level = book.bid_px[-1], book.bids[book.bid_px[-1]]
            maker: Resting = level[0]
            if maker.participant == c.participant:
                if self.stp == "cancel_newest":
                    stp_stop = True
                    break
                self._remove(maker)
                self._emit(events, {"type": "OrderCancelled", "order_id": maker.order_id, "participant": maker.participant,
                                    "symbol": c.symbol, "remaining": maker.remaining, "reason": "SELF_TRADE_PREVENTION"})
                continue
            q = min(remaining, maker.remaining)
            self.trade_seq += 1
            maker.remaining -= q
            remaining -= q
            filled += q
            self._emit(events, {"type": "Trade", "trade_id": f"{c.symbol}-{self.trade_seq}", "symbol": c.symbol,
                                "px": level_px, "qty": q, "aggressor": c.side,
                                "maker_order_id": maker.order_id, "maker": maker.participant,
                                "taker_order_id": c.order_id, "taker": c.participant,
                                "maker_remaining": maker.remaining, "taker_remaining": remaining})
            if maker.remaining == 0:
                self._remove(maker)
                self._emit(events, {"type": "OrderDone", "order_id": maker.order_id, "participant": maker.participant,
                                    "symbol": c.symbol, "reason": "FILLED"})
        if remaining == 0:
            self._emit(events, {"type": "OrderDone", **base, "reason": "FILLED"})
            return {"status": "filled", "filled": filled, "remaining": 0}
        if stp_stop:
            self._emit(events, {"type": "OrderCancelled", **base, "remaining": remaining, "reason": "SELF_TRADE_PREVENTION"})
            return {"status": "cancelled", "reason": "SELF_TRADE_PREVENTION", "filled": filled, "remaining": remaining}
        if c.tif == "IOC":
            self._emit(events, {"type": "OrderCancelled", **base, "remaining": remaining, "reason": "INSUFFICIENT_LIQUIDITY"})
            return {"status": "cancelled", "reason": "INSUFFICIENT_LIQUIDITY", "filled": filled, "remaining": remaining}
        self._rest(Resting(c.order_id, c.participant, c.symbol, c.side, c.px, c.qty, remaining, self.seq))
        self._emit(events, {"type": "OrderRested", **base, "side": c.side, "px": c.px, "remaining": remaining})
        return {"status": "resting", "filled": filled, "remaining": remaining}

    def _cancel(self, c: Cancel, events: list[dict]) -> dict:
        o = self.orders.get(c.order_id)
        if o is None:
            return {"status": "rejected", "reason": "NOT_FOUND"}
        if o.participant != c.participant:
            return {"status": "rejected", "reason": "NOT_OWNER"}
        self._remove(o)
        self._emit(events, {"type": "OrderCancelled", "order_id": o.order_id, "participant": o.participant,
                            "symbol": o.symbol, "remaining": o.remaining, "reason": c.reason})
        return {"status": "cancelled", "remaining": o.remaining}

    def _rest(self, o: Resting) -> None:
        book = self.books[o.symbol]
        side, prices = (book.bids, book.bid_px) if o.side == "BUY" else (book.asks, book.ask_px)
        if o.px not in side:
            side[o.px] = deque()
            bisect.insort(prices, o.px)
        side[o.px].append(o)
        self.orders[o.order_id] = o

    def _remove(self, o: Resting) -> None:
        book = self.books[o.symbol]
        side, prices = (book.bids, book.bid_px) if o.side == "BUY" else (book.asks, book.ask_px)
        level = side[o.px]
        level.remove(o)
        if not level:
            del side[o.px]
            prices.pop(bisect.bisect_left(prices, o.px))
        self.orders.pop(o.order_id, None)

    def check(self) -> None:
        """Book invariants: never crossed, levels non-empty and sorted, orders indexed once."""
        seen = 0
        for sym, b in self.books.items():
            assert b.bid_px == sorted(b.bid_px) and b.ask_px == sorted(b.ask_px)
            assert set(b.bid_px) == set(b.bids) and set(b.ask_px) == set(b.asks)
            if b.bid_px and b.ask_px:
                assert b.bid_px[-1] < b.ask_px[0], f"crossed book {sym}"
            for px, q in list(b.bids.items()) + list(b.asks.items()):
                assert q and all(o.px == px and o.remaining > 0 and self.orders[o.order_id] is o for o in q)
                assert [o.seq for o in q] == sorted(o.seq for o in q)
                seen += len(q)
        assert seen == len(self.orders)
