"""Market-data gateway: the only component that publishes canonical market events.

What it guarantees to everything downstream:

- Events are immutable and typed, with instrument, venue, source, data class (historical,
  synthetic, demo, production), exchange and receive timestamps, a per-stream sequence number,
  and explicit units (prices in integer micros, quantities in integer lots).
- A gateway serves exactly one data class. Mixing, say, demo quotes into a historical run is
  rejected, so the class stays visible end to end.
- Duplicates (same stream and sequence, same payload) are dropped and counted. The same sequence
  with a different payload is a conflict and is rejected, never silently overwritten.
- Out-of-order events are buffered until the gap fills. A gap marks the instrument as needing
  resync; while it does, the instrument is not fresh and the coordinator will not trade it.
  If a resync source is configured the gateway asks it for the missing range.
- Bars and trades are correctness critical: the bounded queue refuses them when full
  (`Backpressure`) instead of dropping them. Top-of-book quotes are replaceable, so a newer quote
  for the same instrument replaces an older queued one (coalescing), and that is counted.
- Strategies see a `MarketSnapshot`: a consistent view at one gateway sequence. Every array a
  strategy gets is its own copy, so one strategy cannot change prices another strategy sees.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

import numpy as np

from hellabandz.contracts import MICROS


class DataClass(StrEnum):
    HISTORICAL = "historical"
    SYNTHETIC = "synthetic"
    DEMO = "demo"
    PRODUCTION = "production"


class EventType(StrEnum):
    BAR = "bar"  # correctness critical
    TRADE = "trade"  # correctness critical
    QUOTE = "quote"  # replaceable: may be coalesced


CRITICAL = frozenset({EventType.BAR, EventType.TRADE})


@dataclass(frozen=True, slots=True)
class MarketEvent:
    instrument: str
    venue: str
    source: str
    data_class: DataClass
    event_type: EventType
    seq: int  # per (instrument, venue, source) stream, starting at 1
    exchange_ts: int | None  # epoch seconds from the venue, when it provides one
    receive_ts: int  # epoch seconds when the gateway received it
    # BAR: open/high/low/close/volume; QUOTE: bid/ask/bid_qty/ask_qty; TRADE: px/qty
    open: int = 0
    high: int = 0
    low: int = 0
    close: int = 0
    volume: int = 0
    bid: int = 0
    ask: int = 0
    bid_qty: int = 0
    ask_qty: int = 0
    px: int = 0
    qty: int = 0
    price_unit: str = "micros"
    qty_unit: str = "lots"

    @property
    def stream(self) -> tuple[str, str, str]:
        return (self.instrument, self.venue, self.source)

    @property
    def ts(self) -> int:
        return self.exchange_ts if self.exchange_ts is not None else self.receive_ts


class Backpressure(RuntimeError):
    """The bounded queue is full of correctness-critical events; the producer must wait and retry."""


@dataclass
class GatewayStats:
    published: int = 0
    duplicates: int = 0
    conflicts: int = 0
    invalid: int = 0
    buffered_out_of_order: int = 0
    gaps: int = 0
    resyncs: int = 0
    coalesced_quotes: int = 0
    backpressure: int = 0
    queue_high_water: int = 0


class _Stream:
    def __init__(self) -> None:
        self.next_seq = 1
        self.pending: dict[int, MarketEvent] = {}  # out-of-order events waiting for the gap to fill
        self.seen_hashes: dict[int, int] = {}  # seq -> payload hash, bounded by `dedupe_window`


class InstrumentHistory:
    """Bounded history of completed bars for one instrument (ring buffer, oldest dropped)."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.ts: deque[int] = deque(maxlen=capacity)
        self.close: deque[float] = deque(maxlen=capacity)
        self.high: deque[float] = deque(maxlen=capacity)
        self.low: deque[float] = deque(maxlen=capacity)
        self.volume: deque[int] = deque(maxlen=capacity)
        self.last_close_micros = 0
        self.last_seq = 0
        self.last_ts = 0

    def add(self, e: MarketEvent) -> None:
        self.ts.append(e.ts)
        self.close.append(e.close / MICROS)
        self.high.append(e.high / MICROS)
        self.low.append(e.low / MICROS)
        self.volume.append(e.volume)
        self.last_close_micros = e.close
        self.last_seq = e.seq
        self.last_ts = e.ts


