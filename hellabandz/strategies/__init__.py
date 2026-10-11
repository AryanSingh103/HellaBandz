"""Whitelisted strategy families.

Agents can only choose a family and numeric parameters; they never ship code.
Every family is a causal function: the weight at bar i depends only on bars
0..i. `tests/unit/test_causality.py` checks this by truncating the input.
Weights are target long exposure in basis points of the strategy's slot (0..10_000).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from hellabandz.contracts import StrategyFamily

BARS_PER_YEAR = 252 * 7  # hourly regular-session bars


@dataclass(frozen=True)
class Series:
    close: np.ndarray  # float64 dollars
    high: np.ndarray
    low: np.ndarray

    def __len__(self) -> int:
        return len(self.close)

    def head(self, n: int) -> "Series":
        return Series(self.close[:n], self.high[:n], self.low[:n])


def _rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if n <= len(x):
        out[n - 1:] = sliding_window_view(x, n).mean(axis=1)
    return out


def _rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if n <= len(x):
        out[n - 1:] = sliding_window_view(x, n).std(axis=1)
    return out


def _to_bps(w: np.ndarray) -> np.ndarray:
    w = np.nan_to_num(w, nan=0.0)
    return np.clip(np.rint(w * 10_000), 0, 10_000).astype(np.int64)


def sma_crossover(s: Series, fast: float, slow: float) -> np.ndarray:
    fast, slow = int(fast), int(slow)
    if fast >= slow:
        raise ValueError("fast must be < slow")
    f, sl = _rolling_mean(s.close, fast), _rolling_mean(s.close, slow)
    return _to_bps(np.where(f > sl, 1.0, 0.0))


def breakout(s: Series, lookback: float) -> np.ndarray:
    n = int(lookback)
    exit_n = max(2, n // 2)
    w = np.zeros(len(s))
    held = False
    for i in range(n, len(s)):
        if not held and s.close[i] > s.high[i - n:i].max():
            held = True
        elif held and s.close[i] < s.low[i - exit_n:i].min():
            held = False
        w[i] = 1.0 if held else 0.0
    return _to_bps(w)


def zscore_reversion(s: Series, lookback: float, entry_z: float, exit_z: float) -> np.ndarray:
    n = int(lookback)
    m, sd = _rolling_mean(s.close, n), _rolling_std(s.close, n)
    z = np.where(sd > 0, (s.close - m) / np.where(sd > 0, sd, 1.0), 0.0)
    w = np.zeros(len(s))
    held = False
    for i in range(n - 1, len(s)):
        if not held and z[i] < -entry_z:
            held = True
        elif held and z[i] > exit_z:
            held = False
        w[i] = 1.0 if held else 0.0
    return _to_bps(w)


def vol_target(s: Series, vol_lookback: float, trend_lookback: float, target_vol: float) -> np.ndarray:
    vn, tn = int(vol_lookback), int(trend_lookback)
    r = np.zeros(len(s))
    r[1:] = np.diff(np.log(s.close))
    vol = _rolling_std(r, vn) * math.sqrt(BARS_PER_YEAR)
    trend = _rolling_mean(s.close, tn)
    w = np.where((s.close > trend) & (vol > 0), np.minimum(1.0, target_vol / np.where(vol > 0, vol, 1.0)), 0.0)
    return _to_bps(w)


def rsi_reversion(s: Series, lookback: float, entry_rsi: float, exit_rsi: float) -> np.ndarray:
    n = int(lookback)
    d = np.zeros(len(s))
    d[1:] = np.diff(s.close)
    gain, loss = _rolling_mean(np.maximum(d, 0.0), n), _rolling_mean(np.maximum(-d, 0.0), n)
    rsi = np.where(loss > 0, 100 - 100 / (1 + gain / np.where(loss > 0, loss, 1.0)), 100.0)
    w = np.zeros(len(s))
    held = False
    for i in range(n, len(s)):
        if not held and rsi[i] < entry_rsi:
            held = True
        elif held and rsi[i] > exit_rsi:
            held = False
        w[i] = 1.0 if held else 0.0
    return _to_bps(w)


def ts_momentum(s: Series, lookback: float, threshold: float) -> np.ndarray:
    n = int(lookback)
    w = np.zeros(len(s))
    if n < len(s):
        w[n:] = np.where(s.close[n:] / s.close[:-n] - 1 > threshold, 1.0, 0.0)
    return _to_bps(w)
def reversion_speed(close: np.ndarray, n: int) -> np.ndarray:
    """Rolling estimate of mean-reversion speed over the trailing n bars: minus the least-squares
    slope of the next log return on the demeaned log price. About theta for an Ornstein-Uhlenbeck
    series, and close to its small-sample bias (roughly 5/n) for a random walk."""
    out = np.full(len(close), np.nan)
    if n + 1 <= len(close):
        win = sliding_window_view(np.log(close), n + 1)
        x = win[:, :-1] - win[:, :-1].mean(axis=1, keepdims=True)
        den = (x * x).sum(axis=1)
        out[n:] = np.where(den > 0, -(x * np.diff(win, axis=1)).sum(axis=1) / np.where(den > 0, den, 1.0), np.nan)
    return out


def regime_reversion(s: Series, regime_lookback: float, min_reversion: float, lookback: float,
                     sell_z: float, rebuy_z: float) -> np.ndarray:
    """Hold by default. While the trailing mean-reversion speed says the series is range-bound,
    step aside once price is stretched above its rolling mean and return when it has come back.
    In a trending or random-walk regime the position is never sold."""
    n = int(lookback)
    speed = reversion_speed(s.close, int(regime_lookback))
    m, sd = _rolling_mean(s.close, n), _rolling_std(s.close, n)
    z = np.where(sd > 0, (s.close - m) / np.where(sd > 0, sd, 1.0), 0.0)
    w = np.ones(len(s))
    rich = False
    for i in range(len(s)):
        if not rich and z[i] > sell_z:
            rich = True
        elif rich and z[i] < rebuy_z:
            rich = False
        if rich and speed[i] > min_reversion:
            w[i] = 0.0
    return _to_bps(w)


def buy_hold(s: Series) -> np.ndarray:
    """Passive core: always fully invested."""
    return _to_bps(np.ones(len(s)))


FAMILIES: dict[StrategyFamily, tuple[Callable[..., np.ndarray], dict[str, tuple[float, float]]]] = {
    # family -> (function, allowed parameter bounds)
    StrategyFamily.SMA_CROSSOVER: (sma_crossover, {"fast": (2, 200), "slow": (5, 600)}),
    StrategyFamily.BREAKOUT: (breakout, {"lookback": (5, 400)}),
    StrategyFamily.ZSCORE_REVERSION: (zscore_reversion, {"lookback": (5, 400), "entry_z": (0.25, 4.0), "exit_z": (-2.0, 2.0)}),
    StrategyFamily.VOL_TARGET: (vol_target, {"vol_lookback": (5, 400), "trend_lookback": (5, 800), "target_vol": (0.02, 1.0)}),
    StrategyFamily.RSI_REVERSION: (rsi_reversion, {"lookback": (2, 100), "entry_rsi": (5, 45), "exit_rsi": (40, 85)}),
    StrategyFamily.TS_MOMENTUM: (ts_momentum, {"lookback": (5, 800), "threshold": (-0.05, 0.10)}),
    StrategyFamily.REGIME_REVERSION: (regime_reversion, {"regime_lookback": (30, 600), "min_reversion": (0.0, 0.3),
                                                         "lookback": (5, 400), "sell_z": (0.25, 4.0), "rebuy_z": (-2.0, 2.0)}),
    StrategyFamily.BUY_HOLD: (buy_hold, {}),
}

# Basket families read one signal from the equal-weight index of their universe and apply it to every
# member, so single-name noise does not drown a market-wide regime.
BASKET_FAMILIES = frozenset({StrategyFamily.REGIME_REVERSION})
# Hold families deploy their slot once at cost and are never trimmed while the target stays long.
HOLD_FAMILIES = frozenset({StrategyFamily.REGIME_REVERSION, StrategyFamily.BUY_HOLD})


def validate_params(family: StrategyFamily, params: dict[str, float]) -> None:
    fn, bounds = FAMILIES[family]
    if set(params) != set(bounds):
        raise ValueError(f"{family} expects params {sorted(bounds)}, got {sorted(params)}")
    for k, v in params.items():
        lo, hi = bounds[k]
        if not (isinstance(v, (int, float)) and math.isfinite(v) and lo <= v <= hi):
            raise ValueError(f"{family}.{k}={v} outside [{lo}, {hi}]")
    if family == StrategyFamily.SMA_CROSSOVER and params["fast"] >= params["slow"]:
        raise ValueError("fast must be < slow")
    if family == StrategyFamily.RSI_REVERSION and params["entry_rsi"] >= params["exit_rsi"]:
        raise ValueError("entry_rsi must be < exit_rsi")
    if family == StrategyFamily.REGIME_REVERSION and params["rebuy_z"] >= params["sell_z"]:
        raise ValueError("rebuy_z must be < sell_z")


def max_lookback(family: StrategyFamily, params: dict[str, float]) -> int:
    return int(max(params.get(k, 0) for k in ("slow", "lookback", "vol_lookback", "trend_lookback", "regime_lookback"))) + 1


def signal(family: StrategyFamily, params: dict[str, float], s: Series) -> np.ndarray:
    validate_params(family, params)
    return FAMILIES[family][0](s, **params)


def basket_index(histories: dict[str, tuple[Series, np.ndarray]]) -> tuple[Series, np.ndarray]:
    """Equal-weight index of a universe on the union of its timestamps. Each member is rebased to its
    close at the first timestamp where every member has traded and forward-filled afterwards, so the
    value at a timestamp depends only on bars at or before it."""
    ts = np.unique(np.concatenate([t for _, t in histories.values()])) if histories else np.array([], dtype=np.int64)
    logs = []
    start = 0
    for sym in sorted(histories):
        series, sym_ts = histories[sym]
        if not len(sym_ts):
            return Series(np.array([]), np.array([]), np.array([])), ts[:0]
        idx = np.searchsorted(sym_ts, ts, side="right") - 1  # last bar at or before each timestamp
        start = max(start, int(np.argmax(idx >= 0)))
        logs.append(np.log(series.close[np.maximum(idx, 0)]))
    stacked = np.vstack(logs)[:, start:]
    level = np.exp((stacked - stacked[:, :1]).mean(axis=0))
    return Series(level, level, level), ts[start:]


def universe_weights(family: StrategyFamily, params: dict[str, float],
                     histories: dict[str, tuple[Series, np.ndarray]]) -> dict[str, np.ndarray]:
    """Target weights for every member of a universe, aligned to each member's own timestamps."""
    if family not in BASKET_FAMILIES:
        return {sym: signal(family, params, series) for sym, (series, _) in histories.items()}
    index, index_ts = basket_index(histories)
    w = signal(family, params, index)
    out = {}
    for sym, (series, sym_ts) in histories.items():
        pos = np.searchsorted(index_ts, sym_ts, side="right") - 1
        out[sym] = np.where(pos >= 0, w[np.maximum(pos, 0)], 0) if len(w) else np.zeros(len(sym_ts), dtype=np.int64)
    return out
