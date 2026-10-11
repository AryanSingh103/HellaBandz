"""Market-data gateway: duplicates, gaps, ordering, staleness, backpressure, coalescing, isolation."""

import pytest

from hellabandz.contracts import MICROS
from hellabandz.trading.marketdata import Backpressure, DataClass, EventType, MarketDataGateway, MarketEvent

T0 = 1_700_000_000


def bar(seq, close=100.0, *, sym="SPY", ts=None, dc=DataClass.HISTORICAL, high=None, low=None):
    c = int(close * MICROS)
    ts = ts if ts is not None else T0 + 3600 * seq
    return MarketEvent(instrument=sym, venue="hist", source="snapshot", data_class=dc, event_type=EventType.BAR,
                       seq=seq, exchange_ts=ts, receive_ts=ts,
                       open=c, high=high or c, low=low or c, close=c, volume=1000)


def quote(seq, bid, ask, sym="SPY"):
    return MarketEvent(instrument=sym, venue="hist", source="quotes", data_class=DataClass.HISTORICAL,
                       event_type=EventType.QUOTE, seq=seq, exchange_ts=T0 + seq, receive_ts=T0 + seq,
                       bid=int(bid * MICROS), ask=int(ask * MICROS), bid_qty=10, ask_qty=10)


def test_duplicates_are_dropped_and_conflicts_rejected():
    g = MarketDataGateway(DataClass.HISTORICAL)
    assert g.publish(bar(1)) == "published"
    assert g.publish(bar(1)) == "duplicate"
    assert g.publish(bar(1, close=101.0)) == "SEQUENCE_CONFLICT"
    assert g.stats.duplicates == 1 and g.stats.conflicts == 1 and len(g.drain()) == 1


def test_out_of_order_events_wait_for_the_gap_and_publish_in_order():
    g = MarketDataGateway(DataClass.HISTORICAL)
    g.publish(bar(1))
    assert g.publish(bar(3)) == "buffered"
    snap = g.snapshot()
    assert "SPY" in g.needs_resync and not snap.fresh("SPY", 10**9)
    assert g.publish(bar(2)) == "published"
    assert [e.seq for e in g.drain()] == [1, 2, 3] and "SPY" not in g.needs_resync
    assert g.snapshot().fresh("SPY", 3600)


def test_gap_is_filled_from_the_resync_source_when_one_exists():
    calls = []

    def resync(stream, first, last):
        calls.append((stream, first, last))
        return [bar(s) for s in range(first, last + 1)]

    g = MarketDataGateway(DataClass.HISTORICAL, resync=resync)
    g.publish(bar(1))
    assert g.publish(bar(5)) == "buffered"
    assert calls == [(("SPY", "hist", "snapshot"), 2, 4)]
    assert [e.seq for e in g.drain()] == [1, 2, 3, 4, 5] and g.stats.resyncs == 1


def test_mixed_data_classes_and_invalid_events_are_rejected():
    g = MarketDataGateway(DataClass.HISTORICAL)
    assert g.publish(bar(1, dc=DataClass.DEMO)) == "DATA_CLASS_MISMATCH"
    assert g.publish(bar(1, high=99.0)) == "INVALID_OHLC"
    future = MarketEvent(instrument="SPY", venue="hist", source="snapshot", data_class=DataClass.HISTORICAL,
                         event_type=EventType.BAR, seq=1, exchange_ts=T0 + 999, receive_ts=T0,
                         open=MICROS, high=MICROS, low=MICROS, close=MICROS, volume=1)
    assert g.publish(future) == "TIMESTAMP_IN_FUTURE"
    assert g.publish(quote(1, 101.0, 100.0)) == "INVALID_QUOTE"
    assert g.stats.invalid == 4 and g.drain() == []


def test_bars_that_move_time_backwards_are_refused_but_the_stream_continues():
    g = MarketDataGateway(DataClass.HISTORICAL)
    g.publish(bar(1, ts=T0 + 7200))
    assert g.publish(bar(2, ts=T0 + 3600)) == "BAR_NOT_FORWARD_IN_TIME"
    assert g.publish(bar(3, ts=T0 + 10800)) == "published"
    assert [e.seq for e in g.drain()] == [1, 3]


def test_critical_events_get_backpressure_instead_of_being_dropped():
    g = MarketDataGateway(DataClass.HISTORICAL, queue_capacity=2)
    g.publish(bar(1))
    g.publish(bar(2))
    with pytest.raises(Backpressure):
        g.publish(bar(3))
    assert g.stats.backpressure == 1
    assert [e.seq for e in g.drain()] == [1, 2]
    assert g.publish(bar(3)) == "published"  # the producer retries after the consumer drains
    assert [e.seq for e in g.drain()] == [3]


def test_quotes_are_coalesced_to_the_newest_and_counted():
    g = MarketDataGateway(DataClass.HISTORICAL)
    for i, bid in enumerate([100.0, 100.1, 100.2], start=1):
        g.publish(quote(i, bid, bid + 0.05))
    out = g.drain()
    assert len(out) == 1 and out[0].bid == int(100.2 * MICROS) and g.stats.coalesced_quotes == 2


def test_snapshots_are_isolated_copies_and_history_is_bounded():
    g = MarketDataGateway(DataClass.HISTORICAL, history_capacity=3)
    for s in range(1, 6):
        g.publish(bar(s, close=100.0 + s))
    snap = g.snapshot()
    a = snap.instruments["SPY"].closes()
    assert list(a) == [103.0, 104.0, 105.0]
    a[:] = -1  # one strategy scribbling on its copy...
    assert list(snap.instruments["SPY"].closes()) == [103.0, 104.0, 105.0]  # ...changes nothing for anyone else
    with pytest.raises(TypeError):
        snap.instruments["SPY"] = None  # the mapping itself is read-only
    g.publish(bar(6, close=110.0))
    assert list(snap.instruments["SPY"].closes()) == [103.0, 104.0, 105.0]  # a snapshot never changes later


def test_staleness_uses_snapshot_time():
    g = MarketDataGateway(DataClass.HISTORICAL)
    g.publish(bar(1, sym="SPY"))
    g.publish(bar(1, sym="QQQ", ts=T0 + 5 * 3600))
    snap = g.snapshot()
    assert snap.fresh("QQQ", 3600) and not snap.fresh("SPY", 3600) and not snap.fresh("NVDA", 3600)