@dataclass(frozen=True)
class InstrumentView:
    instrument: str
    last_seq: int
    last_ts: int
    last_close: int  # micros
    needs_resync: bool
    _close: tuple = field(repr=False, default=())
    _high: tuple = field(repr=False, default=())
    _low: tuple = field(repr=False, default=())
    _ts: tuple = field(repr=False, default=())
    _volume: tuple = field(repr=False, default=())

    # each call returns a new array: a strategy can modify its copy without affecting anyone else
    def closes(self) -> np.ndarray:
        return np.array(self._close, dtype=np.float64)

    def highs(self) -> np.ndarray:
        return np.array(self._high, dtype=np.float64)

    def lows(self) -> np.ndarray:
        return np.array(self._low, dtype=np.float64)

    def timestamps(self) -> np.ndarray:
        return np.array(self._ts, dtype=np.int64)

    def volumes(self) -> np.ndarray:
        return np.array(self._volume, dtype=np.int64)


@dataclass(frozen=True)
class MarketSnapshot:
    gateway_seq: int  # count of canonical events published when this snapshot was taken
    ts: int  # decision time: latest bar timestamp in the snapshot
    data_class: DataClass
    instruments: Mapping[str, InstrumentView]

    def fresh(self, instrument: str, max_age_s: int) -> bool:
        v = self.instruments.get(instrument)
        return v is not None and not v.needs_resync and v.last_seq > 0 and self.ts - v.last_ts <= max_age_s


