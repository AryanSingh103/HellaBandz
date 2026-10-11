"""Training-split market summaries handed to research agents.

Agents only ever see statistics computed on bars before the train/validation
boundary. Validation and test data never reach an LLM prompt.
"""

from __future__ import annotations

import math

import numpy as np

from quantswarm.backtester import SnapshotProvider
from quantswarm.ingestion.dataset import unit_context
from quantswarm.strategies import BARS_PER_YEAR, basket_index, reversion_speed


def basket_summary(p: SnapshotProvider, train_end_ts: int, window: int = 200) -> dict | None:
    """Regime statistics of the equal-weight index of all symbols: is the market range-bound or trending?"""
    index, _ = basket_index({sym: p.history(sym, train_end_ts) for sym in p.symbols()})
    if len(index) <= window + 50:
        return None
    lp = np.log(index.close)
    one = np.diff(lp).var()

    def variance_ratio(k: int) -> float | None:
        return round(float((lp[k:] - lp[:-k]).var() / (k * one)), 4) if one > 0 else None

    speed = reversion_speed(index.close, window)
    speed = speed[~np.isnan(speed)]
    return {"definition": "equal-weight index of all symbols",
            "variance_ratio_20": variance_ratio(20), "variance_ratio_40": variance_ratio(40),
            "reversion_speed_median": round(float(np.median(speed)), 4),
            "share_of_bars_reversion_speed_above_0.03": round(float((speed > 0.03).mean()), 4),
            "reading": f"a variance ratio below 1 and a {window}-bar reversion speed well above {5 / window:.3f} "
                       "(what a random walk produces) indicate range-bound stretches"}


def train_summary(p: SnapshotProvider, train_end_ts: int, metadata: dict | None = None) -> dict:
    out = {}
    for sym in p.symbols():
        s, ts = p.history(sym, train_end_ts)
        c = s.close
        if not len(c):
            raise ValueError(f"no training bars for {sym}")
        r = np.diff(np.log(c))
        sma200 = np.convolve(c, np.ones(200) / 200, mode="valid") if len(c) >= 200 else None
        def autocorr(lag):
            if len(r) <= lag + 1 or not r[:-lag].std() or not r[lag:].std():
                return None
            value = float(np.corrcoef(r[:-lag], r[lag:])[0, 1])
            return round(value, 4) if math.isfinite(value) else None
        peak = np.maximum.accumulate(c)
        out[sym] = {
            "bars": int(len(c)),
            "total_return": round(float(c[-1] / c[0] - 1), 4),
            "annualized_vol": round(float(r.std() * math.sqrt(BARS_PER_YEAR)), 4) if len(r) else None,
            "lag1_autocorr": autocorr(1),
            "lag7_autocorr": autocorr(7),
            "frac_above_sma200": round(float(np.mean(c[199:] > sma200)), 4) if sma200 is not None else None,
            "max_drawdown": round(float(((peak - c) / peak).max()), 4),
        }
    meta = metadata or {}
    return {"split": "train", "interval_s": meta.get("interval_s", 3600), "symbols": out, "basket": basket_summary(p, train_end_ts),
            "units": unit_context(meta), "annualization_bars_per_year": BARS_PER_YEAR,
            "unavailable_statistics": "null means insufficient history or constant returns"}
