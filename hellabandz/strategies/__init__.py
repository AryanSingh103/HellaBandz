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


FAMILIES: dict[StrategyFamily, tuple[Callable[..., np.ndarray], dict[str, tuple[float, float]]]] = {
    # family -> (function, allowed parameter bounds)
    StrategyFamily.SMA_CROSSOVER: (sma_crossover, {"fast": (2, 200), "slow": (5, 600)}),
    StrategyFamily.BREAKOUT: (breakout, {"lookback": (5, 400)}),
    StrategyFamily.ZSCORE_REVERSION: (zscore_reversion, {"lookback": (5, 400), "entry_z": (0.25, 4.0), "exit_z": (-2.0, 2.0)}),
    StrategyFamily.VOL_TARGET: (vol_target, {"vol_lookback": (5, 400), "trend_lookback": (5, 800), "target_vol": (0.02, 1.0)}),
    StrategyFamily.RSI_REVERSION: (rsi_reversion, {"lookback": (2, 100), "entry_rsi": (5, 45), "exit_rsi": (40, 85)}),
    StrategyFamily.TS_MOMENTUM: (ts_momentum, {"lookback": (5, 800), "threshold": (-0.05, 0.10)}),
}


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


def max_lookback(family: StrategyFamily, params: dict[str, float]) -> int:
    return int(max(params.get(k, 0) for k in ("slow", "lookback", "vol_lookback", "trend_lookback"))) + 1


def signal(family: StrategyFamily, params: dict[str, float], s: Series) -> np.ndarray:
    validate_params(family, params)
    return FAMILIES[family][0](s, **params)
