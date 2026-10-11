"""The session computes each rule once over the whole published price path instead of once per step.

That is only valid because every family is causal: the target at bar k must not change when later bars are
appended. These tests pin that equivalence for every tradable family, including the basket family, whose
members are aligned through a shared index.
"""
import numpy as np
import pytest

from hellabandz.contracts import StrategyFamily as F
from hellabandz.strategies import Series, universe_weights

CASES = [
    (F.SMA_CROSSOVER, {"fast": 5, "slow": 20}),
    (F.BREAKOUT, {"lookback": 20}),
    (F.ZSCORE_REVERSION, {"lookback": 60, "entry_z": 1.0, "exit_z": 0.0}),
    (F.VOL_TARGET, {"vol_lookback": 40, "trend_lookback": 80, "target_vol": 0.15}),
    (F.RSI_REVERSION, {"lookback": 14, "entry_rsi": 35, "exit_rsi": 55}),
    (F.TS_MOMENTUM, {"lookback": 40, "threshold": 0.0}),
    (F.REGIME_REVERSION, {"regime_lookback": 80, "min_reversion": 0.02, "lookback": 40, "sell_z": 1.0, "rebuy_z": 0.0}),
    (F.BUY_HOLD, {}),
]


def histories(seed: int, n: int = 520):
    rng = np.random.default_rng(seed)
    out = {}
    for j, sym in enumerate(("AAA", "BBB", "CCC")):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, n)))
        ts = 1_700_000_000 + 3600 * np.arange(n, dtype=np.int64)
        keep = np.ones(n, dtype=bool)
        if j == 2:  # one member misses bars, so the basket index and its own timestamps are not aligned
            keep[rng.choice(np.arange(50, n), 25, replace=False)] = False
        out[sym] = (Series(close[keep], close[keep] * 1.003, close[keep] * 0.997), ts[keep])
    return out


@pytest.mark.parametrize("family,params", CASES, ids=[c[0].value for c in CASES])
def test_whole_path_targets_equal_stepwise_targets(family, params):
    full = histories(7)
    whole = universe_weights(family, params, full)
    clock = full["AAA"][1]
    for k in list(range(300, len(clock), 7)) + [len(clock) - 1]:
        now = clock[k]
        seen = {sym: int(np.searchsorted(ts, now, side="right")) for sym, (_, ts) in full.items()}
        cut = {sym: (Series(s.close[:seen[sym]], s.high[:seen[sym]], s.low[:seen[sym]]), ts[:seen[sym]])
               for sym, (s, ts) in full.items()}
        stepwise = universe_weights(family, params, cut)
        for sym in full:
            assert int(stepwise[sym][-1]) == int(whole[sym][seen[sym] - 1]), (family.value, sym, k)
