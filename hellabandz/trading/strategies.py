"""Runtime strategy agents for the coordinated system.

Three executable strategies, each a reviewed, bounded implementation of an existing whitelisted
family (hellabandz.strategies), plus a research critic (hellabandz.trading.critic) that never trades.

Every bar, a strategy reads a market snapshot and emits one Intent per instrument in its universe.
Indicators are updated incrementally: running sums over bounded windows (with an exact re-sum every
512 updates to stop floating-point drift), not a recomputation over the whole history. On start a
strategy is warmed once with the vectorized family function over the available history, so its
hysteresis state (for example "currently holding after a z-score entry") matches what the research
backtest computed. tests/unit/test_runtime_strategies.py checks the incremental signal against the
vectorized one bar by bar.

LLMs do not run here. Research may propose parameters (bounded by FAMILIES); this module only
executes reviewed code with those parameters.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from hellabandz.contracts import StrategyFamily
from hellabandz.ledger.state import canonical, sha256_hex
from hellabandz.strategies import BARS_PER_YEAR, FAMILIES, Series, signal, validate_params
from hellabandz.trading.intents import Intent
from hellabandz.trading.marketdata import InstrumentView, MarketSnapshot

CODE_VERSION = "runtime-inc-1"  # bump when the incremental implementation changes behavior
REFRESH_EVERY = 512


@dataclass(frozen=True)
class StrategyCard:
    role: str
    family: StrategyFamily
    hypothesis: str
    data: str
    entry: str
    exit: str
    failure_conditions: str
    cost_assumptions: str
    score_meaning: str
    max_slippage_bps: int


COST_NOTE = ("fills at the documented model: bar-sim next open +/- 5 bps, or the paper CLOB synthetic ladder; "
             "fee $0.005/share; turnover is reported and capped by the coordinator")

CARDS: dict[str, StrategyCard] = {
    "trend": StrategyCard(
        "trend", StrategyFamily.SMA_CROSSOVER,
        "Large US ETFs and mega caps trend over weeks; holding while a fast moving average is above a slow one "
        "keeps exposure during those moves and steps aside during sustained declines.",
        "hourly closes for each symbol in the universe",
        "fast simple moving average rises above the slow one",
        "fast simple moving average falls back below the slow one",
        "whipsaw in range-bound markets; when fast and slow are close, costs exceed the edge; late exits after gaps",
        COST_NOTE, "(fast SMA - slow SMA) / slow SMA; positive means uptrend. Not comparable to other strategies.", 30),
    "mean_reversion": StrategyCard(
        "mean_reversion", StrategyFamily.ZSCORE_REVERSION,
        "Single large caps over-react on short horizons; buying when price is far below its rolling mean and "
        "selling once it has come back captures part of the snap-back.",
        "hourly closes for each symbol in the universe",
        "rolling z-score of price falls below -entry_z",
        "rolling z-score rises above exit_z",
        "trending markets where a cheap price keeps getting cheaper; regime changes; earnings gaps",
        COST_NOTE, "-z (how many standard deviations below the rolling mean). Not comparable to other strategies.", 30),
    "defensive": StrategyCard(
        "defensive", StrategyFamily.VOL_TARGET,
        "Exposure sized to a target volatility, and only above a long trend average, should give up less in "
        "turbulent, falling markets than holding a fixed amount.",
        "hourly closes for each symbol in the universe (log returns and a trend average)",
        "price above its trend average; size = target_vol / realized volatility, capped at fully invested",
        "price below its trend average, or volatility so high the size rounds to zero",
        "sharp rebounds after selloffs are missed; volatility estimates lag sudden regime changes",
        COST_NOTE, "target_vol / realized annualized volatility, capped at 1. Not comparable to other strategies.", 30),
}


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    family: StrategyFamily
    params: tuple[tuple[str, float], ...]
    universe: tuple[str, ...]
    version: str
    card: StrategyCard = field(compare=False)

    @property
    def param_dict(self) -> dict[str, float]:
        return dict(self.params)

    def describe(self) -> dict:
        c = self.card
        return {"strategy_id": self.strategy_id, "version": self.version, "family": self.family.value,
                "params": self.param_dict, "universe": list(self.universe), "code_version": CODE_VERSION,
                "hypothesis": c.hypothesis, "data": c.data, "entry": c.entry, "exit": c.exit,
                "parameter_bounds": FAMILIES[self.family][1], "failure_conditions": c.failure_conditions,
                "cost_assumptions": c.cost_assumptions, "score_meaning": c.score_meaning}


def make_version(strategy_id: str, role: str, params: dict[str, float], universe: list[str]) -> StrategyVersion:
    card = CARDS[role]
    validate_params(card.family, params)
    p = tuple(sorted((k, float(v)) for k, v in params.items()))
    u = tuple(sorted(universe))
    h = sha256_hex(canonical({"family": card.family.value, "params": dict(p), "universe": list(u), "code": CODE_VERSION}))
    return StrategyVersion(strategy_id, card.family, p, u, h[:16], card)


class RollingMean:
    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.s = 0.0
        self.k = 0

    def push(self, x: float) -> None:
        if len(self.buf) == self.n:
            self.s -= self.buf[0]
        self.buf.append(x)
        self.s += x
        self.k += 1
        if self.k % REFRESH_EVERY == 0:
            self.s = math.fsum(self.buf)

    @property
    def ready(self) -> bool:
        return len(self.buf) == self.n

    @property
    def value(self) -> float:
        return self.s / self.n


class RollingStd:
    """Population standard deviation (numpy ddof=0) over a window, from shifted running sums."""

    def __init__(self, n: int):
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self.shift = 0.0
        self.s = 0.0
        self.s2 = 0.0
        self.k = 0

    def _resum(self) -> None:
        a = np.fromiter(self.buf, dtype=np.float64)
        self.shift = float(a[0]) if len(a) else 0.0
        d = a - self.shift
        self.s, self.s2 = float(d.sum()), float((d * d).sum())

    def push(self, x: float) -> None:
        if not self.buf:
            self.shift = x
        if len(self.buf) == self.n:
            old = self.buf[0] - self.shift
            self.s -= old
            self.s2 -= old * old
        self.buf.append(x)
        d = x - self.shift
        self.s += d
        self.s2 += d * d
        self.k += 1
        if self.k % REFRESH_EVERY == 0:
            self._resum()

    @property
    def ready(self) -> bool:
        return len(self.buf) == self.n

    @property
    def value(self) -> float:
        var = (self.s2 - self.s * self.s / self.n) / self.n
        return math.sqrt(var) if var > 0 else 0.0


def _bps(w: float) -> int:
    if not math.isfinite(w):
        return 0
    return int(min(max(np.rint(w * 10_000), 0), 10_000))


class _SymbolState:
    def __init__(self) -> None:
        self.last_ts = 0
        self.last_close = 0.0
        self.held = False
        self.weight_bps = 0
        self.score = 0.0
        self.features: dict[str, float] = {}
        self.reasons: list[str] = []
        self.ind: dict[str, object] = {}


class RuntimeStrategy:
    def __init__(self, version: StrategyVersion, venue: str):
        self.v = version
        self.venue = venue
        self.p = version.param_dict
        self.state = {sym: _SymbolState() for sym in version.universe}

    # ------------------------------------------------------------------ indicators

    def _indicators(self) -> dict[str, object]:
        f, p = self.v.family, self.p
        if f == StrategyFamily.SMA_CROSSOVER:
            return {"fast": RollingMean(int(p["fast"])), "slow": RollingMean(int(p["slow"]))}
        if f == StrategyFamily.ZSCORE_REVERSION:
            n = int(p["lookback"])
            return {"mean": RollingMean(n), "std": RollingStd(n)}
        if f == StrategyFamily.VOL_TARGET:
            return {"vol": RollingStd(int(p["vol_lookback"])), "trend": RollingMean(int(p["trend_lookback"]))}
        raise ValueError(f"{f} is not a runtime strategy family")

    def warm(self, sym: str, ts: np.ndarray, close: np.ndarray, high: np.ndarray, low: np.ndarray) -> None:
        """Initialize from history with the vectorized family function (the research definition)."""
        st = self.state[sym]
        st.ind = self._indicators()
        if len(close) == 0:
            return
        w = signal(self.v.family, self.p, Series(close, high, low))
        st.weight_bps = int(w[-1])
        st.held = st.weight_bps > 0
        if self.v.family == StrategyFamily.VOL_TARGET:
            r = np.zeros(len(close))
            r[1:] = np.diff(np.log(close))
            for x in r[-st.ind["vol"].n:]:
                st.ind["vol"].push(float(x))
            for x in close[-st.ind["trend"].n:]:
                st.ind["trend"].push(float(x))
        else:
            for ind in st.ind.values():
                for x in close[-ind.n:]:
                    ind.push(float(x))
        st.last_ts = int(ts[-1])
        st.last_close = float(close[-1])
        self._describe(st, float(close[-1]))

    def _update(self, st: _SymbolState, c: float) -> None:
        f, p, ind = self.v.family, self.p, st.ind
        if f == StrategyFamily.SMA_CROSSOVER:
            ind["fast"].push(c)
            ind["slow"].push(c)
            st.weight_bps = 10_000 if ind["fast"].ready and ind["slow"].ready and ind["fast"].value > ind["slow"].value else 0
        elif f == StrategyFamily.ZSCORE_REVERSION:
            ind["mean"].push(c)
            ind["std"].push(c)
            if ind["mean"].ready:
                sd = ind["std"].value
                z = (c - ind["mean"].value) / sd if sd > 0 else 0.0
                if not st.held and z < -p["entry_z"]:
                    st.held = True
                elif st.held and z > p["exit_z"]:
                    st.held = False
            st.weight_bps = 10_000 if st.held else 0
        else:
            ind["vol"].push(math.log(c / st.last_close) if st.last_close > 0 else 0.0)
            ind["trend"].push(c)
            vol = ind["vol"].value * math.sqrt(BARS_PER_YEAR) if ind["vol"].ready else float("nan")
            above = ind["trend"].ready and c > ind["trend"].value
            st.weight_bps = _bps(min(1.0, p["target_vol"] / vol)) if above and vol > 0 else 0
        st.last_close = c
        self._describe(st, c)

    def _describe(self, st: _SymbolState, c: float) -> None:
        f, ind = self.v.family, st.ind
        if f == StrategyFamily.SMA_CROSSOVER and ind["slow"].ready:
            fa, sl = ind["fast"].value, ind["slow"].value
            st.score, st.features = (fa - sl) / sl, {"sma_fast": fa, "sma_slow": sl}
            st.reasons = ["FAST_ABOVE_SLOW" if fa > sl else "FAST_BELOW_SLOW"]
        elif f == StrategyFamily.ZSCORE_REVERSION and ind["mean"].ready:
            sd = ind["std"].value
            z = (c - ind["mean"].value) / sd if sd > 0 else 0.0
            st.score, st.features = -z, {"zscore": z, "mean": ind["mean"].value, "std": sd}
            st.reasons = ["HOLDING_AFTER_ENTRY" if st.held else ("BELOW_ENTRY" if z < -self.p["entry_z"] else "FLAT")]
        elif f == StrategyFamily.VOL_TARGET and ind["vol"].ready and ind["trend"].ready:
            vol = ind["vol"].value * math.sqrt(BARS_PER_YEAR)
            st.score = min(1.0, self.p["target_vol"] / vol) if vol > 0 else 0.0
            st.features = {"realized_vol": vol, "trend": ind["trend"].value}
            st.reasons = ["ABOVE_TREND" if c > ind["trend"].value else "BELOW_TREND"]
        else:
            st.score, st.features, st.reasons = 0.0, {}, ["WARMING_UP"]

    # ------------------------------------------------------------------ decisions

    def holding_bars(self) -> int:
        p = self.p
        return int(p.get("slow") or p.get("lookback") or p.get("vol_lookback") or 1)

    def on_snapshot(self, snap: MarketSnapshot, interval_s: int = 3600) -> list[Intent]:
        out = []
        for sym in self.v.universe:
            view: InstrumentView | None = snap.instruments.get(sym)
            st = self.state[sym]
            if view is None:
                continue
            if not st.ind:
                st.ind = self._indicators()
            ts = view._ts  # read-only tuple; values are copied below
            k = bisect.bisect_right(ts, st.last_ts)
            for i in range(k, len(ts)):
                self._update(st, float(view._close[i]))
                st.last_ts = int(ts[i])
            out.append(Intent(
                intent_id=f"{self.v.strategy_id}.{self.v.version}.{sym}.{snap.ts}",
                strategy_id=self.v.strategy_id, strategy_version=self.v.version, instrument=sym, venue=self.venue,
                md_seq=snap.gateway_seq, decision_ts=snap.ts, target_weight_bps=st.weight_bps,
                score=round(float(st.score), 8), score_meaning=self.v.card.score_meaning[:200],
                holding_bars=self.holding_bars(), max_slippage_bps=self.v.card.max_slippage_bps,
                expires_ts=snap.ts + interval_s, features={k: round(float(v), 8) for k, v in st.features.items()},
                reasons=list(st.reasons)))
        return out
