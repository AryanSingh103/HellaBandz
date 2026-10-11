"""Runtime strategies: incremental signals must match the vectorized research definition."""

from pathlib import Path

import numpy as np
import pytest

from hellabandz.backtester import SnapshotProvider
from hellabandz.ingestion.dataset import load_snapshot
from hellabandz.strategies import Series, signal
from hellabandz.trading.marketdata import DataClass, EventType, MarketDataGateway, MarketEvent
from hellabandz.trading.strategies import RuntimeStrategy, make_version

FIXTURE = Path(__file__).resolve().parents[2] / "data/fixtures/synthetic_1h"
CASES = [("trend", {"fast": 20, "slow": 100}), ("mean_reversion", {"lookback": 30, "entry_z": 1.5, "exit_z": 0.0}),
         ("defensive", {"vol_lookback": 50, "trend_lookback": 200, "target_vol": 0.12})]


@pytest.fixture(scope="module")
def data():
    t, m = load_snapshot(FIXTURE)
    return SnapshotProvider(t), m


@pytest.mark.parametrize("role,params", CASES)
def test_incremental_signal_matches_vectorized_bar_by_bar(data, role, params):
    p, m = data
    start = m["splits"]["test"][0]
    sym = "SPY"
    hist, hts = p.history(sym, start)
    s = RuntimeStrategy(make_version("s", role, params, [sym]), venue="bar_sim")
    s.warm(sym, hts, hist.close, hist.high, hist.low)
    g = MarketDataGateway(DataClass.SYNTHETIC, history_capacity=64)
    full_c, full_h, full_l = list(hist.close), list(hist.high), list(hist.low)
    got, want, near = [], [], []
    for i, b in enumerate(x for x in p.bars(start, m["splits"]["test"][1]) if x.symbol == sym):
        if i >= 600:
            break
        t = int(b.ts.timestamp())
        g.publish(MarketEvent(instrument=sym, venue="hist", source="fixture", data_class=DataClass.SYNTHETIC,
                              event_type=EventType.BAR, seq=i + 1, exchange_ts=t, receive_ts=t, open=b.open,
                              high=b.high, low=b.low, close=b.close, volume=b.volume))
        g.drain()
        intent = s.on_snapshot(g.snapshot())[0]
        full_c.append(b.close / 1e6)
        full_h.append(b.high / 1e6)
        full_l.append(b.low / 1e6)
        w = int(signal(s.v.family, s.p, Series(np.array(full_c), np.array(full_h), np.array(full_l)))[-1])
        got.append(intent.target_weight_bps)
        want.append(w)
    mism = [i for i, (a, b) in enumerate(zip(got, want)) if a != b]
    # floating-point summation order can differ in the last bits; allow at most 1 bps rounding steps
    assert all(abs(got[i] - want[i]) <= 1 for i in mism), [(i, got[i], want[i]) for i in mism][:5]
    assert len(got) == 600 and len(mism) <= 3


def test_intents_carry_the_required_fields(data):
    p, m = data
    start = m["splits"]["test"][0]
    s = RuntimeStrategy(make_version("trend.v1", "trend", {"fast": 20, "slow": 100}, ["SPY", "QQQ"]), venue="paper_clob")
    g = MarketDataGateway(DataClass.SYNTHETIC)
    for sym in ("SPY", "QQQ"):
        h, ts = p.history(sym, start)
        s.warm(sym, ts, h.close, h.high, h.low)
        g.warm(sym, ts[-200:], h.close[-200:], h.high[-200:], h.low[-200:])
    intents = s.on_snapshot(g.snapshot())
    assert [i.instrument for i in intents] == ["QQQ", "SPY"]
    i = intents[0]
    assert i.strategy_version == s.v.version and i.venue == "paper_clob" and i.expires_ts > i.decision_ts
    assert set(i.features) == {"sma_fast", "sma_slow"} and i.reasons and "Not comparable" in i.score_meaning


def test_versions_are_stable_and_bounded():
    a = make_version("x", "trend", {"fast": 20, "slow": 100}, ["SPY", "QQQ"])
    b = make_version("x", "trend", {"slow": 100.0, "fast": 20.0}, ["QQQ", "SPY"])
    assert a.version == b.version and a.version != make_version("x", "trend", {"fast": 21, "slow": 100}, ["SPY"]).version
    with pytest.raises(ValueError):
        make_version("x", "trend", {"fast": 1000, "slow": 100}, ["SPY"])
    d = a.describe()
    assert {"hypothesis", "entry", "exit", "parameter_bounds", "failure_conditions", "cost_assumptions"} <= set(d)
