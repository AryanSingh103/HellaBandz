"""I-06 determinism, I-09 causality (no look-ahead), I-11 equal-timestamp ordering,
dataset integrity."""

import random
import shutil
from pathlib import Path

import numpy as np
import pytest

from quantswarm.backtester import SnapshotProvider, Slot, run_backtest
from quantswarm.contracts import MICROS, StrategyFamily, StrategySpec
from quantswarm.ingestion.dataset import BarTable, load_snapshot
from quantswarm.ledger import replay
from quantswarm.strategies import Series, signal

FIXTURE = Path(__file__).resolve().parents[2] / "data/fixtures/synthetic_1h"
PARAMS = {
    StrategyFamily.SMA_CROSSOVER: {"fast": 10, "slow": 50},
    StrategyFamily.BREAKOUT: {"lookback": 40},
    StrategyFamily.ZSCORE_REVERSION: {"lookback": 30, "entry_z": 1.5, "exit_z": 0.0},
    StrategyFamily.VOL_TARGET: {"vol_lookback": 30, "trend_lookback": 100, "target_vol": 0.15},
}


@pytest.fixture(scope="module")
def data():
    t, m = load_snapshot(FIXTURE)
    return t, m, SnapshotProvider(t)


@pytest.mark.parametrize("family", list(PARAMS))
def test_signals_are_causal_under_truncation(data, family):
    """Weight at bar i must be identical whether or not bars after i exist."""
    _, _, p = data
    full, _ = p.history("NVDA", 2**62)
    w_full = signal(family, PARAMS[family], full)
    rng = random.Random(7)
    for cut in sorted(rng.sample(range(5, len(full)), 25)):
        w_cut = signal(family, PARAMS[family], full.head(cut))
        assert np.array_equal(w_cut, w_full[:cut]), f"look-ahead detected at cut {cut}"


@pytest.mark.parametrize("family", list(PARAMS))
def test_signals_ignore_future_perturbation(data, family):
    _, _, p = data
    s, _ = p.history("AAPL", 2**62)
    cut = len(s) // 2
    noisy = Series(s.close.copy(), s.high.copy(), s.low.copy())
    noisy.close[cut:] *= 3.0
    noisy.high[cut:] *= 3.0
    noisy.low[cut:] *= 0.1
    assert np.array_equal(signal(family, PARAMS[family], s)[:cut], signal(family, PARAMS[family], noisy)[:cut])


def _specs():
    return [
        StrategySpec(strategy_id="m", agent_id="momentum", family="sma_crossover", params={"fast": 10, "slow": 50},
                     universe=["SPY", "NVDA"]),
        StrategySpec(strategy_id="r", agent_id="meanrev", family="zscore_reversion",
                     params={"lookback": 30, "entry_z": 1.5, "exit_z": 0.0}, universe=["AAPL", "MSFT"]),
    ]


def test_backtest_does_not_read_data_after_split_end(data):
    t, m, p = data
    start, end = m["splits"]["validation"]
    base = run_backtest(p, [Slot(s, 4_000 * MICROS) for s in _specs()], start, end)
    after = t.ts >= end
    poisoned = BarTable(t.symbol, t.ts, t.open.copy(), t.high.copy(), t.low.copy(), t.close.copy(), t.volume)
    for col in (poisoned.open, poisoned.high, poisoned.low, poisoned.close):
        col[after] *= 5
    r = run_backtest(SnapshotProvider(poisoned), [Slot(s, 4_000 * MICROS) for s in _specs()], start, end)
    assert r.trades_digest == base.trades_digest and r.state_digest == base.state_digest


def test_identical_inputs_identical_outputs_and_replay(data):
    _, m, p = data
    start, end = m["splits"]["train"]
    a = run_backtest(p, [Slot(s, 4_000 * MICROS) for s in _specs()], start, end, check_every_step=True)
    b = run_backtest(p, [Slot(s, 4_000 * MICROS) for s in _specs()], start, end)
    assert a.trades_digest == b.trades_digest and a.state_digest == b.state_digest and a.equity == b.equity
    assert a.metrics == b.metrics


def test_input_row_order_does_not_change_results(data):
    t, m, _ = data
    rng = np.random.default_rng(3)
    perm = rng.permutation(len(t))
    shuffled = BarTable(*(getattr(t, c)[perm] for c in ("symbol", "ts", "open", "high", "low", "close", "volume")))
    start, end = m["splits"]["validation"]
    a = run_backtest(SnapshotProvider(t), [Slot(s, 4_000 * MICROS) for s in _specs()], start, end)
    b = run_backtest(SnapshotProvider(shuffled), [Slot(s, 4_000 * MICROS) for s in _specs()], start, end)
    assert shuffled.sorted().content_hash() == t.content_hash()
    assert a.trades_digest == b.trades_digest and a.state_digest == b.state_digest


def test_tampered_snapshot_refused(tmp_path):
    dst = tmp_path / "snap"
    shutil.copytree(FIXTURE, dst)
    import pyarrow.parquet as pq
    tab = pq.read_table(dst / "bars.parquet").to_pydict()
    tab["close"][100] += 10_000
    import pyarrow as pa
    pq.write_table(pa.table(tab), dst / "bars.parquet")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_snapshot(dst)


def test_event_chain_detects_tampering(data):
    from quantswarm.contracts import OpenPortfolio
    from quantswarm.ledger import EngineConfig, PortfolioState, process
    from tests.helpers import bar, order
    s = PortfolioState()
    log = []
    for c in [OpenPortfolio(initial_cash=10_000 * MICROS), bar(0, 100)]:
        log += process(s, c, EngineConfig())[0]
    from quantswarm.contracts import SetAllocation
    log += process(s, SetAllocation(agent_id="a", capital=5_000 * MICROS), EngineConfig())[0]
    log += process(s, order("o1", qty=10), EngineConfig())[0]
    log += process(s, bar(1, 100), EngineConfig())[0]
    assert replay(log).digest() == s.digest()
    bad = [dict(e) for e in log]
    fill = next(e for e in bad if e["type"] == "OrderFilled")
    fill["px"] -= 1
    with pytest.raises(AssertionError, match="hash mismatch"):
        replay(bad)
