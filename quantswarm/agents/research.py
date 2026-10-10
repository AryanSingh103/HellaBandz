"""Training-split market summaries handed to research agents.

Agents only ever see statistics computed on bars before the train/validation
boundary. Validation and test data never reach an LLM prompt.
"""

from __future__ import annotations

import math

import numpy as np

from quantswarm.backtester import SnapshotProvider
from quantswarm.strategies import BARS_PER_YEAR


def train_summary(p: SnapshotProvider, train_end_ts: int) -> dict:
    out = {}
    for sym in p.symbols():
        s, ts = p.history(sym, train_end_ts)
        c = s.close
        r = np.diff(np.log(c))
        sma200 = np.convolve(c, np.ones(200) / 200, mode="valid")
        peak = np.maximum.accumulate(c)
        out[sym] = {
            "bars": int(len(c)),
            "total_return": round(float(c[-1] / c[0] - 1), 4),
            "annualized_vol": round(float(r.std() * math.sqrt(BARS_PER_YEAR)), 4),
            "lag1_autocorr": round(float(np.corrcoef(r[:-1], r[1:])[0, 1]), 4),
            "lag7_autocorr": round(float(np.corrcoef(r[:-7], r[7:])[0, 1]), 4),
            "frac_above_sma200": round(float(np.mean(c[199:] > sma200)), 4),
            "max_drawdown": round(float(((peak - c) / peak).max()), 4),
        }
    return {"split": "train", "interval": "1h", "symbols": out}
