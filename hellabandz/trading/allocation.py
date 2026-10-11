"""Causal cross-sectional allocation on completed New York session observations.

Hourly vendor timestamps label buckets, including the 09:00 bucket containing
the 09:30 open. The last observed 09:00..15:59 bucket close is the session close
proxy. No exchange calendar is invented: holidays and early closes are represented
by the observations. Decisions use only dates strictly before the current session.
"""
from __future__ import annotations

import bisect
import math
import heapq
import itertools
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from hellabandz.contracts import StrategyFamily

NY = ZoneInfo("America/New_York")


def session_date(ts: int):
    local = datetime.fromtimestamp(int(ts), NY)
    if local.weekday() >= 5 or not 9 <= local.hour < 16:
        return None
    return local.date()


class AllocationState:
    """Bounded daily observations; missing member days cannot leak stale rankings."""

    def __init__(self, version):
        self.version = version
        self.days = {s: deque(maxlen=254) for s in version.universe}
        self.last_ts = {s: 0 for s in version.universe}
        self.weights = {s: 0 for s in version.universe}
        self.scores = {s: 0.0 for s in version.universe}
        self.decision_key = None
        self.allocation_id = "warming"
        self.latest_signal_date = None

    def observe(self, sym: str, ts: int, close: float) -> None:
        if ts <= self.last_ts[sym]:
            return
        self.last_ts[sym] = ts
        day = session_date(ts)
        if day is None or not math.isfinite(close) or close <= 0:
            return
        buf = self.days[sym]
        if buf and buf[-1][0] == day:
            buf[-1] = (day, close)
        else:
            buf.append((day, close))

    def warm(self, histories) -> None:
        def rows(sym):
            ts, close, _, _ = histories.get(sym, ([], [], [], []))
            for t, c in zip(ts, close):
                yield int(t), sym, float(c)
        # Replay calendar decisions as well as indicators. A midweek warm start
        # must retain the allocation selected at that week's first observed session.
        for ts, observations in itertools.groupby(heapq.merge(*(rows(s) for s in sorted(self.days))), key=lambda x: x[0]):
            members = set()
            for t, sym, close in observations:
                self.observe(sym, t, close)
                members.add(sym)
            if members == set(self.days):
                self._rebalance(session_date(ts))

    def update(self, snap) -> None:
        for sym in self.days:
            view = snap.instruments.get(sym)
            if view is None:
                continue
            for i in range(bisect.bisect_right(view._ts, self.last_ts[sym]), len(view._ts)):
                self.observe(sym, int(view._ts[i]), float(view._close[i]))
        day = session_date(snap.ts)
        if day is None:
            return
        # Wait for a fresh observation from every member before freezing this decision.
        if any(s not in snap.instruments or snap.instruments[s].last_ts != snap.ts
               or snap.instruments[s].needs_resync for s in self.days):
            return
        self._rebalance(day)

    def _rebalance(self, day) -> None:
        if day is None:
            return
        p = self.version.param_dict
        key = day.isoformat() if p["rebalance_sessions"] == 1 else "%s-W%02d" % day.isocalendar()[:2]
        if key == self.decision_key:
            return
        histories = {s: {d: c for d, c in buf if d < day} for s, buf in self.days.items()}
        common = sorted(set.intersection(*(set(h) for h in histories.values()))) if histories else []
        n = int(p["lookback_sessions"])
        vn = int(p.get("vol_sessions", 0))
        self.weights = dict.fromkeys(self.days, 0)
        self.scores = dict.fromkeys(self.days, 0.0)
        if len(common) >= max(n, vn) + 1:
            eligible = []
            for sym, hist in histories.items():
                momentum = hist[common[-1]] / hist[common[-n - 1]] - 1
                score = momentum
                if self.version.family == StrategyFamily.RISK_ADJUSTED_ROTATION:
                    closes = np.asarray([hist[d] for d in common[-vn - 1:]])
                    vol = float(np.diff(np.log(closes)).std()) * math.sqrt(252)
                    score = momentum / max(vol, 1e-6)
                self.scores[sym] = score
                if momentum > p["threshold"]:
                    eligible.append(sym)
            selected = sorted(eligible, key=lambda s: (-self.scores[s], s))[:int(p["top_k"])]
            # Missing positive candidates keep their planned slots in cash.
            slots = min(int(p["top_k"]), len(self.days))
            base, extra = divmod(10_000, slots)
            for i, sym in enumerate(selected):
                self.weights[sym] = base + int(i < extra)
            self.latest_signal_date = common[-1].isoformat()
        self.decision_key = key
        self.allocation_id = key