class MarketDataGateway:
    def __init__(self, data_class: DataClass, *, history_capacity: int = 1024, queue_capacity: int = 4096,
                 dedupe_window: int = 4096, max_pending_per_stream: int = 256, max_future_skew_s: int = 5,
                 resync: Callable[[tuple[str, str, str], int, int], Iterable[MarketEvent]] | None = None):
        self.data_class = data_class
        self.history_capacity = history_capacity
        self.queue_capacity = queue_capacity
        self.dedupe_window = dedupe_window
        self.max_pending = max_pending_per_stream
        self.max_future_skew_s = max_future_skew_s
        self.resync_source = resync
        self.streams: dict[tuple[str, str, str], _Stream] = {}
        self.history: dict[str, InstrumentHistory] = {}
        self.needs_resync: set[str] = set()
        self.quotes: dict[str, MarketEvent] = {}  # latest top of book per instrument
        self.queue: deque[MarketEvent] = deque()  # canonical events awaiting consumers
        self._queued_quote: dict[str, MarketEvent] = {}
        self.seq = 0
        self.stats = GatewayStats()

    # ------------------------------------------------------------------ ingest

    @staticmethod
    def _payload_hash(e: MarketEvent) -> int:
        # receive time is excluded: a redelivered copy of the same event is a duplicate
        return hash((e.event_type, e.exchange_ts, e.open, e.high, e.low, e.close, e.volume,
                     e.bid, e.ask, e.bid_qty, e.ask_qty, e.px, e.qty))

    def _invalid(self, e: MarketEvent) -> str | None:
        if e.data_class != self.data_class:
            return "DATA_CLASS_MISMATCH"
        if e.seq < 1:
            return "INVALID_SEQUENCE"
        if e.exchange_ts is not None and e.exchange_ts > e.receive_ts + self.max_future_skew_s:
            return "TIMESTAMP_IN_FUTURE"
        if e.price_unit != "micros" or e.qty_unit != "lots":
            return "UNITS"
        if e.event_type == EventType.BAR:
            if not (0 < e.low <= min(e.open, e.close) and max(e.open, e.close) <= e.high and e.volume >= 0):
                return "INVALID_OHLC"
        elif e.event_type == EventType.QUOTE:
            if e.bid <= 0 or e.ask <= 0 or e.bid >= e.ask or e.bid_qty < 0 or e.ask_qty < 0:
                return "INVALID_QUOTE"
        elif e.event_type == EventType.TRADE:
            if e.px <= 0 or e.qty <= 0:
                return "INVALID_TRADE"
        return None

    def publish(self, e: MarketEvent) -> str:
        """Accept one raw event. Returns what happened: published, duplicate, buffered, or a rejection code.
        Raises Backpressure when a critical event cannot be queued (nothing is changed in that case)."""
        bad = self._invalid(e)
        if bad:
            self.stats.invalid += 1
            return bad
        st = self.streams.setdefault(e.stream, _Stream())
        h = self._payload_hash(e)
        if e.seq in st.seen_hashes or e.seq in st.pending:
            prior = st.seen_hashes.get(e.seq, None)
            if prior is None:
                prior = self._payload_hash(st.pending[e.seq])
            if prior == h:
                self.stats.duplicates += 1
                return "duplicate"
            self.stats.conflicts += 1
            return "SEQUENCE_CONFLICT"
        if e.seq < st.next_seq:  # older than our dedupe window: cannot prove it is a duplicate
            self.stats.conflicts += 1
            return "STALE_SEQUENCE"
        if e.seq > st.next_seq:
            if len(st.pending) >= self.max_pending:
                self._resync(e.stream, st)
                if e.seq > st.next_seq and len(st.pending) >= self.max_pending:
                    return "GAP_BUFFER_FULL"
            if e.seq > st.next_seq:
                if not st.pending:
                    self.stats.gaps += 1
                st.pending[e.seq] = e
                self.needs_resync.add(e.instrument)
                self.stats.buffered_out_of_order += 1
                self._resync(e.stream, st)
                return "buffered"
        result = self._accept(e, st, h)
        while st.next_seq in st.pending:
            nxt = st.pending.pop(st.next_seq)
            self._accept(nxt, st, self._payload_hash(nxt))
        if not st.pending:
            self.needs_resync.discard(e.instrument)
        return result

    def _resync(self, stream, st: _Stream) -> None:
        if self.resync_source is None or not st.pending:
            return
        missing_to = min(st.pending) - 1
        self.stats.resyncs += 1
        for ev in sorted(self.resync_source(stream, st.next_seq, missing_to), key=lambda x: x.seq):
            if ev.seq == st.next_seq and not self._invalid(ev):
                self._accept(ev, st, self._payload_hash(ev))
        while st.next_seq in st.pending:
            nxt = st.pending.pop(st.next_seq)
            self._accept(nxt, st, self._payload_hash(nxt))

    def _accept(self, e: MarketEvent, st: _Stream, h: int) -> str:
        if e.event_type in CRITICAL and self._depth() >= self.queue_capacity:
            self.stats.backpressure += 1
            raise Backpressure(f"queue full ({self.queue_capacity}); critical {e.event_type} for {e.instrument} refused")
        st.next_seq = e.seq + 1
        st.seen_hashes[e.seq] = h
        if len(st.seen_hashes) > self.dedupe_window:
            st.seen_hashes.pop(next(iter(st.seen_hashes)))
        hist = self.history.setdefault(e.instrument, InstrumentHistory(self.history_capacity))
        if e.event_type == EventType.BAR and e.ts <= hist.last_ts:
            # the sequence is consumed (the stream moves on) but a bar may never move time backwards
            self.stats.invalid += 1
            return "BAR_NOT_FORWARD_IN_TIME"
        self.seq += 1
        self.stats.published += 1
        if e.event_type == EventType.BAR:
            hist.add(e)
            self.queue.append(e)
        elif e.event_type == EventType.TRADE:
            self.queue.append(e)
        else:
            self.quotes[e.instrument] = e
            if e.instrument in self._queued_quote:
                self.stats.coalesced_quotes += 1
            else:
                self.queue.append(e)
            self._queued_quote[e.instrument] = e
        self.stats.queue_high_water = max(self.stats.queue_high_water, self._depth())
        return "published"

    def _depth(self) -> int:
        return len(self.queue)

    # ------------------------------------------------------------------ consume

    def drain(self, limit: int | None = None) -> list[MarketEvent]:
        """Hand queued canonical events to the consumer in publication order. A coalesced quote slot
        yields the newest quote for that instrument."""
        out = []
        while self.queue and (limit is None or len(out) < limit):
            e = self.queue.popleft()
            if e.event_type == EventType.QUOTE:
                e = self._queued_quote.pop(e.instrument, e)
            out.append(e)
        return out

    def warm(self, instrument: str, ts: Iterable[int], close: Iterable[float], high: Iterable[float],
             low: Iterable[float], volume: Iterable[int] | None = None) -> None:
        """Load pre-session history (bounded to capacity) without publishing events."""
        hist = self.history.setdefault(instrument, InstrumentHistory(self.history_capacity))
        vol = list(volume) if volume is not None else None
        for i, (t, c, h, lo) in enumerate(zip(ts, close, high, low)):
            hist.ts.append(int(t))
            hist.close.append(float(c))
            hist.high.append(float(h))
            hist.low.append(float(lo))
            hist.volume.append(int(vol[i]) if vol else 0)
            hist.last_ts = int(t)
            hist.last_close_micros = int(round(float(c) * MICROS))

    def snapshot(self) -> MarketSnapshot:
        views = {}
        for sym, hist in sorted(self.history.items()):
            views[sym] = InstrumentView(sym, hist.last_seq, hist.last_ts, hist.last_close_micros, sym in self.needs_resync,
                                        tuple(hist.close), tuple(hist.high), tuple(hist.low), tuple(hist.ts),
                                        tuple(hist.volume))
        ts = max((v.last_ts for v in views.values()), default=0)
        return MarketSnapshot(self.seq, ts, self.data_class, MappingProxyType(views))
