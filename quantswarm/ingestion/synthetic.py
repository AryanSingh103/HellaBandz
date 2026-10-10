"""Seeded synthetic hourly bars used as the committed, license-free fixture.

Prices follow a regime-switching process (trending vs mean-reverting market
factor plus per-symbol idiosyncratic noise) so that different strategy
families win in different periods. Calendar: weekdays only, 7 regular-session
hourly bars (14:30-20:30 UTC); exchange holidays are not modeled.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np

from quantswarm.ingestion.dataset import BarTable

UNIVERSE = {  # symbol: (start price, beta, idiosyncratic hourly vol, mean volume)
    "SPY": (383.0, 1.0, 0.0012, 3_000_000),
    "QQQ": (266.0, 1.2, 0.0018, 2_500_000),
    "AAPL": (125.0, 1.1, 0.0035, 2_000_000),
    "MSFT": (240.0, 1.05, 0.0032, 1_200_000),
    "NVDA": (14.6, 1.8, 0.0070, 9_000_000),
}


def session_timestamps(start: date, end: date) -> np.ndarray:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            base = datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc)
            out.extend(int((base + timedelta(hours=h)).timestamp()) for h in range(7))
        d += timedelta(days=1)
    return np.array(out, dtype=np.int64)


def generate(seed: int = 42, start: date = date(2023, 1, 3), end: date = date(2026, 9, 30)) -> BarTable:
    rng = np.random.default_rng(seed)
    ts = session_timestamps(start, end)
    n = len(ts)

    # market factor: alternate trending and mean-reverting regimes
    mkt = np.zeros(n)
    i = 0
    level = 0.0
    while i < n:
        length = int(rng.integers(200, 700))
        trending = rng.random() < 0.55
        drift = rng.normal(0, 0.00028) if trending else 0.0
        anchor = level
        for j in range(i, min(n, i + length)):
            shock = rng.normal(0, 0.0042)
            if trending:
                level += drift + shock
            else:
                level += -0.06 * (level - anchor) + shock
            mkt[j] = level
        i += length
    mret = np.diff(mkt, prepend=0.0)

    rows: list[tuple] = []
    for sym, (p0, beta, ivol, vol0) in UNIVERSE.items():
        idio = rng.normal(0, ivol, n)
        logp = np.log(p0) + np.cumsum(beta * mret + idio + 0.00002)
        close = np.exp(logp)
        gap = rng.normal(0, 0.0015, n)
        open_ = np.empty(n)
        open_[0] = p0
        open_[1:] = close[:-1] * (1 + gap[1:])
        wick = np.abs(rng.normal(0, ivol + 0.0015, (2, n)))
        high = np.maximum(open_, close) * (1 + wick[0])
        low = np.minimum(open_, close) * (1 - wick[1])
        volume = (vol0 * rng.lognormal(0, 0.35, n)).astype(np.int64)

        def cents(x: np.ndarray) -> np.ndarray:
            return np.rint(x * 100).astype(np.int64) * 10_000

        o, h, lo, c = cents(open_), cents(high), cents(low), cents(close)
        h = np.maximum(h, np.maximum(o, c))
        lo = np.minimum(lo, np.minimum(o, c))
        rows.extend(zip([sym] * n, ts.tolist(), o.tolist(), h.tolist(), lo.tolist(), c.tolist(), volume.tolist()))
    return BarTable.from_rows(rows)
